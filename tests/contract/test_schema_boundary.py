"""
Contract: the production package stays free of account-specific values.

The load-bearing test here is `test_production_does_not_import_measured_data`.
An earlier draft baked one API key's Hackathon-plan limits — AOI cap, premium
entitlements, credit costs, a specific empty date — into the shipped package.
A Basic-plan user (10 mi2 cap, no premium endpoints) would have received limits
that did not apply to them and tools that could never work.

So the boundary is:
  src/fortyguard_mcp/domain/api_schema.py   universal API behaviour, hand-written
  tests/reference/measured_envelope.py      one account's measurements, generated

    python -m pytest tests/contract -v
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENVELOPE = REPO / "verified_envelope.json"
TARGET = REPO / "tests" / "reference" / "measured_envelope.py"
SRC = REPO / "src" / "fortyguard_mcp"


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #

def test_production_does_not_import_measured_data() -> None:
    """Production must never depend on one account's measurements."""
    offenders = []
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "measured_envelope" in text or "tests.reference" in text:
            offenders.append(str(p.relative_to(REPO)))
    assert not offenders, (
        f"production imports account-specific test data: {offenders}")


def test_no_account_specific_constants_in_production() -> None:
    """
    Values that vary by plan or moment must not appear as production constants.
    Named here so a future edit reintroducing them fails loudly.
    """
    banned = ["AOI_MAX_KM2", "MIN_AOI_EDGE_M", "KNOWN_EMPTY_DATES",
              "FORECAST_HOURS", "COST_PER_CALL", "DATE_FLOOR"]
    offenders = []
    for p in SRC.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for name in banned:
            # Assignment, not a mention in prose or a comment.
            if f"\n{name}" in text or f"\n{name}:" in text:
                offenders.append(f"{p.relative_to(REPO)}:{name}")
    assert not offenders, (
        f"account-specific constants defined in production: {offenders}. "
        "These vary by plan and must be discovered at runtime.")


# --------------------------------------------------------------------------- #
# Universal schema behaves, and degrades safely
# --------------------------------------------------------------------------- #

def test_status_classification_is_case_insensitive() -> None:
    """The docs say 'completed'; the API returns 'Completed'. Accept both."""
    from fortyguard_mcp.domain.api_schema import classify_status

    # Observed live, plus the vendor-documented spellings.
    for s in ("Completed", "completed", "COMPLETED", "succeeded", "Succeeded"):
        assert classify_status(s) == "success", s
    for s in ("Processing", "processing"):
        assert classify_status(s) == "pending", s
    for s in ("Failed", "failed", "Error", "error"):
        assert classify_status(s) == "failure", s


def test_vocabulary_contains_nothing_invented() -> None:
    """
    Only values that were observed live or documented by the vendor belong in
    the terminal sets. Guessing at extra SUCCESS values is the one direction
    where being wrong returns a wrong ANSWER instead of a timeout, so plausible
    inventions like 'success', 'done' and 'cancelled' are deliberately absent.
    """
    from fortyguard_mcp.domain import api_schema as s

    assert frozenset({"completed", "succeeded"}) == s._TERMINAL_SUCCESS
    assert frozenset({"failed", "error"}) == s._TERMINAL_FAILURE
    for invented in ("success", "done", "cancelled", "canceled", "ok"):
        assert s.classify_status(invented) == "pending", (
            f"{invented!r} was never observed or documented; it must not be "
            f"treated as terminal")


def test_unknown_status_is_pending_never_success() -> None:
    """
    The safety property. A status we have never seen must never be read as
    success — worst case we poll until the caller's timeout.
    """
    from fortyguard_mcp.domain.api_schema import classify_status

    for s in ("Queued", "Rehydrating", "", None, "SomethingNew"):
        assert classify_status(s) == "pending", s


def test_result_shape_detected_structurally() -> None:
    """A new analytic type must classify as `analytic`, not fall off an enum."""
    from fortyguard_mcp.domain.api_schema import classify_result_shape

    temp = {"map_data": {"features": [{"properties": {
        "tile_id": 0, "average_temperature": 30.0}}]},
        "stats_data": {"temperature_stats": {}}}
    analytic = {"map_data": {"features": [{"properties": {
        "tile_id": 0, "value": 17.0}}]},
        "stats_data": {"analytic_type": "a_type_invented_next_year"}}
    empty = {"map_data": {"features": []},
             "stats_data": {"activity_id": "x", "n_cells": 0}}

    assert classify_result_shape(temp) == "temperature"
    assert classify_result_shape(analytic) == "analytic"
    assert classify_result_shape(empty) == "empty"
    assert classify_result_shape(None) == "unknown"


def test_both_error_envelopes_parse() -> None:
    """422 uses `message`; 404/401 use `details.message`."""
    from fortyguard_mcp.domain.api_schema import extract_error_message

    validation = {"error": True, "status_code": 422,
                  "message": "Field 'granularity' is invalid: "
                             "Input should be 60, 80 or 100",
                  "field": "granularity"}
    lookup = {"error": True, "status_code": 404,
              "details": {"message": "No activity found for the provided activity_id."}}

    assert "60, 80 or 100" in extract_error_message(validation)
    assert "No activity found" in extract_error_message(lookup)
    assert extract_error_message({}) is None


def test_hints_are_hints_not_validators() -> None:
    """
    Enum hints exist to guide the model in tool descriptions. They must not be
    exported as anything a validator would enforce, since the API is the
    authority and its rejection messages are better than ours.
    """
    from fortyguard_mcp.domain import api_schema

    assert api_schema.GRANULARITY_HINT == (60, 80, 100)
    assert set(api_schema.FILTER_TYPE_HINT) == {1, 2, 3, 4}
    for name in dir(api_schema):
        assert not name.startswith("validate_"), (
            f"api_schema exposes {name}; validation belongs to the API")


# --------------------------------------------------------------------------- #
# The generated reference stays honest
# --------------------------------------------------------------------------- #

def test_reference_is_generated_and_labelled() -> None:
    assert TARGET.exists(), "run scripts/gen_constraints.py"
    head = TARGET.read_text(encoding="utf-8")[:900]
    assert "NOT PRODUCTION CODE" in head
    assert "TEST REFERENCE" in head


def test_reference_matches_envelope() -> None:
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen_constraints.py"), "--check"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


def test_every_value_carries_provenance() -> None:
    envelope = json.loads(ENVELOPE.read_text(encoding="utf-8"))
    for name, entry in envelope["values"].items():
        assert entry.get("kind") in {"measured", "observed", "decided"}, name
        assert entry.get("provenance"), f"{name}: no provenance recorded"


def test_reference_records_what_the_campaign_found() -> None:
    """These assert the RECORD is intact, not that other accounts behave so."""
    sys.path.insert(0, str(REPO))
    from tests.reference import measured_envelope as m

    assert m.GRANULARITIES == (60, 80, 100)
    # Docs said 1-3 in one place, showed 4 in another, claimed 5 in the handbook.
    assert m.FILTER_TYPES == (1, 2, 3, 4)
    assert "tcm" in m.ANALYTIC_TYPES        # appears in no documentation
    assert len(m.ENV_PARAMETERS) == 17
    assert "Failed" not in m.STATUS_OBSERVED
    assert m.RESULTS_DETERMINISTIC is True
    assert m.DURATION_S["/v1/heat_intelligence"]["max"] > 300
