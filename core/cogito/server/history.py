"""``GET /sessions/{id}/history``: the project's thread, as the window draws it.

`read_page` reads the store backwards from a cursor, one bounded query at a time
(`graph.history`'s sources), until it holds whole turns. `thread_items` turns them into
items the desktop draws with the same row renderers it uses for live events. So a tool row
here carries the fields its live ``tool_start``/``tool_end`` pair did, and a turn's changes
carry the fields ``turn_changes`` did.

## Items, oldest first

Every item has ``at`` (ISO time of the fact it shows)::

    {"type":"user","id","point_id","text","at"}     # an instruction; a rewind point
    {"type":"steer","id","point_id","text","at"}    # sent mid-turn; a rewind point
    {"type":"assistant","id","text","final","at","continued"?}  # its own words, calls
                                                     # stripped; "continued" (APP-ONLY
                                                     # auto-continue) when it joins the
                                                     # paragraph before it, no call between
    {"type":"tool","call_id","name","ok","status":"done|failed|cancelled","target",
     "summary","duration_ms"?,"error"?,"exit_code"?,"added"?,"removed"?,"lines"?,
     "detail"?,"at"}
    {"type":"turn_changes","total_files","files":[{"path","status":"added|modified",
     "added"?,"removed"?,"diff"?,"truncated"?}],"shell_changes_unknown"?,"at"}
    {"type":"turn_stopped","reason","failure"?,"cut"?,"at"}  # a turn that did not finish
    {"type":"compaction","at"}                      # a fold: a marker, not its text
    {"type":"budget_decision","decision":"continue|raise|stop","reason":"user|timeout|
     interrupted","period":"month|day","limit","spent","currency","at"}
    {"type":"rewind","id","point_id","point_at","files","redo","at"}

A tool item's ``detail`` is what its row opens onto (the edit's diff, the command's
output), exactly as its live ``tool_end`` carried it (`loop.detail`, journalled with the
call). A call recorded before ``detail`` existed, or by a session no window watched, has
none, and its row does not open. ``lines`` is a read's file length («64 líneas»), as live.

``final`` marks the assistant message that ended its turn (no calls, last of the turn),
the one a live ``turn_complete`` shows. ``point_id`` is the message id that "↺ Volver a
este punto" rewinds to.

``budget_decision`` is a decision on COGITO's own budget at a pause, placed where it was
taken («Continuaste esta tarea», «Subiste tu presupuesto», «Detuviste la tarea»). A raise
carries the limit as saved.

``rewind`` marks where the conversation was taken back to an earlier point («↺ Volviste a
las HH:MM · N archivos»). While it stands, the messages it hid (the point and everything
after it) are not items. They stay in the store, and a fold the rewind went back past is
hidden with them. ``point_at`` is the point's time, ``id`` the redo id, and ``redo``
whether «Rehacer» still applies: only to the latest rewind of its session, and only while
nothing was said after it. A redone rewind is not an item, and what it hid is back.

``turn_changes`` closes each turn whose edit/write calls changed a file or that ran a
shell command (the same rule as the live event), with each file's path and whether it was
created. The before/after text is never stored, so a file's counts and diff are here only
where they are exact: a file one call touched keeps that call's live ``added``/``removed``
(``lines_added``/``lines_removed``) and its diff (the call's own, `loop.detail`), and a
created file keeps its line count (``after_lines``) net of every later edit's counts.

``turn_stopped`` follows a turn that did not finish (``turn_ended``'s ``stop_reason``
other than ``end_turn``: ``user_interrupt``, ``provider_failure``, ``max_steps``...).
``failure`` is the provider failure's category when one stopped it. The failure's own
words are not stored. ``cut`` is ``"call"`` or ``"text"`` for an ``output_limit`` stop
(`loop.engine.TurnResult.cut`), left out for every other reason.

A ``tool`` item carries ``matches``/``files`` for a search, as live, and ``denied`` for a
call the guard or the person did not let run (including a command whose deletion a PATH shim
refused, which the shell itself reports only as a non-zero exit).

An ``approval`` item comes right before the ``tool`` item whose call asked it::

    {"type":"approval","call_id","allowed","reason":"user|timeout|interrupted",
     "request":{"action","command","summary","packages"?,"manager"?,"source"?,
                "server"?,"tool"?,"tool_title"?},"at"}

It is the pill the card left («Permitiste · rm notas.txt»), from what the call recorded
(`guard.approval.Ledger`). ``allowed`` is false when nobody answered, as the pill says by
``reason``. A call recorded before answers were kept has none.

A step's tool results, a repair prompt and the build gate's findings are the harness
talking to the model, and are not items.

## Pages

``?limit=N`` messages (default 60, at most 400) and ``?before=<session id>:<seq>`` (default:
the end). A page never splits a turn. It starts at a turn's first message, so it may hold
more than ``limit`` when one turn is long. The answer is ``{"items","before","source"}``.
``before`` is the cursor for the next older page (``null`` at the beginning of the
thread). ``source`` is ``"graph"``, or ``"wal"`` when the journals were read because the
graph could not be reached or is behind them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cogito.graph.history import (
    BudgetMarker,
    Handover,
    RewindMarker,
    Source,
    StoredCall,
    StoredMessage,
    StoredSession,
    copied_count,
    thread_order,
)
from cogito.loop import detail as tool_detail
from cogito.loop import readback
from cogito.loop.changes import OUTSIDE_PROJECT
from cogito.loop.engine import SWITCHED_OFF, is_refusal, search_counts

DEFAULT_LIMIT = 60
MAX_LIMIT = 400
#: A tool row's result summary: the first line of what the tool said, like a live error.
_SUMMARY_CHARS = 200


def _status(call: StoredCall) -> str:
    if call.ok:
        return "done"
    # "interrupted": the app closed while it ran (`continuation.CUT_OFF`), drawn as a call
    # that was cut off, not as one that failed with the harness's note for the model.
    if call.properties.get("error_kind") in ("cancelled", "interrupted"):
        return "cancelled"
    return "failed"


def _summary(call: StoredCall, result: str, project_root: Path) -> str:
    """The first line of what the tool said, with the project's absolute prefix removed.
    A live event never carries the host's directory layout, and neither does this."""
    line = readback.tool_error(call.name, result, project_root)
    return line.replace(f"{project_root}/", "").replace(str(project_root), ".")[:_SUMMARY_CHARS]


