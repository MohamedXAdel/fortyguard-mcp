"""
Regression tests for the security audit findings.

One test per finding, each asserting the specific behaviour that was wrong.
None of these existed when the defects were found, which is why they survived.
Finding ids match the audit report.
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from fortyguard_mcp.client.download import (
    BlockedHost,
    _resolve_and_check,
    fetch_to_file,
)
from fortyguard_mcp.client.errors import UnexpectedResponse
from fortyguard_mcp.client.http import MAX_RESPONSE_BYTES, FortyGuardHTTP
from fortyguard_mcp.client.results import empty_result_notice
from fortyguard_mcp.config import Settings
from fortyguard_mcp.domain.geo import (
    count_dropped_holes,
    count_polygons,
    describe_aoi,
    extract_rings,
    plan_split,
    split_ring,
)
from fortyguard_mcp.store.results_store import (
    SIGNED_URL_RE,
    ResultStore,
    scrub_for_storage,
)

PHX = [(-112.10, 33.44), (-112.09, 33.44), (-112.09, 33.45),
       (-112.10, 33.45), (-112.10, 33.44)]


def _settings(tmp_path, **kw):
    return Settings(api_key="k" * 32, data_dir=tmp_path, **kw)


# --------------------------------------------------------------------------- #
# H1 - SSRF via the report download link
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/admin",
    "http://169.254.169.254/latest/meta-data/",      # cloud metadata
    "http://[::1]:9000/x.pdf",
    "http://10.0.0.5/internal.pdf",
    "http://192.168.1.1/router",
    "http://172.16.0.1/x",
    "http://localhost/x.pdf",
])
def test_h1_private_addresses_are_refused(url):
    """A download_link naming an internal service must not be fetched."""
    with pytest.raises(BlockedHost):
        _resolve_and_check(url, allow_private=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://h/x", "gopher://h/x"])
def test_h1_non_http_schemes_still_refused(url):
    with pytest.raises(BlockedHost):
        _resolve_and_check(url, allow_private=False)


def test_h1_the_escape_hatch_works_for_self_hosted_storage():
    assert _resolve_and_check("http://127.0.0.1/x.pdf", allow_private=True) == "127.0.0.1"


async def test_h1_a_redirect_to_a_private_address_is_refused(tmp_path):
    """
    The original bypass: hop one is public, hop two is not.

    httpx's own follow_redirects validated nothing after the first URL, so this
    is the case that made the scheme check decorative.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, content=b"SECRET")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               follow_redirects=False)
    # Literal public IP, so the check resolves it without touching DNS and this
    # test stays offline like the rest of the suite.
    with pytest.raises(Exception) as excinfo:
        await fetch_to_file("http://93.184.216.34/start", tmp_path / "r.pdf",
                            timeout_s=5, max_bytes=1_000_000, client=client)
    assert "refused" in str(excinfo.value).lower()
    assert not (tmp_path / "r.pdf").exists()


