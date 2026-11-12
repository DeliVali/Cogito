"""Rewind ("Volver a un punto"): restoring the files a rewind point's later turns touched.

Slice 1 is file-only. A rewind point is a `:Message {role:"user"}` id: the turn that
started, or a steer spliced into one already running (see `graph.rewind`'s module
docstring). On top of `graph.rewind`'s read-only queries this module does the restore,
built entirely on `guard.undo.UndoStore.restore`. That method preserves whatever is at a
path before replacing or removing it (see `guard.undo`), so "nothing is ever removed
permanently" needs no extra machinery here.

Every path is tried on its own (`perform`/`redo`). One path's failure (a directory where a
file should be, a permission error, a symlink that may not be followed) is recorded and
reported. It never makes the whole operation raise partway, with some paths already
touched and no record of which (strict review, item 4).

This module is the files half. The conversation half (the model's context goes back to
just before the point; later messages are hidden, never deleted) is journalled under the
same redo id by `cli.EngineSession.rewind_perform` (`loop.continuation`, `graph.resume`),
because the transcript belongs to the loop and this layer sits below it. Nothing here
writes to the transcript or runs unless a person asks, so the text the model sees stays
byte-identical when no rewind is requested.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cogito.errors import CogitoError
from cogito.graph.rewind import RewindCandidates, find_candidates, load_rewind
from cogito.guard.trash import TrashError
from cogito.guard.undo import UndoEntry, UndoError, UndoStore, content_hash, entry_from_row, load
from cogito.ids import new_id

#: Matches loop.changes' own cap: a conflict dialog is no place for a diff that large
#: either. Duplicated because this module cannot import loop.changes (guard -> graph
#: only; loop is above guard in the layer order).
_MAX_DIFF_LINES = 200
_MAX_DIFF_BYTES = 1_000_000

#: What `perform`/`redo` catch per path, so one path's failure never aborts the rest.
_PER_PATH_ERRORS = (UndoError, TrashError, OSError)


class RewindError(CogitoError):
    kind = "rewind"


class RewindPointNotFound(RewindError):
    """The message id is not a rewind point, or does not lead to this session. Also used
    for an unknown ``redo_id``, or one outside this session's own compaction lineage
    (strict review item 8): both mean "this id means nothing here"."""

    kind = "rewind_point_not_found"


class RewindConflict(RewindError):
    """Blocked: at least one file changed since the point, and neither `keep_conflicts`
    nor `force` said what to do about it. `plan` is the same shape `dry_run` returns, so
    the caller can show the conflict dialog without a second query."""

    kind = "rewind_conflict"

    def __init__(self, plan: RewindPlan) -> None:
        super().__init__(f"{len(plan.conflicts)} file(s) changed since this point")
        self.plan = plan


class RewindAlreadyRedone(RewindError):
    """A redo is single-use (strict review item 1), and this one already ran."""

    kind = "rewind_already_redone"


def _relative(path: str, project_root: Path) -> str:
    """A path for a person to read, never the machine's absolute one (second strict
    review, item 4). Every path this module carries internally (grouping, filesystem
    operations, the `kept`/`forced` lists, `FileStatus`/`Conflict`/`Failure`) stays
    absolute; this is the one place, at the JSON boundary, that makes it project-relative.
    A path outside the project (a failure whose path resolved elsewhere, say) is returned
    as given, so displaying it never crashes.
    """
    try:
        return str(Path(path).relative_to(project_root.resolve()))
    except ValueError:
        return path


@dataclass(frozen=True, slots=True)
class FileStatus:
    path: str
    #: "modified" | "added" | "deleted": what happened to this path *since* the point, in
    #: the vocabulary `loop.changes` uses for "what happened this turn".
    status: str


@dataclass(frozen=True, slots=True)
class Conflict:
    path: str
    diff: str


@dataclass(frozen=True, slots=True)
class Failure:
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class RewindPlan:
    point_id: str
    files: tuple[FileStatus, ...]
    conflicts: tuple[Conflict, ...]
    #: Always False in slice 1: the commit undo the mockup shows is out of scope, and this
    #: module never runs git. Carried so the response shape already matches what slice 2
    #: will fill in.
    commit_after: bool = False

    def as_dict(self, project_root: Path) -> dict[str, Any]:
        return {
            "files": [{"path": _relative(f.path, project_root), "status": f.status} for f in self.files],
            "conflicts": [
                {"path": _relative(c.path, project_root), "diff": c.diff} for c in self.conflicts
            ],
            "commit_after": self.commit_after,
        }


@dataclass(frozen=True, slots=True)
class RewindResult:
    redo_id: str
    files: tuple[FileStatus, ...]
    kept: tuple[str, ...]
    forced: tuple[str, ...]
    failed: tuple[Failure, ...] = ()

    def as_dict(self, project_root: Path) -> dict[str, Any]:
        return {
            "redo_id": self.redo_id,
            "files": [{"path": _relative(f.path, project_root), "status": f.status} for f in self.files],
            "kept": [_relative(p, project_root) for p in self.kept],
            "forced": [_relative(p, project_root) for p in self.forced],
            "failed": [
                {"path": _relative(f.path, project_root), "reason": f.reason} for f in self.failed
            ],
        }


@dataclass(frozen=True, slots=True)
class RedoResult:
    restored: tuple[str, ...]
    failed: tuple[Failure, ...] = ()

    def as_dict(self, project_root: Path) -> dict[str, Any]:
        return {
            "restored": [_relative(p, project_root) for p in self.restored],
            "failed": [
                {"path": _relative(f.path, project_root), "reason": f.reason} for f in self.failed
            ],
        }


@dataclass(frozen=True, slots=True)
class _Group:
    """One path's undo history after the point, oldest first."""

    path: str
    earliest: UndoEntry
    latest: UndoEntry


