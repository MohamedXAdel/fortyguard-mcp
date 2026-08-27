"""
Structured logging to stderr, with credential redaction.

STDERR ONLY. Under the stdio transport stdout is the JSON-RPC channel, and one
stray byte corrupts the stream — the most common cause of MCP server failure.
The host captures stderr automatically.

The protocol's own logging capability is deliberately unused: deprecated as of
protocol version 2026-07-28. Progress notifications are separate and still sent.

Three things this adds over `logging.basicConfig`: redaction of the API key and
signed URLs from every record, a stdout guard, and quieting of httpx - which
otherwise narrates every request at INFO, URLs included.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from .store.results_store import MIN_SCRUBBABLE_KEY, SIGNED_URL_RE

REDACTED = "[REDACTED]"

# Chatty third parties on a stream the operator has to read. Not silenced -
# warnings and errors still surface - just stopped from narrating.
_NOISY = ("httpx", "httpcore", "urllib3", "asyncio", "anyio")


def scrub(text: str, api_key: str | None) -> str:
    """
    Remove credentials from a line of text.

    The key is matched as a plain substring: it is opaque, so a shape-based
    pattern would fail on the first key shaped differently. Very short values
    are skipped - see `MIN_SCRUBBABLE_KEY`.
    """
    if api_key and len(api_key) >= MIN_SCRUBBABLE_KEY and api_key in text:
        text = text.replace(api_key, REDACTED)
    return SIGNED_URL_RE.sub(REDACTED, text)


class RedactingFilter(logging.Filter):
    """
    Scrubs credentials out of every record that passes through.

    Applied to the HANDLER, not a logger, so it cannot be bypassed by logging
    through a different name - which is exactly where an unredacted URL comes
    from. The key is read lazily, so late-built settings are still honoured.
    """

    def __init__(self, key_source: Any) -> None:
        super().__init__()
        self._key_source = key_source

    def _key(self) -> str | None:
        try:
            value = self._key_source() if callable(self._key_source) \
                else self._key_source
            return str(value) or None
        except Exception:
            # A redaction failure must not break logging. Signed URLs are
            # still scrubbed; only the key substring check goes quiet.
            return None

    def filter(self, record: logging.LogRecord) -> bool:
        key = self._key()
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg, key)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: scrub(v, key) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    scrub(a, key) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """
    One JSON object per line.

    Machine-readable, because these land in a client's log file alongside every
    other server's. Exception text is scrubbed too: a key interpolated into a
    URL inside a traceback would otherwise survive.
    """

    def __init__(self, key_source: Any) -> None:
        super().__init__()
        self._key_source = key_source

    def format(self, record: logging.LogRecord) -> str:
        try:
            key = self._key_source() if callable(self._key_source) \
                else self._key_source
        except Exception:
            key = None

        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage(), key),
        }
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info), key)
        return json.dumps(payload, separators=(",", ":"), default=str)


def _writes_to_stdout(handler: logging.Handler) -> bool:
    stream = getattr(handler, "stream", None)
    return stream is sys.stdout or getattr(stream, "name", "") == "<stdout>"


def configure_logging(level: str = "INFO", key_source: Any = None) -> logging.Handler:
    """
    Install the stderr JSON handler on the root logger. Returns it, for tests.

    Must be called AFTER `MCPServer.__init__`, which runs `logging.basicConfig`
    and installs its own handler; this replaces it rather than adding to it.
    """
    root = logging.getLogger()

    # Every handler, not only stdout ones: the SDK's RichHandler would
    # otherwise double each line, and rich formatting is unhelpful in a file
    # that gets grepped.
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(key_source))
    handler.addFilter(RedactingFilter(key_source))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    return handler


def assert_no_stdout_handlers() -> list[str]:
    """
    Names of any handlers writing to stdout, anywhere in the logging tree.
    Exposed so a test can assert it stays empty: a stdout handler is not a
    degraded log, it silently destroys the protocol.
    """
    offenders: list[str] = []
    manager: Any = logging.Logger.manager
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers += [lg for lg in manager.loggerDict.values()
                if isinstance(lg, logging.Logger)]
    for logger in loggers:
        for handler in logger.handlers:
            if _writes_to_stdout(handler):
                offenders.append(f"{logger.name}:{type(handler).__name__}")
    return offenders
