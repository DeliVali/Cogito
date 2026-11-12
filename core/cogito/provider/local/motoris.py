"""Starting Cogito Motoris for an entry with `serve`.

Cogito then reaches it over `base_url` like any host. A GPU process that outlives its
parent holds VRAM, so the child gets its own session, the kernel is asked to kill it with
the parent, and it is reaped on exit.

In the app, a Motoris that died under a request is started again and the request asked
again (`Revival`, `RevivingProvider`).
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from cogito import procs
from cogito.cancel import CancelToken
from cogito.config.models import ModelEntry
from cogito.errors import ProviderError, ServiceError
from cogito.provider.host import endpoint_of, is_up, served_models, serves_this_entry

log = logging.getLogger(__name__)

_PR_SET_PDEATHSIG = 1

_HEALTH_TIMEOUT = 300.0
_HEALTH_INTERVAL = 1.0
#: A restart's load that has not answered this long after it started is ended, so the next
#: failure starts it again within `Revival`'s bound. A choice: twice a first start's wait.
_LOAD_LIMIT = 2 * _HEALTH_TIMEOUT
#: Cogito Motoris ends its engine (its own process group) in up to 45 s: QUIT 20, terminate
#: 5, kill 20 (cogito-motoris serve/server.py `ENGINE_QUIT_WAIT_S, ENGINE_TERM_WAIT_S,
#: ENGINE_KILL_WAIT_S`, 8019b78a). Its GPU memory is free only once the engine has gone.
_STOP_GRACE = 50.0
#: After SIGKILL, how long a survivor is waited for before `stop` gives up on it.
_KILL_WAIT = 10.0

#: Engine settings Cogito's requests need, unless the person's environment sets them.
#: Strata's hybrid model resumes a prompt only at a saved checkpoint, taken at its last
#: turn, and Cogito ends every request with a short status turn the next request does not
#: have, so no turn checkpoint was ever reused: each step read 4-18K tokens again, 6-11 s at
#: 120K (its log, 2026-11-01). This one also saves a checkpoint before that last short turn
#: (cogito-motoris docs/MESSAGE_BOUNDARY_CACHE.md, read 2026-11-01). The prompt is the same.
_ENGINE_ENV = {"STRATA_CACHE_MESSAGE_BOUNDARY": "1"}


def _child_env() -> dict[str, str]:
    return {**_ENGINE_ENV, **os.environ}


def _posix_tree(root: int) -> list[tuple[int, str]]:
    """`root` and every process below it, each with its start time (so a reused PID is
    never taken for it), read from /proc."""
    parents: dict[int, list[tuple[int, str]]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat.rpartition(")")[2].split()
        parents.setdefault(int(fields[1]), []).append((int(entry.name), fields[19]))
    found: list[tuple[int, str]] = []
    queue = [root]
    while queue:
        for child in parents.get(queue.pop(), []):
            found.append(child)
            queue.append(child[0])
    return found


def _posix_alive(pid: int, started: str) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except OSError:
        return False
    return fields[19] == started and fields[0] != "Z"


def _die_with_parent() -> None:
    """Ask the kernel to SIGKILL this process if its parent goes away.

    atexit is not enough: a crash or a SIGKILL of the agent would leave the model server
    holding the whole GPU.
    """
    with contextlib.suppress(OSError):  # not Linux; atexit still applies
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


def _checkout(entry: ModelEntry) -> Path:
    return Path(str(entry.args.get("motoris", ""))).expanduser()


def python_for(entry: ModelEntry) -> str:
    """The checkout's own Python, which its setup made."""
    if procs.IS_WINDOWS:
        # Unmeasured: Cogito Motoris has not been set up on Windows yet.
        python = _checkout(entry) / ".venv" / "Scripts" / "python.exe"
    else:
        python = _checkout(entry) / ".venv" / "bin" / "python"
    if not python.is_file():
        raise ProviderError(
            f"{entry.id}: no Cogito Motoris checkout at args.motoris = "
            f"{str(entry.args.get('motoris'))!r} (expected {python}). Run Cogito Motoris's "
            "setup there, or fix the path."
        )
    return str(python)


