"""Guard questions waiting for a yes or no from the window.

The guard asks on the thread running the turn. The answer arrives later on another
thread, as an HTTP request from the window. The turn blocks here and the route answers
here.

It only moves an identifier and a boolean and knows nothing of what is being approved.
That keeps it in ``server/``, which may import ``graph`` and ``loop`` only; the request's
vocabulary belongs to ``guard``, and the composition root translates between them.

The default is refusal. An unanswered question is denied, a closing window denies all it
held, and an interrupted turn denies instead of waiting. An agent must never delete
because the person walked away.

The app waits until the person answers (``timeout=None``): a card that resolved itself
after five minutes read «Omitiste» to someone who had only been slow to answer (the
person, 2026-10-31: "no debería salir omitiste cuando tardó en contestar"). «Detener» and
a closing window still end the wait, and nothing runs while it lasts.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from cogito.ids import new_id

#: How long a question waits before it is refused. Enough to read a command, short enough
#: that a forgotten window does not hold a turn open forever.
DEFAULT_TIMEOUT_SECONDS = 300.0

#: How often the wait checks for an interrupt: Stop feels immediate without spinning.
_POLL_SECONDS = 0.2


class Approvals:
    """Pending questions, by id. One instance per application, shared by every session."""

    def __init__(self) -> None:
        self._answers: dict[str, bool | None] = {}
        #: "session" when the person allowed this for the rest of the session.
        self._scopes: dict[str, str] = {}
        self._signals: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def open(self) -> str:
        """Register a question and return the id the window will answer with."""
        identifier = new_id()
        with self._lock:
            self._answers[identifier] = None
            self._signals[identifier] = threading.Event()
        return identifier

    def answer(self, identifier: str, allowed: bool, scope: str = "") -> bool:
        """Record a decision. False when there was no such question still waiting."""
        with self._lock:
            signal = self._signals.get(identifier)
            if signal is None or signal.is_set():
                return False
            self._answers[identifier] = allowed
            if allowed and scope:
                self._scopes[identifier] = scope
            signal.set()
            return True

    def resolve(
        self,
        identifier: str,
        *,
        timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        abort: Callable[[], bool] | None = None,
    ) -> bool | None:
        """Block until the question is answered, abandoned or out of time (never out of time
        with ``timeout=None``).

        Returns the decision, or None when nobody made one. The caller must treat None as
        a refusal, not an error. ``abort`` is polled so an interrupted turn stops waiting.
        """
        with self._lock:
            signal = self._signals.get(identifier)
        if signal is None:
            return None

        remaining = timeout
        while remaining is None or remaining > 0:
            if signal.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining)):
                break
            if abort is not None and abort():
                break
            if remaining is not None:
                remaining -= _POLL_SECONDS

        with self._lock:
            answer = self._answers.pop(identifier, None)
            self._signals.pop(identifier, None)
        return answer

    def scope(self, identifier: str) -> str:
        """How far an allowed answer reaches ("session"), once, after `resolve`."""
        with self._lock:
            return self._scopes.pop(identifier, "")

    def abandon_all(self) -> None:
        """Release every waiting question as unanswered. For a session going away."""
        with self._lock:
            for identifier, signal in self._signals.items():
                self._answers[identifier] = None
                signal.set()
