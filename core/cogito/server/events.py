"""Per-session event channel.

Thread-safe publish/subscribe using ``queue.Queue``.  The worker thread
writes events; the HTTP side drains them as SSE.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

_SENTINEL = object()


class EventChannel:
    """One FIFO queue guarded by a lock so that close() drains cleanly."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any] | None | object] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event: dict[str, Any]) -> None:
        """Push one event onto the channel."""
        with self._lock:
            if self._closed:
                return
            self._queue.put(event)

    def drain(self, timeout: float = 1.0) -> dict[str, Any] | None | object:
        """Pop the next event, or *None* after *timeout* seconds."""
        try:
            return self._queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        """Mark closed and push a sentinel so SSE streams terminate."""
        with self._lock:
            self._closed = True
            self._queue.put(_SENTINEL)

    @property
    def is_closed(self) -> bool:
        return self._closed


class StreamEvents:
    """Turns the engine's `on_stream` callbacks into ``text_delta`` and ``assistant_text``
    events, joining fragments so a fast model does not send one event per token.

    A fragment waits at most ``interval`` seconds, and only until the next one arrives: a
    slow model's fragments, further apart than that, each go out at once. Everything
    pending goes out before an ``assistant_text``, and before a fragment of another
    sample or kind. Called from the turn's own thread only, so it holds no lock.
    """

    def __init__(
        self,
        emit: Any,
        *,
        interval: float = 0.06,
        clock: Any = time.monotonic,
    ) -> None:
        self._emit = emit
        self._interval = interval
        self._clock = clock
        self._key: tuple[int, str] | None = None
        self._parts: list[str] = []
        self._sent_at = 0.0

    def __call__(self, event: Any) -> None:
        payload = event.as_event()
        if "name" in payload:
            # A tool call's progress (`loop.engine.ToolCallProgress`): already throttled
            # by the provider, so it goes out at once rather than joining the next batch.
            self.flush()
            self._emit({"type": "tool_call_progress", **payload})
            return
        if "kind" not in payload:
            self.flush()
            self._emit({"type": "assistant_text", **payload})
            return
        key = (payload["sample"], payload["kind"])
        if key != self._key:
            self.flush()
            self._key = key
        self._parts.append(payload["text"])
        if self._clock() - self._sent_at >= self._interval:
            self.flush()

    def flush(self) -> None:
        if not self._parts or self._key is None:
            return
        sample, kind = self._key
        text = "".join(self._parts)
        self._parts = []
        self._sent_at = self._clock()
        self._emit({"type": "text_delta", "sample": sample, "kind": kind, "text": text})
