"""
HTTP transport and the submit-then-poll loop.

Four behaviours worth knowing, all measured:

  * Terminal detection is timeout-driven, not status-driven. No `Failed` status
    appeared in ~100 live calls, and some requests stayed `Processing` past
    ~470 s with no way to tell a long job from a stuck one - so every wait is
    bounded and returns the activity_id.
  * Statuses are matched case-insensitively; anything unrecognised is pending.
  * `fetch-api-key-usage` needs the key in the body as well as the header.
  * Cancellation propagates: if the caller goes away mid-poll, polling stops.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from ..config import Settings, get_settings
from ..domain.api_schema import (
    STATUS_PATH,
    USAGE_PATH,
    classify_status,
    result_has_arrived,
)
from .errors import (
    APIError,
    PollTimeout,
    TaskFailed,
    TransportError,
    UnexpectedResponse,
    UnsendableRequest,
)

# Ceiling on what one API response may buffer. ~9x the largest real payload
# measured (14 MB), so legitimate traffic never meets it.
MAX_RESPONSE_BYTES = 128 * 1024 * 1024

ProgressFn = Callable[[str | None, float, int], Awaitable[None] | None]
"""
Called as (status, elapsed_seconds, poll_count) during a wait.

`status` is whatever the API reported, including None when it reported nothing.
"""


@dataclass(slots=True)
class Submission:
    activity_id: str
    submit_body: Any
    endpoint: str


@dataclass(slots=True)
class Completion:
    activity_id: str
    # Verbatim, including None. Completion is detected structurally, so a
    # result with no status field is a legitimate success - inventing
    # "Completed" would report something the API never said.
    status: str | None
    result: Any
    poll_count: int
    elapsed_s: float


class FortyGuardHTTP:
    """Thin async transport. Owns no policy beyond timeouts and concurrency."""

    def __init__(self, settings: Settings | None = None,
                 client: httpx.AsyncClient | None = None,
                 semaphore: asyncio.Semaphore | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        # Injectable: a per-instance semaphore limits nothing, since the MCP
        # layer builds one transport per call. ToolContext owns the real one.
        self._sem = semaphore or asyncio.Semaphore(
            self.settings.max_concurrent_requests)

    # -- lifecycle ---------------------------------------------------------- #

    async def __aenter__(self) -> FortyGuardHTTP:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.base_url,
                timeout=self.settings.request_timeout_s,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("FortyGuardHTTP must be used as an async context manager")
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "api-key": self.settings.require_key(),
            "Content-Type": "application/json",
        }

    # -- primitives --------------------------------------------------------- #

    async def _request(self, method: str, path: str, json_body: Any) -> Any:
        async with self._sem:
            try:
                resp = await self.client.request(
                    method, path, headers=self._headers(), json=json_body
                )
            except httpx.HTTPError as e:
                # Never interpolate the key into a message.
                raise TransportError(type(e).__name__ + ": " + str(e), url=path) from e
            except (ValueError, TypeError) as e:
                # httpx encodes with `allow_nan=False`, so a NaN or Infinity in
                # the payload raises here, before anything is sent.
                raise UnsendableRequest(str(e), url=path) from e

        # A redirect is not a success. Redirects are not followed - that would
        # carry the api-key header to whatever host the response names - and a
        # 3xx has no body, so falling through the `>= 400` gate below reported
        # "still running" and an agent polled it forever.
        if 300 <= resp.status_code < 400:
            raise UnexpectedResponse(
                f"HTTP {resp.status_code} redirect to "
                f"{resp.headers.get('location') or 'an unnamed location'}. "
                f"Redirects are not followed: the request carries this "
                f"account's API key, and replaying it at a host named by the "
                f"response would send that key somewhere it does not belong. "
                f"If the API has genuinely moved, set FORTYGUARD_BASE_URL.",
                body=None, url=path,
            )

        # Bounded before decoding: the download path has always had a ceiling,
        # this one buffered whatever arrived.
        raw = resp.content
        if len(raw) > MAX_RESPONSE_BYTES:
            raise UnexpectedResponse(
                f"the response body is {len(raw):,} bytes, over the "
                f"{MAX_RESPONSE_BYTES:,} byte ceiling this client applies. It "
                f"was not parsed. A JSON API response is not this large; "
                f"something is between this client and the API, or the API is "
                f"returning something unexpected.",
                body=None, url=path,
            )

        try:
            body = resp.json()
        except ValueError:
            body = {"non_json_body": resp.text[:2000]}

        if resp.status_code >= 400:
            raise APIError(resp.status_code, body, url=path)
        return body

    async def post(self, path: str, json_body: Any) -> Any:
        return await self._request("POST", path, json_body)

    async def get(self, path: str) -> Any:
        return await self._request("GET", path, None)

    # -- endpoints ---------------------------------------------------------- #

    async def usage(self) -> Any:
        """
        Account plan, credits and per-endpoint breakdown.

        Needs the key in the BODY as `api_key` as well as the header - unlike
        every other endpoint, which accepts the header alone.
        """
        return await self.post(USAGE_PATH, {"api_key": self.settings.require_key()})

    async def submit(self, endpoint: str, payload: Any) -> Submission:
        body = await self.post(endpoint, payload)
        activity_id = _dig(body, "data", "activity_id")
        if not activity_id:
            # A 2xx with no activity_id is not an API error - the call
            # succeeded - it is a response we cannot act on.
            raise UnexpectedResponse(
                "submit succeeded but the response carried no data.activity_id, "
                "so there is nothing to poll",
                body=body, url=endpoint,
            )
        return Submission(activity_id=activity_id, submit_body=body, endpoint=endpoint)

    async def poll_once(self, activity_id: str) -> tuple[str | None, Any]:
        # PERCENT-ENCODED, nothing left safe. `activity_id` comes from the
        # agent and httpx normalises `..` while building the URL, so it could
        # rewrite the request onto any endpoint - carrying the api-key header.
        # Encoded rather than pattern-matched: the API owns the id format, and
        # encoding cannot traverse nor reject a legitimate id.
        body = await self.get(
            STATUS_PATH.format(activity_id=quote(activity_id, safe="")))
        return _dig(body, "data", "status"), body

    # -- the wait ----------------------------------------------------------- #

    async def wait(
        self,
        activity_id: str,
        *,
        timeout_s: float | None = None,
        on_progress: ProgressFn | None = None,
    ) -> Completion:
        """
        Poll until terminal, or until the budget runs out.

        Raises `PollTimeout` carrying the activity_id: the job is not cancelled
        and the caller can collect it later.
        """
        s = self.settings
        budget = s.poll_timeout_s if timeout_s is None else timeout_s
        delay = s.poll_initial_delay_s
        started = time.monotonic()
        polls = 0
        last_status: str | None = None

        while True:
            elapsed = time.monotonic() - started
            if elapsed >= budget:
                raise PollTimeout(activity_id, elapsed, last_status)

            # Sleep first: nothing is ready instantly, and this keeps the
            # cancellation point at the top of the loop.
            await asyncio.sleep(min(delay, max(0.0, budget - elapsed)))
            delay = min(delay * s.poll_backoff_factor, s.poll_max_delay_s)

            status, body = await self.poll_once(activity_id)
            polls += 1
            last_status = status or last_status
            elapsed = time.monotonic() - started

            if on_progress is not None:
                # Verbatim, including None.
                res = on_progress(status, elapsed, polls)
                if asyncio.iscoroutine(res):
                    await res

            # Structural, not string-matched: a renamed or new success
            # status keeps working. The status is still reported verbatim.
            if result_has_arrived(body):
                return Completion(
                    activity_id=activity_id,
                    status=status,
                    result=_dig(body, "data", "result"),
                    poll_count=polls,
                    elapsed_s=elapsed,
                )

            # Failure is only an early exit; an unrecognised string keeps
            # polling until the timeout, which is safe.
            if classify_status(status) == "failure":
                raise TaskFailed(activity_id, status or "unknown", body)
            # otherwise pending, including any status we do not recognise

    async def submit_and_wait(
        self,
        endpoint: str,
        payload: Any,
        *,
        timeout_s: float | None = None,
        on_progress: ProgressFn | None = None,
    ) -> Completion:
        sub = await self.submit(endpoint, payload)
        return await self.wait(
            sub.activity_id, timeout_s=timeout_s, on_progress=on_progress
        )


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur
