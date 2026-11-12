"""A project's whole thread, read back from the store a page at a time, for the desktop.

The desktop shows each project as one continuous thread. Underneath, a project has many
sessions: one per time the app opened it, each chained through compaction to the children
it was folded into. `thread_order` puts them back in one order: every root session (one not
reached by `COMPACTED_TO` or `DELEGATED_TO`) oldest first, each followed by the chain of
sessions it was compacted into, the way `recall` walks the lineage.

A *source* answers the few bounded questions paging needs: the sessions, one session's
messages below a seq (newest first, `limit` at a time), and a session's first and last few.
A page costs a handful of small reads, never the whole thread (`server/history.py` pages).
Two sources answer them the same way:

- `GraphSource`: Neo4j, through the transcript's existing `(session_id, seq)` index.
- `WalSource`: the project's write-ahead journals (`.cogito/wal/*.jsonl`) replayed in
  memory the way the projections read them. Used when the graph cannot be reached or is
  behind (`journals_behind`: an opening whose journal the graph has not caught up with).

Two things a naive read gets wrong:

- **A compaction copies.** `context.compact` seeds the child with a handover (seq 0), then
  re-appends the parent's most recent messages (seq 1..k) under new ids, with `step = 0`
  and `tokens = 0`. The originals are already in the parent, so the copies are skipped
  (`copied_count`). The handover is a `Handover` marker, not a message: its text is the
  model's summary, not something anyone said.
- **A discarded attempt** (`discarded_attempt`, the visible-test gate's set-aside branch)
  never re-enters the conversation, so it stays out of the thread too.

This module only reads, and nothing it returns reaches a model.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cogito.config import paths
from cogito.errors import GraphUnavailable
from cogito.graph.sessions import project_id_for
from cogito.graph.wal import Journal

log = logging.getLogger(__name__)

#: The property the visible-test gate sets on a message it set aside. Passed as a query
#: parameter instead of written into the Cypher, so a database that has never seen the
#: key does not warn about an unknown property on every history read.
DISCARDED_KEY = "discarded_attempt"


@dataclass(frozen=True, slots=True)
class StoredCall:
    """One `tool_called` record: its id, name and outcome, and the rest of what the
    node carries (arguments, the clipped result, tool metadata, provenance)."""

    id: str
    name: str
    ok: bool
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: str
    session_id: str
    seq: int
    role: str
    text: str
    step: int
    at: str
    calls: tuple[StoredCall, ...] = ()
    tokens: int = 0
    #: Hidden by a rewind that no redo brought back: kept, never shown.
    rewound: bool = False
    #: On a message that opened a turn: how the turn ended (`turn_ended`), and the kind of
    #: provider failure when one stopped it. Empty while it runs, or for an older record.
    stop_reason: str = ""
    failure: str = ""
    #: "call" | "text", set only alongside ``stop_reason == "output_limit"``; empty
    #: otherwise or for an older record. See `loop.engine.TurnResult.cut`.
    cut: str = ""
    #: APP-ONLY auto-continue (`BudgetSettings.max_auto_continuations`): true when this
    #: reply followed an auto-continue notice with no call in between
    #: (`loop.engine.Engine._append`'s ``continued``, the same fact as
    #: `AssistantText.continued`). False for an older record. `server.history` joins such
    #: a message onto the one before it instead of a new paragraph.
    continued: bool = False


@dataclass(frozen=True, slots=True)
class Handover:
    """Where a compaction folded the conversation into ``session_id`` (the child)."""

    session_id: str
    message_id: str
    at: str
    #: The fold's seed was hidden by a rewind to an earlier point, so the fold is no
    #: longer part of the conversation either.
    rewound: bool = False


@dataclass(frozen=True, slots=True)
class StoredSession:
    id: str
    created_at: str
    #: The session this one was compacted into, if any.
    child: str | None = None
    #: The session this one was compacted from, if any.
    parent: str | None = None
    #: Started by the delegate tool: a subagent's own conversation, not the project's.
    delegated: bool = False
    #: For a fold's child, how many messages after its handover are the fold's copies, as
    #: `session_compacted` recorded it. None for a fold journalled before it did, and then
    #: `copied_count` finds them.
    copies: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetMarker:
    """A decision on COGITO's own budget (``budget_decided``): what it was and where it
    sits in the thread (right after message ``after_seq`` of ``session_id``). Not a
    message, and never part of the transcript."""

    id: str
    session_id: str
    after_seq: int
    decision: str
    reason: str
    period: str | None
    limit: float | None
    spent: float | None
    currency: str
    at: str


@dataclass(frozen=True, slots=True)
class RewindMarker:
    """Where the conversation was taken back to an earlier point ("Volver a este punto",
    ``conversation_rewound``). It sits right after message ``after_seq`` of ``session_id``,
    the session's last message when it happened, and stands in for the messages it hid
    until a redo brings them back."""

    id: str
    session_id: str
    after_seq: int
    point_id: str
    #: When the point was sent: «Volviste a las HH:MM» names the point's time.
    point_at: str
    #: How many files the rewind restored.
    files: int
    at: str
    #: Whether «Rehacer» can still bring it back: nothing happened in its session since.
    redo: bool


class Source(Protocol):
    """What paging asks of the store. Every message read is bounded by its `limit`."""

    source: str

    def sessions(self) -> list[StoredSession]: ...

    def messages(
        self, session_id: str, *, above: int, below: int | None, limit: int
    ) -> list[StoredMessage]:
        """``above < seq < below`` (no upper bound for None), newest first, at most
        ``limit``, discarded attempts left out."""
        ...

    def head(self, session_id: str, limit: int) -> list[StoredMessage]:
        """The first ``limit`` messages from seq 1 on, oldest first."""
        ...

    def tail(self, session_id: str, limit: int) -> list[StoredMessage]:
        """The last ``limit`` messages, oldest first."""
        ...

    def seed(self, session_id: str) -> StoredMessage | None:
        """Seq 0: a compacted child's handover."""
        ...

    # Optional: ``markers(session_id, low, high)``, the budget decisions and rewinds placed
    # after a message of seq ``low..high`` of a session, oldest first. A source without it
    # has none.


