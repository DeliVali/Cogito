"""MCP servers' tools, offered a few at a time.

A server can carry hundreds of tools (one measured here, 2026-10-24: 207 tools whose input
schemas add up to 3.4 MB). Every definition shown sits in the context window on every
step, so showing them all is not possible, and section 1b found that two extra definitions
already cost 11-16% more tokens. The harness picks which to show, and the model is not
asked to know in advance:

- each message from the person ranks every tool by BM25 over its name, title and
  description, and the best few are shown, within a token budget for their definitions;
- tools the model called, or found with `mcp_find`, stay shown while they are recent;
- `mcp_find` is the fallback when the ranking misses (a question in Spanish against
  English descriptions, say): it searches the same index and shows what it finds.

A local model's list is fixed for the conversation instead (`McpHub(stable=True)`): the
tools ranked best when the person first writes stay shown until a fold, and what
`mcp_find` finds comes back with its arguments, to be called by name without being shown.
A llama.cpp or Motoris chat template renders the tool list before everything else, so a
list that changed re-read the whole conversation: 33% of Motoris's prompt reading in the
256K era went to requests that reused nothing (cogito-lab
research/2026-11-10-single-model-review). A call to a tool not listed still runs
(`McpTool.mcp`): the server leaves it in the text and `provider.repair.xml_tool_call` reads
it. Claude keeps the moving selection, since it calls only what it was shown.

This is RAG-MCP's design (Gan and Sun, arXiv 2505.03275, May 2025, abstract read
2026-10-24: retrieving tool descriptions per query before the model sees them took
tool-selection accuracy from 13.62% to 43.13% on their stress test and cut prompt tokens by
over 50%; their numbers), with the
search tool Anthropic's and Copilot CLI's tool search offer as the way out. Loading tools
on the model's request alone was measured here and lost for the core tools (30/33 to
27/33); for a server's hundreds of tools there is no "all of them" to fall back to.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cogito.config.settings import McpServerSettings, McpSettings
from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters, result_image
from cogito.tools.mcp.catalog import Listed, ToolCatalog
from cogito.tools.mcp.client import McpClient, McpError, RemoteTool, Session
from cogito.tools.mcp.docker import ContainerClient, is_container
from cogito.tools.mcp.remote import HttpMcpClient, NeedsLogin, expand
from cogito.tools.ranking import Bm25, terms

log = logging.getLogger(__name__)

#: A bare MCP tool name as a result writes it (`open_binary`), matched as a whole word.
_IDENTIFIER = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")

#: Schema keywords dropped from what the model is shown. None changes what a valid call
#: is for the model; `pattern` and `format` break llama.cpp's JSON-Schema-to-grammar
#: conversion (`tools.base.parameters`), and examples and `$schema` are only size.
_DROPPED = frozenset({"$schema", "$id", "$comment", "examples", "pattern", "format"})
_NAME = re.compile(r"[^A-Za-z0-9_-]")

#: APP ONLY. Opens the harness note that tells the model which servers are not ready
#: (`McpHub.note`), so a reopened thread does not draw it as the person's message
#: (`loop.readback`).
NOTICE_PREFIX = "[harness: MCP servers; not a message from the person."


def tool_name(server: str, tool: str) -> str:
    """`server__tool`, as Claude Code names them, within the 64 characters providers allow."""
    return _NAME.sub("_", f"{server}__{tool}")[:64]


def clean_schema(schema: Any, defs: Mapping[str, Any] | None = None, depth: int = 0) -> Any:
    """The input schema the model is shown: local `$ref`s inlined, size-only keywords out.

    `pattern` and `format` break llama.cpp's JSON-Schema-to-grammar conversion
    (`tools.base.parameters`), so they are removed from the structure -- but their
    information is folded into the field's `description` first, so the model still learns a
    value's shape ("must match ^ev_[a-f0-9]{64}$"). Dropping the constraint silently once
    made a model invent a value the server then refused (REA's `evidence_id`, 2026-10-25).

    Keys inside `properties` are parameter names, never keywords, so they are kept as they
    are (a parameter called `format` stays)."""
    if depth > 12:
        return {}
    if isinstance(schema, list):
        return [clean_schema(item, defs, depth + 1) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    if defs is None:
        defs = {**(schema.get("definitions") or {}), **(schema.get("$defs") or {})}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target = ref.rsplit("/", 1)[-1]
        if target in defs:
            return clean_schema(defs[target], defs, depth + 1)
    hints = _constraint_hints(schema)
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _DROPPED or key in ("$defs", "definitions", "$ref"):
            continue
        if key == "properties" and isinstance(value, Mapping):
            cleaned[key] = {
                name: clean_schema(item, defs, depth + 1) for name, item in value.items()
            }
        else:
            cleaned[key] = clean_schema(value, defs, depth + 1)
    if hints:
        existing = str(cleaned.get("description") or "").rstrip()
        joined = "; ".join(hints)
        cleaned["description"] = f"{existing} ({joined})" if existing else f"({joined})"
    if cleaned.get("type") == "object" and "properties" not in cleaned:
        cleaned["properties"] = {}
    return cleaned


def _constraint_hints(schema: Mapping[str, Any]) -> list[str]:
    """Short words for the constraints `clean_schema` removes, to keep in the description so
    the model still knows a value's shape."""
    hints = []
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        hints.append(f"must match {pattern}")
    fmt = schema.get("format")
    if isinstance(fmt, str):
        hints.append(f"format: {fmt}")
    return hints


@dataclass(frozen=True, slots=True)
class Offered:
    """One remote tool as this session can show it."""

    name: str
    server: str
    remote: RemoteTool
    spec: ToolSpec
    #: What its definition costs on every step it is shown, at four characters a token.
    tokens: int


def _summary(description: str) -> str:
    first = description.strip().split("\n", 1)[0]
    sentence = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
    return sentence[:160]


def _arguments(schema: Mapping[str, Any]) -> str:
    """A tool's arguments in one line, from its cleaned schema: name, type, whether it is
    required, and the first sentence of what it is for. What a call by name needs when the
    definition is not in the tool list."""
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping) or not properties:
        return "no arguments"
    required = set(schema.get("required") or ())
    parts = []
    for name, prop in properties.items():
        prop = prop if isinstance(prop, Mapping) else {}
        kind = prop.get("type")
        if isinstance(kind, list):
            kind = "|".join(str(k) for k in kind)
        if not kind and isinstance(prop.get("enum"), list):
            kind = "one of " + ", ".join(json.dumps(v) for v in prop["enum"][:6])
        part = f"{name} ({kind or 'any'}{', required' if name in required else ''})"
        if about := _summary(str(prop.get("description") or ""))[:80]:
            part += f" {about}"
        parts.append(part)
    return "; ".join(parts)


