"""
Phase 6 gate — the MCP surface end to end. No live calls, no credits.

Everything runs through `MCPServer.call_tool`, which is the same path the stdio
transport takes: argument validation, Context injection, and result conversion
all really happen. Only the far end of the socket is recorded.

The gate has two halves:

  * every tool works against the replay server
  * **no auto-formatted tool result exceeds the written byte cap** — the
    context-budget assertion, and the reason tools return compact strings

    python -m pytest tests/e2e/test_phase6_mcp.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fortyguard_mcp.config import Settings
from fortyguard_mcp.server import DEFAULT_INLINE_WAIT_S, build_server, byte_cap
from fortyguard_mcp.tools.runtime import ToolContext
from tests.replay import FixtureIndex, ReplayServer

pytestmark = pytest.mark.asyncio

HEATMAP = "/v1/heatmap"


@pytest.fixture(scope="module")
def index() -> FixtureIndex:
    return FixtureIndex()


@pytest.fixture(scope="module")
def server():
    with ReplayServer() as srv:
        yield srv


@pytest.fixture
def ctx(server: ReplayServer, tmp_path: Path) -> ToolContext:
    """
    A fresh archive per test, so a cache hit is never accidental.

    Poll delays are tiny because replay walks every recorded poll — one fixture
    has 121 of them.
    """
    settings = Settings(
        api_key="test-key-not-real",
        base_url=server.base_url,
        data_dir=tmp_path / "data",
        poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005,
        poll_timeout_s=60.0,
        request_timeout_s=10.0,
    )
    return ToolContext(settings=settings)


@pytest.fixture
def mcp(ctx: ToolContext):
    return build_server(ctx)


def text_of(result: Any) -> str:
    """The single text block a tool returns."""
    assert not result.is_error, f"tool reported a protocol error: {result.content}"
    assert len(result.content) == 1, result.content
    return result.content[0].text


async def call(mcp: Any, name: str, **kwargs: Any) -> dict[str, Any]:
    parsed = json.loads(text_of(await mcp.call_tool(name, kwargs)))
    assert isinstance(parsed, dict)
    return parsed


def a_completed_heatmap(index: FixtureIndex) -> Any:
    """The recorded heatmap with the most tiles — the hardest case for a budget."""
    best = None
    best_n = -1
    for fx in index.fixtures:
        if fx.path != HEATMAP or not fx.reached_terminal or fx.submit_status != 200:
            continue
        result = (fx.terminal_body or {}).get("data", {}).get("result") or {}
        n = len((result.get("map_data") or {}).get("features") or [])
        if n > best_n:
            best, best_n = fx, n
    assert best is not None and best_n > 0, "no completed heatmap fixture with tiles"
    return best


# --------------------------------------------------------------------------- #
# Surface
# --------------------------------------------------------------------------- #

async def test_the_advertised_surface_is_what_ships(mcp: Any) -> None:
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "get_credit_usage", "get_storage_info",
        "validate_aoi", "split_aoi",
        "submit_heatmap", "create_heatmap",
        "get_env_params",
        "submit_satellite", "submit_streetview", "submit_heat_intelligence",
        "check_status", "get_result_slice",
    }


async def test_estimate_cost_is_not_offered(mcp: Any) -> None:
    """
    Dropped deliberately. Our cost figures came from one key on one plan, and
    P2 forbids shipping account-specific values while P3 says report what
    actually happened rather than predict. `get_credit_usage` reports the real
    balance instead.
    """
    assert "estimate_cost" not in {t.name for t in await mcp.list_tools()}


async def test_every_tool_documents_itself(mcp: Any) -> None:
    for t in await mcp.list_tools():
        assert t.description and len(t.description) > 40, t.name


async def test_tools_return_one_text_block_and_no_duplicate_payload(
        mcp: Any) -> None:
    """
    The measured reason tools return strings: a dict return would be serialised
    with indent=2 (2.26x) AND repeated in structured_content. Both are absent.
    """
    for t in await mcp.list_tools():
        assert t.output_schema is None, t.name
    res = await mcp.call_tool("validate_aoi", {"polygon_aoi": FC})
    assert res.structured_content is None
    assert len(res.content) == 1


async def test_enum_hints_reach_the_tool_descriptions(mcp: Any) -> None:
    """
    GRANULARITY_HINT / FILTER_TYPE_HINT / ANALYTIC_TYPE_HINT / TIME_BASIS_NOTE
    existed unused until now; they exist to be read by the model.
    """
    by_name = {t.name: t for t in await mcp.list_tools()}
    heatmap = by_name["create_heatmap"].description or ""
    assert "60" in heatmap and "80" in heatmap and "100" in heatmap
    assert "single hour" in heatmap
    assert "exceedance" in heatmap and "persistence" in heatmap
    assert "not UTC" in heatmap

    instructions = mcp._lowlevel_server.instructions or ""
    assert "not UTC" in instructions


# --------------------------------------------------------------------------- #
# Local tools — no API, no credits
# --------------------------------------------------------------------------- #

FC = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [-112.15, 33.38], [-112.00, 33.38], [-112.00, 33.50],
            [-112.15, 33.50], [-112.15, 33.38]]]},
    }],
}


async def test_validate_aoi_makes_no_request(mcp: Any, server: ReplayServer) -> None:
    server.reset_log()
    out = await call(mcp, "validate_aoi", polygon_aoi=FC)
    assert out["readable"] is True
    assert out["total_area_km2"] > 0
    assert len(server.requests()) == 0


async def test_split_aoi_requires_a_cap(mcp: Any) -> None:
    """
    No default: the cap is account-specific (P2).

    A missing REQUIRED argument is a schema violation, not an API condition, so
    it surfaces as a protocol error rather than as structured data — the same
    treatment any malformed call gets. (`MCPServer.call_tool` raises it;
    the transport layer is what turns it into `isError`.)
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        await mcp.call_tool("split_aoi", {"polygon_aoi": FC})


