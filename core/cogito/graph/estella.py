"""Cogito Estella as a subprocess, spoken to over MCP's stdio framing.

Estella runs in its own interpreter: it carries torch and an encoder, it is pinned to the
CPU so it never competes with the model server for VRAM, and a crash in it must not crash
Cogito.

Extraction and retrieval cost zero LLM tokens, so ingesting every turn is free. The first
call loads a model, so nothing here may sit on a turn's critical path. Ingestion is queued
on one worker thread and the turn does not wait for it. Retrieval waits for a bounded time,
and a timeout returns no answer instead of raising: the caller has a ladder to fall back on.

The wire is JSON-RPC over the child's stdin/stdout, one message per line. It is spoken
directly, without an MCP client library: only three methods are used, and only one server
ever sits behind it.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Environment variables that cap the CPU threads of Estella's libraries (`_spawn_options`).
_THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "RAYON_NUM_THREADS",
)
#: How long one ingest of a background Estella may take. At its lowest priority beside a
#: model decoding on the CPU it can be several times slower than the 5.8 ms a character
#: measured in cogito-lab experiments/2026-09-30-doc-cache, and a part abandoned at the
#: call timeout is never recorded as in.
_INGEST_TIMEOUT = 600.0


#: The lowest CPU priority on POSIX: nice 19.
_LOWEST_NICE = 19


def _lowest_priority() -> None:
    """In the child, before it runs Estella: the lowest CPU priority there is. Set, not
    added: `os.nice` is relative, and a desktop may start the app at a negative nice."""
    if hasattr(os, "setpriority"):
        os.setpriority(os.PRIO_PROCESS, 0, _LOWEST_NICE)
    else:
        os.nice(_LOWEST_NICE)

#: The MCP protocol version sent in the handshake. An older server negotiates down.
PROTOCOL_VERSION = "2025-03-26"

#: Estella clamps the budget itself; repeated here so a caller sees the real range.
ASK_BUDGET_MIN, ASK_BUDGET_MAX = 100, 4000

#: `entities: a, b · p=0.42 · scorer=learned`. Only the learned scorer (the default)
#: writes `p=`. Without it a reply has no confidence, and the ladder treats it as
#: unmeasured, not as zero.
_HEADER = re.compile(
    r"^entities:\s*(?P<entities>.*?)"
    r"(?:\s+·\s+p=(?P<p>[0-9.]+))?"
    r"\s+·\s+scorer=(?P<scorer>.+)$"
)
_NO_MATERIAL = re.compile(r"^no material for ")


@dataclass(frozen=True, slots=True)
class AskReply:
    """One `ask` answer, with the header parsed off it.

    ``confidence`` is the learned scorer's probability for its best sentence, or None when
    the configured scorer does not report one. ``abstained`` means Estella said it has
    nothing. That differs from a low score and is not an error.
    """

    text: str
    entities: tuple[str, ...] = ()
    confidence: float | None = None
    scorer: str = ""
    abstained: bool = False

    @property
    def usable(self) -> bool:
        return not self.abstained and bool(self.body)

    @property
    def body(self) -> str:
        """The reply without its header line.

        A reply with sentences but no facts opens with a bare `--` divider. That is dropped
        too, since every line costs context.
        """
        lines = self.text.splitlines()
        if lines and _HEADER.match(lines[0]):
            lines = lines[1:]
        while lines and lines[0].strip() in ("", "--"):
            lines = lines[1:]
        return "\n".join(lines).strip()

    def confident(self, floor: float) -> bool:
        """True when this answer can be used without escalating.

        An unmeasured confidence counts as confident. Otherwise a scorer that reports no
        probabilities would escalate every question and spend the tokens the ladder exists
        to save.
        """
        return self.usable and (self.confidence is None or self.confidence >= floor)


def parse_ask(text: str) -> AskReply:
    """Turn Estella's reply into something a ladder can branch on."""
    stripped = text.strip()
    if _NO_MATERIAL.match(stripped):
        return AskReply(text=stripped, abstained=True)
    first = stripped.splitlines()[0] if stripped else ""
    match = _HEADER.match(first)
    if match is None:
        return AskReply(text=stripped)
    raw = match.group("entities").strip()
    entities = (
        ()
        if raw in ("", "(none)")
        else tuple(e.strip() for e in raw.split(",") if e.strip())
    )
    confidence = float(match.group("p")) if match.group("p") else None
    return AskReply(
        text=stripped,
        entities=entities,
        confidence=confidence,
        scorer=match.group("scorer").strip(),
    )


