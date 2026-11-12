"""The MCP server tools the person allowed, remembered.

An MCP tool that may change something is asked about before it runs (`policy.Guard`). In
the app, «Permitir» is an answer for that tool from then on, in every project and every
later session, so the same question is never asked twice (2026-10-30: "cuando acepto un
permiso, que debería ser global, me lo vuelve a pedir luego"). Without a file the answers
last as long as the guard, which is what `chat` and every benchmark task get.

Only tools are remembered, by how the person knows them ("server · tool"). A deletion or a
system package is never remembered here: those are asked every time.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class AllowedTools:
    """A set of tool labels, kept in ``path`` when one is given.

    The file is read again when it changed, so a tool allowed in one window's session is
    allowed in another's without a restart."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._labels: set[str] = set()
        self._stamp: tuple[int, int] | None = None

    def __contains__(self, label: object) -> bool:
        with self._lock:
            self._reload()
            return label in self._labels

    def add(self, label: str) -> None:
        with self._lock:
            self._reload()
            if label in self._labels:
                return
            self._labels.add(label)
            self._save()

    def _reload(self) -> None:
        if self._path is None:
            return
        try:
            stat = self._path.stat()
        except OSError:
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A file that cannot be read allows nothing more; the person is asked again.
            return
        tools = data.get("tools") if isinstance(data, dict) else None
        if isinstance(tools, list):
            self._labels |= {label for label in tools if isinstance(label, str)}
        self._stamp = stamp

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            spare = self._path.with_name(self._path.name + ".tmp")
            spare.write_text(
                json.dumps({"tools": sorted(self._labels)}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(spare, self._path)
            stat = self._path.stat()
            self._stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            # Not saved: the tool stays allowed for this guard, and is asked again later.
            pass
