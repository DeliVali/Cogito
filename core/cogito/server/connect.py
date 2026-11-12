"""Routes for the welcome and the connection screen (design 1d-1e, and «Conectar otro
servicio…» in the model menu). cli.py's `_Connect` (`ConnectLike` here) does the work.
This module checks shapes, runs it off the event loop and reports the result.

Every route takes the app's bearer token. No route answers, logs or raises with an API key
in it: a key arrives in a request body, goes to `ConnectLike` and nowhere else.

``GET /settings/welcome``: ``{"name": str|null, "done": bool, "model": {"id","label",
"origin","service"}|null, "working": bool}``. That is the name from step 1, whether the
welcome is over, the model served now and whether it can be used (the welcome offers to
skip step 2 only when it can).

``PUT /settings/welcome {"name"?: str|null, "done"?: bool}``: 200 with the same shape; 400
``{"error"}`` for another key or a wrong type.

``GET /connect/providers``: the services «SERVICIO» lists, from `provider.connect`'s
table: ``{"providers": [{"id","name","api","custom_url","base_url","key_page"}],
"key_pages": {id: url}}``. The desktop's main process opens the addresses; the page
itself only names a service.

``POST /connect/api/models {"provider", "key"?, "base_url"?}``: tests an API service by
fetching its real model list. ``{"ok": true, "endpoint", "models": [{"id","label"}]}``, or
``{"ok": false, "failure": {"kind","endpoint","status","message","host","retry_after",
"log"}}``. A refused key is an answer, not an HTTP error. 400 for an unreadable body.

``POST /connect/motoris/install {"checkout", "config"}``: reads a Cogito Motoris install
without running it. ``{"ok": true, "checkout", "config", "model", "ctx"}``, or ``{"ok":
false, "failure": {...}}`` (kind ``not_motoris``).

``POST /connect/motoris/running {"url"?, "key"?}``: asks a running Cogito Motoris (default
http://127.0.0.1:8095/v1) for its models. ``{"ok": true, "models": [id]}``, or ``{"ok":
false, "failure": {...}}`` (kind ``no_server``, ``not_motoris`` or ``plain_http``). A key
goes to that address only.

``POST /connect/save {"kind": "api" | "motoris", "model", "label"?, ... , "activate"?}``: adds the
entry. 201 ``{"model": {...}, "switch"?: <the switch's answer>}``; 400 ``{"error"}``. With ``activate``,
the new model is served from then on through the same guarded switch as the picker's
``POST /sessions/{id}/model`` (``switch`` carries its answer, a refusal included).

``POST /connect/activate {"model_id"}``: serve a catalog model through that same switch,
with no session needed. Answer and status are what the picker's route gives.

``GET /settings/welcome`` also carries ``unserved``: why the model the core started with is
not served (``{"reason": "bench_running"|"failed", "model", "log"}``), or null.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from cogito.errors import ConfigError

log = logging.getLogger(__name__)


class ConnectLike(Protocol):
    def welcome(self) -> Mapping[str, Any]: ...

    def set_welcome(self, values: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def providers(self) -> Mapping[str, Any]: ...

    def api_models(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def motoris_install(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def motoris_running(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def save(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...


async def _body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _answer(answer: Mapping[str, Any], ok_status: int = 200) -> JSONResponse:
    if "error" in answer:
        return JSONResponse(status_code=400, content={"error": str(answer["error"])})
    return JSONResponse(status_code=ok_status, content=dict(answer))


def register(
    app: FastAPI,
    *,
    authorized: Callable[[Request], bool],
    connect: ConnectLike,
    switch: Callable[[str], Awaitable[JSONResponse]] | None,
) -> None:
    """Add the routes above to ``app``. ``switch`` is the app's guarded model switch."""

    def guard(request: Request) -> None:
        if not authorized(request):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    async def run(method: Callable[..., Mapping[str, Any]], *args: Any) -> Mapping[str, Any]:
        """Run ``method`` off the event loop. A bug in it is logged by exception type only,
        since its message could hold what was sent, and answered as a plain failure."""
        try:
            return await asyncio.to_thread(method, *args)
        except ConfigError as exc:
            # A configuration error names the file and the problem, never a value it holds
            # (`config.secrets`), so it is shown as is instead of a bare "failed".
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never a traceback that could carry a key
            log.error(
                "connect: %s failed with %s", getattr(method, "__name__", "?"), type(exc).__name__
            )
            return {"error": "failed"}

    @app.get("/settings/welcome")
    async def get_welcome(request: Request) -> JSONResponse:
        guard(request)
        return _answer(await run(connect.welcome))

    @app.put("/settings/welcome")
    async def put_welcome(request: Request) -> JSONResponse:
        guard(request)
        body = await _body(request)
        if body is None:
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        return _answer(await run(connect.set_welcome, body))

    @app.get("/connect/providers")
    async def get_providers(request: Request) -> JSONResponse:
        guard(request)
        return _answer(await run(connect.providers))

    def posting(path: str, method_name: str) -> None:
        async def handler(request: Request) -> JSONResponse:
            guard(request)
            body = await _body(request)
            if body is None:
                return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
            return _answer(await run(getattr(connect, method_name), body))

        handler.__name__ = f"post_{method_name}"
        app.post(path)(handler)

    posting("/connect/api/models", "api_models")
    posting("/connect/motoris/install", "motoris_install")
    posting("/connect/motoris/running", "motoris_running")

    @app.post("/connect/activate")
    async def post_activate(request: Request) -> JSONResponse:
        """Serve a catalog model from the connection screen, before any session exists.
        Used by «Reintentar» on a model the core could not start with, or after the save's
        own activation was refused. Same guarded switch as the picker's route."""
        guard(request)
        body = await _body(request)
        model_id = body.get("model_id") if body is not None else None
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
            return JSONResponse(status_code=400, content={"error": "model_id must be a model id"})
        if switch is None:
            return JSONResponse(status_code=503, content={"error": "model switching unavailable"})
        return await switch(model_id.strip())

    @app.post("/connect/save")
    async def post_save(request: Request) -> JSONResponse:
        guard(request)
        body = await _body(request)
        if body is None:
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        activate = body.pop("activate", False)
        if not isinstance(activate, bool):
            return JSONResponse(
                status_code=400, content={"error": "activate must be true or false"}
            )
        answer = dict(await run(connect.save, body))
        if "error" in answer:
            return _answer(answer)
        if activate and switch is not None:
            switched = await switch(str(answer["model"]["id"]))
            answer["switch"] = json.loads(bytes(switched.body))
        return JSONResponse(status_code=201, content=answer)
