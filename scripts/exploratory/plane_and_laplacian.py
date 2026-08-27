import os
import requests
import numpy as np
from scipy.interpolate import griddata
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


def fit_plane_r2(xs, ys, vals):
    A = np.column_stack([xs, ys, np.ones_like(xs)])
    coef, *_ = np.linalg.lstsq(A, vals, rcond=None)
    pred = A @ coef
    ss_res = np.sum((vals - pred) ** 2)
    ss_tot = np.sum((vals - vals.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot
    return coef, r2


def to_local_meters(xs, ys):
    lon0, lat0 = xs.mean(), ys.mean()
    m_per_deg_lon = 111320 * np.cos(np.radians(lat0))
    m_per_deg_lat = 111320
    x_m = (xs - lon0) * m_per_deg_lon
    y_m = (ys - lat0) * m_per_deg_lat
    return x_m, y_m


def median_nn_spacing(x_m, y_m):
    pts = np.column_stack([x_m, y_m])
    n = len(pts)
    nn = np.zeros(n)
    for i in range(n):
        d = np.sqrt(((pts - pts[i]) ** 2).sum(axis=1))
        d[i] = np.inf
        nn[i] = d.min()
    return np.median(nn)


def laplacian(grid):
    lap = np.full_like(grid, np.nan)
    H, W = grid.shape
    for i in range(1, H - 1):
        for j in range(1, W - 1):
            c, n, s, e, w = grid[i, j], grid[i - 1, j], grid[i + 1, j], grid[i, j + 1], grid[i, j - 1]
            if not np.isnan([c, n, s, e, w]).any():
                lap[i, j] = n + s + e + w - 4 * c
    return lap


results = {}
for label, aid in ACTIVITIES.items():
    result = fetch(aid)
    xs, ys, vals = tile_centroids_values(result)

    # 1) Plane fit directly on lon/lat (units don't affect R^2)
    coef, r2 = fit_plane_r2(xs, ys, vals)
    print(f"\n=== {label} ===")
    print(f"n tiles = {len(vals)}")
    print(f"plane fit: a(lon)={coef[0]:.4f} b(lat)={coef[1]:.4f} c={coef[2]:.4f}")
    print(f"R^2 = {r2:.5f}  {'-> PLANE (no info beyond linear ramp)' if r2 > 0.98 else '-> NOT a pure plane, residual structure exists'}")

    # 2) Reconstruct true grid in local projected meters (handles lon/lat shear)
    x_m, y_m = to_local_meters(xs, ys)
    spacing = median_nn_spacing(x_m, y_m)
    print(f"estimated true tile spacing (nearest-neighbor median) = {spacing:.2f} m")

    gx = np.arange(x_m.min(), x_m.max() + spacing, spacing)
    gy = np.arange(y_m.min(), y_m.max() + spacing, spacing)
    GX, GY = np.meshgrid(gx, gy)
    grid = griddata((x_m, y_m), vals, (GX, GY), method="nearest")
    # mask points far from any real sample (outside convex hull of real data) as NaN
    dist_to_nearest = griddata((x_m, y_m), np.zeros(len(vals)), (GX, GY), method="nearest")
    print(f"reconstructed raster shape = {grid.shape}")

    lap = laplacian(grid)
    valid = lap[~np.isnan(lap)]
    if valid.size:
        print(f"Laplacian: n valid={valid.size} mean={valid.mean():.5f} std={valid.std():.5f} "
              f"min={valid.min():.5f} max={valid.max():.5f}")
    else:
        print("Laplacian: no valid interior cells (grid too small)")

    lap_filled = np.nan_to_num(lap, nan=0.0)
    fft2 = np.fft.fftshift(np.fft.fft2(lap_filled))
    mag = np.abs(fft2)
    cy, cx = mag.shape[0] // 2, mag.shape[1] // 2
    mag_masked = mag.copy()
    pad = 1
    mag_masked[max(0, cy - pad):cy + pad + 1, max(0, cx - pad):cx + pad + 1] = 0
    if mag_masked.size and mag_masked.max() > 0:
        peak_idx = np.unravel_index(np.argmax(mag_masked), mag_masked.shape)
        freq_y = (peak_idx[0] - cy) / grid.shape[0]
        freq_x = (peak_idx[1] - cx) / grid.shape[1]
        print(f"Dominant non-DC FFT peak: freq=({freq_y:.4f},{freq_x:.4f})")
        if abs(freq_x) > 1e-9:
            px = (1 / abs(freq_x)) * spacing
            print(f"  -> seam period along x: {1/abs(freq_x):.2f} tiles = {px:.1f} m")
        if abs(freq_y) > 1e-9:
            py = (1 / abs(freq_y)) * spacing
            print(f"  -> seam period along y: {1/abs(freq_y):.2f} tiles = {py:.1f} m")

    results[label] = dict(grid=grid, lap=lap, r2=r2, mag=mag, spacing=spacing)

fig, axes = plt.subplots(len(ACTIVITIES), 3, figsize=(15, 5 * len(ACTIVITIES)))
if len(ACTIVITIES) == 1:
    axes = [axes]
for row, (label, r) in zip(axes, results.items()):
    im0 = row[0].imshow(r["grid"], cmap="inferno", origin="lower")
    row[0].set_title(f"{label}: raw temp (R2={r['r2']:.4f})")
    plt.colorbar(im0, ax=row[0])

    im1 = row[1].imshow(r["lap"], cmap="coolwarm", origin="lower")
    row[1].set_title(f"{label}: Laplacian (spacing={r['spacing']:.0f}m)")
    plt.colorbar(im1, ax=row[1])

    im2 = row[2].imshow(np.log1p(r["mag"]), cmap="viridis", origin="lower")
    row[2].set_title(f"{label}: log FFT magnitude of Laplacian")
    plt.colorbar(im2, ax=row[2])

plt.tight_layout()
plt.savefig("plane_laplacian_analysis.png", dpi=150)
print("\nSaved plot to plane_laplacian_analysis.png")
