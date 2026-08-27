# Measured API behaviour

Every value here was measured against the live FortyGuard API during the build,
not taken from documentation. Each one replaced an assumption, and several
reversed a decision that had already been made.

The evidence is in the repository: 50 recorded request/response exchanges in
`tests/fixtures/` (477 poll responses in total) and the campaign reports in
`tests/a0_reports/`. The whole test suite replays those fixtures, so every claim
below is re-checkable offline with no API key and no credits.

Measured on plan `Hackathon`, Aug 2026. **Costs, limits and entitlements vary by
plan** — this is what one account saw, not a price list. The server reads your
real plan at runtime with `get_credit_usage` and hardcodes none of it.

| Campaign tier | Credits spent |
|---|---|
| Tier 0 — foundation | ~12,660 |
| Tier 1 — entitlement | 31,600 |
| Tier 2 — schema | 62,140 |
| Tier 4 — error taxonomy | 15,560 *(not free — 4 invalid requests succeeded and charged)* |
| T3.1 — determinism | 4,220 |
| **Tiers total** | **126,180** |

Exploratory calls made outside the numbered tiers bring total campaign spend to
roughly 206,000 credits.

---

## The constraint layer

Every enum below was extracted from the API's own `422` messages, which
enumerate their valid set — including all 17 `env_params.analysis` values.

```python
GRANULARITIES  = (60, 80, 100)      # API: "Input should be 60, 80 or 100"
FILTER_TYPES   = (1, 2, 3, 4)       # API: "Input should be 1, 2, 3 or 4"  <- no 5
ANALYTIC_TYPES = ("tcm", "time_of_measure", "exceedance", "persistence")
DATE_FLOOR     = 2021-01-01         # for this key; the generic docs say 2019
FORECAST_HOURS = ~10                # works at +10 h; empty by +12 h
TIME_BASIS     = "AOI-local"        # start_time is LOCAL, not UTC
STATUS_PENDING = "Processing"
STATUS_DONE    = "Completed"
GRANULARITY_DEFAULT = 100           # granularity is OPTIONAL
MIN_AOI_EDGE_M = ~250-300           # below this: 0 tiles at full price
```

These are carried as **hints in tool descriptions, not as validation.** The API
is the authority on what it accepts, and its rejection messages are better than
anything a local copy could say when it drifts.

---

## Cost

**Flat per call, per endpoint. Independent of area and of granularity.**

| Endpoint | Credits/call | Evidence |
|---|---|---|
| `/v1/heatmap` | 4,220 | 26 calls, exactly 4,220 every time |
| `/v1/env_params` | 2,900 | 13 calls |
| `/v1/satellite` | 14,400 | 2 calls |
| `/v1/streetview` | 8,600 | 2 calls |
| `/v1/heat_intelligence` | 8,600 | 1 call |

Proven independent of tile count (0 → 527 tiles, same price), granularity
(60/80/100, same price), `filter_type` (1–4) and `analytic_type`.

Polling is free: calls taking 1 poll and 121 polls were charged identically.
Credits attach to the submitted task once, on success.

---

## Duration

| Endpoint | Observed |
|---|---|
| `/v1/env_params` | ~6 s |
| `/v1/satellite`, `/v1/streetview` | 7–15 s |
| `/v1/heatmap` | 21–38 s |
| `/v1/heat_intelligence` | 189–395 s (14–121 polls, n=2) |

Even the smallest possible heatmap took 36 s. This is why `heat_intelligence`
is never waited on inline, and why every wait is bounded and returns the
`activity_id`.

---

## Determinism

Re-ran an identical historical request against a result archived days earlier:
**112/112 tiles identical, statistics identical, zero drift.**

This is what makes the local archive a sound cache — a stored result *is* the
same answer. The claim is deliberately scoped to **historical dates**: it says
nothing about a date not yet past when it was stored, nor about `streetview`,
whose request body carries no date at all. The server labels which of those
three cases a cache hit falls into rather than giving a blanket assurance.

---

## Result schemas

### Heatmap — three distinct shapes

| Variant | `properties` | `stats_data` |
|---|---|---|
| default, `tcm` | `tile_id`, `average_temperature`, `min_temperature`, `max_temperature` | `temperature_stats{minimum,maximum,mean,standard_deviation}` + 3 distribution arrays |
| `time_of_measure`, `exceedance`, `persistence` | `tile_id`, `value` | `activity_id, analytic_type, units, n_cells, min, max, mean` |
| empty result | no features | `activity_id, n_cells: 0` — **no `temperature_stats` at all** |

`standard_deviation` is the **sample** std (ddof=1) — code that recomputes it
must match, or it disagrees in the third decimal.

`exceedance` and `persistence` both report `units: "hour"`.

`tcm` is undocumented and appears to be a no-op: it returned byte-identical
statistics to the same request without it.

### Granularity is the tile edge in metres

| Granularity | Tiles over a 1.4 × 1.0 km box | Measured tile |
|---|---|---|
| 60 | 299 | 60.9 × 60.4 m |
| 80 | 170 | 81.7 × 79.3 m |
| 100 | 112 | 100.2 × 97.6 m |

Worth knowing against marketing describing ~20 m resolution: **60 m is the
finest available.**

### env_params

Omitting `analysis` returns all 15 parameters plus
`solar_irradiance{clear_sky{ghi,dni,dhi}}`. Each location carries `lat`, `lon`,
`elevation`, `temperature`, `parameters`, `solar_irradiance`.

### satellite

`coordinates`, **`original_image` and `orignal_image`** (both keys present, one
misspelled), `image_year`, `segmentation{request_id, processing_time_seconds,
image_dimensions, mode, segments, image_legend, image_content}`.

