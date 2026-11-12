"""Windows: the system directories and the per-user application data that matters.

Untested on real hardware; see the risks section of the plan.
"""

from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path

from cogito.guard.platform.base import BasePlatform


def _environment_paths() -> list[Path]:
    names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
    return [Path(value) for name in names if (value := os.environ.get(name))]


USER_ROOTS: tuple[str, ...] = (
    r"AppData\Roaming\Microsoft",
    r"AppData\Local\Microsoft",
    ".ssh",
    ".gnupg",
)


class WindowsPlatform(BasePlatform):
    name = "windows"

    def __init__(self, home: Path | None = None) -> None:
        self._home = home or Path.home()

    @functools.cached_property
    def _roots(self) -> tuple[tuple[Path, str], ...]:
        system = _environment_paths() or [Path(r"C:\Windows"), Path(r"C:\Program Files")]
        labelled = [(root, "it belongs to the operating system") for root in system]
        labelled += [
            (self._home / relative, "it is your credentials or account configuration")
            for relative in USER_ROOTS
        ]
        return tuple(labelled)

    def protected_roots(self) -> tuple[tuple[Path, str], ...]:
        return self._roots

    @functools.cached_property
    def _resolved_home(self) -> Path:
        try:
            return self._home.resolve()
        except (OSError, RuntimeError):
            return self._home.absolute()

    def home(self) -> Path | None:
        return self._resolved_home

    def scratch_roots(self) -> tuple[Path, ...]:
        return (Path(tempfile.gettempdir()),)
