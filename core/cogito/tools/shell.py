"""A persistent shell.

One `bash` per session, driven over pipes. Persistence is the point: `cd`, an activated
virtualenv and exported variables all hold between commands. Without it the model rebuilds
its environment on every step and pays for that in tokens.

Exit codes come from a sentinel printed after each command, not from a new process per
command, so there is no pty guesswork. The sentinel also carries back the working
directory, so the model never has to ask where it is.

This is bash on purpose, though the user's shell is fish: the model's training data is
bash, and no human reads this shell.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from cogito import procs
from cogito.cancel import CancelToken
from cogito.context.budgets import collapse_repeats
from cogito.errors import CogitoError
from cogito.guard.approval import Waiting
from cogito.provider.base import (
    PayloadShape,
    Provenance,
    Reproducibility,
    Retention,
    ToolSpec,
)
from cogito.tools.base import ToolContext, ToolResult, parameters

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
_GRACE_SECONDS = 2.0
_TIMEOUT_EXIT = 124
#: How long a command runs before the shell looks for a port it listens on. Coder's agent
#: tells its model that dev servers and watchers running longer than 5 s belong in the
#: background (coderd/chattool/execute.go, read 2026-10-17); this is that threshold, with
#: the decision taken by the harness from a fact (a listening socket), not by the model.
_DETACH_AFTER = 5.0
_DETACH_CHECK_EVERY = 1.0
#: Lines of a background command's output kept in memory, for `close` and for a later read.
_BACKGROUND_TAIL = 200

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r(?!\n)")
_SENTINEL_PREFIX = "__COGITO_EOC_"


@dataclass(frozen=True, slots=True)
class ShellOutput:
    text: str
    exit_code: int
    cwd: str
    timed_out: bool = False
    #: Stopped because the person pressed stop, not because it finished.
    cancelled: bool = False
    #: Still running, left in the background because it listens on a port (a server).
    background_pid: int | None = None
    ports: tuple[int, ...] = ()
    #: Left in the background at its timeout instead of being killed (`background_at_timeout`):
    #: the session reports when it ends (`on_exit`).
    moved: bool = False


@dataclass
class BackgroundJob:
    """A command left running: the bash that runs it, and the tail of what it prints."""

    process: subprocess.Popen[str]
    command: str
    pid: int
    ports: tuple[int, ...]
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=_BACKGROUND_TAIL))
    #: Its end is reported (`on_exit`): a command moved at its timeout. A server the model
    #: was told to stop itself is not.
    report: bool = False


def _stop_hint(pid: int) -> str:
    """How to stop a command moved to the background. Its pid is the shell that runs it,
    which leads its own process group (`start_new_session`): `kill PID` would stop only the
    shell and leave the command running, so the whole group is named."""
    if procs.IS_WINDOWS:
        return f"Stop it with `taskkill /T /F /PID {pid}`."
    return f"Stop it, and what it started, with `kill -- -{pid}`."


#: What the guard's PATH shims stand in for (`guard.shims.SHIMMED`; tools may not import
#: guard's internals at this point, so the list is repeated and a lab test compares them).
_SHIMMED_VERBS = ("rm", "rmdir", "shred", "srm", "unlink", "truncate")


def _restore_shims(env: Mapping[str, str]) -> str:
    """The line that puts the guard's shims back in front before a command, or "" where
    the shell has none (no broker socket in its environment, or Windows)."""
    if procs.IS_WINDOWS or "COGITO_GUARD_SOCKET" not in env:
        return ""
    folder = env.get("PATH", "").split(os.pathsep)[0]
    if not folder or not os.path.isfile(os.path.join(folder, "rm")):
        return ""
    quoted = shlex.quote(folder)
    verbs = " ".join(_SHIMMED_VERBS)
    # Appended to the line that closes each command, so it adds no line of its own;
    # `|| :` and `${PATH-}` so a command's own `set -eu` cannot end the shell here.
    return (
        f"__cogito_path=${{PATH-}}; "
        f"PATH={quoted}${{__cogito_path:+:${{__cogito_path//{quoted}:/}}}}; "
        f"hash -r; unalias {verbs} 2>/dev/null || :; unset -f {verbs} 2>/dev/null || :"
    )


class ShellSession:
    """A live bash process. Created once per agent session, reused for every command."""

    def __init__(
        self,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        *,
        waiting: Waiting | None = None,
        wrapper: Sequence[str] = (),
        registry: Path | None = None,
        background_at_timeout: bool = False,
        on_exit: Callable[[BackgroundJob, int | None], None] | None = None,
    ) -> None:
        # A deletion inside this shell reaches a PATH shim, which asks a person and blocks
        # until they answer. That wait is not the command hanging, so it does not count
        # against the command's deadline.
        self._waiting = waiting or Waiting()
        #: An argv prefix the shell runs under: a sandbox for benchmark runs, empty
        #: otherwise. The interactive shell is never wrapped.
        self._wrapper = tuple(wrapper)
        self._nonce = secrets.token_hex(8)
        self._marker = f"{_SENTINEL_PREFIX}{self._nonce}__"
        self._cwd = cwd
        self._env = dict(env or os.environ)
        #: Put back before every command: the guard's shims first on PATH, and no hash,
        #: alias or function standing in for what they shadow. Without it, an
        #: `export PATH=/usr/bin:$PATH` or `hash -p /usr/bin/rm rm` in one command sent a
        #: later plain `rm` to the real binary while the guard left it to the shim, and
        #: nobody was asked (review of 2026-11-03).
        self._restore_shims = _restore_shims(self._env)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._queue: Queue[str | None] = Queue()
        self._reader: threading.Thread | None = None
        self._background: list[BackgroundJob] = []
        #: Where each shell this session runs is recorded while it lives (`reap_orphans`).
        self._registry = registry
        #: APP-ONLY. A command still running at its timeout is left running in the
        #: background instead of being killed, and `on_exit` hears its end (with its exit
        #: code, or None when it was killed with its shell), on the thread that drains it, so
        #: it must not block. Off, a timed-out command is killed, as in `chat` and every
        #: benchmark.
        self._background_at_timeout = background_at_timeout
        self._on_exit = on_exit
        #: Set by `close`: commands it stops are not reported as having ended.
        self._closing = False
        #: Why no shell could be started (Windows without Git Bash): every command then
        #: answers with this, and the rest of the session works.
        self._unavailable = ""
        try:
            self._start()
        except CogitoError as exc:
            self._unavailable = str(exc)

    def _start(self) -> None:
        if procs.IS_WINDOWS:
            # Git Bash, as Claude Code uses on Windows (code.claude.com/docs/en/setup,
            # read 2026-10-24): the model's commands are bash, and the guard's shims are sh.
            # A job of this process's own holds it, so it ends with this process.
            self._process = subprocess.Popen(
                [git_bash(), "--norc", "--noprofile"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self._cwd,
                env={**self._env, "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat"},
                creationflags=procs.NEW_GROUP,
            )
            # Text mode would write each "\n" as "\r\n" on Windows, and bash would read a
            # stray "\r" at the end of every command. The pipes are wrapped here instead:
            # "\n" goes out as it is, and what comes back is read with universal newlines.
            assert self._process.stdin is not None and self._process.stdout is not None
            self._process.stdin = io.TextIOWrapper(
                self._process.stdin, encoding="utf-8", newline="\n", write_through=True
            )
            self._process.stdout = io.TextIOWrapper(
                self._process.stdout, encoding="utf-8", errors="replace"
            )
            procs.bind_to_this_process(self._process)
        else:
            self._process = subprocess.Popen(
                [*self._wrapper, "bash", "--norc", "--noprofile"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self._cwd,
                env={**self._env, "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat"},
                # As on Windows above: a byte that is not UTF-8 (binary, Latin-1) is read as
                # U+FFFD. Strict decoding killed the reader on it and the command timed out.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        self._note(self._process.pid)
        self._queue = Queue()
        # The process and queue are passed in, not read from `self`: a shell left running in
        # the background keeps its own pump, which must not feed the next shell's queue.
        self._reader = threading.Thread(
            target=self._pump, args=(self._process, self._queue), daemon=True,
            name="cogito-shell",
        )
        self._reader.start()

    @staticmethod
    def _pump(process: subprocess.Popen[str], queue: Queue[str | None]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                queue.put(line)
        finally:
            # A reader that stops for any reason still says so, or the running command
            # would wait out its whole timeout for output that never comes.
            queue.put(None)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def cwd(self) -> Path:
        """Where the shell currently is. Updated from the sentinel after every command."""
        return self._cwd

    def _bash_pid(self) -> int | None:
        """The bash process itself, which a wrapper puts one or two processes down.

        bubblewrap keeps a monitor outside the namespace and, with a PID namespace, an
        init inside it; bash is the single child at the end of that chain. Without a
        wrapper the process started is bash.
        """
        if self._process is None:
            return None
        pid = self._process.pid
        if procs.IS_WINDOWS:
            return pid  # started directly, never wrapped
        for _ in range(4):
            try:
                if Path(f"/proc/{pid}/comm").read_text().strip() == "bash":
                    return pid
            except OSError:
                return None
            children = self._children_of(pid)
            if len(children) != 1:
                return None
            pid = children[0]
        return None

    @staticmethod
    def _children_of(pid: int) -> list[int]:
        if procs.IS_WINDOWS:
            return procs.children(pid)
        children: list[int] = []
        try:
            for task in Path(f"/proc/{pid}/task").iterdir():
                children.extend(int(p) for p in (task / "children").read_text().split())
        except OSError:
            return []
        return children

    def _child_pids(self) -> list[int]:
        """Processes the shell is currently waiting on, so a hang can be cut loose.

        A timeout interrupts and then kills these before the shell itself is replaced
        (`_abandon`).
        """
        bash = self._bash_pid()
        return self._children_of(bash) if bash is not None else []

    def _descendants(self) -> list[int]:
        bash = self._bash_pid()
        if bash is None:
            return []
        found: list[int] = []
        pending = self._children_of(bash)
        while pending:
            pid = pending.pop()
            found.append(pid)
            pending.extend(self._children_of(pid))
        return found

    def _listening(self) -> tuple[int | None, tuple[int, ...]]:
        """The process the running command left listening on a TCP port, and its ports.

        Read from /proc: the sockets each descendant of the shell holds open, matched
        against the LISTEN entries of the shell's own network namespace (a sandboxed shell
        has its own). (None, ()) when nothing listens or /proc cannot say.
        """
        bash = self._bash_pid()
        if bash is None:
            return None, ()
        if procs.IS_WINDOWS:
            return procs.listening_ports(bash)
        listening: dict[str, int] = {}
        for table in ("tcp", "tcp6"):
            try:
                rows = Path(f"/proc/{bash}/net/{table}").read_text().splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                fields = row.split()
                if len(fields) > 9 and fields[3] == "0A":  # TCP_LISTEN
                    listening[fields[9]] = int(fields[1].rsplit(":", 1)[1], 16)
        if not listening:
            return None, ()
        for pid in self._descendants():
            ports: set[int] = set()
            try:
                for fd in Path(f"/proc/{pid}/fd").iterdir():
                    target = os.readlink(fd)
                    if target.startswith("socket:[") and target[8:-1] in listening:
                        ports.add(listening[target[8:-1]])
            except OSError:
                continue
            if ports:
                return pid, tuple(sorted(ports))
        return None, ()

    def _detach(
        self, command: str, pid: int, ports: tuple[int, ...], *, report: bool = False
    ) -> None:
        """Leave the running command alive in its own shell and start a fresh one here.

        The old shell's pump keeps reading, now into a bounded tail, so the server never
        blocks on a full pipe. The working directory carries over; exported variables do
        not, as after a timeout.
        """
        assert self._process is not None
        job = BackgroundJob(self._process, command, pid, ports, report=report)
        old_queue = self._queue

        marker = self._marker
        process = self._process

        def report(exit_code: int | None) -> None:
            if job.report and self._on_exit is not None and not self._closing:
                try:
                    self._on_exit(job, exit_code)
                except Exception:  # the report is an extra; it never breaks the shell
                    log.exception("reporting the end of a background command failed")

        def drain() -> None:
            reported = False
            while (line := old_queue.get()) is not None:
                if marker in line:
                    # The command ended (killed, or it stopped on its own): its shell has
                    # nothing left to run and goes too. Reported now, not at the pipe's end:
                    # something it started may hold the pipe open long after.
                    _, _, rest = line.partition(marker)
                    code = rest.strip().partition("__")[0]
                    with contextlib.suppress(OSError):
                        assert process.stdin is not None
                        process.stdin.close()
                    self._forget(process.pid)
                    if not reported:
                        reported = True
                        report(int(code) if code.isdigit() else None)
                    continue
                job.tail.append(line)
            if not reported:
                # No marker: the command ended its shell (`exit 3` runs in it), so the
                # shell's own status is the command's. A signal (negative) stays unknown.
                status = None
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    status = process.wait(timeout=2)
                report(status if status is not None and status >= 0 else None)

        threading.Thread(target=drain, daemon=True, name="cogito-shell-bg").start()
        self._background.append(job)
        self._process = None
        self._start()

    def quiet_background(self) -> None:
        """Stop reporting the ends of the commands running now: the conversation that started
        them is gone («Reiniciar contexto»). They keep running."""
        for job in self._background:
            job.report = False

    @property
    def background(self) -> tuple[BackgroundJob, ...]:
        """Commands left running, oldest first; ones that have since ended are dropped."""
        self._background = [job for job in self._background if job.process.poll() is None]
        return tuple(self._background)

    def _abandon(self) -> None:
        """Stop a hung command for good, and come back with a usable shell.

        Killing only the child bash waits on is not enough: bash goes on with the rest of
        the command list, so `sleep 30; echo nunca` would still print. The list lives in
        the shell, so the shell has to go. The working directory carries over; exported
        variables do not, and the result says so.
        """
        self._kill_children()
        surviving_cwd = self._cwd
        self._close_shell()
        self._cwd = surviving_cwd
        self._start()

    def _kill_children(self) -> None:
        if procs.IS_WINDOWS:
            procs.terminate_trees(self._child_pids(), _GRACE_SECONDS)
            return
        for pid in self._child_pids():
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                continue
        # Wait out the grace period, but no longer than the children take to exit.
        deadline = time.monotonic() + _GRACE_SECONDS
        while self._child_pids() and time.monotonic() < deadline:
            time.sleep(0.05)
        for pid in self._child_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue

    def run(
        self, command: str, timeout: int = DEFAULT_TIMEOUT, cancel: CancelToken | None = None
    ) -> ShellOutput:
        with self._lock:
            return self._run_locked(command, timeout, cancel)

    def _run_locked(
        self, command: str, timeout: int, cancel: CancelToken | None = None
    ) -> ShellOutput:
        if self._unavailable:
            return ShellOutput(text=self._unavailable, exit_code=127, cwd=str(self._cwd))
        process = self._process
        if process is None or process.poll() is not None:
            self._start()
            process = self._process
        assert process is not None and process.stdin is not None

        # $? and $PWD are captured in the same expansion, after the command has finished.
        # The command runs as a group whose standard input is /dev/null: it shares this
        # pipe with the probe, and a command that read input (`input()`, `cat`, `read`)
        # read the probe itself and left the session out of step (QA rig, 2026-10-16). A
        # group, not a subshell, so `cd` and `export` still hold; the closing brace shares
        # the probe's line, so bash's line numbers in error messages do not move.
        probe = f'\n}} < /dev/null; printf "\\n{self._marker}%d__%s\\n" "$?" "$PWD"\n'
        if procs.IS_WINDOWS:
            # Git Bash's $PWD is MSYS's (/c/..., /tmp/...), which Windows cannot start a
            # process in; `pwd -W` names the same directory as Windows does (C:/...). The
            # status is saved first so the command substitution cannot change it.
            probe = (
                f'\n}} < /dev/null; __cogito_status=$?; '
                f'printf "\\n{self._marker}%d__%s\\n" "$__cogito_status" "$(pwd -W)"\n'
            )
        if self._restore_shims:
            # The shims are put back after the command, on the probe's own line (bash
            # expands an alias when it reads a line, so a restore read with the next
            # command would come too late), before the marker and silently, so nothing it
            # says is read as the next command's output.
            probe = (
                f'\n}} < /dev/null; __cogito_status=$?; '
                f'{{ {self._restore_shims}; }} 2>/dev/null; '
                f'printf "\\n{self._marker}%d__%s\\n" "$__cogito_status" "$PWD"\n'
            )
        process.stdin.write("{ " + command + probe)
        process.stdin.flush()

        collected: list[str] = []
        started = time.monotonic()
        next_look = started + _DETACH_AFTER
        deadline = started + timeout
        asked_for = self._waiting.elapsed()
        timed_out = False
        cancelled = False
        exit_code = -1
        cwd = str(self._cwd)

        while True:
            # The deadline moves forward by the time a person has been kept waiting since
            # this command started, so a slow answer cannot time the command out.
            paused = self._waiting.elapsed() - asked_for
            remaining = deadline + paused - time.monotonic()
            # Stop first: a person who pressed it near the deadline wants the command
            # stopped, never moved to the background.
            if cancel is not None and cancel.cancelled:
                # Stop pressed: the command goes the way a timed-out one does.
                cancelled = True
                self._abandon()
                break
            if remaining <= 0:
                if self._background_at_timeout and self._process is not None:
                    # APP-ONLY: a download or a build that outlives the timeout is left
                    # running, and its end is reported (`on_exit`), instead of being killed.
                    # What is queued is read first: if the command ended meanwhile, it is
                    # finished, not moved.
                    ended = False
                    while True:
                        try:
                            line = self._queue.get_nowait()
                        except Empty:
                            break
                        if line is None:
                            ended = True
                            break
                        if self._marker in line:
                            _, _, tail = line.partition(self._marker)
                            code, _, directory = tail.strip().partition("__")
                            exit_code = int(code) if code.isdigit() else -1
                            cwd = directory or cwd
                            self._cwd = Path(cwd)
                            ended = True
                            break
                        collected.append(line)
                    if ended:
                        break
                    pid = self._process.pid
                    self._detach(command, pid, (), report=True)
                    return ShellOutput(
                        text="".join(collected), exit_code=0, cwd=str(self._cwd),
                        background_pid=pid, moved=True,
                    )
                timed_out = True
                self._abandon()
                break
            if time.monotonic() >= next_look:
                # A command still running after a few seconds and listening on a port is a
                # server: it is left running instead of being waited on until the timeout
                # kills it.
                next_look = time.monotonic() + _DETACH_CHECK_EVERY
                pid, ports = self._listening()
                if pid is not None:
                    # What it printed so far, without waiting for more.
                    while True:
                        try:
                            line = self._queue.get_nowait()
                        except Empty:
                            break
                        if line is None:
                            break
                        collected.append(line)
                    self._detach(command, pid, ports)
                    return ShellOutput(
                        text="".join(collected), exit_code=0, cwd=str(self._cwd),
                        background_pid=pid, ports=ports,
                    )
            try:
                line = self._queue.get(timeout=min(remaining, 0.25))
            except Empty:
                continue
            if line is None:  # the shell died
                break
            if self._marker in line:
                _, _, tail = line.partition(self._marker)
                code, _, directory = tail.strip().partition("__")
                exit_code = int(code) if code.isdigit() else -1
                cwd = directory or cwd
                self._cwd = Path(cwd)
                break
            collected.append(line)

        return ShellOutput(
            text="".join(collected),
            # 124 is what coreutils `timeout` reports, so the model reads it as a timeout,
            # not as whatever the abandoned command left in $?.
            exit_code=_TIMEOUT_EXIT if timed_out else exit_code,
            cwd=cwd,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def close(self) -> None:
        """End the shell, and every command left running in the background with it: a
        server, a model or anything else the agent started does not outlive its session."""
        self._closing = True
        roots = [job.process.pid for job in self._background]
        if self._process is not None:
            if self._process.stdin:
                with contextlib.suppress(OSError):
                    self._process.stdin.close()
            roots.append(self._process.pid)
        self._background = []
        self._process = None
        _terminate_trees(roots)
        for root in roots:
            self._forget(root)

    def _close_shell(self) -> None:
        if self._process is None:
            return
        if self._process.stdin:
            with contextlib.suppress(OSError):
                self._process.stdin.close()
        _terminate_trees([self._process.pid])
        self._forget(self._process.pid)
        self._process = None

    def _note(self, pid: int) -> None:
        if self._registry is None:
            return
        with contextlib.suppress(OSError):
            self._registry.mkdir(parents=True, exist_ok=True)
            (self._registry / f"{os.getpid()}-{pid}").write_text(_start_time(pid))

    def _forget(self, pid: int) -> None:
        if self._registry is not None:
            with contextlib.suppress(OSError):
                (self._registry / f"{os.getpid()}-{pid}").unlink()


def git_bash() -> str:
    """Git for Windows' bash. `usr\\bin\\bash.exe` is the shell itself; `bin\\bash.exe`
    is a launcher that starts it as a child, and `System32\\bash.exe` is WSL's, a
    different Linux whose paths are not the project's. `COGITO_GIT_BASH` names one by hand.
    """
    import shutil

    candidates = [os.environ.get("COGITO_GIT_BASH", "")]
    if git := shutil.which("git"):
        root = Path(git).resolve().parent.parent  # <Git>\cmd\git.exe or <Git>\bin\git.exe
        candidates.append(str(root / "usr" / "bin" / "bash.exe"))
    for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMW6432", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Git", "usr", "bin", "bash.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise CogitoError(
        "the shell needs Git for Windows (its bash): install it from "
        "https://git-scm.com/download/win, or set COGITO_GIT_BASH to its usr\\bin\\bash.exe"
    )


def _start_time(pid: int) -> str:
    """When `pid` started, in clock ticks since boot (/proc/PID/stat, field 22): with the
    PID it names one process, never a later one that reused the number."""
    if procs.IS_WINDOWS:
        return procs.start_time(pid)
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def reap_orphans(registry: Path) -> int:
    """Stop the shells, and everything under them, that a core which is no longer running
    left behind: it died before it could close them (a crash, a kill). A record whose core
    is alive is left alone; one whose shell has gone, or whose PID now names a different
    process, is only removed. Returns how many shells were stopped."""
    stopped = 0
    try:
        records = list(registry.iterdir())
    except OSError:
        return 0
    for record in records:
        owner, _, shell = record.name.partition("-")
        if not (owner.isdigit() and shell.isdigit()):
            continue
        if _alive(int(owner)):
            continue
        try:
            started = record.read_text().strip()
        except OSError:
            started = ""
        if started and _alive(int(shell)) and _start_time(int(shell)) == started:
            _terminate_trees([int(shell)])
            stopped += 1
        with contextlib.suppress(OSError):
            record.unlink()
    return stopped


def _process_tree(root: int) -> list[int]:
    """`root` and every process below it, from /proc."""
    if procs.IS_WINDOWS:
        return procs.tree(root)
    found: list[int] = []
    pending = [root]
    while pending:
        pid = pending.pop()
        found.append(pid)
        pending.extend(ShellSession._children_of(pid))
    return found


def _alive(pid: int) -> bool:
    if procs.IS_WINDOWS:
        return procs.alive(pid)
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _terminate_trees(roots: Sequence[int], grace: float = _GRACE_SECONDS) -> None:
    """SIGTERM every process under `roots` and every process group they lead, then SIGKILL
    whatever is still alive after `grace` seconds.

    By tree and by group, because each catches what the other misses: a command that
    started its own session (`setsid`) leaves the group but stays in the tree, and a
    child whose parent already exited leaves the tree but stays in the group.
    """
    if procs.IS_WINDOWS:
        procs.terminate_trees(roots, grace)
        return
    pids: set[int] = set()
    for root in roots:
        pids.update(_process_tree(root))
    groups: set[int] = set()
    for pid in pids:
        with contextlib.suppress(OSError):
            groups.add(os.getpgid(pid))
    groups.discard(os.getpgid(0))  # never this process's own group

    def send(sig: signal.Signals) -> None:
        for group in groups:
            with contextlib.suppress(OSError):
                os.killpg(group, sig)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)

    send(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    if any(_alive(pid) for pid in pids):
        send(signal.SIGKILL)
    for root in roots:
        # Reap the shells this process started, so they leave no zombie behind.
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(root, os.WNOHANG)


def clean(text: str) -> str:
    """Strip terminal control sequences and fold repeated lines."""
    return collapse_repeats(_ANSI.sub("", text))


class ShellTool:
    """The model-facing tool."""

    spec = ToolSpec(
        name="shell",
        #: What a command printed, at the moment it ran. Nothing can produce it again.
        provenance=Provenance(
            reproducibility=Reproducibility.EPHEMERAL,
            retention=Retention.EVIDENCE,
            shape=PayloadShape.COMPACT,
        ),
        description=(
            "Run a bash command. The shell is persistent: working directory, "
            "exported variables and activated environments survive between calls."
        ),
        parameters=parameters(
            {
                "command": {"type": "string", "description": "The bash command to run."},
                "timeout": {
                    "type": "integer",
                    "description": f"Seconds. Default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}.",
                },
            },
            required=("command",),
        ),
        aliases={"command": ("cmd", "script", "bash", "shell_command", "input")},
        budget_tokens=2000,
        mutates=True,
        core=True,
        verbatim=("command",),
    )

    def __init__(self, session: ShellSession) -> None:
        self._session = session

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult.error("command was empty; pass the bash to run", "empty_command")

        timeout = min(int(arguments.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        if context.cancel.cancelled:
            return ToolResult.error("cancelled before the command started", "cancelled")

        started = time.monotonic()
        output = self._session.run(command, timeout=timeout, cancel=context.cancel)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if output.cancelled:
            body = clean(output.text).rstrip()
            message = (
                f"stopped by the user after {elapsed_ms / 1000:.1f}s, before the command "
                f"finished; a fresh shell is ready in {output.cwd}, but exported variables "
                f"were reset"
            )
            return ToolResult(
                content=f"{message}\n{body}" if body else message,
                is_error=True,
                error_kind="cancelled",
                metadata={
                    "exit_code": output.exit_code,
                    "cwd": output.cwd,
                    "timed_out": False,
                    "cancelled": True,
                    "duration_ms": elapsed_ms,
                },
            )

        body = clean(output.text).rstrip()
        if output.moved and output.background_pid is not None:
            # Said the way Claude Code says it ("moved to the background"; its docs,
            # tools-reference, read 2026-10-31), and the model is told not to poll: the end
            # comes to it by itself (`ShellSession.on_exit`).
            header = (
                f"still running after {timeout}s, so it was moved to the background "
                f"(pid {output.background_pid})  cwd {output.cwd}\n"
                "You will be told here when it ends, with its exit code and its last lines. "
                "Do not poll it, sleep or wait for it: go on with what does not need it, or "
                f"end your turn. {_stop_hint(output.background_pid)} A fresh shell "
                "is ready in the same directory, but exported variables were reset."
            )
            return ToolResult(
                content=f"{header}\n{body}" if body else header,
                metadata={
                    "exit_code": None,
                    "cwd": output.cwd,
                    "timed_out": False,
                    "duration_ms": elapsed_ms,
                    "background_pid": output.background_pid,
                    "moved": True,
                },
            )
        if output.background_pid is not None:
            ports = ", ".join(str(port) for port in output.ports)
            header = (
                f"still running in the background (pid {output.background_pid}, listening "
                f"on port {ports})  cwd {output.cwd}\n"
                f"It keeps running after this call. Stop it with `kill "
                f"{output.background_pid}`. A fresh shell is ready in the same directory, "
                f"but exported variables were reset."
            )
            return ToolResult(
                content=f"{header}\n{body}" if body else header,
                metadata={
                    "exit_code": None,
                    "cwd": output.cwd,
                    "timed_out": False,
                    "duration_ms": elapsed_ms,
                    "background_pid": output.background_pid,
                    "ports": ports,
                },
            )
        header = f"exit {output.exit_code}  cwd {output.cwd}"
        if output.timed_out:
            header += (
                f"  [timed out after {timeout}s and was stopped; a fresh shell is ready "
                f"in the same directory, but exported variables were reset]"
            )

        return ToolResult(
            content=f"{header}\n{body}" if body else header,
            is_error=output.exit_code != 0,
            error_kind="nonzero_exit" if output.exit_code else "",
            metadata={
                "exit_code": output.exit_code,
                "cwd": output.cwd,
                "timed_out": output.timed_out,
                "duration_ms": elapsed_ms,
            },
        )
