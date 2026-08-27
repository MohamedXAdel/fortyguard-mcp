"""
Report download — the path that makes `heat_intelligence` deliver anything.

WHAT WENT WRONG, AND WHY NOTHING CAUGHT IT
------------------------------------------
`/v1/heat_intelligence` costs credits, takes ~395 s, and its whole deliverable
is `data.result.download_link`. Signed URLs are credentials, so the server
stripped them from the archive AND from every tool response - which meant the
analysis was charged for and its output was unreachable by every route offered.
Nothing in `src/` had ever downloaded the file; `download_link` appeared only in
the redaction key set.

The suite could not see it. The A0 recorder replaced the live URL with a marker
at record time, so the only heat_intelligence fixture in the library exercises a
path where the link was never real. A test that replays that fixture proves the
redaction works and says nothing about whether anyone can read the report.

So these tests use a real HTTP server serving real bytes over a real socket, and
assert on what lands on disk.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from fortyguard_mcp.client.download import (
    ReportFetchFailed,
    fetch_to_file,
    find_downloadable,
)

PDF_BYTES = b"%PDF-1.7\n" + b"heat intelligence report payload " * 300 + b"\n%%EOF\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/report.pdf"):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(PDF_BYTES)))
            self.end_headers()
            self.wfile.write(PDF_BYTES)
        elif self.path.startswith("/expired"):
            body = b'{"message":"Request has expired"}'
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/huge"):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.end_headers()
            for _ in range(64):
                self.wfile.write(b"x" * 8192)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    """A stand-in for the object storage a signed URL points at."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def signed(origin: str, path: str = "/report.pdf") -> str:
    """A URL shaped like the real thing: signature parameters and all."""
    return (f"{origin}{path}?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential=AKIAEXAMPLE%2F20260826%2Fus-east-1"
            f"&X-Amz-Date=20260826T075500Z&X-Amz-Expires=900"
            f"&X-Amz-Signature=deadbeefcafe")


# --------------------------------------------------------------------------- #
# Finding the link
# --------------------------------------------------------------------------- #

def test_finds_the_link_heat_intelligence_actually_returns() -> None:
    result = {"download_link": "https://example.com/r.pdf?X-Amz-Signature=abc"}
    assert find_downloadable(result) == (
        "download_link", "https://example.com/r.pdf?X-Amz-Signature=abc")


def test_finds_a_nested_link() -> None:
    assert find_downloadable(
        {"data": {"inner": [{"signed_url": "https://e.com/x"}]}}
    ) == ("signed_url", "https://e.com/x")


def test_no_link_in_a_heatmap_result_is_not_an_error() -> None:
    """Every other endpoint must pass through this untouched."""
    heatmap = {"map_data": {"features": []}, "stats_data": {"n_cells": 0}}
    assert find_downloadable(heatmap) is None
    assert find_downloadable(None) is None
    assert find_downloadable({"download_link": None}) is None


def test_a_non_http_link_is_not_treated_as_downloadable() -> None:
    assert find_downloadable({"download_link": "/local/path.pdf"}) is None


def test_the_searched_keys_are_exactly_the_redacted_keys() -> None:
    """
    The set fetched and the set redacted must be one definition.

    If they diverge, the divergent case is a link that gets destroyed before
    anything downloads it - which is the bug this whole module exists to fix,
    reappearing quietly.
    """
    from fortyguard_mcp.client import download
    from fortyguard_mcp.store.results_store import LINK_KEYS

    assert download.LINK_KEYS is LINK_KEYS


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

async def test_downloads_the_file_and_returns_a_usable_path(
    origin: str, tmp_path: Path
) -> None:
    dest = tmp_path / "reports" / "act-1.pdf"
    got = await fetch_to_file(signed(origin), dest,
                              timeout_s=10, max_bytes=10_000_000, allow_private_hosts=True)

    assert got.path == dest
    assert dest.exists(), "the whole point is a file on disk"
    assert dest.read_bytes() == PDF_BYTES, "bytes must be intact, not truncated"
    assert got.size_bytes == len(PDF_BYTES)
    assert got.content_type == "application/pdf"
    assert dest.read_bytes().startswith(b"%PDF-"), "a real PDF, openable"


async def test_creates_the_reports_directory_if_absent(
    origin: str, tmp_path: Path
) -> None:
    dest = tmp_path / "a" / "b" / "c" / "act.pdf"
    await fetch_to_file(signed(origin), dest, timeout_s=10,
                        max_bytes=10_000_000, allow_private_hosts=True)
    assert dest.exists()


async def test_an_expired_link_fails_without_leaving_a_file(
    origin: str, tmp_path: Path
) -> None:
    dest = tmp_path / "act-2.pdf"
    with pytest.raises(ReportFetchFailed) as e:
        await fetch_to_file(signed(origin, "/expired"), dest,
                            timeout_s=10, max_bytes=10_000_000, allow_private_hosts=True)
    assert "403" in str(e.value)
    assert not dest.exists()
    assert list(tmp_path.glob("*.part")) == [], "no partial file survives"


