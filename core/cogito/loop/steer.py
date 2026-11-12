"""Messages sent while a turn is running: steering the agent without stopping it.

Enter never stops anything here. A message typed during a turn joins that turn once every
tool call of the current step has finished, just before the model is sampled again. It is
never spliced between two tool calls of one step, which the OpenAI wire dialect rejects
(see `loop.engine.Engine.run`'s comment). Only the turn's own `CancelToken` stops anything,
and this module never touches it: `loop.engine.Engine._apply_steers` only appends a
message.

`SteerQueue` is owned by `Engine`, not `RunState`, on purpose. `RunState` is built fresh in
`server.app._run_turn` for each `POST /turns` and is never visible outside it, while the
`POST /sessions/{id}/steer` route only holds `sessions[sid]["engine"]`, an
`Engine`/`EngineSession` that outlives any one turn. A queue on `RunState` would need the
current `RunState` passed out to the server on every turn start (the coupling the
`Engine`/`RunState` split avoids), or would lose a steer that arrives between one turn
ending and the next `RunState` being built. On `Engine` it exists as soon as the session
does, across every turn.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class QueuedSteer:
    """One message, waiting to be spliced into the transcript at the next boundary."""

    id: str
    text: str
    #: What the person attached to it (`context.attachments.Attachment`), joined to the
    #: words when the steer is applied; empty for a message without attachments.
    attachments: tuple[Any, ...] = ()


class SteerQueue:
    """FIFO of pending steer messages. `push` is safe from any thread. `drain` never
    blocks: it takes whatever is queued right now, in order, and returns at once when there
    is nothing, so the engine can check it at a boundary and move on.

    Not `queue.Queue`: the engine never waits for the next item, it asks whether anything
    is there now, and `queue.Queue` has no atomic "take everything" operation.
    """

    __slots__ = ("_items", "_lock")

    def __init__(self) -> None:
        self._items: deque[QueuedSteer] = deque()
        self._lock = threading.Lock()

    def push(self, item: QueuedSteer) -> None:
        with self._lock:
            self._items.append(item)

    def drain(self) -> list[QueuedSteer]:
        """Remove and return everything queued right now, oldest first.

        Called at every boundary the engine checks, and the queue is almost always empty,
        so it must be cheap (one lock, one length check) and free of side effects: when it
        returns `[]`, nothing a turn sends to the model may change. See
        `test_engine_steer.py`'s byte-identity case.
        """
        with self._lock:
            if not self._items:
                return []
            items = list(self._items)
            self._items.clear()
            return items

    def peek(self) -> tuple[QueuedSteer, ...]:
        """What is queued right now, without removing it.

        `Engine` does not use it: a peek let a steer be reported dropped on a crash while it
        stayed queued and was applied again to the next turn (review: app-b2-steer,
        blocker 1), so `Engine.drain_leftover_steers` drains instead. Kept for callers that
        only look, such as a test checking the queue's contents.
        """
        with self._lock:
            return tuple(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
