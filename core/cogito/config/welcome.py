"""COGITO's welcome (design 1d-1e): the person's name, and whether the welcome is over.

Step 1 asks for a name; the projects screen (1f) then greets with it («¿En qué trabajamos
hoy, {nombre}?»). Step 2 connects a model. Once the person finishes step 2, or skips it
because a working model already existed, the welcome never shows again. The file is
``~/.config/cogito/welcome.toml``::

    name = "Ana"
    done = true

Kept out of ``config.toml`` on purpose: a benchmark's configuration digest reads that file
whole (cogito-lab ``bench/extend.py``, ``config_record``), and a name has nothing to do with
what a run measures. Global like ``last-model.toml``, never per project.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from cogito.config import paths
from cogito.config.tomlfile import quote, read, write_atomically
from cogito.errors import ConfigError

#: Long enough for any name a person gives; short enough that the 1f question stays one line.
MAX_NAME_CHARS = 60


@dataclass(frozen=True, slots=True)
class Welcome:
    name: str | None = None
    done: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "done": self.done}


def welcome_file() -> Path:
    return paths.config_dir() / "welcome.toml"


def clean_name(value: object) -> str | None:
    """A name as stored: trimmed, one line, no control characters, at most
    `MAX_NAME_CHARS`. None for an empty one. Raises `ConfigError` for a non-string value."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("name must be text")
    kept = "".join(
        " " if char in "\t\n\r" else char
        for char in value
        if unicodedata.category(char) not in ("Cc", "Cf") or char in "\t\n\r"
    )
    collapsed = " ".join(kept.split())
    return collapsed[:MAX_NAME_CHARS].strip() or None


def load(path: Path | None = None) -> Welcome:
    """The stored state, or the defaults when the file is absent or unreadable: showing the
    welcome once more does less harm than a window that cannot start."""
    try:
        document = read(path if path is not None else welcome_file())
    except (ConfigError, OSError):
        return Welcome()
    try:
        name = clean_name(document.get("name"))
    except ConfigError:
        name = None
    return Welcome(name=name, done=document.get("done") is True)


def save(state: Welcome, path: Path | None = None) -> Welcome:
    """Replace the file with ``state`` in one step and return what was written."""
    lines = ["# COGITO's welcome (design 1d–1e). Written by the app.\n"]
    if state.name:
        lines.append(f"name = {quote(state.name)}\n")
    lines.append(f"done = {'true' if state.done else 'false'}\n")
    write_atomically(path if path is not None else welcome_file(), "".join(lines))
    return state
