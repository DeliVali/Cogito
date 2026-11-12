"""Loopback routes for components, system programs, saved decisions, search levels and
permission answers. ``app.create_app`` registers them when given a components service.
Every route takes the same bearer token as the rest of the API.

The service lives in ``setup/`` (``service.ComponentService``), which this layer may not
import. cli.py builds it and hands it in, and this module reaches it only through
``ComponentsLike``. Every method returns a plain dict, with an ``"error"`` key in place of
an exception, the same boundary ``EngineLike``'s rewind methods use.

## Routes

``GET /components``: the "Herramientas" (tools) tab::

    {"components":[{"id","name","description","size_bytes","installed","installed_at",
                    "version","enabled","state":"not_installed|installing|installed|failed",
                    "kind"?, "error"?}]}

``POST /components/{id}/install`` and ``POST /components/{id}/retry``: start installing in
the background. 202 ``{"status":"installing"}``; 404 unknown; 400 ``not installable`` (a
``never`` component); 409 ``busy`` (already installing), ``no build`` (none for this
machine) or ``unpinned``. Progress arrives on every session's event stream (below).

``POST /components/{id}/ignore``: "Ignorar" on a failed card. 200, 404.
``POST /components/{id}/decline``: "Ahora no", saved globally. 200 ``{"decision"}``, 404.
``PUT /components/{id}/enabled {"enabled": bool}``: the tools tab switch. 200, 400, 404.

``GET /system/programs``: the "Del sistema" (system) list::

    {"os":"linux","managers":["pacman"],
     "programs":[{"id","name","found","version","path","variant"}]}

``GET /system/programs/{id}/help``: what the missing-program card shows. Same payload as
``program_missing`` without ``type``/``capability``::

    {"id":"rust","name":"Rust","manager":"pacman"|null,"command":"sudo pacman -S rustup"|null,
     "official_url":"https://..."|null}

``official_url`` is set only when ``command`` is null, i.e. no manager present can
install it.

``GET /decisions``: ``{"decisions":[{"id","what":"install|use","declined_at","name"}]}``.
``POST /decisions/reactivate {"id","what"}``: 200 ``{"reactivated":true}``, 400, 404.

``GET /settings/search``: the three search levels, which are fixed (`config.search`). The
answer carries ``"local_search": {"state","kind"?}`` while the core runs the app's own
SearXNG (``setup/searxng.py``): ``state`` is ``unsupported|off|preparing|starting|up|failed``,
``kind`` (with ``failed``) is ``offline|disk|start|other``. Absent when there is no such
service. The same change is broadcast as ``{"type":"local_search_state","state","kind"?}``.

``POST /sessions/{id}/permissions/{question} {"decision":"allow_session|deny|install_and_allow"}``:
answer a ``permission_request``. 200, 400, 404 (no such session, or no question waiting).

## Events

On the event stream of the session that caused them::

    {"type":"component_proposal","id","name","description","size_bytes","capability","caller"}
    {"type":"program_missing","id","name","manager","command","official_url","capability"}
    {"type":"permission_request","id","component","name","description","size_bytes",
     "installed","action","target","scope"}
    {"type":"permission_resolved","id","decision":"allow_session|deny|install_and_allow"|null,
     "reason":"user|timeout|interrupted"}
    {"type":"step_stopped","reason","where"}

On every session's stream, since an install belongs to the application, not one session::

    {"type":"component_install_started","id","name","size_bytes"}
    {"type":"component_install_progress","id","downloaded","total"}
    {"type":"component_installed","id","name","size_bytes","duration_ms"}
    {"type":"component_install_failed","id","name","kind":"disk|offline|other|unpinned",
     "error"}

``error`` is the real error text, for the card to show verbatim. It is empty for
``unpinned``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .questions import Questions

#: The answers a permission card can give. Wire contract read by `setup.service`.
PERMISSION_ANSWERS = ("allow_session", "deny", "install_and_allow")

_INSTALL_ERRORS = {
    "not_found": (404, "component not found"),
    "not_installable": (400, "not installable"),
    "busy": (409, "busy"),
    "no_build": (409, "no build"),
    # No SHA-256 pinned for this build: nothing is downloaded (setup.service.install).
    "unpinned": (409, "unpinned"),
}


class ComponentsLike(Protocol):
    """What ``setup.service.ComponentService`` offers this layer."""

    def set_broadcast(self, broadcast: Callable[[dict[str, Any]], None]) -> None: ...

    def list_components(self) -> list[dict[str, Any]]: ...

    def install(self, component_id: str) -> dict[str, Any]: ...

    def retry(self, component_id: str) -> dict[str, Any]: ...

    def ignore(self, component_id: str) -> dict[str, Any]: ...

    def decline(self, component_id: str) -> dict[str, Any]: ...

    def set_enabled(self, component_id: str, enabled: bool) -> dict[str, Any]: ...

    def system_programs(self) -> dict[str, Any]: ...

    def program_help(self, program_id: str) -> dict[str, Any]: ...

    def decisions_list(self) -> list[dict[str, Any]]: ...

    def reactivate(self, component_id: str, what: str) -> dict[str, Any]: ...

    def search_levels(self) -> dict[str, str]: ...


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def register(
    app: FastAPI,
    *,
    authorized: Callable[[Request], bool],
    components: ComponentsLike,
    questions: Questions,
    sessions: dict[str, dict[str, Any]],
    sessions_lock: threading.Lock,
) -> None:
    """Add the routes above to ``app`` and point the service's broadcast at every open
    session's event channel."""

    def broadcast(event: dict[str, Any]) -> None:
        with sessions_lock:
            channels = [session["channel"] for session in sessions.values()]
        for channel in channels:
            channel.emit(event)

    components.set_broadcast(broadcast)

    def guard(request: Request) -> None:
        if not authorized(request):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    def install_answer(result: dict[str, Any]) -> JSONResponse:
        error = result.get("error")
        if error is None:
            return JSONResponse(status_code=202, content=result)
        status, message = _INSTALL_ERRORS.get(error, (500, error))
        return JSONResponse(status_code=status, content={"error": message})

    def found_or_404(result: dict[str, Any], message: str) -> JSONResponse:
        if result.get("error") == "not_found":
            return JSONResponse(status_code=404, content={"error": message})
        return JSONResponse(status_code=200, content=result)

    @app.get("/components")
    async def get_components(request: Request) -> JSONResponse:
        guard(request)
        listed = await asyncio.to_thread(components.list_components)
        return JSONResponse(status_code=200, content={"components": listed})

    @app.post("/components/{component_id}/install")
    async def post_install(component_id: str, request: Request) -> JSONResponse:
        guard(request)
        return install_answer(await asyncio.to_thread(components.install, component_id))

    @app.post("/components/{component_id}/retry")
    async def post_retry(component_id: str, request: Request) -> JSONResponse:
        guard(request)
        return install_answer(await asyncio.to_thread(components.retry, component_id))

    @app.post("/components/{component_id}/ignore")
    async def post_ignore(component_id: str, request: Request) -> JSONResponse:
        guard(request)
        return found_or_404(
            await asyncio.to_thread(components.ignore, component_id), "component not found"
        )

    @app.post("/components/{component_id}/decline")
    async def post_decline(component_id: str, request: Request) -> JSONResponse:
        guard(request)
        return found_or_404(
            await asyncio.to_thread(components.decline, component_id), "component not found"
        )

    @app.put("/components/{component_id}/enabled")
    async def put_enabled(component_id: str, request: Request) -> JSONResponse:
        guard(request)
        body = await _json_object(request)
        if body is None or not isinstance(body.get("enabled"), bool):
            return JSONResponse(status_code=400, content={"error": "enabled must be a boolean"})
        result = await asyncio.to_thread(components.set_enabled, component_id, body["enabled"])
        return found_or_404(result, "component not found")

    @app.get("/system/programs")
    async def get_programs(request: Request) -> JSONResponse:
        guard(request)
        return JSONResponse(
            status_code=200, content=await asyncio.to_thread(components.system_programs)
        )

    @app.get("/system/programs/{program_id}/help")
    async def get_program_help(program_id: str, request: Request) -> JSONResponse:
        guard(request)
        return found_or_404(
            await asyncio.to_thread(components.program_help, program_id), "program not found"
        )

    @app.get("/decisions")
    async def get_decisions(request: Request) -> JSONResponse:
        guard(request)
        listed = await asyncio.to_thread(components.decisions_list)
        return JSONResponse(status_code=200, content={"decisions": listed})

    @app.post("/decisions/reactivate")
    async def post_reactivate(request: Request) -> JSONResponse:
        guard(request)
        body = await _json_object(request)
        if (
            body is None
            or not isinstance(body.get("id"), str)
            or not isinstance(body.get("what"), str)
        ):
            return JSONResponse(status_code=400, content={"error": "id and what must be strings"})
        result = await asyncio.to_thread(components.reactivate, body["id"], body["what"])
        return found_or_404(result, "decision not found")

    @app.get("/settings/search")
    async def get_search(request: Request) -> JSONResponse:
        guard(request)
        return JSONResponse(
            status_code=200, content=await asyncio.to_thread(components.search_levels)
        )

    @app.post("/sessions/{session_id}/permissions/{question_id}")
    async def post_permission(session_id: str, question_id: str, request: Request) -> JSONResponse:
        guard(request)
        body = await _json_object(request)
        decision = body.get("decision") if body is not None else None
        if decision not in PERMISSION_ANSWERS:
            return JSONResponse(
                status_code=400,
                content={"error": f"decision must be one of {', '.join(PERMISSION_ANSWERS)}"},
            )
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            # Only the session that asked may answer: another session's id is "not waiting".
            asked = question_id in sess.get("permission_ids", ())
        if not asked or not questions.answer(question_id, decision):
            return JSONResponse(
                status_code=404, content={"error": "no question is waiting under that id"}
            )
        return JSONResponse(status_code=200, content={"status": "answered"})
