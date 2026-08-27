"""
Regression pins for the blind audit of `domain/geo.py`.

Every test here corresponds to a defect that was live in the shipped code and
was found by probing rather than by reading. Kept separate from `test_geo.py`
so the list of things that once went wrong stays legible, and so they run
without pyproj.
"""

from __future__ import annotations

import json
import math
import random
import time
from itertools import pairwise
from typing import Any

import pytest

from fortyguard_mcp.domain.geo import (
    SplitTooLarge,
    _unwrapped_delta,
    bbox_of,
    describe_aoi,
    extract_rings,
    plan_split,
    polygon_area_km2,
    split_ring,
)

PHOENIX = [(-112.15, 33.38), (-112.00, 33.38), (-112.00, 33.50),
           (-112.15, 33.50), (-112.15, 33.38)]
SLIVER = [(-120.0, 35.0), (-100.0, 45.0), (-100.001, 45.0),
          (-120.001, 35.0), (-120.0, 35.0)]
L_SHAPE = [(-112.2, 33.3), (-112.0, 33.3), (-112.0, 33.4), (-112.1, 33.4),
           (-112.1, 33.6), (-112.2, 33.6), (-112.2, 33.3)]
ALEUTIANS = [(179.90, 51.90), (-179.90, 51.90), (-179.90, 52.00),
             (179.90, 52.00), (179.90, 51.90)]


def poly(rings: list[Any]) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": rings}


def covers(pieces: list[Any], px: float, py: float) -> bool:
    for piece in pieces:
        xs = [p[0] for p in piece]
        ys = [p[1] for p in piece]
        if (min(xs) - 1e-9 <= px <= max(xs) + 1e-9
                and min(ys) - 1e-9 <= py <= max(ys) + 1e-9):
            return True
    return False


# --------------------------------------------------------------------------- #
# 1. Denial of service
# --------------------------------------------------------------------------- #

def test_huge_longitude_does_not_hang() -> None:
    """
    `while d > 180: d -= 360` needed ~2.8e305 iterations at 1e308 - an ordinary
    valid JSON number. The loop is synchronous, so one `validate_aoi` call
    wedged the whole event loop and every other session with it.
    """
    started = time.monotonic()
    for a, b in ((0.0, 1e308), (-1e308, 1e308), (1e300, -1e300)):
        d = _unwrapped_delta(a, b)
        assert math.isnan(d) or -180.0 < d <= 180.0
    assert time.monotonic() - started < 1.0


def test_non_finite_longitude_does_not_hang() -> None:
    started = time.monotonic()
    assert math.isnan(_unwrapped_delta(0.0, math.inf))
    assert math.isnan(_unwrapped_delta(-math.inf, 0.0))
    assert math.isnan(_unwrapped_delta(math.nan, 0.0))
    assert time.monotonic() - started < 1.0


def test_delta_still_wraps_correctly() -> None:
    assert _unwrapped_delta(179.0, -179.0) == pytest.approx(2.0)
    assert _unwrapped_delta(-179.0, 179.0) == pytest.approx(-2.0)
    assert _unwrapped_delta(179.0, 180.0) == pytest.approx(1.0)
    assert _unwrapped_delta(0.0, 90.0) == pytest.approx(90.0)
    assert -180.0 < _unwrapped_delta(0.0, 180.0) <= 180.0


def test_a_polygon_with_an_absurd_coordinate_returns_promptly() -> None:
    started = time.monotonic()
    describe_aoi(poly([[[1e308, 33.4], [-112.0, 33.4], [-112.0, 33.5],
                        [1e308, 33.4]]]))
    assert time.monotonic() - started < 1.0


# --------------------------------------------------------------------------- #
# 2. split_ring silently breaking the cap it was given
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,ring", [("diagonal", SLIVER), ("L", L_SHAPE),
                                       ("rectangle", PHOENIX),
                                       ("antimeridian", ALEUTIANS)])
@pytest.mark.parametrize("cap", [50.0, 10.0, 1.0])
def test_no_piece_ever_exceeds_the_cap(name: str, ring: list[Any],
                                       cap: float) -> None:
    """
    The defect: the split factor came from the RING's area while the pieces
    covered its BOUNDING BOX. For a thin diagonal those differ by ~20,000x, so
    asking for 10 km2 pieces returned 225 pieces of up to 8,966 km2 - 897x over -
    while still reporting `max_area_km2: 10`.
    """
    pieces = split_ring(ring, cap)
    assert pieces
    for p in pieces:
        assert polygon_area_km2(p) <= cap * 1.0001, name


def test_empty_cells_are_dropped_for_a_diagonal_area() -> None:
    """
    Tiling the bounding box of a diagonal is almost entirely empty rectangles.
    Dropping the untouched cells is what turns a refusal into a usable answer:
    measured 65x to 459x fewer pieces.
    """
    plan = plan_split(SLIVER, 10.0)
    assert len(split_ring(SLIVER, 10.0)) < plan.grid_cells / 20


