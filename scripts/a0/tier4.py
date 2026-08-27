"""
Tier 4 — Error taxonomy. FREE: rejected and failed tasks consume no credits.

Every message the MCP server produces under principle P4 ("errors that teach")
is built from what is recorded here, not invented. Run exhaustively.

Also the most likely place to finally observe a `Failed` terminal status, which
Tier 0 did not produce.

    python scripts/a0/tier4.py
"""

from __future__ import annotations

import json

import requests

from record import (
    BASE_URL,
    FIXTURE_ROOT,
    Recorder,
    _json_or_text,
    breakdown_delta,
    probe_breakdown,
    scrub,
)

OUT = FIXTURE_ROOT.parent / "a0_reports"

LAT, LON = 33.4484, -112.0740


def box(lat_min, lat_max, lon_min, lon_max):
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[
                [lon_min, lat_min], [lon_max, lat_min],
                [lon_max, lat_max], [lon_min, lat_max],
                [lon_min, lat_min],
            ]]},
        }],
    }


GOOD = box(33.4700, 33.4790, -112.0950, -112.0800)          # known-good, 112 tiles
HUGE = box(33.0000, 34.5000, -113.0000, -111.0000)          # far over any cap
DT1 = {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1}


def unclosed_ring():
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[
                [-112.0950, 33.4700], [-112.0800, 33.4700],
                [-112.0800, 33.4790], [-112.0950, 33.4790],
            ]]},  # last != first
        }],
    }


def transposed():
    """[lat, lon] instead of [lon, lat] — the classic GeoJSON footgun."""
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [[
                [33.4700, -112.0950], [33.4700, -112.0800],
                [33.4790, -112.0800], [33.4790, -112.0950],
                [33.4700, -112.0950],
            ]]},
        }],
    }


CASES: list[tuple[str, str, dict]] = [
    ("e_aoi_over_cap", "/v1/heatmap",
     {"polygon_aoi": HUGE, "date_time": DT1, "granularity": 100}),

    ("e_date_before_floor", "/v1/heatmap",
     {"polygon_aoi": GOOD,
      "date_time": {"start_date": "2020-12-31", "start_time": "05:00", "filter_type": 1},
      "granularity": 100}),

    ("e_date_far_future", "/v1/heatmap",
     {"polygon_aoi": GOOD,
      "date_time": {"start_date": "2027-01-01", "start_time": "05:00", "filter_type": 1},
      "granularity": 100}),

    ("e_coords_outside_us", "/v1/heatmap",
     {"polygon_aoi": box(51.500, 51.510, -0.130, -0.115),  # London
      "date_time": DT1, "granularity": 100}),

    ("e_unclosed_ring", "/v1/heatmap",
     {"polygon_aoi": unclosed_ring(), "date_time": DT1, "granularity": 100}),

    ("e_lat_lon_transposed", "/v1/heatmap",
     {"polygon_aoi": transposed(), "date_time": DT1, "granularity": 100}),

    ("e_granularity_50", "/v1/heatmap",
     {"polygon_aoi": GOOD, "date_time": DT1, "granularity": 50}),

    ("e_granularity_120", "/v1/heatmap",
     {"polygon_aoi": GOOD, "date_time": DT1, "granularity": 120}),

    ("e_filter_type_9", "/v1/heatmap",
     {"polygon_aoi": GOOD,
      "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 9},
      "granularity": 100}),

    ("e_bad_analytic_type", "/v1/heatmap",
     {"polygon_aoi": GOOD, "date_time": DT1, "granularity": 100,
      "analytic_type": "not_a_real_type"}),

    ("e_geojson_bare_polygon", "/v1/heatmap",
     {"polygon_aoi": {"type": "Polygon", "coordinates":
                      GOOD["features"][0]["geometry"]["coordinates"]},
      "date_time": DT1, "granularity": 100}),

    ("e_geojson_missing_features", "/v1/heatmap",
     {"polygon_aoi": {"type": "FeatureCollection"},
      "date_time": DT1, "granularity": 100}),

    ("e_missing_granularity", "/v1/heatmap",
     {"polygon_aoi": GOOD, "date_time": DT1}),

    ("e_missing_polygon", "/v1/heatmap",
     {"date_time": DT1, "granularity": 100}),

    ("e_env_empty_analysis", "/v1/env_params",
     {"latitude": LAT, "longitude": LON, "temperature": 30.0,
      "date_time": DT1, "analysis": []}),

    ("e_env_bad_param_name", "/v1/env_params",
     {"latitude": LAT, "longitude": LON, "temperature": 30.0,
      "date_time": DT1, "analysis": ["not_a_parameter"]}),

    ("e_env_lat_out_of_range", "/v1/env_params",
     {"latitude": 999.0, "longitude": LON, "temperature": 30.0,
      "date_time": DT1}),
]


