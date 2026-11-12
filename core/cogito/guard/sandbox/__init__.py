"""Runs a process that can write only the paths it was given.

Benchmark runs, and later the harness's own validation runs, execute inside bubblewrap:
the filesystem read-only, the paths they need bound writable, the paths they must not see
replaced by an empty tmpfs, and no network. Codex CLI and Claude Code use the same
primitive on Linux. Measured on this machine: about 1 ms per sandbox against 271 ms for a
Docker container (2026-09-23).

The interactive shell does not use it, because full control of the machine is the product.
The sandbox is for runs whose results are measured or whose side effects must be discarded.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

BINARY = "bwrap"

#: Every sandbox starts from these. `available` probes with them too, so its answer holds.
_BASE_MOUNTS = (
    "--ro-bind", "/", "/",
    "--dev", "/dev",
    "--proc", "/proc",
    "--tmpfs", "/tmp",
)


class SandboxUnavailable(RuntimeError):
    """bubblewrap is missing, or this kernel will not give it a user namespace."""


def available() -> tuple[bool, str]:
    """Whether a sandbox can be created here, and if not, why.

    Finding the binary is not enough: Ubuntu's AppArmor blocks unprivileged user
    namespaces by default, and bwrap then exists but fails. So this starts one with the
    same mounts `prefix` uses, since a kernel can allow a bare namespace and refuse /proc.
    """
    path = shutil.which(BINARY)
    if path is None:
        return False, "bwrap not on PATH"
    try:
        probe = subprocess.run(
            [path, *_BASE_MOUNTS, "--unshare-all", "--", "true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if probe.returncode != 0:
        return False, (probe.stderr.strip() or f"exit {probe.returncode}")[:200]
    return True, path


def prefix(
    *,
    writable: Sequence[Path],
    hidden: Sequence[Path] = (),
    network: bool = False,
    read_only: Sequence[Path] = (),
) -> list[str]:
    """The argv that runs whatever follows it inside the sandbox. Ends with ``--``.

    Hidden paths are mounted first and writable paths bound after, so a writable
    directory inside a hidden one is reachable and nothing else there is. ``read_only``
    paths are bound last, over whatever a writable bind exposed. The project's `.cogito/`
    stays readable inside a writable project, and nothing run here can delete, move or
    write it, however the command is spelled. Codex CLI's sandbox does the same for `.git`
    and `.codex` inside a writable root. Each path must exist when the sandbox starts.
    """
    path = shutil.which(BINARY)
    if path is None:
        raise SandboxUnavailable("bwrap not on PATH")
    argv = [path, *_BASE_MOUNTS]
    for directory in hidden:
        argv += ["--tmpfs", str(Path(directory).resolve())]
    for directory in writable:
        resolved = str(Path(directory).resolve())
        argv += ["--bind", resolved, resolved]
    for directory in read_only:
        resolved = str(Path(directory).resolve())
        argv += ["--ro-bind", resolved, resolved]
    argv.append("--unshare-all")
    if network:
        argv.append("--share-net")
    argv += ["--die-with-parent", "--"]
    return argv
