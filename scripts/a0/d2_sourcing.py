"""
Record the one exchange the D2 sourcing path needs and the campaign lacked.

WHY THIS EXISTS
---------------
`get_env_params(from_activity_id=...)` builds a request the campaign never
issued: the temperature comes from a tile of a stored heatmap, and the date
comes from that heatmap's own request body. The result is a body that differs
from every recorded `env_params` fixture, so the replay server answered

    404  No fixture recorded for this request.

That left the D2 sourcing flow unit-tested but never exercised end to end
offline - the one path where a wrong temperature would be silently plausible.

The request is derived from `t2_15_granularity_60` exactly as the server derives
it, so the recorded fixture matches what the tools actually emit. Anything
hand-written here would match nothing.

    python scripts/a0/d2_sourcing.py          # costs ~2,900 credits, once

Re-running is free: `record_async` skips a case whose fixture already exists.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from record import Recorder, probe_usage  # noqa: E402

from fortyguard_mcp.config import Settings  # noqa: E402
from fortyguard_mcp.tools.runtime import ToolContext  # noqa: E402
from fortyguard_mcp.tools.sourcing import source_from_heatmap  # noqa: E402
from tests.replay import FixtureIndex  # noqa: E402

CASE = "d2_sourced_temperature"
SOURCE_FIXTURE = "t2_15_granularity_60"


def build_payload() -> dict:
    """The body the server produces, derived the way the server derives it."""
    index = FixtureIndex()
    fx = next(f for f in index.fixtures if f.case == SOURCE_FIXTURE)
    result = (fx.terminal_body or {})["data"]["result"]

    ctx = ToolContext(settings=Settings(api_key="unused-offline",
                                        data_dir=Path(tempfile.mkdtemp())))
    ctx.results.put(fx.activity_id, "/v1/heatmap", fx.request_body, result)

    ring = fx.request_body["polygon_aoi"]["features"][0]["geometry"]["coordinates"][0]
    lon = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
    lat = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)

    sourced = source_from_heatmap(ctx, fx.activity_id, lat, lon)
    date_time = dict(sourced["date_time"] or {})
    date_time["filter_type"] = 1        # the caller's mode, per the D2 fix

    return {
        "latitude": lat,
        "longitude": lon,
        "temperature": sourced["temperature"],
        "date_time": date_time,
    }


def main() -> int:
    payload = build_payload()
    print("payload to record:")
    print(json.dumps(payload, indent=1))

    _, before = probe_usage()
    print(f"\ncredits before: {before:,}" if before else "\ncredits before: unknown")

    rec = Recorder(measure_credits=True)
    outcome = rec.record_async(CASE, "/v1/env_params", payload)

    if outcome.skipped:
        print(f"\n{CASE}: already recorded, nothing spent")
        return 0
    if outcome.error:
        print(f"\n{CASE}: FAILED - {outcome.error}")
        return 1

    _, after = probe_usage()
    spent = (before - after) if (before and after) else None
    print(f"\n{CASE}: recorded")
    print(f"  status         : {outcome.terminal_status}")
    print(f"  polls          : {outcome.poll_count}")
    print(f"  credits after  : {after:,}" if after else "  credits after  : unknown")
    print(f"  spent          : {spent:,}" if spent is not None else "  spent: unknown")
    print(f"  credits_delta  : {outcome.credits_delta}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
