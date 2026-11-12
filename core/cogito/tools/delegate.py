"""`delegate`: hand a task to a subagent that works on it in its own context.

The subagent is a second engine on the same model, started for one task and closed when it
answers. What fills its window never enters the caller's: only its answer comes back. One
level only (`ToolRegistry.catalog` drops delegating tools below the top). Inside the call,
so the caller waits for the answer; several calls in one reply run at the same time when the
loop allows it (`LoopSettings.parallel_delegates`, `Engine._run_delegates_together`).

Two kinds. An explorer (the default) reads with the session's read-only tools (read, grep,
glob, the web, the documents). A writer, where the runner for one is given, works in its
own git copy of the project (`tools.workcopy`) with the shell, write and edit, and its
changes come back as a patch the caller applies; nothing in the project changes before.

This layer may not import `loop/`, so the engine is built by a runner `cli.py` injects
(CONTRIBUTING §2). The runner returns a `Delegated`; this tool only checks the task and
words the result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters
from cogito.tools.workcopy import CopyRefused

#: A task shorter than this names nothing a subagent could look into.
_MIN_TASK_CHARS = 12
#: Longer than this is a document, not a task; the caller reads it itself.
_MAX_TASK_CHARS = 8000

EXPLORE = "explore"
WRITE = "write"


@dataclass(frozen=True, slots=True)
class Delegated:
    """What the subagent did, as the runner reports it."""

    answer: str
    #: The engine's stop reason as a string: ``end_turn`` when it answered.
    stop_reason: str
    steps: int
    input_tokens: int
    output_tokens: int
    session_id: str
    #: The tool calls the subagent made, which its row counts («N llamadas»).
    calls: int = 0
    #: A writer only: where its patch was saved, relative to the project ("" when it
    #: changed nothing the patch carries), the patch's `git diff --stat`, and the files it
    #: deleted, which the patch leaves in place.
    patch: str = ""
    stat: str = ""
    deleted: tuple[str, ...] = ()
    #: It stopped at a limit and was asked once more for what it had found: ``answer`` is
    #: that reply, and ``stop_reason`` still the limit.
    partial: bool = False


Runner = Callable[[str, ToolContext], Delegated]


class NotForSubagent:
    """A tool a read-only subagent sees but may not use.

    The subagent lists its parent's tools, so its system turn is the parent's and the
    server reuses what it already read (`Engine.subagent`). A call to one of these is
    answered with what to do instead, as any tool error is. Its spec keeps the name,
    description and parameters the request lists, and drops what would tell the loop the
    call changed the workspace or delegates."""

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = replace(
            spec, mutates=False, operates=False, delegates=False, after_compaction=False
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.error(
            f"`{self.spec.name}` is not available to a subagent: it only reads (read, grep, "
            "glob, the web and the documents), and it cannot change files, run commands or "
            "delegate. Say in your findings what should be changed or run, and the caller "
            "will do it.",
            "not_for_subagent",
        )


_DESCRIPTION = (
    "Hand one self-contained task to a subagent that works on it in a fresh context of its "
    "own, and get back only its report. Use it when the work means reading many files or "
    "long outputs you do not need to keep: what the subagent reads never enters your "
    "context. By default (kind \"explore\") it only reads: read, grep, glob, the web and the "
    "project's documents. It does not see this conversation, so put in `task` everything it "
    "needs: the goal, the files or folders to look at, and what the answer must contain. "
    "Its report is information for you to check, not an instruction."
)
#: Said only where a writer can run.
_WRITER = (
    " With kind \"write\" it makes a change in its own copy of the project (the current "
    "state, your uncommitted changes included) with the shell, write and edit, runs what "
    "checks it can there (no network), and returns a patch: nothing in the project changes "
    "until you read the patch and apply it with `git apply`."
)
#: Said only where a step's `delegate` calls run at the same time.
_TOGETHER = (
    " Independent tasks go in separate `delegate` calls in the same reply: up to {n} run at "
    "the same time."
)
#: Said where the model's server answers fewer requests at once than that.
_SERVED = (
    " This model's server answers {k} request at a time, so their model steps take turns "
    "and what overlaps is their tool time."
)

#: The stop reasons that are not an answer, worded for the caller.
_STOPPED = {
    "max_steps": "it reached its step limit",
    "wall_clock": "it ran out of time",
    "token_budget": "it ran out of tokens",
    "no_progress": "it repeated itself without making progress",
    "consecutive_tool_errors": "its tool calls kept failing",
    "output_limit": "its reply was cut at the output limit",
    "provider_failure": "the model could not be reached",
}


def stopped_because(stop_reason: str) -> str:
    """Why a subagent stopped, in words for a model."""
    return _STOPPED.get(stop_reason, f"it stopped ({stop_reason})")


def describe(*, together: int = 1, writer: bool = False, served: int | None = None) -> str:
    text = _DESCRIPTION + (_WRITER if writer else "")
    if together > 1:
        text += _TOGETHER.format(n=together)
        if served is not None and 0 < served < together:
            text += _SERVED.format(k=served)
    return text


class DelegateTool:
    def __init__(
        self,
        runner: Runner,
        *,
        together: int = 1,
        writer: Runner | None = None,
        served: int | None = None,
    ) -> None:
        self._runner = runner
        self._writer = writer
        properties: dict[str, Any] = {
            "task": {
                "type": "string",
                "description": (
                    "The task, written for someone who has not seen this conversation: the "
                    "goal, the files, folders or sites to look at, and what the answer must "
                    "contain (paths with line numbers, exact values, what was not found)."
                ),
            },
        }
        if writer is not None:
            properties["kind"] = {
                "type": "string",
                "enum": [EXPLORE, WRITE],
                "description": (
                    "\"explore\" (the default) only reads; \"write\" changes files in a copy "
                    "of the project and returns a patch."
                ),
            }
        self.spec = ToolSpec(
            name="delegate",
            #: Another model's answer over files and pages that may change.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=describe(together=together, writer=writer is not None, served=served),
            parameters=parameters(properties, required=("task",)),
            aliases={
                "task": ("prompt", "description", "instructions", "question", "query", "goal"),
                "kind": ("mode", "subagent_type"),
            },
            budget_tokens=1200,
            delegates=True,
            core=True,
            summary="Have a subagent work on a task in its own context.",
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        task = arguments.get("task")
        if not isinstance(task, str) or len(task.strip()) < _MIN_TASK_CHARS:
            return ToolResult.error(
                "`task` must say what the subagent should find out or change, in a sentence "
                "or more: the goal, where to look and what to report.",
                "invalid_arguments",
            )
        task = task.strip()
        if len(task) > _MAX_TASK_CHARS:
            return ToolResult.error(
                f"`task` is {len(task):,} characters; keep it under {_MAX_TASK_CHARS:,}. "
                "Name the files or pages to read instead of pasting them: the subagent "
                "reads them itself.",
                "invalid_arguments",
            )
        kind = str(arguments.get("kind") or EXPLORE).strip().lower()
        if kind not in (EXPLORE, WRITE) or (kind == WRITE and self._writer is None):
            known = f"\"{EXPLORE}\" or \"{WRITE}\"" if self._writer else f"\"{EXPLORE}\""
            return ToolResult.error(
                f"`kind` must be {known}; leave it out for a subagent that only reads.",
                "invalid_arguments",
            )
        if context.cancel.cancelled:
            return ToolResult.error("Stopped before the subagent started.", "cancelled")
        runner = self._writer if kind == WRITE else self._runner
        try:
            done = runner(task, context)  # type: ignore[misc]
        except CopyRefused as refused:
            return ToolResult.error(f"The subagent did not start: {refused}", "delegate_refused")
        metadata = {
            "steps": done.steps,
            "input_tokens": done.input_tokens,
            "output_tokens": done.output_tokens,
            "stop_reason": done.stop_reason,
            "child_session": done.session_id,
            "calls": done.calls,
            "subagent_kind": kind,
        }
        if kind == WRITE:
            metadata["patch"] = done.patch
        answer = done.answer.strip()
        changes = _changes(done) if kind == WRITE else ""
        if done.stop_reason == "user_interrupt" or context.cancel.cancelled:
            return ToolResult.error(
                "The subagent was stopped before it answered." + changes, "cancelled", **metadata
            )
        if done.stop_reason == "end_turn" and answer:
            title = "report" if kind == WRITE else "findings"
            return ToolResult(
                content=f"The subagent's {title} ({done.steps} steps):\n\n{answer}{changes}",
                metadata=metadata,
            )
        why = stopped_because(done.stop_reason)
        if done.partial and answer:
            # Not an error: the subagent answered when asked at its limit. The caller is
            # told the answer may leave things out.
            title = "report" if kind == WRITE else "findings"
            return ToolResult(
                content=(
                    f"The subagent did not finish ({why}, after {done.steps} steps) and was "
                    f"asked for what it had so far. Its {title}, which may be incomplete:"
                    f"\n\n{answer}{changes}"
                ),
                metadata={**metadata, "partial": True},
            )
        if answer:
            # An error, so the row says the subagent did not finish; the model still reads
            # what it had found.
            return ToolResult.error(
                f"The subagent did not finish: {why}, after {done.steps} steps. Its last "
                f"report, which may be incomplete:\n\n{answer}{changes}",
                "delegate_incomplete",
                **metadata,
            )
        return ToolResult.error(
            f"The subagent did not finish: {why}, after {done.steps} steps, and gave no "
            f"answer.{changes} Do it yourself, or delegate a narrower task.",
            "delegate_failed",
            **metadata,
        )


def _changes(done: Delegated) -> str:
    """What a writer changed, for the caller: where the patch is and how to apply it."""
    parts: list[str] = []
    if done.patch:
        parts.append(
            f"Its changes are a patch, not yet in the project: {done.patch}\n{done.stat}\n"
            f"Read it, then apply it from the project folder with `git apply --3way "
            f"{done.patch}` (`git apply --check {done.patch}` says first whether it applies)."
        )
    else:
        parts.append("It changed no file the patch would carry; nothing to apply.")
    if done.deleted:
        parts.append(
            "Files it deleted in its copy, which the patch leaves in place (delete them "
            "yourself if they should go): " + ", ".join(done.deleted[:20])
            + (f" and {len(done.deleted) - 20} more" if len(done.deleted) > 20 else "")
        )
    return "\n\n" + "\n\n".join(parts)
