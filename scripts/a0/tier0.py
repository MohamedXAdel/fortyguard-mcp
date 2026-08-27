"""
Tier 0 — Foundation. Must run before anything else can be measured.

  T0.1  What does /v1/system/fetch-api-key-usage actually return?
  T0.2  Enumerate every status string, INCLUDING in-progress values.
  T0.3  What comes back for an unknown activity_id?
  T0.4  Cost of the smallest legal request — establishes the floor unit
        BEFORE any larger spend.

Deliberately tiny: roughly 4-6 billable calls. Run this, read the report, then
decide the budget for the rest of the campaign with real numbers in hand.

    python scripts/a0/tier0.py
"""

from __future__ import annotations

import json
from pathlib import Path

from record import (
    FIXTURE_ROOT,
    Recorder,
    STATUS_PATH,
    BASE_URL,
    _headers,
    _json_or_text,
    new_activity_uuid,
    probe_usage,
    scrub,
)

import requests

OUT = FIXTURE_ROOT.parent / "a0_reports"


def tiny_aoi() -> dict:
    """
    Smallest sensible AOI: ~220 m x 220 m over downtown Phoenix. Inside the
    footprint already known to return data, so a failure here means something
    other than coverage.
    """
    lat_min, lat_max = 33.4480, 33.4500
    lon_min, lon_max = -112.0750, -112.0726
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon_min, lat_min],
                    [lon_max, lat_min],
                    [lon_max, lat_max],
                    [lon_min, lat_max],
                    [lon_min, lat_min],
                ]],
            },
        }],
    }


def t0_1() -> dict:
    print("\n=== T0.1  fetch-api-key-usage ===")
    raw, balance = probe_usage()
    accepted = raw.get("accepted_method")
    print(f"  accepted method : {accepted}")
    print(f"  extracted balance: {balance}")
    for att in raw["attempts"]:
        status = att.get("status", att.get("error"))
        print(f"    {att['method']:5s} -> {status}")
    if accepted and balance is None:
        print("  !! endpoint responded 200 but no balance key matched.")
        print("     Inspect the recorded body and extend _BALANCE_KEY_CANDIDATES.")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "T0.1_usage.json").write_text(
        json.dumps(scrub(raw), indent=2), encoding="utf-8")
    return {"accepted_method": accepted, "balance": balance, "raw": raw}


def t0_3() -> dict:
    print("\n=== T0.3  status for an unknown activity_id ===")
    fake = new_activity_uuid()
    url = f"{BASE_URL}{STATUS_PATH.format(activity_id=fake)}"
    try:
        r = requests.get(url, headers=_headers(), timeout=30)
        body = _json_or_text(r)
        print(f"  HTTP {r.status_code}")
        print(f"  body: {json.dumps(body)[:400]}")
        rec = {"activity_id": fake, "status_code": r.status_code, "body": body}
    except requests.RequestException as e:
        print(f"  request failed: {e!r}")
        rec = {"activity_id": fake, "error": repr(e)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "T0.3_unknown_activity.json").write_text(
        json.dumps(scrub(rec), indent=2), encoding="utf-8")
    return rec


def t0_4(rec: Recorder) -> dict:
    print("\n=== T0.4  smallest legal heatmap — the floor unit cost ===")
    payload = {
        "polygon_aoi": tiny_aoi(),
        "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1},
        "granularity": 100,
    }
    res = rec.record_async("t0_4_smallest_heatmap", "/v1/heatmap", payload)
    print(res.line())
    if res.statuses_seen:
        print(f"  statuses observed (T0.2): {res.statuses_seen}")
    return {
        "credits_delta": res.credits_delta,
        "duration_s": res.duration_s,
        "terminal": res.terminal_status,
        "statuses_seen": res.statuses_seen,
        "poll_count": res.poll_count,
    }


def collect_statuses() -> list[str]:
    """T0.2 — harvest every status string seen across all fixtures recorded so far."""
    seen: set[str] = set()
    for p in FIXTURE_ROOT.rglob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for s in doc.get("observed_statuses", []) or []:
            seen.add(s)
    return sorted(seen)


def main() -> None:
    print("Tier 0 — Foundation")
    print("=" * 60)

    usage = t0_1()
    can_measure_cost = usage["balance"] is not None
    if not can_measure_cost:
        print("\n  NOTE: balance could not be extracted; T0.4 will still run and")
        print("        record timing, but credits_delta will be null.")

    unknown = t0_3()

    rec = Recorder(measure_credits=can_measure_cost)
    floor = t0_4(rec)

    statuses = collect_statuses()

    print("\n" + "=" * 60)
    print("TIER 0 SUMMARY")
    print("=" * 60)
    print(f"  usage endpoint method : {usage['accepted_method']}")
    print(f"  credit balance        : {usage['balance']}")
    print(f"  unknown-id HTTP status: {unknown.get('status_code')}")
    print(f"  floor call cost       : {floor['credits_delta']} credits")
    print(f"  floor call duration   : {floor['duration_s']} s over {floor['poll_count']} polls")
    print(f"  terminal status value : {floor['terminal']}")
    print(f"  T0.2 statuses observed: {statuses or '(none captured)'}")

    summary = {
        "T0.1": usage, "T0.2": {"statuses_observed": statuses},
        "T0.3": unknown, "T0.4": floor,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tier0_summary.json").write_text(
        json.dumps(scrub(summary), indent=2), encoding="utf-8")
    print(f"\n  written -> {OUT / 'tier0_summary.json'}")

    if floor["credits_delta"] is None:
        print("\n  NEXT: credit deltas unresolved. Fall back to N-identical-calls / N")
        print("        (combine with T3.1 determinism — same calls answer both).")
    else:
        print(f"\n  NEXT: report {floor['credits_delta']} credits/call to set the")
        print("        Tier 3.5 cost-grid density before spending further.")


if __name__ == "__main__":
    main()