# --------------------------------------------------------------------------- #
# M1 - activity_id traversing out of the status path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("activity_id", [
    "../../v1/system/fetch-api-key-usage",
    "../../../secret/internal",
    "x/../../../../etc/passwd",
    "..%2f..%2fadmin",
    "x?leak=1",
])
async def test_m1_activity_id_cannot_leave_the_status_path(activity_id, tmp_path):
    """
    Whatever the id contains, the request must stay on /v1/status/.

    It used to reach any path on the API host, carrying the api-key header, and
    reflect the response back into the model's context.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # raw_path, not path: `.path` percent-DECODES for display, so a safely
        # encoded id still reads as "../.." there.
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"data": {"status": "Processing"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.fortyguard.test")
    async with FortyGuardHTTP(_settings(tmp_path), client=client) as api:
        await api.poll_once(activity_id)

    assert len(seen) == 1
    prefix = "/v1/status/"
    assert seen[0].startswith(prefix), seen[0]
    # Exactly one path segment after the prefix, and no query smuggled on.
    tail = seen[0][len(prefix):]
    assert "/" not in tail and "?" not in tail, seen[0]


async def test_m1_a_normal_uuid_is_unchanged(tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"data": {"status": "Completed"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.fortyguard.test")
    uuid = "04ad0d0c-804e-4b2e-b37a-e13e6a83a229"
    async with FortyGuardHTTP(_settings(tmp_path), client=client) as api:
        await api.poll_once(uuid)
    assert seen == [f"/v1/status/{uuid}"]


# --------------------------------------------------------------------------- #
# M3 - the false "0 tiles, credits consumed" notice
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("result", [
    {"metadata": {}, "locations": []},                    # env_params
    {"coordinates": {}, "segmentation": {}},              # satellite
    {"coordinates": {}, "front": {}},                     # streetview
    {"download_link": "https://x/y.pdf"},                 # heat_intelligence
])
def test_m3_non_heatmap_results_are_not_called_empty(result):
    """These four endpoints never have tiles; saying they returned none is wrong."""
    assert empty_result_notice(result) is None


def test_m3_a_genuinely_empty_heatmap_still_warns():
    empty = {"map_data": {"type": "FeatureCollection", "features": []},
             "stats_data": {"activity_id": "a", "n_cells": 0}}
    notice = empty_result_notice(empty)
    assert notice is not None
    assert "0 tiles" in notice


def test_m3_a_populated_heatmap_does_not_warn():
    full = {"map_data": {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"tile_id": 1, "average_temperature": 30.0},
         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}}]},
        "stats_data": {"n_cells": 1}}
    assert empty_result_notice(full) is None


# --------------------------------------------------------------------------- #
# M4 - archive isolation between API keys
# --------------------------------------------------------------------------- #

def _payload():
    return {"map_data": {"type": "FeatureCollection", "features": []},
            "stats_data": {"n_cells": 0}}


def test_m4_a_second_key_cannot_read_the_first_keys_result(tmp_path):
    """The id-keyed route used to ignore scope entirely."""
    a = ResultStore(Settings(api_key="A" * 32, data_dir=tmp_path))
    a.put("job-1", "/v1/heatmap", {"q": 1}, _payload())
    assert a.get("job-1") is not None

    b = ResultStore(Settings(api_key="B" * 32, data_dir=tmp_path))
    assert b.get("job-1") is None
    assert b.find_by_request("/v1/heatmap", {"q": 1}) is None


def test_m4_the_owning_key_still_reads_its_own(tmp_path):
    a = ResultStore(Settings(api_key="A" * 32, data_dir=tmp_path))
    a.put("job-1", "/v1/heatmap", {"q": 1}, _payload())
    again = ResultStore(Settings(api_key="A" * 32, data_dir=tmp_path))
    assert again.get("job-1") is not None
    assert again.find_by_request("/v1/heatmap", {"q": 1}) is not None


def test_m4_a_different_base_url_is_a_different_scope(tmp_path):
    """Staging must not answer with production data marked authoritative."""
    prod = ResultStore(Settings(api_key="A" * 32, data_dir=tmp_path))
    prod.put("job-1", "/v1/heatmap", {"q": 1}, _payload())
    stag = ResultStore(Settings(api_key="A" * 32, data_dir=tmp_path,
                                base_url="https://staging.fortyguard.test"))
    assert stag.get("job-1") is None


def test_m4_pre_scope_archives_are_adopted_not_orphaned(tmp_path):
    """An upgrade must not strip access to results the user already paid for."""
    store = ResultStore(Settings(api_key="A" * 32, data_dir=tmp_path))
    store.put("job-1", "/v1/heatmap", {"q": 1}, _payload())

    meta = store._meta_path("job-1")
    d = json.loads(meta.read_text(encoding="utf-8"))
    del d["scope"]                                   # as written by an older version
    meta.write_text(json.dumps(d), encoding="utf-8")

    assert store.get("job-1") is not None


def test_m4_storage_info_still_describes_the_whole_directory(tmp_path):
    """Disk belongs to the OS user, not to one key - counts must stay honest."""
    ResultStore(Settings(api_key="A" * 32, data_dir=tmp_path)).put(
        "job-a", "/v1/heatmap", {"q": 1}, _payload())
    b = ResultStore(Settings(api_key="B" * 32, data_dir=tmp_path))
    b.put("job-b", "/v1/heatmap", {"q": 2}, _payload())
    assert b.info().result_count == 2


# --------------------------------------------------------------------------- #
# L1 / L2 - geometry input handling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cap", [5e-324, 1e-320, math.inf, math.nan, 0.0, -1.0])
def test_l1_a_hostile_cap_raises_valueerror_not_overflowerror(cap):
    """split_aoi catches ValueError; OverflowError escaped as a raw ToolError."""
    with pytest.raises(ValueError):
        plan_split(PHX, cap)
    with pytest.raises(ValueError):
        split_ring(PHX, cap)


def _nested(depth: int):
    node = {"type": "Polygon", "coordinates": [[list(p) for p in PHX]]}
    for _ in range(depth):
        node = {"type": "FeatureCollection", "features": [node]}
    return node


@pytest.mark.parametrize("depth", [200, 700, 5000])
def test_l2_deep_nesting_is_reported_not_a_recursionerror(depth):
    """These three walkers had no depth guard; _has_non_finite beside them did."""
    payload = _nested(depth)
    extract_rings(payload)
    count_polygons(payload)
    count_dropped_holes(payload)
    assert describe_aoi(payload)["readable"] is False


def test_l2_ordinary_nesting_still_reads():
    assert describe_aoi(_nested(2))["readable"] is True


# --------------------------------------------------------------------------- #
# L4 - protocol-relative signed URLs
# --------------------------------------------------------------------------- #

def test_l4_a_scheme_relative_signed_url_is_redacted():
    """It matched neither startswith('http') nor the regex, so it was archived raw."""
    obj = {"download_link": "//storage.example.com/f.pdf?X-Amz-Signature=deadbeef"}
    clean, changed = scrub_for_storage(obj, "k" * 32)
    assert changed is True
    assert "X-Amz-Signature" not in json.dumps(clean)


@pytest.mark.parametrize("url", [
    "https://storage.googleapis.com/b/f.pdf?X-Goog-Signature=abc",
    "https://s3.amazonaws.com/b/f.pdf?X-Amz-Signature=abc",
    "//acct.blob.core.windows.net/c/f.pdf?se=2026-01-01&sig=abc",
])
def test_l4_known_signed_url_forms_all_match(url):
    assert SIGNED_URL_RE.search(url) is not None


def test_l4_an_ordinary_url_is_not_mangled():
    """Over-redaction corrupts the archive; `?case=1` must not match on `se=`."""
    assert SIGNED_URL_RE.search("https://example.com/docs?case=1&usage=2") is None


# --------------------------------------------------------------------------- #
# L6 / L7 - transport responses
# --------------------------------------------------------------------------- #

async def test_l6_a_redirect_is_an_error_not_a_pending_job(tmp_path):
    """A 3xx fell through the >=400 gate and was reported as 'still running'."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.test/x"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.fortyguard.test")
    async with FortyGuardHTTP(_settings(tmp_path), client=client) as api:
        with pytest.raises(UnexpectedResponse) as e:
            await api.poll_once("abc")
    assert "redirect" in str(e.value).lower()


