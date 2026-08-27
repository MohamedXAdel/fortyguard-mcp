"""
In-flight bookkeeping for submitted-but-not-yet-collected work.

`submit_*` returns an activity_id while the job runs, and `check_status` knows
only that id. The archive is keyed on the REQUEST, so without the original body
a collected result can never be matched to an identical future request - which
then pays for it again.

Separate from `results_store` because the lifetimes are opposite: a stored
result is paid data that is never evicted; a pending record is disposable.
On disk, because the server can restart between submit and collection.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from .results_store import safe_filename


@dataclass(slots=True)
class PendingSubmission:
    activity_id: str
    endpoint: str
    request_body: Any
    submitted_at: str


class PendingStore:
    """One small JSON file per in-flight activity, under `data_dir/pending`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = self.settings.data_dir / "pending"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, activity_id: str) -> Path:
        # `safe_filename` rather than a local sanitiser: it appends a digest when
        # it alters a name, so two long ids cannot collapse onto one file and
        # overwrite each other's request body.
        return self.root / f"{safe_filename(activity_id)}.json"

    def remember(self, activity_id: str, endpoint: str, request_body: Any) -> None:
        """
        Record a submission, before any wait — so a timeout, a cancellation or a
        killed process still leaves the request recoverable.

        Never raises: failing to write bookkeeping must not fail a call whose
        real work was already paid for. The cost is a future cache miss.
        """
        with contextlib.suppress(OSError, TypeError, ValueError):
            self._path(activity_id).write_text(
                json.dumps({
                    "activity_id": activity_id,
                    "endpoint": endpoint,
                    "request_body": request_body,
                    "submitted_at": datetime.now(UTC).isoformat(),
                }),
                encoding="utf-8",
            )

    def recall(self, activity_id: str) -> PendingSubmission | None:
        """
        The record for one in-flight submission, or None if it cannot be read.

        Unreadable counts as absent; the cost is one cache miss. The isinstance
        check is not redundant with the `except`: `[]` and `null` are valid
        JSON, and indexing them raises `TypeError`, not `KeyError`.
        """
        p = self._path(activity_id)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict):
            return None
        try:
            return PendingSubmission(
                activity_id=str(d["activity_id"]),
                endpoint=str(d["endpoint"]),
                request_body=d.get("request_body"),
                submitted_at=str(d.get("submitted_at", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def forget(self, activity_id: str) -> None:
        """Drop the record once the result is archived. Missing is fine."""
        with contextlib.suppress(OSError):
            self._path(activity_id).unlink(missing_ok=True)

    def count(self) -> int:
        return sum(1 for _ in self.root.glob("*.json"))