async def test_split_aoi_pieces_are_submittable(mcp: Any) -> None:
    out = await call(mcp, "split_aoi", polygon_aoi=FC, max_area_km2=10.0)
    assert out["n_pieces"] > 1
    for piece in out["pieces"]:
        assert piece["type"] == "FeatureCollection"
        ring = piece["features"][0]["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1]


async def test_storage_info_reports_and_never_deletes(mcp: Any,
                                                      ctx: ToolContext) -> None:
    out = await call(mcp, "get_storage_info")
    assert out["result_count"] == 0
    assert out["path"] == str(ctx.results.root)
    assert out["max_storage_bytes"] is None
    assert "never evicted" in out["note"]


# --------------------------------------------------------------------------- #
# The full async lifecycle
# --------------------------------------------------------------------------- #

async def test_submit_returns_an_id_without_waiting(mcp: Any,
                                                    index: FixtureIndex) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    out = await call(
        mcp, "submit_heatmap",
        polygon_aoi=body["polygon_aoi"],
        **_flat_date(body.get("date_time") or {}),
        granularity=body.get("granularity"),
    )
    assert out["status"] == "submitted"
    assert out["activity_id"] == fx.activity_id
    assert out["error"] is False
    assert out["next"] == "check_status"


def _flat_date(dt: dict[str, Any]) -> dict[str, Any]:
    return {k: dt.get(k) for k in
            ("start_date", "start_time", "end_time", "end_date", "filter_type")
            if dt.get(k) is not None}


async def test_submit_then_check_status_collects_and_archives(
        mcp: Any, index: FixtureIndex, ctx: ToolContext) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "submit_heatmap", polygon_aoi=body["polygon_aoi"],
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    out = await call(mcp, "check_status", activity_id=fx.activity_id, wait_s=30.0)
    assert out.get("error") is not True
    assert out["archived"] is True
    assert ctx.results.get(fx.activity_id) is not None


async def test_a_timed_out_submission_is_still_collectable(
        mcp: Any, index: FixtureIndex, ctx: ToolContext) -> None:
    """
    The whole reason the pending store exists: after a timeout the request body
    must survive, or the eventual result can never be matched to an identical
    future request and gets paid for twice.
    """
    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "submit_heatmap", polygon_aoi=body["polygon_aoi"],
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    recalled = ctx.inflight.recall(fx.activity_id)
    assert recalled is not None
    assert recalled.endpoint == HEATMAP
    assert recalled.request_body["polygon_aoi"] == body["polygon_aoi"]

    await call(mcp, "check_status", activity_id=fx.activity_id, wait_s=30.0)
    # Collected: the bookkeeping is dropped and the paid result is keyed by
    # request, so an identical call is now free.
    assert ctx.inflight.recall(fx.activity_id) is None
    assert ctx.results.find_by_request(HEATMAP, recalled.request_body) is not None


async def test_an_identical_request_is_served_from_disk_for_free(
        mcp: Any, index: FixtureIndex, server: ReplayServer) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    args = dict(polygon_aoi=body["polygon_aoi"],
                **_flat_date(body.get("date_time") or {}),
                granularity=body.get("granularity"))

    first = await call(mcp, "create_heatmap", wait_s=30.0, **args)
    assert first["from_archive"] is False

    server.reset_log()
    second = await call(mcp, "create_heatmap", wait_s=30.0, **args)
    assert second["from_archive"] is True
    assert second["credits_charged"] == 0
    assert len(server.requests()) == 0, "a cache hit must make no request at all"


async def test_check_status_on_an_unfinished_job_says_so_distinctly(
        mcp: Any, index: FixtureIndex) -> None:
    """
    2.3: "still processing" and "finished but large" must never be confusable.
    This is the first: no data exists yet, and the next step is to poll again.
    """
    never = next((f for f in index.fixtures
                  if f.path == HEATMAP and f.activity_id and not f.reached_terminal),
                 None)
    if never is None:
        pytest.skip("no non-terminal fixture recorded")
    out = await call(mcp, "check_status", activity_id=never.activity_id)
    assert out["error"] is False
    assert out["next"] == "check_status"
    assert "still running" in out["message"].lower()


# --------------------------------------------------------------------------- #
# THE GATE — context budget
# --------------------------------------------------------------------------- #

async def test_no_auto_result_exceeds_the_byte_cap(
        mcp: Any, ctx: ToolContext, index: FixtureIndex) -> None:
    """
    The Phase 6 gate. Every tool that can carry a result payload is driven with
    the largest recorded heatmap and its serialised output measured.
    """
    fx = a_completed_heatmap(index)
    body = fx.request_body
    cap = byte_cap(ctx.settings)

    await mcp.call_tool("create_heatmap", dict(
        polygon_aoi=body["polygon_aoi"], wait_s=30.0,
        **_flat_date(body.get("date_time") or {}),
        granularity=body.get("granularity")))

    for name, args in [
        ("create_heatmap", dict(polygon_aoi=body["polygon_aoi"], wait_s=30.0,
                                **_flat_date(body.get("date_time") or {}),
                                granularity=body.get("granularity"))),
        ("check_status", {"activity_id": fx.activity_id}),
        ("get_result_slice", {"activity_id": fx.activity_id, "format": "auto"}),
    ]:
        raw = text_of(await mcp.call_tool(name, args))
        assert len(raw) <= cap, f"{name} returned {len(raw)} bytes, cap is {cap}"


async def test_over_budget_reports_what_exists_instead_of_truncating(
        mcp: Any, server: ReplayServer, tmp_path: Path,
        index: FixtureIndex) -> None:
    """
    Squeeze the budget so the largest fixture cannot fit, then check the
    response is a POINTER, not a truncation: still valid JSON, states that
    nothing was cut, and names how to fetch the data at no further cost.
    """
    fx = a_completed_heatmap(index)
    body = fx.request_body
    tight = Settings(
        api_key="test-key-not-real", base_url=server.base_url,
        data_dir=tmp_path / "tight", poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005, poll_timeout_s=60.0,
        inline_token_budget=400,
    )
    small = build_server(ToolContext(settings=tight))

    out = await call(small, "create_heatmap",
                     polygon_aoi=body["polygon_aoi"], wait_s=30.0,
                     **_flat_date(body.get("date_time") or {}),
                     granularity=body.get("granularity"))

    assert out["format"] == "summary"
    assert out["next"] == "get_result_slice"
    assert "not truncated" in out["message"].lower() \
        or "nothing was truncated" in out["message"].lower()
    # Finished, not pending: the two "come back later" states stay distinct.
    assert out.get("error") is not True
    assert "n_cells" in out or "bytes" in out


async def test_result_slice_costs_nothing_and_makes_no_request(
        mcp: Any, server: ReplayServer, index: FixtureIndex) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "create_heatmap", polygon_aoi=body["polygon_aoi"], wait_s=30.0,
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    server.reset_log()
    out = await call(mcp, "get_result_slice", activity_id=fx.activity_id, top_n=5)
    assert len(server.requests()) == 0
    assert out["credits_charged"] == 0
    assert out["n_cells_returned"] <= 5
    # Both statistics, always: a slice maximum is not the field maximum.
    assert "stats_of_slice" in out and "stats_of_full_result" in out


async def test_slice_of_an_uncollected_id_points_at_check_status(mcp: Any) -> None:
    out = await call(mcp, "get_result_slice", activity_id="never-collected")
    assert out["error"] is True
    assert out["next"] == "check_status"


async def test_bad_bbox_is_rejected_clearly(mcp: Any, index: FixtureIndex) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "create_heatmap", polygon_aoi=body["polygon_aoi"], wait_s=30.0,
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))
    out = await call(mcp, "get_result_slice", activity_id=fx.activity_id,
                     bbox=[1.0, 2.0, 3.0])
    assert out["error"] is True
    assert "west, south, east, north" in out["message"]


