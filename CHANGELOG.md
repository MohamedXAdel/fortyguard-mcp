# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — unreleased

First public release.

### Security

Findings from a full line-by-line and adversarial review, each reproduced
against running code before it was fixed, and each now covered by a regression
test in `tests/unit/test_security_audit.py`.

- **Report downloads can no longer reach internal services.** A `download_link`
  in an API response is a URL chosen outside this process; it was fetched with
  redirects followed and no host check, so a loopback or link-local address —
  a cloud metadata endpoint, an internal admin port — was reachable and its
  response was written to the user's disk. Every hop is now resolved and refused
  unless it is a public address. `FORTYGUARD_REPORT_ALLOW_PRIVATE_HOSTS=true`
  opts out for self-hosted storage.
- **`activity_id` can no longer leave the status path.** It was interpolated
  into `/v1/status/{id}` unescaped, so `../../` rewrote the request onto any
  endpoint on the API host, carrying the account's `api-key` header and
  reflecting the response back to the caller. It is now percent-encoded.
- **Stored results are scoped to the key that paid for them.** The scope digest
  was mixed into the request hash only, so a second API key sharing a data
  directory could read the first key's results in full by naming the
  `activity_id`. Every read path now checks it. Archives written before this
  release carry no scope and are adopted rather than orphaned.
- **`FORTYGUARD_BASE_URL` must be `https`** unless it is loopback. It was an
  unvalidated string, so a typo could put the API key on the wire in clear.
- **Protocol-relative signed URLs are redacted.** `//host/f.pdf?X-Amz-Signature=`
  matched neither the scheme check nor the pattern, and was archived verbatim.
- **Bounded inputs.** API responses have a size ceiling, redirect chains a hop
  limit, and the three recursive GeoJSON walkers a depth limit — a 600-deep
  `FeatureCollection` raised `RecursionError` out of `validate_aoi`.
- **`--set-key` reports the permissions it actually set.** The mode passed to
  `os.open` applies only on creation, so overwriting an existing file left its
  old permissions while printing `0600`.

### Fixed

- **A completed analysis is no longer lost if archiving fails.** An `OSError`
  from the archive write — a full disk, a read-only mount, a permissions change
  — escaped as a bare error string and discarded a result that had already been
  charged for. The result is now returned in full with `archived: false` and the
  reason.
- **The "0 tiles, credits consumed" warning no longer fires on endpoints that
  never have tiles.** `env_params`, `satellite`, `streetview` and
  `heat_intelligence` received it on every successful call.
- **A 3xx response is an error, not a pending job.** It fell through the status
  gate and was reported as "still running", so an agent would poll it forever.
- **`split_aoi` no longer raises `OverflowError`** on a denormal `max_area_km2`.
- **The archive write is atomic on Windows too.** `shutil.move` falls back to
  copy-then-delete when the destination exists, which on Windows is always.
- **Startup failures explain themselves.** An unwritable `FORTYGUARD_DATA_DIR`
  killed the process with a bare traceback before logging was configured.
- **CLI output no longer crashes on a Windows cp1252 console.**

### Added

- **`fortyguard-mcp setup`** — guided setup: stores the key, verifies it against
  the API, detects installed MCP clients and writes the config, taking a backup
  first.
- **`fortyguard-mcp --doctor`** — checks configuration, disk permissions, the
  API key and every client config, and names what to fix.
- **`fortyguard-mcp --print-config`** and **`--version`**.
- **`--transport`** for `sse` and `streamable-http`, with a warning: this server
  has no authentication or per-caller isolation, so stdio remains the supported
  transport.

### Added

- **12 MCP tools** over FortyGuard's five analysis endpoints, plus geometry and
  account helpers: `create_heatmap`, `submit_heatmap`, `get_env_params`,
  `submit_satellite`, `submit_streetview`, `submit_heat_intelligence`,
  `check_status`, `get_result_slice`, `validate_aoi`, `split_aoi`,
  `get_credit_usage`, `get_storage_info`.
- **2 resources and 1 resource template**: `fortyguard://account/usage`,
  `fortyguard://storage`, `fortyguard://result/{activity_id}`.
- **Bounded waits with recoverable work.** Every wait returns the `activity_id`
  on timeout; the job keeps running server-side and `check_status` collects it.
  Progress notifications are sent during long polls.
- **A durable local archive that doubles as the cache.** Results are
  deterministic for historical dates (verified byte-identical across days), so an
  identical request is served from disk. Nothing is ever evicted.
- **Report downloads.** Heat Intelligence returns a short-lived signed URL rather
  than a document; the PDF is fetched to local disk and the path is returned. The
  URL is never returned, logged, or archived: it is as sensitive as the API key
  itself.
- **Context-aware response shaping.** `format="auto"` inlines the raw payload
  when it fits a token budget and otherwise reports what exists plus every route
  to the rest. `columnar` and `geojson` are uncapped: naming a format is a choice
  and is honoured at any size. Nothing is ever silently truncated.
- **Local geometry.** Geodesic area on the WGS84 authalic sphere — correct at any
  US latitude including Alaska, Hawaii, Puerto Rico and across the antimeridian,
  where a CONUS projection is not. Ring closure, coordinate-order detection, and
  AOI splitting against a cap the caller supplies.
- **Structured stderr logging** with the API key and signed URLs redacted at the
  handler, including third-party loggers and exception text. Never stdout, which
  under stdio is the JSON-RPC channel.
- **An offline test suite**: 478 tests against a replay server built from 50
  recorded API exchanges. No API key, no credits, no network.

### Design notes

- **Nothing account-specific is compiled in.** Area caps, entitlements, credit
  costs and date ranges vary by plan and are read from the account at runtime.
  There is deliberately no `estimate_cost`.
- **Errors pass through verbatim.** FortyGuard's validation messages enumerate
  their own valid sets and are better than anything this layer would write.
- **The current directory is not searched for a `.env`.** An MCP server is
  spawned wherever the client happens to be, so a CWD-relative file either adopts
  an unrelated project's keys or misses yours.

### Known limitations

Documented in full in the README. In brief: United States only; finest
granularity 60 m; no published model accuracy figures; `start_time` is
AOI-local, not UTC; empty results are still billed; some out-of-range requests
reach no terminal state.
