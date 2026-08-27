"""
Geometry helpers. Pure local computation - no API calls, no credits.

Everything here REPORTS; nothing gates. Caps vary by plan, so a cap is always
supplied by the caller rather than assumed.

Area is spherical excess on the WGS84 authalic sphere over AUTHALIC latitudes,
not a projection: coverage includes Alaska, Hawaii and Puerto Rico, all far
outside EPSG:5070's design region. Agrees with `pyproj.Geod` to better than
0.0001% at US latitudes. Edges are rhumb lines, indistinguishable at AOI scale.
Cross-checked in tests/unit/test_geo.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal

# WGS84 authalic radius: the sphere with the same surface area as the
# ellipsoid. Using the equatorial radius instead would overstate area by ~0.3%.
_AUTHALIC_RADIUS_M = 6_371_007.181

# WGS84 first eccentricity, from the defining flattening 1/298.257223563.
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)
_WGS84_E = math.sqrt(_WGS84_E2)


def _authalic_lat(phi: float) -> float:
    """
    Geodetic latitude (radians) -> authalic latitude (radians).

    Preserves AREA when the ellipsoid is mapped to a sphere. Using geodetic
    latitude directly is the usual shortcut and costs 0.58% at Anchorage.
    """
    if not math.isfinite(phi):
        # `min(1.0, nan)` returns 1.0, so without this the clamp below would
        # relocate a NaN latitude to the North Pole and measure a polygon there.
        return math.nan
    sin_phi = math.sin(phi)
    es = _WGS84_E * sin_phi
    # q(phi): the authalic ("equal-area") function, Snyder eq. 3-12.
    q = (1.0 - _WGS84_E2) * (
        sin_phi / (1.0 - es * es)
        - (1.0 / (2.0 * _WGS84_E)) * math.log((1.0 - es) / (1.0 + es))
    )
    # q at the pole, used to normalise q into a sine.
    qp = (1.0 - _WGS84_E2) * (
        1.0 / (1.0 - _WGS84_E2)
        - (1.0 / (2.0 * _WGS84_E)) * math.log((1.0 - _WGS84_E) / (1.0 + _WGS84_E))
    )
    # Clamped: floating point can nudge |q/qp| past 1 at the pole, where asin
    # raises rather than returning +/- 90 degrees.
    return math.asin(max(-1.0, min(1.0, q / qp)))


Position = tuple[float, float]
Ring = list[Position]


# --------------------------------------------------------------------------- #
# Coordinate order
# --------------------------------------------------------------------------- #

TranspositionVerdict = Literal["ok", "suspected", "impossible_as_lonlat"]

# Generously drawn: it holds Guam through Puerto Rico and the Aleutians on both
# sides of the antimeridian. Used ONLY for the transposition heuristic below,
# never to reject a request - coverage is FortyGuard's to decide.
_US_LAT_RANGE = (13.0, 72.0)
_US_LON_RANGE = (-180.0, -64.0)
_US_LON_RANGE_EAST = (144.0, 180.0)      # Guam, western Aleutians


def _plausible_us_lon(v: float) -> bool:
    return (_US_LON_RANGE[0] <= v <= _US_LON_RANGE[1]
            or _US_LON_RANGE_EAST[0] <= v <= _US_LON_RANGE_EAST[1])


def check_order(ring: Ring) -> tuple[TranspositionVerdict, str | None]:
    """
    Is this ring [lon, lat], as GeoJSON requires, or were the pairs swapped?

    Three verdicts:

      impossible_as_lonlat  a second element exceeds 90, so it cannot be a
                            latitude. Certain.
      suspected             every pair reads naturally the other way round.
                            Likely, not provable: (30, -95) is a real place.
      ok                    no evidence of transposition.

    Never auto-corrected: silently swapping coordinates turns a typo into a
    confident answer about somewhere the caller never asked about.
    """
    if not ring:
        return ("ok", None)

    for lon, lat in ring:
        # First, because `abs(nan) > 90.0` is False - a NaN latitude would
        # otherwise sail through every test below and be reported "ok".
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return ("impossible_as_lonlat",
                    f"position [{lon}, {lat}] is not a finite coordinate, so "
                    f"nothing can be said about its ordering or anything else.")
        if abs(lat) > 90.0:
            return ("impossible_as_lonlat",
                    f"position [{lon}, {lat}] has {lat} in the latitude slot, "
                    f"which is out of range. GeoJSON positions are [longitude, "
                    f"latitude] - these look transposed.")

    swapped_reads_better = all(
        _US_LAT_RANGE[0] <= lon <= _US_LAT_RANGE[1] and _plausible_us_lon(lat)
        for lon, lat in ring
    )
    if swapped_reads_better:
        return ("suspected",
                "every position reads as [latitude, longitude] rather than the "
                "[longitude, latitude] order GeoJSON requires. If that is what "
                "you meant, swap each pair.")
    return ("ok", None)


# --------------------------------------------------------------------------- #
# Rings
# --------------------------------------------------------------------------- #

def ring_is_closed(ring: Ring) -> bool:
    """A ring is closed when its first and last positions are identical."""
    return len(ring) >= 4 and ring[0] == ring[-1]


def close_ring(ring: Ring) -> Ring:
    """Append the first position if the ring is open. Never mutates the input."""
    if not ring or ring[0] == ring[-1]:
        return list(ring)
    return [*ring, ring[0]]


def _unwrapped_delta(lon1: float, lon2: float) -> float:
    """
    Longitude difference taken the short way round, in (-180, 180].

    Without this an edge from 179.9 to -179.9 reads as a 359.8 degree sweep.

    CONSTANT TIME, deliberately: a `while d > 180: d -= 360` loop needs ~2.8e305
    iterations for a longitude of 1e308 - a valid JSON number - and being
    CPU-bound it would wedge the event loop.
    """
    if not (math.isfinite(lon1) and math.isfinite(lon2)):
        return math.nan
    # Reduce EACH longitude before subtracting: `lon2 - lon1` overflows for
    # finite-but-huge inputs, and `math.remainder(inf, 360)` raises.
    d = math.remainder(math.remainder(lon2, 360.0) - math.remainder(lon1, 360.0),
                       360.0)
    # `remainder` returns [-180, 180]; this interval is (-180, 180], so the
    # exactly-antipodal case is normalised to the positive end.
    return 180.0 if d == -180.0 else d


def require_finite(ring: Ring) -> None:
    """
    Refuse a ring carrying NaN or Infinity.

    `_rings_from_coords` screens the tool path already. This exists because
    these functions are PUBLIC: with a NaN ring, `bbox_of` returns bare `nan`
    and `polygon_area_km2` a confident 15,880 km2.
    """
    for lon, lat in ring:
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError(
                f"position [{lon}, {lat}] is not finite; NaN and Infinity are "
                f"not measurable coordinates"
            )


def polygon_area_km2(ring: Ring) -> float:
    """
    Geodesic area of a closed ring, in square kilometres.

    Spherical excess on the WGS84 authalic sphere, over authalic latitudes.
    Correct at any latitude and across the antimeridian. Winding order is
    irrelevant - the magnitude is taken. Raises on a non-finite coordinate
    rather than returning a number for it.
    """
    require_finite(ring)
    r = close_ring(ring)
    if len(r) < 4:
        return 0.0

    total = 0.0
    for (lon1, lat1), (lon2, lat2) in pairwise(r):
        # sin(authalic), not sin(geodetic) - see `_authalic_lat`.
        total += (math.radians(_unwrapped_delta(lon1, lon2))
                  * (2.0
                     + math.sin(_authalic_lat(math.radians(lat1)))
                     + math.sin(_authalic_lat(math.radians(lat2)))))
    area_m2 = abs(total) * _AUTHALIC_RADIUS_M ** 2 / 2.0
    return area_m2 / 1e6


def bbox_of(ring: Ring) -> tuple[float, float, float, float]:
    """
    (west, south, east, north).

    When the ring crosses the antimeridian the box has WEST > EAST, the GeoJSON
    convention and what `slice_result` expects; a naive min/max would span the
    whole planet the wrong way round.

    Raises on a non-finite coordinate.
    """
    if not ring:
        return (0.0, 0.0, 0.0, 0.0)
    require_finite(ring)
    lats = [lat for _, lat in ring]
    lons = [lon for lon, _ in ring]

    # Accumulate unwrapped longitude, so a crossing shows up as a span leaving
    # [-180, 180] rather than as two clusters at the edges.
    walked = [lons[0]]
    for prev, cur in pairwise(lons):
        walked.append(walked[-1] + _unwrapped_delta(prev, cur))
    w_raw, e_raw = min(walked), max(walked)

    def wrap(v: float) -> float:
        return ((v + 180.0) % 360.0) - 180.0

    if e_raw - w_raw >= 360.0:          # degenerate: wraps the globe
        return (-180.0, min(lats), 180.0, max(lats))

    west, east = wrap(w_raw), wrap(e_raw)
    # `wrap` sends exactly +180 to -180, which turns a box ENDING on the
    # antimeridian into one that appears to CROSS it. East is the maximum edge,
    # so +180 is its representative whenever the walk did not pass through.
    if east == -180.0 and e_raw > w_raw:
        east = 180.0
    return (west, min(lats), east, max(lats))


def edge_lengths_m(ring: Ring) -> list[float]:
    """
    Great-circle length of each edge, in metres.

    Raises on a non-finite coordinate, the same contract as `polygon_area_km2`
    and `bbox_of`: `describe_aoi` rounds these straight into its output, where a
    bare NaN would be invalid JSON.
    """
    require_finite(ring)
    r = close_ring(ring)
    out: list[float] = []
    for (lon1, lat1), (lon2, lat2) in pairwise(r):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(_unwrapped_delta(lon1, lon2))
        # Haversine: stable for short edges, unlike the law of cosines.
        a = (math.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
        out.append(2 * _AUTHALIC_RADIUS_M * math.asin(min(1.0, math.sqrt(a))))
    return out


# --------------------------------------------------------------------------- #
# GeoJSON extraction
# --------------------------------------------------------------------------- #

_MAX_GEOJSON_DEPTH = 24


def extract_rings(aoi: Any, _depth: int = 0) -> list[Ring]:
    """
    Every exterior ring in whatever GeoJSON shape was handed to us.

    Accepts FeatureCollection, Feature, Polygon, MultiPolygon and a bare
    coordinates array - the API accepts more shapes than it documents. Anything
    unrecognised yields an empty list rather than raising: the caller reports
    "could not read it" and the request still goes to the API, whose own message
    is better than ours.
    """
    if _depth > _MAX_GEOJSON_DEPTH or not isinstance(aoi, dict):
        return _rings_from_coords(aoi) if not isinstance(aoi, dict) else []

    t = aoi.get("type")
    if t == "FeatureCollection":
        out: list[Ring] = []
        for f in aoi.get("features") or []:
            out.extend(extract_rings(f, _depth + 1))
        return out
    if t == "Feature":
        return extract_rings(aoi.get("geometry"), _depth + 1)
    if t == "Polygon":
        return _rings_from_coords(aoi.get("coordinates"))
    if t == "MultiPolygon":
        out = []
        for poly in aoi.get("coordinates") or []:
            out.extend(_rings_from_coords(poly))
        return out
    if "coordinates" in aoi:
        return _rings_from_coords(aoi.get("coordinates"))
    return []


def _rings_from_coords(coords: Any) -> list[Ring]:
    """
    Exterior ring only - a hole does not change which outline gets analysed.
    `describe_aoi` reports when one was dropped, so it is never silent.

    NON-FINITE VALUES ARE REFUSED HERE, so every ring reaching a measurement
    function has passed through it. Python's JSON parser accepts `NaN`, and
    `1e308` is valid JSON outright; letting either through fabricates a number.
    """
    if not isinstance(coords, list) or not coords:
        return []
    first = coords[0]
    if not isinstance(first, list) or not first:
        return []
    ring: Ring = []
    for pos in first:
        if isinstance(pos, list | tuple) and len(pos) >= 2:
            try:
                lon, lat = float(pos[0]), float(pos[1])
            except (TypeError, ValueError):
                return []
            if not (math.isfinite(lon) and math.isfinite(lat)):
                return []
            ring.append((lon, lat))
        else:
            return []
    return [ring] if ring else []


def count_dropped_holes(aoi: Any, _depth: int = 0) -> int:
    """
    How many interior rings (holes) were discarded by `extract_rings`.

    Only the exterior ring is measured, so a polygon with a hole has its area
    OVERSTATED. `describe_aoi` says so out loud: an unmentioned overstatement is
    the same class of problem as a silently substituted value.
    """
    if _depth > _MAX_GEOJSON_DEPTH or not isinstance(aoi, dict):
        return 0
    t = aoi.get("type")
    if t == "FeatureCollection":
        return sum(count_dropped_holes(f, _depth + 1) for f in aoi.get("features") or [])
    if t == "Feature":
        return count_dropped_holes(aoi.get("geometry"), _depth + 1)
    if t == "Polygon":
        rings = aoi.get("coordinates")
        return max(0, len(rings) - 1) if isinstance(rings, list) else 0
    if t == "MultiPolygon":
        polys = aoi.get("coordinates")
        if not isinstance(polys, list):
            return 0
        return sum(max(0, len(p) - 1) for p in polys if isinstance(p, list))
    return 0


def count_polygons(aoi: Any, _depth: int = 0) -> int:
    """
    How many geometries this GeoJSON declares, readable or not.

    Counts any geometry with coordinates, so a stray Point registers too.
    Compared against the rings actually extracted, this catches a partial read -
    a MultiPolygon of [valid, NaN-polygon] reporting success with half missing.
    """
    if _depth > _MAX_GEOJSON_DEPTH or not isinstance(aoi, dict):
        return 0
    t = aoi.get("type")
    if t == "FeatureCollection":
        return sum(count_polygons(f, _depth + 1) for f in aoi.get("features") or [])
    if t == "Feature":
        return count_polygons(aoi.get("geometry"), _depth + 1)
    if t == "Polygon":
        return 1
    if t == "MultiPolygon":
        polys = aoi.get("coordinates")
        return len(polys) if isinstance(polys, list) else 0
    return 1 if "coordinates" in aoi else 0


def _boxes_overlap(a: tuple[float, float, float, float],
                   b: tuple[float, float, float, float]) -> bool:
    """Do two bounding boxes intersect? Antimeridian-aware on the longitude axis."""
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    if an < bs or bn < as_:
        return False

    def spans(w: float, e: float) -> list[tuple[float, float]]:
        # A crossing box is two intervals once cut at the antimeridian.
        return [(w, 180.0), (-180.0, e)] if w > e else [(w, e)]

    return any(x0 <= y1 and y0 <= x1
               for x0, x1 in spans(aw, ae) for y0, y1 in spans(bw, be))


def _has_non_finite(obj: Any, depth: int = 0) -> bool:
    """
    Does this GeoJSON carry a NaN or an Infinity anywhere?

    Turns a bare "could not read it" into a message naming the actual problem.
    Depth-bounded so a nested value cannot recurse away the stack.
    """
    if depth > 12:
        return False
    if isinstance(obj, bool):
        return False
    if isinstance(obj, float):
        return not math.isfinite(obj)
    if isinstance(obj, list | tuple):
        return any(_has_non_finite(v, depth + 1) for v in obj)
    if isinstance(obj, dict):
        return any(_has_non_finite(v, depth + 1) for v in obj.values())
    return False


def as_feature_collection(ring: Ring) -> dict[str, Any]:
    """Wrap a ring in the FeatureCollection form the API documents."""
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [
                [[lon, lat] for lon, lat in close_ring(ring)]]},
        }],
    }


# --------------------------------------------------------------------------- #
# Description — reporting only
# --------------------------------------------------------------------------- #

def describe_aoi(aoi: Any) -> dict[str, Any]:
    """
    Everything locally knowable about an AOI. Judges nothing.

    No verdict field, no pass/fail: whether an area is too large, too small or
    outside coverage depends on the plan and on FortyGuard's coverage map,
    neither of which is ours to assert. What comes back is measurement, plus the
    observations that are provably local.
    """
    rings = extract_rings(aoi)
    if not rings:
        if _has_non_finite(aoi):
            return {
                "readable": False,
                "message": ("This geometry contains a coordinate that is NaN or "
                            "Infinity. Those are not valid positions and cannot "
                            "be measured - any number reported for them would be "
                            "invented. Replace them with real coordinates."),
            }
        return {
            "readable": False,
            "message": ("Could not read a polygon out of this value. Expected a "
                        "GeoJSON FeatureCollection, Feature, Polygon or "
                        "MultiPolygon. It can still be sent to the API as-is; "
                        "its validation message will be more specific."),
        }

    parts: list[dict[str, Any]] = []
    observations: list[str] = []
    for i, ring in enumerate(rings):
        verdict, note = check_order(ring)
        if note:
            observations.append(f"ring {i}: {note}")
        closed = ring_is_closed(ring)
        if not closed:
            observations.append(
                f"ring {i}: not closed - the first and last positions differ. "
                f"The API rejects this; append the first position to the end.")
        edges = edge_lengths_m(ring)
        parts.append({
            "index": i,
            "n_positions": len(ring),
            "closed": closed,
            "coordinate_order": verdict,
            "area_km2": round(polygon_area_km2(ring), 6),
            "bbox": [round(v, 6) for v in bbox_of(ring)],
            "shortest_edge_m": round(min(edges), 2) if edges else None,
            "longest_edge_m": round(max(edges), 2) if edges else None,
        })

    declared = count_polygons(aoi)
    if declared > len(rings):
        observations.append(
            f"{declared - len(rings)} of {declared} geometries could not be read "
            f"and are NOT included in anything below - check them for non-finite "
            f"coordinates or malformed positions. The figures here describe only "
            f"the {len(rings)} that were readable.")

    holes = count_dropped_holes(aoi)
    if holes:
        observations.append(
            f"{holes} interior ring(s) were ignored. Only the outer outline is "
            f"measured, so the area below is larger than the polygon-with-holes "
            f"actually encloses.")

    # Bounding-box overlap, not true intersection - cheap and enough to warn.
    # Worded as "may": overlapping boxes do not prove overlapping shapes.
    boxes = [bbox_of(r) for r in rings]
    if any(_boxes_overlap(boxes[i], boxes[j])
           for i in range(len(boxes)) for j in range(i + 1, len(boxes))):
        observations.append(
            "some rings have overlapping bounds, so total_area_km2 may "
            "double-count the shared parts. Per-ring areas are listed below.")

    total = sum(p["area_km2"] for p in parts)
    return {
        "readable": True,
        "n_rings": len(parts),
        "total_area_km2": round(total, 6),
        "total_area_sq_miles": round(total / 2.589988110336, 6),
        "rings": parts,
        "observations": observations or ["none"],
        "note": ("Area is computed on the WGS84 ellipsoid and is accurate at any "
                 "US latitude including Alaska, Hawaii and Puerto Rico; edges "
                 "are treated as rhumb lines, which is indistinguishable from "
                 "geodesic at AOI scale. The total is the SUM across rings. No "
                 "size limit is applied here: caps vary by plan, so the API is "
                 "the authority."),
    }


# --------------------------------------------------------------------------- #
# Splitting
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class SplitPlan:
    """
    What a split would come to, computed by arithmetic alone.

    Cheap enough to answer even when the pieces are far too many to build, so a
    caller asking for something enormous learns the number rather than hitting a
    wall.

    `grid_cells` is an UPPER BOUND, often loose: cells the polygon never touches
    are dropped when the pieces are built.
    """

    nx: int
    ny: int
    grid_cells: int
    nominal_piece_area_km2: float
    bbox_area_km2: float
    bbox: tuple[float, float, float, float]

    def at_grid(self, nx: int, ny: int) -> SplitPlan:
        """
        The same box measured against a different grid.

        The split loop tightens `nx`/`ny` until the worst cell fits, so a plan
        computed before the loop stops describing the grid in use - and
        reporting the stale one produced an "at most" BELOW its own "at least".
        """
        cells = max(1, nx * ny)
        return SplitPlan(
            nx=nx, ny=ny, grid_cells=cells,
            nominal_piece_area_km2=self.bbox_area_km2 / cells,
            bbox_area_km2=self.bbox_area_km2, bbox=self.bbox,
        )


class SplitTooLarge(Exception):
    """
    More pieces than the caller said they could take.

    Carries the plan and how far counting got, so the caller reports a real
    number rather than "too many". `pieces_at_least` is a FLOOR: counting stops
    at the limit, so an absurd request costs bounded work to answer.
    """

    def __init__(self, pieces_at_least: int, limit: int, plan: SplitPlan) -> None:
        self.pieces_at_least = pieces_at_least
        self.limit = limit
        self.plan = plan
        super().__init__(
            f"this split needs more than {pieces_at_least:,} pieces, "
            f"past the {limit:,} requested"
        )


def plan_split(ring: Ring, max_area_km2: float) -> SplitPlan:
    """Size the grid without building it. Pure arithmetic, no allocation."""
    if not math.isfinite(max_area_km2) or max_area_km2 <= 0:
        raise ValueError("max_area_km2 must be a positive, finite number")

    w, s, e, n = bbox_of(ring)
    span_lon = _unwrapped_delta(w, e)
    if span_lon <= 0:
        span_lon += 360.0
    span_lat = n - s

    box_area = polygon_area_km2(
        [(w, s), (w + span_lon, s), (w + span_lon, s + span_lat),
         (w, s + span_lat), (w, s)])
    ratio = box_area / max_area_km2
    if not math.isfinite(ratio):
        raise ValueError(
            f"max_area_km2={max_area_km2!r} is too small to tile this area: the "
            f"grid it implies cannot be counted. Use a larger value.")
    side = max(1, math.ceil(math.sqrt(ratio)))
    return SplitPlan(
        nx=side, ny=side, grid_cells=side * side,
        nominal_piece_area_km2=box_area / (side * side),
        bbox_area_km2=box_area, bbox=(w, s, e, n),
    )


def split_ring(ring: Ring, max_area_km2: float,
               *, max_pieces: int | None = None) -> list[Ring]:
    """
    Cut a ring's bounding box into a grid of tiles each at or under the cap.

    `max_area_km2` is REQUIRED and has no default: caps vary by plan, so a
    default would hand most accounts a wrong number.

    Pieces are axis-aligned rectangles. Cells the polygon does not touch are
    dropped, so they cover the POLYGON rather than its bounding box - for a
    diagonal area that is the difference between a usable answer and thousands
    of empty boxes. A ring already under the cap comes back UNCHANGED, and a
    piece straddling the boundary is kept whole, so the union always covers at
    least the original area.

    `max_pieces` is the caller's too; `SplitTooLarge` reports the real count
    rather than a made-up wall.
    """
    if not math.isfinite(max_area_km2) or max_area_km2 <= 0:
        raise ValueError("max_area_km2 must be a positive, finite number")

    area = polygon_area_km2(ring)
    if area <= max_area_km2:
        # Even the no-split case respects the caller's budget: a MultiPolygon
        # of many small rings is individually under the cap but collectively
        # over it.
        if max_pieces is not None and max_pieces < 1:
            raise SplitTooLarge(1, max_pieces, plan_split(ring, max_area_km2))
        return [close_ring(ring)]

    w, s, e, n = bbox_of(ring)
    span_lon = _unwrapped_delta(w, e)
    if span_lon <= 0:                    # crosses the antimeridian
        span_lon += 360.0
    span_lat = n - s

    # UNWRAPPED longitude - a monotonic axis starting at `w` - so a ring over
    # the Aleutians needs no special cases below. Wrapping happens once, on
    # output.
    unwrapped: Ring = [(w + _unwrapped_delta(w, lon) % 360.0, lat)
                       for lon, lat in close_ring(ring)]

    # Sized from the BOUNDING BOX, not the ring's own area: a thin diagonal AOI
    # has a ring area ~20,000x smaller than its box.
    plan = plan_split(ring, max_area_km2)
    nx, ny = plan.nx, plan.ny

    # Cells are not equal in area, so the factor sizes the AVERAGE and the
    # worst can still be over. Tightening converges in an iteration or two.
    for _ in range(24):
        # `plan.at_grid(nx, ny)`, not `plan`: a refusal must describe the grid
        # it actually refused on.
        tiles = _covering_cells(unwrapped, w, s, span_lon, span_lat, nx, ny,
                                max_pieces, plan.at_grid(nx, ny))
        worst = max((polygon_area_km2(t) for t in tiles), default=0.0)
        if worst <= max_area_km2:
            return [[(_wrap_lon(x), y) for x, y in t] for t in tiles]
        overshoot = math.sqrt(worst / max_area_km2)
        nx = max(nx + 1, math.ceil(nx * overshoot))
        ny = max(ny + 1, math.ceil(ny * overshoot))

    # Never return tiles that break the promise.
    raise ValueError(
        f"Could not tile this area into pieces of {max_area_km2} km2 or less. "
        f"This should not happen for a well-formed polygon - please report it "
        f"with the geometry that caused it."
    )


def _wrap_lon(v: float) -> float:
    return ((v + 180.0) % 360.0) - 180.0


def _covering_cells(unwrapped: Ring, w: float, s: float,
                    span_lon: float, span_lat: float,
                    nx: int, ny: int,
                    max_pieces: int | None, plan: SplitPlan) -> list[Ring]:
    """
    The grid cells the polygon actually touches, in unwrapped longitude.

    Kept when any ring edge meets it, or when its centre lies inside the ring -
    a cell wholly in the interior has no edge crossing it. Both tests err
    towards keeping, so the union always covers the polygon.
    """
    dx = span_lon / nx
    dy = span_lat / ny
    out: list[Ring] = []

    for j in range(ny):
        y0 = s + dy * j
        y1 = s + dy * (j + 1)
        band = _row_lon_range(unwrapped, y0, y1)
        if band is None:
            continue                       # the polygon never reaches this row
        lo, hi = band
        i0 = max(0, int((lo - w) / dx) - 1) if dx > 0 else 0
        i1 = min(nx - 1, int((hi - w) / dx) + 1) if dx > 0 else 0

        for i in range(i0, i1 + 1):
            x0 = w + dx * i
            x1 = w + dx * (i + 1)
            if _cell_touches(unwrapped, x0, y0, x1, y1):
                out.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
                # Stop as soon as the limit is passed. The count is then a
                # floor, which is all the caller needs, and an absurd request
                # costs bounded work to answer.
                if max_pieces is not None and len(out) > max_pieces:
                    raise SplitTooLarge(len(out), max_pieces, plan)
    return out


def _cell_touches(ring: Ring, x0: float, y0: float, x1: float, y1: float) -> bool:
    for (ax, ay), (bx, by) in pairwise(ring):
        if _seg_hits_rect(ax, ay, bx, by, x0, y0, x1, y1):
            return True
    return _point_in_ring((x0 + x1) / 2, (y0 + y1) / 2, ring)


# --------------------------------------------------------------------------- #
# Intersection tests — used to drop grid cells the polygon never touches
# --------------------------------------------------------------------------- #

def _seg_hits_rect(x0: float, y0: float, x1: float, y1: float,
                   xmin: float, ymin: float, xmax: float, ymax: float) -> bool:
    """
    Does the segment touch the axis-aligned rectangle? Liang-Barsky clipping.

    True for a segment lying wholly inside, which is what makes it correct for
    a ring smaller than one grid cell.
    """
    dx, dy = x1 - x0, y1 - y0
    p = (-dx, dx, -dy, dy)
    q = (x0 - xmin, xmax - x0, y0 - ymin, ymax - y0)
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q, strict=True):
        if pi == 0.0:
            if qi < 0.0:
                return False
        else:
            t = qi / pi
            if pi < 0.0:
                if t > u2:
                    return False
                u1 = max(u1, t)
            else:
                if t < u1:
                    return False
                u2 = min(u2, t)
    return u1 <= u2


def _point_in_ring(x: float, y: float, ring: Ring) -> bool:
    """Ray casting. Used to keep cells that lie wholly INSIDE the polygon."""
    inside = False
    for (ax, ay), (bx, by) in pairwise(close_ring(ring)):
        if (ay > y) != (by > y):
            t = (y - ay) / (by - ay)
            if x < ax + t * (bx - ax):
                inside = not inside
    return inside


def _row_lon_range(ring: Ring, y0: float, y1: float) -> tuple[float, float] | None:
    """
    Longitude extent of the ring within a latitude band, or None if it misses.

    What keeps splitting affordable: testing every cell of a 1,377 x 1,377 grid
    took 27.5 s. Nothing is missed - any interior point in a band lies
    horizontally between two boundary crossings.
    """
    lo, hi = math.inf, -math.inf
    for (ax, ay), (bx, by) in pairwise(close_ring(ring)):
        elo, ehi = (ay, by) if ay <= by else (by, ay)
        if ehi < y0 or elo > y1:
            continue
        if ay == by:
            xs = (ax, bx)
        else:
            def x_at(y: float, ax: float = ax, ay: float = ay,
                     bx: float = bx, by: float = by) -> float:
                t = max(0.0, min(1.0, (y - ay) / (by - ay)))
                return ax + t * (bx - ax)
            xs = (x_at(max(y0, elo)), x_at(min(y1, ehi)))
        lo, hi = min(lo, *xs), max(hi, *xs)
    return None if lo > hi else (lo, hi)
