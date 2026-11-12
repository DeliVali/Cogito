"""Fixed points in the loop where internal parts attach.

Not a plugin system and not user-configurable. `cli.py` subscribes the guard (before a
tool runs), recall and the read tracker (on a compaction) and the document index (after a
read), so each attaches the same way and the loop knows none of them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HookPoint(StrEnum):
    BEFORE_SAMPLE = "before_sample"
    AFTER_SAMPLE = "after_sample"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    ON_COMPACT = "on_compact"
    ON_STOP = "on_stop"


@dataclass(frozen=True, slots=True)
class HookDecision:
    """``deny`` turns into an error result the model can read and act on.

    ``result`` (APP ONLY: an approved system package install, `cli._guard_hook`) means the
    hook already did the work: the tool does not run, and ``reason`` is the result the
    model reads. ``payload`` may carry ``is_error``, ``error_kind`` and ``metadata``.
    """

    action: str = "continue"
    reason: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return self.action == "deny"

    @property
    def answered(self) -> bool:
        return self.action == "result"


CONTINUE = HookDecision()

Hook = Callable[[HookPoint, Mapping[str, Any]], HookDecision | None]


class HookBus:
    """First denial wins; observers that return nothing never block anything."""

    def __init__(self) -> None:
        self._hooks: dict[HookPoint, list[Hook]] = {}

    def subscribe(self, point: HookPoint, hook: Hook) -> None:
        self._hooks.setdefault(point, []).append(hook)

    def fire(self, point: HookPoint, **payload: Any) -> HookDecision:
        for hook in self._hooks.get(point, ()):
            decision = hook(point, payload)
            if decision is not None and decision.action != "continue":
                return decision
        return CONTINUE
