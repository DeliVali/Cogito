"""Running an approved package request, without the agent ever seeing a password.

APP ONLY, after the person approved a `PackageInstall` (`guard/packages.py`): an install, a
full system upgrade, or an install through an AUR helper. The agent's own
`sudo` could never work here: its shell has no terminal to ask on, and the password must not
pass through the model anyway. So the core runs the install itself:

- Linux: `pkexec <manager> ...`. polkit shows the operating system's own password dialog
  (KDE Plasma, GNOME and others run an agent for the session); the password goes from the
  person to polkit, never through this process. `--disable-internal-agent` keeps pkexec from
  falling back to a text prompt on a child that has no terminal. As root, no pkexec.
- macOS: `brew install`, as the user (Homebrew refuses to run as root).
- Windows: `winget install`, which asks for elevation itself.
- AUR (paru, yay): as the user, never through pkexec, because a helper refuses to run as
  root and calls `sudo` itself for the pacman steps. `--sudo /usr/bin/pkexec` replaces that
  sudo (paru.8 and yay.8: "The command to use for sudo calls", read 2026-10-12), so each
  privileged step shows the polkit dialog, and the dialog may appear more than once. The
  prompts are preset (`--noconfirm`, and `--skipreview` for paru; `--answerclean/diff/edit/
  upgrade None` for yay): the person approved the package names, not a PKGBUILD review.

The manager is resolved from the system's own directories, not the user's PATH, because
pkexec runs it as root. The flags that keep it from asking questions are added here, not
taken from the model's command.

How pkexec ends, from its man page (man.archlinux.org/man/pkexec.1, read 2026-10-12): 126
when the person dismissed the dialog, 127 when authorization could not be obtained or it
failed to run the program.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from cogito.cancel import CancelToken
from cogito.config import paths
from cogito.guard.packages import AUR_INSTALL, INSTALL, SYSTEM_UPGRADE, PackageInstall

#: Where a manager that pkexec will run as root is looked for.
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: An install may wait on the person's password and then download; this bounds a hang.
INSTALL_TIMEOUT_SECONDS = 3600.0

#: How much of the manager's output the model is shown (the end of it).
_TAIL_CHARS = 2000

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")

#: Manager id -> the program names to look for, first found wins.
_PROGRAMS: dict[str, tuple[str, ...]] = {
    "pacman": ("pacman",),
    "apt": ("apt-get",),
    "dnf": ("dnf", "dnf5", "yum"),
    "zypper": ("zypper",),
    "apk": ("apk",),
    "brew": ("brew",),
    "winget": ("winget",),
}

STATUS_INSTALLED = "installed"
STATUS_CANCELLED = "cancelled"
STATUS_UNAUTHORIZED = "unauthorized"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    ok: bool
    #: One of the STATUS_* values.
    status: str
    #: What the model reads. It says what happened and what to do next.
    text: str
    exit_code: int = -1
    #: The command that ran, for the record. Never holds a secret: there is none.
    argv: tuple[str, ...] = field(default_factory=tuple)


#: Runs ``argv`` and returns (exit code, output). Raises nothing for a failing program.
Runner = Callable[[Sequence[str], Mapping[str, str], CancelToken | None, float], tuple[int, str]]


def run_process(
    argv: Sequence[str], env: Mapping[str, str], cancel: CancelToken | None, timeout: float
) -> tuple[int, str]:
    """Run ``argv`` with no stdin, output merged. -2 on a timeout, -3 when cancelled."""
    process = subprocess.Popen(
        list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=dict(env), text=True, errors="replace",
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    code = -2
    output = ""
    while True:
        try:
            output, _ = process.communicate(timeout=0.25)
            code = process.returncode
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.cancelled:
                code = -3
            elif time.monotonic() >= deadline:
                code = -2
            else:
                continue
            process.kill()
            output, _ = process.communicate()
            break
    return code, output or ""


def _tail(output: str) -> str:
    text = _ANSI.sub("", output).strip()
    return text[-_TAIL_CHARS:]


def _find(manager: str, which: Callable[..., str | None], os_name: str) -> str | None:
    for program in _PROGRAMS[manager]:
        # Homebrew and winget live where the person's PATH puts them; the rest are run as
        # root and are looked up in the system's own directories.
        user_level = manager in {"brew", "winget"} or os_name != "linux"
        found = which(program) if user_level else which(program, path=SYSTEM_PATH)
        if found:
            return found
    return None


def _manual(install: PackageInstall) -> str:
    """The command the person can type themselves."""
    names = " ".join(install.packages)
    if install.kind == AUR_INSTALL:
        return f"{install.manager} -S {names}"
    if install.kind == SYSTEM_UPGRADE:
        return {
            "pacman": "sudo pacman -Syu",
            "apt": f"sudo apt {'full-upgrade' if install.variant == 'dist-upgrade' else 'upgrade'}",
            "dnf": "sudo dnf upgrade",
            "zypper": f"sudo zypper {'dup' if install.variant == 'dist-upgrade' else 'up'}",
            "apk": "doas apk upgrade",
            "brew": "brew upgrade",
            "winget": "winget upgrade --all",
        }[install.manager]
    return {
        "pacman": f"sudo pacman -S {names}",
        "apt": f"sudo apt install {names}",
        "dnf": f"sudo dnf install {names}",
        "zypper": f"sudo zypper install {names}",
        "apk": f"doas apk add {names}",
        "brew": f"brew install {names}",
        "winget": f"winget install {names}",
    }[install.manager]


_WINGET_QUIET = ["--silent", "--accept-package-agreements", "--accept-source-agreements",
                 "--disable-interactivity"]

#: The prompts an AUR helper would otherwise stop on, preset.
_AUR_FLAGS = {
    "paru": ["--noconfirm", "--skipreview"],
    "yay": ["--noconfirm", "--answerclean", "None", "--answerdiff", "None", "--answeredit",
            "None", "--answerupgrade", "None"],
}


def _aur_steps(
    install: PackageInstall, *, os_name: str, which: Callable[..., str | None], is_root: bool
) -> tuple[list[list[str]], str]:
    helper = which(install.manager)
    if os_name != "linux":
        return [], "AUR helpers exist only on Linux"
    if helper is None:
        return [], f"{install.manager} was not found, so nothing can be installed with it"
    if is_root:
        return [], (
            f"AUR helpers refuse to run as root, and this session runs as root, so "
            f"{install.manager} cannot be used"
        )
    elevator = which("pkexec", path=SYSTEM_PATH)
    if elevator is None:
        return [], (
            "pkexec (polkit) is not installed, so the system password dialog cannot be shown"
        )
    command = [helper, "-S", *_AUR_FLAGS[install.manager], "--sudo", elevator, *install.packages]
    return [command], ""


def build_steps(
    install: PackageInstall,
    *,
    os_name: str,
    which: Callable[..., str | None],
    is_root: bool,
) -> tuple[list[list[str]], str]:
    """The commands that run the request, and "" or why it cannot run on this machine.

    One command per step: winget installs one package at a time, and every other manager
    takes them all, so a single password dialog covers the whole request.
    """
    if install.kind == AUR_INSTALL:
        return _aur_steps(install, os_name=os_name, which=which, is_root=is_root)
    program = _find(install.manager, which, os_name)
    if program is None:
        return [], (
            f"{install.manager} was not found in the system's directories, so nothing can "
            f"be installed with it"
        )
    names = list(install.packages)
    manager = install.manager
    upgrading = install.kind == SYSTEM_UPGRADE
    if manager == "winget":
        if upgrading:
            return [[program, "upgrade", "--all", *_WINGET_QUIET]], ""
        return [
            [program, "install", "--exact", *_WINGET_QUIET, name] for name in names
        ], ""
    if manager == "brew":
        return [[program, "upgrade" if upgrading else "install", *names]], ""
    if manager == "pacman":
        command = [program, "-Syu" if upgrading else "-S", "--noconfirm", *names]
    elif manager == "apt":
        env_program = which("env", path=SYSTEM_PATH)
        sh = which("sh", path=SYSTEM_PATH)
        if env_program is None or sh is None:
            return [], "env and sh were not found in the system's directories"
        # One elevated call for the refresh and the request, so one dialog covers both.
        verb = install.variant or "upgrade" if upgrading else "install"
        command = [
            env_program, "DEBIAN_FRONTEND=noninteractive", sh, "-c",
            f'{program} update && {program} {verb} -y{"" if upgrading else " \"$@\""}',
            "sh", *names,
        ]
    elif manager == "dnf":
        command = [program, "upgrade" if upgrading else "install", "-y", *names]
    elif manager == "zypper":
        verb = (install.variant or "update") if upgrading else "install"
        command = [program, "--non-interactive", verb, *names]
    else:  # apk
        command = [program, "upgrade" if upgrading else "add", "--update-cache", *names]
    if is_root:
        return [command], ""
    elevator = which("pkexec", path=SYSTEM_PATH)
    if elevator is None:
        return [], (
            "pkexec (polkit) is not installed, so the system password dialog cannot be shown"
        )
    return [[elevator, "--disable-internal-agent", *command]], ""


def _words(install: PackageInstall) -> tuple[str, str, str]:
    """(done, not done, what it needs) for the texts the model reads."""
    if install.kind == SYSTEM_UPGRADE:
        return "upgraded", "not upgraded", "the upgrade"
    return "installed", "not installed", ", ".join(install.packages)


def install_packages(
    install: PackageInstall,
    cancel: CancelToken | None = None,
    *,
    runner: Runner | None = None,
    which: Callable[..., str | None] | None = None,
    os_name: str | None = None,
    is_root: bool | None = None,
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> InstallOutcome:
    """Run the request the person approved and say what happened, for the model."""
    runner = runner or run_process
    which = which or shutil.which
    system = os_name or ("windows" if sys.platform.startswith("win") else sys.platform)
    os_name = "linux" if system.startswith("linux") else system
    root = (hasattr(os, "geteuid") and os.geteuid() == 0) if is_root is None else is_root
    done, undone, _ = _words(install)
    steps, problem = build_steps(install, os_name=os_name, which=which, is_root=root)
    if problem:
        return InstallOutcome(
            False, STATUS_UNAVAILABLE,
            f"{undone}: {problem}. Nothing was changed. Tell the person to run "
            f"`{_manual(install)}` themselves.",
        )
    env = paths.user_environment()
    if install.manager == "brew":
        env["HOMEBREW_NO_ENV_HINTS"] = "1"
    elevated = bool(steps[0]) and steps[0][0].rsplit("/", 1)[-1] == "pkexec"
    last: tuple[int, str] = (0, "")
    for argv in steps:
        code, output = runner(argv, env, cancel, timeout)
        last = (code, output)
        if code != 0:
            return _failure(install, argv, code, output, elevated)
    if install.kind == SYSTEM_UPGRADE:
        what = f"the system with {install.manager} ({install.source})"
    elif install.kind == AUR_INSTALL:
        what = f"{', '.join(install.packages)} from the AUR with {install.manager}"
    else:
        what = f"{', '.join(install.packages)} with {install.manager} ({install.source})"
    return InstallOutcome(
        True, STATUS_INSTALLED, f"{done}: {what}.\n{_tail(last[1])}".rstrip(),
        exit_code=0, argv=tuple(steps[-1]),
    )


def _helper_status(code: int, output: str) -> int:
    """An AUR helper hides pkexec's exit code behind its own, so read pkexec's words."""
    lowered = output.lower()
    if "request dismissed" in lowered:
        return 126
    if "not authorized" in lowered or "error getting authority" in lowered:
        return 127
    return code


def _failure(
    install: PackageInstall, argv: Sequence[str], code: int, output: str, elevated: bool,
) -> InstallOutcome:
    _, undone, needs = _words(install)
    detail = _tail(output)
    tail = f"\n{detail}" if detail else ""
    manual = _manual(install)
    if install.kind == AUR_INSTALL:
        code, elevated = _helper_status(code, output), True
    if code == -3:
        return InstallOutcome(
            False, STATUS_INTERRUPTED,
            f"{undone}: the turn was stopped before {install.manager} finished. The "
            f"system may be half way through; do not retry unless the person asks.{tail}",
            code, tuple(argv),
        )
    if code == -2:
        return InstallOutcome(
            False, STATUS_TIMEOUT,
            f"{undone}: {install.manager} did not finish in time and was stopped.{tail}",
            code, tuple(argv),
        )
    if elevated and code == 126:
        return InstallOutcome(
            False, STATUS_CANCELLED,
            f"{undone}: the person closed the system password dialog, so nothing was "
            f"changed. Do not ask again on your own; tell them `{manual}` is what {needs} "
            f"needs.",
            code, tuple(argv),
        )
    if elevated and code == 127:
        return InstallOutcome(
            False, STATUS_UNAUTHORIZED,
            f"{undone}: the system did not authorize it (no password dialog could be shown "
            f"in this session, or the request was refused). Nothing was changed. Tell the "
            f"person to run `{manual}` themselves.{tail}",
            code, tuple(argv),
        )
    hint = (
        "Read the message above: a name that is not in the official repositories is the "
        "usual cause."
        if install.kind == INSTALL
        else "Read the message above."
    )
    return InstallOutcome(
        False, STATUS_FAILED,
        f"{undone}: {install.manager} exited {code}.{tail}\n{hint}",
        code, tuple(argv),
    )