def _tool_item(call: StoredCall, at: str, project_root: Path) -> dict[str, Any]:
    arguments = readback.call_arguments(call.properties.get("arguments"))
    result = str(call.properties.get("result") or "")
    item: dict[str, Any] = {
        "type": "tool",
        "call_id": call.id,
        "name": call.name,
        "ok": call.ok,
        "status": _status(call),
        "target": readback.tool_target(arguments, project_root),
        "summary": _summary(call, result, project_root),
        "at": at,
    }
    if not call.ok:
        item["error"] = readback.tool_error(call.name, result, project_root)
    # The loop's timing of the call (`Engine._execute`), stored on every call. Same number
    # the live `tool_end` carried.
    duration = call.properties.get("duration_ms")
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        item["duration_ms"] = int(duration)
    exit_code = call.properties.get("exit_code")
    if call.name == "shell" and isinstance(exit_code, int) and not isinstance(exit_code, bool):
        item["exit_code"] = exit_code
    if call.name == "shell" and call.properties.get("background_pid"):
        item["background"] = str(call.properties.get("ports") or "")
    created = _created_lines(call)
    if created is not None:
        # A file `write` created: every line is new, as the live row showed.
        item["added"], item["removed"] = created, 0
    counted = _counted_lines(call)
    if counted is not None:
        # The live +N/−M, kept with the call when a window was watching.
        item["added"], item["removed"] = counted
    lines = call.properties.get("lines")
    if call.name == "read" and call.ok and isinstance(lines, int) and not isinstance(lines, bool):
        item["lines"] = lines
    matches, files = search_counts(call.name, call.properties, ok=call.ok)
    if matches is not None:
        item["matches"] = matches
    if files is not None:
        item["files"] = files
    calls = call.properties.get("calls")
    if call.name == "delegate" and isinstance(calls, int) and not isinstance(calls, bool):
        # A subagent's row counts its calls («N llamadas»), as it did live.
        item["calls"] = calls
    if not call.ok and (
        is_refusal(call.name, str(call.properties.get("error_kind") or ""))
        or call.properties.get("guard_refused") is True
    ):
        item["denied"] = True
    if not call.ok and call.properties.get("error_kind") == SWITCHED_OFF:
        item["off"] = True
    # What the row opens onto, as the live `tool_end` carried it; none for an older record.
    detail = tool_detail.from_properties(call.properties)
    if detail is not None:
        item["detail"] = detail
    return item


