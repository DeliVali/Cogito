"""Whether a benchmark holds the GPU right now.

cogito-lab's benchmark processes (`bench suite`, `bench matrix`, `bench extend`) hold an
exclusive ``flock`` on ``$XDG_CACHE_HOME/cogito-lab/bench.lock`` (``~/.cache`` when unset)
while they run, with their PID written in it (cogito-lab ``bench/lock.py``). A model the
window loaded onto the GPU meanwhile would skew what the benchmark measures, so the app
never starts a local model while that lock is held.

On Linux the lock is checked without taking it: ``/proc/locks`` lists every held
``flock`` with its inode. Taking even a brief shared lock could make a benchmark starting
at that instant refuse to run. Elsewhere, a non-blocking shared lock is tried and released
at once. A missing lock file means no lab on this machine, so no holder.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

_PROC_LOCKS = Path("/proc/locks")


def lock_path() -> Path:
    """Where cogito-lab keeps its benchmark lock, resolved as ``bench/lock.py`` resolves it."""
    configured = os.environ.get("XDG_CACHE_HOME")
    root = Path(configured) if configured else Path.home() / ".cache"
    return root / "cogito-lab" / "bench.lock"


@dataclass(frozen=True, slots=True)
class Holder:
    """A benchmark holding the lock: its PID as the lock file records it, or "" if unknown."""

    path: Path
    pid: str


def _held_per_proc(path: Path, proc_locks: Path) -> bool | None:
    """True/False from ``/proc/locks``; None when it cannot be read. Matched by inode alone:
    on btrfs a subvolume's ``st_dev`` is not the device ``/proc/locks`` prints. An inode
    that collides with one on another filesystem can only err toward "held"."""
    try:
        inode = path.stat().st_ino
        text = proc_locks.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        fields = line.split()
        # "1: FLOCK  ADVISORY  WRITE 1234 00:26:7056438 0 EOF"; a blocked waiter is "->".
        if len(fields) < 6 or "->" in fields or "FLOCK" not in fields:
            continue
        where = next((field for field in fields if field.count(":") == 2), "")
        if where.rsplit(":", 1)[-1] == str(inode):
            return True
    return False


def _held_by_trying(path: Path) -> bool:
    import fcntl

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def holder(path: Path | None = None, *, proc_locks: Path = _PROC_LOCKS) -> Holder | None:
    """The benchmark holding the lock, or None when none does."""
    target = path if path is not None else lock_path()
    if not target.exists():
        return None
    held = _held_per_proc(target, proc_locks) if proc_locks.exists() else None
    if held is None:
        held = _held_by_trying(target)
    if not held:
        return None
    try:
        pid = target.read_text(encoding="utf-8").strip()
    except OSError:
        pid = ""
    return Holder(path=target, pid=pid if pid.isdigit() else "")
