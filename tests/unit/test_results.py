"""
Result accessors, encoding and the response policy — against real fixtures.

The governing rule: raw goes back untouched until raw does not fit. Accessors
are internal plumbing; nothing here should leak into the agent-facing contract
except when the budget forces it.

    python -m pytest tests/unit/test_results.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fortyguard_mcp.client.results import (
    estimate_tokens,
    n_cells,
    shape_response,
    slice_result,
    stats_of,
    tile_values,
    to_columnar,
    units_of,
)
from fortyguard_mcp.domain.api_schema import classify_result_shape

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v1_heatmap"


def result_of(case: str):
    doc = json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))
    return doc["poll_responses"][-1]["body"]["data"]["result"]


DEFAULT = "t2_1_filter3_day"        # 112 tiles, min/max differ from average
SINGLE_HOUR = "t3_1_determinism_encanto"   # 112 tiles, min==max==average
ANALYTIC = "t2_5_exceedance"        # bare `value`, units=hour
EMPTY = "t2_forecast_plus6h"        # 0 tiles, no temperature_stats


# --------------------------------------------------------------------------- #
# Accessors handle all three shapes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("case,shape,expect_cells", [
    (DEFAULT, "temperature", 112),
    (ANALYTIC, "analytic", 112),
    (EMPTY, "empty", 0),
])
def test_shape_and_cell_count(case, shape, expect_cells):
    r = result_of(case)
    assert classify_result_shape(r) == shape
    assert n_cells(r) == expect_cells
    assert len(tile_values(r)) == expect_cells


def test_stats_read_from_both_locations():
    """temperature_stats.minimum vs a bare min — one accessor, both shapes."""
    d, a = stats_of(result_of(DEFAULT)), stats_of(result_of(ANALYTIC))
    assert d is not None and a is not None
    assert d.minimum == 35.652 and d.maximum == 36.1767
    assert d.std is not None            # sample std, only on temperature shape
    assert a.minimum == 12.0 and a.std is None
    assert d.spread == pytest.approx(d.maximum - d.minimum)


def test_empty_result_has_no_stats_rather_than_fake_zeros():
    """An empty result genuinely has no minimum. None is the honest answer."""
    r = result_of(EMPTY)
    assert stats_of(r) is None
    assert n_cells(r) == 0
    assert tile_values(r) == []


def test_units_distinguish_temperature_from_analytic():
    assert units_of(result_of(DEFAULT)) == "celsius"
    assert units_of(result_of(ANALYTIC)) == "hour"


def test_columns_are_stable_regardless_of_the_data():
    """
    The column set must come from the SHAPE, not the values. filter_type=1 makes
    min==max==average on every tile, but the columns must NOT disappear because
    of it - otherwise the same query on a flat day and a varied day returns
    different schemas, and an agent indexing row[4] silently gets a different
    field.
    """
    flat = to_columnar(result_of(SINGLE_HOUR))     # all tiles collapsed
    varied = to_columnar(result_of(DEFAULT))       # tiles genuinely differ
    assert flat["columns"] == varied["columns"] == [
        "tile_id", "lon", "lat", "value", "min", "max"]

    # Values are reported as they are, not normalised away.
    tiles = tile_values(result_of(SINGLE_HOUR))
    assert all(t.vmin == t.value == t.vmax for t in tiles)

    # A different SHAPE legitimately has a different column set.
    assert to_columnar(result_of(ANALYTIC))["columns"] == [
        "tile_id", "lon", "lat", "value"]


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #

def test_columnar_is_far_smaller_and_keeps_every_tile():
    r = result_of(DEFAULT)
    col = to_columnar(r)
    assert col["n_cells"] == n_cells(r)
    assert len(col["rows"]) == n_cells(r)
    ratio = estimate_tokens(r) / estimate_tokens(col)
    assert ratio > 5, f"columnar only {ratio:.1f}x smaller; expected a large win"


def test_columnar_preserves_values_exactly():
    """Coordinates are rounded; the measurements themselves are not."""
    r = result_of(DEFAULT)
    col = to_columnar(r)
    vi = col["columns"].index("value")
    assert [row[vi] for row in col["rows"]] == [t.value for t in tile_values(r)]


def test_columnar_declares_its_own_approximation():
    col = to_columnar(result_of(DEFAULT))
    assert "centroid" in col["note"].lower()
    assert "geojson" in col["note"].lower()


# --------------------------------------------------------------------------- #
# Response policy: raw until it does not fit
# --------------------------------------------------------------------------- #

def test_small_result_returns_raw_untouched():
    r = result_of(SINGLE_HOUR)
    out = shape_response(r, activity_id="a1", budget_tokens=100_000)
    assert out["format"] == "raw"
    assert out["result"] is r, "the raw payload must pass through by identity"


def test_over_budget_returns_summary_not_truncation():
    r = result_of(DEFAULT)
    out = shape_response(r, activity_id="a1", budget_tokens=100)
    assert out["format"] == "summary"
    assert out["reason"] == "over_budget"
    assert "result" not in out and "rows" not in out
    assert out["n_cells"] == 112
    assert out["stats"]["min"] == 35.652
    assert out["next"] == "get_result_slice"
    assert "NOT truncated" in out["message"]
    assert any("columnar" in o for o in out["options"])
    assert out["estimated_tokens"]["columnar"] < out["estimated_tokens"]["raw"]


def test_explicit_formats_bypass_the_budget():
    """An explicit request is honoured regardless of size - the agent chose."""
    r = result_of(DEFAULT)
    raw = shape_response(r, activity_id="a1", budget_tokens=1, fmt="geojson")
    assert raw["format"] == "geojson" and raw["result"] is r

    col = shape_response(r, activity_id="a1", budget_tokens=1, fmt="columnar")
    assert col["format"] == "columnar" and len(col["rows"]) == 112


def test_empty_result_passes_through_raw_with_a_credit_notice():
    """
    0 tiles is tiny, so raw passes through. But a successful-looking empty
    result still cost credits, and that is easy to miss when the status says
    Completed - so the envelope says so. The `result` itself stays untouched.
    """
    raw = result_of(EMPTY)
    out = shape_response(raw, activity_id="a1", budget_tokens=25_000)
    assert out["format"] == "raw"
    assert out["result"] is raw, "the notice must not mutate the payload"
    assert n_cells(out["result"]) == 0
    assert "0 tiles" in out["notice"]
    assert "consume credits" in out["notice"]


def test_non_empty_result_has_no_notice():
    out = shape_response(result_of(DEFAULT), activity_id="a1",
                         budget_tokens=1_000_000)
    assert "notice" not in out


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="unknown format"):
        shape_response(result_of(EMPTY), activity_id="a", budget_tokens=1, fmt="nope")


# --------------------------------------------------------------------------- #
# Slicing
# --------------------------------------------------------------------------- #

def test_top_n_returns_the_highest_values():
    r = result_of(DEFAULT)
    out = slice_result(r, top_n=5)
    vi = out["columns"].index("value")
    vals = [row[vi] for row in out["rows"]]
    assert len(vals) == 5
    assert vals == sorted(vals, reverse=True)
    assert vals[0] == stats_of(r).maximum
    assert out["n_cells_total"] == 112 and out["n_cells_returned"] == 5


def test_bbox_filters_spatially():
    r = result_of(DEFAULT)
    tiles = tile_values(r)
    lons = sorted(t.lon for t in tiles)
    mid = lons[len(lons) // 2]
    out = slice_result(r, bbox=(lons[0], -90, mid, 90))
    assert 0 < out["n_cells_returned"] < out["n_cells_total"]


def test_every_nth_downsamples():
    out = slice_result(result_of(DEFAULT), every_nth=10)
    assert out["n_cells_returned"] == len(range(0, 112, 10))


def test_slice_reports_both_its_own_stats_and_the_parents():
    """
    Reporting only the parent's stats leaves the agent to compute the slice's
    own; reporting only the slice's implies the field is hotter than it is.
    Both, explicitly named.
    """
    r = result_of(DEFAULT)
    out = slice_result(r, top_n=3)
    full, sl = out["stats_of_full_result"], out["stats_of_slice"]

    assert full["min"] == stats_of(r).minimum
    assert sl["min"] > full["min"], "top_n slice should start above the field min"
    assert sl["max"] == full["max"], "the hottest tile is in the top 3"
    assert out["filters_applied"] == ["top_n=3"]


def test_empty_slice_reports_no_stats():
    out = slice_result(result_of(DEFAULT), bbox=(0.0, 0.0, 1.0, 1.0))
    assert out["n_cells_returned"] == 0
    assert out["stats_of_slice"] is None
    assert out["stats_of_full_result"] is not None
