import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("FORTYGUARD_API_KEY") or os.environ.get("API_KEY")
if not api_key:
    raise SystemExit("set FORTYGUARD_API_KEY")

response = requests.post(
    "https://api.fortyguard.com/v1/satellite",
    headers={"api-key": api_key},
    json={
        "sat": {"latitude": 33.4484, "longitude": -112.0740},
        "date_time": {
            "start_date": "2024-07-15",
            "start_time": "14:00",
            "filter_type": 1
        },
        "granularity": 80
    }
)

print("Status code:", response.status_code)
print("Response body:", response.text)
