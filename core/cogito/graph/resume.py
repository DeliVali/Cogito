"""Where a project's conversation continues when the app opens it again, and what the
conversation is after a person rewound it.

A project is one continuous chat. `graph.history` reads the whole thread back for the
desktop, and after a restart the model must hold the conversation the window shows. `find`
answers what an opening asks first: which session is this project's conversation, and what
did the model hold in it?

- **The conversation is the session the thread's latest message belongs to**, followed
  through its compaction lineage (`COMPACTED_TO`) to the live leaf. An opening that said
  nothing, a subagent's own session, and a fold that died before its edge was written are
  not the conversation.
- **It is read from the journal, not the graph.** The journal is written and fsynced before
  the graph is tried, so it is complete even when Neo4j is behind or down, which is exactly
  when a killed app is opened again. It is also the only place a message's blocks are kept
  (`graph.schema.parameters_for` says why they are not projected). A conversation's lineage
  is always in one journal, the one its first opening created: a fold records its child
  through the same store, and a resumed session appends to the journal that holds it.
- **Newest journal first.** A journal cannot hold a message newer than its own modification
  time, so the files are read newest first and reading stops once the newest message found
  is newer than the next file. Usually that is one file.

## The conversation is a chain, replayed

The journal is replayed event by event (`Replay`). Each message's parent is the message
that was its session's tip when it was appended; the first message of a session, a fold's
seed included, has none. What the model holds is the chain from its session's tip back to
the start. A fold's ``fold_point`` (the parent's tip when it folded) links what came before
it, for what outlives a fold: the instructions and the values tools printed.

A rewind ("Volver a este punto", `conversation_rewound`) is one more event. It hides the
messages from the point on and moves the session's tip back to the point's parent. That can
be a message of an earlier session: a point from before a fold, or from another opening of
the project. Nothing is deleted: the hidden messages stay in the journal and the graph, and
a redo (`conversation_restored`) puts the tip back where it was. Claude Code's JSONL
transcripts have the same shape (every entry names its parent, so a branch is a tree, not a
rewrite), and so does Codex's revert: "Old rollouts stay intact. The new file references the
retained prefix" (codex-rs/thread-store/src/local/revert_thread.rs, read 2026-09-30).

Read-only. `loop.continuation` decides what the model is sent, from what this returns.
"""

from __future__ import annotations

import heapq
import logging
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cogito.config import paths
from cogito.graph.history import same_text
from cogito.graph.sessions import project_id_for
from cogito.graph.wal import Event, Journal

log = logging.getLogger(__name__)

#: What `compact` titles the child it seeds. A child whose `session_compacted` never reached
#: the journal is a fold the app died during, not a conversation.
_FOLD_TITLE = "continued"

#: The event kinds the replay reads. Every other line is left unparsed: a long session's
#: journal is mostly `model_context_recorded` and tool telemetry.
_KINDS = (
    "session_started",
    "message_appended",
    "tool_called",
    "session_compacted",
    "session_delegated",
    "turn_ended",
    "attempt_branch",
    "rewind_performed",
    "redo_performed",
    "conversation_rewound",
    "conversation_restored",
)
_MARKERS = tuple(f'"kind":"{kind}"'.encode() for kind in _KINDS)


@dataclass(frozen=True, slots=True)
class JournalMessage:
    """One ``message_appended``, as the journal holds it."""

    id: str
    session_id: str
    seq: int
    role: str
    text: str
    step: int
    tokens: int
    at: str
    #: The encoded blocks (`provider.base.encode_blocks`), or None for a message journalled
    #: before blocks were. Its ``text`` is then all there is.
    blocks: tuple[Mapping[str, Any], ...] | None = None
    blocks_version: int | None = None
    #: The size of the request an assistant message answered; None for any other message
    #: and for one journalled before the size was.
    input_tokens: int | None = None
    #: ``"instruction"`` or ``"steer"``: a person's own words (`Engine._append`'s ``point``).
    point: str | None = None


