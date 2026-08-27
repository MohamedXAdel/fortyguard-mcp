import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("FORTYGUARD_API_KEY") or os.environ.get("API_KEY")
if not api_key:
    raise SystemExit("set FORTYGUARD_API_KEY")
headers = {"api-key": api_key, "Content-Type": "application/json"}

# ~2km x 2km tile over downtown Phoenix
POLYGON = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature", "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-112.0850, 33.4400],
                [-112.0600, 33.4400],
                [-112.0600, 33.4600],
                [-112.0850, 33.4600],
                [-112.0850, 33.4400]
            ]]
        }
    }]
}


def submit(payload):
    r = requests.post("https://api.fortyguard.com/v1/heatmap", headers=headers, json=payload)
    print("submit status:", r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()["data"]["activity_id"]


def poll(activity_id, label):
    url = f"https://api.fortyguard.com/v1/status/{activity_id}"
    for i in range(40):
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


print("=== 1) Real heatmap over city tile — checking spread ===")
heatmap_payload = {
    "polygon_aoi": POLYGON,
    "date_time": {"start_date": "2024-07-15", "start_time": "14:00", "filter_type": 1},
    "granularity": 100
}
aid = submit(heatmap_payload)
print("activity_id:", aid)
result = poll(aid, "heatmap")
if result:
    stats = result.get("stats_data") or result.get("stats") or {}
    print("stats_data keys:", list(result.keys()))
    print("stats:", stats)
    # try to find min/max temp directly if present
    for key in ["min_temp", "max_temp", "min_temperature", "max_temperature"]:
        if key in result:
            print(key, "=", result[key])

print("\n\n=== 2) analytic_type test: exceedance ===")
exceedance_payload = {
    "polygon_aoi": POLYGON,
    "date_time": {"start_date": "2024-07-15", "start_time": "06:00", "end_time": "18:00", "filter_type": 2},
    "granularity": 100,
    "analytic_type": "exceedance",
    "threshold": 30,
    "direction": "above"
}
aid2 = submit(exceedance_payload)
print("activity_id:", aid2)
result2 = poll(aid2, "exceedance")
print("exceedance result keys:", list(result2.keys()) if result2 else None)

print("\n\n=== 2b) analytic_type test: time_of_measure ===")
tom_payload = {
    "polygon_aoi": POLYGON,
    "date_time": {"start_date": "2024-07-15", "filter_type": 3},
    "granularity": 100,
    "analytic_type": "time_of_measure"
}
aid3 = submit(tom_payload)
print("activity_id:", aid3)
result3 = poll(aid3, "time_of_measure")
print("time_of_measure result keys:", list(result3.keys()) if result3 else None)
