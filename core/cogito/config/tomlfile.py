"""The little TOML writing Cogito does, in one place.

The standard library reads TOML but does not write it. The files Cogito writes are small
and flat (mostly tables of strings), so a quoting function and an atomic replace cover them
without a dependency.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from cogito.config import paths
from cogito.errors import ConfigError

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def quote(value: str) -> str:
    """A TOML basic string for ``value``, with every character TOML requires escaped."""
    out = []
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def read(path: Path) -> dict[str, Any]:
    """The parsed document, or ``{}`` when the file does not exist. An unparseable file is
    an error, never an empty document: a caller that then wrote would destroy what it could
    not read.
    """
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc


def write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, so a crash never leaves half a file.

    The text is parsed first, so nothing that would fail to load again is written.
    """
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - a bug in the caller
        raise ConfigError(f"refusing to write invalid TOML to {path}: {exc}") from exc
    paths.ensure(path.parent)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            # On disk before it replaces the old file, so a power cut after the rename
            # cannot leave an empty file in its place.
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    sync_directory(path.parent)


def sync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` durable, where the system allows it (POSIX)."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
