"""Per-turn file changes: aggregation and diffing.

A turn can call `edit` and `write` several times on the same path, and the desktop app's
"Resumen de cambios" screen wants one diff per path for the whole turn. `PathChange` holds
the first "before" and the last "after" seen this turn (kept by ``loop.engine``), and this
module turns it into a unified diff.

Nothing here, or in what it returns, is appended to the conversation. The engine keeps
`PathChange` off the transcript, and the server is the only reader of the diffs.

Every size below is a hard cap, checked before any `SequenceMatcher` pass. `difflib`'s cost
is not bounded by file size: a file of mostly repeated lines defeats the matcher's
short-circuits, and a running match cannot be interrupted. Before these caps, a
32,000-line file of near-identical lines took ~19s to diff (phase B wave 1 review, in
`git log`).
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: What a key outside the project renders as, plus its basename. Never the real absolute
#: path, which would leak the host's directory layout. `loop.engine` also uses it alone
#: for `tool_start`/`tool_end`'s `target`, where nothing is aggregated, so there is no
#: collision to avoid.
OUTSIDE_PROJECT = "<outside project>"

#: 3 lines of context, clipped to 200 lines of diff text, per file.
DIFF_CONTEXT_LINES = 3
MAX_DIFF_LINES = 200

#: Skip diffing when either side is over this size, in characters or in lines. Generous
#: for a source file, small for a build artifact or data dump, which a "what changed"
#: screen cannot show usefully anyway.
MAX_DIFF_BYTES = 1_000_000
MAX_DIFF_LINES_EITHER_SIDE = 20_000

#: A longer line is clipped, so a minified bundle or JSON blob cannot dominate the payload
#: even when the file is under the caps above.
MAX_LINE_CHARS = 400

#: Once one turn's diff bytes cross this, further files are reported without a diff
#: (`truncated=True`, empty `diff`).
MAX_EVENT_BYTES = 256_000


@dataclass(frozen=True, slots=True)
class PathChange:
    """Raw before/after text for one path, aggregated over a turn.

    ``None`` means the path did not exist at that end: ``before=None`` is a create,
    ``after=None`` a delete. `edit` and `write` only produce creates and modifies. A
    delete arrives when the engine finds, at the end of a turn, that a tracked path is
    gone (see `loop.engine._reconcile_with_disk`). The renderer treats all three alike.
    """

    before: str | None
    after: str | None


def _looks_binary(text: str) -> bool:
    """A NUL byte, or the replacement character `read_text(..., errors="replace")` leaves
    for invalid UTF-8. Either means the content is not text, and a line diff would mislead.
    """
    return "\x00" in text or "�" in text


def _too_large(before: str | None, after: str | None) -> bool:
    before_str = before or ""
    after_str = after or ""
    if len(before_str) > MAX_DIFF_BYTES or len(after_str) > MAX_DIFF_BYTES:
        return True
    return (
        before_str.count("\n") > MAX_DIFF_LINES_EITHER_SIDE
        or after_str.count("\n") > MAX_DIFF_LINES_EITHER_SIDE
    )


def _counts_from_diff(diff_lines: Sequence[str]) -> tuple[int, int]:
    """Added/removed counted from a unified diff's own lines.

    Never a second comparison over the same texts: two algorithms computing the same
    number can disagree. A separate `SequenceMatcher(autojunk=False)` here was the source
    of the 19s worst case above, because disabling autojunk removes the short-circuit that
    makes a file of repeated lines fast to compare.
    """
    added = removed = 0
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def line_counts(before: str | None, after: str | None) -> tuple[int | None, int | None]:
    """(added, removed) line counts between two texts, or (None, None) above the size cap.

    Counted by `_counts_from_diff` from one `difflib.unified_diff` pass, the same call
    `build_file_diff` makes, never a separate comparison.
    """
    if _too_large(before, after):
        return None, None
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    diff_lines = list(difflib.unified_diff(before_lines, after_lines, n=0))
    return _counts_from_diff(diff_lines)


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    status: str  # modified | added | deleted
    added: int | None = None
    removed: int | None = None
    diff: str = ""
    truncated: bool = False
    #: Set, with `diff` empty, when either side was over the size cap above.
    too_large: bool = False
    before_bytes: int | None = None
    after_bytes: int | None = None
    #: Set, with `diff` empty, when either side looked like it was not text.
    binary: bool = False


def _status(change: PathChange) -> str:
    if change.before is None:
        return "added"
    if change.after is None:
        return "deleted"
    return "modified"


def _clip_line(line: str) -> str:
    """`MAX_LINE_CHARS` of a diff line, keeping its trailing newline so a clipped line
    never merges with the next when the lines are joined.
    """
    has_newline = line.endswith("\n")
    body = line[:-1] if has_newline else line
    if len(body) > MAX_LINE_CHARS:
        body = body[:MAX_LINE_CHARS] + "…"
    return body + "\n" if has_newline else body


def _mark_missing_newlines(diff_lines: list[str]) -> list[str]:
    """Insert the `diff`/`patch` "\\ No newline at end of file" marker after a content
    line from text with no trailing newline, and add the newline that line lacks.

    `difflib.unified_diff` leaves that line without a trailing "\\n", so joining the
    diff glues it onto the next line (`"-b"` then `"+B"` renders as `"-b+B"`).
    """
    marked: list[str] = []
    for line in diff_lines:
        if line.endswith("\n"):
            marked.append(line)
            continue
        marked.append(line + "\n")
        if line[:1] in (" ", "+", "-"):
            marked.append("\\ No newline at end of file\n")
    return marked


def build_file_diff(path: str, change: PathChange) -> FileDiff:
    """One path's unified diff, clipped to `MAX_DIFF_LINES` lines of diff text.

    Binary content and an oversize side are both checked before any `SequenceMatcher`
    pass, and both skip diffing instead of clipping a meaningless result.
    """
    status = _status(change)
    before_str = change.before or ""
    after_str = change.after or ""

    if _looks_binary(before_str) or _looks_binary(after_str):
        return FileDiff(path=path, status=status, binary=True)

    if _too_large(change.before, change.after):
        return FileDiff(
            path=path,
            status=status,
            too_large=True,
            before_bytes=(
                len(before_str.encode("utf-8", "surrogatepass"))
                if change.before is not None
                else None
            ),
            after_bytes=(
                len(after_str.encode("utf-8", "surrogatepass"))
                if change.after is not None
                else None
            ),
        )

    before_lines = before_str.splitlines(keepends=True)
    after_lines = after_str.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            before_lines, after_lines, fromfile=path, tofile=path, n=DIFF_CONTEXT_LINES
        )
    )
    added, removed = _counts_from_diff(diff_lines)
    truncated = len(diff_lines) > MAX_DIFF_LINES
    visible = _mark_missing_newlines(diff_lines[:MAX_DIFF_LINES])
    diff_text = "".join(_clip_line(line) for line in visible)
    return FileDiff(
        path=path,
        status=status,
        added=added,
        removed=removed,
        diff=diff_text,
        truncated=truncated,
    )


def whole_diff(path: str, change: PathChange) -> str | None:
    """The unified diff `build_file_diff` renders, without its `MAX_DIFF_LINES` clip, for
    one call's detail (`loop.detail` previews it and stashes the rest). None when either
    side is binary or over the size cap, where nothing is diffed. Each line is still
    clipped to `MAX_LINE_CHARS`."""
    before_str = change.before or ""
    after_str = change.after or ""
    if _looks_binary(before_str) or _looks_binary(after_str):
        return None
    if _too_large(change.before, change.after):
        return None
    diff_lines = difflib.unified_diff(
        before_str.splitlines(keepends=True),
        after_str.splitlines(keepends=True),
        fromfile=path,
        tofile=path,
        n=DIFF_CONTEXT_LINES,
    )
    return "".join(_clip_line(line) for line in _mark_missing_newlines(list(diff_lines)))


@dataclass(frozen=True, slots=True)
class TurnChanges:
    files: tuple[FileDiff, ...]
    #: A shell call ran this turn. Files it touched (sed -i, git checkout, ...) are not in
    #: `files`: the harness has no before/after for them in v1.
    shell_ran: bool

    @property
    def total_files(self) -> int:
        return len(self.files)


def _display_path(key: str) -> str:
    """The path one aggregate entry shows in an event.

    `key` is `loop.engine`'s aggregation key: a project-relative path, or for a file
    outside the project its real absolute path, so two outside files never share a key
    (see `loop.engine._aggregation_key`). The display string is made here, per file, so
    two outside files with the same basename still become two `FileDiff` entries in the
    returned list even though their display strings match.
    """
    path = Path(key)
    if path.is_absolute():
        return f"{OUTSIDE_PROJECT}/{path.name}"
    return key


def build_turn_changes(
    aggregates: Mapping[str, PathChange], *, shell_ran: bool = False
) -> TurnChanges:
    """One diff per path, sorted so the order does not depend on call order, and capped at
    `MAX_EVENT_BYTES` of diff text in total. Past the cap, remaining files are reported
    (`truncated=True`) without being diffed, so the cap bounds the work as well as the
    payload.
    """
    used_bytes = 0
    files: list[FileDiff] = []
    for key, change in sorted(aggregates.items()):
        display = _display_path(key)
        if used_bytes >= MAX_EVENT_BYTES:
            files.append(FileDiff(path=display, status=_status(change), truncated=True))
            continue
        diff = build_file_diff(display, change)
        used_bytes += len(diff.diff.encode("utf-8", "surrogatepass"))
        files.append(diff)
    return TurnChanges(files=tuple(files), shell_ran=shell_ran)
