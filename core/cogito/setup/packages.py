"""Package managers: which are present, whose index knows a package, and what to type.

Cogito never installs a ``never`` component (a compiler, a language toolchain). It shows
the one command that would, for a package manager present on this machine whose index has
the package. The index is checked read-only, with a timeout, before the command is shown
(spec 2026-09-28, addendum). With no such manager the card shows the catalog's official link
instead (user decision, 2026-09-28).

Index checks only *read*: ``apt-cache policy``, ``dnf -C info`` (cache only, never a
metadata refresh), ``pacman -Si``, ``zypper --no-refresh info``, ``brew info``,
``port info``, ``winget show``, ``scoop info``, ``choco info``, ``flatpak search``,
``snap info``. A check that fails, times out or cannot run counts as "does not know it".
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from cogito.setup.components import Component, PackageRef, ProbeEnv

#: Which managers exist per OS, in the order they are tried (spec addendum).
MANAGER_ORDER: dict[str, tuple[str, ...]] = {
    "windows": ("winget", "scoop", "choco"),
    "darwin": ("brew", "port"),
    "linux": ("apt", "dnf", "pacman", "zypper", "flatpak", "snap"),
}

#: Index queries can reach a remote catalog (snap, choco, winget). Bounded so a slow one
#: costs only its own timeout.
INDEX_TIMEOUT_SECONDS = 10.0


def _rc_zero(completed: subprocess.CompletedProcess, _package: PackageRef) -> bool:
    return completed.returncode == 0


def _apt_knows(completed: subprocess.CompletedProcess, _package: PackageRef) -> bool:
    # `apt-cache policy` exits 0 either way. No output, or "Candidate: (none)", means no.
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            return stripped != "Candidate: (none)"
    return False


def _zypper_knows(completed: subprocess.CompletedProcess, _package: PackageRef) -> bool:
    return completed.returncode == 0 and "Information for package" in completed.stdout


def _flatpak_knows(completed: subprocess.CompletedProcess, package: PackageRef) -> bool:
    return completed.returncode == 0 and any(
        line.strip() == package.name for line in completed.stdout.splitlines()
    )


def _scoop_knows(completed: subprocess.CompletedProcess, _package: PackageRef) -> bool:
    return completed.returncode == 0 and "Could not find" not in completed.stdout


def _choco_knows(completed: subprocess.CompletedProcess, package: PackageRef) -> bool:
    # `--limit-output` prints "name|version" per match, nothing when there is none.
    return any(
        line.split("|", 1)[0].strip().lower() == package.name.lower()
        for line in completed.stdout.splitlines()
        if "|" in line
    )


@dataclass(frozen=True)
class Manager:
    id: str
    #: What must be on PATH for the manager to count as present (and what its command runs).
    binary: str
    #: The command the card shows, as argv, before the package name.
    install: tuple[str, ...]
    #: The read-only index query, as argv after the query binary, before the package name.
    query: tuple[str, ...]
    #: Reads the query's answer.
    knows: Callable[[subprocess.CompletedProcess, PackageRef], bool]
    #: The binary the query runs, when it differs from ``binary`` (apt vs apt-cache).
    query_binary_override: str | None = None
    #: Flags that go after the package name rather than before it (snap's --classic).
    flags_after_name: bool = False

    @property
    def query_binary(self) -> str:
        return self.query_binary_override or self.binary


MANAGERS: dict[str, Manager] = {
    manager.id: manager
    for manager in (
        Manager("winget", "winget", ("winget", "install"), ("show", "--id"), _rc_zero),
        Manager("scoop", "scoop", ("scoop", "install"), ("info",), _scoop_knows),
        Manager("choco", "choco", ("choco", "install"), ("info", "--limit-output"), _choco_knows),
        Manager("brew", "brew", ("brew", "install"), ("info",), _rc_zero),
        Manager("port", "port", ("sudo", "port", "install"), ("info",), _rc_zero),
        Manager("apt", "apt", ("sudo", "apt", "install"), ("policy",), _apt_knows, "apt-cache"),
        Manager("dnf", "dnf", ("sudo", "dnf", "install"), ("-C", "-q", "info"), _rc_zero),
        Manager("pacman", "pacman", ("sudo", "pacman", "-S"), ("-Si",), _rc_zero),
        Manager(
            "zypper",
            "zypper",
            ("sudo", "zypper", "install"),
            ("--non-interactive", "--no-refresh", "info"),
            _zypper_knows,
        ),
        Manager(
            "flatpak",
            "flatpak",
            ("flatpak", "install", "flathub"),
            ("search", "--columns=application"),
            _flatpak_knows,
        ),
        Manager(
            "snap", "snap", ("sudo", "snap", "install"), ("info",), _rc_zero, flags_after_name=True
        ),
    )
}

#: Arguments some queries need after the package name.
_QUERY_SUFFIX: dict[str, tuple[str, ...]] = {"winget": ("--exact", "--disable-interactivity")}


def command_for(manager_id: str, package: PackageRef) -> str:
    """The command a person would type, exactly as the card shows it."""
    manager = MANAGERS[manager_id]
    if manager.flags_after_name:
        words = (*manager.install, package.name, *package.flags)
    else:
        words = (*manager.install, *package.flags, package.name)
    return " ".join(words)


def _query_argv(manager: Manager, resolved: str, package: PackageRef) -> list[str]:
    # A Homebrew cask is queried as a cask; every other flag belongs to the install only.
    kind = (
        [flag for flag in package.flags if flag in {"--cask", "--formula"}]
        if manager.id == "brew"
        else []
    )
    return [resolved, *manager.query, *kind, package.name, *_QUERY_SUFFIX.get(manager.id, ())]


def present(os_key: str, env: ProbeEnv) -> tuple[str, ...]:
    """The managers on this machine, in the order they are tried."""
    return tuple(m for m in MANAGER_ORDER.get(os_key, ()) if env.which(MANAGERS[m].binary))


def index_knows(manager_id: str, package: PackageRef, env: ProbeEnv) -> bool:
    """Whether this manager's own index has ``package``. Read-only; False on any doubt."""
    manager = MANAGERS[manager_id]
    resolved = env.which(manager.query_binary)
    if not resolved:
        return False
    completed = replace(env, timeout=INDEX_TIMEOUT_SECONDS).execute(
        _query_argv(manager, resolved, package)
    )
    if completed is None:
        return False
    completed.stdout = completed.stdout or ""
    try:
        return manager.knows(completed, package)
    except Exception:  # an answer this parser did not expect is a "no", never a crash
        return False


