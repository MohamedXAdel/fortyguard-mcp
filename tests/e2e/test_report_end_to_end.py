"""
Heat Intelligence, end to end: submit -> collect -> a PDF the caller can open.

This is the test the project did not have, and its absence is why the defect
shipped. Every existing heat_intelligence test replays a fixture whose
`download_link` was already replaced by a marker at record time, so they all
assert on a path where the link was never live.

Here the link is live. Two servers stand in for the two real hosts: a replay
server serving the API's JSON, and a separate origin serving the actual bytes -
which is the true shape of this endpoint, since the signed URL points at object
storage rather than at FortyGuard.

Everything runs through `MCPServer.call_tool`, the same path stdio takes.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fortyguard_mcp.config import Settings
from fortyguard_mcp.server import build_server
from fortyguard_mcp.tools.runtime import ToolContext
from tests.replay import ReplayServer

PDF_BYTES = b"%PDF-1.7\n" + b"multi-dimensional heat analysis " * 200 + b"\n%%EOF\n"
SIGNATURE = "b7f3c1deadbeefsignature"
ACTIVITY = "f3e1c68b-1cc3-46bc-8589-1faaf30ef30a"

HI_REQUEST: dict[str, Any] = {
    "latitude": 33.4746,
    "longitude": -112.0878,
    "temperature": 29.79,
    "date": "2024-07-15",
    "analysis": ["geographic", "urban"],
}


class _Origin(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/reports/") and SIGNATURE in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(PDF_BYTES)))
            self.end_headers()
            self.wfile.write(PDF_BYTES)
        else:                       # an unsigned or stale request is refused
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


@pytest.fixture
def download_url(origin: str) -> str:
    return (f"{origin}/reports/{ACTIVITY}.pdf"
            f"?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=900"
            f"&X-Amz-Signature={SIGNATURE}")


@pytest.fixture
def api(tmp_path: Path, download_url: str) -> Iterator[ReplayServer]:
    """
    A replay server serving one crafted fixture: heat_intelligence completing
    with a link to the live origin above.

    Written at test time rather than added to `tests/fixtures/`, because the
    origin's port is only known once it is listening - and because a recorded
    fixture must never contain a real signed URL.
    """
    root = tmp_path / "fx" / "v1_heat_intelligence"
    root.mkdir(parents=True)
    (root / "live_link.json").write_text(json.dumps({
        "case": "live_link",
        "request": {"method": "POST",
                    "url": "https://api.fortyguard.com/v1/heat_intelligence",
                    "body": HI_REQUEST},
        "submit_response": {"status": 200, "body": {
            "error": False, "status_code": 200,
            "message": "Heat Intelligence Submitted Successfully",
            "data": {"activity_id": ACTIVITY}}},
        "poll_responses": [{"status_code": 200, "body": {
            "error": False, "status_code": 200, "message": "Completed",
            "data": {"activity_id": ACTIVITY, "status": "Completed",
                     "result": {"download_link": download_url}}}}],
    }), encoding="utf-8")

    with ReplayServer(fixture_root=tmp_path / "fx") as srv:
        yield srv


@pytest.fixture
def server(api: ReplayServer, tmp_path: Path) -> Any:
    ctx = ToolContext(settings=Settings(
        report_allow_private_hosts=True,
        api_key="k" * 32, base_url=api.base_url, data_dir=tmp_path / "data"))
    return build_server(ctx), ctx


async def call(srv: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    res = await srv.call_tool(tool, args)
    return json.loads(res.content[0].text)


# --------------------------------------------------------------------------- #
# The whole point
# --------------------------------------------------------------------------- #

async def test_the_report_reaches_local_disk_and_the_caller_is_told_where(
    server: Any, tmp_path: Path
) -> None:
    srv, _ctx = server

    submitted = await call(srv, "submit_heat_intelligence", HI_REQUEST)
    assert submitted["activity_id"] == ACTIVITY
    assert submitted["status"] == "submitted"

    collected = await call(srv, "check_status", {"activity_id": ACTIVITY})

    report = collected.get("report")
    assert report is not None, "no report block: the deliverable is unreachable"
    assert report["downloaded"] is True
    assert report["source_field"] == "download_link"

    path = Path(report["path"])
    assert path.exists(), "the path handed to the caller must actually exist"
    assert path.read_bytes() == PDF_BYTES, "the PDF must be intact"
    assert path.read_bytes().startswith(b"%PDF-"), "openable as a PDF"
    assert report["size_bytes"] == len(PDF_BYTES)
    assert path.parent == tmp_path / "data" / "reports"


async def test_the_signed_url_still_reaches_nobody(server: Any) -> None:
    """
    Downloading must not weaken the redaction it exists to work around. The URL
    is a credential; the file is the deliverable.
    """
    srv, ctx = server
    await call(srv, "submit_heat_intelligence", HI_REQUEST)
    raw = (await srv.call_tool("check_status", {"activity_id": ACTIVITY})).content[0].text

    assert SIGNATURE not in raw, "the signature leaked into the tool response"
    assert "X-Amz-Signature" not in raw

    stored = ctx.results.path_for(ACTIVITY).read_text(encoding="utf-8")
    assert SIGNATURE not in stored, "the signature was written to the archive"
    assert "REDACTED" in stored


async def test_re_collecting_from_the_archive_still_names_the_report(
    server: Any
) -> None:
    """
    The `archived` flag had exactly this bug twice (rounds 6 and 8): present on
    the fresh path, absent when the same result was re-read from disk. A path
    the caller can open must not disappear on the second call.
    """
    srv, _ctx = server
    await call(srv, "submit_heat_intelligence", HI_REQUEST)
    first = await call(srv, "check_status", {"activity_id": ACTIVITY})
    second = await call(srv, "check_status", {"activity_id": ACTIVITY})

    assert second["from_archive"] is True
    assert second["credits_charged"] == 0
    assert "report" in second, "the report vanished on re-collection"
    assert second["report"]["path"] == first["report"]["path"]
    assert second["report"]["from_disk"] is True
    assert Path(second["report"]["path"]).exists()


async def test_the_file_is_fetched_once_not_on_every_read(
    server: Any, api: ReplayServer
) -> None:
    srv, ctx = server
    await call(srv, "submit_heat_intelligence", HI_REQUEST)
    await call(srv, "check_status", {"activity_id": ACTIVITY})
    stamp = ctx.results.report_path_for(ACTIVITY).stat().st_mtime_ns

    for _ in range(3):
        await call(srv, "check_status", {"activity_id": ACTIVITY})

    assert ctx.results.report_path_for(ACTIVITY).stat().st_mtime_ns == stamp, \
        "the report was re-downloaded from a link that is meant to be expiring"


async def test_an_identical_resubmission_is_free_and_still_has_the_report(
    server: Any
) -> None:
    srv, _ctx = server
    await call(srv, "submit_heat_intelligence", HI_REQUEST)
    await call(srv, "check_status", {"activity_id": ACTIVITY})

    again = await call(srv, "submit_heat_intelligence", HI_REQUEST)
    assert again["from_archive"] is True
    assert again["credits_charged"] == 0
    assert again["report"]["path"] == str(_ctx.results.report_path_for(ACTIVITY))


# --------------------------------------------------------------------------- #
# Failure must not cost the result
# --------------------------------------------------------------------------- #

async def test_a_dead_link_reports_plainly_and_keeps_the_paid_result(
    tmp_path: Path, origin: str
) -> None:
    """
    The analysis succeeded and was charged. A failed download must not turn that
    into a failed call - and must say the document is gone, not merely late.
    """
    root = tmp_path / "fx2" / "v1_heat_intelligence"
    root.mkdir(parents=True)
    dead = f"{origin}/reports/{ACTIVITY}.pdf?X-Amz-Signature=expired-and-invalid"
    (root / "dead_link.json").write_text(json.dumps({
        "case": "dead_link",
        "request": {"method": "POST",
                    "url": "https://api.fortyguard.com/v1/heat_intelligence",
                    "body": HI_REQUEST},
        "submit_response": {"status": 200, "body": {
            "error": False, "data": {"activity_id": ACTIVITY}}},
        "poll_responses": [{"status_code": 200, "body": {
            "error": False, "message": "Completed",
            "data": {"activity_id": ACTIVITY, "status": "Completed",
                     "result": {"download_link": dead}}}}],
    }), encoding="utf-8")

    with ReplayServer(fixture_root=tmp_path / "fx2") as api2:
        ctx = ToolContext(settings=Settings(
        report_allow_private_hosts=True,
            api_key="k" * 32, base_url=api2.base_url, data_dir=tmp_path / "d2"))
        srv = build_server(ctx)
        await call(srv, "submit_heat_intelligence", HI_REQUEST)
        out = await call(srv, "check_status", {"activity_id": ACTIVITY})

    assert out.get("error") is not True, "a lost PDF is not a failed call"
    assert out["archived"] is True, "the paid result must still be archived"
    assert out["report"]["downloaded"] is False
    assert "403" in out["report"]["reason"]
    assert "charged again" in out["report"]["note"], \
        "the caller must be told re-running is the only way to get the document"
    assert not ctx.results.has_report(ACTIVITY)


# --------------------------------------------------------------------------- #
# Every other endpoint is untouched
# --------------------------------------------------------------------------- #

async def test_a_heatmap_result_gains_no_report_block(tmp_path: Path) -> None:
    """
    The download is keyed on the SHAPE of the result, so it must be inert for
    the four endpoints that return no link. A stray `report` key on a heatmap
    would be the classic data-dependent envelope this codebase keeps catching.
    """
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    with ReplayServer(fixture_root=fixtures) as api2:
        ctx = ToolContext(settings=Settings(
        report_allow_private_hosts=True,
            api_key="k" * 32, base_url=api2.base_url, data_dir=tmp_path / "d3"))
        srv = build_server(ctx)
        fx = next(f for f in api2.index.fixtures
                  if f.path == "/v1/heatmap" and f.reached_terminal)
        out = await call(srv, "create_heatmap",
                         {**_heatmap_args(fx.request_body), "wait_s": 30})

    assert "report" not in out
    assert not (tmp_path / "d3" / "reports").exists(), \
        "no reports directory should be created for endpoints that link nothing"


def _heatmap_args(body: Any) -> dict[str, Any]:
    dt = (body or {}).get("date_time") or {}
    args: dict[str, Any] = {"polygon_aoi": body["polygon_aoi"]}
    for src, dst in (("start_date", "start_date"), ("start_time", "start_time"),
                     ("end_time", "end_time"), ("end_date", "end_date"),
                     ("filter_type", "filter_type")):
        if src in dt:
            args[dst] = dt[src]
    for k in ("granularity", "analytic_type", "threshold", "direction"):
        if k in (body or {}):
            args[k] = body[k]
    return args
