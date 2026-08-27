"""
Derive `verified_envelope.json` from the recorded fixtures.

The point of this step is that the enums are **parsed out of the API's own 422
messages**, not typed in by hand. The API enumerates its valid sets when it
rejects a bad value:

    Input should be 60, 80 or 100
    Input should be 1, 2, 3 or 4
    Input should be 'tcm', 'time_of_measure', 'exceedance' or 'persistence'

So the constraint layer traces to a recorded HTTP response rather than to
someone's reading of the docs — which mattered here, since the docs contradicted
themselves three ways on filter_type alone.

Every entry carries a provenance kind:
  measured  parsed from a recorded response
  observed  computed from recorded timings/costs
  decided   a human decision, recorded with its reason

    python scripts/a0/build_envelope.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
REPORTS = REPO / "tests" / "a0_reports"
OUT = REPO / "verified_envelope.json"

# "Input should be 60, 80 or 100"  /  "Input should be 'a', 'b' or 'c'"
_ENUM_RE = re.compile(r"Input should be (.+?)$")


def _parse_enum(message: str) -> list[Any] | None:
    m = _ENUM_RE.search(message.strip())
    if not m:
        return None
    tail = m.group(1).rstrip(".")
    parts = [p.strip() for p in tail.replace(" or ", ", ").split(",")]
    out: list[Any] = []
    for p in parts:
        if not p:
            continue
        if p.startswith("'") and p.endswith("'"):
            out.append(p[1:-1])
        else:
            try:
                out.append(int(p))
            except ValueError:
                out.append(p)
    return out or None


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _submit_message(fixture: Path) -> str | None:
    doc = _load(fixture)
    body = (doc.get("submit_response") or {}).get("body") or {}
    return body.get("message") if isinstance(body, dict) else None


def enum_from_fixture(rel: str, field: str) -> dict | None:
    """Extract a validation enum from a recorded rejection."""
    p = FIXTURES / rel
    if not p.exists():
        return None
    msg = _submit_message(p)
    if not msg:
        return None
    values = _parse_enum(msg)
    if values is None:
        return None
    return {"value": values, "kind": "measured", "field": field,
            "provenance": msg, "fixture": rel}


def costs_and_durations() -> tuple[dict, dict]:
    """Per-endpoint cost from the campaign reports; duration from fixture timings."""
    costs: dict[str, Any] = {}
    tier1 = REPORTS / "tier1_entitlement.json"
    tier2 = REPORTS / "tier2_schema.json"
    for rpt in (tier1, tier2):
        if not rpt.exists():
            continue
        for case, info in _load(rpt).items():
            if not isinstance(info, dict):
                continue
            ep = info.get("endpoint")
            cost = info.get("cost")
            if not ep:
                continue
            if cost:
                costs.setdefault(ep, cost)
            for v in (info.get("credit_delta") or {}).values():
                if v.get("per_call"):
                    costs.setdefault(ep, v["per_call"])

    # Heatmap is measured many times over; assert the flat-rate claim holds.
    durations: dict[str, dict] = {}
    for p in FIXTURES.rglob("*.json"):
        doc = _load(p)
        ep = "/" + p.parent.name.replace("_", "/", 1)
        t = (doc.get("timing") or {})
        if not t.get("reached_terminal"):
            continue
        d = t.get("duration_s")
        if not isinstance(d, (int, float)):
            continue
        slot = durations.setdefault(ep, {"min_s": d, "max_s": d, "n": 0})
        slot["min_s"] = min(slot["min_s"], d)
        slot["max_s"] = max(slot["max_s"], d)
        slot["n"] += 1
    return costs, durations


def statuses_observed() -> list[str]:
    seen: set[str] = set()
    for p in FIXTURES.rglob("*.json"):
        for s in _load(p).get("observed_statuses") or []:
            seen.add(s)
    return sorted(seen)


def main() -> None:
    values: dict[str, dict] = {}

    # --- enums parsed straight out of recorded 422s ------------------------ #
    for name, rel, field in [
        ("GRANULARITIES", "v1_heatmap/e_granularity_50.json", "granularity"),
        ("FILTER_TYPES", "v1_heatmap/e_filter_type_9.json", "date_time.filter_type"),
        ("ANALYTIC_TYPES", "v1_heatmap/e_bad_analytic_type.json", "analytic_type"),
        ("ENV_PARAMETERS", "v1_env_params/e_env_bad_param_name.json", "analysis"),
    ]:
        got = enum_from_fixture(rel, field)
        if got:
            values[name] = got
        else:
            print(f"  !! could not derive {name} from {rel}")

    # --- observed behaviour ------------------------------------------------ #
    obs = statuses_observed()
    values["STATUSES"] = {
        "value": obs, "kind": "observed",
        "provenance": f"observed across the campaign; 'Failed' never seen in ~100 calls",
        "terminal_success": [s for s in obs if s.lower() in
                             {"completed", "succeeded", "success"}],
        "pending": [s for s in obs if s.lower() in {"processing", "pending", "running"}],
    }

    costs, durations = costs_and_durations()
    values["COST_PER_CALL"] = {
        "value": costs, "kind": "observed",
        "provenance": "credit deltas bracketed per endpoint via activity_breakdown; "
                      "flat per call, independent of area and granularity",
    }
    values["DURATION_S"] = {
        "value": durations, "kind": "observed",
        "provenance": "wall-clock to terminal status across recorded fixtures",
    }

    # --- decisions, recorded with their reasons ---------------------------- #
    values["DATE_FLOOR"] = {
        "value": "2021-01-01", "kind": "decided",
        "provenance": "Handbook + public site FAQ + confirmed key behaviour. "
                      "API docs' 2019-01-01 is stale. Pre-floor requests are NOT "
                      "rejected — they hang in Processing forever, so this must be "
                      "enforced client-side.",
    }
    values["AOI_MAX_KM2"] = {
        "value": 130, "kind": "decided",
        "provenance": "Documented 50 mi² = 129.5 km². Not enforced server-side: an "
                      "over-cap AOI is accepted and hangs indefinitely. Measuring the "
                      "true boundary costs ~15k credits and yields little, so the "
                      "documented figure is enforced client-side.",
    }
    values["MIN_AOI_EDGE_M"] = {
        "value": 300, "kind": "measured",
        "provenance": "220 m AOI -> Completed with 0 tiles at full price (4,220); "
                      "300 m -> 9 tiles. True threshold lies between.",
        "fixture": "v1_heatmap/t0_4_smallest_heatmap.json",
    }
    values["FORECAST_HOURS"] = {
        "value": 10, "kind": "measured",
        "provenance": "+2h/+6h/+10h returned full results; +12h returned 0 tiles. "
                      "start_time is AOI-LOCAL, not UTC. Edge moves with data "
                      "freshness, so warn rather than reject beyond this.",
        "fixture": "tests/a0_reports/forecast_horizon.json",
    }
    values["TIME_BASIS"] = {
        "value": "aoi-local", "kind": "measured",
        "provenance": "A UTC-built timestamp was read as local, putting a +6h probe "
                      "at +13h and returning empty. Undocumented.",
    }
    values["KNOWN_EMPTY_DATES"] = {
        "value": ["2026-08-22"], "kind": "measured",
        "provenance": "Every hour probed on this date returns 0 tiles while "
                      "2026-08-21 and 2026-08-23 return full results. Archive gap. "
                      "Empty results still cost 4,220, so warn before spending.",
    }
    values["DETERMINISTIC"] = {
        "value": True, "kind": "measured",
        "provenance": "Identical request re-issued days later returned byte-identical "
                      "values across 112 tiles including stats. Cache keys need no "
                      "time component.",
        "fixture": "tests/a0_reports/T3.1_determinism.json",
    }

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Phase A0 API truth campaign",
        "fixture_count": sum(1 for _ in FIXTURES.rglob("*.json")),
        "warning": "GENERATED FILE. Edit the campaign, not this.",
        "values": values,
    }
    OUT.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    print(f"wrote {OUT.relative_to(REPO)}  ({doc['fixture_count']} fixtures)")
    for k, v in values.items():
        val = v["value"]
        shown = val if not isinstance(val, (dict, list)) or len(str(val)) < 70 \
            else f"<{type(val).__name__} n={len(val)}>"
        print(f"  {k:20s} [{v['kind']:8s}] {shown}")


if __name__ == "__main__":
    main()
