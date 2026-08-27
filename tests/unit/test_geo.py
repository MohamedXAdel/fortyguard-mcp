"""
Geometry — verified against pyproj where possible, not asserted.

pyproj is a DEV-TIME cross-check only. The package deliberately does not depend
on it: PROJ is a large native dependency to carry for one function, and the
CONUS Albers projection the plan originally called for is wrong for a service
covering Alaska, Hawaii and Puerto Rico. These tests are what justify that
choice, so they skip rather than pass when pyproj is absent.
"""

from __future__ import annotations

import math

import pytest

from fortyguard_mcp.domain.geo import (
    as_feature_collection,
    bbox_of,
    check_order,
    close_ring,
    describe_aoi,
    edge_lengths_m,
    extract_rings,
    polygon_area_km2,
    ring_is_closed,
    split_ring,
)

# NOT `pytest.importorskip` at module scope. That skipped the ENTIRE FILE when
# pyproj was absent - all 35 tests, though only ONE of them needs it - and pyproj
# is absent in exactly the two environments that are supposed to be
# authoritative: the clean-room venv (`pip install -e ".[dev]"`) and CI, which
# installs nothing else on purpose. So the unit tests for `geo.py`, the most
# defect-dense module in the project, had never once run in CI.
#
# A gate that silently does not run is not a gate. The skip now covers only the
# cross-check that genuinely requires the dependency.


def box(w: float, s: float, e: float, n: float) -> list[tuple[float, float]]:
    return [(w, s), (e, s), (e, n), (w, n), (w, s)]


# Deliberately spread across the extremes of US coverage: the whole reason for
# not using EPSG:5070 is that CONUS is only part of the story.
US_CASES = {
    "phoenix_1km2": box(-112.08, 33.44, -112.07, 33.45),
    "phoenix_metro": box(-112.15, 33.38, -112.00, 33.50),
    "anchorage": box(-149.95, 61.18, -149.80, 61.25),
    "utqiagvik_71N": box(-156.80, 71.28, -156.70, 71.33),
    "honolulu": box(-157.87, 21.29, -157.80, 21.34),
    "san_juan_pr": box(-66.12, 18.44, -66.05, 18.49),
    "aleutians_antimeridian": [(179.90, 51.90), (-179.90, 51.90),
                               (-179.90, 52.00), (179.90, 52.00), (179.90, 51.90)],
}


@pytest.mark.parametrize("name", sorted(US_CASES))
def test_area_matches_pyproj_geodesic(name: str) -> None:
    """
    Agreement with the ellipsoidal geodesic area, everywhere the API covers.

    The tolerance is tight on purpose. An earlier version used geodetic
    latitudes directly in the spherical formula and was 0.58% out at Anchorage —
    which passes any loose tolerance while being wrong in exactly the place the
    projection choice was supposed to fix.
    """
    # Imported HERE, not at module scope: this is the only test in the file that
    # needs pyproj, and skipping the whole module for it hid 34 other tests.
    pyproj = pytest.importorskip("pyproj", reason="dev-time cross-check only")
    ring = US_CASES[name]
    geod = pyproj.Geod(ellps="WGS84")
    ref = abs(geod.polygon_area_perimeter(
        [p[0] for p in ring], [p[1] for p in ring])[0]) / 1e6
    got = polygon_area_km2(ring)
    assert got == pytest.approx(ref, rel=1e-5), f"{name}: {got} vs {ref}"


def test_antimeridian_area_is_not_the_whole_planet() -> None:
    """A ring spanning 179.9E to 179.9W is 0.2 degrees wide, not 359.8."""
    area = polygon_area_km2(US_CASES["aleutians_antimeridian"])
    assert 100 < area < 300


def test_antimeridian_bbox_keeps_west_greater_than_east() -> None:
    """
    The convention `slice_result` already relies on: w > e means the box crosses
    the antimeridian. A naive min/max would return (-179.9, ..., 179.9), a box
    spanning the globe the wrong way.
    """
    w, s, e, n = bbox_of(US_CASES["aleutians_antimeridian"])
    assert w > e
    assert (w, e) == pytest.approx((179.90, -179.90))
    assert (s, n) == pytest.approx((51.90, 52.00))


