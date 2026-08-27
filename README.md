# fortyguard-mcp

An MCP server for the [FortyGuard](https://www.fortyguard.com) Temperature API — hyperlocal urban heat data for US locations.

[![CI](https://github.com/mohamedxadel/fortyguard-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/mohamedxadel/fortyguard-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Gives an AI agent 12 tools over FortyGuard's five analysis endpoints: street-level
temperature heatmaps, environmental parameters, satellite and street-view
segmentation, and heat intelligence reports.

---

## Why this exists

FortyGuard's API is asynchronous, returns large payloads, charges per call, and
has a handful of behaviours that are easy to get wrong and expensive to get wrong.
This server handles those:

- **Long-running jobs.** A heat-intelligence report takes 3–7 minutes; MCP
  clients time out long before. Waits are bounded and always return the
  `activity_id`, so nothing is lost.
- **Large results.** One heatmap can be 527 tiles / 223 KB. Payloads pass
  through untouched when they fit; when they don't, you get the statistics plus
  every route to the rest — never a silent truncation.
- **Repeat cost.** Results are deterministic, so they're stored locally and an
  identical request is served from disk instead of being paid for twice.
- **Reports you can actually open.** Heat Intelligence returns a short-lived
  signed URL rather than a document. The server downloads the PDF and hands you
  a local path.

## Design position

**It is a thin pass-through, not a translation layer.** API responses and error
messages are returned verbatim — FortyGuard's validation messages are genuinely
good, and rewriting them would be both brittle and worse:

```
Polygon ring is not closed: the first and last positions must be identical.
Input should be 60, 80 or 100
Latitude -112.095 is out of bounds; must be between -90.0 and 90.0.
```

**Nothing account-specific is baked in.** Area caps, endpoint entitlements,
credit costs and date ranges all vary by plan — Basic allows 10 mi² with no
premium endpoints, Premium allows 50 mi². Those are read from your account at
runtime rather than hardcoded, so the server behaves correctly whatever plan
you're on. There is deliberately no `estimate_cost`: reporting your real balance
is truthful where predicting from someone else's price list would not be.

**It degrades safely.** An unknown status counts as pending, never as success.
An unrecognised result shape is passed through rather than guessed at. A change
at FortyGuard's end costs this server efficiency, not correctness.

## Install

```bash
uvx --from git+https://github.com/mohamedxadel/fortyguard-mcp fortyguard-mcp
```

Or from a checkout:

```bash
pip install -e .
```

Requires Python 3.11+. Runs on Linux, macOS and Windows.

## Set it up

One command does the whole thing — stores your key, checks it against the API,
finds your MCP client and writes the config:

```bash
fortyguard-mcp setup
```

```
FortyGuard MCP setup

1. API key
------------------------------------------------------------------------
Get a key from the FortyGuard dashboard, then paste it here.
FortyGuard API key (input hidden):

OK Stored 32 characters in /home/you/.config/fortyguard-mcp/.env
  Permissions: 0600 (owner read/write only)

2. Check
------------------------------------------------------------------------
Checking the key against the API... works
  plan: Hackathon | credits remaining: 1,242,100

3. Connect a client
------------------------------------------------------------------------
Found:
  1. Claude Desktop         not configured
  2. Cursor                 already configured
  3. none - just print the config

Configure which? [1-3, or Enter to skip]
```

Your existing client config is backed up before anything is written, and only
the `fortyguard` entry is touched.

If something is wrong later, `fortyguard-mcp --doctor` checks the key, the API,
disk permissions and every client config, and tells you what to fix.

### Configuring a client by hand

The key lives under your user profile, so **the client config needs no secret in
it at all** — which matters, because client configs get committed.

<details>
<summary><b>Claude Desktop</b> · <code>claude_desktop_config.json</code></summary>

```json
{
  "mcpServers": {
    "fortyguard": { "command": "fortyguard-mcp" }
  }
}
```
- macOS `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows `%APPDATA%\Claude\claude_desktop_config.json`
- Linux `~/.config/Claude/claude_desktop_config.json`
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add fortyguard -- fortyguard-mcp
```
</details>

<details>
<summary><b>Cursor</b> · <code>~/.cursor/mcp.json</code></summary>

```json
{
  "mcpServers": {
    "fortyguard": { "command": "fortyguard-mcp" }
  }
}
```
</details>

<details>
<summary><b>VS Code</b> · user <code>settings.json</code></summary>

```json
{
  "mcp": {
    "servers": {
      "fortyguard": { "command": "fortyguard-mcp" }
    }
  }
}
```
</details>

<details>
<summary><b>Windsurf</b> · <code>~/.codeium/windsurf/mcp_config.json</code></summary>

```json
{
  "mcpServers": {
    "fortyguard": { "command": "fortyguard-mcp" }
  }
}
```
</details>

Not installed on PATH? Use `{"command": "uvx", "args": ["fortyguard-mcp"]}`.
`fortyguard-mcp --print-config` prints the right block for your machine.

This server speaks **stdio**, so any client that launches a local process works.
Hosted connectors that only accept an HTTPS URL cannot reach it as shipped —
see [Serving over a network](#serving-over-a-network).

### Where the key is looked for

| | Source | |
|---|---|---|
| 1 | `FORTYGUARD_API_KEY` in the environment | your client's `env` block |
| 2 | `FORTYGUARD_ENV_FILE=/abs/path` | explicit opt-in |
| 3 | `<config dir>/.env` | what `setup` writes |

**The current directory is deliberately not searched.** An MCP server is spawned
wherever the client happens to be — the protocol docs warn it may be `/` on
macOS. A CWD-relative `.env` therefore does one of two wrong things: silently
adopts an unrelated repository's keys (including `FORTYGUARD_DATA_DIR`, which
would redirect your paid archive), or fails to find the key you did set, with
nothing to indicate why. Both were reproduced before this was changed.

Run `fortyguard-mcp --where` to see every path checked and which one resolved.

### Settings

| Variable | Default | Purpose |
|---|---|---|
| `FORTYGUARD_API_KEY` | — | **Required.** Never logged or written to disk by this server. |
| `FORTYGUARD_BASE_URL` | `https://api.fortyguard.com` | API endpoint. Must be `https` unless it is loopback. |
| `FORTYGUARD_DATA_DIR` | platform data dir | Where results and reports are stored |
| `FORTYGUARD_INLINE_TOKEN_BUDGET` | `25000` | Above this, `format="auto"` summarises rather than inlines |
| `FORTYGUARD_COORDINATE_PRECISION` | `5` | Decimal places in compact encoding (~1 m) |
| `FORTYGUARD_POLL_TIMEOUT_S` | `600` | Ceiling on any single wait |
| `FORTYGUARD_REPORT_TIMEOUT_S` | `120` | Ceiling on a report download |
| `FORTYGUARD_REPORT_MAX_BYTES` | `104857600` | Ceiling on what one download may write to disk |
| `FORTYGUARD_REPORT_ALLOW_PRIVATE_HOSTS` | `false` | Allow report downloads from private/loopback addresses. Only for self-hosted storage. |
| `FORTYGUARD_MAX_STORAGE_BYTES` | unset | Optional archive cap for CI/containers |
| `FORTYGUARD_LOG_LEVEL` | `INFO` | Diagnostics to **stderr** as JSON lines, credentials redacted |

## Troubleshooting

Run `fortyguard-mcp --doctor` first — it checks each of these and names the fix.

| Symptom | Cause | Fix |
|---|---|---|
| Client shows no tools | Server failed to start | `fortyguard-mcp --doctor`; check the client's MCP log |
| Every call fails with "FORTYGUARD_API_KEY is not set" | Key not found on any of the three paths | `fortyguard-mcp setup`, or `--where` to see what was checked |
| `[401]` or `[403]` | Key rejected by the API | Check it in the FortyGuard dashboard |
| `[402]` insufficient credits | Balance exhausted | `get_credit_usage` for the real balance |
| Result "completed" with 0 tiles | Outside coverage, below the minimum area, or no data for that date | Still charged — check the AOI with `validate_aoi` |
| "cannot start … data directory could not be prepared" | `FORTYGUARD_DATA_DIR` unwritable | Point it somewhere writable |
| Report download refused, "not a public address" | The link named a private address | Expected. Set `FORTYGUARD_REPORT_ALLOW_PRIVATE_HOSTS=true` only for self-hosted storage |

## Tools

| Tool | Costs credits? | What it does |
|---|---|---|
| `get_credit_usage` | no | Your plan, balance and per-endpoint breakdown, from the API |
| `get_storage_info` | no | What is archived locally, by endpoint, and where it lives |
| `validate_aoi` | no | Geodesic area, bounds, edge lengths, ring closure, coordinate order |
| `split_aoi` | no | Cut an area into pieces under a maximum you supply |
| `create_heatmap` | yes | Run a heatmap and wait inline (measured 21–38 s) |
| `submit_heatmap` | yes | Submit and return immediately with an `activity_id` |
| `get_env_params` | yes | Humidity, heat index, wet bulb, air quality at a point |
| `submit_satellite` | yes | Satellite land-cover segmentation |
| `submit_streetview` | yes | Street-view scene analysis |
| `submit_heat_intelligence` | yes | Full heat report as a PDF (3–7 min, never waited on inline) |
| `check_status` | no* | Collect a submitted analysis; free once collected |
| `get_result_slice` | **no** | Read part or all of a stored result: `top_n`, `bbox`, `every_nth`, `columnar`, `geojson` |

\* Polling itself is free — measured across calls taking 1 to 121 polls, all
charged identically. Credits attach to the submitted task once, on success.

### Resources

| URI | Contents |
|---|---|
| `fortyguard://account/usage` | This key's plan and credits |
| `fortyguard://storage` | The local archive |
| `fortyguard://result/{activity_id}` | The complete untouched payload, uncapped |

### Result size is your choice, not ours

A large result is never truncated and never withheld. `format` decides:

| `format` | Behaviour |
|---|---|
| `auto` *(default)* | the raw payload when it fits the context budget; otherwise a summary listing **every** route to the rest, including taking all of it |
| `columnar` | every tile as a compact table — **no ceiling**, roughly 12× smaller than raw |
| `geojson` | the untouched API payload — **no ceiling** |

The budget applies to `auto` only, because `auto` is you declining to choose.
Naming a format is you choosing, and it is honoured at whatever size the result
comes to.

### Supplying `temperature`

`get_env_params` and `submit_heat_intelligence` need a temperature matching the
heatmap for the same place and time. Give **either** `temperature=` **or**
`from_activity_id=` naming a completed heatmap — not both. Sourcing reads a
stored result, so it costs nothing and also supplies the matching date, keeping
the two consistent by construction. Supplying both is an error rather than a
silent precedence rule, because the two can disagree and picking a winner would
hide that.

### Heat Intelligence reports

The API returns this analysis as a **temporary signed URL**, not as a document.
`check_status` downloads the PDF before that link expires and returns the path:

```json
"report": {
  "downloaded": true,
  "path": "/home/you/.local/share/fortyguard-mcp/reports/<activity_id>.pdf",
  "size_bytes": 960709,
  "content_type": "application/pdf"
}
```

The URL itself is never returned, logged, or archived. That is not merely
tidiness: this URL should be treated as being **as sensitive as your API key**,
not as a scoped capability that stops mattering once it expires. Anywhere the
link lands is somewhere a credential has landed.

If the download fails, the analysis is still archived and the response says so
plainly — the link is not recoverable, and re-running the analysis is charged
again.

## A real API request and response

Recorded live on 2026-08-23, verbatim. This exact exchange is in the repository
as `tests/fixtures/v1_heatmap/t2_5_exceedance.json`, and the test suite replays
it — so this is re-checkable rather than illustrative.

**What was asked:** how many hours on 15 July 2024, between 06:00 and 18:00
local, did each 100 m tile of a downtown-Phoenix block spend above 30 °C?

**Request** — `POST https://api.fortyguard.com/v1/heatmap`
(`api-key` header redacted):

```json
{
  "polygon_aoi": {
    "type": "FeatureCollection",
    "features": [{
      "type": "Feature",
      "properties": {},
      "geometry": {
        "type": "Polygon",
        "coordinates": [[
          [-112.095, 33.470], [-112.080, 33.470],
          [-112.080, 33.479], [-112.095, 33.479],
          [-112.095, 33.470]
        ]]
      }
    }]
  },
  "granularity": 100,
  "date_time": {
    "start_date": "2024-07-15",
    "start_time": "06:00",
    "end_time": "18:00",
    "filter_type": 2
  },
  "analytic_type": "exceedance",
  "threshold": 30,
  "direction": "above"
}
```

**Submit response** — the API is asynchronous, so this returns an id, not data:

```json
{
  "error": false,
  "status_code": 200,
  "message": "Heatmap Submitted Successfully",
  "data": { "activity_id": "5ca4bab3-7ae2-463f-b7a9-8ab77bc5e6c0" }
}
```

**Final poll** — `GET /v1/status/5ca4bab3-7ae2-463f-b7a9-8ab77bc5e6c0`,
truncated to one of 112 tiles:

```json
{
  "error": false,
  "status_code": 200,
  "data": {
    "activity_id": "5ca4bab3-7ae2-463f-b7a9-8ab77bc5e6c0",
    "status": "Completed",
    "result": {
      "map_data": {
        "type": "FeatureCollection",
        "features": [{
          "id": "0",
          "type": "Feature",
          "properties": { "tile_id": 0, "value": 12.0 },
          "geometry": {
            "type": "Polygon",
            "coordinates": [[
              [-112.09525400214712, 33.47190687658275],
              [-112.09418659710028, 33.47191630437707],
              [-112.09419749679817, 33.47278345511555],
              [-112.09526491247276, 33.47277402701284],
              [-112.09525400214712, 33.47190687658275]
            ]]
          }
        }]
      },
      "stats_data": {
        "activity_id": "5ca4bab3-7ae2-463f-b7a9-8ab77bc5e6c0",
        "analytic_type": "exceedance",
        "units": "hour",
        "n_cells": 112,
        "min": 12.0,
        "max": 12.0,
        "mean": 12.0
      }
    }
  }
}
```

**The answer:** every one of the 112 tiles was above 30 °C for all 12 hours
requested. Cost, measured: 4,220 credits.

Through this server, that whole exchange — submit, poll until complete, archive,
shape to fit the context window — is one `create_heatmap` call.

## What does not work yet

Stated plainly, because knowing the edges is more useful than a feature list.

**Not built**

- **No remote/hosted mode.** The server speaks stdio and is launched as a local
  subprocess. `--transport sse|streamable-http` exists but has no authentication
  and no per-caller isolation, so it is not a supported deployment — see
  [Serving over a network](#serving-over-a-network).
- **No cost estimation.** Deliberate: per-call cost varies by plan, and a lookup
  table built from one account would confidently mislead every other one. Use
  `get_credit_usage` for your real balance.
- **No geocoding.** Areas of interest are GeoJSON. There is no "Phoenix
  downtown" → polygon step; the agent supplies coordinates.
- **No caching of failed or in-flight work.** Only completed results are
  archived.
- **No automatic retry.** A transport failure is reported, not retried.

**Known limits, measured against the live API**

- **United States only.** Areas outside coverage return a *successful* response
  with zero tiles and are **still charged**. The server says so explicitly, but
  it cannot prevent the charge — coverage is not published as a queryable map.
- **60 m is the finest granularity**, despite marketing describing ~20 m.
- **Empty results are billed** — sub-minimum areas, dates with no data, and
  times past the forecast edge all return `Completed` with zero tiles at full
  price.
- **Some requests never reach a terminal state.** Very large areas and
  out-of-range dates were still `Processing` after ~8 minutes. Every wait is
  bounded for this reason, and returns the `activity_id`.
- **`start_time` is local to the area of interest, not UTC.** Undocumented by
  the vendor and easy to get wrong.
- **No published accuracy figures.** No RMSE, MAE or bias for FortyGuard's
  models is publicly available, and we found no independent validation. Treat
  outputs as a relative heat surface, not as calibrated ground truth.
- **Compact encoding uses tile centroids**, accurate to about a centimetre, not
  exact polygon rings. Request `format="geojson"` for exact geometry.
- **The archive grows without bound.** Nothing is evicted, by design — results
  cost credits and never go stale. `FORTYGUARD_MAX_STORAGE_BYTES` caps it if you
  need that; `get_storage_info` shows what is there.

**Verified platform support**

Linux, macOS and Windows; Python 3.11–3.14. CI covers all three on 3.14 and
Linux across every version. Windows was the development machine.

## Stored data

Results are written to a **durable data directory**, not a cache directory:

| | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\fortyguard-mcp\` |
| macOS | `~/Library/Application Support/fortyguard-mcp/` |
| Linux | `~/.local/share/fortyguard-mcp/` |

```
results/<activity_id>.json        the payload
results/<activity_id>.meta.json   endpoint, request, size, hash
reports/<activity_id>.pdf         files fetched from a signed URL
index/<request_hash>              request -> activity_id, for the cache
```

**Nothing is evicted.** Results cost credits and never go stale, so deleting them
to reclaim cheap disk would cost real money to undo. Cache directories get
reclaimed by the OS under disk pressure, which is exactly why this isn't one.
The directory is plain files and safe to delete whenever you like — you lose only
the ability to avoid re-paying for those queries.

API keys and pre-signed URLs are stripped before anything is written, and stored
payloads are always valid JSON: non-finite numbers are nulled on the way in and
the count is recorded on the sidecar, so the archive never quietly differs from
what the API sent.

## Security

The server treats everything it did not originate as untrusted — including the
API's own responses.

- **Credentials never leave.** The API key is redacted from every log record,
  every tool response and everything written to disk. Pre-signed URLs are
  treated the same way: the report URL is as sensitive as the key itself, so it
  is fetched and then destroyed rather than stored.
- **Downloads are scoped to the public internet.** A `download_link` in an API
  response is a URL chosen by something outside this process. Every hop,
  redirects included, is resolved and refused if it points at a loopback,
  link-local, private or reserved address — so a malformed or hostile response
  cannot use this server to read your cloud metadata endpoint or an internal
  admin port. `FORTYGUARD_REPORT_ALLOW_PRIVATE_HOSTS=true` opts out for
  self-hosted storage.
- **Agent input never builds a URL or a path.** `activity_id` is percent-encoded
  before it enters the status path, and every filename is sanitised with a
  digest appended when sanitising changes it, so two ids cannot collide.
- **`https` is required** for `FORTYGUARD_BASE_URL` unless it is loopback: the
  key travels as a header on every request.
- **Several API keys can share one machine.** Every stored result is stamped
  with a digest of the key and base URL that paid for it, and a key only ever
  reads back its own — by request, by `activity_id`, or by resource URI. A
  staging base URL likewise never answers with production data.
- **Bounded by default.** Response bodies, report downloads, redirect chains,
  GeoJSON nesting depth and poll waits all have ceilings, so a broken or hostile
  upstream cannot exhaust memory, disk or the event loop.

Found something? Please open a security advisory on the repository rather than a
public issue.

## Logging

Diagnostics are written to **stderr** as one JSON object per line:

```json
{"ts":"2026-08-26T00:46:17.007+00:00","level":"INFO","logger":"fortyguard_mcp.server","msg":"fortyguard-mcp starting"}
```

Never to stdout — under the stdio transport that is the JSON-RPC channel, and a
single stray byte corrupts the stream. A test drives a real subprocess and asserts
every line of stdout parses as JSON-RPC.

The API key and any pre-signed URLs are stripped from every record, including
exception text and third-party loggers. The protocol's own logging capability
(`notifications/message`) is deliberately unused: it is deprecated as of protocol
version 2026-07-28, and the SDK drops messages the client did not opt into.
Progress notifications during long polls are a separate mechanism and are still
sent.

## Verifying an install

With the [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector):

```bash
npx @modelcontextprotocol/inspector --cli --config your-config.json --server fortyguard --method tools/list
```

Expect 12 tools, 2 resources and 1 resource template. `validate_aoi` is the safest
smoke test — it is local and costs nothing.

## Serving over a network

The default and supported transport is **stdio**: the client launches the server
as a local subprocess. `--transport sse` and `--transport streamable-http` exist
but print a warning, because everything above assumes a single local user:

> There is no authentication, no per-caller isolation and no rate limiting.
> Anyone who can reach the port can spend your credits and read your archive.

If you need it, bind to loopback behind an authenticating reverse proxy.

## Development

```bash
pip install -e ".[dev]"
pytest                          # 534 tests, fully offline — no API key, no credits
ruff check .
mypy src
```

The suite runs against a replay server built from 50 real recorded API exchanges,
so it is deterministic, free, and green even when the API is unreachable.

Two cross-checks need extra libraries and are kept in their own extra, because
they verify the two numbers this package quotes most — the geodesic area
agreement with `pyproj`, and the chars-per-token ratios that decide whether a
payload is inlined:

```bash
pip install -e ".[dev,verify]"  # adds pyproj + tiktoken; nothing should skip
```

CI runs the suite on Python 3.11–3.14 (Linux, plus Windows and macOS at 3.14),
type-checks, lints, builds both artifacts, installs the wheel into a clean venv,
and runs the cross-check job separately.

Run `mypy` in an environment holding only this package's dependencies. A dev box
that also has `numpy` installed trips over numpy's stubs, which use 3.12-only
syntax; nothing in `src/` imports numpy.

## Further reading

- [`MEASUREMENTS.md`](MEASUREMENTS.md) — the measured API envelope: costs,
  durations, enums, error taxonomy, determinism. Every value measured live, with
  the recorded exchanges in `tests/fixtures/` so each is re-checkable offline.
- [`CHANGELOG.md`](CHANGELOG.md) — including the security review this release
  came out of

## Provenance

Built for the FortyGuard "Building the World's Temperature AI" hackathon
(kickoff 18 Aug 2026). All source in this repository was written after kickoff,
between 22 and 28 Aug 2026. No pre-existing boilerplate was carried in; the
project depends only on the third-party packages declared in `pyproject.toml`
(`mcp`, `httpx`, `pydantic`, `pydantic-settings`, `platformdirs`).

The 50 recorded API exchanges in `tests/fixtures/` are real responses from the
live FortyGuard API, captured during the build with the `api-key` header
redacted at record time.

## Licence

MIT
