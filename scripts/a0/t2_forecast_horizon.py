"""
Forecast horizon — re-test against the REAL data horizon.

The first probe (now + 6h, i.e. 2026-08-23) returned 0 tiles and I concluded
forecasting was broken. That conclusion was wrong: per FortyGuard's update the
latest available data is 2026-08-22 04:00 UTC, with forecast extending ~12h
beyond THAT. So the earlier probe was past the end of the data entirely, not
inside the forecast window.

This maps the actual boundary. Note the API reports GMT-7 for Phoenix, so the
local/UTC interpretation of `start_time` is itself part of what's being tested.

Cost: 4,220 per probe.

    python scripts/a0/t2_forecast_horizon.py
"""

from __future__ import annotations

import json

from record import FIXTURE_ROOT, Recorder, breakdown_delta, probe_breakdown, scrub

OUT = FIXTURE_ROOT.parent / "a0_reports"
CEILING = 45


def box(lat_min, lat_max, lon_min, lon_max):
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [[
            [lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max],
            [lon_min, lat_max], [lon_min, lat_min]]]}}]}


GOOD = box(33.4700, 33.4790, -112.0950, -112.0800)   # 112 tiles at g=100

# Stated horizon: 2026-08-22 04:00 UTC, forecast +12h beyond it.
PROBES = [
    ("f_0821_1200", "2026-08-21", "12:00", "well inside history"),
    ("f_0822_0400", "2026-08-22", "04:00", "stated latest available"),
    ("f_0822_1000", "2026-08-22", "10:00", "+6h into forecast window"),
    ("f_0822_1600", "2026-08-22", "16:00", "+12h — forecast edge"),
    ("f_0822_2200", "2026-08-22", "22:00", "+18h — expected past the edge"),
    ("f_0823_0400", "2026-08-23", "04:00", "+24h — expected empty"),
]


def n_tiles(case: str) -> tuple[int, str, dict]:
    p = FIXTURE_ROOT / "v1_heatmap" / f"{case}.json"
    if not p.exists():
        return -1, "no fixture", {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    sub = doc.get("submit_response", {})
    if sub.get("status") != 200:
        return -1, f"HTTP {sub.get('status')}: {(sub.get('body') or {}).get('message')}", {}
    polls = doc.get("poll_responses") or []
    if not polls:
        return -1, "no polls", {}
    data = polls[-1].get("body", {}).get("data", {})
    r = data.get("result") or {}
    feats = (r.get("map_data") or {}).get("features", [])
    stats = (r.get("stats_data") or {}).get("temperature_stats", {})
    return len(feats), data.get("status", "?"), stats


def main() -> None:
    rec = Recorder(measure_credits=False)
    before = probe_breakdown()
    report = {}

    print("Forecast horizon probe")
    print("=" * 88)
    print(f"  {'case':16s} {'date':12s} {'time':6s} {'tiles':>6s}  {'status':10s} note")
    print("-" * 88)

    for case, date, time_, note in PROBES:
        payload = {
            "polygon_aoi": GOOD, "granularity": 100,
            "date_time": {"start_date": date, "start_time": time_, "filter_type": 1},
        }
        rec.record_async(case, "/v1/heatmap", payload, poll_ceiling=CEILING)
        n, status, stats = n_tiles(case)
        mean = stats.get("mean")
        mean_s = f" mean={mean:.2f}C" if isinstance(mean, (int, float)) else ""
        print(f"  {case:16s} {date:12s} {time_:6s} {n:>6d}  {status:10s} {note}{mean_s}")
        report[case] = {"date": date, "time": time_, "n_tiles": n,
                        "status": status, "note": note,
                        "mean": mean}

    delta = breakdown_delta(before, probe_breakdown())
    total = sum(v["credits"] for v in delta.values())

    print("-" * 88)
    have = [c for c, v in report.items() if v["n_tiles"] > 0]
    empty = [c for c, v in report.items() if v["n_tiles"] == 0]
    print(f"  returned data : {have}")
    print(f"  returned empty: {empty}")
    print(f"\n  spend: {total:,} credits")

    if have:
        last = report[have[-1]]
        print(f"\n  => Latest timestamp returning data: {last['date']} {last['time']}")
        print(f"     ({last['note']})")

    report["_spend"] = total
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "forecast_horizon.json").write_text(
        json.dumps(scrub(report), indent=2), encoding="utf-8")
    print(f"  written -> {OUT / 'forecast_horizon.json'}")


if __name__ == "__main__":
    main()
