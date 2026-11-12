"""System packages: which commands the guard asks about, and which it refuses.

APP ONLY. `Guard(packages=True)` is set by `cli._build_engine` for the desktop app and
nowhere else, so `chat` and every benchmark task keep today's behaviour (a package command
is just a command).

The person decided on 2026-10-12 that the agent may install system packages, upgrade the
whole system and install from the AUR, each with their approval. The rest of what a package
manager can do stays out of the agent's hands:

- install by plain name from the official repositories: asked (`PackageInstall`);
- a full system upgrade (`pacman -Syu`, `apt upgrade`, `brew upgrade`, `winget upgrade
  --all`): asked, with its own card;
- an install through paru or yay: asked, naming the packages and the AUR, and run as the
  user (a helper refuses root) with pkexec as its `--sudo`;
- an index refresh on its own, a partial upgrade (`pacman -Sy pkg`), cleaning, removing, adding
  a repository or a key, any other use of an AUR helper, an install from a file or URL:
  refused, with what to use instead.

A download piped into a shell is not judged here: the person left it as it was.

The words are read from `bashscan`'s simple commands, so `sudo`, `doas`, `env` and a nested
`bash -c` are already unwrapped. When the install is the whole command, the core runs it
itself (`guard/installer.py`): the model's own `sudo` could never be answered anyway, since
the agent's shell has no terminal, and the person's password must never reach the agent.

Options are matched against a short list per manager and anything else refuses: `apt -o`
runs arbitrary hooks as root, `pacman --overwrite` clobbers files, and `dnf --allowerasing`
removes packages.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cogito.guard.bashscan import Simple

#: The managers an install may go through, by the id the card shows.
MANAGERS: tuple[str, ...] = ("pacman", "apt", "dnf", "zypper", "apk", "brew", "winget")

#: Binary name (lower case, no `.exe`) -> manager id.
_BINARIES: dict[str, str] = {
    "pacman": "pacman", "apt": "apt", "apt-get": "apt", "dnf": "dnf", "dnf5": "dnf",
    "yum": "dnf", "zypper": "zypper", "apk": "apk", "brew": "brew", "winget": "winget",
}
#: Helpers that build packages from scripts nobody reviewed.
_AUR_HELPERS = frozenset({"paru", "yay", "pikaur", "trizen", "aura", "yaourt", "aurman",
                          "pacaur", "pamac"})
#: The helpers the core can run with the system password dialog: both take the command to
#: use for sudo (`--sudo`, paru.8 and yay.8, read 2026-10-12), so pkexec can stand in.
SUPPORTED_HELPERS = frozenset({"paru", "yay"})
#: Why the others are not run, for the refusal.
_UNSUPPORTED_WHY = {
    "pikaur": "pikaur only supports sudo or doas for privileges (its PrivilegeEscalationTool "
              "setting), not pkexec",
    "trizen": "trizen's sudo command is not documented to take pkexec",
}
#: Programs that change what the system trusts.
_REPOSITORY_PROGRAMS = frozenset({"add-apt-repository", "apt-key", "pacman-key", "ppa-purge"})

#: A package name the official repositories would have: no path, URL, version pin, repo
#: prefix, glob or option.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*")
_MAX_NAME = 100

OFFICIAL = "official repositories"
AUR_SOURCE = "AUR"

UPGRADE = "upgrade"
REMOVE = "remove"
REPOSITORY = "repository"
AUR = "aur"
UNOFFICIAL = "unofficial"
INSTALL = "install"
REFRESH = "refresh"
#: Asked, like INSTALL: a full upgrade, and an install through an AUR helper.
SYSTEM_UPGRADE = "system_upgrade"
AUR_INSTALL = "aur_install"


@dataclass(frozen=True, slots=True)
class PackageInstall:
    """What the person is asked about, and the core then runs itself: an install, a full
    upgrade (no packages) or an AUR install (``manager`` is then the helper)."""

    manager: str
    packages: tuple[str, ...]
    #: Where the packages come from, as the card says it.
    source: str = OFFICIAL
    #: INSTALL, SYSTEM_UPGRADE or AUR_INSTALL.
    kind: str = INSTALL
    #: For an upgrade, which one the manager runs when it has more than one: "upgrade" or
    #: "dist-upgrade" for apt, "update" or "dist-upgrade" for zypper.
    variant: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    """What one simple command does to the system's packages."""

    kind: str
    manager: str = ""
    packages: tuple[str, ...] = ()
    variant: str = ""