def test_pieces_cover_every_vertex_of_the_polygon() -> None:
    for ring, cap in ((SLIVER, 10.0), (L_SHAPE, 1.0), (PHOENIX, 1.0)):
        pieces = split_ring(ring, cap)
        for px, py in ring:
            assert covers(pieces, px, py), (px, py)


def test_pieces_cover_the_interior_not_just_the_outline() -> None:
    """
    A cell wholly inside the polygon has no edge crossing it, so an edge test
    alone would drop it and leave a hole. The centre-inside test catches those.
    """
    rng = random.Random(7)
    pieces = split_ring(L_SHAPE, 1.0)
    pts = [*L_SHAPE, L_SHAPE[0]]

    def inside_l(x: float, y: float) -> bool:
        inside = False
        for (ax, ay), (bx, by) in pairwise(pts):
            if (ay > y) != (by > y):
                t = (y - ay) / (by - ay)
                if x < ax + t * (bx - ax):
                    inside = not inside
        return inside

    sampled = 0
    while sampled < 300:
        x, y = rng.uniform(-112.2, -112.0), rng.uniform(33.3, 33.6)
        if not inside_l(x, y):
            continue
        sampled += 1
        assert covers(pieces, x, y), (x, y)


def test_split_pieces_are_closed_rings_with_wrapped_longitudes() -> None:
    for piece in split_ring(ALEUTIANS, 1.0):
        assert piece[0] == piece[-1]
        for lon, lat in piece:
            assert -180.0 <= lon <= 180.0
            assert -90.0 <= lat <= 90.0


def test_split_is_fast_on_a_pathological_shape() -> None:
    """The naive cell-by-cell scan took 27.5s here; the per-row range fixed it."""
    started = time.monotonic()
    split_ring(SLIVER, 1.0)
    assert time.monotonic() - started < 5.0


# --------------------------------------------------------------------------- #
# 2b. No invented ceiling — the limit belongs to the caller
# --------------------------------------------------------------------------- #

def test_split_has_no_built_in_piece_limit() -> None:
    """
    There was a flat `MAX_SPLIT_PIECES = 20_000`, which protected nothing
    (20,000 pieces is ~85x more JSON than a response can carry) and was derived
    from nothing. The limit now arrives from the caller, who computes it from
    what they can actually deliver.
    """
    assert len(split_ring(SLIVER, 1.0, max_pieces=None)) > 4_000


def test_the_caller_supplied_limit_is_honoured_and_reports_a_real_count() -> None:
    with pytest.raises(SplitTooLarge) as caught:
        split_ring(SLIVER, 1.0, max_pieces=100)
    e = caught.value
    assert e.limit == 100
    assert e.pieces_at_least > 100
    assert e.plan.nx > 1
    assert e.plan.grid_cells > e.pieces_at_least


def test_plan_split_answers_without_building_anything() -> None:
    """Arithmetic only, so an enormous request can still be described."""
    started = time.monotonic()
    plan = plan_split([(-125.0, 25.0), (-67.0, 25.0), (-67.0, 49.0),
                       (-125.0, 49.0), (-125.0, 25.0)], 130.0)
    assert time.monotonic() - started < 0.5
    assert plan.grid_cells > 100_000
    assert plan.nominal_piece_area_km2 == pytest.approx(130.0, rel=0.05)


# --------------------------------------------------------------------------- #
# 3 & 4. Non-finite coordinates
# --------------------------------------------------------------------------- #

def test_nan_latitude_is_refused_not_measured() -> None:
    """
    `min(1.0, nan)` is `1.0` in Python, so the clamp inside `_authalic_lat`
    silently turned a NaN latitude into 90 degrees North and reported a
    confident 15,880 km2 for a ring with no measurable area at all.
    """
    out = describe_aoi(poly([[[-112.1, float("nan")], [-112.0, 33.4],
                              [-112.0, 33.5], [-112.1, 33.5],
                              [-112.1, float("nan")]]]))
    assert out["readable"] is False
    assert "NaN" in out["message"]
    assert "total_area_km2" not in out


def test_non_finite_never_reaches_the_output_as_a_bare_literal() -> None:
    """
    A bare `NaN` in JSON is invalid and fails the whole tool response - the same
    defect as audit round 1, reintroduced in a new module.
    """
    def strict(c: str) -> None:
        raise AssertionError(f"bare {c} in output")

    for bad in (float("nan"), float("inf"), float("-inf")):
        out = describe_aoi(poly([[[bad, 33.4], [-112.0, 33.4],
                                  [-112.0, 33.5], [bad, 33.4]]]))
        json.loads(json.dumps(out), parse_constant=strict)