class EstellaClient:
    """One Estella process over one graph directory.

    Every public method is safe to call when the process is dead, missing or still warming
    up: retrieval returns None and ingestion returns False. Callers need a fallback, since
    that is the normal state for most of a session's first minute.
    """

    def __init__(
        self,
        command: tuple[str, ...],
        directory: Path,
        *,
        device: str = "cpu",
        start_timeout: float = 60.0,
        call_timeout: float = 90.0,
        threads: int | None = None,
        background: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command = tuple(command)
        #: Set on top of the parent's environment (the app's vector cache).
        self._env = dict(env or {})
        self._directory = directory
        self._device = device
        self._start_timeout = start_timeout
        self._call_timeout = call_timeout
        #: At most this many CPU threads for its math libraries; None leaves them their
        #: own default.
        self._threads = threads
        #: Run at the lowest CPU priority, and give one ingest `_INGEST_TIMEOUT` to finish.
        self._background = background

        self._process: subprocess.Popen[str] | None = None
        self._replies: dict[int, queue.Queue[Mapping[str, Any]]] = {}
        self._next_id = 0
        self._ready = threading.Event()
        self._failed = ""
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ingest: queue.Queue[tuple[str, str, Callable[[bool], None] | None] | None] = (
            queue.Queue()
        )
        self._workers: list[threading.Thread] = []
        self._closing = False

    # -- lifecycle -----------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def failure(self) -> str:
        """Why it is not running, for `doctor` and the setup screen. Empty when it runs."""
        return self._failed

    def start(self) -> bool:
        """Spawn the child and complete the MCP handshake. Idempotent."""
        with self._lock:
            if self._ready.is_set():
                return True
            if self._process is not None or self._failed:
                return False
            argv = [
                *self._command,
                "--dir",
                str(self._directory),
                "--device",
                self._device,
            ]
            self._directory.mkdir(parents=True, exist_ok=True)
            try:
                self._process = subprocess.Popen(  # noqa: S603 - argv is built here, not parsed
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    **self._spawn_options(),
                )
            except OSError as exc:
                self._failed = f"could not start {argv[0]}: {exc}"
                log.warning("estella: %s", self._failed)
                return False
            self._spawn(self._read_stdout, "cogito-estella-read")
            self._spawn(self._read_stderr, "cogito-estella-log")
            self._spawn(self._ingest_loop, "cogito-estella-ingest")

        if not self._handshake():
            return False
        self._ready.set()
        return True

    def _spawn_options(self) -> dict[str, Any]:
        """How the child is started: the parent's environment without VIRTUAL_ENV, limited
        to a few threads or run in the background when asked. PyTorch takes its CPU thread
        count from OMP_NUM_THREADS and MKL from MKL_NUM_THREADS ("CPU threading and
        TorchScript inference", PyTorch docs,
        read 2026-10-11); the tokenizers' pool reads RAYON_NUM_THREADS. A nice value is
        inherited by every thread the child starts afterwards."""
        # Without the parent's VIRTUAL_ENV: Cogito Estella 0.19 installs the spaCy model it
        # downloads with pip into the environment VIRTUAL_ENV names, which would be the core's
        # own when the core runs from a venv (seen 2026-10-15: it installed into another
        # project's venv). Its own interpreter is the right place, and it finds it unaided.
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        env.update(self._env)
        if self._threads is not None:
            count = str(max(1, self._threads))
            env.update({name: count for name in _THREAD_VARIABLES})
        options: dict[str, Any] = {"env": env}
        if self._background:
            if os.name == "nt":
                options["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
            elif hasattr(os, "nice"):
                options["preexec_fn"] = _lowest_priority
        return options

    def wait_ready(self, timeout: float) -> bool:
        """Block until the handshake is done, for at most ``timeout`` seconds. Returns False
        at once if the process has already failed. For a caller that would rather wait a few
        seconds than answer "not ready" to a question asked early in a session."""
        if self._failed:
            return False
        if not self._ready.is_set() and self._process is None:
            self.start_background()
        return self._ready.wait(timeout)

    def start_background(self) -> None:
        """Warm up without blocking a turn. The first `ask` may still find it not ready."""
        if self._ready.is_set() or self._failed:
            return
        self._spawn(self.start, "cogito-estella-start")

    def _spawn(self, target: Any, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._workers.append(thread)

    def _handshake(self) -> bool:
        reply = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cogito", "version": "0"},
            },
            timeout=self._start_timeout,
        )
        if reply is None:
            self._failed = self._failed or "no answer to initialize"
            return False
        self._notify("notifications/initialized", {})
        return True

    def close(self) -> None:
        self._closing = True
        self._ingest.put(None)
        process = self._process
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        finally:
            self._process = None
            self._ready.clear()

    # -- the wire ------------------------------------------------------------------

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # A server printing to its own wire is a bug, but not one to die of.
                # Anything unparseable is logged.
                log.debug("estella: non-JSON on stdout: %s", line[:200])
                continue
            identifier = message.get("id")
            if isinstance(identifier, int):
                waiting = self._replies.get(identifier)
                if waiting is not None:
                    waiting.put(message)
        if not self._closing:
            self._failed = self._failed or "the process exited"
            self._ready.clear()

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            if line.strip():
                log.info("estella: %s", line.rstrip())

    def _send(self, message: Mapping[str, Any]) -> bool:
        process = self._process
        if process is None or process.stdin is None:
            return False
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                process.stdin.write(payload)
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._failed = f"writing to the process failed: {exc}"
            self._ready.clear()
            return False
        return True

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(
        self, method: str, params: Mapping[str, Any], *, timeout: float
    ) -> Mapping[str, Any] | None:
        with self._lock:
            self._next_id += 1
            identifier = self._next_id
        inbox: queue.Queue[Mapping[str, Any]] = queue.Queue(maxsize=1)
        self._replies[identifier] = inbox
        try:
            if not self._send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": method,
                    "params": dict(params),
                }
            ):
                return None
            try:
                message = inbox.get(timeout=timeout)
            except queue.Empty:
                log.warning("estella: %s timed out after %.0fs", method, timeout)
                return None
        finally:
            self._replies.pop(identifier, None)
        if "error" in message:
            log.warning("estella: %s failed: %s", method, message["error"])
            return None
        result = message.get("result")
        return result if isinstance(result, Mapping) else None

    def _call_tool(
        self, name: str, arguments: Mapping[str, Any], timeout: float | None = None
    ) -> str | None:
        """One `tools/call`, flattened to the text the tool returned."""
        if not self._ready.is_set():
            return None
        result = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            timeout=self._call_timeout if timeout is None else timeout,
        )
        if result is None:
            return None
        if result.get("isError"):
            blocks = result.get("content") or []
            detail = blocks[0].get("text", "") if blocks else ""
            log.warning("estella: tool %s reported an error: %s", name, detail[:200])
            return None
        parts = [
            block.get("text", "")
            for block in result.get("content") or []
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)

    # -- the two things we use it for ----------------------------------------------

    def ask(self, question: str, budget: int) -> AskReply | None:
        """Facts and source sentences for a question, or None when Estella cannot answer.

        None means the subprocess produced no reply: it is not running, still loading its
        model, or slower than the call timeout. That is distinct from an `AskReply` that
        abstained, which means Estella looked and found nothing.
        """
        clamped = min(max(int(budget), ASK_BUDGET_MIN), ASK_BUDGET_MAX)
        text = self._call_tool("ask", {"question": question, "budget": clamped})
        return None if text is None else parse_ask(text)

    def provenance(self, edge_ids: list[str]) -> str | None:
        """Where each of these facts came from: the source and sentence of every edge, or
        None when Estella cannot answer."""
        if not edge_ids:
            return ""
        return self._call_tool("provenance", {"edge_ids": ",".join(edge_ids)})

    def ingest(self, text: str, source: str) -> bool:
        """Add one document to the graph, blocking. Costs no LLM tokens but loads a model on
        the first call, so the turn path uses `submit` instead."""
        if not text.strip():
            return False
        timeout = _INGEST_TIMEOUT if self._background else None
        reply = self._call_tool("ingest", {"path_or_text": text, "source": source}, timeout)
        return reply is not None

    def submit(
        self, text: str, source: str, done: Callable[[bool], None] | None = None
    ) -> None:
        """Queue a document for ingestion and return immediately.

        One worker drains the queue, so two turns never trigger two concurrent model loads,
        and a slow extraction delays only the next ingest, never the next turn. ``done``, if
        given, is called on that worker with whether Estella took the document.
        """
        if text.strip():
            self._ingest.put((text, source, done))

    def _ingest_loop(self) -> None:
        while True:
            item = self._ingest.get()
            if item is None:
                return
            text, source, done = item
            if not self._ready.wait(timeout=self._start_timeout):
                log.debug("estella: dropping ingest of %s, never became ready", source)
                ok = False
            else:
                ok = self.ingest(text, source)
            if done is not None:
                try:
                    done(ok)
                except Exception:  # a caller's bookkeeping must never stop the worker
                    log.exception("estella: an ingest callback for %s raised", source)

    def drain(self, timeout: float) -> bool:
        """Block until the ingest queue is empty. Used by compaction, which needs the turn
        it is about to fold away to be retrievable, and by tests."""
        deadline = threading.Event()
        threading.Timer(timeout, deadline.set).start()
        while not self._ingest.empty() and not deadline.is_set():
            deadline.wait(0.1)
        return self._ingest.empty()


