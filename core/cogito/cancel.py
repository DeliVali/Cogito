"""Cooperative cancellation, shared by every layer that can block.

Interrupting a run must not kill the session: the loop drains, records what happened, and
carries on with whatever the user said next. So cancellation is a token the code checks,
not an exception raised from outside.
"""

from __future__ import annotations

import threading


class CancelToken:
    """Set once by the user; checked at every phase boundary and inside blocking work."""

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""

    def cancel(self, reason: str = "user interrupted") -> None:
        self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout``, returning early and True if cancelled meanwhile."""
        return self._event.wait(timeout)

    def reset(self) -> None:
        """Clear the token so the same session can accept the next turn."""
        self._event.clear()
        self._reason = ""


#: A token that is never cancelled, for call sites that do not care.
NEVER = CancelToken()
