"""
Phase 3 gate — the client against the replay server. No live calls, no credits.

The client speaks real HTTP to a real socket; only the far end is recorded. So
the transport, the poll loop, backoff, timeouts and error parsing are genuinely
exercised rather than mocked.

    python -m pytest tests/e2e/test_phase3_client.py -v
"""

from __future__ import annotations

import json
import time
from itertools import pairwise

import httpx
import pytest

from fortyguard_mcp.client.errors import (
    APIError,
    MissingKeyError,
    PollTimeout,
    TransportError,
    UnexpectedResponse,
)
from fortyguard_mcp.client.http import FortyGuardHTTP
from fortyguard_mcp.config import Settings
from tests.replay import FixtureIndex, ReplayServer

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def index() -> FixtureIndex:
    return FixtureIndex()


@pytest.fixture(scope="module")
def server():
    with ReplayServer() as srv:
        yield srv


def settings_for(server: ReplayServer, **over) -> Settings:
    base = dict(
        api_key="test-key-not-real",
        base_url=server.base_url,
        poll_initial_delay_s=0.01,
        poll_max_delay_s=0.05,
        poll_timeout_s=10.0,
        request_timeout_s=10.0,
    )
    base.update(over)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# Submit -> poll -> terminal, for every endpoint
# --------------------------------------------------------------------------- #

async def test_every_endpoint_round_trips(server: ReplayServer,
                                          index: FixtureIndex) -> None:
    """Each endpoint completes end to end through the real client code path."""
    server.reset_cursors()
    done: set[str] = set()

    # heat_intelligence recorded 121 polls (~395s live). Replay walks every one,
    # so the delays must be tiny or the budget has to absorb 121 round trips.
    fast = settings_for(server, poll_initial_delay_s=0.001,
                        poll_max_delay_s=0.005, poll_timeout_s=120.0)
    async with FortyGuardHTTP(fast) as api:
        for fx in index.fixtures:
            if fx.submit_status != 200 or not fx.activity_id or not fx.polls:
                continue
            # Skip the two that never finished in the campaign (over-cap AOI,
            # pre-2021 date). Replay reproduces their non-termination exactly,
            # which is covered by the never_terminal test instead.
            if not fx.reached_terminal or fx.path in done:
                continue
            comp = await api.submit_and_wait(fx.path, fx.request_body)
            assert comp.activity_id
            assert comp.status == "Completed"
            assert comp.poll_count >= 1
            done.add(fx.path)

    assert done >= {"/v1/heatmap", "/v1/env_params", "/v1/satellite",
                    "/v1/streetview", "/v1/heat_intelligence"}


async def test_intermediate_processing_states_are_traversed(
        server: ReplayServer, index: FixtureIndex) -> None:
    """The loop must actually poll through Processing, not shortcut to the end."""
    server.reset_cursors()
    fx = next(f for f in index.fixtures
              if f.activity_id and f.reached_terminal and 3 < len(f.polls) < 15)
    seen: list[str] = []

    async with FortyGuardHTTP(settings_for(server)) as api:
        comp = await api.wait(
            fx.activity_id,
            on_progress=lambda status, elapsed, n: seen.append(status),
        )

    assert "Processing" in seen, f"never observed Processing: {seen}"
    assert comp.status == "Completed"
    assert seen[-1] == "Completed"


# --------------------------------------------------------------------------- #
# The finding this whole design exists for
# --------------------------------------------------------------------------- #

async def test_never_terminal_times_out_instead_of_hanging(
        server: ReplayServer, index: FixtureIndex) -> None:
    """
    A0 measured requests still `Processing` after ~470s with no way to tell a
    long job from a stuck one. The client must bound the wait, return the
    activity_id, and NOT report failure - the job may still be running.
    """
    fx = next(f for f in index.fixtures if f.activity_id and len(f.polls) > 1)
    server.set_mode("fault", "never_terminal")
    try:
        started = time.monotonic()
        async with FortyGuardHTTP(settings_for(server, poll_timeout_s=0.5)) as api:
            with pytest.raises(PollTimeout) as ei:
                await api.wait(fx.activity_id)
        elapsed = time.monotonic() - started
    finally:
        server.set_mode("normal")
        server.reset_cursors()

    assert elapsed < 5.0, "timeout did not bound the wait"
    err = ei.value
    assert err.activity_id == fx.activity_id
    payload = err.to_dict()
    # Not an error: work is still in flight and nothing was lost.
    assert payload["error"] is False
    assert payload["activity_id"] == fx.activity_id
    assert payload["next"] == "check_status"


