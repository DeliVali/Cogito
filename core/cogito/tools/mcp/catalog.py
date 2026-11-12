"""Each MCP server's tool list from its last start, kept on disk (APP ONLY).

A conversation can then register a server's tools at once while the server (re)starts, and
a call to one waits for the start within its timeout. Codex does the same with
`startup_readiness = "catalog"`: "a valid cached tool catalog satisfy startup while the
server connects", and "tool calls still need a live connection" (its MCP documentation,
learn.chatgpt.com/docs/extend/mcp, read 2026-11-10).

A file is named by a hash of what starts or reaches the server (`hub._key`: command, args,
env, address, headers), so a changed command never reads an old list, and no value of the
key (an API key in `env`, a token in a header) is written anywhere. What is written is what
the server itself answered: its tools and its instructions. Per user, owner-only, and safe
to delete: a missing or unreadable file only means waiting for the start, as before.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cogito.config import paths
from cogito.tools.mcp.client import RemoteTool

#: Changed when what is written changes shape; an older file is then not read.
_VERSION = 1


@dataclass(frozen=True, slots=True)
class Listed:
    """What a server answered at its last start."""

    tools: tuple[RemoteTool, ...]
    instructions: str
    saved_at: float


class ToolCatalog:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or paths.cache_dir() / "mcp-tools"

    def _file(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self._directory / f"{digest}.json"

    def load(self, key: str) -> Listed | None:
        try:
            data = json.loads(self._file(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            return None
        tools = []
        for raw in data.get("tools") or ():
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            tools.append(
                RemoteTool(
                    name=str(raw["name"]),
                    description=str(raw.get("description") or ""),
                    input_schema=raw.get("input_schema") or {"type": "object"},
                    title=str(raw.get("title") or ""),
                    annotations=dict(raw.get("annotations") or {}),
                )
            )
        if not tools:
            return None
        return Listed(
            tools=tuple(tools),
            instructions=str(data.get("instructions") or ""),
            saved_at=float(data.get("saved_at") or 0.0),
        )

    def save(self, key: str, tools: Sequence[RemoteTool], instructions: str) -> None:
        """Written whole and moved into place, so a reader never sees half a file."""
        if not tools:
            return
        data = {
            "version": _VERSION,
            "saved_at": time.time(),
            "instructions": instructions,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.input_schema),
                    "title": tool.title,
                    "annotations": dict(tool.annotations),
                }
                for tool in tools
            ],
        }
        target = self._file(key)
        with contextlib.suppress(OSError, TypeError, ValueError):
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            partial = target.with_suffix(f".{os.getpid()}.tmp")
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(partial, target)
