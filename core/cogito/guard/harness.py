"""Cogito's own record of a session: the agent may read it, and nothing it runs may destroy it.

`<project>/.cogito` (`config.paths.project_dir`) is where the harness keeps what it knows
about the work. None of it is the work:

- `wal/`: the journal every event is fsynced to before the graph sees it, the checkpoint
  of what reached the graph, and `events.seq`, the conversation's order;
- `undo/`: the copy taken before every overwrite, which `cogito undo` and a rewind
  restore from;
- `artifacts/`: tool output clipped out of the window, which a result points back to;
- `estella/`: Cogito Estella's graphs over the project's prose and the conversation;
- `trash/`: the fallback trash, when the home trash is on another device.

Losing it loses the session: the only transcript while the graph is down, every way back
from an edit, the values a fold promised to keep. So it is judged under rule 2, like
operating-system files, not rule 1: refused, never asked about. A person approving
`rm -rf .cogito` in the desktop app cannot be shown what it costs, and a benchmark's
`--allow-destructive` approves everything.

Seen in lab run 20261001T001854-qwen3-coder-30b-hashline-a2653e6 (impl-cpp-knapsack,
repeat 0): the model ran `rm -rf .cogito` after finishing, the guard and the benchmark let
it through as an ordinary project deletion, and the archive kept 7 of 125 events.

Reading stays open: `read`, `grep` and `glob` reach it, and a clipped result names a path
inside it. Refused is anything that deletes, moves, truncates, overwrites or
re-permissions it, or a directory holding it:

- `write` and `edit` aimed inside it, every symlink followed;
- in the shell: `rm` and its kin, `mv` from or into it, `cp`/`install`/`ln`/`rsync` into
  it, `tee`, `sed -i`/`perl -i`, `truncate`, `dd of=`, `chmod`/`chown`, every redirection
  (`>`, `>>`, `>|`, `&>`, `<>`), `find -delete` or `-exec rm` that would descend into it,
  `git clean -x`/`-X`, `git stash --all`, `rsync --delete` over it. This holds behind
  wrappers, inside `bash -c` and `eval`, after a `cd`, and through a glob that can expand
  to it;
- every argument the PATH shims receive, after the shell expanded it.

The text cannot be read perfectly (a variable, a script the model wrote), so two floors
sit beneath this: the shims see a deletion's real arguments, and a sandboxed run mounts
the directory read-only inside the shell (`guard/sandbox`).
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path

from cogito.config import paths
from cogito.guard.bashscan import Scan, Simple

#: The name the agent sees, in every refusal.
_NAME = f"{paths.PROJECT_DIRNAME}/"

#: Verbs whose operands are removed outright, with everything beneath them.
_REMOVERS = frozenset({"rm", "rmdir", "shred", "srm", "unlink", "wipe"})
#: Verbs that copy or link their sources to a destination, given last or by `-t`.
_COPIERS = frozenset({"cp", "install", "ln", "rsync"})
#: Verbs that change a file's permissions, ownership or attributes: `chmod 000` on the
#: journal stops the harness writing it as surely as deleting it does.
_PERMISSIONS = frozenset({"chmod", "chown", "chgrp", "chattr"})

#: Options, per verb, whose value is the next word rather than an operand.
_VALUED: dict[str, frozenset[str]] = {
    "truncate": frozenset({"-s", "--size", "-r", "--reference"}),
    "cp": frozenset({"-t", "-S", "--target-directory", "--suffix"}),
    "mv": frozenset({"-t", "-S", "--target-directory", "--suffix"}),
    "ln": frozenset({"-t", "-S", "--target-directory", "--suffix"}),
    "install": frozenset({"-t", "-S", "-m", "-o", "-g", "--target-directory", "--suffix",
                          "--mode", "--owner", "--group"}),
    "rsync": frozenset({"-e", "--rsh", "--exclude", "--include", "--filter", "-f",
                        "--exclude-from", "--include-from", "--files-from", "--rsync-path",
                        "--chmod", "--chown", "--log-file", "--backup-dir", "-T",
                        "--temp-dir", "--partial-dir"}),
    "sed": frozenset({"-e", "-f", "-l", "--expression", "--file", "--line-length"}),
    "perl": frozenset({"-e", "-E", "-M", "-m", "-I", "-x"}),
    "tee": frozenset(),
}

#: Short options of `sed` and `perl` that take the rest of their word, or the next one.
_VALUE_LETTERS = {"sed": frozenset("efl"), "perl": frozenset("eEMmIx0lCdD")}

#: find tests that name what they match, and whether they compare the whole path.
_FIND_NAME_TESTS = {
    "-name": False, "-iname": False,
    "-path": True, "-ipath": True, "-wholename": True, "-iwholename": True,
}
#: find actions whose next words are a command, ended by `;` or `+`.
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: find actions that write the file named by their next word.
_FIND_WRITES = frozenset({"-fprint", "-fprint0", "-fls", "-fprintf"})
#: Past this many entries the record is not walked, and a find is judged as reaching it.
_MAX_ENTRIES = 20_000

_TOUCHY = re.compile(
    r"\b(rm|rmdir|shred|srm|unlink|wipe|truncate|mv|cp|install|ln|rsync|dd|tee|find|git"
    r"|sed|perl|chmod|chown|chgrp)\b|>"
)


def _refusal(action: str, advice: str = "") -> str:
    """What the model is told: what was refused, what the directory is, what to do."""
    return (
        f"refused: {action}. The directory {_NAME} is Cogito's own record of this session "
        f"— its journal, undo snapshots and offloaded tool output — and not part of your "
        f"task. You may read it, but nothing may delete, move or overwrite it, and no "
        f"approval changes that. {advice or 'Leave it as it is and carry on with your own files.'}"
    )


def _expand(raw: str) -> str | None:
    """A shell word as path text, or None when the shell still has to fill it in."""
    if not raw or "$" in raw or "`" in raw:
        return None
    return os.path.expanduser(raw)


def _globbed(text: str) -> bool:
    return any(marker in text for marker in "*?[")


def _located(text: str, cwd: Path, *, follow_last: bool) -> Path:
    """Where ``text`` lands from ``cwd``. A removal does not follow a final symlink (`rm
    link` removes the link); a write follows every one."""
    candidate = Path(text) if os.path.isabs(text) else cwd / text
    if follow_last or candidate.name in {"", ".", ".."}:
        return Path(os.path.realpath(candidate))
    return Path(os.path.realpath(candidate.parent)) / candidate.name


def _component_matches(pattern: str, name: str) -> bool:
    """One path component against one glob component, as bash expands it: `*` and `?`
    never match a leading dot unless the pattern spells it."""
    if not _globbed(pattern):
        return pattern == name
    if name.startswith(".") and not pattern.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, pattern)


def _operands(words: Sequence[str], valued: frozenset[str] = frozenset()) -> list[str]:
    """The words that are not options, skipping the value an option takes."""
    out: list[str] = []
    skip = False
    after_separator = False
    for word in words:
        if skip:
            skip = False
            continue
        if after_separator:
            out.append(word)
        elif word == "--":
            after_separator = True
        elif word.startswith("-") and len(word) > 1:
            skip = word in valued
        else:
            out.append(word)
    return out


def _option_value(words: Sequence[str], short: str, long: str) -> str | None:
    """The value of `-t DIR`, `--target-directory DIR` or `--target-directory=DIR`."""
    for index, word in enumerate(words):
        if word in {short, long} and index + 1 < len(words):
            return words[index + 1]
        if word.startswith(long + "="):
            return word.split("=", 1)[1]
    return None


def _excludes_record(patterns: Iterable[str]) -> bool:
    """Whether an exclude pattern given to git or rsync names the record."""
    name = paths.PROJECT_DIRNAME
    return any(fnmatch.fnmatchcase(name, pattern.strip("/")) for pattern in patterns if pattern)


def _conjunction(expression: Sequence[str]) -> list[tuple[bool, str, str]] | None:
    """The name and path tests a find expression ANDs together at its top level, each with
    whether it is negated; None when the top level is not a plain conjunction.

    A parenthesised group and an `-exec ... ;` block are opaque terms: they can only narrow
    a conjunction, so skipping them never makes the answer wrong.
    """
    terms: list[tuple[bool, str, str]] = []
    negated = False
    position = 0
    while position < len(expression):
        word = expression[position]
        if word in {"-o", "-or", ","}:
            return None
        if word in _FIND_EXEC:
            position += 1
            while position < len(expression) and expression[position] not in {";", "+"}:
                position += 1
            position += 1
            negated = False
        elif word == "(":
            level = 1
            position += 1
            while position < len(expression) and level:
                level += {"(": 1, ")": -1}.get(expression[position], 0)
                position += 1
            negated = False
        elif word in {"!", "-not"}:
            negated = not negated
            position += 1
        elif word in _FIND_NAME_TESTS and position + 1 < len(expression):
            terms.append((negated, word, expression[position + 1]))
            negated = False
            position += 2
        elif word in {"-a", "-and"}:
            position += 1
        else:
            negated = False
            position += 1
    return terms


def _work_tree(start: Path) -> Path | None:
    """The git work tree ``start`` is in, found the way git finds it: the nearest `.git`."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