async def test_l7_an_oversized_response_is_refused_before_parsing(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"A" * (MAX_RESPONSE_BYTES + 1))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://api.fortyguard.test")
    async with FortyGuardHTTP(_settings(tmp_path), client=client) as api:
        with pytest.raises(UnexpectedResponse) as e:
            await api.poll_once("abc")
    assert "ceiling" in str(e.value).lower()


# --------------------------------------------------------------------------- #
# L8 - base_url validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "http://api.fortyguard.com",        # plaintext: the key is a header
    "ftp://api.fortyguard.com",
    "not-a-url",
    "https://",
])
def test_l8_a_bad_base_url_is_rejected(url, tmp_path):
    with pytest.raises(ValueError):
        Settings(api_key="k" * 32, data_dir=tmp_path, base_url=url)


@pytest.mark.parametrize("url", [
    "https://api.fortyguard.com",
    "http://127.0.0.1:8931",            # the replay server
    "http://localhost:8931",
])
def test_l8_https_and_loopback_are_accepted(url, tmp_path):
    assert Settings(api_key="k" * 32, data_dir=tmp_path, base_url=url).base_url


def test_l8_a_trailing_slash_is_normalised(tmp_path):
    s = Settings(api_key="k" * 32, data_dir=tmp_path,
                 base_url="https://api.fortyguard.com/")
    assert s.base_url == "https://api.fortyguard.com"


