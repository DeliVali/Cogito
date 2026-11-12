"""A project's own MCP servers, in its `.mcp.json` (Claude Code's project scope). APP ONLY.

Claude Code keeps project servers in `.mcp.json` at the project's root, as
`{"mcpServers": {"NAME": {...}}}`, and "prompts for approval in interactive sessions before
using project-scoped servers from `.mcp.json` files" (code.claude.com/docs/en/mcp, read
2026-11-10). A cloned repository can carry that file, and a server is a program run on
this machine, so Cogito starts only the entries the person approved, each as it was when
approved: a changed command needs approval again. The approvals are kept per user, outside
the project (`paths.data_dir()`), so the repository cannot approve itself.

An entry is approved by the person's own «Permitir» on `mcp_add` with the project scope, or
by adding it from Conexiones' import list.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cogito.config import paths
from cogito.config.settings import McpServerSettings
from cogito.tools.mcp.connections import server_settings, servers_in

FILE_NAME = ".mcp.json"


def file_for(project_root: Path) -> Path:
    return Path(project_root) / FILE_NAME


def _approvals_file() -> Path:
    return paths.data_dir() / "mcp-project-servers.json"


def _fingerprint(entry: Mapping[str, Any]) -> str:
    """What the entry runs, hashed: no value of it is written to the approvals file."""
    text = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw_entries(project_root: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(file_for(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    table = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return {}
    return {str(k): v for k, v in table.items() if isinstance(v, dict)}


def specs(project_root: Path) -> dict[str, dict[str, Any]]:
    """Every server the project's `.mcp.json` names, approved or not, as `entry_spec`
    reads each."""
    return servers_in(_raw_entries(project_root))


def _load_approvals() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_approvals_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def approve(project_root: Path, name: str) -> None:
    """The entry as the project's file holds it now is the person's to run."""
    raw = _raw_entries(project_root).get(name)
    if raw is None:
        return
    approvals = _load_approvals()
    approvals.setdefault(str(Path(project_root).resolve()), {})[name] = _fingerprint(raw)
    target = _approvals_file()
    with contextlib.suppress(OSError):
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(approvals, handle, indent=1, sort_keys=True)
        os.replace(partial, target)


def approved(project_root: Path) -> dict[str, McpServerSettings]:
    """The project's servers the person approved, unchanged since."""
    granted = _load_approvals().get(str(Path(project_root).resolve()), {})
    raw = _raw_entries(project_root)
    found = specs(project_root)
    return {
        name: server_settings(spec)
        for name, spec in found.items()
        if name in raw and granted.get(name) == _fingerprint(raw[name])
    }


def pending(project_root: Path) -> list[str]:
    """The project's servers not approved (or changed since they were)."""
    granted = _load_approvals().get(str(Path(project_root).resolve()), {})
    raw = _raw_entries(project_root)
    return sorted(name for name in raw if granted.get(name) != _fingerprint(raw[name]))


def write(project_root: Path, name: str, spec: Mapping[str, Any]) -> None:
    """Add the server to the project's `.mcp.json` in Claude Code's shape, keeping every
    other key and server. ValueError when the name is taken or the file is not JSON."""
    target = file_for(project_root)
    data: dict[str, Any] = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"{target} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{target} is not a JSON object")
    table = data.setdefault("mcpServers", {})
    if not isinstance(table, dict):
        raise ValueError(f"{target} has an mcpServers that is not an object")
    if name in table:
        raise ValueError(f"an MCP server named {name!r} already exists in {target}")
    if spec.get("url"):
        entry: dict[str, Any] = {"type": spec.get("type", "http"), "url": spec["url"]}
        if spec.get("headers"):
            entry["headers"] = dict(spec["headers"])
    else:
        entry = {"command": spec.get("command", ""), "args": list(spec.get("args", ()))}
        if spec.get("env"):
            entry["env"] = dict(spec["env"])
    table[name] = entry
    partial = target.with_suffix(f".{os.getpid()}.tmp")
    partial.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(partial, target)
