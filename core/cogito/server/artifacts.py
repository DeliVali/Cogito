"""``GET /sessions/{id}/artifacts/{session}/{artifact}``: the full output behind a tool
row, fetched only when a person asks for it.

A row's ``detail`` (`loop.detail`) carries a bounded preview and, when there is more, a
``ref`` of the form ``<session>/<artifact id>``: the directory and file stem the loop
wrote under the project's ``.cogito/artifacts/``. This reads that file back and nothing
else. Both ids must be plain (no separator, no dot, no path). They are joined under the
window's own project and resolved, and the read is refused unless the real file is a
regular file inside the real artifacts directory. A symlink is always refused, wherever
it points. Any session of the project may be named, since a row read back from history
can belong to an earlier run of the app.

The answer is ``{"text", "lines", "truncated"}``: ``lines`` is the file's line count,
``text`` its head up to `MAX_BYTES` in whole lines, and ``truncated`` whether the head is
less than the whole file.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from cogito.config import paths

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: The most one answer carries. A megabyte is already more than a thread can show.
MAX_BYTES = 1024 * 1024
_CHUNK = 1 << 16


def valid(owner: str, artifact: str) -> bool:
    return bool(ID.match(owner) and ID.match(artifact))


def read(project_root: Path, owner: str, artifact: str) -> dict[str, Any] | None:
    """The artifact's head and line count, or None when there is no such file to serve."""
    if not valid(owner, artifact):
        return None
    root = paths.project_dir(project_root) / "artifacts"
    candidate = root / owner / f"{artifact}.txt"
    try:
        if candidate.is_symlink() or (root / owner).is_symlink():
            return None
        real_root = root.resolve(strict=True)
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if real.parent.parent != real_root:
        return None
    try:
        descriptor = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        head = handle.read(MAX_BYTES + 1)
        newlines = head.count(b"\n")
        last = head[-1:] if head else b""
        while chunk := handle.read(_CHUNK):
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    lines = newlines + (1 if last and last != b"\n" else 0)
    truncated = len(head) > MAX_BYTES
    if truncated:
        cut = head.rfind(b"\n", 0, MAX_BYTES)
        head = head[: cut if cut > 0 else MAX_BYTES]
    return {
        "text": head.decode("utf-8", "replace"),
        "lines": lines,
        "truncated": truncated,
    }
