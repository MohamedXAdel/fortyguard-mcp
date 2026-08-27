"""
Reading and shaping heatmap results.

The accessors here are internal plumbing, so that slicing, sorting and size
estimation can work across the three payload shapes the API returns. They are
deliberately NOT the agent-facing contract: the agent receives the raw response,
so a shape change reaches it as itself rather than as our stale reading of it.

The three shapes, all from `/v1/heatmap`:

    default / tcm     properties: {tile_id, average_temperature, min_, max_}
                      stats_data: {temperature_stats: {minimum, maximum, ...}, ...}

    analytic          properties: {tile_id, value}
                      stats_data: {analytic_type, units, n_cells, min, max, mean}

    empty             no features at all
                      stats_data: {activity_id, n_cells: 0}   <- no minimum exists

Response policy: return raw until raw does not fit, then report what exists and
let the caller choose how to fetch it. Nothing is silently transformed or
truncated.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..domain.api_schema import classify_result_shape

# Chars-per-token, measured with tiktoken over the recorded fixtures: raw
# GeoJSON 2.82-2.94, columnar 1.86-1.93. Rounded DOWN from the observed minimum,
# since over-estimating summarises early and under-estimating overshoots.
# tiktoken is not Claude's tokenizer, so treat these as +/-20% - which is why
# the budget is a soft policy, not a hard limit.
CHARS_PER_TOKEN_JSON = 2.8
CHARS_PER_TOKEN_DENSE = 1.85


@dataclass(slots=True, frozen=True)
class Tile:
    tile_id: int
    # None when the geometry was unusable. Not NaN: `json.dumps` emits a bare
    # `NaN` literal, which no strict parser accepts.
    lon: float | None
    lat: float | None
    value: float
    vmin: float | None = None
    vmax: float | None = None


@dataclass(slots=True, frozen=True)
class Stats:
    minimum: float
    maximum: float
    mean: float
    std: float | None
    spread: float


# --------------------------------------------------------------------------- #
# Accessors — internal only
# --------------------------------------------------------------------------- #

def features_of(result: Any) -> list[Any]:
    """
    The features list, or empty.

    `list[Any]`, not `list[dict]`: this is an external payload, and declaring
    the stronger type made the malformed-element guards in `tile_values` look
    unreachable to the type checker while staying reachable at runtime.
    """
    if not isinstance(result, dict):
        return []
    map_data = result.get("map_data")
    if not isinstance(map_data, dict):
        return []
    features = map_data.get("features")
    return features if isinstance(features, list) else []


def n_cells(result: Any) -> int:
    """Prefer the reported count; fall back to counting features."""
    stats = (result or {}).get("stats_data") or {} if isinstance(result, dict) else {}
    reported = stats.get("n_cells")
    if isinstance(reported, int):
        return reported
    return len(features_of(result))


def _centroid(feature: dict[str, Any]) -> tuple[float | None, float | None]:
    """
    Centroid, or (None, None) when the geometry is unusable.

    Everything touching external shape is inside the guard: a Point geometry,
    whose `coordinates` is `[lon, lat]` rather than a ring, would otherwise
    raise after the analysis had already been paid for.
    """
    try:
        ring = (feature.get("geometry") or {}).get("coordinates") or [[]]
        pts = ring[0][:-1] if ring and ring[0] else []
        if not pts:
            return (None, None)
        lon = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
    except (TypeError, IndexError, KeyError, ValueError, ZeroDivisionError):
        return (None, None)
    # Non-finite is unusable in the same way malformed geometry is.
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return (None, None)
    return (lon, lat)


def tile_values(result: Any) -> list[Tile]:
    """
    Tiles as (id, lon, lat, value), across all shapes.

    Centroids rather than rings - lossy by about a centimetre on a 100 m tile.
    Fine for ranking and slicing; exact rings stay in the raw payload.
    """
    out: list[Tile] = []
    for f in features_of(result):
        # One malformed feature must not cost the caller the other 526.
        if not isinstance(f, dict):
            continue
        props = f.get("properties")
        if not isinstance(props, dict):
            continue
        if "average_temperature" in props:
            value = props["average_temperature"]
            # Converted, not passed through: `max_temperature: "hot"` would
            # put a string in a numeric column. Kept as separate columns
            # whatever their values - collapsing them when they equal `value`
            # would make the column set data-dependent.
            vmin = _num(props.get("min_temperature"))
            vmax = _num(props.get("max_temperature"))
        elif "value" in props:
            value, vmin, vmax = props["value"], None, None
        else:
            continue
        # An unreadable value skips the tile; an unreadable LABEL does not, so
        # the id falls back to the positional index. `_num` not `float()`:
        # `float("NaN")` succeeds, and a NaN in `value` compares False against
        # everything, so it survives `sorted()` and displaces a real tile.
        numeric = _num(value)
        if numeric is None:
            continue
        try:
            tile_id = int(props.get("tile_id", len(out)))
        except (TypeError, ValueError):
            tile_id = len(out)
        lon, lat = _centroid(f)
        out.append(Tile(tile_id, lon, lat, numeric, vmin, vmax))
    return out


def _round(v: float | None, precision: int) -> float | None:
    """None-safe rounding. Never returns NaN into a JSON payload."""
    return None if v is None else round(v, precision)


def _columns_for(result: Any) -> list[str]:
    """
    Column set is determined by the SHAPE, never by the values.

    Inferring it from the data would mean the same query on two dates returning
    different columns, so an agent indexing `row[4]` silently gets a different
    field. Shape-derived columns are stable by construction.
    """
    base = ["tile_id", "lon", "lat", "value"]
    if classify_result_shape(result) == "temperature":
        return [*base, "min", "max"]
    return base


def _num(v: Any) -> float | None:
    """
    A FINITE float, or None when the value is missing, not a number, or
    non-finite.

    `bool` is excluded because it subclasses `int`, so a stray boolean would
    become a temperature. Non-finite is excluded because `float("NaN")` succeeds,
    so the string `"NaN"` would manufacture a real NaN into a numeric column.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _triplet(d: dict[str, Any], kmin: str, kmax: str,
             kmean: str) -> tuple[float, float, float] | None:
    """All three statistics as floats, or None if any is missing or unreadable."""
    mn, mx, mean = _num(d.get(kmin)), _num(d.get(kmax)), _num(d.get(kmean))
    if mn is None or mx is None or mean is None:
        return None
    return (mn, mx, mean)


