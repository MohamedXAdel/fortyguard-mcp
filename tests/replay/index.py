"""
Fixture index — maps incoming requests to recorded exchanges.

Two lookup paths, because the API is asynchronous:

  (method, path, canonical_body_hash) -> fixture     for submits
  activity_id                          -> fixture     for status polls

The canonical hash normalises key ordering and float precision so a request
reconstructed by the client matches the one the recorder sent, even if the
serialisation differs. This is deliberately simpler than the production cache
key (Phase 5), which additionally normalises equivalent polygon
representations — rotated rings, winding direction, duplicate closing points.
Replay only needs to recognise requests our own code emits.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"

FLOAT_PRECISION = 7


def _canonical(obj: Any) -> Any:
    """Stable form: sorted keys, floats rounded, so serialisation quirks vanish."""
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    if isinstance(obj, float):
        r = round(obj, FLOAT_PRECISION)
        return int(r) if r == int(r) else r
    if isinstance(obj, int) and not isinstance(obj, bool):
        return obj
    return obj


def body_hash(body: Any) -> str:
    blob = json.dumps(_canonical(body), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def request_key(method: str, path: str, body: Any) -> str:
    return f"{method.upper()} {path} {body_hash(body)}"


@dataclass
class Fixture:
    case: str
    path: str
    method: str
    request_body: Any
    submit_status: int
    submit_body: Any
    polls: list[dict] = field(default_factory=list)
    activity_id: str | None = None
    source: Path | None = None

    @property
    def is_async(self) -> bool:
        return self.activity_id is not None and bool(self.polls)

    @property
    def terminal_body(self) -> Any:
        return self.polls[-1]["body"] if self.polls else None

    @property
    def final_status(self) -> str | None:
        body = self.terminal_body
        return _dig(body, "data", "status") if body else None

    @property
    def reached_terminal(self) -> bool:
        """
        False for the fixtures that never finished — an over-cap AOI and a
        pre-2021 date were both still `Processing` when recording stopped.
        Replay reproduces that faithfully, so anything expecting completion must
        filter these out.
        """
        s = (self.final_status or "").strip().lower()
        return s in {"completed", "succeeded", "success", "failed", "error"}


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def load_fixture(p: Path) -> Fixture | None:
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    req = doc.get("request") or {}
    url = req.get("url", "")
    if not url:
        return None

    # `record_simple` writes `response`; `record_async` writes `submit_response`.
    sub = doc.get("submit_response") or doc.get("response") or {}

    return Fixture(
        case=doc.get("case", p.stem),
        path=urlsplit(url).path,
        method=req.get("method", "POST"),
        request_body=req.get("body"),
        submit_status=sub.get("status", 0),
        submit_body=sub.get("body"),
        polls=doc.get("poll_responses") or [],
        activity_id=_dig(sub.get("body"), "data", "activity_id"),
        source=p,
    )


class FixtureIndex:
    """
    One request key can legitimately map to SEVERAL recordings: the determinism
    test issued the same request three times on purpose, and two forecast probes
    coincidentally landed on the same timestamp. Each recording has its own
    activity_id, so the index keeps a list rather than silently overwriting.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or FIXTURE_ROOT
        self.by_request: dict[str, list[Fixture]] = {}
        self.by_activity: dict[str, Fixture] = {}
        self.fixtures: list[Fixture] = []
        self._load()

    def _load(self) -> None:
        for p in sorted(self.root.rglob("*.json")):
            fx = load_fixture(p)
            if fx is None:
                continue
            self.fixtures.append(fx)
            self.by_request.setdefault(
                request_key(fx.method, fx.path, fx.request_body), []).append(fx)
            if fx.activity_id:
                self.by_activity[fx.activity_id] = fx

    def match(self, method: str, path: str, body: Any) -> Fixture | None:
        """First recording for this request. Deterministic across runs."""
        group = self.by_request.get(request_key(method, path, body))
        return group[0] if group else None

    def matches(self, method: str, path: str, body: Any) -> list[Fixture]:
        return list(self.by_request.get(request_key(method, path, body), []))

    def duplicate_groups(self) -> list[list[Fixture]]:
        return [g for g in self.by_request.values() if len(g) > 1]

    def by_id(self, activity_id: str) -> Fixture | None:
        return self.by_activity.get(activity_id)

    def coverage(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for fx in self.fixtures:
            out[fx.path] = out.get(fx.path, 0) + 1
        return dict(sorted(out.items()))

    def __len__(self) -> int:
        return len(self.fixtures)
