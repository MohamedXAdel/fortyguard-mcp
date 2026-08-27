"""
Console entry point: `fortyguard-mcp`, or `uvx fortyguard-mcp`.

    fortyguard-mcp              serve over stdio (what an MCP client runs)
    fortyguard-mcp setup        guided setup: key, check, client config
    fortyguard-mcp --set-key    store your API key, nothing else
    fortyguard-mcp --doctor     diagnose a broken install
    fortyguard-mcp --where      show where config and results live
    fortyguard-mcp --help       this

STDOUT DISCIPLINE
-----------------
With no arguments this process IS an MCP server and stdout is the JSON-RPC
channel: one stray byte corrupts the stream. So in server mode nothing but the
protocol touches stdout. With arguments we are a plain CLI and stdout is ours.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from .clients import detect_clients
from .config import default_config_dir, default_data_dir, get_settings
from .server import Transport

USAGE = __doc__
TRANSPORTS: tuple[Transport, ...] = ("stdio", "sse", "streamable-http")

# ANSI, but only when stdout is a terminal that is not a dumb one.
_TTY = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def _encodable(sample: str) -> bool:
    """Can this console actually print that? A Windows cp1252 console cannot."""
    try:
        sample.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# Checked, not assumed: printing a box-drawing character to a cp1252 console
# raises UnicodeEncodeError and takes the whole command down.
_UNICODE = _encodable("─✓·")
RULE_CH = "─" if _UNICODE else "-"
TICK = "✓" if _UNICODE else "OK"
DOT = "·" if _UNICODE else "|"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def rule(title: str = "") -> None:
    width = min(shutil.get_terminal_size((80, 20)).columns, 72)
    bar = dim(RULE_CH * width)
    print(f"\n{bold(title)}\n{bar}" if title else bar)


# --------------------------------------------------------------------------- #
# Key file
# --------------------------------------------------------------------------- #

def _write_key(path: Path, key: str) -> int | None:
    """
    Write the key file 0600. Returns the mode actually on disk afterwards.

    The mode argument to `os.open` only applies when the file is CREATED, and
    this path overwrites - so an existing file kept its old permissions while
    we printed "0600". Unlinking first makes O_CREAT real; reading the mode
    back means we report what is true rather than what was intended.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"FORTYGUARD_API_KEY={key}\n")
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _report_mode(mode: int | None) -> None:
    if os.name == "nt":
        print(dim("  Windows: the file inherits the folder's permissions. It "
                  "sits under your user profile, which is not the same "
                  "guarantee as POSIX 0600."))
    elif mode is None:
        print(yellow("  Could not read the file's permissions back."))
    elif mode == 0o600:
        print(green("  Permissions: 0600 (owner read/write only)"))
    else:
        print(yellow(f"  Permissions: {mode:04o} - wider than 0600. "
                     f"Run: chmod 600 {default_config_dir() / '.env'}"))


def _prompt_key() -> str | None:
    """Prompt for a key. Never an argument: that leaks via ps and shell history."""
    key = getpass.getpass("FortyGuard API key (input hidden): ").strip()
    if not key:
        print(red("No key entered; nothing was written."))
        return None
    return key


def _store_key(key: str) -> Path | None:
    target = default_config_dir() / ".env"
    try:
        mode = _write_key(target, key)
    except OSError as e:
        print(red(f"Could not write {target}: {e}"))
        return None
    print(f"\n{green(TICK)} Stored {len(key)} characters in {bold(str(target))}")
    _report_mode(mode)
    return target


# --------------------------------------------------------------------------- #
# Live key check
# --------------------------------------------------------------------------- #