@dataclass(frozen=True, slots=True)
class PackageJudgement:
    """The guard's answer for a whole command. Both empty: not about packages."""

    refusal: str = ""
    install: PackageInstall | None = None


def refusal_for(kind: str, manager: str = "", packages: Sequence[str] = ()) -> str:
    """The structured refusal, saying what to use instead."""
    what = manager or "the package manager"
    if kind == UPGRADE:
        return (
            "refused: refreshing the index on its own, a partial upgrade (`pacman -Sy "
            "<name>`), cleaning caches or a distribution upgrade is for the person to run, "
            "not the agent. To update everything, run the plain full upgrade "
            "(`pacman -Syu`, `apt upgrade`): the person is asked first. To install one "
            "thing, install it by name (`pacman -S <name>`)."
        )
    if kind == REMOVE:
        return (
            "refused: the agent does not remove system packages. Tell the person which "
            "package should go and why."
        )
    if kind == REPOSITORY:
        return (
            "refused: adding a repository or a key changes what the system trusts. Use a "
            "package from the official repositories, or tell the person what to add."
        )
    if kind == AUR:
        if manager in SUPPORTED_HELPERS:
            return (
                f"refused: through the agent only plain installs are supported "
                f"(`{manager} -S <name>`, no other options). Upgrading the AUR, building by "
                f"hand or any other use of {manager} is for the person to run."
            )
        names = " ".join(packages) or "<name>"
        why = _UNSUPPORTED_WHY.get(manager, f"{manager} is not known to work with it")
        return (
            f"refused: the agent cannot drive {manager} with the system password dialog "
            f"({why}). Use paru or yay (`paru -S <name>`), or tell the person to run "
            f"`{manager} -S {names}` themselves."
        )
    if kind == REFRESH:
        return (
            "refused: refreshing the package index on its own is not needed. Ask for the "
            f"package directly (`{what} install <name>`); the harness refreshes the index "
            "itself when it installs, and asks the person first."
        )
    return (
        "refused: install packages by plain name from the official repositories, with no "
        "URL, file, version pin, extra repository or extra options (only the manager's "
        "yes/quiet flags are accepted). Example: `pacman -S love`."
    )