# -- the thread's order -------------------------------------------------------------------


def thread_order(sessions: Sequence[StoredSession]) -> list[StoredSession]:
    """Root sessions oldest first, each followed by its compaction chain. Delegated
    sessions are a subagent's conversation and are not in the project's thread."""
    by_id = {s.id: s for s in sessions}
    roots = sorted(
        (s for s in sessions if (s.parent is None or s.parent not in by_id) and not s.delegated),
        key=lambda s: (s.created_at or "", s.id),
    )
    order: list[StoredSession] = []
    seen: set[str] = set()
    for root in roots:
        current: StoredSession | None = root
        while current is not None and current.id not in seen:
            seen.add(current.id)
            order.append(current)
            current = by_id.get(current.child) if current.child else None
    return order


#: How `context.inbound` says where it saved a message it took out of the window: the
#: SHA-256 of exactly the text it replaced. Repeated here because the graph cannot import
#: `context` (CONTRIBUTING section 2); cogito-lab checks every opening `inbound.fit` writes
#: against it.
_OFFLOADED = re.compile(
    r"\[(?:The user's message|An earlier message from the user|This message) was too long to "
    r"(?:send whole|keep in the context window): about [\d,]+ tokens[^\n]*? It is saved "
    r"unchanged in [^\n]+? \([\d,]+ lines, [\d,]+ bytes, sha256 (?P<sha256>[0-9a-f]{64})\)\."
)


