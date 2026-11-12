"""A monotonic number for every event in a conversation, kept across processes.

Order ("the first command you ran") comes from a counter, not the clock: two results in the
same second share an `observed_at`, and a clock that steps backwards breaks order outright.

The counter cannot live only in memory. A fold opens a new session, a restart a new
process, a resumed conversation both, and a count that starts again at one in any of them
makes "earlier" meaningless across the boundary. So the high-water mark is on disk beside
the journal it orders, and is read back on start.

Durability follows the journal: write, flush, fsync, then hand the number out. That is one
fsync per event, the same cost the WAL already pays.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from cogito import oslocks

log = logging.getLogger(__name__)

#: Reserved for "no event". Real sequences start at one, so a falsy value is never a real
#: position.
UNSET = 0


class EventSequence:
    """Hands out strictly increasing numbers and remembers where it got to.

    One instance per conversation. `next()` is atomic across threads and durable if the
    process ends between two calls.
    """

    def __init__(self, path: Path, *, floor: Callable[[], int] | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        #: Where the count starts when the mark cannot be read: the highest position
        #: already recorded, from the journal or the graph. Without it an unreadable mark
        #: would restart the count and reuse numbers that already name other events.
        self._floor = floor or (lambda: UNSET)
        self._current = self._recover()

    @property
    def current(self) -> int:
        """The last number handed out, or UNSET if none has been."""
        return self._current

    def _recover(self) -> int:
        """The high-water mark, never lower than what is already recorded.

        A truncated or corrupt mark must not restart the count. That would reuse numbers
        that already name other events, so "first" would match two things and order would
        run backwards, which is worse than a crash. The recorded events are always the
        floor, and the mark can only raise it.
        """
        floor = self._floor_or_zero()
        mark = UNSET
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
            mark = int(raw)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning(
                "event sequence mark at %s is unreadable; falling back to the highest "
                "recorded position (%d)",
                self._path,
                floor,
            )
        return max(mark, floor, UNSET)

    def _floor_or_zero(self) -> int:
        """What is already recorded. A source that cannot answer must not lower the floor."""
        try:
            return max(int(self._floor()), UNSET)
        except Exception:  # noqa: BLE001 - any failing source is simply no floor
            log.warning("could not read the recorded high-water mark for %s", self._path)
            return UNSET

    def next(self) -> int:
        """The next position, persisted before it is returned.

        The lock guards against other threads and a file lock against other processes.
        `fsync` gives durability only: without the file lock, two processes writing the
        mark a microsecond apart would read the same value and both hand it out, durably
        and wrongly.
        """
        with self._lock:
            nxt = self._locked_increment()
            self._current = nxt
            return nxt

    def _locked_increment(self) -> int:
        """Read, add one, write, with the file locked exclusively throughout."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a+", encoding="utf-8") as handle:
            oslocks.lock(handle)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                try:
                    on_disk = int(raw)
                except ValueError:
                    on_disk = self._floor_or_zero()
                nxt = max(on_disk, self._current, self._floor_or_zero()) + 1
                handle.seek(0)
                handle.truncate()
                handle.write(f"{nxt}\n")
                handle.flush()
                os.fsync(handle.fileno())
                return nxt
            finally:
                oslocks.unlock(handle)

    def observe(self, sequence: int) -> None:
        """Take another source's position into account, never going backwards.

        Used when a conversation resumes and the graph knows events this counter's file
        does not (a copied working directory, a rebuilt journal).
        """
        with self._lock:
            if sequence > self._current:
                self._persist(sequence)
                self._current = sequence

    def _persist(self, value: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Written in place, not through a temporary file: the value is one short line, and
        # a torn write reads back as unreadable, which falls back to the recorded floor
        # (`_recover`, `_locked_increment`).
        with self._path.open("w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")
            handle.flush()
            os.fsync(handle.fileno())
