"""Finding the destructive parts of a shell command.

Deletion can only reach the machine through the shell, so this is where it is caught. The
command is parsed, not pattern-matched, and every branch of `;`, `&&`, `||`, a pipeline
and a command substitution is walked, because `true || rm -rf x` deletes as surely as
`rm -rf x`.

Perfect detection is impossible: a command can build its own argument, or call a script
that deletes. So this is one layer of several. The parser catches what is written plainly,
the PATH shims catch what is written cleverly, and the protected-path list sits beneath
both. When the parser cannot understand a command, it says so instead of returning
"nothing found"; those two answers must never be confused.

It reports two kinds of finding. An *operation* destroys something (a deletion, an
overwrite) and is put to the person unless it is refused. A *modification* writes without
destroying (an append, a copy into a directory, a permission change) and answers to the
operating-system rule alone: refused on a protected path, and otherwise allowed as the
ordinary work it is.

It also lists every simple command, destructive or not, with the directory it runs in, for
the check that guards the session's own record (`guard/harness.py`).
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field, replace

import bashlex

#: Verbs whose arguments are the things destroyed.
_DELETES: frozenset[str] = frozenset({"rm", "rmdir", "shred", "srm", "unlink", "wipe"})
_TRUNCATES: frozenset[str] = frozenset({"truncate"})
#: truncate's options that take the next word: a size or a reference file, not a target.
_TRUNCATE_VALUES = frozenset({"-s", "--size", "-r", "--reference"})
_MOVES: frozenset[str] = frozenset({"mv"})

#: Programs that run the rest of their words as another command, the options of each that
#: take the next word, and how many operands of their own come before that command
#: (`timeout DURATION cmd`). Removing them lets `sudo rm` and `timeout 5 mv` be judged as
#: `rm` and `mv`. Codex unwraps sudo, env and trap the same way (openai/codex,
#: shell-command/src/command_safety/is_dangerous_command.rs, 2026-09-01). `command`, `env`
#: and `nohup` hiding `rm -rf /` from a flag-matching check is the gap OpenHands' review
#: found in software-agent-sdk#2718 (2026-09).
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "sudo": (frozenset({"-u", "--user", "-g", "--group", "-C", "--close-from", "-D",
                        "--chdir", "-h", "--host", "-p", "--prompt", "-r", "--role", "-t",
                        "--type", "-U", "--other-user", "-T", "--command-timeout"}), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "nohup": (frozenset(), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "setsid": (frozenset(), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "ionice": (frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P",
                          "--pgid", "-u", "--uid"}), 0),
    "xargs": (frozenset({"-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L", "-n",
                         "--max-args", "-P", "--max-procs", "-s", "--max-chars",
                         "--process-slot-var"}), 0),
    "busybox": (frozenset(), 0),
}
#: Wrappers after which the command is not looked up on the agent's PATH, so the PATH shim
#: never sees it: sudo and doas reset PATH (secure_path), busybox runs its own applet.
_PATH_BLIND = frozenset({"sudo", "doas", "busybox"})
#: Wrapper options that run the command in another directory: `sudo -D DIR`, `env -C DIR`.
_CHDIR = {"sudo": ("-D", "--chdir"), "env": ("-C", "--chdir")}

#: Shells whose `-c` argument is a script, read here like the command around it. Claude
#: Code's dangerous-rm check stops at `sh -c`/`bash -c` (anthropics/claude-code#96300,
#: 2026-09-23). Codex reads them, and fails closed past 8 levels.
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash"})
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
#: `bash -c "bash -c '...'"` is read this many levels down. Past it, the scan fails closed.
_MAX_NESTING = 8
_PARSE_ERRORS = (bashlex.errors.ParsingError, NotImplementedError, IndexError, AttributeError,
                 TypeError)
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

#: Whole commands that destroy without naming a file: the working tree, or Docker's storage.
_SWEEPERS: dict[tuple[str, ...], str] = {
    ("git", "clean"): "removes untracked files from the working tree",
    ("git", "reset"): "discards changes when --hard is given",
    ("git", "checkout"): "discards uncommitted changes to the paths it is given",
    ("git", "restore"): "discards uncommitted changes to the paths it is given",
    ("docker", "system"): "prunes images, containers and volumes",
    ("docker", "volume"): "removes volumes",
}

#: Interpreters whose inline code (`python -c CODE`, `perl -e CODE`) is read for calls that
#: delete files. A script file is out of reach; inline code is right there in the text.
_INTERPRETER = re.compile(r"(python[0-9.]*|perl|ruby|node|nodejs|php)")
#: The option each interpreter takes its inline code with.
_INLINE_CODE = {"python": ("-c",), "perl": ("-e", "-E"), "ruby": ("-e",),
                "node": ("-e", "--eval", "-p", "--print"), "php": ("-r",)}
_DELETES_IN_CODE = re.compile(
    r"\b(os\.(remove|unlink|rmdir|removedirs)|shutil\.rmtree|\.unlink\(|\.rmdir\("
    r"|unlink\b|rmtree|rmSync|rmdirSync|unlinkSync|fs\.rm\b|rm_rf|rm_r\b|FileUtils\.rm"
    r"|File\.delete|truncate\()"
)

_PACKAGE_REMOVERS: dict[tuple[str, ...], str] = {
    ("pacman",): "removes installed packages",
    ("apt",): "removes installed packages",
    ("apt-get",): "removes installed packages",
    ("dnf",): "removes installed packages",
    ("yum",): "removes installed packages",
    ("paru",): "removes installed packages",
    ("yay",): "removes installed packages",
}

_REMOVE_FLAGS = frozenset({"-R", "-Rs", "-Rns", "-Rsn", "-Rdd", "remove", "purge", "autoremove"})

_RECURSIVE = frozenset({"-r", "-R", "--recursive", "-rf", "-fr", "-Rf", "-fR"})
_FORCE = frozenset({"-f", "--force", "-rf", "-fr", "-Rf", "-fR"})

#: Redirections that truncate their target: one operation spelt four ways. Bash's manual
#: ("Redirections") defines `&>word` and `>&word` as `>word 2>&1`, and `>|` as `>` past
#: `noclobber`. So each is judged exactly as `>` always was.
_OVERWRITES = frozenset({">", ">|", "&>", ">&"})
#: Redirections that open their target for writing without truncating it.
_OPENS_FOR_WRITING = {">>": ">>", "&>>": ">>", "<>": "<>"}

#: Commands that write into a named destination without destroying what they name, and
#: the options among theirs that take the next word as a value.
_COPIERS: dict[str, frozenset[str]] = {
    "cp": frozenset({"-t", "--target-directory", "-S", "--suffix"}),
    "install": frozenset({"-t", "--target-directory", "-S", "--suffix", "-m", "--mode",
                          "-o", "--owner", "-g", "--group", "--strip-program"}),
    "ln": frozenset({"-t", "--target-directory", "-S", "--suffix"}),
    "mv": frozenset({"-t", "--target-directory", "-S", "--suffix"}),
}
#: Commands that change a file's permissions or ownership in place.
_CHANGERS = frozenset({"chmod", "chown", "chgrp"})
_CHANGER_OPTIONS = frozenset({
    "-c", "-f", "-v", "-h", "-H", "-L", "-P", "-R", "--changes", "--silent", "--quiet",
    "--verbose", "--recursive", "--dereference", "--no-dereference", "--preserve-root",
    "--no-preserve-root",
})

#: Write targets that are not files. Writing to one changes nothing on disk, so it is not
#: the OS-file modification rule 2 refuses. `2>/dev/null` is the most common redirection
#: there is, and refusing it cost qwen3.5-9b 22 refused calls in 16 runs of its quick
#: control at e12e48e, 14 of which failed. Deletions are not writes: `rm /dev/null` is
#: still refused, and so is every other path under /dev.
_WRITE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
_FD_SINK = re.compile(r"/dev/fd/[0-9]+")


def is_write_sink(word: str) -> bool:
    """True only when ``word`` names one of the sinks above, compared as written.

    Lexical on purpose: `/dev/stdout` resolves into `/proc/<pid>/fd/1` and `/dev/fd/N`
    into `/proc`, so asking the filesystem would compare the wrong thing. Duplicate
    slashes and `.` components are collapsed. A `..` anywhere refuses the match, because
    the kernel resolves `..` physically and `normpath` lexically, and the two disagree
    across a symlink (`/dev/fd/..` is `/proc/self`, not `/dev`). A relative word, `~`, a
    variable or a glob never matches.
    """
    if not word.startswith("/") or ".." in word.split("/"):
        return False
    normalized = posixpath.normpath(word)
    return normalized in _WRITE_SINKS or _FD_SINK.fullmatch(normalized) is not None


@dataclass(frozen=True, slots=True)
class Destructive:
    """One destructive operation found inside a command."""

    verb: str
    #: What it acts on. Empty when the operation destroys without naming anything.
    targets: tuple[str, ...] = ()
    why: str = ""
    recursive: bool = False
    force: bool = False
    #: The fragment it was found in, for showing the user what they are approving.
    fragment: str = ""
    #: True when it reaches everything beneath its targets too, as a deletion or a move
    #: does. A target that only *contains* a protected path is then refused like one
    #: inside it, and a target the shell has not expanded yet is refused when it could be.
    subtree: bool = False
    #: False when the verb is not looked up on the agent's PATH (an absolute path, a
    #: `PATH=` assignment, `sudo`, `busybox`). The PATH shim never sees it run, so it cannot
    #: be left to the shim to ask about.
    shimmable: bool = True
    #: True when it destroys only what is already there: `tee FILE` empties an existing
    #: FILE and only creates a missing one. The policy, which can look, decides which.
    existing_only: bool = False
    #: The `cd` arguments in force where it runs, as in `Simple.cd`: empty for the shell's
    #: own directory, None when one cannot be known from the text. `cd ~ && rm -rf .ssh`
    #: deletes `~/.ssh`, not a `.ssh` in the project.
    cd: tuple[str, ...] | None = ()
    #: True when it replaces what its targets hold instead of removing them (`>`, tee, dd,
    #: truncate, a copy or move onto an existing file).
    overwrites: bool = False
    #: False when its targets are not what it destroys but where it looks: find's start
    #: points, rsync's destination, a package manager's package names.
    named: bool = True
    #: For a write into a destination (cp, install, ln -f, mv): what is written there, so
    #: the policy can tell whether an existing file of that name is replaced.
    sources: tuple[str, ...] = ()
    #: The command's own words, for a sweeper the policy can ask git about (`git clean -n`).
    argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Simple:
    """One simple command, for a check that must see every command and not only the
    destructive ones (`guard/harness.py`): what runs, what its redirections write, and in
    which directory."""

    #: Its words after quote removal, with a leading wrapper (`sudo`, `env`, `nice`,
    #: `timeout`, ...) removed so the first word is the program that actually runs.
    words: tuple[str, ...] = ()
    #: Every file a redirection writes, truncating or appending. A sink (`is_write_sink`)
    #: is not a file and is left out.
    writes: tuple[str, ...] = ()
    #: The `cd` arguments that ran before it in this command and still apply, in order and
    #: as written. A subshell's or a pipeline stage's `cd` does not leak out. Empty means
    #: the shell's own working directory; None means one of them cannot be known from the
    #: text (`cd -`, `popd`).
    cd: tuple[str, ...] | None = ()
    #: Set instead of `words` when this was the script of a nested shell (`bash -c`,
    #: `eval`) that could not be parsed or was nested too deep: the text itself, so it can
    #: still be judged.
    unparsed: str = ""
    #: True when `sudo` or `doas` was among the wrappers removed from ``words``.
    elevated: bool = False


@dataclass(frozen=True, slots=True)
class Scan:
    operations: tuple[Destructive, ...] = ()
    #: True when the command could not be parsed and was judged by pattern alone. An
    #: unparseable command is treated as suspicious, never as clean.
    unparsed: bool = False
    uses_sudo: bool = False
    findings: tuple[str, ...] = field(default_factory=tuple)
    #: Writes that destroy nothing: an append, `tee -a`, a copy or link into a directory,
    #: a chmod. Judged against the operating-system rule alone, never put to the person.
    modifications: tuple[Destructive, ...] = ()
    #: Every simple command, in the order the shell runs them, destructive or not. Empty
    #: when the command could not be parsed.
    commands: tuple[Simple, ...] = ()

    @property
    def is_destructive(self) -> bool:
        return bool(self.operations)


def _words(node: object) -> list[str]:
    return [
        part.word
        for part in getattr(node, "parts", [])
        if getattr(part, "kind", "") == "word" and hasattr(part, "word")
    ]


def _commands(nodes: list[object]) -> list[object]:
    """Every command node, through lists, pipelines, compounds and substitutions, and
    every compound command that carries its own redirections (`{ ...; } > file`)."""
    found: list[object] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        kind = getattr(node, "kind", "")
        if kind == "command" or (kind == "compound" and getattr(node, "redirects", None)):
            found.append(node)
        for attribute in ("parts", "list", "commands"):
            children = getattr(node, attribute, None)
            if isinstance(children, list):
                stack.extend(children)
        # A command substitution hangs its inner command off `.command`, singular. Missing
        # it would make `echo $(rm secret)` read as a harmless echo.
        inner = getattr(node, "command", None)
        if inner is not None and hasattr(inner, "kind"):
            stack.append(inner)
    return found


def _flags(words: list[str]) -> tuple[bool, bool]:
    recursive = any(w in _RECURSIVE for w in words)
    force = any(w in _FORCE for w in words)
    # Clustered short flags: -rf, -rfv, -fRv ...
    for word in words:
        if re.fullmatch(r"-[a-zA-Z]{2,}", word):
            letters = set(word[1:])
            recursive = recursive or bool(letters & {"r", "R"})
            force = force or "f" in letters
    return recursive, force


def _targets(words: list[str], takes_value: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """Arguments that are not flags. `--` ends the flags, as it does everywhere; an option
    in ``takes_value`` swallows the word after it."""
    out: list[str] = []
    after_separator = False
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word == "--":
            after_separator = True
            continue
        if not after_separator and word.startswith("-"):
            skip = word in takes_value
            continue
        out.append(word)
    return tuple(out)


def _split_string(words: list[str], index: int) -> list[str] | None:
    """``words`` with env's `-S STRING` at ``index`` replaced by STRING's own words, as env
    does. None when that word is not the option.

    `--split-string` may be shortened, and `-iS` keeps its `-i`. A string that does not
    split (an open quote) is read as a script, which fails closed.
    """
    word = words[index]
    name, equals, value = word.partition("=")
    if word.startswith("--") and len(name) >= 3 and "--split-string".startswith(name):
        flags: list[str] = []
        inline = bool(equals)
    elif match := re.fullmatch(r"-([0iv]*)S(.*)", word, re.DOTALL):
        flags = [f"-{letter}" for letter in match.group(1)]
        value = match.group(2)
        inline = bool(value)
    else:
        return None
    end = index + 1
    if not inline:
        value = words[end] if end < len(words) else ""
        end += 1
    try:
        split = shlex.split(value)
    except ValueError:
        split = ["sh", "-c", value]
    return [*words[:index], *flags, *split, *words[end:]]


def _chdir(name: str, words: list[str], index: int) -> str | None:
    """The directory a wrapper's option at ``index`` moves into, or None if it does not."""
    short, long = _CHDIR.get(name, ("", ""))
    word = words[index]
    if not short:
        return None
    if word in {short, long}:
        return words[index + 1] if index + 1 < len(words) else None
    if word.startswith(long + "="):
        return word[len(long) + 1 :]
    if word.startswith(short) and len(word) > len(short):
        return word[len(short) :]
    return None


