"""Linux: protected paths, and the package manager as an exact oracle.

The deny-list is a floor. The package manager says which package owns a file, which is a
fact, not a heuristic, and it covers paths no list would include.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from cogito.guard.platform.base import BasePlatform
from cogito.guard.trash import home_trash

#: Refused outright. `/bin`, `/lib` and `/lib64` are symlinks into `/usr` on a merged-usr
#: system. They are listed anyway because not every distribution has merged; paths are
#: resolved before matching either way.
SYSTEM_ROOTS: tuple[str, ...] = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32",
    "/boot", "/efi", "/etc", "/opt", "/srv", "/root",
    "/proc", "/sys", "/dev", "/run",
    "/var/lib", "/var/cache", "/var/log",
)

#: The user's own files, but losing them breaks the machine or the person, so they get the
#: same treatment. The refusal's wording says which is which: "your SSH keys" and "the
#: operating system" are different warnings.
USER_ROOTS: tuple[str, ...] = (
    ".ssh", ".gnupg", ".pki",
    ".config/systemd", ".local/share/systemd",
    ".config/fish", ".bashrc", ".bash_profile", ".zshrc", ".profile",
    ".local/share/Trash",
    ".local/share/uv", ".cargo", ".rustup", ".nvm",
)

#: APP ONLY (`Guard.ask_terminal`): what the person's terminal tabs load when one opens.
#: A line the person approves runs in a new tab so that nothing an earlier line left can
#: change it; a file planted here would. Refused, never asked about, and only where the
#: terminal tabs exist. The last five are in USER_ROOTS too, which refuses every write the
#: scanner models before this list is read; they are here for the writes it does not model,
#: such as a download saved with ``curl -o``.
TERMINAL_STARTUP: tuple[str, ...] = (
    ".zshenv", ".zprofile", ".zlogin", ".zlogout",
    ".inputrc", ".bash_aliases", ".bash_login", ".bash_logout",
    ".local/share/fish/vendor_conf.d", ".local/share/fish/vendor_functions.d",
    ".local/share/fish/vendor_completions.d",
    ".bashrc", ".bash_profile", ".zshrc", ".profile", ".config/fish",
)

#: APP ONLY: names a program that asks for the person's password takes. A file by one of
#: these names, wherever it is, could run in place of the real one at the card where the
#: person types their password: a startup file can put any folder first on PATH.
PRIVILEGE_TOOLS: frozenset[str] = frozenset({"sudo", "doas", "su", "pkexec", "run0", "passwd"})

#: query -> the command that answers "which package owns this file".
_ORACLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pacman", ("pacman", "-Qo")),   # Arch, CachyOS
    ("dpkg", ("dpkg", "-S")),        # Debian, Ubuntu
    ("rpm", ("rpm", "-qf")),         # Fedora, RHEL
)


class LinuxPlatform(BasePlatform):
    name = "linux"
    terminal_startup = TERMINAL_STARTUP
    privilege_tools = PRIVILEGE_TOOLS

    def __init__(self, home: Path | None = None) -> None:
        self._home = home or Path.home()

    @functools.cached_property
    def _oracle(self) -> tuple[str, ...] | None:
        for binary, command in _ORACLES:
            if shutil.which(binary):
                return command
        return None

    @functools.cached_property
    def _roots(self) -> tuple[tuple[Path, str], ...]:
        labelled = [(Path(root), "it belongs to the operating system") for root in SYSTEM_ROOTS]
        labelled += [
            (self._home / relative, "it is your credentials, shell or toolchain configuration")
            for relative in USER_ROOTS
        ]
        # The trash itself, wherever XDG_DATA_HOME puts it: what Cogito deleted lives there.
        trash = home_trash()
        if trash != self._home / ".local/share/Trash":
            labelled.append((trash, "it is your trash, where deleted files can be restored from"))
        resolved: list[tuple[Path, str]] = []
        for root, description in labelled:
            try:
                resolved.append((root.resolve(), description))
            except (OSError, RuntimeError):
                resolved.append((root, description))
        return tuple(resolved)

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
        return (Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir()))

    @functools.lru_cache(maxsize=2048)  # noqa: B019 — bounded, and the answer cannot change
    def _ask(self, path: str) -> str | None:
        command = self._oracle
        if command is None:
            return None
        try:
            # The parse below reads the C locale's wording. Under es_ES pacman says
            # "/tmp/ está contenido en filesystem ...", which fell through to the rpm
            # branch and put that whole sentence in the refusal as a package name.
            result = subprocess.run(
                [*command, path], capture_output=True, text=True, timeout=10,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        first = result.stdout.strip().splitlines()[0]
        # pacman: "/usr/bin/git is owned by git 2.55.0-1"
        if " is owned by " in first:
            return first.split(" is owned by ", 1)[1].strip()
        # dpkg: "git: /usr/bin/git"
        if ": " in first and first.split(": ", 1)[1].strip().startswith("/"):
            return first.split(": ", 1)[0].strip()
        # rpm: "git-2.55.0-1.x86_64"
        return first.strip() or None

    def package_owner(self, path: Path) -> str | None:
        if not path.exists():
            return None
        return self._ask(str(path))