def same_text(first: str, second: str) -> bool:
    """Whether two journalled texts are one message: equal, or one is the other with its
    body offloaded. A message the loop offloaded after it was copied (a fold's copy written
    again on a reopening) keeps whatever came before the notice, a steer's wrapper say, and
    names the digest of what it replaced."""
    if first == second:
        return True
    for offloaded, whole in ((first, second), (second, first)):
        found = _OFFLOADED.search(offloaded)
        if found is None:
            continue
        prefix = offloaded[: found.start()]
        if not whole.startswith(prefix):
            continue
        rest = whole[len(prefix) :]
        if hashlib.sha256(rest.encode("utf-8")).hexdigest() == found["sha256"]:
            return True
        other = _OFFLOADED.search(whole)
        same_place = other is not None and other.start() == found.start()
        if same_place and other["sha256"] == found["sha256"]:
            return True
    return False


def _same(first: StoredMessage, second: StoredMessage) -> bool:
    return first.role == second.role and same_text(first.text, second.text)


def copied_count(source: Source, parent_id: str, child_id: str) -> int:
    """How many of the child's messages after its handover are `context.compact`'s copies
    of the parent's last messages.

    Every copy is recorded with ``step = 0`` and ``tokens = 0`` right after the handover,
    so the candidates are that leading run. A first instruction or steer appended at step 0
    right after a fold can join the run, so the copies are its longest prefix that matches
    the parent's tail message for message.
    """
    window = 16
    while True:
        head = source.head(child_id, window)
        run = 0
        while run < len(head) and head[run].step == 0 and head[run].tokens == 0:
            run += 1
        if run < len(head) or len(head) < window:
            break
        window *= 4
    if run == 0:
        return 0
    tail = source.tail(parent_id, run)
    for k in range(min(run, len(tail)), 0, -1):
        if all(_same(a, b) for a, b in zip(head[:k], tail[-k:], strict=True)):
            return k
    return 0


# -- the graph ----------------------------------------------------------------------------

_SESSIONS = (
    "MATCH (:Project {id: $project_id})-[:HAS_SESSION]->(s:Session) "
    "OPTIONAL MATCH (s)-[:COMPACTED_TO]->(child:Session) "
    "OPTIONAL MATCH (parent:Session)-[:COMPACTED_TO]->(s) "
    "RETURN s.id AS id, s.created_at AS created_at, child.id AS child, parent.id AS parent, "
    "s[$copies] AS copies, "
    "EXISTS { MATCH (:Session)-[:DELEGATED_TO]->(s) } AS delegated, "
    # Whether a budget decision was ever taken in it, or the conversation rewound: only such
    # a session is asked for its markers.
    "EXISTS { MATCH (s)-[:RECORDED]->(d) WHERE $decision IN labels(d) } AS decided, "
    "EXISTS { MATCH (s)-[:RECORDED]->(r) WHERE $rewind IN labels(r) AND r[$after] IS NOT NULL } "
    "AS rewound"
)
_DECISION_LABEL = "BudgetDecision"
_REWIND_LABEL = "Rewind"
#: The property a rewind sets on each message it hides; see `DISCARDED_KEY` for why a key
#: is a parameter.
REWOUND_KEY = "rewound_by"
#: The property `session_compacted` sets on a fold's child: how many copies follow its
#: handover (`graph.schema`). A parameter for the same reason.
COPIES_KEY = "copies"