def build_command(entry: ModelEntry) -> list[str]:
    """Cogito Motoris's own server (`serve/server.py`), with the engine config its setup
    wrote for this model, on the entry's host and port. Its API is OpenAI-compatible."""
    python = python_for(entry)
    install = _checkout(entry)
    config = Path(str(entry.args.get("config", ""))).expanduser()
    if not config.is_absolute():
        config = install / config
    if not config.is_file():
        raise ProviderError(
            f"{entry.id}: args.config = {str(config)!r} does not exist. Run Cogito Motoris's "
            "setup there for this model, or point args.config at the strata-*.json it wrote."
        )
    where = endpoint_of(entry)
    return [
        python, str(install / "serve" / "server.py"),
        "--engine", "strata", "--config", str(config),
        "--host", where.host, "--port", str(where.port),
    ]


class MotorisServer:
    """Supervises one Cogito Motoris process."""

    def __init__(self, entry: ModelEntry, *, log_file: Path | None = None):
        self._entry = entry
        self._log_file = log_file
        self._process: subprocess.Popen[bytes] | None = None
        self._started_at = 0.0
        self._adopted = False
        self._lock = threading.Lock()
        self._stop_registered = False

    @property
    def adopted(self) -> bool:
        """True when we attached to a server someone else started, and must not kill it."""
        return self._adopted

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def start(self, *, wait: bool = True) -> None:
        with self._lock:
            if is_up(self._entry):
                # Whatever answers may be another model: where two models take turns on
                # 8080, a run would attach to the other one and report numbers for a model
                # it never asked for. Ask which model it serves, and refuse on a mismatch.
                match = serves_this_entry(self._entry)
                if match is False:
                    others = ", ".join(served_models(self._entry)) or "something else"
                    endpoint = endpoint_of(self._entry)
                    raise ProviderError(
                        f"{self._entry.id}: {endpoint.host}:{endpoint.port} is already "
                        f"serving {others}, not {self._entry.model!r}.\n"
                        f"Cogito will not attach to it and will not stop it — another "
                        f"program may be using it.\n"
                        f"Either stop that server yourself, or give this entry a port of "
                        f"its own by editing base_url in models.toml."
                    )
                self._adopted = True
                log.info(
                    "%s: a server is already answering on this port%s; using it",
                    self._entry.id,
                    "" if match else " (it does not say which model)",
                )
                return
            command = build_command(self._entry)
            log.info("starting %s", " ".join(command))
            self._process = self._spawn(command)
            self._started_at = time.monotonic()
            if not self._stop_registered:
                # Once: a restart reuses this object, and `stop` reads the live process.
                atexit.register(self.stop)
                self._stop_registered = True

        if wait:
            self.wait_until_healthy()

    def _spawn(self, command: list[str]) -> subprocess.Popen[bytes]:
        """Start ``command`` on a thread that lives as long as the process does.

        On Linux `PR_SET_PDEATHSIG` fires when the thread that forked the child ends, not
        the process. The app runs each turn, and each model switch, on a thread that ends
        with it, so a server started there would be killed with it. `tools.mcp.hub` keeps
        its stdio servers the same way. The caller waits until the process exists."""
        started = threading.Event()
        box: dict[str, Any] = {}

        def keep() -> None:
            try:
                box["process"] = process = self._popen(command)
            except BaseException as exc:  # noqa: BLE001 - raised again on the caller's thread
                box["error"] = exc
                return
            finally:
                started.set()
            # Reaps the process as soon as it ends; `poll` and `wait` elsewhere still work.
            with contextlib.suppress(Exception):
                process.wait()

        threading.Thread(target=keep, name=f"motoris-{self._entry.id}", daemon=True).start()
        started.wait()
        if "error" in box:
            raise box["error"]
        return box["process"]

    def _popen(self, command: list[str]) -> subprocess.Popen[bytes]:
        sink = (
            self._log_file.open("ab")
            if self._log_file
            else subprocess.DEVNULL  # type: ignore[assignment]
        )
        try:
            if procs.IS_WINDOWS:
                # No process groups or PR_SET_PDEATHSIG there: a kill-on-close job held by
                # this process ends the server (and the engine under it) when this process
                # ends, crash included (`cogito.procs`).
                process = subprocess.Popen(
                    command,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    env=_child_env(),
                    creationflags=procs.NEW_GROUP,
                )
                procs.bind_to_this_process(process)
                return process
            return subprocess.Popen(
                command,
                stdout=sink,
                stderr=subprocess.STDOUT,
                env=_child_env(),
                start_new_session=True,
                preexec_fn=_die_with_parent,
            )
        finally:
            if sink is not subprocess.DEVNULL:
                # The child has its own copy; a restart opens the log again.
                sink.close()

    def wait_until_healthy(
        self,
        timeout: float = _HEALTH_TIMEOUT,
        *,
        cancel: CancelToken | None = None,
        keep_loading: bool = False,
    ) -> None:
        """Block until the server answers, or explain why it never will.

        Loading a 16 GB model with expert offload takes tens of seconds, so a slow start is
        normal and a dead process is not; the two are told apart instead of both timing
        out. A third case: several `cogito chat` processes can start in the same instant
        (a benchmark's ``--workers`` does this). `is_up()` is false for all of them before
        any binds the port, so each spawns its own server. One wins the bind and the
        rest exit at once with "Address already in use". That is not a crash, since a
        sibling is about to become healthy, so the loser waits and adopts it.

        With ``cancel``, «Detener» ends the wait with a `ProviderError` and leaves the
        server loading: the next request finds it up, or waits for it (`Revival`). A
        timeout stops the server, unless ``keep_loading``: a restart's slow load goes on.
        """
        deadline = time.monotonic() + timeout
        lost_the_race = False
        while time.monotonic() < deadline:
            if is_up(self._entry):
                if not lost_the_race:
                    return
                # What answers now is another process's instance of this entry, not ours.
                # serves_this_entry, not is_up alone, tells that apart from a different
                # model taking the port, which stays an error.
                if serves_this_entry(self._entry) is False:
                    break
                self._adopted = True
                log.info(
                    "%s: lost the startup race for this port to another process; adopting "
                    "the instance that won it",
                    self._entry.id,
                )
                return
            if (
                not lost_the_race
                and self._process is not None
                and self._process.poll() is not None
            ):
                if not self._lost_a_bind_race():
                    raise ProviderError(
                        f"{self._entry.id}: Cogito Motoris exited with code "
                        f"{self._process.returncode} before becoming healthy"
                        + (f"; see {self._log_file}" if self._log_file else "")
                    )
                lost_the_race = True
                log.info(
                    "%s: lost a startup race for the port (another process is loading the "
                    "model); waiting for it to finish rather than failing this turn",
                    self._entry.id,
                )
            if cancel is None:
                time.sleep(_HEALTH_INTERVAL)
            elif cancel.wait(_HEALTH_INTERVAL):
                raise ProviderError(
                    f"{self._entry.id}: stopped while Cogito Motoris was loading"
                )
        if not keep_loading:
            self.stop()
        raise ProviderError(
            f"{self._entry.id}: Cogito Motoris did not become healthy within {timeout:.0f}s"
        )

    def exited(self, grace: float = 0.0) -> int | None:
        """The exit code of the process this object started, once it has ended on its
        own or `await_load` ended a load that never answered; None while it runs, after
        `stop`, and for a server it only adopted. ``grace`` waits that long for a process
        that is going down: a request can see its connection reset a moment before the
        process is gone."""
        with self._lock:
            process = None if self._adopted else self._process
        if process is None:
            return None
        if grace > 0:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=grace)
        return process.poll()

    def loading(self) -> bool:
        """True while the process this object started runs but does not answer yet: a
        restart «Detener» left loading, or one another request is waiting for."""
        with self._lock:
            process = None if self._adopted else self._process
        return process is not None and process.poll() is None and not is_up(self._entry)

    def await_load(self, *, cancel: CancelToken | None = None) -> None:
        """Wait for a restart's load that is still going, up to `_HEALTH_TIMEOUT`. A load
        that has run `_LOAD_LIMIT` without answering is ended here and kept as ended, so
        `exited` reports it and the next failure starts it again."""
        age = time.monotonic() - self._started_at
        try:
            self.wait_until_healthy(
                max(0.0, min(_HEALTH_TIMEOUT, _LOAD_LIMIT - age)),
                cancel=cancel,
                keep_loading=True,
            )
        except ProviderError:
            if (cancel is None or not cancel.cancelled) and (
                time.monotonic() - self._started_at >= _LOAD_LIMIT
            ):
                with self._lock:
                    process = None if self._adopted else self._process
                if process is not None:
                    log.warning(
                        "%s: Cogito Motoris did not answer within %.0f s of starting; ending it",
                        self._entry.id,
                        _LOAD_LIMIT,
                    )
                    self._end(process)
            raise

    def restart(
        self, *, cancel: CancelToken | None = None, timeout: float | None = None
    ) -> None:
        """Start again, with the same command, the process this object started and that
        has ended on its own, and wait until it answers. Never a server it only adopted,
        and never one still running: raises `ProviderError` for both. A load slower than
        ``timeout`` raises but goes on, so a later request can still use it, or wait for it
        (`await_load`)."""
        with self._lock:
            process = self._process
            if process is None or self._adopted or process.poll() is None:
                raise ProviderError(
                    f"{self._entry.id}: Cogito Motoris is not a server Cogito started and saw end"
                )
            self._process = None
        log.warning(
            "%s: Cogito Motoris exited with code %s; starting it again%s",
            self._entry.id,
            process.returncode,
            f" (log: {self._log_file})" if self._log_file else "",
        )
        self.start(wait=False)
        self.wait_until_healthy(
            _HEALTH_TIMEOUT if timeout is None else timeout, cancel=cancel, keep_loading=True
        )

    def _lost_a_bind_race(self) -> bool:
        """True when the tail of this entry's log shows a lost bind race.

        The log file is shared: every process starting this entry appends to the same
        path, ours included. So this can say yes when a sibling lost the race and we
        crashed for another reason a moment later. That costs a wait up to the timeout,
        never a false success. It cannot say yes for a crash with no sibling, since nothing
        else writes a bind error into the log. The exit code does not tell the two
        apart; the message is the only signal: Cogito Motoris's own "port N is already in
        use" (serve/server.py `listen_problem`).
        """
        if self._log_file is None or not self._log_file.exists():
            return False
        try:
            tail = self._log_file.read_text(errors="replace")[-4000:]
        except OSError:
            return False
        return f"port {endpoint_of(self._entry).port} is already in use" in tail

    def stop(self) -> None:
        """End the server and wait until it and everything it started are gone, the engine
        included, so the GPU memory is free when this returns. The engine runs in its own
        process group, so it is found by walking the tree before anything is signalled."""
        with self._lock:
            process = self._process
            if process is None or self._adopted:
                return
            self._process = None
        self._end(process)

    def _end(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if procs.IS_WINDOWS:
            below = procs.tree(process.pid)
            procs.terminate_trees([process.pid], _STOP_GRACE)
            for pid in below:
                procs.wait_gone(pid, _KILL_WAIT)
            return
        below = _posix_tree(process.pid)
        deadline = time.monotonic() + _STOP_GRACE
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_STOP_GRACE)
        while any(_posix_alive(*one) for one in below) and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        survivors = [one for one in below if _posix_alive(*one)]
        for pid, _ in survivors:
            log.warning("%s: pid %d outlived the server's stop; killing it", self._entry.id, pid)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_KILL_WAIT)
        deadline = time.monotonic() + _KILL_WAIT
        while any(_posix_alive(*one) for one in survivors) and time.monotonic() < deadline:
            time.sleep(0.05)

    def __enter__(self) -> MotorisServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


