"""
Audit round 10 — does each tool actually deliver its product?

Rounds 1-9 all asked the same question in different clothes: *does this handle
its inputs correctly?* Nine passes of that found 41 defects and left the codebase
genuinely hard to break with a malformed payload.

Round 10 asked a different one: *given a well-formed, successful, paid-for
response, can the caller actually get the thing they bought?* That question has a
different blind spot, and everything below sat inside it - including one endpoint
whose entire output was unreachable through every route the server offered.

The recurring theme is unchanged and arrived twice more: a guard applied to one
member of a class and never swept to its sibling. `_num` hardened the two range
columns and not the measurement between them. `MIN_SCRUBBABLE_KEY` protected the
log writer and not the archive writer. `resolve_date` was fixed for `filter_type`
and not for `start_time`.

Every test here was verified to FAIL against the pre-fix source.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from fortyguard_mcp.client.results import (
    shape_response,
    slice_result,
    tile_values,
)
from fortyguard_mcp.config import Settings
from fortyguard_mcp.store.results_store import ResultStore, scrub_for_storage
from fortyguard_mcp.tools.runtime import _cache_hit_note


def _tile(tid: int, value: Any, lon: float = -112.09, lat: float = 33.47) -> dict:
    return {
        "type": "Feature",
        "properties": {"tile_id": tid, "average_temperature": value,
                       "min_temperature": value, "max_temperature": value},
        "geometry": {"type": "Polygon", "coordinates": [[
            [lon, lat], [lon + 0.001, lat], [lon + 0.001, lat + 0.001],
            [lon, lat + 0.001], [lon, lat]]]},
    }


def _result(values: list[Any]) -> dict:
    return {
        "map_data": {"type": "FeatureCollection",
                     "features": [_tile(i, v) for i, v in enumerate(values)]},
        "stats_data": {"temperature_stats": {"minimum": 25.0, "maximum": 45.0,
                                             "mean": 32.5}},
    }


def store_at(tmp_path: Path) -> ResultStore:
    return ResultStore(Settings(api_key="k" * 32, data_dir=tmp_path))


# --------------------------------------------------------------------------- #
# R10-2 — the ranking defect
# --------------------------------------------------------------------------- #

def test_a_non_finite_measurement_never_becomes_a_tile() -> None:
    """
    `float("NaN")` succeeds, and Python's JSON parser accepts a bare `NaN`, so a
    bare `float(value)` guarded only by TypeError/ValueError let a non-finite
    measurement into `Tile.value`.
    """
    tiles = tile_values(_result([30.0, float("nan"), 45.0, 25.0]))
    assert len(tiles) == 3
    assert all(math.isfinite(t.value) for t in tiles)
    assert [t.value for t in tiles] == [30.0, 45.0, 25.0]


def test_a_nan_literal_parsed_from_json_is_also_rejected() -> None:
    """The realistic route in: the API sends it and Python accepts it happily."""
    raw = json.loads(
        '{"map_data":{"features":[{"properties":'
        '{"tile_id":0,"average_temperature":NaN},'
        '"geometry":{"type":"Polygon","coordinates":'
        '[[[-112,33],[-112,33.1],[-111.9,33.1],[-112,33]]]}}]},"stats_data":{}}')
    assert tile_values(raw) == []


def test_top_n_returns_the_actually_highest_tiles() -> None:
    """
    The defect was not that a NaN travelled - it was that it CORRUPTED THE
    ORDER. NaN compares False against everything, so `sorted(reverse=True)`
    leaves it where it started and displaces a real tile.

    Measured on this exact input before the fix: `top_n=3` returned 30/NaN/45
    and silently dropped the genuine 25.0. For a product whose whole function is
    ranking, that is the worst possible place for it.
    """
    out = slice_result(_result([30.0, float("nan"), 45.0, 25.0]), top_n=3)
    values = [row[3] for row in out["rows"]]
    assert values == [45.0, 30.0, 25.0]
    assert not any(v != v for v in values), "a NaN survived into the ranking"


def test_slice_statistics_stay_internally_consistent() -> None:
    """Before the fix: min 30.0, max 45.0, mean NaN - a self-contradicting block."""
    stats = slice_result(_result([30.0, float("nan"), 45.0, 25.0]),
                         top_n=3)["stats_of_slice"]
    assert stats is not None
    for key in ("min", "max", "mean", "spread"):
        assert math.isfinite(stats[key]), f"{key} is not a finite number"
    assert stats["min"] == 25.0 and stats["max"] == 45.0


# --------------------------------------------------------------------------- #
# R10-3 — the archive must hold valid JSON
# --------------------------------------------------------------------------- #

def test_the_archive_never_holds_a_bare_nan_literal(tmp_path: Path) -> None:
    """
    `json.dump` emits `NaN` by default, and `fortyguard://result/{id}` serves the
    stored bytes VERBATIM without parsing - which is the point of that route. So
    an unsanitised write handed the client something no strict decoder accepts.
    """
    store = store_at(tmp_path)
    store.put("act-nan", "/v1/heatmap", {"q": 1}, _result([float("nan"), 30.0]))

    raw = store.path_for("act-nan").read_text(encoding="utf-8")
    assert "NaN" not in raw and "Infinity" not in raw

    def strict(constant: str) -> None:
        raise AssertionError(f"bare {constant} reached the archive")

    json.loads(raw, parse_constant=strict)


def test_the_zero_copy_resource_route_serves_parseable_bytes(
    tmp_path: Path,
) -> None:
    """The route that never parses is the one that most needs valid bytes."""
    store = store_at(tmp_path)
    stored = store.put("act-inf", "/v1/heatmap", {"q": 2},
                       {"stats_data": {"mean": float("inf")}})
    assert stored is not None
    with stored.open_bytes() as fh:
        json.loads(fh.read().decode("utf-8"))


def test_the_nulling_is_recorded_not_silent(tmp_path: Path) -> None:
    """
    The archive already admits it redacts credentials. Nulling a number is the
    same kind of change and gets the same treatment - counted, on the sidecar.
    """
    store = store_at(tmp_path)
    store.put("act-c", "/v1/heatmap", {"q": 3},
              {"a": float("nan"), "b": [1.0, float("-inf")], "c": 2.0})
    back = store.get("act-c")
    assert back is not None
    assert back.non_finite_nulled == 2


def test_a_clean_payload_records_nothing_and_is_unchanged(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    original = _result([30.0, 31.5])
    store.put("act-ok", "/v1/heatmap", {"q": 4}, original)
    back = store.get("act-ok")
    assert back is not None
    assert back.non_finite_nulled == 0
    assert back.load() == original


# --------------------------------------------------------------------------- #
# R10-4 — a field that could only ever be null
# --------------------------------------------------------------------------- #

def test_storage_info_no_longer_promises_credits_it_cannot_know(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    store.put("h1", "/v1/heatmap", {"a": 1}, _result([30.0]))
    store.put("h2", "/v1/heatmap", {"a": 2}, _result([31.0]))
    store.put("s1", "/v1/satellite", {"a": 3}, {"coordinates": {}})

    info = store.info()
    assert info.results_by_endpoint == {"/v1/heatmap": 2, "/v1/satellite": 1}
    assert not hasattr(info, "credits_represented")


# --------------------------------------------------------------------------- #
# R10-5 — retrieval advice that actually retrieves something
# --------------------------------------------------------------------------- #

def test_a_result_without_tiles_is_not_offered_tile_based_routes() -> None:
    """
    A real satellite result is ~351 KB of base64 - measured, from the lengths the
    A0 recorder preserved - so it goes over budget routinely. Every narrowing
    option returns an empty table there, and four of six routes were dead ends.
    """
    satellite = {"coordinates": {"latitude": "41.8"},
                 "original_image": ["i" * 114_696],
                 "segmentation": {"image_content": "i" * 235_976}}
    out = shape_response(satellite, activity_id="sat-1", budget_tokens=500,
                         precision=5, fmt="auto")
    assert out["format"] == "summary"
    options = " | ".join(out["options"])
    assert "format='geojson'" in options
    assert "fortyguard://result/sat-1" in options
    for dead_end in ("top_n=", "bbox=", "every_nth=", "format='columnar'"):
        assert f"get_result_slice('sat-1', {dead_end}" not in options
    assert "no tiles" in options


def test_a_result_with_tiles_still_gets_every_route() -> None:
    """The narrowing options must not be lost for the case they were built for."""
    out = shape_response(_result([30.0] * 400), activity_id="hm-1",
                         budget_tokens=500, precision=5, fmt="auto")
    options = " | ".join(out["options"])
    for route in ("format='columnar'", "format='geojson'", "top_n=50",
                  "bbox=[w,s,e,n]", "every_nth=10"):
        assert route in options
    assert "no ceiling" in out["options"][0], "taking it all must come first"


# --------------------------------------------------------------------------- #
# R10-6 — the short-key guard reached the logs and not the archive
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key", ["k", "ab", "che"])
def test_a_short_key_does_not_corrupt_the_permanent_archive(key: str) -> None:
    """
    `MIN_SCRUBBABLE_KEY` was added to `logging_setup` in round 8 and never swept
    to `scrub_for_storage`, so substring redaction still ran here - turning
    `"check_status"` into `"chec<REDACTED_API_KEY>_status"`.

    Worse where it was missed: a mangled log line scrolls away, while the
    archive is paid data that is never evicted and costs credits to rebuild.
    """
    payload = {"next": "check_status", "message": "backoff ok", "status": "Completed"}
    clean, changed = scrub_for_storage(payload, key)
    assert clean == payload
    assert changed is False


def test_a_real_length_key_is_still_redacted() -> None:
    """
    The guard must not become an excuse to stop redacting.

    A SYNTHETIC key of the right length and shape. The first draft of this test
    pasted a real one in as fixture data, which the pre-publish artifact scan
    caught sitting in the sdist - a credential does not stop being a credential
    because it is standing in for one.
    """
    key = "0123456789abcdef0123456789abcdef"
    clean, changed = scrub_for_storage({"echo": f"api-key={key}"}, key)
    assert changed is True
    assert key not in json.dumps(clean)


def test_signed_urls_are_redacted_regardless_of_key_length() -> None:
    """
    Pattern-matched, not substring-matched, so it has no short-key failure mode
    and must keep working when the key is skipped.
    """
    url = "https://s3.amazonaws.com/r.pdf?X-Amz-Signature=deadbeef"
    clean, changed = scrub_for_storage({"download_link": url}, "k")
    assert changed is True
    assert "deadbeef" not in json.dumps(clean)


# --------------------------------------------------------------------------- #
# R10-7 — an error naming a parameter the caller never passed
# --------------------------------------------------------------------------- #

def _seed_heatmap(tmp_path: Path) -> Any:
    from fortyguard_mcp.server import build_server
    from fortyguard_mcp.tools.runtime import ToolContext

    ctx = ToolContext(settings=Settings(api_key="k" * 32, data_dir=tmp_path))
    ctx.results.put("hm-1", "/v1/heatmap", {
        "polygon_aoi": {"type": "Polygon", "coordinates": [[
            [-112.10, 33.46], [-112.07, 33.46], [-112.07, 33.49],
            [-112.10, 33.49], [-112.10, 33.46]]]},
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00",
                      "filter_type": 1},
    }, _result([31.5]))
    return build_server(ctx)


@pytest.mark.parametrize("extra,expected_name", [
    ({"start_time": "06:00"}, "start_time"),
    ({"start_date": "2024-07-16"}, "start_date"),
    ({"start_date": "2024-07-16", "start_time": "06:00"},
     "start_date or start_time"),
])
async def test_a_date_conflict_names_the_argument_that_was_passed(
    tmp_path: Path, extra: dict[str, Any], expected_name: str
) -> None:
    """
    The conflict itself is correct - a sourced heatmap supplies the date, and an
    explicit one can disagree with it. What was wrong is that the message always
    said "start_date", so passing only `start_time` told the caller to drop a
    parameter they never used.

    Round 6 fixed exactly this for `filter_type` and left `start_time` beside it.
    """
    srv = _seed_heatmap(tmp_path)
    res = await srv.call_tool("get_env_params", {
        "latitude": 33.475, "longitude": -112.085,
        "from_activity_id": "hm-1", **extra})
    message = json.loads(res.content[0].text)["message"]
    assert f"Supply either {expected_name}= or from_activity_id=" in message


async def test_filter_type_alone_still_does_not_count_as_a_date(
    tmp_path: Path,
) -> None:
    """Round 6's fix must survive round 10's."""
    srv = _seed_heatmap(tmp_path)
    res = await srv.call_tool("get_env_params", {
        "latitude": 33.475, "longitude": -112.085,
        "from_activity_id": "hm-1", "filter_type": 1})
    out = json.loads(res.content[0].text)
    assert "not both" not in str(out.get("message", ""))


# --------------------------------------------------------------------------- #
# R10-8 — the determinism claim and the end of a date range
# --------------------------------------------------------------------------- #

STORED_AT = "2026-08-25T04:00:00+00:00"


def test_a_range_reaching_into_the_future_is_not_called_historical() -> None:
    """
    `filter_type: 4` covers a range and `end_date` was never consulted, so a
    request spanning sixteen months past the stored date was told it "was
    already history when it was stored, and re-running an identical historical
    request was measured as byte-identical".
    """
    body = {"date_time": {"start_date": "2024-07-01",
                          "end_date": "2026-12-31", "filter_type": 4}}
    note, confidence = _cache_hit_note("/v1/heatmap", body, STORED_AT)
    assert confidence == "unverified_not_historical"
    assert "NOT yet in the past" in note


def test_a_fully_historical_range_keeps_the_stronger_claim() -> None:
    body = {"date_time": {"start_date": "2024-07-01",
                          "end_date": "2024-07-31", "filter_type": 4}}
    assert _cache_hit_note("/v1/heatmap", body, STORED_AT)[1] == "historical_verified"


@pytest.mark.parametrize("date,expected", [
    ("2024-7-5", "historical_verified"),        # unpadded: sorts wrong as text
    ("999-01-01", "historical_verified"),       # 3-digit year: sorts wrong as text
    ("2026-12-31", "unverified_not_historical"),
    ("not-a-date", "unverified_not_historical"),
])
def test_dates_are_compared_as_dates_not_as_strings(
    date: str, expected: str
) -> None:
    """
    Lexicographic comparison happens to work for zero-padded ISO dates and fails
    quietly for everything else. Unparseable must never read as historical.
    """
    body = {"date_time": {"start_date": date, "filter_type": 1}}
    assert _cache_hit_note("/v1/heatmap", body, STORED_AT)[1] == expected
