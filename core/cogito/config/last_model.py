"""The model last chosen in the window's picker (design 1a, the model chip's menu).

`cogito serve` starts with it next time, so the person's choice survives an app restart
(an update, a quit). Only `serve` reads it: `cogito chat`, which benchmarks drive, always
runs the model it is given (`--model`) or the configured default. The file is
``~/.config/cogito/last-model.toml``::

    model = "qwen3-coder"

A choice the catalog no longer has is ignored, not an error: the entry may have been
removed from ``models.toml`` since.
"""

from __future__ import annotations

from pathlib import Path

from cogito.config import paths
from cogito.config.tomlfile import quote, read, write_atomically
from cogito.errors import ConfigError


def load(path: Path | None = None) -> str | None:
    """The model id last chosen, or None when there is none or the file cannot be read."""
    try:
        document = read(path if path is not None else paths.last_model_file())
    except (ConfigError, OSError):
        return None
    chosen = document.get("model")
    return chosen if isinstance(chosen, str) and chosen.strip() else None


def save(model_id: str, path: Path | None = None) -> None:
    """Record ``model_id`` as the one chosen, replacing the file in one step."""
    write_atomically(
        path if path is not None else paths.last_model_file(),
        f"# The model last chosen in COGITO's picker; `cogito serve` starts with it.\n"
        f"model = {quote(model_id)}\n",
    )