def _status_for(kind: str) -> str:
    return {"create": "added", "overwrite": "modified", "delete": "deleted"}[kind]


def _group_by_path(candidates: RewindCandidates) -> list[_Group]:
    by_path: dict[str, list[UndoEntry]] = {}
    for row in candidates.entries:
        entry = entry_from_row(row)
        by_path.setdefault(str(entry.original_path), []).append(entry)
    groups = []
    for path, entries in by_path.items():
        ordered = sorted(entries, key=lambda e: e.created_at)
        groups.append(_Group(path=path, earliest=ordered[0], latest=ordered[-1]))
    return sorted(groups, key=lambda g: g.path)


def _read_text(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _restore_target_text(entry: UndoEntry) -> str:
    """What the path would hold right after a rewind restores ``entry``: empty for a
    `create`, which rewind undoes by deleting the path."""
    if entry.kind == "create":
        return ""
    return _read_text(entry.saved_path)


def _mark_missing_newlines(lines: list[str]) -> list[str]:
    """`difflib.unified_diff` never adds a terminator to a line that lacked one in the
    source (and writes no `\\ No newline at end of file` marker). Joined with `"".join`,
    such a line runs into whatever follows, the next hunk or the truncation notice this
    module appends, giving one garbled line instead of two (strict review, item 10)."""
    return [line if line.endswith("\n") else f"{line}\n" for line in lines]


def _diff(path: str, current: str, target: str) -> str:
    too_big = (
        len(current.encode("utf-8")) > _MAX_DIFF_BYTES
        or len(target.encode("utf-8")) > _MAX_DIFF_BYTES
    )
    if too_big:
        return ""
    lines = _mark_missing_newlines(
        list(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                target.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=3,
            )
        )
    )
    if len(lines) > _MAX_DIFF_LINES:
        lines = lines[:_MAX_DIFF_LINES] + [f"... ({len(lines) - _MAX_DIFF_LINES} more lines)\n"]
    return "".join(lines)


def _conflict_for(group: _Group) -> Conflict | None:
    expected_hash = "" if group.latest.kind == "delete" else group.latest.after_hash
    current_path = Path(group.path)
    current_hash = content_hash(current_path)
    if current_hash == expected_hash:
        return None
    current_text = _read_text(current_path if current_path.exists() else None)
    target_text = _restore_target_text(group.earliest)
    return Conflict(path=group.path, diff=_diff(group.path, current_text, target_text))


def _build_plan(point_id: str, candidates: RewindCandidates) -> RewindPlan:
    groups = _group_by_path(candidates)
    files = tuple(FileStatus(path=g.path, status=_status_for(g.earliest.kind)) for g in groups)
    conflicts = tuple(c for g in groups if (c := _conflict_for(g)) is not None)
    return RewindPlan(point_id=point_id, files=files, conflicts=conflicts)


