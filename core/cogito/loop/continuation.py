"""Turns a project's journalled conversation back into the loop's state.

`graph.resume.find` reads where the conversation stands; this decides what the engine holds
when it continues from there (`Engine.continue_from`). The rule is full fidelity: the model
is sent exactly the messages it was sent before the app closed (every block, call id and
signed thinking block), and the normal compaction applies if they no longer fit, as it
would have with the app open. Nothing is re-summarised on the way in.

Restored besides the messages:

- the instructions, what became of each turn (`turn_ended`), and the steers that amended
  them. The next fold's seed names them, and the fold cannot run without them;
- the values tools printed (`context.literals`), replayed in the order first seen;
- how full the window was at the last request, which `context_status` shows and which
  decides the next fold;
- whether the leaf is a fold's child, which exposes `recall`.

Not restored, on purpose: the file tracker's read snapshots (a file may have changed while
the app was closed, so the model must read it again before writing, as after a fold); the
build gate's baselines (taken afresh at the first shell call, against the files as they are
now); a provider failure's resumable turn (`Engine.resume` is for a turn this process
stopped); and the session's repair counters (a per-process measurement). The todo list the
graph schema has a projection for is never journalled, so there is nothing to restore.

Journals written before blocks existed come back as text: a tool call as its
``[calls ...]`` line, a result as ``[result] ...``. That is the best approximation (call
ids, object arguments and signatures were never kept), and text is valid on both wire
dialects.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cogito.context import attachments
from cogito.context import compact as compaction
from cogito.graph.resume import Continuation, JournalCall, JournalMessage, Replay, TurnEnd
from cogito.loop import readback
from cogito.loop.engine import (
    _OUTCOMES,
    _REPAIRED_NOTICE,
    _REPEAT_NOTICE,
    _RESULT_ON_NODE_CHARS,
)
from cogito.loop.state import StopReason
from cogito.provider.base import (
    BLOCKS_VERSION,
    Message,
    ToolResultBlock,
    ToolUseBlock,
    UnknownBlock,
    decode_blocks,
)

#: The result for a call that was running when the app closed, so none was recorded.
#: Never dropped: every wire dialect rejects a call with no result, and a model that is not
#: told assumes nothing happened. Something may have: a command can finish after the
#: process waiting on it is gone. Claude Code tells its model the same about a call cut off
#: by a crash (code.claude.com/docs/en/sessions, "What a resumed session restores", read
#: 2026-09-30).
CUT_OFF = (
    "This call was cut off: the app closed before its result was recorded. It may or may "
    "not have taken effect; check before running it again."
)

#: Appended to a result the graph kept only the start of (`engine._RESULT_ON_NODE_CHARS`),
#: when the app closed before the step's results were appended and that start is all there
#: is.
UNRECORDED_REST = "\n[The rest of this result was not recorded.]"

#: Why a turn that never ended is recorded as ended when the conversation resumes: the app
#: closed during it, or a rewind went back to a point inside it (a steer). Either way the
#: turn was cut, and a later fold's seed says so.
APP_CLOSED = "app_closed"
REWOUND = "rewound"
INTERRUPTED = _OUTCOMES[StopReason.USER_INTERRUPT]


def _notice_pattern(template: str, field: str) -> re.Pattern[str]:
    head, tail = template.split("{" + field + "}", 1)
    return re.compile(re.escape(head) + r".+?" + re.escape(tail) + r"$", re.DOTALL)


#: Notices the loop appends to a result after the literal store has seen it
#: (`Engine._continue`). Stripped so the replay feeds the store what it saw the first time.
_REPEAT = _notice_pattern(_REPEAT_NOTICE, "count")
_REPAIRED = _notice_pattern(_REPAIRED_NOTICE, "names")


@dataclass(frozen=True, slots=True)
class Closing:
    """What closes a step the app closed in the middle of."""

    #: The message that made the calls, and its step.
    assistant_id: str
    step: int
    #: One result per call, in the order the calls were made.
    results: tuple[ToolResultBlock, ...]
    #: The calls that never finished, so they have no `tool_called`.
    cut: tuple[ToolUseBlock, ...]


@dataclass(frozen=True, slots=True)
class Rebuilt:
    messages: tuple[Message, ...]
    message_ids: tuple[str, ...]
    next_seq: int
    instructions: tuple[compaction.Instruction, ...]
    #: The message that opened the last turn.
    instruction_id: str | None
    #: Turns that never ended (the app closed during them), by the id of the message that
    #: opened them. The engine journals them as such once.
    unended: tuple[str, ...]
    window_used: int
    compacted_at: int
    #: ``(content, tool, step)`` for every tool result, in the order it was first seen.
    literals: tuple[tuple[str, str, int], ...]
    closing: Closing | None
    #: A fold's copies the app died before writing; written when the conversation resumes.
    missing_copies: tuple[Message, ...] = ()
    #: Turn ends to journal again: the current end of a turn that is not its last recorded.
    restate: tuple[TurnEnd, ...] = ()


def message_of(stored: JournalMessage) -> Message:
    """The message as the model was sent it, or its text when that is all the journal kept:
    a message journalled before blocks existed, in an encoding this version does not know,
    or with a block this version cannot read (a field of the wrong type)."""
    role = stored.role if stored.role in ("user", "assistant") else "user"
    if stored.blocks is not None and stored.blocks_version == BLOCKS_VERSION:
        try:
            return Message(role, decode_blocks(stored.blocks))
        except (UnknownBlock, TypeError, ValueError, AttributeError):
            pass
    return Message.text(role, stored.text)


def _legacy_point(stored: JournalMessage, previous: JournalMessage | None) -> str | None:
    """The point of a message journalled before `point` existed, read off its text as the
    history does: a steer by the loop's own template, a person's instruction when it is
    neither a harness notice nor a step's tool results."""
    if stored.role != "user":
        return None
    if readback.steer_text(stored.text) is not None:
        return "steer"
    if readback.is_harness_notice(stored.text):
        return None
    if readback.is_tool_results(stored.text) and (
        previous is None or readback.made_calls(previous.text)
    ):
        return None
    return "instruction" if stored.text.strip() else None


