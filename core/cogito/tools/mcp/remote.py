"""One MCP server reached at an address: Streamable HTTP, or the older HTTP+SSE.

Read from the specification's Transports section (modelcontextprotocol.io, 2025-06-18
revision, read 2026-10-29):

- Every message is its own POST to the endpoint, with `Accept: application/json,
  text/event-stream`. A request is answered with one JSON object or an SSE stream that
  carries the response (and may carry the server's own requests first); a notification or
  a response is answered 202.
- An `Mcp-Session-Id` the server gives on `initialize` goes on every later request; a 404
  with it means the session ended, and the client starts a new one. `MCP-Protocol-Version`
  goes on every request after `initialize`. A DELETE ends the session.
- Backwards compatibility: when the `initialize` POST fails with a 4xx, the client GETs the
  same address, expecting an SSE stream whose first event, `endpoint`, says where to POST;
  answers then come on that stream (the 2024-11-05 HTTP+SSE transport).

`Authorization` is the person's own header when the table sets one, else the token signed
in for this address (`oauth.py`). A 401 is refreshed once, then reported as a sign-in the
person has to do.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from cogito.cancel import CancelToken
from cogito.tools.mcp import oauth
from cogito.tools.mcp.client import McpError, Session

log = logging.getLogger(__name__)

_CONNECT = 15.0
_ACCEPT = "application/json, text/event-stream"
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class NeedsLogin(McpError):
    """The server wants a token Cogito does not hold: the person signs in."""

    def __init__(self, name: str, challenge: oauth.Challenge) -> None:
        super().__init__(f"MCP server {name!r} needs you to sign in")
        self.challenge = challenge


class _Fallback(Exception):
    """`initialize` was refused with a 4xx: try the older HTTP+SSE transport."""


def expand(value: str, environ: Mapping[str, str] | None = None) -> str:
    """`${VAR}` and `${VAR:-default}` from the environment, as Claude Code's `.mcp.json`
    reads them in `url` and `headers`."""
    environ = os.environ if environ is None else environ
    return _VARIABLE.sub(lambda match: environ.get(match.group(1), match.group(2) or ""), value)


def _sse(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    """(event, data) for each event of a stream (the WHATWG rules a client needs)."""
    event, data = "", []
    for line in lines:
        if line == "":
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
        elif line.startswith(":"):
            continue
        else:
            key, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                event = value
            elif key == "data":
                data.append(value)
    if data:
        yield event or "message", "\n".join(data)


class HttpMcpClient(Session):
    def __init__(
        self,
        name: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        transport: str = "http",
        client_id: str = "",
    ) -> None:
        self.name = name
        self.url = expand(url)
        self._headers = {key: expand(value) for key, value in (headers or {}).items()}
        self._transport = transport
        self.client_id = client_id
        self._http: httpx.Client | None = None
        self._session_id = ""
        self._initialized = False
        self._legacy_endpoint = ""
        self._legacy_alive = False
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self._failed: dict[int, McpError] = {}
        self._arrived = threading.Condition()
        self._refreshed = False

    # -- lifetime ------------------------------------------------------------------

    def start(self, timeout: float) -> None:
        self._http = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(_CONNECT, read=None),
        )
        if self._transport == "sse":
            self._open_legacy(timeout)
            self._initialize(timeout)
            return
        try:
            self._initialize(timeout)
        except _Fallback:
            self._open_legacy(timeout)
            self._initialize(timeout)
        self._initialized = True

    def close(self) -> None:
        http, self._http = self._http, None
        if http is None:
            return
        if self._session_id and not self._legacy_endpoint:
            with contextlib.suppress(httpx.HTTPError):
                http.delete(self.url, headers=self._request_headers(), timeout=5.0)
        http.close()
        self._legacy_alive = False
        with self._arrived:
            self._arrived.notify_all()

    @property
    def running(self) -> bool:
        if self._http is None:
            return False
        return self._legacy_alive if self._legacy_endpoint else True

    # -- headers -------------------------------------------------------------------

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": _ACCEPT, **self._headers}
        if not any(key.lower() == "authorization" for key in headers) and self._http is not None:
            token = oauth.bearer(self.url, self._http)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    # -- requests ------------------------------------------------------------------

    def _notify(self, method: str, params: Mapping[str, Any] | None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        if method == "notifications/initialized":
            self._initialized = True
        self._post(message, request_id=None)

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout: float,
        cancel: CancelToken | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        if method == "initialize":
            # Asked in this thread, so a refusal decides the fallback before anything else.
            self._post(message, request_id, first=True, deadline=timeout)
        else:
            threading.Thread(
                target=self._post_quietly, args=(message, request_id),
                name=f"mcp-{self.name}-post", daemon=True,
            ).start()
        deadline = time.monotonic() + timeout
        with self._arrived:
            while request_id not in self._pending:
                if request_id in self._failed:
                    raise self._failed.pop(request_id)
                if self._http is None:
                    raise McpError(f"MCP server {self.name!r} was closed")
                if self._legacy_endpoint and not self._legacy_alive:
                    raise McpError(f"MCP server {self.name!r} closed its event stream")
                if cancel is not None and cancel.cancelled:
                    self._cancelled(request_id, cancel.reason)
                    raise McpError("cancelled")
                left = deadline - time.monotonic()
                if left <= 0:
                    self._cancelled(request_id, "timed out")
                    raise McpError(f"{method} got no answer from {self.name!r} in {timeout:.0f} s")
                self._arrived.wait(min(0.25, left))
            reply = self._pending.pop(request_id)
        if "error" in reply:
            error = reply["error"] or {}
            raise McpError(str(error.get("message") or error))
        return dict(reply.get("result") or {})

    def _cancelled(self, request_id: int, reason: str) -> None:
        with contextlib.suppress(McpError):
            self._notify("notifications/cancelled", {"requestId": request_id, "reason": reason})

    def _post_quietly(self, message: dict[str, Any], request_id: int) -> None:
        try:
            self._post(message, request_id)
        except McpError as exc:
            with self._arrived:
                self._failed[request_id] = exc
                self._arrived.notify_all()

    def _post(
        self,
        message: dict[str, Any],
        request_id: int | None,
        first: bool = False,
        deadline: float | None = None,
    ) -> None:
        """One message out. Its answer, if it has one, is delivered to `_pending` (on the
        event stream, for the older transport)."""
        http = self._http
        if http is None:
            raise McpError(f"MCP server {self.name!r} is not connected")
        target = self._legacy_endpoint or self.url
        for attempt in range(2):
            try:
                # Only `initialize` is asked in the caller's thread, so only it needs a read
                # deadline here; every other request is waited on in `_request`.
                read = httpx.Timeout(_CONNECT, read=deadline) if deadline else None
                options = {"timeout": read} if read else {}
                with http.stream("POST", target, json=message, headers=self._request_headers(), **options) as response:
                    status = response.status_code
                    if status == 401 or (status == 403 and "insufficient_scope" in response.headers.get("www-authenticate", "")):
                        challenge = oauth.read_challenge(response.headers.get("www-authenticate", ""))
                        if attempt == 0 and status == 401 and self._refresh():
                            continue
                        raise NeedsLogin(self.name, challenge)
                    if status == 404 and self._session_id and not first and attempt == 0:
                        # The session ended: a new one, then the message again.
                        self._session_id = ""
                        self._initialized = False
                        self._initialize(_CONNECT)
                        continue
                    if first and 400 <= status < 500 and self._transport != "sse" and not self._legacy_endpoint:
                        raise _Fallback()
                    if status >= 400:
                        response.read()
                        raise McpError(f"MCP server {self.name!r} answered HTTP {status}: {response.text[:300]}")
                    if first and response.headers.get("mcp-session-id"):
                        self._session_id = response.headers["mcp-session-id"]
                    if status == 202 or self._legacy_endpoint:
                        return
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind == "text/event-stream":
                        for _event, data in _sse(response.iter_lines()):
                            self._take(data)
                            if request_id is not None and self._answered(request_id):
                                return
                        return
                    response.read()
                    if response.content.strip():
                        self._take(response.text)
                    return
            except httpx.TimeoutException as exc:
                raise McpError(f"{message.get('method')} got no answer from {self.name!r} in {deadline or _CONNECT:.0f} s") from exc
            except httpx.HTTPError as exc:
                raise McpError(f"could not reach MCP server {self.name!r}: {exc}") from exc
        raise McpError(f"MCP server {self.name!r} refused the request twice")

    def _refresh(self) -> bool:
        if self._refreshed or self._http is None or any(k.lower() == "authorization" for k in self._headers):
            return False
        self._refreshed = True
        grant = oauth.Grant.load(self.url)
        return bool(grant and oauth.refresh(self.url, grant, self._http))

    def _answered(self, request_id: int) -> bool:
        with self._arrived:
            return request_id in self._pending

    def _take(self, text: str) -> None:
        """A message (or a batch) from the server: an answer is delivered; a request is
        answered, `ping` as the spec asks and anything else as not supported."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.debug("%s sent a message that is not JSON: %.200s", self.name, text)
            return
        for message in data if isinstance(data, list) else [data]:
            if not isinstance(message, dict):
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._arrived:
                    self._pending[message["id"]] = message
                    self._arrived.notify_all()
            elif "id" in message and "method" in message:
                if message["method"] == "ping":
                    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                else:
                    reply = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "not supported by this client"},
                    }
                threading.Thread(
                    target=self._post_quietly, args=(reply, -1), daemon=True,
                ).start()

    # -- the older HTTP+SSE transport ---------------------------------------------------

    def _open_legacy(self, timeout: float) -> None:
        ready = threading.Event()
        problem: list[McpError] = []

        def listen() -> None:
            http = self._http
            if http is None:
                return
            try:
                with http.stream("GET", self.url, headers={**self._request_headers(), "Accept": "text/event-stream"}) as response:
                    if response.status_code == 401:
                        challenge = oauth.read_challenge(response.headers.get("www-authenticate", ""))
                        problem.append(NeedsLogin(self.name, challenge))
                        return
                    if response.status_code != 200:
                        problem.append(McpError(
                            f"MCP server {self.name!r} answered HTTP {response.status_code} to both transports"
                        ))
                        return
                    self._legacy_alive = True
                    for event, data in _sse(response.iter_lines()):
                        if event == "endpoint" and not self._legacy_endpoint:
                            self._legacy_endpoint = urllib.parse.urljoin(self.url, data.strip())
                            ready.set()
                        elif event == "message":
                            self._take(data)
            except httpx.HTTPError as exc:
                problem.append(McpError(f"could not reach MCP server {self.name!r}: {exc}"))
            finally:
                self._legacy_alive = False
                ready.set()
                with self._arrived:
                    self._arrived.notify_all()

        threading.Thread(target=listen, name=f"mcp-{self.name}-sse", daemon=True).start()
        if not ready.wait(timeout):
            raise McpError(f"MCP server {self.name!r} sent no endpoint in {timeout:.0f} s")
        if problem:
            raise problem[0]
        if not self._legacy_endpoint:
            raise McpError(f"MCP server {self.name!r} closed its event stream before naming an endpoint")
