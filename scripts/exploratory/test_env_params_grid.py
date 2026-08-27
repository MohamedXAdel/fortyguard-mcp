import os
import time
import math
import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("FORTYGUARD_API_KEY") or os.environ.get("API_KEY")
if not api_key:
    raise SystemExit("set FORTYGUARD_API_KEY")
headers = {"api-key": api_key, "Content-Type": "application/json"}

DATE_TIME = {"start_date": "2024-07-15", "start_time": "14:00", "filter_type": 1}
SPACINGS_M = [20, 50, 200, 1000]

SITES = {
    "park_to_parking": {
        "base": (33.4720, -112.0900),   # Encanto Park edge (green cover)
        "bearing_deg": 90,               # moving east toward developed/parking area
        "desc": "Park edge -> parking lot direction (Encanto Park, Phoenix)",
    },
    "river_crossing": {
        "base": (33.4320, -111.9500),    # Salt River north bank near Tempe Town Lake
        "bearing_deg": 180,               # moving south across the river
        "desc": "North bank -> south bank (Salt River, Tempe)",
    },
}


def offset_point(lat, lon, bearing_deg, distance_m):
    R = 6371000
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(distance_m / R) +
                      math.cos(lat1) * math.sin(distance_m / R) * math.cos(brng))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(distance_m / R) * math.cos(lat1),
        math.cos(distance_m / R) - math.sin(lat1) * math.sin(lat2)
    )
    return math.degrees(lat2), math.degrees(lon2)


def submit(lat, lon):
    payload = {
        "latitude": lat, "longitude": lon, "temperature": 38.0,
        "date_time": DATE_TIME,
        "analysis": ["heat_index_celsius", "relative_humidity_percent"]
    }
    r = requests.post("https://api.fortyguard.com/v1/env_params", headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["data"]["activity_id"]


def poll(activity_id):
    url = f"https://api.fortyguard.com/v1/status/{activity_id}"
    for _ in range(30):
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()["data"]
        if data.get("status") == "Completed":
            return data["result"]["locations"][0]["parameters"]
        if data.get("status") == "Failed":
            return None
        time.sleep(2)
    return None


rows = []  # (site, spacing, lat, lon, activity_id)
jobs = []

for site_key, site in SITES.items():
    lat0, lon0 = site["base"]
    # base point (0m)
    aid = submit(lat0, lon0)
    jobs.append((site_key, 0, lat0, lon0, aid))
    for m in SPACINGS_M:
        lat, lon = offset_point(lat0, lon0, site["bearing_deg"], m)
        aid = submit(lat, lon)
        jobs.append((site_key, m, lat, lon, aid))

print(f"Submitted {len(jobs)} requests. Polling...\n")

results = []
for site_key, m, lat, lon, aid in jobs:
    params = poll(aid)
    temp = params["heat_index_celsius"][0] if params else None
    rh = params["relative_humidity_percent"][0] if params else None
    results.append((site_key, m, lat, lon, temp, rh))
    print(f"{site_key:16s} +{m:4d}m  ({lat:.5f},{lon:.5f})  heat_index={temp}  RH={rh}")

print("\n=== Summary per site ===")
for site_key, site in SITES.items():
    print(f"\n{site_key} — {site['desc']}")
    site_rows = [r for r in results if r[0] == site_key]
    for _, m, lat, lon, temp, rh in site_rows:
        print(f"  +{m:4d}m: heat_index={temp}  RH={rh}")
