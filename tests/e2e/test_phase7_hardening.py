"""
Phase 7 gate — the surface under fault, exhaustion and partial failure.

    python -m pytest tests/e2e/test_phase7_hardening.py -v

The standard the plan sets is **no hang, no silent data loss**. Not "recognises
every error" — we cannot recognise errors we have never seen. The tests below
therefore assert STRUCTURE: whatever comes back is delivered as data, carries
the API's own words, and never leaves the caller without an activity_id for work
that was paid for.

ON CREDIT EXHAUSTION, STATED PLAINLY: the replay server's `exhausted` mode
returns `402 {"details": {"message": "Insufficient credits..."}}`, and that shape
is **synthesised, not measured** — its own docstring says so, because verifying
it would mean burning 1.7M credits. So nothing here asserts 402 specifically and
nothing in `src/` special-cases it. What is asserted is that an unrecognised
error degrades safely, which holds whatever FortyGuard actually returns.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from fortyguard_mcp.config import Settings
from fortyguard_mcp.logging_setup import (
    JsonFormatter,
    RedactingFilter,
    assert_no_stdout_handlers,
    configure_logging,
    scrub,
)
from fortyguard_mcp.server import build_server
from fortyguard_mcp.tools.runtime import ToolContext
from tests.replay import FixtureIndex, ReplayServer

pytestmark = pytest.mark.asyncio

HEATMAP = "/v1/heatmap"
FAKE_KEY = "test-key-not-real"


@pytest.fixture(scope="module")
def index() -> FixtureIndex:
    return FixtureIndex()


@pytest.fixture
def server():
    """Function-scoped: these tests change the server's MODE."""
    with ReplayServer() as srv:
        yield srv
        srv.set_mode("normal")


def settings_for(srv: ReplayServer, tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = dict(
        api_key=FAKE_KEY, base_url=srv.base_url, data_dir=tmp_path / "data",
        poll_initial_delay_s=0.001, poll_max_delay_s=0.005,
        poll_timeout_s=10.0, request_timeout_s=5.0,
    )
    base.update(over)
    return Settings(**base)


def text_of(result: Any) -> str:
    assert len(result.content) == 1, result.content
    return result.content[0].text


def a_heatmap(index: FixtureIndex) -> Any:
    return max(
        (f for f in index.fixtures
         if f.path == HEATMAP and f.reached_terminal and f.submit_status == 200),
        key=lambda f: len(((f.terminal_body or {}).get("data", {}).get("result")
                           or {}).get("map_data", {}).get("features") or []))


def flat_date(fx: Any) -> dict[str, Any]:
    dt = fx.request_body.get("date_time") or {}
    return {k: dt[k] for k in ("start_date", "start_time", "end_time",
                               "end_date", "filter_type") if dt.get(k) is not None}


# --------------------------------------------------------------------------- #
# stdout purity — the failure that breaks everything and explains nothing
# --------------------------------------------------------------------------- #

async def test_a_real_stdio_session_writes_only_jsonrpc_to_stdout(
        server: ReplayServer, tmp_path: Path) -> None:
    """
    Every byte of stdout must parse as JSON-RPC.

    This is the single most common way an MCP server fails, per the protocol
    docs, and it fails silently: the client reports a parse error rather than
    whatever was printed. Asserting it needs a real subprocess — nothing
    in-process can catch a stray `print` in a dependency.
    """
    env = dict(os.environ)
    env.update({
        "FORTYGUARD_API_KEY": FAKE_KEY,
        "FORTYGUARD_BASE_URL": server.base_url,
        "FORTYGUARD_DATA_DIR": str(tmp_path / "stdio"),
        "FORTYGUARD_LOG_LEVEL": "DEBUG",          # maximum chance to misbehave
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "purity-probe", "version": "0"},
        },
    })
    proc = subprocess.run(
        [sys.executable, "-m", "fortyguard_mcp"],
        input=request + "\n", env=env, capture_output=True, text=True, timeout=60,
    )

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"no stdout at all; stderr was:\n{proc.stderr[:2000]}"
    for line in lines:
        parsed = json.loads(line)           # raises if anything else got in
        assert parsed.get("jsonrpc") == "2.0", line

    # The banner about a missing key, the startup line, any httpx chatter -
    # all of it belongs on stderr, and none of it may reach stdout.
    assert "fortyguard-mcp starting" not in proc.stdout


async def test_no_logging_handler_writes_to_stdout(tmp_path: Path) -> None:
    configure_logging("DEBUG", key_source=lambda: FAKE_KEY)
    assert assert_no_stdout_handlers() == []


async def test_configure_logging_replaces_rather_than_adds(
        tmp_path: Path) -> None:
    """
    `MCPServer.__init__` runs `logging.basicConfig` and installs a RichHandler.
    Leaving it would double every line in two formats.
    """
    build_server(ToolContext(settings=Settings(api_key="k", data_dir=tmp_path)))
    configure_logging("INFO", key_source=lambda: "k")
    assert len(logging.getLogger().handlers) == 1


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

