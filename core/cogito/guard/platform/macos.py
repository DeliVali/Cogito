"""macOS: the system volume is read-only under SIP, and the list reflects that.

There is no package-manager oracle here, so the deny-list does all the work and is
broader. Untested on real hardware; see the risks section of the plan.
"""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path

from cogito.guard.platform.base import BasePlatform
from cogito.guard.platform.linux import PRIVILEGE_TOOLS, TERMINAL_STARTUP

SYSTEM_ROOTS: tuple[str, ...] = (
    "/System", "/Library", "/usr", "/bin", "/sbin", "/private/etc", "/private/var/db",
    "/Applications", "/Volumes/Macintosh HD",
)

USER_ROOTS: tuple[str, ...] = (
    ".ssh", ".gnupg",
    "Library/Keychains", "Library/LaunchAgents", "Library/Preferences",
    ".zshrc", ".bash_profile", ".profile",
    ".Trash",
)


class MacOSPlatform(BasePlatform):
    name = "macos"
    # The terminal tabs run the same shells here (app only, `Guard.ask_terminal`).
    terminal_startup = TERMINAL_STARTUP
    privilege_tools = PRIVILEGE_TOOLS

    def __init__(self, home: Path | None = None) -> None:
        self._home = home or Path.home()

    @functools.cached_property
    def _roots(self) -> tuple[tuple[Path, str], ...]:
        labelled = [(Path(root), "it belongs to the operating system") for root in SYSTEM_ROOTS]
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
