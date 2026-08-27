"""
Regression pins for the blind audit of the Phase 6 surface.

`server.py` · `tools/runtime.py` · `tools/sourcing.py` · `store/pending.py`

Every test corresponds to a defect that was live in the shipped code and was
found by probing rather than by reading. Unit-level where the defect is
unit-level; the ones needing a live socket live in tests/e2e/test_phase6_mcp.py.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest

from fortyguard_mcp.client.results import Tile, _centroid
from fortyguard_mcp.config import Settings
from fortyguard_mcp.domain.geo import SplitTooLarge, split_ring
from fortyguard_mcp.server import build_server
from fortyguard_mcp.tools.runtime import ToolContext
from fortyguard_mcp.tools.sourcing import (
    SourcingError,
    _aoi_boxes,
    _nearest_tile,
    _tile_bbox,
    source_from_heatmap,
)

PHOENIX_RING = [[-112.15, 33.38], [-112.00, 33.38], [-112.00, 33.50],
                [-112.15, 33.50], [-112.15, 33.38]]


def ctx_at(tmp_path: Path) -> ToolContext:
    return ToolContext(settings=Settings(api_key="test-key-not-real",
                                         data_dir=tmp_path))


# --------------------------------------------------------------------------- #
# A. `format` was validated after the job ran and the credits were spent
# --------------------------------------------------------------------------- #

async def test_format_is_a_closed_set_in_the_schema(tmp_path: Path) -> None:
    """
    It was a bare `str`, and `shape_response` raised only at the very end -
    after the job had been submitted, polled to completion and charged for. A
    `format="xml"` typo cost 4,220 credits and returned an opaque protocol error
    with no activity_id.

    Declaring the enum in the schema means the SDK rejects it before the tool
    body runs, and the model can see the valid set.
    """
    mcp = build_server(ctx_at(tmp_path))
    tools = {t.name: t for t in await mcp.list_tools()}
    for name in ("create_heatmap", "check_status", "get_result_slice"):
        schema = tools[name].input_schema["properties"]["format"]
        assert schema["enum"] == ["auto", "geojson", "columnar"], name


async def test_a_bad_format_never_reaches_the_tool_body(tmp_path: Path) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    mcp = build_server(ctx_at(tmp_path))
    with pytest.raises(ToolError, match="format"):
        await mcp.call_tool("get_result_slice",
                            {"activity_id": "x", "format": "xml"})


# --------------------------------------------------------------------------- #
# B. max_concurrent_requests was inert
# --------------------------------------------------------------------------- #

def test_the_transport_is_shared_across_tool_calls(tmp_path: Path) -> None:
    """
    `ToolContext.http()` built a fresh `FortyGuardHTTP` per call, and the
    semaphore was constructed in its `__init__` - so `max_concurrent_requests`
    only ever bounded requests WITHIN one tool call, of which at most one is in
    flight. The setting did nothing.
    """
    ctx = ctx_at(tmp_path)
    a, b = ctx.http(), ctx.http()
    assert a._sem is b._sem
    assert a._client is b._client
    assert a._sem._value == ctx.settings.max_concurrent_requests


def test_the_semaphore_actually_bounds_concurrency(tmp_path: Path) -> None:
    ctx = ToolContext(settings=Settings(api_key="k", data_dir=tmp_path,
                                        max_concurrent_requests=2))

    async def check() -> None:
        sem = ctx.http()._sem
        assert sem._value == 2
        # Two nested acquisitions exhaust a limit of 2, so the third would block.
        async with sem, sem:
            assert sem.locked()

    asyncio.run(check())


def test_an_injected_client_is_still_honoured(tmp_path: Path) -> None:
    """Tests point the whole surface at the replay server this way."""
    injected = httpx.AsyncClient(base_url="http://127.0.0.1:1")
    ctx = ToolContext(settings=Settings(api_key="k", data_dir=tmp_path),
                      http_client=injected)
    assert ctx.http()._client is injected
    assert ctx._owned_client is None


async def test_closing_the_context_is_idempotent(tmp_path: Path) -> None:
    ctx = ctx_at(tmp_path)
    ctx.http()
    assert ctx._owned_client is not None
    await ctx.aclose()
    await ctx.aclose()
    assert ctx._owned_client is None


# --------------------------------------------------------------------------- #
# D. sourcing from something that is not a heatmap
# --------------------------------------------------------------------------- #

def test_sourcing_from_a_non_heatmap_says_what_it_actually_is(
        tmp_path: Path) -> None:
    """
    It fell through to the empty-tiles branch: "Heatmap X completed with no
    tiles ... empty results still consume credits". Wrong twice - not a heatmap,
    and not empty. The endpoint was on record and never consulted.
    """
    ctx = ctx_at(tmp_path)
    ctx.results.put("env-1", "/v1/env_params", {"latitude": 1}, {"locations": []})

    with pytest.raises(SourcingError) as caught:
        source_from_heatmap(ctx, "env-1", 33.4, -112.0)
    payload = caught.value.payload
    assert "/v1/env_params" in payload["message"]
    assert "not" in payload["message"] and "heatmap" in payload["message"]
    assert payload["stored_endpoint"] == "/v1/env_params"
    assert "no tiles" not in payload["message"]


def test_a_result_with_an_unrecoverable_request_can_still_be_sourced(
        tmp_path: Path) -> None:
    """
    `collect` stores endpoint "unknown" when the request body was lost. That
    must stay sourceable - it is very often a heatmap.
    """
    ctx = ctx_at(tmp_path)
    ctx.results.put("act-1", "unknown", {"unrecoverable_request": "act-1"},
                    {"map_data": {"features": []}, "stats_data": {"n_cells": 0}})
    with pytest.raises(SourcingError) as caught:
        source_from_heatmap(ctx, "act-1", 33.4, -112.0)
    # Reaches the empty-tiles branch, not the wrong-endpoint one.
    assert "no tiles" in caught.value.payload["message"]


# --------------------------------------------------------------------------- #
# E. NaN tile centroids
# --------------------------------------------------------------------------- #

def test_centroid_no_longer_produces_nan() -> None:
    """
    This test used to assert the OPPOSITE, and was right to.

    `_centroid` averaged a ring's positions and returned `(nan, nan)` rather
    than `None` when one of them was NaN - which is what made `_tile_bbox` crash
    once `bbox_of` began refusing non-finite input. That was finding #25.

    A later audit fixed it at the source: `_centroid` now treats non-finite
    exactly as it treats malformed geometry. The downstream filter in
    `_tile_bbox` is kept as defence in depth and is still asserted below, but
    the cause is gone, so the premise had to be rewritten rather than the fix
    reverted to keep an old test passing.
    """
    lon, lat = _centroid({"geometry": {"coordinates":
                                       [[[float("nan"), 1], [2, 2], [3, 3],
                                         [float("nan"), 1]]]}})
    assert lon is None and lat is None


def test_tile_bbox_skips_non_finite_centroids() -> None:
    """
    `bbox_of` refuses non-finite input now, so filtering on `is not None` alone
    turned one malformed tile into an uncaught ValueError and a protocol error.
    """
    tiles = [Tile(0, float("nan"), float("nan"), 30.0),
             Tile(1, -112.0, 33.4, 31.0),
             Tile(2, -112.1, 33.5, 32.0)]
    box = _tile_bbox(tiles)
    assert box is not None
    assert all(math.isfinite(v) for v in box)


def test_nearest_tile_skips_non_finite_centroids() -> None:
    tiles = [Tile(0, float("nan"), float("nan"), 99.0),
             Tile(1, -112.0, 33.4, 31.0)]
    tile, distance = _nearest_tile(tiles, 33.4, -112.0)
    assert tile is not None and tile.tile_id == 1
    assert math.isfinite(distance)


def test_all_non_finite_tiles_yields_no_box() -> None:
    assert _tile_bbox([Tile(0, float("nan"), float("nan"), 1.0)]) is None


# --------------------------------------------------------------------------- #
# F. a MultiPolygon area of interest
# --------------------------------------------------------------------------- #

FAR_RING = [[-80.0, 40.0], [-79.0, 40.0], [-79.0, 41.0], [-80.0, 41.0],
            [-80.0, 40.0]]


def test_every_ring_of_a_multipolygon_aoi_is_considered() -> None:
    """`rings[0]` alone refused points genuinely covered by the second polygon."""
    boxes = _aoi_boxes({"polygon_aoi": {
        "type": "MultiPolygon", "coordinates": [[PHOENIX_RING], [FAR_RING]]}})
    assert len(boxes) == 2


def test_a_point_in_the_second_polygon_is_accepted(tmp_path: Path) -> None:
    ctx = ctx_at(tmp_path)
    aoi = {"type": "MultiPolygon", "coordinates": [[PHOENIX_RING], [FAR_RING]]}
    result = {
        "map_data": {"features": [{
            "properties": {"tile_id": 1, "average_temperature": 30.0},
            "geometry": {"coordinates": [[[-79.6, 40.4], [-79.4, 40.4],
                                          [-79.4, 40.6], [-79.6, 40.6],
                                          [-79.6, 40.4]]]},
        }]},
        "stats_data": {"n_cells": 1},
    }
    ctx.results.put("mp-1", "/v1/heatmap", {"polygon_aoi": aoi}, result)
    got = source_from_heatmap(ctx, "mp-1", 40.5, -79.5)
    assert got["temperature"] == 30.0


def test_a_point_outside_every_ring_is_still_refused(tmp_path: Path) -> None:
    ctx = ctx_at(tmp_path)
    aoi = {"type": "MultiPolygon", "coordinates": [[PHOENIX_RING], [FAR_RING]]}
    result = {
        "map_data": {"features": [{
            "properties": {"tile_id": 1, "average_temperature": 30.0},
            "geometry": {"coordinates": [[[-112.1, 33.4], [-112.0, 33.4],
                                          [-112.0, 33.5], [-112.1, 33.5],
                                          [-112.1, 33.4]]]},
        }]},
        "stats_data": {"n_cells": 1},
    }
    ctx.results.put("mp-2", "/v1/heatmap", {"polygon_aoi": aoi}, result)
    with pytest.raises(SourcingError) as caught:
        source_from_heatmap(ctx, "mp-2", 51.5, -0.12)          # London
    assert "outside the area of interest" in caught.value.payload["message"]
    assert len(caught.value.payload["aoi_bbox"]) == 2


# --------------------------------------------------------------------------- #
# G / H — the smaller ones
# --------------------------------------------------------------------------- #

def test_remember_really_never_raises(tmp_path: Path) -> None:
    """
    The docstring said "Never raises" while `json.dumps` raised TypeError on an
    unserialisable body, outside the OSError suppression.
    """
    ctx = ctx_at(tmp_path)
    ctx.inflight.remember("act-x", "/v1/heatmap", {"bad": {1, 2, 3}})
    assert ctx.inflight.recall("act-x") is None      # not written, not fatal

    ctx.inflight.remember("act-y", "/v1/heatmap", {"fine": [1, 2]})
    recalled = ctx.inflight.recall("act-y")
    assert recalled is not None and recalled.request_body == {"fine": [1, 2]}


def test_an_under_cap_ring_still_respects_max_pieces() -> None:
    """
    The no-split early return handed back its piece unconditionally, so a
    MultiPolygon of many small rings could exceed the caller's budget - each
    ring under the cap, the total over it.
    """
    tiny = [(-112.0, 33.0), (-111.999, 33.0), (-111.999, 33.001),
            (-112.0, 33.001), (-112.0, 33.0)]
    with pytest.raises(SplitTooLarge):
        split_ring(tiny, 1.0, max_pieces=0)
    assert len(split_ring(tiny, 1.0, max_pieces=5)) == 1
    assert len(split_ring(tiny, 1.0)) == 1


# --------------------------------------------------------------------------- #
# I / K — response-shape consistency
# --------------------------------------------------------------------------- #

def test_the_cache_hit_path_reports_archived(tmp_path: Path) -> None:
    """
    `archived` was set only on the fresh path, so it vanished on a cache hit -
    the one case where it is most certainly true.
    """
    import inspect

    from fortyguard_mcp.tools import runtime
    src = inspect.getsource(runtime.run_analysis)
    hit_block = src[src.index("hit is not None"):src.index("budget =")]
    assert '"archived"' in hit_block


def test_the_storage_cap_warning_is_not_overwritten() -> None:
    """
    The unrecoverable-request path assigned `archive_note`, discarding the
    "your paid result was NOT saved" warning when both applied.
    """
    import inspect

    from fortyguard_mcp.tools import runtime
    src = inspect.getsource(runtime.collect)
    assert 'out.get("archive_note")' in src
    assert 'out["archive_note"] = (' not in src


def test_no_no_op_cancellation_handler_remains() -> None:
    """`except asyncio.CancelledError: raise` looked like it did something."""
    import inspect

    from fortyguard_mcp.tools import runtime
    assert "except asyncio.CancelledError" not in inspect.getsource(runtime)


async def test_storage_cap_and_unrecoverable_notes_combine(tmp_path: Path) -> None:
    """
    Both conditions at once must leave both warnings intact.

    The cap declines the write AFTER the total reaches it — verified, not
    assumed — so one result has to land before the second is refused.
    """
    from fortyguard_mcp.client.http import Completion
    from fortyguard_mcp.tools.runtime import _archive_and_shape

    ctx = ToolContext(settings=Settings(api_key="k", data_dir=tmp_path,
                                        max_storage_bytes=1))
    assert ctx.results.put("seed", "/v1/heatmap", {}, {"x": 1}) is not None

    done = Completion(activity_id="a1", status="Completed",
                      result={"map_data": {"features": []}}, poll_count=1,
                      elapsed_s=0.0)
    # Async since the report download landed: any linked file is fetched before
    # the result is archived, because both the archive and the wire boundary
    # redact the link.
    out = await _archive_and_shape(ctx, done, "unknown", {"x": 1},
                                   fmt="auto", budget_tokens=None, precision=None)
    assert out["archived"] is False
    assert "NOT saved" in out["archive_note"]


def test_json_serialisable(tmp_path: Path) -> None:
    """Every payload above has to survive the serialisation boundary."""
    ctx = ctx_at(tmp_path)
    ctx.results.put("env-2", "/v1/env_params", {}, {"locations": []})
    try:
        source_from_heatmap(ctx, "env-2", 1.0, 2.0)
    except SourcingError as e:
        json.loads(json.dumps(e.payload))


def test_the_server_declares_a_lifespan_that_closes_the_client(
        tmp_path: Path) -> None:
    """
    The shared client is server-lifetime now, so something has to own its end.
    Process exit happened to be clean, but that is interpreter teardown, not a
    managed lifecycle - and it leaves the pool open through any graceful
    shutdown that does not immediately exit.
    """
    ctx = ctx_at(tmp_path)
    mcp = build_server(ctx)
    assert mcp.settings.lifespan is not None


async def test_the_lifespan_actually_closes_the_owned_client(
        tmp_path: Path) -> None:
    ctx = ctx_at(tmp_path)
    mcp = build_server(ctx)
    ctx.http()                                    # force the client into being
    assert ctx._owned_client is not None

    lifespan = mcp.settings.lifespan
    assert lifespan is not None
    async with lifespan(mcp):
        pass
    assert ctx._owned_client is None


async def test_the_lifespan_leaves_an_injected_client_alone(
        tmp_path: Path) -> None:
    """An injected client belongs to whoever injected it."""
    injected = httpx.AsyncClient(base_url="http://127.0.0.1:1")
    ctx = ToolContext(settings=Settings(api_key="k", data_dir=tmp_path),
                      http_client=injected)
    mcp = build_server(ctx)
    lifespan = mcp.settings.lifespan
    assert lifespan is not None
    async with lifespan(mcp):
        pass
    assert not injected.is_closed
    await injected.aclose()
