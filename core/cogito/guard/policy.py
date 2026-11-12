"""The one place an action is allowed, refused, or put to the user.

Two rules, exactly as asked for:

1. Never delete without the user saying so.
2. Never delete or modify what belongs to the operating system, or Cogito's own record of
   the session, the project's `.cogito/` (`guard/harness.py`).

There is no third rule, no capability model and no policy file. Changing files inside the
project is the product working, so `write` and `edit` pass straight through: the undo store
makes them reversible, which is a better guarantee than a prompt. The one place inside the
project they may not write is `.cogito/`, because the undo store lives there.

One app-only addition (`Guard(packages=True)`, set for the desktop app and nowhere else): a
system package install, a full system upgrade and an AUR install (paru, yay) are put to the
user, and the rest of what a package manager can do is refused. See `guard/packages.py`.
`chat` and the benchmark never turn it on.

Another, only where the app's terminal tabs exist (`ask_terminal`): a write, edit, chmod,
redirection or download (``curl -o``, ``wget -O``) that creates or changes a file a new
tab's shell loads at start, or a file named
like `sudo` anywhere, is refused (`Platform.terminal_refusal`). A line the person approves
runs in a new tab so that nothing earlier can change it; such a file would.

A sandboxed run adds one containment, not a rule: `confine_to`. Only the shell runs under
bubblewrap; the file tools run in this process and could otherwise reach any absolute path
on the machine. So in a sandboxed run a file tool's path must resolve (symlinks followed)
inside the project, and so must a deletion the shims forward. It is off unless sandboxed:
the interactive product keeps full control of the machine.

A target that cannot be resolved (a variable, a substitution, a glob) is never assumed
harmless. It is shown to the user as written, since that is what they are approving. When
it is being deleted and its expansion could be `/`, the home directory or a protected path
(`$HOME`, `"$dir"`, `/*`), it is refused instead. No answer a person could give makes
`rm -rf "$dir"` safe without knowing what `$dir` is, and the literal path costs the model
one more word.
"""

from __future__ import annotations

import itertools
import os
import posixpath
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from cogito.guard import platform as platform_module
from cogito.guard.allowed_tools import AllowedTools
from cogito.guard.approval import (
    ApprovalItem,
    ApprovalRequest,
    ApprovalResponse,
    Approver,
    Grants,
    Ledger,
    Waiting,
    deny_all,
)
from cogito.guard.bashscan import Destructive, Simple, scan
from cogito.guard.harness import HarnessDirectory
from cogito.guard.packages import AUR_INSTALL, INSTALL, SYSTEM_UPGRADE, PackageInstall
from cogito.guard.packages import judge as judge_packages
from cogito.guard.platform.base import Platform, Ruling, Zone

#: Anything in a command's text that can write a file: a redirection or a program that
#: copies, moves, links, downloads or edits in place. Read only for commands the scanner
#: could not parse (`Guard._terminal_unparsed`).
_WRITES_TEXT = re.compile(
    r">|\b(cp|mv|install|ln|rsync|dd|tee|curl|wget|sed|perl|chmod|touch|truncate"
    r"|python3?|gzip|tar|unzip|git)\b"
)

#: Characters that mean a word is not a path yet: the shell still has to expand it.
#: `~` is left out on purpose: it expands deterministically, and treating it as unknown
#: let `rm -rf ~/.ssh` past the protected-path check.
_UNRESOLVED = ("$", "`", "*", "?", "[")

#: What in a command plainly changes which `rm` its later words run, so that a PATH shim
#: may be passed by: PATH assigned, exported or declared, the shell's hash table, aliases,
#: builtins switched, a function named like a shimmed verb, `eval`, `exec`, `BASH_ENV`.
#: Found by review on 2026-11-03: `export PATH=/usr/bin:$PATH; rm x` was left to a shim
#: that never ran. A pattern over the text cannot follow every way bash can do this
#: (`readonly`, `trap`, a name built at runtime); it only keeps the plain ones from
#: being deferred, and leaves `source .venv/bin/activate && rm -rf dist` to the shim.
_LOOKUP_CHANGES = re.compile(
    r"\bPATH\+?=|\bBASH_ENV\b"
    r"|\b(?:export|declare|typeset|readonly|local)\b[^;&|\n]*\bPATH\b"
    r"|(?:^|[;&|(\n{])\s*(?:builtin\s+|command\s+)?(?:hash|alias|enable|eval|exec)(?=\s|$)"
    r"|\b(?:rm|rmdir|shred|srm|unlink|truncate)\s*\(\s*\)"
    r"|\bfunction\s+(?:rm|rmdir|shred|srm|unlink|truncate)\b"
)

#: The tools that take a path and run in the core's own process, outside any sandbox.
#: Each reads `path` relative to the project root, and an empty one means the root.
_PATH_TOOLS = frozenset({"read", "grep", "glob", "write", "edit"})

#: The file tools that write. Their `path` may not land inside `.cogito/`.
_WRITE_TOOLS = frozenset({"write", "edit"})

#: Same containment, but the argument is a list (`paths`) rather than one `path`.
#: An empty or omitted list means the whole project, which needs no check.
_PATHS_TOOLS = frozenset({"check"})


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class Target:
    raw: str
    path: Path | None
    ruling: Ruling | None

    @property
    def resolved(self) -> bool:
        return self.path is not None


@dataclass(frozen=True, slots=True)
class ExternalTool:
    """A tool from an MCP server, as the person is asked about it."""

    server: str
    tool: str
    #: The tool's own title, else its name with spaces.
    title: str
    #: False only for a tool the server declares read-only.
    changes: bool
    #: «Permitir» lasts this session only, never saved for later ones: Cogito's own tools
    #: that see and drive the person's screen or a browser.
    session_only: bool = False
    #: Asked about on every call, never remembered, and kept asking in automatic mode:
    #: `mcp_add`, whose every call saves a different program to run in every later
    #: conversation.
    every_time: bool = False

    @property
    def label(self) -> str:
        return f"{self.server} · {self.tool}"


