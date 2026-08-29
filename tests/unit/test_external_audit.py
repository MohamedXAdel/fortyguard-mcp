"""
Regression pins for an independent pre-publication audit.

Every test here corresponds to a finding raised by a separate reviewer and then
reproduced against this code. They are kept together because the most valuable
one is the reviewer's observation about OUR OWN test: the existing
`test_the_api_key_never_appears_in_any_tool_output` asserted
`key not in json.dumps(out)` — but the replay server never echoes a request
body, so it passed VACUOUSLY. It asserted shape, not truth.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from fortyguard_mcp.client.errors import APIError
from fortyguard_mcp.client.results import shape_response, tile_values, to_columnar
from fortyguard_mcp.config import Settings
from fortyguard_mcp.server import _emit, build_server
from fortyguard_mcp.store.results_store import ResultStore
from fortyguard_mcp.tools.runtime import ToolContext, _cache_hit_note

RING = {"coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


def feature(geometry: Any, properties: Any) -> dict[str, Any]:
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def settings_at(tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {"api_key": "test-key-not-real-0123456789",
                            "data_dir": tmp_path}
    base.update(over)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# HIGH-1 — the key echoed back in a 422 and handed to the agent
# --------------------------------------------------------------------------- #

def echoing_422(key: str) -> dict[str, Any]:
    """
    The real shape. Proven by `tests/fixtures/v1_heatmap/e_missing_polygon.json`:
    a body-level validation failure returns `detail[].input` containing the
    WHOLE request body — and `usage()` is the one endpoint that must carry the
    key in the body.
    """
    return {"error": True, "status_code": 422,
            "message": "Field 'plan' is required.", "field": "plan",
            "detail": [{"type": "missing", "loc": ["body", "plan"],
                        "msg": "Field required", "input": {"api_key": key}}]}


def test_an_echoed_key_is_scrubbed_from_the_tool_response(
        tmp_path: Path) -> None:
    key = "SECRET-KEY-12345"
    out = _emit(APIError(422, echoing_422(key)).to_dict(),
                settings=settings_at(tmp_path, api_key=key))
    assert key not in out
    assert "[REDACTED]" in out
    json.loads(out)                       # and it is still valid JSON


def test_scrubbing_survives_the_over_cap_path(tmp_path: Path) -> None:
    """The pointer branch builds its own payload and must scrub too."""
    key = "SECRET-KEY-12345"
    settings = settings_at(tmp_path, api_key=key, inline_token_budget=1)
    out = _emit({"activity_id": "a1", "junk": key * 200}, settings=settings)
    assert key not in out


def test_the_resources_scrub_as_well(tmp_path: Path) -> None:
    """
    Resources bypass `_emit` entirely. They were serialising with a bare
    `json.dumps` and had no scrubbing at all.
    """
    import inspect

    from fortyguard_mcp import server
    source = inspect.getsource(server.build_server)
    marker = "# ------------------------------------------------------------- resources #"
    resource_block = source[source.index(marker):]
    assert "json.dumps" not in resource_block, "a resource still serialises raw"


def test_config_docstring_promise_now_holds(tmp_path: Path) -> None:
    """
    `config.py` claims the key is "never logged, never echoed, and never written
    into a stored result". Echoing was the one that did not hold.
    """
    key = "SECRET-KEY-12345"
    settings = settings_at(tmp_path, api_key=key)
    for payload in (APIError(422, echoing_422(key)).to_dict(),
                    {"nested": {"deep": [{"api_key": key}]}},
                    {"text": f"failed with api-key={key}"}):
        assert key not in _emit(payload, settings=settings)


# --------------------------------------------------------------------------- #
# HIGH-2 — the determinism claim, scoped to what was measured
# --------------------------------------------------------------------------- #

def test_a_historical_request_keeps_the_strong_claim() -> None:
    note, confidence = _cache_hit_note(
        "/v1/heatmap", {"date_time": {"start_date": "2024-07-15"}},
        "2026-08-24T00:00:00")
    assert confidence == "historical_verified"
    assert "byte-identical" in note


def test_a_request_not_yet_historical_is_flagged() -> None:
    """
    Determinism was measured on 2024-07-15 - a past date. A request stored
    before its own date arrived can still have data appear later.
    """
    note, confidence = _cache_hit_note(
        "/v1/heatmap", {"date_time": {"start_date": "2026-08-30"}},
        "2026-08-24T00:00:00")
    assert confidence == "unverified_not_historical"
    assert "NOT yet in the past" in note
    assert "Delete the archived entry" in note


def test_a_dateless_request_is_flagged() -> None:
    """`submit_streetview` sends lat/lon/angles - no temporal key whatsoever."""
    note, confidence = _cache_hit_note(
        "/v1/streetview", {"latitude": 33.4, "longitude": -112.0},
        "2026-08-24T00:00:00")
    assert confidence == "unverified_no_date"
    assert "no date" in note


def test_the_blanket_determinism_sentence_is_gone() -> None:
    import inspect

    from fortyguard_mcp.tools import runtime
    source = inspect.getsource(runtime)
    assert "FortyGuard results are deterministic, so this is the same" not in source


def test_heat_intelligences_flat_date_is_understood() -> None:
    _note, confidence = _cache_hit_note(
        "/v1/heat_intelligence", {"date": "2024-07-15"}, "2026-08-24T00:00:00")
    assert confidence == "historical_verified"


# --------------------------------------------------------------------------- #
# MEDIUM-3 — malformed features cost the caller a paid result
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label,features", [
    ("point geometry", [feature({"type": "Point", "coordinates": [1.0, 2.0]},
                                {"tile_id": 1, "average_temperature": 30.0})]),
    ("tile_id not an int", [feature(RING, {"tile_id": "abc",
                                           "average_temperature": 30.0})]),
    ("value not a float", [feature(RING, {"tile_id": 1,
                                          "average_temperature": "hot"})]),
    ("nan in the ring", [feature(
        {"coordinates": [[[float("nan"), 0], [1, 0], [1, 1], [0, 1],
                          [float("nan"), 0]]]},
        {"tile_id": 1, "average_temperature": 30.0})]),
])
def test_a_malformed_feature_never_raises(label: str,
                                          features: list[Any]) -> None:
    """
    `tile_values` promised "one bad tile should not cost the caller the other
    526" but guarded only the feature and properties - not the conversions. Each
    of these raised, AFTER the analysis had been paid for.
    """
    tiles = tile_values({"map_data": {"features": features}})
    assert isinstance(tiles, list), label
    for tile in tiles:
        assert math.isfinite(tile.value)
        assert tile.lon is None or math.isfinite(tile.lon)


def test_one_bad_tile_does_not_cost_the_good_ones() -> None:
    tiles = tile_values({"map_data": {"features": [
        feature(RING, {"tile_id": 1, "average_temperature": 30.0}),
        feature(RING, {"tile_id": "abc", "average_temperature": 31.0}),
        feature(RING, {"tile_id": 3, "average_temperature": 32.0}),
    ]}})
    assert len(tiles) == 3
    assert {t.value for t in tiles} == {30.0, 31.0, 32.0}


def test_an_unreadable_label_does_not_discard_the_measurement() -> None:
    """
    The MEASUREMENT decides whether a tile survives. A malformed `tile_id` is a
    bad label on good data, so the id falls back to the positional index.
    """
    tiles = tile_values({"map_data": {"features": [
        feature(RING, {"tile_id": "abc", "average_temperature": 31.0})]}})
    assert len(tiles) == 1
    assert tiles[0].value == 31.0


def test_an_unreadable_measurement_is_dropped() -> None:
    assert tile_values({"map_data": {"features": [
        feature(RING, {"tile_id": 1, "average_temperature": "hot"})]}}) == []


# --------------------------------------------------------------------------- #
# MEDIUM-4 — NaN in an API payload produced invalid JSON
# --------------------------------------------------------------------------- #

def strict(constant: str) -> None:
    raise AssertionError(f"bare {constant} reached the wire")


def test_a_non_finite_value_never_reaches_the_wire(tmp_path: Path) -> None:
    """
    `httpx.Response.json()` uses `json.loads`, which accepts bare NaN/Infinity.
    Nothing in the READ path rejected them, so one such value failed the whole
    response for any strict parser.
    """
    result = {"map_data": {"features": [feature(
        RING, {"tile_id": 1, "average_temperature": float("nan")})]},
        "stats_data": {"n_cells": 1}}
    text = _emit(shape_response(result, activity_id="a1", fmt="auto"),
                 settings=settings_at(tmp_path))
    json.loads(text, parse_constant=strict)


def test_the_replacement_is_reported_not_hidden(tmp_path: Path) -> None:
    text = _emit({"a": float("nan"), "b": float("inf"), "c": 1.0},
                 settings=settings_at(tmp_path))
    payload = json.loads(text, parse_constant=strict)
    assert payload["a"] is None and payload["b"] is None
    assert payload["c"] == 1.0
    assert payload["non_finite_values"]["replaced_with_null"] == 2


def test_ordinary_payloads_are_untouched(tmp_path: Path) -> None:
    payload = {"x": 1.5, "y": [1, 2, 3], "z": "text"}
    out = json.loads(_emit(payload, settings=settings_at(tmp_path)))
    assert "non_finite_values" not in out
    assert out["x"] == 1.5


def test_columnar_rows_carry_no_nan() -> None:
    result = {"map_data": {"features": [feature(
        {"coordinates": [[[float("nan"), 0], [1, 0], [1, 1], [0, 1],
                          [float("nan"), 0]]]},
        {"tile_id": 1, "average_temperature": 30.0})]}}
    for row in to_columnar(result, 5)["rows"]:
        for cell in row:
            assert not (isinstance(cell, float) and math.isnan(cell))


# --------------------------------------------------------------------------- #
# MEDIUM-6 — the cache key ignored who asked and where
# --------------------------------------------------------------------------- #

def test_two_accounts_sharing_a_data_dir_do_not_cross_serve(
        tmp_path: Path) -> None:
    body = {"polygon_aoi": {"x": 1}}
    a = ResultStore(settings_at(tmp_path, api_key="key-AAA"))
    b = ResultStore(settings_at(tmp_path, api_key="key-BBB"))
    a.put("act-a", "/v1/heatmap", body, {"from": "account A"})

    assert a.find_by_request("/v1/heatmap", body) is not None
    assert b.find_by_request("/v1/heatmap", body) is None


def test_staging_is_not_answered_with_production_data(tmp_path: Path) -> None:
    body = {"polygon_aoi": {"x": 1}}
    prod = ResultStore(settings_at(tmp_path, api_key="k"))
    staging = ResultStore(settings_at(tmp_path, api_key="k",
                                      base_url="https://staging.fortyguard.com"))
    prod.put("act-a", "/v1/heatmap", body, {"from": "production"})

    assert prod.find_by_request("/v1/heatmap", body) is not None
    assert staging.find_by_request("/v1/heatmap", body) is None


def test_the_scope_is_a_digest_not_the_key(tmp_path: Path) -> None:
    """It lands in filenames and in metadata on disk."""
    store = ResultStore(settings_at(tmp_path, api_key="super-secret-key"))
    scope = store._scope()
    assert "super-secret-key" not in scope
    assert len(scope) == 16


# --------------------------------------------------------------------------- #
# LOW — the raw resource raised where the tool reported
# --------------------------------------------------------------------------- #

async def test_a_resource_whose_payload_vanished_reports_instead_of_raising(
        tmp_path: Path) -> None:
    """
    `get()` succeeds on the metadata sidecar, then `open_bytes()` raised
    FileNotFoundError, which the SDK turned into an opaque ResourceError. The
    equivalent TOOL path already returned structured data.
    """
    ctx = ToolContext(settings=settings_at(tmp_path))
    mcp = build_server(ctx)
    ctx.results.put("a1", "/v1/heatmap", {}, {"map_data": {"features": []}})
    ctx.results.path_for("a1").unlink()

    contents = list(await mcp.read_resource("fortyguard://result/a1"))
    payload = json.loads(contents[0].content)
    assert payload["error"] is True
    assert "unreadable" in payload["message"]
    assert payload["next"] == "check_status"


# --------------------------------------------------------------------------- #
# MEDIUM-5 — secrets covered by a gitignore at every level
# --------------------------------------------------------------------------- #

def test_the_gitignore_covers_env_at_any_depth() -> None:
    """
    A `.env` anywhere in this repository must be ignored, and `.env.example`
    must not be.

    Checked against THIS repository's own `.gitignore`. An earlier version
    asserted against the parent directory of the checkout, which was the
    developer's enclosing project folder - true on that one machine, absent for
    anyone who cloned, and it failed the moment CI ran.

    A bare pattern with no slash applies at every depth in git, so `.env` alone
    is sufficient; `**/.env` is the explicit spelling of the same rule.
    """
    ignore = Path(__file__).resolve().parents[2] / ".gitignore"
    assert ignore.exists(), f"no .gitignore at {ignore.parent}"

    lines = [ln.strip() for ln in ignore.read_text(encoding="utf-8").splitlines()]
    assert any(ln in (".env", "**/.env") for ln in lines), \
        "no pattern ignoring .env at every depth"
    assert "!.env.example" in lines, "the example file must stay tracked"

    # The negation has to come after the pattern it re-includes, or git ignores it.
    env_at = min(i for i, ln in enumerate(lines) if ln in (".env", "**/.env", "*.env"))
    assert lines.index("!.env.example") > env_at, \
        "!.env.example must follow the .env patterns to take effect"


def test_a_very_short_key_does_not_corrupt_the_payload() -> None:
    """
    Found by this suite: with `api_key="k"`, `scrub` replaced every `k` in the
    response, so `"next": "check_status"` was emitted as
    `"chec[REDACTED]_status"`. Silent corruption of field values and messages -
    far worse than the exposure it was guarding, since nothing that short is a
    real credential.
    """
    from fortyguard_mcp.logging_setup import MIN_SCRUBBABLE_KEY, scrub

    assert scrub("check_status", "k") == "check_status"
    assert scrub("a and b", "a") == "a and b"

    real = "x" * MIN_SCRUBBABLE_KEY
    assert real not in scrub(f"key={real}", real)


def test_signed_url_redaction_is_unaffected_by_key_length() -> None:
    """It matches a pattern, not a substring, so length never applies."""
    from fortyguard_mcp.logging_setup import scrub

    url = "https://s.googleapis.com/f.pdf?X-Goog-Signature=deadbeef"
    assert "deadbeef" not in scrub(url, "k")
    assert "deadbeef" not in scrub(url, None)


def test_the_example_file_carries_no_key() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    for line in example.read_text(encoding="utf-8").splitlines():
        if line.startswith("FORTYGUARD_API_KEY="):
            assert line.strip() == "FORTYGUARD_API_KEY="
