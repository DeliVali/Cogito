"""Compaction: the conversation is folded, never rewritten.

Replacing the transcript with a summary and discarding the original means nobody can later
say what a command printed two hours ago. That is the degradation this project exists to
avoid.

So compaction is a link. The current session is closed, a child session is created and
seeded with the summary, and `(parent)-[:COMPACTED_TO]->(child)` records it. Every original
message stays where it was, and `recall` walks that edge to reach it. Messages leave the
context window, never the store.

The summary is structured on purpose. Free-form prose drops the field that matters most,
what was already tried and failed, and a model that cannot see its dead ends walks back
into them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from typing import Any

from cogito.cancel import CancelToken
from cogito.context import budgets, images, inbound
from cogito.context.offload import ArtifactStore
from cogito.errors import CONTEXT_OVERFLOW, ProviderError
from cogito.ids import new_id
from cogito.provider.base import (
    BLOCKS_VERSION,
    ImageBlock,
    Message,
    Provider,
    SamplingParams,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    complete_stoppable,
    encode_blocks,
)

log = logging.getLogger(__name__)

#: Headings the summary must fill. `approaches_that_failed` matters most: everything else
#: can be re-derived from the code, and that cannot.
SECTIONS: tuple[str, ...] = (
    "goal",
    "facts_to_keep",
    "decisions_made",
    "files_touched",
    "approaches_that_failed",
    "open_questions",
    "next_step",
)

_INSTRUCTION = """\
This conversation is about to be folded up to free context. Write the handover for whoever \
continues it — which will be you, with none of the messages above.

Use exactly these headings, each on its own line, in this order:

goal:
facts_to_keep:
decisions_made:
files_touched:
approaches_that_failed:
open_questions:
next_step:

Rules that matter:
- Be concrete. Name files, functions, commands and error messages, not topics.
- `facts_to_keep` holds anything you were *told* rather than anything you found: ticket \
numbers, identifiers, names, credentials, deadlines, a value someone said to use. Copy \
each one **verbatim**, never paraphrased and never summarised. These are the things that \
exist nowhere on disk, so losing one here loses it for good — and a message that ends in \
an instruction often begins with one of them.
- `approaches_that_failed` is the most valuable section of the rest. Every dead end you \
leave out is one you will walk back into. Say what was tried and how it failed.
- `next_step` is one sentence: what to do the moment you read this.
- Do not call any tool. Write only the summary.
- Nothing is lost: every message above stays searchable with `recall`. Summarise for \
working memory, not for the archive.
"""

#: Added to `_INSTRUCTION` only when the person changed course (`changed_course`): a
#: message sent mid-turn, or a request left unfinished before a later one. The handover's
#: `goal` and `next_step` are written by the model, and without this they followed the
#: first request after the person had moved on (2026-10-30: "le decía a qwen, haz otra
#: cosa ... pero todavía realizaba procesos antiguos"). Never in a single-request
#: conversation, so those folds are byte for byte as before.
_LATEST_WINS_RULE = """- The person changed course in this conversation. `goal` and `next_step` are what they \
want **now**: where a later message of theirs conflicts with an earlier request, the \
later one wins. Keep an earlier request in `goal` only where it still applies. A question \
they asked while you worked does not replace the request.
"""

#: The seed carries what was asked plus the handover, in one message. What was asked is
#: pinned because a summary of a goal is a worse goal. One message because one of the two
#: wire dialects rejects two user messages in a row. Pinned across every fold: the caller
#: holds it, so a second fold cannot mistake the first fold's seed for the task.
#:
#: It is a list because a multi-turn conversation was asked several things. Naming only the
#: first misleads once it is done: in a real transcript the model dropped a later request to
#: redo the first, because the handover kept calling that one "the task". The current turn
#: is not in it (compaction runs before it is appended), so this is the history and the
#: live message after the seed is the present.
_SEED_HEAD = """\
This continues an earlier conversation that was folded up to free context.

What you have been asked in this conversation, oldest first and unchanged. The mark on \
each one is this harness's own record of how that turn ended, not your recollection: \
`[done]` means you finished that turn and stopped of your own accord — do not start it \
again; `[cut short: ...]` means the turn was stopped for you and may be unfinished; \
`[working on this now]` is the one you are in the middle of.