class NullEstella:
    """Stands in for Estella when it is switched off.

    Every caller already handles "no answer", since a warming-up Estella returns the same,
    so disabling it needs no branches elsewhere.
    """

    ready = False
    failure = "disabled in settings"

    def start(self) -> bool:
        return False

    def start_background(self) -> None:
        return None

    def wait_ready(self, timeout: float) -> bool:
        return False

    def ask(self, question: str, budget: int) -> AskReply | None:
        return None

    def provenance(self, edge_ids: list[str]) -> str | None:
        return None

    def ingest(self, text: str, source: str) -> bool:
        return False

    def submit(
        self, text: str, source: str, done: Callable[[bool], None] | None = None
    ) -> None:
        return None

    def drain(self, timeout: float) -> bool:
        return True

    def close(self) -> None:
        return None


#: The two graphs Cogito keeps, and why they are separate.
#:
#: `ask` answers questions about the project's prose, `recall` about the conversation. One
#: store would save a process, but the conversation outgrows the documentation within an
#: hour of work, and Estella shortlists by IDF over the whole sentence universe. The docs
#: would be buried under the transcript just when a session gets long enough for either
#: tool to matter. Each store is spawned only when something asks it a question.
DOCS = "docs"
CONVERSATION = "conversation"
#: A third graph, for long documents a conversation attaches or reads (`graph.documents`).
#: Same reason: a 300-page PDF in `DOCS` would bury the project's own prose. One per
#: Estella version (`version_slug`).
DOCUMENTS = "documents"


