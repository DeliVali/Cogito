"""The in-app browser's bridge to the desktop's main process (app only, contract of
2026-11-05, cogito-lab research/2026-11-05-native-browser/CONTRACT.md).

Main owns the browser tabs: Electron's own Chromium, one `WebContentsView` per tab, driven
through each tab's `webContents.debugger`. The core never speaks CDP. It asks main for a
named operation by emitting, on the session's event stream,

    {"type": "browser_command", "id": "<32 hex>", "op": "<op>", "args": {...}}

and main answers on ``POST /sessions/{id}/browser/answers/{answer_id}`` (bearer token) with
``{"ok": true, "result": {...}}`` or ``{"ok": false, "error": "<message>", "kind":
"<kind>"}``. The route answers 401 without the token, 404 for an unknown session or for an
id nobody is waiting for (late, duplicate or unknown), 413 for a body over 8 MB and 409 for
a body that is not one of those two shapes. A waiting call is handed the 413 and the 409 as
an error at once, so a turn never waits out its deadline over a bad answer; the id is then
spent, and a second answer to it is a 404.

`BrowserBridge.call` waits up to the op's deadline (the tool sets it: navigate 35 s, the
rest 15 s, `wait` its seconds and 5) and gives up at «Detener», the session's cancel token,
with kind ``"cancelled"``. Main answers inside those, within about 32 s for navigate and 12 s
for the rest, with ``"loading": true`` in the result when the page has not finished; the
deadlines are only the outer bound. Failures raise `BrowserError`, whose ``kind`` and
``message`` the `browser` tool reads by name: this layer does not import `tools/`, nor
`tools/` this one. Other fields of an error answer (main may add a ``code``) are left out.

`chat`, and so every benchmark task, has no main process to answer and no bridge: the tool
is not registered there.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from cogito.cancel import NEVER, CancelToken

#: The most one answer may carry, in bytes. Main cuts long text fields first and says so
#: with ``"cut": true``; a screenshot's base64 fits well under it.
MAX_ANSWER = 8 * 1024 * 1024
#: How often a waiting call looks at the cancel token, in seconds.
_POLL = 0.1


class BrowserError(Exception):
    """An operation that did not give a result: main's own ``kind`` (``stale_ref``,
    ``covered``, ``no_tab``, ...) or the bridge's (``timeout``, ``cancelled``, ``closed``,
    ``bad_answer``, ``too_large``)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class _Waiter:
    __slots__ = ("done", "answer")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.answer: Mapping[str, Any] | None = None


class BrowserBridge:
    """One session's operations in flight: each an id main answers once."""

    def __init__(self, emit: Callable[[dict[str, Any]], None], cancel: CancelToken = NEVER) -> None:
        self._emit = emit
        self._cancel = cancel
        self._lock = threading.Lock()
        self._waiting: dict[str, _Waiter] = {}
        self._closed = False

    def call(
        self,
        op: str,
        args: Mapping[str, Any],
        timeout: float = 15.0,
        cancel: CancelToken | None = None,
    ) -> dict[str, Any]:
        """Ask main for ``op`` and wait for its result. Raises `BrowserError`."""
        answer_id = secrets.token_hex(16)
        waiter = _Waiter()
        with self._lock:
            if self._closed:
                raise BrowserError("closed", "the conversation closed")
            self._waiting[answer_id] = waiter
        try:
            self._emit({"type": "browser_command", "id": answer_id, "op": op, "args": dict(args)})
            deadline = time.monotonic() + timeout
            tokens = [token for token in (self._cancel, cancel) if token is not None]
            while not waiter.done.wait(_POLL):
                if any(token.cancelled for token in tokens):
                    raise BrowserError("cancelled", "stopped")
                if time.monotonic() >= deadline:
                    raise BrowserError(
                        "timeout", f"the browser did not answer within {timeout:.0f} s"
                    )
        finally:
            with self._lock:
                self._waiting.pop(answer_id, None)
        answer = waiter.answer or {}
        if answer.get("ok") is True:
            return dict(answer["result"])
        raise BrowserError(str(answer.get("kind") or "failed"), str(answer.get("error") or ""))

    def waiting(self, answer_id: str) -> bool:
        with self._lock:
            return answer_id in self._waiting

    def answer(self, answer_id: str, body: Mapping[str, Any]) -> bool:
        """Hand ``body`` to the call waiting on ``answer_id``; False when none is."""
        with self._lock:
            waiter = self._waiting.pop(answer_id, None)
        if waiter is None:
            return False
        waiter.answer = body
        waiter.done.set()
        return True

    def close(self) -> None:
        """The session ends: every call still waiting fails at once."""
        with self._lock:
            self._closed = True
            waiting = list(self._waiting.items())
            self._waiting.clear()
        for _, waiter in waiting:
            waiter.answer = {"ok": False, "kind": "closed", "error": "the conversation closed"}
            waiter.done.set()


def _shape_error(body: Any) -> str | None:
    """Why ``body`` is not an answer, or None."""
    if not isinstance(body, dict):
        return "expected a JSON object"
    ok = body.get("ok")
    if ok is True:
        return None if isinstance(body.get("result"), dict) else "result must be an object"
    if ok is False:
        if not isinstance(body.get("error"), str):
            return "error must be a string"
        kind = body.get("kind")
        return None if kind is None or isinstance(kind, str) else "kind must be a string"
    return "ok must be true or false"


#: ``(found, bridge)`` for a session id: ``found`` False for no such session, ``bridge`` None
#: for a session without a browser.
BridgeFor = Callable[[str], "tuple[bool, BrowserBridge | None]"]


def register(app: FastAPI, *, authorized: Callable[[Request], bool], bridge_for: BridgeFor) -> None:
    """Wire the answers route. ``bridge_for`` maps a session id to its bridge."""

    def missing(what: str) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": f"{what} not found"})

    @app.post("/sessions/{session_id}/browser/answers/{answer_id}")
    async def browser_answer(session_id: str, answer_id: str, request: Request) -> JSONResponse:
        if not authorized(request):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        found, bridge = bridge_for(session_id)
        if not found:
            return missing("session")
        if bridge is None or not bridge.waiting(answer_id):
            return missing("command")
        too_large = JSONResponse(
            status_code=413, content={"error": f"the answer is over {MAX_ANSWER} bytes"}
        )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_ANSWER:
            bridge.answer(answer_id, _failed("too_large", "the page's answer was too large"))
            return too_large
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_ANSWER:
                bridge.answer(answer_id, _failed("too_large", "the page's answer was too large"))
                return too_large
        try:
            body = await asyncio.to_thread(json.loads, bytes(raw))
        except (ValueError, RecursionError):
            body = None
        error = _shape_error(body)
        if error is not None:
            bridge.answer(answer_id, _failed("bad_answer", f"the browser answered badly: {error}"))
            return JSONResponse(status_code=409, content={"error": error})
        if not bridge.answer(answer_id, body):
            return missing("command")
        return JSONResponse({"status": "ok"})


def _failed(kind: str, error: str) -> dict[str, Any]:
    return {"ok": False, "kind": kind, "error": error}