{task}
{literals}
Handover from the part of the conversation that is no longer in front of you:

{summary}

"""

_SEED_TAIL = """\
None of it is lost — every earlier message is still searchable. Call `recall` with what \
you are looking for to read any of it back.
"""

#: The whole seed with nothing restored. APP-ONLY: the files a fold hands back
#: (`loop.fold_files`) sit between the two parts, so the text without them is exactly this.
_SEED = _SEED_HEAD + _SEED_TAIL

#: How much instruction history the seed may carry. The first instruction is always kept
#: (it frames the rest), then the most recent that fit, since what the conversation turned
#: into is what a handover must keep. Without a bound, a long session grows this past the
#: summary next to it.
MAX_TASK_CHARS = 1200

#: Tokens allowed for the summary itself. A handover longer than this is a transcript.
MAX_SUMMARY_TOKENS = 1400

#: Recent messages kept verbatim after the summary. The cut then moves back to a clean
#: exchange boundary, so this is a floor, not an exact count.
KEEP_RECENT = 4

#: The first message of a summary request whose oldest messages were left out so it fits
#: the window (`bounded_summary_input`). Only the summary request carries it.
LEFT_OUT = (
    "[{count:,} earlier messages are left out here so this request fits the context window. "
    "They are kept, and stay searchable.]"
)

#: The least a capped instruction keeps of itself in the seed (`render_tasks`), in tokens.
_MIN_INSTRUCTION_TOKENS = 64


@dataclass(frozen=True, slots=True)
class Instruction:
    """One thing the user asked for, and what became of the turn that answered it.

    The outcome is a fact the harness holds (the turn's stop reason), never the model's
    opinion of its own work. Asking the model to judge what it finished asks the question
    it got wrong in the first place.
    """

    text: str
    #: None while this instruction is the one being worked on. Otherwise a short phrase.
    outcome: str | None = None
    #: Steer messages sent while this instruction's turn was running, oldest first (see
    #: `loop.engine.Engine._apply_steers`). Never a separate `Instruction`: a mid-turn
    #: correction amends what was asked. `render_tasks` shows them under this instruction,
    #: so a fold's seed keeps them after the raw steer message is cut away.
    amendments: tuple[str, ...] = ()


#: What a turn that finished without being cut short is called in the seed. Other turns
#: show their reason: "cut short" and "done" are different claims, and the reader acts on
#: the difference.
DONE = "done"


@dataclass(frozen=True, slots=True)
class Compaction:
    """What happened, in the terms the caller has to act on."""

    summary: str
    child_session_id: str
    summary_message_id: str
    messages: tuple[Message, ...]
    folded: int
    #: The journal ids of the kept copies after the seed, in `messages[1:]` order. The
    #: caller tracks each message's id so one already in the transcript can be offloaded
    #: and journalled again under its own id (`loop.engine.Engine._offload_history`).
    copy_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.child_session_id)


def _is_tool_results(message: Message) -> bool:
    return bool(message.blocks) and all(
        isinstance(block, ToolResultBlock) for block in message.blocks
    )


def cut_point(messages: Sequence[Message], keep_recent: int = KEEP_RECENT) -> int:
    """Where the kept tail starts, at a boundary that leaves no orphaned tool call.

    Every provider rejects an assistant tool call with no matching result, and a tool
    result with no call before it. Cutting at an arbitrary index produces both, so the
    index moves back until the slice starts with an assistant message that is not
    mid-exchange, or until nothing is kept.
    """
    if len(messages) <= keep_recent + 1:
        return len(messages)
    index = len(messages) - keep_recent
    while index >= 1:
        candidate = messages[index]
        if candidate.role == "assistant" and not _is_tool_results(candidate):
            return index
        index -= 1
    # No boundary: keep only the seed rather than send a transcript that starts with a tool
    # result whose call is gone.
    return len(messages)


def fit_tail(messages: Sequence[Message], index: int, tail_tokens: int) -> int:
    """Move the cut at ``index`` forward, one exchange boundary at a time, until the kept
    tail takes at most ``tail_tokens`` by the loop's count, or nothing is kept.

    `cut_point` keeps a number of messages, not of tokens. After a switch to a smaller
    window, a few long answers kept verbatim can be larger than the new window by
    themselves, and the fold then summarizes almost nothing while its seed makes the
    request larger (QA 2026-10-11 bug 57). Boundaries are `cut_point`'s: an assistant
    message that is not mid-exchange.
    """

    def kept(start: int) -> int:
        return sum(inbound.size(message.transcript_text()) for message in messages[start:])

    while index < len(messages) and kept(index) > tail_tokens:
        index += 1
        while index < len(messages) and (
            messages[index].role != "assistant" or _is_tool_results(messages[index])
        ):
            index += 1
    return index


def shrink_tail(
    messages: Sequence[Message],
    *,
    per_result_tokens: int,
    store: ArtifactStore | None,
) -> list[Message]:
    """Re-clip each large tool result the fold keeps verbatim down to ``per_result_tokens``.

    The handover carries the last few messages whole; on a small window one big tool output
    (an REA dump, a long file) keeps the seed large, so the session re-folds in a few steps
    and the model, given a bloated seed, restates its plan and stops instead of acting. This
    shrinks such a result to a head-and-tail preview with a pointer to the whole (as the
    loop's own clipping does, `budgets.clip`), which stays readable with read/grep and
    searchable with `recall`. It is older context, kept small on purpose. A result already
    within ``per_result_tokens``, and a message with no tool result, are returned unchanged.

    ``per_result_tokens`` 0 or no ``store`` leaves the tail exactly as it was, so a session
    that does not opt in folds byte for byte as before.
    """
    if store is None or per_result_tokens <= 0:
        return list(messages)
    shrunk: list[Message] = []
    for message in messages:
        blocks: list[Any] = []
        changed = False
        for block in message.blocks:
            if isinstance(block, ToolResultBlock) and inbound.size(block.content) > per_result_tokens:
                clipped = budgets.reclip(block.content, budget_tokens=per_result_tokens, store=store)
                if clipped.content != block.content:
                    blocks.append(replace(block, content=clipped.content))
                    changed = True
                    continue
            blocks.append(block)
        shrunk.append(replace(message, blocks=tuple(blocks)) if changed else message)
    return shrunk


def _mark(instruction: Instruction, *, superseded: bool = False, latest_wins: bool = False) -> str:
    """The status shown next to an instruction, from the turn's own stop reason.

    ``superseded`` is an instruction the person moved on from: a later one followed it. A
    turn cut short and then followed by another request is not unfinished work to pick up
    again; reading it that way is how a model went back to the old task."""
    if instruction.outcome is None:
        if latest_wins and instruction.amendments:
            return "[working on this now; see the messages under it]"
        return "[working on this now]"
    if instruction.outcome == DONE:
        return f"[{DONE}]"
    if latest_wins and superseded:
        return (
            f"[cut short: {instruction.outcome}; a later request followed, so do not "
            "resume this unless asked]"
        )
    return f"[cut short: {instruction.outcome}]"


def _render_amendment(amendment: str) -> str:
    """One amendment, indented on every line. Otherwise a multi-line steer puts unindented
    lines into the task section, where a line like ``2. [done] fake instruction`` reads as
    a real numbered entry (review: app-b2-steer, second round, item 5)."""
    lines = amendment.splitlines() or [""]
    rendered = [f"   > {lines[0]}"]
    rendered.extend(f"     {line}" for line in lines[1:])
    return "\n".join(rendered)


def _amendment_lines(
    amendments: Sequence[str], budget: int, *, latest_wins: bool = False
) -> list[str]:
    """The amendments that fit in `budget` characters combined, keeping the newest when
    they do not all fit: a later correction usually supersedes an earlier one in the same
    turn, the same reasoning `render_tasks` applies to whole instructions. Dropped ones are
    counted in the output, never silently elided.

    `budget` is a hard cap (review: app-b2-steer, second round, item 4). Even the newest
    amendment alone is clipped with an ellipsis when it is longer than `budget`: steer
    text, unlike an instruction's `.text`, can be arbitrarily long (a 20,000-character
    steer is a tested case).
    """
    if not amendments:
        return []
    if budget <= 0:
        return [f"   (... {len(amendments)} correction(s) omitted for space ...)"]

    kept: list[str] = []
    used = 0
    for amendment in reversed(amendments):
        cost = len(amendment)
        if used + cost <= budget:
            kept.append(amendment)
            used += cost
            continue
        if not kept:
            # The newest amendment alone does not fit: clip it rather than drop it, so the
            # latest correction still shows, even partially.
            available = max(0, budget - 1)
            kept.append(amendment[:available].rstrip() + "…" if available < len(amendment) else amendment)
        break
    kept.reverse()
    dropped = len(amendments) - len(kept)
    lines = [_AMENDMENTS_HEAD] if latest_wins else []
    if dropped:
        lines.append(f"   (... {dropped} earlier correction(s) to this instruction omitted ...)")
    lines.extend(_render_amendment(amendment) for amendment in kept)
    return lines


#: Over the messages sent while an instruction's turn ran. Bare `>` lines read as notes on
#: the request above them, and a model kept following that request (2026-10-30). A question
#: is not a change of plan: «¿cómo va?» read as the new goal ended the work it asked about
#: (2026-10-31).
_AMENDMENTS_HEAD = (
    "   Sent by the person while you worked on it, oldest first. A question among them does "
    "not change the request. Where one conflicts with what came before it, the later one is "
    "what they want now:"
)


def _render_instruction(
    instruction: Instruction,
    prefix: str,
    budget: int,
    *,
    superseded: bool = False,
    latest_wins: bool = False,
) -> str:
    """One instruction line, numbered or not per `prefix`, with its amendments indented
    below (steer messages sent during its turn, oldest first; see
    `loop.engine.Engine._apply_steers`). `budget` bounds only the amendments: the
    instruction's own `.text` is never clipped (`render_tasks` explains why only amendments
    are treated as unbounded input)."""
    mark = _mark(instruction, superseded=superseded, latest_wins=latest_wins)
    head_line = f"{prefix}{mark} {instruction.text}"
    lines = [head_line]
    if instruction.amendments:
        lines.extend(
            _amendment_lines(
                instruction.amendments, budget - len(head_line), latest_wins=latest_wins
            )
        )
    return "\n".join(lines)


def _instruction_chars(instruction: Instruction) -> int:
    """What `render_tasks` counts when choosing which instructions to keep: the text plus
    every amendment, so a heavily corrected turn cannot push past `MAX_TASK_CHARS`."""
    return len(instruction.text) + sum(len(a) for a in instruction.amendments)


def render_tasks(
    tasks: Sequence[Instruction],
    *,
    max_tokens: int | None = None,
    store: ArtifactStore | None = None,
    tools: Collection[str] = (),
    latest_wins: bool = False,
) -> str:
    """The instructions given so far, oldest first, each with what became of its turn
    (`_render_tasks`), in at most about ``max_tokens`` when given. ``latest_wins`` marks
    where the person changed course (APP-ONLY, `changed_course`).

    Under ``max_tokens`` the text is exactly what it is without the cap. Over it, the
    largest instructions are cut first (`inbound.cap`: their beginning and end, and where
    the whole of each is saved in ``store`` and how to read it with ``tools``), halving the
    allowance per instruction until the whole fits. Without it, a page pasted as the first
    or newest instruction rides whole in every handover, and the seed alone can fill the
    window. The engine passes a quarter of the window (`inbound.limit_tokens`), the share any
    one message may take. No benchmark seed comes near it (cogito-lab
    tests/context/test_fit_never_in_bench.py)."""
    rendered = _render_tasks(tasks, latest_wins=latest_wins)
    if max_tokens is None or inbound.size(rendered) <= max_tokens:
        return rendered
    allowance = max_tokens
    while allowance >= _MIN_INSTRUCTION_TOKENS:
        capped = [
            replace(task, text=inbound.cap(task.text, allowance, store=store, tools=tools))
            if inbound.size(task.text) > allowance
            else task
            for task in tasks
        ]
        rendered = _render_tasks(capped, latest_wins=latest_wins)
        if inbound.size(rendered) <= max_tokens:
            break
        allowance //= 2
    return rendered


def _render_tasks(tasks: Sequence[Instruction], *, latest_wins: bool = False) -> str:
    """The instructions given so far, oldest first, each with what became of its turn.

    A bare list is not enough. In a real transcript the model redid work it had already
    finished, because a list of requests reads as a list of things still to do. Codex (and
    the Hermes proposal that follows it) keeps every user message verbatim and leaves the
    model to infer status from context; here that inference failed.

    When the list is long, the middle goes: the first instruction says what the
    conversation is for, the last ones what it has become, and the ones between are the
    most likely to be answered already. What was dropped is counted in the output, never
    silently elided: a handover that quietly loses a request is the defect to avoid.

    `MAX_TASK_CHARS` is a hard cap on amendments, spent in render order (review:
    app-b2-steer, second round, item 4). An instruction's own `.text` is typed by the user
    and stays uncapped. An amendment is steer text that a person (or a script, in the
    stress probe) can send as a 20,000-character message. The total is close to
    `MAX_TASK_CHARS`, not exact: the head and every kept instruction's own line are never
    clipped, so many short instructions can push it slightly past. What is guaranteed is
    that amendments cannot make the output grow without bound.
    """
    kept = [
        replace(task, text=task.text.strip()) for task in tasks if task.text.strip()
    ]
    if not kept:
        return ""
    if len(kept) == 1:
        return _render_instruction(kept[0], "", MAX_TASK_CHARS, latest_wins=latest_wins)

    head, rest = kept[0], kept[1:]
    tail: list[Instruction] = []
    used = _instruction_chars(head)
    for instruction in reversed(rest):
        used += _instruction_chars(instruction)
        if used > MAX_TASK_CHARS and tail:
            break
        tail.append(instruction)
    tail.reverse()

    dropped = len(rest) - len(tail)
    # Spent in render order: the head, then the tail oldest first. An instruction's
    # amendments get what is left after its own line, so two instructions with amendments
    # cannot each claim the full budget.
    remaining = MAX_TASK_CHARS

    def render(instruction: Instruction, prefix: str) -> str:
        nonlocal remaining
        superseded = instruction is not kept[-1]
        text = _render_instruction(
            instruction, prefix, remaining, superseded=superseded, latest_wins=latest_wins
        )
        remaining -= len(text)
        return text

    lines = [render(head, "1. ")]
    if dropped:
        notice = f"   (... {dropped} instruction(s) from the middle omitted ...)"
        lines.append(notice)
        remaining -= len(notice)
    lines.extend(
        render(instruction, f"{number}. ")
        for number, instruction in enumerate(tail, start=2 + dropped)
    )
    return "\n".join(lines)


def changed_course(tasks: Sequence[Instruction]) -> bool:
    """Whether the person moved on from what they first asked: a message sent while a turn
    ran, or a request cut short and then followed by another. A done request followed by
    another is not one: the seed already marks it done."""
    kept = [task for task in tasks if getattr(task, "text", "").strip()]
    if not kept:
        return False
    if kept[-1].amendments:
        return True
    return any(task.outcome not in (None, DONE) for task in kept[:-1])


def handover_instruction(tasks: Sequence[Instruction] = (), *, latest_wins: bool = False) -> str:
    """The handover request for these ``tasks``: `_INSTRUCTION`, plus `_LATEST_WINS_RULE`
    when ``latest_wins`` (APP-ONLY) and the person changed course (`changed_course`)."""
    if latest_wins and changed_course(tasks):
        return _INSTRUCTION + _LATEST_WINS_RULE
    return _INSTRUCTION


def summary_budget(window_tokens: int, instruction: str = _INSTRUCTION) -> int:
    """What a summary request's messages may take of a ``window_tokens`` window: the
    window less the summary's own allowance and the handover instruction."""
    return window_tokens - MAX_SUMMARY_TOKENS - inbound.size(instruction)