def estella_version(command: tuple[str, ...]) -> str:
    """The version of the Cogito Estella that ``command`` runs, read without starting it.

    That is the version of its ``cogito_estella`` distribution, plus the checkout's commit
    for an editable install from git, whose version number does not move between commits.
    "unknown" when the distribution cannot be found. A graph built by one extractor is never
    served as another's (`graph.documents`), so this is part of a document index's key."""
    if not command:
        return "unknown"
    try:
        environment = Path(command[0]).resolve().parent.parent
        found = sorted(environment.glob("lib/python*/site-packages/cogito_estella-*.dist-info"))
        found += sorted(environment.glob("Lib/site-packages/cogito_estella-*.dist-info"))
    except OSError:
        return "unknown"
    if not found:
        return "unknown"
    dist = found[-1]
    version = dist.name.removeprefix("cogito_estella-").removesuffix(".dist-info")
    commit = _editable_commit(dist)
    return f"{version}+g{commit[:12]}" if commit else version


def has_graph_model(command: tuple[str, ...]) -> bool:
    """Whether that Estella can load its graph model: torch and transformers are installed
    beside it (Cogito Estella's ``[full]`` extra). The base install the app provisions has
    neither, and a full server started on it exits (2026-10-15), so a graph that needs facts
    is only asked for where they can be made. Read without starting it."""
    if not command:
        return False
    try:
        environment = Path(command[0]).resolve().parent.parent
        installed = [
            *environment.glob("lib/python*/site-packages/*.dist-info"),
            *environment.glob("Lib/site-packages/*.dist-info"),
        ]
    except OSError:
        return False
    names = {d.name.split("-", 1)[0].lower() for d in installed}
    return {"torch", "transformers"} <= names


