"""`todo`: the model's own task list for work of several steps (APP-ONLY).

The model writes the whole list each time, with each item pending, in progress or done,
and the result shows it back, so the list is in the conversation where the model last left
it and nothing is added to the system message or the status (the prompt prefix stays the
one the server holds). The loop sends a turn back once when it would end with items still
open (`Engine._maybe_continue_open_tasks`), and a fold carries the list into the seed.

Claude Code gives its model the same kind of list (TodoWrite, and the Task tools that
replace it: code.claude.com/docs/en/agent-sdk/todo-tracking.md, read 2026-11-10), and
Cursor's agent keeps one too (docs.cursor.com/agent/planning). No controlled measurement of
what such a list is worth was found, so here it is a hypothesis, and its measurement is
owed (CONTRIBUTING §7).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters

STATUSES = ("pending", "in_progress", "done")
_MARKS = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]"}
#: Status words a model may use for the three.
_STATUS_ALIASES = {
    "todo": "pending", "open": "pending", "not_started": "pending", "pendiente": "pending",
    "doing": "in_progress", "active": "in_progress", "in progress": "in_progress",
    "started": "in_progress", "en_progreso": "in_progress", "en progreso": "in_progress",
    "completed": "done", "complete": "done", "finished": "done", "hecho": "done",
    "cancelled": "done", "canceled": "done", "dropped": "done",
}
_MAX_ITEMS = 20
_MAX_TEXT = 200

_DESCRIPTION = (
    "Keep a task list for work of three or more steps, so nothing is left half done. Send "
    "the whole list every time: each item's text and its status (pending, in_progress, "
    "done). Mark one item in_progress before you start it and done as soon as it is "
    "finished; drop an item that is no longer needed by marking it done. The person sees "
    "the list. Skip it for a question or a one-step change."
)


@dataclass(frozen=True, slots=True)
class Item:
    text: str
    status: str


class TodoList:
    """One session's list, as the model last wrote it."""

    def __init__(self) -> None:
        self.items: tuple[Item, ...] = ()

    def open_items(self) -> tuple[Item, ...]:
        return tuple(item for item in self.items if item.status != "done")

    def render(self) -> str:
        if not self.items:
            return ""
        done = sum(1 for item in self.items if item.status == "done")
        lines = [f"Task list ({done} of {len(self.items)} done):"]
        lines += [f"{_MARKS[item.status]} {item.text}" for item in self.items]
        return "\n".join(lines)

    def clear(self) -> None:
        self.items = ()


def _item(raw: Any) -> Item | str:
    """An item from what the model sent, or why it cannot be one."""
    if isinstance(raw, str):
        text, status = raw, "pending"
    elif isinstance(raw, Mapping):
        text = raw.get("text") or raw.get("content") or raw.get("task") or raw.get("title") or ""
        status = raw.get("status") or raw.get("state") or "pending"
    else:
        return "each item is an object with `text` and `status`"
    text = " ".join(str(text).split())
    if not text:
        return "an item has no `text`"
    status = str(status).strip().lower()
    status = _STATUS_ALIASES.get(status, status.replace(" ", "_"))
    if status not in STATUSES:
        return f"status {status!r} is not one of pending, in_progress, done"
    return Item(text[:_MAX_TEXT], status)


class TodoTool:
    def __init__(
        self, todos: TodoList, record: Callable[..., Any] | None = None
    ) -> None:
        self.todos = todos
        self._record = record
        self.spec = ToolSpec(
            name="todo",
            description=_DESCRIPTION,
            parameters=parameters(
                {
                    "items": {
                        "type": "array",
                        "description": "The whole list, in order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "What to do."},
                                "status": {"type": "string", "enum": list(STATUSES)},
                            },
                            "required": ["text", "status"],
                        },
                    }
                },
                ("items",),
            ),
            aliases={"items": ("todos", "tasks", "list", "todo")},
            budget_tokens=400,
            summary="keep a task list for work of several steps",
            # The list as the model wrote it: the next call replaces it.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.ACKNOWLEDGEMENT,
                shape=PayloadShape.COMPACT,
            ),
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw = arguments.get("items")
        if not isinstance(raw, list) or not raw:
            return ToolResult.error(
                "todo needs `items`: the whole list, each with `text` and `status` "
                "(pending, in_progress, done).",
                "bad_arguments",
            )
        if len(raw) > _MAX_ITEMS:
            return ToolResult.error(
                f"At most {_MAX_ITEMS} items; group the small ones.", "bad_arguments"
            )
        items = []
        for entry in raw:
            item = _item(entry)
            if isinstance(item, str):
                return ToolResult.error(f"todo: {item}.", "bad_arguments")
            items.append(item)
        self.todos.items = tuple(items)
        if self._record is not None:
            self._record(
                "todo_set",
                session_id=context.session_id,
                items=[{"text": item.text, "status": item.status} for item in items],
            )
        working = [item for item in items if item.status == "in_progress"]
        note = ""
        if len(working) > 1:
            note = "\nWork on one item at a time: keep a single item in_progress."
        elif not working and self.todos.open_items():
            note = "\nMark the item you start next in_progress."
        return ToolResult(
            content=self.todos.render() + note,
            metadata={
                "items": len(items),
                "done": sum(1 for item in items if item.status == "done"),
            },
        )