# --------------------------------------------------------------------------- #
# Errors are data, with the API's own words
# --------------------------------------------------------------------------- #

async def test_api_errors_come_back_structured_and_verbatim(
        mcp: Any, index: FixtureIndex) -> None:
    """
    P1. A 422 must arrive as data carrying the API's message, the status code
    and the raw body — not as a protocol error with one flat string.
    """
    bad = next((f for f in index.fixtures
                if f.path == HEATMAP and f.submit_status == 422), None)
    if bad is None:
        pytest.skip("no 422 heatmap fixture recorded")

    body = bad.request_body
    out = await call(mcp, "create_heatmap", polygon_aoi=body.get("polygon_aoi"),
                     wait_s=5.0, **_flat_date(body.get("date_time") or {}),
                     granularity=body.get("granularity"),
                     analytic_type=body.get("analytic_type"),
                     threshold=body.get("threshold"),
                     direction=body.get("direction"))

    assert out["error"] is True
    assert out["status_code"] == 422
    assert out["source"] == "fortyguard-api"
    assert out["raw"] is not None
    recorded = (bad.submit_body or {}).get("message")
    if recorded:
        assert out["message"] == recorded


async def test_the_api_key_never_appears_in_any_tool_output(
        mcp: Any, ctx: ToolContext, index: FixtureIndex) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    outputs = [
        text_of(await mcp.call_tool("create_heatmap", dict(
            polygon_aoi=body["polygon_aoi"], wait_s=30.0,
            **_flat_date(body.get("date_time") or {}),
            granularity=body.get("granularity")))),
        text_of(await mcp.call_tool("get_credit_usage", {})),
        text_of(await mcp.call_tool("get_storage_info", {})),
        text_of(await mcp.call_tool("check_status", {"activity_id": fx.activity_id})),
    ]
    key = ctx.settings.key
    assert key
    for blob in outputs:
        assert key not in blob