def _editable_commit(dist: Path) -> str:
    """The commit an editable install's source checkout is at, read from its ``.git``."""
    try:
        direct = json.loads((dist / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    url = direct.get("url", "") if isinstance(direct, dict) else ""
    if not (isinstance(direct.get("dir_info"), dict) and direct["dir_info"].get("editable")):
        return ""
    if not url.startswith("file://"):
        return ""
    from urllib.parse import unquote, urlparse

    git = Path(unquote(urlparse(url).path)) / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head.removeprefix("ref: ").strip()
        loose = git / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip()
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except OSError:
        return ""
    return ""


def supports_lite(version: str) -> bool:
    """Whether that Estella has `--lite` (0.18.0 and later)."""
    found = re.match(r"(\d+)\.(\d+)", version)
    return bool(found) and (int(found.group(1)), int(found.group(2))) >= (0, 18)


def supports_vectors(version: str) -> bool:
    """Whether that Estella's lite index keeps a multilingual vector per sentence (0.20.0 and
    later), so a question finds its passage in another language."""
    found = re.match(r"(\d+)\.(\d+)", version)
    return bool(found) and (int(found.group(1)), int(found.group(2))) >= (0, 20)


def supports_code(version: str) -> bool:
    """Whether that Estella splits a code source by line (0.19.3 and later): an older one
    reads a whole source file as one sentence."""
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    return bool(found) and tuple(int(g) for g in found.groups()) >= (0, 19, 3)


def version_slug(version: str) -> str:
    """``version`` as one directory name."""
    return re.sub(r"[^A-Za-z0-9._+-]", "_", version) or "unknown"


def resolve_command(configured: str = "") -> tuple[str, ...]:
    """The argv prefix that starts Estella, or an empty tuple when it is not installed.

    A configured value always wins. Setup writes the interpreter it provisioned, and
    falling back to another one on PATH would serve answers from a graph built by a
    different build of the extractor.
    """
    if configured:
        return (configured,)
    found = shutil.which("cogito-mcp")
    return (found,) if found else ()


class EstellaPool:
    """Lazily-spawned Estella processes, one per graph, keyed by name."""

    def __init__(
        self,
        command: tuple[str, ...],
        root: Path,
        *,
        device: str = "cpu",
        enabled: bool = True,
        background_threads: int | None = None,
        vector_cache: Path | None = None,
    ) -> None:
        self._command = command
        #: The app's own install (`setup/estella.py`): its multilingual vector model was
        #: fetched at install into this directory, so a lite graph is started with
        #: ``--no-download`` and never waits on a download mid-turn. None for a person's own
        #: Estella, which keeps its own cache and behaviour.
        self._vector_cache = vector_cache
        self._root = root
        self._device = device
        self._enabled = enabled and bool(command)
        #: With a number, the documents graphs' Estella runs in the background: at the
        #: lowest priority, with that many threads (the app's, `cli._build_engine`). None
        #: starts it like every other.
        self._background_threads = background_threads
        self._clients: dict[str, EstellaClient | NullEstella] = {}
        self._lock = threading.Lock()

    def client(self, name: str, extra: tuple[str, ...] = ()) -> EstellaClient | NullEstella:
        """The Estella for graph ``name``, started with ``extra`` arguments the first time."""
        with self._lock:
            existing = self._clients.get(name)
            if existing is not None:
                return existing
            client: EstellaClient | NullEstella
            if not self._enabled:
                client = NullEstella()
            else:
                background = self._background_threads is not None and (
                    name == DOCUMENTS or name.startswith(DOCUMENTS + "/")
                )
                if self._vector_cache is not None and "--lite" in extra:
                    extra = (*extra, "--no-download")
                client = EstellaClient(
                    (*self._command, *extra),
                    self._root / name,
                    device=self._device,
                    threads=self._background_threads if background else None,
                    background=background,
                    env=(
                        {"COGITO_CACHE": str(self._vector_cache)}
                        if self._vector_cache is not None
                        else None
                    ),
                )
                client.start_background()
            self._clients[name] = client
            return client

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()
