"""Queries "Volver a un punto" (rewind) needs from the store.

A rewind point is not a new kind of node. It is the `id` of a `:Message` already on the
transcript with `role: "user"`: the message that opened the turn, or a steer spliced in
while one was running (`loop.engine._apply_steers`). Both already get a `message_appended`
id and a first-write-wins `created_at`, so giving every user message a rewind point mints
nothing new.

This module only reads. Restoring files (through `guard.undo.UndoStore` and `guard.trash`)
lives in `guard.rewind`, one layer up: `graph` may not import `guard` (CONTRIBUTING.md
section 2), and undoing a write is `guard`'s job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Must match `guard.undo.ENTRY_COLUMNS`. It cannot be imported from there: the layer
#: order (CONTRIBUTING.md section 2) only allows `guard -> graph`.
#: `tests/graph/test_rewind.py` and `tests/guard/test_undo.py` assert the two strings
#: stay identical.
_ENTRY_COLUMNS = (
    "u.key AS key, u.kind AS kind, u.original_path AS original_path, "
    "u.trash_path AS trash_path, u.content_hash AS content_hash, "
    "u.after_hash AS after_hash, u.created_at AS created_at"
)


@dataclass(frozen=True, slots=True)
class RewindCandidates:
    """What `guard.rewind.dry_run`/`perform` need to build a plan: the point's timestamp
    (entries at or before it are untouched) and every `:UndoEntry` recorded after it. The
    entries come from the point's session and any session it was compacted forward into,
    or, for a point from an earlier opening of the same project (the desktop's history),
    from any session of that project."""

    point_id: str
    point_session_id: str
    point_created_at: str
    lineage: tuple[str, ...]
    entries: tuple[Mapping[str, Any], ...]


def find_candidates(driver: Any, session_id: str, point_id: str) -> RewindCandidates | None:
    """Locate the point and the undo history after it.

    When the point's session reaches ``session_id`` through zero or more `COMPACTED_TO`
    hops, the scope is that lineage. Otherwise the point may still be in the same
    project's thread (a message from an earlier opening of this project, shown again by
    the desktop's history). Then the scope is every session of that project, since going
    back to the point undoes everything any of them changed after it.

    Returns ``None`` when the message does not exist, or belongs to neither this session's
    lineage nor its project (a point from an unrelated conversation).
    """
    point_rows = driver.query(
        "MATCH (point:Message {id: $point_id, role: 'user'}) "
        "MATCH (point_session:Session {id: point.session_id}) "
        "MATCH (current:Session {id: $session_id}) "
        "OPTIONAL MATCH lineage_path = (point_session)-[:COMPACTED_TO*0..]->(current) "
        "OPTIONAL MATCH (project:Project)-[:HAS_SESSION]->(point_session) "
        "WHERE (project)-[:HAS_SESSION]->(current) "
        "OPTIONAL MATCH (project)-[:HAS_SESSION]->(member:Session) "
        "WITH point, lineage_path, project, collect(DISTINCT member.id) AS members "
        "WHERE lineage_path IS NOT NULL OR project IS NOT NULL "
        "RETURN point.created_at AS point_created_at, point.session_id AS point_session_id, "
        "CASE WHEN lineage_path IS NOT NULL THEN [n IN nodes(lineage_path) | n.id] "
        "ELSE members END AS lineage "
        "LIMIT 1",
        point_id=point_id,
        session_id=session_id,
    )
    if not point_rows:
        return None
    point_row = point_rows[0]
    lineage: tuple[str, ...] = tuple(point_row["lineage"])

    entry_rows = driver.query(
        "MATCH (s:Session)-[:RECORDED]->(u:UndoEntry) "
        "WHERE s.id IN $lineage AND u.created_at > $point_created_at "
        f"RETURN {_ENTRY_COLUMNS} "
        "ORDER BY u.original_path, u.created_at",
        lineage=list(lineage),
        point_created_at=point_row["point_created_at"],
    )
    return RewindCandidates(
        point_id=point_id,
        point_session_id=point_row["point_session_id"],
        point_created_at=point_row["point_created_at"],
        lineage=lineage,
        entries=tuple(entry_rows),
    )


@dataclass(frozen=True, slots=True)
class RewindRecord:
    """One past `rewind_performed` event, as needed to redo it."""

    id: str
    session_id: str
    point_id: str
    #: (path, undo_entry_key) pairs. Each key is a fresh `UndoEntry` created *during* the
    #: rewind, holding what the path contained right before it was overwritten. The key is
    #: an empty string for a path the rewind restored without changing anything (see
    #: `guard.rewind.perform`). `guard.rewind.redo` restores each non-empty key.
    restored: tuple[tuple[str, str], ...]
    #: Set once this rewind has been redone. `guard.rewind.redo` refuses a second redo
    #: (strict review, item 1: "make redo single-use").
    redone_at: str | None


def load_rewind(driver: Any, redo_id: str, current_session_id: str) -> RewindRecord | None:
    """The bookkeeping a past rewind left, for `POST /redo/{redo_id}`.

    Scoped to ``current_session_id``'s own compaction lineage only; unlike
    `find_candidates` there is no project-wide fallback. A redo id from an unrelated
    conversation (or one this session cannot reach through any chain of compactions) is
    treated as not found, never redone against the wrong project's files.
    """
    rows = driver.query(
        "MATCH (s:Session)-[:RECORDED]->(r:Rewind {id: $id}) "
        "MATCH lineage_path = (s)-[:COMPACTED_TO*0..]->(current:Session {id: $current_session_id}) "
        "RETURN r.id AS id, s.id AS session_id, r.point_id AS point_id, "
        "r.restored_paths AS restored_paths, r.restored_keys AS restored_keys, "
        "r.redone_at AS redone_at "
        "LIMIT 1",
        id=redo_id,
        current_session_id=current_session_id,
    )
    if not rows:
        return None
    row = rows[0]
    paths = row.get("restored_paths") or []
    keys = row.get("restored_keys") or []
    return RewindRecord(
        id=row["id"],
        session_id=row["session_id"],
        point_id=row["point_id"],
        restored=tuple(zip(paths, keys, strict=True)),
        redone_at=row.get("redone_at"),
    )