@dataclass(frozen=True, slots=True)
class JournalCall:
    """One ``tool_called``: the call's own id, the message that made it, its properties."""

    id: str
    message_id: str
    properties: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TurnEnd:
    """One ``turn_ended``: how the turn a message opened ended, and the message the
    conversation stood at when it did (None in a journal from before that was recorded)."""

    message_id: str
    stop_reason: str
    outcome: str
    last_message_id: str | None
    order: int
    #: "call" | "text" | "" (anything but `stop_reason == "output_limit"`, or a journal
    #: from before this was recorded). See `loop.engine.TurnResult.cut`.
    cut: str = ""


@dataclass
class RewindRecord:
    """One ``conversation_rewound``, and what became of it."""

    id: str
    session_id: str
    point_id: str
    hidden: tuple[str, ...]
    #: The session's tip before the rewind: what a redo puts back.
    tip_before: str | None
    #: The session's highest seq when it happened: a message after it has a higher one.
    after_seq: int
    at: str
    order: int
    restored: bool = False
    #: How many files the `rewind_performed` of the same id restored.
    files: int = 0
    files_redone: bool = False


class Replay:
    """A set of journals replayed in order, the way the loop wrote them."""

    def __init__(self) -> None:
        self.order = 0
        self.started: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, JournalMessage] = {}
        self.parent: dict[str, str | None] = {}
        self.tip: dict[str, str | None] = {}
        self.highest: dict[str, int] = {}
        self.last_append: dict[str, int] = {}
        self.by_seq: dict[tuple[str, int], str] = {}
        self.journal_of: dict[str, Path] = {}
        self.child: dict[str, str] = {}
        self.parent_session: dict[str, str] = {}
        self.copies: dict[str, int] = {}
        self.fold_point: dict[str, str | None] = {}
        self.delegated: set[str] = set()
        self.calls: dict[str, dict[str, JournalCall]] = {}
        self.turn_ends: dict[str, list[TurnEnd]] = {}
        self.discarded: set[tuple[str, int]] = set()
        self.hidden: dict[str, str] = {}
        self.rewinds: dict[str, RewindRecord] = {}
        #: `rewind_performed` file counts, for the `conversation_rewound` that follows it.
        self._files: dict[str, int] = {}
        self._files_redone: set[str] = set()
        #: `history(tip)` by tip, until the next event is applied. A fold's history contains
        #: its parent's, and a fold journalled before `copies` existed counts its copies
        #: against that history too. Uncached, reading a long conversation back doubled in
        #: cost with every fold.
        self._histories: dict[str | None, list[tuple[JournalMessage, bool]]] = {}

    # -- applying events ------------------------------------------------------------

    def apply(self, event: Event, journal: Path) -> None:
        self.order += 1
        self._histories.clear()
        data = event.data
        kind = event.kind
        if kind == "session_started":
            session_id = str(data.get("id"))
            self.started.setdefault(session_id, {**data, "at": event.at})
            self.journal_of.setdefault(session_id, journal)
        elif kind == "message_appended":
            self._append(data, event.at)
        elif kind == "tool_called":
            message_id = str(data.get("message_id"))
            call_id = str(data.get("id"))
            properties = data.get("properties")
            self.calls.setdefault(message_id, {})[call_id] = JournalCall(
                id=call_id,
                message_id=message_id,
                properties=dict(properties) if isinstance(properties, Mapping) else {},
            )
        elif kind == "session_compacted":
            parent, child = str(data.get("parent_id")), str(data.get("child_id"))
            self.child[parent] = child
            self.parent_session[child] = parent
            self.fold_point[child] = self.tip.get(parent)
            copies = data.get("copies")
            if isinstance(copies, int) and not isinstance(copies, bool) and copies >= 0:
                self.copies[child] = copies
        elif kind == "session_delegated":
            self.delegated.add(str(data.get("child_id")))
        elif kind == "turn_ended":
            message_id = str(data.get("message_id"))
            last = data.get("last_message_id")
            self.turn_ends.setdefault(message_id, []).append(
                TurnEnd(
                    message_id=message_id,
                    stop_reason=str(data.get("stop_reason") or ""),
                    outcome=str(data.get("outcome") or ""),
                    last_message_id=str(last) if last else None,
                    order=self.order,
                    cut=str(data.get("cut") or ""),
                )
            )
        elif kind == "attempt_branch" and data.get("status") == "discarded":
            self._discard(data)
        elif kind == "rewind_performed":
            self._files[str(data.get("id"))] = len(data.get("restored_paths") or ())
        elif kind == "redo_performed":
            rewind_id = str(data.get("rewind_id"))
            self._files_redone.add(rewind_id)
            if rewind_id in self.rewinds:
                self.rewinds[rewind_id].files_redone = True
        elif kind == "conversation_rewound":
            self._rewind(data, event.at)
        elif kind == "conversation_restored":
            record = self.rewinds.get(str(data.get("id")))
            if record is not None and not record.restored:
                record.restored = True
                for message_id in record.hidden:
                    if self.hidden.get(message_id) == record.id:
                        del self.hidden[message_id]
                self.tip[record.session_id] = record.tip_before

    def _append(self, data: Mapping[str, Any], at: str) -> None:
        message_id = str(data.get("id"))
        session_id = str(data.get("session_id"))
        known = self.messages.get(message_id)
        message = _message(data, known.at if known is not None else at)
        self.messages[message_id] = message
        if known is not None:
            return  # a second write of one message: the projection's MERGE, not a new one
        self.parent[message_id] = self.tip.get(session_id)
        self.tip[session_id] = message_id
        self.last_append[session_id] = self.order
        self.by_seq[(session_id, message.seq)] = message_id
        self.highest[session_id] = max(self.highest.get(session_id, -1), message.seq)

    def _discard(self, data: Mapping[str, Any]) -> None:
        """A set-aside attempt never re-enters the conversation. If the session's tip is
        inside it, the tip goes back to before its first message."""
        session_id = str(data.get("session_id"))
        first, last = _int(data.get("first_seq")), _int(data.get("last_seq"), -1)
        self.discarded.update((session_id, seq) for seq in range(first, last + 1))
        opening = self.by_seq.get((session_id, first))
        tip = self.tip.get(session_id)
        if opening is None or tip is None or tip not in self.messages:
            return
        if first <= self.messages[tip].seq <= last:
            self.tip[session_id] = self.parent.get(opening)

    def _rewind(self, data: Mapping[str, Any], at: str) -> None:
        rewind_id = str(data.get("id"))
        session_id = str(data.get("session_id"))
        point_id = str(data.get("point_id"))
        hidden = tuple(str(h) for h in data.get("hidden") or ())
        after_seq = data.get("after_seq")
        record = RewindRecord(
            id=rewind_id,
            session_id=session_id,
            point_id=point_id,
            hidden=hidden,
            tip_before=self.tip.get(session_id),
            after_seq=(
                after_seq
                if isinstance(after_seq, int) and not isinstance(after_seq, bool)
                else self.highest.get(session_id, -1)
            ),
            at=at,
            order=self.order,
            files=self._files.get(rewind_id, 0),
            files_redone=rewind_id in self._files_redone,
        )
        self.rewinds[rewind_id] = record
        for message_id in hidden:
            self.hidden[message_id] = rewind_id
        self.tip[session_id] = self.parent.get(point_id)

    # -- reading the conversation ---------------------------------------------------

    def chain(self, tip: str | None) -> list[JournalMessage]:
        """From the start of the chain up to ``tip``, oldest first."""
        found: list[JournalMessage] = []
        seen: set[str] = set()
        current = tip
        while current is not None and current not in seen and current in self.messages:
            seen.add(current)
            found.append(self.messages[current])
            current = self.parent.get(current)
        found.reverse()
        return found

    def is_seed(self, message: JournalMessage) -> bool:
        return message.seq == 0 and message.session_id in self.parent_session

    def copies_after(self, seed: JournalMessage, chain: Sequence[JournalMessage]) -> int:
        """How many messages after a fold's seed, on ``chain`` (which starts at it), are the
        parent's own, copied."""
        child = seed.session_id
        if child in self.copies:
            return self.copies[child]
        before = [m for m, _ in self.history(self.fold_point.get(child))]
        return _copied(before, chain[1:])

    def history(self, tip: str | None) -> list[tuple[JournalMessage, bool]]:
        """Everything the conversation that ends at ``tip`` has been through, oldest first,
        across its folds: each message with whether it is a fold's seed or copy (the
        harness's own restatement, not something anyone said)."""
        if tip in self._histories:
            return self._histories[tip]
        chain = self.chain(tip)
        if not chain or not self.is_seed(chain[0]):
            found = [(message, False) for message in chain]
        else:
            before = self.history(self.fold_point.get(chain[0].session_id))
            copies = self.copies_after(chain[0], chain)
            restated = [(message, True) for message in chain[: 1 + copies]]
            found = [*before, *restated, *[(message, False) for message in chain[1 + copies :]]]
        self._histories[tip] = found
        return found

    def leaf(self, session_id: str) -> str:
        seen = {session_id}
        while session_id in self.child and self.child[session_id] not in seen:
            session_id = self.child[session_id]
            seen.add(session_id)
        return session_id

    def lineage(self, session_id: str) -> list[str]:
        lineage = [session_id]
        while lineage[0] in self.parent_session and self.parent_session[lineage[0]] not in lineage:
            lineage.insert(0, self.parent_session[lineage[0]])
        return lineage

    def eligible(self, session_id: str, project_id: str) -> bool:
        started = self.started.get(session_id)
        if started is None or started.get("project_id") != project_id:
            # Started elsewhere, or a journal copied with a project to another path. The
            # graph files it under the other project, so continuing it here would split it.
            return False
        if session_id in self.delegated:
            return False
        return not (started.get("title") == _FOLD_TITLE and session_id not in self.parent_session)

    def latest(self, project_id: str) -> JournalMessage | None:
        best: JournalMessage | None = None
        for message in self.messages.values():
            if (message.session_id, message.seq) in self.discarded:
                continue
            if not self.eligible(message.session_id, project_id):
                continue
            if best is None or _when(message.at) > _when(best.at):
                best = message
        return best

    def thread(self, project_id: str) -> list[str]:
        """The project's sessions in the order its thread shows them (`graph.history`'s
        `thread_order`): every root oldest first, each followed by the sessions it was
        folded into; a subagent's session is not part of it."""
        members = {
            session_id
            for session_id, started in self.started.items()
            if started.get("project_id") == project_id and session_id not in self.delegated
        }
        roots = sorted(
            (s for s in members if self.parent_session.get(s) not in members),
            key=lambda s: (str(self.started[s].get("at") or ""), s),
        )
        order: list[str] = []
        seen: set[str] = set()
        for root in roots:
            current: str | None = root
            while current is not None and current in members and current not in seen:
                seen.add(current)
                order.append(current)
                current = self.child.get(current)
        return order

    def thread_after(self, point_id: str, project_id: str) -> list[str]:
        """Every message the thread shows from ``point_id`` on, in the thread's order:
        the rest of the point's session, then every session after it."""
        point = self.messages[point_id]
        by_session: dict[str, list[JournalMessage]] = {}
        for message in self.messages.values():
            if (message.session_id, message.seq) not in self.discarded:
                by_session.setdefault(message.session_id, []).append(message)
        after: list[str] = []
        reached = False
        for session_id in self.thread(project_id):
            listed = sorted(by_session.get(session_id, ()), key=lambda m: m.seq)
            if session_id == point.session_id:
                reached = True
                after.extend(m.id for m in listed if m.seq >= point.seq)
            elif reached:
                after.extend(m.id for m in listed)
        return after

    def missing(self) -> bool:
        """Whether a rewind points at a message none of the journals read holds."""
        return any(record.point_id not in self.messages for record in self.rewinds.values())

    def redo_available(self, rewind_id: str) -> bool:
        """Whether «Rehacer» can still bring a rewound conversation back: it has not been
        redone, and nothing happened in its session since (a message, because a new turn
        makes the rewound branch final, or another rewind). A redo of its files alone does
        not count: the app may have closed between the two halves, and the conversation's
        half is still owed."""
        record = self.rewinds.get(rewind_id)
        if record is None or record.restored:
            return False
        if self.last_append.get(record.session_id, 0) > record.order:
            return False
        return not any(
            other.session_id == record.session_id and other.order > record.order
            for other in self.rewinds.values()
        )


