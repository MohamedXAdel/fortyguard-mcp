import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("FORTYGUARD_API_KEY") or os.environ.get("API_KEY")
if not api_key:
    raise SystemExit("set FORTYGUARD_API_KEY")
headers = {"api-key": api_key, "Content-Type": "application/json"}

# Two points ~200m apart (0.0018 deg latitude ~= 200m)
POINTS = [
    {"label": "Point A", "latitude": 33.4484, "longitude": -112.0740},
    {"label": "Point B", "latitude": 33.4502, "longitude": -112.0740},
]

DATE_TIME = {
    "start_date": "2024-07-15",
    "start_time": "14:00",
    "filter_type": 1
}


def submit(point):
    payload = {
        "latitude": point["latitude"],
        "longitude": point["longitude"],
        "temperature": 38.0,
        "date_time": DATE_TIME,
        "analysis": ["relative_humidity_percent"]
    }
    r = requests.post(
        "https://api.fortyguard.com/v1/env_params",
        headers=headers,
        json=payload
    )
    r.raise_for_status()
    return r.json()["data"]["activity_id"]


def poll(activity_id, label):
    url = f"https://api.fortyguard.com/v1/status/{activity_id}"
    for attempt in range(30):
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()["data"]
        status = data.get("status")
        print(f"[{label}] poll {attempt+1}: status={status}")
        if status == "Completed":
            return data["result"]
        if status == "Failed":
            raise RuntimeError(f"{label} task failed: {data}")
        time.sleep(3)
    raise TimeoutError(f"{label} did not complete in time")


results = {}
activity_ids = {}

for point in POINTS:
    aid = submit(point)
    activity_ids[point["label"]] = aid
    print(f"{point['label']} activity_id: {aid}")

for point in POINTS:
    label = point["label"]
    result = poll(activity_ids[label], label)
    rh = result["locations"][0]["parameters"]["relative_humidity_percent"]
    results[label] = rh
    print(f"{label} ({point['latitude']}, {point['longitude']}) RH: {rh}")

print("\n=== Comparison ===")
print("Point A RH:", results["Point A"])
print("Point B RH:", results["Point B"])
if results["Point A"] == results["Point B"]:
    print("RH is IDENTICAL between the two points.")
else:
    print("RH DIFFERS between the two points.")
