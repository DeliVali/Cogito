"""A recording's samples, through the person's ffmpeg.

ffmpeg reads the file and writes mono 16-bit samples at the analysis rate to a temporary
file. Only local files are opened (`-protocol_whitelist file`), so a playlist that names
an address fetches nothing. ffmpeg is the person's install: Cogito ships none.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cogito import procs
from cogito.cancel import CancelToken

#: What ffmpeg is asked to read. A file of another kind is refused before it is opened.
SUFFIXES = (
    ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma", ".aiff",
    ".aif", ".mp4", ".webm", ".mkv",
)
#: The longest stretch studied in one call: past it the tables no longer fit a result.
MAX_SECONDS = 480.0
DECODE_SECONDS = 120.0
_TAIL = 600


@dataclass(frozen=True, slots=True)
class Decoded:
    #: 16-bit mono samples, little-endian. Empty when ffmpeg failed.
    samples: bytes
    #: 0 done, -1 stopped by the person, -2 timed out, else ffmpeg's status.
    status: int
    output: str


def find(env: dict[str, str] | None = None) -> str:
    """The `ffmpeg` program on PATH, or ""."""
    return shutil.which("ffmpeg", path=(env or os.environ).get("PATH")) or ""


def decode(
    ffmpeg: str, path: Path, rate: int, *, start: float, seconds: float,
    cancel: CancelToken | None = None, timeout: float = DECODE_SECONDS,
) -> Decoded:
    """``seconds`` of ``path`` from ``start``, as mono samples at ``rate``."""
    argv = [
        ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file",
        "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(path),
        "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1",
    ]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as log:
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=out, stderr=log, start_new_session=True
            )
        except OSError as exc:
            return Decoded(b"", 127, str(exc))
        waited = 0.0
        while True:
            try:
                status = process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                waited += 0.2
            stopped = cancel is not None and cancel.cancelled
            if stopped or waited >= timeout:
                _stop(process)
                status = -1 if stopped else -2
                break
        log.seek(0)
        text = log.read().decode("utf-8", "replace").strip()[-_TAIL:]
        if status != 0:
            return Decoded(b"", status, text)
        out.seek(0)
        return Decoded(out.read(), 0, text)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if os.name != "posix":
        procs.terminate_trees([process.pid], grace=2.0)
        return
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signal_number)
        except OSError:
            return
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            continue