def _cost(message: Message) -> int:
    # An image's marker is a line of text; the image itself takes about what Claude's rule
    # gives (`context.images.estimated_tokens`).
    pictures = sum(
        images.estimated_tokens(images.ImageInfo("png", block.width, block.height))
        if block.width and block.height
        else images.STANDARD_MAX_PATCHES
        for block in (*message.images, *_result_images(message))
    )
    return inbound.size(message.transcript_text()) + pictures


def _result_images(message: Message) -> tuple[ImageBlock, ...]:
    return tuple(
        image
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
        for image in block.images
    )


def _without_images(message: Message) -> Message:
    """``message`` with no image blocks, its tool results' included: a summary is written
    from the marker each one leaves in the text, never from the image again."""
    if not message.images and not _result_images(message):
        return message
    kept = tuple(
        replace(block, images=()) if isinstance(block, ToolResultBlock) else block
        for block in message.blocks
        if not isinstance(block, ImageBlock)
    )
    return Message(message.role, kept or (TextBlock("..."),))


def _shrunk(message: Message, piece: int) -> Message:
    """``message`` without signed thinking, and with any text, tool result or tool-call
    argument over ``piece`` tokens cut to its beginning and end (`inbound.excerpt`)."""
    blocks: list[Any] = []
    changed = False
    for block in message.blocks:
        if isinstance(block, ThinkingBlock):
            # A signed thinking block that was edited is refused by its API, and the summary
            # does not need the reasoning.
            changed = True
        elif isinstance(block, TextBlock) and inbound.size(block.text) > piece:
            blocks.append(TextBlock(inbound.excerpt(block.text, piece)))
            changed = True
        elif isinstance(block, ToolResultBlock) and inbound.size(block.content) > piece:
            blocks.append(replace(block, content=inbound.excerpt(block.content, piece)))
            changed = True
        elif isinstance(block, ToolUseBlock) and any(
            isinstance(v, str) and inbound.size(v) > piece for v in block.input.values()
        ):
            arguments = {
                k: inbound.excerpt(v, piece) if isinstance(v, str) else v
                for k, v in block.input.items()
            }
            blocks.append(replace(block, input=arguments))
            changed = True
        else:
            blocks.append(block)
    if not changed:
        return message
    return Message(message.role, tuple(blocks) or (TextBlock("..."),))


