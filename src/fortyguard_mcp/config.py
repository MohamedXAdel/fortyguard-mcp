"""
Configuration. Everything the operator can tune, and nothing the agent can.

Two rules hold here:

  * The API key comes from the environment and is never logged, never echoed,
    and never written into a stored result.
  * Nothing account-specific is hardcoded. AOI caps, entitlements and credit
    costs vary by plan, so they are discovered at runtime. The only
    limit-shaped settings are about the user's disk, not about FortyGuard.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from platformdirs import user_config_dir, user_data_dir
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.api_schema import DEFAULT_BASE_URL

APP_NAME = "fortyguard-mcp"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_data_dir() -> Path:
    """
    A durable data directory, deliberately NOT a cache directory.

    Stored results cost money and never go stale, so they are a paid archive.
    Cache paths get reclaimed by the OS under disk pressure.

        Windows  %LOCALAPPDATA%\\fortyguard-mcp
        macOS    ~/Library/Application Support/fortyguard-mcp
        Linux    ~/.local/share/fortyguard-mcp
    """
    return Path(user_data_dir(APP_NAME, appauthor=False))


def default_config_dir() -> Path:
    """
    Where a per-user key file lives.

        Windows  %LOCALAPPDATA%\\fortyguard-mcp
        macOS    ~/Library/Application Support/fortyguard-mcp
        Linux    ~/.config/fortyguard-mcp
    """
    return Path(user_config_dir(APP_NAME, appauthor=False))


def env_files() -> tuple[Path, ...]:
    """
    Files a key may be read from, LOWEST precedence first. Never the CWD.

    An MCP server is spawned wherever the client happened to be - the MCP docs
    warn it may be `/` on macOS - so a CWD-relative `.env` either adopts an
    unrelated project's keys or misses yours with nothing to indicate why.

    Process environment beats every file below, which makes the client's `env`
    block authoritative.
    """
    files = [default_config_dir() / ".env"]
    explicit = os.environ.get("FORTYGUARD_ENV_FILE")
    if explicit:
        # Last wins, so an explicitly named file overrides the shared one.
        files.append(Path(explicit).expanduser())
    return tuple(files)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FORTYGUARD_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **kwargs: Any) -> None:
        # Resolved per construction, not at class-definition time, so
        # FORTYGUARD_ENV_FILE is honoured whenever it is set.
        kwargs.setdefault("_env_file", env_files())
        super().__init__(**kwargs)

    # --- credentials ------------------------------------------------------- #
    api_key: SecretStr = Field(
        default=SecretStr(""),
        description="FortyGuard API key. Never logged or persisted.",
    )
    base_url: str = Field(default=DEFAULT_BASE_URL)

    # --- storage ----------------------------------------------------------- #
    data_dir: Path = Field(default_factory=default_data_dir)
    max_storage_bytes: int | None = Field(
        default=None,
        description="Optional cap for constrained environments (CI, containers). "
                    "None means unlimited, which is the default: results are a "
                    "paid archive and evicting them costs credits to restore.",
    )

    # --- polling ----------------------------------------------------------- #
    # Generous because heat_intelligence runs 189-395 s. Bounds are `> 0`
    # rather than a polite floor: backoff handles politeness, and a floor only
    # makes the loop untestable at speed.
    poll_initial_delay_s: float = Field(default=2.0, gt=0)
    poll_max_delay_s: float = Field(default=15.0, gt=0)
    poll_backoff_factor: float = Field(default=1.6, ge=1.0)
    poll_timeout_s: float = Field(default=600.0, gt=0)

    # --- http -------------------------------------------------------------- #
    request_timeout_s: float = Field(default=60.0, gt=0)
    # Bounds requests across the whole server, not per tool call: `ToolContext`
    # owns one semaphore for its lifetime.
    max_concurrent_requests: int = Field(default=4, ge=1)

    # --- report downloads --------------------------------------------------- #
    # Separate from `request_timeout_s`: a different host doing a different
    # kind of work - a multi-megabyte transfer, not a JSON round trip.
    report_timeout_s: float = Field(default=120.0, gt=0)
    # A ceiling on what a third-party URL may write to the user's disk.
    # Exceeding it deletes the partial rather than leaving a truncated file.
    report_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    # OFF: the download URL comes from the API response, so a link naming an
    # internal address must not be fetchable. Turn it on only if you genuinely
    # serve reports from a private address - self-hosted MinIO, say.
    report_allow_private_hosts: bool = Field(
        default=False,
        description="Allow report downloads from private, loopback and "
                    "link-local addresses. Off by default.",
    )

    # --- logging ----------------------------------------------------------- #
    # A closed set, so a typo is REJECTED rather than silently becoming INFO.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Written to stderr as JSON lines, with the API key and any "
                    "signed URLs redacted. Never stdout - under stdio that is "
                    "the JSON-RPC channel.",
    )

    # --- response shaping -------------------------------------------------- #
    # Measured: raw GeoJSON is ~12.5x larger than a columnar encoding of the
    # same information. 25k tokens is roughly 12% of a 200k context window.
    inline_token_budget: int = Field(default=25_000, ge=0)
    coordinate_precision: int = Field(default=5, ge=1, le=15)

    @field_validator("data_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(v).expanduser()

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        """
        Require https, except on loopback.

        The API key travels as a header on every request, so plain http puts it
        on the wire in clear. Loopback stays allowed for the replay tests and
        for a local proxy.
        """
        parsed = urlsplit(v.rstrip("/"))
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"FORTYGUARD_BASE_URL must be an http(s) URL, got {v!r}")
        if not parsed.hostname:
            raise ValueError(f"FORTYGUARD_BASE_URL has no host: {v!r}")
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise ValueError(
                f"FORTYGUARD_BASE_URL is plain http ({v!r}). The API key is "
                f"sent as a header on every request, so this would put it on "
                f"the wire in clear. Use https, or point at localhost for a "
                f"local proxy."
            )
        return v.rstrip("/")

    # --- helpers ----------------------------------------------------------- #

    @property
    def key(self) -> str:
        return str(self.api_key.get_secret_value())

    def require_key(self) -> str:
        """
        The key, or a message naming every place that was checked.

        Listing the actual paths turns the most likely support question into
        something the reader can solve alone.
        """
        k = self.key
        if k:
            return k

        explicit = os.environ.get("FORTYGUARD_ENV_FILE")
        checked = [
            "1. environment variable FORTYGUARD_API_KEY - set this in the "
            '"env" block of your MCP client config',
            f"2. FORTYGUARD_ENV_FILE - {explicit or 'not set'}",
            f"3. {default_config_dir() / '.env'} - "
            f"{'exists' if (default_config_dir() / '.env').exists() else 'does not exist'}",
        ]
        # Imported here, not at module scope: `client.errors` is part of the
        # client package, which imports config, and a top-level import would
        # close that cycle.
        from .client.errors import MissingKeyError

        raise MissingKeyError(
            "FORTYGUARD_API_KEY is not set. Checked, in order:\n  "
            + "\n  ".join(checked)
            + "\n\nRun `fortyguard-mcp --set-key` to store it for your user "
              "account, or add it to your client config. Get a key from the "
              "FortyGuard dashboard.\n"
              "Note: the current directory is deliberately NOT searched - an "
              "MCP server is spawned wherever the client happens to be."
        )

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def reports_dir(self) -> Path:
        """
        Downloaded report files, beside the JSON archive rather than inside it.

        `results_dir` is globbed for `*.json` when sizing the archive, so a PDF
        in there would be counted by some scans and not others.
        """
        return self.data_dir / "reports"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
