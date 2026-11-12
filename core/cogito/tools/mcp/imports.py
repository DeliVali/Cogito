"""MCP servers the person already configured for another client, to add to Cogito. APP ONLY.

Read-only: each client's file is opened, never written. Where each one keeps them, read
2026-11-10:

- Claude Code: `~/.claude.json`, `mcpServers` at the top (user scope) and under
  `projects[<path>]` (local scope), and the project's `.mcp.json` (code.claude.com/docs/en/mcp);
- Claude Desktop: `claude_desktop_config.json`, `mcpServers`, in its configuration folder
  (`~/.config/Claude` on Linux, `~/Library/Application Support/Claude` on macOS,
  `%APPDATA%\\Claude` on Windows);
- Cursor: `~/.cursor/mcp.json` and the project's `.cursor/mcp.json`, `mcpServers`, with
  variables as `${env:NAME}` (cursor.com/docs/context/mcp);
- Gemini CLI: `~/.gemini/settings.json` and the project's `.gemini/settings.json`,
  `mcpServers`, where `url` is SSE and `httpUrl` Streamable HTTP (its
  docs/tools/mcp-server.md);
- Codex: `~/.codex/config.toml`, `[mcp_servers.NAME]` with `command`, `args`, `env`,
  `env_vars`, `url`, `http_headers` and `bearer_token_env_var`
  (learn.chatgpt.com/docs/extend/mcp).

What the list shows names the variables and headers a server sets, never their values. What
the person ticks is copied into config.toml as the file holds it, values included: the
window says so before they add.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cogito.tools.mcp import project
from cogito.tools.mcp.connections import entry_spec, missing_values, safe_name, servers_in

_SECRET_FLAG = re.compile(r"(key|token|secret|password|passwd|auth|credential)", re.IGNORECASE)


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _desktop_config(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return base / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _table(data: Any, *keys: str) -> Mapping[str, Any]:
    for key in keys:
        data = data.get(key) if isinstance(data, Mapping) else None
    return data if isinstance(data, Mapping) else {}


def _codex(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    found = {}
    for name, server in _table(data, "mcp_servers").items():
        if not isinstance(server, Mapping):
            continue
        entry: dict[str, Any] = {}
        if server.get("url"):
            entry = {"type": "http", "url": str(server["url"])}
            headers = {str(k): str(v) for k, v in dict(server.get("http_headers") or {}).items()}
            if server.get("bearer_token_env_var"):
                headers["Authorization"] = "Bearer ${" + str(server["bearer_token_env_var"]) + "}"
            if headers:
                entry["headers"] = headers
        elif server.get("command"):
            env = {str(k): str(v) for k, v in dict(server.get("env") or {}).items()}
            # `env_vars` forwards these from Codex's own environment by name.
            for variable in server.get("env_vars") or ():
                env.setdefault(str(variable), "${" + str(variable) + "}")
            entry = {"command": server["command"], "args": list(server.get("args") or []), "env": env}
        if entry:
            with contextlib.suppress(ValueError):
                found[str(name)] = entry_spec(str(name), entry)
    return found


def _strings(value: Any, change) -> Any:
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [_strings(item, change) for item in value]
    if isinstance(value, Mapping):
        return {key: _strings(item, change) for key, item in value.items()}
    return value


def _cursor(table: Mapping[str, Any]) -> Mapping[str, Any]:
    """Cursor writes a variable as `${env:NAME}` (cursor.com/docs/context/mcp, read
    2026-11-10); Cogito reads `${NAME}`."""
    return _strings(table, lambda text: text.replace("${env:", "${"))


_BARE_VARIABLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _gemini(table: Mapping[str, Any]) -> Mapping[str, Any]:
    """Gemini CLI's `url` is the older SSE transport and `httpUrl` Streamable HTTP, and an
    `env` value may name a variable as `$NAME` (its docs/tools/mcp-server.md, read
    2026-11-10)."""
    changed = {}
    for name, server in table.items():
        if not isinstance(server, Mapping):
            continue
        server = dict(server)
        if server.get("url") and not server.get("httpUrl"):
            server.setdefault("type", "sse")
        if isinstance(server.get("env"), Mapping):
            server["env"] = {
                key: _BARE_VARIABLE.sub(r"${\1}", str(value)) for key, value in server["env"].items()
            }
        changed[name] = server
    return changed


def _sources(home: Path, project_root: Path | None) -> list[tuple[str, Path, str, dict[str, dict[str, Any]]]]:
    """(source, file, scope, servers) for every file that exists."""
    found: list[tuple[str, Path, str, dict[str, dict[str, Any]]]] = []
    claude = home / ".claude.json"
    data = _json(claude)
    if data is not None:
        found.append(("Claude Code", claude, "user", servers_in(_table(data, "mcpServers"))))
        if project_root is not None:
            local = _table(_table(data, "projects"), str(Path(project_root).resolve()))
            found.append(("Claude Code", claude, "user", servers_in(_table(local, "mcpServers"))))
    desktop = _desktop_config(home)
    if (data := _json(desktop)) is not None:
        found.append(("Claude Desktop", desktop, "user", servers_in(_table(data, "mcpServers"))))
    codex = home / ".codex" / "config.toml"
    cursor_files = [home / ".cursor" / "mcp.json"]
    gemini_files = [home / ".gemini" / "settings.json"]
    if project_root is not None:
        cursor_files.append(Path(project_root) / ".cursor" / "mcp.json")
        gemini_files.append(Path(project_root) / ".gemini" / "settings.json")
    for file in cursor_files:
        if (data := _json(file)) is not None:
            found.append(("Cursor", file, "user", servers_in(_cursor(_table(data, "mcpServers")))))
    for file in gemini_files:
        if (data := _json(file)) is not None:
            found.append(("Gemini CLI", file, "user", servers_in(_gemini(_table(data, "mcpServers")))))
    if codex.exists():
        found.append(("Codex", codex, "user", _codex(codex)))
    if project_root is not None and project.file_for(Path(project_root)).exists():
        root = Path(project_root)
        found.append(("Este proyecto", project.file_for(root), "project", project.specs(root)))
    return [entry for entry in found if entry[3]]


def shown_command(spec: Mapping[str, Any]) -> str:
    """The command or address as the list shows it: an argument after a flag whose name says
    it is a secret, or a `KEY=value` one, is shown as `…`."""
    if spec.get("url"):
        return str(spec["url"]).split("?", 1)[0]
    words = [str(spec.get("command", ""))]
    hide = False
    for arg in spec.get("args", ()):
        arg = str(arg)
        if hide:
            words.append("…")
            hide = False
        elif arg.startswith("-") and _SECRET_FLAG.search(arg):
            if "=" in arg:
                words.append(arg.split("=", 1)[0] + "=…")
            else:
                words.append(arg)
                hide = True
        elif "=" in arg and _SECRET_FLAG.search(arg.split("=", 1)[0]):
            words.append(arg.split("=", 1)[0] + "=…")
        else:
            words.append(arg)
    return " ".join(word if word.endswith("…") else shlex.quote(word) for word in words)


def discover(
    home: Path,
    project_root: Path | None,
    configured: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """What the import list shows: one row per server found, keyed `n:<index>:<name>` by
    source, with whether a server by that name is already in Cogito."""
    rows = []
    pending = set(project.pending(Path(project_root))) if project_root is not None else set()
    for index, (source, file, scope, servers) in enumerate(_sources(home, project_root)):
        for name, spec in servers.items():
            taken = safe_name(name) in configured
            if scope == "project":
                taken = name not in pending
            rows.append({
                "key": f"{index}:{name}",
                "source": source,
                "file": str(file),
                "scope": scope,
                "name": safe_name(name),
                "command": shown_command(spec),
                "env": sorted({**(spec.get("env") or {}), **(spec.get("headers") or {})}),
                "needs": missing_values(spec),
                "taken": taken,
            })
    return rows


def chosen(
    home: Path, project_root: Path | None, keys: list[str]
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """(key, name, scope, spec) of each ticked row, as its file holds it now."""
    picked = []
    wanted = set(keys)
    for index, (_source, _file, scope, servers) in enumerate(_sources(home, project_root)):
        for name, spec in servers.items():
            key = f"{index}:{name}"
            if key in wanted:
                picked.append((key, name, scope, dict(spec)))
    return picked
