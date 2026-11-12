"""Terminal tabs: the person's shell on a pseudo-terminal, in the app (POSIX only).

Each tab is the person's own shell (`integration.person_shell`), interactive, in the
project's root, with their environment: no guard shims, no sandbox. The person types there;
a line the model proposes is typed only after the person approves that exact line
(`run_in_terminal`, asked through the guard).

The controlling terminal is set in a launcher process (``sys.executable -c``) that calls
``setsid`` and ``TIOCSCTTY`` and then execs the shell. ``os.forkpty`` warns in a
multithreaded process since Python 3.12 and ``preexec_fn`` is not thread-safe, and the core
runs many threads.

One reader thread per tab decodes the output as UTF-8 as it comes, keeps the raw text (at
most 256 KiB, replayed into xterm when a window reopens) and the plain-text screen
(`screen.Screen`), and hands the window joined pieces through ``emit``, at most 16 ms or
64 Ki characters after the first one, however busy the shell is:

- ``terminal_opened`` ``{tab, focus}``
- ``terminal_data`` ``{id, data, end}``: ``end`` is how much output the tab has produced,
  this piece included, in UTF-16 code units (JavaScript's string length), so a window that
  replayed a scrollback ending at ``E`` drops what it queued up to ``E``
- ``terminal_status`` ``{id, running, exit_code?}``
- ``terminal_exited`` ``{id, exit_code}``
- ``terminal_unsent`` ``{id, reason}``: the line given when the tab was opened (the window's
  Run button) was not typed. ``reason`` is ``"busy"`` when something other than the
  shell's line editor would have read it: a program the person started there first (an ssh
  session, a Python prompt) or a line the person entered there that has not given the
  prompt back within 30 s. ``"not_ready"`` when the shell never got to its prompt in 30 s.

A line typed for the person's click or for the model goes in as a bracketed paste
(``ESC [200~ ... ESC [201~``) while the shell has mode 2004 on, so the line editor inserts
it as it is (no key binding), then Enter. Without it, several lines are sent separated by
Enter. fish still expands an abbreviation under the cursor when Enter is pressed, so for
fish the paste ends with a space, after which the cursor is on no word (fish 4.9.3).

A new tab's line is typed only once the line editor is up: after the ready mark that follows
the first prompt mark once the prompt is drawn (`integration`), however long the prompt takes.
Typed before, it goes in as plain keys: the second line of a Run button's block became input
for the first line's ``read``, and a fish abbreviation expanded. A mark that promises no
ready mark (bash with ``promptvars`` off) falls back to bracketed paste turning on, or 2 s.
One that promises it, followed by bracketed paste but no ready mark for 1 s, is taken as up
too: fish draws no right prompt, which carries the mark, when the left prompt leaves it no
room.

When the person pressed Enter in the tab before the line is typed, the line waits for the
prompt mark that answers that Enter (`Tab.owed_prompt`), then for the line editor again, and
is typed only if the shell itself holds the terminal and no line is running by the marks. A
line that never gets its prompt back (``cat``, a ``read``) is never typed into.
"""

from __future__ import annotations

import codecs
import contextlib
import errno
import fcntl
import logging
import os
import secrets
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cogito.tools.terminal import integration
from cogito.tools.terminal.screen import Screen

log = logging.getLogger(__name__)