async def test_the_api_key_never_survives_a_log_line() -> None:
    key = "sk-live-abcdef0123456789"
    assert key not in scrub(f"calling with api-key={key}", key)
    assert "[REDACTED]" in scrub(f"api-key={key}", key)


async def test_signed_urls_are_redacted_with_the_storage_pattern() -> None:
    """
    Same regex as the storage backstop, deliberately - two copies of a
    security-critical pattern drift, and the drifted one stops redacting.
    """
    url = ("https://storage.googleapis.com/report.pdf"
           "?X-Goog-Signature=deadbeefcafe&expires=1")
    assert "X-Goog-Signature" not in scrub(url, None)
    # An ordinary URL must survive untouched.
    plain = "https://api.fortyguard.com/v1/heatmap?case=1"
    assert scrub(plain, None) == plain


async def test_redaction_covers_message_args_and_exception_text() -> None:
    key = "sk-live-secret"
    handler = configure_logging("DEBUG", key_source=lambda: key)
    formatter = handler.formatter
    assert isinstance(formatter, JsonFormatter)

    record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                               "key=%s", (key,), None)
    RedactingFilter(lambda: key).filter(record)
    rendered = formatter.format(record)
    assert key not in rendered
    assert json.loads(rendered)["level"] == "ERROR"

    try:
        raise RuntimeError(f"failed with {key}")
    except RuntimeError:
        exc_record = logging.LogRecord("t", logging.ERROR, __file__, 1,
                                       "boom", (), sys.exc_info())
        assert key not in formatter.format(exc_record)


async def test_a_broken_key_source_does_not_break_logging() -> None:
    def explode() -> str:
        raise RuntimeError("no settings yet")

    handler = configure_logging("INFO", key_source=explode)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)
    assert RedactingFilter(explode).filter(record) is True
    assert isinstance(handler.formatter, JsonFormatter)
    assert "hello" in handler.formatter.format(record)


async def test_third_party_loggers_are_quieted_not_silenced() -> None:
    configure_logging("DEBUG", key_source=lambda: FAKE_KEY)
    assert logging.getLogger("httpx").level == logging.WARNING
    # Quieted, not off: a warning still gets through.
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


# --------------------------------------------------------------------------- #
# Fault mode — the upstream misbehaving
# --------------------------------------------------------------------------- #

async def test_a_never_terminal_job_times_out_and_keeps_the_activity_id(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """
    The result of a bounded wait must never be a lost job. A0 measured requests
    still `Processing` after ~470s with no way to tell a long job from a stuck
    one, which is why every wait is bounded.
    """
    fx = a_heatmap(index)
    ctx = ToolContext(settings=settings_for(server, tmp_path))
    mcp = build_server(ctx)
    server.set_mode("fault", fault_kind="never_terminal")

    out = json.loads(text_of(await mcp.call_tool("create_heatmap", dict(
        polygon_aoi=fx.request_body["polygon_aoi"], wait_s=0.4,
        granularity=fx.request_body.get("granularity"), **flat_date(fx)))))

    assert out["error"] is False, out            # still running, not a failure
    assert out["activity_id"]
    assert out["next"] == "check_status"
    # And the request body survived, so the eventual result is still cacheable.
    assert ctx.inflight.recall(out["activity_id"]) is not None


async def test_a_dead_upstream_is_reported_as_transport_not_a_crash(
        tmp_path: Path) -> None:
    dead = Settings(api_key=FAKE_KEY, base_url="http://127.0.0.1:1",
                    data_dir=tmp_path / "dead", request_timeout_s=1.0)
    mcp = build_server(ToolContext(settings=dead))
    out = json.loads(text_of(await mcp.call_tool("get_credit_usage", {})))
    assert out["error"] is True
    assert out["source"] == "transport"
    assert FAKE_KEY not in json.dumps(out)


async def test_every_tool_survives_a_faulting_upstream(
        server: ReplayServer, tmp_path: Path) -> None:
    """
    No tool may raise a protocol error because the upstream misbehaved. Local
    tools must keep working regardless - they never touch the network.
    """
    ctx = ToolContext(settings=settings_for(server, tmp_path))
    mcp = build_server(ctx)
    server.set_mode("fault")

    fc = {"type": "Polygon", "coordinates": [[[-112.15, 33.38], [-112.0, 33.38],
                                              [-112.0, 33.5], [-112.15, 33.5],
                                              [-112.15, 33.38]]]}
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_credit_usage", {}),
        ("get_storage_info", {}),
        ("validate_aoi", {"polygon_aoi": fc}),
        ("split_aoi", {"polygon_aoi": fc, "max_area_km2": 50.0}),
        ("check_status", {"activity_id": "does-not-exist"}),
        ("get_result_slice", {"activity_id": "does-not-exist"}),
        ("submit_streetview", {"latitude": 33.4, "longitude": -112.0}),
    ]
    for name, args in calls:
        result = await mcp.call_tool(name, args)
        assert not result.is_error, f"{name} raised a protocol error"
        payload = json.loads(text_of(result))
        assert isinstance(payload, dict)
        assert FAKE_KEY not in json.dumps(payload), name


# --------------------------------------------------------------------------- #
# Exhaustion — structural only, because the shape is synthesised
# --------------------------------------------------------------------------- #

async def test_exhaustion_degrades_without_special_casing_it(
        server: ReplayServer, tmp_path: Path) -> None:
    """
    Whatever the upstream says when credits run out, it must arrive as data
    carrying the API's own message - not as a crash, and not as our paraphrase.
    """
    ctx = ToolContext(settings=settings_for(server, tmp_path))
    mcp = build_server(ctx)
    server.set_mode("exhausted")

    # An ANALYSIS endpoint, not get_credit_usage: the replay server answers the
    # usage path before the exhaustion check, which is right - you need the
    # usage endpoint to work in order to discover that you are out of credits.
    fc = {"type": "Polygon", "coordinates": [[[-112.15, 33.38], [-112.0, 33.38],
                                              [-112.0, 33.5], [-112.15, 33.5],
                                              [-112.15, 33.38]]]}
    out = json.loads(text_of(await mcp.call_tool("submit_heatmap", {
        "polygon_aoi": fc, "start_date": "2024-07-15",
        "start_time": "05:00", "filter_type": 1})))
    assert out["error"] is True
    assert out["source"] == "fortyguard-api"
    assert out["message"]                        # the API's words, whatever they are
    assert "raw" in out                          # and the untouched body
    assert FAKE_KEY not in json.dumps(out)


async def test_the_usage_endpoint_still_answers_when_exhausted(
        server: ReplayServer, tmp_path: Path) -> None:
    """
    Discovering you are out of credits must not itself require credits. This
    documents the replay server's ordering as intentional rather than accidental.
    """
    mcp = build_server(ToolContext(settings=settings_for(server, tmp_path)))
    server.set_mode("exhausted")
    out = json.loads(text_of(await mcp.call_tool("get_credit_usage", {})))
    assert out.get("error") is not True
    assert "usage" in out


async def test_no_status_code_is_hardcoded_anywhere_in_src() -> None:
    """
    The 402 exhaustion response is INVENTED - the replay server says so. Writing
    `if status == 402` would bake a fabricated fact into the package, which is
    the thing the fact boundary exists to prevent.
    """
    import fortyguard_mcp

    root = Path(fortyguard_mcp.__file__).parent
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for invented in ("402", "Insufficient credits"):
            assert invented not in source, f"{path.name} references {invented!r}"


async def test_an_unknown_activity_id_is_reported_not_invented(
        server: ReplayServer, tmp_path: Path) -> None:
    mcp = build_server(ToolContext(settings=settings_for(server, tmp_path)))
    out = json.loads(text_of(await mcp.call_tool(
        "check_status", {"activity_id": "11111111-2222-3333-4444-555555555555"})))
    assert out["error"] is True
    assert out["status_code"] == 404
    assert out["source"] == "fortyguard-api"


# --------------------------------------------------------------------------- #
# Partial failure — the local side going wrong
# --------------------------------------------------------------------------- #

