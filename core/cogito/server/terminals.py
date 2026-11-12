"""Routes for the terminal tabs beside «Hilo» (app only, design 2026-11-01). The session's
engine holds them (cli.py's ``EngineSession.terminals``, a `tools.terminal.host.TerminalHost`
there, `HostLike` here); this module checks shapes and runs the blocking parts off the event
loop. Every route needs the bearer token; an unknown session or tab is a 404.

- ``GET /sessions/{id}/terminals`` -> ``{"available": bool, "tabs": [Tab]}``, where ``Tab``
  is ``{"id", "title", "cwd", "running", "opened_by": "person"|"model", "touched",
  "exited"}``. ``{"available": false, "tabs": []}`` when the session has no terminals
  (Windows, or a core the desktop app did not start).
- ``POST /sessions/{id}/terminals`` ``{"command"?, "cols"?, "rows"?}`` -> ``Tab``. With
  ``command`` (the window's ▶ on a reply's code block, the person's own click), it is typed
  once the shell has drawn its first prompt, however long that takes. It may hold several
  lines (at most 16,000 characters): no control character but the newline, and none of the
  invisible or text-direction characters `FORMAT_CHARS` lists. 409 without terminals.
- ``GET /sessions/{id}/terminals/{tab}`` -> ``Tab`` plus ``"scrollback"``, the raw output
  kept (at most 256 KiB), to replay into xterm when a window reopens, and ``"end"``, the
  output offset it ends at (``terminal_data``'s ``end``, in UTF-16 code units).
- ``POST /sessions/{id}/terminals/{tab}/input`` ``{"data"}`` -> ``{"status": "ok"}``; the tab
  is the person's from then on (``touched``).
- ``POST /sessions/{id}/terminals/{tab}/resize`` ``{"cols", "rows"}``, each 2 to 1000 ->
  ``{"status": "ok"}``.
- ``DELETE /sessions/{id}/terminals/{tab}`` -> ``{"status": "closed"}``: its shell and
  everything it started end.

The output itself goes out as session events (``terminal_opened``, ``terminal_data``,
``terminal_status``, ``terminal_exited``), from the tabs' own reader threads. A ``command``
given when opening that is not typed after all says so with ``terminal_unsent`` ``{id,
reason}``: ``"busy"`` when the person ran something in the tab first that still holds it
(the line would have gone to that program), ``"not_ready"`` when the shell never reached its
prompt (`tools.terminal.host`).

`chat`, and so every benchmark task, has no terminals, so nothing here changes a request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

#: The most one input request may carry, in characters (a paste; the window sends a larger
#: one in pieces). The desktop's main process holds the same limits.
MAX_INPUT = 64 * 1024
#: The longest text the Run button may type, lines included.
MAX_COMMAND = 16_000
_MIN_SIZE, _MAX_SIZE = 2, 1000
#: Zero-width and direction marks, bidi embeddings, overrides and isolates, the BOM and the
#: C1 controls: shown in the reply, they can make the block read differently from what is
#: typed. The same set as `tools.terminal.tools.FORMAT_CHARS`, which this layer may not
#: import.
FORMAT_CHARS = frozenset(
    [chr(c) for c in range(0x200B, 0x2010)]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + ["\ufeff"]
    + [chr(c) for c in range(0x80, 0xA0)]
)


class TabLike(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def snapshot(self) -> tuple[str, int]: ...


class HostLike(Protocol):
    available: bool

    def tabs(self) -> list[dict[str, Any]]: ...

    def tab(self, tab_id: str) -> TabLike | None: ...

    def open(
        self,
        *,
        command: str | None = None,
        cols: int | None = None,
        rows: int | None = None,
        opened_by: str = "person",
        focus: bool = True,
    ) -> TabLike: ...

    def input(self, tab: Any, data: str) -> None: ...

    def resize(self, tab: Any, cols: int, rows: int) -> None: ...

    def close(self, tab_id: str) -> bool: ...


#: ``(found, host)`` for a session id: ``found`` False for no such session, ``host`` None
#: for a session without terminals.
HostFor = Callable[[str], "tuple[bool, HostLike | None]"]


def _not_found(what: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": f"{what} not found"})


def _bad(message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": message})


async def _body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _size(body: dict[str, Any], key: str, required: bool) -> tuple[int | None, str | None]:
    value = body.get(key)
    if value is None:
        return None, (f"{key} is required" if required else None)
    if isinstance(value, bool) or not isinstance(value, int) or not _MIN_SIZE <= value <= _MAX_SIZE:
        return None, f"{key} must be a whole number from {_MIN_SIZE} to {_MAX_SIZE}"
    return value, None


def register(app: FastAPI, *, authorized: Callable[[Request], bool], host_for: HostFor) -> None:
    """Wire the terminal routes. ``host_for`` maps a session id to its terminals."""

    def check(request: Request) -> None:
        if not authorized(request):
            raise HTTPException(status_code=401, detail="missing or wrong token")

    def find_tab(session_id: str, tab_id: str) -> tuple[HostLike | None, Any, JSONResponse | None]:
        found, host = host_for(session_id)
        if not found:
            return None, None, _not_found("session")
        tab = host.tab(tab_id) if host is not None else None
        if tab is None:
            return None, None, _not_found("terminal")
        return host, tab, None

    @app.get("/sessions/{session_id}/terminals")
    async def list_terminals(session_id: str, request: Request) -> JSONResponse:
        check(request)
        found, host = host_for(session_id)
        if not found:
            return _not_found("session")
        if host is None or not getattr(host, "available", False):
            return JSONResponse({"available": False, "tabs": []})
        return JSONResponse({"available": True, "tabs": host.tabs()})

    @app.post("/sessions/{session_id}/terminals")
    async def open_terminal(session_id: str, request: Request) -> JSONResponse:
        check(request)
        found, host = host_for(session_id)
        if not found:
            return _not_found("session")
        if host is None or not getattr(host, "available", False):
            return JSONResponse(status_code=409, content={"error": "terminals not available"})
        body = await _body(request)
        if body is None:
            body = {}
        command = body.get("command")
        if command is not None:
            if not isinstance(command, str):
                return _bad("command must be a string")
            command = command.strip()
            if any((char < " " and char != "\n") or char == "\x7f" for char in command):
                return _bad("command must hold no control character but the newline")
            if any(char in FORMAT_CHARS for char in command):
                return _bad("command holds an invisible or text-direction character")
            if len(command) > MAX_COMMAND:
                return _bad(f"command is over {MAX_COMMAND} characters")
        cols, error = _size(body, "cols", False)
        if error:
            return _bad(error)
        rows, error = _size(body, "rows", False)
        if error:
            return _bad(error)
        try:
            tab = await asyncio.to_thread(
                host.open, command=command or None, cols=cols, rows=rows, opened_by="person"
            )
        except (OSError, RuntimeError) as exc:
            return JSONResponse(
                status_code=500, content={"error": f"the terminal did not start: {exc}"}
            )
        return JSONResponse(tab.describe())

    @app.get("/sessions/{session_id}/terminals/{tab_id}")
    async def get_terminal(session_id: str, tab_id: str, request: Request) -> JSONResponse:
        check(request)
        _, tab, missing = find_tab(session_id, tab_id)
        if missing is not None:
            return missing
        scrollback, end = tab.snapshot()
        return JSONResponse({**tab.describe(), "scrollback": scrollback, "end": end})

    @app.post("/sessions/{session_id}/terminals/{tab_id}/input")
    async def terminal_input(session_id: str, tab_id: str, request: Request) -> JSONResponse:
        check(request)
        host, tab, missing = find_tab(session_id, tab_id)
        if missing is not None:
            return missing
        body = await _body(request)
        data = body.get("data") if body is not None else None
        if not isinstance(data, str):
            return _bad("data must be a string")
        if len(data) > MAX_INPUT:
            return _bad(f"data is over {MAX_INPUT} characters")
        await asyncio.to_thread(host.input, tab, data)
        return JSONResponse({"status": "ok"})

    @app.post("/sessions/{session_id}/terminals/{tab_id}/resize")
    async def terminal_resize(session_id: str, tab_id: str, request: Request) -> JSONResponse:
        check(request)
        host, tab, missing = find_tab(session_id, tab_id)
        if missing is not None:
            return missing
        body = await _body(request)
        if body is None:
            return _bad("expected a JSON object with cols and rows")
        cols, error = _size(body, "cols", True)
        if error:
            return _bad(error)
        rows, error = _size(body, "rows", True)
        if error:
            return _bad(error)
        await asyncio.to_thread(host.resize, tab, cols, rows)
        return JSONResponse({"status": "ok"})

    @app.delete("/sessions/{session_id}/terminals/{tab_id}")
    async def close_terminal(session_id: str, tab_id: str, request: Request) -> JSONResponse:
        check(request)
        found, host = host_for(session_id)
        if not found:
            return _not_found("session")
        if host is None or not await asyncio.to_thread(host.close, tab_id):
            return _not_found("terminal")
        return JSONResponse({"status": "closed"})
