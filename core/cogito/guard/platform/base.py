"""What a platform treats as untouchable, and how to ask it.

guard/ enforces two rules: never delete without the user's approval, and never delete or
modify what belongs to the operating system. The second needs a per-platform answer. The
paths differ, and so do the ways to ask the system instead of guessing from a list.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class Zone(StrEnum):
    """Where a path sits, which decides how much ceremony touching it needs."""

    #: Inside the project being worked on, or scratch space. Deleting asks once.
    PROJECT = "project"
    #: The user's own files, outside the project. Deleting asks, and says where.
    HOME = "home"
    #: Owned by the operating system or its package manager. Refused without asking.
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Ruling:
    zone: Zone
    #: Why, in words the user will read in a prompt. Never a rule name.
    reason: str
    #: Set when a package manager was able to say which package owns the path.
    owner: str | None = None


class Platform(Protocol):
    name: str

    def protected_roots(self) -> tuple[tuple[Path, str], ...]:
        """Paths refused outright, each with what it is, already resolved."""
        ...

    def scratch_roots(self) -> tuple[Path, ...]:
        """Temporary space. Deleting here is ordinary work, not a decision."""
        ...

    def package_owner(self, path: Path) -> str | None:
        """The package that owns this path, when the system can be asked. Exact, not a guess."""
        ...

    def home(self) -> Path | None:
        """The user's home directory, resolved, when the platform knows it."""
        ...

    def removal_roots(self) -> tuple[tuple[Path, str], ...]:
        """What must never be removed whole or taken along with an ancestor."""
        ...

    def classify(self, path: Path, project_root: Path) -> Ruling: ...

    def classify_write(self, path: Path, project_root: Path) -> Ruling: ...

    def classify_removal(self, path: Path, project_root: Path) -> Ruling: ...

    def terminal_refusal(
        self, path: Path, project_root: Path, env: Mapping[str, str] | None = None
    ) -> str | None: ...


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class BasePlatform:
    """Shared classification. Subclasses supply the paths and the oracle."""

    name = "generic"
    #: APP ONLY: what the person's terminal tabs load when one opens, relative to the home
    #: (`terminal_refusal`). Empty where there are no terminal tabs.
    terminal_startup: tuple[str, ...] = ()
    #: APP ONLY: names of programs that ask for the person's password.
    privilege_tools: frozenset[str] = frozenset()

    def protected_roots(self) -> tuple[tuple[Path, str], ...]:
        return ()

    def scratch_roots(self) -> tuple[Path, ...]:
        return ()

    def package_owner(self, path: Path) -> str | None:
        return None

    def home(self) -> Path | None:
        return None

    def removal_roots(self) -> tuple[tuple[Path, str], ...]:
        """Every protected path, and the home directory itself.

        The home directory is not protected, since working in it is ordinary. But removing
        it whole is the one deletion no approval makes sensible, and an agent has actually
        run it: `rm -rf tests/ patches/ plan/ ~/`, a Claude Code session relayed by Simon
        Willison on 2025-12-09 (simonwillison.net/2025/Dec/9/claude/).
        """
        home = self.home()
        extra = ((home, "it is your home directory"),) if home is not None else ()
        return (*self.protected_roots(), *extra)

    def terminal_refusal(
        self, path: Path, project_root: Path, env: Mapping[str, str] | None = None
    ) -> str | None:
        """APP ONLY: why writing ``path`` would change what the person's terminal runs, or
        None. Two cases: a file a new tab's shell loads at start (`terminal_startup`, and
        the same names under ``ZDOTDIR``, ``XDG_DATA_HOME``, ``XDG_CONFIG_HOME`` and
        ``INPUTRC`` where they point elsewhere), and a file named like a password-asking
        program (`privilege_tools`) anywhere: a startup file can put any folder first on
        PATH, so which folders a tab searches is not known here. ``path`` is judged as
        written (its last component not followed) and resolved."""
        home = self.home()
        if home is None or not (self.terminal_startup or self.privilege_tools):
            return None
        env = os.environ if env is None else env
        written = Path(os.path.abspath(path))
        if written.name not in {"", ".", ".."}:
            written = Path(os.path.realpath(written.parent)) / written.name
        candidates = dict.fromkeys((written, _resolve(path)))
        for root in _terminal_roots(home, self.terminal_startup, env):
            for candidate in candidates:
                if _is_within(candidate, root):
                    return (
                        f"refused: {candidate} is loaded by the person's terminal when a tab "
                        "opens, and Cogito never changes it."
                    )
        for candidate in candidates:
            if candidate.name in self.privilege_tools:
                return (
                    f"refused: {candidate} would run in place of the real "
                    f"`{candidate.name}` in the person's terminal, where they type their "
                    "password, and Cogito never creates or changes it."
                )
        return None

    def classify_removal(self, path: Path, project_root: Path) -> Ruling:
        """`classify`, for an operation that also reaches everything beneath ``path``.

        A deletion, a move or a recursive change takes the whole tree under its target
        with it, so a target that merely *contains* a protected path is as untouchable as
        one inside it: `rm -rf /` names nothing on any list, and everything on the list is
        under it. What `classify` already refuses keeps its reason word for word.
        """
        ruling = self.classify(path, project_root)
        if ruling.zone is Zone.SYSTEM:
            return ruling
        resolved = _resolve(path)
        roots = self.removal_roots()
        for protected, description in roots:
            if resolved == protected:
                return Ruling(Zone.SYSTEM, f"{resolved} — {description}")
        for protected, description in roots:
            if _is_within(protected, resolved):
                return Ruling(Zone.SYSTEM, f"{resolved} contains {protected} — {description}")
        return ruling

    def classify_write(self, path: Path, project_root: Path) -> Ruling:
        """`classify`, for a write that destroys nothing: an append, a copy into a
        directory, a permission change.

        A package that owns a *directory* owns the directory, not every name that can be
        created in it: pacman reports `/tmp` as owned by `filesystem`, and `cp notes /tmp/`
        modifies nothing the package installed. So the package manager is asked only about
        a path that is not an existing directory. The protected list applies as always.
        """
        return self._classify(path, project_root, ask_owner_of_directories=False)

    def classify(self, path: Path, project_root: Path) -> Ruling:
        return self._classify(path, project_root, ask_owner_of_directories=True)

    def _classify(
        self, path: Path, project_root: Path, *, ask_owner_of_directories: bool
    ) -> Ruling:
        # Resolve first. On a merged-usr system /bin, /lib and /lib64 are symlinks into
        # /usr, so the list must match the destination, not the spelling.
        resolved = _resolve(path)
        root = _resolve(project_root)

        for protected, description in self.protected_roots():
            if _is_within(resolved, protected):
                where = (
                    f"{resolved}" if resolved == protected else f"{resolved} is inside {protected}"
                )
                return Ruling(Zone.SYSTEM, f"{where} — {description}")

        asks_owner = ask_owner_of_directories or not resolved.is_dir()
        if asks_owner and (owner := self.package_owner(resolved)):
            return Ruling(
                Zone.SYSTEM,
                f"{resolved} is owned by the installed package {owner}",
                owner=owner,
            )

        if _is_within(resolved, root):
            return Ruling(Zone.PROJECT, f"{_relative(resolved, root)} is inside the project")

        for scratch in self.scratch_roots():
            if _is_within(resolved, scratch):
                return Ruling(Zone.PROJECT, f"{resolved} is temporary space")

        return Ruling(Zone.HOME, f"{resolved} is outside the project")