async def test_timeout_leaves_the_job_retrievable(server: ReplayServer,
                                                  index: FixtureIndex) -> None:
    """After a timeout the same activity_id still resolves normally."""
    fx = next(f for f in index.fixtures
              if f.activity_id and f.reached_terminal and 3 < len(f.polls) < 15)

    server.set_mode("fault", "never_terminal")
    try:
        async with FortyGuardHTTP(settings_for(server, poll_timeout_s=0.3)) as api:
            with pytest.raises(PollTimeout):
                await api.wait(fx.activity_id)
    finally:
        server.set_mode("normal")
        server.reset_cursors()

    async with FortyGuardHTTP(settings_for(server)) as api:
        comp = await api.wait(fx.activity_id)
    assert comp.status == "Completed"


# --------------------------------------------------------------------------- #
# Errors pass through verbatim (P1)
# --------------------------------------------------------------------------- #

async def test_validation_message_is_preserved_word_for_word(
        server: ReplayServer, index: FixtureIndex) -> None:
    fx = next(f for f in index.fixtures if f.case == "e_granularity_50")
    recorded = fx.submit_body["message"]

    async with FortyGuardHTTP(settings_for(server)) as api:
        with pytest.raises(APIError) as ei:
            await api.submit(fx.path, fx.request_body)

    err = ei.value
    assert err.api_message == recorded
    assert "Input should be 60, 80 or 100" in err.api_message
    assert err.field == "granularity"
    assert err.is_validation_error
    # We add structure, never prose.
    assert err.to_dict()["message"] == recorded
    assert err.to_dict()["source"] == "fortyguard-api"


async def test_both_error_envelopes_are_read(server: ReplayServer) -> None:
    """422 uses `message`; 404 uses `details.message`."""
    async with FortyGuardHTTP(settings_for(server)) as api:
        with pytest.raises(APIError) as ei:
            await api.poll_once("00000000-dead-beef-0000-000000000000")
    assert ei.value.status_code == 404
    assert "No activity found" in ei.value.api_message


async def test_http_500_surfaces_as_api_error(server: ReplayServer,
                                              index: FixtureIndex) -> None:
    fx = index.fixtures[0]
    server.set_mode("fault", "http_500")
    try:
        async with FortyGuardHTTP(settings_for(server)) as api:
            with pytest.raises(APIError) as ei:
                await api.submit(fx.path, fx.request_body)
        assert ei.value.status_code == 500
    finally:
        server.set_mode("normal")


async def test_malformed_json_does_not_crash(server: ReplayServer,
                                             index: FixtureIndex) -> None:
    """
    A truncated body must produce a clean, typed error rather than a parser
    explosion — and specifically UnexpectedResponse, since the HTTP call itself
    succeeded. Calling it an APIError would imply the API reported a failure.
    """
    fx = index.fixtures[0]
    server.set_mode("fault", "malformed")
    try:
        async with FortyGuardHTTP(settings_for(server)) as api:
            with pytest.raises(UnexpectedResponse) as ei:
                await api.submit_and_wait(fx.path, fx.request_body)
    finally:
        server.set_mode("normal")

    payload = ei.value.to_dict()
    assert payload["source"] == "protocol"
    # The unparseable body is carried through so the caller can see it.
    assert "non_json_body" in json.dumps(payload["raw"])


async def test_dropped_connection_is_a_transport_error(
        server: ReplayServer, index: FixtureIndex) -> None:
    fx = index.fixtures[0]
    server.set_mode("fault", "drop")
    try:
        async with FortyGuardHTTP(settings_for(server)) as api:
            with pytest.raises(TransportError) as ei:
                await api.submit(fx.path, fx.request_body)
        assert ei.value.to_dict()["retryable"] is True
    finally:
        server.set_mode("normal")


# --------------------------------------------------------------------------- #
# Usage endpoint quirk, and secret hygiene
# --------------------------------------------------------------------------- #

