# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[security advisory](https://github.com/mohamedxadel/fortyguard-mcp/security/advisories/new)
form rather than as a public issue.

Include what you did, what happened, and what you expected. A reproduction
against the offline replay server in `tests/replay/` is ideal — the whole suite
runs with no API key, no credits and no network.

Expect an acknowledgement within a week.

## Scope

This server holds an API key, writes paid results to local disk, and fetches
files from URLs that arrive in API responses. Findings in any of those areas are
in scope, in particular:

- the API key reaching a log, a tool response, the archive, or any host other
  than `FORTYGUARD_BASE_URL`
- a signed URL surviving into the archive, a log, or a tool response
- agent-supplied input escaping a URL path or a filename
- one API key reading results stored by another on a shared data directory
- a response from the API causing a request to a host or address the operator
  did not configure
- unbounded memory, disk or CPU use driven by a single request

## Out of scope

- The FortyGuard API itself. Report API vulnerabilities to FortyGuard
  directly, not here.
- Running with `--transport sse` or `--transport streamable-http`. These are
  unauthenticated by design and print a warning saying so; stdio is the
  supported transport.
- Setting `FORTYGUARD_REPORT_ALLOW_PRIVATE_HOSTS=true`, which exists to disable
  the private-address check deliberately.
