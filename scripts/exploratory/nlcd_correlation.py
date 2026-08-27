"""
Does FortyGuard's nighttime temperature residual correlate with land cover?

Input : heatmap GeoJSON (map_data) with per-tile "average_temperature" = temperature
Output: correlation of (temp - best-fit plane) against NLCD impervious % and canopy %

Two paths:
  MODE="wcs"  -> one WCS GetCoverage for the bbox, zonal means. Correct, needs rasterio.
  MODE="point"-> GetFeatureInfo at N sampled tile centroids. Noisier but answers yes/no fast.

  pip install requests numpy scipy pyproj
  pip install rasterio            # only for MODE="wcs"
"""

import json, math, random, sys
import numpy as np
import requests
from pyproj import Transformer
from scipy import stats

# ----------------------------------------------------------------------------
MODE            = "point"          # "wcs" or "point"
HEATMAP_JSON    = "encanto_0500_map_data.json"
N_SAMPLE        = 112              # point mode only (Encanto box has 112 tiles total)
GEOSERVER       = "https://www.mrlc.gov/geoserver"

# Confirmed via GetCapabilities: both layers live in the mrlc_display workspace.
IMPERVIOUS_WS, IMPERVIOUS_LAYER = "mrlc_display", "NLCD_2021_Impervious_L48"
CANOPY_WS,     CANOPY_LAYER     = "mrlc_display", "nlcd_tcc_conus_2021_v2021-4"

NODATA = {250, 251, 252, 253, 254, 255}   # NLCD nodata / fill codes
ALBERS = "EPSG:5070"                       # NLCD native CRS - do stats here, not in 4326
# ----------------------------------------------------------------------------

to_albers = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)


def load_tiles(path):
    """Return centroids (lon, lat) and temperature per tile."""
    gj = json.load(open(path))
    feats = gj["features"] if "features" in gj else gj["map_data"]["features"]
    lons, lats, temps = [], [], []
    for f in feats:
        v = f["properties"].get("average_temperature")
        if v is None:
            continue
        ring = f["geometry"]["coordinates"][0]
        xs = [p[0] for p in ring[:-1]]
        ys = [p[1] for p in ring[:-1]]
        lons.append(sum(xs) / len(xs))
        lats.append(sum(ys) / len(ys))
        temps.append(float(v))
    return np.array(lons), np.array(lats), np.array(temps)


def plane_residual(lon, lat, temp):
    """Remove the regional linear gradient. What's left is the local signal - if any."""
    A = np.column_stack([lon, lat, np.ones_like(lon)])
    coef, *_ = np.linalg.lstsq(A, temp, rcond=None)
    fit = A @ coef
    ss_res = float(np.sum((temp - fit) ** 2))
    ss_tot = float(np.sum((temp - temp.mean()) ** 2))
    return temp - fit, 1 - ss_res / ss_tot if ss_tot else float("nan")


# --- point mode: WMS GetFeatureInfo -----------------------------------------
def feature_info(ws, layer, lon, lat, half_m=45.0):
    """
    GetFeatureInfo needs a fake map request. Build a tiny bbox in Albers around
    the point, 3x3 pixels, and ask for the centre pixel.
    """
    x, y = to_albers.transform(lon, lat)
    bbox = f"{x-half_m},{y-half_m},{x+half_m},{y+half_m}"
    p = {
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
        "layers": layer, "query_layers": layer,
        "srs": ALBERS, "bbox": bbox,
        "width": 3, "height": 3, "x": 1, "y": 1,
        "info_format": "application/json", "feature_count": 1,
    }
    r = requests.get(f"{GEOSERVER}/{ws}/wms", params=p, timeout=30)
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None
    for v in feats[0].get("properties", {}).values():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        return None if int(v) in NODATA else v
    return None


def run_point(lon, lat, resid):
    idx = random.sample(range(len(lon)), min(N_SAMPLE, len(lon)))
    out = {"impervious": [], "canopy": [], "resid": []}
    for n, i in enumerate(idx, 1):
        try:
            imp = feature_info(IMPERVIOUS_WS, IMPERVIOUS_LAYER, lon[i], lat[i])
            can = feature_info(CANOPY_WS, CANOPY_LAYER, lon[i], lat[i])
        except Exception as e:
            print(f"  [{n}] request failed: {e}", file=sys.stderr)
            continue
        if imp is None and can is None:
            continue
        out["impervious"].append(imp if imp is not None else np.nan)
        out["canopy"].append(can if can is not None else np.nan)
        out["resid"].append(resid[i])
        if n % 25 == 0:
            print(f"  ...{n}/{len(idx)}")
    return {k: np.array(v, dtype=float) for k, v in out.items()}


# --- wcs mode: one raster, zonal means --------------------------------------
def run_wcs(lon, lat, resid):
    import rasterio  # noqa: F401
    raise NotImplementedError(
        "Fetch coverageId and axis labels from\n"
        f"  {GEOSERVER}/{IMPERVIOUS_WS}/wcs?service=WCS&version=2.0.1&request=GetCapabilities\n"
        "then GetCoverage with subset=X(xmin,xmax)&subset=Y(ymin,ymax) in EPSG:5070,\n"
        "format=image/tiff;application=geotiff, and take a masked mean per tile polygon."
    )


def report(name, x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10:
        print(f"{name:<12} n={m.sum()} - too few points")
        return
    r, p = stats.pearsonr(x[m], y[m])
    rho, _ = stats.spearmanr(x[m], y[m])
    verdict = "SIGNAL" if (p < 0.05 and abs(r) > 0.3) else "no signal"
    print(f"{name:<12} n={m.sum():<4} r={r:+.3f}  R2={r*r:.3f}  "
          f"rho={rho:+.3f}  p={p:.2e}   -> {verdict}")


if __name__ == "__main__":
    lon, lat, temp = load_tiles(HEATMAP_JSON)
    resid, r2 = plane_residual(lon, lat, temp)
    print(f"tiles={len(lon)}  plane R2={r2:.4f}  "
          f"residual sd={resid.std():.4f} C  range={np.ptp(resid):.4f} C\n")

    d = run_point(lon, lat, resid) if MODE == "point" else run_wcs(lon, lat, resid)

    print()
    for key in ("impervious", "canopy"):
        arr = d[key]
        finite = arr[np.isfinite(arr)]
        if finite.size:
            print(f"{key:<12} predictor spread: n={finite.size} min={finite.min():.1f} "
                  f"max={finite.max():.1f} mean={finite.mean():.1f} sd={finite.std():.1f}")
    print()
    report("impervious", d["impervious"], d["resid"])
    report("canopy",     d["canopy"],     d["resid"])
    print("\nSIGNAL on either -> model resolves urban form; explain rankings causally.")
    print("Neither          -> curvature is interpolation artifact; report rankings,")
    print("                    do not claim a land-cover cause.")