async def test_usage_sends_key_in_body(server: ReplayServer) -> None:
    """Header alone yields 422; the replay server enforces the real behaviour."""
    async with FortyGuardHTTP(settings_for(server)) as api:
        body = await api.usage()
    assert body["credit_summary"]["cycle_remaining_credits"] > 0


async def test_missing_key_is_a_clear_error(server: ReplayServer) -> None:
    async with FortyGuardHTTP(settings_for(server, api_key="")) as api:
        with pytest.raises(MissingKeyError, match="FORTYGUARD_API_KEY"):
            await api.usage()


async def test_key_never_appears_in_error_text(server: ReplayServer,
                                               index: FixtureIndex) -> None:
    secret = "super-secret-key-value"
    fx = index.fixtures[0]
    server.set_mode("fault", "drop")
    try:
        async with FortyGuardHTTP(settings_for(server, api_key=secret)) as api:
            with pytest.raises(TransportError) as ei:
                await api.submit(fx.path, fx.request_body)
        assert secret not in str(ei.value)
        assert secret not in repr(ei.value.to_dict())
    finally:
        server.set_mode("normal")


# --------------------------------------------------------------------------- #
# Backoff and cancellation
# --------------------------------------------------------------------------- #

async def test_backoff_grows_and_is_capped(server: ReplayServer,
                                           index: FixtureIndex,
                                           monkeypatch) -> None:
    """
    Assert the SLEEP SCHEDULE, not wall-clock gaps.

    An earlier version measured elapsed time between progress callbacks, which
    also includes the HTTP round trip - so on a slower machine the "delay"
    exceeded the cap and the test failed for a reason unrelated to backoff.
    Capturing the arguments to asyncio.sleep tests the actual property, exactly
    and without timing flakiness.
    """
    import asyncio as _asyncio

    server.reset_cursors()
    fx = next(f for f in index.fixtures
              if f.activity_id and f.reached_terminal and 4 < len(f.polls) < 15)

    slept: list[float] = []
    real_sleep = _asyncio.sleep

    async def spy(delay: float, *a, **kw):
        slept.append(delay)
        return await real_sleep(0, *a, **kw)     # run fast, record the intent

    monkeypatch.setattr("fortyguard_mcp.client.http.asyncio.sleep", spy)

    s = settings_for(server, poll_initial_delay_s=0.02,
                     poll_max_delay_s=0.08, poll_backoff_factor=2.0)
    async with FortyGuardHTTP(s) as api:
        await api.wait(fx.activity_id)

    assert len(slept) >= 3, f"too few polls to observe backoff: {slept}"
    assert slept[0] == pytest.approx(0.02), f"wrong initial delay: {slept}"
    # Monotonic non-decreasing, and never above the cap.
    assert all(b >= a for a, b in pairwise(slept)), f"not growing: {slept}"
    assert max(slept) <= 0.08, f"exceeded the cap: {slept}"
    # 0.02 -> 0.04 -> 0.08 -> pinned at the cap thereafter.
    assert slept[:3] == pytest.approx([0.02, 0.04, 0.08])


async def test_cancellation_stops_polling_promptly(server: ReplayServer,
                                                   index: FixtureIndex) -> None:
    """If the caller goes away we must stop, not finish the wait."""
    import asyncio

    fx = next(f for f in index.fixtures if f.activity_id and len(f.polls) > 1)
    server.set_mode("fault", "never_terminal")
    try:
        async with FortyGuardHTTP(settings_for(server, poll_timeout_s=30)) as api:
            task = asyncio.create_task(api.wait(fx.activity_id))
            await asyncio.sleep(0.1)
            task.cancel()
            started = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - started < 2.0
    finally:
        server.set_mode("normal")
        server.reset_cursors()


async def test_client_accepts_an_injected_httpx_client(server: ReplayServer,
                                                       index: FixtureIndex) -> None:
    """Needed so the MCP server can share one connection pool."""
    fx = next(f for f in index.fixtures if f.activity_id and f.reached_terminal)
    async with httpx.AsyncClient(base_url=server.base_url) as hc:
        api = FortyGuardHTTP(settings_for(server), client=hc)
        sub = await api.submit(fx.path, fx.request_body)
        assert sub.activity_id
    assert hc.is_closed