def stats_of(result: Any) -> Stats | None:
    """
    Summary statistics, or None when the result is empty or unreadable.

    Empty results genuinely have no minimum - the key does not exist - so None
    is the honest answer rather than a fabricated zero.

    Every field is read defensively. Indexing `stats_data` directly meant any
    drift in it - a renamed key, a null, a string - raised out of
    `get_result_slice` as an opaque protocol error, on a result already paid
    for. A shape change must cost efficiency, never correctness.

    Unreadable is distinguished from absent by `stats_unreadable()`, so the two
    are never conflated.
    """
    if not isinstance(result, dict):
        return None
    raw = result.get("stats_data")
    if not isinstance(raw, dict):
        return None

    ts = raw.get("temperature_stats")
    if isinstance(ts, dict):
        got = _triplet(ts, "minimum", "maximum", "mean")
        if got is not None:
            mn, mx, mean = got
            # Sample std (ddof=1), verified against recomputation. Converted
            # like every sibling field, so a string std cannot travel as one.
            return Stats(mn, mx, mean, _num(ts.get("standard_deviation")), mx - mn)

    got = _triplet(raw, "min", "max", "mean")
    if got is not None:
        mn, mx, mean = got
        return Stats(mn, mx, mean, None, mx - mn)
    return None


def stats_unreadable(result: Any) -> bool:
    """
    Did the payload carry statistics we could not read?

    "No statistics" and "statistics we could not parse" are different facts.
    Reporting both as `null` would hide an API change behind the shape an empty
    result legitimately has.
    """
    if not isinstance(result, dict):
        return False
    raw = result.get("stats_data")
    if not isinstance(raw, dict) or not raw:
        return False
    if stats_of(result) is not None:
        return False
    # An empty result is SUPPOSED to have no statistics - that is its
    # documented shape, not a failure to read.
    return classify_result_shape(result) != "empty"