def bad_api_key() -> dict:
    """Recorded separately — needs a header the shared session must never send."""
    url = f"{BASE_URL}/v1/heatmap"
    try:
        r = requests.post(
            url,
            headers={"api-key": "fg_live_invalid_key_for_testing", "Content-Type": "application/json"},
            json={"polygon_aoi": GOOD, "date_time": DT1, "granularity": 100},
            timeout=30,
        )
        return {"status_code": r.status_code, "body": _json_or_text(r)}
    except requests.RequestException as e:
        return {"error": repr(e)}


def summarise(endpoint: str, case: str) -> dict:
    safe = endpoint.strip("/").replace("/", "_")
    path = FIXTURE_ROOT / safe / f"{case}.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    sub = doc.get("submit_response", {})
    body = sub.get("body", {})
    out = {
        "submit_status": sub.get("status"),
        "message": body.get("message") if isinstance(body, dict) else None,
        "field": body.get("field") if isinstance(body, dict) else None,
    }
    if isinstance(body, dict) and "details" in body:
        out["details"] = body["details"]
    if isinstance(body, dict) and "detail" in body:
        d = body["detail"]
        out["detail"] = d[:2] if isinstance(d, list) else d
    polls = doc.get("poll_responses") or []
    if polls:
        last = polls[-1].get("body", {})
        out["terminal_status"] = (last.get("data") or {}).get("status")
        out["poll_count"] = len(polls)
    return out


def main() -> None:
    rec = Recorder(measure_credits=False)
    report: dict = {}

    before_all = probe_breakdown()

    print("Tier 4 — Error taxonomy (expected free)")
    print("=" * 72)

    # Short ceiling: an error case that neither rejects at submit nor fails fast
    # IS the finding. The over-cap AOI ground for 467s without terminating, so
    # there is no reason to wait minutes per case to learn that.
    for case, endpoint, payload in CASES:
        res = rec.record_async(case, endpoint, payload, poll_ceiling=12)
        info = summarise(endpoint, case)
        report[case] = {"endpoint": endpoint, **info}
        msg = info.get("message") or info.get("details") or info.get("detail") or ""
        term = info.get("terminal_status")
        term_s = f" terminal={term}" if term else ""
        print(f"  {case:26s} HTTP {str(info.get('submit_status')):4s}{term_s}  {str(msg)[:80]}")

    print("\n--- invalid api key ---")
    bad = bad_api_key()
    report["e_invalid_api_key"] = bad
    print(f"  HTTP {bad.get('status_code')}  {json.dumps(bad.get('body'))[:160]}")

    after_all = probe_breakdown()
    spent = breakdown_delta(before_all, after_all)

    print("\n" + "=" * 72)
    print(f"  TOTAL CREDITS SPENT BY TIER 4: {json.dumps(spent) if spent else '0 — all free, as expected'}")

    statuses = {v.get("terminal_status") for v in report.values()
                if isinstance(v, dict) and v.get("terminal_status")}
    print(f"  terminal statuses observed: {sorted(statuses) or '(none — all rejected at submit)'}")

    report["_credits_spent"] = spent
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tier4_error_taxonomy.json").write_text(
        json.dumps(scrub(report), indent=2), encoding="utf-8")
    print(f"\n  written -> {OUT / 'tier4_error_taxonomy.json'}")


if __name__ == "__main__":
    main()
