"""
Decision D2 — where `temperature` comes from for env_params and heat_intelligence.

Both endpoints require a `temperature`, and FortyGuard's docs say it must match
the heatmap generated for the same location and time. Two ways to supply it,
**mutually exclusive**:

    temperature=                       use it; the caller is authoritative
    from_activity_id= + lat/lon        read it out of that heatmap's result
    both                               error, naming both
    neither                            error, naming both

Mutually exclusive rather than a fallback: a fallback would silently discard
`from_activity_id` and hide any disagreement between the two.

Sourcing is free - it reads a finished result already on local disk. The date
comes from the same place, so temperature and date are consistent by
construction.
"""

from __future__ import annotations

import math
from typing import Any

from ..client.results import Tile, tile_values
from ..domain.geo import _unwrapped_delta, bbox_of, extract_rings
from .runtime import ToolContext


class SourcingError(Exception):
    """Refusal to guess. Carries a structured payload for the tool response."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(payload.get("message", "could not source temperature"))


def _conflict(name: str) -> SourcingError:
    return SourcingError({
        "error": True,
        "source": "fortyguard-mcp",
        "message": (
            f"Supply either {name}= or from_activity_id=, not both. They can "
            f"disagree, and there is no correct way to pick a winner: choosing "
            f"one silently would hide the disagreement. Drop whichever is not "
            f"the value you meant."
        ),
    })


def _missing() -> SourcingError:
    return SourcingError({
        "error": True,
        "source": "fortyguard-mcp",
        "message": (
            "This endpoint requires a temperature. Either pass temperature= "
            "directly, or pass from_activity_id= naming a completed heatmap "
            "covering this coordinate - the temperature and the matching date "
            "are then read out of that result. Sourcing costs no credits."
        ),
        "next": "submit_heatmap",
    })


def _nearest_tile(tiles: list[Tile], lat: float, lon: float) -> tuple[Tile | None, float]:
    """
    Closest tile centroid, and its distance in metres.

    Equirectangular with a cos(lat) correction: exact enough at 60-100 m tile
    spacing and far cheaper than haversine across 36,000 tiles.

    Longitude is taken the SHORT WAY ROUND - a plain `t.lon - lon` reads the
    0.2 degrees across the antimeridian as 359.8, so a tile 158 m away loses to
    one 34 km away. `_unwrapped_delta` is reused rather than reimplemented.
    """
    best: Tile | None = None
    best_d = math.inf
    coslat = math.cos(math.radians(lat))
    for t in tiles:
        if t.lon is None or t.lat is None:
            continue
        if not (math.isfinite(t.lon) and math.isfinite(t.lat)):
            # A NaN distance compares False against everything, so such a tile
            # could never win anyway; skipping it says so explicitly.
            continue
        dx = _unwrapped_delta(lon, t.lon) * coslat
        dy = t.lat - lat
        d = dx * dx + dy * dy
        if d < best_d:
            best_d, best = d, t
    if best is None:
        return (None, math.inf)
    return (best, math.sqrt(best_d) * 111_320.0)


def _aoi_boxes(request_body: Any) -> list[tuple[float, float, float, float]]:
    """
    A bounding box per ring of the heatmap's area of interest.

    EVERY ring, not just the first: a point inside the second polygon of a
    MultiPolygon AOI is genuinely covered and must not be refused as outside it.
    """
    if not isinstance(request_body, dict):
        return []
    return [bbox_of(r) for r in extract_rings(request_body.get("polygon_aoi"))]


def _tile_bbox(tiles: list[Tile]) -> tuple[float, float, float, float] | None:
    """
    Bounds of the tiles that actually came back — the fallback containment test.

    `bbox_of` rather than min/max, so a result over the Aleutians produces a
    west > east box instead of one spanning the globe.

    Conservative: these are tile CENTROIDS, so the box sits half a tile inside
    real coverage and a boundary point is refused rather than answered.
    Non-finite centroids are filtered, not merely `None` ones.
    """
    placed = [(t.lon, t.lat) for t in tiles
              if t.lon is not None and t.lat is not None
              and math.isfinite(t.lon) and math.isfinite(t.lat)]
    return bbox_of(placed) if placed else None


def _inside(bbox: tuple[float, float, float, float], lat: float, lon: float) -> bool:
    w, s, e, n = bbox
    if not (s <= lat <= n):
        return False
    # w > e means the box crosses the antimeridian - the Aleutians do.
    return (lon >= w or lon <= e) if w > e else (w <= lon <= e)


def source_from_heatmap(
    tool_ctx: ToolContext, activity_id: str, lat: float, lon: float
) -> dict[str, Any]:
    """
    Read a temperature (and the matching date) out of a stored heatmap.

    Refuses rather than returning something plausible when the activity_id is
    not archived, the coordinate is outside that heatmap's AOI, or there are no
    tiles. The middle one matters most: every heatmap has a nearest tile to any
    point on Earth, so no containment check means a Phoenix question gets a
    Boston temperature with no sign of it.
    """
    stored = tool_ctx.results.get(activity_id)
    if stored is None:
        raise SourcingError({
            "error": True,
            "source": "fortyguard-mcp",
            "message": (
                f"No stored result for activity_id {activity_id!r}. Only results "
                f"this server has collected can be read from; if the heatmap was "
                f"run elsewhere, call check_status('{activity_id}') first to "
                f"fetch and archive it, or pass temperature= directly."
            ),
            "next": "check_status",
        })

    # Checked BEFORE the tiles: pointing this at a stored env_params result
    # would otherwise fall through to the empty-tiles branch and report that a
    # heatmap completed with no tiles - wrong twice over.
    if stored.endpoint not in ("/v1/heatmap", "unknown"):
        raise SourcingError({
            "error": True,
            "source": "fortyguard-mcp",
            "message": (
                f"activity_id {activity_id!r} is a {stored.endpoint} result, not "
                f"a heatmap, so it carries no temperature grid to read from. "
                f"Point from_activity_id at a completed heatmap covering this "
                f"location, or pass temperature= directly."
            ),
            "stored_endpoint": stored.endpoint,
        })

    result = stored.load()
    if result is None:
        raise SourcingError({
            "error": True,
            "source": "fortyguard-mcp",
            "message": f"The stored payload for {activity_id!r} is missing from disk.",
        })

    tiles = tile_values(result)
    if not tiles:
        raise SourcingError({
            "error": True,
            "source": "fortyguard-mcp",
            "message": (
                f"Heatmap {activity_id} completed with no tiles, so there is no "
                f"temperature in it to read. Empty results still consume credits; "
                f"common causes are an area outside coverage, an area below the "
                f"minimum size, or a date with no data."
            ),
        })

    # Against the REQUESTED area when it can be read, otherwise against the
    # tiles that came back. Never skipped: a result collected after a restart
    # has no request body on record, and skipping there would accept any
    # coordinate on Earth.
    boxes = _aoi_boxes(stored.request_body)
    if not boxes:
        fallback = _tile_bbox(tiles)
        boxes = [fallback] if fallback is not None else []
    if not boxes:
        raise SourcingError({
            "error": True,
            "source": "fortyguard-mcp",
            "message": (
                f"Heatmap {activity_id} has no readable area of interest and no "
                f"tile carried usable coordinates, so there is no way to confirm "
                f"({lat}, {lon}) is inside it. Pass temperature= directly."
            ),
        })
    # ANY ring - a MultiPolygon AOI covers all of them.
    if not any(_inside(b, lat, lon) for b in boxes):
        raise SourcingError({
            "error": True,
            "source": "fortyguard-mcp",
            "message": (
                f"({lat}, {lon}) is outside the area of interest of heatmap "
                f"{activity_id}, whose bounds are "
                f"{[[round(v, 5) for v in b] for b in boxes]}. Substituting the "
                f"nearest tile would give you a temperature from somewhere you "
                f"did not ask about. Use a heatmap covering this point, or pass "
                f"temperature= directly."
            ),
            "aoi_bbox": [[round(v, 6) for v in b] for b in boxes],
        })

    tile, distance_m = _nearest_tile(tiles, lat, lon)
    if tile is None:
        raise SourcingError({
            "error": True,
            "source": "fortyguard-mcp",
            "message": (
                f"Heatmap {activity_id} has tiles, but none carried usable "
                f"coordinates, so none can be matched to ({lat}, {lon})."
            ),
        })

    req = stored.request_body if isinstance(stored.request_body, dict) else {}
    dt = req.get("date_time") if isinstance(req.get("date_time"), dict) else {}

    return {
        "temperature": tile.value,
        "date_time": dt or None,
        "provenance": {
            "from_activity_id": activity_id,
            "tile_id": tile.tile_id,
            "tile_centroid": [tile.lon, tile.lat],
            "distance_from_requested_point_m": round(distance_m, 1),
            "credits_charged": 0,
            "note": ("Read from a stored heatmap result. No API call, no credits. "
                     "The tile centroid is the nearest to the requested point "
                     "within the heatmap's own area of interest."),
        },
    }


def resolve_temperature(
    tool_ctx: ToolContext,
    *,
    lat: float,
    lon: float,
    temperature: float | None,
    from_activity_id: str | None,
) -> tuple[float, dict[str, Any] | None, dict[str, Any] | None]:
    """
    Apply the D2 rule. Returns (temperature, sourced_date_time, provenance).

    Raises `SourcingError` for both the conflict and the omission, so the
    caller reports the same structured refusal either way.
    """
    if temperature is not None and from_activity_id is not None:
        raise _conflict("temperature")
    if temperature is not None:
        return (temperature, None, None)
    if from_activity_id is None:
        raise _missing()

    sourced = source_from_heatmap(tool_ctx, from_activity_id, lat, lon)
    return (
        float(sourced["temperature"]),
        sourced.get("date_time"),
        sourced["provenance"],
    )


def resolve_date(
    explicit: Any, sourced: dict[str, Any] | None, *, field_name: str
) -> Any:
    """
    Same exclusivity rule for the date: an explicit date and a sourced one are
    a conflict, not a precedence question.
    """
    if explicit is not None and sourced is not None:
        raise _conflict(field_name)
    return explicit if explicit is not None else sourced
