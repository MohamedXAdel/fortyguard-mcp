"""
Regressions for the second audit round.

    python -m pytest tests/unit/test_audit_round2.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fortyguard_mcp.client.results import (
    CHARS_PER_TOKEN_DENSE,
    CHARS_PER_TOKEN_JSON,
    estimate_tokens,
    slice_result,
    to_columnar,
)
from fortyguard_mcp.config import Settings
from fortyguard_mcp.domain.api_schema import classify_status, result_has_arrived
from fortyguard_mcp.store.results_store import ResultStore, scrub_for_storage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def result_of(stem: str):
    p = next(FIXTURES.rglob(f"{stem}.json"))
    doc = json.loads(p.read_text(encoding="utf-8"))
    return doc["poll_responses"][-1]["body"]["data"]["result"]


# --------------------------------------------------------------------------- #
# Completion is structural, not string-matched
# --------------------------------------------------------------------------- #

def test_result_presence_is_the_completion_signal():
    """
    Verified across 476 recorded polls: `data.result` present appeared only with
    "Completed", never with "Processing". Turning on the structure means a
    renamed success status keeps working.
    """
    assert result_has_arrived({"data": {"status": "Completed", "result": {"x": 1}}})
    assert not result_has_arrived({"data": {"status": "Processing"}})
    assert not result_has_arrived({"data": {"status": "Completed", "result": None}})
    assert not result_has_arrived({})
    assert not result_has_arrived(None)


def test_unknown_success_status_still_completes_if_result_present():
    """The point of the change: no vocabulary to keep in sync."""
    body = {"data": {"status": "FinishedSuccessfully", "result": {"x": 1}}}
    assert result_has_arrived(body)
    # The string alone would not have been recognised.
    assert classify_status("FinishedSuccessfully") == "pending"


def test_recorded_polls_agree_with_the_structural_rule():
    """Re-assert the property against every recorded poll, not a synthetic one."""
    seen_pending = seen_done = 0
    for p in FIXTURES.rglob("*.json"):
        doc = json.loads(p.read_text(encoding="utf-8"))
        for poll in doc.get("poll_responses") or []:
            body = poll.get("body") or {}
            status = ((body.get("data") or {}).get("status"))
            if status == "Completed":
                assert result_has_arrived(body), p.name
                seen_done += 1
            elif status == "Processing":
                assert not result_has_arrived(body), p.name
                seen_pending += 1
    assert seen_done > 0 and seen_pending > 0


# --------------------------------------------------------------------------- #
# Token estimation was wrong in the dangerous direction
# --------------------------------------------------------------------------- #

def test_ratios_are_conservative_versus_measurement():
    """
    Measured with tiktoken over the fixtures: raw 2.82-2.94, columnar 1.86-1.93.
    Constants must sit at or below the observed minimum, because a ratio that is
    too HIGH underestimates tokens and inlines an oversized payload.
    """
    assert CHARS_PER_TOKEN_JSON <= 2.82
    assert CHARS_PER_TOKEN_DENSE <= 1.86


def test_dense_content_uses_the_dense_ratio():
    """One constant for both profiles misreports whichever it was not tuned on."""
    raw = result_of("t3_1_determinism_encanto")
    col = to_columnar(raw)

    raw_chars = len(json.dumps(raw, separators=(",", ":")))
    col_chars = len(json.dumps(col, separators=(",", ":")))

    assert estimate_tokens(raw) == round(raw_chars / CHARS_PER_TOKEN_JSON)
    assert estimate_tokens(col) == round(col_chars / CHARS_PER_TOKEN_DENSE)


def test_estimate_never_undershoots_tiktoken_on_real_payloads():
    """Sanity against a real tokenizer. tiktoken is not Claude's, so allow slack."""
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("cl100k_base")
    for stem in ("t3_1_determinism_encanto", "t2_15_granularity_60", "t2_5_exceedance"):
        raw = result_of(stem)
        for obj in (raw, to_columnar(raw)):
            actual = len(enc.encode(json.dumps(obj, separators=(",", ":"))))
            assert estimate_tokens(obj) >= actual * 0.95, (
                f"{stem}: estimate {estimate_tokens(obj)} under actual {actual}")


# --------------------------------------------------------------------------- #
# Antimeridian bbox
# --------------------------------------------------------------------------- #

def _synthetic(lons: list[float]):
    feats = []
    for i, lon in enumerate(lons):
        ring = [[lon, 51.0], [lon + 0.01, 51.0], [lon + 0.01, 51.01],
                [lon, 51.01], [lon, 51.0]]
        feats.append({"properties": {"tile_id": i, "average_temperature": 10.0 + i},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"map_data": {"features": feats},
            "stats_data": {"temperature_stats": {"minimum": 10.0, "maximum": 12.0,
                                                 "mean": 11.0,
                                                 "standard_deviation": 1.0}}}


def test_bbox_crossing_the_antimeridian_matches():
    """
    US coverage reaches past 180 degrees - the Aleutians. `w <= lon <= e` matches
    nothing there, and an empty slice reads as "no data" rather than "your box
    was misread".
    """
    r = _synthetic([179.5, -179.5, 0.0])
    out = slice_result(r, bbox=(179.0, 50.0, -179.0, 52.0))   # w > e
    assert out["n_cells_returned"] == 2                        # both sides
    assert "antimeridian" in out["filters_applied"][0]


def test_ordinary_bbox_unaffected():
    r = _synthetic([179.5, -179.5, 0.0])
    out = slice_result(r, bbox=(-1.0, 50.0, 1.0, 52.0))
    assert out["n_cells_returned"] == 1
    assert "antimeridian" not in out["filters_applied"][0]


# --------------------------------------------------------------------------- #
# Storage: no needless copy, O(1) cap check
# --------------------------------------------------------------------------- #

def test_clean_payload_is_not_copied():
    """
    Building a full copy of a 14 MB result only to find nothing needed redacting
    held the whole thing in memory twice. A clean payload returns by identity.
    """
    payload = result_of("t3_1_determinism_encanto")
    clean, changed = scrub_for_storage(payload, "some-key-not-present")
    assert changed is False
    assert clean is payload


def test_dirty_payload_is_copied_and_original_untouched():
    payload = {"download_link": "https://s3.amazonaws.com/x?X-Amz-Signature=abc"}
    clean, changed = scrub_for_storage(payload, None)
    assert changed is True
    assert clean is not payload
    assert payload["download_link"].startswith("https://")   # original intact


def test_cap_check_does_not_rescan_when_no_cap(tmp_path: Path, monkeypatch):
    """With no cap configured (the default) the write path never stats the dir."""
    store = ResultStore(Settings(api_key="k", data_dir=tmp_path))
    calls = {"n": 0}
    real = ResultStore.total_bytes

    def counting(self, **kw):
        calls["n"] += 1
        return real(self, **kw)

    monkeypatch.setattr(ResultStore, "total_bytes", counting)
    for i in range(5):
        store.put(f"a-{i}", "/v1/heatmap", {"i": i}, {"ok": i})
    assert calls["n"] == 0, "no cap set, yet the directory was scanned"


def test_running_total_tracks_disk(tmp_path: Path):
    store = ResultStore(Settings(api_key="k", data_dir=tmp_path,
                                 max_storage_bytes=10_000_000))
    for i in range(4):
        store.put(f"a-{i}", "/v1/heatmap", {"i": i}, {"payload": "x" * 100})
    assert store.total_bytes(refresh=False) == store.total_bytes(refresh=True)
