"""Route for the composer's skills picker (design: the 1a chip's sibling, app-only). The
person activates an Agent Skill for the next message; cli.py's `_Skills` (`SkillsLike` here,
backed by `tools/skills.py`) lists what a project has, this module checks shapes and runs it
off the event loop. The turn itself carries the activated names in ``POST
/sessions/{id}/turns`` (``server/app.py``); this route only fills the menu.

``GET /sessions/{id}/skills``: ``{"skills": [{"name", "description", "directory"}]}`` — the
Agent Skills discovered for the session's project (and the person's home), sorted by name,
empty when skills are turned off. 404 for no such session.

`chat`, and so every benchmark task, never registers this route (cli.py passes no skills
lister outside the app), so a benchmark run is byte-identical to one without the feature.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


class SkillsLike(Protocol):
    def list(self, project_root: Path) -> dict[str, Any]: ...


def register(
    app: FastAPI,
    *,
    authorized: Callable[[Request], bool],
    skills: SkillsLike,
    project_root_for: Callable[[str], Path | None],
) -> None:
    """Wire ``GET /sessions/{id}/skills``. ``project_root_for`` maps a session id to its
    project root (``None`` for no such session), so the listing is scoped to the open
    project, the way attachments read ``sess["project_root"]``."""

    @app.get("/sessions/{session_id}/skills")
    async def list_skills(session_id: str, request: Request) -> JSONResponse:
        if not authorized(request):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        project_root = project_root_for(session_id)
        if project_root is None:
            return JSONResponse(status_code=404, content={"error": "session not found"})
        return JSONResponse(await asyncio.to_thread(skills.list, project_root))
