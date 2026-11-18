"""Running LMMS: rendering a project to audio, and opening it for the person.

`lmms render PROJECT -f FORMAT -o FILE` renders without a window (LMMS 1.2.2
src/core/main.cpp builds a `QCoreApplication` for it), so it needs no display. Opening is
the program itself with the project as its argument, left running in a session of its own.

LMMS is the person's install: Cogito neither ships nor downloads it. Where it is missing
the tool says how it is installed, and the project file is still written.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cogito import procs
from cogito.cancel import CancelToken

#: What `lmms render -f` takes in LMMS 1.2.2, tried on 2026-11-18. It refuses flac
#: ("Invalid output format flac").
FORMATS = ("wav", "ogg", "mp3")
#: A render is slower than the song for nothing this tool writes; ten minutes is a song
#: of well over that at any tempo, and a stuck render must end.
RENDER_SECONDS = 600.0
#: How long the program is watched after it is started: one that fails does so at once.
_OPEN_WAIT = 3.0
_TAIL = 800

MISSING = (
    "LMMS is not installed, so the project cannot be rendered or opened here. It was not "
    "installed for the person: tell them to install it (Arch: `sudo pacman -S lmms "
    "soundfont-fluid`; Debian/Ubuntu: `sudo apt install lmms fluid-soundfont-gm`; "
    "Fedora: `sudo dnf install lmms fluid-soundfont-gm`; or lmms.io), then call again."
)


@dataclass(frozen=True, slots=True)
class Ran:
    #: None while the program is still running (an opened window).
    status: int | None
    output: str


#: Starts ``argv`` and waits up to ``timeout`` seconds; ``detach`` leaves it running.
Runner = Callable[..., Ran]


def find(env: dict[str, str] | None = None) -> str:
    """The `lmms` program on PATH, or ""."""
    return shutil.which("lmms", path=(env or os.environ).get("PATH")) or ""


def run(
    argv: Sequence[str],
    *,
    timeout: float,
    cancel: CancelToken | None = None,
    detach: bool = False,
) -> Ran:
    """Run ``argv``. A render is waited for and stopped with its children on a timeout or
    «Detener»; an opened window is watched for a moment and left running."""
    with tempfile.TemporaryFile() as log:
        try:
            process = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return Ran(127, str(exc))
        waited = 0.0
        status: int | None = None
        while True:
            try:
                status = process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                waited += 0.2
            if detach and waited >= timeout:
                break
            if (cancel is not None and cancel.cancelled) or (not detach and waited >= timeout):
                _stop(process)
                status = -1 if cancel is not None and cancel.cancelled else -2
                break
        log.seek(0)
        text = log.read().decode("utf-8", "replace").strip()
    return Ran(status, text[-_TAIL:])


def _stop(process: subprocess.Popen[bytes]) -> None:
    """End the program and what it started. It leads a session of its own, so on POSIX
    its process group is all of it."""
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


def render_command(lmms: str, project: Path, output: Path, fmt: str) -> list[str]:
    return [lmms, "render", str(project), "-f", fmt, "-o", str(output)]


def open_command(lmms: str, project: Path) -> list[str]:
    return [lmms, str(project)]
