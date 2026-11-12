"""Per-OS protected paths and package-ownership oracles."""

from __future__ import annotations

import sys
from functools import cache

from cogito.guard.platform.base import BasePlatform, Platform, Ruling, Zone

__all__ = ["BasePlatform", "Platform", "Ruling", "Zone", "current"]


@cache
def current() -> Platform:
    """The policy for the machine this is running on."""
    if sys.platform == "darwin":
        from cogito.guard.platform.macos import MacOSPlatform

        return MacOSPlatform()
    if sys.platform.startswith("win"):
        from cogito.guard.platform.windows import WindowsPlatform

        return WindowsPlatform()
    from cogito.guard.platform.linux import LinuxPlatform

    return LinuxPlatform()