def _unwrap(
    words: list[str], shimmable: bool
) -> tuple[list[str] | None, bool, tuple[str, ...]]:
    """The words of the real command behind any wrappers, whether a PATH shim will still
    see it, and the directories the wrappers move into before running it (`sudo -D`,
    `env -C`). None when the wrappers run nothing (`command -v rm`, `sudo -l`)."""
    index = 0
    moves: list[str] = []
    while index < len(words):
        name = words[index].rsplit("/", 1)[-1]
        spec = _WRAPPERS.get(name)
        if spec is None:
            return words[index:], shimmable, tuple(moves)
        takes_value, operands_before = spec
        shimmable = shimmable and name not in _PATH_BLIND
        index += 1
        skipped = 0
        options = True
        while index < len(words):
            word = words[index]
            if options and word == "--":
                # Only the options end here. env's assignments and timeout's duration
                # still come before the command (`env -- A=1 rm`, `timeout -- 5 rm`).
                options = False
                index += 1
                continue
            if name == "env" and options and (split := _split_string(words, index)) is not None:
                words = split
                continue
            if name == "env" and (word == "-" or (not word.startswith("-") and "=" in word)):
                # `env -` empties the environment; `env PATH=...` replaces the lookup.
                shimmable = shimmable and word != "-" and not word.startswith("PATH=")
                index += 1
                continue
            if options and word.startswith("-") and len(word) > 1:
                if name == "command" and word in {"-v", "-V"}:
                    return None, shimmable, tuple(moves)
                if (directory := _chdir(name, words, index)) is not None:
                    moves.append(directory)
                clears_path = (
                    (name == "command" and word == "-p")
                    or (name == "env" and word in {"-i", "--ignore-environment",
                                                   "-uPATH", "--unset=PATH"})
                    or (name == "env" and word in {"-u", "--unset"}
                        and words[index + 1 : index + 2] == ["PATH"])
                )
                shimmable = shimmable and not clears_path
                index += 2 if word in takes_value else 1
                continue
            if skipped < operands_before:
                skipped += 1
                index += 1
                continue
            break
    return None, shimmable, tuple(moves)