def dry_run(driver: Any, session_id: str, point_id: str) -> RewindPlan:
    candidates = find_candidates(driver, session_id, point_id)
    if candidates is None:
        raise RewindPointNotFound(point_id)
    return _build_plan(point_id, candidates)


def perform(
    driver: Any,
    undo_store: UndoStore,
    record: Any,
    *,
    session_id: str,
    point_id: str,
    keep_conflicts: bool = False,
    force: bool = False,
) -> RewindResult:
    candidates = find_candidates(driver, session_id, point_id)
    if candidates is None:
        raise RewindPointNotFound(point_id)
    plan = _build_plan(point_id, candidates)
    conflict_paths = {c.path for c in plan.conflicts}

    if conflict_paths and not keep_conflicts and not force:
        raise RewindConflict(plan)

    groups = {g.path: g for g in _group_by_path(candidates)}
    kept: list[str] = []
    forced: list[str] = []
    restored_paths: list[str] = []
    restored_keys: list[str] = []
    restored_files: list[FileStatus] = []
    failed: list[Failure] = []

    for status in plan.files:
        is_conflict = status.path in conflict_paths
        if is_conflict and keep_conflicts and not force:
            kept.append(status.path)
            continue

        group = groups[status.path]
        # `UndoStore.restore` preserves whatever is at `target` first (which is how
        # `force` keeps the user's version, and what "Rehacer" replays), refuses or
        # replaces a symlink destination, and hashes what it wrote. One path's failure
        # here (a directory where a file should be, a permission error) is recorded and
        # skipped, so it does not abort the paths still to come.
        try:
            undo_store.adopt(group.earliest)
            result = undo_store.restore(group.earliest.key, overwrite=True)
        except _PER_PATH_ERRORS as exc:
            failed.append(Failure(path=status.path, reason=str(exc)))
            continue

        if is_conflict and force:
            forced.append(status.path)
        restored_paths.append(status.path)
        # "" (never None, since Neo4j properties are flat) when the restore was a no-op:
        # the path already matched the point's state, so there is nothing to redo.
        restored_keys.append(result.preserved_key or "")
        restored_files.append(status)

    redo_id = new_id()
    record(
        "rewind_performed",
        id=redo_id,
        session_id=session_id,
        point_id=point_id,
        restored_paths=restored_paths,
        restored_keys=restored_keys,
        kept_paths=sorted(kept),
        forced_paths=sorted(forced),
        failed_paths=[f.path for f in failed],
        failed_reasons=[f.reason for f in failed],
    )
    return RewindResult(
        redo_id=redo_id,
        files=tuple(restored_files),
        kept=tuple(kept),
        forced=tuple(forced),
        failed=tuple(failed),
    )


def redo(
    driver: Any,
    undo_store: UndoStore,
    record: Any,
    *,
    session_id: str,
    redo_id: str,
) -> RedoResult:
    """Reverse exactly what one `rewind_performed` event did, bringing every path it
    touched back to what it held just before that rewind ran. Refuses a redo that already
    ran (single-use, strict review item 1) or whose session is not reachable from
    `session_id` through any chain of compactions (item 8).
    """
    found = load_rewind(driver, redo_id, session_id)
    if found is None:
        raise RewindPointNotFound(redo_id)
    if found.redone_at is not None:
        raise RewindAlreadyRedone(redo_id)

    restored: list[str] = []
    failed: list[Failure] = []
    for path, key in found.restored:
        if not key:
            continue  # this path's rewind step was a no-op; nothing to redo
        entry = load(driver, key)
        if entry is None:
            failed.append(Failure(path=path, reason="the preserved copy for this path is gone"))
            continue
        try:
            undo_store.adopt(entry)
            # `restore` (see `guard.undo`) handles every kind safely, `create` included.
            undo_store.restore(entry.key, overwrite=True)
        except _PER_PATH_ERRORS as exc:
            failed.append(Failure(path=path, reason=str(exc)))
            continue
        restored.append(path)

    if restored:
        # Not recorded when nothing was redone: an all-failed attempt must stay retryable
        # (second strict review, item 3). Setting `Rewind.redone_at` would make the
        # single-use check (`RewindAlreadyRedone`) refuse a retry of something that never
        # happened.
        record("redo_performed", rewind_id=redo_id, session_id=session_id)
    return RedoResult(restored=tuple(restored), failed=tuple(failed))