def _copies(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value

_COLUMNS = (
    "m.id AS id, m.session_id AS session_id, m.seq AS seq, m.role AS role, m.text AS text, "
    "m.step AS step, m.tokens AS tokens, m.created_at AS created_at, "
    "m[$rewound] AS rewound_by, m.stop_reason AS stop_reason, m.turn_failure AS turn_failure, "
    "m.turn_cut AS turn_cut, m.continued AS continued"
)

_PAGE = (
    "MATCH (m:Message {session_id: $session}) "
    "WHERE m.seq > $above AND ($below IS NULL OR m.seq < $below) AND m[$discarded] IS NULL "
    "WITH m ORDER BY m.seq DESC LIMIT $limit "
    # An older projection shared one node between messages that reused a call id (no
    # `message_id`). Such a node is shown only if the message has no node of its own for
    # that call.
    "OPTIONAL MATCH (m)-[:INVOKED]->(t:ToolCall) "
    "WHERE t.message_id = m.id OR (t.message_id IS NULL AND NOT EXISTS { "
    "MATCH (m)-[:INVOKED]->(own:ToolCall {message_id: m.id}) WHERE own.call_id = t.id }) "
    "WITH m, t ORDER BY t.event_sequence "
    "WITH m, collect(properties(t)) AS calls "
    f"RETURN {_COLUMNS}, calls ORDER BY seq DESC"
)

_HEAD = (
    "MATCH (m:Message {session_id: $session}) WHERE m.seq >= 1 AND m[$discarded] IS NULL "
    f"RETURN {_COLUMNS} ORDER BY m.seq LIMIT $limit"
)

_TAIL = (
    "MATCH (m:Message {session_id: $session}) WHERE m[$discarded] IS NULL "
    f"RETURN {_COLUMNS} ORDER BY m.seq DESC LIMIT $limit"
)

_SEED = f"MATCH (m:Message {{session_id: $session, seq: 0}}) RETURN {_COLUMNS} LIMIT 1"

#: A session's rewinds, each point's time, and how many messages came after each: what
#: decides whether «Rehacer» still applies.
_REWINDS = (
    "MATCH (s:Session {id: $session})-[:RECORDED]->(r) "
    "WHERE $label IN labels(r) AND r[$after] IS NOT NULL "
    "OPTIONAL MATCH (p:Message {id: r.point_id}) "
    "RETURN properties(r) AS rewind, p.created_at AS point_at, "
    "COUNT { MATCH (m:Message {session_id: $session}) WHERE m.seq > r[$after] } AS later"
)

#: Label and keys passed as parameters, as `DISCARDED_KEY` is: a database that has never
#: seen a budget decision does not warn about an unknown label or key on every read.
_MARKERS = (
    "MATCH (:Session {id: $session})-[:RECORDED]->(d) "
    "WHERE $label IN labels(d) AND d[$after] >= $low AND d[$after] <= $high "
    "RETURN properties(d) AS marker ORDER BY d[$after], d[$created]"
)


def _marker(properties: Mapping[str, Any], at: str | None = None) -> BudgetMarker:
    def amount(key: str) -> float | None:
        value = properties.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    period = properties.get("period")
    return BudgetMarker(
        id=str(properties.get("id") or ""),
        session_id=str(properties.get("session_id") or ""),
        after_seq=int(properties.get("after_seq") or 0),
        decision=str(properties.get("decision") or ""),
        reason=str(properties.get("reason") or ""),
        period=str(period) if period else None,
        limit=amount("limit"),
        spent=amount("spent"),
        currency=str(properties.get("currency") or ""),
        at=str(at if at is not None else properties.get("created_at") or ""),
    )


def _call(properties: Mapping[str, Any]) -> StoredCall:
    """The provider's own call id: `call_id` on a node, `id` in the journal (and on nodes
    an older projection made)."""
    own = ("id", "call_id", "name", "ok", "message_id")
    rest = {k: v for k, v in properties.items() if k not in own}
    return StoredCall(
        id=str(properties.get("call_id") or properties.get("id") or ""),
        name=str(properties.get("name") or ""),
        ok=bool(properties.get("ok")),
        properties=rest,
    )


def _message(row: Mapping[str, Any]) -> StoredMessage:
    return StoredMessage(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        seq=int(row.get("seq") or 0),
        role=str(row.get("role") or ""),
        text=str(row.get("text") or ""),
        step=int(row.get("step") or 0),
        at=str(row.get("created_at") or ""),
        calls=tuple(_call(p) for p in row.get("calls") or () if p),
        tokens=int(row.get("tokens") or 0),
        rewound=bool(row.get("rewound_by")),
        stop_reason=str(row.get("stop_reason") or ""),
        failure=str(row.get("turn_failure") or ""),
        cut=str(row.get("turn_cut") or ""),
        continued=bool(row.get("continued")),
    )


def _rewind_markers(rows: Sequence[Mapping[str, Any]], session_id: str) -> list[RewindMarker]:
    """The session's standing rewinds as markers. A rewind whose conversation a redo
    brought back is not shown. «Rehacer» applies only to the last rewind, and only while
    nothing was appended after it."""
    standing = []
    latest = max((str(row["rewind"].get("created_at") or "") for row in rows), default="")
    for row in rows:
        rewind = row["rewind"]
        # Standing until its conversation is back. If only its files were redone (the app
        # closed between the two halves of a redo), the conversation is still owed.
        if rewind.get("conversation_restored_at"):
            continue
        created = str(rewind.get("created_at") or "")
        standing.append(
            RewindMarker(
                id=str(rewind.get("id") or ""),
                session_id=session_id,
                after_seq=int(rewind.get("after_seq") or 0),
                point_id=str(rewind.get("point_id") or ""),
                point_at=str(row.get("point_at") or ""),
                files=len(rewind.get("restored_paths") or ()),
                at=created,
                redo=created == latest and not row.get("later"),
            )
        )
    standing.sort(key=lambda m: (m.after_seq, m.at))
    return standing


class GraphSource:
    """Every question is one small query. Raises `GraphUnavailable` when Neo4j cannot be
    reached."""

    source = "graph"

    def __init__(self, driver: Any, project_root: Path) -> None:
        self._driver = driver
        self._project_id = project_id_for(project_root)
        self._sessions: list[StoredSession] | None = None
        #: Sessions in which a budget decision was taken, from the same read.
        self._decided: set[str] = set()
        #: Sessions whose conversation was rewound, from the same read.
        self._rewound: set[str] = set()

    def sessions(self) -> list[StoredSession]:
        """Asked once per source (one source serves one page)."""
        if self._sessions is None:
            rows = self._driver.query(
                _SESSIONS,
                project_id=self._project_id,
                decision=_DECISION_LABEL,
                rewind=_REWIND_LABEL,
                after="after_seq",
                copies=COPIES_KEY,
            )
            self._decided = {row["id"] for row in rows if row.get("decided")}
            self._rewound = {row["id"] for row in rows if row.get("rewound")}
            self._sessions = [
                StoredSession(
                    id=row["id"],
                    created_at=str(row.get("created_at") or ""),
                    child=row.get("child"),
                    parent=row.get("parent"),
                    delegated=bool(row.get("delegated")),
                    copies=_copies(row.get("copies")),
                )
                for row in rows
            ]
        return list(self._sessions)

    def messages(
        self, session_id: str, *, above: int, below: int | None, limit: int
    ) -> list[StoredMessage]:
        rows = self._driver.query(
            _PAGE,
            session=session_id,
            above=above,
            below=below,
            limit=limit,
            discarded=DISCARDED_KEY,
            rewound=REWOUND_KEY,
        )
        return [_message(row) for row in rows]

    def head(self, session_id: str, limit: int) -> list[StoredMessage]:
        rows = self._driver.query(
            _HEAD, session=session_id, limit=limit, discarded=DISCARDED_KEY, rewound=REWOUND_KEY
        )
        return [_message(row) for row in rows]

    def tail(self, session_id: str, limit: int) -> list[StoredMessage]:
        rows = self._driver.query(
            _TAIL, session=session_id, limit=limit, discarded=DISCARDED_KEY, rewound=REWOUND_KEY
        )
        return [_message(row) for row in reversed(rows)]

    def seed(self, session_id: str) -> StoredMessage | None:
        rows = self._driver.query(_SEED, session=session_id, rewound=REWOUND_KEY)
        return _message(rows[0]) if rows else None

    def markers(
        self, session_id: str, low: int, high: int
    ) -> list[BudgetMarker | RewindMarker]:
        """Queries only sessions where `sessions` found a decision or a rewind, so a thread
        with neither costs no read."""
        found: list[BudgetMarker | RewindMarker] = []
        if session_id in self._decided:
            rows = self._driver.query(
                _MARKERS,
                session=session_id,
                label=_DECISION_LABEL,
                after="after_seq",
                created="created_at",
                low=low,
                high=high,
            )
            found.extend(_marker(row["marker"]) for row in rows if row.get("marker"))
        if session_id in self._rewound:
            rows = self._driver.query(
                _REWINDS, session=session_id, label=_REWIND_LABEL, after="after_seq"
            )
            found.extend(
                marker
                for marker in _rewind_markers(rows, session_id)
                if low <= marker.after_seq <= high
            )
        return found


# -- the journal --------------------------------------------------------------------------


def _journals(project_root: Path) -> list[Path]:
    directory = paths.project_dir(project_root) / "wal"
    return sorted(directory.glob("*.jsonl")) if directory.is_dir() else []


#: Per journal, whether anything is unprojected, keyed by its size, its mtime and its
#: checkpoint's mtime. An unchanged journal and checkpoint are not read again. Past
#: openings never change, so each is read at most once per process.
_BEHIND_CACHE: dict[str, tuple[tuple[int, int, int], bool]] = {}
_BEHIND_LOCK = threading.Lock()


def journals_behind(project_root: Path) -> bool:
    """True when any of the project's journals holds an event the graph has not taken:
    an earlier opening that ended while Neo4j was down, or this one while it is."""
    for path in _journals(project_root):
        try:
            written = path.stat()
        except OSError:
            continue
        if written.st_size == 0:
            continue
        try:
            projected = path.with_suffix(".ckpt").stat().st_mtime_ns
        except OSError:
            projected = -1
        stamp = (written.st_size, written.st_mtime_ns, projected)
        with _BEHIND_LOCK:
            cached = _BEHIND_CACHE.get(str(path))
        if cached is not None and cached[0] == stamp:
            behind = cached[1]
        else:
            behind = Journal(path).pending_count() > 0
            with _BEHIND_LOCK:
                _BEHIND_CACHE[str(path)] = (stamp, behind)
        if behind:
            return True
    return False


class WalSource:
    """The journals replayed in memory: exactly the events `graph.schema` would have
    projected, read the way its projections read them. Later facts about the same id
    replace earlier ones, and a tool call belongs to the message that made it."""

    source = "wal"

    def __init__(self, project_root: Path) -> None:
        events = []
        for path in _journals(project_root):
            events.extend(event for _, event in Journal(path).read())
        events.sort(key=lambda event: event.at)
        self._sessions, self._by_session, self._markers = _replay(events)

    def sessions(self) -> list[StoredSession]:
        return list(self._sessions)

    def messages(
        self, session_id: str, *, above: int, below: int | None, limit: int
    ) -> list[StoredMessage]:
        chosen = [
            m
            for m in reversed(self._by_session.get(session_id, []))
            if m.seq > above and (below is None or m.seq < below)
        ]
        return chosen[:limit]

    def head(self, session_id: str, limit: int) -> list[StoredMessage]:
        return [m for m in self._by_session.get(session_id, []) if m.seq >= 1][:limit]

    def tail(self, session_id: str, limit: int) -> list[StoredMessage]:
        listed = self._by_session.get(session_id, [])
        return listed[-limit:] if limit > 0 else []

    def seed(self, session_id: str) -> StoredMessage | None:
        return next((m for m in self._by_session.get(session_id, []) if m.seq == 0), None)

    def markers(
        self, session_id: str, low: int, high: int
    ) -> list[BudgetMarker | RewindMarker]:
        return [m for m in self._markers.get(session_id, []) if low <= m.after_seq <= high]


def _replay(
    events: Sequence[Any],
) -> tuple[
    list[StoredSession],
    dict[str, list[StoredMessage]],
    dict[str, list[BudgetMarker | RewindMarker]],
]:
    sessions: dict[str, dict[str, Any]] = {}
    messages: dict[str, dict[str, Any]] = {}
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    attempts: dict[str, dict[str, Any]] = {}
    markers: dict[str, BudgetMarker] = {}
    ended: dict[str, Mapping[str, Any]] = {}
    # Rewinds, the way `graph.schema` projects them: the messages each hid, and what became
    # of it. `order` places each against the messages appended after it.
    rewinds: dict[str, dict[str, Any]] = {}
    files: dict[str, int] = {}
    performed_at: dict[str, str] = {}
    hidden: dict[str, str] = {}
    appended: dict[str, int] = {}
    for order, event in enumerate(events):
        data = event.data
        if event.kind == "rewind_performed":
            files[str(data.get("id"))] = len(data.get("restored_paths") or ())
            # The `:Rewind` node's `created_at` is this event's, the first to write it.
            performed_at.setdefault(str(data.get("id")), event.at)
        elif event.kind == "conversation_rewound":
            rewind_id = str(data.get("id"))
            rewinds[rewind_id] = {**data, "at": event.at, "order": order}
            for message_id in data.get("hidden") or ():
                hidden[str(message_id)] = rewind_id
        elif event.kind == "conversation_restored":
            rewind_id = str(data.get("id"))
            if rewind_id in rewinds:
                rewinds[rewind_id]["restored"] = True
                for message_id in rewinds[rewind_id].get("hidden") or ():
                    if hidden.get(str(message_id)) == rewind_id:
                        del hidden[str(message_id)]
        if event.kind == "message_appended" and str(data.get("id")) not in messages:
            appended[str(data.get("session_id"))] = order
        if event.kind == "budget_decided":
            markers[str(data.get("id"))] = _marker(data, at=event.at)
        elif event.kind == "session_started":
            entry = sessions.setdefault(str(data.get("id")), {})
            entry.setdefault("created_at", event.at)
        elif event.kind == "message_appended":
            # `created_at` is the first write's, like the projection's `coalesce`.
            message_id = str(data.get("id"))
            first_at = messages.get(message_id, {}).get("at", event.at)
            messages[message_id] = {**data, "at": first_at}
        elif event.kind == "tool_called":
            properties = dict(data.get("properties") or {})
            key = (str(data.get("message_id")), str(data.get("id")))
            calls[key] = {**properties, "id": data.get("id"), "message_id": data.get("message_id")}
        elif event.kind == "session_compacted":
            sessions.setdefault(str(data.get("parent_id")), {})["child"] = data.get("child_id")
            child = sessions.setdefault(str(data.get("child_id")), {})
            child["parent"] = data.get("parent_id")
            child["copies"] = _copies(data.get("copies"))
        elif event.kind == "session_delegated":
            sessions.setdefault(str(data.get("child_id")), {})["delegated"] = True
        elif event.kind == "attempt_branch":
            attempts[str(data.get("id"))] = dict(data)
        elif event.kind == "turn_ended":
            ended[str(data.get("message_id"))] = data

    discarded: set[tuple[str, int]] = set()
    for attempt in attempts.values():
        if attempt.get("status") != "discarded":
            continue
        first, last = int(attempt.get("first_seq", 0)), int(attempt.get("last_seq", -1))
        discarded.update((str(attempt.get("session_id")), seq) for seq in range(first, last + 1))

    calls_by_message: dict[str, list[dict[str, Any]]] = {}
    for call in calls.values():
        calls_by_message.setdefault(str(call.get("message_id")), []).append(call)

    by_session: dict[str, list[StoredMessage]] = {}
    for data in messages.values():
        session_id, seq = str(data.get("session_id")), int(data.get("seq") or 0)
        if (session_id, seq) in discarded:
            continue
        own = sorted(
            calls_by_message.get(str(data.get("id")), []),
            key=lambda c: (c.get("event_sequence") is None, c.get("event_sequence") or 0),
        )
        by_session.setdefault(session_id, []).append(
            StoredMessage(
                id=str(data.get("id")),
                session_id=session_id,
                seq=seq,
                role=str(data.get("role") or ""),
                text=str(data.get("text") or ""),
                step=int(data.get("step") or 0),
                at=str(data.get("at") or ""),
                calls=tuple(_call(c) for c in own),
                rewound=str(data.get("id")) in hidden,
                tokens=int(data.get("tokens") or 0),
                stop_reason=str(ended.get(str(data.get("id")), {}).get("stop_reason") or ""),
                failure=str(ended.get(str(data.get("id")), {}).get("failure") or ""),
                cut=str(ended.get(str(data.get("id")), {}).get("cut") or ""),
                continued=bool(data.get("continued")),
            )
        )
    for listed in by_session.values():
        listed.sort(key=lambda m: m.seq)
    stored = [
        StoredSession(
            id=session_id,
            created_at=str(entry.get("created_at") or ""),
            child=entry.get("child"),
            parent=entry.get("parent"),
            delegated=bool(entry.get("delegated")),
            copies=entry.get("copies"),
        )
        for session_id, entry in sessions.items()
        if "created_at" in entry
    ]
    placed: dict[str, list[BudgetMarker | RewindMarker]] = {}
    for marker in markers.values():
        placed.setdefault(marker.session_id, []).append(marker)
    point_at = {str(data.get("id")): str(data.get("at") or "") for data in messages.values()}
    for rewind_id, rewind in rewinds.items():
        # Standing until its conversation is back. If only its files were redone (the app
        # closed between the two halves of a redo), the conversation is still owed.
        if rewind.get("restored"):
            continue
        session_id = str(rewind.get("session_id"))
        later = any(
            other.get("order", 0) > rewind["order"] and str(other.get("session_id")) == session_id
            for other in rewinds.values()
        )
        placed.setdefault(session_id, []).append(
            RewindMarker(
                id=rewind_id,
                session_id=session_id,
                after_seq=int(rewind.get("after_seq") or 0),
                point_id=str(rewind.get("point_id") or ""),
                point_at=point_at.get(str(rewind.get("point_id")), ""),
                files=files.get(rewind_id, 0),
                at=performed_at.get(rewind_id, str(rewind.get("at") or "")),
                redo=not later and appended.get(session_id, -1) < rewind["order"],
            )
        )
    for listed_markers in placed.values():
        listed_markers.sort(key=lambda m: (m.after_seq, m.at))
    return stored, by_session, placed


#: The last replay per project, reused while no journal of it has changed (size and
#: modification time), so paging through history on the journal reads it once.
_WAL_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], WalSource]] = {}
_WAL_CACHE_LOCK = threading.Lock()


def wal_source(project_root: Path) -> WalSource:
    stamp = []
    for path in _journals(project_root):
        try:
            status = path.stat()
        except OSError:
            continue
        stamp.append((str(path), status.st_size, status.st_mtime_ns))
    key = os.fspath(project_root)
    with _WAL_CACHE_LOCK:
        cached = _WAL_CACHE.get(key)
        if cached is not None and cached[0] == tuple(stamp):
            return cached[1]
    fresh = WalSource(project_root)
    with _WAL_CACHE_LOCK:
        _WAL_CACHE[key] = (tuple(stamp), fresh)
    return fresh


def open_source(driver: Any | None, project_root: Path) -> Source:
    """The graph, or the journals when there is no graph, it cannot be reached, or it is
    behind them. A `GraphSource` can still raise `GraphUnavailable` on a later question;
    the pager then falls back to `wal_source`."""
    if driver is None:
        return wal_source(project_root)
    if journals_behind(project_root):
        log.info("history: the graph is behind the journals; reading the journals")
        return wal_source(project_root)
    source = GraphSource(driver, project_root)
    try:
        source.sessions()
    except GraphUnavailable as exc:
        log.warning("history: graph unavailable, replaying the journal: %s", exc)
        return wal_source(project_root)
    return source
