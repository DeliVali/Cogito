"""Release gate for what only a Windows session exercises, run by release.yml on
windows-latest with the packaged core's own interpreter:

    <resources>/core/python/python.exe -I -B desktop/packaging/windows-smoke.py

1. the agent's shell is Git for Windows' bash, runs a command and reports its directory,
   with no stray carriage return in what it reads;
2. the guard's broker answers over loopback TCP: an `rm` it refuses leaves the file, one
   it allows moves the file out of the project;
3. a command that listens on a port is left running in the background, and closing the
   session stops it;
4. a process the core started dies with the core even when the core is killed outright
   (the kill-on-close job).

Prints one line per check and exits 1 on the first failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cogito import procs
from cogito.guard.approval import ApprovalResponse, deny_all
from cogito.guard.policy import Guard
from cogito.guard.shims import ShimBroker
from cogito.guard.undo import UndoStore
from cogito.tools.shell import ShellSession, git_bash


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)
    if not ok:
        raise SystemExit(1)


def session(root: Path, allow: bool) -> tuple[ShellSession, ShimBroker]:
    approver = (lambda request: ApprovalResponse(allowed=True)) if allow else deny_all
    guard = Guard(root, approver=approver)
    broker = ShimBroker(guard, UndoStore(root, "smoke"), interpreter=sys.executable)
    env = {**os.environ, **broker.start()}
    return ShellSession(cwd=root, env=env, waiting=guard.waiting), broker


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="cogito-smoke-"))
    check("Git Bash found", Path(git_bash()).is_file(), git_bash())

    shell, broker = session(root, allow=False)
    out = shell.run("echo hola; printf 'a\\nb\\n' | wc -l; pwd")
    lines = out.text.split()
    check("a command runs", out.exit_code == 0 and lines[:2] == ["hola", "2"], repr(out.text))
    check("no stray carriage return", "\r" not in out.text)

    victim = root / "notas.txt"
    victim.write_text("x")
    refused = shell.run("rm notas.txt")
    refused_ok = refused.exit_code != 0 and victim.exists()
    check("the guard refuses over TCP", refused_ok, refused.text.strip())
    shell.close()
    broker.stop()

    shell, broker = session(root, allow=True)
    allowed = shell.run("rm notas.txt")
    allowed_ok = allowed.exit_code == 0 and not victim.exists()
    check("the guard allows over TCP", allowed_ok, allowed.text.strip())

    python = Path(sys.executable).as_posix()
    server = shell.run(f'"{python}" -m http.server 0 --bind 127.0.0.1', timeout=60)
    pid = server.background_pid
    running = pid is not None and procs.alive(pid)
    check("a server is left running", running, f"pid {pid} ports {server.ports}")
    shell.close()
    broker.stop()
    check("closing the session stops it", procs.wait_gone(pid, 10))

    # A child core starts a long command, then is killed outright: its job must end it.
    marker = root / "child.pid"
    script = (
        "import sys, threading, time\n"
        "from pathlib import Path\n"
        "from cogito.tools.shell import ShellSession\n"
        f"s = ShellSession(Path(r'{root}'))\n"
        "command = 'echo $$ > child.pid; exec sleep 300'\n"
        "threading.Thread(target=s.run, args=(command, 600), daemon=True).start()\n"
        "time.sleep(120)\n"
    )
    core = subprocess.Popen([sys.executable, "-I", "-B", "-c", script])
    deadline = time.monotonic() + 30
    while not (marker.exists() and marker.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.2)
    check("the child shell started", marker.exists())
    shell_pid = next(iter(procs.children(core.pid)), None)
    check("the core has its shell", shell_pid is not None)
    subprocess.run(["taskkill", "/PID", str(core.pid), "/F"], capture_output=True, check=False)
    check("the shell died with its core", procs.wait_gone(shell_pid, 10), f"pid {shell_pid}")


if __name__ == "__main__":
    main()
