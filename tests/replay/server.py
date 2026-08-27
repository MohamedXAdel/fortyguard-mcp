"""
Replay server — serves the recorded fixtures over real HTTP.

This is what makes Phases 3-7 testable offline, deterministically, and for free.
The client under test talks real HTTP to a real socket; only the far end is
recorded rather than live. Nothing is mocked, so the transport, the poll loop,
timeouts, and error parsing are all genuinely exercised.

Modes:
  normal     replay the recorded exchange, including intermediate Processing polls
  slow       every response delayed, to exercise backoff and timeouts
  fault      500s, malformed JSON, dropped connections, never-terminal status
  exhausted  credit-exhaustion responses (cannot be recorded live without
             burning 1.7M credits, so it is synthesised here)

The request log is a first-class feature: the Phase 5 cache gate asserts a warm
run issues ZERO requests, measured here rather than from a mock.

    from tests.replay import ReplayServer
    with ReplayServer() as srv:
        ...  # srv.base_url
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .index import FixtureIndex

MODES = ("normal", "slow", "fault", "exhausted")

USAGE_PATH = "/v1/system/fetch-api-key-usage"
STATUS_PREFIX = "/v1/status/"

# Mirrors the real shape measured in T0.1, with balances left round.
USAGE_BODY: dict[str, Any] = {
    "api_key": None,
    "subscription_id": "sub_replay",
    "plan_details": {"plan_type": "Hackathon", "cycle_type": "Hackathon",
                     "active": True, "credits_reset_date": "Sep 26, 2026"},
    "api_key_details": {"status": "active", "valid": True,
                        "api_access_available": True},
    "credit_summary": {"total_available_credits": 2000000,
                       "cycle_credits_used": 100000,
                       "cycle_remaining_credits": 1900000,
                       "cycle_usage_percentage": 5.0,
                       "total_credits_used": 100000,
                       "total_remaining_credits": 1900000},
    "activity_breakdown": [
        {"name": "Heatmap Generation", "credits": 42200, "count": 10, "percentage": 2.11},
        {"name": "Unused Credits", "credits": 1900000, "count": 0, "percentage": 95.0},
    ],
}

EXHAUSTED_BODY = {"error": True, "status_code": 402,
                  "details": {"message": "Insufficient credits for this request."}}


@dataclass
class LoggedRequest:
    method: str
    path: str
    body: Any
    matched: str | None = None


@dataclass
class ReplayState:
    mode: str = "normal"
    slow_delay_s: float = 0.5
    fault_kind: str = "http_500"     # http_500 | malformed | drop | never_terminal
    requests: list[LoggedRequest] = field(default_factory=list)
    poll_cursor: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(BaseHTTPRequestHandler):
    index: FixtureIndex
    state: ReplayState

    # Silence per-request logging to stderr during tests.
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # -- plumbing ---------------------------------------------------------- #

    def _read_body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except ValueError:
            return {"<unparseable>": raw[:200].decode("utf-8", "replace")}

    def _send(self, status: int, body: Any) -> None:
        blob = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _send_raw(self, status: int, raw: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # -- fault injection --------------------------------------------------- #

    def _maybe_fault(self) -> bool:
        """Returns True if the fault was applied and the response is finished."""
        st = self.state
        if st.mode != "fault":
            return False
        kind = st.fault_kind
        if kind == "http_500":
            self._send(500, {"error": True, "status_code": 500,
                             "details": {"message": "Internal server error."}})
            return True
        if kind == "malformed":
            self._send_raw(200, b'{"error": false, "data": {"status": "Comp')
            return True
        if kind == "drop":
            # Close without a response: the client must survive a dead socket.
            try:
                self.close_connection = True
                self.wfile.close()
            except OSError:
                pass
            return True
        # never_terminal is handled in the status branch, not here.
        return False

    def _delay(self) -> None:
        if self.state.mode == "slow":
            time.sleep(self.state.slow_delay_s)

    # -- routing ----------------------------------------------------------- #

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._read_body()
        with self.state.lock:
            self.state.requests.append(LoggedRequest("POST", path, body))
        self._delay()

        if path == USAGE_PATH:
            if isinstance(body, dict) and "api_key" not in body:
                self._send(422, {"error": True, "status_code": 422,
                                 "message": "Field 'api_key' is required.",
                                 "field": "api_key"})
                return
            self._send(200, USAGE_BODY)
            return

        if self.state.mode == "exhausted":
            self._send(402, EXHAUSTED_BODY)
            return
        if self._maybe_fault():
            return

        fx = self.index.match("POST", path, body)
        if fx is None:
            self._send(404, {"error": True, "status_code": 404,
                             "details": {"message":
                                         "No fixture recorded for this request. "
                                         "Record it in Phase A0 before relying on it."}})
            return
        with self.state.lock:
            self.state.requests[-1].matched = fx.case
            if fx.activity_id:
                self.state.poll_cursor.setdefault(fx.activity_id, 0)
        self._send(fx.submit_status, fx.submit_body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        with self.state.lock:
            self.state.requests.append(LoggedRequest("GET", path, None))
        self._delay()

        if not path.startswith(STATUS_PREFIX):
            self._send(404, {"error": True, "status_code": 404,
                             "details": {"message": "Unknown path."}})
            return

        if self.state.mode == "exhausted":
            self._send(402, EXHAUSTED_BODY)
            return
        if self._maybe_fault():
            return

        activity_id = path[len(STATUS_PREFIX):]
        fx = self.index.by_id(activity_id)
        if fx is None:
            # Matches the real 404 measured in T0.3.
            self._send(404, {"error": True, "status_code": 404,
                             "details": {"message":
                                         "No activity found for the provided activity_id."}})
            return

        with self.state.lock:
            self.state.requests[-1].matched = fx.case
            i = self.state.poll_cursor.get(activity_id, 0)
            # never_terminal: pin to the first non-terminal poll forever, which
            # is exactly what the real API does for over-cap AOIs and pre-2021
            # dates: no terminal state is ever reached.
            if self.state.mode == "fault" and self.state.fault_kind == "never_terminal":
                idx = 0
            else:
                idx = min(i, len(fx.polls) - 1)
                self.state.poll_cursor[activity_id] = i + 1

        poll = fx.polls[idx]
        self._send(poll.get("status_code", 200), poll.get("body"))


class ReplayServer:
    """Threaded HTTP server serving recorded fixtures. Use as a context manager."""

    def __init__(self, fixture_root: Path | None = None, mode: str = "normal",
                 port: int = 0) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.index = FixtureIndex(fixture_root)
        self.state = ReplayState(mode=mode)
        handler = type("_BoundHandler", (_Handler,),
                       {"index": self.index, "state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> ReplayServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> ReplayServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- test affordances --------------------------------------------------- #

    def set_mode(self, mode: str, fault_kind: str | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        with self.state.lock:
            self.state.mode = mode
            if fault_kind:
                self.state.fault_kind = fault_kind

    def reset_log(self) -> None:
        with self.state.lock:
            self.state.requests.clear()

    def reset_cursors(self) -> None:
        with self.state.lock:
            self.state.poll_cursor.clear()

    @property
    def request_count(self) -> int:
        with self.state.lock:
            return len(self.state.requests)

    def requests(self, exclude_usage: bool = True) -> list[LoggedRequest]:
        with self.state.lock:
            rs = list(self.state.requests)
        if exclude_usage:
            rs = [r for r in rs if r.path != USAGE_PATH]
        return rs