def _shell_script(rest: list[str]) -> str | None:
    """The script a shell was handed with `-c` (`bash -lc '...'`), or None."""
    reads_string = False
    index = 0
    while index < len(rest):
        word = rest[index]
        if word in {"--", "-"}:
            index += 1
            break
        if word in _SHELL_VALUE_OPTIONS:
            index += 2
            continue
        if word.startswith("--"):
            index += 1
            continue
        if word[:1] in {"-", "+"} and len(word) > 1:
            reads_string = reads_string or (word[0] == "-" and "c" in word[1:])
            index += 1
            continue
        break
    return rest[index] if reads_string and index < len(rest) else None


def _find_parts(rest: list[str]) -> tuple[tuple[str, ...], list[str]]:
    """find's start points and its expression. With none named, it starts at `.`."""
    index = 0
    while index < len(rest):
        word = rest[index]
        if word in {"-H", "-L", "-P"} or (word.startswith("-O") and word[2:].isdigit()):
            index += 1
        elif word == "-D":
            index += 2
        else:
            break
    starts: list[str] = []
    while index < len(rest) and not rest[index].startswith("-") and rest[index] not in {
        "(", ")", "!", ","
    }:
        starts.append(rest[index])
        index += 1
    return tuple(starts) or (".",), rest[index:]