def _replay_files(files: Sequence[Path]) -> Replay:
    """The journals' events merged by time, each journal's own order kept as written."""
    replayed = Replay()

    def events(path: Path) -> Iterator[tuple[datetime, int, Event, Path]]:
        for index, event in Journal(path).read(only=_MARKERS):
            yield _when(event.at), index, event, path

    for _, _, event, path in heapq.merge(*(events(p) for p in files), key=lambda e: e[:2]):
        replayed.apply(event, path)
    return replayed


@dataclass(frozen=True, slots=True)
class Continuation:
    """A project's conversation as the journals left it."""

    #: The journal that holds the conversation, and the one the next opening appends to.
    journal: Path
    project_id: str
    #: The session the conversation continues in.
    session_id: str
    #: Its compaction lineage, oldest first; the session is last.
    lineage: tuple[str, ...]
    #: What the model holds: the chain from its start (a fold's seed, when it has one) to
    #: the session's tip. Rewound messages and set-aside attempts are not on it.
    transcript: tuple[JournalMessage, ...]
    #: Everything the conversation has been through, oldest first, across its folds, each
    #: with whether it is a fold's seed or copy.
    history: tuple[tuple[JournalMessage, bool], ...]
    #: When the transcript starts at a fold's seed, how many copies follow it; else None.
    copies: int | None
    #: The session's next free seq: one past the highest it ever wrote.
    next_seq: int
    #: Each message's tool calls, in the order they were recorded.
    calls: Mapping[str, tuple[JournalCall, ...]] = field(default_factory=dict)
    #: How the turns ended, by the id of the message that opened each.
    turn_ends: Mapping[str, tuple[TurnEnd, ...]] = field(default_factory=dict)
    #: Copies a fold never wrote because the app died during it: parent messages the
    #: transcript should hold after its seed and does not. Written when it is taken up.
    missing_copies: tuple[JournalMessage, ...] = ()


