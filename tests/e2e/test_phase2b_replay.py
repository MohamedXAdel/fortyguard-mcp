"""
Phase 2b gate — the replay server is faithful to what was recorded.

Everything downstream trusts this. If replay diverges from the recording, every
later test is measuring fiction, so these assertions are deliberately strict.

    python -m pytest tests/e2e/test_phase2b_replay.py -v
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.replay import FixtureIndex, ReplayServer

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def index() -> FixtureIndex:
    return FixtureIndex()


@pytest.fixture(scope="module")
def server():
    with ReplayServer() as srv:
        yield srv


# --------------------------------------------------------------------------- #
# Library integrity — the fixtures themselves
# --------------------------------------------------------------------------- #

def test_library_is_populated(index: FixtureIndex) -> None:
    assert len(index) >= 45, f"expected the A0 library, found {len(index)}"


def test_every_endpoint_covered(index: FixtureIndex) -> None:
    cov = index.coverage()
    for path in ("/v1/heatmap", "/v1/env_params", "/v1/satellite",
                 "/v1/streetview", "/v1/heat_intelligence"):
        assert cov.get(path, 0) > 0, f"no fixture for {path}"


def test_fixtures_carry_full_envelope(index: FixtureIndex) -> None:
    """
    The whole point of the recorder: a fixture holding only `data.result` leaves
    the polling state machine untestable. Any successful submit must carry both
    an activity_id and at least one poll.
    """
    offenders = [
        fx.case for fx in index.fixtures
        if fx.submit_status == 200 and fx.activity_id and not fx.polls
    ]
    assert not offenders, f"fixtures with no recorded polls: {offenders}"


def test_intermediate_polls_were_recorded(index: FixtureIndex) -> None:
    """
    Backoff and terminal-detection tests are only real if a non-terminal state
    was captured. At least some fixtures must show Processing before Completed.
    """
    with_intermediate = [
        fx.case for fx in index.fixtures
        if len(fx.polls) > 1
        and (fx.polls[0].get("body", {}).get("data") or {}).get("status") == "Processing"
    ]
    assert with_intermediate, "no fixture captured a Processing->Completed transition"


def test_no_secrets_in_fixtures() -> None:
    """Belt and braces: the recorder scrubs, but assert it independently."""
    env = FIXTURE_ROOT.parents[1] / ".env"
    key = None
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("FORTYGUARD_API_KEY="):
                key = line.split("=", 1)[1].strip()
    leaked, signed = [], []
    for p in FIXTURE_ROOT.rglob("*.json"):
        text = p.read_text(encoding="utf-8")
        if key and key in text:
            leaked.append(p.name)
        if "X-Goog-Signature" in text or "AWSAccessKeyId" in text:
            signed.append(p.name)
    assert not leaked, f"API key leaked into: {leaked}"
    assert not signed, f"signed URL leaked into: {signed}"


# --------------------------------------------------------------------------- #
# Round trip — replay returns byte-identical responses
# --------------------------------------------------------------------------- #

def test_submit_round_trip_is_byte_identical(server: ReplayServer,
                                             index: FixtureIndex) -> None:
    """
    Replay must return exactly what was recorded. Where several recordings share
    a request body, any of them is a correct answer — they differ only by
    activity_id.
    """
    checked = 0
    for fx in index.fixtures:
        if fx.method != "POST":
            continue
        group = index.matches("POST", fx.path, fx.request_body)
        r = httpx.post(f"{server.base_url}{fx.path}", json=fx.request_body,
                          timeout=10)
        assert r.status_code == fx.submit_status, f"{fx.case}: status mismatch"
        assert r.json() in [g.submit_body for g in group], \
            f"{fx.case}: body matched no recording in its group"
        checked += 1
    assert checked >= 45


def test_duplicate_requests_recorded_identical_results(index: FixtureIndex) -> None:
    """
    Determinism, re-asserted offline. Fixtures sharing a request body must carry
    identical terminal results — differing only in activity_id, which is
    per-submission. This is the property the Phase 5 cache depends on.
    """
    groups = index.duplicate_groups()
    assert groups, "expected the determinism trio in the library"

    for group in groups:
        completed = [g for g in group if g.terminal_body]
        if len(completed) < 2:
            continue

        def payload(fx):
            result = ((fx.terminal_body or {}).get("data") or {}).get("result") or {}
            feats = (result.get("map_data") or {}).get("features", [])
            stats = dict(result.get("stats_data") or {})
            stats.pop("activity_id", None)   # per-submission, not part of the answer
            return feats, stats

        first, rest = payload(completed[0]), [payload(g) for g in completed[1:]]
        for other, fx in zip(rest, completed[1:], strict=True):
            assert other == first, (
                f"{fx.case} differs from {completed[0].case} for an identical request")


def test_poll_sequence_replays_in_order(server: ReplayServer,
                                        index: FixtureIndex) -> None:
    """Every recorded poll comes back in sequence, terminal state last."""
    server.reset_cursors()
    fx = next(f for f in index.fixtures
              if f.activity_id and len(f.polls) > 2)
    for expected in fx.polls:
        r = httpx.get(f"{server.base_url}/v1/status/{fx.activity_id}",
                         timeout=10)
        assert r.json() == expected["body"]
    # Past the end it pins to the terminal response rather than erroring.
    r = httpx.get(f"{server.base_url}/v1/status/{fx.activity_id}", timeout=10)
    assert r.json() == fx.polls[-1]["body"]


def test_unknown_activity_matches_real_404(server: ReplayServer) -> None:
    r = httpx.get(f"{server.base_url}/v1/status/00000000-dead-beef-0000-000000000000",
                     timeout=10)
    assert r.status_code == 404
    assert r.json()["details"]["message"] == \
        "No activity found for the provided activity_id."


def test_unrecorded_request_is_flagged_not_faked(server: ReplayServer) -> None:
    """A miss must be loud. Silently inventing a response would be worse than failing."""
    r = httpx.post(f"{server.base_url}/v1/heatmap",
                      json={"polygon_aoi": {"type": "Nonsense"}}, timeout=10)
    assert r.status_code == 404
    assert "No fixture recorded" in r.json()["details"]["message"]


# --------------------------------------------------------------------------- #
# Usage endpoint — reproduces the T0.1 quirk
# --------------------------------------------------------------------------- #

def test_usage_requires_api_key_in_body(server: ReplayServer) -> None:
    bad = httpx.post(f"{server.base_url}/v1/system/fetch-api-key-usage",
                        json={}, timeout=10)
    assert bad.status_code == 422
    assert bad.json()["field"] == "api_key"

    ok = httpx.post(f"{server.base_url}/v1/system/fetch-api-key-usage",
                       json={"api_key": "anything"}, timeout=10)
    assert ok.status_code == 200
    assert ok.json()["credit_summary"]["cycle_remaining_credits"] > 0


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def test_fault_mode_http_500(server: ReplayServer, index: FixtureIndex) -> None:
    fx = index.fixtures[0]
    server.set_mode("fault", "http_500")
    try:
        r = httpx.post(f"{server.base_url}{fx.path}", json=fx.request_body,
                          timeout=10)
        assert r.status_code == 500
    finally:
        server.set_mode("normal")


def test_fault_mode_malformed_json(server: ReplayServer, index: FixtureIndex) -> None:
    fx = index.fixtures[0]
    server.set_mode("fault", "malformed")
    try:
        r = httpx.post(f"{server.base_url}{fx.path}", json=fx.request_body,
                          timeout=10)
        with pytest.raises(ValueError):
            r.json()
    finally:
        server.set_mode("normal")


def test_fault_mode_never_terminal(server: ReplayServer, index: FixtureIndex) -> None:
    """
    The real API leaves over-cap AOIs and
    pre-2021 dates in Processing forever. The client must survive this.
    """
    fx = next(f for f in index.fixtures if f.activity_id and len(f.polls) > 1)
    server.set_mode("fault", "never_terminal")
    try:
        for _ in range(5):
            r = httpx.get(f"{server.base_url}/v1/status/{fx.activity_id}",
                             timeout=10)
            status = (r.json().get("data") or {}).get("status")
            assert status != "Completed", "never_terminal mode reached a terminal state"
    finally:
        server.set_mode("normal")
        server.reset_cursors()


def test_exhausted_mode(server: ReplayServer, index: FixtureIndex) -> None:
    """Credit exhaustion cannot be recorded live without burning 1.7M credits."""
    fx = index.fixtures[0]
    server.set_mode("exhausted")
    try:
        r = httpx.post(f"{server.base_url}{fx.path}", json=fx.request_body,
                          timeout=10)
        assert r.status_code == 402
        assert "credits" in r.json()["details"]["message"].lower()
    finally:
        server.set_mode("normal")


# --------------------------------------------------------------------------- #
# Request log — the instrument the Phase 5 cache gate depends on
# --------------------------------------------------------------------------- #

def test_request_log_counts_real_traffic(server: ReplayServer,
                                         index: FixtureIndex) -> None:
    fx = index.fixtures[0]
    server.reset_log()
    httpx.post(f"{server.base_url}{fx.path}", json=fx.request_body, timeout=10)
    httpx.post(f"{server.base_url}{fx.path}", json=fx.request_body, timeout=10)
    logged = server.requests()
    assert len(logged) == 2
    assert all(r.matched == fx.case for r in logged)
