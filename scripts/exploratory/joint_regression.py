"""
Joint regression: does impervious surface explain residual variance
independent of the regional lon/lat gradient, or is it confounded?

  temp ~ lon + lat              (baseline, already known: R^2 = 0.6397)
  temp ~ lon + lat + impervious (does adding it lift R^2 and is coef significant?)

Re-fetches impervious % for all 112 Encanto tiles (free MRLC WMS, zero
FortyGuard API credits) since the per-tile values weren't persisted last run.
"""
import json
import numpy as np
import requests
from pyproj import Transformer
import statsmodels.api as sm

HEATMAP_JSON = "encanto_0500_map_data.json"
GEOSERVER = "https://www.mrlc.gov/geoserver"
IMPERVIOUS_WS, IMPERVIOUS_LAYER = "mrlc_display", "NLCD_2021_Impervious_L48"
NODATA = {250, 251, 252, 253, 254, 255}
ALBERS = "EPSG:5070"

to_albers = Transformer.from_crs("EPSG:4326", ALBERS, always_xy=True)


def load_tiles(path):
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


def feature_info(ws, layer, lon, lat, half_m=45.0):
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


lon, lat, temp = load_tiles(HEATMAP_JSON)
print(f"tiles={len(lon)}")

imp = np.full(len(lon), np.nan)
for i in range(len(lon)):
    try:
        v = feature_info(IMPERVIOUS_WS, IMPERVIOUS_LAYER, lon[i], lat[i])
        imp[i] = v if v is not None else np.nan
    except Exception as e:
        print(f"  [{i}] failed: {e}")
    if (i + 1) % 25 == 0:
        print(f"  ...{i+1}/{len(lon)}")

mask = np.isfinite(imp)
print(f"\nusable tiles with impervious data: {mask.sum()} / {len(lon)}")

lon_m, lat_m, temp_m, imp_m = lon[mask], lat[mask], temp[mask], imp[mask]

print("\n=== Baseline: temp ~ lon + lat ===")
X1 = sm.add_constant(np.column_stack([lon_m, lat_m]))
model1 = sm.OLS(temp_m, X1).fit()
print(f"R^2 = {model1.rsquared:.4f}")
print(model1.summary().tables[1])

print("\n=== Joint: temp ~ lon + lat + impervious ===")
X2 = sm.add_constant(np.column_stack([lon_m, lat_m, imp_m]))
model2 = sm.OLS(temp_m, X2).fit()
print(f"R^2 = {model2.rsquared:.4f}")
print(model2.summary().tables[1])

delta_r2 = model2.rsquared - model1.rsquared
imp_coef = model2.params[3]
imp_p = model2.pvalues[3]

print("\n=== Verdict ===")
print(f"R^2: {model1.rsquared:.4f} -> {model2.rsquared:.4f}  (delta = {delta_r2:+.4f})")
print(f"impervious coefficient = {imp_coef:.5f}  p = {imp_p:.2e}")
if delta_r2 > 0.03 and imp_p < 0.05:
    print("-> Impervious effect is INDEPENDENT of the regional gradient. Causal claim holds.")
else:
    print("-> R^2 barely moved / coefficient not significant. Effect is CONFOUNDED with the "
          "regional gradient. Drop back to no-attribution position.")
