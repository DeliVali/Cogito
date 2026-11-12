"""What an edit format is.

The edit tool's wire format is the largest measured lever on agent quality: changing only
the format moved fifteen models by five to fourteen points, and the weakest ones by more.
So it is a protocol with interchangeable implementations, chosen by a configuration key.
A negative result then costs a config change, not a refactor.

A format owns three things that must agree: how a file is shown, how an edit is described,
and how a description is turned back into line numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Kind = Literal["replace", "insert_after", "delete"]


@dataclass(frozen=True, slots=True)
class Op:
    """One edit, as the model described it."""

    kind: Kind
    #: Whatever the format uses to point at a line: an anchor, a string, nothing.
    start: str = ""
    end: str = ""
    lines: tuple[str, ...] = ()
    #: Whether `lines` arrived as its own JSON array, not as a string split on `\n`.
    #: str_replace and whole_file never set it. Only an array-sourced hashline op gets
    #: `diagnose.mismatched_lines_note`: a multi-line string is deliberately one value,
    #: never a JSON-quote accident.
    lines_from_array: bool = False
    #: A leniency applied while reading the op itself (hashline's `stray_key`), reported
    #: with the anchor repairs.
    repaired: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedOp:
    """The same edit, pinned to concrete line indices in the file as it is now."""

    kind: Kind
    #: Zero-based, half-open. For insert_after both point at the insertion position.
    start: int
    end: int
    lines: tuple[str, ...]
    #: Set when the anchor was found somewhere other than where it claimed to be.
    drift: str = ""
    #: Carried from `Op.lines_from_array` (see there).
    lines_from_array: bool = False


@dataclass(frozen=True, slots=True)
class Resolved:
    ops: tuple[ResolvedOp, ...]
    notes: tuple[str, ...] = ()
    #: Leniency kinds used to resolve an anchor (e.g. `bare_hash`,
    #: `line_number_fresh_read`, `pipe_content`). `EditTool` merges them into
    #: `repaired_by` (`ToolResult.metadata`, comma-joined) and shows the model a
    #: `(repaired: <kind>)` line in `content`, outside `notes`'s cap, so a model that never
    #: reads metadata still learns the canonical form.
    repairs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Failure:
    """Why an edit could not be placed, in terms that say what to do instead."""

    kind: str
    message: str
    #: The file as it is around the failure, rendered in this format. This is what stops
    #: retry loops: a bare rejection teaches nothing.
    context: str = ""
    #: Which op failed, so the model can fix one rather than resend everything.
    op_index: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class EditFormat(Protocol):
    name: str

    def render(self, lines: Sequence[str], start: int = 1) -> str:
        """How `read` shows the file. Must be addressable by whatever `parse` accepts."""
        ...

    def schema(self) -> dict[str, Any]:
        """The edit tool's parameters under this format."""
        ...

    def aliases(self) -> Mapping[str, tuple[str, ...]]:
        """Parameter names other harnesses use, so a model trained elsewhere still lands."""
        ...

    def parse(self, arguments: Mapping[str, Any]) -> list[Op] | Failure: ...

    def resolve(
        self,
        ops: Sequence[Op],
        lines: Sequence[str],
        *,
        fresh_read_range: tuple[int, int] | None = None,
    ) -> Resolved | Failure:
        """`fresh_read_range` (1-based, inclusive) is the span the model's latest `read` of
        this file showed. It is passed only while the file is byte-identical to that read,
        and is `None` otherwise (also when the file was never read this session). Only
        `hashline` uses it, to accept a plain line number without a hash. `str_replace`
        (which always needs the content) and `whole_file` ignore it.
        """
        ...


def apply(lines: Sequence[str], resolved: Resolved) -> list[str]:
    """Apply resolved ops to the file.

    Ops run in descending order of start, so an earlier edit never shifts the indices a
    later one was resolved against. Every op was resolved against the original lines before
    any of this runs: partial application is what sends a weak model into a spiral.

    At the same `start`, replace/delete runs before an insert (`insert_after`, including
    `insert_before` mapped onto it). An insert has zero width, so running it first would
    shift the array and the replace/delete would land on the inserted text instead of the
    line the model meant (strict review of edit-anchor-leniency, item 2).

    Two inserts at the same gap: each later insertion lands in front of what is already
    there, so the one that runs first ends up furthest from the gap. To keep the model's
    order, the first-listed insert must run last, so the op's index in `ops`, descending,
    breaks that tie (strict re-review of edit-anchor-leniency, item 4).
    """
    ordered = sorted(
        enumerate(resolved.ops),
        key=lambda pair: (pair[1].start, pair[1].kind != "insert_after", pair[0]),
        reverse=True,
    )
    out = list(lines)
    for _, op in ordered:
        if op.kind == "delete":
            del out[op.start : op.end]
        elif op.kind == "replace":
            out[op.start : op.end] = list(op.lines)
        else:  # insert_after
            out[op.start : op.start] = list(op.lines)
    return out
