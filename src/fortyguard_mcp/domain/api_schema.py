"""
Universal API facts — the minimum the client needs to function.

Deliberately small. Only properties of the FortyGuard API itself belong here.
Anything that varies by plan, contract or point in time — AOI caps,
entitlements, credit costs, date floors, coverage, forecast horizon, data gaps —
is discovered at runtime or supplied by config, never hardcoded. A Basic-plan
user (10 mi² cap, no premium endpoints) must not be handed a Hackathon plan's
limits.

Everything below degrades safely if the API changes: unknown statuses are
treated as pending, and result shape is detected structurally rather than from
an enum. A new analytic type or status value costs efficiency, never correctness.
"""

from __future__ import annotations

from typing import Any, Final, Literal

DEFAULT_BASE_URL: Final[str] = "https://api.fortyguard.com"

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

STATUS_PATH: Final[str] = "/v1/status/{activity_id}"
# POST-only (GET returns 405), and the key must be in the BODY as `api_key` as
# well as the header every other endpoint accepts alone. See client/http.py.
USAGE_PATH: Final[str] = "/v1/system/fetch-api-key-usage"


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #

# Observed live: "Processing", "Completed". Documented (handbook 7.4):
# "succeeded"/"completed", "failed"/"error". Matching is case-insensitive and
# honours both. Nothing speculative: a wrong guess in the SUCCESS set yields a
# wrong ANSWER rather than a timeout.
_TERMINAL_SUCCESS: Final[frozenset[str]] = frozenset({"completed", "succeeded"})
_TERMINAL_FAILURE: Final[frozenset[str]] = frozenset({"failed", "error"})

TerminalKind = Literal["success", "failure", "pending"]


def classify_status(status: str | None) -> TerminalKind:
    """
    Classify a status string.

    Secondary to `result_has_arrived`, which is what the poll loop turns on;
    this exists for labelling and the failure early-exit.

    Unknown values are PENDING, never success. A new terminal state we have not
    seen costs a wait until the caller's timeout, never a wrong answer.
    """
    if not status:
        return "pending"
    s = status.strip().lower()
    if s in _TERMINAL_SUCCESS:
        return "success"
    if s in _TERMINAL_FAILURE:
        return "failure"
    return "pending"


def result_has_arrived(status_body: Any) -> bool:
    """
    Structural completion test: the work is done when `data.result` is present.

    Verified across 476 recorded polls: `result` appeared only with
    "Completed". Turning on structure rather than a status string means a
    renamed success value keeps working. The status is still reported verbatim.
    """
    if not isinstance(status_body, dict):
        return False
    data = status_body.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("result") is not None


# --------------------------------------------------------------------------- #
# Response envelopes
# --------------------------------------------------------------------------- #

# Two error shapes, so a client needs to read both. Note `message` vs
# `details.message`, and `detail` vs `details`:
#   422 validation -> {"message": "...", "field": "...", "detail": [...]}
#   404 / 401      -> {"details": {"message": "..."}}
def extract_error_message(body: Any) -> str | None:
    """Best-effort human-readable message from either error envelope."""
    if not isinstance(body, dict):
        return None
    msg = body.get("message")
    if isinstance(msg, str) and msg:
        return msg
    details = body.get("details")
    if isinstance(details, dict):
        dm = details.get("message")
        if isinstance(dm, str) and dm:
            return dm
    if isinstance(details, str):
        return details
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    return None


# --------------------------------------------------------------------------- #
# Result shapes
# --------------------------------------------------------------------------- #

ResultShape = Literal["temperature", "analytic", "empty", "unknown"]


def classify_result_shape(result: Any) -> ResultShape:
    """
    Detect a heatmap result's shape structurally rather than by analytic type.

      temperature  tiles carry average/min/max_temperature; stats_data has
                   temperature_stats plus distribution arrays
      analytic     tiles carry a bare `value`; stats_data has
                   analytic_type/units/n_cells/min/max/mean
      empty        `map_data` present with no features; stats_data is
                   {activity_id, n_cells}

    `empty` requires `map_data`: without it the payload is not a heatmap at all
    (an env_params response, or `{}`), and those are `unknown`.

    Structural detection means a new analytic type slots into `analytic` rather
    than falling off a hardcoded enum.
    """
    if not isinstance(result, dict):
        return "unknown"
    map_data = result.get("map_data")
    if not isinstance(map_data, dict):
        return "unknown"
    features = map_data.get("features")
    if not isinstance(features, list):
        return "unknown"
    if not features:
        return "empty"
    # The first READABLE feature, not `features[0]`: one malformed leading
    # feature would classify the whole result `unknown` and drop the min/max
    # columns for every tile.
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        if "average_temperature" in props:
            return "temperature"
        if "value" in props:
            return "analytic"
    return "unknown"


# `standard_deviation` in `temperature_stats` is the SAMPLE standard deviation
# (ddof=1). Code recomputing it must match or it disagrees in the third decimal.


# --------------------------------------------------------------------------- #
# Enum hints for tool descriptions — guidance, NOT validation
# --------------------------------------------------------------------------- #

# Shown in tool descriptions so the model gets the call right first time.
# Deliberately not enforced: the API is the authority, and its rejection
# messages enumerate the valid set.
GRANULARITY_HINT: Final[tuple[int, ...]] = (60, 80, 100)
FILTER_TYPE_HINT: Final[dict[int, str]] = {
    1: "single hour (start_date + start_time)",
    2: "range of hours, same day (start_date + start_time + end_time)",
    3: "single day (start_date only)",
    4: "range of days (start_date + end_date)",
}
ANALYTIC_TYPE_HINT: Final[tuple[str, ...]] = (
    "tcm", "time_of_measure", "exceedance", "persistence",
)

# Undocumented, and easy to get wrong: a UTC-built timestamp lands hours off.
TIME_BASIS_NOTE: Final[str] = (
    "start_time is interpreted as local time at the area of interest, not UTC."
)