@dataclass(frozen=True, slots=True)
class PackageHelp:
    """What the missing-program card shows for one program."""

    program: str
    name: str
    #: The manager whose command is shown, or None.
    manager: str | None
    command: str | None
    #: Only when no manager can install it (user decision 2026-09-28).
    official_url: str | None

    def as_event(self) -> dict[str, Any]:
        return {
            "id": self.program,
            "name": self.name,
            "manager": self.manager,
            "command": self.command,
            "official_url": self.official_url,
        }


def package_help(
    component: Component,
    env: ProbeEnv,
    os_key: str,
    cache: dict[tuple[str, str], bool] | None = None,
) -> PackageHelp:
    """The first present manager, in the OS's order, whose index knows the component's
    package. Otherwise no command and the official link. ``cache`` keeps index answers
    between calls (they do not change while the application runs)."""
    for manager_id in MANAGER_ORDER.get(os_key, ()):
        package = component.packages.get(manager_id)
        if package is None or not env.which(MANAGERS[manager_id].binary):
            continue
        key = (manager_id, package.name)
        if cache is not None and key in cache:
            known = cache[key]
        else:
            known = index_knows(manager_id, package, env)
            if cache is not None:
                cache[key] = known
        if known:
            return PackageHelp(
                component.id, component.name, manager_id, command_for(manager_id, package), None
            )
    return PackageHelp(component.id, component.name, None, None, component.official_url_for(os_key))
