"""
Storage layer — the paid archive that doubles as the cache.

Two properties matter most here and both have dedicated tests:

  * nothing is evicted (results cost credits and never go stale)
  * the cache key never collides two different requests, because that would
    silently return the wrong data

    python -m pytest tests/unit/test_results_store.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fortyguard_mcp.config import Settings
from fortyguard_mcp.store.results_store import (
    ResultStore,
    canonical_request_hash,
    scrub_for_storage,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v1_heatmap"


def result_of(case: str):
    doc = json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))
    return doc["poll_responses"][-1]["body"]["data"]["result"]


@pytest.fixture
def store(tmp_path: Path) -> ResultStore:
    return ResultStore(Settings(api_key="k", data_dir=tmp_path))


def aoi(lon_min=-112.095, lat_min=33.470, lon_max=-112.080, lat_max=33.479):
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max],
            [lon_min, lat_max], [lon_min, lat_min]]]}}]}


BODY = {"polygon_aoi": aoi(), "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00",
                      "filter_type": 1}}


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #

def test_put_then_get_returns_the_payload(store: ResultStore):
    r = result_of("t2_1_filter3_day")
    stored = store.put("act-1", "/v1/heatmap", BODY, r)
    assert stored is not None and stored.size_bytes > 0

    back = store.get("act-1")
    assert back is not None
    assert back.endpoint == "/v1/heatmap"
    assert back.load() == r


def test_get_unknown_id_is_none_not_an_error(store: ResultStore):
    assert store.get("nope") is None


def test_survives_a_new_store_instance(store: ResultStore, tmp_path: Path):
    """It is an archive: a restart must not lose anything."""
    store.put("act-1", "/v1/heatmap", BODY, result_of("t2_5_exceedance"))
    fresh = ResultStore(Settings(api_key="k", data_dir=tmp_path))
    assert fresh.get("act-1") is not None


# --------------------------------------------------------------------------- #
# Cache behaviour
# --------------------------------------------------------------------------- #

def test_identical_request_hits(store: ResultStore):
    store.put("act-1", "/v1/heatmap", BODY, result_of("t2_1_filter3_day"))
    hit = store.find_by_request("/v1/heatmap", json.loads(json.dumps(BODY)))
    assert hit is not None and hit.activity_id == "act-1"


def test_key_ignores_ordering_and_float_noise(store: ResultStore):
    """Reordered keys and 1.0-vs-1 are the same request."""
    reordered = {"date_time": {"filter_type": 1, "start_time": "05:00",
                               "start_date": "2024-07-15"},
                 "granularity": 100.0,
                 "polygon_aoi": aoi()}
    assert canonical_request_hash("/v1/heatmap", BODY) == \
        canonical_request_hash("/v1/heatmap", reordered)
    assert canonical_request_hash("/v1/heatmap", BODY) == \
        canonical_request_hash("v1/heatmap", BODY)


@pytest.mark.parametrize("changed", [
    {"granularity": 60},
    {"date_time": {"start_date": "2024-07-16", "start_time": "05:00",
                   "filter_type": 1}},
    {"date_time": {"start_date": "2024-07-15", "start_time": "06:00",
                   "filter_type": 1}},
    {"polygon_aoi": aoi(lon_min=-112.096)},
    {"analytic_type": "exceedance"},
])
def test_different_requests_never_collide(changed):
    """
    The dangerous direction. A cache miss costs credits; a collision returns
    the WRONG DATA silently. Only the first is acceptable.
    """
    other = {**BODY, **changed}
    assert canonical_request_hash("/v1/heatmap", BODY) != \
        canonical_request_hash("/v1/heatmap", other)


def test_endpoint_is_part_of_the_key():
    assert canonical_request_hash("/v1/heatmap", BODY) != \
        canonical_request_hash("/v1/satellite", BODY)


def test_tiny_coordinate_difference_is_a_different_request():
    """
    9dp is ~0.1mm - enough to absorb float noise, far too fine to merge AOIs
    that genuinely differ. A 1e-6 shift is about 10cm and must not collide.
    """
    assert canonical_request_hash("/v1/heatmap", BODY) != \
        canonical_request_hash("/v1/heatmap",
                               {**BODY, "polygon_aoi": aoi(lon_min=-112.095001)})


def test_dangling_index_entry_is_a_miss_not_a_crash(store: ResultStore):
    store.put("act-1", "/v1/heatmap", BODY, result_of("t2_1_filter3_day"))
    store.path_for("act-1").unlink()          # payload deleted by hand
    assert store.find_by_request("/v1/heatmap", BODY) is None


# --------------------------------------------------------------------------- #
# No eviction
# --------------------------------------------------------------------------- #

def test_nothing_is_ever_evicted(store: ResultStore):
    """Results cost credits and never go stale. Writing more removes nothing."""
    for i in range(12):
        store.put(f"act-{i}", "/v1/heatmap",
                  {**BODY, "granularity": 60 + i},
                  result_of("t2_1_filter3_day"))
    assert store.info().result_count == 12
    assert all(store.get(f"act-{i}") is not None for i in range(12))


def test_storage_info_counts_by_endpoint_rather_than_inventing_a_credit_total(
    store: ResultStore,
):
    """
    `credits_represented` used to sit here and was ALWAYS null: `put()` took a
    `credits=` argument that its only caller never passed, so every sidecar on
    disk recorded null while the tool description promised "how many credits
    they represent".

    It is not recoverable either - per-call cost varies by plan, and P2 forbids
    baking one account's price list into the package. So the honest report is a
    count the caller can apply their own rate to.
    """
    for i in range(3):
        store.put(f"act-{i}", "/v1/heatmap", {**BODY, "granularity": 60 + i},
                  result_of("t2_5_exceedance"))
    store.put("env-1", "/v1/env_params", {"latitude": 1}, {"locations": []})

    info = store.info()
    assert info.result_count == 4
    assert info.results_by_endpoint == {"/v1/env_params": 1, "/v1/heatmap": 3}
    assert not hasattr(info, "credits_represented"), (
        "a field that can only ever be null is worse than no field")
    assert info.report_count == 0 and info.report_bytes == 0
    assert info.total_bytes > 0
    assert info.max_storage_bytes is None and info.over_cap is False
    assert Path(info.path).exists()


def test_cap_declines_to_archive_rather_than_deleting(tmp_path: Path):
    """
    An optional cap exists for CI and containers. Reaching it must never delete
    a paid result - it only declines to archive new ones, and the caller still
    receives the payload from memory.
    """
    s = Settings(api_key="k", data_dir=tmp_path, max_storage_bytes=1)
    store = ResultStore(s)
    first = store.put("act-1", "/v1/heatmap", BODY, result_of("t2_5_exceedance"))
    assert first is not None                        # first write allowed

    second = store.put("act-2", "/v1/heatmap", {**BODY, "granularity": 60},
                       result_of("t2_5_exceedance"))
    assert second is None                           # declined, not evicted
    assert store.get("act-1") is not None           # the earlier one survives
    assert store.info().over_cap is True


# --------------------------------------------------------------------------- #
# Credentials never reach disk
# --------------------------------------------------------------------------- #

def test_signed_download_link_is_not_archived(store: ResultStore):
    """
    heat_intelligence returns a pre-signed S3 URL. It expires and is a
    credential, so it must never be written to a durable archive.
    """
    payload = {"download_link":
               "https://tos-dashboard-prod.s3.amazonaws.com/r.pdf?X-Amz-Signature=deadbeef"}
    stored = store.put("act-1", "/v1/heat_intelligence", {}, payload)
    assert stored is not None and stored.redacted is True

    on_disk = store.path_for("act-1").read_text(encoding="utf-8")
    assert "X-Amz-Signature" not in on_disk
    assert "deadbeef" not in on_disk
    assert "REDACTED" in on_disk


def test_api_key_never_reaches_disk(tmp_path: Path):
    secret = "fg_live_super_secret_value"
    store = ResultStore(Settings(api_key=secret, data_dir=tmp_path))
    stored = store.put("act-1", "/v1/heatmap", {},
                       {"echo": f"called with {secret}"})
    assert stored is not None and stored.redacted is True
    assert secret not in store.path_for("act-1").read_text(encoding="utf-8")


def test_ordinary_results_are_untouched(store: ResultStore):
    """Redaction is a backstop; it must not mangle normal payloads."""
    r = result_of("t2_1_filter3_day")
    stored = store.put("act-1", "/v1/heatmap", BODY, r)
    assert stored is not None and stored.redacted is False
    assert stored.load() == r


def test_scrub_leaves_ordinary_urls_alone():
    clean, changed = scrub_for_storage(
        {"docs": "https://docs-api.fortyguard.com/heatmap"}, None)
    assert changed is False
    assert clean["docs"] == "https://docs-api.fortyguard.com/heatmap"


# --------------------------------------------------------------------------- #
# Durability
# --------------------------------------------------------------------------- #

def test_writes_are_atomic(store: ResultStore):
    """A reader must never observe a half-written payload."""
    store.put("act-1", "/v1/heatmap", BODY, result_of("t2_1_filter3_day"))
    assert list(store.root.glob("*.tmp")) == []
    assert json.loads(store.path_for("act-1").read_text(encoding="utf-8"))


def test_activity_id_cannot_escape_the_directory(store: ResultStore):
    store.put("../../evil", "/v1/heatmap", BODY, {"ok": True})
    assert store.path_for("../../evil").parent == store.root
    assert not (store.root.parent.parent / "evil.json").exists()
