"""
Campaign recorder for Phase A0 — the API Truth Campaign.

Captures the COMPLETE HTTP exchange for every call: the submit response, every
intermediate poll response, timing, and the credit delta. Fixtures written here
are the durable asset of the whole project — every later test replays them
offline, so they must carry the full envelope, not just `data.result`.

Design rules (from the approved plan):
  * Record the full wire envelope: {error, status_code, message, data:{status, result}}.
    Fixtures holding only `result` leave the polling state machine untestable.
  * Record EVERY poll, not just the terminal one. Backoff and terminal-detection
    tests are only real if intermediate states are present.
  * Resumable. A case whose fixture already exists is skipped, so no measurement
    is ever paid for twice.
  * Never persist secrets. The api-key header is redacted and signed
    download_links are scrubbed before writing.
  * Never assume a terminal vocabulary. Unrecognised statuses are treated as
    non-terminal and recorded verbatim (resolves T0.2).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"

load_dotenv(REPO_ROOT / ".env")

BASE_URL = os.environ.get("FORTYGUARD_BASE_URL", "https://api.fortyguard.com").rstrip("/")
API_KEY = os.environ.get("FORTYGUARD_API_KEY")

USAGE_PATH = "/v1/system/fetch-api-key-usage"
STATUS_PATH = "/v1/status/{activity_id}"

# Verified terminal values. T0.2 may add more; anything unrecognised is treated
# as non-terminal and surfaced loudly rather than guessed at.
KNOWN_TERMINAL_SUCCESS = {"completed", "succeeded", "success"}
KNOWN_TERMINAL_FAILURE = {"failed", "error"}

POLL_INTERVAL_S = 2.0
POLL_CEILING = 150  # 150 * 2s = 5 minutes; heat_intelligence may need this

# Redaction.
# NOTE: a generic "url" key used to be in this set and clobbered each fixture's
# own `request.url`, which broke replay. Only keys that actually carry signed
# download URLs belong here.
_DOWNLOAD_LINK_KEYS = {"download_link", "downloadLink", "signed_url", "signedUrl"}
_SECRET_VALUE_PATTERNS = [re.compile(re.escape(API_KEY))] if API_KEY else []


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

def scrub(obj: Any) -> Any:
    """Recursively remove secrets from anything destined for disk."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _DOWNLOAD_LINK_KEYS and isinstance(v, str) and v.startswith("http"):
                out[k] = f"<REDACTED_SIGNED_URL len={len(v)} host={_host_of(v)}>"
            else:
                out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        for pat in _SECRET_VALUE_PATTERNS:
            obj = pat.sub("<REDACTED_API_KEY>", obj)
        return obj
    return obj


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else "?"


