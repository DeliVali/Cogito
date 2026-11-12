"""An exclusive lock on an open file, on POSIX and on Windows.

POSIX uses `fcntl.flock`, as every caller did before this module existed, so the lock is
tied to the open file and the kernel releases it when the file is closed or the process
dies. Windows has no `flock`; `msvcrt.locking` locks a byte range of the open file, which
Windows also releases when the handle closes or the process ends. One byte at offset 0 is
locked, beyond the end of an empty file if need be, which Windows allows.
"""

from __future__ import annotations

import os
import time
from typing import IO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

#: How often a blocking lock on Windows tries again. `msvcrt.LK_LOCK` itself retries for
#: about 10 s and then raises, so the wait is done here, without that limit.
_RETRY_SECONDS = 0.05


def lock(handle: IO[str] | IO[bytes], *, blocking: bool = True) -> bool:
    """Take the exclusive lock on ``handle``. True when held; False only when
    ``blocking`` is false and another process holds it."""
    if os.name != "nt":
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except OSError:
            if blocking:
                raise
            return False
        return True
    while True:
        try:
            _at_start(handle, msvcrt.LK_NBLCK)
            return True
        except OSError:
            if not blocking:
                return False
            time.sleep(_RETRY_SECONDS)


def unlock(handle: IO[str] | IO[bytes]) -> None:
    if os.name != "nt":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    _at_start(handle, msvcrt.LK_UNLCK)


def _at_start(handle: IO[str] | IO[bytes], mode: int) -> None:
    """`msvcrt.locking` acts at the current position; the lock is always byte 0, and the
    position the caller had is put back."""
    fd = handle.fileno()
    position = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, mode, 1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)