def units_of(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    raw = result.get("stats_data") or {}
    if "units" in raw:
        return str(raw["units"])
    return "celsius" if "temperature_stats" in raw else None


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #

def estimate_tokens(obj: Any) -> int:
    """
    Approximate token count.

    The ratio is picked by content profile: columnar is almost all numbers and
    tokenises far worse per character than GeoJSON, which repeats key names.
    """
    blob = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    dense = isinstance(obj, dict) and "rows" in obj and "columns" in obj
    ratio = CHARS_PER_TOKEN_DENSE if dense else CHARS_PER_TOKEN_JSON
    return round(len(blob) / ratio)


def to_columnar(result: Any, precision: int = 5) -> dict[str, Any]:
    """
    Compact re-encoding: drop the redundant 5-point ring and the excess digits.

    Measured ~12.5x smaller than raw GeoJSON. 5 decimal places is about 1 m,
    already 100x finer than a 60-100 m tile.
    """
    tiles = tile_values(result)
    columns = _columns_for(result)
    wants_range = "min" in columns

    rows: list[list[Any]] = []
    for t in tiles:
        row: list[Any] = [t.tile_id, _round(t.lon, precision),
                          _round(t.lat, precision), t.value]
        if wants_range:
            row += [t.vmin, t.vmax]
        rows.append(row)

    out: dict[str, Any] = {
        "format": "columnar",
        "note": "Tile centroids, not polygon rings; approximate to ~1 cm. "
                "Request format='geojson' for the exact original payload.",
        "columns": columns,
        "units": units_of(result),
        "n_cells": len(rows),
        "stats": _stats_dict(stats_of(result)),
        "rows": rows,
    }
    _annotate_counts(out, result, len(rows))
    _annotate_stats_gap(out, result)
    no_geom = sum(1 for t in tiles if t.lon is None)
    if no_geom:
        out["tiles_without_geometry"] = no_geom
    return out


def _annotate_stats_gap(out: dict[str, Any], result: Any) -> None:
    """
    Say so when `stats_data` was present but could not be read.

    Without it, unreadable statistics and a genuinely empty result both come
    back as `"stats": null`. The tiles are unaffected and still returned in full.
    """
    if stats_unreadable(result):
        out["stats_note"] = (
            "This result carried a stats_data block that could not be read - "
            "the minimum, maximum or mean was missing or was not a number. The "
            "tiles below are unaffected and complete; only the summary is "
            "unavailable. Statistics can be recomputed from the rows.")


def _annotate_counts(out: dict[str, Any], result: Any, returned: int) -> None:
    """
    Surface a disagreement between the API's reported cell count and the number
    of features present, rather than silently picking one.
    """
    reported = n_cells(result)
    if reported != returned:
        out["n_cells_reported_by_api"] = reported
        out["count_mismatch"] = (
            f"The API reported {reported:,} cells but {returned:,} features were "
            f"present in the payload. Both numbers are shown; neither was "
            f"inferred.")


def _stats_dict(st: Stats | None) -> dict[str, Any] | None:
    if st is None:
        return None
    return {"min": st.minimum, "max": st.maximum, "mean": st.mean,
            "std": st.std, "spread": round(st.spread, 6)}


def empty_result_notice(result: Any) -> str | None:
    """
    A successful call that returned nothing still consumed credits.

    The API reports `Completed` with `n_cells: 0` for out-of-coverage areas,
    sub-minimum AOIs, dates past the forecast edge and archive gaps, charging
    full price for each - easy to miss, because the status says success.

    States what happened, not why: the cause is account- and time-specific.

    ONLY for results that could have had tiles. `n_cells` falls back to
    counting features, and env_params/satellite/streetview/heat_intelligence
    carry no `map_data` at all - so all four read as empty and every successful
    call on them claimed wasted credits. `classify_result_shape` draws the line.
    """
    if not isinstance(result, dict):
        return None
    if classify_result_shape(result) != "empty":
        return None
    if n_cells(result) != 0 or features_of(result):
        return None
    return ("This request completed successfully but returned 0 tiles. "
            "Empty results still consume credits. Common causes include an "
            "area outside coverage, an area below the minimum size, or a "
            "date with no data available.")


def _stats_from_tiles(tiles: list[Tile]) -> dict[str, Any] | None:
    if not tiles:
        return None
    vals = [t.value for t in tiles]
    mn, mx = min(vals), max(vals)
    return {"min": mn, "max": mx, "mean": sum(vals) / len(vals),
            "std": None, "spread": round(mx - mn, 6)}


# --------------------------------------------------------------------------- #
# Response policy
# --------------------------------------------------------------------------- #

def shape_response(
    result: Any,
    *,
    activity_id: str,
    budget_tokens: int | None = None,
    precision: int | None = None,
    fmt: str = "auto",
) -> dict[str, Any]:
    """
    Decide what actually goes back to the caller.

    fmt='geojson'   the raw payload, whatever its size - explicit opt-in
    fmt='columnar'  the compact encoding - explicit opt-in
    fmt='auto'      raw if it fits the budget; otherwise a summary naming every
                    way to retrieve the rest

    Nothing is ever silently transformed or truncated. `budget_tokens` and
    `precision` fall back to configuration when omitted.
    """
    if budget_tokens is None or precision is None:
        s = get_settings()
        budget_tokens = s.inline_token_budget if budget_tokens is None else budget_tokens
        precision = s.coordinate_precision if precision is None else precision

    raw_tokens = estimate_tokens(result)

    notice = empty_result_notice(result)

    if fmt == "geojson":
        out = {"format": "geojson", "activity_id": activity_id,
               "estimated_tokens": raw_tokens, "result": result}
        if notice:
            out["notice"] = notice
        return out

    if fmt == "columnar":
        col = to_columnar(result, precision)
        out = {"activity_id": activity_id,
               "estimated_tokens": estimate_tokens(col), **col}
        if notice:
            out["notice"] = notice
        return out

    if fmt != "auto":
        raise ValueError(f"unknown format {fmt!r}; use auto, geojson or columnar")

    if raw_tokens <= budget_tokens:
        # The API's own response, untouched: anything we add rides on the
        # ENVELOPE so `result` stays byte-identical.
        out = {"format": "raw", "activity_id": activity_id,
               "estimated_tokens": raw_tokens, "result": result}
        if notice:
            out["notice"] = notice
        return out

    # Too large to deliver. Report exactly what exists and how to get it.
    st = stats_of(result)
    cells = n_cells(result)
    col_tokens = estimate_tokens(to_columnar(result, precision))
    return {
        "format": "summary",
        "activity_id": activity_id,
        "reason": "over_budget",
        "n_cells": cells,
        "units": units_of(result),
        "stats": _stats_dict(st),
        "estimated_tokens": {"raw": raw_tokens, "columnar": col_tokens},
        "budget_tokens": budget_tokens,
        "next": "get_result_slice",
        "message": (
            f"Complete and stored locally. The raw payload is ~{raw_tokens:,} "
            f"tokens, over the {budget_tokens:,} budget that format='auto' "
            f"applies, so it is not inlined here. It was NOT truncated and "
            f"costs no further credits to retrieve. The budget applies to "
            f"'auto' only - naming a format overrides it and delivers the whole "
            f"result, however large. Every option below is available; choose "
            f"whichever suits you."
        ),
        # Taking the WHOLE result comes first and is labelled unlimited. The
        # budget exists so `auto` has a safe meaning, not to steer the caller
        # towards a smaller answer - that choice is the caller's.
        "options": _retrieval_options(activity_id, result, cells,
                                      raw_tokens, col_tokens),
    }


def _retrieval_options(activity_id: str, result: Any, cells: int,
                       raw_tokens: int, col_tokens: int) -> list[str]:
    """Only routes that actually return something for THIS result."""
    whole = [
        f"get_result_slice('{activity_id}', format='geojson')  "
        f"- the full raw payload, ~{raw_tokens:,} tokens, no ceiling",
        f"resource fortyguard://result/{activity_id}"
        f"  - the stored payload verbatim",
    ]
    if not has_tiles(result):
        return [
            *whole,
            "(this result has no tiles, so top_n, bbox, every_nth and "
            "format='columnar' would all return an empty table)",
        ]
    return [
        f"get_result_slice('{activity_id}', format='columnar') "
        f"- all {cells:,} tiles, ~{col_tokens:,} tokens, no ceiling",
        *whole,
        f"get_result_slice('{activity_id}', top_n=50)          - highest values only",
        f"get_result_slice('{activity_id}', bbox=[w,s,e,n])    - a sub-area",
        f"get_result_slice('{activity_id}', every_nth=10)      - downsampled",
    ]


def has_tiles(result: Any) -> bool:
    """
    Is this a result that slicing and columnar encoding can act on?

    True only when tiles are present. `map_data` with an empty feature list is
    a heatmap that returned nothing, and slicing it is as pointless as slicing a
    satellite image, so both answer False.
    """
    return bool(features_of(result))


# --------------------------------------------------------------------------- #
# Slicing — reads from stored results, never the API
# --------------------------------------------------------------------------- #

def slice_result(
    result: Any,
    *,
    top_n: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    every_nth: int | None = None,
    precision: int = 5,
) -> dict[str, Any]:
    """Filter tiles locally. Costs nothing: the payload is already on disk."""
    tiles = tile_values(result)
    total = len(tiles)
    applied: list[str] = []

    if bbox is not None:
        w, s, e, n = bbox
        # w > e means the box crosses the antimeridian, which US coverage
        # reaches: the Aleutians extend past 180 degrees. Testing
        # `w <= lon <= e` there matches nothing and reads as "no data".
        crosses = w > e

        def in_box(t: Tile) -> bool:
            if t.lon is None or t.lat is None:
                return False          # unplaceable, cannot satisfy a spatial filter
            if not (s <= t.lat <= n):
                return False
            return (t.lon >= w or t.lon <= e) if crosses else (w <= t.lon <= e)

        tiles = [t for t in tiles if in_box(t)]
        applied.append(
            f"bbox={list(bbox)}" + (" (crosses antimeridian)" if crosses else ""))
    if every_nth is not None and every_nth > 1:
        tiles = tiles[::every_nth]
        applied.append(f"every_nth={every_nth}")
    if top_n is not None:
        tiles = sorted(tiles, key=lambda t: t.value, reverse=True)[:top_n]
        applied.append(f"top_n={top_n}")

    columns = _columns_for(result)
    wants_range = "min" in columns
    rows: list[list[Any]] = [
        [t.tile_id, _round(t.lon, precision), _round(t.lat, precision), t.value]
        + ([t.vmin, t.vmax] if wants_range else [])
        for t in tiles]

    out: dict[str, Any] = {
        "format": "columnar",
        "columns": columns,
        "units": units_of(result),
        "n_cells_total": total,
        "n_cells_returned": len(rows),
        "filters_applied": applied or ["none"],
        # Both, explicitly named: a slice's max is not the field's max.
        "stats_of_slice": _stats_from_tiles(tiles),
        "stats_of_full_result": _stats_dict(stats_of(result)),
        "rows": rows,
    }
    _annotate_stats_gap(out, result)
    return out


__all__ = [
    "Stats",
    "Tile",
    "classify_result_shape",
    "estimate_tokens",
    "features_of",
    "has_tiles",
    "n_cells",
    "shape_response",
    "slice_result",
    "stats_of",
    "stats_unreadable",
    "tile_values",
    "to_columnar",
    "units_of",
]
