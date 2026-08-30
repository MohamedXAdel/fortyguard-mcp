"""
The MCP surface: 12 tools, 2 resources and 1 resource template.

Two decisions worth knowing before reading the code.

**Tools return a compact JSON string, not a dict.** The SDK indents a
non-string return and duplicates it as `structured_content` - 272,090 bytes
against 120,216 on the largest fixture, and the budget layer was calibrated on
compact JSON. The output schema given up is `additionalProperties: true`, which
tells a client nothing. Tools build dicts internally; `_emit` serialises.

**API errors are returned as data, not raised.** A raised exception reaches the
client as one flat `str(e)`, losing the status, field and body. Our own bugs
still raise: an agent can act on "latitude out of bounds", not `AttributeError`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .client.errors import FortyGuardError
from .client.results import (
    CHARS_PER_TOKEN_JSON,
    has_tiles,
    shape_response,
    slice_result,
)
from .config import Settings
from .domain.api_schema import (
    ANALYTIC_TYPE_HINT,
    ENV_ANALYSIS_HINT,
    FILTER_TYPE_HINT,
    GRANULARITY_HINT,
    REPORT_ANALYSIS_HINT,
    TIME_BASIS_NOTE,
)
from .domain.geo import (
    SplitTooLarge,
    as_feature_collection,
    describe_aoi,
    extract_rings,
    plan_split,
    split_ring,
)
from .logging_setup import configure_logging, scrub
from .store.results_store import replace_non_finite
from .tools.runtime import ToolContext, collect, run_analysis
from .tools.sourcing import SourcingError, resolve_date, resolve_temperature

# Heatmaps were measured at 21-38 s, so this leaves headroom while staying
# inside a typical client timeout. A statement about MCP clients, not about
# FortyGuard: exceeding it returns the activity_id and the job keeps running.
DEFAULT_INLINE_WAIT_S = 120.0

# Measured: one split piece runs 203-329 bytes as compact GeoJSON. 340 is the
# observed maximum rounded up, so the response ceiling divided by it fits.
BYTES_PER_PIECE = 340

# Declared in the SCHEMA so the SDK rejects a typo before the tool body runs,
# rather than after the job is charged for. Not gating: `format` is OUR
# vocabulary, unlike FortyGuard's enums, which stay unvalidated.
ResultFormat = Literal["auto", "geojson", "columnar"]

# The SDK's three; stdio is the only one a local MCP client uses.
Transport = Literal["stdio", "sse", "streamable-http"]

_HINT_FILTER = "; ".join(f"{k} = {v}" for k, v in FILTER_TYPE_HINT.items())
_HINT_GRAN = ", ".join(str(g) for g in GRANULARITY_HINT)
_HINT_ANALYTIC = ", ".join(ANALYTIC_TYPE_HINT)
_HINT_ENV_ANALYSIS = ", ".join(ENV_ANALYSIS_HINT)
_HINT_REPORT_ANALYSIS = ", ".join(REPORT_ANALYSIS_HINT)


# --------------------------------------------------------------------------- #
# Serialisation boundary
# --------------------------------------------------------------------------- #

def byte_cap(settings: Settings) -> int:
    """
    The hard ceiling on an auto-formatted tool result, in bytes.

    Derived from the same measured ratio the budget layer uses, so the two
    cannot drift.

    Applies to `format="auto"` ONLY - the caller declining to choose. A named
    format is honoured at whatever size it comes to.
    """
    return int(settings.inline_token_budget * CHARS_PER_TOKEN_JSON)


def _dump(payload: Any, *, settings: Settings) -> str:
    """
    Serialise for the wire: valid JSON, and no credentials.

    The ONE boundary every tool and resource passes through, so both guarantees
    live here rather than at each call site.

    **Scrubbing.** `usage()` must send the key in the body, and the API echoes
    input back in a 422 - so without this the key reaches the model's context.

    **Non-finite numbers.** `allow_nan=False` raises rather than emitting bare
    `NaN`; only then is a sanitising pass paid for.
    """
    try:
        text = json.dumps(payload, separators=(",", ":"),
                          default=str, allow_nan=False)
    except ValueError:
        cleaned, replaced = replace_non_finite(payload)
        if isinstance(cleaned, dict):
            cleaned["non_finite_values"] = {
                "replaced_with_null": replaced,
                "note": ("The API returned NaN or Infinity for these values. "
                         "They are not valid JSON and cannot be transmitted, so "
                         "they are null here. Nothing else was altered."),
            }
        text = json.dumps(cleaned, separators=(",", ":"),
                          default=str, allow_nan=False)
    return scrub(text, settings.key or None)


def _emit(payload: dict[str, Any], *, settings: Settings, capped: bool = True) -> str:
    """
    Serialise compactly, and enforce the ceiling as a backstop.

    `shape_response` already keeps the result inside the budget; this catches
    the ENVELOPE pushing it over. The payload is replaced by a pointer, never
    truncated - truncated JSON is both unparseable and silently wrong.

    The pointer has a floor of ~1.2 KB, so below a ~500 token budget it exceeds
    the cap it reports and is emitted whole anyway.
    """
    text = _dump(payload, settings=settings)
    if not capped:
        return text

    cap = byte_cap(settings)
    if len(text) <= cap:
        return text

    activity_id = payload.get("activity_id")
    out: dict[str, Any] = {
        "format": "summary",
        "reason": "over_byte_cap",
        "bytes": len(text),
        "byte_cap": cap,
        "error": False,
    }

    # A stored analysis and a 35 MB list of split pieces are not the same
    # situation, and one notice cannot describe both - `split_aoi` would
    # otherwise be told its output was stored locally and fetchable by an
    # activity_id it does not have.
    if activity_id:
        out["activity_id"] = activity_id
        out["next"] = "get_result_slice"
        out["message"] = (
            f"The result is complete and stored locally, but at {len(text):,} "
            f"bytes it is over the {cap:,} byte ceiling that format='auto' "
            f"applies. Nothing was truncated and nothing further costs credits. "
            f"The ceiling applies to 'auto' only - naming a format overrides it "
            f"and delivers the whole result, however large. Every option below "
            f"is available; choose whichever suits you."
        )
        # The whole payload comes first. Tile routes are listed only when the
        # payload HAS tiles; on a satellite result they return an empty table.
        whole = [
            f"get_result_slice('{activity_id}', format='geojson')  "
            f"- the entire raw payload, no ceiling",
            f"read the resource fortyguard://result/{activity_id}"
            f" - the stored payload verbatim",
        ]
        if _payload_has_tiles(payload):
            out["options"] = [
                f"get_result_slice('{activity_id}', format='columnar') "
                f"- every tile, compact table, no ceiling",
                *whole,
                f"get_result_slice('{activity_id}', top_n=50)          "
                f"- the 50 highest values",
                f"get_result_slice('{activity_id}', bbox=[w,s,e,n])    - a sub-area",
                f"get_result_slice('{activity_id}', every_nth=10)      - downsampled",
            ]
        else:
            out["options"] = [
                *whole,
                "(this result has no tiles, so top_n, bbox, every_nth and "
                "format='columnar' would all return an empty table)",
            ]
        return _dump(out, settings=settings)

    out["message"] = (
        f"This response would be {len(text):,} bytes, over the {cap:,} byte "
        f"ceiling, so it was not sent. Nothing failed and nothing was charged - "
        f"the call did what you asked, the answer is simply too large to hand "
        f"back in one piece. Ask for less of it: a smaller area, a larger "
        f"max_area_km2, or fewer parameters."
    )
    return _dump(out, settings=settings)


def _payload_has_tiles(payload: dict[str, Any]) -> bool:
    """
    Does this ENVELOPE describe sliceable tiles?

    `_emit` sees the shaped envelope, so tiles can be in three states:
    `result` (raw), `rows` (columnar), or `n_cells` (already summarised). The
    third is why this is not two lines: a summarised 527-tile heatmap carries no
    tiles, and reading it as tile-less strips the options it supports.
    """
    if payload.get("rows"):
        return True
    if has_tiles(payload.get("result")):
        return True
    cells = payload.get("n_cells")
    return isinstance(cells, int) and cells > 0


def _sourcing_error(e: SourcingError, settings: Settings) -> str:
    return _emit(e.payload, settings=settings)


def _date_time(
    start_date: str | None,
    start_time: str | None,
    end_time: str | None,
    end_date: str | None,
    filter_type: int | None,
) -> dict[str, Any]:
    """
    Assemble the API's `date_time` object from flat arguments.

    Assembly only: nothing is defaulted, inferred or validated. A flat
    signature is what a model fills reliably; the nesting is FortyGuard's.
    """
    out: dict[str, Any] = {}
    if start_date is not None:
        out["start_date"] = start_date
    if start_time is not None:
        out["start_time"] = start_time
    if end_time is not None:
        out["end_time"] = end_time
    if end_date is not None:
        out["end_date"] = end_date
    if filter_type is not None:
        out["filter_type"] = filter_type
    return out


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    """Drop unset keys so the request body carries only what the caller asked for."""
    return {k: v for k, v in d.items() if v is not None and v != {}}


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

INSTRUCTIONS = f"""\
Hyperlocal urban heat data for the United States, from the FortyGuard \
Temperature API.