# --------------------------------------------------------------------------- #
# D2 — temperature sourcing
# --------------------------------------------------------------------------- #

async def test_temperature_and_from_activity_id_together_is_an_error(
        mcp: Any) -> None:
    out = await call(mcp, "get_env_params", latitude=33.4484, longitude=-112.074,
                     temperature=30.0, from_activity_id="abc")
    assert out["error"] is True
    assert "not both" in out["message"]
    assert "temperature" in out["message"] and "from_activity_id" in out["message"]


async def test_neither_temperature_nor_source_is_an_error_naming_both(
        mcp: Any) -> None:
    out = await call(mcp, "get_env_params", latitude=33.4484, longitude=-112.074)
    assert out["error"] is True
    assert "temperature" in out["message"] and "from_activity_id" in out["message"]


async def test_explicit_temperature_is_used_as_given(
        mcp: Any, index: FixtureIndex, server: ReplayServer) -> None:
    fx = next((f for f in index.fixtures
               if f.path == "/v1/env_params" and f.reached_terminal), None)
    if fx is None:
        pytest.skip("no completed env_params fixture")
    b = fx.request_body
    dt = b.get("date_time") or {}
    server.reset_log()
    out = await call(mcp, "get_env_params",
                     latitude=b["latitude"], longitude=b["longitude"],
                     temperature=b["temperature"],
                     start_date=dt.get("start_date"),
                     start_time=dt.get("start_time"),
                     filter_type=dt.get("filter_type"), wait_s=30.0)
    assert out.get("error") is not True
    assert "temperature_provenance" not in out
    sent = server.requests()[0].body
    assert sent["temperature"] == b["temperature"]


async def test_sourcing_from_an_unknown_activity_id_refuses(mcp: Any) -> None:
    out = await call(mcp, "get_env_params", latitude=33.4484, longitude=-112.074,
                     from_activity_id="not-a-real-id")
    assert out["error"] is True
    assert out["next"] == "check_status"


async def test_sourcing_outside_the_aoi_refuses_rather_than_guessing(
        mcp: Any, index: FixtureIndex) -> None:
    """
    Every heatmap has a nearest tile to any point on Earth. Without a
    containment check, a Boston coordinate would be answered with a Phoenix
    temperature and nothing would say so.
    """
    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "create_heatmap", polygon_aoi=body["polygon_aoi"], wait_s=30.0,
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    out = await call(mcp, "get_env_params", latitude=42.36, longitude=-71.06,
                     from_activity_id=fx.activity_id)
    assert out["error"] is True
    assert "outside the area of interest" in out["message"]
    assert "aoi_bbox" in out


async def test_sourcing_inside_the_aoi_reports_its_provenance(
        mcp: Any, index: FixtureIndex, ctx: ToolContext) -> None:
    from fortyguard_mcp.tools.sourcing import source_from_heatmap

    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "create_heatmap", polygon_aoi=body["polygon_aoi"], wait_s=30.0,
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    ring = body["polygon_aoi"]["features"][0]["geometry"]["coordinates"][0]
    lon = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
    lat = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)

    got = source_from_heatmap(ctx, fx.activity_id, lat, lon)
    assert isinstance(got["temperature"], float)
    prov = got["provenance"]
    assert prov["from_activity_id"] == fx.activity_id
    assert prov["credits_charged"] == 0
    assert prov["distance_from_requested_point_m"] >= 0


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #

async def test_result_resource_serves_the_untouched_payload(
        mcp: Any, index: FixtureIndex) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "create_heatmap", polygon_aoi=body["polygon_aoi"], wait_s=30.0,
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    contents = list(await mcp.read_resource(f"fortyguard://result/{fx.activity_id}"))
    served = json.loads(contents[0].content)
    recorded = (fx.terminal_body or {})["data"]["result"]
    assert served == recorded, "the raw resource must not reshape anything"


async def test_result_resource_is_uncapped(mcp: Any, ctx: ToolContext,
                                           index: FixtureIndex) -> None:
    """The raw-access path deliberately ignores the inline budget."""
    fx = a_completed_heatmap(index)
    body = fx.request_body
    await call(mcp, "create_heatmap", polygon_aoi=body["polygon_aoi"], wait_s=30.0,
               **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))
    contents = list(await mcp.read_resource(f"fortyguard://result/{fx.activity_id}"))
    assert len(contents[0].content) > byte_cap(ctx.settings) / 4


async def test_missing_result_resource_reports_rather_than_raises(mcp: Any) -> None:
    contents = list(await mcp.read_resource("fortyguard://result/nope"))
    assert json.loads(contents[0].content)["error"] is True


async def test_storage_resource_reads(mcp: Any) -> None:
    contents = list(await mcp.read_resource("fortyguard://storage"))
    assert json.loads(contents[0].content)["result_count"] == 0


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #

async def test_a_dead_upstream_returns_a_transport_error_not_a_crash(
        tmp_path: Path) -> None:
    dead = Settings(api_key="test-key-not-real",
                    base_url="http://127.0.0.1:1",  # nothing listens on port 1
                    data_dir=tmp_path / "dead", request_timeout_s=1.0)
    srv = build_server(ToolContext(settings=dead))
    out = json.loads(text_of(await srv.call_tool("get_credit_usage", {})))
    assert out["error"] is True
    assert out["source"] == "transport"


async def test_progress_is_reported_during_a_wait(
        mcp: Any, index: FixtureIndex) -> None:
    """
    2.1: progress notifications during polling. The fraction is elapsed against
    OUR timeout — the API never says how far along a job is, so a completion
    percentage would be invented.
    """
    from mcp.server.mcpserver import Context

    seen: list[tuple[float, float | None, str | None]] = []

    class Recording(Context):  # type: ignore[misc]
        async def report_progress(self, progress: float, total: float | None = None,
                                  message: str | None = None) -> None:
            seen.append((progress, total, message))

    fx = a_completed_heatmap(index)
    body = fx.request_body
    await mcp.call_tool(
        "create_heatmap",
        dict(polygon_aoi=body["polygon_aoi"], wait_s=30.0,
             **_flat_date(body.get("date_time") or {}),
             granularity=body.get("granularity")),
        Recording(mcp_server=mcp),
    )
    assert seen, "no progress notifications were emitted"
    for progress, total, message in seen:
        assert total == 30.0
        assert 0 <= progress <= total
        assert message


async def test_default_inline_wait_is_bounded(mcp: Any) -> None:
    """A tool call cannot block forever; the ceiling is an MCP obligation."""
    assert 0 < DEFAULT_INLINE_WAIT_S <= 300


# --------------------------------------------------------------------------- #
# Scripted stdio session
# --------------------------------------------------------------------------- #

async def test_a_real_stdio_session_initialises_and_lists(
        server: ReplayServer, tmp_path: Path) -> None:
    """
    The gate's other half: a genuine client speaking JSON-RPC over stdio to the
    packaged entry point, in its own process. Catches anything that only works
    in-process — a stray print to stdout, an import cycle, a broken console
    script.
    """
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters, stdio_client

    env = dict(os.environ)
    env.update({
        "FORTYGUARD_API_KEY": "test-key-not-real",
        "FORTYGUARD_BASE_URL": server.base_url,
        "FORTYGUARD_DATA_DIR": str(tmp_path / "stdio"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONIOENCODING": "utf-8",
    })
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "fortyguard_mcp"], env=env)

    async with stdio_client(params) as (read, write), \
            ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "fortyguard"
        assert init.instructions and "not UTC" in init.instructions

        tools = await session.list_tools()
        assert len(tools.tools) == 12

        res = await session.call_tool("validate_aoi", {"polygon_aoi": FC})
        assert not res.is_error
        payload = json.loads(res.content[0].text)
        assert payload["readable"] is True

        resources = await session.list_resources()
        assert {str(r.uri) for r in resources.resources} == {
            "fortyguard://account/usage", "fortyguard://storage"}

        # The archive lives where the environment said, not in a cache dir the
        # OS may reclaim - stored results are paid data.
        storage = json.loads(text_of(await session.call_tool("get_storage_info", {})))
        assert str(tmp_path / "stdio") in storage["path"]


# --------------------------------------------------------------------------- #
# Regressions — defects found auditing this phase's own code
# --------------------------------------------------------------------------- #

