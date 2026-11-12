"""Reading the loop's own transcript back, for a person.

A stored ``user`` message has one of four authors: the person's instruction, a steer sent
mid-turn (wrapped in ``_STEER_NOTICE`` or, in the app, ``_STEER_CONTINUE_NOTICE``), a
step's tool results, or a harness notice (a repair prompt, the build gate's findings). The message does not say which, because nothing
sent to the model may change to say it. The loop can still tell them apart from its own
templates and from ``provider.base.Message.transcript_text``'s rendering. That knowledge
lives here, next to the code that writes it, so the desktop's history
(``server/history.py``) does not re-derive the formats and drift from them.

Pure functions only; nothing here is called while a turn runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cogito.context import attachments, inbound
from cogito.loop.engine import (
    _ANNOUNCED_STEP_NOTICE,
    _AUTO_CONTINUE_CALL_NOTICE,
    _AUTO_CONTINUE_TEXT_NOTICE,
    _BACKGROUND_NOTICE,
    _BUILD_GATE_PROMPT,
    _GROUNDING_NOTICE,
    _OPEN_TASKS_NOTICE,
    _RECALLED_NOTICE,
    _REPAIR_PROMPT,
    _STATUS_ECHO_NOTICE,
    _TRUNCATED_NOTICE,
    _sanitize_shell_error,
    _short_error,
    _tool_target,
    unwrap_steer,
)
from cogito.provider import repair
from cogito.provider.base import ToolSpec
from cogito.tools.mcp.hub import NOTICE_PREFIX as _MCP_NOTICE

_HARNESS_PREFIXES = (
    _REPAIR_PROMPT.split("{failure}", 1)[0],
    _BUILD_GATE_PROMPT.split("{count}", 1)[0],
    # Harness text the loop offloaded once it no longer fit (`inbound.OPENINGS`).
    inbound.OPENINGS["other"],
    # APP-ONLY auto-continue (`BudgetSettings.max_auto_continuations`): a reopened
    # project's history must not show these as a person's own instruction.
    _AUTO_CONTINUE_TEXT_NOTICE,
    _BACKGROUND_NOTICE,
    _AUTO_CONTINUE_CALL_NOTICE,
    _BACKGROUND_NOTICE,
    # The grounding check, the status-echo check and the output-limit reprompt: a reopened
    # history drew these as the person's own messages (QA rig, 2026-10-16).
    _GROUNDING_NOTICE.split("{values}", 1)[0],
    _STATUS_ECHO_NOTICE,
    _TRUNCATED_NOTICE.split("{limit}", 1)[0],
    # A reply that announced a step and made no call (`Engine._maybe_continue_announced_step`).
    _ANNOUNCED_STEP_NOTICE,
    # APP-ONLY: what the harness found in the conversation's record after a fold.
    _RECALLED_NOTICE.split("{passages}", 1)[0],
    # APP-ONLY: a background command's end (`Engine.notify`).
    _BACKGROUND_NOTICE.split("{report}", 1)[0],
    # APP-ONLY: a turn sent back with items open on the model's task list.
    _OPEN_TASKS_NOTICE.split("{count}", 1)[0],
    # APP-ONLY: which MCP servers are still starting, failed or joined (`McpHub.note`).
    _MCP_NOTICE,
)
#: How `Message.transcript_text` renders a step's tool results: one block per call, each
#: opened by this marker.
_RESULT_MARKERS = ("[result] ", "[error] ")
#: How `Message.transcript_text` renders a tool call inside an assistant message: one line
#: per call, after the message's own text. `repr()` escapes newlines in every argument, so
#: a call is always exactly one line.
_CALL_LINE = re.compile(r"^\[calls [^\s(]+\(.*\)\]$")


def steer_text(text: str) -> str | None:
    """The person's own words, when ``text`` is a steer the loop spliced into a turn."""
    steer = unwrap_steer(text)
    return steer[1] if steer is not None else None