def continuation_of(replayed: Replay, session_id: str, project_id: str) -> Continuation:
    tip = replayed.tip.get(session_id)
    transcript = replayed.chain(tip)
    history = replayed.history(tip)
    copies = (
        replayed.copies_after(transcript[0], transcript)
        if transcript and replayed.is_seed(transcript[0])
        else None
    )
    missing: tuple[JournalMessage, ...] = ()
    if copies is not None and len(transcript) - 1 < copies:
        # The fold wrote its seed and the edge, and the app closed before all the copies.
        # The rest are the parent's last messages at the fold, exactly what it would have
        # copied. Nothing can follow copies the fold did not finish, so only the tail is
        # missing.
        kept = replayed.chain(replayed.fold_point.get(transcript[0].session_id))[-copies:]
        missing = tuple(kept[len(transcript) - 1 :])
    wanted = {message.id for message, _ in history} | {message.id for message in missing}
    journal = replayed.journal_of.get(session_id)
    if journal is None:
        journal = next(iter(replayed.journal_of.values()))
    return Continuation(
        journal=journal,
        project_id=project_id,
        session_id=session_id,
        lineage=tuple(replayed.lineage(session_id)),
        transcript=tuple(transcript),
        history=tuple(history),
        copies=copies,
        next_seq=replayed.highest.get(session_id, -1) + 1,
        calls={
            message_id: tuple(calls.values())
            for message_id, calls in replayed.calls.items()
            if message_id in wanted
        },
        turn_ends={k: tuple(v) for k, v in replayed.turn_ends.items() if k in wanted},
        missing_copies=missing,
    )


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _message(data: Mapping[str, Any], at: str) -> JournalMessage:
    blocks = data.get("blocks")
    encoded: tuple[Mapping[str, Any], ...] | None = None
    if isinstance(blocks, list) and all(isinstance(b, Mapping) for b in blocks):
        encoded = tuple(blocks)
    version = data.get("blocks_version")
    input_tokens = data.get("input_tokens")
    point = data.get("point")
    return JournalMessage(
        id=str(data.get("id")),
        session_id=str(data.get("session_id")),
        seq=_int(data.get("seq")),
        role=str(data.get("role") or ""),
        text=str(data.get("text") or ""),
        step=_int(data.get("step")),
        tokens=_int(data.get("tokens")),
        at=at,
        blocks=encoded,
        blocks_version=(
            version if isinstance(version, int) and not isinstance(version, bool) else None
        ),
        input_tokens=(
            input_tokens
            if isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
            else None
        ),
        point=point if point in ("instruction", "steer") else None,
    )