@dataclass
class ServerState:
    """One configured server as this process has it: running with its tools, or the
    error it gave when started."""

    name: str
    key: str
    client: Session | None = None
    tools: tuple[RemoteTool, ...] = ()
    error: str = ""
    #: The server wants a sign-in, which the person starts from the Conexiones tab.
    needs_login: bool = False
    #: What its 401 said, for the sign-in to start from.
    challenge: Any = None
    #: Set once the start has ended, either way; until then the server is starting.
    ready: threading.Event = field(default_factory=threading.Event)
    #: The client while it starts, so a drop (or the process ending) can stop it then too.
    pending: Any = None
    #: APP ONLY: what the server answered at its last start (`catalog.ToolCatalog`).
    cached: Listed | None = None
    #: When this start began, for the Conexiones tab.
    since: float = field(default_factory=time.time)
    #: What a start that failed printed, kept once its client is closed.
    last_log: tuple[str, ...] = ()

    @property
    def starting(self) -> bool:
        return not self.ready.is_set()

    @property
    def log(self) -> tuple[str, ...]:
        """The last lines the server wrote while starting, or since it started."""
        client = self.pending or self.client
        return client.log_tail() if client is not None else self.last_log


def _key(server: McpServerSettings) -> str:
    """What starts or reaches the server; `enabled` changes nothing it runs, and neither
    does the port a container was found to serve on (`http_port`, written while it runs)."""
    return repr((
        server.command, server.args, sorted(server.env.items()), server.cwd,
        server.type, server.url, sorted(server.headers.items()), server.client_id,
    ))