#: The fields of a recorded question that the window words its pill from
#: (`guard.approval.Ledger.note_answer`).
_REQUEST_FIELDS = (
    "action", "command", "summary", "packages", "manager", "source", "server", "tool", "tool_title"
)


def _approval_items(call: StoredCall, at: str) -> list[dict[str, Any]]:
    """The questions this call put to a person, each as the pill its card left live, in the
    order they were asked. A refusal nobody was asked about drew no card, so it has none.
    A call recorded before answers were kept has none."""
    raw = call.properties.get("answers")
    if not isinstance(raw, str):
        return []
    try:
        answers = json.loads(raw)
    except ValueError:
        return []
    items: list[dict[str, Any]] = []
    for answer in answers if isinstance(answers, list) else []:
        if not isinstance(answer, dict) or answer.get("asked") is not True:
            continue
        items.append(
            {
                "type": "approval",
                "call_id": call.id,
                "allowed": answer.get("allowed") is True,
                "reason": str(answer.get("reason") or "user"),
                "request": {key: answer[key] for key in _REQUEST_FIELDS if key in answer},
                "at": at,
            }
        )
    return items


def _counted_lines(call: StoredCall) -> tuple[int, int] | None:
    """An edit's or write's live +N/−M, as `loop.detail.to_properties` kept it. None for a
    call recorded with no window watching, or before these counts were kept."""
    if call.name not in ("edit", "write") or not call.ok:
        return None
    added = call.properties.get("lines_added")
    removed = call.properties.get("lines_removed")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (added, removed)):
        return None
    return added, removed


def _created_lines(call: StoredCall) -> int | None:
    """The line count of a file a successful `write` created, from the `after_lines` the
    loop stores instead of the text. None for anything else. A modification's live +N/−M
    came from diffing text that is never stored, so it cannot be rebuilt."""
    if call.name != "write" or not call.ok or call.properties.get("before_hash") is not None:
        return None
    lines = call.properties.get("after_lines")
    return lines if isinstance(lines, int) and not isinstance(lines, bool) else None