def _truncate_base64(obj: Any, limit: int = 512) -> Any:
    """
    Base64 imagery would bloat fixtures into the megabytes. Keep a prefix plus the
    measured length — enough to test the decode path and to answer U14 (payload
    sizing) without storing the whole image.
    """
    if isinstance(obj, dict):
        return {k: _truncate_base64(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_base64(v, limit) for v in obj]
    if isinstance(obj, str) and len(obj) > limit and _looks_base64(obj):
        return f"<BASE64 len={len(obj)} head={obj[:64]}>"
    return obj


def _looks_base64(s: str) -> bool:
    sample = s[:256].removeprefix("data:image/png;base64,").removeprefix("data:image/jpeg;base64,")
    return bool(sample) and all(c.isalnum() or c in "+/=\n\r" for c in sample)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #

def _headers() -> dict[str, str]:
    if not API_KEY:
        raise SystemExit(
            "FORTYGUARD_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return {"api-key": API_KEY, "Content-Type": "application/json"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_or_text(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"<non-json-body>": resp.text[:4000]}


# --------------------------------------------------------------------------- #
# Credit probing (T0.1 / T0.4)
# --------------------------------------------------------------------------- #

# T0.1 VERIFIED schema. Ordered most- to least-specific; the first two are the
# real keys, the rest are defensive in case the shape shifts.
_BALANCE_KEY_CANDIDATES = [
    ("credit_summary", "cycle_remaining_credits"),
    ("credit_summary", "total_remaining_credits"),
    ("data", "credit_summary", "cycle_remaining_credits"),
    ("remaining_credits",),
    ("balance",),
]


def probe_breakdown() -> dict[str, dict[str, int]]:
    """
    Per-endpoint cumulative credits AND call counts, from `activity_breakdown`.

    This is a strictly better cost instrument than total-balance bracketing: it
    attributes spend to a specific endpoint, so a concurrent call elsewhere
    cannot contaminate the measurement. Returns {name: {credits, count}}.
    """
    raw, _ = probe_usage()
    if raw.get("accepted_method") != "POST":
        return {}
    body = raw["attempts"][0].get("body") or {}
    out: dict[str, dict[str, int]] = {}
    for row in body.get("activity_breakdown", []) or []:
        name = row.get("name")
        if not name or name == "Unused Credits":
            continue
        out[name] = {"credits": int(row.get("credits", 0)),
                     "count": int(row.get("count", 0))}
    return out


def breakdown_delta(before: dict, after: dict) -> dict[str, dict[str, int]]:
    """Exact per-endpoint cost and call-count change between two probes."""
    names = set(before) | set(after)
    delta = {}
    for n in names:
        b = before.get(n, {"credits": 0, "count": 0})
        a = after.get(n, {"credits": 0, "count": 0})
        dc, dn = a["credits"] - b["credits"], a["count"] - b["count"]
        if dc or dn:
            delta[n] = {"credits": dc, "count": dn,
                        "per_call": (dc // dn) if dn else None}
    return delta


def probe_usage() -> tuple[Any, int | None]:
    """
    Returns (raw_response_body, extracted_balance_or_None).

    T0.1 VERIFIED: POST only (GET -> 405), and the api key must appear in the
    BODY as `api_key`, not merely in the header. The endpoint told us so itself:
        422 {"error":true,"message":"Field 'api_key' is required.","field":"api_key"}
    """
    url = f"{BASE_URL}{USAGE_PATH}"
    attempts: list[dict[str, Any]] = []

    try:
        resp = requests.post(
            url, headers=_headers(), json={"api_key": API_KEY}, timeout=30
        )
        body = _json_or_text(resp)
        attempts.append({"method": "POST", "status": resp.status_code, "body": body})
        if resp.status_code == 200:
            return {"attempts": attempts, "accepted_method": "POST"}, _extract_balance(body)
    except requests.RequestException as e:
        attempts.append({"method": "POST", "error": repr(e)})

    return {"attempts": attempts, "accepted_method": None}, None


def _extract_balance(body: Any) -> int | None:
    for path in _BALANCE_KEY_CANDIDATES:
        cur = body
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if isinstance(cur, (int, float)):
            return int(cur)
    return None


# --------------------------------------------------------------------------- #
# The recorder
# --------------------------------------------------------------------------- #

@dataclass
class CaseResult:
    case_id: str
    skipped: bool = False
    submit_status: int | None = None
    activity_id: str | None = None
    terminal_status: str | None = None
    duration_s: float | None = None
    poll_count: int = 0
    credits_delta: int | None = None
    statuses_seen: list[str] = field(default_factory=list)
    error: str | None = None

    def line(self) -> str:
        if self.skipped:
            return f"  [skip] {self.case_id}"
        if self.error:
            return f"  [ERR ] {self.case_id}: {self.error}"
        cost = "?" if self.credits_delta is None else str(self.credits_delta)
        dur = "?" if self.duration_s is None else f"{self.duration_s:.1f}s"
        return (
            f"  [ok  ] {self.case_id}: submit={self.submit_status} "
            f"terminal={self.terminal_status} in {dur} "
            f"polls={self.poll_count} credits={cost}"
        )


class Recorder:
    def __init__(self, measure_credits: bool = True) -> None:
        self.measure_credits = measure_credits
        FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)

    def fixture_path(self, endpoint: str, case: str) -> Path:
        safe = endpoint.strip("/").replace("/", "_")
        return FIXTURE_ROOT / safe / f"{case}.json"

    # -- synchronous (non-activity) calls ---------------------------------- #

    def record_simple(self, case: str, endpoint: str, method: str, payload: Any) -> CaseResult:
        """For endpoints that return directly, with no activity_id to poll."""
        path = self.fixture_path(endpoint, case)
        if path.exists():
            return CaseResult(case, skipped=True)

        url = f"{BASE_URL}{endpoint}"
        started = _now()
        t0 = time.monotonic()
        try:
            resp = requests.request(method, url, headers=_headers(), json=payload, timeout=60)
        except requests.RequestException as e:
            return CaseResult(case, error=repr(e))
        body = _json_or_text(resp)

        self._write(path, {
            "case": case,
            "kind": "simple",
            "request": {
                "method": method, "url": url,
                "headers_redacted": ["api-key"], "body": payload,
            },
            "response": {"status": resp.status_code, "body": body},
            "timing": {"started_at": started, "duration_s": round(time.monotonic() - t0, 3)},
        })
        return CaseResult(case, submit_status=resp.status_code,
                          duration_s=round(time.monotonic() - t0, 3))

    # -- asynchronous submit-then-poll calls -------------------------------- #

    def record_async(
        self, case: str, endpoint: str, payload: Any, poll_ceiling: int | None = None
    ) -> CaseResult:
        """
        Submit, then poll to terminal, recording every intermediate response.
        This is the shape that makes the client's poll loop testable offline.

        `poll_ceiling` overrides the default. Error-taxonomy cases use a short
        ceiling: a request that neither rejects at submit nor fails quickly is
        itself the finding, and there is no reason to wait minutes to learn it.
        """
        ceiling = poll_ceiling if poll_ceiling is not None else POLL_CEILING
        path = self.fixture_path(endpoint, case)
        if path.exists():
            return CaseResult(case, skipped=True)

        url = f"{BASE_URL}{endpoint}"
        result = CaseResult(case)

        credits_before = None
        if self.measure_credits:
            _, credits_before = probe_usage()

        submitted_at = _now()
        t0 = time.monotonic()
        try:
            resp = requests.post(url, headers=_headers(), json=payload, timeout=60)
        except requests.RequestException as e:
            result.error = repr(e)
            return result

        submit_body = _json_or_text(resp)
        result.submit_status = resp.status_code

        activity_id = _dig(submit_body, "data", "activity_id")
        result.activity_id = activity_id

        polls: list[dict[str, Any]] = []
        terminal_status: str | None = None

        if activity_id:
            status_url = f"{BASE_URL}{STATUS_PATH.format(activity_id=activity_id)}"
            for _ in range(ceiling):
                time.sleep(POLL_INTERVAL_S)
                try:
                    pr = requests.get(status_url, headers=_headers(), timeout=60)
                except requests.RequestException as e:
                    polls.append({"elapsed_s": round(time.monotonic() - t0, 3),
                                  "error": repr(e)})
                    continue
                pbody = _json_or_text(pr)
                status = _dig(pbody, "data", "status")
                polls.append({
                    "elapsed_s": round(time.monotonic() - t0, 3),
                    "status_code": pr.status_code,
                    "body": pbody,
                })
                if isinstance(status, str):
                    if status not in result.statuses_seen:
                        result.statuses_seen.append(status)
                    low = status.strip().lower()
                    if low in KNOWN_TERMINAL_SUCCESS or low in KNOWN_TERMINAL_FAILURE:
                        terminal_status = status
                        break
                    # Unrecognised: NOT treated as terminal. Recorded verbatim.

        result.poll_count = len(polls)
        result.terminal_status = terminal_status
        result.duration_s = round(time.monotonic() - t0, 3)

        credits_after = None
        if self.measure_credits:
            _, credits_after = probe_usage()
        if credits_before is not None and credits_after is not None:
            result.credits_delta = credits_before - credits_after

        self._write(path, {
            "case": case,
            "kind": "async",
            "request": {
                "method": "POST", "url": url,
                "headers_redacted": ["api-key"], "body": payload,
            },
            "submit_response": {"status": resp.status_code, "body": submit_body},
            "poll_responses": polls,
            "timing": {
                "submitted_at": submitted_at,
                "terminal_at": _now() if terminal_status else None,
                "poll_count": len(polls),
                "poll_interval_s": POLL_INTERVAL_S,
                "poll_ceiling": ceiling,
                "duration_s": result.duration_s,
                "reached_terminal": terminal_status is not None,
            },
            "credits": {
                "before": credits_before,
                "after": credits_after,
                "delta": result.credits_delta,
            },
            "observed_statuses": result.statuses_seen,
        })
        return result

    # -- writing ------------------------------------------------------------ #

    def _write(self, path: Path, doc: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc["recorded_at"] = _now()
        cleaned = _truncate_base64(scrub(doc))
        path.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")

        # Belt and braces: a fixture must never contain the key.
        if API_KEY and API_KEY in path.read_text(encoding="utf-8"):
            path.unlink()
            raise RuntimeError(f"API key leaked into {path}; fixture deleted")


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def new_activity_uuid() -> str:
    """For T0.3 — a well-formed id that should not exist."""
    return str(uuid.uuid4())