class HarnessDirectory:
    """Judges whether an action would destroy the project's `.cogito`, and says so."""

    def __init__(self, project_root: Path) -> None:
        self._root = Path(os.path.realpath(paths.project_dir(project_root)))

    @property
    def root(self) -> Path:
        return self._root

    def holds(self, path: Path) -> bool:
        """``path`` (already resolved) is the record or inside it."""
        return path == self._root or path.is_relative_to(self._root)

    def reaches(self, path: Path) -> bool:
        """Removing ``path`` (already resolved) removes the record or part of it."""
        return self.holds(path) or self._root.is_relative_to(path)

    # -- the file tools ---------------------------------------------------------------

    def judge_file_tool(self, tool: str, raw: str, resolved: Path) -> str | None:
        """A refusal when `write` or `edit` would change something inside the record."""
        if self.holds(resolved) or self._hard_linked(resolved):
            return _refusal(f"{tool} {raw!r} would change a file inside {_NAME}")
        return None

    # -- the shims ----------------------------------------------------------------------

    def judge_shim(self, verb: str, cwd: Path, arguments: Sequence[str]) -> str | None:
        """A refusal when a shimmed verb's arguments, already expanded by the shell, reach
        the record. The words are literal here: a `*` in one is part of a file name."""
        for raw in arguments:
            if self.reaches(_located(raw, cwd, follow_last=False)):
                return self._removal_refusal(raw, cwd)
            if verb == "truncate":
                written = _located(raw, cwd, follow_last=True)
                if self.holds(written) or self._hard_linked(written):
                    return _refusal(f"truncating {raw!r} would empty a file inside {_NAME}")
        return None

    # -- the shell ----------------------------------------------------------------------

    def judge_command(self, found: Scan, cwd: Path, command: str) -> str | None:
        """A refusal when anything in ``command``, run from ``cwd``, would destroy the
        record; None when nothing in it can be seen to."""
        if found.unparsed:
            return self._judge_unparsed(command)
        project_root = self._root.parent
        for simple in found.commands:
            if simple.unparsed:
                if (reason := self._judge_unparsed(simple.unparsed)) is not None:
                    return reason
                continue
            for here in self._directories(simple.cd, cwd, project_root):
                if (reason := self._judge_simple(simple, here)) is not None:
                    return reason
        return None

    def _directories(
        self, cd: tuple[str, ...] | None, cwd: Path, project_root: Path
    ) -> tuple[Path, ...]:
        """Where a command runs. When a `cd` cannot be read, a relative word is judged
        from both places it most likely means."""
        unknown = tuple(dict.fromkeys((cwd, project_root)))
        if cd is None:
            return unknown
        current = cwd
        for word in cd:
            text = _expand(word)
            if text is None or _globbed(text):
                return unknown
            current = Path(os.path.realpath(text if os.path.isabs(text) else current / text))
        return (current,)

    def _judge_unparsed(self, text: str) -> str | None:
        if paths.PROJECT_DIRNAME in text and _TOUCHY.search(text):
            return _refusal(
                f"this command names {_NAME} and could not be parsed, so Cogito cannot tell "
                f"whether it deletes or overwrites something there",
                f"Rewrite it as a plain command that leaves {_NAME} alone.",
            )
        return None

    def _judge_simple(self, simple: Simple, here: Path) -> str | None:
        for target in simple.writes:
            if self._writes(target, here):
                return _refusal(f"redirecting output to {target!r} would write inside {_NAME}")
        if not simple.words:
            return None
        verb = simple.words[0].rsplit("/", 1)[-1]
        rest = list(simple.words[1:])

        if verb in _REMOVERS:
            for target in _operands(rest):
                if self._removes(target, here):
                    return self._removal_refusal(target, here)
        elif verb == "truncate":
            for target in _operands(rest, _VALUED["truncate"]):
                if self._writes(target, here):
                    return _refusal(f"truncating {target!r} would empty a file inside {_NAME}")
        elif verb == "mv":
            sources, destination = self._split(verb, rest)
            for source in sources:
                if self._removes(source, here):
                    return _refusal(
                        f"moving {source!r} would move {_NAME} or part of it away",
                        "Move the specific files you mean instead.",
                    )
            if destination is not None and self._into(destination, sources, here):
                return _refusal(f"moving onto {destination!r} would overwrite part of {_NAME}")
        elif verb in _COPIERS:
            sources, destination = self._split(verb, rest)
            if destination is not None and self._into(destination, sources, here):
                return _refusal(f"{verb} onto {destination!r} would overwrite part of {_NAME}")
            if verb == "rsync" and "--remove-source-files" in rest:
                for source in sources:
                    if self._removes(source, here):
                        return _refusal(
                            f"rsync --remove-source-files from {source!r} would delete "
                            f"files inside {_NAME}"
                        )
            if verb == "rsync" and destination is not None:
                return self._judge_rsync_delete(rest, destination, here)
        elif verb == "tee":
            for target in _operands(rest):
                if self._writes(target, here):
                    return _refusal(f"tee {target!r} would write inside {_NAME}")
        elif verb in {"sed", "perl"} and self._in_place(verb, rest):
            for target in _operands(rest, _VALUED[verb]):
                if self._writes(target, here):
                    return _refusal(f"{verb} -i on {target!r} would rewrite a file inside {_NAME}")
        elif verb in _PERMISSIONS:
            # A mode can look like an option (`chmod -w`), so every word is tried: a mode
            # or an owner never resolves into the record, and a file there always does.
            for target in (word for word in rest if not word.startswith("--")):
                if self._writes(target, here):
                    return _refusal(
                        f"{verb} on {target!r} would change permissions inside {_NAME}"
                    )
        elif verb == "dd":
            for word in rest:
                if word.startswith("of=") and self._writes(word[3:], here):
                    return _refusal(f"dd of={word[3:]!r} would write over a file inside {_NAME}")
        elif verb == "find":
            return self._judge_find(rest, here)
        elif verb == "git":
            return self._judge_git(rest, here)
        return None

    # -- what a word lands on -------------------------------------------------------------

    def _removes(self, raw: str, cwd: Path) -> bool:
        text = _expand(raw)
        if text is None:
            return False
        if _globbed(text):
            return self._pattern_reaches(text, cwd, removal=True)
        return self.reaches(_located(text, cwd, follow_last=False))

    def _writes(self, raw: str, cwd: Path) -> bool:
        text = _expand(raw)
        if text is None:
            return False
        if _globbed(text):
            return self._pattern_reaches(text, cwd, removal=False)
        path = _located(text, cwd, follow_last=True)
        return self.holds(path) or self._hard_linked(path)

    def _into(self, raw: str, sources: Sequence[str], cwd: Path) -> bool:
        """Whether copying or moving ``sources`` to ``raw`` writes inside the record:
        onto it, into it, or, when ``raw`` is a directory, as an entry named like it."""
        if self._writes(raw, cwd):
            return True
        text = _expand(raw)
        if text is None or _globbed(text):
            return False
        destination = _located(text, cwd, follow_last=True)
        if not destination.is_dir():
            return False
        for source in sources:
            source_text = _expand(source)
            if source_text is None:
                continue
            name = Path(source_text).name
            if not name:
                continue
            if _globbed(name):
                if destination == self._root.parent and _component_matches(
                    name, self._root.name
                ):
                    return True
            elif self.holds(destination / name):
                return True
        return False

    def _pattern_reaches(self, text: str, cwd: Path, *, removal: bool) -> bool:
        """Whether a glob can expand to the record or something inside it, or (for a
        removal) to a directory that holds it.

        The literal head is resolved like any path; the rest is compared component by
        component with the record's own path. A pattern as long as the record's path or
        longer lands on it or inside it; a shorter one lands on a directory above it.
        """
        absolute = text if os.path.isabs(text) else f"{cwd}/{text}"
        components = absolute.split("/")
        cut = next(
            (index for index, part in enumerate(components) if _globbed(part)),
            len(components),
        )
        head = Path(os.path.realpath("/".join(components[:cut]) or "/"))
        pattern = [*head.parts, *(part for part in components[cut:] if part not in {"", "."})]
        record = self._root.parts
        overlap = min(len(pattern), len(record))
        if not all(_component_matches(pattern[i], record[i]) for i in range(overlap)):
            return False
        return len(pattern) >= len(record) or removal

    def _hard_linked(self, path: Path) -> bool:
        """Whether ``path`` is another name for a file inside the record: writing through
        a hard link changes that file too."""
        try:
            status = path.stat()
        except OSError:
            return False
        if not stat.S_ISREG(status.st_mode) or status.st_nlink < 2:
            return False
        identity = (status.st_dev, status.st_ino)
        for directory, _, files in os.walk(self._root):
            for name in files:
                try:
                    candidate = os.lstat(os.path.join(directory, name))
                except OSError:
                    continue
                if (candidate.st_dev, candidate.st_ino) == identity:
                    return True
        return False

    def _removal_refusal(self, raw: str, cwd: Path) -> str:
        text = _expand(raw)
        if text is not None and not _globbed(text):
            located = _located(text, cwd, follow_last=False)
            if located != self._root and self._root.is_relative_to(located):
                return _refusal(
                    f"deleting {raw!r} would delete {_NAME} along with it",
                    "Delete the specific files or directories you mean instead "
                    "(for example `rm -rf build dist`).",
                )
        return _refusal(f"deleting {raw!r} would delete {_NAME} or part of it")

    # -- verbs with their own grammar --------------------------------------------------

    @staticmethod
    def _split(verb: str, words: Sequence[str]) -> tuple[list[str], str | None]:
        """Sources and destination of `mv`, `cp`, `install`, `ln` and `rsync`."""
        operands = _operands(words, _VALUED.get(verb, frozenset()))
        target = _option_value(words, "-t", "--target-directory")
        if target is not None:
            return operands, target
        if len(operands) >= 2:
            return operands[:-1], operands[-1]
        if verb == "ln" and len(operands) == 1:
            # `ln -s TARGET` creates the link in the working directory.
            return operands, "."
        return operands, None

    @staticmethod
    def _in_place(verb: str, words: Sequence[str]) -> bool:
        """`sed -i`, `sed -ni.bak`, `perl -pi -e`: an `i` among the short options, before
        any option that takes the rest of the word as its value."""
        for word in words:
            if word == "--":
                break
            if word.startswith("--in-place"):
                return True
            if word.startswith("-") and not word.startswith("--"):
                for letter in word[1:]:
                    if letter == "i":
                        return True
                    if letter in _VALUE_LETTERS[verb]:
                        break
        return False

    def _judge_rsync_delete(self, words: Sequence[str], destination: str, here: Path) -> str | None:
        if not any(word.startswith("--delete") for word in words):
            return None
        if "--delete-excluded" not in words:
            excludes = [word.split("=", 1)[1] for word in words if word.startswith("--exclude=")]
            excludes += [
                words[index + 1]
                for index, word in enumerate(words[:-1])
                if word == "--exclude"
            ]
            if _excludes_record(excludes):
                return None
        if self._removes(destination, here):
            return _refusal(
                f"rsync --delete into {destination!r} would delete {_NAME}, which the "
                f"source does not have",
                f"Add `--exclude={paths.PROJECT_DIRNAME}`, or sync into the directory you mean.",
            )
        return None

    def _judge_find(self, words: Sequence[str], here: Path) -> str | None:
        index = 0
        while index < len(words) and (
            words[index] in {"-H", "-L", "-P", "-D"} or words[index].startswith("-O")
        ):
            index += 2 if words[index] == "-D" else 1
        starts: list[str] = []
        while index < len(words) and not (
            words[index].startswith("-") or words[index] in {"(", ")", "!", ","}
        ):
            starts.append(words[index])
            index += 1
        expression = list(words[index:])

        for position, word in enumerate(expression[:-1]):
            if word in _FIND_WRITES and self._writes(expression[position + 1], here):
                return _refusal(f"find {word} {expression[position + 1]!r} would write "
                                f"inside {_NAME}")

        deleting = "-delete" in expression or any(
            word in _FIND_EXEC
            and position + 1 < len(expression)
            and expression[position + 1].rsplit("/", 1)[-1] in _REMOVERS | {"mv"}
            for position, word in enumerate(expression)
        )
        if not deleting:
            return None
        advice = (
            f"Add `-not -path '*/{paths.PROJECT_DIRNAME}*'` to the expression, or start it "
            f"from the directories you mean."
        )
        for start in starts or ["."]:
            text = _expand(start)
            if text is None:
                continue
            if _globbed(text):
                if self._pattern_reaches(text, here, removal=True):
                    return _refusal(f"this find deletes what it matches under {start!r}, "
                                    f"which includes {_NAME}", advice)
                continue
            path = _located(text, here, follow_last=True)
            if self.holds(path):
                return _refusal(f"this find deletes what it matches inside {_NAME}")
            if self._root.is_relative_to(path) and not self._find_spares(expression, text, path):
                return _refusal(
                    f"this find deletes what it matches under {start!r} and would descend "
                    f"into {_NAME}",
                    advice,
                )
        return None

    def _find_spares(self, expression: Sequence[str], start_text: str, start: Path) -> bool:
        """Whether this find's expression provably matches nothing at or under the record.

        Only a plain conjunction is analysed: one positive name or path test that no entry
        of the record satisfies, or one negated test that every entry satisfies, makes the
        whole expression false there. Anything with `-o` or `,` is not analysed. `-prune`
        is not counted: `-delete` turns on `-depth`, which makes `-prune` do nothing (GNU
        findutils manual, 3.4).
        """
        depth = len(self._root.relative_to(start).parts)
        for position, word in enumerate(expression[:-1]):
            following = expression[position + 1]
            if word == "-maxdepth" and following.isdigit() and int(following) < depth:
                return True
        terms = _conjunction(expression)
        if terms is None:
            return False
        entries = self._entries()
        if entries is None:
            return False
        prefix = start_text.rstrip("/")
        relative = self._root.relative_to(start)
        spelled = [f"{prefix}/{relative / entry}" if entry else f"{prefix}/{relative}"
                   for entry in entries]
        names = [Path(entry).name if entry else self._root.name for entry in entries]
        for negated, test, pattern in terms:
            folded = test.startswith("-i")
            candidates = spelled if _FIND_NAME_TESTS[test] else names
            hits = [
                fnmatch.fnmatchcase(
                    value.lower() if folded else value, pattern.lower() if folded else pattern
                )
                for value in candidates
            ]
            if (negated and all(hits)) or (not negated and not any(hits)):
                return True
        return False

    def _entries(self) -> list[str] | None:
        """The record and everything in it, relative to it (`""` is the record itself);
        None when there are too many to look at."""
        found = [""]
        for directory, subdirectories, files in os.walk(self._root):
            base = Path(directory).relative_to(self._root)
            for name in (*subdirectories, *files):
                found.append(str(base / name) if str(base) != "." else name)
                if len(found) > _MAX_ENTRIES:
                    return None
        return found

    def _judge_git(self, words: Sequence[str], here: Path) -> str | None:
        index = 0
        base = here
        while index < len(words) and words[index].startswith("-"):
            word = words[index]
            if word == "-C" and index + 1 < len(words):
                text = _expand(words[index + 1])
                if text is None:
                    return None
                base = _located(text, base, follow_last=True)
                index += 2
                continue
            if word in {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}:
                index += 2
                continue
            index += 1
        if index >= len(words):
            return None
        subcommand, arguments = words[index], list(words[index + 1 :])
        if subcommand == "clean":
            return self._judge_git_clean(arguments, base)
        if subcommand == "stash":
            return self._judge_git_stash(arguments, base)
        if subcommand == "rm" and "--cached" not in arguments:
            for target in _operands(arguments):
                text = _expand(target)
                if (
                    text is not None
                    and not _globbed(text)
                    and self.holds(_located(text, base, follow_last=False))
                ):
                    return self._removal_refusal(target, base)
        return None

    def _ignores_itself(self) -> bool:
        """Whether the record's own `.gitignore` tells git to ignore all of it."""
        try:
            lines = (self._root / ".gitignore").read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        return any(line.strip() == "*" for line in lines)

    def _judge_git_clean(self, arguments: Sequence[str], base: Path) -> str | None:
        flags: set[str] = set()
        excludes: list[str] = []
        pathspecs: list[str] = []
        index = 0
        after_separator = False
        while index < len(arguments):
            word = arguments[index]
            index += 1
            if after_separator or not word.startswith("-") or word == "-":
                pathspecs.append(word)
            elif word == "--":
                after_separator = True
            elif word in {"-e", "--exclude"}:
                if index < len(arguments):
                    excludes.append(arguments[index])
                    index += 1
            elif word.startswith("--exclude="):
                excludes.append(word.split("=", 1)[1])
            elif word == "--dry-run":
                flags.add("n")
            elif word.startswith("--"):
                continue
            else:
                cluster = word[1:]
                for position, letter in enumerate(cluster):
                    if letter == "e":
                        value = cluster[position + 1 :]
                        if value:
                            excludes.append(value)
                        elif index < len(arguments):
                            excludes.append(arguments[index])
                            index += 1
                        break
                    flags.add(letter)

        if "n" in flags:
            return None  # a dry run removes nothing
        every_ignored, only_ignored = "x" in flags, "X" in flags
        excluded = _excludes_record(excludes)
        # Without a pathspec git clean does not enter an untracked directory unless -d is
        # given; with one, -d is irrelevant (git-clean(1)).
        enters_directories = "d" in flags or bool(pathspecs)
        protected = self._ignores_itself()

        scopes: list[Path] = []
        for spec in pathspecs or ["."]:
            text = _expand(spec)
            if text is None or _globbed(text):
                scopes.append(base)  # a pathspec glob matches at any depth below here
            else:
                scopes.append(_located(text, base, follow_last=False))

        for scope in scopes:
            if not self.reaches(scope):
                continue
            if not self.holds(scope) and not enters_directories:
                continue
            if only_ignored:
                return _refusal(
                    f"`git clean -X` here deletes ignored files, and {_NAME} is one",
                    "Use `git clean -fd`, or name the paths you want cleaned.",
                )
            if every_ignored and not excluded:
                return _refusal(
                    f"`git clean -x` here would delete {_NAME}",
                    f"Add `-e {paths.PROJECT_DIRNAME}` (for example `git clean -fdx -e "
                    f"{paths.PROJECT_DIRNAME}`), or drop -x: {_NAME} ignores itself, so "
                    f"`git clean -fd` leaves it alone.",
                )
            if not every_ignored and not protected and not excluded:
                return _refusal(
                    f"`git clean` here would delete {_NAME}",
                    f"Add `-e {paths.PROJECT_DIRNAME}`.",
                )
        return None

    def _judge_git_stash(self, arguments: Sequence[str], base: Path) -> str | None:
        if arguments and not arguments[0].startswith("-") and arguments[0] not in {"push", "save"}:
            return None
        everything = any(
            word == "--all" or (word.startswith("-") and not word.startswith("--") and "a" in word)
            for word in arguments
        )
        if not everything:
            return None
        tree = _work_tree(base)
        if tree is None or not self._root.is_relative_to(tree):
            return None
        return _refusal(
            f"`git stash --all` would stash {_NAME} away and delete it from the working tree",
            f"Use `git stash -u`, which leaves ignored files such as {_NAME} in place.",
        )
