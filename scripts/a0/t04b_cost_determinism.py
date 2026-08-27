"""
T0.4b — exact unit cost, and T3.1 — determinism, in one spend.

`activity_breakdown` attributes credits and call counts per endpoint, so a call
can be bracketed exactly rather than inferred from a total balance. Issuing the
SAME request twice answers both questions at once:

  * cost   : credits delta per endpoint, per call
  * determinism : do two identical requests return identical values?

T3.1 gates the entire cache design, so this is the highest-leverage pair of
calls in the campaign. Cost: 2 heatmap calls.

    python scripts/a0/t04b_cost_determinism.py
"""

from __future__ import annotations

import json

from record import (
    FIXTURE_ROOT,
    Recorder,
    breakdown_delta,
    probe_breakdown,
    scrub,
)
from tier0 import tiny_aoi

OUT = FIXTURE_ROOT.parent / "a0_reports"

PAYLOAD = {
    "polygon_aoi": tiny_aoi(),
    "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1},
    "granularity": 100,
}


def tiles_of(case: str) -> list[tuple[int, float]]:
    """(tile_id, average_temperature) pairs from a recorded fixture."""
    path = FIXTURE_ROOT / "v1_heatmap" / f"{case}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    terminal = doc["poll_responses"][-1]["body"]
    result = terminal["data"]["result"]
    feats = result["map_data"]["features"]
    return [(f["properties"]["tile_id"], f["properties"]["average_temperature"])
            for f in feats]


def stats_of(case: str) -> dict:
    path = FIXTURE_ROOT / "v1_heatmap" / f"{case}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc["poll_responses"][-1]["body"]["data"]["result"]["stats_data"]


def main() -> None:
    rec = Recorder(measure_credits=False)  # we bracket manually, more precisely
    report: dict = {}

    print("T0.4b / T3.1 — exact cost and determinism")
    print("=" * 60)

    for i, case in enumerate(["t3_1_determinism_a", "t3_1_determinism_b"], start=1):
        before = probe_breakdown()
        print(f"\n--- call {i}: {case} ---")
        res = rec.record_async(case, "/v1/heatmap", PAYLOAD)
        print(" ", res.line())
        after = probe_breakdown()
        delta = breakdown_delta(before, after)
        print(f"  credit delta: {json.dumps(delta)}")
        report[case] = {
            "delta": delta,
            "duration_s": res.duration_s,
            "poll_count": res.poll_count,
            "terminal": res.terminal_status,
            "skipped": res.skipped,
        }

    print("\n" + "=" * 60)
    print("DETERMINISM (T3.1)")
    print("=" * 60)
    try:
        a, b = tiles_of("t3_1_determinism_a"), tiles_of("t3_1_determinism_b")
        sa, sb = stats_of("t3_1_determinism_a"), stats_of("t3_1_determinism_b")
        print(f"  tile counts        : {len(a)} vs {len(b)}")
        identical = a == b
        print(f"  tiles identical    : {identical}")
        if not identical and len(a) == len(b):
            diffs = [(t1, v1, v2) for (t1, v1), (_, v2) in zip(a, b) if v1 != v2]
            print(f"  differing tiles    : {len(diffs)} / {len(a)}")
            if diffs:
                mx = max(abs(v1 - v2) for _, v1, v2 in diffs)
                print(f"  max abs difference : {mx:.6f} C")
                for t, v1, v2 in diffs[:5]:
                    print(f"    tile {t}: {v1} -> {v2}")
        print(f"  stats identical    : {sa == sb}")
        report["determinism"] = {
            "tiles_identical": identical,
            "stats_identical": sa == sb,
            "n_tiles": len(a),
        }
    except (OSError, KeyError, ValueError) as e:
        print(f"  could not compare: {e!r}")
        report["determinism"] = {"error": repr(e)}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "T0.4b_T3.1_cost_determinism.json").write_text(
        json.dumps(scrub(report), indent=2), encoding="utf-8")
    print(f"\n  written -> {OUT / 'T0.4b_T3.1_cost_determinism.json'}")

    det = report.get("determinism", {})
    if det.get("tiles_identical"):
        print("\n  => Cache design is SOUND as specified. Phase 5 unblocked.")
    elif "error" not in det:
        print("\n  => Values DRIFT between identical requests.")
        print("     Cache must become time-boxed with an explicit staleness warning.")


if __name__ == "__main__":
    main()