def test_winding_order_does_not_change_area() -> None:
    ring = US_CASES["phoenix_metro"]
    assert polygon_area_km2(ring) == pytest.approx(polygon_area_km2(ring[::-1]))


# --------------------------------------------------------------------------- #
# Rings
# --------------------------------------------------------------------------- #

def test_ring_closure() -> None:
    closed = box(-112.1, 33.4, -112.0, 33.5)
    assert ring_is_closed(closed)
    assert not ring_is_closed(closed[:-1])
    assert close_ring(closed[:-1]) == closed


def test_close_ring_does_not_mutate_input() -> None:
    original = box(-112.1, 33.4, -112.0, 33.5)[:-1]
    snapshot = list(original)
    close_ring(original)
    assert original == snapshot


def test_open_ring_area_equals_closed_ring_area() -> None:
    ring = US_CASES["phoenix_metro"]
    assert polygon_area_km2(ring[:-1]) == pytest.approx(polygon_area_km2(ring))


def test_edge_lengths_are_plausible() -> None:
    """A 0.01 degree latitude step is ~1.11 km anywhere on Earth."""
    edges = edge_lengths_m(box(-112.08, 33.44, -112.07, 33.45))
    verticals = sorted(edges)[-2:]
    for e in verticals:
        assert 1050 < e < 1150


# --------------------------------------------------------------------------- #
# Coordinate order — reported, never corrected
# --------------------------------------------------------------------------- #

def test_transposed_is_certain_when_latitude_is_impossible() -> None:
    verdict, note = check_order([(33.44, -112.08), (33.45, -112.08)])
    assert verdict == "impossible_as_lonlat"
    assert note and "-112.08" in note


def test_transposition_suspected_for_us_shaped_swap() -> None:
    """
    (33.44, -75.0) is ambiguous rather than impossible: -75 is a legal latitude
    (it is in the Southern Ocean), so nothing rules the pair out. But read the
    other way it is Cape Hatteras, which is overwhelmingly more likely for a US
    heat API. Reported as `suspected`, never corrected.
    """
    verdict, note = check_order([(33.44, -75.0), (33.45, -75.0)])
    assert verdict == "suspected"
    assert note


def test_a_real_southern_ocean_point_is_only_suspected_not_rejected() -> None:
    """
    The soft heuristic must stay soft. (33.44, -75.0) genuinely exists, so the
    verdict has to leave room for the caller to have meant it - which is why
    `suspected` is a separate verdict from `impossible_as_lonlat`.
    """
    verdict, _ = check_order([(33.44, -75.0)])
    assert verdict != "impossible_as_lonlat"


def test_correct_order_is_not_flagged() -> None:
    verdict, note = check_order(box(-112.08, 33.44, -112.07, 33.45))
    assert verdict == "ok"
    assert note is None


def test_order_is_never_silently_corrected() -> None:
    """
    describe_aoi reports the problem and still returns the geometry as given.
    Swapping it for the caller would turn a typo into a confident answer about
    the wrong continent.
    """
    swapped = {"type": "Polygon", "coordinates": [
        [[33.44, -112.08], [33.45, -112.08], [33.45, -112.07],
         [33.44, -112.07], [33.44, -112.08]]]}
    out = describe_aoi(swapped)
    assert out["rings"][0]["coordinate_order"] == "impossible_as_lonlat"
    assert any("transposed" in o for o in out["observations"])
    assert "corrected" not in str(out)


# --------------------------------------------------------------------------- #
# GeoJSON shapes
# --------------------------------------------------------------------------- #