#: The raw output a tab keeps, in UTF-8 bytes.
SCROLLBACK_BYTES = 256 * 1024
#: How long output is joined before it goes to the window.
JOIN_SECONDS = 0.016
#: How much output, in characters, is joined at most before it goes to the window.
FLUSH_CHARS = 64 * 1024
#: How much a reader still reads once its shell is gone, before it stops: what a job left
#: behind keeps printing is not waited for.
_DRAIN_BYTES = 1024 * 1024
#: How long a closing shell has after SIGHUP before it is killed.
_CLOSE_GRACE = 1.0
#: How often a reader with nothing to read checks that its shell is still there.
_POLL_SECONDS = 1.0
#: How long the output must be quiet once the line editor is up before a line is typed.
_SETTLE = 0.1
#: The longest a line waits for the output to go quiet once the line editor is up: a prompt
#: that keeps redrawing (a clock) does not hold it back for good.
_SETTLE_MAX = 2.0
#: How long after a prompt mark that promises no ready mark (bash with ``promptvars`` off) a
#: shell that never turns bracketed paste on is taken to have its line editor up.
_EDITOR_FALLBACK = 2.0
#: How long after the line editor is up a line still waits for bracketed paste to be turned
#: on (zsh turns it on just after the ready mark), before it goes in as keys.
_PASTE_GRACE = 0.3
#: How long after bracketed paste is turned on a prompt mark's promised ready mark may be
#: missing before the line editor is taken to be up all the same: fish drops its right
#: prompt, which carries the mark, when the left prompt leaves it no room.
_READY_LATE = 1.0
#: How long a line queued for the first prompt (the Run button) waits for the line editor.
_QUEUED_WAIT = 30.0
#: Typed before a Run button's line in a tab the person typed in before its first prompt:
#: Ctrl-E, Ctrl-U, which clear the line in bash, zsh and fish's default (emacs) bindings,
#: and Ctrl-U is the terminal's own line kill where no line editor reads.
_CLEAR_LINE = "\x05\x15"
_PASTE_START, _PASTE_END = "\x1b[200~", "\x1b[201~"
_DEFAULT_COLS, _DEFAULT_ROWS = 80, 24

