"""
Tier 1 — Entitlement. Which tools can exist at all.

Each endpoint must be polled TO TERMINAL STATUS, not merely submitted.
Trials/test_satellite.py only ever POSTed and printed, so "returned 200 OK"
proved nothing about entitlement: a task that fails costs zero credits, so the
API has no reason to reject at submit time.

Note: satellite is already proven entitled INDIRECTLY — the T0.1 activity
breakdown shows "Tile Satellite Segmentation: 14,400 credits, count 1", and
credits are only deducted on success. This run captures its result SCHEMA
(T2.11), which the breakdown cannot give us.

    python scripts/a0/tier1.py [--skip-satellite]
"""

from __future__ import annotations

import json
import sys

from record import FIXTURE_ROOT, Recorder, breakdown_delta, probe_breakdown, scrub

OUT = FIXTURE_ROOT.parent / "a0_reports"

LAT, LON = 33.4484, -112.0740          # downtown Phoenix, known-good footprint
TEMP_C = 30.2129                        # from a real completed heatmap tile
DATE = "2024-07-15"

CASES = [
    ("t1_1_satellite", "/v1/satellite", {
        "sat": {"latitude": LAT, "longitude": LON},
        "date_time": {"start_date": DATE, "start_time": "14:00", "filter_type": 1},
        "granularity": 80,
    }),
    ("t1_2_streetview_front", "/v1/streetview", {
        "latitude": LAT, "longitude": LON,
        "vertical_angle": 10.0, "horizontal_angle": 90.0,
        "back_view": False,
    }),
    ("t1_3_heat_intelligence", "/v1/heat_intelligence", {
        "latitude": LAT, "longitude": LON,
        "temperature": TEMP_C,
        "date": DATE,
        "analysis": ["geographic", "environmental", "urban", "events", "anthropogenic"],
    }),
]


def describe_result(endpoint: str, case: str) -> dict:
    safe = endpoint.strip("/").replace("/", "_")
    path = FIXTURE_ROOT / safe / f"{case}.json"
    if not path.exists():
        return {"note": "no fixture"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    polls = doc.get("poll_responses") or []
    if not polls:
        return {"note": "no polls recorded"}
    body = polls[-1].get("body", {})
    data = body.get("data", {}) if isinstance(body, dict) else {}
    result = data.get("result")
    out = {"terminal_status": data.get("status")}
    if isinstance(result, dict):
        out["result_keys"] = list(result.keys())
        for k, v in result.items():
            if isinstance(v, dict):
                out[f"{k}_keys"] = list(v.keys())[:20]
            elif isinstance(v, list):
                out[f"{k}_len"] = len(v)
    elif result is not None:
        out["result_type"] = type(result).__name__
    return out


def main() -> None:
    skip_sat = "--skip-satellite" in sys.argv
    rec = Recorder(measure_credits=False)
    report: dict = {}

    print("Tier 1 — Entitlement")
    print("=" * 60)

    for case, endpoint, payload in CASES:
        if skip_sat and "satellite" in case:
            print(f"\n--- {case}: skipped by flag ---")
            continue
        print(f"\n--- {case}  ({endpoint}) ---")
        before = probe_breakdown()
        res = rec.record_async(case, endpoint, payload)
        print(" ", res.line())
        after = probe_breakdown()
        delta = breakdown_delta(before, after)
        print(f"  credit delta : {json.dumps(delta) if delta else '{} (no charge)'}")

        shape = describe_result(endpoint, case)
        print(f"  result shape : {json.dumps(shape)[:600]}")

        entitled = res.terminal_status is not None and \
            res.terminal_status.strip().lower() in {"completed", "succeeded", "success"}
        print(f"  ENTITLED     : {entitled}")

        report[case] = {
            "endpoint": endpoint,
            "submit_status": res.submit_status,
            "terminal_status": res.terminal_status,
            "entitled": entitled,
            "credit_delta": delta,
            "duration_s": res.duration_s,
            "statuses_seen": res.statuses_seen,
            "result_shape": shape,
        }

    print("\n" + "=" * 60)
    print("TIER 1 SUMMARY — which tools ship")
    print("=" * 60)
    for case, info in report.items():
        mark = "SHIP" if info["entitled"] else "DO NOT SHIP"
        cost = info["credit_delta"] or {}
        per = next((v["per_call"] for v in cost.values()), None)
        print(f"  {case:28s} {mark:12s} terminal={info['terminal_status']} cost={per}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tier1_entitlement.json").write_text(
        json.dumps(scrub(report), indent=2), encoding="utf-8")
    print(f"\n  written -> {OUT / 'tier1_entitlement.json'}")


if __name__ == "__main__":
    main()
