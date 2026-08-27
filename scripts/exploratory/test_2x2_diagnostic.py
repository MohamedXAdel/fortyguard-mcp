import os
import time
import json
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("FORTYGUARD_API_KEY") or os.environ.get("API_KEY")
if not api_key:
    raise SystemExit("set FORTYGUARD_API_KEY")
headers = {"api-key": api_key, "Content-Type": "application/json"}

def box(lat_min, lat_max, lon_min, lon_max):
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon_min, lat_min], [lon_max, lat_min],
                    [lon_max, lat_max], [lon_min, lat_max],
                    [lon_min, lat_min]
                ]]
            }
        }]
    }

# Downtown Phoenix (uniform impervious) - same box as prior 14:00 run (spread 0.15C)
DOWNTOWN = box(33.4400, 33.4600, -112.0850, -112.0600)

# Encanto Park box: straddles park (west portion) and built-up neighborhood (east portion)
# Park approx bounds: lat 33.4700-33.4790, lon -112.0980 to -112.0870
# Box below: west edge inside park, east edge well outside -> ~50% park coverage
ENCANTO = box(33.4700, 33.4790, -112.0950, -112.0800)


def submit(payload):
    r = requests.post("https://api.fortyguard.com/v1/heatmap", headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["data"]["activity_id"]


def poll(activity_id, label, timeout_polls=60):
    url = f"https://api.fortyguard.com/v1/status/{activity_id}"
    for i in range(timeout_polls):
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()["data"]
        status = data.get("status")
        if status == "Completed":
            return data.get("result")
        if status == "Failed":
            print(f"[{label}] FAILED:", data)
            return None
        time.sleep(3)
    print(f"[{label}] timed out")
    return None


def run_heatmap(label, polygon, start_time):
    payload = {
        "polygon_aoi": polygon,
        "date_time": {"start_date": "2024-07-15", "start_time": start_time, "filter_type": 1},
        "granularity": 100
    }
    aid = submit(payload)
    print(f"[{label}] activity_id={aid}")
    result = poll(aid, label)
    if not result:
        return None
    stats = result.get("stats_data", {}).get("temperature_stats", {})
    mn, mx = stats.get("minimum"), stats.get("maximum")
    spread = (mx - mn) if (mn is not None and mx is not None) else None
    print(f"[{label}] min={mn} max={mx} spread={spread}")
    return result, spread


results = {}

print("=== Downtown 05:00 ===")
results["downtown_0500"] = run_heatmap("downtown_0500", DOWNTOWN, "05:00")

print("\n=== Encanto 05:00 ===")
results["encanto_0500"] = run_heatmap("encanto_0500", ENCANTO, "05:00")

print("\n=== Encanto 14:00 ===")
results["encanto_1400"] = run_heatmap("encanto_1400", ENCANTO, "14:00")

print("\n=== time_of_measure over Encanto box, entire day ===")
tom_payload = {
    "polygon_aoi": ENCANTO,
    "date_time": {"start_date": "2024-07-15", "filter_type": 3},
    "granularity": 100,
    "analytic_type": "time_of_measure"
}
aid = submit(tom_payload)
print(f"[time_of_measure] activity_id={aid}")
tom_result = poll(aid, "time_of_measure")

print("\n\n########## SUMMARY ##########")
for label in ["downtown_0500", "encanto_0500", "encanto_1400"]:
    r = results[label]
    if r:
        _, spread = r
        print(f"{label}: spread = {spread:.4f} C" if spread is not None else f"{label}: spread unknown")
    else:
        print(f"{label}: FAILED / no data")

print("\n--- time_of_measure raw structure ---")
if tom_result:
    print("Top-level keys:", list(tom_result.keys()))
    map_data = tom_result.get("map_data")
    print("map_data type:", type(map_data))
    if isinstance(map_data, list):
        print("map_data length:", len(map_data))
        print("First 3 entries:")
        for entry in map_data[:3]:
            print(" ", entry)
    elif isinstance(map_data, dict):
        print("map_data keys:", list(map_data.keys()))
        print(json.dumps(map_data, indent=2)[:2000])

    stats_data = tom_result.get("stats_data")
    print("\nstats_data:", json.dumps(stats_data, indent=2)[:3000])
else:
    print("time_of_measure FAILED / no data")

# Save full raw result for later inspection
with open("tom_raw_result.json", "w") as f:
    json.dump(tom_result, f, indent=2)
print("\nFull time_of_measure result saved to tom_raw_result.json")