def _find_actions(expression: list[str]) -> list[tuple[str, list[str]]]:
    """Each `-exec`, `-execdir`, `-ok` or `-okdir` in a find expression, with the words of
    the command it runs. It ends at `;`, or at a `+` right after `{}`."""
    actions: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(expression):
        action = expression[index]
        index += 1
        if action not in _FIND_ACTIONS:
            continue
        words: list[str] = []
        while index < len(expression):
            word = expression[index]
            index += 1
            if word == ";" or (word == "+" and words[-1:] == ["{}"]):
                break
            words.append(word)
        actions.append((action, words))
    return actions


def _matched(operation: Destructive) -> Destructive | None:
    """A find action's operation without the targets that hold `{}`: those are the
    matches, already judged through the start points. None when nothing else is left."""
    targets = tuple(target for target in operation.targets if "{}" not in target)
    return replace(operation, targets=targets) if targets else None


def _operands(words: list[str], takes_value: frozenset[str]) -> tuple[list[str], dict[str, str]]:
    """Operands, and the values of the options in ``takes_value``.

    A value is the next word, or attached (`-tDIR`, `--target-directory=DIR`). `--` ends
    the options; a lone `-` is an operand.
    """
    operands: list[str] = []
    values: dict[str, str] = {}
    index = 0
    after_separator = False
    while index < len(words):
        word = words[index]
        index += 1
        if after_separator or not word.startswith("-") or word == "-":
            operands.append(word)
        elif word == "--":
            after_separator = True
        elif word.startswith("--"):
            name, attached, value = word.partition("=")
            if attached:
                values[name] = value
            elif name in takes_value and index < len(words):
                values[name] = words[index]
                index += 1
        elif word[:2] in takes_value:
            if len(word) > 2:
                values[word[:2]] = word[2:]
            elif index < len(words):
                values[word[:2]] = words[index]
                index += 1
    return operands, values


def _written(node: object) -> tuple[list[str], list[tuple[str, str]]]:
    """What a node's redirections write: files overwritten, and (form, file) opened for
    writing without truncation. A sink (`is_write_sink`) is not a file, and `>&` onto a
    descriptor (`>&2`, `>&-`, `>&$fd`) names none."""
    overwritten: list[str] = []
    opened: list[tuple[str, str]] = []
    for part in (*getattr(node, "parts", []), *(getattr(node, "redirects", None) or [])):
        if getattr(part, "kind", "") != "redirect":
            continue
        form = str(getattr(part, "type", ""))
        word = getattr(getattr(part, "output", None), "word", None)
        if not isinstance(word, str) or is_write_sink(word):
            continue
        if form == ">&" and (word == "-" or word.isdigit() or word.startswith("$")):
            continue
        if form in _OVERWRITES:
            overwritten.append(word)
        elif form in _OPENS_FOR_WRITING:
            opened.append((_OPENS_FOR_WRITING[form], word))
    return overwritten, opened


def _destinations(binary: str, rest: list[str]) -> tuple[str, ...]:
    """Where cp, install, ln or mv write: `-t DIR`, else the last operand of two or more;
    for `install -d`, every operand."""
    operands, values = _operands(rest, _COPIERS[binary])
    if target := values.get("-t") or values.get("--target-directory"):
        return (target,)
    directories = binary == "install" and any(
        word == "--directory" or re.fullmatch(r"-[a-zA-Z]*d[a-zA-Z]*", word) for word in rest
    )
    if directories:
        return tuple(operands)
    return tuple(operands[-1:]) if len(operands) >= 2 else ()