def _when(stamp: str) -> datetime:
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _copied(parent: Sequence[JournalMessage], after_seed: Sequence[JournalMessage]) -> int:
    """How many messages after a fold's seed copy its parent's last ones, for a fold
    journalled before `session_compacted` recorded the count. Same rule as
    `graph.history.copied_count`: every copy is written with ``step = 0`` and ``tokens = 0``
    right after the seed, and the copies are the longest prefix of that run that equals the
    parent's tail, a copy offloaded since counting as equal (`graph.history.same_text`)."""
    run: list[JournalMessage] = []
    for message in after_seed:
        if message.step != 0 or message.tokens != 0:
            break
        run.append(message)
    for k in range(min(len(run), len(parent)), 0, -1):
        if all(
            a.role == b.role and same_text(a.text, b.text)
            for a, b in zip(run[:k], parent[-k:], strict=True)
        ):
            return k
    return 0


def journals(project_root: Path) -> list[tuple[float, Path]]:
    """The project's journals, newest first by modification time."""
    directory = paths.project_dir(project_root) / "wal"
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.jsonl"):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def _read_journal(path: Path) -> Replay:
    """One journal, replayed on its own."""
    return _replay_files([path])


def replay(
    project_root: Path, journal: Path, *, everything: bool = False, wanted: str | None = None
) -> Replay:
    """``journal`` replayed, together with every other journal of the project when one of
    its rewinds points into an opening it does not hold, when it does not hold the message
    ``wanted`` (a point from another opening, shown by the history), or when ``everything``
    asks."""
    if not everything:
        replayed = _read_journal(journal)
        if not replayed.missing() and (wanted is None or wanted in replayed.messages):
            return replayed
    return _replay_files([path for _, path in journals(project_root)] or [journal])


