"""The write-ahead journal.

Every event of a turn is appended and fsynced here before anything tries to reach Neo4j.
So a turn survives the database being down, a crash mid-turn loses nothing, and the whole
graph can be rebuilt from the log.

Nothing queries this file. It is a journal, not a store, which keeps the "context lives in
a graph" rule honest. Besides the graph's own replay it has two readers, and both replay
rather than query. `graph.history`, only when Neo4j cannot be reached, replays the events
in memory the way the projections would, so the desktop can still show the project's
thread. `graph.resume`, when the app reopens a project, reads back the session the
conversation continues in: the journal is the only place a message's blocks are kept, and
the only complete record when the app was killed with the graph behind.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cogito.ids import new_id

try:  # POSIX: an advisory lock the journal's own appends never wait on.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    """One durable fact about a session. ``kind`` selects its projection into the graph."""

    kind: str
    data: Mapping[str, Any]
    id: str = field(default_factory=new_id)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_json(self) -> str:
        return json.dumps(
            {"id": self.id, "at": self.at, "kind": self.kind, "data": dict(self.data)},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @classmethod
    def from_json(cls, line: str) -> Event:
        payload = json.loads(line)
        return cls(
            kind=payload["kind"],
            data=payload.get("data", {}),
            id=payload["id"],
            at=payload["at"],
        )


class Journal:
    """Append-only log for one session, with a checkpoint of what reached the graph.

    The checkpoint is a separate file because the log is never rewritten: marking a line as
    projected in place would make the log mutable, and a mutable log cannot be trusted to
    replay.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._checkpoint_path = path.with_suffix(".ckpt")
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: Event) -> Event:
        """Append and fsync. Returns the event so callers can chain.

        If the previous process died mid-write, the file ends without a newline, so that
        torn line is terminated first. Otherwise the new entry would be joined onto the
        broken one and both would be lost when the log is read back.
        """
        payload = event.to_json().encode("utf-8")
        with self._lock, self._path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.write(payload + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read(
        self, *, after_index: int = -1, only: Sequence[bytes] = ()
    ) -> Iterator[tuple[int, Event]]:
        """Yield ``(index, event)`` for entries after ``after_index``.

        ``only``, when given, holds byte strings a line must contain one of to be parsed at
        all (``b'"kind":"message_appended"'``, as `Event.to_json` writes it), so a reader
        that wants a few kinds does not decode every other line. A matching line is still
        parsed and returned whole; the caller checks ``event.kind``.

        A truncated line (a crash mid-write can cut a multi-byte character as well as the
        JSON) is skipped rather than raised, and logged when it is not valid UTF-8: the
        journal exists to recover what is there. The file is read as bytes and decoded a
        line at a time, so one torn character costs only its own line.
        """
        if not self._path.exists():
            return
        with self._path.open("rb") as handle:
            for index, raw in enumerate(handle):
                if index <= after_index:
                    continue
                if only and not any(marker in raw for marker in only):
                    continue
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    log.warning(
                        "%s: line %d is not valid UTF-8 (a torn write); skipped", self._path, index
                    )
                    continue
                if not line:
                    continue
                try:
                    yield index, Event.from_json(line)
                except (json.JSONDecodeError, KeyError):
                    continue

    @property
    def checkpoint(self) -> int:
        """Index of the last entry known to have reached the graph, or -1."""
        try:
            return int(self._checkpoint_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return -1

    def set_checkpoint(self, index: int) -> None:
        temporary = self._checkpoint_path.with_suffix(".ckpt.tmp")
        temporary.write_text(str(index), encoding="utf-8")
        temporary.replace(self._checkpoint_path)

    def pending(self) -> Iterator[tuple[int, Event]]:
        """Entries written to the log that the graph has not acknowledged yet."""
        return self.read(after_index=self.checkpoint)

    def pending_count(self) -> int:
        return sum(1 for _ in self.pending())


class JournalLock:
    """One writer's hold on a journal, for as long as it appends to it.

    A conversation continues in the journal that already holds it (`graph.resume`), so two
    writers could otherwise reach one journal: a core still exiting while the next one opens
    the project (the desktop switches projects by stopping one core and starting another),
    or a `cogito chat` on the same project. Two writers of one session interleave their
    messages under the same seqs. Whoever builds a writer takes this lock first; a journal
    someone else holds is not continued (`cli._OpenConversations.claim`).

    It is `fcntl.flock` on the journal itself: advisory, so it never blocks an append, and
    tied to this open file, so a second holder in the same process is refused too. The OS
    drops it when the process ends, however it ends. Windows has no `fcntl` and its
    byte-range locks are mandatory (they would block the journal's own appends), so there
    this holds nothing and logs that. The gap is two processes on one project at once,
    which the desktop does not do; within one process `cli._OpenConversations` still keeps
    writers apart.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, wait: float = 0.0) -> bool:
        """True once this holds the journal; False when another writer still does after
        ``wait`` seconds."""
        if self._handle is not None:
            return True
        if fcntl is None:  # pragma: no cover - Windows
            log.debug("%s: no journal lock on this platform", self._path)
            self._handle = True
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)
                continue
            self._handle = handle
            return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None or handle is True:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