def _appends(rest: list[str]) -> bool:
    """True when tee was told to append: `-a`, `--append`, or a cluster holding `a`."""
    for word in rest:
        if word == "--":
            return False
        if word == "--append" or re.fullmatch(r"-[aip]*a[aip]*", word):
            return True
    return False


def _changed(binary: str, rest: list[str]) -> tuple[tuple[str, ...], bool]:
    """The files chmod, chown or chgrp change, and whether it recurses.

    A chmod mode may itself start with a dash (`chmod -x file`), so a dashed word that is
    not one of their options is the mode, not a flag.
    """
    recursive = False
    words: list[str] = []
    reference = False
    after_separator = False
    for word in rest:
        if after_separator:
            words.append(word)
        elif word == "--":
            after_separator = True
        elif word.startswith("--reference"):
            reference = True
        elif word in _CHANGER_OPTIONS or re.fullmatch(r"-[cfvhHLPR]+", word):
            recursive = recursive or word in {"-R", "--recursive"} or (
                not word.startswith("--") and "R" in word
            )
        else:
            words.append(word)
    return tuple(words if reference else words[1:]), recursive


def _inspect(
    node: object, source: str, depth: int
) -> tuple[list[Destructive], list[Destructive]]:
    words = _words(node)
    fragment = source[node.pos[0] : node.pos[1]] if hasattr(node, "pos") else " ".join(words)
    found: list[Destructive] = []
    touched: list[Destructive] = []
    if words:
        # `PATH=/usr/bin rm x` looks rm up somewhere the shim is not.
        path_assigned = any(
            getattr(part, "kind", "") == "assignment"
            and str(getattr(part, "word", "")).startswith("PATH=")
            for part in getattr(node, "parts", [])
        )
        _command(words, fragment, found, touched, depth, shimmable=not path_assigned)

    overwritten, opened = _written(node)
    for target in overwritten:
        # A file that is not there yet is created, which destroys nothing; the policy looks.
        found.append(
            Destructive(">", (target,), "overwrites it", False, False, fragment,
                        existing_only=True, overwrites=True)
        )
    for form, target in opened:
        touched.append(Destructive(form, (target,), "writes to it", False, False, fragment))
    return found, touched


def _command(
    words: list[str],
    fragment: str,
    found: list[Destructive],
    touched: list[Destructive],
    depth: int,
    *,
    shimmable: bool = True,
) -> None:
    """What one simple command destroys (``found``) and modifies (``touched``)."""
    program, shimmable, moves = _unwrap(words, shimmable)
    if not program:
        return
    if moves:
        # `sudo -D DIR` and `env -C DIR` run it in DIR: what it finds starts from there.
        inner_found: list[Destructive] = []
        inner_touched: list[Destructive] = []
        _command(program, fragment, inner_found, inner_touched, depth, shimmable=shimmable)
        found.extend(_moved(operation, moves) for operation in inner_found)
        touched.extend(_moved(operation, moves) for operation in inner_touched)
        return
    # A verb named by its path is never looked up on PATH, so no shim will see it.
    shimmable = shimmable and "/" not in program[0]
    binary = program[0].rsplit("/", 1)[-1]
    rest = program[1:]
    recursive, force = _flags(rest)

    if binary in _DELETES:
        found.append(
            Destructive(
                binary, _targets(rest), "deletes files", recursive, force, fragment, subtree=True,
                shimmable=shimmable,
            )
        )
    elif binary in _TRUNCATES:
        found.append(
            Destructive(binary, _targets(rest, _TRUNCATE_VALUES), "empties files", False, force,
                        fragment, shimmable=shimmable, overwrites=True)
        )
    elif binary in _MOVES:
        operands, values = _operands(rest, _COPIERS["mv"])
        directory = values.get("-t") or values.get("--target-directory")
        sources = operands if directory else operands[:-1]
        if sources and (directory or len(operands) >= 2):
            found.append(
                Destructive(
                    "mv", tuple(sources), f"moves them to {directory or operands[-1]}",
                    recursive, force, fragment, subtree=True,
                )
            )
        # Where it lands is written too. Not a sink: `mv x /dev/null` replaces the node.
        if destination := _destinations("mv", rest):
            touched.append(Destructive("mv", destination, "moves files into it", False, force,
                                       fragment, sources=_replacing("mv", rest, sources)))
    elif binary in _COPIERS:
        destination = _destinations(binary, rest)
        if binary != "ln":
            # cp and install write *through* an existing file, so a sink stays a sink; ln,
            # like mv, replaces the node itself.
            destination = tuple(t for t in destination if not is_write_sink(t))
        if destination:
            operands, values = _operands(rest, _COPIERS[binary])
            directory = values.get("-t") or values.get("--target-directory")
            copied = operands if directory else operands[:-1]
            touched.append(Destructive(binary, destination, "writes into it", recursive, force,
                                       fragment, sources=_replacing(binary, rest, copied)))
    elif binary == "tee":
        targets = tuple(t for t in _targets(rest) if not is_write_sink(t))
        if targets and _appends(rest):
            touched.append(Destructive("tee", targets, "writes to it", False, False, fragment))
        elif targets:
            # Without -a, tee truncates what it opens: an existing file is overwritten,
            # exactly like `>`.
            found.append(Destructive("tee", targets, "overwrites it", False, False, fragment,
                                     existing_only=True, overwrites=True))
    elif binary in _CHANGERS:
        targets, recursive = _changed(binary, rest)
        if targets:
            touched.append(Destructive(binary, targets, "changes its permissions or owner",
                                       recursive, False, fragment, subtree=recursive))
    elif binary.startswith("mkfs"):
        touched.append(Destructive(binary, _targets(rest), "formats it", False, True, fragment))
    elif binary == "dd":
        for word in rest:
            if word.startswith("of=") and not is_write_sink(word[3:]):
                found.append(
                    Destructive("dd", (word[3:],), "writes over it", False, True, fragment,
                                existing_only=True, overwrites=True)
                )
    elif binary == "find":
        starts, expression = _find_parts(rest)
        # Each action is a command of its own: `-exec sh -c '...'`, `-exec cp {} /etc/`.
        action_found: list[Destructive] = []
        action_touched: list[Destructive] = []
        for action, action_words in _find_actions(expression):
            ran: list[Destructive] = []
            wrote: list[Destructive] = []
            _command(action_words, fragment, ran, wrote, depth, shimmable=shimmable)
            if action in {"-execdir", "-okdir"}:
                # It runs in each match's own directory, which the text cannot tell.
                ran = [replace(operation, cd=None) for operation in ran]
                wrote = [replace(operation, cd=None) for operation in wrote]
            action_found.extend(ran)
            action_touched.extend(wrote)
        if "-delete" in expression:
            found.append(
                Destructive("find -delete", starts, "deletes what it matches",
                            True, False, fragment, subtree=True, named=False)
            )
        elif any(operation.verb in _DELETES for operation in action_found):
            found.append(
                Destructive("find -exec rm", starts,
                            "deletes what it matches", True, True, fragment, subtree=True,
                            named=False)
            )
        found.extend(kept for operation in action_found if (kept := _matched(operation)))
        touched.extend(kept for operation in action_touched if (kept := _matched(operation)))
    elif binary == "rsync":
        operands = _targets(rest)
        if any(w in {"--delete", "--del"} or w.startswith("--delete-") for w in rest):
            found.append(
                Destructive("rsync --delete", operands[-1:],
                            "deletes whatever is not in the source", True, False, fragment,
                            subtree=True, named=False)
            )
        if "--remove-source-files" in rest and len(operands) >= 2:
            found.append(
                Destructive("rsync --remove-source-files", operands[:-1],
                            "deletes the source files it sent", True, False, fragment,
                            subtree=True)
            )
        if len(operands) >= 2:
            touched.append(Destructive("rsync", operands[-1:], "writes into it", True,
                                       False, fragment))
    elif binary in _SHELLS or binary in {"eval", "trap"}:
        if (script := _nested_script(program)) is not None:
            _nested(script, depth, found, touched, shimmable)
    elif _INTERPRETER.fullmatch(binary):
        if (flag := _deleting_code(binary, rest)) is not None:
            found.append(
                Destructive(f"{binary} {flag}", (), "runs code that deletes files",
                            True, True, fragment)
            )
    else:
        key = (binary, rest[0]) if rest else (binary,)
        if (why := _SWEEPERS.get(key)) is not None:
            if _sweeps(key, rest[1:], force):
                found.append(Destructive(" ".join(key), (), why, True, force, fragment,
                                         argv=tuple(program)))
        elif (why := _PACKAGE_REMOVERS.get((binary,))) is not None and any(
            flag in _REMOVE_FLAGS for flag in rest
        ):
            found.append(Destructive(binary, _targets(rest), why, False, force, fragment,
                                     named=False))