async def _check_key_async(key: str) -> tuple[bool, str]:
    from .client.errors import FortyGuardError
    from .client.http import FortyGuardHTTP
    from .config import Settings

    settings = Settings(api_key=key)
    try:
        async with FortyGuardHTTP(settings) as api:
            body = await api.usage()
    except FortyGuardError as e:
        return (False, str(e))
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")

    plan = credits = None
    if isinstance(body, dict):
        plan = (body.get("plan_details") or {}).get("plan_type")
        credits = (body.get("credit_summary") or {}).get("cycle_remaining_credits")
    parts = [p for p in (f"plan: {plan}" if plan else None,
                         f"credits remaining: {credits:,}"
                         if isinstance(credits, int) else None) if p]
    return (True, f" {DOT} ".join(parts) or "key accepted")


def check_key(key: str) -> bool:
    print("\nChecking the key against the API...", end=" ", flush=True)
    ok, detail = asyncio.run(_check_key_async(key))
    print(green("works") if ok else red("failed"))
    print(f"  {detail}")
    return ok


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _server_command() -> tuple[str, list[str]]:
    """How a client should launch this server, given how it was installed."""
    exe = shutil.which("fortyguard-mcp")
    if exe:
        return ("fortyguard-mcp", [])
    if shutil.which("uvx"):
        return ("uvx", ["fortyguard-mcp"])
    return (sys.executable, ["-m", "fortyguard_mcp"])


def config_block(include_key: str | None = None) -> dict[str, Any]:
    cmd, args = _server_command()
    entry: dict[str, Any] = {"command": cmd}
    if args:
        entry["args"] = args
    if include_key:
        entry["env"] = {"FORTYGUARD_API_KEY": include_key}
    return {"mcpServers": {"fortyguard": entry}}