#: zsh's startup files, also read from ``ZDOTDIR`` when the person sets it.
_ZSH_FILES = (".zshenv", ".zprofile", ".zshrc", ".zlogin", ".zlogout")


def _terminal_roots(
    home: Path, startup: Sequence[str], env: Mapping[str, str]
) -> tuple[Path, ...]:
    """Every startup file and folder a terminal tab loads, resolved."""
    if not startup:
        return ()
    roots = [home / relative for relative in startup]
    if zdotdir := env.get("ZDOTDIR", "").strip():
        roots += [Path(zdotdir) / name for name in _ZSH_FILES]
    if data := env.get("XDG_DATA_HOME", "").strip():
        roots += [
            Path(data) / relative.removeprefix(".local/share/")
            for relative in startup
            if relative.startswith(".local/share/")
        ]
    if config := env.get("XDG_CONFIG_HOME", "").strip():
        roots.append(Path(config) / "fish")
    if inputrc := env.get("INPUTRC", "").strip():
        roots.append(Path(inputrc))
    return tuple(dict.fromkeys(_resolve(Path(os.path.expanduser(root))) for root in roots))


def _resolve(path: Path) -> Path:
    """Resolve symlinks without requiring the path to exist.

    A path being deleted usually exists, but one being written may not, and it still needs
    a ruling.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return Path(path).absolute()


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