How this server behaves:

* Analyses are asynchronous. `submit_*` returns an `activity_id` immediately; \
`check_status` collects the result. `create_heatmap` submits and waits inline \
for you when the job is short enough.
* Every completed result is archived on local disk permanently. Re-requesting \
an identical analysis is served from that archive for free - results are \
deterministic, so it is the same answer. Slicing a stored result never costs \
credits.
* Errors come back as the API's own message, unchanged. They are precise; read \
them rather than guessing.
* A completed analysis can return zero tiles and still be charged in full. \
When that happens it is stated explicitly.
* {TIME_BASIS_NOTE}

Limits such as maximum area, coverage, and available date range depend on your \
account's plan, so this server does not enforce any of them. Call \
`get_credit_usage` to see your plan, and let the API reject what it will not \
accept.
"""


def build_server(tool_ctx: ToolContext | None = None) -> MCPServer:
    """
    Construct the server.

    `tool_ctx` is injectable so tests can point the whole surface at the replay
    server: the path exercised offline is the one that runs live.
    """
    ctx = tool_ctx or ToolContext()
    settings = ctx.settings

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        """
        Close the shared HTTP client when the server shuts down.

        The client is server-lifetime, so something has to own its end. Only
        the client this context created is closed.
        """
        try:
            yield None
        finally:
            await ctx.aclose()

    mcp: MCPServer = MCPServer(
        name="fortyguard",
        title="FortyGuard Temperature",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    # ---------------------------------------------------------------- account #

    @mcp.tool(
        description=(
            "Your account's plan, credit balance, and per-endpoint usage "
            "breakdown, straight from the API. This is the authority on what "
            "your key can do - area limits and endpoint access vary by plan and "
            "are not assumed anywhere in this server."
        ),
        structured_output=False,
    )
    async def get_credit_usage() -> str:
        try:
            async with ctx.http() as api:
                return _emit({"usage": await api.usage()}, settings=settings)
        except FortyGuardError as e:
            return _emit(e.to_dict(), settings=settings)

    @mcp.tool(
        description=(
            "What this server has archived on local disk: how many results, "
            "broken down by endpoint, how much space they take, any report "
            "files downloaded from signed URLs, and where all of it lives. "
            "Nothing is ever deleted automatically - stored results cost money "
            "and never go stale, so the directory is yours to manage."
        ),
        structured_output=False,
    )
    async def get_storage_info() -> str:
        info = ctx.results.info()
        return _emit({
            "path": info.path,
            "result_count": info.result_count,
            "total_bytes": info.total_bytes,
            "results_by_endpoint": info.results_by_endpoint,
            "reports_path": info.reports_path,
            "report_count": info.report_count,
            "report_bytes": info.report_bytes,
            "oldest": info.oldest,
            "newest": info.newest,
            "max_storage_bytes": info.max_storage_bytes,
            "over_cap": info.over_cap,
            "pending_submissions": ctx.inflight.count(),
            "note": ("Results are never evicted. Re-fetching one costs credits, "
                     "so deleting is a decision only you should make - the "
                     "directories above are plainly deletable if you want to."),
            "credits_note": (
                "Counts, not credits. What a call costs depends on your plan, "
                "which this server does not assume - call get_credit_usage for "
                "your account's real per-endpoint spend."
            ),
        }, settings=settings)

    # -------------------------------------------------------------------- aoi #

    @mcp.tool(
        description=(
            "Measure a GeoJSON area of interest locally: geodesic area in km2 "
            "and square miles, bounding box, edge lengths, ring closure, and "
            "whether the coordinates look transposed. Costs nothing and makes "
            "no API call. It reports only - no size limit is applied, because "
            "limits vary by plan."
        ),
        structured_output=False,
    )
    async def validate_aoi(
        polygon_aoi: Annotated[dict[str, Any], Field(
            description="GeoJSON FeatureCollection, Feature, Polygon or MultiPolygon.")],
    ) -> str:
        return _emit(describe_aoi(polygon_aoi), settings=settings)

    @mcp.tool(
        description=(
            "Cut an area of interest into a grid of smaller areas, each at or "
            "under a maximum size you specify. Use it when the API rejects an "
            "area as too large. There is no default maximum: the cap depends on "
            "your plan, so take it from your contract or from the API's own "
            "rejection message. Local computation, no credits."
        ),
        structured_output=False,
    )
    async def split_aoi(
        polygon_aoi: Annotated[dict[str, Any], Field(
            description="The area to split, as GeoJSON.")],
        max_area_km2: Annotated[float, Field(
            gt=0,
            description="Maximum area per piece, in square kilometres. Required - "
                        "this server does not assume your plan's limit.")],
    ) -> str:
        rings = extract_rings(polygon_aoi)
        if not rings:
            return _emit({
                "error": True,
                "source": "fortyguard-mcp",
                "message": "Could not read a polygon out of that value.",
            }, settings=settings)

        # The limit comes from what a tool response can actually carry, not
        # from a number someone picked.
        deliverable = max(1, byte_cap(settings) // BYTES_PER_PIECE)

        # Every ring's plan up front, so the reported bounds describe the WHOLE
        # area of interest rather than whichever ring tripped the limit.
        try:
            plans = [plan_split(r, max_area_km2) for r in rings]
        except ValueError as e:
            return _emit({"error": True, "source": "fortyguard-mcp",
                          "message": str(e)}, settings=settings)
        total_cells = sum(p.grid_cells for p in plans)

        pieces: list[dict[str, Any]] = []
        try:
            for ring in rings:
                for tile in split_ring(ring, max_area_km2,
                                       max_pieces=deliverable - len(pieces)):
                    pieces.append(as_feature_collection(tile))
        except SplitTooLarge as e:
            # Not a failure: the split is bigger than one response, so we
            # answer with the SHAPE of it.
            at_least = len(pieces) + e.pieces_at_least
            widest = max(plans, key=lambda p: p.grid_cells)
            return _emit({
                "error": False,
                "reason": "too_many_pieces_for_one_response",
                "max_area_km2": max_area_km2,
                "n_rings": len(rings),
                # Both bounds: the lower is a counted floor, the upper the
                # full grid across all rings - exact for rectangular areas,
                # loose for diagonal ones where most cells get dropped.
                "pieces_needed_at_least": at_least,
                "pieces_needed_at_most": total_cells,
                # The floor is weak by construction - for a single-ring area
                # it is always deliverable+1 - so say which number to read.
                "bounds_note": (
                    "pieces_needed_at_least is where counting stopped, not a "
                    "measurement - it is always just past what one response "
                    "carries. pieces_needed_at_most is the full grid and is "
                    "exact for a rectangular area; a diagonal or L-shaped one "
                    "drops most of those cells and lands well below it."
                ),
                "pieces_deliverable_per_call": deliverable,
                "largest_ring_grid": {"nx": widest.nx, "ny": widest.ny},
                "nominal_piece_area_km2": round(
                    widest.nominal_piece_area_km2, 4),
                "bounding_box": [round(v, 6) for v in widest.bbox],
                "bounding_box_area_km2": round(
                    sum(p.bbox_area_km2 for p in plans), 3),
                "message": (
                    f"Splitting this area into {max_area_km2} km2 pieces needs "
                    f"more than the {deliverable:,} that fit in one response. "
                    f"The full grid across {len(rings)} ring(s) is "
                    f"{total_cells:,} cells; the pieces your polygon actually "
                    f"touches will be that or fewer. Nothing failed and nothing "
                    f"was charged. Raise max_area_km2, or split a sub-area at a "
                    f"time. Each piece is a separate analysis and is charged "
                    f"separately."
                ),
                "original": describe_aoi(polygon_aoi),
            }, settings=settings)
        except ValueError as e:
            return _emit({
                "error": True,
                "source": "fortyguard-mcp",
                "message": str(e),
                "max_area_km2": max_area_km2,
            }, settings=settings)

        return _emit({
            "n_pieces": len(pieces),
            "max_area_km2": max_area_km2,
            "original": describe_aoi(polygon_aoi),
            "pieces": pieces,
            "note": ("Pieces are rectangles covering the polygon - grid cells "
                     "the shape never touches are dropped, so a diagonal or "
                     "L-shaped area does not come back padded with empty "
                     "boxes. Pieces on the boundary are kept whole, so together "
                     "they cover at least the original area and a little more: "
                     "over-covering costs slightly extra, while under-covering "
                     "would leave an unanalysed hole and say nothing about it. "
                     "Every piece is at or under max_area_km2. Each is a "
                     "separate analysis and is charged separately."),
        }, settings=settings)

    # ---------------------------------------------------------------- heatmap #

    async def _heatmap(
        polygon_aoi: dict[str, Any],
        start_date: str | None,
        start_time: str | None,
        end_time: str | None,
        end_date: str | None,
        filter_type: int | None,
        granularity: int | None,
        analytic_type: str | None,
        threshold: float | None,
        direction: str | None,
        wait_s: float,
        fmt: str,
        mcp_ctx: Context | None,
    ) -> str:
        body = _prune({
            "polygon_aoi": polygon_aoi,
            "date_time": _date_time(start_date, start_time, end_time,
                                    end_date, filter_type),
            "granularity": granularity,
            "analytic_type": analytic_type,
            "threshold": threshold,
            "direction": direction,
        })
        out = await run_analysis(ctx, "/v1/heatmap", body,
                                 mcp_ctx=mcp_ctx, wait_s=wait_s, fmt=fmt)
        return _emit(out, settings=settings, capped=(fmt == "auto"))

    _heatmap_args = (
        f"filter_type: {_HINT_FILTER}. "
        f"granularity is the tile edge in metres ({_HINT_GRAN}); omit it to let "
        f"the API choose. analytic_type is optional ({_HINT_ANALYTIC}); omit it "
        f"for plain temperature. {TIME_BASIS_NOTE}"
    )

    @mcp.tool(
        description=(
            "Submit a temperature heatmap over an area and return immediately "
            "with an activity_id, without waiting for it to finish. Use this "
            "when you have other work to do, or for a large area. Collect it "
            "with check_status. If this exact request was run before, the "
            "stored result comes back straight away instead of an activity_id, "
            f"marked from_archive, and costs nothing. {_heatmap_args}"
        ),
        structured_output=False,
    )
    async def submit_heatmap(
        polygon_aoi: Annotated[dict[str, Any], Field(
            description="Area of interest as GeoJSON.")],
        start_date: Annotated[str | None, Field(
            default=None, description="YYYY-MM-DD.")] = None,
        start_time: Annotated[str | None, Field(
            default=None, description="HH:MM, local to the area, not UTC.")] = None,
        end_time: Annotated[str | None, Field(
            default=None, description="HH:MM, for filter_type 2.")] = None,
        end_date: Annotated[str | None, Field(
            default=None, description="YYYY-MM-DD, for filter_type 4.")] = None,
        filter_type: Annotated[int | None, Field(
            default=None, description=_HINT_FILTER)] = None,
        granularity: Annotated[int | None, Field(
            default=None, description=f"Tile edge in metres: {_HINT_GRAN}.")] = None,
        analytic_type: Annotated[str | None, Field(
            default=None, description=f"Optional analytic: {_HINT_ANALYTIC}.")] = None,
        threshold: Annotated[float | None, Field(default=None)] = None,
        direction: Annotated[str | None, Field(default=None)] = None,
    ) -> str:
        return await _heatmap(polygon_aoi, start_date, start_time, end_time,
                              end_date, filter_type, granularity, analytic_type,
                              threshold, direction, 0.0, "auto", None)

    @mcp.tool(
        description=(
            "Run a temperature heatmap and wait for the result inline. Measured "
            "heatmaps take 21-38 seconds. If the wait runs out you get the "
            "activity_id back and nothing is lost - the job keeps running and "
            "check_status collects it. An identical earlier request is served "
            f"from the local archive for free. {_heatmap_args}"
        ),
        structured_output=False,
    )
    async def create_heatmap(
        polygon_aoi: Annotated[dict[str, Any], Field(
            description="Area of interest as GeoJSON.")],
        start_date: Annotated[str | None, Field(default=None)] = None,
        start_time: Annotated[str | None, Field(
            default=None, description="HH:MM, local to the area, not UTC.")] = None,
        end_time: Annotated[str | None, Field(default=None)] = None,
        end_date: Annotated[str | None, Field(default=None)] = None,
        filter_type: Annotated[int | None, Field(
            default=None, description=_HINT_FILTER)] = None,
        granularity: Annotated[int | None, Field(
            default=None, description=f"Tile edge in metres: {_HINT_GRAN}.")] = None,
        analytic_type: Annotated[str | None, Field(
            default=None, description=f"Optional analytic: {_HINT_ANALYTIC}.")] = None,
        threshold: Annotated[float | None, Field(default=None)] = None,
        direction: Annotated[str | None, Field(default=None)] = None,
        wait_s: Annotated[float, Field(
            default=DEFAULT_INLINE_WAIT_S, ge=0,
            description="Seconds to wait inline before returning the "
                        "activity_id instead.")] = DEFAULT_INLINE_WAIT_S,
        format: Annotated[ResultFormat, Field(
            default="auto",
            description="auto = the raw API payload when it fits the context "
                        "budget, otherwise a summary naming every way to fetch "
                        "it. geojson = the raw payload whatever its size. "
                        "columnar = a compact table, about 12x smaller.")] = "auto",
        ctx_: Context | None = None,
    ) -> str:
        return await _heatmap(polygon_aoi, start_date, start_time, end_time,
                              end_date, filter_type, granularity, analytic_type,
                              threshold, direction, wait_s, format, ctx_)

    # ------------------------------------------------------------ env_params #

    @mcp.tool(
        description=(
            "Environmental parameters at a single point - humidity, heat index, "
            "wet bulb, air quality and more. Requires a temperature, which must "
            "match the heatmap for the same place and time. Supply it either as "
            "temperature=, or as from_activity_id= naming a completed heatmap "
            "covering this point, which also supplies the matching date. Give "
            "one or the other, not both. Narrow the response with analysis=, "
            "or omit it to receive every parameter."
        ),
        structured_output=False,
    )
    async def get_env_params(
        latitude: Annotated[float, Field(description="Decimal degrees.")],
        longitude: Annotated[float, Field(description="Decimal degrees.")],
        temperature: Annotated[float | None, Field(
            default=None,
            description="Degrees Celsius. Mutually exclusive with "
                        "from_activity_id.")] = None,
        from_activity_id: Annotated[str | None, Field(
            default=None,
            description="A completed heatmap to read the temperature and date "
                        "out of. Free - no API call. Mutually exclusive with "
                        "temperature.")] = None,
        start_date: Annotated[str | None, Field(default=None)] = None,
        start_time: Annotated[str | None, Field(default=None)] = None,
        filter_type: Annotated[int | None, Field(
            default=None, description=_HINT_FILTER)] = None,
        analysis: Annotated[list[str] | None, Field(
            default=None,
            description=f"Which parameters to return. Omit for all of them "
                        f"(verified). Values: {_HINT_ENV_ANALYSIS}. NOTE these "
                        f"are NOT the sections submit_heat_intelligence takes "
                        f"- that endpoint uses a different vocabulary.")] = None,
        wait_s: Annotated[float, Field(
            default=DEFAULT_INLINE_WAIT_S, ge=0)] = DEFAULT_INLINE_WAIT_S,
        ctx_: Context | None = None,
    ) -> str:
        try:
            temp, sourced_dt, provenance = resolve_temperature(
                ctx, lat=latitude, lon=longitude,
                temperature=temperature, from_activity_id=from_activity_id,
            )
            # `filter_type` is EXCLUDED from the exclusivity check: it
            # selects a time mode, it is not a date. Including it rejected a
            # coherent request naming a parameter the caller never passed.
            explicit_dates = _date_time(start_date, start_time, None, None, None)
            # Name the fields the caller ACTUALLY passed, so the conflict
            # never cites a parameter they did not use.
            supplied = " or ".join(
                name for name, value in (("start_date", start_date),
                                         ("start_time", start_time))
                if value is not None
            )
            date_time = resolve_date(explicit_dates or None, sourced_dt,
                                     field_name=supplied or "start_date")
        except SourcingError as e:
            return _sourcing_error(e, settings)

        mode_note = None
        if filter_type is not None:
            sourced_ft = (date_time or {}).get("filter_type")
            date_time = {**(date_time or {}), "filter_type": filter_type}
            if sourced_ft is not None and sourced_ft != filter_type:
                # Overriding rather than erroring - the mode is the caller's
                # to choose - but never silently.
                mode_note = (
                    f"filter_type {filter_type} was used, overriding the "
                    f"{sourced_ft} recorded on heatmap {from_activity_id}. The "
                    f"date itself still comes from that heatmap."
                )

        body = _prune({
            "latitude": latitude,
            "longitude": longitude,
            "temperature": temp,
            "date_time": date_time,
            "analysis": analysis,
        })
        out = await run_analysis(ctx, "/v1/env_params", body,
                                 mcp_ctx=ctx_, wait_s=wait_s)
        if provenance is not None:
            out["temperature_provenance"] = provenance
        if mode_note is not None:
            out["date_time_note"] = mode_note
        return _emit(out, settings=settings)

    # -------------------------------------------------------------- imagery #

    @mcp.tool(
        description=(
            "Submit satellite land-cover segmentation for a point and return an "
            "activity_id. Collect it with check_status."
        ),
        structured_output=False,
    )
    async def submit_satellite(
        latitude: Annotated[float, Field(description="Decimal degrees.")],
        longitude: Annotated[float, Field(description="Decimal degrees.")],
        start_date: Annotated[str | None, Field(default=None)] = None,
        start_time: Annotated[str | None, Field(
            default=None, description="HH:MM, local to the point, not UTC.")] = None,
        filter_type: Annotated[int | None, Field(
            default=None, description=_HINT_FILTER)] = None,
        # OPTIONAL, measured: a call omitting it was accepted (HTTP 200). The
        # vendor documentation lists it under Required attributes and is wrong,
        # exactly as it is wrong for heatmap.
        granularity: Annotated[int | None, Field(
            default=None,
            description=f"Tile edge in metres: {_HINT_GRAN}.")] = None,
    ) -> str:
        body = _prune({
            "sat": {"latitude": latitude, "longitude": longitude},
            "date_time": _date_time(start_date, start_time, None, None, filter_type),
            "granularity": granularity,
        })
        return _emit(
            await run_analysis(ctx, "/v1/satellite", body, wait_s=0.0),
            settings=settings)

    @mcp.tool(
        description=(
            "Submit street-view scene analysis for a point and return an "
            "activity_id. Collect it with check_status."
        ),
        structured_output=False,
    )
    async def submit_streetview(
        latitude: Annotated[float, Field(description="Decimal degrees.")],
        longitude: Annotated[float, Field(description="Decimal degrees.")],
        # All three are Required attributes in the vendor's documentation, and
        # a 422 names the angles when either is absent.
        vertical_angle: Annotated[float, Field(
            description="Camera pitch in degrees. 10 is a normal "
                        "street-level view.")],
        horizontal_angle: Annotated[float, Field(
            description="Field of view in degrees, 0-360. 90 is a normal "
                        "street-level view.")],
        back_view: Annotated[bool, Field(
            description="Also analyse the opposing direction.")],
    ) -> str:
        body = _prune({
            "latitude": latitude,
            "longitude": longitude,
            "vertical_angle": vertical_angle,
            "horizontal_angle": horizontal_angle,
            "back_view": back_view,
        })
        return _emit(
            await run_analysis(ctx, "/v1/streetview", body, wait_s=0.0),
            settings=settings)

    @mcp.tool(
        description=(
            "Submit a heat intelligence report for a point and return an "
            "activity_id. This one is slow - measured at about 395 seconds - so "
            "it is never waited on inline. Collect it with check_status, which "
            "downloads the PDF to local disk and returns its path under "
            "'report'; the API delivers this analysis as a short-lived signed "
            "URL, which is never returned or stored. Needs a temperature: give "
            "temperature= or from_activity_id=, not both. `analysis` is "
            "required - pass all five categories for a complete report."
        ),
        structured_output=False,
    )
    async def submit_heat_intelligence(
        latitude: Annotated[float, Field(description="Decimal degrees.")],
        longitude: Annotated[float, Field(description="Decimal degrees.")],
        # Required by the API: a 422 names it when absent. Declared before the
        # optional arguments because Python allows no default after a default.
        analysis: Annotated[list[str], Field(
            description=f"Report sections to include, at least one: "
                        f"{_HINT_REPORT_ANALYSIS}. Pass all five for a "
                        f"complete report. NOTE these are NOT the measurement "
                        f"names get_env_params takes - that endpoint uses a "
                        f"different vocabulary and does not require this.")],
        temperature: Annotated[float | None, Field(
            default=None, description="Degrees Celsius. Mutually exclusive with "
                                      "from_activity_id.")] = None,
        from_activity_id: Annotated[str | None, Field(
            default=None,
            description="A completed heatmap to read temperature and date from. "
                        "Free. Mutually exclusive with temperature.")] = None,
        date: Annotated[str | None, Field(
            default=None, description="YYYY-MM-DD.")] = None,
    ) -> str:
        try:
            temp, sourced_dt, provenance = resolve_temperature(
                ctx, lat=latitude, lon=longitude,
                temperature=temperature, from_activity_id=from_activity_id,
            )
            # This endpoint takes a flat `date`, not the `date_time` object
            # the others use.
            sourced_date = (sourced_dt or {}).get("start_date") if sourced_dt else None
            use_date = resolve_date(date, sourced_date, field_name="date")
        except SourcingError as e:
            return _sourcing_error(e, settings)

        body = _prune({
            "latitude": latitude,
            "longitude": longitude,
            "temperature": temp,
            "date": use_date,
            "analysis": analysis,
        })
        out = await run_analysis(ctx, "/v1/heat_intelligence", body, wait_s=0.0)
        if provenance is not None:
            out["temperature_provenance"] = provenance
        return _emit(out, settings=settings)

    # --------------------------------------------------------------- collect #

    @mcp.tool(
        description=(
            "Check on a submitted analysis, or wait for it. If it has finished, "
            "the result comes back and is archived locally. Polling is free and "
            "the job runs whether or not you poll. Once collected, calling this "
            "again is served from disk at no cost."
        ),
        structured_output=False,
    )
    async def check_status(
        activity_id: Annotated[str, Field(description="From a submit_* call.")],
        wait_s: Annotated[float, Field(
            default=0.0, ge=0,
            description="Seconds to wait for completion. 0 checks once and "
                        "returns immediately.")] = 0.0,
        format: Annotated[ResultFormat, Field(
            default="auto",
            description="auto, geojson, or columnar.")] = "auto",
        ctx_: Context | None = None,
    ) -> str:
        out = await collect(ctx, activity_id, mcp_ctx=ctx_,
                            wait_s=wait_s, fmt=format)
        return _emit(out, settings=settings, capped=(format == "auto"))

    @mcp.tool(
        description=(
            "Read part or all of an already-collected result from local disk. "
            "Costs nothing and makes no API call, so use it freely: take the "
            "hottest tiles with top_n, a sub-area with bbox, a downsample with "
            "every_nth, the whole thing compactly with format='columnar', or "
            "the untouched payload with format='geojson'. Naming a format gets "
            "you all of it at whatever size it comes to; the context budget "
            "applies only to the default format='auto'. Statistics for both the "
            "slice and the full result are always reported, so a slice maximum "
            "is never mistaken for the real one."
        ),
        structured_output=False,
    )
    async def get_result_slice(
        activity_id: Annotated[str, Field(description="A collected analysis.")],
        top_n: Annotated[int | None, Field(
            default=None, gt=0,
            description="Return only the N highest-valued tiles.")] = None,
        bbox: Annotated[list[float] | None, Field(
            default=None,
            description="[west, south, east, north]. West may exceed east to "
                        "cross the antimeridian.")] = None,
        every_nth: Annotated[int | None, Field(
            default=None, gt=0, description="Keep every Nth tile.")] = None,
        format: Annotated[ResultFormat, Field(
            default="auto",
            description="auto = fit it to the context budget, and if it does "
                        "not fit, say what exists and how to fetch it. "
                        "columnar = the compact table in full, whatever its "
                        "size. geojson = the raw payload in full, whatever its "
                        "size. Both named formats are delivered complete - the "
                        "budget applies to auto only.")] = "auto",
    ) -> str:
        stored = ctx.results.get(activity_id)
        if stored is None:
            return _emit({
                "error": True,
                "source": "fortyguard-mcp",
                "message": (
                    f"No collected result for {activity_id!r} on local disk. If "
                    f"it was submitted but never collected, call "
                    f"check_status('{activity_id}') first."),
                "next": "check_status",
            }, settings=settings)

        result = stored.load()
        if result is None:
            return _emit({
                "error": True,
                "source": "fortyguard-mcp",
                "message": f"The stored payload for {activity_id!r} is missing.",
            }, settings=settings)

        # Nothing to slice: hand the stored payload to `shape_response` and
        # let the format decide, exactly as the analysis tools do.
        no_filters = top_n is None and bbox is None and every_nth is None
        if no_filters and format != "columnar":
            out = shape_response(result, activity_id=activity_id, fmt=format)
            out["from_archive"] = True
            out["credits_charged"] = 0
            return _emit(out, settings=settings, capped=(format == "auto"))

        box = None
        if bbox is not None:
            if len(bbox) != 4:
                return _emit({
                    "error": True,
                    "source": "fortyguard-mcp",
                    "message": "bbox must be exactly [west, south, east, north].",
                }, settings=settings)
            box = (bbox[0], bbox[1], bbox[2], bbox[3])

        out = slice_result(result, top_n=top_n, bbox=box, every_nth=every_nth,
                           precision=settings.coordinate_precision)
        out["activity_id"] = activity_id
        out["from_archive"] = True
        out["credits_charged"] = 0
        # Uncapped whenever the caller named a format: they chose it and are
        # entitled to all of it. Capped under `auto`.
        return _emit(out, settings=settings, capped=(format == "auto"))

    # ------------------------------------------------------------- resources #

    @mcp.resource(
        "fortyguard://account/usage",
        name="Account usage",
        description="This API key's plan, credit balance and endpoint breakdown.",
        mime_type="application/json",
    )
    async def usage_resource() -> str:
        try:
            async with ctx.http() as api:
                return _dump(await api.usage(), settings=settings)
        except FortyGuardError as e:
            return _dump(e.to_dict(), settings=settings)

    @mcp.resource(
        "fortyguard://storage",
        name="Local archive",
        description="What this server has stored on disk and what it cost.",
        mime_type="application/json",
    )
    async def storage_resource() -> str:
        info = ctx.results.info()
        return _dump({
            "path": info.path,
            "result_count": info.result_count,
            "total_bytes": info.total_bytes,
            "results_by_endpoint": info.results_by_endpoint,
            "reports_path": info.reports_path,
            "report_count": info.report_count,
            "report_bytes": info.report_bytes,
        }, settings=settings)

    @mcp.resource(
        "fortyguard://result/{activity_id}",
        name="Stored result",
        description=("The complete, untouched API payload for a collected "
                     "analysis. Uncapped - this is the raw-access path."),
        mime_type="application/json",
    )
    async def result_resource(activity_id: str) -> str:
        stored = ctx.results.get(activity_id)
        if stored is None:
            return _dump({"error": True, "next": "check_status",
                          "message": f"No stored result for {activity_id!r}."},
                         settings=settings)
        try:
            # Bytes straight through: never parsed into Python objects, so a
            # 14 MB result costs a buffer rather than megabytes of dicts.
            with stored.open_bytes() as fh:
                return fh.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # The sidecar can outlive its payload - deleted by hand, or an
            # interrupted write - and an uncaught FileNotFoundError would reach
            # the client as an opaque ResourceError.
            return _dump({
                "error": True,
                "activity_id": activity_id,
                "message": (f"The stored payload for {activity_id!r} is "
                            f"unreadable ({type(e).__name__}). Its metadata "
                            f"survives, so it was archived and then removed or "
                            f"truncated."),
                "next": "check_status",
            }, settings=settings)

    return mcp


def main(transport: Transport = "stdio") -> None:
    """Entry point for `fortyguard-mcp` and `uvx fortyguard-mcp`."""
    ctx = ToolContext()
    server = build_server(ctx)
    # AFTER build_server: `MCPServer.__init__` runs `logging.basicConfig`, so
    # configuring first would be overridden. The key is a callable so the filter
    # reads the current value rather than a copy.
    configure_logging(ctx.settings.log_level, key_source=lambda: ctx.settings.key)
    # Only facts that cannot drift: a hardcoded tool count becomes a lie.
    logging.getLogger(__name__).info(
        "fortyguard-mcp starting: transport=%s base_url=%s data_dir=%s "
        "log_level=%s api_key=%s",
        transport, ctx.settings.base_url, ctx.settings.data_dir,
        ctx.settings.log_level, "set" if ctx.settings.key else "MISSING",
    )
    server.run(transport=transport)