# -- bringing a crashed Motoris back (app only) ----------------------------------------------

#: At most this many restarts in `RESTART_WINDOW` seconds; after that the failure is shown
#: as before. A choice, not a measurement: an engine that keeps dying (a bad config, failing
#: memory) must stop being restarted and say so, not load a model in a loop.
MAX_RESTARTS = 3
RESTART_WINDOW = 600.0
#: The wait before each restart within the window, by how many came before it. A choice:
#: the first is at once, since a crash is usually a one-off.
RESTART_BACKOFF = (0.0, 5.0, 15.0)
#: How long a request whose connection failed waits for the process to finish going down
#: before deciding it is still alive.
_EXIT_GRACE = 2.0
#: The transport failures a dead server leaves (`ServiceError.code` is the exception's class
#: name, httpx's or the Anthropic SDK's). Anything else came from a live server, or is a
#: timeout, which a dead peer does not cause: it is never a reason to look for a restart.
_DEAD_PEER = frozenset(
    {"ConnectError", "RemoteProtocolError", "ReadError", "WriteError", "APIConnectionError"}
)


def _peer_gone(error: ProviderError) -> bool:
    """True for a failure with no answer at all that a server going away causes."""
    return getattr(error, "status", None) is None and getattr(error, "code", "") in _DEAD_PEER


