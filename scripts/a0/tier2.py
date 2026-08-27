"""
Tier 2 — Schema. Everything that determines a tool signature.

Enums are already known from Tier 4's 422 messages (the API lists them itself):
  analytic_type : 'tcm', 'time_of_measure', 'exceedance', 'persistence'
  filter_type   : 1, 2, 3, 4          <- U1 RESOLVED; there is no 5
  granularity   : 60, 80, 100

What is NOT known is what each RETURNS. That is what this measures.

    python scripts/a0/tier2.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from record import FIXTURE_ROOT, Recorder, breakdown_delta, probe_breakdown, scrub

OUT = FIXTURE_ROOT.parent / "a0_reports"
CEILING = 45  # 90s; legitimate heatmaps terminate in 21-36s

LAT, LON = 33.4484, -112.0740


def box(lat_min, lat_max, lon_min, lon_max):
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max],
            [lon_min, lat_max], [lon_min, lat_min]]]}}]}


GOOD = box(33.4700, 33.4790, -112.0950, -112.0800)   # 112 tiles at g=100


def square_m(metres: float):
    """Square AOI of a given edge length centred on downtown Phoenix."""
    dlat = metres / 111320.0
    dlon = metres / (111320.0 * 0.8345)   # cos(33.45)
    return box(LAT - dlat / 2, LAT + dlat / 2, LON - dlon / 2, LON + dlon / 2)


def forecast_dt(hours_ahead: int):
    t = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return {"start_date": t.strftime("%Y-%m-%d"),
            "start_time": t.strftime("%H:00"), "filter_type": 1}


CASES: list[tuple[str, str, dict]] = [
    # --- filter_type result shapes (1 already verified) ---
    ("t2_1_filter2_hours", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00",
                      "end_time": "09:00", "filter_type": 2}}),
    ("t2_1_filter3_day", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "filter_type": 3}}),
    ("t2_1_filter4_days", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 100,
        "date_time": {"start_date": "2024-07-01", "end_date": "2024-07-31",
                      "filter_type": 4}}),

    # --- analytic types (time_of_measure already verified) ---
    ("t2_5_exceedance", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "start_time": "06:00",
                      "end_time": "18:00", "filter_type": 2},
        "analytic_type": "exceedance", "threshold": 30, "direction": "above"}),
    ("t2_6_persistence", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "start_time": "06:00",
                      "end_time": "18:00", "filter_type": 2},
        "analytic_type": "persistence", "threshold": 30, "direction": "above"}),
    ("t2_5b_tcm_undocumented", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "filter_type": 3},
        "analytic_type": "tcm"}),

    # --- granularity: tile size and whether cost changes ---
    ("t2_15_granularity_60", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 60,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1}}),
    ("t2_15_granularity_80", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 80,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1}}),

    # --- minimum viable AOI: 220 m returned 0 tiles at full price ---
    ("t2_min_aoi_300m", "/v1/heatmap", {
        "polygon_aoi": square_m(300), "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1}}),
    ("t2_min_aoi_500m", "/v1/heatmap", {
        "polygon_aoi": square_m(500), "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1}}),
    ("t2_min_aoi_800m", "/v1/heatmap", {
        "polygon_aoi": square_m(800), "granularity": 100,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1}}),

    # --- forecast horizon: Tier 4 saw "must be for a past or present date" ---
    ("t2_forecast_plus6h", "/v1/heatmap", {
        "polygon_aoi": GOOD, "granularity": 100,
        "date_time": forecast_dt(6)}),

    # --- env_params: full parameter set on this plan ---
    ("t2_8_env_all_params", "/v1/env_params", {
        "latitude": LAT, "longitude": LON, "temperature": 30.0,
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1}}),

    # --- streetview back_view: docs show only a `front` object ---
    ("t2_12_streetview_back", "/v1/streetview", {
        "latitude": LAT, "longitude": LON, "vertical_angle": 10.0,
        "horizontal_angle": 90.0, "back_view": True}),
]


def describe(endpoint: str, case: str) -> dict:
    safe = endpoint.strip("/").replace("/", "_")
    path = FIXTURE_ROOT / safe / f"{case}.json"
    if not path.exists():
        return {"note": "no fixture"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    sub = doc.get("submit_response", {})
    out = {"submit_status": sub.get("status")}
    body = sub.get("body") or {}
    if sub.get("status") != 200:
        out["message"] = body.get("message")
        return out
    polls = doc.get("poll_responses") or []
    if not polls:
        return out | {"note": "no polls"}
    data = polls[-1].get("body", {}).get("data", {})
    out["terminal"] = data.get("status")
    out["duration_s"] = doc["timing"]["duration_s"]
    r = data.get("result")
    if not isinstance(r, dict):
        return out
    if "map_data" in r:
        feats = r["map_data"]["features"]
        out["n_features"] = len(feats)
        if feats:
            out["property_keys"] = list(feats[0]["properties"].keys())
            ring = feats[0]["geometry"]["coordinates"][0]
            lons = [c[0] for c in ring]
            lats = [c[1] for c in ring]
            out["tile_m"] = [
                round((max(lons) - min(lons)) * 111320 * 0.8345, 1),
                round((max(lats) - min(lats)) * 111320, 1)]
        st = r.get("stats_data") or {}
        out["stats_keys"] = list(st.keys())
        if "units" in st:
            out["units"] = st["units"]
        for k in ("min", "max", "mean", "n_cells"):
            if k in st:
                out[k] = st[k]
        if "temperature_stats" in st:
            out["temperature_stats"] = st["temperature_stats"]
    else:
        out["result_keys"] = list(r.keys())
        for k, v in r.items():
            if isinstance(v, dict):
                out[f"{k}_keys"] = list(v.keys())[:25]
    return out


def main() -> None:
    rec = Recorder(measure_credits=False)
    report: dict = {}
    grand_before = probe_breakdown()

    print("Tier 2 — Schema")
    print("=" * 78)

    for case, endpoint, payload in CASES:
        before = probe_breakdown()
        res = rec.record_async(case, endpoint, payload, poll_ceiling=CEILING)
        after = probe_breakdown()
        delta = breakdown_delta(before, after)
        info = describe(endpoint, case)
        per = next((v["per_call"] for v in delta.values()), 0)
        report[case] = {"endpoint": endpoint, "cost": per, **info}
        print(f"\n--- {case} ({endpoint}) cost={per}")
        print(f"    {json.dumps(info)[:520]}")

    grand = breakdown_delta(grand_before, probe_breakdown())
    total = sum(v["credits"] for v in grand.values())
    print("\n" + "=" * 78)
    print(f"TIER 2 TOTAL SPEND: {total:,} credits  {json.dumps(grand)}")

    report["_total_spend"] = total
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tier2_schema.json").write_text(
        json.dumps(scrub(report), indent=2), encoding="utf-8")
    print(f"  written -> {OUT / 'tier2_schema.json'}")


if __name__ == "__main__":
    main()