async def test_a_dead_host_fails_cleanly(tmp_path: Path) -> None:
    dest = tmp_path / "act-3.pdf"
    with pytest.raises(ReportFetchFailed):
        # Port 1 on loopback: nothing listens, so this is a connection error.
        await fetch_to_file("http://127.0.0.1:1/report.pdf", dest,
                            timeout_s=5, max_bytes=10_000_000, allow_private_hosts=True)
    assert not dest.exists()
    assert list(tmp_path.glob("*.part")) == []


async def test_oversize_is_refused_and_the_partial_is_deleted(
    origin: str, tmp_path: Path
) -> None:
    """
    A truncated PDF that looks finished is worse than no PDF: the caller has no
    way to tell. So the partial is discarded, not kept.
    """
    dest = tmp_path / "act-4.pdf"
    with pytest.raises(ReportFetchFailed) as e:
        await fetch_to_file(signed(origin, "/huge"), dest,
                            timeout_s=10, max_bytes=50_000, allow_private_hosts=True)
    assert "50,000 byte limit" in str(e.value)
    assert not dest.exists()
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "file://C:/Windows/win.ini",
    "ftp://example.com/x.pdf",
    "data:application/pdf;base64,AAAA",
])
async def test_only_http_schemes_are_fetched(url: str, tmp_path: Path) -> None:
    """
    A malformed or hostile API response must not be able to make the server read
    the user's own disk and hand the contents back.
    """
    dest = tmp_path / "act-5.pdf"
    with pytest.raises(ReportFetchFailed) as e:
        await fetch_to_file(url, dest, timeout_s=5, max_bytes=10_000, allow_private_hosts=True)
    assert "refusing to fetch" in str(e.value)
    assert not dest.exists()


# --------------------------------------------------------------------------- #
# The URL is a credential
# --------------------------------------------------------------------------- #

async def test_the_signed_url_never_appears_in_the_failure_message(
    origin: str, tmp_path: Path
) -> None:
    """
    `str(e)` on an httpx error can carry the whole URL, and the whole URL is the
    credential. The host is fine - it is not the secret part.
    """
    url = signed(origin, "/expired")
    with pytest.raises(ReportFetchFailed) as e:
        await fetch_to_file(url, tmp_path / "x.pdf",
                            timeout_s=10, max_bytes=10_000, allow_private_hosts=True)
    rendered = str(e.value) + repr(e.value.to_dict())
    assert "X-Amz-Signature" not in rendered
    assert "deadbeefcafe" not in rendered
    assert url not in rendered


def test_the_download_url_cannot_survive_in_the_logs() -> None:
    """
    Downloading created a NEW place for a signed URL to escape: httpx logs the
    full request line, and that line now carries the credential.

    Two independent guards, asserted separately so neither can quietly become
    the only one - httpx is pinned below INFO, and the handler filter scrubs the
    pattern even if something logs it anyway.
    """
    import io
    import logging

    from fortyguard_mcp.logging_setup import configure_logging, scrub

    url = ("https://tos-dashboard-prod.s3.amazonaws.com/r.pdf"
           "?X-Amz-Expires=900&X-Amz-Signature=deadbeefcafe")

    assert "deadbeefcafe" not in scrub(f"HTTP Request: GET {url}", None)

    handler = configure_logging("DEBUG", key_source=lambda: "k" * 32)
    buf = io.StringIO()
    handler.stream = buf                       # type: ignore[attr-defined]
    try:
        httpx_logger = logging.getLogger("httpx")
        assert httpx_logger.getEffectiveLevel() >= logging.WARNING, \
            "httpx would narrate every download at INFO, URL included"
        httpx_logger.warning('HTTP Request: GET %s "200 OK"', url)
        assert "deadbeefcafe" not in buf.getvalue()
        assert "[REDACTED]" in buf.getvalue()
    finally:
        logging.getLogger().handlers.clear()


async def test_no_api_key_is_sent_to_the_third_party_host(
    origin: str, tmp_path: Path
) -> None:
    """
    The signed URL points at object storage, not at FortyGuard. Reusing the API
    transport would send the user's key to a host that has no business with it.
    """
    seen: list[dict[str, str]] = []

    async def spy(request: httpx.Request) -> httpx.Response:
        seen.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, content=PDF_BYTES,
                              headers={"content-type": "application/pdf"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(spy)) as client:
        await fetch_to_file(signed(origin), tmp_path / "x.pdf",
                            timeout_s=10, max_bytes=10_000_000,
                            client=client, allow_private_hosts=True)

    assert seen, "the request was never made"
    assert "api-key" not in seen[0]
    assert "authorization" not in seen[0]