def test_non_finite_positions_are_rejected_at_extraction() -> None:
    assert extract_rings(poly([[[float("nan"), 1.0], [2.0, 2.0],
                                [3.0, 3.0], [float("nan"), 1.0]]])) == []


# --------------------------------------------------------------------------- #
# 6. False antimeridian crossing
# --------------------------------------------------------------------------- #

def test_touching_180_is_not_reported_as_crossing_it() -> None:
    """
    `wrap()` maps exactly +180 to -180, so a ring spanning 179..180 came back as
    (179, ..., -180) - and west > east is this module's signal for a crossing.
    """
    w, _s, e, _n = bbox_of([(179.0, 51.0), (180.0, 51.0), (180.0, 52.0),
                            (179.0, 52.0), (179.0, 51.0)])
    assert w < e
    assert (w, e) == pytest.approx((179.0, 180.0))


def test_a_genuine_crossing_still_reports_west_greater_than_east() -> None:
    w, _s, e, _n = bbox_of(ALEUTIANS)
    assert w > e


def test_a_box_starting_at_minus_180_is_unaffected() -> None:
    w, _s, e, _n = bbox_of([(-180.0, 51.0), (-179.0, 51.0), (-179.0, 52.0),
                            (-180.0, 52.0), (-180.0, 51.0)])
    assert w < e
    assert (w, e) == pytest.approx((-180.0, -179.0))


# --------------------------------------------------------------------------- #
# 7 & 8. Silent overstatements in describe_aoi
# --------------------------------------------------------------------------- #

def test_dropped_holes_are_reported() -> None:
    outer = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    inner = [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]]
    out = describe_aoi(poly([outer, inner]))
    assert any("interior ring" in o for o in out["observations"])
    assert any("larger than" in o for o in out["observations"])


def test_no_hole_no_observation() -> None:
    out = describe_aoi(poly([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]))
    assert out["observations"] == ["none"]


def test_overlapping_rings_are_flagged_as_possibly_double_counted() -> None:
    ring = [[p[0], p[1]] for p in PHOENIX]
    out = describe_aoi({"type": "MultiPolygon", "coordinates": [[ring], [ring]]})
    assert out["total_area_km2"] == pytest.approx(
        2 * polygon_area_km2(PHOENIX), rel=1e-6)
    assert any("double-count" in o for o in out["observations"])


def test_disjoint_rings_are_not_flagged() -> None:
    a = [[p[0], p[1]] for p in PHOENIX]
    b = [[-80.0, 40.0], [-79.0, 40.0], [-79.0, 41.0], [-80.0, 41.0], [-80.0, 40.0]]
    out = describe_aoi({"type": "MultiPolygon", "coordinates": [[a], [b]]})
    assert out["observations"] == ["none"]


def test_the_note_says_the_total_is_a_sum() -> None:
    out = describe_aoi(poly([[[p[0], p[1]] for p in PHOENIX]]))
    assert "SUM across" in out["note"]


# --------------------------------------------------------------------------- #
# Second audit round — defects introduced BY the first round's fixes
# --------------------------------------------------------------------------- #

TALL = [(-112.1, 20.0), (-112.0, 20.0), (-112.0, 60.0), (-112.1, 60.0),
        (-112.1, 20.0)]


def test_the_reported_range_contains_its_own_floor() -> None:
    """
    The convergence loop moves the grid but the plan was computed once, before
    it. A refusal on the second iteration therefore quoted an "at most" of
    37,249 against an "at least" of 46,656 - a range that excluded the number it
    was bounding.

    Uses cap=50 rather than cap=1: both take the two-iteration path that exposes
    the bug (verified), but this one builds 961 pieces instead of 46,656, which
    is the difference between a 0.1s test and a 6.3s one.
    """
    full = len(split_ring(TALL, 50.0))
    with pytest.raises(SplitTooLarge) as caught:
        split_ring(TALL, 50.0, max_pieces=full - 1)
    e = caught.value
    assert e.plan.grid_cells >= e.pieces_at_least
    assert e.plan.nx * e.plan.ny == e.plan.grid_cells


def test_at_grid_rescales_the_nominal_piece_area() -> None:
    plan = plan_split(TALL, 1.0)
    tighter = plan.at_grid(plan.nx * 2, plan.ny * 2)
    assert tighter.grid_cells == plan.grid_cells * 4
    assert tighter.nominal_piece_area_km2 == pytest.approx(
        plan.nominal_piece_area_km2 / 4, rel=1e-9)
    assert tighter.bbox == plan.bbox