### streetview

`back_view: false` → `coordinates`, `front`.
`back_view: true` → `coordinates`, `front`, **`back`** — undocumented but real.
Both carry `original_image`, `segments`, `image_legend`, `segmented_image`,
`image_date`.

### heat_intelligence

`download_link` only — the API never sends the document itself. The link is a
short-lived pre-signed URL and should be treated as being **as sensitive as the
API key**, so this server downloads the file while the link is valid and never
returns, logs or archives the URL.

---

## Error taxonomy

The API validates **syntax strictly and semantics not at all**. That split is
the single most expensive thing to know about it.

### Rejected at submit — free

| Case | Status | Message |
|---|---|---|
| future date | 400 | `Field 'date_time.start_date' (2027-01-01) is in the future.` |
| unclosed ring | 400 | `Polygon ring is not closed: the first and last positions must be identical.` |
| `[lat,lon]` transposed | 400 | `Latitude -112.095 is out of bounds; must be between -90.0 and 90.0.` |
| empty `features` | 400 | `FeatureCollection 'features' must be a non-empty array.` |
| bad granularity | 422 | `Input should be 60, 80 or 100` |
| bad filter_type | 422 | `Input should be 1, 2, 3 or 4` |
| bad analytic_type | 422 | `Input should be 'tcm', 'time_of_measure', 'exceedance' or 'persistence'` |
| missing polygon | 422 | `Field 'polygon_aoi' is required.` |
| bad parameter name | 422 | enumerates all 17 valid values |
| invalid API key | 401 | `{"details":{"message":"Invalid or unknown API key."}}` |

These messages are good, and the server returns them **verbatim** rather than
translating them.

### Accepted, charged, and empty

| Case | Result | Cost |
|---|---|---|
| London (outside US coverage) | `Completed`, 0 tiles | 4,220 |
| AOI ~220 m edge | `Completed`, 0 tiles | 4,220 |
| beyond the forecast edge (~+12 h) | `Completed`, 0 tiles | 4,220 |
| any time on 2026-08-22 (archive gap) | `Completed`, 0 tiles | 4,220 |
| bare `Polygon` instead of `FeatureCollection` | works, 112 tiles | 4,220 |
| `granularity` omitted | works, defaults to 100 | 4,220 |

The server states explicitly when a completed analysis returned zero tiles,
because the status says success and the charge is full.

### Accepted, with no terminal state

| Case | Observed |
|---|---|
| AOI ≈ 30,000 km² | still `Processing` after 467 s / 150 polls |
| date 2020-12-31 (below the floor) | still `Processing` after 10+ min |

**`Failed` was never observed across roughly 100 live calls.** Invalid work
either rejects at submit, succeeds emptily at full price, or hangs. Terminal
detection therefore cannot be status-driven — every wait in this server is
bounded by a timeout and returns the `activity_id` so the job stays collectable.

---

## Time basis and forecast horizon

An earlier probe concluded "forecasting does not work." That was wrong, and the
cause is itself the finding.

**`start_time` is interpreted as time local to the AOI, not UTC.** The failed
probe built its timestamp in UTC; `21:00` UTC was read as `21:00` *Phoenix
local*, i.e. +13 h from then — past the edge.

Measured from 2026-08-23 08:07 Phoenix local:

| Requested (local) | Offset | Tiles |
|---|---|---|
| 10:00 | +2 h | 112, mean 40.64 °C |
| 14:00 | +6 h | 112, mean 42.51 °C |
| 18:00 | +10 h | 112, mean 36.13 °C |
| 20:00 | +12 h | **0** |

Forecast reaches at least +10 h and is empty by +12 h. The horizon is **not**
hardcoded: an over-horizon call costs 4,220 and returns nothing, but the exact
edge moves with a slightly stale "now", so the server reports rather than gates.

**Archive gap: 2026-08-22.** Every hour probed on that date returned 0 tiles,
while 2026-08-21 and 2026-08-23 both returned full results. A whole-day hole,
not a horizon effect.

---

## What this changed in the build

Two of these measurements reversed decisions that had already been made, which
is the main reason the campaign was worth its cost.

1. **Bounded polling is mandatory, not a nicety.** No `Failed` status exists in
   practice, so a status-driven client waits forever on out-of-range work.
2. **The archive is a sound cache.** Determinism was assumed to need a time
   component in the cache key; measuring it showed none was needed for
   historical dates, and scoped the claim honestly for the dates it does not
   cover.
3. **Three statistics shapes, not two.** The empty-result shape has no
   `temperature_stats` at all, so any code indexing it directly breaks on a
   result that was already paid for.
4. **`start_time` handling.** AOI-local, not UTC — undocumented, and the single
   easiest way to silently get an answer about the wrong hour.
5. **Local validation was reconsidered and rejected.** The first conclusion here
   was that the client should enforce US bounds, a minimum AOI edge, a maximum
   area and a date floor, since the API charges or hangs for each. That was
   dropped: those limits **vary by plan**, so a hardcoded cap is wrong for most
   accounts, and a Basic-plan user would be blocked from calls a Premium user
   can make. The server reports what it measures and lets the API decide. The
   same reasoning removed a planned `estimate_cost` tool — a lookup table built
   from one account's price list would confidently mislead every other account.

---

## Still open

| ID | Item |
|---|---|
| T3.4 | Rate limits — concurrency ramp not run |
| T3.6 | Exact AOI maximum (over-cap hangs rather than rejecting, so a binary search is expensive) |
| T3.7 | Alaska / Hawaii coverage |
| — | Exact minimum AOI edge, somewhere between 220 m and 300 m |
| U11 | Long-term result retention on FortyGuard's side |
