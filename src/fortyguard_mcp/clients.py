"""
Where MCP clients keep their server config, and how to add an entry safely.

Only clients whose config is a documented JSON file are listed. Detection is
"does the file or its parent directory exist" - never a process scan.

Writing is conservative: the existing file is read, one key is added under
`mcpServers`, everything else is preserved byte-for-byte where possible, and a
timestamped backup is taken first. A user's client config usually holds other
servers, and losing those would be a far worse outcome than not writing at all.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _home() -> Path:
    return Path.home()


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or _home() / "AppData/Roaming")


def _claude_desktop() -> Path:
    if sys.platform == "darwin":
        return _home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if os.name == "nt":
        return _appdata() / "Claude/claude_desktop_config.json"
    return _home() / ".config/Claude/claude_desktop_config.json"


def _vscode_user_dir() -> Path:
    if sys.platform == "darwin":
        return _home() / "Library/Application Support/Code/User"
    if os.name == "nt":
        return _appdata() / "Code/User"
    return _home() / ".config/Code/User"


@dataclass(frozen=True, slots=True)
class ClientTarget:
    label: str
    path: Path
    # Where the server map lives in that file. Clients disagree: most use a
    # top-level "mcpServers", VS Code nests it under "mcp"."servers".
    container: tuple[str, ...] = ("mcpServers",)

    def exists(self) -> bool:
        return self.path.exists() or self.path.parent.is_dir()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def servers(self) -> dict[str, Any]:
        node: Any = self._load()
        for key in self.container:
            if not isinstance(node, dict):
                return {}
            node = node.get(key)
        return node if isinstance(node, dict) else {}

    def has_fortyguard(self) -> bool:
        return "fortyguard" in self.servers()

    def install(self, entry: dict[str, Any]) -> Path | None:
        """
        Add or replace the `fortyguard` entry. Returns the backup path, if any.

        Raises OSError on a write failure; the caller reports it. An unreadable
        or absent config is treated as empty rather than as an error, so a first
        run on a fresh machine still works.
        """
        data = self._load()
        backup: Path | None = None
        if self.path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            backup = self.path.with_suffix(self.path.suffix + f".{stamp}.bak")
            shutil.copy2(self.path, backup)

        node = data
        for key in self.container[:-1]:
            nxt = node.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                node[key] = nxt
            node = nxt
        leaf = self.container[-1]
        servers = node.get(leaf)
        if not isinstance(servers, dict):
            servers = {}
            node[leaf] = servers
        servers["fortyguard"] = entry

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        return backup


def _targets() -> list[ClientTarget]:
    return [
        ClientTarget("Claude Desktop", _claude_desktop()),
        ClientTarget("Claude Code", _home() / ".claude.json"),
        ClientTarget("Cursor", _home() / ".cursor/mcp.json"),
        ClientTarget("Windsurf", _home() / ".codeium/windsurf/mcp_config.json"),
        ClientTarget("VS Code", _vscode_user_dir() / "settings.json",
                     container=("mcp", "servers")),
    ]



def detect_clients() -> list[ClientTarget]:
    """Every supported client that appears to be installed."""
    return [t for t in _targets() if t.exists()]
