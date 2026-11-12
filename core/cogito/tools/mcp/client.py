"""One MCP server over stdio: JSON-RPC 2.0, one message per line.

Only what a client needs to use a server's tools: `initialize`, the `initialized`
notification, `tools/list` with its cursor, `tools/call` and `notifications/cancelled`.
Read from the specification (modelcontextprotocol.io, 2025-06-18 revision, read
2026-10-24): stdio messages are newline-delimited and never contain a newline, and the
server may write logs to stderr, which the client may ignore. The last of those lines are
kept, so a server that exits says why.

The server lives as long as this process: its own process group, killed with the group
on close; on Linux the kernel kills the direct child if this process dies, and on Windows
the kill-on-close job does (`cogito.procs`).
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import ctypes
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cogito import procs
from cogito.cancel import CancelToken

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
_PR_SET_PDEATHSIG = 1
#: How much of a server's stderr an exit message quotes: its last lines, clipped.
_STDERR_LINES = 6
_STDERR_CHARS = 600
#: How many stderr lines are kept for the Conexiones tab to show while a server starts.
_LOG_LINES = 40


class McpError(Exception):
    """The server could not be started, answered with an error, or went away."""


@dataclass(frozen=True, slots=True)
class RemoteTool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    title: str = ""
    annotations: Mapping[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.annotations.get("readOnlyHint") is True

    @property
    def destructive(self) -> bool:
        """The specification's default: a tool that is not read-only may destroy, unless
        it says it does not (`destructiveHint` false)."""
        if self.read_only:
            return False
        return self.annotations.get("destructiveHint") is not False


@dataclass(frozen=True, slots=True)
class CallOutcome:
    text: str
    is_error: bool
    images: int = 0
    #: The image parts, as their bytes (base64 decoded), for a model that sees them.
    pictures: tuple[bytes, ...] = ()


def _die_with_parent() -> None:
    with contextlib.suppress(OSError):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


class Session:
    """What every transport does the same: the handshake, `tools/list` and `tools/call`.
    A transport supplies `_request` and `_notify`."""

    name: str
    instructions: str = ""
    server_name: str = ""
    #: The version the server answered `initialize` with.
    protocol_version: str = PROTOCOL_VERSION

    def start(self, timeout: float) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def wait(self) -> None:
        """Until the server is gone. A transport with no process of its own returns."""

    def log_tail(self) -> tuple[str, ...]:
        """The last lines the server wrote to stderr, oldest first. Empty for a transport
        with no process of its own."""
        return ()

    @property
    def running(self) -> bool:
        raise NotImplementedError

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        timeout: float,
        cancel: CancelToken | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _notify(self, method: str, params: Mapping[str, Any] | None) -> None:
        raise NotImplementedError

    def _initialize(self, timeout: float) -> None:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cogito", "version": "1"},
            },
            timeout,
        )
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.instructions = str(result.get("instructions") or "")
        self.server_name = str((result.get("serverInfo") or {}).get("name") or self.name)
        self._notify("notifications/initialized", None)

    def list_tools(self, timeout: float) -> list[RemoteTool]:
        tools: list[RemoteTool] = []
        cursor: str | None = None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            for raw in result.get("tools") or ():
                if not isinstance(raw, Mapping) or not raw.get("name"):
                    continue
                tools.append(
                    RemoteTool(
                        name=str(raw["name"]),
                        description=str(raw.get("description") or ""),
                        input_schema=raw.get("inputSchema") or {"type": "object"},
                        title=str(raw.get("title") or ""),
                        annotations=dict(raw.get("annotations") or {}),
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        timeout: float,
        cancel: CancelToken | None = None,
    ) -> CallOutcome:
        result = self._request(
            "tools/call", {"name": name, "arguments": dict(arguments)}, timeout, cancel
        )
        return _outcome(result)


class McpClient(Session):
    def __init__(
        self,
        name: str,
        command: str,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.name = name
        self._command = [command, *args]
        self._env = {**os.environ, **(env or {})}
        self._cwd = cwd
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self._arrived = threading.Condition()
        self._stderr: deque[str] = deque(maxlen=_LOG_LINES)
        self._stderr_reader: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self.instructions = ""
        self.server_name = ""

    # -- lifetime ------------------------------------------------------------------

    def start(self, timeout: float) -> None:
        options: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self._env,
            "cwd": self._cwd,
        }
        # `npx` is `npx.cmd` on Windows, which Popen does not find by its bare name.
        found = shutil.which(self._command[0], path=self._env.get("PATH"))
        command = [found or self._command[0], *self._command[1:]]
        try:
            if procs.IS_WINDOWS:
                process = subprocess.Popen(command, creationflags=procs.NEW_GROUP, **options)
                procs.bind_to_this_process(process)
            else:
                process = subprocess.Popen(
                    command, start_new_session=True, preexec_fn=_die_with_parent, **options
                )
        except OSError as exc:
            raise McpError(f"could not start {self._command[0]!r}: {exc}") from exc
        self._process = process
        atexit.register(self.close)
        self._reader = threading.Thread(target=self._read, name=f"mcp-{self.name}", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name=f"mcp-{self.name}-stderr", daemon=True
        )
        self._stderr_reader.start()
        try:
            self._initialize(timeout)
        except McpError as exc:
            # A start that timed out said nothing of what the server printed meanwhile,
            # which is most often why ("npm ERR! 404", a missing key).
            if "no answer" in str(exc) and (said := self._said()):
                raise McpError(f"{exc}; it printed: {said}") from exc
            raise

    def log_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr)

    def _said(self) -> str:
        """The last stderr lines, joined and clipped, as an error quotes them."""
        said = " | ".join(list(self._stderr)[-_STDERR_LINES:])
        if len(said) > _STDERR_CHARS:
            said = "…" + said[-_STDERR_CHARS:]
        return said

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        with contextlib.suppress(OSError, ValueError):
            process.stdin.close()  # type: ignore[union-attr]
        if procs.IS_WINDOWS:
            procs.terminate_trees([process.pid], grace=2.0)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
        with self._arrived:
            self._arrived.notify_all()

    def wait(self) -> None:
        process = self._process
        if process is not None:
            with contextlib.suppress(OSError):
                process.wait()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- requests ------------------------------------------------------------------

    # -- wire ----------------------------------------------------------------------

    def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError(f"MCP server {self.name!r} is not running")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
                process.stdin.write(line.encode("utf-8") + b"\n")
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"MCP server {self.name!r} closed its input: {exc}") from exc

    def _notify(self, method: str, params: Mapping[str, Any] | None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._send(message)

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
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        deadline = time.monotonic() + timeout
        with self._arrived:
            while request_id not in self._pending:
                if not self.running:
                    raise McpError(self._exited())
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

    def _exited(self) -> str:
        process = self._process
        code = process.poll() if process is not None else None
        # The reader may still be taking the last lines in; it ends when the pipe closes.
        if self._stderr_reader is not None:
            self._stderr_reader.join(0.5)
        said = self._said()
        status = f" with code {code}" if code is not None else ""
        return f"MCP server {self.name!r} exited{status}" + (f": {said}" if said else "")

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr.append(line)

    def _cancelled(self, request_id: int, reason: str) -> None:
        with contextlib.suppress(McpError):
            self._notify(
                "notifications/cancelled", {"requestId": request_id, "reason": reason}
            )

    def _read(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                log.debug("%s wrote a line that is not JSON: %.200s", self.name, line)
                continue
            if not isinstance(message, dict):
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self._arrived:
                    self._pending[message["id"]] = message
                    self._arrived.notify_all()
            elif "id" in message and "method" in message:
                # A request from the server (sampling, roots, ping). None is offered in
                # `initialize`, so each is declined; `ping` is answered as the spec asks.
                if message["method"] == "ping":
                    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                else:
                    reply = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "not supported by this client"},
                    }
                with contextlib.suppress(McpError):
                    self._send(reply)
        with self._arrived:
            self._arrived.notify_all()


def _outcome(result: Mapping[str, Any]) -> CallOutcome:
    """A `tools/call` result as text. Text parts as written; an embedded resource's text;
    image parts kept as bytes for the hub to show (`tools.base.result_image`), audio counted;
    structured content only when there is no text, since the specification asks a server to
    send both."""
    parts: list[str] = []
    images = 0
    pictures: list[bytes] = []
    for block in result.get("content") or ():
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource":
            resource = block.get("resource") or {}
            if resource.get("text"):
                parts.append(str(resource["text"]))
            else:
                parts.append(f"[resource {resource.get('uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[resource {block.get('uri', '')}: {block.get('name', '')}]")
        elif kind == "image":
            data = b""
            with contextlib.suppress(ValueError, TypeError):
                data = base64.b64decode(str(block.get("data") or ""), validate=True)
            if data:
                pictures.append(data)
            else:
                images += 1
        elif kind == "audio":
            images += 1
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=1))
    if images:
        parts.append(f"[{images} image or audio part(s) not shown]")
    return CallOutcome(
        text="\n".join(parts) if parts or pictures else "(the tool returned nothing)",
        is_error=bool(result.get("isError")),
        images=images,
        pictures=tuple(pictures),
    )
