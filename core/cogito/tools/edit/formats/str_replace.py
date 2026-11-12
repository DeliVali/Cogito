"""Find an exact snippet and replace it.

This is the format frontier models are trained on, and the baseline to measure against.
Roughly one in five first attempts fails with "string not found", because the model has to
reproduce every character, including whitespace it cannot see.

Matching is line by line, not over raw text. Models paste whole lines, the near-miss
probes in `diagnose.py` (indentation, trailing space, line endings) are line-shaped, and a
line-aligned match can be reported with a line number the model can act on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cogito.tools.edit.formats.base import Failure, Op, Resolved, ResolvedOp
from cogito.tools.line_endings import to_lf

_CONTEXT_RADIUS = 3


def split(text: str) -> tuple[str, ...]:
    """Split into lines the way the file is held, dropping one trailing newline."""
    if text.endswith("\n"):
        text = text[:-1]
    return tuple(text.split("\n"))


def occurrences(haystack: Sequence[str], needle: Sequence[str]) -> list[int]:
    """Every index where ``needle`` appears as a run of whole lines."""
    if not needle:
        return []
    found: list[int] = []
    limit = len(haystack) - len(needle)
    for index in range(limit + 1):
        if list(haystack[index : index + len(needle)]) == list(needle):
            found.append(index)
    return found


class StrReplaceFormat:
    name = "str_replace"

    def render(self, lines: Sequence[str], start: int = 1) -> str:
        width = len(str(start + len(lines) - 1))
        return "\n".join(
            f"{str(start + offset).rjust(width)}| {line}" for offset, line in enumerate(lines)
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "old": {
                    "type": "string",
                    "description": (
                        "The exact text to replace, copied from the file including its "
                        "indentation. Include enough surrounding lines to be unique."
                    ),
                },
                "new": {"type": "string", "description": "What to put in its place."},
                "occurrence": {
                    "type": "integer",
                    "description": "Which match to replace, 1-based, when several match.",
                },
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        }

    def aliases(self) -> Mapping[str, tuple[str, ...]]:
        return {
            "path": ("file_path", "filename", "file", "filepath"),
            "old": ("old_string", "old_str", "search", "find", "target", "from"),
            "new": ("new_string", "new_str", "replace", "replacement", "to"),
            "occurrence": ("index", "nth", "which"),
        }

    def parse(self, arguments: Mapping[str, Any]) -> list[Op] | Failure:
        old = arguments.get("old")
        new = arguments.get("new")
        if not isinstance(old, str) or not old:
            return Failure(
                "no_old",
                "old must be the exact text to replace, copied from the file. "
                "To create or overwrite a file, use write instead.",
            )
        if not isinstance(new, str):
            return Failure("no_new", "new must be the text to put in place of old.")
        if old == new:
            return Failure("no_change", "old and new are identical; nothing to do.")

        occurrence = arguments.get("occurrence")
        return [
            Op(
                kind="replace",
                start=old,
                end=str(occurrence) if occurrence is not None else "",
                # LF, like the lines around it; the file's own ending is restored on
                # write. `old` stays as sent: a CR there is a matching problem, which
                # `diagnose`'s `line_endings` probe handles and reports.
                lines=split(to_lf(new)),
            )
        ]

    def resolve(
        self,
        ops: Sequence[Op],
        lines: Sequence[str],
        *,
        fresh_read_range: tuple[int, int] | None = None,
    ) -> Resolved | Failure:
        op = ops[0]
        needle = split(op.start)
        found = occurrences(lines, needle)

        if not found:
            # diagnose.py rescues a near miss or explains what is there. A bare
            # "string not found" from here would be the bug.
            return Failure(
                "not_found",
                "that exact text is not in the file",
                op_index=0,
                metadata={"needle": needle},
            )

        if len(found) > 1:
            if not op.end:
                return Failure(
                    "ambiguous",
                    f"that text appears {len(found)} times. Add surrounding lines to make "
                    f"it unique, or pass occurrence to choose one.",
                    context=self._sites(lines, found, len(needle)),
                    op_index=0,
                    metadata={"count": len(found), "lines": [i + 1 for i in found]},
                )
            try:
                chosen = int(op.end)
            except ValueError:
                return Failure("bad_occurrence", f"occurrence must be a number, got {op.end!r}.")
            if not 1 <= chosen <= len(found):
                return Failure(
                    "bad_occurrence",
                    f"occurrence {chosen} is out of range; there are {len(found)} matches.",
                    context=self._sites(lines, found, len(needle)),
                )
            found = [found[chosen - 1]]

        start = found[0]
        return Resolved(
            ops=(ResolvedOp("replace", start, start + len(needle), op.lines),),
            notes=(),
        )

    def _sites(self, lines: Sequence[str], where: Sequence[int], height: int) -> str:
        blocks: list[str] = []
        for index in where[:5]:
            low = max(0, index - _CONTEXT_RADIUS)
            high = min(len(lines), index + height + _CONTEXT_RADIUS)
            blocks.append(self.render(lines[low:high], start=low + 1))
        if len(where) > 5:
            blocks.append(f"... and {len(where) - 5} more matches")
        return "\n---\n".join(blocks)
