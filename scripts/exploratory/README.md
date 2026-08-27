# Exploratory scripts

Ported from the original `Trials/` folder. These are the provenance for the
empirical claims in `MEASUREMENTS.md` and for the Night Shift agent's thesis.
They are **not** part of the server, are not covered by its test suite, and are
excluded from the published sdist - they import numpy, pyproj, rasterio and
requests, none of which this package declares.

They read `FORTYGUARD_API_KEY`, the same variable the server uses. They used to
read a separate `API_KEY`, which meant running them required keeping a second
copy of the same live credential in a second file.

- `test_heatmap_spread.py`, `test_2x2_diagnostic.py` — diurnal spread measurement
- `plot_spatial_pattern.py`, `plane_and_laplacian.py` — spatial structure
- `nlcd_correlation.py`, `joint_regression.py` — land-cover attribution
- `test_env_params_*.py`, `test_satellite.py` — early endpoint probes

`downtown_0500_map_data.json` / `encanto_0500_map_data.json` are genuine API
results, re-verified: a fresh call reproduced the Encanto file byte-for-byte.
Note they contain only `data.result`, not the full wire envelope, which is why
they are reference data rather than test fixtures.