#: Run as ``sys.executable -c``: a new session, the PTY on fd 0 as its controlling
#: terminal, then the shell in place of this process.
#: CPython ignores SIGPIPE and SIGXFSZ at startup and an exec keeps ignored signals; the
#: shell gets them back as they were, or `yes | head -1` reports a broken pipe.
_LAUNCHER = (
    "import fcntl, os, signal, sys, termios\n"
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
    "os.setsid()\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)

Emit = Callable[[dict[str, Any]], None]


class Tab:
    """One shell on one PTY. The host's methods are the way in; fields are read under
    ``lock``."""

    def __init__(
        self,
        tab_id: str,
        title: str,
        cwd: Path,
        opened_by: str,
        process: subprocess.Popen,
        master: int,
        integrated: bool,
        kind: str,
        nonce: str = "",
    ) -> None:
        self.id = tab_id
        self.title = title
        self.cwd = cwd
        self.opened_by = opened_by
        self.process = process
        self.master = master
        self.integrated = integrated
        self.kind = kind
        self.lock = threading.Condition()
        self.screen = Screen(nonce=nonce)
        #: The shell's start time in /proc, to tell it from a later process with its pid.
        self.started = _starttime(process.pid)
        #: Output produced so far, in UTF-16 code units (``terminal_data``'s ``end``).
        self.produced = 0
        self._chunks: list[str] = []
        self._chunk_bytes: list[int] = []
        self._scroll_bytes = 0
        #: The person typed here (or opened it to run something of their own).
        self.touched = False
        #: The person sent keys to this tab (`TerminalHost.input`).
        self.keyed = False
        #: The prompt count that answers the last Enter the person pressed here (0: none
        #: owed). A line to type waits until ``prompts`` reaches it (`TerminalHost.wait_to_type`).
        self.owed_prompt = 0
        #: Prompt marks read from the output so far. ``prompts`` catches up once the reader
        #: has handled each; this one moves under the same lock as the screen.
        self.marks = 0
        #: By the marks, the line editor reads the next Enter: a prompt mark came, and no
        #: Enter was pressed and no line typed since.
        self.reading = False
        #: A command line was entered and its prompt has not come back yet.
        self.running = False
        #: The shell drew its first prompt.
        self.ready = False
        #: Prompts seen so far: a waiter compares it with the count before its line.
        self.prompts = 0
        self.last_status: int | None = None
        self.exited = False
        self.exit_code: int | None = None
        #: The line the model typed last, while it runs.
        self.command = ""
        #: Typed at the first prompt (the window's Run button).
        self.pending: str | None = None
        #: When output last arrived, for a shell that reports no prompt.
        self.last_output = time.monotonic()
        #: When the last prompt mark arrived, when the line editor came up after it, and
        #: when bracketed paste was turned on after it.
        self.marked_at = 0.0
        self.editor_at = 0.0
        self.paste_at = 0.0
        #: Called once with (exit_code or None) at the next prompt, or at exit.
        self.on_prompt: list[Callable[[int | None], None]] = []
        #: Held while writing to ``master``, so it is never closed under a writer.
        self.write_lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()

    # -- what the window and the tools read -------------------------------------------------

    def describe(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "title": self.title,
                "cwd": str(self.cwd),
                "running": self.running,
                "opened_by": self.opened_by,
                "touched": self.touched,
                "exited": self.exited,
            }

    def scrollback(self) -> str:
        with self.lock:
            return "".join(self._chunks)

    def snapshot(self) -> tuple[str, int]:
        """The kept output and the offset it ends at, taken together."""
        with self.lock:
            return "".join(self._chunks), self.produced

    def lines(self, since: int | None = None, last: int | None = None) -> list[str]:
        with self.lock:
            return self.screen.lines(since=since, last=last)

    def shell_in_front(self) -> bool:
        """The shell itself holds the terminal: no program it started is in the foreground,
        whatever the prompt marks say (a shell without them, a line entered with Ctrl-J)."""
        with self.lock:
            if self.exited or self.master < 0:
                return False
            try:
                return os.tcgetpgrp(self.master) == self.process.pid
            except OSError:
                return False

    def editor_since(self) -> float | None:
        """When the line editor came up after the last prompt mark, or None while it has
        not. Called under ``lock``."""
        if not self.ready:
            return None
        if self.screen.editor_ready:
            return self.editor_at
        if self.screen.promised:
            # The ready mark is the word: a slow prompt is still being drawn. Unless
            # bracketed paste came on and the mark is still missing a moment later (a fish
            # left prompt too wide for the right one that carries it).
            if not self.screen.editor_up:
                return None
            late = self.paste_at + _READY_LATE
            return late if time.monotonic() >= late else None
        fallback = self.marked_at + _EDITOR_FALLBACK
        return fallback if time.monotonic() >= fallback else None

    # -- the reader's side ------------------------------------------------------------------

    def keep(self, text: str) -> list[int | None]:
        """Take decoded output: the scrollback and the screen. Returns its prompt marks."""
        with self.lock:
            size = len(text.encode("utf-8", "surrogatepass"))
            self._chunks.append(text)
            self._chunk_bytes.append(size)
            self._scroll_bytes += size
            while self._scroll_bytes > SCROLLBACK_BYTES and self._chunks:
                excess = self._scroll_bytes - SCROLLBACK_BYTES
                if self._chunk_bytes[0] <= excess:
                    self._scroll_bytes -= self._chunk_bytes.pop(0)
                    self._chunks.pop(0)
                else:
                    head = self._chunks[0].encode("utf-8", "surrogatepass")[excess:]
                    kept = head.decode("utf-8", "ignore")
                    self._chunks[0] = kept
                    new = len(kept.encode("utf-8", "surrogatepass"))
                    self._scroll_bytes -= self._chunk_bytes[0] - new
                    self._chunk_bytes[0] = new
            self.produced += len(text.encode("utf-16-le", "surrogatepass")) // 2
            now = self.last_output = time.monotonic()
            was_up = self.screen.editor_ready
            was_paste = self.screen.editor_up
            prompts = self.screen.feed(text)
            if prompts:
                self.marked_at = now
                self.marks += len(prompts)
                self.reading = True
            if self.screen.editor_ready and (prompts or not was_up):
                self.editor_at = now
            if self.screen.editor_up and (prompts or not was_paste):
                self.paste_at = now
            return prompts

    def typed(self, text: str) -> bytes:
        """``text`` (one or more lines) as typed keys, ending with Enter."""
        with self.lock:
            bracketed = self.screen.bracketed
        if bracketed:
            # fish expands an abbreviation under the cursor on Enter, pasted or not; after a
            # space the cursor is on no word. Not after a backslash, which would escape it.
            ends_escaped = (len(text) - len(text.rstrip("\\"))) % 2 == 1
            if self.kind == "fish" and not ends_escaped:
                text += " "
            return (_PASTE_START + text + _PASTE_END + "\r").encode("utf-8")
        return ("\r".join(text.split("\n")) + "\r").encode("utf-8")


#: Keys that end a line the person typed (`TerminalHost.input`).
_LINE_ENDS = ("\r", "\n", "\x0f")


class TerminalHost:
    """The terminal tabs of one engine session."""

    available = True

    def __init__(
        self,
        project_root: Path,
        env: dict[str, str],
        emit: Emit | None = None,
        *,
        shell: str | None = None,
    ) -> None:
        self._root = project_root
        self._env = dict(env)
        self._env["TERM"] = "xterm-256color"
        self._env["COLORTERM"] = "truecolor"
        self._emit = emit
        self._shell = shell or integration.person_shell(self._env)
        self._state_dir = Path(tempfile.mkdtemp(prefix="cogito-terminal-"))
        #: In every prompt mark, and only in our 0700 startup files (`integration`).
        self._nonce = secrets.token_hex(16)
        self._lock = threading.Lock()
        self._tabs: dict[str, Tab] = {}
        self._count = 0
        self._closed = False

    @property
    def shell(self) -> str:
        return self._shell

    @property
    def root(self) -> Path:
        """Where every tab's shell starts."""
        return self._root

    # -- events -----------------------------------------------------------------------------

    def _send(self, event: dict[str, Any]) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event)
        except Exception:  # noqa: BLE001 - a window that went away never stops a shell
            log.debug("terminal event not delivered", exc_info=True)

    # -- tabs -------------------------------------------------------------------------------

    def tabs(self) -> list[dict[str, Any]]:
        with self._lock:
            tabs = list(self._tabs.values())
        return [tab.describe() for tab in tabs]

    def has_tabs(self) -> bool:
        with self._lock:
            return bool(self._tabs)

    def tab(self, tab_id: str) -> Tab | None:
        with self._lock:
            return self._tabs.get(tab_id)

    def all_tabs(self) -> list[Tab]:
        with self._lock:
            return list(self._tabs.values())

    def next_title(self) -> str:
        """The title the next tab opened will have."""
        with self._lock:
            return f"Terminal {self._count + 1}"

    def find(self, name: str) -> Tab | None:
        """A tab by its id, its title or its number ("t2", "Terminal 2", "2")."""
        wanted = name.strip().lower()
        with self._lock:
            tabs = list(self._tabs.values())
        for tab in tabs:
            number = tab.title.rsplit(" ", 1)[-1]
            if wanted in (tab.id.lower(), tab.title.lower(), number):
                return tab
        return None

    def open(
        self,
        *,
        command: str | None = None,
        cols: int | None = None,
        rows: int | None = None,
        opened_by: str = "person",
        focus: bool = True,
    ) -> Tab:
        """Start a shell in a new tab. With ``command`` (one or more lines), it is typed at
        the shell's first prompt, however long that takes."""
        with self._lock:
            if self._closed:
                raise RuntimeError("the terminals of this session are closed")
            self._count += 1
            number = self._count
        tab_id = f"t{number}"
        title = f"Terminal {number}"
        start = integration.launch(self._shell, self._state_dir, self._env, self._nonce)
        env = {**self._env, **start.env}
        master, slave = os.openpty()
        try:
            _set_size(slave, cols or _DEFAULT_COLS, rows or _DEFAULT_ROWS)
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _LAUNCHER, *start.argv],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=str(self._root),
                env=env,
                close_fds=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        tab = Tab(
            tab_id, title, self._root, opened_by, process, master, start.integrated, start.kind,
            self._nonce,
        )
        if command:
            tab.pending = command
            tab.touched = opened_by == "person"
        elif opened_by == "person":
            tab.touched = True
        with self._lock:
            closed = self._closed
            if not closed:
                self._tabs[tab_id] = tab
        if closed:
            # `close_all` ran while this shell started.
            threading.Thread(target=self._read, args=(tab,), daemon=True).start()
            _end(tab)
            raise RuntimeError("the terminals of this session are closed")
        self._send({"type": "terminal_opened", "tab": tab.describe(), "focus": focus})
        threading.Thread(
            target=self._read, args=(tab,), name=f"cogito-terminal-{tab_id}", daemon=True
        ).start()
        if command and not tab.integrated:
            # Nothing will say the shell is ready; the terminal keeps what is typed early.
            self._type(tab, command)
        return tab

    def input(self, tab: Tab, data: str) -> None:
        """What the person typed: written as it is, and the tab is theirs from now on."""
        # Every key that ends a line: Enter, Ctrl-J, and Ctrl-O (bash and zsh run the line
        # with it). Counting one too many only makes a queued line wait longer.
        entered = sum(data.count(key) for key in _LINE_ENDS)
        with tab.lock:
            tab.touched = tab.keyed = True
            for _ in range(entered):
                # The editor reading now takes this line, and the next prompt answers it.
                # Otherwise the next editor takes it (after any prompt an earlier line is
                # still owed), and the prompt after that one answers it.
                if tab.reading:
                    owed = tab.marks + 1
                else:
                    owed = max(tab.owed_prompt, tab.marks + 1) + 1
                tab.owed_prompt = max(tab.owed_prompt, owed)
                tab.reading = False
            starts = entered and tab.integrated and tab.ready and not tab.running
            if starts:
                tab.running = True
        if starts:
            self._send({"type": "terminal_status", "id": tab.id, "running": True})
        _write(tab, data.encode("utf-8", "replace"))

    def run_line(
        self, tab: Tab, line: str, *, untouched: bool = False
    ) -> tuple[int, int] | None:
        """Type ``line`` and Enter. Returns ``(screen mark, prompt count)`` from before it,
        for `wait` and for reading what it printed. With ``untouched``, nothing is typed
        and None is returned once the person has typed in the tab: checked under the write
        lock, so their keys come either before (and the line is refused) or after it."""
        with tab.write_lock:
            with tab.lock:
                if untouched and tab.touched:
                    return None
                mark = tab.screen.mark()
                prompts = tab.prompts
                tab.command = line
                tab.reading = False
                if tab.integrated:
                    tab.running = True
            _write_held(tab, tab.typed(line))
        if tab.integrated:
            self._send({"type": "terminal_status", "id": tab.id, "running": True})
        return mark, prompts

    def _type(self, tab: Tab, line: str, *, at_prompt: bool = False) -> str:
        """Type the Run button's line: "typed". With ``at_prompt`` (the shell reported its
        prompt), nothing is typed when the shell's line editor may not be the one reading:
        "again" when the person pressed Enter after the last prompt (`wait_to_type` waits
        for its answer), "busy" when a program holds the terminal or by the marks a line is
        running. Checked under the write lock, so the person's keys come either before (and
        nothing is typed) or after it."""
        with tab.write_lock:
            with tab.lock:
                if at_prompt and tab.prompts < tab.owed_prompt:
                    return "again"
                if at_prompt and (tab.running or not tab.shell_in_front()):
                    return "busy"
                tab.reading = False
                if tab.integrated:
                    tab.running = True
                # The person typed before the first prompt: their half line would go in
                # front of the line they clicked Run on.
                clear = _CLEAR_LINE.encode() if tab.keyed else b""
            _write_held(tab, clear + tab.typed(line))
        if tab.integrated:
            self._send({"type": "terminal_status", "id": tab.id, "running": True})
        return "typed"

    def _type_when_ready(self, tab: Tab, line: str) -> None:
        # The first prompt mark came; wait for the line editor, never typing before it, and
        # for the prompt that answers an Enter the person pressed meanwhile.
        deadline = time.monotonic() + _QUEUED_WAIT
        while True:
            outcome = self.wait_to_type(tab, deadline - time.monotonic())
            if outcome == "ready":
                outcome = self._type(tab, line, at_prompt=True)
                if outcome == "again":
                    continue
            break
        if outcome == "typed":
            return
        with tab.lock:
            exited = tab.exited
        if not exited:
            self._send({"type": "terminal_unsent", "id": tab.id, "reason": outcome})

    def wait_to_type(self, tab: Tab, timeout: float, cancel: Any = None) -> str:
        """Until a line may be typed at the shell's prompt: "ready", "busy", "not_ready" or
        "cancelled". The line editor must be up after a prompt (`wait_ready`). When the
        person pressed Enter in the tab, the prompt that answers it must come first, then
        the editor again: "busy" when it does not come in time (what they started still
        runs, or reads the terminal). And at that prompt the shell itself must hold the
        terminal, with no line running by the marks, else "busy"."""
        deadline = time.monotonic() + timeout
        while True:
            if not self.wait_ready(tab, deadline - time.monotonic(), cancel):
                if cancel is not None and cancel.cancelled:
                    return "cancelled"
                return "not_ready"
            with tab.lock:
                if tab.prompts >= tab.owed_prompt:
                    if tab.integrated and (tab.running or not tab.shell_in_front()):
                        return "busy"
                    return "ready"
                while tab.prompts < tab.owed_prompt:
                    if tab.exited:
                        return "not_ready"
                    if cancel is not None and cancel.cancelled:
                        return "cancelled"
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return "busy"
                    tab.lock.wait(min(left, 0.1))

    def wait_ready(self, tab: Tab, timeout: float, cancel: Any = None) -> bool:
        """Until the shell's line editor is up after its first prompt (or, without
        integration, its output has been quiet a moment)."""
        deadline = time.monotonic() + timeout
        with tab.lock:
            while True:
                if tab.exited:
                    return False
                now = time.monotonic()
                quiet = now - tab.last_output
                since = tab.editor_since() if tab.integrated else None
                # Once the editor is up, let the prompt finish drawing: a line typed at once
                # is echoed above it. And give bracketed paste a moment to be turned on.
                if (
                    since is not None
                    and (tab.screen.bracketed or now - since >= _PASTE_GRACE)
                    and (quiet > _SETTLE or now - since >= _SETTLE_MAX)
                ):
                    return True
                if not tab.integrated and quiet > 0.5:
                    return True
                left = deadline - time.monotonic()
                if left <= 0 or (cancel is not None and cancel.cancelled):
                    return False
                tab.lock.wait(min(left, 0.1))

    def wait(self, tab: Tab, prompts: int, timeout: float, cancel: Any = None) -> str:
        """Until a prompt after ``prompts`` comes: "prompt", "exited", "timeout" or
        "cancelled"."""
        deadline = time.monotonic() + timeout
        with tab.lock:
            while True:
                if tab.prompts > prompts:
                    return "prompt"
                if tab.exited:
                    return "exited"
                if cancel is not None and cancel.cancelled:
                    return "cancelled"
                left = deadline - time.monotonic()
                if left <= 0:
                    return "timeout"
                tab.lock.wait(min(left, 0.1))

    def resize(self, tab: Tab, cols: int, rows: int) -> None:
        # Under the write lock, as `_exited` closes the terminal: an ended tab's descriptor
        # number may already be another tab's.
        with tab.write_lock:
            with tab.lock:
                if tab.exited or tab.master < 0:
                    return
            with contextlib.suppress(OSError):
                _set_size(tab.master, cols, rows)

    def quiet(self) -> None:
        """Forget the reports still owed: the conversation that typed those lines was
        reset. The tabs and what runs in them stay."""
        for tab in self.all_tabs():
            with tab.lock:
                tab.on_prompt = []

    def close(self, tab_id: str) -> bool:
        """End a tab's shell and everything it started. False for no such tab. For a tab
        whose shell already ended, only what is left of its session (`_end`)."""
        with self._lock:
            tab = self._tabs.pop(tab_id, None)
        if tab is None:
            return False
        _end(tab)
        return True

    def close_all(self) -> None:
        with self._lock:
            self._closed = True
            tabs = list(self._tabs.values())
            self._tabs.clear()
        for tab in tabs:
            _end(tab)
        import shutil

        shutil.rmtree(self._state_dir, ignore_errors=True)

    # -- the reader -------------------------------------------------------------------------

    def _read(self, tab: Tab) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending: list[str] = []
        size = 0
        since = 0.0
        next_poll = time.monotonic() + _POLL_SECONDS
        # The shell was seen gone: read what is already there, then stop.
        gone = False
        drained = 0
        while True:
            now = time.monotonic()
            due = min(since + JOIN_SECONDS, next_poll) if pending else next_poll
            timeout = 0.0 if gone else max(0.0, due - now)
            try:
                ready, _, _ = select.select([tab.master, tab._wake_r], [], [], timeout)
            except (OSError, ValueError):
                break
            if tab._wake_r in ready:
                break
            now = time.monotonic()
            if not gone and now >= next_poll:
                next_poll = now + _POLL_SECONDS
                # The shell ended while something it left behind still holds the terminal
                # open (`sleep 1000 &` then `exit`), or still prints: the tab has ended all
                # the same. Checked on a timer, not only when the output is quiet.
                gone = tab.process.poll() is not None
            if tab.master not in ready:
                self._flush(tab, pending)
                size = 0
                if gone:
                    break
                continue
            try:
                data = os.read(tab.master, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno not in (errno.EIO, errno.EBADF):
                    log.debug("terminal %s read failed", tab.id, exc_info=True)
                break
            if not data:
                break
            if gone:
                drained += len(data)
            text = decoder.decode(data)
            if text:
                prompts = tab.keep(text)
                if not pending:
                    since = now
                pending.append(text)
                size += len(text)
                # However busy the shell is, the window gets its output about every 16 ms,
                # and what waits is bounded.
                if prompts or size >= FLUSH_CHARS or time.monotonic() >= since + JOIN_SECONDS:
                    self._flush(tab, pending)
                    size = 0
                for status in prompts:
                    self._prompt(tab, status)
            if gone and drained >= _DRAIN_BYTES:
                break
        tail = decoder.decode(b"", final=True)
        if tail:
            tab.keep(tail)
            pending.append(tail)
        self._flush(tab, pending)
        self._exited(tab)

    def _flush(self, tab: Tab, pending: list[str]) -> None:
        if pending:
            data = "".join(pending)
            pending.clear()
            with tab.lock:
                end = tab.produced
            self._send({"type": "terminal_data", "id": tab.id, "data": data, "end": end})

    def _prompt(self, tab: Tab, status: int | None) -> None:
        with tab.lock:
            first = not tab.ready
            tab.ready = True
            tab.running = False
            tab.prompts += 1
            exit_code = None if first else status
            tab.last_status = exit_code
            tab.command = ""
            waiting, tab.on_prompt = tab.on_prompt, []
            pending, tab.pending = tab.pending, None
            tab.lock.notify_all()
        event: dict[str, Any] = {"type": "terminal_status", "id": tab.id, "running": False}
        if exit_code is not None:
            event["exit_code"] = exit_code
        self._send(event)
        for callback in waiting:
            _call(callback, exit_code)
        if pending:
            threading.Thread(
                target=self._type_when_ready, args=(tab, pending), daemon=True,
                name=f"cogito-terminal-{tab.id}-run",
            ).start()

    def _exited(self, tab: Tab) -> None:
        try:
            code = tab.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        if code is not None and code < 0:
            code = 128 - code
        with tab.lock:
            tab.exited = True
            tab.running = False
            tab.exit_code = code
            waiting, tab.on_prompt = tab.on_prompt, []
            tab.lock.notify_all()
        with tab.write_lock, tab.lock:
            for fd in (tab.master, tab._wake_r, tab._wake_w):
                with contextlib.suppress(OSError):
                    os.close(fd)
            tab.master = -1
        self._send({"type": "terminal_exited", "id": tab.id, "exit_code": code})
        for callback in waiting:
            _call(callback, None)


def _call(callback: Callable[[int | None], None], value: int | None) -> None:
    try:
        callback(value)
    except Exception:  # noqa: BLE001 - one report that fails never stops the reader
        log.exception("a terminal prompt callback failed")


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _write(tab: Tab, data: bytes, timeout: float = 2.0) -> None:
    """Write everything, waiting while the terminal's buffer is full, at most ``timeout``."""
    with tab.write_lock:
        _write_held(tab, data, timeout)


def _write_held(tab: Tab, data: bytes, timeout: float = 2.0) -> None:
    """`_write` for a caller that holds ``write_lock``."""
    with tab.lock:
        if tab.exited or tab.master < 0:
            return
    _write_fd(tab.master, data, timeout)


def _write_fd(fd: int, data: bytes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
            view = view[written:]
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                log.warning("terminal input dropped: the shell is not reading")
                return
            with contextlib.suppress(OSError, ValueError):
                select.select([], [fd], [], min(left, 0.1))
        except OSError:
            return


def _end(tab: Tab) -> None:
    """SIGHUP the shell's group and everything under it, as closing a terminal does, then
    SIGKILL what is left after a moment. The reader then reports the exit.

    A pid is signalled only while it is known to be ours. The shell's own pid, and the tree
    under it, only while /proc shows the process that started then (alive or a zombie the
    reader has not reaped): once reaped, the number may be another process's. What is left
    of its session is always ours: the kernel does not give out a session's number again
    while a process is still in it."""
    root = tab.process.pid
    now = _starttime(root)
    if tab.started is not None:
        reused = now is not None and now != tab.started
        mine = now is not None and not reused
    else:
        reused = False
        mine = tab.process.returncode is None
    pids = set() if reused else _belonging(root, tree=mine)
    groups: set[int] = set()
    for pid in pids:
        with contextlib.suppress(OSError):
            groups.add(os.getpgid(pid))
    groups.discard(os.getpgid(0))

    def send(sig: signal.Signals) -> None:
        for group in groups:
            with contextlib.suppress(OSError):
                os.killpg(group, sig)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)

    send(signal.SIGHUP)
    deadline = time.monotonic() + _CLOSE_GRACE
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    send(signal.SIGKILL)
    with tab.lock:
        if not tab.exited:
            with contextlib.suppress(OSError):
                os.write(tab._wake_w, b"x")
    with contextlib.suppress(subprocess.TimeoutExpired):
        tab.process.wait(timeout=2)


def _stat(pid: str) -> tuple[int, int, str] | None:
    """``(ppid, session, state)`` from /proc, or None for a process that is gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[3]), fields[0]
    except (OSError, IndexError, ValueError):
        return None


def _starttime(pid: int) -> int | None:
    """When ``pid`` started, in clock ticks since boot (/proc), or None for no such process
    (or no /proc)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _belonging(root: int, *, tree: bool = True) -> set[int]:
    """Every process still in the shell's session (one whose parent exited leaves the tree,
    not the session) and, with ``tree``, the shell and every process under it."""
    parents: dict[int, list[int]] = {}
    found = {root} if tree else set()
    with contextlib.suppress(OSError):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            stat = _stat(entry)
            if stat is None:
                continue
            ppid, session, _ = stat
            parents.setdefault(ppid, []).append(int(entry))
            if session == root:
                found.add(int(entry))
    pending = [root] if tree else []
    while pending:
        for child in parents.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _alive(pid: int) -> bool:
    stat = _stat(str(pid))
    return stat is not None and stat[2] != "Z"
