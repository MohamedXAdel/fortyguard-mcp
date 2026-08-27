"""
Fetching the one thing an API result cannot carry: a file behind a signed URL.

`/v1/heat_intelligence`'s entire deliverable is `data.result.download_link`, a
temporary pre-signed URL. Treat that URL as being as sensitive as the API key
itself, not as a scoped, expiring capability - so it is stripped from logs,
the archive and every tool response. This module fetches the file while the
link is still valid and hands back a local path instead.

Three deliberate choices:

* **Its own HTTP client.** `FortyGuardHTTP` attaches the `api-key` header; a
  signed URL points at third-party storage, where that key has no business going.
* **No retry.** A failure is almost always expiry or a revoked signature.
* **Destinations are checked.** The URL comes from an API response, so every
  hop is resolved and refused if it is not a public address.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..store.results_store import LINK_KEYS, is_link_value
from .errors import FortyGuardError

# Refused before a request is made. `file://` in particular would let a
# malformed or hostile API response read the user's disk and hand it back.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Redirects are followed by hand so every hop is checked; httpx's own
# `follow_redirects` validates nothing after the first URL.
MAX_REDIRECTS = 5


class BlockedHost(FortyGuardError):
    """
    A URL resolved to an address this client will not fetch from.

    Separate from `ReportFetchFailed` so the reason is legible: nothing went
    wrong on the network, the destination was refused before a byte was sent.
    """


def _resolve_and_check(url: str, *, allow_private: bool) -> str:
    """
    Refuse a URL that points anywhere but the public internet. Returns the host.

    The URL comes from an API response, so it names a destination of its
    choosing - cloud metadata and loopback admin ports included. EVERY resolved
    address is checked, not just the first.

    Not proof against DNS rebinding, which can change the address between this
    check and the connection; it closes the realistic case for one resolution.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedHost(
            f"refusing to fetch a {parsed.scheme or 'scheme-less'} URL; only "
            f"{', '.join(sorted(ALLOWED_SCHEMES))} are downloaded"
        )

    host = parsed.hostname
    if not host:
        raise BlockedHost("the link carries no host, so there is nothing to fetch")
    if allow_private:
        return host

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedHost(f"{host} could not be resolved ({e.strerror or e})") from e

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        # `is_global` excludes loopback, link-local (where cloud metadata
        # lives), RFC1918, unique-local, multicast and reserved - IPv6 included.
        if not addr.is_global:
            raise BlockedHost(
                f"{host} resolves to {addr}, which is not a public address. "
                f"This client only downloads from the public internet, so a "
                f"link naming an internal service cannot be used to read it. "
                f"Set FORTYGUARD_REPORT_ALLOW_PRIVATE_HOSTS=true if you "
                f"deliberately serve reports from a private address."
            )
    return host


@dataclass(frozen=True, slots=True)
class FetchedFile:
    path: Path
    size_bytes: int
    content_type: str | None


class ReportFetchFailed(FortyGuardError):
    """
    The linked file could not be retrieved.

    Never carries the URL - that is the credential. The host is named; it is
    useful and is not the secret part.

    Not raised past the tool layer: the analysis was paid for, so this is
    reported alongside the result rather than failing the call.
    """

    def __init__(self, detail: str, *, host: str | None = None) -> None:
        self.detail = detail
        self.host = host
        where = f" from {host}" if host else ""
        super().__init__(f"Could not download the linked file{where}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "message": str(self),
            "source": "report-download",
            "host": self.host,
        }


def _walk(obj: Any, depth: int = 0) -> Iterator[tuple[str, str]]:
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in LINK_KEYS and is_link_value(v):
                yield (k, v)
            else:
                yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)


def find_downloadable(result: Any) -> tuple[str, str] | None:
    """
    The first `(field_name, url)` in a result that names a downloadable file.

    Keyed on `LINK_KEYS`, not on the endpoint, so what is fetched is by
    construction what `scrub_for_storage` redacts. Two lists would drift, and
    the drifted case is a link redacted before anything fetched it.
    """
    return next(_walk(result), None)


async def fetch_to_file(
    url: str,
    dest: Path,
    *,
    timeout_s: float,
    max_bytes: int,
    client: httpx.AsyncClient | None = None,
    allow_private_hosts: bool = False,
) -> FetchedFile:
    """
    Stream a URL to `dest`. Raises `ReportFetchFailed` and leaves no partial file.

    Streamed to a temp file and moved into place, so a reader never sees a
    half-written document. Every hop passes `_resolve_and_check` first.

    `client` is injectable for tests; a fresh one carries no default headers,
    which is what keeps the API key off a third-party host.
    """
    try:
        host = _resolve_and_check(url, allow_private=allow_private_hosts)
    except BlockedHost as e:
        # Reported through the same type as every other download failure, so
        # the tool layer keeps one branch and the analysis is still returned.
        raise ReportFetchFailed(str(e), host=urlsplit(url).hostname) from e

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    os.close(fd)          # reopened by name below; mkstemp is used for the name
    tmp = Path(tmp_name)
    written = 0
    content_type: str | None = None

    owns_client = client is None
    # No `headers=`, no `base_url`: nothing of ours travels to this host.
    # `follow_redirects=False` is load-bearing - see the manual walk below.
    http = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)

    try:
        try:
            current = url
            for hop in range(MAX_REDIRECTS + 1):
                async with http.stream("GET", current, timeout=timeout_s) as resp:
                    if 300 <= resp.status_code < 400 and "location" in resp.headers:
                        if hop == MAX_REDIRECTS:
                            raise ReportFetchFailed(
                                f"the link redirected more than {MAX_REDIRECTS} "
                                f"times without delivering a file",
                                host=host,
                            )
                        # Resolved against the current URL, so a relative
                        # Location is handled the way a browser would.
                        nxt = str(httpx.URL(current).join(resp.headers["location"]))
                        try:
                            host = _resolve_and_check(
                                nxt, allow_private=allow_private_hosts)
                        except BlockedHost as e:
                            # Why hops are walked here: hop one passing says
                            # nothing about hop two.
                            raise ReportFetchFailed(
                                f"the link redirected to a destination that was "
                                f"refused - {e}",
                                host=urlsplit(nxt).hostname,
                            ) from e
                        current = nxt
                        continue

                    if resp.status_code >= 400:
                        raise ReportFetchFailed(
                            f"HTTP {resp.status_code} - the link has most likely "
                            f"expired, since these URLs are short-lived",
                            host=host,
                        )
                    content_type = resp.headers.get("content-type")
                    with tmp.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            written += len(chunk)
                            if written > max_bytes:
                                raise ReportFetchFailed(
                                    f"the file exceeds the {max_bytes:,} byte "
                                    f"limit (FORTYGUARD_REPORT_MAX_BYTES); the "
                                    f"partial download was discarded rather "
                                    f"than kept as a truncated file",
                                    host=host,
                                )
                            fh.write(chunk)
                    break
        except httpx.HTTPError as e:
            # `str(e)` can contain the full URL, which is the credential. Only
            # the exception type travels.
            raise ReportFetchFailed(type(e).__name__, host=host) from e

        tmp.replace(dest)
        return FetchedFile(path=dest, size_bytes=written, content_type=content_type)

    except BaseException:
        # A truncated PDF that looks finished is worse than no file.
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            await http.aclose()