async def test_a_corrupt_stored_payload_is_reported_not_crashed(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    fx = a_heatmap(index)
    ctx = ToolContext(settings=settings_for(server, tmp_path))
    mcp = build_server(ctx)
    ctx.results.put(fx.activity_id, HEATMAP, fx.request_body,
                    (fx.terminal_body or {})["data"]["result"])
    ctx.results.path_for(fx.activity_id).write_text("{not json", encoding="utf-8")

    result = await mcp.call_tool("get_result_slice",
                                 {"activity_id": fx.activity_id})
    assert not result.is_error
    out = json.loads(text_of(result))
    assert out["error"] is True
    assert "missing" in out["message"] or "payload" in out["message"]


async def test_a_full_archive_still_delivers_the_paid_result(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """
    The cap must never cost the caller data they already paid for. It declines
    to ARCHIVE; it does not decline to answer.
    """
    fx = a_heatmap(index)
    ctx = ToolContext(settings=settings_for(server, tmp_path,
                                            max_storage_bytes=1))
    mcp = build_server(ctx)
    ctx.results.put("seed", HEATMAP, {"seed": True}, {"x": 1})   # fill the cap

    out = json.loads(text_of(await mcp.call_tool("create_heatmap", dict(
        polygon_aoi=fx.request_body["polygon_aoi"], wait_s=20.0,
        granularity=fx.request_body.get("granularity"), **flat_date(fx)))))

    assert out.get("error") is not True
    assert out["archived"] is False
    assert "NOT saved" in out["archive_note"]
    assert out["format"] == "summary" or out.get("result") is not None


async def test_an_unwritable_pending_record_does_not_fail_the_call(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """
    Bookkeeping failure must not fail work that already succeeded and was
    already paid for. The cost is a future cache miss, which is strictly better
    than losing the activity_id.
    """
    fx = a_heatmap(index)
    ctx = ToolContext(settings=settings_for(server, tmp_path))
    mcp = build_server(ctx)

    # A body the recorder cannot serialise stands in for any write failure.
    ctx.inflight.remember(fx.activity_id, HEATMAP, {"bad": {1, 2, 3}})
    assert ctx.inflight.recall(fx.activity_id) is None

    out = json.loads(text_of(await mcp.call_tool("submit_heatmap", dict(
        polygon_aoi=fx.request_body["polygon_aoi"],
        granularity=fx.request_body.get("granularity"), **flat_date(fx)))))
    assert out["error"] is False
    assert out["activity_id"]


# --------------------------------------------------------------------------- #
# Full-codebase sweep — non-finite numbers in a REQUEST body
# --------------------------------------------------------------------------- #

async def test_the_cache_key_survives_non_finite_numbers() -> None:
    """
    `_canonical` rounded floats with `int(r)`, which raises ValueError on NaN
    and OverflowError on Infinity. It runs inside `find_by_request` - the first
    thing EVERY analysis tool does - so one NaN coordinate took down
    submit_heatmap with "cannot convert float NaN to integer".
    """
    from fortyguard_mcp.store.results_store import canonical_request_hash

    nan = canonical_request_hash("/v1/heatmap", {"x": float("nan")})
    inf = canonical_request_hash("/v1/heatmap", {"x": float("inf")})
    ninf = canonical_request_hash("/v1/heatmap", {"x": float("-inf")})
    real = canonical_request_hash("/v1/heatmap", {"x": 0.0})

    assert len({nan, inf, ninf, real}) == 4          # all distinct
    assert nan == canonical_request_hash("/v1/heatmap", {"x": float("nan")})
    # Ordinary bodies are unaffected.
    assert (canonical_request_hash("/v1/heatmap", {"x": 33.4})
            == canonical_request_hash("/v1/heatmap", {"x": 33.400000000001}))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_a_non_finite_request_is_refused_as_data_not_a_crash(
        tmp_path: Path, literal: str) -> None:
    """
    Python's JSON parser accepts these literals, so a permissive client sends
    one without trying. httpx encodes outgoing bodies with `allow_nan=False`
    and raises - and only `httpx.HTTPError` was caught, so it escaped as a
    protocol error reading "Out of range float values are not JSON compliant".
    """
    aoi = json.loads(
        f'{{"type":"Polygon","coordinates":[[[{literal},33.4],'
        f'[-112.0,33.4],[-112.0,33.5],[{literal},33.4]]]}}')
    dead = Settings(api_key=FAKE_KEY, base_url="http://127.0.0.1:1",
                    data_dir=tmp_path / f"nf-{literal}", request_timeout_s=1.0)
    mcp = build_server(ToolContext(settings=dead))

    result = await mcp.call_tool("submit_heatmap", {
        "polygon_aoi": aoi, "start_date": "2024-07-15", "filter_type": 1})
    assert not result.is_error, "must be data, not a protocol error"

    out = json.loads(text_of(result))
    assert out["error"] is True
    assert out["source"] == "fortyguard-mcp"
    assert out["retryable"] is False          # the same body fails identically
    assert "NaN" in out["hint"]


async def test_a_non_finite_temperature_is_refused_too(tmp_path: Path) -> None:
    dead = Settings(api_key=FAKE_KEY, base_url="http://127.0.0.1:1",
                    data_dir=tmp_path / "nf-temp", request_timeout_s=1.0)
    mcp = build_server(ToolContext(settings=dead))
    out = json.loads(text_of(await mcp.call_tool("get_env_params", {
        "latitude": 33.4, "longitude": -112.0, "temperature": float("nan")})))
    assert out["error"] is True
    assert out["source"] == "fortyguard-mcp"


async def test_unsendable_is_distinct_from_transport() -> None:
    """
    Nothing was attempted, so "could not reach the API" would be false - and it
    is not retryable, because the same body fails identically every time.
    """
    from fortyguard_mcp.client.errors import TransportError, UnsendableRequest

    unsendable = UnsendableRequest("nan").to_dict()
    transport = TransportError("dns").to_dict()
    assert unsendable["retryable"] is False
    assert transport["retryable"] is True
    assert unsendable["source"] != transport["source"]
    assert "reach" not in unsendable["message"]
