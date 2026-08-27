"""
Where the API key comes from — and, more importantly, where it does not.

`env_file=".env"` used to resolve against the CURRENT WORKING DIRECTORY. For a
package meant to be published that is not merely unreliable, it is a live
hazard: an MCP server is spawned wherever the client happens to be, so the
server would adopt an unrelated repository's `.env` - including
`FORTYGUARD_DATA_DIR`, silently redirecting the paid archive.

Both directions were reproduced before the change and are pinned below.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import fortyguard_mcp.config as config_module
from fortyguard_mcp.client.errors import MissingKeyError
from fortyguard_mcp.config import (
    Settings,
    env_files,
)

SRC = str(Path(__file__).resolve().parents[2] / "src")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    These tests are about resolution order, so start from nothing set.

    THE CONFIG DIRECTORY IS REDIRECTED TOO, and that part was missing. Four of
    these tests asserted `Settings().key == ""` or expected `require_key()` to
    raise — true only on a machine where nobody has ever run
    `fortyguard-mcp --set-key`, because that writes a real key to
    `default_config_dir()/.env` and `Settings` then finds it.

    So they passed on a fresh checkout and failed for anyone who had followed
    the README's own recommended setup, which is the worst possible split: the
    suite is green for the author and red for the user. Found the moment a key
    was actually stored, during the round-8 audit.

    A test that reads state from outside the repository is not hermetic, and
    "it passes on my machine" is precisely the claim a test exists to replace.
    """
    for name in ("FORTYGUARD_API_KEY", "FORTYGUARD_ENV_FILE",
                 "FORTYGUARD_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)

    # Patched in the config module, where `env_files()` and `require_key()` both
    # look it up at call time - so the redirect covers resolution AND the
    # message that reports which paths were checked.
    fake_config_dir = tmp_path / "config-home"
    fake_config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("fortyguard_mcp.config.default_config_dir",
                        lambda: fake_config_dir)


# --------------------------------------------------------------------------- #
# The hazard that motivated all of this
# --------------------------------------------------------------------------- #

def test_a_stray_env_in_the_working_directory_is_ignored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Launching inside someone else's repo must not hand us their key - nor
    redirect the archive, which is the part that loses paid data.
    """
    intruder = tmp_path / "someone-elses-repo"
    intruder.mkdir()
    (intruder / ".env").write_text(
        "FORTYGUARD_API_KEY=key-belonging-to-another-project\n"
        f"FORTYGUARD_DATA_DIR={tmp_path / 'wrong-place'}\n",
        encoding="utf-8")
    monkeypatch.chdir(intruder)

    settings = Settings()
    assert settings.key == ""
    assert "wrong-place" not in str(settings.data_dir)


def test_the_working_directory_is_not_among_the_searched_files() -> None:
    for path in env_files():
        assert path.is_absolute(), path
    assert Path(".env").resolve() not in [p.resolve() for p in env_files()]


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #

def test_process_environment_wins(monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Path) -> None:
    """The client's `env` block is authoritative - it is the MCP mechanism."""
    named = tmp_path / "named.env"
    named.write_text("FORTYGUARD_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("FORTYGUARD_ENV_FILE", str(named))
    monkeypatch.setenv("FORTYGUARD_API_KEY", "from-process-env")
    assert Settings().key == "from-process-env"


def test_an_explicitly_named_file_is_read(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:
    named = tmp_path / "named.env"
    named.write_text("FORTYGUARD_API_KEY=from-explicit-file\n", encoding="utf-8")
    monkeypatch.setenv("FORTYGUARD_ENV_FILE", str(named))
    assert Settings().key == "from-explicit-file"


def test_the_explicit_file_is_resolved_per_construction(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Computed on every construction, not once at class-definition time - so a
    variable set after import is still honoured.
    """
    assert Settings().key == ""
    named = tmp_path / "late.env"
    named.write_text("FORTYGUARD_API_KEY=set-after-import\n", encoding="utf-8")
    monkeypatch.setenv("FORTYGUARD_ENV_FILE", str(named))
    assert Settings().key == "set-after-import"


def test_a_missing_explicit_file_is_not_fatal(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORTYGUARD_ENV_FILE", str(tmp_path / "nope.env"))
    assert Settings().key == ""          # reports absence, does not crash


def test_explicit_arguments_still_beat_everything(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORTYGUARD_API_KEY", "from-process-env")
    assert Settings(api_key="explicit").key == "explicit"


# --------------------------------------------------------------------------- #
# The message when nothing is found
# --------------------------------------------------------------------------- #

def test_the_missing_key_error_names_every_path_checked() -> None:
    # MissingKeyError, not RuntimeError: it has to travel the FortyGuardError
    # path so the tool layer returns it as data with the message intact.
    with pytest.raises(MissingKeyError) as caught:
        Settings().require_key()
    message = str(caught.value)
    assert "FORTYGUARD_API_KEY" in message
    assert "FORTYGUARD_ENV_FILE" in message
    # Called THROUGH the module, so this reads the redirect `_clean_env`
    # installed. A `from ... import default_config_dir` binds the ORIGINAL
    # function, which would compare the real config path against a message built
    # from the fake one - the test would then only pass on a machine with no key
    # stored, which is the whole hazard this fixture removes.
    assert str(config_module.default_config_dir() / ".env") in message
    assert "--set-key" in message
    # And says plainly why the obvious place is not searched.
    assert "NOT searched" in message


def test_the_missing_key_error_carries_no_secret(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    named = tmp_path / "named.env"
    named.write_text("FORTYGUARD_API_KEY=super-secret-value\n", encoding="utf-8")
    monkeypatch.setenv("FORTYGUARD_ENV_FILE", str(named))
    settings = Settings()
    assert settings.key == "super-secret-value"
    # The path may be shown; the value never.
    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings)


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #

def run_cli(*args: str, stdin: str = "", env: dict[str, str] | None = None
            ) -> subprocess.CompletedProcess[str]:
    full = dict(os.environ)
    full.update({"PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"})
    for name in ("FORTYGUARD_API_KEY", "FORTYGUARD_ENV_FILE"):
        full.pop(name, None)
    full.update(env or {})
    return subprocess.run([sys.executable, "-m", "fortyguard_mcp", *args],
                          input=stdin, capture_output=True, text=True,
                          timeout=60, env=full)


def test_set_key_refuses_without_a_terminal() -> None:
    """
    Critical: in server mode stdin carries JSON-RPC. Reading it here would eat
    protocol traffic and store a JSON fragment as somebody's API key.
    """
    done = run_cli("--set-key", stdin="")
    assert done.returncode == 2
    assert "interactive terminal" in done.stderr
    assert done.stdout == ""


def test_where_reports_the_sources_without_leaking_a_key(
        tmp_path: Path) -> None:
    named = tmp_path / "named.env"
    named.write_text("FORTYGUARD_API_KEY=secret-in-a-file\n", encoding="utf-8")
    done = run_cli("--where", env={"FORTYGUARD_ENV_FILE": str(named)})
    assert done.returncode == 0
    assert "secret-in-a-file" not in done.stdout
    assert "key currently resolves: yes" in done.stdout
    assert "NOT searched" in done.stdout


def test_help_exits_cleanly() -> None:
    done = run_cli("--help")
    assert done.returncode == 0
    assert "--set-key" in done.stdout


def test_an_unknown_argument_is_refused_on_stderr() -> None:
    """Never silently start serving because an argument was misspelled."""
    done = run_cli("--srv")
    assert done.returncode == 2
    assert "unknown argument" in done.stderr
    assert done.stdout == ""