class _Turn:
    """What one turn changed, gathered while its items go by."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        #: How many calls touched each path, and the lines of the one that created it.
        self.touches: dict[str, int] = {}
        self.created: dict[str, int] = {}
        #: Each touching call's live +N/−M, or None where a call kept none.
        self.counts: dict[str, list[tuple[int, int] | None]] = {}
        #: The one call's own diff, kept with it (`loop.detail`).
        self.diffs: dict[str, dict[str, Any]] = {}
        self.shell_ran = False
        self.last_at = ""
        #: How the turn ended (`turn_ended`), read from the message that opened it.
        self.stop_reason = ""
        self.failure = ""
        #: "call" | "text", set only alongside `stop_reason == "output_limit"`.
        self.cut = ""
        self.opened_at = ""
        #: The words of the turn's last reply that made a call (`thread_items`).
        self.call_prose = ""

    def note(self, call: StoredCall, project_root: Path) -> None:
        if call.name == "shell":
            self.shell_ran = True
        if call.name not in ("edit", "write") or not call.ok:
            return
        arguments = readback.call_arguments(call.properties.get("arguments"))
        paths = readback.tool_target(arguments, project_root).get("paths") or []
        for path in paths:
            shown = path
            if path == OUTSIDE_PROJECT:
                raw = str(arguments.get("path") or "")
                shown = f"{OUTSIDE_PROJECT}/{Path(raw).name}" if raw else OUTSIDE_PROJECT
            created = call.properties.get("before_hash") is None and call.name == "write"
            # The first call on a path decides: a write that created it followed by edits
            # still made a new file this turn.
            self.files.setdefault(shown, "added" if created else "modified")
            self.touches[shown] = self.touches.get(shown, 0) + 1
            if (lines := _created_lines(call)) is not None:
                self.created[shown] = lines
            self.counts.setdefault(shown, []).append(_counted_lines(call))
            detail = tool_detail.from_properties(call.properties)
            if detail is not None and detail["kind"] == "diff":
                self.diffs[shown] = detail

    def item(self) -> dict[str, Any] | None:
        if not self.files and not self.shell_ran:
            return None
        files = []
        for path, status in sorted(self.files.items()):
            entry: dict[str, Any] = {"path": path, "status": status}
            counted = self.counts.get(path, [])
            known = all(pair is not None for pair in counted)
            # Counts only where they are exact, as the live card had them: one call on the
            # path, or a created file (its lines, net of every later edit's).
            if self.touches.get(path) == 1 and known and counted:
                entry["added"], entry["removed"] = counted[0]
            elif status == "added" and path in self.created:
                rest = [pair for pair in counted[1:] if pair is not None]
                if len(rest) == len(counted) - 1:
                    lines = self.created[path] + sum(a - r for a, r in rest)
                    entry["added"], entry["removed"] = lines, 0
            # One call's own diff is the turn's diff for that file.
            if self.touches.get(path) == 1 and path in self.diffs:
                detail = self.diffs[path]
                entry["diff"] = detail["text"]
                if detail["truncated"]:
                    entry["truncated"] = True
            files.append(entry)
        item: dict[str, Any] = {
            "type": "turn_changes",
            "total_files": len(files),
            "files": files,
            "at": self.last_at,
        }
        if self.shell_ran:
            item["shell_changes_unknown"] = True
        return item


def _follows_calls(older: StoredMessage | Handover | None) -> bool:
    """Whether a message right after ``older`` can be a step's tool results: it follows
    the assistant message that made the calls, or opens a session (after a fold)."""
    if older is None or isinstance(older, Handover):
        return True
    return older.role == "assistant" and (bool(older.calls) or readback.made_calls(older.text))


def _said(kind: str, message: StoredMessage, project_root: Path) -> dict[str, Any]:
    """A person's message as the thread draws it: their words and, only when they attached
    something (design 1n), what each attachment was. Never the attachment's content."""
    words, sent = readback.person_message(message.text, project_root)
    item: dict[str, Any] = {
        "type": kind,
        "id": message.id,
        "point_id": message.id,
        "text": words,
        "at": message.at,
    }
    if sent:
        item["attachments"] = [attachment.facts(project_root) for attachment in sent]
    return item


def _is_instruction(
    message: StoredMessage | Handover, older: StoredMessage | Handover | None
) -> bool:
    """A person's own instruction: where a turn, and so a page, starts."""
    if not isinstance(message, StoredMessage) or message.role != "user":
        return False
    if readback.steer_text(message.text) is not None or readback.is_harness_notice(message.text):
        return False
    return not (readback.is_tool_results(message.text) and _follows_calls(older))