class Revival:
    """Restarts the Cogito Motoris this core started when it has died, within
    `MAX_RESTARTS` per `RESTART_WINDOW`. One per served model, shared by every session on
    it: the lock makes a second request that failed with the first wait for the same
    restart instead of starting another.

    ``server_of`` answers the `MotorisServer` serving now, or None (a model this core does
    not start). ``notify`` gets ``{"type": "model_restarting", ...}`` before a restart.

    Ollama notices a dead runner only when the next request comes: `needsReload` pings it,
    and a failed ping expires the runner and loads a new one (server/sched.go). llama-swap
    sets an upstream that exited on its own to stopped, fails the requests waiting on it,
    and the next request starts it again (internal/process/process_command.go, last changed
    2026-11-01). vLLM's API server exits when its engine dies, unless
    `VLLM_KEEP_ALIVE_ON_ENGINE_DEATH`, and leaves the restart to whatever supervises it
    (vllm/entrypoints/launchers/launcher.py `terminate_if_errored`). All read 2026-11-03.
    The bound and asking the failed request again are this module's choices, not theirs:
    Ollama and llama-swap fail the request the death cut and start again on the next one,
    each time. A leftover engine is not this code's concern: Motoris ends one an
    earlier run lost before it starts its own (serve/server.py `end_leftover_engines`).
    """

    def __init__(
        self,
        server_of: Callable[[], Any],
        *,
        notify: Callable[[dict[str, Any]], None] | None = None,
        max_restarts: int = MAX_RESTARTS,
        window: float = RESTART_WINDOW,
        backoff: Sequence[float] = RESTART_BACKOFF,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._server_of = server_of
        self._notify = notify
        self._max = max_restarts
        self._window = window
        self._backoff = tuple(backoff)
        self._clock = clock
        self._lock = threading.Lock()
        self._restarts: list[float] = []
        #: Counts restarts that came back, so a request that waited for another's sees it.
        self._revived = 0

    def revive(self, error: ProviderError, cancel: CancelToken) -> bool:
        """After ``error``, True when the server this core started is answering again,
        because it was restarted here or by another request. False when it is not ours,
        still running, over the bound, stopped by «Detener», or failed to come back."""
        # Only a refused or cut connection can be a dead server. An HTTP error came from a
        # live one (Motoris answers 503 and restarts its own engine when only the engine
        # died), and no other failure waits here for the process.
        if not _peer_gone(error):
            return False
        server = self._server_of()
        if server is None:
            return False
        seen = self._revived
        with self._lock:
            # A load still going when the request failed can finish during the grace wait;
            # the model then answers, and the request is asked again.
            was_loading = server.loading()
            code = server.exited(_EXIT_GRACE)
            if code is None:
                if self._revived != seen:
                    return True  # another request's restart, finished while this one waited
                if not server.loading():
                    return was_loading
                return self._wait(server, cancel)
            now = self._clock()
            self._restarts = [t for t in self._restarts if now - t < self._window]
            if len(self._restarts) >= self._max:
                log.warning(
                    "Cogito Motoris exited with code %s; restarted %d times in %.0f s, not again",
                    code, len(self._restarts), self._window,
                )
                return False
            attempt = len(self._restarts) + 1
            self._say(
                {
                    "type": "model_restarting",
                    "attempt": attempt,
                    "max": self._max,
                    "exit_code": code,
                }
            )
            waits = self._backoff
            delay = waits[min(attempt - 1, len(waits) - 1)] if waits else 0.0
            if delay > 0 and cancel.wait(delay):
                return False
            self._restarts.append(self._clock())
            try:
                server.restart(cancel=cancel)
            except ProviderError as exc:
                log.warning("Cogito Motoris did not come back: %s", exc)
                return False
            self._revived += 1
            return True

    def _wait(self, server: Any, cancel: CancelToken) -> bool:
        self._say(
            {"type": "model_restarting", "attempt": None, "max": self._max, "exit_code": None}
        )
        try:
            server.await_load(cancel=cancel)
        except ProviderError as exc:
            log.warning("Cogito Motoris did not finish loading: %s", exc)
            return False
        return True

    def _say(self, event: dict[str, Any]) -> None:
        if self._notify is None:
            return
        try:
            self._notify(event)
        except Exception:  # noqa: BLE001 - a display must never cost the restart
            log.exception("telling the window about a restart failed")


def _ask_again_now(error: ProviderError) -> ServiceError:
    """``error`` with ``retry_after`` 0: the server answers again, so a turn that stops on
    it is resumed at once by the server's own resume (`server.app._plan_resume`)."""
    if isinstance(error, ServiceError):
        again = ServiceError(
            str(error),
            status=error.status,
            code=error.code,
            endpoint=error.endpoint,
            provider_message=error.provider_message,
            retry_after=0.0,
        )
    else:
        again = ServiceError(str(error), provider_message=str(error), retry_after=0.0)
    again.__cause__ = error
    return again


class RevivingProvider:
    """APP ONLY. A provider that, when the Cogito Motoris this core started dies under a
    request, has `Revival` start it again and asks again.

    A request that failed before any chunk came is sent again here, once. One cut after
    chunks were shown is not: it raises (with ``retry_after`` 0), and the loop asks the
    whole request again as its next attempt, under a new sample number, so the window drops
    what the cut reply had streamed (`loop.engine.StreamDelta`). On success the request and
    its bytes are those of the inner provider, untouched.
    """

    def __init__(self, inner: Any, revival: Revival) -> None:
        self._inner = inner
        self._revival = revival
        self.name = inner.name

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this class does not define.
        return getattr(self._inner, name)

    def capabilities(self):
        return self._inner.capabilities()

    def stream(self, messages, tools, *, params, cancel: CancelToken) -> Iterator[Any]:
        shown = False
        try:
            for chunk in self._inner.stream(messages, tools, params=params, cancel=cancel):
                shown = True
                yield chunk
            return
        except ProviderError as error:
            if cancel.cancelled or not self._revival.revive(error, cancel):
                raise
            if shown:
                raise _ask_again_now(error) from error
        yield from self._inner.stream(messages, tools, params=params, cancel=cancel)

    def configured(self, options: Any) -> Any:
        configure = getattr(self._inner, "configured", None)
        if configure is None:
            return self
        copy = configure(options)
        return self if copy is self._inner else RevivingProvider(copy, self._revival)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()