def point_of(stored: JournalMessage, previous: JournalMessage | None) -> str | None:
    """``"instruction"``, ``"steer"`` or None: whether a message is a person's own words."""
    if stored.blocks is not None:
        return stored.point
    return _legacy_point(stored, previous)


def _current_end(
    ends: tuple[TurnEnd, ...], positions: dict[str, int], last: JournalMessage | None
) -> TurnEnd | None:
    """The recorded end that says how the turn ended in this conversation, or None.

    An end counts only if the conversation stood where it was recorded when the turn
    stopped. Its ``last_message_id`` must still be in the conversation (a rewind to a steer
    hides the turn's later end and records its own), and nothing of the turn may come after
    it (a turn stopped by a provider failure, resumed in the same process and then cut by
    the app closing, went on past the failure's end). A redo makes the turn's real end
    current again. An end from before `last_message_id` was journalled is taken as is.
    """
    turn_last = positions.get(last.id, -1) if last is not None else -1
    current = [
        end
        for end in ends
        if end.last_message_id is None
        or (end.last_message_id in positions and positions[end.last_message_id] >= turn_last)
    ]
    return max(current, key=lambda end: end.order) if current else None


def _instructions(
    continuation: Continuation,
) -> tuple[list[tuple[str, compaction.Instruction]], list[str], list[TurnEnd]]:
    """Every instruction the conversation was given, oldest first, across its folds, with
    its amendments and what became of its turn."""
    found: list[tuple[str, compaction.Instruction]] = []
    unended: list[str] = []
    restate: list[TurnEnd] = []
    # For a turn from before `turn_ended` existed, the ending is read off its last message:
    # an assistant message without calls means the model stopped on its own. Anything else
    # could be any other stop, and "stopped early" is the loop's word for a stop with no
    # better name.
    last_of: dict[str, JournalMessage] = {}
    positions = {stored.id: index for index, (stored, _) in enumerate(continuation.history)}
    previous: JournalMessage | None = None
    current: str | None = None
    for stored, restated in continuation.history:
        if not restated:
            point = point_of(stored, previous)
            if point == "instruction":
                # As the live loop records it (`Engine.run`): an attachment as a line saying
                # where it is kept, never its content.
                found.append(
                    (stored.id, compaction.Instruction(attachments.seed_text(stored.text).strip()))
                )
                current = stored.id
            elif point == "steer" and found:
                words = readback.steer_text(stored.text)
                if words is not None:
                    key, instruction = found[-1]
                    found[-1] = (
                        key,
                        compaction.Instruction(
                            instruction.text,
                            amendments=(
                                *instruction.amendments,
                                attachments.seed_text(words).strip(),
                            ),
                        ),
                    )
            if current is not None:
                last_of[current] = stored
        previous = stored
    resolved: list[tuple[str, compaction.Instruction]] = []
    for key, instruction in found:
        ends = continuation.turn_ends.get(key, ())
        end = _current_end(ends, positions, last_of.get(key))
        if end is not None:
            ended = end.outcome or None
            if end.last_message_id is not None and end.order != max(e.order for e in ends):
                # The graph keeps the last end journalled for a turn. Journal this one again
                # so it is the last there too (a redo after a rewind into the turn).
                restate.append(end)
        elif key in last_of and last_of[key].blocks is None:
            last = last_of[key]
            finished = last.role == "assistant" and not readback.made_calls(last.text)
            ended = compaction.DONE if finished else "stopped early"
        else:
            # Opened and never ended on this conversation, and nothing runs now: the app
            # closed in the middle of it, or a rewind went back into it.
            ended = INTERRUPTED
            unended.append(key)
        resolved.append(
            (
                key,
                compaction.Instruction(
                    instruction.text, outcome=ended, amendments=instruction.amendments
                ),
            )
        )
    return resolved, unended, restate