def _split(words: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """``words`` as (short option letters, long options, operands). `--` ends the options;
    a long option's `=value` is cut off, a short cluster is split into its letters."""
    letters: list[str] = []
    longs: list[str] = []
    operands: list[str] = []
    ended = False
    for word in words:
        if ended or not word.startswith("-") or word == "-":
            operands.append(word)
        elif word == "--":
            ended = True
        elif word.startswith("--"):
            longs.append(word.split("=", 1)[0])
        else:
            letters.extend(word[1:])
    return letters, longs, operands


def _plain(names: Sequence[str]) -> bool:
    return bool(names) and all(
        len(name) <= _MAX_NAME and _NAME.fullmatch(name) is not None for name in names
    )


def _install_or_unofficial(
    manager: str, names: Sequence[str], letters: Sequence[str], longs: Sequence[str],
    allowed_letters: frozenset[str], allowed_longs: frozenset[str],
) -> Finding | None:
    if not names:
        return None  # `apt install` alone fails by itself; nothing to ask about
    if (
        not _plain(names)
        or any(letter not in allowed_letters for letter in letters)
        or any(long not in allowed_longs for long in longs)
    ):
        return Finding(UNOFFICIAL, manager)
    return Finding(INSTALL, manager, tuple(dict.fromkeys(names)))


# -- pacman ------------------------------------------------------------------------------

#: Sync operations that only read: search, info, list, groups, print.
_PACMAN_READS = frozenset({"s", "i", "l", "g", "p"})
_PACMAN_READ_LONGS = frozenset({"--search", "--info", "--list", "--groups", "--print"})
_PACMAN_LETTERS = frozenset({"S", "q"})
_PACMAN_LONGS = frozenset({"--sync", "--noconfirm", "--needed", "--noprogressbar", "--quiet",
                           "--asdeps", "--asexplicit"})


def _pacman_kind(words: Sequence[str]) -> tuple[str, list[str], list[str], list[str]]:
    """(operation, letters, longs, operands). Operation is one of "read", "install",
    "upgrade", "remove", "file" (-U) or "none"."""
    letters, longs, names = _split(words)
    upper = {letter for letter in letters if letter.isupper()}
    if "R" in letters or "--remove" in longs:
        return REMOVE, letters, longs, names
    if "U" in letters or "--upgrade" in longs:
        return "file", letters, longs, names
    if "S" in letters or "--sync" in longs:
        if set(letters) & _PACMAN_READS or set(longs) & _PACMAN_READ_LONGS:
            return "read", letters, longs, names
        cleans = bool(set(letters) & {"c", "w"} or set(longs) & {"--clean", "--downloadonly"})
        upgrades = "u" in letters or "--sysupgrade" in longs
        if upgrades and not cleans and not names:
            return SYSTEM_UPGRADE, letters, longs, names
        # y alone refreshes the index and `-Sy pkg` is a partial upgrade, which Arch does
        # not support; c cleans the cache; w only downloads; `-Syu pkg` mixes the two.
        if upgrades or cleans or set(letters) & {"y"} or "--refresh" in longs:
            return UPGRADE, letters, longs, names
        return INSTALL, letters, longs, names
    if "D" in upper or "--database" in longs:
        return UPGRADE, letters, longs, names
    return "read" if upper or longs else "none", letters, longs, names


def _pacman(words: Sequence[str]) -> Finding | None:
    operation, letters, longs, names = _pacman_kind(words)
    if operation in {"read", "none"}:
        return None
    if operation == "file":
        return Finding(UNOFFICIAL, "pacman")
    if operation in {UPGRADE, REMOVE}:
        return Finding(operation, "pacman")
    if operation == SYSTEM_UPGRADE:
        if any(letter not in _PACMAN_LETTERS | {"y", "u"} for letter in letters) or any(
            long not in _PACMAN_LONGS | {"--refresh", "--sysupgrade"} for long in longs
        ):
            return Finding(UNOFFICIAL, "pacman")
        return Finding(SYSTEM_UPGRADE, "pacman")
    return _install_or_unofficial(
        "pacman", names, letters, longs, _PACMAN_LETTERS, _PACMAN_LONGS
    )


_AUR_LONGS = _PACMAN_LONGS | {"--skipreview", "--noprovides"}


def _aur(helper: str, words: Sequence[str]) -> Finding | None:
    """An AUR helper. A pacman-style read is left alone; a plain `-S names` through paru or
    yay is an AUR install; everything else is refused."""
    operation, letters, longs, names = _pacman_kind(words)
    if operation == "read":
        return None
    if operation == REMOVE:
        return Finding(REMOVE, helper)
    if operation == INSTALL:
        if not names:
            return None
        if helper not in SUPPORTED_HELPERS:
            return Finding(AUR, helper, tuple(names))
        if (
            _plain(names)
            and all(letter in _PACMAN_LETTERS for letter in letters)
            and all(long in _AUR_LONGS for long in longs)
        ):
            return Finding(AUR_INSTALL, helper, tuple(dict.fromkeys(names)))
    return Finding(AUR, helper, tuple(names) if helper not in SUPPORTED_HELPERS else ())


# -- apt, dnf, zypper, apk, brew, winget -------------------------------------------------

_APT_LETTERS = frozenset({"y", "q", "V"})
_APT_LONGS = frozenset({"--yes", "--assume-yes", "--quiet", "--no-install-recommends",
                        "--verbose-versions"})
_APT_SUBCOMMANDS = {
    "install": INSTALL, "update": REFRESH,
    "upgrade": SYSTEM_UPGRADE, "full-upgrade": SYSTEM_UPGRADE, "dist-upgrade": SYSTEM_UPGRADE,
    "remove": REMOVE, "purge": REMOVE, "autoremove": REMOVE, "autopurge": REMOVE,
    "edit-sources": REPOSITORY,
}

_DNF_LETTERS = frozenset({"y", "q"})
_DNF_LONGS = frozenset({"--assumeyes", "--quiet", "--best"})
_DNF_SUBCOMMANDS = {
    "install": INSTALL, "in": INSTALL, "makecache": REFRESH,
    "upgrade": SYSTEM_UPGRADE, "update": SYSTEM_UPGRADE, "up": SYSTEM_UPGRADE,
    "distro-sync": UPGRADE,
    "dsync": UPGRADE, "system-upgrade": UPGRADE, "offline": UPGRADE, "clean": UPGRADE,
    "reinstall": UPGRADE, "downgrade": UPGRADE, "group": UPGRADE, "groupinstall": UPGRADE,
    "module": UPGRADE,
    "remove": REMOVE, "erase": REMOVE, "rm": REMOVE, "autoremove": REMOVE, "swap": REMOVE,
    "config-manager": REPOSITORY, "copr": REPOSITORY, "repo": REPOSITORY,
}

_ZYPPER_LETTERS = frozenset({"n", "q"})
_ZYPPER_LONGS = frozenset({"--non-interactive", "--quiet", "--no-recommends"})
_ZYPPER_SUBCOMMANDS = {
    "install": INSTALL, "in": INSTALL, "refresh": REFRESH, "ref": REFRESH,
    "update": SYSTEM_UPGRADE, "up": SYSTEM_UPGRADE, "dist-upgrade": SYSTEM_UPGRADE,
    "dup": SYSTEM_UPGRADE, "patch": UPGRADE, "clean": UPGRADE, "verify": UPGRADE,
    "remove": REMOVE, "rm": REMOVE,
    "addrepo": REPOSITORY, "ar": REPOSITORY, "modifyrepo": REPOSITORY, "mr": REPOSITORY,
    "removerepo": REPOSITORY, "rr": REPOSITORY, "addservice": REPOSITORY, "as": REPOSITORY,
    "removeservice": REPOSITORY, "rs": REPOSITORY,
}

_APK_LETTERS = frozenset({"q", "U"})
_APK_LONGS = frozenset({"--no-cache", "--update-cache", "--quiet", "--no-progress"})
_APK_SUBCOMMANDS = {
    "add": INSTALL, "update": UPGRADE, "upgrade": SYSTEM_UPGRADE, "fix": UPGRADE, "cache": UPGRADE,
    "del": REMOVE,
}

_BREW_LETTERS = frozenset({"q"})
_BREW_LONGS = frozenset({"--formula", "--formulae", "--cask", "--casks", "--quiet"})
_BREW_SUBCOMMANDS = {
    "install": INSTALL, "upgrade": SYSTEM_UPGRADE, "reinstall": UPGRADE, "cleanup": UPGRADE,
    "uninstall": REMOVE, "remove": REMOVE, "rm": REMOVE, "rmtree": REMOVE, "zap": REMOVE,
    "tap": REPOSITORY, "untap": REPOSITORY, "tap-new": REPOSITORY,
}

_WINGET_LETTERS = frozenset({"e", "h"})
_WINGET_LONGS = frozenset({"--exact", "--silent", "--accept-package-agreements",
                           "--accept-source-agreements", "--disable-interactivity", "--id"})
_WINGET_SUBCOMMANDS = {
    "install": INSTALL, "add": INSTALL,
    "upgrade": SYSTEM_UPGRADE, "update": SYSTEM_UPGRADE,
    "uninstall": REMOVE, "remove": REMOVE, "rm": REMOVE,
    "import": UNOFFICIAL, "configure": UNOFFICIAL,
}

#: The only `--source` values winget accepts: its own and the Store's.
_WINGET_SOURCES = frozenset({"winget", "msstore"})

_TABLES: dict[str, tuple[dict[str, str], frozenset[str], frozenset[str]]] = {
    "apt": (_APT_SUBCOMMANDS, _APT_LETTERS, _APT_LONGS),
    "dnf": (_DNF_SUBCOMMANDS, _DNF_LETTERS, _DNF_LONGS),
    "zypper": (_ZYPPER_SUBCOMMANDS, _ZYPPER_LETTERS, _ZYPPER_LONGS),
    "apk": (_APK_SUBCOMMANDS, _APK_LETTERS, _APK_LONGS),
    "brew": (_BREW_SUBCOMMANDS, _BREW_LETTERS, _BREW_LONGS),
    "winget": (_WINGET_SUBCOMMANDS, _WINGET_LETTERS, _WINGET_LONGS),
}
#: Which run a subcommand asks for, where the manager has more than one.
_VARIANTS = {
    ("apt", "upgrade"): "upgrade", ("apt", "full-upgrade"): "dist-upgrade",
    ("apt", "dist-upgrade"): "dist-upgrade",
    ("zypper", "update"): "update", ("zypper", "up"): "update",
    ("zypper", "dist-upgrade"): "dist-upgrade", ("zypper", "dup"): "dist-upgrade",
}
#: apk options that add a repository or lower the trust check.
_APK_REPOSITORY_LONGS = frozenset({"--repository", "--allow-untrusted", "--keys-dir"})


def _generic(manager: str, words: Sequence[str]) -> Finding | None:
    table, allowed_letters, allowed_longs = _TABLES[manager]
    if manager == "winget":
        return _winget(words, table)
    letters, longs, operands = _split(words)
    index = next((i for i, word in enumerate(operands) if word in table), None)
    if index is None:
        return None
    unknown_options = any(letter not in allowed_letters for letter in letters) or any(
        long not in allowed_longs for long in longs
    )
    if index > 0 and not unknown_options:
        return None  # `apt search remove`: the subcommand is the first word
    kind = table[operands[index]]
    if manager == "apk" and (set(longs) & _APK_REPOSITORY_LONGS or "X" in letters):
        return Finding(REPOSITORY, manager)
    if kind == INSTALL:
        if index > 0:
            # Words before the subcommand are values of options this guard does not accept
            # (`apt-get -o Hook=... install x`).
            return Finding(UNOFFICIAL, manager)
        return _install_or_unofficial(
            manager, operands[1:], letters, longs, allowed_letters, allowed_longs
        )
    if kind == SYSTEM_UPGRADE:
        if index > 0 or unknown_options:
            return Finding(UNOFFICIAL, manager)
        if operands[1:]:
            return Finding(UPGRADE, manager)  # upgrading named packages is a partial upgrade
        return Finding(SYSTEM_UPGRADE, manager, (), _VARIANTS.get((manager, operands[0]), ""))
    return Finding(kind, manager)


def _winget(words: Sequence[str], table: dict[str, str]) -> Finding | None:
    """winget takes `--id X` and `--source S` with a value, so it is read word by word."""
    items = list(words)
    if not items:
        return None
    if items[0] == "source":
        sub = items[1] if len(items) > 1 else ""
        changes = sub in {"add", "remove", "reset", "import"}
        return Finding(REPOSITORY, "winget") if changes else None
    kind = table.get(items[0])
    if kind is None:
        return None
    if kind == SYSTEM_UPGRADE:
        return _winget_upgrade(items[1:])
    if kind != INSTALL:
        return Finding(kind, "winget")
    names: list[str] = []
    rest = items[1:]
    index = 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if word in {"--id", "--source"}:
            value = rest[index] if index < len(rest) else ""
            index += 1
            if word == "--id":
                names.append(value)
            elif value not in _WINGET_SOURCES:
                return Finding(UNOFFICIAL, "winget")
        elif word.startswith("--"):
            if word not in _WINGET_LONGS:
                return Finding(UNOFFICIAL, "winget")
        elif word.startswith("-") and word != "-":
            if any(letter not in _WINGET_LETTERS for letter in word[1:]):
                return Finding(UNOFFICIAL, "winget")
        else:
            names.append(word)
    if not names:
        return None
    if not _plain(names):
        return Finding(UNOFFICIAL, "winget")
    return Finding(INSTALL, "winget", tuple(dict.fromkeys(names)))


def _winget_upgrade(rest: Sequence[str]) -> Finding | None:
    """`winget upgrade --all` upgrades everything; without `--all` it only lists what could
    be upgraded; a name or `--id` upgrades part of it."""
    allowed = _WINGET_LONGS - {"--id", "--exact"} | {"--all"}
    everything = False
    for word in rest:
        if word == "--all":
            everything = True
        elif word == "--id" or not word.startswith("-"):
            return Finding(UPGRADE, "winget")
        else:
            unknown = (
                word not in allowed
                if word.startswith("--")
                else any(letter not in {"h", "r"} for letter in word[1:])
            )
            if unknown:
                return Finding(UNOFFICIAL, "winget")
    return Finding(SYSTEM_UPGRADE, "winget") if everything else None


# -- one command, then the whole command ------------------------------------------------


def _without_pkexec(words: Sequence[str]) -> list[str]:
    """``words`` behind a leading `pkexec` and its options: it runs the rest as root."""
    items = list(words)
    if not items or items[0].rsplit("/", 1)[-1] != "pkexec":
        return items
    index = 1
    while index < len(items) and items[index].startswith("-"):
        index += 2 if items[index] in {"--user", "-u"} else 1
    return items[index:]


def classify(words: Sequence[str]) -> Finding | None:
    """What one simple command (wrappers already removed) does to the system's packages,
    or None when it does nothing to them (a read, or not a package command at all)."""
    items = _without_pkexec(words)
    if not items:
        return None
    binary = items[0].rsplit("/", 1)[-1].lower().removesuffix(".exe")
    rest = items[1:]
    if binary in _AUR_HELPERS:
        return _aur(binary, rest)
    if binary in _REPOSITORY_PROGRAMS:
        return Finding(REPOSITORY)
    if binary == "rpm" and any(word in {"--import", "--addsign"} for word in rest):
        return Finding(REPOSITORY)
    manager = _BINARIES.get(binary)
    if manager is None:
        return None
    if manager == "pacman":
        return _pacman(rest)
    return _generic(manager, rest)


_REFUSED = frozenset({UPGRADE, REMOVE, REPOSITORY, AUR, UNOFFICIAL})
_ASKED = frozenset({INSTALL, SYSTEM_UPGRADE, AUR_INSTALL})


def _example(finding: Finding) -> str:
    names = " ".join(finding.packages)
    if finding.kind == SYSTEM_UPGRADE:
        return "pacman -Syu" if finding.manager == "pacman" else f"{finding.manager} upgrade"
    if finding.manager in {"pacman", "paru", "yay"}:
        return f"{finding.manager} -S {names}"
    return f"{finding.manager} {'add' if finding.manager == 'apk' else 'install'} {names}"


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def _installer_script_shell(words: Sequence[str]) -> bool:
    """A shell that reads its script from stdin (`| sudo bash`, `| sudo sh -s -- --yes`):
    a download piped into a shell is left as it was, so it is not refused here either."""
    if not words or words[0].rsplit("/", 1)[-1] not in _SHELLS:
        return False
    letters, longs, operands = _split(words[1:])
    return "c" not in letters and (not operands or "s" in letters)


def sudo_refusal(manager: str = "", terminal: bool = False) -> str:
    """Why a `sudo`/`doas` command that is not a package request does not run: the agent's
    shell has no terminal, so sudo could never be answered. ``manager`` is the machine's own,
    for the example; empty when none is known. With ``terminal`` (the app's terminal tabs
    are there), anything else goes to `run_in_terminal`, where the person approves the line
    and types the password."""
    what = (
        "A package install, full upgrade or AUR install" if manager == "pacman"
        else "A package install or full upgrade"
    )
    example = f" (`{_example(Finding(INSTALL, manager, ('<name>',)))}`)" if manager else ""
    rest = (
        "For anything else that needs root, call `run_in_terminal` with the exact command; "
        "the person approves it and types the password in the terminal."
        if terminal
        else "For anything else that needs root, tell the person the exact command."
    )
    return (
        f"refused: sudo cannot ask for a password in this shell. {what} runs without sudo "
        f"after the person approves{example}. {rest}"
    )


def judge(
    command: str, commands: Sequence[Simple], manager: str = "", terminal: bool = False
) -> PackageJudgement:
    """The guard's answer for ``command``, whose simple commands are ``commands``.

    Refuses a command that refreshes alone, upgrades part of the system, removes, adds a
    repository, installs from a file or uses an AUR helper for anything but a plain install.
    Asks about one that is only an install, only a full upgrade or only an AUR install,
    through a single manager (with the index refresh `apt update` that usually goes with it).
    A request mixed with any other command, or with a request of another kind, is refused:
    the core runs the request itself, in place of the command, so nothing else could go
    with it. Any other command run through `sudo` or `doas` is refused: it would fail at
    the password prompt (``manager`` is the machine's own, for the example in the message;
    ``terminal`` says `run_in_terminal` is there for it).
    """
    findings = [(simple, classify(simple.words) if simple.words else None) for simple in commands]
    for _, finding in findings:
        if finding is not None and finding.kind in _REFUSED:
            return PackageJudgement(
                refusal=refusal_for(finding.kind, finding.manager, finding.packages)
            )
    asks = [f for _, f in findings if f is not None and f.kind in _ASKED]
    refreshes = [f for _, f in findings if f is not None and f.kind == REFRESH]
    if not asks:
        if refreshes:
            return PackageJudgement(refusal=refusal_for(REFRESH, refreshes[0].manager))
        if any(
            simple.elevated and (simple.words or simple.unparsed)
            and not _installer_script_shell(simple.words)
            for simple in commands
        ):
            return PackageJudgement(refusal=sudo_refusal(manager, terminal))
        return PackageJudgement()
    managers = {f.manager for f in (*asks, *refreshes)}
    kinds = {f.kind for f in asks}
    others = [
        simple for simple, finding in findings
        if simple.writes or simple.unparsed
        or (simple.words and (finding is None or finding.kind not in _ASKED | {REFRESH}))
    ]
    if len(managers) > 1 or len(kinds) > 1 or others:
        return PackageJudgement(
            refusal=(
                "refused: run the package request as a command of its own, with nothing "
                "before, after or around it (no pipe, `&&`, redirect or wrapper script), "
                f"for example `{_example(asks[0])}`. The harness runs it for you after the "
                "person approves."
            )
        )
    kind = asks[0].kind
    packages = tuple(dict.fromkeys(name for f in asks for name in f.packages))
    variant = "dist-upgrade" if any(f.variant == "dist-upgrade" for f in asks) else asks[0].variant
    source = AUR_SOURCE if kind == AUR_INSTALL else OFFICIAL
    install = PackageInstall(asks[0].manager, packages, source, kind, variant)
    return PackageJudgement(install=install)