def load(project_root: Path, journal: Path, session_id: str) -> Continuation:
    """The conversation ``session_id`` is in, from the journal that holds it. An engine
    already holding it rebuilds itself from this after a rewind or a redo."""
    return continuation_of(replay(project_root, journal), session_id, project_id_for(project_root))


def find(project_root: Path, *, held: Collection[str] = ()) -> Continuation | None:
    """The project's conversation, or None when there is none to continue.

    ``held`` are sessions another live engine of this process is still writing to: two
    engines appending to one session would interleave their messages under the same seqs,
    so the caller starts a new one instead.
    """
    project_id = project_id_for(project_root)
    best: tuple[JournalMessage, Path, Replay] | None = None
    for modified, path in journals(project_root):
        if best is not None and _when(best[0].at) >= datetime.fromtimestamp(modified, UTC):
            break  # nothing in this file, or any older one, can be newer
        try:
            read = _read_journal(path)
        except OSError as exc:
            log.warning("resume: could not read %s: %s", path, exc)
            continue
        latest = read.latest(project_id)
        if latest is not None and (best is None or _when(latest.at) > _when(best[0].at)):
            best = (latest, path, read)
    if best is None:
        return None
    message, path, replayed = best
    if replayed.missing():
        replayed = replay(project_root, path, everything=True)
    leaf = replayed.leaf(message.session_id)
    if leaf in held:
        log.info("resume: session %s is held by another live engine; starting a new one", leaf)
        return None
    return continuation_of(replayed, leaf, project_id)