@dataclass(frozen=True, slots=True)
class TerminalLine:
    """A line the model asks to type into one of the person's terminal tabs (app only):
    asked about every time, on the exact line, and never remembered."""

    command: str
    #: The title the new tab it runs in will have.
    tab_title: str
    #: Where that tab's shell starts.
    cwd: str
    #: Always "new": every line runs in a tab opened for it, never in one that an earlier
    #: line left state in (`tools.terminal.tools`).
    tab: str = "new"


#: The tool whose calls are `TerminalLine`s (`tools/terminal`).
TERMINAL_TOOL = "run_in_terminal"


@dataclass(frozen=True, slots=True)
class Verdict:
    decision: Decision
    reason: str
    operations: tuple[Destructive, ...] = ()
    targets: tuple[Target, ...] = ()
    #: Set with `Decision.ASK` for a system package install (`guard/packages.py`, app only).
    install: PackageInstall | None = None
    #: Set with `Decision.ASK` for a tool from an MCP server the person is asked about
    #: (`ask_before`).
    external: ExternalTool | None = None
    #: Set with `Decision.ASK` for a line typed into the person's terminal (`ask_terminal`).
    terminal: TerminalLine | None = None
    #: Where the command's relative words were read from, when `evaluate` was told (a PATH
    #: shim's own folder); `request_for` reads its patterns from there too.
    here: Path | None = field(default=None, compare=False, repr=False)
    #: True when the command itself may change which `rm` the shell runs (PATH, `hash`,
    #: `alias`, a function by that name, `source`, `eval`, `exec`): the shim may then be
    #: passed by, so the loop asks and nothing is said to go to the trash.
    lookup_changed: bool = field(default=False, compare=False, repr=False)

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def _resolve(raw: str, cwd: Path) -> Path | None:
    """Turn a shell word into a path, or admit that it is not one yet."""
    if not raw:
        return None
    expanded = os.path.expanduser(raw)
    if any(marker in expanded for marker in _UNRESOLVED):
        return None
    candidate = Path(expanded)
    return candidate if candidate.is_absolute() else (cwd / candidate)


def _literal_prefix(raw: str, cwd: Path) -> Path | None:
    """The part of a word the shell cannot change, so a glob can still be judged.

    `rm -rf /etc/*` never resolves to a path, but everything it can possibly match is
    inside `/etc`, and that is enough to refuse it.
    """
    expanded = os.path.expanduser(raw)
    cut = min(
        (expanded.index(marker) for marker in _UNRESOLVED if marker in expanded),
        default=len(expanded),
    )
    head = expanded[:cut]
    if "/" not in head:
        return None
    directory = head.rsplit("/", 1)[0] or "/"
    candidate = Path(directory)
    return candidate if candidate.is_absolute() else (cwd / candidate)


def _brace_group_end(text: str, start: int) -> int | None:
    """Where a brace expansion opened at ``start`` closes, or None if it is not one."""
    end = text.find("}", start + 1)
    if end < 0:
        return None
    body = text[start + 1 : end]
    return end if ("," in body or ".." in body) else None


def _expansion_end(text: str, start: int) -> int:
    """Just past the `$...` expansion that starts at ``start``."""
    if start + 1 >= len(text):
        return start + 1
    opener = text[start + 1]
    if opener in "({":
        closer = ")" if opener == "(" else "}"
        depth = 0
        for index in range(start + 1, len(text)):
            if text[index] == opener:
                depth += 1
            elif text[index] == closer:
                depth -= 1
                if depth == 0:
                    return index + 1
        return len(text)
    if opener.isalpha() or opener == "_":
        index = start + 1
        while index < len(text) and (text[index].isalnum() or text[index] == "_"):
            index += 1
        return index
    return start + 2  # $1, $@, $*, $#, $?, $$, $!, $-