class McpPool:
    """The servers this process runs, shared by its sessions and by the Conexiones tab: a
    disassembler keeps what it opened across conversations, and the tab shows the state
    the sessions see. A server is started on first use and kept, failed or not, until it
    is dropped (a retry, a change to its command, a removal) or the process ends."""

    def __init__(
        self,
        remember: Callable[[str, dict[str, object]], None] | None = None,
        catalog: ToolCatalog | None = None,
    ) -> None:
        self._states: dict[str, ServerState] = {}
        self._lock = threading.Lock()
        #: Where what a container start found is kept (the server's table in config.toml).
        self._remember = remember
        #: APP ONLY: each server's tool list from its last start, on disk.
        self._catalog = catalog

    def get(
        self,
        name: str,
        server: McpServerSettings,
        timeout: float,
        wait: float | None = None,
    ) -> ServerState:
        """The server's state, started in the background when it is not running. Waits up
        to `wait` seconds (by default the whole `timeout`) for a start to end; a server
        still starting then is returned as it is (`starting`), and its start goes on.
        Each server starts on its own thread, so one that never answers holds no other."""
        with self._lock:
            state = self._states.get(name)
            reusable = state is not None and state.key == _key(server) and (
                state.starting or state.error or (state.client and state.client.running)
            )
            if not reusable:
                if state is not None and state.client is not None:
                    state.client.close()
                state = ServerState(name=name, key=_key(server))
                if self._catalog is not None:
                    state.cached = self._catalog.load(state.key)
                self._states[name] = state
                threading.Thread(
                    target=self._start, args=(state, server, timeout),
                    name=f"mcp-start-{name}", daemon=True,
                ).start()
        state.ready.wait(timeout if wait is None else wait)
        return state

    def _start(self, state: ServerState, server: McpServerSettings, timeout: float) -> None:
        remember = self._remember
        client = _client(
            state.name, server,
            (lambda fields: remember(state.name, fields)) if remember else None,
        )
        state.pending = client
        try:
            client.start(timeout)
            state.tools = tuple(client.list_tools(timeout))
            state.client = client
            if self._catalog is not None:
                self._catalog.save(state.key, state.tools, client.instructions)
        except McpError as exc:
            state.last_log = client.log_tail()
            client.close()
            state.error = str(exc)
            state.needs_login = isinstance(exc, NeedsLogin)
            state.challenge = getattr(exc, "challenge", None)
            log.warning("MCP server %s left out: %s", state.name, exc)
        state.pending = None
        with self._lock:
            # Dropped (a retry, a removal) while it started: nobody holds it any more.
            orphaned = self._states.get(state.name) is not state
        if orphaned and state.client is not None:
            state.client.close()
            state.client = None
        state.ready.set()
        # On Linux the server dies with the thread that started it (`PR_SET_PDEATHSIG` is
        # per thread), so this thread lives as long as the server does.
        if state.client is not None:
            state.client.wait()

    def peek(self, name: str) -> ServerState | None:
        with self._lock:
            return self._states.get(name)

    def drop(self, name: str) -> None:
        with self._lock:
            state = self._states.pop(name, None)
        if state is None:
            return
        if state.client is not None:
            state.client.close()
        elif state.pending is not None:
            state.pending.close()

    def close(self) -> None:
        for name in list(self._states):
            self.drop(name)