def bounded_summary_input(
    messages: Sequence[Message], budget_tokens: int
) -> tuple[list[Message], int]:
    """What a fold summarizes, brought under ``budget_tokens`` (the loop's count), and how
    many of the oldest messages it left out. Unchanged when it already fits, except that an
    attached image is never sent again: its marker in the text is what the summary reads.

    Over the budget: signed thinking is dropped, and any text, tool result or call argument
    over a quarter of the budget is cut to its beginning and end. Nothing is saved, since
    the originals stay in the parent session. If that is not enough, the oldest messages
    are left out up to an assistant message, so no call loses its result, and a first
    message says how many (`LEFT_OUT`). Codex drops the oldest item the same way when its
    summary request overflows (codex-rs/core/src/compact.rs @cc7ba33, read 2026-10-10)."""
    messages = list(messages)
    messages = [_without_images(m) for m in messages]
    if sum(_cost(m) for m in messages) <= budget_tokens:
        return messages, 0
    piece = max(1, budget_tokens // 4)
    shrunk = [_shrunk(m, piece) for m in messages]
    costs = [_cost(m) for m in shrunk]
    total = sum(costs)
    marker = inbound.size(LEFT_OUT.format(count=10**6))
    start = 0
    while total + (marker if start else 0) > budget_tokens:
        following = next(
            (i for i in range(start + 1, len(shrunk)) if shrunk[i].role == "assistant"), None
        )
        if following is None:
            break
        total -= sum(costs[start:following])
        start = following
    if not start:
        return shrunk, 0
    return [Message.text("user", LEFT_OUT.format(count=start)), *shrunk[start:]], start


def summarize(
    provider: Provider,
    messages: Sequence[Message],
    *,
    params: SamplingParams | None = None,
    cancel: CancelToken | None = None,
    instruction: str = _INSTRUCTION,
    think: bool = True,
) -> str:
    """Ask the model to write its own handover. No tools are offered, so none can be called.

    The composer's thinking level is the turn's, not the handover's: a 1,400-token summary
    at Claude's ``max`` effort, or with a token budget of three quarters of it, would spend
    the summary thinking. The request goes with the session's own options.

    ``think`` False (APP-ONLY, `BudgetSettings.fold_without_thinking`) asks with thinking
    off: the "off" level, which Claude maps to its own switch and llama.cpp's dialect sends
    as ``enable_thinking`` false, with no reasoning budget. A level the turn raised ("long")
    raises the budget too, so clearing the level alone left it thinking."""
    options = getattr(provider, "options", None)
    if not think and options is not None and hasattr(provider, "configured"):
        provider = provider.configured(
            replace(options, thinking_level="off", reasoning=False, reasoning_budget=None)
        )
    elif getattr(options, "thinking_level", None) is not None and hasattr(provider, "configured"):
        provider = provider.configured(replace(options, thinking_level=None))
    conversation = [*messages, Message.text("user", instruction)]
    sampling = replace(params or SamplingParams(), max_tokens=MAX_SUMMARY_TOKENS)
    completion = complete_stoppable(
        provider,
        conversation,
        (),
        params=sampling,
        cancel=cancel or CancelToken(),
    )
    return (completion.raw_text or "").strip()


def is_structured(summary: str) -> bool:
    """True when the model filled the headings instead of writing an essay.

    A summary without the structure is still better than none, so this only decides what
    the log says, never whether to compact.
    """
    lowered = summary.lower()
    return sum(1 for section in SECTIONS if f"{section}:" in lowered) >= 4


def compact(
    *,
    provider: Provider,
    messages: Sequence[Message],
    tasks: Sequence[str],
    literals: str = "",
    record: Callable[..., Any],
    project_id: str,
    parent_session_id: str,
    model_id: str,
    params: SamplingParams | None = None,
    prompt_time: str | None = None,
    cancel: CancelToken | None = None,
    keep_recent: int = KEEP_RECENT,
    task_tokens: int | None = None,
    task_store: ArtifactStore | None = None,
    tools: Collection[str] = (),
    window_tokens: int | None = None,
    tail_tokens: int | None = None,
    result_tokens: int | None = None,
    restore: Callable[[], str] | None = None,
    latest_wins: bool = False,
    think: bool = True,
) -> Compaction:
    """Fold the conversation into a child session and return what the caller should hold.

    ``tasks`` are the instructions given so far, oldest first. They are passed in, not read
    from ``messages[0]``: after the first fold that message is this function's own seed,
    and deriving the task from it nests each seed in the next until the handover is mostly
    boilerplate. ``task_tokens``, ``task_store`` and ``tools`` cap the seed's task section
    (`render_tasks`).

    ``window_tokens``, when given, is the window the summary request must fit. If by the
    loop's count it does not, what is summarized is bounded first (`bounded_summary_input`);
    ``tail_tokens``, when given, bounds what is kept verbatim after the seed (`fit_tail`).
    The loop gives it only after the window changed under the transcript.
    and if the server still refuses it as too large, it is bounded once more by the
    refusal's own numbers and asked again. Without it the request goes as it is, as a fold
    whose last request fit always did.

    ``restore`` is APP-ONLY (`loop.fold_files`): called once the summary exists, right before
    the seed is built, it returns the files the conversation touched as they are on disk
    now, to sit after the handover. Empty or None leaves the seed exactly as it was; one
    that raises is ignored, since a fold must not depend on it.

    ``latest_wins`` is APP-ONLY: where the person changed course (`changed_course`), the
    seed marks it and the handover request says the latest message sets the goal. False
    leaves the fold byte for byte as before.

    ``think`` False is APP-ONLY: the handover is asked for with thinking off (`summarize`).

    On any failure this returns a Compaction with ``ok`` False and the ``messages`` passed
    in, unchanged. A failed compaction must leave the run as it found it; otherwise a turn
    loses its transcript and its lineage at once.
    """
    unchanged = Compaction("", "", "", tuple(messages), 0)
    if len(messages) < 3:
        return unchanged
    rendered_tasks = render_tasks(
        tasks, max_tokens=task_tokens, store=task_store, tools=tools, latest_wins=latest_wins
    )
    if not rendered_tasks:
        # Checked before the sampling call: a fold that cannot say what was asked would
        # seed the child with an empty goal, which is worse than not folding.
        log.warning("compaction has no task to pin; keeping the transcript")
        return unchanged

    index = cut_point(messages, keep_recent)
    if tail_tokens is not None:
        index = fit_tail(messages, index, tail_tokens)
    asked = list(messages[:index])
    instruction = handover_instruction(tasks, latest_wins=latest_wins)
    budget: int | None = None
    if window_tokens:
        budget = summary_budget(window_tokens, instruction)
        if budget <= 0:
            log.warning("a summary request cannot fit %d tokens; not folding", window_tokens)
            return unchanged
        request = [*asked, Message.text("user", instruction)]
        asked_tokens = inbound.size("\n".join(m.transcript_text() for m in request))
        if asked_tokens > window_tokens - MAX_SUMMARY_TOKENS:
            asked, dropped = bounded_summary_input(asked, budget)
            log.info("the summary request was bounded to fit (%d messages left out)", dropped)
    try:
        summary = summarize(
            provider, asked, params=params, cancel=cancel, instruction=instruction, think=think
        )
    except ProviderError as exc:
        requested = getattr(exc, "requested_tokens", None)
        available = getattr(exc, "available_tokens", None)
        if (
            budget is None
            or getattr(exc, "category", None) != CONTEXT_OVERFLOW
            or not requested
            or not available
        ):
            raise
        # The loop's count was low for this text. Once more, by the server's own numbers.
        asked, _ = bounded_summary_input(messages[:index], budget * available // requested)
        summary = summarize(
            provider, asked, params=params, cancel=cancel, instruction=instruction, think=think
        )
    if not summary:
        log.warning("compaction produced an empty summary; keeping the transcript")
        return unchanged
    if not is_structured(summary):
        log.info("compaction summary did not keep the headings; using it anyway")

    child_session_id = new_id()
    record(
        "session_started",
        id=child_session_id,
        project_id=project_id,
        title="continued",
        model_id=model_id,
        sampling_seed=params.seed if params is not None else None,
        prompt_time=prompt_time,
    )
    # The literal block is data, not instructions: values a tool already produced, copied
    # verbatim. Prose loses exact values, and a value still in the window is one the model
    # cannot get wrong.
    restored = ""
    if restore is not None:
        try:
            restored = restore() or ""
        except Exception as exc:  # the fold must not depend on it
            log.warning("restoring files for the seed failed, folding without them: %s", exc)
    seed = Message.text(
        "user",
        _SEED_HEAD.format(
            task=rendered_tasks,
            literals=f"\n{literals}\n" if literals else "",
            summary=summary,
        )
        + (f"{restored}\n\n" if restored else "")
        + _SEED_TAIL,
    )
    summary_message_id = new_id()
    # With its blocks, like every message the loop appends: a restarted app rebuilds the
    # child's transcript (this seed and the copies below) from the journal.
    record(
        "message_appended",
        id=summary_message_id,
        session_id=child_session_id,
        seq=0,
        role="user",
        text=seed.transcript_text(),
        tokens=0,
        step=0,
        blocks=encode_blocks(seed.blocks),
        blocks_version=BLOCKS_VERSION,
    )
    # Shrink a large tool result the fold keeps verbatim, so one big output does not keep
    # the seed large on a small window (`shrink_tail`). Off by default; the app opts in.
    tail = messages[index:]
    if result_tokens is not None:
        tail = shrink_tail(tail, per_result_tokens=result_tokens, store=task_store)
    kept = (seed, *tail) if index < len(messages) else (seed,)
    # Only once both nodes exist: the edge makes the parent's messages reachable, and an
    # edge projected before its endpoints would be dropped.
    # ``copies`` is how many of the child's messages after the seed are copies of the
    # parent's; what the child itself says starts after them.
    record(
        "session_compacted",
        parent_id=parent_session_id,
        child_id=child_session_id,
        summary_message_id=summary_message_id,
        copies=len(kept) - 1,
    )

    copy_ids: list[str] = []
    for position, message in enumerate(kept[1:], start=1):
        copy_id = new_id()
        copy_ids.append(copy_id)
        record(
            "message_appended",
            id=copy_id,
            session_id=child_session_id,
            seq=position,
            role=message.role,
            text=message.transcript_text(),
            tokens=0,
            step=0,
            blocks=encode_blocks(message.blocks),
            blocks_version=BLOCKS_VERSION,
        )
    return Compaction(
        summary=summary,
        child_session_id=child_session_id,
        summary_message_id=summary_message_id,
        messages=tuple(kept),
        folded=index - 1,
        copy_ids=tuple(copy_ids),
    )
