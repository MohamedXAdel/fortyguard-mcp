"""
Audit round 9 — the file-by-file sweep.

Round 8 read the modules most likely to be wrong. Round 9 read the rest, line by
line: `sourcing.py`, `pending.py`, `errors.py`, `logging_setup.py`,
`__main__.py`, and the parts of `server.py` not covered before.

Two of the five findings are the SAME defect round 8 had just fixed, sitting in
a sibling module that the sweep missed. That is the audit record's oldest
lesson arriving again: fixing one member of a class without sweeping the class.

Every test here was verified to FAIL against the pre-fix source.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from fortyguard_mcp.client.errors import APIError, TaskFailed
from fortyguard_mcp.client.results import Tile
from fortyguard_mcp.config import Settings
from fortyguard_mcp.store.pending import PendingStore
from fortyguard_mcp.tools.sourcing import _nearest_tile

# --------------------------------------------------------------------------- #
# 38. _nearest_tile ignored the antimeridian
# --------------------------------------------------------------------------- #

def test_nearest_tile_crosses_the_antimeridian() -> None:
    """
    The most serious finding of the round: a WRONG TEMPERATURE, silently.

    A plain `t.lon - lon` reads the 0.2 degrees between 179.9 and -179.9 as
    359.8, so every tile on the far side of the line looked half a world away.

    Measured before the fix: a query at -179.9997 with a tile 158 m east at
    179.998 and another 34 km west at -179.5 returned **the 34 km one** — and
    `source_from_heatmap` then described it in its provenance block as "the
    nearest to the requested point within the heatmap's own area of interest".

    Containment passed, because `_inside` handles wraparound. So the check built
    to prevent exactly this substitution let it through one function later.
    """
    near = Tile(1, 179.998, 51.9, 30.0)      # ~158 m from the query point
    far = Tile(2, -179.5, 51.9, 99.0)        # ~34 km, same side as the query
    best, distance_m = _nearest_tile([near, far], 51.9, -179.9997)

    assert best is not None
    assert best.tile_id == 1, "picked a tile 34 km away over one 158 m away"
    assert best.value == 30.0
    assert distance_m < 500, f"reported {distance_m:.0f} m for a 158 m separation"


def test_nearest_tile_is_unchanged_away_from_the_line() -> None:
    """The wrap fix must not perturb the ordinary case, which is every US metro."""
    tiles = [Tile(1, -112.05, 33.45, 30.0),
             Tile(2, -112.09, 33.45, 31.0),
             Tile(3, -112.20, 33.45, 32.0)]
    best, distance_m = _nearest_tile(tiles, 33.45, -112.06)
    assert best is not None and best.tile_id == 1
    # ~0.01 deg of longitude at 33.45 N is roughly 930 m.
    assert 500 < distance_m < 1500, distance_m


def test_nearest_tile_still_skips_unusable_centroids() -> None:
    usable = Tile(2, -112.05, 33.45, 31.0)
    best, _ = _nearest_tile(
        [Tile(0, None, None, 30.0),
         Tile(1, float("nan"), float("nan"), 30.0),
         usable], 33.45, -112.05)
    assert best is usable


def test_nearest_tile_reports_no_tile_when_none_are_placed() -> None:
    best, distance_m = _nearest_tile([Tile(0, None, None, 30.0)], 33.45, -112.05)
    assert best is None
    assert math.isinf(distance_m)


# --------------------------------------------------------------------------- #
# 39. PendingStore.recall did not catch TypeError
# --------------------------------------------------------------------------- #

WRONG_SHAPES = [
    pytest.param("[]", id="a_list"),
    pytest.param('"x"', id="a_string"),
    pytest.param("null", id="null"),
    pytest.param("123", id="a_number"),
    pytest.param("{}", id="empty_object"),
    pytest.param('{"activity_id": "x"}', id="only_one_key"),
]


@pytest.mark.parametrize("content", WRONG_SHAPES)
def test_a_wrong_shape_pending_record_reads_as_absent(content: str) -> None:
    """
    `recall()` caught `(OSError, ValueError, KeyError)` — and **not**
    `TypeError`, which is what `d["activity_id"]` raises when `json.loads`
    returns a list, a string, a number or None. All four are valid JSON.

    It escaped out of `collect()` as a protocol error while a paid result sat
    waiting to be archived. Identical to the `ResultStore.get()` defect fixed in
    round 8, in the module next door, missed by that sweep.
    """
    tmp = Path(tempfile.mkdtemp())
    store = PendingStore(Settings(api_key="k" * 32, data_dir=tmp))
    store.remember("act-1", "/v1/heatmap", {"a": 1})
    store._path("act-1").write_text(content, encoding="utf-8")

    assert store.recall("act-1") is None


def test_a_healthy_pending_record_still_round_trips() -> None:
    tmp = Path(tempfile.mkdtemp())
    store = PendingStore(Settings(api_key="k" * 32, data_dir=tmp))
    store.remember("act-1", "/v1/heatmap", {"polygon_aoi": {"a": 1}})

    got = store.recall("act-1")
    assert got is not None
    assert got.activity_id == "act-1"
    assert got.endpoint == "/v1/heatmap"
    assert got.request_body == {"polygon_aoi": {"a": 1}}
    assert got.submitted_at


# --------------------------------------------------------------------------- #
# 40. An error whose only readable field was null
# --------------------------------------------------------------------------- #

NO_MESSAGE_BODIES = [
    pytest.param({}, id="empty_object"),
    pytest.param("gateway down", id="plain_string"),
    pytest.param({"non_json_body": "<html>502 Bad Gateway</html>"}, id="html_page"),
    pytest.param(None, id="null_body"),
    pytest.param([1, 2, 3], id="a_list"),
]


@pytest.mark.parametrize("body", NO_MESSAGE_BODIES)
def test_an_api_error_always_carries_a_readable_message(body: object) -> None:
    """
    `to_dict()["message"]` was `self.api_message`, which is None whenever the
    API named no message — an empty 500, a non-dict body, an HTML error page.
    The agent then received an error object whose only human-readable field was
    `null`.

    Not a P1 violation: nothing is rewritten. The API's words are still passed
    through untouched when there are any. This fills the gap when there are none.
    """
    payload = APIError(500, body).to_dict()
    assert payload["message"], f"empty message for body {body!r}"
    assert "500" in payload["message"]
    assert payload["api_message_present"] is False
    # The raw body survives regardless, so nothing is lost.
    assert payload["raw"] == body


def test_a_real_api_message_is_still_passed_through_verbatim() -> None:
    """The fallback must never displace the API's own wording."""
    body = {"message": "Input should be 60, 80 or 100", "field": "granularity"}
    payload = APIError(422, body).to_dict()
    assert payload["message"] == "Input should be 60, 80 or 100"
    assert payload["api_message_present"] is True
    assert payload["field"] == "granularity"