def _replacing(binary: str, rest: list[str], sources: list[str]) -> tuple[str, ...]:
    """The sources a cp, install, ln or mv may write over an existing file with, or none
    when it was told never to replace one (`-n`, `--no-clobber`; ln without `-f`)."""
    words = [w for w in rest if w != "--"]
    if binary == "ln":
        forced = any(
            w == "--force" or (re.fullmatch(r"-[a-zA-Z]+", w) and "f" in w) for w in words
        )
        return tuple(sources) if forced else ()
    no_clobber = any(
        w in {"--no-clobber", "--update=none", "--update=none-fail"}
        or (re.fullmatch(r"-[a-zA-Z]+", w) and "n" in w and binary in {"cp", "mv"})
        for w in words
    )
    return () if no_clobber else tuple(sources)


def _sweeps(key: tuple[str, ...], rest: list[str], force: bool) -> bool:
    """True when this git or docker command destroys work: `git reset --hard`, a checkout
    or restore of paths over the working tree, any `git clean` or Docker prune."""
    if key == ("git", "reset"):
        return "--hard" in rest
    if key == ("git", "checkout"):
        # `git checkout BRANCH` refuses to overwrite local changes; a path does not.
        return force or "--" in rest or any(w in {".", ":/"} for w in rest)
    if key == ("git", "restore"):
        staged = any(w in {"--staged", "-S"} for w in rest)
        worktree = any(w in {"--worktree", "-W"} for w in rest)
        return worktree or not staged
    return True


def _deleting_code(binary: str, rest: list[str]) -> str | None:
    """The option that handed an interpreter inline code calling a deleting function, or
    None. A script file is not read: its contents are not in the command."""
    family = "python" if binary.startswith("python") else binary.removesuffix("js")
    flags = _INLINE_CODE.get(family, ())
    for index, word in enumerate(rest):
        if word in flags and index + 1 < len(rest):
            return word if _DELETES_IN_CODE.search(rest[index + 1]) else None
        if not word.startswith("-"):
            return None
    return None


def _nested(
    script: str,
    depth: int,
    found: list[Destructive],
    touched: list[Destructive],
    shimmable: bool,
) -> None:
    """Read a script handed to a shell, `eval` or `trap` like the command around it.

    Whatever kept the shell off the agent's PATH keeps its commands off it too: under
    `sudo bash -c 'rm x'` the rm is not the shim's.
    """
    inner = _scan(script, depth + 1)
    found.extend(
        operation if shimmable else replace(operation, shimmable=False)
        for operation in inner.operations
    )
    touched.extend(inner.modifications)