def test_extract_rings_accepts_every_shape_the_api_does() -> None:
    ring = box(-112.1, 33.4, -112.0, 33.5)
    coords = [[list(p) for p in ring]]
    polygon = {"type": "Polygon", "coordinates": coords}
    feature = {"type": "Feature", "properties": {}, "geometry": polygon}
    fc = {"type": "FeatureCollection", "features": [feature]}
    multi = {"type": "MultiPolygon", "coordinates": [coords]}

    for shape in (polygon, feature, fc, multi):
        got = extract_rings(shape)
        assert len(got) == 1
        assert got[0] == ring


def test_unreadable_input_reports_rather_than_raises() -> None:
    for junk in (None, 42, "polygon", {}, {"type": "Point", "coordinates": [1, 2]}):
        out = describe_aoi(junk)
        assert out["readable"] is False
        assert "message" in out


def test_round_trip_through_feature_collection() -> None:
    ring = box(-112.1, 33.4, -112.0, 33.5)
    assert extract_rings(as_feature_collection(ring))[0] == ring


def test_describe_reports_both_area_units() -> None:
    out = describe_aoi(as_feature_collection(US_CASES["phoenix_metro"]))
    km2 = out["total_area_km2"]
    assert out["total_area_sq_miles"] == pytest.approx(km2 / 2.589988110336, rel=1e-9)


def test_describe_applies_no_limit() -> None:
    """
    P2: caps are account-specific. A continent-sized AOI is described, not
    judged - no verdict, no pass/fail, no threshold anywhere in the output.
    """
    out = describe_aoi(as_feature_collection(box(-120.0, 30.0, -100.0, 45.0)))
    assert out["readable"] is True
    assert out["total_area_km2"] > 1_000_000
    assert "verdict" not in out
    for banned in ("too large", "exceeds", "limit exceeded", "rejected"):
        assert banned not in str(out).lower()


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #

def test_split_requires_an_explicit_cap() -> None:
    with pytest.raises(TypeError):
        split_ring(US_CASES["phoenix_metro"])  # type: ignore[call-arg]


def test_split_rejects_a_nonsense_cap() -> None:
    for bad in (0.0, -5.0):
        with pytest.raises(ValueError):
            split_ring(US_CASES["phoenix_metro"], bad)


@pytest.mark.parametrize("cap", [1.0, 10.0, 50.0, 130.0])
def test_every_piece_is_under_the_cap(cap: float) -> None:
    pieces = split_ring(US_CASES["phoenix_metro"], cap)
    assert pieces
    for p in pieces:
        assert polygon_area_km2(p) <= cap * 1.0001


def test_split_covers_at_least_the_original() -> None:
    """
    Pieces tile the bounding box, so their total is >= the original area.
    Erring outward is deliberate: a gap in the middle of a study area is a
    silent hole in the data, while overlap is merely extra cost the caller can
    see.
    """
    ring = US_CASES["phoenix_metro"]
    total = sum(polygon_area_km2(p) for p in split_ring(ring, 10.0))
    assert total >= polygon_area_km2(ring) * 0.999


def test_small_area_is_returned_whole() -> None:
    ring = US_CASES["phoenix_1km2"]
    pieces = split_ring(ring, 130.0)
    assert len(pieces) == 1
    assert ring_is_closed(pieces[0])


def test_split_across_the_antimeridian_stays_local() -> None:
    """
    Every piece must land near the antimeridian, not be scattered across the
    Pacific by a longitude that was never unwrapped.
    """
    pieces = split_ring(US_CASES["aleutians_antimeridian"], 10.0)
    assert len(pieces) > 1
    for p in pieces:
        for lon, lat in p:
            assert abs(lon) > 179.0
            assert 51.5 < lat < 52.5
        assert polygon_area_km2(p) <= 10.0001


def test_split_pieces_are_closed_rings() -> None:
    for p in split_ring(US_CASES["phoenix_metro"], 5.0):
        assert ring_is_closed(p)


def test_split_terminates_on_a_degenerate_ring() -> None:
    """A zero-area ring must not spin the tightening loop."""
    degenerate = [(-112.0, 33.0)] * 5
    pieces = split_ring(degenerate, 1.0)
    assert pieces and all(math.isfinite(polygon_area_km2(p)) for p in pieces)
