"""
Regressions for the third audit round.

    python -m pytest tests/unit/test_audit_round3.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fortyguard_mcp.client.results import to_columnar
from fortyguard_mcp.config import Settings
from fortyguard_mcp.domain.api_schema import classify_result_shape
from fortyguard_mcp.store.results_store import ResultStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def result_of(stem: str):
    p = next(FIXTURES.rglob(f"{stem}.json"))
    doc = json.loads(p.read_text(encoding="utf-8"))
    return doc["poll_responses"][-1]["body"]["data"]["result"]


# --------------------------------------------------------------------------- #
# "empty" must mean a heatmap with no tiles, not "unrecognised"
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload", [
    {},                                        # nothing at all
    {"stats_data": {"n_cells": 0}},            # stats but no map_data
    {"map_data": None},
    {"map_data": "not-a-dict"},
    {"map_data": {}},                          # map_data present, no features key
    {"map_data": {"features": "not-a-list"}},
    {"metadata": {}, "locations": []},         # an env_params response
])
def test_non_heatmap_payloads_are_unknown_not_empty(payload):
    """
    Calling an unrecognised payload "empty" asserts we identified something we
    did not. `unknown` is the honest answer, and it is what makes callers fall
    back to passing the payload through untouched.
    """
    assert classify_result_shape(payload) == "unknown"


def test_genuine_empty_heatmap_is_still_empty():
    """The real 0-tile result must keep classifying as `empty`."""
    real = result_of("t2_forecast_plus6h")
    assert real["map_data"]["features"] == []
    assert classify_result_shape(real) == "empty"
    assert classify_result_shape({"map_data": {"features": []}}) == "empty"


def test_known_shapes_unaffected():
    assert classify_result_shape(result_of("t2_1_filter3_day")) == "temperature"
    assert classify_result_shape(result_of("t2_5_exceedance")) == "analytic"


def test_malformed_features_do_not_crash_the_encoder():
    """A non-dict feature must not blow up shape detection or encoding."""
    payload = {"map_data": {"features": [None, 42, "x"]}, "stats_data": {}}
    assert classify_result_shape(payload) == "unknown"
    col = to_columnar(payload)
    assert col["rows"] == []


# --------------------------------------------------------------------------- #
# Byte streaming for the pass-through path
# --------------------------------------------------------------------------- #

@pytest.fixture
def stored(tmp_path: Path):
    store = ResultStore(Settings(api_key="k", data_dir=tmp_path))
    payload = result_of("t2_1_filter3_day")
    rec = store.put("act-1", "/v1/heatmap", {"q": 1}, payload)
    assert rec is not None
    return store, rec, payload


def test_iter_bytes_reproduces_the_file_exactly(stored):
    store, rec, payload = stored
    streamed = b"".join(rec.iter_bytes(chunk_size=1024))
    assert streamed == store.path_for("act-1").read_bytes()
    assert json.loads(streamed) == payload


def test_open_bytes_never_parses(stored):
    """
    The zero-copy path: handing the payload onwards needs no Python objects, so
    a large result costs a buffer instead of tens of megabytes of dicts.
    """
    _, rec, _ = stored
    with rec.open_bytes() as fh:
        head = fh.read(1)
    assert head == b"{"


def test_chunking_boundary_is_irrelevant(stored):
    _, rec, _ = stored
    whole = b"".join(rec.iter_bytes(chunk_size=1 << 20))
    tiny = b"".join(rec.iter_bytes(chunk_size=7))
    assert whole == tiny


def test_streaming_a_missing_payload_raises_clearly(tmp_path: Path):
    store = ResultStore(Settings(api_key="k", data_dir=tmp_path))
    rec = store.put("act-1", "/v1/heatmap", {"q": 1}, {"ok": True})
    assert rec is not None
    store.path_for("act-1").unlink()

    with pytest.raises(FileNotFoundError, match="act-1"), rec.open_bytes():
        pass


def test_load_still_returns_the_parsed_payload(stored):
    _, rec, payload = stored
    assert rec.load() == payload