def _elevated(words: list[str], program: list[str]) -> bool:
    """Whether `sudo` or `doas` stood in front of ``program``, the real command of
    ``words``. If ``program`` is not the tail of ``words`` (`env -S` split it), any such
    word counts."""
    cut = len(words) - len(program)
    front = words[:cut] if program and cut >= 0 and words[cut:] == program else words
    return any(word.rsplit("/", 1)[-1] in {"sudo", "doas"} for word in front)


def _program(words: list[str]) -> list[str]:
    """``words`` from the program that actually runs, behind any wrappers. Empty when the
    wrappers run nothing (`command -v rm`)."""
    program, _, _ = _unwrap(words, True)
    return program or []


def _nested_script(words: list[str]) -> str | None:
    """The script a nested shell (`bash -c`), `eval` or `trap` would run, when it is right
    there in the words."""
    if not words:
        return None
    name = words[0].rsplit("/", 1)[-1]
    rest = words[1:]
    if name == "eval":
        return " ".join(rest) or None
    if name == "trap":
        action = rest[1:] if rest[:1] == ["--"] else rest
        return action[0] if action and action[0] and not action[0].startswith("-") else None
    if name in _SHELLS:
        return _shell_script(rest)
    return None


def _writes(node: object) -> tuple[str, ...]:
    """Every file a redirection on this node writes: `>`, `>>`, `>|`, `&>`, `&>>`, `<>`
    and `>& file`. A descriptor (`2>&1`) and a sink are not files."""
    found: list[str] = []
    parts = getattr(node, "redirects", None) or getattr(node, "parts", None) or []
    for part in parts:
        if getattr(part, "kind", "") != "redirect":
            continue
        if ">" not in str(getattr(part, "type", "")):
            continue
        word = getattr(getattr(part, "output", None), "word", None)
        if isinstance(word, str) and not is_write_sink(word):
            found.append(word)
    return tuple(found)


def _substitutions(node: object) -> list[object]:
    """The commands inside `$(...)`, backticks and `<(...)` anywhere in this word."""
    found: list[object] = []
    stack = list(getattr(node, "parts", None) or [])
    while stack:
        part = stack.pop()
        if getattr(part, "kind", "") in {"commandsubstitution", "processsubstitution"}:
            inner = getattr(part, "command", None)
            if inner is not None:
                found.append(inner)
            continue
        stack.extend(getattr(part, "parts", None) or [])
    return found