def _placed(
    entries: Sequence[StoredMessage | Handover],
    markers: Sequence[BudgetMarker | RewindMarker],
) -> list[StoredMessage | Handover | BudgetMarker | RewindMarker]:
    """``entries`` with each marker (budget decision or rewind) right after the message it
    followed. One that followed a message a fold copied (so not shown) goes right after
    that fold's marker."""
    after: dict[tuple[str, int], list[BudgetMarker | RewindMarker]] = {}
    for marker in markers:
        after.setdefault((marker.session_id, marker.after_seq), []).append(marker)
    shown = {(e.session_id, e.seq) for e in entries if isinstance(e, StoredMessage)}
    early: dict[str, list[BudgetMarker | RewindMarker]] = {}
    for (session_id, seq), listed in after.items():
        if (session_id, seq) not in shown:
            early.setdefault(session_id, []).extend(listed)
    placed: list[StoredMessage | Handover | BudgetMarker | RewindMarker] = []
    for entry in entries:
        placed.append(entry)
        if isinstance(entry, StoredMessage):
            placed.extend(after.get((entry.session_id, entry.seq), ()))
        else:
            placed.extend(sorted(early.get(entry.session_id, ()), key=lambda m: m.after_seq))
    return placed


def _budget_item(marker: BudgetMarker) -> dict[str, Any]:
    return {
        "type": "budget_decision",
        "decision": marker.decision,
        "reason": marker.reason,
        "period": marker.period,
        "limit": marker.limit,
        "spent": marker.spent,
        "currency": marker.currency,
        "at": marker.at,
    }


def _rewind_item(marker: RewindMarker) -> dict[str, Any]:
    return {
        "type": "rewind",
        "id": marker.id,
        "point_id": marker.point_id,
        "point_at": marker.point_at,
        "files": marker.files,
        "redo": marker.redo,
        "at": marker.at,
    }


