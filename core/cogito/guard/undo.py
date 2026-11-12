"""Makes destructive actions reversible and records them in the graph.

Every deletion and overwrite is preserved before it happens and written to the graph, so
`cogito undo` reads the same store as everything else.

First invariant: a preserved entry can be restored. The snapshot is taken first and the
action runs only if it succeeded. A record of a save that never happened is worse than none.

Second invariant (from the `app-b2-rewind` strict review): `restore()` preserves whatever
is at the path now before it replaces or removes it. Otherwise restoring an old version
destroys newer work. `cogito undo`, rewind and its redo ("Rehacer") all go through
`restore()`, so this one rule covers all three.

Snapshots under `<project>/.cogito/undo` (or the system trash, for a delete) are never
removed, so the directory grows without bound in a long session. This is a known limit.
Pruning is future work and needs its own measurement before it decides what is safe to
discard.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cogito.config import paths
from cogito.errors import CogitoError
from cogito.guard.trash import Trashed, copy_to_trash, trash
from cogito.guard.trash import restore as restore_from_trash
from cogito.ids import new_id

Kind = Literal["delete", "overwrite", "create"]


class UndoError(CogitoError):
    kind = "undo"


def content_hash(path: Path) -> str:
    """SHA-256 of a file's bytes, or ``""`` for a path that is not a regular file.

    Public so rewind's conflict check hashes the current file by the same rule as an
    entry's own hash. Always hash the file after the write, never the string that
    produced it: text I/O can translate newlines (`Path.write_text` does on Windows), so
    the string and the bytes on disk can differ.
    """
    if not path.is_file():
        return ""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# Old name, still used inside this module.
_digest = content_hash


@dataclass(frozen=True, slots=True)
class UndoEntry:
    key: str
    kind: Kind
    original_path: Path
    #: Where the content was put. None for a `create`, which is undone by deleting.
    saved_path: Path | None
    content_hash: str
    created_at: str
    #: True when the saved copy is in the desktop trash rather than Cogito's own store.
    in_trash: bool = False
    #: Hash of what this path held right after this entry, read from the file after the
    #: write (`note_written`). Empty until `note_written` runs, or for an older entry.
    #: Rewind's conflict check compares it with the live hash to see whether the path
    #: was edited since.
    after_hash: str = ""

    def as_properties(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "original_path": str(self.original_path),
            "trash_path": str(self.saved_path) if self.saved_path else "",
            "content_hash": self.content_hash,
            "after_hash": self.after_hash,
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    path: Path
    #: Key of a new `UndoEntry` holding what `path` held just before this `restore()`.
    #: `None` only when nothing (no file, no symlink) was there. Rewind's redo is built
    #: from it: preserving before restoring is what makes a restore undoable.
    preserved_key: str | None


class UndoStore:
    """Preserves what an action is about to destroy, and puts it back on request."""

    def __init__(
        self,
        project_root: Path,
        session_id: str,
        record: Callable[..., Any] | None = None,
    ) -> None:
        self._project_root = project_root
        #: Mutable: a session that folds (`context.compact`) gets a new id while this
        #: store lives. Later entries must carry the current id, or a rewind walking the
        #: compaction lineage from the new session would not find them. `Engine`'s
        #: `on_session_changed` callback calls `set_session_id` (see `cli.py`'s
        #: `_build_engine`).
        self._session_id = session_id
        self._record = record or (lambda *a, **k: None)
        self._entries: dict[str, UndoEntry] = {}

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def project_root(self) -> Path:
        """Public so `guard.rewind`, which turns absolute paths into project-relative ones
        for display, does not need a second copy."""
        return self._project_root

    def _remember(self, entry: UndoEntry) -> UndoEntry:
        self._entries[entry.key] = entry
        self._record("undo_recorded", session_id=self._session_id, **entry.as_properties())
        return entry

    def note_written(self, key: str, path: Path) -> UndoEntry:
        """Call once, right after `path` was written. Hashes the bytes on disk (see
        `content_hash` for why never a string) and records it as this entry's
        `after_hash`. Idempotent: it sends the same MERGE+SET `undo_recorded` event as
        the first record, so a second call updates the graph node instead of adding one.
        """
        entry = self._entries.get(key)
        if entry is None:
            raise UndoError(f"no undo entry {key!r} in this session")
        updated = replace(entry, after_hash=content_hash(path))
        self._entries[key] = updated
        self._record("undo_recorded", session_id=self._session_id, **updated.as_properties())
        return updated

    def discard(self, key: str) -> None:
        """Forget an entry whose change never happened: the write after it failed.

        It is closed the way an entry already undone is, so neither `cogito undo` nor this
        store offers to undo what nobody did.
        """
        if self._entries.pop(key, None) is not None:
            self._record("undo_restored", key=key)

    def preserve_delete(self, path: Path) -> UndoEntry:
        """Move ``path`` to the trash. The deletion *is* this call; nothing is unlinked."""
        path = Path(path)
        digest = _digest(path)
        moved: Trashed = trash(path, project_root=self._project_root)
        return self._remember(
            UndoEntry(
                key=new_id(),
                kind="delete",
                original_path=moved.original,
                saved_path=moved.trashed,
                content_hash=digest,
                created_at=moved.deleted_at,
                in_trash=True,
            )
        )

    def preserve_in_trash(self, path: Path) -> UndoEntry:
        """Put a copy of ``path`` in the trash before it is changed in place, and record it
        as an overwrite: the file stays, only its content changes. Call `note_written`
        afterward with the same key once the change is on disk."""
        path = Path(path)
        digest = _digest(path)
        copied: Trashed = copy_to_trash(path, project_root=self._project_root)
        return self._remember(
            UndoEntry(
                key=new_id(),
                kind="overwrite",
                original_path=path.resolve(),
                saved_path=copied.trashed,
                content_hash=digest,
                created_at=copied.deleted_at,
                in_trash=True,
            )
        )

    def preserve_overwrite(self, path: Path) -> UndoEntry:
        """Snapshot a file before it is written over. Call `note_written` with the same
        key once the new content is on disk."""
        path = Path(path)
        if not path.exists():
            return self.preserve_create(path)
        slot = paths.ensure(paths.undo_dir(self._project_root)) / f"{new_id()}{path.suffix}"
        shutil.copy2(path, slot)
        return self._remember(
            UndoEntry(
                key=slot.stem,
                kind="overwrite",
                # Resolve as `preserve_create` does (strict review, item 6). Otherwise
                # one file reached two ways (a symlinked project root, a `..` segment)
                # gets two `original_path`s, and rewind, which groups entries by that
                # string, splits its history in two.
                original_path=path.resolve(),
                saved_path=slot,
                content_hash=content_hash(slot),
                created_at=datetime.now(UTC).isoformat(),
            )
        )

    def preserve_create(self, path: Path) -> UndoEntry:
        """Record that a path did not exist, so creating it can be undone by removing it."""
        return self._remember(
            UndoEntry(
                key=new_id(),
                kind="create",
                original_path=Path(path).resolve(),
                saved_path=None,
                content_hash="",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

    def _validate_ancestors(self, target: Path) -> None:
        """Refuses when a directory above `target` has changed since it was recorded.

        Checks the parent, not `target`: a symlink leaf is an expected case that
        `restore()` preserves and replaces once the overwrite gate passes. `target.parent`
        is already resolved (`preserve_create` and `preserve_overwrite` resolve the whole
        path, and a resolved path's parent is resolved), so resolving it again must give
        the same path. If not, some component between the project root and this directory
        now points elsewhere, e.g. a real directory replaced by a symlink (second strict
        review, item 2, scenario H: a project subdirectory swapped for a symlink outside
        the project after the agent wrote inside it). Any change is refused, whether the
        new location is outside the project or elsewhere inside it.
        """
        parent = target.parent
        resolved = parent.resolve(strict=False)
        if resolved != parent:
            raise UndoError(
                f"{target}'s own directory no longer resolves the way it did when this "
                f"was recorded (now: {resolved}) — refusing to restore through it"
            )

    def _refuse_symlink_outside_project(self, target: Path) -> None:
        """Refuses a symlink leaf that resolves outside the project, the guard's usual
        "never touch a path outside the project" rule. (`_validate_ancestors` covers the
        ancestor directories.) A symlink inside the project goes through `restore()`'s
        overwrite gate and, if that passes, is preserved intact (second review, item 6).
        """
        try:
            resolved = target.resolve(strict=False)
        except OSError as exc:  # pragma: no cover - defensive; resolve(strict=False) rarely raises
            raise UndoError(f"could not resolve the symlink at {target}: {exc}") from exc
        root = self._project_root.resolve()
        if resolved != root and root not in resolved.parents:
            raise UndoError(
                f"{target} is a symlink to {resolved}, outside the project — refusing to "
                f"restore through it"
            )

    def restore(self, key: str, *, overwrite: bool = False) -> RestoreResult:
        """Put back what the entry preserved.

        Whatever is at the path now is preserved first: trashed if the entry is a
        `create` (the path should not exist afterwards), snapshotted otherwise. That
        preservation is an ordinary `UndoEntry` whose key is `RestoreResult.preserved_key`.
        Its `after_hash` is filled in (`note_written`) once the restore's own write lands,
        so a later conflict check sees what this restore produced.

        A `create` entry never needs `overwrite=True`, even when something is there now.
        Undoing a create sends the file to the trash, so it is safe, and this is
        `cogito undo`'s most common case (second strict review, item 5).
        """
        entry = self._entries.get(key)
        if entry is None:
            raise UndoError(f"no undo entry {key!r} in this session")

        target = entry.original_path
        self._validate_ancestors(target)
        is_symlink = target.is_symlink()
        if is_symlink:
            self._refuse_symlink_outside_project(target)

        currently_there = target.exists() or is_symlink
        if currently_there and entry.kind != "create" and not overwrite:
            raise UndoError(f"{target} exists again; pass overwrite to replace it")

        preserved_key: str | None = None
        if is_symlink:
            # `preserve_delete` moves the link itself to the trash (a rename never
            # follows it). A bare `unlink()` would lose it. Only reached after the
            # overwrite gate above passed.
            preserved_key = self.preserve_delete(target).key
        elif entry.kind == "create":
            if currently_there:
                # `preserve_delete` is the deletion, so nothing is left to unlink. A raw
                # unlink here would delete a just-restored file for good on redo (see
                # `guard.rewind.redo`). If the path is already absent, this is a no-op.
                preserved_key = self.preserve_delete(target).key
        else:
            # Preserve even when nothing is there: `preserve_overwrite` then falls back to
            # `preserve_create` and records that the path did not exist. Redo needs that
            # to reverse a restore that created the path. Without it, redo has no key and
            # leaves a deleted-then-restored file in place (strict review's
            # redo-loses-work repro).
            preserved_key = self.preserve_overwrite(target).key

        if entry.kind == "create":
            self._record("undo_restored", key=entry.key)
            if preserved_key is not None:
                self.note_written(preserved_key, target)  # target no longer exists; hash ""
            return RestoreResult(target, preserved_key)

        saved = entry.saved_path
        if saved is None or not (saved.exists() or saved.is_symlink()):
            # A trashed link is there even when what it points at is not.
            raise UndoError(f"the saved copy for {target} is gone")

        if entry.in_trash:
            restored = restore_from_trash(
                Trashed(target, entry.saved_path, entry.created_at, standard=True),
                overwrite=True,  # already preserved above if anything was there
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry.saved_path, target)
            restored = target

        if content_hash(restored) != entry.content_hash:
            raise UndoError(
                f"{restored} was restored but its contents do not match what was preserved"
            )
        self._record("undo_restored", key=entry.key)
        if preserved_key is not None:
            self.note_written(preserved_key, restored)
        return RestoreResult(restored, preserved_key)

    @property
    def entries(self) -> tuple[UndoEntry, ...]:
        return tuple(self._entries.values())

    def adopt(self, entry: UndoEntry) -> None:
        """Take responsibility for an entry loaded from the graph."""
        self._entries[entry.key] = entry


#: Columns every query that reads an `:UndoEntry` back returns, so rows from `recent`,
#: `load` and `graph.rewind`'s queries convert the same way. Must stay identical to
#: `graph.rewind._ENTRY_COLUMNS`.
ENTRY_COLUMNS = (
    "u.key AS key, u.kind AS kind, u.original_path AS original_path, "
    "u.trash_path AS trash_path, u.content_hash AS content_hash, "
    "u.after_hash AS after_hash, u.created_at AS created_at"
)


def entry_from_row(row: Mapping[str, Any]) -> UndoEntry:
    """Rebuild an `UndoEntry` from one row of `ENTRY_COLUMNS`. Public so `guard.rewind`
    builds the same object `recent` and `load` do from rows `graph.rewind` queried
    (`graph` cannot import `guard`, so it returns plain rows)."""
    saved = Path(row["trash_path"]) if row.get("trash_path") else None
    return UndoEntry(
        key=row["key"],
        kind=row["kind"],
        original_path=Path(row["original_path"]),
        saved_path=saved,
        content_hash=row.get("content_hash") or "",
        created_at=row.get("created_at") or "",
        # A freedesktop trash keeps each item directly in `<trash>/files/`, wherever the
        # trash itself is (`trash.home_trash`). Cogito's own store keeps it in `undo/` or
        # `trash/`.
        in_trash=bool(
            saved and saved.parent.name == "files" and saved.parent.parent.name == "Trash"
        ),
        after_hash=row.get("after_hash") or "",
    )


def recent(driver: Any, limit: int = 20) -> list[UndoEntry]:
    """Undo entries from the graph, newest first, across every session of any project."""
    rows = driver.query(
        f"MATCH (u:UndoEntry) WHERE u.restored_at IS NULL "
        f"RETURN {ENTRY_COLUMNS} ORDER BY u.created_at DESC LIMIT $limit",
        limit=limit,
    )
    return [entry_from_row(row) for row in rows]


def owner(driver: Any, key: str) -> tuple[str, Path] | None:
    """The session that recorded entry ``key`` and its project's root, or None when the
    graph does not say. `cogito undo` records against that session, and keeps snapshots in
    that project's `.cogito/`."""
    rows = driver.query(
        "MATCH (p:Project)-[:HAS_SESSION]->(s:Session)-[:RECORDED]->(u:UndoEntry {key: $key}) "
        "RETURN s.id AS session_id, p.root AS root LIMIT 1",
        key=key,
    )
    if not rows or not rows[0].get("session_id") or not rows[0].get("root"):
        return None
    return str(rows[0]["session_id"]), Path(rows[0]["root"])


def load(driver: Any, key: str) -> UndoEntry | None:
    """One entry by key, restored or not. Rewind's redo needs entries an earlier rewind
    created, possibly in another process, so it cannot rely on an `UndoStore`'s
    in-memory `_entries`."""
    rows = driver.query(f"MATCH (u:UndoEntry {{key: $key}}) RETURN {ENTRY_COLUMNS}", key=key)
    return entry_from_row(rows[0]) if rows else None