def _after_cd(words: list[str], cd: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """Where the commands after this one run, when this one changes directory."""
    if not words or words[0] not in {"cd", "pushd", "popd"} or cd is None:
        return cd
    if words[0] == "popd":
        return None
    arguments = [word for word in words[1:] if word not in {"-P", "-L", "-e", "-@", "--"}]
    target = arguments[0] if arguments else "~"
    if target == "-" or target.startswith(("+", "-")):
        return None
    return (*cd, target)


#: A script that could change directory, when it cannot be read.
_MOVES_TEXT = re.compile(r"\b(cd|pushd|popd)\b")


def _readable(script: str, depth: int) -> list[object] | None:
    """A nested script, parsed. None when it is nested too deep or does not parse."""
    if depth >= _MAX_NESTING:
        return None
    try:
        return bashlex.parse(script)
    except _PARSE_ERRORS:
        return None


class _Walker:
    """Collects `Simple` commands in the order the shell runs them, keeping track of `cd`."""

    def __init__(self, *, nested: bool = True) -> None:
        self.found: list[Simple] = []
        #: The `cd`s in force at each node of the outermost trees, by `id`, for the
        #: destructive scan of the same trees.
        self.where: dict[int, tuple[str, ...] | None] = {}
        #: Whether a nested shell's script is read too. The destructive scan reads its own.
        self._nested = nested
        #: Functions defined here whose body changes directory.
        self._moving: set[str] = set()

    def walk(
        self, node: object, cd: tuple[str, ...] | None, depth: int
    ) -> tuple[str, ...] | None:
        """Record ``node``'s commands; return the directory the next sibling runs in."""
        kind = getattr(node, "kind", "")
        if kind == "command":
            return self._command(node, cd, depth)
        if kind == "pipeline":
            # Every stage runs in its own subshell: a `cd` in one changes nothing after it.
            for part in getattr(node, "parts", None) or []:
                self.walk(part, cd, depth)
            return cd
        if kind == "compound":
            if depth == 0:
                self.where[id(node)] = cd
            children = list(getattr(node, "list", None) or [])
            subshell = bool(children) and getattr(children[0], "word", "") == "("
            current = cd
            for part in children:
                current = self.walk(part, current, depth)
            if writes := _writes(node):
                self.found.append(Simple(writes=writes, cd=cd))
            return cd if subshell else current
        if kind == "function":
            # Defining it runs nothing yet, but it is usually called right after. Where a
            # call to it leaves the shell is not known when its body changes directory.
            after = cd
            for part in getattr(node, "parts", None) or []:
                after = self.walk(part, after, depth)
            if after != cd and (name := getattr(getattr(node, "name", None), "word", None)):
                self._moving.add(name)
            return cd
        current = cd
        for part in getattr(node, "parts", None) or []:
            current = self.walk(part, current, depth)
        return current

    def _command(
        self, node: object, cd: tuple[str, ...] | None, depth: int
    ) -> tuple[str, ...] | None:
        if depth == 0:
            self.where[id(node)] = cd
        for part in getattr(node, "parts", None) or []:
            for inner in _substitutions(part):
                self.walk(inner, cd, depth)
        raw = _words(node)
        program, _, moves = _unwrap(raw, True)
        words = program or []
        self.found.append(
            Simple(words=tuple(words), writes=_writes(node), cd=cd,
                   elevated=bool(words) and _elevated(raw, words))
        )
        # `sudo -D DIR` and `env -C DIR` run their command in DIR, and nothing after it.
        runs_in = cd if cd is None or not moves else (*cd, *moves)
        if moves:
            self.found[-1] = replace(self.found[-1], cd=runs_in)
        after = self._script(words, runs_in, depth)
        if words and words[0].rsplit("/", 1)[-1] == "find":
            for action, action_words in _find_actions(_find_parts(words[1:])[1]):
                # `-execdir` runs in each match's own directory, which the text cannot tell.
                where = None if action in {"-execdir", "-okdir"} else runs_in
                self._action(action_words, where, depth)
        if moves:
            after = cd
        if words and words[0] in self._moving:
            return None
        return _after_cd(words, after)

    def _action(self, words: list[str], cd: tuple[str, ...] | None, depth: int) -> None:
        """A command find runs for its matches, recorded as one of its own."""
        program, _, moves = _unwrap(words, True)
        program = program or []
        if moves and cd is not None:
            cd = (*cd, *moves)
        self.found.append(Simple(words=tuple(program), cd=cd))
        # A child process: its `cd`s change nothing after it.
        self._script(program, cd, depth)

    def _script(
        self, words: list[str], cd: tuple[str, ...] | None, depth: int
    ) -> tuple[str, ...] | None:
        """Walk the script a nested shell, `eval` or `trap` runs; return where the commands
        after it run."""
        script = _nested_script(words)
        # A child shell's own `cd` stays in the child. `eval` runs its script in this
        # shell, so a `cd` there moves what comes after it.
        same_shell = bool(words) and words[0].rsplit("/", 1)[-1] == "eval"
        after = cd
        if script is not None and (self._nested or same_shell):
            trees = _readable(script, depth)
            if trees is None:
                # Too deep or not parseable: judged as text.
                self.found.append(Simple(cd=cd, unparsed=script))
                if same_shell and _MOVES_TEXT.search(script):
                    after = None
            else:
                current = cd
                for tree in trees:
                    current = self.walk(tree, current, depth + 1)
                if same_shell:
                    after = current
        return after


def _simple_commands(
    trees: list[object], *, nested: bool = True
) -> tuple[tuple[Simple, ...], dict[int, tuple[str, ...] | None]]:
    """Every simple command in the parsed command, in the order the shell runs them, and
    the `cd`s in force at each command node of ``trees``.

    A tree shaped in a way the walker does not expect falls back to the flat list the
    destructive scan uses, with the directory marked unknown rather than guessed.
    """
    walker = _Walker(nested=nested)
    try:
        current: tuple[str, ...] | None = ()
        for tree in trees:
            current = walker.walk(tree, current, 0)
    except (AttributeError, TypeError, IndexError):
        nodes = _commands(list(trees))
        simples = tuple(
            Simple(words=tuple(_program(_words(node))), writes=_writes(node), cd=None)
            for node in nodes
        )
        # Without a `cd` anywhere every command runs where the shell is; with one, nobody
        # can say where each runs.
        moves = any(simple.words[:1] in {("cd",), ("pushd",), ("popd",)} for simple in simples)
        return simples, ({id(node): None for node in nodes} if moves else {})
    return tuple(walker.found), walker.where


def _moved(operation: Destructive, cd: tuple[str, ...] | None) -> Destructive:
    """``operation`` run after ``cd``: the `cd`s of a nested script come after its shell's."""
    if cd is None or operation.cd is None:
        return replace(operation, cd=None)
    return replace(operation, cd=(*cd, *operation.cd))


_SUSPICIOUS_TEXT = re.compile(
    r"\b(rm|rmdir|shred|srm|unlink|truncate|mkfs|dd)\b|>\s*/|--delete\b|git\s+clean\b"
)


def scan(command: str) -> Scan:
    """Find every destructive operation in a command."""
    return _scan(command, 0)


def _scan(command: str, depth: int) -> Scan:
    uses_sudo = bool(re.search(r"\bsudo\b", command))
    if depth > _MAX_NESTING:
        # Fail closed: a script nested this deep is not read, so it is not called clean.
        return Scan(
            operations=(
                Destructive("unparsed", (), "this command nests shells too deeply to be read",
                            True, True, command),
            ),
            unparsed=True,
            uses_sudo=uses_sudo,
        )
    try:
        trees = bashlex.parse(command)
    except (bashlex.errors.ParsingError, NotImplementedError, IndexError, AttributeError):
        # Unparseable is not clean. If it looks like it destroys something, say so and let
        # the user decide, so a quoting trick cannot slip through.
        if _SUSPICIOUS_TEXT.search(command):
            return Scan(
                operations=(
                    Destructive(
                        "unparsed",
                        (),
                        "this command could not be parsed and looks like it deletes something",
                        True,
                        True,
                        command,
                    ),
                ),
                unparsed=True,
                uses_sudo=uses_sudo,
            )
        return Scan(unparsed=True, uses_sudo=uses_sudo)

    # Only the outermost scan lists the commands: the walker reads nested scripts itself.
    commands, where = _simple_commands(list(trees), nested=depth == 0)
    operations: list[Destructive] = []
    modifications: list[Destructive] = []
    for node in _commands(list(trees)):
        found, touched = _inspect(node, command, depth)
        if (cd := where.get(id(node), ())) != ():
            found = [_moved(operation, cd) for operation in found]
            touched = [_moved(operation, cd) for operation in touched]
        operations.extend(found)
        modifications.extend(touched)
    return Scan(
        operations=tuple(operations),
        uses_sudo=uses_sudo,
        modifications=tuple(modifications),
        commands=commands if depth == 0 else (),
    )
