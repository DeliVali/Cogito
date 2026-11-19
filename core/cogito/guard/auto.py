"""APP ONLY: the composer's automatic mode, which answers the guard's questions itself.

With it on, what the guard would put to the person is allowed without a card: a deletion,
permanent or to the trash, a system package install, an MCP tool that may change
something, a line for the person's terminal. What the guard refuses is never a question,
so it stays refused: operating-system files (`Zone.SYSTEM`, the home directory itself and
`/` among them), the protected home roots and the session's own `.cogito/`. That refusal
is the mode's only guardrail for files (the person, 2026-11-18). Adding an MCP server is
still asked.

With it off, one kind of question is still answered here (`in_project`): a deletion that
goes to the trash and touches only what is inside the project folder. `_beyond_trash` is
that narrow judgement: it leaves to the person whatever destroys for good, and, among what
goes to the trash, the project folder or all of it, their keys and credentials, Cogito's
own settings and memory, and a folder directly in their home or all of it.

Read before choosing (2026-11-03): Claude Code's `bypassPermissions` removes its prompts but
still asks before removing "the filesystem root, your home directory, and your working
directory", and keeps its deny rules (code.claude.com/docs/en/permission-modes); its auto mode
blocks "Irreversibly destroying files that existed before the session" through a classifier
model, and Anthropic says it "does not guarantee safety". Codex's `approval_policy = "never"`
removes only the prompts, and Gemini CLI starts a sandbox when its YOLO mode is on. No mode
here judges intent: a page the model read can still steer it into an allowed action.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path

from cogito.config import paths
from cogito.guard.approval import ApprovalRequest

#: Why a question is still asked in automatic mode, as the window words it.
PERMANENT = "permanent"
PROJECT = "project"
CREDENTIALS = "credentials"
COGITO = "cogito"
HOME_FOLDER = "home_folder"
UNKNOWN = "unknown"
MCP_SERVER = "mcp_server"

#: Under the home directory: keys, tokens, password stores and browser profiles (saved
#: passwords and sessions). Any change to these is asked about, not only a deletion.
_CREDENTIALS = (
    ".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".local/share/kwalletd",
    ".pki", ".aws", ".azure", ".kube", ".docker", ".config/gcloud", ".config/gh", ".netrc",
    ".git-credentials", ".config/git/credentials", ".npmrc", ".pypirc",
    ".cargo/credentials.toml", ".config/rclone", ".vault-token", ".terraform.d",
    ".mozilla", ".config/mozilla", ".var/app", ".config/google-chrome", ".config/chromium",
    ".config/BraveSoftware", ".config/vivaldi", ".config/microsoft-edge", ".config/opera",
    ".config/Bitwarden", ".config/keepassxc", ".thunderbird",
)

#: In a target the guard could not resolve, a sign that nobody can say where it points
#: before the shell runs: a variable or a substitution.
_REACHES = ("$", "`")

#: A brace group the shell expands (`{a,b}`, `{1..3}`, `x{,.bak}`): the guard reads such
#: a word as one path that is not there, so nobody knows what it names.
_BRACES = re.compile(r"(?<!\\)\{[^{}\s'\"]*(?:,|\.\.)[^{}\s'\"]*\}")

#: What an item or request does that takes a whole path, and what is under it, away.
_REMOVES = frozenset({"delete", "move", "command"})

#: The questions about files, as against an install, a server's tool or a terminal line.
_FILE_ACTIONS = frozenset({"delete", "overwrite", "move", "command"})


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(path)))


def _entry(path: Path) -> Path:
    """``path`` as the folder entry a deletion takes: its folder resolved, its own name
    kept, so a link is the link and not what it points at."""
    lexical = Path(os.path.normpath(os.path.expanduser(path)))
    return Path(os.path.realpath(lexical.parent)) / lexical.name


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class AutoMode:
    """One window session's automatic mode: whether it is on, and what it still asks.

    On, it asks only about adding an MCP server. Until 2026-11-18 it answered, of the
    questions about files, only those whose loss the trash undoes, because three review
    rounds on 2026-11-03 found 31 spellings of the shell that a reading of a command's
    text took for something else; the person then chose a mode that asks nothing and
    keeps the guard's refusals. That narrower judgement is still what `in_project` uses
    with the mode off (`_beyond_trash`).
    """

    def __init__(self, project_root: Path, home: Path | None = None) -> None:
        self.on = False
        self._project = _resolved(project_root)
        self._home = _resolved(home if home is not None else Path.home())
        self._credentials = tuple(
            dict.fromkeys(_resolved(self._home / name) for name in _CREDENTIALS)
        )
        own = (paths.config_dir(), paths.data_dir())
        self._cogito = tuple(dict.fromkeys(_resolved(directory) for directory in own))

    def kept_asking(self, request: ApprovalRequest) -> str | None:
        """Why ``request`` is still put to the person with the mode on, or None to allow it
        unasked.

        Every question about files is allowed, permanent ones included (the person,
        2026-11-18, shown a card for a script that rewrote its own file: "en modo
        automático no quiero que me haga esas preguntas ... solo los guardrails para no
        borrar archivos del sistema"). What the guard refuses is never a question and
        stays refused. The one thing still asked is adding an MCP server."""
        if request.every_time:
            # Adding an MCP server saves a program that runs in every later conversation,
            # chosen by text the model may have read on a page: a standing change, not a
            # call to a server the person already approved.
            return MCP_SERVER
        return None

    def _beyond_trash(self, request: ApprovalRequest) -> str | None:
        """Why ``request`` is more than a deletion the trash undoes, or None. With the mode
        off, `in_project` answers only what passes this."""
        if request.every_time:
            return MCP_SERVER
        if request.action in _FILE_ACTIONS and (
            request.recovery != "trash" or not request.from_shim
        ):
            # Only what goes to the trash is allowed unasked: what a PATH shim deletes,
            # judged on the names the shell expanded and the folder it really ran in. A
            # question the loop puts from the command's text (`rm x; mv a b` reads as
            # "trash" there) is not one of those.
            return PERMANENT
        if any(item.action == "overwrite" and _linked(item.path) for item in request.items):
            # A file with another name elsewhere: overwriting it changes that one too.
            return UNKNOWN
        if request.action != "external_tool" and (
            "CDPATH" in request.command or _BRACES.search(request.command)
        ):
            # `cd NAME` lands wherever CDPATH says, and braces name what the guard did not
            # see: neither is followed here. A tool's `command` is its arguments, which no
            # shell expands: a `*.{js,ts}` pattern in them is text, and asked about it the
            # mode kept a card up for an MCP search.
            return UNKNOWN
        if request.sweeps or (request.targets_unknown and not request.within):
            return UNKNOWN
        if request.from_shim and request.unresolved:
            # A shim's words are names the shell already expanded: one still unresolved
            # holds a `*`, `?` or `[` of its own, which the guard read as a pattern.
            return UNKNOWN
        bases = request.unresolved_from or (None,) * len(request.unresolved)
        for raw, base in zip(request.unresolved, bases, strict=False):
            if _reaches(raw) or base is None:
                return UNKNOWN
            # A pattern deletes what it matches inside the folder its fixed part names.
            folder = self._place(base, _fixed_part(raw))
            if why := self._container(folder) or self._emptied(folder, _last_part(raw)):
                return why
        named = list(self._named(request))
        for path, _entry_path, removes in named:
            if why := self._path(path, removes=removes):
                return why
        # `rm -rf *` reaches the guard again from the PATH shim with the names the shell
        # expanded: every entry of a folder named one by one is still the whole folder.
        if why := self._covered([entry for _path, entry, removes in named if removes]):
            return why
        for path in request.within:
            folder = _resolved(path)
            # `find X -delete` can take X itself, and `rsync --delete` empties it.
            if why := self._container(folder) or self._whole_folder(folder):
                return why
        return None

    def in_project(self, request: ApprovalRequest) -> bool:
        """APP ONLY, whether the switch is on or not: True when ``request`` deletes or
        empties only what is inside the project folder and the trash keeps it, so the
        person is not asked (2026-11-16: "que no me pida permisos al eliminar archivos
        dentro de la carpeta").

        It is the automatic mode's own judgement (`kept_asking`), narrowed to the project:
        a PATH shim's question, with the names the shell expanded, each one strictly inside
        the folder, where it leads and as the entry it is. The folder itself, all of it
        at once, anything outside it and whatever destroys for good are still asked."""
        if request.action not in _FILE_ACTIONS or self._beyond_trash(request) is not None:
            return False
        named = list(self._named(request))
        if not named and not request.within:
            return False

        def inside(path: Path) -> bool:
            return self._project in path.parents

        return all(inside(leads) and inside(entry) for leads, entry, _removes in named) and all(
            inside(_resolved(folder)) for folder in request.within
        )

    def _place(self, base: Path, fixed: str) -> Path:
        """Where a pattern's fixed part points from ``base``: `~` is this home, as the
        guard reads it."""
        if fixed == "~" or fixed.startswith("~/"):
            return _resolved(self._home / fixed[2:])
        return _resolved(base / os.path.expanduser(fixed))

    def _home_folders(self) -> set[Path]:
        """The folders directly in the home directory, each where it really is: a
        `~/Pictures` that links to a second disk is that disk's folder."""
        try:
            names = os.listdir(self._home)
        except OSError:
            return set()
        return {
            _resolved(self._home / name) for name in names if (self._home / name).is_dir()
        }

    def _container(self, folder: Path) -> str | None:
        """Why deleting what matches inside ``folder`` is asked about, or None."""
        if folder == self._home:
            return HOME_FOLDER
        if why := self._guarded(folder):
            return why
        if self._project != folder and _within(self._project, folder):
            return PROJECT
        return None

    def _whole_folder(self, folder: Path) -> str | None:
        """The project, or a folder directly in the home directory: what losing all of
        is asked about."""
        if folder == self._project:
            return PROJECT
        if folder.parent == self._home and folder.is_dir():
            return HOME_FOLDER
        if folder in self._home_folders():
            return HOME_FOLDER
        return None

    def _emptied(self, folder: Path, pattern: str) -> str | None:
        """A pattern that matches every entry the shell would give it in a folder whose
        whole content is asked about (`*`, `**`, `?*`, `[!.]*` alike)."""
        why = self._whole_folder(folder)
        if why is None or not pattern or "/" in pattern:
            return None
        entries = _entries(folder, hidden=pattern.startswith("."))
        if entries and all(fnmatchcase(name, pattern) for name in entries):
            return why
        return None

    def _covered(self, removed: list[Path]) -> str | None:
        """Every entry of the project, or of a folder in the home directory, removed by
        name."""
        by_folder: dict[Path, set[str]] = {}
        for path in removed:
            by_folder.setdefault(path.parent, set()).add(path.name)
        for folder, names in by_folder.items():
            why = self._whole_folder(folder)
            entries = _entries(folder, hidden=False) if why else []
            if entries and set(entries) <= names:
                return why
        return None

    def _guarded(self, path: Path) -> str | None:
        """Keys, credentials and Cogito's own files: asked about whatever is done to them."""
        if any(_within(path, root) or _within(root, path) for root in self._credentials):
            return CREDENTIALS
        if any(_within(path, root) or _within(root, path) for root in self._cogito):
            return COGITO
        return None

    def _named(self, request: ApprovalRequest) -> Iterable[tuple[Path, Path, bool]]:
        """Each named path, where it leads and as the entry it is, and whether what is
        done to it takes it away: an item's own action, else the request's."""
        named: dict[Path, tuple[Path, bool]] = {}
        for item in request.items:
            entry = _entry(item.path)
            removes = named.get(entry, (entry, False))[1] or item.action in _REMOVES
            named[entry] = (_resolved(item.path), removes)
        for path in request.targets:
            entry = _entry(path)
            named.setdefault(entry, (_resolved(path), request.action in _REMOVES))
        return [(resolved, entry, removes) for entry, (resolved, removes) in named.items()]

    def _path(self, path: Path, *, removes: bool) -> str | None:
        if why := self._guarded(path):
            return why
        if removes and _within(self._project, path):
            return PROJECT
        if removes and (why := self._whole_folder(path)) == HOME_FOLDER:
            return why
        return None


def _linked(path: Path) -> bool:
    """Whether ``path`` is a file with more than one name (a hard link)."""
    try:
        return os.stat(path).st_nlink > 1 and not os.path.isdir(path)
    except OSError:
        return False


def _entries(folder: Path, *, hidden: bool) -> list[str]:
    """What the shell's patterns choose from in ``folder``: the dot entries only for a
    pattern that starts with a dot, never the session's `.cogito/`."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [
        name for name in names
        if name != ".cogito" and name.startswith(".") == hidden
    ]


def _fixed_part(raw: str) -> str:
    """The folders a pattern names before its first wildcard: `build/*.o` -> `build`."""
    head = raw.split("*")[0].split("?")[0].split("[")[0]
    return head.rsplit("/", 1)[0] if "/" in head else ""


def _last_part(raw: str) -> str:
    """The pattern's own last part, when the folders before it are fixed: `build/*.o` ->
    `*.o`; empty when a wildcard sits in a folder (`*/cache`)."""
    fixed = _fixed_part(raw)
    rest = raw[len(fixed) + 1:] if fixed else raw
    rest = rest.rstrip("/")
    return rest if "/" not in rest else ""


def _reaches(raw: str) -> bool:
    return any(marker in raw for marker in _REACHES)