# --------------------------------------------------------------------------- #
# M2 - an archive failure must not destroy a paid result
# --------------------------------------------------------------------------- #

async def test_m2_a_storage_failure_still_returns_the_paid_result(tmp_path, monkeypatch):
    """
    The result is in memory and has been charged for. A full disk is not a
    reason to lose it - it used to escape as a bare ToolError string.
    """
    from fortyguard_mcp.client.http import Completion
    from fortyguard_mcp.tools import runtime
    from fortyguard_mcp.tools.runtime import ToolContext, _archive_and_shape

    ctx = ToolContext(settings=_settings(tmp_path))

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runtime.ResultStore, "put", boom)

    done = Completion(activity_id="job-1", status="Completed",
                      result=_payload(), poll_count=1, elapsed_s=1.0)
    out = await _archive_and_shape(ctx, done, "/v1/heatmap", {"q": 1},
                                   fmt="auto", budget_tokens=None, precision=None)

    assert out["archived"] is False
    assert out["activity_id"] == "job-1"
    assert "No space left on device" in out["archive_error"]
    assert out["result"] is not None                  # the payload survived
    assert "check_status" in out["archive_note"]      # and is recoverable
    await ctx.aclose()


async def test_m2_a_healthy_archive_still_reports_success(tmp_path):
    from fortyguard_mcp.client.http import Completion
    from fortyguard_mcp.tools.runtime import ToolContext, _archive_and_shape

    ctx = ToolContext(settings=_settings(tmp_path))
    done = Completion(activity_id="job-2", status="Completed",
                      result=_payload(), poll_count=1, elapsed_s=1.0)
    out = await _archive_and_shape(ctx, done, "/v1/heatmap", {"q": 1},
                                   fmt="auto", budget_tokens=None, precision=None)
    assert out["archived"] is True
    assert "archive_error" not in out
    await ctx.aclose()


# --------------------------------------------------------------------------- #
# Schema fidelity - a parameter the API requires must not be optional here
# --------------------------------------------------------------------------- #

def test_streetview_angles_are_required_in_the_schema():
    """
    The API returns 422 "Field 'vertical_angle' is required" when either angle
    is absent, so declaring them optional made the default call impossible:
    `_prune` dropped the Nones and the request could only ever fail.

    Both recorded fixtures happened to supply them, so no replay test could
    catch it - it took a live call from an agent.
    """
    from fortyguard_mcp.server import build_server
    from fortyguard_mcp.tools.runtime import ToolContext

    server = build_server(ToolContext(settings=_settings_for_schema()))
    tools = server._tool_manager.list_tools()
    sv = next(t for t in tools if t.name == "submit_streetview")
    required = set((sv.parameters or {}).get("required", []))

    # The live 422 named only the two angles, because back_view happened to be
    # supplied on that call. The vendor documentation lists all five under
    # Required attributes, and every recorded fixture carries all five.
    assert required == {"latitude", "longitude", "vertical_angle",
                        "horizontal_angle", "back_view"}