async def test_the_default_slice_call_is_capped(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """
    A bare `get_result_slice(id)` names no format, so it gets `auto` and the
    context budget applies.

    The first version of this defaulted to `format="columnar"`, which is
    uncapped — so the most obvious call on a 36,000-tile result would have
    returned roughly 550,000 tokens. The fix is the DEFAULT, not the cap: see
    the two tests below, where naming a format still delivers everything.
    """
    fx = a_completed_heatmap(index)
    body = fx.request_body
    tight = Settings(
        api_key="test-key-not-real", base_url=server.base_url,
        data_dir=tmp_path / "cap", poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005, poll_timeout_s=60.0, inline_token_budget=400,
    )
    small = build_server(ToolContext(settings=tight))
    await call(small, "create_heatmap", polygon_aoi=body["polygon_aoi"],
               wait_s=30.0, **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    raw = text_of(await small.call_tool("get_result_slice",
                                        {"activity_id": fx.activity_id}))
    full = len(json.dumps(
        json.loads(text_of(await small.call_tool(
            "get_result_slice",
            {"activity_id": fx.activity_id, "format": "columnar"})))))

    out = json.loads(raw)
    # Either over-budget path is acceptable: `shape_response` normally catches
    # it, and `_emit` is the backstop for when the envelope alone is too big.
    assert out["reason"] in ("over_budget", "over_byte_cap")
    # A pointer, not the payload. (The pointer has its own ~1.2 KB floor, so at
    # this deliberately absurd 400-token budget it is the notice itself that
    # sets the size - the property that matters is that the DATA did not come.)
    assert len(raw) < full / 5
    # Not a truncation, and still free.
    assert out.get("error") is not True


async def test_the_notice_fits_comfortably_at_the_default_budget(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """The pointer's size floor must be irrelevant at any realistic budget."""
    fx = a_completed_heatmap(index)
    body = fx.request_body
    normal = Settings(
        api_key="test-key-not-real", base_url=server.base_url,
        data_dir=tmp_path / "default", poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005, poll_timeout_s=60.0,
    )
    srv = build_server(ToolContext(settings=normal))
    await call(srv, "create_heatmap", polygon_aoi=body["polygon_aoi"],
               wait_s=30.0, **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    raw = text_of(await srv.call_tool("get_result_slice",
                                      {"activity_id": fx.activity_id}))
    assert len(raw) <= byte_cap(normal)


async def test_the_over_budget_notice_offers_the_whole_payload_first(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """
    The ceiling exists so `auto` has a safe meaning — NOT to talk the caller out
    of the full result. So the options must include taking all of it, and must
    not read as a refusal.
    """
    fx = a_completed_heatmap(index)
    body = fx.request_body
    tight = Settings(
        api_key="test-key-not-real", base_url=server.base_url,
        data_dir=tmp_path / "notice", poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005, poll_timeout_s=60.0, inline_token_budget=400,
    )
    small = build_server(ToolContext(settings=tight))
    await call(small, "create_heatmap", polygon_aoi=body["polygon_aoi"],
               wait_s=30.0, **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    out = await call(small, "get_result_slice", activity_id=fx.activity_id)
    options = " | ".join(out["options"])
    assert "format='geojson'" in options
    assert "format='columnar'" in options
    assert "top_n=50" in options
    assert "every_nth" in options and "bbox" in options
    # The two ways of taking EVERYTHING come first, both labelled unlimited -
    # not buried beneath the narrowing options.
    assert "no ceiling" in out["options"][0]
    assert "no ceiling" in out["options"][1]
    assert {"columnar", "geojson"} <= {
        f for f in ("columnar", "geojson") if f in out["options"][0] + out["options"][1]}
    for refusal in ("cannot", "too large", "must narrow", "not allowed"):
        assert refusal not in out["message"].lower()
    assert "naming a format overrides it" in out["message"]


async def test_naming_columnar_delivers_every_tile_regardless_of_size(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """
    The point the cap must not violate: an explicitly named format is the
    caller's decision and is honoured in full. `auto` is the caller declining to
    decide, which is the only case where we decide for them.
    """
    fx = a_completed_heatmap(index)
    body = fx.request_body
    result = (fx.terminal_body or {})["data"]["result"]
    total_tiles = len((result.get("map_data") or {}).get("features") or [])

    tight = Settings(
        api_key="test-key-not-real", base_url=server.base_url,
        data_dir=tmp_path / "named", poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005, poll_timeout_s=60.0, inline_token_budget=400,
    )
    small = build_server(ToolContext(settings=tight))
    await call(small, "create_heatmap", polygon_aoi=body["polygon_aoi"],
               wait_s=30.0, **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    out = await call(small, "get_result_slice", activity_id=fx.activity_id,
                     format="columnar")
    assert out["format"] == "columnar"
    assert out["n_cells_returned"] == total_tiles
    # Far over the ceiling, and handed over anyway.
    assert len(json.dumps(out)) > byte_cap(tight)


async def test_geojson_remains_the_named_uncapped_escape_hatch(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    fx = a_completed_heatmap(index)
    body = fx.request_body
    tight = Settings(
        api_key="test-key-not-real", base_url=server.base_url,
        data_dir=tmp_path / "hatch", poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005, poll_timeout_s=60.0, inline_token_budget=400,
    )
    small = build_server(ToolContext(settings=tight))
    await call(small, "create_heatmap", polygon_aoi=body["polygon_aoi"],
               wait_s=30.0, **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    out = await call(small, "get_result_slice", activity_id=fx.activity_id,
                     format="geojson")
    assert out["format"] == "geojson"
    assert out["result"] == (fx.terminal_body or {})["data"]["result"]


async def test_a_narrowed_slice_fits_where_the_full_one_did_not(
        server: ReplayServer, tmp_path: Path, index: FixtureIndex) -> None:
    """The advice in the over-cap message has to actually work."""
    fx = a_completed_heatmap(index)
    body = fx.request_body
    tight = Settings(
        api_key="test-key-not-real", base_url=server.base_url,
        data_dir=tmp_path / "narrow", poll_initial_delay_s=0.001,
        poll_max_delay_s=0.005, poll_timeout_s=60.0, inline_token_budget=400,
    )
    small = build_server(ToolContext(settings=tight))
    await call(small, "create_heatmap", polygon_aoi=body["polygon_aoi"],
               wait_s=30.0, **_flat_date(body.get("date_time") or {}),
               granularity=body.get("granularity"))

    out = await call(small, "get_result_slice", activity_id=fx.activity_id,
                     top_n=3)
    assert out["format"] == "columnar"
    assert out["n_cells_returned"] == 3


async def test_containment_is_checked_even_without_the_original_request(
        mcp: Any, ctx: ToolContext, index: FixtureIndex) -> None:
    """
    A result collected after a restart has no request body on record. An earlier
    draft only checked containment when the AOI could be read from that body, so
    this path silently accepted any coordinate on Earth. The tiles' own bounds
    are the fallback.
    """
    from fortyguard_mcp.tools.sourcing import SourcingError, source_from_heatmap

    fx = a_completed_heatmap(index)
    result = (fx.terminal_body or {})["data"]["result"]
    # Exactly what `collect` writes when the request could not be recovered.
    ctx.results.put(fx.activity_id, "unknown",
                    {"unrecoverable_request": fx.activity_id}, result)

    with pytest.raises(SourcingError) as caught:
        source_from_heatmap(ctx, fx.activity_id, 42.36, -71.06)   # Boston
    assert "outside the area of interest" in caught.value.payload["message"]


async def test_containment_fallback_still_serves_a_point_inside(
        mcp: Any, ctx: ToolContext, index: FixtureIndex) -> None:
    """The fallback must refuse the outside, not refuse everything."""
    from fortyguard_mcp.client.results import tile_values
    from fortyguard_mcp.tools.sourcing import source_from_heatmap

    fx = a_completed_heatmap(index)
    result = (fx.terminal_body or {})["data"]["result"]
    ctx.results.put(fx.activity_id, "unknown",
                    {"unrecoverable_request": fx.activity_id}, result)

    tiles = [t for t in tile_values(result) if t.lon is not None]
    mid = tiles[len(tiles) // 2]
    got = source_from_heatmap(ctx, fx.activity_id, mid.lat, mid.lon)
    assert got["temperature"] == mid.value


async def test_every_error_type_renders_as_a_tool_response() -> None:
    """
    `to_dict` moved onto the base class: the tools catch FortyGuardError and
    render whatever they caught, so a subclass without the method would fail
    while reporting a failure.
    """
    from fortyguard_mcp.client import errors as E

    instances = [
        E.FortyGuardError("something"),
        E.APIError(422, {"message": "bad"}),
        E.UnexpectedResponse("no id", body={}),
        E.TransportError("dns"),
        E.PollTimeout("act-1", 30.0, "Processing"),
        E.TaskFailed("act-1", "Failed", {"message": "nope"}),
    ]
    for exc in instances:
        d = exc.to_dict()
        assert isinstance(d, dict) and "message" in d and "error" in d
        json.dumps(d)          # must survive the serialisation boundary


async def test_multi_ring_split_bounds_describe_the_whole_aoi(mcp: Any) -> None:
    """
    `max_pieces` was handed out per ring as `deliverable - len(pieces)`, so when
    ring 2 tripped it the reported figures described ring 2 alone with whatever
    budget was left: a two-part AOI needing thousands of pieces came back as
    "between 10 and 9,604", where the 10 was simply the remaining allowance.
    """
    a = [[-112.15, 33.38], [-112.00, 33.38], [-112.00, 33.50],
         [-112.15, 33.50], [-112.15, 33.38]]
    b = [[-80.0, 40.0], [-79.0, 40.0], [-79.0, 41.0], [-80.0, 41.0], [-80.0, 40.0]]
    out = await call(mcp, "split_aoi",
                     polygon_aoi={"type": "MultiPolygon", "coordinates": [[a], [b]]},
                     max_area_km2=1.0)

    assert out["error"] is False
    assert out["n_rings"] == 2
    # The ceiling counts BOTH rings' grids, so it must exceed either alone.
    assert out["pieces_needed_at_most"] > 9_604
    assert out["pieces_needed_at_least"] <= out["pieces_needed_at_most"]
    assert "bounds_note" in out
    assert "2 ring(s)" in out["message"]


async def test_split_refusal_never_claims_a_stored_result(mcp: Any) -> None:
    """It is not an analysis: nothing was stored, charged, or is retrievable."""
    big = [[-125.0, 25.0], [-67.0, 25.0], [-67.0, 49.0], [-125.0, 49.0],
           [-125.0, 25.0]]
    out = await call(mcp, "split_aoi",
                     polygon_aoi={"type": "Polygon", "coordinates": [big]},
                     max_area_km2=130.0)
    assert out["error"] is False
    blob = json.dumps(out)
    assert "get_result_slice" not in blob
    assert "stored locally" not in blob
    assert "activity_id" not in blob


# --------------------------------------------------------------------------- #
# D2 sourcing, end to end — the coverage gap the audit surfaced
# --------------------------------------------------------------------------- #

SOURCE_HEATMAP = "t2_15_granularity_60"


async def test_sourced_temperature_reaches_the_api_end_to_end(
        mcp: Any, ctx: ToolContext, index: FixtureIndex,
        server: ReplayServer) -> None:
    """
    The whole D2 path against a real recording: source the temperature and the
    date out of a stored heatmap, send them, get a real env_params result back.

    This was unit-tested but never exercised end to end, because the body it
    builds - a tile's temperature plus the heatmap's own date - matched no
    recorded exchange, and replay answered "404 No fixture recorded". That is
    precisely the path where a wrong temperature would look plausible, so the
    exchange was recorded live (`scripts/a0/d2_sourcing.py`, 2,900 credits) to
    close it.
    """
    heatmap = next(f for f in index.fixtures if f.case == SOURCE_HEATMAP)
    result = (heatmap.terminal_body or {})["data"]["result"]
    ctx.results.put(heatmap.activity_id, "/v1/heatmap",
                    heatmap.request_body, result)

    ring = heatmap.request_body["polygon_aoi"]["features"][0]["geometry"][
        "coordinates"][0]
    lon = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
    lat = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)

    server.reset_log()
    out = await call(mcp, "get_env_params", latitude=lat, longitude=lon,
                     from_activity_id=heatmap.activity_id, filter_type=1,
                     wait_s=30.0)

    assert out.get("error") is not True, out
    assert out["api_status"] == "Completed"

    # The temperature and date actually sent came from the heatmap, not the
    # caller - which is the whole point of sourcing.
    sent = server.requests()[0].body
    assert sent["temperature"] == 29.567
    assert sent["date_time"]["start_date"] == "2024-07-15"
    assert sent["date_time"]["start_time"] == "05:00"
    assert sent["date_time"]["filter_type"] == 1        # the caller's mode

    # Provenance travels with the answer, so the temperature is never anonymous.
    prov = out["temperature_provenance"]
    assert prov["from_activity_id"] == heatmap.activity_id
    assert prov["credits_charged"] == 0
    assert prov["distance_from_requested_point_m"] < 100

    # And a real environmental payload came back.
    location = out["result"]["locations"][0]
    assert location["temperature"] == 29.567
    assert len(location["parameters"]) > 5


async def test_sourcing_costs_nothing_beyond_the_analysis_itself(
        mcp: Any, ctx: ToolContext, index: FixtureIndex,
        server: ReplayServer) -> None:
    """Reading the stored heatmap must make no request of its own."""
    heatmap = next(f for f in index.fixtures if f.case == SOURCE_HEATMAP)
    ctx.results.put(heatmap.activity_id, "/v1/heatmap", heatmap.request_body,
                    (heatmap.terminal_body or {})["data"]["result"])
    ring = heatmap.request_body["polygon_aoi"]["features"][0]["geometry"][
        "coordinates"][0]
    lon = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
    lat = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)

    server.reset_log()
    await call(mcp, "get_env_params", latitude=lat, longitude=lon,
               from_activity_id=heatmap.activity_id, filter_type=1, wait_s=30.0)

    # One submit plus its polls - nothing extra for the sourcing step.
    assert len([r for r in server.requests() if r.method == "POST"]) == 1
