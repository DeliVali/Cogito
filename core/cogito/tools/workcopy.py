"""A subagent's own copy of the project, and the patch of what it changed there.

A subagent that writes never works in the person's folder. It gets a git clone of the
project's current state: the commit checked out, the uncommitted changes and the files git
does not ignore yet. The clone borrows the project's objects read-only (`--shared`) and
writes its own, so nothing in the project or its `.git` changes. When the subagent is done,
what it changed is one patch against that state, for the caller to read and apply. Claude
Code gives a writing subagent a temporary git worktree, kept only when it changed
something (code.claude.com/docs/en/sub-agents.md, "isolation: worktree", read 2026-11-10);
a clone is used here instead so the project's `.git` gains no worktree entry.

Deletions are reported, never carried in the patch: a deleted file stays in the project
until the caller deletes it itself, under the guard's rules.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Folders git ignores that a project's checks need and that are too large to copy: linked
#: into the copy, read-only there (the subagent's shell is sandboxed, and the file tools
#: may not write outside the copy).
_LINKED = (".venv", "venv", "node_modules")
#: What is never part of the patch: the subagent's own record.
_NOT_CARRIED = (".cogito",)
_IDENTITY = ("-c", "user.name=Cogito", "-c", "user.email=cogito@localhost")


class CopyRefused(Exception):
    """The project cannot be copied this way; the message says why and what to do."""


@dataclass(frozen=True, slots=True)
class Changes:
    """What a subagent changed in its copy."""

    #: The patch against the copy's starting state, deletions left out, as git wrote it.
    #: Empty when only deletions, or nothing, changed.
    patch: bytes
    #: ``git diff --stat`` of the patch.
    stat: str
    #: Files the subagent deleted, which the patch does not delete.
    deleted: tuple[str, ...]


def _git(root: Path, *arguments: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=data,
        capture_output=True,
        timeout=120,
        check=False,
    )


def _out(result: subprocess.CompletedProcess) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def refusal(project: Path) -> str:
    """Why this project cannot be copied for a writing subagent, or "" when it can."""
    try:
        top = _git(project, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return "git could not be run here."
    if top.returncode != 0:
        return (
            "a writing subagent works in a git copy of the project, and this folder is not "
            "in a git repository. Make the change yourself, or use kind \"explore\"."
        )
    if Path(_out(top)).resolve() != project.resolve():
        return (
            "a writing subagent copies a whole git repository, and this project is a folder "
            f"inside one ({_out(top)}). Make the change yourself, or use kind \"explore\"."
        )
    if _git(project, "rev-parse", "--verify", "HEAD").returncode != 0:
        return (
            "a writing subagent starts from the project's last commit, and this repository "
            "has none yet. Make the change yourself, or use kind \"explore\"."
        )
    return ""


def prepare(project: Path, copy: Path) -> Path:
    """Clone the project's current state into ``copy`` and return it. Raises `CopyRefused`."""
    if why := refusal(project):
        raise CopyRefused(why)
    head = _git(project, "rev-parse", "--verify", "HEAD")
    copy.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", "--quiet", str(project), str(copy)],
        capture_output=True,
        timeout=300,
        check=False,
    )
    if clone.returncode != 0:
        raise CopyRefused(f"the copy could not be made: {clone.stderr.decode()[-300:]}")
    checkout = _git(copy, "checkout", "--quiet", "--detach", _out(head))
    if checkout.returncode != 0:
        raise CopyRefused(f"the copy could not be made: {checkout.stderr.decode()[-300:]}")
    # The uncommitted changes, staged or not, as git sees them.
    diff = _git(project, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD")
    if diff.returncode == 0 and diff.stdout:
        applied = _git(copy, "apply", "--binary", "--whitespace=nowarn", "-", data=diff.stdout)
        if applied.returncode != 0:
            raise CopyRefused(
                "the project's uncommitted changes could not be copied: "
                f"{applied.stderr.decode()[-300:]}"
            )
    untracked = _git(
        project, "ls-files", "--others", "--exclude-standard", "-z",
        "--", ".", *(f":(exclude){name}" for name in _NOT_CARRIED),
    )
    for name in untracked.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not name:
            continue
        source, target = project / name, copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        elif source.is_file():
            shutil.copy2(source, target)
    # The starting state is a commit of the copy's own, so the patch is against it.
    _git(copy, "add", "-A")
    _git(copy, *_IDENTITY, "commit", "--quiet", "--no-verify", "--allow-empty", "-m", "start")
    for name in _LINKED:
        source, target = project / name, copy / name
        if source.is_dir() and not target.exists() and _ignored(project, name):
            target.symlink_to(source)
    return copy


def _ignored(project: Path, name: str) -> bool:
    return _git(project, "check-ignore", "--quiet", name).returncode == 0


def collect(copy: Path) -> Changes:
    """What changed in ``copy`` since `prepare`, as a patch without deletions."""
    _git(copy, "add", "-A", "--", ".", *(f":(exclude){name}" for name in _NOT_CARRIED + _LINKED))
    excluded = [f":(exclude){name}" for name in _NOT_CARRIED + _LINKED]
    status = _out(_git(copy, "diff", "--cached", "--no-renames", "--name-status", "HEAD"))
    deleted = tuple(
        line.split("\t", 1)[1] for line in status.splitlines() if line.startswith("D\t")
    )
    # Lowercase `d` leaves deletions out (git diff --diff-filter).
    patch = _git(
        copy, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
        "--no-renames", "--diff-filter=d", "HEAD", "--", ".", *excluded,
    )
    stat = _git(
        copy, "diff", "--cached", "--stat", "--no-ext-diff", "--no-renames", "--diff-filter=d",
        "HEAD",
        "--", ".", *excluded,
    )
    return Changes(
        patch=patch.stdout,
        stat=_out(stat),
        deleted=deleted,
    )


def discard(copy: Path) -> None:
    """Remove the copy. It is the harness's own folder, made by `prepare`."""
    shutil.rmtree(copy, ignore_errors=True)
