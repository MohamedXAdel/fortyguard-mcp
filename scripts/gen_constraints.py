"""
Generate `tests/reference/measured_envelope.py` from `verified_envelope.json`.

TEST REFERENCE DATA — NOT PRODUCTION CODE.

Everything here was measured against ONE API key on the Hackathon plan. AOI
caps, entitlements, credit costs, date floors, coverage and forecast horizon all
vary by plan and contract, so baking them into the shipped package would hand a
Basic-plan user (10 mi2 cap, no premium endpoints) limits that do not apply to
them. Production discovers those at runtime; see
src/fortyguard_mcp/domain/api_schema.py for the small universal subset.

What this file is good for: writing tests. It records what this account actually
returned, so tests can assert the client handles those values correctly, and so
a future re-run of the campaign shows exactly what drifted.

    python scripts/gen_constraints.py          # write
    python scripts/gen_constraints.py --check  # verify, exit 1 on drift
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
ENVELOPE = REPO / "verified_envelope.json"
TARGET = REPO / "tests" / "reference" / "measured_envelope.py"

BANNER = "GENERATED TEST REFERENCE - DO NOT EDIT BY HAND - NOT PRODUCTION CODE"


def _wrap(text: str, width: int = 74, indent: str = "#   ") -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(indent + cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(indent + cur)
    return lines


def _provenance_block(name: str, entry: dict) -> list[str]:
    out = [f"# {name} [{entry['kind']}]"]
    out += _wrap(entry.get("provenance", ""))
    if entry.get("fixture"):
        out.append(f"#   fixture: {entry['fixture']}")
    return out


def render(envelope: dict) -> str:
    v = envelope["values"]
    blob = json.dumps(envelope["values"], sort_keys=True).encode()
    digest = hashlib.sha256(blob).hexdigest()

    L: list[str] = []
    A = L.append

    A('"""')
    A(BANNER)
    A("")
    A("Generated from verified_envelope.json by scripts/gen_constraints.py.")
    A("")
    A("TEST REFERENCE ONLY. Measured against a single API key on the Hackathon")
    A("plan. Plan-specific values - AOI cap, entitlements, credit costs, date")
    A("floor, coverage, forecast horizon, data gaps - do NOT generalise to other")
    A("accounts and must never be imported by production code.")
    A("")
    A("The enums were parsed out of the API's own 422 validation messages, which")
    A("enumerate their valid sets - not from the documentation, which")
    A("contradicted itself three ways on filter_type.")
    A("")
    A("To change anything here, change the campaign and regenerate.")
    A('"""')
    A("")
    A("from __future__ import annotations")
    A("")
    A("from datetime import date")
    A("from typing import Final")
    A("")
    A(f'ENVELOPE_SHA256: Final[str] = "{digest}"')
    A(f'GENERATED_AT: Final[str] = "{envelope["generated_at"]}"')
    A(f'FIXTURE_COUNT: Final[int] = {envelope["fixture_count"]}')
    A("")
    A("")

    # --- enums ---
    for name in ("GRANULARITIES", "FILTER_TYPES", "ANALYTIC_TYPES"):
        e = v[name]
        L.extend(_provenance_block(name, e))
        A(f"{name}: Final[tuple] = {tuple(e['value'])!r}")
        A("")

    e = v["ENV_PARAMETERS"]
    L.extend(_provenance_block("ENV_PARAMETERS", e))
    A("ENV_PARAMETERS: Final[tuple[str, ...]] = (")
    for p in e["value"]:
        A(f'    "{p}",')
    A(")")
    A("")

    # --- statuses ---
    e = v["STATUSES"]
    L.extend(_provenance_block("STATUSES", e))
    A(f"STATUS_OBSERVED: Final[tuple[str, ...]] = {tuple(e['value'])!r}")
    A(f"STATUS_TERMINAL_SUCCESS: Final[frozenset[str]] = frozenset({set(e['terminal_success'])!r})")
    A(f"STATUS_PENDING: Final[frozenset[str]] = frozenset({set(e['pending'])!r})")
    A("# No 'Failed' status was ever observed. Terminal detection must therefore")
    A("# be timeout-driven, never solely status-driven.")
    A("STATUS_TERMINAL_FAILURE: Final[frozenset[str]] = frozenset({\"Failed\", \"Error\"})")
    A("")

    # --- scalars ---
    e = v["DATE_FLOOR"]
    L.extend(_provenance_block("DATE_FLOOR", e))
    y, m, d = e["value"].split("-")
    A(f"DATE_FLOOR: Final[date] = date({int(y)}, {int(m)}, {int(d)})")
    A("")

    for name, typ in (("AOI_MAX_KM2", "float"), ("MIN_AOI_EDGE_M", "float"),
                      ("FORECAST_HOURS", "int")):
        e = v[name]
        L.extend(_provenance_block(name, e))
        A(f"{name}: Final[{typ}] = {e['value']}")
        A("")

    e = v["TIME_BASIS"]
    L.extend(_provenance_block("TIME_BASIS", e))
    A(f'TIME_BASIS: Final[str] = "{e["value"]}"')
    A("")

    e = v["DETERMINISTIC"]
    L.extend(_provenance_block("DETERMINISTIC", e))
    A(f"RESULTS_DETERMINISTIC: Final[bool] = {e['value']}")
    A("")

    e = v["KNOWN_EMPTY_DATES"]
    L.extend(_provenance_block("KNOWN_EMPTY_DATES", e))
    A("KNOWN_EMPTY_DATES: Final[frozenset[date]] = frozenset({")
    for s in e["value"]:
        y, m, d = s.split("-")
        A(f"    date({int(y)}, {int(m)}, {int(d)}),")
    A("})")
    A("")

    # --- cost & duration ---
    e = v["COST_PER_CALL"]
    L.extend(_provenance_block("COST_PER_CALL", e))
    A("COST_PER_CALL: Final[dict[str, int]] = {")
    for k, c in sorted(e["value"].items()):
        A(f'    "{k}": {c},')
    A("}")
    A("")

    e = v["DURATION_S"]
    L.extend(_provenance_block("DURATION_S", e))
    A("DURATION_S: Final[dict[str, dict[str, float]]] = {")
    for k, dd in sorted(e["value"].items()):
        A(f'    "{k}": {{"min": {dd["min_s"]}, "max": {dd["max_s"]}, "n": {dd["n"]}}},')
    A("}")
    A("")
    A("# Granularity is optional in the API and defaults to 100 despite the docs")
    A("# listing it as required.")
    A("GRANULARITY_DEFAULT: Final[int] = 100")
    A("")

    return "\n".join(L) + "\n"


def main() -> None:
    if not ENVELOPE.exists():
        sys.exit("verified_envelope.json missing - run scripts/a0/build_envelope.py")
    envelope: dict[str, Any] = json.loads(ENVELOPE.read_text(encoding="utf-8"))
    source = render(envelope)

    if "--check" in sys.argv:
        if not TARGET.exists():
            sys.exit(f"FAIL: {TARGET.relative_to(REPO)} does not exist")
        current = TARGET.read_text(encoding="utf-8")
        # Ignore the timestamp line, which changes on every envelope rebuild.
        def strip_ts(t: str) -> str:
            return "\n".join(l for l in t.splitlines()
                             if not l.startswith("GENERATED_AT"))
        if strip_ts(current) != strip_ts(source):
            sys.exit("FAIL: measured_envelope.py has drifted from "
                     "verified_envelope.json.\n"
                     "      It is a generated file - run scripts/gen_constraints.py.")
        print("OK: measured_envelope.py matches verified_envelope.json")
        return

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(source, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)}  ({len(source.splitlines())} lines)")


if __name__ == "__main__":
    main()