class McpHub:
    """One session's view of the servers: their tools, and which of them each step shows."""

    def __init__(
        self,
        settings: McpSettings,
        pool: McpPool | None = None,
        *,
        stable: bool = False,
        late: bool = False,
    ) -> None:
        self._settings = settings
        #: The list is fixed for the conversation, until a fold (`refresh`).
        self.stable = stable
        #: APP ONLY: a server not ready when the conversation started joins it later
        #: (`join`), the model is told which are not ready (`note`), and a call to a server
        #: still starting waits for it. Off, a hub does exactly what it did before.
        self.late = late
        self._servers: dict[str, McpServerSettings] = {}
        #: Servers whose tools came from their last start (`ServerState.cached`) and that
        #: were still starting then.
        self._from_cache: set[str] = set()
        self._instructions: dict[str, str] = {}
        #: What the model was last told about each server (`note`), so it is told once.
        self._told: dict[str, str] = {}
        #: Servers that joined since the last note.
        self._joined: list[str] = []
        self._last_text = ""
        #: Tools joined quietly that a stable list has not taken in yet.
        self._unsettled = False
        #: Set when `join` chose the list again: `mcp_find`'s description names the servers
        #: then (`cli._build_engine` clears it).
        self.relisted = False
        self._frozen: frozenset[str] | None = None
        self._own_pool = pool is None
        self._pool = pool or McpPool()
        self._clients: dict[str, Session] = {}
        self._offered: dict[str, Offered] = {}
        self._failures: dict[str, str] = {}
        self._index: Bm25 | None = None
        self._order: list[str] = []
        self._pinned: OrderedDict[str, None] = OrderedDict()
        self._ranked: tuple[str, ...] = ()
        self._bare_names: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    # -- servers -------------------------------------------------------------------

    def connect(self) -> None:
        """Every enabled server, from the pool, started together. A conversation waits up
        to `connect_wait` for them; one still starting then is left out of it and joins the
        next. A server that fails is recorded and left out; the others are still offered."""
        wanted = {
            name: server for name, server in self._settings.servers.items()
            if server.enabled and name not in self._clients
        }
        self._servers.update(wanted)
        states = {
            name: self._pool.get(name, server, self._settings.start_timeout, wait=0)
            for name, server in wanted.items()
        }
        deadline = time.monotonic() + self._settings.connect_wait
        for name, state in states.items():
            state.ready.wait(max(0.0, deadline - time.monotonic()))
            if state.client is None:
                if self.late and state.starting and state.cached is not None:
                    # Registered at once from its last start; a call waits for the start.
                    self._from_cache.add(name)
                    self._instructions[name] = state.cached.instructions
                    self._add_tools(name, state.cached.tools)
                    continue
                self._failures[name] = state.error or "still starting"
                continue
            self._clients[name] = state.client
            self._instructions[name] = state.client.instructions
            self._add_tools(name, state.tools)
        self._reindex()

    def _add_tools(self, server: str, tools: Iterable[RemoteTool]) -> list[Offered]:
        added = []
        for tool in tools:
            offered = self._offer(server, tool)
            if offered.name not in self._offered:
                self._offered[offered.name] = offered
                added.append(offered)
        return added

    def _reindex(self) -> None:
        self._order = sorted(self._offered)
        self._index = Bm25([self._document(self._offered[name]) for name in self._order])
        self._bare_names.clear()

    def add_server(self, name: str, server: McpServerSettings) -> None:
        """APP ONLY. A server added while the conversation is open (`mcp_add`): started in
        the background, and joined at the next point `join` runs."""
        self._servers[name] = server
        self._failures[name] = "still starting"
        # `mcp_add`'s own result says it is starting; the note says only what follows.
        self._told[name] = "starting:still starting"
        self._pool.get(name, server, self._settings.start_timeout, wait=0)

    def wait_started(self, name: str, seconds: float, cancel: Any = None) -> ServerState | None:
        """APP ONLY. The pool's state of ``name`` once its start ended, or as it is after
        ``seconds`` (or a cancel)."""
        state = self._pool.peek(name)
        if state is None:
            return None
        deadline = time.monotonic() + seconds
        while not state.ready.wait(0.25):
            if (cancel is not None and cancel.cancelled) or time.monotonic() > deadline:
                break
        return state

    def join(self, *, quiet: bool = False) -> tuple[Offered, ...]:
        """APP ONLY. The tools of servers that were not ready and now are, registered from
        here on. Runs where the person writes and at a fold (`cli._build_engine`), the two
        points where a list that changes costs least: a fold re-reads the conversation
        anyway, and the person's message starts a turn the person is already waiting on.
        Returns what was added, for the registry and the guard.

        ``quiet`` (`mcp_add`, mid-turn) registers the tools so a call by name runs, but a
        stable list stays as it is until the next point above: it is chosen again there."""
        if not self.late:
            return ()
        added: list[Offered] = []
        for name in sorted(set(self._failures) | self._from_cache):
            state = self._pool.peek(name)
            if state is None or state.starting:
                continue
            if state.client is None:
                # Tools listed from the catalog stay registered; a call says why it fails.
                self._from_cache.discard(name)
                self._failures[name] = state.error or "stopped"
                continue
            self._failures.pop(name, None)
            self._from_cache.discard(name)
            self._clients[name] = state.client
            self._instructions[name] = state.client.instructions
            new = self._add_tools(name, state.tools)
            added.extend(new)
            if new and not quiet and self._told.get(name, "ready") != "ready":
                # Said only to a model that was told the server was not ready; a quiet join
                # is the model's own `mcp_add`, whose result said so already.
                self._joined.append(name)
            self._told[name] = "ready"
        if added:
            self._reindex()
            self._unsettled = True
        if self._unsettled and not (quiet and self.stable):
            self._unsettled = False
            self.relisted = True
            if self._last_text:
                ranked = self.search(self._last_text, limit=self._settings.max_tools * 2)
                with self._lock:
                    self._ranked = tuple(name for name, _ in ranked)
            if self.stable:
                chosen = self._choose()
                with self._lock:
                    self._frozen = chosen
        return tuple(added)

    def note(self) -> str | None:
        """APP ONLY. Harness text for the model, once per change: which servers are still
        starting, which failed and why, and which joined since. None when nothing changed.
        Before it, `failures` was read by nobody: a server not ready within `connect_wait`
        had no tools for the whole conversation, and neither the model nor the person was
        told."""
        if not self.late:
            return None
        lines = []
        for name in self._joined:
            lines.append(
                f"- {name} is ready now; its tools can be called"
                + (" (find them with mcp_find)." if self.stable else ".")
            )
            self._told[name] = "ready"
        self._joined.clear()
        for name, why in sorted(self._failures.items()):
            status = "starting" if why == "still starting" else "failed"
            if self._told.get(name) == f"{status}:{why}":
                continue
            self._told[name] = f"{status}:{why}"
            if status == "starting":
                lines.append(
                    f"- {name} is still starting, so its tools are not in your list yet; they "
                    "join at the person's next message once it is ready."
                )
            else:
                lines.append(
                    f"- {name} could not start: {why[:300]}. Its tools are not available; the "
                    "person sees the error in Conexiones."
                )
        if not lines:
            return None
        return NOTICE_PREFIX + "]\n" + "\n".join(lines)

    def forget_told(self) -> None:
        """A fold took the notes out of the window: what is still wrong is said again."""
        self._told = {name: told for name, told in self._told.items() if told == "ready"}

    def _live(self, server: str, cancel: Any) -> Session | str:
        """APP ONLY. The server's running client, waiting within `start_timeout` for a start
        still under way (one whose tools came from the catalog, or one that exited and is
        started again); else why there is none."""
        settings = self._servers.get(server)
        if settings is None:
            return "it is not configured"
        state = self._pool.get(server, settings, self._settings.start_timeout, wait=0)
        deadline = time.monotonic() + self._settings.start_timeout
        while not state.ready.wait(0.25):
            if cancel is not None and cancel.cancelled:
                return "cancelled"
            if time.monotonic() > deadline:
                return f"it did not finish starting in {self._settings.start_timeout:.0f} s"
        if state.client is None or not state.client.running:
            return state.error or "it stopped"
        self._clients[server] = state.client
        self._from_cache.discard(server)
        return state.client

    def close(self) -> None:
        """Stops the servers only when this hub started them itself; pooled ones belong
        to the process."""
        if self._own_pool:
            self._pool.close()
        self._clients.clear()

    @property
    def failures(self) -> Mapping[str, str]:
        return dict(self._failures)

    @property
    def servers(self) -> tuple[Session, ...]:
        return tuple(self._clients.values())

    def summaries(self) -> list[tuple[str, str]]:
        """(name, instructions) of every server whose tools are registered."""
        names = sorted({offered.server for offered in self._offered.values()})
        return [(name, self._instructions.get(name, "")) for name in names]

    def offered(self) -> tuple[Offered, ...]:
        return tuple(self._offered[name] for name in self._order)

    def _offer(self, server: str, tool: RemoteTool) -> Offered:
        name = tool_name(server, tool.name)
        schema = clean_schema(tool.input_schema)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = parameters({})
        description = tool.description.strip() or tool.title or tool.name
        if len(description) > self._settings.description_chars:
            description = description[: self._settings.description_chars].rstrip() + " …"
        spec = ToolSpec(
            name=name,
            description=f"[{server}] {description}",
            parameters=schema,
            budget_tokens=self._settings.result_budget,
            mutates=not tool.read_only,
            summary=_summary(tool.description),
            # What another program answered when asked: like a command's output.
            provenance=Provenance(
                reproducibility=Reproducibility.EPHEMERAL,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.BULK,
            ),
        )
        size = len(name) + len(spec.description) + len(json.dumps(schema, ensure_ascii=False))
        return Offered(name=name, server=server, remote=tool, spec=spec, tokens=size // 4 + 1)

    @staticmethod
    def _document(offered: Offered) -> list[str]:
        remote = offered.remote
        # The name counts twice: it is the most specific thing a tool says about itself.
        return terms(f"{remote.name} {remote.name} {remote.title} {remote.description}")

    # -- what a step shows ---------------------------------------------------------

    def observe(self, text: str) -> None:
        """A message from the person: rank every tool against it."""
        self._last_text = text
        ranked = self.search(text, limit=self._settings.max_tools * 2)
        with self._lock:
            self._ranked = tuple(name for name, _ in ranked)
            fix = self.stable and self._frozen is None
        if fix:
            # Fixed here, before any call or search of the turn can pin something into it.
            chosen = self._choose()
            with self._lock:
                self._frozen = chosen

    def refresh(self) -> None:
        """A fold rebuilt the conversation, so a stable list may change once: from the next
        step it shows the tools called and found most recently, then the ranking."""
        if not self.stable:
            return
        chosen = self._choose()
        with self._lock:
            self._frozen = chosen

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        if self._index is None or not self._order:
            return []
        scores = self._index.scores(terms(query))
        top = max(scores, default=0.0)
        # Half the best score: a tool that matched only a stray word ("main", "de") is
        # left out rather than shown at the cost of its definition.
        ranked = sorted(
            (
                (name, score)
                for name, score in zip(self._order, scores, strict=True)
                if score > 0 and score >= top * 0.5
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return ranked[:limit]

    def pin(self, names: Iterable[str]) -> None:
        """Keep these shown (a tool just called, or just found), most recent first."""
        with self._lock:
            for name in names:
                if name in self._offered:
                    self._pinned.pop(name, None)
                    self._pinned[name] = None
            while len(self._pinned) > self._settings.max_tools:
                self._pinned.popitem(last=False)

    def fits(self, name: str) -> bool:
        offered = self._offered.get(name)
        return offered is not None and offered.tokens <= self._settings.definitions_budget

    def shown(self) -> frozenset[str]:
        """The tools this step shows: pinned ones first, then the ranking, within the
        count and the token budget. A definition larger than the whole budget is never
        shown; `mcp_find` says so.

        A stable hub keeps the set it chose when the person wrote, until `refresh`."""
        with self._lock:
            if self._frozen is not None:
                return self._frozen
        return self._choose()

    def _choose(self) -> frozenset[str]:
        with self._lock:
            candidates = [*reversed(self._pinned), *self._ranked]
        chosen: list[str] = []
        spent = 0
        for name in candidates:
            if name in chosen or not self.fits(name):
                continue
            cost = self._offered[name].tokens
            if spent + cost > self._settings.definitions_budget:
                continue
            chosen.append(name)
            spent += cost
            if len(chosen) >= self._settings.max_tools:
                break
        return frozenset(chosen)

    def how_to_call(self, name: str) -> str:
        """How to call ``name`` by its name: its arguments, from its schema."""
        offered = self._offered[name]
        return f"arguments: {_arguments(offered.spec.parameters)}"

    # -- calls ---------------------------------------------------------------------

    def call(self, name: str, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        offered = self._offered[name]
        client = self._clients.get(offered.server)
        self.pin([name])
        if self.late and (client is None or not client.running):
            live = self._live(offered.server, getattr(context, "cancel", None))
            if live == "cancelled":
                return ToolResult.error("Cancelled before the server was ready.", "cancelled")
            if isinstance(live, str):
                return ToolResult.error(
                    f"The MCP server {offered.server!r} is not running ({live}), so "
                    f"{offered.remote.name} could not be called. Continue without it, or "
                    "tell the person; the error is shown in Conexiones.",
                    "mcp_server_down",
                )
            client = live
        if client is None or not client.running:
            return ToolResult.error(
                f"The MCP server {offered.server!r} is not running, so {offered.remote.name} "
                "could not be called. Continue without it, or tell the person.",
                "mcp_server_down",
            )
        try:
            outcome = client.call_tool(
                offered.remote.name, arguments, self._settings.call_timeout, context.cancel
            )
        except McpError as exc:
            if str(exc) == "cancelled":
                return ToolResult.error("Cancelled before the tool answered.", "cancelled")
            return ToolResult.error(
                f"{offered.remote.name} on {offered.server!r} failed: {exc}. Check the "
                "arguments against its schema, or try another tool.",
                "mcp_call_failed",
            )
        metadata = {"server": offered.server, "tool": offered.remote.name}
        # A result, above all an error, often names the tool to call next ("No app is open;
        # call open_binary first"). Pin any tool of this server it names, so the next step
        # shows it even when the person's words did not rank it. This is the generic escape
        # from a prerequisite the ranking missed; it is how a weak model recovers.
        mentioned = self._mentioned(offered.server, outcome.text)
        self.pin(mentioned)
        pictures = []
        text = outcome.text
        if self.stable:
            # The list does not change until a fold, so a tool the result names comes with
            # what a call to it needs.
            unshown = [n for n in mentioned if n != name and n not in self.shown()]
            if unshown:
                text += "\n\n" + "\n".join(
                    f"[{n} is not in your tool list; call it by this name. {self.how_to_call(n)}]"
                    for n in sorted(unshown)
                )
        for index, data in enumerate(outcome.pictures, start=1):
            artifacts = getattr(context, "artifacts", None)
            block, marker = result_image(artifacts, data, name=f"image {index}")
            text = f"{text}\n{marker}" if text else marker
            if block is not None:
                pictures.append(block)
        if outcome.is_error:
            return ToolResult(
                content=text, is_error=True, error_kind="mcp_tool_error", metadata=metadata,
                images=tuple(pictures),
            )
        return ToolResult(content=text, metadata=metadata, images=tuple(pictures))

    def _mentioned(self, server: str, text: str) -> list[str]:
        """Registered names of this server's tools named in `text`, the one just called
        aside. A tool's bare name (`open_binary`) is matched as a whole word."""
        catalog = self._bare_names.get(server)
        if catalog is None:
            catalog = {
                offered.remote.name: offered.name
                for offered in self._offered.values()
                if offered.server == server
            }
            self._bare_names[server] = catalog
        found = []
        for word in set(_IDENTIFIER.findall(text)):
            registered = catalog.get(word)
            if registered is not None:
                found.append(registered)
        return found


def _client(
    name: str,
    server: McpServerSettings,
    remember: Callable[[dict[str, object]], None] | None = None,
) -> Session:
    if server.remote:
        return HttpMcpClient(
            name, server.url, server.headers, transport=server.type, client_id=server.client_id
        )
    # `${VAR}` and `${VAR:-default}` in `command`, `args` and `env`, as Claude Code reads
    # them in `.mcp.json` (code.claude.com/docs/en/mcp, read 2026-11-10); `url` and
    # `headers` already were (`remote.expand`).
    command = expand(server.command)
    args = tuple(expand(arg) for arg in server.args)
    env = {key: expand(value) for key, value in server.env.items()}
    if is_container(command, args):
        return ContainerClient(
            name, command, args, env=env, cwd=server.cwd or None,
            http_port=server.http_port, http_path=server.http_path, remember=remember,
        )
    return McpClient(name, command, args, env=env, cwd=server.cwd or None)


class McpTool:
    """One remote tool, registered like any other and shown when the hub picks it."""

    #: Marks an MCP tool for the registry (`runnable_specs`): what the prompt lists each
    #: step is a few of these, but a call to any one by name still runs. Naming a tool is
    #: itself the discovery the listing saves tokens on, so there is no reason to refuse it.
    mcp = True

    def __init__(self, hub: McpHub, offered: Offered) -> None:
        self._hub = hub
        self.spec = offered.spec

    def available(self) -> bool:
        return self.spec.name in self._hub.shown()

    def withheld_reason(self) -> str:
        if not self._hub.fits(self.spec.name):
            return (
                "its definition is too large to show; use another tool, or tell the person "
                "which one is missing"
            )
        return f'not loaded this step; call mcp_find with "{self.spec.name}" to load it'

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return self._hub.call(self.spec.name, arguments, context)


class McpFindTool:
    """Search every connected server's tools and show the best matches from the next step."""

    _LOADED = 3
    _LISTED = 8

    def __init__(self, hub: McpHub) -> None:
        self._hub = hub
        self.spec = self._spec(hub)

    @staticmethod
    def _spec(hub: McpHub) -> ToolSpec:
        stable = hub.stable
        servers = []
        for name, instructions in hub.summaries():
            count = sum(1 for offered in hub.offered() if offered.server == name)
            about = _summary(instructions) if instructions else ""
            servers.append(f"{name} ({count} tools){': ' + about if about else ''}")
        return ToolSpec(
            name="mcp_find",
            description=(
                (
                    "Find tools from the connected MCP servers. Each match comes with its "
                    "arguments: call it by its name, though it is not in your tool list. "
                    if stable
                    else "Find tools from the connected MCP servers and load the best "
                    "matches, which you can call from your next step. Only a few of their "
                    "tools are loaded at a time. "
                )
                + "Search in English, with words a tool's name or description would use. "
                "Servers: " + "; ".join(servers)
            ),
            parameters=parameters(
                {
                    "query": {
                        "type": "string",
                        "description": "What the tool should do, or its name.",
                    }
                },
                ("query",),
            ),
            aliases={"query": ("q", "search", "name", "tool")},
            # Room for the arguments of the tools a stable list leaves to be called by name.
            budget_tokens=1500 if stable else 600,
            core=True,
            summary="find and load tools from the connected MCP servers",
            # A list of tool names, found again by asking again.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.ACKNOWLEDGEMENT,
                shape=PayloadShape.COMPACT,
            ),
        )

    def refresh(self) -> None:
        """A server joined (`McpHub.join`): the description names it from here on."""
        self.spec = self._spec(self._hub)

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult.error("Give a query: what the tool should do.", "missing_argument")
        exact = [o.name for o in self._hub.offered() if query in (o.name, o.remote.name)]
        ranked = exact + [name for name, _ in self._hub.search(query, self._LISTED)]
        names = list(dict.fromkeys(ranked))[: self._LISTED]
        if not names:
            return ToolResult(
                content=f"No MCP tool matches {query!r}. Try other English words.",
                metadata={"found": 0},
            )
        stable = self._hub.stable
        if stable:
            # Nothing is loaded: the list stays as it is, and the best few come with what a
            # call by name needs.
            loadable = names[: self._LOADED]
        else:
            loadable = [name for name in names if self._hub.fits(name)][: self._LOADED]
        self._hub.pin(reversed(loadable))
        offered = {o.name: o for o in self._hub.offered()}
        shown = self._hub.shown() if stable else frozenset()
        lines = []
        for name in names:
            remote = offered[name].remote
            notes = []
            if stable and name in shown:
                notes.append("in your tool list")
            elif name in loadable:
                notes.append("callable now" if stable else "loaded")
            elif not stable and not self._hub.fits(name):
                notes.append("too large to load")
            if remote.destructive:
                notes.append("asks the person first")
            suffix = f" ({', '.join(notes)})" if notes else ""
            lines.append(f"- {name}{suffix}: {_summary(remote.description)}")
            if stable and name in loadable and name not in shown:
                lines.append(f"  {self._hub.how_to_call(name)}")
        closing = (
            "\nCall one by its exact name with those arguments."
            if stable
            else "\nCall a loaded tool by its name from your next step."
        )
        return ToolResult(
            content="\n".join(lines) + closing,
            metadata={"found": len(names), "loaded": loadable},
        )