def thread_items(
    entries: Sequence[StoredMessage | Handover],
    project_root: Path,
    markers: Sequence[BudgetMarker | RewindMarker] = (),
) -> list[dict[str, Any]]:
    """The items of whole turns, oldest first (``entries`` starts at a turn's first
    message, or at the thread's beginning), with each budget decision and rewind in
    ``markers`` where it happened. A message a rewind hid, and a fold whose seed it hid,
    are not items. Markers are placed before those are dropped, so a rewind lands where
    the messages it hid were."""
    items: list[dict[str, Any]] = []
    turn: _Turn | None = None
    previous: StoredMessage | Handover | None = None

    def close_turn() -> None:
        """The turn's changes go after its last item (where the live card lands, after the
        final text) and before any fold between it and the next turn."""
        nonlocal turn
        if turn is not None:
            position = len(items)
            while position > 0 and items[position - 1]["type"] == "compaction":
                position -= 1
            closing = [summary] if (summary := turn.item()) is not None else []
            if turn.stop_reason and turn.stop_reason != "end_turn":
                # It did not finish: stopped, interrupted or failed, as the live thread said.
                stopped: dict[str, Any] = {
                    "type": "turn_stopped",
                    "reason": turn.stop_reason,
                    "at": turn.last_at or turn.opened_at,
                }
                if turn.failure:
                    stopped["failure"] = turn.failure
                if turn.cut:
                    stopped["cut"] = turn.cut
                closing.append(stopped)
            items[position:position] = closing
        turn = None

    for entry in _placed(entries, markers) if markers else entries:
        if isinstance(entry, StoredMessage | Handover) and entry.rewound:
            continue
        if isinstance(entry, RewindMarker):
            # The conversation went back here, so the turn it cut ends here.
            close_turn()
            items.append(_rewind_item(entry))
            continue
        if isinstance(entry, BudgetMarker):
            # Harness control, not conversation: it does not change which message came
            # before the next one.
            items.append(_budget_item(entry))
            continue
        if isinstance(entry, Handover):
            items.append({"type": "compaction", "at": entry.at})
            previous = entry
            continue
        message = entry
        if message.role == "user":
            steer = readback.steer_text(message.text)
            if steer is not None:
                items.append(_said("steer", message, project_root))
            elif _is_instruction(message, previous):
                close_turn()
                turn = _Turn()
                turn.stop_reason, turn.failure = message.stop_reason, message.failure
                turn.cut = message.cut
                turn.opened_at = message.at
                items.append(_said("user", message, project_root))
            elif (
                readback.is_status_echo_notice(message.text)
                and items
                and items[-1]["type"] == "assistant"
                and isinstance(previous, StoredMessage)
                and items[-1]["id"] == previous.id
            ):
                # That reply only repeated the status line, and the live thread took it
                # back when the harness sent it to the model again (QA rig, 2026-10-16).
                items.pop()
            # Otherwise tool results or a harness notice: the harness talking to the model.
        elif message.role == "assistant":
            if turn is None:
                turn = _Turn()
            # `keep_leading`: a continuation's own leading whitespace is part of the join
            # (`readback.assistant_prose`'s docstring); stripping it could mash two words.
            prose = readback.prose_without_calls(
                readback.assistant_prose(message.text, keep_leading=message.continued),
                [call.name for call in message.calls],
            )
            made_calls = bool(message.calls) or readback.made_calls(message.text)
            # Words repeated from the turn's previous reply, before another call, are not
            # drawn again, as the live thread does (`loop.engine.Engine.run`).
            repeated = made_calls and bool(prose) and prose.strip() == turn.call_prose
            turn.call_prose = prose.strip() if made_calls else ""
            if prose and not repeated:
                item: dict[str, Any] = {
                    "type": "assistant",
                    "id": message.id,
                    "text": prose,
                    "final": not message.calls and not readback.made_calls(message.text),
                    "at": message.at,
                }
                # APP-ONLY auto-continue (`BudgetSettings.max_auto_continuations`): this
                # reply followed a hidden auto-continue notice with no call in between
                # (`loop.engine.Engine._append`'s `continued`). The window joins it onto
                # the paragraph before it instead of drawing a new one
                # (`desktop/src/renderer/history.js`), exactly as the live thread did.
                if message.continued:
                    item["continued"] = True
                items.append(item)
            for call in message.calls:
                # Before the row, as live: the card (now its pill) comes first, and the
                # call it gated below it.
                items.extend(_approval_items(call, message.at))
                items.append(_tool_item(call, message.at, project_root))
                turn.note(call, project_root)
            turn.last_at = message.at
        previous = message
    close_turn()

    # `final` is the turn's last word. A call-free message the turn went on past (a repair
    # round, a steer that arrived just as it answered) is not what it ended on.
    last_final: int | None = None
    for position, item in enumerate(items):
        kind = item["type"]
        if kind == "user":
            last_final = None
        elif kind in ("assistant", "tool", "steer"):
            if last_final is not None:
                items[last_final]["final"] = False
                last_final = None
            if kind == "assistant" and item["final"]:
                last_final = position
    return items


# -- pages --------------------------------------------------------------------------------

#: ``<session id>:<seq>``: the first message of the page just read, which the next older
#: page ends before.
CURSOR = re.compile(r"^(?P<session>[A-Za-z0-9_-]{1,64}):(?P<seq>\d{1,12})$")


@dataclass(frozen=True, slots=True)
class Page:
    """Whole turns, oldest first. ``before`` is the cursor of the next older page, or
    None at the beginning of the thread."""

    entries: tuple[StoredMessage | Handover, ...]
    before: str | None
    source: str
    #: The budget decisions and rewinds within these turns (`thread_items`' ``markers``).
    markers: tuple[BudgetMarker | RewindMarker, ...] = ()