def test_heat_intelligence_analysis_is_required_in_the_schema():
    """
    The API returns 422 "Field 'analysis' is required" when it is absent, but
    the tool description said "Omit for all" - so following the documentation
    produced a request that could not succeed.

    Same shape as the streetview angles: the single recorded fixture supplied
    it, so the omit-case was never exercised by any replay test.
    """
    from fortyguard_mcp.server import build_server
    from fortyguard_mcp.tools.runtime import ToolContext

    server = build_server(ToolContext(settings=_settings_for_schema()))
    tools = server._tool_manager.list_tools()
    hi = next(t for t in tools if t.name == "submit_heat_intelligence")
    assert "analysis" in set((hi.parameters or {}).get("required", []))
    # And the description must not still promise the opposite.
    assert "Omit for all" not in (hi.description or "")


def test_every_field_the_api_always_receives_is_still_sent(tmp_path):
    """
    Guards the shape of the bug rather than the one instance: for the two
    endpoints whose recorded requests always carry a field, that field must
    survive `_prune` when the caller supplies it.
    """
    from fortyguard_mcp.server import _prune

    body = _prune({
        "latitude": 33.4484, "longitude": -112.074,
        "vertical_angle": 10.0, "horizontal_angle": 90.0, "back_view": False,
    })
    assert set(body) == {"latitude", "longitude", "vertical_angle",
                         "horizontal_angle", "back_view"}
    # False and 0.0 are meaningful values, not absences.
    assert body["back_view"] is False


def _settings_for_schema():
    import tempfile
    return Settings(api_key="k" * 32, data_dir=tempfile.mkdtemp())


def test_satellite_granularity_stays_optional():
    """
    Measured live: a satellite call omitting `granularity` was ACCEPTED (200).

    The vendor documentation lists it under Required attributes and is wrong -
    the same way it is wrong for heatmap, where `e_missing_granularity` also
    returned 200. Requiring it here would force every caller to choose a value
    the API is content to pick itself.
    """
    from fortyguard_mcp.server import build_server
    from fortyguard_mcp.tools.runtime import ToolContext

    server = build_server(ToolContext(settings=_settings_for_schema()))
    tools = server._tool_manager.list_tools()
    sat = next(t for t in tools if t.name == "submit_satellite")
    assert "granularity" not in set((sat.parameters or {}).get("required", []))


def test_env_params_exposes_analysis_and_it_is_optional():
    """
    Measured: env_params calls omitting `analysis` returned 200 twice, so it is
    optional - but it was not exposed at all, so a caller could never narrow
    the response and always paid for all 15 parameters.
    """
    from fortyguard_mcp.server import build_server
    from fortyguard_mcp.tools.runtime import ToolContext

    server = build_server(ToolContext(settings=_settings_for_schema()))
    tools = server._tool_manager.list_tools()
    ep = next(t for t in tools if t.name == "get_env_params")
    props = (ep.parameters or {}).get("properties", {})
    assert "analysis" in props
    assert "analysis" not in set((ep.parameters or {}).get("required", []))


def test_the_two_analysis_vocabularies_are_kept_apart():
    """
    `analysis` means different things on the two endpoints that take it, with
    opposite optionality. Each description must warn about the other, or a
    caller will send report sections to env_params and get a 422.
    """
    from fortyguard_mcp.domain.api_schema import (
        ENV_ANALYSIS_HINT,
        REPORT_ANALYSIS_HINT,
    )
    from fortyguard_mcp.server import build_server
    from fortyguard_mcp.tools.runtime import ToolContext

    assert not set(ENV_ANALYSIS_HINT) & set(REPORT_ANALYSIS_HINT)

    server = build_server(ToolContext(settings=_settings_for_schema()))
    tools = server._tool_manager.list_tools()
    by_name = {t.name: t for t in tools}
    env = by_name["get_env_params"].parameters["properties"]["analysis"]
    rep = by_name["submit_heat_intelligence"].parameters["properties"]["analysis"]
    assert "submit_heat_intelligence" in env["description"]
    assert "get_env_params" in rep["description"]
