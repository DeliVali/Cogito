"""Asking the user, and what an answer means.

The guard never talks to a person directly. It builds a request and hands it to whatever
can ask: a terminal prompt, a toast when the desktop window is hidden, or a policy that
answers without asking. So every surface shares one decision path.

When nothing can ask, the default is to refuse. An unattended agent that deletes because no
one was there to say no is the failure this module exists to prevent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ApprovalItem:
    """One named path the command acts on, as the person should see it."""

    path: Path
    #: "delete", "overwrite" or "move".
    action: str
    #: "file", "dir", "link" or "missing" (not there now, or not known yet).
    kind: str = "missing"
    #: For a directory: how many files it holds, counted up to a cap. None otherwise.
    entries: int | None = None
    #: True when the count stopped at the cap.
    capped: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What the user is being asked to allow, in terms they can check."""

    #: One line, in English, for the terminal: "Delete 3 files?". The window builds its own
    #: sentence from the fields below.
    summary: str
    #: The command as written, so the user approves what will actually run.
    command: str
    #: Resolved paths, when they could be resolved.
    targets: tuple[Path, ...] = ()
    #: Targets that could not be resolved (a variable, a substitution, a glob).
    unresolved: tuple[str, ...] = ()
    #: For each of ``unresolved``, the directory it is read from, or None when a `cd` the
    #: command makes cannot be known from its text.
    unresolved_from: tuple[Path | None, ...] = ()
    #: Why this needs asking at all.
    reason: str = ""
    #: True when nothing is lost for good: what is deleted goes to the trash (or a copy
    #: does), or it is only moved.
    recoverable: bool = True
    #: A path prefix the user may choose to stop being asked about this session.
    suggested_scope: Path | None = None
    #: What the command mostly does: "delete", "overwrite", "move", or "command" when it
    #: destroys files it does not name (`git clean`, `find -delete`), "install_packages", or
    #: "run_in_terminal" (a line typed into the person's terminal).
    action: str = "delete"
    #: Each named path and what happens to it.
    items: tuple[ApprovalItem, ...] = ()
    #: "trash" (it goes to the trash, or a copy does), "kept" (only moved) or "none".
    recovery: str = "trash"
    #: True when some of what it destroys is not named in the command.
    targets_unknown: bool = False
    #: Directories under which it deletes what matches (`find DIR -delete`).
    within: tuple[Path, ...] = ()
    #: The verbs of what destroys files it does not name and is no `find` or `rsync`
    #: (`git clean`, `git reset --hard`, `xargs rm`): nothing bounds them to a folder.
    sweeps: tuple[str, ...] = ()
    #: What a dry run says it would remove (`git clean -n`), as git prints it.
    listed: tuple[str, ...] = ()
    #: How many more the dry run named beyond `listed`.
    listed_more: int = 0
    #: True when a target is outside the project.
    outside: bool = False
    #: For `action == "install_packages"` (the app only, `guard/packages.py`): the package
    #: names, the manager that installs them, and where they come from.
    packages: tuple[str, ...] = ()
    manager: str = ""
    source: str = ""
    #: For `action == "external_tool"`: the MCP server, its tool, the tool's title and
    #: whether it may change something; ``command`` holds the arguments as sent.
    server: str = ""
    tool: str = ""
    tool_title: str = ""
    changes: bool = True
    #: Asked about every time, automatic mode included (`ExternalTool.every_time`).
    every_time: bool = False
    #: For `action == "run_in_terminal"` (the app only): the terminal tab the line goes to
    #: (or the new tab's title) and where its shell runs; ``command`` is the line.
    tab_title: str = ""
    cwd: str = ""
    #: True when a PATH shim asked, with the names the shell expanded and the folder the
    #: command really ran in (`ShimBroker`), not the loop with the command's text.
    from_shim: bool = False
    #: APP ONLY: set when the composer's automatic mode is on and still asks about this
    #: (`guard.auto`): "project", "credentials", "cogito", "home_folder" or "unknown".
    kept_asking: str = ""


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    allowed: bool
    #: Set when the user chose "yes, and stop asking for this directory".
    remember_prefix: Path | None = None
    #: Set when the user chose «Permitir en esta sesión» for an MCP server's tool.
    remember_tool: bool = False
    note: str = ""
    #: Who decided: "user" when a person answered, "timeout" when nobody did in time,
    #: "interrupted" when the turn was stopped while the question waited, "nobody" when there
    #: was no one to ask (`deny_all`), "auto" when the composer's automatic mode allowed it
    #: (`guard.auto`).
    reason: str = "user"


