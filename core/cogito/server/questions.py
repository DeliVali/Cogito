"""Questions that wait on a word from the window, such as a permission card's answer.

Works like ``approvals.Approvals`` (the asking thread blocks, an HTTP request from the
window answers), but the answer is one of a few words ("allow_session", "deny",
"install_and_allow") instead of a boolean. It is a separate class so the guard's yes/no
stays unchanged.

The default is refusal: an unanswered question resolves to None, which every caller
treats as "no", and a closing window abandons whatever it held.

A permission question gives up after `DEFAULT_TIMEOUT_SECONDS`. A caller may wait until
answered instead (``timeout=None``). The pause at 100 % of COGITO's own budget
(`provider/budget.py`) does this, since design 2c keeps that card open until it is
answered. Nothing is in flight while it waits, and an abort («Detener») or `abandon_all`
still ends it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from cogito.ids import new_id

#: Long enough to read the card; short enough that a forgotten window frees the turn.
DEFAULT_TIMEOUT_SECONDS = 300.0
_POLL_SECONDS = 0.2


class Questions:
    """Pending questions, by id. One instance per application."""

    def __init__(self) -> None:
        self._answers: dict[str, str | None] = {}
        self._signals: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def open(self) -> str:
        identifier = new_id()
        with self._lock:
            self._answers[identifier] = None
            self._signals[identifier] = threading.Event()
        return identifier

    def answer(self, identifier: str, value: str) -> bool:
        """Record an answer. False when no such question is still waiting."""
        with self._lock:
            signal = self._signals.get(identifier)
            if signal is None or signal.is_set():
                return False
            self._answers[identifier] = value
            signal.set()
            return True

    def resolve(
        self,
        identifier: str,
        *,
        timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
        abort: Callable[[], bool] | None = None,
    ) -> str | None:
        """Block until answered, abandoned, aborted or out of time (never out of time with
        ``timeout=None``). None means nobody answered, and the caller must treat it as a
        refusal."""
        with self._lock:
            signal = self._signals.get(identifier)
        if signal is None:
            return None
        remaining = timeout
        while remaining is None or remaining > 0:
            wait = _POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining)
            if signal.wait(wait):
                break
            if abort is not None and abort():
                break
            if remaining is not None:
                remaining -= _POLL_SECONDS
        with self._lock:
            answer = self._answers.pop(identifier, None)
            self._signals.pop(identifier, None)
        return answer

    def abandon_all(self) -> None:
        with self._lock:
            for identifier, signal in self._signals.items():
                self._answers[identifier] = None
                signal.set()
