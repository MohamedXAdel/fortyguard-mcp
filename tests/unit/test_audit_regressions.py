"""
Regressions for bugs found in the full-source audit.

Each test pins a specific defect that shipped into "done" code, so it cannot
return quietly.

    python -m pytest tests/unit/test_audit_regressions.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fortyguard_mcp.client.results import (
    n_cells,
    shape_response,
    slice_result,
    tile_values,
    to_columnar,
)
from fortyguard_mcp.config import Settings
from fortyguard_mcp.store.results_store import ResultStore, scrub_for_storage


def _tile(tile_id: int, coords: list | None) -> dict:
    geom = {"type": "Polygon", "coordinates": coords} if coords is not None else {}
    return {"properties": {"tile_id": tile_id, "average_temperature": 30.0 + tile_id},
            "geometry": geom}


GOOD_RING = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]]
STATS = {"temperature_stats": {"minimum": 30.0, "maximum": 31.0,
                              "mean": 30.5, "standard_deviation": 0.5}}


# --------------------------------------------------------------------------- #
# B1 — NaN made the payload invalid JSON
# --------------------------------------------------------------------------- #

def _strict_loads(blob: str):
    """Reject NaN/Infinity the way a conformant JSON parser does."""
    def boom(c):
        raise ValueError(f"invalid JSON constant: {c}")
    return json.loads(blob, parse_constant=boom)


@pytest.mark.parametrize("bad_coords", [[], [[]], None])
def test_unusable_geometry_never_emits_nan(bad_coords):
    """
    `_centroid` used to return float('nan'), and json.dumps writes a bare `NaN`
    literal - invalid JSON, rejected outright by a strict parser. That would
    fail the whole tool response over one malformed tile.
    """
    result = {"map_data": {"features": [_tile(0, bad_coords), _tile(1, GOOD_RING)]},
              "stats_data": STATS}
    col = to_columnar(result)
    blob = json.dumps(col)

    assert "NaN" not in blob
    _strict_loads(blob)                       # must not raise

    # The tile is kept with null coordinates, not silently dropped.
    assert col["n_cells"] == 2
    assert col["rows"][0][1] is None and col["rows"][0][2] is None
    assert col["tiles_without_geometry"] == 1
    # Its measurement survives - only the position is unknown.
    assert col["rows"][0][3] == 30.0


def test_bbox_excludes_unplaceable_tiles_without_crashing():
    result = {"map_data": {"features": [_tile(0, []), _tile(1, GOOD_RING)]},
              "stats_data": STATS}
    out = slice_result(result, bbox=(-1.0, -1.0, 2.0, 2.0))
    assert out["n_cells_returned"] == 1        # the placeable one
    _strict_loads(json.dumps(out))


def test_tile_coordinates_are_none_not_nan():
    tiles = tile_values({"map_data": {"features": [_tile(0, [])]},
                         "stats_data": STATS})
    assert tiles[0].lon is None and tiles[0].lat is None


# --------------------------------------------------------------------------- #
# B2 — signed-URL regex mangled ordinary links
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "https://example.com/?case=1",                      # contains 'se='
    "https://api.fortyguard.com/v1/heatmap?these=1",    # contains 'se='
    "https://docs-api.fortyguard.com/usage=1",
    "https://example.com/?design=1",                    # contains 'sig'
])
def test_ordinary_urls_are_not_redacted(url):
    """
    The pattern used to match the bare substrings `se=` and `sig=` anywhere in a
    URL, so `ca[se=]1` and `the[se=]1` were corrupted. Silently mangling a stored
    payload is worse than missing an exotic URL form.
    """
    clean, changed = scrub_for_storage({"u": url}, None)
    assert changed is False, f"wrongly redacted: {url}"
    assert clean["u"] == url


@pytest.mark.parametrize("url", [
    "https://s3.amazonaws.com/r.pdf?X-Amz-Signature=deadbeef",
    "https://storage.googleapis.com/x?X-Goog-Signature=abc",
    "https://acct.blob.core.windows.net/x?se=2026-01-01&sig=abc",
    "https://s3.amazonaws.com/r.pdf?AWSAccessKeyId=AKIA123",
])
def test_real_signed_urls_are_still_redacted(url):
    clean, changed = scrub_for_storage({"u": url}, None)
    assert changed is True, f"failed to redact: {url}"
    assert "REDACTED" in clean["u"]


# --------------------------------------------------------------------------- #
# B3 — reported vs actual cell count disagreed silently
# --------------------------------------------------------------------------- #

def test_count_mismatch_is_surfaced_not_resolved():
    """
    n_cells() trusts the API's reported count; the encoder counts real features.
    When they disagree, report BOTH rather than quietly picking one - an agent
    told "112 tiles" that receives 3 otherwise has no way to know why.
    """
    result = {"map_data": {"features": [_tile(i, GOOD_RING) for i in range(3)]},
              "stats_data": {"n_cells": 112, **STATS}}
    col = to_columnar(result)

    assert n_cells(result) == 112              # what the API claimed
    assert col["n_cells"] == 3                 # what was actually present
    assert col["n_cells_reported_by_api"] == 112
    assert "112" in col["count_mismatch"] and "3" in col["count_mismatch"]


def test_no_mismatch_annotation_when_counts_agree():
    result = {"map_data": {"features": [_tile(i, GOOD_RING) for i in range(3)]},
              "stats_data": {"n_cells": 3, **STATS}}
    col = to_columnar(result)
    assert "count_mismatch" not in col
    assert "n_cells_reported_by_api" not in col


# --------------------------------------------------------------------------- #
# Config was documented but never read
# --------------------------------------------------------------------------- #

def test_configured_budget_actually_takes_effect(monkeypatch):
    """
    FORTYGUARD_INLINE_TOKEN_BUDGET and _COORDINATE_PRECISION were documented in
    the README while shape_response used its own hardcoded defaults, so setting
    them did nothing.
    """
    import fortyguard_mcp.config as cfg

    big = {"map_data": {"features": [_tile(i, GOOD_RING) for i in range(50)]},
           "stats_data": STATS}

    monkeypatch.setattr(cfg, "_settings",
                        Settings(api_key="k", inline_token_budget=10))
    assert shape_response(big, activity_id="a")["format"] == "summary"

    monkeypatch.setattr(cfg, "_settings",
                        Settings(api_key="k", inline_token_budget=10_000_000))
    assert shape_response(big, activity_id="a")["format"] == "raw"


def test_configured_precision_takes_effect(monkeypatch):
    import fortyguard_mcp.config as cfg

    result = {"map_data": {"features": [
        {"properties": {"tile_id": 0, "average_temperature": 30.0},
         "geometry": {"type": "Polygon", "coordinates":
                      [[[1.123456789, 2.123456789]] * 4 + [[1.123456789, 2.123456789]]]}}]},
        "stats_data": STATS}

    monkeypatch.setattr(cfg, "_settings",
                        Settings(api_key="k", coordinate_precision=2))
    assert shape_response(result, activity_id="a", fmt="columnar")["rows"][0][1] == 1.12


# --------------------------------------------------------------------------- #
# Empty-result notice applied to only one of three paths
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", ["auto", "geojson", "columnar"])
def test_credit_notice_appears_on_every_format(fmt):
    empty = {"map_data": {"features": []},
             "stats_data": {"activity_id": "x", "n_cells": 0}}
    out = shape_response(empty, activity_id="a", budget_tokens=25_000, fmt=fmt)
    assert "0 tiles" in out["notice"], f"no credit notice for fmt={fmt}"


# --------------------------------------------------------------------------- #
# Storage bookkeeping
# --------------------------------------------------------------------------- #

def test_total_bytes_excludes_metadata_sidecars(tmp_path: Path):
    store = ResultStore(Settings(api_key="k", data_dir=tmp_path))
    store.put("act-1", "/v1/heatmap", {"a": 1},
              {"map_data": {"features": []}, "stats_data": {"n_cells": 0}})

    payload_only = store.path_for("act-1").stat().st_size
    assert store.total_bytes() == payload_only


def test_long_ids_cannot_collide_and_overwrite(tmp_path: Path):
    """
    Truncating to a fixed length meant two different long ids could map to the
    same file and silently overwrite a paid result.
    """
    store = ResultStore(Settings(api_key="k", data_dir=tmp_path))
    a, b = "x" * 200 + "AAA", "x" * 200 + "BBB"
    store.put(a, "/v1/heatmap", {"q": 1}, {"tag": "first"})
    store.put(b, "/v1/heatmap", {"q": 2}, {"tag": "second"})

    assert store.path_for(a) != store.path_for(b)
    assert store.get(a).load()["tag"] == "first"
    assert store.get(b).load()["tag"] == "second"