def test_the_other_error_envelope_is_still_read() -> None:
    body = {"details": {"message": "Invalid or unknown API key."}}
    assert APIError(401, body).to_dict()["message"] == "Invalid or unknown API key."


def test_task_failed_also_always_has_a_message() -> None:
    payload = TaskFailed("act-9", "Failed", {}).to_dict()
    assert payload["message"]
    assert "act-9" in payload["message"]
    assert payload["api_message_present"] is False


# --------------------------------------------------------------------------- #
# 41. A financial claim about a state never observed
# --------------------------------------------------------------------------- #

def test_the_failed_credit_claim_is_attributed_not_asserted() -> None:
    """
    `TaskFailed.to_dict()` returned a flat `"credits_consumed": false`.

    A0 ran ~100 live calls and never once saw a `Failed` status — invalid work
    rejects at submit, succeeds emptily at full price, or stays `Processing`
    past any bound. So that was an unqualified financial assertion about a state
    this client has never seen, in a codebase whose whole discipline is not
    stating unmeasured things as fact.

    The same reasoning already keeps a synthesised 402 out of `src/`.
    """
    payload = TaskFailed("act-9", "Failed", {}).to_dict()

    assert "credits_consumed" not in payload, (
        "a bare boolean re-states the vendor's claim as our measurement")
    note = payload["credits_note"].lower()
    assert "documentation" in note or "vendor" in note
    assert "never observed" in note