def setup() -> int:
    """Guided setup. Interactive only."""
    if not sys.stdin.isatty():
        print(red("setup needs an interactive terminal."), file=sys.stderr)
        print("Use --set-key in a terminal, or set FORTYGUARD_API_KEY in your "
              "client config.", file=sys.stderr)
        return 2

    print(bold("\nFortyGuard MCP setup"))
    print(dim("Hyperlocal urban heat data for the United States.\n"))

    rule("1. API key")
    target = default_config_dir() / ".env"
    if target.exists():
        print(f"A key is already stored in {target}")
        if input("Replace it? [y/N] ").strip().lower() not in ("y", "yes"):
            print(dim("Keeping the existing key."))
            key = get_settings().key or ""
        else:
            key = _prompt_key() or ""
            if key and _store_key(key) is None:
                return 1
    else:
        print("Get a key from the FortyGuard dashboard, then paste it here.")
        print(dim("It is stored under your user profile, so your MCP client "
                  "config never has to contain a secret."))
        key = _prompt_key() or ""
        if not key:
            return 2
        if _store_key(key) is None:
            return 1

    rule("2. Check")
    if key:
        check_key(key)
    else:
        print(yellow("  No key available to check."))

    rule("3. Connect a client")
    found = detect_clients()
    block = config_block()
    if not found:
        print("No supported MCP client config was found on this machine.")
        print("\nAdd this to your client's MCP config:\n")
        print(json.dumps(block, indent=2))
    else:
        print("Found:")
        for i, t in enumerate(found, 1):
            state = green("already configured") if t.has_fortyguard() else dim("not configured")
            print(f"  {i}. {t.label:22} {state}")
        print(f"  {len(found) + 1}. none - just print the config\n")
        raw = input(f"Configure which? [1-{len(found) + 1}, or Enter to skip] ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(found):
            t = found[int(raw) - 1]
            try:
                backup = t.install(block["mcpServers"]["fortyguard"])
            except OSError as e:
                print(red(f"  Could not write {t.path}: {e}"))
                return 1
            print(f"\n{green(TICK)} Wrote {bold(str(t.path))}")
            if backup:
                print(dim(f"  Previous file backed up to {backup}"))
            print(dim(f"  Restart {t.label} to pick it up."))
        else:
            print("\nAdd this to your client's MCP config:\n")
            print(json.dumps(block, indent=2))

    rule("Done")
    print("Try asking your assistant:")
    print(dim('  "How hot is downtown Phoenix at 5am on 2024-07-15?"'))
    print(f"\nDiagnose problems with {bold('fortyguard-mcp --doctor')}.\n")
    return 0


def set_key() -> int:
    """Store the key and nothing else."""
    if not sys.stdin.isatty():
        print("--set-key needs an interactive terminal: the key is typed at a "
              "prompt, never passed as an argument, so it cannot leak through "
              "shell history or the process list.", file=sys.stderr)
        return 2

    target = default_config_dir() / ".env"
    if target.exists():
        print(f"{target} already exists and will be overwritten.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled; nothing was written.")
            return 1

    key = _prompt_key()
    if not key:
        return 2
    if _store_key(key) is None:
        return 1
    print("\nYour MCP client config now needs no API key in it at all.")
    return 0


def doctor() -> int:
    """Everything a support question would ask, answered up front."""
    print(bold("\nfortyguard-mcp --doctor\n"))
    problems: list[str] = []

    rule("Environment")
    print(f"  python           : {sys.version.split()[0]}")
    print(f"  platform         : {sys.platform}")
    cmd, args = _server_command()
    print(f"  launch command   : {' '.join([cmd, *args])}")

    rule("Configuration")
    try:
        settings = get_settings()
    except Exception as e:
        print(red(f"  settings failed to load: {e}"))
        print("\n  Fix that first - nothing else can be checked until it loads.")
        return 1
    print(f"  base_url         : {settings.base_url}")
    print(f"  data_dir         : {settings.data_dir}")
    print(f"  log_level        : {settings.log_level}")
    key = settings.key
    print(f"  api key          : {green('set') if key else red('MISSING')}"
          f"{dim(f' ({len(key)} chars)') if key else ''}")
    if not key:
        problems.append("No API key. Run: fortyguard-mcp setup")

    rule("Storage")
    for label, path in (("data dir", settings.data_dir),
                        ("results", settings.results_dir),
                        ("reports", settings.reports_dir)):
        ok, note = _probe_writable(path)
        print(f"  {label:16} : {green('writable') if ok else red(note)}  {dim(str(path))}")
        if not ok:
            problems.append(f"{path} is not writable: {note}")

    rule("API")
    if key:
        check_key(key)
    else:
        print(dim("  Skipped - no key."))

    rule("Clients")
    found = detect_clients()
    if not found:
        print(dim("  No supported MCP client config found."))
    for t in found:
        state = green("configured") if t.has_fortyguard() else yellow("not configured")
        print(f"  {t.label:22} {state}  {dim(str(t.path))}")

    rule("Summary")
    if problems:
        print(red(f"{len(problems)} problem(s):"))
        for p in problems:
            print(f"  - {p}")
        return 1
    print(green("No problems found."))
    return 0


def _probe_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".fortyguard-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return (True, "")
    except OSError as e:
        return (False, e.strerror or type(e).__name__)


def where() -> int:
    """Print every path this server reads or writes. No secrets, just paths."""
    settings = get_settings()
    print("config directory :", default_config_dir())
    print("key file         :", default_config_dir() / ".env")
    print("data directory   :", default_data_dir())
    print("results archive  :", settings.results_dir)
    print()
    print("key sources, in order of precedence (first wins):")
    print("  1. FORTYGUARD_API_KEY in the environment"
          f"  [{'set' if os.environ.get('FORTYGUARD_API_KEY') else 'not set'}]")
    explicit = os.environ.get("FORTYGUARD_ENV_FILE")
    if explicit:
        shown = Path(explicit).expanduser()
        print(f"  2. FORTYGUARD_ENV_FILE  [{shown}"
              f" - {'exists' if shown.exists() else 'MISSING'}]")
    else:
        print("  2. FORTYGUARD_ENV_FILE  [not set]")
    shared = default_config_dir() / ".env"
    print(f"  3. {shared}  [{'exists' if shared.exists() else 'missing'}]")
    print()
    print("The current directory is deliberately NOT searched.")
    print("key currently resolves:", "yes" if settings.key else "NO")
    return 0


def print_config() -> int:
    print(json.dumps(config_block(), indent=2))
    return 0


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #

def _serve(transport: Transport) -> None:
    """
    Start the server, turning startup failures into something readable.

    The stores mkdir at construction, so a bad FORTYGUARD_DATA_DIR or a
    read-only mount used to kill the process with a bare traceback before
    logging was even configured - the client just saw a server that died. The
    missing-key path a few lines below has always explained itself; this now
    does too.
    """
    from .server import main as _run
    try:
        _run(transport=transport)
    except OSError as e:
        settings = None
        with contextlib.suppress(Exception):
            settings = get_settings()
        where_ = settings.data_dir if settings else "(unresolved)"
        print(
            f"fortyguard-mcp: cannot start.\n"
            f"  {type(e).__name__}: {e}\n"
            f"  The data directory could not be prepared: {where_}\n"
            f"  Set FORTYGUARD_DATA_DIR to a writable path, or run "
            f"`fortyguard-mcp --doctor` to see exactly what failed.",
            file=sys.stderr,
        )
        raise SystemExit(1) from e


def main() -> None:
    args = sys.argv[1:]

    if args:
        # CLI mode: stdout is ours, because nothing is speaking JSON-RPC.
        cmd = args[0]
        if cmd in ("-h", "--help", "help"):
            print(USAGE)
            raise SystemExit(0)
        if cmd in ("setup", "--setup"):
            raise SystemExit(setup())
        if cmd == "--set-key":
            raise SystemExit(set_key())
        if cmd in ("--doctor", "doctor"):
            raise SystemExit(doctor())
        if cmd == "--where":
            raise SystemExit(where())
        if cmd == "--print-config":
            raise SystemExit(print_config())
        if cmd == "--version":
            from importlib.metadata import PackageNotFoundError, version
            try:
                print(version("fortyguard-mcp"))
            except PackageNotFoundError:
                print("unknown (not installed as a package)")
            raise SystemExit(0)
        if cmd == "--transport":
            if len(args) < 2 or args[1] not in TRANSPORTS:
                print("--transport needs one of: stdio, sse, streamable-http",
                      file=sys.stderr)
                raise SystemExit(2)
            if args[1] != "stdio":
                # Said plainly rather than buried in docs: everything about the
                # security posture here assumes one local user. Over a network
                # there is no authentication, no per-caller isolation and no
                # rate limiting, and the archive is shared by every caller.
                print(
                    f"fortyguard-mcp: WARNING - serving over {args[1]}.\n"
                    f"  This server has no authentication and no per-caller "
                    f"isolation. Anyone who can reach the port can spend your "
                    f"credits and read your stored results.\n"
                    f"  Bind it to loopback behind an authenticating proxy, or "
                    f"use stdio.",
                    file=sys.stderr,
                )
            _serve(args[1])
            return
        print(f"unknown argument: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    # Server mode. Warn on stderr - never stdout - if there is no key. The
    # server still starts: a client needs it running to list tools at all.
    try:
        settings = get_settings()
    except Exception as e:
        print(f"fortyguard-mcp: configuration is invalid and the server cannot "
              f"start.\n  {e}\n  Run `fortyguard-mcp --doctor` for details.",
              file=sys.stderr)
        raise SystemExit(1) from e

    if not settings.key:
        print(
            "fortyguard-mcp: no API key found.\n"
            "  Run `fortyguard-mcp setup` for guided setup, or set "
            "FORTYGUARD_API_KEY in the `env` block of your MCP client config.\n"
            "  Run `fortyguard-mcp --where` to see every path that was checked.\n"
            "  The server will start, but every API call will fail until a key "
            "is available.",
            file=sys.stderr,
        )
    _serve("stdio")


if __name__ == "__main__":
    main()
