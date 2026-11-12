"""Rescuing an edit that nearly matched.

"String to replace not found" is the bug. Roughly one in five `str_replace` attempts fails
on the first try, nearly always over something the model could not see: a trailing space,
a CRLF, indentation off by two columns, a character that normalises differently. A plain
rejection teaches nothing and costs a round trip, which is where retry loops and wasted
output tokens come from.

So each of those is probed for, and a unique near-match is applied instead of refused.
Indentation matters most: the replacement is re-indented to the file's real indentation,
because matching loosely and then inserting the model's own indentation would silently
produce broken code.

A block that is only similar is never edited on the model's behalf. The real text is
returned instead, so the next attempt is made against what is actually there.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from cogito.tools.edit.formats.base import Failure, Resolved, ResolvedOp
from cogito.tools.edit.formats.str_replace import occurrences

#: Below this, two blocks are different code rather than the same code typed imperfectly.
SIMILARITY_FLOOR = 0.85

#: How much of the real file to hand back when a match could not be made safely.
CONTEXT_LINES = 40

_INDENT = re.compile(r"^[ \t]*")


@dataclass(frozen=True, slots=True)
class Repair:
    resolved: Resolved
    #: The probe that rescued it, recorded on the tool call as telemetry.
    name: str
    note: str = ""


def _trailing(line: str) -> str:
    return line.rstrip()


def _endings(line: str) -> str:
    return line.replace("\r", "")


def _indent_free(line: str) -> str:
    return line.strip()


def _unicode(line: str) -> str:
    return unicodedata.normalize("NFKC", line)


#: Ordered from least to most forgiving. The first unique match wins, so a whole file is
#: never re-indented by a probe that should not have been reached.
PROBES: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("trailing_ws", _trailing),
    ("line_endings", _endings),
    ("unicode", _unicode),
    ("reindented", _indent_free),
)


def _indent_of(line: str) -> str:
    return _INDENT.match(line).group(0)  # type: ignore[union-attr]


def _reindent(replacement: Sequence[str], from_indent: str, to_indent: str) -> tuple[str, ...]:
    """Shift a replacement block from the indentation the model used to the file's own.

    Relative indentation inside the block is preserved; only the common prefix moves.
    """
    if from_indent == to_indent:
        return tuple(replacement)
    out: list[str] = []
    for line in replacement:
        if not line.strip():
            out.append(line)
            continue
        current = _indent_of(line)
        if current.startswith(from_indent):
            out.append(to_indent + current[len(from_indent) :] + line.lstrip())
        else:
            out.append(to_indent + line.lstrip())
    return tuple(out)


def rescue(
    needle: Sequence[str],
    replacement: Sequence[str],
    lines: Sequence[str],
) -> Repair | Failure:
    """Try to place an edit that did not match exactly. Never guesses."""
    for name, normalise in PROBES:
        wanted = [normalise(line) for line in needle]
        haystack = [normalise(line) for line in lines]
        found = occurrences(haystack, wanted)
        if len(found) != 1:
            continue

        start = found[0]
        end = start + len(needle)
        new_lines = tuple(replacement)

        if name == "reindented":
            file_indent = _indent_of(lines[start])
            model_indent = _indent_of(needle[0]) if needle else ""
            new_lines = _reindent(replacement, model_indent, file_indent)

        note = {
            "trailing_ws": "matched ignoring trailing whitespace",
            "line_endings": "matched ignoring line endings",
            "unicode": "matched after unicode normalisation",
            "reindented": (
                f"matched ignoring indentation; the replacement was re-indented to "
                f"{len(_indent_of(lines[start]))} column(s) to match the file"
            ),
        }[name]
        return Repair(
            resolved=Resolved(ops=(ResolvedOp("replace", start, end, new_lines),)),
            name=name,
            note=note,
        )

    return _explain(needle, lines)


def _explain(needle: Sequence[str], lines: Sequence[str]) -> Failure:
    """No safe match. Hand back what the file actually says, at the likeliest place."""
    best_index, best_ratio = _closest(needle, lines)
    if best_index is None or best_ratio < SIMILARITY_FLOOR:
        return Failure(
            "not_found",
            "that text is not in the file, and nothing close to it is either. "
            "Read the file and copy the lines you mean to change.",
            op_index=0,
            metadata={"similarity": round(best_ratio, 3)},
        )

    low = max(0, best_index - 2)
    high = min(len(lines), best_index + len(needle) + 2)
    window = lines[low : low + min(CONTEXT_LINES, high - low)]
    width = len(str(low + len(window)))
    rendered = "\n".join(
        f"{str(low + offset + 1).rjust(width)}| {line}" for offset, line in enumerate(window)
    )
    return Failure(
        "not_found",
        f"that exact text is not in the file. The closest place is line {best_index + 1} "
        f"({best_ratio:.0%} similar), and this is what is actually there — copy from it:",
        context=rendered,
        op_index=0,
        metadata={"similarity": round(best_ratio, 3), "line": best_index + 1},
    )


def _closest(needle: Sequence[str], lines: Sequence[str]) -> tuple[int | None, float]:
    """The window of the file most like the needle, by similarity of the joined text."""
    if not needle or not lines:
        return None, 0.0
    target = "\n".join(needle)
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target)

    best_index: int | None = None
    best_ratio = 0.0
    height = len(needle)
    for index in range(max(1, len(lines) - height + 1)):
        window = "\n".join(lines[index : index + height])
        matcher.set_seq1(window)
        # quick_ratio is an upper bound and much cheaper; compute the real ratio only when
        # it could win.
        if matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_index, best_ratio = index, ratio
    return best_index, best_ratio


# -- an unescaped quote splitting one JSON string into several `lines` elements ---------
#
# A raw `"` inside what the model meant as one string value ends that string early. The
# JSON is still well-formed on either side, so it parses, into more array elements than
# the model wrote lines, split at each stray quote.
#
# Nothing is rejoined automatically. A rejoin that makes the file parse can still be the
# wrong one for a genuinely broken two-line string (`f("a", "b")` is a fine two-argument
# call either way), and an automatic rejoin fixed none of the archived calls it touched
# (strict re-review, item 3), so it was removed (CONTRIBUTING §7). Only the note remains:
# `mismatched_lines_note` explains a `lines`-longer-than-span mismatch and shows where it
# split.
#
# This applies to hashline only, and only to an op whose `lines` was sent as its own JSON
# array. `str_replace` and `whole_file` build `lines` by splitting one string on `\n` (a
# deliberate multi-line value, not a JSON-array accident), so the note would be wrong for
# them (strict review of edit-anchor-leniency, item 5). `_expected_span` enforces this
# through `ResolvedOp.lines_from_array`, which only `HashLineFormat.resolve` sets.

#: `mismatched_lines_note` lists at most this many elements and says how many were left
#: out, so a huge `lines` array cannot make the note itself too long.
_MAX_NOTE_ELEMENTS = 20


def _expected_span(op: ResolvedOp) -> int | None:
    """How many lines `op` replaces, or None where that is not well-defined.

    Only `replace` has a ceiling: the anchors it points between. `insert_after` has none
    (five new lines after one anchor is ordinary), so it is excluded. So is an op whose
    `lines` did not arrive as hashline's own JSON array (see the note above).
    """
    if op.kind != "replace" or not op.lines_from_array:
        return None
    return max(1, op.end - op.start)


def mismatched_lines_note(resolved: Resolved) -> str:
    """Text appended to a `would_not_parse` refusal when a hashline `replace`'s JSON-array
    `lines` is longer than its span.

    It states the mismatch, names the likely cause (an unescaped quote) and shows where the
    array split. Nothing is rewritten because of it (see the note above). Returns `""` when
    the note does not apply (str_replace, whole_file, or `lines` sent as one string): the
    quote-escaping hint is specific to a JSON array.
    """
    for op in resolved.ops:
        span = _expected_span(op)
        if span is None or len(op.lines) <= span:
            continue
        shown = op.lines[:_MAX_NOTE_ELEMENTS]
        boundaries = "\n".join(f"  [{i}] {element!r}" for i, element in enumerate(shown))
        if len(op.lines) > len(shown):
            boundaries += f"\n  ... and {len(op.lines) - len(shown)} more"
        return (
            f"\n\nlines had {len(op.lines)} element(s) where the edit's span is "
            f"{span} line(s):\n{boundaries}\n"
            f'A double quote inside a line must be escaped as \\" in JSON — an '
            f"unescaped one ends the string early, and the rest becomes extra array "
            f"elements."
        )
    return ""
