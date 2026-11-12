"""Routes for the Conexiones screens (design 4e → Conexiones, 4f, the 1a chip): the MCP
servers in config.toml, their state and their tools. cli.py's `Connections`
(`tools/mcp/connections.py`, `ConnectionsLike` here) does the work; this module checks
shapes, runs it off the event loop and reports the result. Every route takes the app's
bearer token.

``GET /connections``: ``{"connections": [{"name", "status": "connected|starting|failed|login|disabled",
"enabled", "error", "signing_in", "remote", "command", "tools": [{"name", "title",
"changes"}], "tokens", "log", "started_at", "needs"}]}``. An enabled server not yet running is
started in the background and reads ``starting`` until it has; ``log`` holds the last lines it
printed while starting or before it failed, ``needs`` the values its entry leaves to fill in.
In the app, adding or retrying one answers at once with ``starting``.

``PUT /connections/{name} {"enabled": bool}``: written to
config.toml; the connection as above. 400 for a wrong value, 404 for no such server.

``POST /connections {"name"?, "command"}``: a new server started by ``command`` on this
machine, an ``https`` address, or another client's install line or JSON, saved as what it
names. The connection as above; 400 with ``{"error"}`` (a taken name, a plain-http address).

``DELETE /connections/{name}``: ``{"removed": name}``; 404.

``POST /connections/{name}/retry``: stops it and starts it again; the connection; 404.

``GET /connections/import?session=ID``: ``{"found": [{"key", "source", "file", "name",
"command", "env", "needs", "taken", "scope"}]}``, the servers other clients configured
(Claude Code, Claude Desktop, Cursor, Codex, Gemini CLI) and, with a session, its project's
``.mcp.json``. Read-only; what they set in the environment or headers is named, never shown.

``POST /connections/import {"keys", "session"?}``: ``{"added": [names], "skipped": [{"key",
"reason"}]}``. Each ticked server is copied into config.toml as its file holds it, values
included; one from the project's ``.mcp.json`` is approved for that project instead.

``POST /connections/{name}/login``: ``{"url", "name"}``, the sign-in page of a server at an
address, for the browser; the core waits for the answer and reconnects. 400 with
``{"error"}`` when the server cannot be signed in to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


class ConnectionsLike(Protocol):
    def list(self) -> dict[str, Any]: ...

    def set(self, name: str, values: Mapping[str, Any]) -> dict[str, Any]: ...

    def add(self, name: str, command: str) -> dict[str, Any]: ...

    def remove(self, name: str) -> dict[str, Any]: ...

    def retry(self, name: str) -> dict[str, Any]: ...

    def login(self, name: str) -> dict[str, Any]: ...

    def discover(self, project_root: Path | None) -> dict[str, Any]: ...

    def import_servers(self, keys: list[str], project_root: Path | None) -> dict[str, Any]: ...


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _answer(result: dict[str, Any]) -> JSONResponse:
    """A refusal carries a non-empty ``error`` and a ``code`` the window words itself
    (``name``, ``taken``, ``address``, ``command``); a connection's own ``error`` is its
    start log, empty while it runs."""
    if not result.get("refused"):
        return JSONResponse(status_code=200, content=result)
    status = 404 if result.get("missing") else 400
    return JSONResponse(
        status_code=status, content={"error": result["error"], "code": result.get("code", "")}
    )


def register(
    app: FastAPI,
    *,
    authorized: Callable[[Request], bool],
    connections: ConnectionsLike,
    project_root_for: Callable[[str], Path | None] | None = None,
) -> None:
    def guard(request: Request) -> None:
        if not authorized(request):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    @app.get("/connections")
    async def list_connections(request: Request) -> JSONResponse:
        guard(request)
        return _answer(await asyncio.to_thread(connections.list))

    @app.put("/connections/{name}")
    async def set_connection(name: str, request: Request) -> JSONResponse:
        guard(request)
        body = await _json_object(request)
        if body is None:
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        return _answer(await asyncio.to_thread(connections.set, name, body))

    @app.post("/connections")
    async def add_connection(request: Request) -> JSONResponse:
        guard(request)
        body = await _json_object(request)
        # The name may be left out when the pasted line or JSON gives one.
        name = body.get("name", "") if body is not None else None
        if body is None or not isinstance(name, str) or not isinstance(body.get("command"), str):
            return JSONResponse(status_code=400, content={"error": "a command is required"})
        return _answer(await asyncio.to_thread(connections.add, name, body["command"]))

    @app.delete("/connections/{name}")
    async def remove_connection(name: str, request: Request) -> JSONResponse:
        guard(request)
        return _answer(await asyncio.to_thread(connections.remove, name))

    @app.post("/connections/{name}/retry")
    async def retry_connection(name: str, request: Request) -> JSONResponse:
        guard(request)
        return _answer(await asyncio.to_thread(connections.retry, name))

    @app.post("/connections/{name}/login")
    async def login_connection(name: str, request: Request) -> JSONResponse:
        guard(request)
        return _answer(await asyncio.to_thread(connections.login, name))

    def project_of(session: object) -> Path | None:
        if not isinstance(session, str) or not session or project_root_for is None:
            return None
        return project_root_for(session)

    @app.get("/connections/import")
    async def discover_connections(request: Request) -> JSONResponse:
        guard(request)
        project = project_of(request.query_params.get("session"))
        return _answer(await asyncio.to_thread(connections.discover, project))

    @app.post("/connections/import")
    async def import_connections(request: Request) -> JSONResponse:
        guard(request)
        body = await _json_object(request)
        keys = body.get("keys") if body is not None else None
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            return JSONResponse(status_code=400, content={"error": "keys must be a list of strings"})
        project = project_of(body.get("session"))
        return _answer(await asyncio.to_thread(connections.import_servers, keys, project))
