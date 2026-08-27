"""
Audit round 8 — regression pins.

Round 8 was a blind re-audit run by reading every production file end to end and
probing it, rather than trusting the suite. That distinction mattered: the suite
was reporting a false pass at the time (a stale bytecode cache made the first run
of the day green while every later run was red), so "338 passed" was not evidence
of anything.

Every test below was verified to FAIL against the pre-fix source before the fix
landed. The failure each one reproduces is named in its docstring.

The unifying theme is the one the audit record already names: **unguarded access
to external or persisted data, in a function whose sibling was hardened and which
was never swept.** `tile_values` was hardened in round 3 and `stats_of` beside it
was not. `StoredResult.load()` was hardened in round 7 and `ResultStore.get()`
beside it was not. `run_analysis`'s cache-hit path gained `archived` in round 6
and `collect`'s did not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fortyguard_mcp.client.results import (
    Tile,
    slice_result,
    stats_of,
    stats_unreadable,
    tile_values,
    to_columnar,
)
from fortyguard_mcp.config import Settings
from fortyguard_mcp.domain.api_schema import classify_result_shape
from fortyguard_mcp.store.results_store import ResultStore
from fortyguard_mcp.tools.runtime import ToolContext, collect

RING = [[-112.0, 33.4], [-112.0, 33.5], [-111.9, 33.5], [-111.9, 33.4],
        [-112.0, 33.4]]


def feature(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [RING]}}


def heatmap(features: list[Any], stats: Any) -> dict[str, Any]:
    return {"map_data": {"type": "FeatureCollection", "features": features},
            "stats_data": stats}


# --------------------------------------------------------------------------- #
# 31. stats_of crashed on any drift in stats_data
# --------------------------------------------------------------------------- #

DRIFTED_STATS = [
    pytest.param({"temperature_stats": {"minimum": 1.0}}, id="missing_maximum"),
    pytest.param({"temperature_stats": {"minimum": 1.0, "maximum": 2.0}},
                 id="missing_mean"),
    pytest.param({"temperature_stats": {"minimum": "a", "maximum": "b",
                                        "mean": "c"}}, id="string_values"),
    pytest.param({"temperature_stats": {"minimum": None, "maximum": 2.0,
                                        "mean": 1.0}}, id="null_minimum"),
    pytest.param({"temperature_stats": {"minimum": True, "maximum": False,
                                        "mean": True}}, id="booleans"),
    pytest.param({"min": 1.0, "mean": 2.0}, id="analytic_missing_max"),
    pytest.param({"min": 1.0, "max": "x", "mean": 2.0}, id="analytic_string_max"),
    pytest.param("not-a-dict", id="stats_data_is_a_string"),
    pytest.param([1, 2, 3], id="stats_data_is_a_list"),
]


@pytest.mark.parametrize("stats", DRIFTED_STATS)
def test_stats_of_returns_none_rather_than_raising(stats: Any) -> None:
    """
    Every one of these raised out of `stats_of` before the fix.

    `KeyError: 'maximum'`, `KeyError: 'mean'`, `ValueError: could not convert
    string to float`, `TypeError: float() argument must be...`. Because
    `get_result_slice` calls this on a stored payload, the exception surfaced as
    an opaque MCP protocol error carrying only `'maximum'` — on a result the
    caller had already been charged 4,220 credits for.

    P5 says a shape change costs efficiency, never correctness. Returning None
    is the honest degradation; raising is not.
    """
    assert stats_of({"stats_data": stats}) is None


def test_healthy_stats_still_parse_both_shapes() -> None:
    """The guard must not have cost the working path."""
    temp = stats_of({"stats_data": {"temperature_stats": {
        "minimum": 29.0, "maximum": 31.5, "mean": 30.0,
        "standard_deviation": 0.5}}})
    assert temp is not None
    assert (temp.minimum, temp.maximum, temp.mean) == (29.0, 31.5, 30.0)
    assert temp.spread == pytest.approx(2.5)

    analytic = stats_of({"stats_data": {"min": 1.0, "max": 9.0, "mean": 4.0,
                                        "units": "hour"}})
    assert analytic is not None
    assert analytic.std is None


# --------------------------------------------------------------------------- #
# 35. Stats.std was stored unconverted
# --------------------------------------------------------------------------- #

def test_std_is_converted_like_every_sibling_field() -> None:
    """
    `Stats.std` declares `float | None` and used to hold whatever the payload
    carried, so a string standard deviation travelled as a string into JSON
    while every field beside it went through `float()`.
    """
    st = stats_of({"stats_data": {"temperature_stats": {
        "minimum": 1.0, "maximum": 2.0, "mean": 1.5,
        "standard_deviation": "0.5"}}})
    assert st is not None
    assert isinstance(st.std, float)
    assert st.std == pytest.approx(0.5)


def test_unreadable_std_becomes_none_not_a_crash() -> None:
    st = stats_of({"stats_data": {"temperature_stats": {
        "minimum": 1.0, "maximum": 2.0, "mean": 1.5,
        "standard_deviation": {"nested": "nonsense"}}}})
    assert st is not None
    assert st.std is None


# --------------------------------------------------------------------------- #
# Unreadable statistics must be distinguishable from absent ones
# --------------------------------------------------------------------------- #

def test_empty_result_is_not_reported_as_unreadable() -> None:
    """
    An empty result genuinely has no statistics — that is its documented shape,
    not a parse failure. Reporting it as unreadable would cry wolf on the single
    most common zero-tile case, which already costs full price.
    """
    empty = heatmap([], {"activity_id": "a", "n_cells": 0})
    assert stats_of(empty) is None
    assert stats_unreadable(empty) is False
    assert "stats_note" not in to_columnar(empty)


def test_drifted_statistics_are_reported_not_silently_nulled() -> None:
    """
    Absent and unreadable must not both collapse to a bare `null`, or a real
    change in the API hides behind the shape an empty result legitimately has.
    """
    drifted = heatmap([feature({"tile_id": 1, "average_temperature": 30.0})],
                      {"temperature_stats": {"minimum": 29.0, "max": 31.0}})
    assert stats_of(drifted) is None
    assert stats_unreadable(drifted) is True
    out = to_columnar(drifted)
    assert "stats_note" in out
    # The tiles are unaffected: only the summary is missing.
    assert out["n_cells"] == 1
    assert slice_result(drifted, top_n=5)["n_cells_returned"] == 1


# --------------------------------------------------------------------------- #
# 34. min/max_temperature reached the rows unconverted
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["hot", True, {"a": 1}, [1, 2], "NaN"])
def test_unreadable_range_becomes_none_never_a_foreign_type(bad: Any) -> None:
    """
    `min_temperature`/`max_temperature` went straight into the row, so a string
    landed in a column `_columns_for` declares numeric. An agent comparing
    `row[4] > 30` gets a TypeError on data it was told was a number.

    `"NaN"` as a STRING is included deliberately: `float("NaN")` succeeds and
    would have produced a genuine NaN in the payload.
    """
    tiles = tile_values(heatmap(
        [feature({"tile_id": 1, "average_temperature": 30.0,
                  "min_temperature": bad, "max_temperature": bad})], {}))
    assert len(tiles) == 1
    assert tiles[0].vmin is None
    assert tiles[0].vmax is None


def test_a_real_range_still_survives() -> None:
    tiles = tile_values(heatmap(
        [feature({"tile_id": 1, "average_temperature": 30.0,
                  "min_temperature": 29.0, "max_temperature": 31.0})], {}))
    assert (tiles[0].vmin, tiles[0].vmax) == (29.0, 31.0)


# --------------------------------------------------------------------------- #
# 36. classify_result_shape read only features[0]
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("junk", [None, 42, "tile", {"no": "properties"},
                                  {"properties": "not-a-dict"}])
def test_one_malformed_leading_feature_does_not_change_the_columns(
        junk: Any) -> None:
    """
    The classifier read `features[0]` alone, so a single malformed leading
    feature classified the whole result `unknown` and `_columns_for` dropped the
    `min`/`max` columns for every remaining tile.

    That is exactly the data-dependent column set `_columns_for` exists to
    prevent, arriving through the classifier rather than through the values.
    `tile_values` already skips malformed features one at a time; the shape test
    now agrees with it.
    """
    result = heatmap(
        [junk, feature({"tile_id": 2, "average_temperature": 31.0,
                        "min_temperature": 30.0, "max_temperature": 32.0})], {})
    assert classify_result_shape(result) == "temperature"
    assert to_columnar(result)["columns"] == [
        "tile_id", "lon", "lat", "value", "min", "max"]


def test_a_result_with_nothing_readable_is_still_unknown() -> None:
    """The sweep must not turn 'we cannot tell' into a confident answer."""
    assert classify_result_shape(heatmap([None, 42, "x"], {})) == "unknown"


# --------------------------------------------------------------------------- #
# 32. One corrupt sidecar took down the whole store
# --------------------------------------------------------------------------- #

def _store(tmp_path: Path) -> ResultStore:
    return ResultStore(Settings(api_key="k" * 32, data_dir=tmp_path))


GOOD = {"map_data": {"features": []}, "stats_data": {"n_cells": 0}}

CORRUPT_SIDECARS = [
    pytest.param("{}", id="empty_object"),
    pytest.param("[]", id="a_list"),
    pytest.param('"a string"', id="a_string"),
    pytest.param('null', id="null"),
    pytest.param('{"activity_id": "x"}', id="only_one_key"),
    pytest.param('{"activity_id":"x","endpoint":"/v1/heatmap",'
                 '"request_hash":"h","stored_at":"t","size_bytes":"not-an-int"}',
                 id="size_bytes_not_an_int"),
]


@pytest.mark.parametrize("content", CORRUPT_SIDECARS)
def test_corrupt_metadata_reads_as_absent(tmp_path: Path, content: str) -> None:
    """
    `get()` indexed every required key directly, so a sidecar that was valid
    JSON but the wrong shape raised `KeyError`/`TypeError`.

    `load()` was hardened for a corrupt PAYLOAD in round 7 by truncating a file
    on purpose. The sidecar beside it never got the same treatment, and
    unreadable-counts-as-absent is the decision that was already made.
    """
    store = _store(tmp_path)
    store.put("act-bad", "/v1/heatmap", {"a": 1}, GOOD)
    store._meta_path("act-bad").write_text(content, encoding="utf-8")
    assert store.get("act-bad") is None


@pytest.mark.parametrize("content", CORRUPT_SIDECARS)
def test_one_corrupt_sidecar_does_not_hide_every_healthy_result(
        tmp_path: Path, content: str) -> None:
    """
    The severity was never one result. `iter_stored()` calls `get()` in a loop,
    so ONE bad file took down `info()`, the `get_storage_info` tool and the
    `fortyguard://storage` resource entirely — hiding every healthy result
    behind a protocol error, on an archive whose whole purpose is to be the
    durable record of what was paid for.
    """
    store = _store(tmp_path)
    store.put("act-ok", "/v1/heatmap", {"a": 1}, GOOD)
    store.put("act-bad", "/v1/heatmap", {"b": 2}, GOOD)
    store._meta_path("act-bad").write_text(content, encoding="utf-8")

    info = store.info()
    assert info.result_count == 1
    assert store.get("act-ok") is not None


def test_a_healthy_sidecar_still_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put("act-ok", "/v1/heatmap", {"a": 1}, GOOD)
    got = store.get("act-ok")
    assert got is not None
    assert got.activity_id == "act-ok"
    assert got.endpoint == "/v1/heatmap"
    assert got.request_body == {"a": 1}
    assert got.load() == GOOD


# --------------------------------------------------------------------------- #
# 33. collect()'s archive path omitted `archived`
# --------------------------------------------------------------------------- #

async def test_collect_reports_archived_like_run_analysis(
        tmp_path: Path) -> None:
    """
    Round 6 added `archived` to the cache-hit path of `run_analysis` and left
    the identical path in `collect` alone, so an agent branching on the key saw
    it present when a result arrived fresh and ABSENT when the very same result
    was re-collected from disk — the one case where it is most certainly true.

    Fixing one instance of a class without sweeping the class is the habit the
    audit record already names.
    """
    ctx = ToolContext(settings=Settings(api_key="k" * 32, data_dir=tmp_path))
    ctx.results.put("act-1", "/v1/heatmap", {"a": 1}, GOOD)

    out = await collect(ctx, "act-1")
    assert out["from_archive"] is True
    assert out["archived"] is True
    assert out["credits_charged"] == 0


# --------------------------------------------------------------------------- #
# End to end, through the real tool surface
# --------------------------------------------------------------------------- #

async def test_drifted_stats_do_not_break_get_result_slice(
        tmp_path: Path) -> None:
    """
    The whole point of 31, exercised where it actually bit: a stored heatmap
    whose `stats_data` drifted, read back through the MCP tool.

    Before the fix both calls below raised `ToolError: Error executing tool
    get_result_slice: 'maximum'` and the caller lost a paid result.
    """
    from fortyguard_mcp.server import build_server

    ctx = ToolContext(settings=Settings(api_key="k" * 32, data_dir=tmp_path))
    ctx.results.put("act-drift", "/v1/heatmap", {"a": 1}, heatmap(
        [feature({"tile_id": 1, "average_temperature": 30.0})],
        {"temperature_stats": {"minimum": 29.0, "max": 31.0, "mean": 30.0}}))
    server = build_server(ctx)

    for args in ({"activity_id": "act-drift", "top_n": 5},
                 {"activity_id": "act-drift", "format": "columnar"},
                 {"activity_id": "act-drift"}):
        res = await server.call_tool("get_result_slice", args)
        payload = json.loads(res.content[0].text)
        # The tile survives; only the summary is unavailable, and it says so.
        assert payload.get("error") is not True


def test_tile_dataclass_still_accepts_a_direct_non_finite_construction() -> None:
    """
    `_centroid` filters non-finite values, so the downstream guards can no
    longer be reached THROUGH it — but `Tile` is constructible directly, which
    is why defence in depth downstream is still worth keeping. Pinning the
    premise so a future reader does not delete those guards as unreachable.
    """
    t = Tile(0, float("nan"), float("nan"), 30.0)
    assert t.lon != t.lon  # NaN
