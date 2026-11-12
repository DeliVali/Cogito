"""PATH shims: the last check before the model unlinks a file.

The command scanner reads the command text. It cannot see what is built at runtime, hidden
behind an alias, or inside a script the model just wrote. So the binaries themselves are
shadowed in the agent's shell: `rm` on its PATH is a program that asks the guard and then
moves the file to the trash.

This is also what makes deletion recoverable. A permitted `rm` would otherwise unlink the
file, and `cogito undo` would have nothing to restore. `truncate` is the one verb that
changes a file instead of removing it: a copy goes to the trash, then the real `truncate`
runs, so the file ends up exactly as the command asked.

An absolute `/usr/bin/rm` still gets through, since no PATH can shadow it. The protected
path list is the floor under that.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path

from cogito.guard.platform.base import Zone
from cogito.guard.policy import (
    Decision,
    Guard,
    confinement_refusal,
    deletion_path,
    outside,
    real_path,
)
from cogito.guard.trash import TrashError
from cogito.guard.undo import UndoStore

log = logging.getLogger(__name__)

#: The verbs worth shadowing: the ones that remove or empty a file outright.
SHIMMED = ("rm", "rmdir", "shred", "srm", "unlink", "truncate")

#: Why a deletion did not run, by who decided (`ApprovalResponse.reason`). The person's no
#: says it was theirs: told only "not approved", a model asked the person to confirm the
#: deletion they had just declined (QA rig, 2026-10-16).
_NOT_APPROVED = {
    "user": "the person you are working with declined it",
    "nobody": "nobody was available to approve it",
    "interrupted": "the turn was stopped before this was answered",
    "timeout": "nobody answered in time",
}

_CLIENT = Path(__file__).with_name("client.py")
_ENVIRONMENT_VARIABLE = "COGITO_GUARD_SOCKET"


def _caller_gone(connection: socket.socket) -> bool:
    """True when the shim that asked died before the answer arrived.

    A person can take longer to decide than the command's deadline, and the shell then
    kills the shim. The answer still arrives on this thread with nobody to receive it.
    Acting on it would trash a file minutes after the model was told nothing was removed.

    The client sends one line and then only reads, so anything peekable is end-of-stream.
    """
    if os.name == "nt":
        # No MSG_DONTWAIT on Windows: ask select whether anything is there first, so the
        # peek below never waits.
        import select

        try:
            if not select.select([connection], [], [], 0)[0]:
                return False
            return connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True
    try:
        return connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


def _targets(verb: str, arguments: list[str]) -> list[str]:
    out: list[str] = []
    after_separator = False
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--":
            after_separator = True
            continue
        # A lone `-` is an operand: GNU truncate opens a file named `-`, and judging the
        # command without it once let it empty whatever `-` linked to (review, 2026-11-03).
        if not after_separator and argument.startswith("-") and argument != "-":
            # `truncate -s 0 file` carries its size in the next argument, and
            # `truncate -r ref file` its reference file; `shred -n 3 file` its passes.
            if verb == "truncate" and argument in {"-s", "--size", "-r", "--reference"}:
                skip_next = True
            if verb == "shred" and argument in {"-n", "--iterations", "-s", "--size"}:
                skip_next = True
            continue
        out.append(argument)
    return out


_PREFIX = "cogito-guard-"
#: What a broker's directory holds, and nothing else: a sweep never removes a directory
#: that merely shares the prefix.
_BROKER_LAYOUT = {"bin", "guard.sock"}


def _sweep_orphans(temp: Path) -> None:
    """Remove broker directories a process left behind when it died without `stop`.

    `stop` removes its own directory, but a process killed outright (the app quitting
    under it, a timeout's SIGKILL) never reaches it: 412 were left in /tmp in one day of
    QA (2026-10-13). One is an orphan when it is this user's, holds exactly a broker's
    layout, and its socket accepts no connection; a live session's broker answers, so it
    is kept. Errors are ignored: a sweep must never stop a session from starting.
    """
    if os.name == "nt":
        # A Windows broker listens on loopback TCP and leaves no socket file to probe; its
        # directory holds only the shims, and the next start makes a new one.
        return
    try:
        candidates = list(temp.glob(f"{_PREFIX}*"))
    except OSError:
        return
    for directory in candidates:
        try:
            if directory.is_symlink() or not directory.is_dir():
                continue
            if directory.stat().st_uid != os.getuid():
                continue
            if {entry.name for entry in directory.iterdir()} != _BROKER_LAYOUT:
                continue
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(directory / "guard.sock"))
                # A request with no targets: a live broker allows it without asking or
                # recording anything, and answers, so the probe leaves no trace there.
                probe.sendall(b'{"args": []}\n')
                probe.recv(4096)
            except ConnectionRefusedError:
                shutil.rmtree(directory, ignore_errors=True)
            except FileNotFoundError:
                shutil.rmtree(directory, ignore_errors=True)
            except OSError:
                pass  # it answered slowly or oddly: not provably an orphan, keep it
            finally:
                probe.close()
        except OSError:
            continue


class ShimBroker:
    """A unix socket the shims call back into, answered by the guard in this process."""

    def __init__(
        self,
        guard: Guard,
        undo: UndoStore,
        *,
        interpreter: str | None = None,
        on_decision: Callable[[str, bool, list[str]], None] | None = None,
        confine_to: Sequence[Path] | None = None,
    ) -> None:
        self._guard = guard
        #: Set only for a sandboxed run. The shell cannot write outside its writable roots,
        #: but the broker runs on the host and moves files itself. So a target outside them
        #: is refused here, before anyone is asked, instead of being approved by
        #: `--allow-destructive` and carried out past the sandbox.
        self._confine_to = tuple(confine_to) if confine_to is not None else None
        self._undo = undo
        self._interpreter = interpreter or "python3"
        self._on_decision = on_decision or (lambda verb, allowed, targets: None)
        self._directory: Path | None = None
        self._socket_path: Path | None = None
        #: Windows only: Python has no AF_UNIX there, so the broker listens on loopback TCP
        #: and every request must carry this secret (`client.py`).
        self._token = ""
        self._address = ""
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def bin_dir(self) -> Path | None:
        return self._directory

    def environment(self) -> dict[str, str]:
        """What to add to the shell's environment for the shims to be found and work."""
        if self._directory is None or self._socket_path is None:
            return {}
        return {
            "PATH": f"{self._directory}{os.pathsep}{os.environ.get('PATH', '')}",
            _ENVIRONMENT_VARIABLE: self._address or str(self._socket_path),
        }

    def start(self) -> dict[str, str]:
        _sweep_orphans(Path(tempfile.gettempdir()))
        root = Path(tempfile.mkdtemp(prefix=_PREFIX))
        self._directory = root / "bin"
        self._directory.mkdir()
        self._socket_path = root / "guard.sock"

        # The verb is passed explicitly rather than inferred from argv[0]: a shell
        # `exec` hands the interpreter its own name, not the script's.
        self._write_dispatchers()

        if os.name == "nt":
            self._token = secrets.token_hex(16)
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.bind(("127.0.0.1", 0))
            self._address = f"tcp:127.0.0.1:{self._server.getsockname()[1]}:{self._token}"
        else:
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(self._socket_path))
        self._server.listen(8)
        self._server.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, name="cogito-guard", daemon=True)
        self._thread.start()
        return self.environment()

    @property
    def root(self) -> Path:
        """The directory holding the shims and the socket. A sandbox must bind it."""
        return self._socket_path.parent

    def _write_dispatchers(self) -> None:
        """Each shim is a script named after its verb that execs the client with the verb.

        The client runs with -I, so the agent shell's PYTHONPATH and other PYTHON*
        variables, the user site and the current directory cannot put a project's module
        in place of the standard library's. -B: no bytecode is written beside the
        interpreter, which in an installed app is read-only or signed."""
        assert self._directory is not None
        # Git Bash reads forward slashes in a Windows path, and backslashes inside double
        # quotes would be escapes there.
        interpreter = Path(self._interpreter).as_posix() if os.name == "nt" else self._interpreter
        client = Path(_CLIENT).as_posix() if os.name == "nt" else _CLIENT
        for verb in SHIMMED:
            shim = self._directory / verb
            shim.write_text(
                "#!/bin/sh\n"
                f'exec "{interpreter}" -I -B "{client}" "{verb}" "$@"\n',
                encoding="utf-8",
                newline="\n",
            )
            shim.chmod(0o755)

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    self._handle(connection)
                except Exception:  # a broken shim must not take the session down
                    log.exception("guard broker failed while handling a request")

    def _handle(self, connection: socket.socket) -> None:
        chunks: list[bytes] = []
        while not chunks or not chunks[-1].endswith(b"\n"):
            received = connection.recv(65536)
            if not received:
                break
            chunks.append(received)
        try:
            request = json.loads(b"".join(chunks).decode("utf-8") or "{}")
        except ValueError:
            connection.sendall(b'{"allowed": false, "message": "malformed request"}\n')
            return

        if self._token and not secrets.compare_digest(
            str(request.get("token", "")), self._token
        ):
            connection.sendall(b'{"allowed": false, "message": "not from this session"}\n')
            return
        verb = str(request.get("verb", "rm"))
        cwd = Path(str(request.get("cwd") or "."))
        arguments = [str(a) for a in request.get("args", [])]
        reply = self._decide(verb, cwd, arguments, connection)
        connection.sendall(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")

    def _decide(
        self, verb: str, cwd: Path, arguments: list[str], connection: socket.socket
    ) -> dict[str, object]:
        raw_targets = _targets(verb, arguments)

        # A target this process would read differently from the command it runs: an empty
        # word or `.`/`..` (`Path(cwd) / ""` is the whole folder, which rm itself refuses),
        # or a path through /proc or /dev/fd, which names another process's files here
        # than in the real command (`truncate -s 0 /proc/self/cwd/id_rsa` from `~/.ssh`
        # was judged as the core's own folder). Review of 2026-11-03.
        for raw in raw_targets:
            if why := _unreadable_target(raw, cwd, follow_last=verb in {"truncate", "shred"}):
                self._on_decision(verb, False, raw_targets)
                command = shlex.join([verb, *arguments])
                self._guard.ledger.note_refusal(command)
                return {
                    "allowed": False,
                    "message": f"refused: {verb} {shlex.quote(raw)}: {why}. Nothing was removed.",
                }

        if verb == "rm" and _forced(arguments):
            # `rm -f` of what is not there succeeds and does nothing, as GNU rm does; the
            # broker used to fail it, which broke `rm -rf dist && build`.
            raw_targets = [
                raw for raw in raw_targets
                if raw in {"", "-"} or os.path.lexists(cwd / raw)
            ]
        if not raw_targets:
            return {"allowed": True, "message": ""}

        # Rebuild the command so the guard judges what the user will see. Quote each
        # argument: the shell already expanded it into one word. Joined bare,
        # `rm x '#' ~/.ssh` came back as `rm x # ~/.ssh`, a comment judged as `rm x`, and
        # the broker then moved every argument, `~/.ssh` included.
        # The shell already expanded every word, so one that starts with `~`, `$` or a
        # backtick is a name: written `./~` the guard reads it as one, not as the home
        # directory (a link named `~` once passed as `$HOME/x`; review of 2026-11-03).
        targets = set(raw_targets)
        named = [
            f"./{argument}"
            if argument in targets and argument[:1] in {"~", "$", "`"}
            else argument
            for argument in arguments
        ]
        command = shlex.join([verb, *named])
        # The shell already expanded every word, so a `*`, `?`, `[`, `$` or backtick left in
        # one is part of a name, which the guard would read as a pattern it cannot resolve:
        # `ln -s ~/.ssh 'k*'; rm 'k*/id_ed25519'` passed the refusal of `~/.ssh` that way
        # (review of 2026-11-03). Such a name is judged again as the path it really is.
        for raw in raw_targets:
            if not any(mark in raw for mark in _PATTERN_MARKS):
                continue
            through = verb in {"truncate", "shred"}
            literal = real_path(raw, cwd) if through else deletion_path(raw, cwd)
            if any(mark in str(literal) for mark in _PATTERN_MARKS):
                continue
            plain = self._guard.evaluate(
                "shell", {"command": shlex.join([verb, str(literal)])}, cwd=cwd
            )
            if plain.decision is Decision.REFUSE:
                self._on_decision(verb, False, raw_targets)
                self._guard.ledger.note_refusal(command)
                return {"allowed": False, "message": plain.reason}
        verdict = self._guard.evaluate("shell", {"command": command}, cwd=cwd)
        system = verdict.decision is Decision.REFUSE and any(
            target.ruling is not None and target.ruling.zone is Zone.SYSTEM
            for target in verdict.targets
        )

        # Judge the arguments the shell expanded, in the directory the shim was called
        # from, so a glob, variable or script the command text hid cannot reach the
        # session's record through here. An operating-system path is refused below for
        # its own, larger reason.
        refusal = None if system else self._guard.harness.judge_shim(verb, cwd, raw_targets)
        if refusal is not None:
            self._on_decision(verb, False, raw_targets)
            self._guard.ledger.note_refusal(command)
            return {"allowed": False, "message": f"{verb}: {refusal} Nothing was removed."}

        if self._confine_to is not None:
            roots = (*self._confine_to, self.root)
            for raw in raw_targets:
                # rm removes a link itself; truncate writes through it.
                resolved = real_path(raw, cwd) if verb == "truncate" else deletion_path(raw, cwd)
                if outside(resolved, roots):
                    self._on_decision(verb, False, raw_targets)
                    self._guard.ledger.note_refusal(command)
                    return {
                        "allowed": False,
                        "message": f"{verb}: "
                        + confinement_refusal(raw, resolved, roots, "files can be deleted")
                        + " Nothing was removed.",
                    }

        if verdict.decision is Decision.REFUSE:
            self._on_decision(verb, False, raw_targets)
            self._guard.ledger.note_refusal(command)
            return {"allowed": False, "message": verdict.reason}

        if verdict.decision is Decision.ASK:
            response = self._guard.answer(verdict, command, from_shim=True)
            if not response.allowed:
                self._on_decision(verb, False, raw_targets)
                why = _NOT_APPROVED.get(response.reason, _NOT_APPROVED["user"])
                return {"allowed": False, "message": f"{verb}: {why}; nothing was removed"}

        # Act on an approval only while the caller is still waiting for it.
        if _caller_gone(connection):
            log.warning(
                "%s was approved after the command had already been abandoned; "
                "nothing was removed",
                command,
            )
            self._on_decision(verb, False, raw_targets)
            return {"allowed": False, "message": ""}

        if verb == "truncate":
            return self._truncate(cwd, arguments, raw_targets)

        moved: list[str] = []
        failures: list[str] = []
        for raw in raw_targets:
            target = Path(raw)
            path = target if target.is_absolute() else cwd / target
            try:
                entry = self._undo.preserve_delete(path)
                moved.append(str(entry.original_path))
            except TrashError as exc:
                failures.append(str(exc))

        self._on_decision(verb, True, moved)
        message = ""
        if moved:
            message = f"moved {len(moved)} item(s) to the trash; undo with `cogito undo`"
        if failures:
            message = (message + "\n" if message else "") + "\n".join(failures)
        return {"allowed": not failures or bool(moved), "message": message}

    def _truncate(
        self, cwd: Path, arguments: list[str], raw_targets: list[str]
    ) -> dict[str, object]:
        """Save what each file holds to the trash, then run the real `truncate`.

        Moving the file to the trash, as a deletion does, left it gone instead of emptied:
        `truncate -s 0 log && echo x >> log` ended with a one-line new file and the old one
        in the trash, and `truncate -s 1M disk.img` with no file at all. Now the copy is
        taken first, so nothing changes that could not be saved, and the file ends up
        exactly as the command asked. A file the command creates is recorded as created.
        """
        binary = self._real("truncate")
        if binary is None:
            self._on_decision("truncate", False, raw_targets)
            return {"allowed": False,
                    "message": "truncate: the real truncate is not on PATH; nothing was changed"}

        saved: list[tuple[str, Path]] = []
        absent: list[Path] = []
        failures: list[str] = []
        for raw in raw_targets:
            target = Path(raw)
            path = target if target.is_absolute() else cwd / target
            if path.is_file():
                try:
                    saved.append((self._undo.preserve_in_trash(path).key, path))
                except TrashError as exc:
                    failures.append(str(exc))
            elif not path.exists() and not path.is_symlink():
                absent.append(path)
        if failures:
            self._on_decision("truncate", False, raw_targets)
            return {"allowed": False,
                    "message": "\n".join(failures) + "\ntruncate: nothing was changed"}

        try:
            result = subprocess.run(
                [binary, *arguments], cwd=cwd, capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._on_decision("truncate", False, raw_targets)
            return {"allowed": False, "message": f"truncate: {exc}"}

        # Hashed from the files as they now are, so a rewind can tell this change from a
        # later one (see `UndoStore.note_written`).
        for key, path in saved:
            self._undo.note_written(key, path)
        for path in absent:
            if path.exists():
                self._undo.note_written(self._undo.preserve_create(path).key, path)

        self._on_decision("truncate", result.returncode == 0, [str(p) for _, p in saved])
        lines = [text.rstrip("\n") for text in (result.stdout, result.stderr) if text.strip()]
        if saved:
            lines.append(
                f"saved a copy of {len(saved)} item(s) to the trash first; "
                "undo with `cogito undo`"
            )
        return {"allowed": result.returncode == 0, "message": "\n".join(lines)}

    def _real(self, verb: str) -> str | None:
        """The binary a shim stands in for: the first on PATH that is not a shim."""
        shims = os.path.realpath(self._directory) if self._directory is not None else None
        search = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
            if entry and os.path.realpath(entry) != shims
        )
        return shutil.which(verb, path=search)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            finally:
                self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._directory is not None:
            shutil.rmtree(self._directory.parent, ignore_errors=True)
            self._directory = None

    def __enter__(self) -> ShimBroker:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


#: What the guard reads as a pattern or an expansion in a word.
_PATTERN_MARKS = ("*", "?", "[", "$", "`")

#: Where a path names the files of whichever process opens it.
_PROCESS_ROOTS = ("/proc", "/dev/fd", "/dev/stdin", "/dev/stdout", "/dev/stderr")

#: How many links one target may pass through, as the kernel's own limit.
_MAX_LINKS = 40


def _forced(arguments: list[str]) -> bool:
    """Whether rm was given `-f` or `--force`, alone or among other letters (`-rf`)."""
    for argument in arguments:
        if argument == "--":
            return False
        if argument == "--force" or (
            argument.startswith("-") and not argument.startswith("--") and "f" in argument[1:]
        ):
            return True
    return False


def _in_process_root(path: Path) -> bool:
    text = str(path)
    return any(text == root or text.startswith(root + "/") for root in _PROCESS_ROOTS)


def _unreadable_target(raw: str, cwd: Path, *, follow_last: bool = False) -> str | None:
    """Why the broker cannot judge ``raw`` as the real command will act on it, or None.

    The path is walked one part at a time as the kernel walks it: a link is followed
    before the `..` after it applies, so `/dev/fd/../cwd/x` is seen passing through
    /proc. The last part's link is followed only for a verb that writes through it
    (``follow_last``: truncate, shred) or when the word ends in `/`.
    """
    if raw == "-":
        return "a lone '-' is read differently by each program; write ./- for a file named -"
    if raw.rstrip("/") == "" and raw != "/":
        return "an empty name stands for the folder the command runs in"
    if os.path.basename(raw.rstrip("/")) in {".", ".."}:
        return "rm refuses to remove '.' or '..', and so does Cogito"
    follow_last = follow_last or raw.endswith("/")
    parts = deque(Path(raw if os.path.isabs(raw) else cwd / raw).parts[1:])
    current = Path("/")
    links = 0
    why = "a path through /proc or /dev/fd names another process's files here"
    while parts:
        part = parts.popleft()
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        following = current / part
        if _in_process_root(following):
            return why
        if (parts or follow_last) and following.is_symlink():
            links += 1
            if links > _MAX_LINKS:
                return "too many links to follow"
            try:
                target = Path(os.readlink(following))
            except OSError:
                return None
            if target.is_absolute():
                current = Path("/")
                parts.extendleft(reversed(target.parts[1:]))
            else:
                parts.extendleft(reversed(target.parts))
            continue
        current = following
    return why if _in_process_root(current) else None