def _strip_notices(content: str) -> str:
    return _REPAIRED.sub("", _REPEAT.sub("", content))


def _literals(
    continuation: Continuation, messages: dict[str, Message]
) -> list[tuple[str, str, int]]:
    """Every result the literal store saw, in order: each call as `tool_called` recorded it,
    with the content the model was given when the step's results were journalled. A fold's
    copies made no calls, so their results count once, where they were first seen."""
    inputs: list[tuple[str, str, int]] = []
    history = continuation.history
    for index, (stored, _) in enumerate(history):
        if stored.role != "assistant":
            continue
        results: dict[str, str] = {}
        if index + 1 < len(history):
            following = messages.get(history[index + 1][0].id)
            if following is not None:
                results = {
                    b.tool_use_id: b.content
                    for b in following.blocks
                    if isinstance(b, ToolResultBlock)
                }
        for call in continuation.calls.get(stored.id, ()):
            name = str(call.properties.get("name") or "")
            if call.id in results:
                content = _strip_notices(results[call.id])
            else:
                content = str(call.properties.get("result") or "")
            inputs.append((content, name, stored.step))
    return inputs


def _recorded_result(call: JournalCall) -> ToolResultBlock:
    content = str(call.properties.get("result") or "")
    if len(content) >= _RESULT_ON_NODE_CHARS:
        content += UNRECORDED_REST
    return ToolResultBlock(call.id, content, is_error=not bool(call.properties.get("ok")))


def _closing(continuation: Continuation, transcript: Sequence[Message]) -> Closing | None:
    """The results for a step whose calls were made and never answered because the app
    closed while they ran. Only the last message can be one, since a step's results are
    appended together right after its calls. A call that did finish keeps the result
    `tool_called` recorded for it."""
    if not transcript or transcript[-1].role != "assistant" or not transcript[-1].tool_uses:
        return None
    stored = continuation.transcript[-1]
    recorded = {call.id: call for call in continuation.calls.get(stored.id, ())}
    results: list[ToolResultBlock] = []
    cut: list[ToolUseBlock] = []
    for use in transcript[-1].tool_uses:
        if use.id in recorded:
            results.append(_recorded_result(recorded[use.id]))
        else:
            results.append(ToolResultBlock(use.id, CUT_OFF, is_error=True))
            cut.append(use)
    return Closing(stored.id, stored.step, tuple(results), tuple(cut))