Approver = Callable[[ApprovalRequest], ApprovalResponse]


def deny_all(request: ApprovalRequest) -> ApprovalResponse:
    """The default. Nothing is destroyed when there is nobody to ask."""
    return ApprovalResponse(
        allowed=False,
        note="no one was available to approve this; nothing was deleted",
        reason="nobody",
    )


@dataclass
class Grants:
    """Path prefixes the user has stopped being asked about, for this session only.

    Nothing here survives the process. A permission that outlives the conversation it was
    given in is one the user has forgotten granting.
    """

    prefixes: list[Path] = field(default_factory=list)

    def allows(self, path: Path) -> bool:
        return any(path == prefix or prefix in path.parents for prefix in self.prefixes)

    def add(self, prefix: Path) -> None:
        resolved = prefix.resolve() if prefix.exists() else prefix.absolute()
        if resolved not in self.prefixes:
            self.prefixes.append(resolved)

    def covers(self, paths: Sequence[Path]) -> bool:
        return bool(paths) and all(self.allows(path) for path in paths)


class Waiting:
    """Wall-clock time during which a person was being asked something.

    A command's timeout is there to catch a hung process, not a person reading. Without
    this both share one clock: an approval that takes a minute spends a minute of the
    command's budget, and a careful person makes the command fail.

    Only the outermost question counts, so two questions open at once count their overlap
    once, not twice.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0.0
        self._since: float | None = None
        self._depth = 0

    def __enter__(self) -> Waiting:
        with self._lock:
            if self._depth == 0:
                self._since = time.monotonic()
            self._depth += 1
        return self

    def __exit__(self, *_: object) -> None:
        with self._lock:
            self._depth -= 1
            if self._depth == 0 and self._since is not None:
                self._total += time.monotonic() - self._since
                self._since = None

    def elapsed(self) -> float:
        """Total so far, including a question that is still open.

        The open interval must be included: a deadline is checked while the person is
        still deciding, which is exactly when the discount matters.
        """
        with self._lock:
            open_interval = 0.0 if self._since is None else time.monotonic() - self._since
            return self._total + open_interval


#: How much of a command a recorded answer keeps. The window's pill ellipsizes anyway.
_RECORDED_COMMAND_CHARS = 400
#: How many entries it holds undrained.
_LEDGER_LIMIT = 64


class Ledger:
    """What the guard decided while one tool call ran, newest last, for that call's record.

    A question put to a person, and a refusal that was never put to anyone (a PATH shim
    turning down a path it may not touch), both land here. The loop drains it around each
    call (`Engine`'s ``take_answers``) and keeps what it holds with the call, so the thread
    read back after a restart still has the pill each question left, and a command a shim
    refused reads as not run. The shim answers from another thread, so every access takes
    the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict[str, object]] = []

    def note_answer(
        self, request: ApprovalRequest, response: ApprovalResponse, waited: float
    ) -> None:
        """A person's (or the timeout's, or the interrupt's) answer to ``request``, and how
        long it took. The fields are those the window draws the pill from."""
        entry: dict[str, object] = {
            "asked": True,
            "allowed": bool(response.allowed),
            "reason": response.reason,
            "action": request.action,
            "command": request.command[:_RECORDED_COMMAND_CHARS],
            "summary": request.summary[:_RECORDED_COMMAND_CHARS],
            "waited_ms": max(0, int(waited * 1000)),
        }
        if request.packages or request.manager:
            entry["packages"] = list(request.packages)
            entry["manager"] = request.manager
            entry["source"] = request.source
        if request.tab_title:
            entry["tab_title"] = request.tab_title
        if request.server:
            # An MCP tool: the pill names it by server and title, not by its arguments.
            entry["server"] = request.server
            entry["tool"] = request.tool
            entry["tool_title"] = request.tool_title
        self._append(entry)

    def note_refusal(self, command: str) -> None:
        """A refusal nobody was asked about. It draws no pill; it only says the call was
        turned down."""
        self._append(
            {
                "asked": False,
                "allowed": False,
                "reason": "guard",
                "command": command[:_RECORDED_COMMAND_CHARS],
                "waited_ms": 0,
            }
        )

    def _append(self, entry: dict[str, object]) -> None:
        with self._lock:
            self._entries.append(entry)
            # Nothing drains it in `chat`; the newest are the ones a call can still own.
            del self._entries[:-_LEDGER_LIMIT]

    def take(self) -> list[dict[str, object]]:
        """Everything noted since the last call, which it clears."""
        with self._lock:
            taken, self._entries = self._entries, []
        return taken