def _cut(collected: Sequence[StoredMessage | Handover], limit: int, exhausted: bool) -> int | None:
    """Where the page starts in ``collected`` (newest first). That is the first
    instruction at or past ``limit`` messages, or the last entry when the thread's
    beginning is reached with none. None when more must be read to tell, because the entry
    older than a candidate decides whether it is tool results or a person's words."""
    seen = 0
    for position, entry in enumerate(collected):
        if isinstance(entry, StoredMessage):
            seen += 1
        if seen < limit and not exhausted:
            continue
        if seen < limit and exhausted and position < len(collected) - 1:
            continue
        if position + 1 < len(collected):
            older = collected[position + 1]
        elif exhausted:
            older = None
        else:
            return None
        if _is_instruction(entry, older):
            return position
    return len(collected) - 1 if exhausted and collected else None


def read_page(source: Source, *, before: str | None = None, limit: int = DEFAULT_LIMIT) -> Page:
    """The newest whole turns before ``before`` (the thread's end when None), at least
    ``limit`` messages when there are that many. Reads backwards from the cursor, one
    bounded query at a time, never the rest of the thread."""
    limit = max(1, min(limit, MAX_LIMIT))
    order = thread_order(source.sessions())
    if not order:
        return Page((), None, source.source)
    position = {session.id: index for index, session in enumerate(order)}
    if before is None:
        index, below = len(order) - 1, None
    else:
        match = CURSOR.match(before)
        if match is None or match["session"] not in position:
            return Page((), None, source.source)
        index, below = position[match["session"]], int(match["seq"])

    floors: dict[str, int] = {}

    def floor(session: StoredSession) -> int:
        """Messages at or under this seq are not the session's own: -1 for a root, the
        handover (0) and its copies for a compacted child."""
        if session.parent is None or session.parent not in position:
            return -1
        if session.id not in floors:
            floors[session.id] = (
                session.copies
                if session.copies is not None
                else copied_count(source, session.parent, session.id)
            )
        return floors[session.id]

    batch = max(limit, 32)
    collected: list[StoredMessage | Handover] = []
    exhausted = False
    while True:
        cut = _cut(collected, limit, exhausted)
        if cut is not None or (exhausted and not collected):
            break
        session = order[index]
        lowest = floor(session)
        rows = source.messages(session.id, above=lowest, below=below, limit=batch)
        collected.extend(rows)
        if len(rows) < batch:
            if lowest >= 0:
                seed = source.seed(session.id)
                collected.append(
                    Handover(
                        session_id=session.id,
                        message_id=seed.id if seed else "",
                        at=seed.at if seed else session.created_at,
                        rewound=bool(seed and seed.rewound),
                    )
                )
            if index == 0:
                exhausted = True
            else:
                index, below = index - 1, None
        else:
            below = rows[-1].seq
    if not collected:
        return Page((), None, source.source)
    assert cut is not None
    first = collected[cut]
    at_beginning = exhausted and cut == len(collected) - 1
    cursor = None
    if not at_beginning and isinstance(first, StoredMessage):
        cursor = f"{first.session_id}:{first.seq}"
    entries = tuple(reversed(collected[: cut + 1]))
    return Page(entries, cursor, source.source, _markers(source, entries))


def _markers(
    source: Source, entries: Sequence[StoredMessage | Handover]
) -> tuple[BudgetMarker | RewindMarker, ...]:
    """The budget decisions and rewinds within the page's turns: after one of its
    messages or, in a session whose fold the page shows, after a message that fold
    copied."""
    read = getattr(source, "markers", None)
    if read is None:
        return ()
    spans: dict[str, list[int]] = {}
    folded: set[str] = set()
    for entry in entries:
        if isinstance(entry, StoredMessage):
            span = spans.setdefault(entry.session_id, [entry.seq, entry.seq])
            span[0], span[1] = min(span[0], entry.seq), max(span[1], entry.seq)
        else:
            folded.add(entry.session_id)
    found: list[BudgetMarker | RewindMarker] = []
    for session_id, (low, high) in spans.items():
        found.extend(read(session_id, -1 if session_id in folded else low, high))
    return tuple(found)
