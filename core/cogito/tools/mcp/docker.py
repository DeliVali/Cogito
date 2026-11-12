"""An MCP server that is a container: `docker run … IMAGE` (or `podman run`), as a README
gives it, run the way the image actually speaks.

Many images speak MCP over stdio, and the line works as written. Others serve HTTP inside
the container (a uvicorn on 0.0.0.0:8080) and never answer on stdin, and the person would
have to publish a port and connect to it by hand. Here the harness does that:

1. Docker is checked first (installed, the daemon up, the socket allowed), and the image is
   pulled when it is not here yet, so a slow download is not mistaken for a silent server.
2. The container starts as written, with a name Cogito chooses, and `initialize` is sent on
   stdin. If it answers, that is the server.
3. While it does not, the container's own TCP listeners are read (`docker exec … cat
   /proc/net/tcp*`, then the ports its output names). A stdio server listens on none, so a
   listener means the image serves HTTP. That container is stopped and started again with
   the port published on loopback only, and `/mcp` (Streamable HTTP, with the older SSE
   fallback), then `/sse`, is asked until it answers.
4. The port and path that worked are kept in the server's table (`http_port`,
   `http_path`), so the next start skips the probe.

Every container Cogito starts carries `--name cogito-mcp-…` and is stopped by that name on
close, since killing the `docker` client does not always stop the container.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from cogito.cancel import CancelToken
from cogito.tools.mcp.client import CallOutcome, McpClient, McpError, RemoteTool, Session
from cogito.tools.mcp.remote import HttpMcpClient, NeedsLogin

ENGINES = ("docker", "podman")
#: How long a quiet container is given before its listeners are read; a stdio server
#: answers `initialize` in well under this once its image is here.
_FIRST_LOOK = 3.0
_LOOK_EVERY = 1.0
_QUICK = 20.0
#: A first download of a large image (one the person used is 2 GB) runs on its own clock; the
#: Conexiones tab reads «Iniciando…» meanwhile.
_PULL = 900.0
_PATHS = ("/mcp", "/sse", "/")

#: `docker run` options that take a value (Docker CLI reference, read 2026-10-29); every
#: other option is a flag, and the first word that is neither is the image.
_VALUED = {
    "-a", "--attach", "--add-host", "--blkio-weight", "--cap-add", "--cap-drop",
    "--cgroup-parent", "--cgroupns", "--cidfile", "--cpu-period", "--cpu-quota",
    "--cpu-shares", "-c", "--cpus", "--cpuset-cpus", "--cpuset-mems", "--device",
    "--dns", "--dns-option", "--dns-search", "--domainname", "--entrypoint", "-e", "--env",
    "--env-file", "--expose", "--gpus", "--group-add", "--health-cmd", "--health-interval",
    "--health-retries", "--health-start-period", "--health-timeout", "-h", "--hostname",
    "--ip", "--ip6", "--ipc", "--isolation", "-l", "--label", "--label-file", "--link",
    "--log-driver", "--log-opt", "--mac-address", "-m", "--memory", "--memory-reservation",
    "--memory-swap", "--mount", "--name", "--network", "--net", "--network-alias",
    "--pid", "--platform", "-p", "--publish", "--pull", "--restart", "--runtime",
    "--security-opt", "--shm-size", "--stop-signal", "--stop-timeout", "--storage-opt",
    "--sysctl", "--tmpfs", "-u", "--user", "--ulimit", "--userns", "--uts", "-v",
    "--volume", "--volume-driver", "--volumes-from", "-w", "--workdir",
}
_LISTENING = re.compile(r"(?:0\.0\.0\.0|127\.0\.0\.1|localhost|\[::\]|::):(\d{2,5})\b")


class DockerError(McpError):
    """Docker itself is the problem: not installed, not running, not allowed, no image."""


def is_container(command: str, args: Sequence[str]) -> bool:
    return os.path.basename(command) in ENGINES and "run" in list(args)[:3]


def split_run(args: Sequence[str]) -> tuple[list[str], list[str], str, list[str]]:
    """`[global options] run [run options] IMAGE [command…]` into its four parts."""
    words = list(args)
    at = words.index("run")
    head, rest = words[:at], words[at + 1:]
    options: list[str] = []
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-"):
            break
        options.append(word)
        if word in _VALUED and "=" not in word:
            index += 1
            if index < len(rest):
                options.append(rest[index])
        index += 1
    if index >= len(rest):
        raise McpError("the docker line names no image")
    return head, options, rest[index], rest[index + 1:]


def _named(options: list[str]) -> str:
    for index, word in enumerate(options):
        if word == "--name" and index + 1 < len(options):
            return options[index + 1]
        if word.startswith("--name="):
            return word.split("=", 1)[1]
    return ""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _listeners(table: str) -> set[int]:
    """Ports in LISTEN (state 0A) in a /proc/net/tcp or tcp6 table."""
    ports = set()
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 3 and fields[3] == "0A":
            with contextlib.suppress(ValueError, IndexError):
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


class ContainerClient(Session):
    """A `docker run` server: over stdio when it answers there, else over HTTP on a port
    published for it. `remember(fields)` keeps what worked in the server's table."""

    def __init__(
        self,
        name: str,
        command: str,
        args: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        http_port: int = 0,
        http_path: str = "",
        remember: Callable[[dict[str, object]], None] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.name = name
        self._engine = shutil.which(command) or command
        self._args = list(args)
        self._env = env
        self._cwd = cwd
        self._http_port = http_port
        self._http_path = http_path
        self._remember = remember
        self._run = run or subprocess.run
        self._inner: Session | None = None
        self._attempt: McpClient | None = None
        self._container = ""
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    # -- the engine ------------------------------------------------------------------

    def _docker(self, *words: str, timeout: float = _QUICK) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(
                [self._engine, *words], capture_output=True, text=True, timeout=timeout,
                env={**os.environ, **(self._env or {})},
            )
        except FileNotFoundError as exc:
            raise DockerError(
                f"{os.path.basename(self._engine)} is not installed, so the {self.name!r} "
                "container cannot run"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(
                f"{os.path.basename(self._engine)} {words[0]} did not finish in {timeout:.0f} s"
            ) from exc

    def _preflight(self, image: str, timeout: float) -> None:
        engine = os.path.basename(self._engine)
        info = self._docker("info", "--format", "{{.ServerVersion}}")
        if info.returncode != 0:
            said = (info.stderr or info.stdout).strip()
            if "permission denied" in said.lower():
                raise DockerError(
                    f"this user may not use {engine} (permission denied on its socket); "
                    "add it to the docker group, or use rootless Docker"
                )
            raise DockerError(f"{engine} is installed but its service is not running: {said[:200]}")
        if self._docker("image", "inspect", image).returncode == 0:
            return
        pulled = self._docker("pull", image, timeout=max(timeout, _PULL))
        if pulled.returncode != 0:
            said = (pulled.stderr or pulled.stdout).strip().splitlines()
            raise DockerError(f"the image {image} could not be downloaded: {said[-1][:200] if said else 'no output'}")

    def _stop(self, container: str) -> None:
        if container:
            with contextlib.suppress(DockerError):
                self._docker("stop", "-t", "5", container)

    # -- lifetime --------------------------------------------------------------------

    def start(self, timeout: float) -> None:
        # A container outlives the `docker` client that was killed: stop it on exit too.
        atexit.register(self.close)
        head, options, image, rest = split_run(self._args)
        self._preflight(image, timeout)
        own = _named(options)
        if self._http_port:
            self._start_http(head, options, image, rest, own, self._http_port, timeout, [self._http_path or "/mcp"])
            return
        self._container = own or f"cogito-mcp-{self.name.lower()}-{secrets.token_hex(3)}"
        named = options if own else ["--name", self._container, *options]
        stdio = McpClient(self.name, self._engine, [*head, "run", *named, image, *rest], env=self._env, cwd=self._cwd)
        self._attempt = stdio
        # The stdio attempt runs on this thread: the server dies with the thread that
        # started it (`PR_SET_PDEATHSIG`). A watcher reads the container's listeners meanwhile
        # and, on finding one, closes the attempt so `start` returns here.
        found: list[int] = []
        done = threading.Event()

        def watch() -> None:
            if done.wait(_FIRST_LOOK):
                return
            while not done.is_set():
                ports = self._ports(stdio)
                if ports:
                    found.append(min(ports))
                    stdio.close()
                    return
                done.wait(_LOOK_EVERY)

        threading.Thread(target=watch, name=f"mcp-{self.name}-ports", daemon=True).start()
        started = time.monotonic()
        try:
            stdio.start(timeout)
        except McpError as exc:
            done.set()
            stdio.close()
            self._stop(self._container)
            if not found:
                if "no answer" in str(exc):
                    raise McpError(
                        f"the {self.name!r} container answered neither on stdin nor on a port "
                        f"in {timeout:.0f} s"
                    ) from exc
                raise
            left = max(1.0, timeout - (time.monotonic() - started))
            self._start_http(head, options, image, rest, own, found[0], left, list(_PATHS))
            return
        done.set()
        self._inner, self._attempt = stdio, None

    def _ports(self, stdio: McpClient) -> set[int]:
        found: set[int] = set()
        with contextlib.suppress(DockerError):
            table = self._docker("exec", self._container, "cat", "/proc/net/tcp", "/proc/net/tcp6")
            if table.returncode == 0:
                found = _listeners(table.stdout)
        if not found:
            # An image with no `cat`: the address its own output names.
            for line in list(getattr(stdio, "_stderr", ())):
                found.update(int(port) for port in _LISTENING.findall(line))
        return found

    def _start_http(
        self,
        head: list[str],
        options: list[str],
        image: str,
        rest: list[str],
        own: str,
        port: int,
        timeout: float,
        paths: list[str],
    ) -> None:
        host_port = _free_port()
        self._container = own or f"cogito-mcp-{self.name.lower()}-{secrets.token_hex(3)}"
        named = options if own else ["--name", self._container, *options]
        command = [self._engine, *head, "run", "-p", f"127.0.0.1:{host_port}:{port}", *named, image, *rest]
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env={**os.environ, **(self._env or {})}, cwd=self._cwd,
                start_new_session=True,
            )
        except OSError as exc:
            raise DockerError(f"could not start {os.path.basename(self._engine)}: {exc}") from exc
        # What the container prints, drained so a full pipe never blocks it; the last lines
        # say why it exited.
        said: deque[str] = deque(maxlen=6)
        threading.Thread(target=self._drain, args=(self._process, said), daemon=True).start()
        deadline = time.monotonic() + timeout
        last: McpError | None = None
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                time.sleep(0.2)
                tail = " | ".join(said)[-400:]
                raise McpError(
                    f"the {self.name!r} container exited with code {self._process.returncode}"
                    + (f": {tail}" if tail else "")
                )
            for path in paths:
                client = HttpMcpClient(self.name, f"http://127.0.0.1:{host_port}{path}")
                try:
                    client.start(min(10.0, max(1.0, deadline - time.monotonic())))
                except NeedsLogin:
                    raise
                except McpError as exc:
                    client.close()
                    last = exc
                    continue
                self._inner = client
                if self._remember and (port, path) != (self._http_port, self._http_path):
                    with contextlib.suppress(Exception):
                        self._remember({"http_port": port, "http_path": path})
                return
            time.sleep(_LOOK_EVERY)
        self.close()
        raise McpError(
            f"the {self.name!r} container listens on port {port}, but no MCP server answered "
            f"there in {timeout:.0f} s" + (f" ({last})" if last else "")
        )

    @staticmethod
    def _drain(process: subprocess.Popen[bytes], said: deque[str]) -> None:
        if process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                said.append(line)

    def close(self) -> None:
        with self._lock:
            attempt, self._attempt = self._attempt, None
            inner, self._inner = self._inner, None
            process, self._process = self._process, None
            container, self._container = self._container, ""
        for client in (attempt, inner):
            if client is not None:
                client.close()
        self._stop(container)
        if process is not None and process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)

    def wait(self) -> None:
        inner, process = self._inner, self._process
        if process is not None:
            with contextlib.suppress(OSError):
                process.wait()
        elif inner is not None:
            inner.wait()

    def log_tail(self) -> tuple[str, ...]:
        client = self._attempt or self._inner
        return client.log_tail() if client is not None else ()

    @property
    def running(self) -> bool:
        inner = self._inner
        if inner is None:
            return False
        if self._process is not None:
            return self._process.poll() is None and inner.running
        return inner.running

    # -- what the hub calls -----------------------------------------------------------

    @property
    def instructions(self) -> str:  # type: ignore[override]
        return self._inner.instructions if self._inner else ""

    @property
    def server_name(self) -> str:  # type: ignore[override]
        return self._inner.server_name if self._inner else self.name

    def list_tools(self, timeout: float) -> list[RemoteTool]:
        if self._inner is None:
            raise McpError(f"MCP server {self.name!r} is not running")
        return self._inner.list_tools(timeout)

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        timeout: float,
        cancel: CancelToken | None = None,
    ) -> CallOutcome:
        if self._inner is None:
            raise McpError(f"MCP server {self.name!r} is not running")
        return self._inner.call_tool(name, arguments, timeout, cancel)
