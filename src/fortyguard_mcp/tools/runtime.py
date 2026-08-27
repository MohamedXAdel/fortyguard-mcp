"""
The shared machinery every API-touching tool runs on.

Three jobs:

1. **Errors become data, not protocol failures.** The SDK flattens a raised
   exception to `str(e)`, losing the status, field and body - so
   `FortyGuardError` is caught and returned as `to_dict()`. Our own bugs still
   raise: an agent can act on "latitude out of bounds", not on `AttributeError`.

2. **The archive is consulted before the API.** An identical request already on
   disk is the same answer for free, and the hit is labelled as such.

3. **The activity_id is written down before the wait begins**, so a timeout or a
   killed process still leaves the result collectable.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..client.download import ReportFetchFailed, fetch_to_file, find_downloadable
from ..client.errors import FortyGuardError, PollTimeout
from ..client.http import Completion, FortyGuardHTTP
from ..client.results import shape_response
from ..config import Settings, get_settings
from ..domain.api_schema import result_has_arrived
from ..store.pending import PendingStore
from ..store.results_store import ResultStore


@dataclass(slots=True)
class ToolContext:
    """
    Everything the tools need, injectable for tests.

    `http_client` exists so the replay server can be pointed at without any
    network: the same code path runs in tests as in production.
    """

    settings: Settings = field(default_factory=get_settings)
    store: ResultStore | None = None
    pending: PendingStore | None = None
    http_client: httpx.AsyncClient | None = None
    _owned_client: httpx.AsyncClient | None = field(default=None, init=False)
    _sem: asyncio.Semaphore | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = ResultStore(self.settings)
        if self.pending is None:
            self.pending = PendingStore(self.settings)

    @property
    def results(self) -> ResultStore:
        assert self.store is not None
        return self.store

    @property
    def inflight(self) -> PendingStore:
        assert self.pending is not None
        return self.pending

    def http(self) -> FortyGuardHTTP:
        """
        A transport over the SERVER-LIFETIME client and semaphore.

        Shared rather than per-call: a fresh semaphore would bound only the
        requests inside one call, of which at most one is in flight. Sharing
        also pools connections. Lazy, so building a ToolContext outside an
        event loop is safe.
        """
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.settings.max_concurrent_requests)

        client = self.http_client
        if client is None:
            if self._owned_client is None:
                self._owned_client = httpx.AsyncClient(
                    base_url=self.settings.base_url,
                    timeout=self.settings.request_timeout_s,
                )
            client = self._owned_client
        # Passing a client means FortyGuardHTTP does not own it, so leaving
        # its `async with` block will not close the shared pool.
        return FortyGuardHTTP(self.settings, client=client, semaphore=self._sem)

    async def aclose(self) -> None:
        """Close the shared client. Safe to call more than once."""
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None


class ProgressReporter:
    """
    Adapts the client's `on_progress` callback to MCP progress notifications.

    The fraction is elapsed-against-OUR-TIMEOUT, not progress to completion:
    the API never says how far along a job is. The message carries the API's own
    status verbatim, including when it sent none.
    """

    def __init__(self, ctx: Any, budget_s: float) -> None:
        self._ctx = ctx
        self._budget = budget_s if budget_s > 0 else 1.0

    async def __call__(self, status: str | None, elapsed: float, polls: int) -> None:
        if self._ctx is None:
            return
        label = status if status else "no status reported"
        # A client without progress support must not break the call.
        with contextlib.suppress(Exception):
            await self._ctx.report_progress(
                min(elapsed, self._budget), self._budget,
                f"{label} - {elapsed:.0f}s elapsed, poll {polls}",
            )


def _requested_date(body: Any) -> str | None:
    """
    The LATEST date a request asks about, across the two shapes the API uses.

    The LATEST, because `filter_type: 4` covers a range: judging on
    `start_date` alone would call a range reaching into the future "history".
    """
    if not isinstance(body, dict):
        return None
    flat = body.get("date")                       # heat_intelligence
    if isinstance(flat, str) and flat:
        return flat
    nested = body.get("date_time")                # heatmap, env_params, satellite
    if isinstance(nested, dict):
        dates = [d for d in (nested.get("start_date"), nested.get("end_date"))
                 if isinstance(d, str) and d]
        if dates:
            return max(dates, key=_as_date_key)
    return None                                   # streetview has none at all


def _as_date_key(value: str) -> tuple[int, int, int]:
    """
    A date as a sortable/comparable key, or a sentinel that never reads as past.

    Parsed, not compared as text: an unpadded `2024-7-5` sorts after
    `2026-08-25`. Unparseable returns a far-future key, so an unrecognised
    format is never claimed to be historical.
    """
    try:
        y, m, d = (int(part) for part in value.split("-", 2))
    except (TypeError, ValueError):
        return (9999, 12, 31)
    return (y, m, d)


def _cache_hit_note(endpoint: str, body: Any, stored_at: str) -> tuple[str, str]:
    """
    What to say when serving from the archive. Returns (note, confidence).

    THE CLAIM IS SCOPED TO WHAT WAS MEASURED: a HISTORICAL date, re-run,
    byte-identical across 112 tiles. It says nothing about a date not yet past
    when stored, nor about `streetview`, which carries no date. Serving the hit
    is still right; the caller is told which case they are in.
    """
    free = ("No API call was made and no credits were spent.")
    date = _requested_date(body)

    if date is None:
        return (
            f"This exact request was already run and its result is on local "
            f"disk. {free} NOTE: this request carries no date, so nothing ties "
            f"the stored answer to a point in time. Determinism was verified "
            f"only for dated, historical requests - if the underlying imagery "
            f"or data has since changed, this will not reflect it. Delete the "
            f"archived entry to force a fresh call.",
            "unverified_no_date",
        )

    stored_day = stored_at[:10]
    # Compared as dates, not as strings - see `_as_date_key`.
    if _as_date_key(date) < _as_date_key(stored_day):
        return (
            f"This exact request was already run and its result is on local "
            f"disk. It asks about {date}, which was already history when it was "
            f"stored, and re-running an identical historical request was "
            f"measured as byte-identical. {free}",
            "historical_verified",
        )

    return (
        f"This exact request was already run and its result is on local disk. "
        f"{free} NOTE: it asks about {date}, which was NOT yet in the past when "
        f"it was stored on {stored_day}. Determinism was verified for "
        f"historical dates only, so data for that date may have arrived since. "
        f"Delete the archived entry to force a fresh call.",
        "unverified_not_historical",
    )


_REPORT_NOTE = (
    "The API delivers this analysis as a temporary signed URL rather than as a "
    "document. The file was downloaded to the path above while that link was "
    "still valid. The link itself is never returned, logged, or archived - it "
    "is a credential, and it expires."
)

_REPORT_LOST_NOTE = (
    "This analysis completed and was charged in full. Its only deliverable is a "
    "file behind a temporary signed URL, that URL could not be fetched, and it "
    "is not recoverable - it is deliberately never stored, and it expires "
    "regardless. The result below is archived and re-readable, but obtaining "
    "the document itself now requires submitting the analysis again, which is "
    "charged again."
)


async def _fetch_report(
    tool_ctx: ToolContext, activity_id: str, result: Any
) -> dict[str, Any] | None:
    """
    Download whatever file this result links to, before the link is redacted.

    Returns the block for the response envelope, or None when the result links
    to nothing - which is every endpoint except heat_intelligence.

    NEVER RAISES. The analysis was paid for, so a failed download is reported
    alongside the result rather than turning a completed call into a failed one.
    """
    found = find_downloadable(result)
    if found is None:
        return None
    field, url = found

    if tool_ctx.results.has_report(activity_id):
        # Already on disk from an earlier collection of the same activity.
        return _existing_report_block(tool_ctx, activity_id, field)

    dest = tool_ctx.results.report_path_for(activity_id)
    try:
        got = await fetch_to_file(
            url, dest,
            timeout_s=tool_ctx.settings.report_timeout_s,
            max_bytes=tool_ctx.settings.report_max_bytes,
            allow_private_hosts=tool_ctx.settings.report_allow_private_hosts,
        )
    except ReportFetchFailed as e:
        return {
            "downloaded": False,
            "source_field": field,
            "reason": str(e),
            "host": e.host,
            "note": _REPORT_LOST_NOTE,
        }
    return {
        "downloaded": True,
        "source_field": field,
        "path": str(got.path),
        "size_bytes": got.size_bytes,
        "content_type": got.content_type,
        "note": _REPORT_NOTE,
    }


def _existing_report_block(
    tool_ctx: ToolContext, activity_id: str, field: str | None = None
) -> dict[str, Any] | None:
    """The block for a report already on disk, or None if there is not one."""
    path = tool_ctx.results.report_path_for(activity_id)
    if not path.exists():
        return None
    return {
        "downloaded": True,
        "from_disk": True,
        "source_field": field,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "note": ("Downloaded when this analysis was first collected. No API "
                 "call and no credits - the file is on local disk."),
    }


def as_error(exc: FortyGuardError) -> dict[str, Any]:
    """
    A FortyGuardError rendered as tool output.

    `PollTimeout.to_dict()` deliberately reports `"error": false`: the job is
    still running and the caller has lost nothing.
    """
    return exc.to_dict()


async def run_analysis(
    tool_ctx: ToolContext,
    endpoint: str,
    body: Any,
    *,
    mcp_ctx: Any = None,
    wait_s: float | None = None,
    fmt: str = "auto",
    budget_tokens: int | None = None,
    precision: int | None = None,
) -> dict[str, Any]:
    """
    Submit, wait as long as we are allowed, archive, and shape the response.

    `wait_s=0` means submit and return the activity_id immediately, which is the
    right shape for `heat_intelligence` (~395s measured) and for any client with
    a short tool-call timeout.
    """
    store = tool_ctx.results

    hit = store.find_by_request(endpoint, body)
    if hit is not None:
        loaded = hit.load()
        if loaded is not None:
            out = shape_response(
                loaded, activity_id=hit.activity_id,
                budget_tokens=budget_tokens, precision=precision, fmt=fmt,
            )
            out["from_archive"] = True
            # Set on BOTH paths, so an agent branching on `archived` does not
            # see the key vanish on a cache hit.
            out["archived"] = True
            out["archived_at"] = hit.stored_at
            out["credits_charged"] = 0
            out["note"], out["determinism"] = _cache_hit_note(
                endpoint, body, hit.stored_at)
            report = _existing_report_block(tool_ctx, hit.activity_id)
            if report is not None:
                out["report"] = report
            return out

    budget = tool_ctx.settings.poll_timeout_s if wait_s is None else wait_s

    try:
        async with tool_ctx.http() as api:
            sub = await api.submit(endpoint, body)
            # Before the wait: a timeout or cancellation still leaves the
            # request body recoverable.
            tool_ctx.inflight.remember(sub.activity_id, endpoint, body)

            if budget <= 0:
                return {
                    "status": "submitted",
                    "activity_id": sub.activity_id,
                    "endpoint": endpoint,
                    "error": False,
                    "next": "check_status",
                    "message": (
                        f"Submitted. This endpoint is not waited on inline; call "
                        f"check_status('{sub.activity_id}') to collect the result. "
                        f"The job is running regardless of whether you poll."
                    ),
                }

            # CancelledError propagates untouched - it is a BaseException, so
            # nothing here swallows it. The agent going away stops polling
            # immediately, and the record above keeps the work collectable.
            done = await api.wait(
                sub.activity_id, timeout_s=budget,
                on_progress=ProgressReporter(mcp_ctx, budget),
            )

        return await _archive_and_shape(
            tool_ctx, done, endpoint, body,
            fmt=fmt, budget_tokens=budget_tokens, precision=precision,
        )

    except PollTimeout as e:
        out = as_error(e)
        out["endpoint"] = endpoint
        return out
    except FortyGuardError as e:
        return as_error(e)


async def _archive_and_shape(
    tool_ctx: ToolContext,
    done: Completion,
    endpoint: str,
    body: Any,
    *,
    fmt: str,
    budget_tokens: int | None,
    precision: int | None,
) -> dict[str, Any]:
    """
    Fetch any linked file, persist, then shape. Nothing here loses the result.

    The download comes FIRST, and the ordering is load-bearing: `put()` redacts
    signed URLs on the way to disk, so fetching afterwards would reach for a
    link that layer had already, correctly, destroyed.
    """
    report = await _fetch_report(tool_ctx, done.activity_id, done.result)

    # NEVER RAISES. The result is paid for and sitting in `done.result`; a full
    # disk or a read-only mount is not a reason to lose it. Same rule
    # `PendingStore.remember` applies to the bookkeeping write.
    stored = None
    archive_error: str | None = None
    try:
        stored = tool_ctx.results.put(done.activity_id, endpoint, body, done.result)
    except (OSError, ValueError) as e:
        archive_error = f"{type(e).__name__}: {e}"
    else:
        tool_ctx.inflight.forget(done.activity_id)

    out = shape_response(
        done.result, activity_id=done.activity_id,
        budget_tokens=budget_tokens, precision=precision, fmt=fmt,
    )
    # Verbatim, including None - the API's word for what happened, not ours.
    out["api_status"] = done.status
    out["poll_count"] = done.poll_count
    out["elapsed_s"] = round(done.elapsed_s, 1)
    out["from_archive"] = False
    if report is not None:
        out["report"] = report
    if archive_error is not None:
        out["archived"] = False
        out["archive_error"] = archive_error
        out["activity_id"] = done.activity_id
        out["archive_note"] = (
            f"This result is COMPLETE and is returned above in full - it was "
            f"charged for and nothing about it was lost. Writing it to the "
            f"local archive failed ({archive_error}), so it is the one copy "
            f"you have: an identical request later will cost credits again, "
            f"and get_result_slice will not find it. Check free space and "
            f"permissions on {tool_ctx.settings.data_dir}. The activity_id is "
            f"recorded, so check_status('{done.activity_id}') can collect it "
            f"again once the archive is writable."
        )
    elif stored is None:
        out["archived"] = False
        out["archive_note"] = (
            "FORTYGUARD_MAX_STORAGE is set and the archive is at its cap, so "
            "this result was NOT saved. You have it here in full, but an "
            "identical request later will cost credits again. Raise or unset "
            "the cap to keep paid results."
        )
    else:
        out["archived"] = True
    return out


async def collect(
    tool_ctx: ToolContext,
    activity_id: str,
    *,
    mcp_ctx: Any = None,
    wait_s: float = 0.0,
    fmt: str = "auto",
    budget_tokens: int | None = None,
    precision: int | None = None,
) -> dict[str, Any]:
    """
    Check on, or wait for, an already-submitted job.

    Reads the archive first: once a result has been collected it is on disk
    forever, so re-checking an old activity_id costs nothing and works offline.
    """
    store = tool_ctx.results

    already = store.get(activity_id)
    if already is not None:
        loaded = already.load()
        if loaded is not None:
            out = shape_response(
                loaded, activity_id=activity_id,
                budget_tokens=budget_tokens, precision=precision, fmt=fmt,
            )
            out["from_archive"] = True
            # Set here as well as in `run_analysis`: both functions serve
            # from the archive, and the flag must not depend on which one did.
            out["archived"] = True
            out["archived_at"] = already.stored_at
            out["credits_charged"] = 0
            # Same reasoning as `archived` beside it: a path the caller can
            # open must not vanish when the result is re-read from disk.
            report = _existing_report_block(tool_ctx, activity_id)
            if report is not None:
                out["report"] = report
            return out

    pending = tool_ctx.inflight.recall(activity_id)

    try:
        async with tool_ctx.http() as api:
            if wait_s <= 0:
                status, raw = await api.poll_once(activity_id)
                if not result_has_arrived(raw):
                    return {
                        "error": False,
                        "activity_id": activity_id,
                        "status": status,
                        "next": "check_status",
                        "message": (
                            "Still running. Nothing is lost by waiting - the job "
                            "continues whether or not you poll, and polling is "
                            "free. Call check_status again, or pass wait_s to "
                            "have this tool wait for you."
                        ),
                        "raw": raw,
                    }
                done = Completion(
                    activity_id=activity_id, status=status,
                    result=(raw.get("data") or {}).get("result"),
                    poll_count=1, elapsed_s=0.0,
                )
            else:
                done = await api.wait(
                    activity_id, timeout_s=wait_s,
                    on_progress=ProgressReporter(mcp_ctx, wait_s),
                )
    except PollTimeout as e:
        return as_error(e)
    except FortyGuardError as e:
        return as_error(e)

    if pending is not None:
        return await _archive_and_shape(
            tool_ctx, done, pending.endpoint, pending.request_body,
            fmt=fmt, budget_tokens=budget_tokens, precision=precision,
        )

    # No record of the original request - restarted server, or an id from
    # elsewhere. The result is still delivered and stored under its
    # activity_id; only the request-keyed cache entry is unavailable.
    out = await _archive_and_shape(
        tool_ctx, done, "unknown", {"unrecoverable_request": activity_id},
        fmt=fmt, budget_tokens=budget_tokens, precision=precision,
    )
    unmatched = (
        "The original request parameters were not on record, so this result "
        "cannot be matched to an identical future request. Retrieving it by "
        "activity_id works normally."
    )
    # Appended, not assigned: a plain assignment would discard the storage-cap
    # warning `_archive_and_shape` may have just written.
    existing = out.get("archive_note")
    out["archive_note"] = f"{existing} {unmatched}" if existing else unmatched
    return out
