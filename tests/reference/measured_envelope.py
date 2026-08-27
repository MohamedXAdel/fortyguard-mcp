"""
GENERATED TEST REFERENCE - DO NOT EDIT BY HAND - NOT PRODUCTION CODE

Generated from verified_envelope.json by scripts/gen_constraints.py.

TEST REFERENCE ONLY. Measured against a single API key on the Hackathon
plan. Plan-specific values - AOI cap, entitlements, credit costs, date
floor, coverage, forecast horizon, data gaps - do NOT generalise to other
accounts and must never be imported by production code.

The enums were parsed out of the API's own 422 validation messages, which
enumerate their valid sets - not from the documentation, which
contradicted itself three ways on filter_type.

To change anything here, change the campaign and regenerate.
"""

from __future__ import annotations

from datetime import date
from typing import Final

ENVELOPE_SHA256: Final[str] = "c3f3a5eb52b286dc926779807079cd78e39992b191ce6d16f1acc2481964184b"
GENERATED_AT: Final[str] = "2026-08-23T15:35:53.259712+00:00"
FIXTURE_COUNT: Final[int] = 49


# GRANULARITIES [measured]
#   Field 'granularity' is invalid: Input should be 60, 80 or 100
#   fixture: v1_heatmap/e_granularity_50.json
GRANULARITIES: Final[tuple] = (60, 80, 100)

# FILTER_TYPES [measured]
#   Field 'date_time.filter_type' is invalid: Input should be 1, 2, 3 or 4
#   fixture: v1_heatmap/e_filter_type_9.json
FILTER_TYPES: Final[tuple] = (1, 2, 3, 4)

# ANALYTIC_TYPES [measured]
#   Field 'analytic_type' is invalid: Input should be 'tcm',
#   'time_of_measure', 'exceedance' or 'persistence'
#   fixture: v1_heatmap/e_bad_analytic_type.json
ANALYTIC_TYPES: Final[tuple] = ('tcm', 'time_of_measure', 'exceedance', 'persistence')

# ENV_PARAMETERS [measured]
#   Field 'analysis.0' is invalid: Input should be 'heat_index_celsius',
#   'apparent_temperature_celsius', 'wet_bulb_temperature_celsius',
#   'relative_humidity_percent', 'precipitation_mm', 'cloud_cover_octas',
#   'air_quality:idx', 'air_quality_no2:idx', 'air_quality_o3:idx',
#   'air_quality_pm2p5:idx', 'air_quality_pm10:idx', 'air_quality_so2:idx',
#   'aqi_us_co', 'methane_ppb', 'co2_ppm', 'elevation' or 'solar_irradiance'
#   fixture: v1_env_params/e_env_bad_param_name.json
ENV_PARAMETERS: Final[tuple[str, ...]] = (
    "heat_index_celsius",
    "apparent_temperature_celsius",
    "wet_bulb_temperature_celsius",
    "relative_humidity_percent",
    "precipitation_mm",
    "cloud_cover_octas",
    "air_quality:idx",
    "air_quality_no2:idx",
    "air_quality_o3:idx",
    "air_quality_pm2p5:idx",
    "air_quality_pm10:idx",
    "air_quality_so2:idx",
    "aqi_us_co",
    "methane_ppb",
    "co2_ppm",
    "elevation",
    "solar_irradiance",
)

# STATUSES [observed]
#   observed across the campaign; 'Failed' never seen in ~100 calls
STATUS_OBSERVED: Final[tuple[str, ...]] = ('Completed', 'Processing')
STATUS_TERMINAL_SUCCESS: Final[frozenset[str]] = frozenset({'Completed'})
STATUS_PENDING: Final[frozenset[str]] = frozenset({'Processing'})
# No 'Failed' status was ever observed. Terminal detection must therefore
# be timeout-driven, never solely status-driven.
STATUS_TERMINAL_FAILURE: Final[frozenset[str]] = frozenset({"Failed", "Error"})

# DATE_FLOOR [decided]
#   Handbook + public site FAQ + confirmed key behaviour. API docs' 2019-01-01
#   is stale. Pre-floor requests are NOT rejected - they were still Processing
#   after 10+ minutes, with no terminal state observed within the polling
#   window, so this must be enforced client-side.
DATE_FLOOR: Final[date] = date(2021, 1, 1)

# AOI_MAX_KM2 [decided]
#   Documented 50 mi2 = 129.5 km2. Not enforced server-side: an over-cap AOI
#   is accepted and was still Processing after 467 s / 150 polls, with no
#   terminal state observed within the polling window. Measuring the true
#   boundary costs ~15k credits and yields little, so the documented figure is
#   enforced client-side.
AOI_MAX_KM2: Final[float] = 130

# MIN_AOI_EDGE_M [measured]
#   220 m AOI -> Completed with 0 tiles at full price (4,220); 300 m -> 9
#   tiles. True threshold lies between.
#   fixture: v1_heatmap/t0_4_smallest_heatmap.json
MIN_AOI_EDGE_M: Final[float] = 300

# FORECAST_HOURS [measured]
#   +2h/+6h/+10h returned full results; +12h returned 0 tiles. start_time is
#   AOI-LOCAL, not UTC. Edge moves with data freshness, so warn rather than
#   reject beyond this.
#   fixture: tests/a0_reports/forecast_horizon.json
FORECAST_HOURS: Final[int] = 10

# TIME_BASIS [measured]
#   A UTC-built timestamp was read as local, putting a +6h probe at +13h and
#   returning empty. Undocumented.
TIME_BASIS: Final[str] = "aoi-local"

# DETERMINISTIC [measured]
#   Identical request re-issued days later returned byte-identical values
#   across 112 tiles including stats. Cache keys need no time component.
#   fixture: tests/a0_reports/T3.1_determinism.json
RESULTS_DETERMINISTIC: Final[bool] = True

# KNOWN_EMPTY_DATES [measured]
#   Every hour probed on this date returns 0 tiles while 2026-08-21 and
#   2026-08-23 return full results. Archive gap. Empty results still cost
#   4,220, so warn before spending.
KNOWN_EMPTY_DATES: Final[frozenset[date]] = frozenset({
    date(2026, 8, 22),
})

# COST_PER_CALL [observed]
#   credit deltas bracketed per endpoint via activity_breakdown; flat per
#   call, independent of area and granularity
COST_PER_CALL: Final[dict[str, int]] = {
    "/v1/env_params": 2900,
    "/v1/heat_intelligence": 8600,
    "/v1/heatmap": 4220,
    "/v1/satellite": 14400,
    "/v1/streetview": 8600,
}

# DURATION_S [observed]
#   wall-clock to terminal status across recorded fixtures
DURATION_S: Final[dict[str, dict[str, float]]] = {
    "/v1/env_params": {"min": 5.013, "max": 6.019, "n": 2},
    "/v1/heat_intelligence": {"min": 395.256, "max": 395.256, "n": 1},
    "/v1/heatmap": {"min": 19.644, "max": 37.814, "n": 30},
    "/v1/satellite": {"min": 7.297, "max": 7.297, "n": 1},
    "/v1/streetview": {"min": 7.136, "max": 14.986, "n": 2},
}

# Granularity is optional in the API and defaults to 100 despite the docs
# listing it as required.
GRANULARITY_DEFAULT: Final[int] = 100

