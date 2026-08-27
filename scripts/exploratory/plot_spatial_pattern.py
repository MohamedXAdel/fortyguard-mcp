import os
import json
import requests
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("FORTYGUARD_API_KEY") or os.environ.get("API_KEY")
if not api_key:
    raise SystemExit("set FORTYGUARD_API_KEY")
headers = {"api-key": api_key, "Content-Type": "application/json"}

ACTIVITIES = {
    "downtown_0500": "cd6a5ee4-0a03-44ec-bc99-4ea0b100058e",
    "encanto_0500": "cde39e64-f322-4560-912a-d64838e833ae",
}


def fetch(activity_id):
    r = requests.get(f"https://api.fortyguard.com/v1/status/{activity_id}", headers=headers)
    r.raise_for_status()
    data = r.json()["data"]
    if data.get("status") != "Completed":
        raise RuntimeError(f"activity {activity_id} status={data.get('status')}")
    return data["result"]


def tile_centroids_values(result):
    feats = result["map_data"]["features"]
    xs, ys, vals = [], [], []
    for f in feats:
        coords = f["geometry"]["coordinates"][0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        xs.append(sum(lons) / len(lons))
        ys.append(sum(lats) / len(lats))
        vals.append(f["properties"]["average_temperature"])
    return np.array(xs), np.array(ys), np.array(vals)


def morans_i_like(xs, ys, vals, k=8):
    """Quick k-NN spatial autocorrelation: correlation between each tile's value
    and the mean value of its k nearest neighbors. High positive = clustered/contiguous.
    Near zero = salt-and-pepper / random."""
    n = len(vals)
    pts = np.column_stack([xs, ys])
    neighbor_means = np.zeros(n)
    for i in range(n):
        d = np.sqrt(((pts - pts[i]) ** 2).sum(axis=1))
        idx = np.argsort(d)[1:k + 1]  # exclude self
        neighbor_means[i] = vals[idx].mean()
    if np.std(vals) == 0 or np.std(neighbor_means) == 0:
        return 0.0
    corr = np.corrcoef(vals, neighbor_means)[0, 1]
    return corr


fig, axes = plt.subplots(1, 2, figsize=(16, 7))

for ax, (label, aid) in zip(axes, ACTIVITIES.items()):
    result = fetch(aid)
    xs, ys, vals = tile_centroids_values(result)
    autocorr = morans_i_like(xs, ys, vals)
    sc = ax.scatter(xs, ys, c=vals, cmap="inferno", s=120, marker="s")
    plt.colorbar(sc, ax=ax, label="Temp (°C)")
    ax.set_title(f"{label}\nn={len(vals)} tiles | spread={vals.max()-vals.min():.2f}°C | "
                 f"kNN spatial autocorr={autocorr:.3f}")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_aspect("equal")
    print(f"{label}: n={len(vals)} min={vals.min():.3f} max={vals.max():.3f} "
          f"spread={vals.max()-vals.min():.3f} autocorr={autocorr:.3f}")

plt.tight_layout()
plt.savefig("spatial_pattern_0500.png", dpi=150)
print("\nSaved plot to spatial_pattern_0500.png")