def rebuild(continuation: Continuation) -> Rebuilt:
    decoded = {stored.id: message_of(stored) for stored, _ in continuation.history}
    for stored in (*continuation.transcript, *continuation.missing_copies):
        decoded.setdefault(stored.id, message_of(stored))
    missing = tuple(decoded[stored.id] for stored in continuation.missing_copies)
    transcript = tuple(decoded[stored.id] for stored in continuation.transcript) + missing
    instructions, unended, restate = _instructions(continuation)

    # How full the window was: the last request this conversation sent, as the provider
    # measured it. A fold's copies carry no measurement, and a fold makes an older one
    # meaningless (`Engine._maybe_compact` zeroes it), so only the transcript's own
    # messages count. Nothing measured, nothing shown.
    window_used = 0
    for stored in reversed(continuation.transcript):
        if stored.role == "assistant" and stored.input_tokens is not None:
            window_used = stored.input_tokens + stored.tokens
            break

    return Rebuilt(
        messages=transcript,
        message_ids=tuple(stored.id for stored in continuation.transcript),
        next_seq=continuation.next_seq,
        instructions=tuple(instruction for _, instruction in instructions),
        instruction_id=instructions[-1][0] if instructions else None,
        unended=tuple(unended),
        window_used=window_used,
        # What `_maybe_compact` set after the fold: the seed and the messages it kept.
        compacted_at=(
            -1
            if continuation.copies is None
            else min(1 + continuation.copies, len(transcript))
        ),
        literals=tuple(_literals(continuation, decoded)),
        # Copies are the parent's own messages, whose calls were answered there.
        closing=None if missing else _closing(continuation, transcript),
        missing_copies=missing,
        restate=tuple(restate),
    )


@dataclass(frozen=True, slots=True)
class RewindPlan:
    """What rewinding the conversation to a point does: the messages it hides (the point
    and everything said after it), and the point's own words, which go back to the
    composer."""

    point_id: str
    point_text: str
    point_at: str
    hidden: tuple[str, ...]
    #: The session's highest seq now: whatever is appended after the rewind is above it.
    after_seq: int
    #: The session holding the point, when it is not part of this conversation (another
    #: opening of the project). The conversation continues from it.
    continues_from: str | None = None
    #: What the person attached to the point (design 1n), as the window attaches it again:
    #: ``{"path"}`` for a file, read anew when sent, ``{"text"}`` for a paste.
    point_attachments: tuple[dict[str, str], ...] = ()


def plan_rewind(
    replayed: Replay,
    session_id: str,
    point_id: str,
    project_id: str | None = None,
    project_root: Path | None = None,
) -> RewindPlan | None:
    """None when ``point_id`` is not a person's message of this project's thread that the
    conversation still holds (unknown, already rewound, or another role).

    Everything after the point goes back: what the conversation said after it and, with
    ``project_id`` over a replay of every journal of the project, whatever else the thread
    shows after it, in the thread's order (another opening of the project in between, say).
    The person reads the thread as one line, so a rewind that left part of what followed
    showing would show a thread the model no longer holds."""
    point = replayed.messages.get(point_id)
    if point is None or point_id in replayed.hidden:
        return None
    if point_of(point, replayed.messages.get(replayed.parent.get(point_id) or "")) is None:
        return None
    ours = [stored.id for stored, _ in replayed.history(replayed.tip.get(session_id))]
    continues_from = None
    if point_id in ours:
        hidden = ours[ours.index(point_id) :]
    else:
        continues_from = point.session_id
        # A point from another opening of the project, shown by the history: rewinding to
        # it hides everything after it there, and all of this conversation.
        theirs = [
            stored.id
            for stored, _ in replayed.history(replayed.tip.get(replayed.leaf(point.session_id)))
        ]
        if point_id not in theirs:
            return None
        after = theirs[theirs.index(point_id) :]
        hidden = after + [message_id for message_id in ours if message_id not in set(after)]
    if project_id is not None:
        taken = set(hidden)
        hidden = hidden + [m for m in replayed.thread_after(point_id, project_id) if m not in taken]
    # The person's own words as they wrote them (`readback.person_message`): without the
    # notice a steer is wrapped in, and read back from its file when the message was too
    # large to send whole. ``project_root`` is where that file lives; without it only the
    # steer's wrapper can be removed.
    sent: tuple[attachments.Sent, ...] = ()
    if project_root is not None:
        text, sent = readback.person_message(point.text, project_root)
    else:
        text = readback.steer_text(point.text) or point.text
    return RewindPlan(
        point_id=point_id,
        point_text=text,
        point_at=point.at,
        hidden=tuple(message_id for message_id in hidden if message_id not in replayed.hidden),
        after_seq=replayed.highest.get(session_id, -1),
        continues_from=continues_from,
        point_attachments=tuple(item.spec() for item in sent),
    )