def test_an_unreadable_sub_polygon_is_announced() -> None:
    """
    MultiPolygon [valid, NaN-polygon] reported `readable: true, n_rings: 1,
    observations: ["none"]` - half the geometry gone, nothing said. The same
    silent omission as the dropped holes and the double-counted overlaps, missed
    while fixing exactly those.
    """
    good = [[p[0], p[1]] for p in PHOENIX]
    bad = [[float("nan"), 1.0], [2.0, 2.0], [3.0, 3.0], [float("nan"), 1.0]]
    out = describe_aoi({"type": "MultiPolygon", "coordinates": [[good], [bad]]})
    assert out["readable"] is True
    assert out["n_rings"] == 1
    assert any("could not be read" in o for o in out["observations"])
    json.loads(json.dumps(out))


def test_a_fully_readable_aoi_gets_no_partial_read_warning() -> None:
    good = [[p[0], p[1]] for p in PHOENIX]
    out = describe_aoi({"type": "MultiPolygon", "coordinates": [[good]]})
    assert out["observations"] == ["none"]


@pytest.mark.parametrize("fn", ["polygon_area_km2", "bbox_of", "edge_lengths_m",
                                "split_ring", "plan_split"])
def test_public_functions_refuse_non_finite_rings(fn: str) -> None:
    """
    The extraction guard covers the tool path, but these are PUBLIC. Called
    directly, `bbox_of` returned `nan` (invalid JSON) and `polygon_area_km2`
    returned a confident 15,880 km2 measured at the North Pole.
    """
    import fortyguard_mcp.domain.geo as geo

    ring = [(-112.1, float("nan")), (-112.0, 33.4), (-112.0, 33.5),
            (-112.1, 33.5), (-112.1, float("nan"))]
    target = getattr(geo, fn)
    args = (ring, 1.0) if fn in ("split_ring", "plan_split") else (ring,)
    with pytest.raises(ValueError, match="not finite"):
        target(*args)


def test_authalic_latitude_propagates_nan_rather_than_clamping_it() -> None:
    """`min(1.0, nan)` is 1.0, which turned NaN into the North Pole."""
    import fortyguard_mcp.domain.geo as geo
    assert math.isnan(geo._authalic_lat(math.nan))
    assert math.isnan(geo._authalic_lat(math.inf))


def test_check_order_does_not_call_a_nan_position_ok() -> None:
    """`abs(nan) > 90.0` is False, so NaN sailed through as 'ok'."""
    from fortyguard_mcp.domain.geo import check_order
    verdict, note = check_order([(float("nan"), 33.4), (-112.0, 33.4)])
    assert verdict == "impossible_as_lonlat"
    assert note and "not a finite coordinate" in note


def test_no_dead_grid_helper_and_no_invented_ceiling_remain() -> None:
    """`_grid` lost its last caller when splitting was rewritten."""
    import inspect

    import fortyguard_mcp.domain.geo as geo
    src = inspect.getsource(geo)
    assert "def _grid(" not in src
    assert "MAX_SPLIT_PIECES" not in src


def test_definitions_precede_their_users() -> None:
    """
    `SplitPlan` / `SplitTooLarge` / `plan_split` were defined after the code
    using them - legal under postponed annotations, and how a comment describing
    a deleted constant ended up sitting on top of a dataclass.
    """
    import inspect

    import fortyguard_mcp.domain.geo as geo
    src = inspect.getsource(geo)
    assert src.index("class SplitPlan") < src.index("def split_ring")
    assert src.index("class SplitTooLarge") < src.index("def _covering_cells")
    assert src.index("def plan_split") < src.index("def split_ring")


def test_the_extraction_docstring_no_longer_claims_a_sole_guard() -> None:
    """It said it was 'the only place that has to care' - which the new
    `require_finite` guards on the public functions made untrue."""
    import fortyguard_mcp.domain.geo as geo
    assert "only place that has to care" not in (geo._rings_from_coords.__doc__ or "")


def test_split_ring_documents_the_under_cap_exception() -> None:
    """
    An under-cap ring comes back unchanged, not as a rectangle.

    The docstring check asserts SUBSTANCE, not spelling. It used to require the
    literal marker "ONE EXCEPTION", which broke on a reword that kept the fact
    intact - a test that pins prose fails on edits that do not change behaviour,
    and teaches nothing when it does.
    """
    doc = (split_ring.__doc__ or "").lower()
    assert "unchanged" in doc and "cap" in doc, (
        "the under-cap behaviour must stay documented: callers rely on getting "
        "their own polygon back rather than its bounding box")
    piece = split_ring(L_SHAPE, 10_000.0)[0]
    assert len(piece) == len(L_SHAPE)


def test_the_area_note_does_not_overclaim_geodesic_edges() -> None:
    out = describe_aoi({"type": "Polygon",
                        "coordinates": [[[p[0], p[1]] for p in PHOENIX]]})
    assert "rhumb" in out["note"]
