"""
T3.1 (proper) — determinism on an AOI that actually returns tiles.

The first attempt compared two EMPTY results (a ~220 m AOI returns Completed
with n_cells=0) and was therefore vacuous. This re-runs the exact Encanto
request from Trials/test_2x2_diagnostic.py, for which a genuine result was
saved previously, giving two independent comparisons:

  * fresh vs fresh   — not needed; one fresh call suffices because
  * fresh vs archived — the saved encanto_0500_map_data.json was produced days
    earlier from the identical request. If they match, the API is deterministic
    across days, which is the property the cache actually needs.

Cost: 1 heatmap call (4,220 credits).

    python scripts/a0/t31_determinism_real.py
"""

from __future__ import annotations

import json
from pathlib import Path

from record import FIXTURE_ROOT, Recorder, breakdown_delta, probe_breakdown, scrub

OUT = FIXTURE_ROOT.parent / "a0_reports"
ARCHIVE = Path(__file__).resolve().parents[3] / "Trials" / "encanto_0500_map_data.json"


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


# Identical to ENCANTO in Trials/test_2x2_diagnostic.py
PAYLOAD = {
    "polygon_aoi": box(33.4700, 33.4790, -112.0950, -112.0800),
    "date_time": {"start_date": "2024-07-15", "start_time": "05:00", "filter_type": 1},
    "granularity": 100,
}
CASE = "t3_1_determinism_encanto"


def tiles(features):
    return [(f["properties"]["tile_id"], f["properties"]["average_temperature"])
            for f in features]


def main() -> None:
    rec = Recorder(measure_credits=False)

    before = probe_breakdown()
    res = rec.record_async(CASE, "/v1/heatmap", PAYLOAD)
    print(res.line())
    after = probe_breakdown()
    delta = breakdown_delta(before, after)
    print(f"  credit delta: {json.dumps(delta)}")

    doc = json.loads((FIXTURE_ROOT / "v1_heatmap" / f"{CASE}.json").read_text("utf-8"))
    fresh_result = doc["poll_responses"][-1]["body"]["data"]["result"]
    fresh = tiles(fresh_result["map_data"]["features"])
    fresh_stats = fresh_result["stats_data"]

    archived_doc = json.loads(ARCHIVE.read_text("utf-8"))
    archived = tiles(archived_doc["map_data"]["features"])
    archived_stats = archived_doc["stats_data"]

    print("\n" + "=" * 60)
    print("T3.1  DETERMINISM — fresh call vs archived identical request")
    print("=" * 60)
    print(f"  tile count fresh / archived : {len(fresh)} / {len(archived)}")

    report = {"credit_delta": delta, "n_fresh": len(fresh), "n_archived": len(archived)}

    if len(fresh) != len(archived):
        print("  !! tile counts differ — geometry itself is not stable")
        report["verdict"] = "tile_count_mismatch"
    else:
        ids_match = [t for t, _ in fresh] == [t for t, _ in archived]
        diffs = [(t, a, b) for (t, a), (_, b) in zip(fresh, archived) if a != b]
        print(f"  tile ids identical          : {ids_match}")
        print(f"  differing values            : {len(diffs)} / {len(fresh)}")
        if diffs:
            mx = max(abs(a - b) for _, a, b in diffs)
            print(f"  max abs difference          : {mx:.6f} C")
            for t, a, b in diffs[:5]:
                print(f"    tile {t}: archived={a} fresh={b}")
            report["max_abs_diff"] = mx
        print(f"  stats_data identical        : {fresh_stats == archived_stats}")
        report["n_differing"] = len(diffs)
        report["stats_identical"] = fresh_stats == archived_stats
        report["verdict"] = "deterministic" if not diffs else "drifts"

        print()
        if not diffs:
            print("  => DETERMINISTIC across days. Cache design SOUND as specified.")
            print("     Phase 5 unblocked; keys need no time component.")
        else:
            print("  => VALUES DRIFT for an identical request.")
            print("     Cache must be time-boxed with an explicit staleness warning.")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "T3.1_determinism.json").write_text(
        json.dumps(scrub(report), indent=2), encoding="utf-8")
    print(f"\n  written -> {OUT / 'T3.1_determinism.json'}")


if __name__ == "__main__":
    main()