def person_message(
    text: str, project_root: Path
) -> tuple[str, tuple[attachments.Sent, ...]]:
    """What the person wrote and what they attached, from any stored text of theirs (an
    instruction or a steer, as `message_appended` journalled it).

    The one way back to their own words: the thread (`server/history.py`) and anything that
    puts a rewind point's text back in the composer must use it (or `person_text`), never
    `steer_text` alone. It undoes the three wrappers the loop puts around a person's words,
    alone or together: either steer notice (`unwrap_steer`); the notice that replaced a
    message too large to send whole (`context.inbound`), read back from its saved file
    while that file is unchanged; and the attachment blocks (`context.attachments.split`).
    Any other text comes back as it is, with nothing attached."""
    steer = steer_text(text)
    words = steer if steer is not None else text
    return attachments.split(inbound.original(words, project_root) or words)


def person_text(text: str, project_root: Path) -> str:
    """What the person wrote, without what they attached (`person_message`)."""
    return person_message(text, project_root)[0]


def is_harness_notice(text: str) -> bool:
    """A message the harness wrote in the user role: a repair prompt or the build gate's."""
    return any(text.startswith(prefix) for prefix in _HARNESS_PREFIXES)


def is_status_echo_notice(text: str) -> bool:
    """The notice that sent back a reply which only repeated the status line
    (`Engine._maybe_answer_after_status_echo`): the reply before it is not shown either."""
    return text.startswith(_STATUS_ECHO_NOTICE)


def is_tool_results(text: str) -> bool:
    """A step's tool results, as `transcript_text` renders them."""
    return text.startswith(_RESULT_MARKERS)


def made_calls(text: str) -> bool:
    """Whether an assistant message's transcript records at least one tool call."""
    return any(_CALL_LINE.match(line) for line in text.split("\n"))


def assistant_prose(text: str, *, keep_leading: bool = False) -> str:
    """An assistant message's own text, without the ``[calls ...]`` lines the transcript
    appends for each tool call it made.

    ``keep_leading`` is APP-ONLY auto-continue (`BudgetSettings.max_auto_continuations`):
    true for a message that continues the one before it with no call in between
    (`StoredMessage.continued`). Its own leading whitespace is part of the join -- the
    model's continuation picks up exactly where the cut left off, and stripping it could
    mash two words together (``"primero"`` + ``"y después"`` -> ``"primeroy después"``).
    The trailing edge is always stripped: a fragment after this one, if any, supplies its
    own leading separator the same way.
    """
    lines = text.split("\n")
    while lines and _CALL_LINE.match(lines[-1]):
        lines.pop()
    joined = "\n".join(lines)
    return joined.rstrip() if keep_leading else joined.strip()


def prose_without_calls(text: str, names: Sequence[str]) -> str:
    """An assistant message's prose without the calls it wrote as text.

    The live window shows a call read from the text only as its tool row, and one the
    harness refused (a tool that does not exist) not at all (`repair.strip_calls`); the
    transcript keeps the text as the model wrote it. A thread drawn again from the store
    takes them out the same way: every closed `<tool_call>` the live extractor reads, whatever
    tool it names, since the live loop read each as a call. ``names`` (the tools this message
    called) only type the arguments. A message whose calls were native has none in its text,
    and prose that only mentions the tag is left alone.
    """
    tools = {
        name: ToolSpec(name=name, description="", parameters={"type": "object"}) for name in names
    }
    for _, extractor in repair.EXTRACTORS:
        if found := extractor(text, tools):
            return repair.strip_calls(text, found)
    return text


def call_arguments(raw: Any) -> Mapping[str, Any]:
    """A stored call's arguments (``tool_called``'s JSON string) as a mapping."""
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, Mapping) else {}
    return {}


def tool_target(arguments: Mapping[str, Any], project_root: Path) -> dict[str, Any]:
    """What a call was aimed at: the same ``target`` its live ``tool_start`` carried."""
    return _tool_target(SimpleNamespace(input=arguments), project_root)


def tool_error(name: str, content: str, project_root: Path) -> str:
    """A failed call's one-line error: the same ``error`` its live ``tool_end`` carried."""
    error = _short_error(content)
    if name == "shell":
        error = _sanitize_shell_error(error, project_root)
    return error
