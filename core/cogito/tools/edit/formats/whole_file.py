"""Send the whole file back.

The most reliable format: 82-100% across every model, since there is nothing to locate or
match. Also by far the most expensive: on a file of a few thousand lines it costs about 18
times the output tokens of a targeted edit, on every change.

It is the control arm of the experiment, and the right choice for a file small enough that
locating anything in it is wasted effort.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cogito.tools.edit.formats.base import Failure, Op, Resolved, ResolvedOp
from cogito.tools.edit.formats.str_replace import split
from cogito.tools.line_endings import to_lf


class WholeFileFormat:
    name = "whole_file"

    def render(self, lines: Sequence[str], start: int = 1) -> str:
        return "\n".join(lines)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "content": {
                    "type": "string",
                    "description": "The complete new contents of the file.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    def aliases(self) -> Mapping[str, tuple[str, ...]]:
        return {
            "path": ("file_path", "filename", "file", "filepath"),
            "content": ("contents", "text", "body", "new_content", "file_text"),
        }

    def parse(self, arguments: Mapping[str, Any]) -> list[Op] | Failure:
        content = arguments.get("content")
        if not isinstance(content, str):
            return Failure("no_content", "content must be the complete new text of the file.")
        # LF here; the file's own line ending is restored on write.
        return [Op(kind="replace", start="", lines=split(to_lf(content)))]

    def resolve(
        self,
        ops: Sequence[Op],
        lines: Sequence[str],
        *,
        fresh_read_range: tuple[int, int] | None = None,
    ) -> Resolved | Failure:
        return Resolved(ops=(ResolvedOp("replace", 0, len(lines), ops[0].lines),))