def _pattern_of(text: str) -> str:
    """A shell word as a regular expression over the paths it can expand to.

    A parameter, a substitution or a brace group can become anything, `/` included; a glob
    stays inside one path component.
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "$":
            out.append(".*")
            index = _expansion_end(text, index)
        elif char == "`":
            close = text.find("`", index + 1)
            out.append(".*")
            index = len(text) if close < 0 else close + 1
        elif char == "{" and (close := _brace_group_end(text, index)) is not None:
            out.append(".*")
            index = close + 1
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[" and (close := text.find("]", index + 1)) > index + 1:
            out.append("[^/]")
            index = close + 1
        else:
            out.append(re.escape(char))
            index += 1
    return "".join(out)


def _expansions(raw: str, cwd: Path) -> re.Pattern[str] | None:
    """Every absolute path an unexpanded word could turn into, or None if it expands to
    nothing but itself.

    The literal directory in front of the first expansion is resolved the way the shell
    will see it; whatever follows is a pattern. A `..` after an expansion could climb
    anywhere, so it makes the whole word a wildcard.
    """
    expanded = os.path.expanduser(raw)
    markers = [expanded.index(m) for m in (*_UNRESOLVED, "{") if m in expanded]
    starts = [i for i in markers if expanded[i] != "{" or _brace_group_end(expanded, i)]
    if not starts:
        return None
    cut = min(starts)
    if ".." in expanded[cut:].split("/"):
        return re.compile(".*", re.DOTALL)
    head = expanded[:cut]
    if "/" in head:
        split = head.rindex("/")
        directory = head[:split] or "/"
        base = directory if directory.startswith("/") else str(cwd / directory)
        tail = expanded[split + 1 :]
    elif head or expanded[cut] in "*?[":
        base, tail = str(cwd), expanded
    else:
        # It starts with an expansion: absolute or relative, it could be anything.
        return re.compile(_pattern_of(expanded), re.DOTALL)
    prefix = os.path.realpath(base).rstrip("/")
    return re.compile(re.escape(prefix) + "/" + _pattern_of(tail), re.DOTALL)


def _anchored(raw: str) -> bool:
    """True when ``raw`` names the same place from any directory: `/x`, `~/x`."""
    return os.path.isabs(os.path.expanduser(raw))


#: Short options that take a value, so a combined word such as ``-fsSLo FILE`` is read the
#: way the program reads it: the letter after one of these starts its value. From
#: ``curl --help all`` (8.23.0) and ``wget --help`` (1.25.0); curl's ``-h`` is left out, so
#: a doubtful word is read as holding more options, never fewer.
_VALUED_SHORT = {"curl": "ACDEFHKPQTUXYbcdemortuwxyz", "wget": "ABDIOPQRTUXaeilnotw"}
#: The option that names the file a download is written to, short and long.
_OUTPUT_OPTION = {"curl": ("o", "output"), "wget": ("O", "output-document")}
#: The shortest prefix wget accepts for ``--output-document`` (``--output`` alone is
#: ambiguous with ``--output-file``). curl takes no abbreviation.
_WGET_OUTPUT_PREFIX = "output-d"


def _download_outputs(words: Sequence[str]) -> list[str]:
    """The files a ``curl -o`` or ``wget -O`` in ``words`` writes the download to. ``-``
    (standard output) is not a file."""
    if not words:
        return []
    program = posixpath.basename(words[0])
    if program not in _OUTPUT_OPTION:
        return []
    short, long = _OUTPUT_OPTION[program]
    valued = _VALUED_SHORT[program]
    found: list[str] = []
    index = 1
    while index < len(words):
        word = words[index]
        index += 1
        if word == "--":
            break
        if word.startswith("--"):
            name, equals, value = word[2:].partition("=")
            matches = name == long or (
                program == "wget" and len(name) >= len(_WGET_OUTPUT_PREFIX)
                and long.startswith(name)
            )
            if not matches:
                continue
            if not equals and index < len(words):
                value = words[index]
                index += 1
            found.append(value)
            continue
        if not word.startswith("-") or word == "-":
            continue
        letters = word[1:]
        for at, letter in enumerate(letters):
            if letter not in valued:
                continue
            value = letters[at + 1:]
            if not value and index < len(words):
                value = words[index]
                index += 1
            if letter == short:
                found.append(value)
            break
    return [value for value in found if value and value != "-"]


def _directory(cd: tuple[str, ...] | None, cwd: Path) -> Path | None:
    """Where a command runs after the `cd`s before it in the same command
    (`Destructive.cd`), or None when one of them cannot be known from the text."""
    if cd is None:
        return None
    current = cwd
    for word in cd:
        text = os.path.expanduser(word)
        if any(marker in text for marker in _UNRESOLVED):
            return None
        # Logical, as bash's own `cd` is: `cd link/..` comes back to where it started.
        current = Path(os.path.normpath(current / text))
    return current


def _anywhere(raw: str) -> re.Pattern[str]:
    """Every absolute path a relative word could name from a directory nobody knows."""
    normalized = posixpath.normpath(raw)
    if normalized == "." or ".." in normalized.split("/"):
        return re.compile(".*", re.DOTALL)
    return re.compile(".*/" + _pattern_of(normalized), re.DOTALL)


_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(?=/|$)")


def native(raw: str) -> str:
    """On Windows, a Git Bash path to a drive (`/c/Users/x`) as Windows names it
    (`C:/Users/x`), so the guard judges the file the command really reaches. Unchanged
    elsewhere, and for every other path."""
    if os.name != "nt":
        return raw
    return _MSYS_DRIVE.sub(lambda m: f"{m.group(1).upper()}:", raw, count=1)


def real_path(raw: str, cwd: Path) -> Path:
    """Where a file tool's ``path`` argument lands, every symlink followed.

    Mirrors the tools themselves: stripped, relative to ``cwd``, no ``~`` expansion, and
    empty meaning ``cwd``.
    """
    text = native(raw.strip())
    candidate = Path(text) if text else cwd
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return Path(os.path.realpath(candidate))


def deletion_path(raw: str, cwd: Path) -> Path:
    """What deleting ``raw`` removes: the entry itself, not what a final symlink names.

    `rm link` removes the link, so the last component is not followed; every directory on
    the way to it is.
    """
    candidate = Path(native(raw))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if candidate.name in {"", ".", ".."}:
        return Path(os.path.realpath(candidate))
    return Path(os.path.realpath(candidate.parent)) / candidate.name


def outside(path: Path, roots: Sequence[Path]) -> bool:
    """True when ``path`` (already resolved) lies under none of ``roots``."""
    return not any(path.is_relative_to(Path(os.path.realpath(root))) for root in roots)


def confinement_refusal(raw: str, resolved: Path, roots: Sequence[Path], what: str) -> str:
    """The refusal a sandboxed run gives, saying what to do instead."""
    shown = ", ".join(str(Path(os.path.realpath(root))) for root in roots)
    return (
        f"refused: {raw!r} resolves to {resolved}, which is outside the sandboxed project "
        f"({shown}). In a sandboxed run {what} only inside the project; use a path under it."
    )


def _describe(operations: tuple[Destructive, ...], targets: tuple[Target, ...]) -> str:
    verbs = sorted({operation.verb for operation in operations})
    named = [target.raw for target in targets]
    if named:
        shown = ", ".join(named[:3]) + (f" and {len(named) - 3} more" if len(named) > 3 else "")
        return f"{' and '.join(verbs)}: {shown}"
    return " and ".join(f"{o.verb} — {o.why}" for o in operations)


#: How many files a directory target's count goes up to, and how many paths a dry run
#: lists, before the card says "and more".
_HOLDINGS_CAP = 1000
_LISTED = 50


def _kind(operation: Destructive) -> str:
    """What one operation does to what it names: "delete", "overwrite", "move", or
    "command" when it destroys what it does not name."""
    if not operation.named or not operation.targets:
        return "command"
    if operation.overwrites:
        return "overwrite"
    if operation.verb == "mv":
        return "move"
    return "delete"


def _item(path: Path, action: str) -> ApprovalItem:
    """``path`` as the card shows it: a file, a directory and how many files it holds, a
    link, or nothing there yet."""
    if path.is_symlink():
        return ApprovalItem(path, action, "link")
    if path.is_dir():
        count = 0
        for _, _, files in os.walk(path):
            count += len(files)
            if count >= _HOLDINGS_CAP:
                return ApprovalItem(path, action, "dir", _HOLDINGS_CAP, True)
        return ApprovalItem(path, action, "dir", count)
    if path.exists():
        return ApprovalItem(path, action, "file")
    return ApprovalItem(path, action)


#: What the approval request calls each kind of package request.
INSTALL_ACTIONS = {
    INSTALL: "install_packages",
    SYSTEM_UPGRADE: "upgrade_system",
    AUR_INSTALL: "install_aur",
}


def _describe_install(install: PackageInstall) -> str:
    names = ", ".join(install.packages)
    if install.kind == SYSTEM_UPGRADE:
        return f"upgrade the system with {install.manager}"
    if install.kind == AUR_INSTALL:
        return f"install {names} from the AUR with {install.manager}"
    return f"install {names} with {install.manager}"


def _install_summary(install: PackageInstall) -> str:
    """The one-line question for the terminal, in English. The window builds its own."""
    names = ", ".join(install.packages)
    if install.kind == SYSTEM_UPGRADE:
        return f"Upgrade the system with {install.manager} ({install.source})?"
    if install.kind == AUR_INSTALL:
        return f"Install {names} from the AUR with {install.manager}?"
    return f"Install {names} with {install.manager} ({install.source})?"


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _summary(action: str, items: tuple[ApprovalItem, ...], outside: bool) -> str:
    """The one-line question for the terminal, in English. The window builds its own."""
    if action == "command":
        return "Run a command that deletes or discards files it does not name?"
    deleted = [item for item in items if item.action == "delete"]
    overwritten = [item for item in items if item.action == "overwrite"]
    moved = [item for item in items if item.action == "move"]
    parts: list[str] = []
    if moved:
        parts.append(f"move {_plural(len(moved), 'item', 'items')}")
    if deleted:
        if all(item.kind == "dir" for item in deleted):
            noun = _plural(len(deleted), "folder", "folders")
        elif all(item.kind == "file" for item in deleted):
            noun = _plural(len(deleted), "file", "files")
        else:
            noun = _plural(len(deleted), "item", "items")
        if len(deleted) == 1 and deleted[0].kind == "dir":
            held = deleted[0].entries or 0
            noun += f" ({_plural(held, 'file', 'files')}{'+' if deleted[0].capped else ''} inside)"
        parts.append(f"delete {noun}")
    if overwritten:
        parts.append(f"overwrite {_plural(len(overwritten), 'file', 'files')}")
    if not parts:
        parts.append("run a destructive command")
    text = " and ".join(parts)
    text = text[0].upper() + text[1:]
    return f"{text} from outside the project?" if outside else f"{text}?"


def _clean_dry_run(operation: Destructive, directory: Path | None) -> list[str]:
    """What `git clean` would remove, from git itself (`git clean -n`, the same options and
    paths). Empty when it cannot be asked: no repository, no git, an unknown directory."""
    if directory is None or len(operation.argv) < 2:
        return []
    keep: list[str] = []
    words = list(operation.argv[2:])
    index = 0
    paths = False
    while index < len(words):
        word = words[index]
        index += 1
        if paths or not word.startswith("-"):
            keep.append(word)
        elif word == "--":
            paths = True
            keep.append(word)
        elif word in {"-e", "--exclude"} and index < len(words):
            keep.extend((word, words[index]))
            index += 1
        elif word.startswith("--exclude="):
            keep.append(word)
        elif re.fullmatch(r"-[a-zA-Z]+", word):
            letters = "".join(letter for letter in word[1:] if letter in "dxX")
            if letters:
                keep.append(f"-{letters}")
            if word.endswith("e") and index < len(words):
                keep.extend(("-e", words[index]))
                index += 1
    try:
        result = subprocess.run(
            ["git", "clean", "-n", *keep], cwd=directory, capture_output=True, text=True,
            timeout=5, env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    prefix = "Would remove "
    return [line[len(prefix):] for line in result.stdout.splitlines() if line.startswith(prefix)]


class Guard:
    """Evaluates one tool call at a time. Holds the session's grants and waiting time."""

    def __init__(
        self,
        project_root: Path,
        *,
        approver: Approver | None = None,
        cwd: Callable[[], Path] | None = None,
        host: Platform | None = None,
        grants: Grants | None = None,
        waiting: Waiting | None = None,
        confine_to: Sequence[Path] | None = None,
        packages: bool = False,
        package_manager: str = "",
        allowed_tools: AllowedTools | None = None,
    ) -> None:
        self._project_root = project_root
        #: APP ONLY: judge system package commands (`guard/packages.py`). Off for `chat`
        #: and every benchmark task, where such a command is just a command.
        self._packages = packages
        #: APP ONLY: the machine's system package manager, for the example in a refusal.
        self._package_manager = package_manager
        #: Set only for a sandboxed run: the file tools may not reach past these.
        self._confine_to = tuple(confine_to) if confine_to is not None else None
        self._approve = approver or deny_all
        self._cwd = cwd or (lambda: project_root)
        self._platform = host or platform_module.current()
        #: The project's `.cogito/`, which nothing the agent runs may destroy.
        self.harness = HarnessDirectory(project_root)
        self.grants = grants or Grants()
        #: How long people have been kept waiting. A deadline elsewhere discounts it.
        self.waiting = waiting or Waiting()
        #: What was asked and refused, for the record of the call it happened in.
        self.ledger = Ledger()
        #: Tools from MCP servers that are asked about before each call, by registered
        #: name, with how the person knows them ("server · tool"). The server says which
        #: may destroy (`RemoteTool.destructive`); the guard cannot read their effects.
        self._ask_before: dict[str, ExternalTool] = {}
        #: Of those, the ones the person allowed: for this guard, or remembered in a file
        #: for every later session (the app, `allowed_tools.py`).
        self._tools_allowed = allowed_tools if allowed_tools is not None else AllowedTools()
        #: Tools allowed for this session only (`ExternalTool.session_only`).
        self._session_allowed: set[str] = set()
        #: APP ONLY: session-only tools the automatic mode allowed, which hold while it is
        #: on (`auto_on`): the mode would allow the next call too, and asking it again put
        #: one more pill and two more events on every `computer` and `browser` call.
        self._auto_session: set[str] = set()
        #: APP ONLY: whether the composer's automatic mode is on now (`guard.auto.AutoMode`).
        self.auto_on: Callable[[], bool] | None = None
        #: APP ONLY: what a `run_in_terminal` call would type and where, or why it cannot
        #: (`ask_terminal`). None where the tool is not registered.
        self._terminal: Callable[[Mapping[str, Any]], TerminalLine | str] | None = None

    def ask_before(self, tools: Mapping[str, ExternalTool]) -> None:
        self._ask_before.update(tools)

    def ask_terminal(self, plan: Callable[[Mapping[str, Any]], TerminalLine | str]) -> None:
        """Ask before every `run_in_terminal` call. ``plan`` reads the call's arguments and
        returns the line and the new tab it goes to, or why it is refused before anyone is
        asked (not one complete line, a tab named)."""
        self._terminal = plan

    def evaluate(
        self, tool_name: str, arguments: Mapping[str, Any], *, cwd: Path | None = None
    ) -> Verdict:
        """Decide what happens to this call, before it runs.

        ``cwd`` is where a shell command runs when the caller knows better than the guard:
        a PATH shim reports the directory it was called from. Relative words are placed
        from it, for the `.cogito/` check and the operating-system rule alike.
        """
        external = self._ask_before.get(tool_name)
        if external is not None and not (
            external.label in self._tools_allowed
            or external.label in self._session_allowed
            or (
                external.label in self._auto_session
                and self.auto_on is not None
                and self.auto_on()
            )
        ):
            return Verdict(
                Decision.ASK,
                f"{external.label} is asked about before it runs",
                external=external,
            )

        if tool_name == TERMINAL_TOOL and self._terminal is not None:
            planned = self._terminal(arguments)
            if isinstance(planned, str):
                return Verdict(Decision.REFUSE, planned)
            # The person types there too, outside any sandbox and the shims: the exact
            # line is theirs to allow, every time.
            return Verdict(
                Decision.ASK,
                "a line typed into the person's terminal is asked about every time",
                terminal=planned,
            )

        if self._confine_to is not None and tool_name in _PATH_TOOLS:
            raw = str(arguments.get("path", "") or "")
            resolved = real_path(raw, self._project_root)
            if outside(resolved, self._confine_to):
                return Verdict(
                    Decision.REFUSE,
                    confinement_refusal(
                        raw, resolved, self._confine_to,
                        "read, grep, glob, write and edit reach files",
                    ),
                )

        if self._confine_to is not None and tool_name in _PATHS_TOOLS:
            raw_paths = arguments.get("paths") or []
            if isinstance(raw_paths, str):
                raw_paths = [raw_paths]
            for raw_item in raw_paths:
                raw = str(raw_item or "")
                resolved = real_path(raw, self._project_root)
                if outside(resolved, self._confine_to):
                    return Verdict(
                        Decision.REFUSE,
                        confinement_refusal(
                            raw, resolved, self._confine_to, "check reaches files",
                        ),
                    )

        if tool_name in _WRITE_TOOLS:
            raw = str(arguments.get("path", "") or "")
            landing = real_path(raw, self._project_root)
            refusal = self.harness.judge_file_tool(tool_name, raw, landing)
            if refusal is not None:
                return Verdict(Decision.REFUSE, refusal)
            # Rule 2 holds for the file tools as it does for the shell: `write /etc/x` and
            # `edit ~/.bashrc` are refused, not left to the operating system's permissions.
            ruling = self._platform.classify_write(landing, self._project_root)
            if ruling.zone is Zone.SYSTEM:
                return Verdict(
                    Decision.REFUSE,
                    f"refused: {ruling.reason}. "
                    f"Cogito never deletes or modifies what the system owns.",
                    targets=(Target(raw=raw, path=landing, ruling=ruling),),
                )
            if self._terminal is not None:
                written = Path(os.path.expanduser(native(raw.strip())))
                if not written.is_absolute():
                    written = self._project_root / written
                for candidate in dict.fromkeys((written, landing)):
                    if refusal := self._platform.terminal_refusal(candidate, self._project_root):
                        return Verdict(Decision.REFUSE, refusal)

        if tool_name != "shell":
            # Writing and editing are the product working. They are reversible, and
            # deletion cannot reach the disk through them.
            return Verdict(Decision.ALLOW, "not a destructive tool")

        command = str(arguments.get("command", ""))
        if not command.strip():
            return Verdict(Decision.ALLOW, "empty command")

        found = scan(command)
        here = self._here(cwd)
        operations, modifications = self._settle(found.operations, found.modifications, here)
        targets = self._classify(operations, here)
        # Writes that destroy nothing answer to rule 2 alone: refused on a protected path,
        # and otherwise never put to anyone.
        touched = self._classify(modifications, here, writes=True)

        # An operating-system path is refused first. `rm -rf /` and `rm -rf ~` take the
        # project's `.cogito/` with them too, but that is not the larger loss.
        judged = (*targets, *touched)
        if system := [t for t in judged if t.ruling and t.ruling.zone is Zone.SYSTEM]:
            first = system[0]
            return Verdict(
                Decision.REFUSE,
                f"refused: {first.ruling.reason}. "
                f"Cogito never deletes or modifies what the system owns.",
                (*operations, *modifications),
                judged,
            )

        if self._terminal is not None and (
            refusal := self._terminal_writes(operations, modifications, here, found.commands)
            or (self._terminal_unparsed(command) if found.unparsed else None)
        ):
            return Verdict(Decision.REFUSE, refusal, (*operations, *modifications), judged)

        # Checked before anything is asked: no approval, a person's or a benchmark's, lets
        # the agent destroy the record of its own session.
        refusal = self.harness.judge_command(found, cwd or self._cwd(), command)
        if refusal is not None:
            return Verdict(Decision.REFUSE, refusal, found.operations)

        if self._packages:
            judged_packages = judge_packages(
                command, found.commands, self._package_manager,
                terminal=self._terminal is not None,
            )
            if judged_packages.refusal:
                return Verdict(Decision.REFUSE, judged_packages.refusal, found.operations)
            if (install := judged_packages.install) is not None:
                return Verdict(Decision.ASK, _describe_install(install), install=install)

        if not operations:
            return Verdict(Decision.ALLOW, "nothing destructive in this command")

        resolved = tuple(t.path for t in targets if t.path is not None)
        unresolved = [t for t in targets if not t.resolved]

        if resolved and not unresolved and self.grants.covers(list(resolved)):
            return Verdict(
                Decision.ALLOW, "already approved for this directory this session",
                operations, targets,
            )

        return Verdict(
            Decision.ASK, _describe(operations, targets), operations, targets, here=here,
            lookup_changed=bool(_LOOKUP_CHANGES.search(command)),
        )

    def _here(self, cwd: Path | None) -> Path:
        """Where a command's relative words start from.

        A PATH shim reports the directory it really ran in. That is not the session's when
        the command moved first (`cd ~ && rm -rf .ssh`), and its words then name files
        there. When both are the same place, the session's own spelling is kept.
        """
        session = self._cwd()
        if cwd is None or os.path.realpath(cwd) == os.path.realpath(session):
            return session
        return cwd

    def _settle(
        self,
        operations: tuple[Destructive, ...],
        modifications: tuple[Destructive, ...],
        cwd: Path,
    ) -> tuple[tuple[Destructive, ...], tuple[Destructive, ...]]:
        """Decide, by looking, what an operation that destroys only existing files does.

        `tee FILE` on a file that is not there yet creates it: nothing is destroyed, so it
        answers to rule 2 alone, like an append. A target that exists, or that cannot be
        resolved so nobody can say, stays an overwrite and is asked about like `>`.
        """
        operations = (*operations, *self._replaced(modifications, cwd))
        if not any(operation.existing_only for operation in operations):
            return operations, modifications
        kept: list[Destructive] = []
        created: list[Destructive] = list(modifications)
        for operation in operations:
            if not operation.existing_only:
                kept.append(operation)
                continue
            here = _directory(operation.cd, cwd)
            new = tuple(
                raw for raw in operation.targets
                if (here is not None or _anchored(raw))
                and (path := _resolve(raw, here or cwd)) is not None
                and not os.path.lexists(path)
            )
            existing = tuple(raw for raw in operation.targets if raw not in new)
            if existing:
                kept.append(replace(operation, targets=existing))
            if new:
                created.append(replace(operation, targets=new, why="creates it"))
        return tuple(kept), tuple(created)

    def _replaced(
        self, modifications: tuple[Destructive, ...], cwd: Path
    ) -> tuple[Destructive, ...]:
        """The existing files a cp, install, ln -f or mv writes over, as overwrites.

        `mv a b` destroys what `b` held, and `mv a dir/` what `dir/a` held. Only a file
        that is there now and can be named from the text counts; a glob source or a
        directory nobody can know leaves the write a plain modification.
        """
        found: list[Destructive] = []
        for modification in modifications:
            if not modification.sources:
                continue
            here = _directory(modification.cd, cwd)
            for raw in modification.targets:
                if here is None and not _anchored(raw):
                    continue
                destination = _resolve(raw, here or cwd)
                if destination is None:
                    continue
                if destination.is_dir():
                    names = [
                        f"{raw.rstrip('/')}/{posixpath.basename(source.rstrip('/'))}"
                        for source in modification.sources
                        if not any(marker in source for marker in _UNRESOLVED)
                    ]
                else:
                    names = [raw]
                for name in names:
                    path = _resolve(name, here or cwd)
                    if path is not None and os.path.lexists(path) and not path.is_dir():
                        found.append(
                            replace(modification, targets=(name,), why="replaces it",
                                    overwrites=True, sources=(), subtree=False)
                        )
        return tuple(found)

    def _classify(
        self, operations: Sequence[Destructive], cwd: Path, *, writes: bool = False
    ) -> tuple[Target, ...]:
        plain = self._platform.classify_write if writes else self._platform.classify
        targets: list[Target] = []
        for operation in operations:
            here = _directory(operation.cd, cwd)
            for raw in operation.targets:
                if here is None and not _anchored(raw):
                    # Relative to a directory the command moved into without naming it
                    # (`cd "$dir"`): shown as written, and refused when it could be a
                    # protected path.
                    ruling = (
                        self._could_be_protected(raw, _anywhere(raw), moved=True)
                        if operation.subtree
                        else None
                    )
                    targets.append(Target(raw=raw, path=None, ruling=ruling))
                    continue
                base = here or cwd
                path = _resolve(raw, base)
                if path is not None:
                    judge = self._platform.classify_removal if operation.subtree else plain
                    ruling = judge(path, self._project_root)
                else:
                    # Unresolvable, but often still knowable: judge the part the shell
                    # cannot change, so a glob over a protected directory is still caught.
                    prefix = _literal_prefix(raw, base)
                    ruling = plain(prefix, self._project_root) if prefix else None
                if operation.subtree and (ruling is None or ruling.zone is not Zone.SYSTEM):
                    ruling = self._could_be_protected(raw, _expansions(raw, base)) or ruling
                targets.append(Target(raw=raw, path=path, ruling=ruling))
        return tuple(targets)

    def _terminal_unparsed(self, command: str) -> str | None:
        """APP ONLY: a command the scanner could not read (`time …`, `[[ … ]]`, `case`) that
        names a file the person's terminal loads, or a password program's name as a path,
        next to anything that writes. Read as text, as `harness._judge_unparsed` reads one
        that names `.cogito/` (verification of 2026-11-03: `time cp x ~/.local/bin/sudo`
        passed)."""
        if not _WRITES_TEXT.search(command):
            return None
        if not (self._platform.terminal_startup and self._platform.privilege_tools):
            return None
        startup = "|".join(re.escape(name) for name in self._platform.terminal_startup)
        names = "|".join(sorted(self._platform.privilege_tools))
        if re.search(rf"(?:^|[/\s'\"~]){startup}(?:$|[/\s'\";&|)])", command) or re.search(
            rf"/(?:{names})(?:$|[\s'\";&|)])", command
        ):
            return (
                "refused: this command names a file the person's terminal loads, or one "
                "named like a password program, and could not be parsed, so Cogito cannot "
                "tell whether it writes there. Cogito never creates or changes those; "
                "rewrite it as a plain command that leaves them alone."
            )
        return None

    def _terminal_writes(
        self,
        operations: Sequence[Destructive],
        modifications: Sequence[Destructive],
        cwd: Path,
        commands: Sequence[Simple] = (),
    ) -> str | None:
        """APP ONLY (the terminal tabs exist): a refusal when the command writes a file the
        person's terminal loads at start, or a file named like a password-asking program
        (`Platform.terminal_refusal`). Every write that destroys nothing counts, and of the
        destructive operations those that replace what a file holds; a deletion plants
        nothing. So does the file a download is saved to (``curl -o``, ``wget -O``), which
        the scanner does not model as a write. ``$HOME`` and the like are expanded here, as
        the shell will."""
        downloads = [
            (simple.cd, tuple(outputs), ())
            for simple in commands
            if (outputs := _download_outputs(simple.words))
        ]
        for cd, targets, sources in (
            *[(o.cd, o.targets, o.sources) for o in operations if o.overwrites],
            *[(o.cd, o.targets, o.sources) for o in modifications],
            *downloads,
        ):
            here = _directory(cd, cwd)
            for raw in targets:
                expanded = os.path.expandvars(raw)
                if here is None and not _anchored(expanded):
                    continue
                path = _resolve(expanded, here or cwd)
                if path is None:
                    continue
                written = [path]
                if sources and path.is_dir():
                    # `cp fake ~/.local/bin/` writes `~/.local/bin/fake`'s name there.
                    written += [
                        path / posixpath.basename(source.rstrip("/"))
                        for source in sources
                        if not any(marker in source for marker in _UNRESOLVED)
                    ]
                for candidate in written:
                    if refusal := self._platform.terminal_refusal(candidate, self._project_root):
                        return refusal
        return None

    def _could_be_protected(
        self, raw: str, pattern: re.Pattern[str] | None, *, moved: bool = False
    ) -> Ruling | None:
        """A refusal when ``raw``, once expanded, could be a path that must never be
        removed whole, or an ancestor of one. ``pattern`` is every path it could be;
        ``moved`` says it is unknown because of the directory, not the word."""
        if pattern is None:
            return None
        for root, description in self._platform.removal_roots():
            for candidate in (root, *root.parents):
                text = str(candidate)
                if pattern.fullmatch(text) or pattern.fullmatch(text.rstrip("/") + "/"):
                    what = (
                        f"{candidate}"
                        if candidate == root
                        else f"{candidate}, which contains {root}"
                    )
                    unknown = (
                        f"{raw} is relative to a directory the command changes into, which "
                        f"is not known until it runs"
                        if moved
                        else f"{raw} is not known until the shell expands it"
                    )
                    return Ruling(
                        Zone.SYSTEM,
                        f"{unknown}, and it could be {what} — {description}. "
                        f"Name the path literally instead",
                    )
        return None

    def defers_to_shim(self, verdict: Verdict) -> bool:
        """True when every operation here will be intercepted later by a PATH shim.

        Both the loop and the shims consult this guard, and one `rm` must not be asked
        about twice. The shim wins because it asks when it can also enforce the answer: it
        sees the arguments after the shell expanded them, and it is what moves the file to
        the trash instead of unlinking it.

        Only a verb the shell looks up on PATH reaches a shim. `/bin/rm`, `sudo rm`,
        `busybox rm` and `PATH=/usr/bin rm` do not, and deferring them would let the
        deletion run with nobody asked.

        A refusal still fires early: it costs nothing and tells the model sooner.
        """
        from cogito.guard.shims import SHIMMED

        return (
            bool(verdict.operations)
            and not verdict.lookup_changed
            and all(
                operation.verb in SHIMMED and operation.shimmable
                for operation in verdict.operations
            )
        )

    def request_for(self, verdict: Verdict, command: str) -> ApprovalRequest:
        """Turn a verdict that needs asking into something a person can answer.

        Each operation says what it does to what it names (deletes, overwrites, moves) and
        whether that can be undone. Only a deletion a PATH shim carries out goes to the
        trash, and only a shimmed truncate saves a copy there; a move loses nothing; every
        other destruction is for good, and the request says so.
        """
        from cogito.guard.shims import SHIMMED

        if verdict.external is not None:
            external = verdict.external
            return ApprovalRequest(
                summary=f"Let {external.label} run?",
                command=command,
                reason=verdict.reason,
                recoverable=not external.changes,
                action="external_tool",
                recovery="none",
                server=external.server,
                tool=external.tool,
                tool_title=external.title,
                changes=external.changes,
                every_time=external.every_time,
            )

        if verdict.terminal is not None:
            line = verdict.terminal
            return ApprovalRequest(
                summary=f"Run this line in {line.tab_title}?",
                command=line.command,
                reason=verdict.reason,
                recoverable=False,
                action=TERMINAL_TOOL,
                recovery="none",
                tab_title=line.tab_title,
                cwd=line.cwd,
            )

        if verdict.install is not None:
            install = verdict.install
            return ApprovalRequest(
                summary=_install_summary(install),
                command=command,
                reason=verdict.reason,
                recoverable=True,
                action=INSTALL_ACTIONS[install.kind],
                recovery="kept",
                packages=install.packages,
                manager=install.manager,
                source=install.source,
            )

        here = verdict.here or self._cwd()
        items: dict[tuple[Path, str], ApprovalItem] = {}
        unresolved: list[str] = []
        #: Where each unresolved word is read from, or None when a `cd` hides it.
        bases: list[Path | None] = []
        #: Operations that destroy what they do not name and are no find or rsync.
        sweeps: list[str] = []
        within: list[Path] = []
        listed: list[str] = []
        kinds: list[str] = []
        effects: list[str] = []
        flat = iter(verdict.targets)
        for operation in verdict.operations:
            targets = list(itertools.islice(flat, len(operation.targets)))
            kind = _kind(operation)
            kinds.append(kind)
            if operation.verb in SHIMMED and operation.shimmable and not verdict.lookup_changed:
                effects.append("trash")
            elif operation.verb == "mv" and not operation.overwrites:
                effects.append("kept")
            else:
                effects.append("lost")
            if kind == "command":
                if not operation.verb.startswith(("find", "rsync")):
                    sweeps.append(operation.verb)
                if operation.verb.startswith(("find", "rsync")):
                    for target in targets:
                        if target.path is not None:
                            within.append(target.path)
                        else:
                            unresolved.append(target.raw)
                            bases.append(_directory(operation.cd, here))
                if operation.verb == "git clean":
                    listed.extend(_clean_dry_run(operation, _directory(operation.cd, here)))
                continue
            for target in targets:
                if target.path is None:
                    unresolved.append(target.raw)
                    bases.append(_directory(operation.cd, here))
                elif (target.path, kind) not in items:
                    items[(target.path, kind)] = _item(target.path, kind)

        outside = any(t.ruling and t.ruling.zone is Zone.HOME for t in verdict.targets)
        recovery = (
            "none" if "lost" in effects else "trash" if "trash" in effects else "kept"
        )
        if "command" in kinds:
            action = "command"
        elif "move" in kinds:
            action = "move"
        elif kinds and all(kind == "overwrite" for kind in kinds):
            action = "overwrite"
        else:
            action = "delete"

        # The scope a person may stop being asked about: every path the command names,
        # find's start points included, under one directory.
        resolved = tuple(t.path for t in verdict.targets if t.path is not None)
        scope: Path | None = None
        if resolved and not any(not t.resolved for t in verdict.targets):
            parents = {p.parent for p in resolved}
            if len(parents) == 1:
                scope = parents.pop()

        shown = tuple(items.values())
        first_bases: dict[str, Path | None] = {}
        for raw, base in zip(unresolved, bases, strict=True):
            first_bases.setdefault(raw, base)
        return ApprovalRequest(
            summary=_summary(action, shown, outside),
            command=command,
            targets=tuple(dict.fromkeys(item.path for item in shown)),
            unresolved=tuple(dict.fromkeys(unresolved)),
            unresolved_from=tuple(first_bases.values()),
            sweeps=tuple(dict.fromkeys(sweeps)),
            reason=verdict.reason,
            recoverable=recovery != "none",
            suggested_scope=scope,
            action=action,
            items=shown,
            recovery=recovery,
            targets_unknown="command" in kinds,
            within=tuple(dict.fromkeys(within)),
            listed=tuple(listed[:_LISTED]),
            listed_more=max(0, len(listed) - _LISTED),
            outside=outside,
        )

    def ask(self, verdict: Verdict, command: str) -> bool:
        """Put the verdict to the user and apply whatever they chose."""
        return self.answer(verdict, command).allowed

    def answer(
        self, verdict: Verdict, command: str, *, from_shim: bool = False
    ) -> ApprovalResponse:
        """`ask`, returning the whole answer: who decided, not only whether.

        The time spent here is recorded: a command's deadline keeps running meanwhile, and
        the wait is not the command's fault. ``from_shim`` marks a question a PATH shim put,
        with the names the shell expanded (`ApprovalRequest.from_shim`).
        """
        started = time.monotonic()
        with self.waiting:
            request = self.request_for(verdict, command)
            if from_shim:
                request = replace(request, from_shim=True)
            response = self._approve(request)
        self.ledger.note_answer(request, response, time.monotonic() - started)
        if verdict.terminal is not None:
            # Never remembered: the next line is asked about too.
            return response
        if response.remember_prefix is not None:
            self.grants.add(response.remember_prefix)
        if verdict.external is not None and verdict.external.every_time:
            # Never remembered: the next server is asked about too.
            return response
        if (
            response.allowed
            and response.reason == "auto"
            and verdict.external is not None
            and verdict.external.session_only
        ):
            self._auto_session.add(verdict.external.label)
        if response.allowed and response.remember_tool and verdict.external is not None:
            if verdict.external.session_only:
                self._session_allowed.add(verdict.external.label)
            else:
                self._tools_allowed.add(verdict.external.label)
        return response
