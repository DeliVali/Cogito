"""Projects and sessions.

A project is a directory. Its identity comes from the resolved path, so the same checkout
is the same project however it was reached. Sessions belong to a project and chain through
compaction, so the interface can show one continuous conversation per project while the
history underneath stays intact.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cogito.graph.driver import GraphDriver


def project_id_for(root: Path) -> str:
    """A stable identity for a directory, independent of how the path was spelled."""
    resolved = str(root.resolve())
    return "P" + hashlib.sha256(resolved.encode()).hexdigest()[:24].upper()


def open_project(record: Callable[..., Any], root: Path) -> str:
    project_id = project_id_for(root)
    record("project_opened", id=project_id, root=str(root.resolve()), name=root.resolve().name)
    return project_id


def end_session(record: Callable[..., Any], session_id: str, reason: str) -> None:
    record("session_ended", id=session_id, close_reason=reason)


def latest_open_session(driver: GraphDriver, project_id: str) -> str | None:
    """The newest session of this project that has not been closed."""
    rows = driver.query(
        "MATCH (:Project {id: $project_id})-[:HAS_SESSION]->(s:Session) "
        "WHERE s.ended_at IS NULL "
        "RETURN s.id AS id ORDER BY s.created_at DESC LIMIT 1",
        project_id=project_id,
    )
    return rows[0]["id"] if rows else None


def transcript(driver: GraphDriver, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Messages of one session in order. The lineage is walked separately by recall."""
    return driver.query(
        "MATCH (m:Message {session_id: $session_id}) "
        "RETURN m.seq AS seq, m.role AS role, m.text AS text, m.tokens AS tokens "
        "ORDER BY seq LIMIT $limit",
        session_id=session_id,
        limit=limit,
    )


def list_projects(driver: GraphDriver, limit: int = 100) -> list[dict[str, Any]]:
    """Every project, most recently opened session first.

    "Opened" is the newest session's `created_at`, or its `resumed_at` when the app has
    reopened it (`graph.resume`), not the project's own timestamp: a project is opened
    again every time a session starts or resumes under it. A project with no session yet
    (between `project_opened` and its first `session_started`) still appears, after every
    project that has one, with ``last_opened_at`` of ``None``.

    Raises ``GraphUnavailable`` (via ``driver.query``) when Neo4j cannot be reached. The
    caller decides what a project list means without a graph, as everywhere else the WAL
    keeps the app usable without one.
    """
    return driver.query(
        "MATCH (p:Project) "
        "OPTIONAL MATCH (p)-[:HAS_SESSION]->(s:Session) "
        "WITH p, max(coalesce(s.resumed_at, s.created_at)) AS last_opened_at "
        "RETURN p.id AS id, p.root AS root, p.name AS name, last_opened_at "
        # Cypher's `DESC` puts NULL first, which would list a project with no session yet
        # at the top instead of the bottom.
        "ORDER BY last_opened_at IS NULL, last_opened_at DESC LIMIT $limit",
        limit=limit,
    )
