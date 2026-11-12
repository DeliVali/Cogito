"""Which system the agent is on, and which package managers it has.

APP ONLY: `cli._build_engine` states these in the environment line for the desktop app, so
the model stops guessing the distribution. Read once, from the files and the PATH the OS
itself provides; nothing is run except macOS's `sw_vers`.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from cogito.guard.packages import MANAGERS, SUPPORTED_HELPERS

OS_RELEASE = Path("/etc/os-release")


def _os_release_name(text: str) -> str:
    """PRETTY_NAME, else NAME, from an os-release file's text. Empty if neither."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if equals and key in {"PRETTY_NAME", "NAME"}:
            values[key] = value.strip().strip('"').strip("'")
    return values.get("PRETTY_NAME") or values.get("NAME") or ""


def _macos_version() -> str:
    try:
        done = subprocess.run(
            ["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def os_label(
    *,
    system: str | None = None,
    os_release: Path = OS_RELEASE,
    mac_version: Callable[[], str] = _macos_version,
) -> str:
    """"CachyOS", "macOS 15.1", "Windows 11": the distribution or release by name."""
    system = system or platform.system()
    if system == "Linux":
        try:
            name = _os_release_name(os_release.read_text(encoding="utf-8"))
        except OSError:
            name = ""
        return name or "Linux"
    if system == "Darwin":
        version = mac_version()
        return f"macOS {version}".strip()
    if system == "Windows":
        return f"Windows {platform.release()}".strip()
    return system or sys.platform


def managers_on_path(which: Callable[[str], str | None] = shutil.which) -> tuple[str, ...]:
    """The system package managers found on PATH, in `packages.MANAGERS` order."""
    found = []
    for manager in MANAGERS:
        programs = ("apt", "apt-get") if manager == "apt" else (manager,)
        if manager == "dnf":
            programs = ("dnf", "dnf5", "yum")
        if any(which(program) for program in programs):
            found.append(manager)
    return tuple(found)


def aur_helpers_on_path(which: Callable[[str], str | None] = shutil.which) -> tuple[str, ...]:
    """The AUR helpers the installer can run (`paru`, `yay`) found on PATH."""
    return tuple(helper for helper in sorted(SUPPORTED_HELPERS) if which(helper))
