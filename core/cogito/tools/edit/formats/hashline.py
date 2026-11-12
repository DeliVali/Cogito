"""Line numbers plus a content hash.

`read` returns `11:a3k|function hello() {`, and an edit points at `11:a3k`. The number
says where to look and the hash says whether it is still the line the model meant. A plain
line number goes stale silently after any edit, and copying the line verbatim (what
`str_replace` asks for) fails on a single wrong space.

When the number is stale but the hash is unique, the edit lands where the hash is and the
result reports the drift. The model does not have to re-read the file, which is where the
measured drop in output tokens comes from.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from cogito.tools.edit.formats.base import Failure, Op, Resolved, ResolvedOp

#: Characters of the hash. Three base32 characters is 15 bits: collisions in one file are
#: rare enough for the line number to break the tie, and the hash costs few tokens.
HASH_LENGTH = 3

#: How far from the stated line number to look before widening to the whole file.
NEARBY = 25

_ANCHOR = re.compile(r"^\s*(\d+)\s*:\s*([A-Za-z0-9]{%d})\s*$" % HASH_LENGTH)

#: The whole `N:hash|content` label `render()` prints, copied back verbatim. The model is
#: pointing at the exact line, so the content after `|` is a free cross-check.
_ANCHOR_WITH_CONTENT = re.compile(
    r"^\s*(\d+\s*:\s*[A-Za-z0-9]{%d})\s*\|(.*)$" % HASH_LENGTH, re.DOTALL
)

#: A hash with no line number at all, optionally with the same trailing `|content`.
_BARE_HASH = re.compile(r"^\s*([A-Za-z0-9]{%d})(?:\s*\|(.*))?\s*$" % HASH_LENGTH, re.DOTALL)

#: Well-formed `N:H` where H is not `HASH_LENGTH` characters. Usually a hash copied from
#: an error message's example instead of from a real line (see `_example`).
_ANCHOR_WRONG_LENGTH = re.compile(r"^\s*(\d+)\s*:\s*([A-Za-z0-9]+)\s*$")

#: A plain line number with no hash. Never resolved on its own (see `_find_bare_line`).
_BARE_LINE_NUMBER = re.compile(r"^\s*(\d+)\s*$")

#: A line number followed by `|content`: `render()`'s label without the hash. Always
#: refused (`_refuse_number_with_content`). Checked before `_BARE_HASH`, which a 3-digit
#: number would also match.
_LINE_NUMBER_WITH_CONTENT = re.compile(r"^\s*(\d+)\s*\|(.*)$", re.DOTALL)


def _example(lines: Sequence[str], near: int) -> str:
    """A real anchor from this file, for error messages. A fake example once taught a
    model to send `'11:a3'`, one character short of a real hash. A label `render()`
    actually prints cannot be wrong."""
    if not lines:
        return f"1:{'a' * HASH_LENGTH}"
    index = near if 0 <= near < len(lines) else 0
    return anchor_for(index, lines[index])


def _past_end(stated: int, lines: Sequence[str]) -> str:
    """Why an anchor's line number cannot be right, and what to do instead."""
    if not lines:
        return (
            f"Line {stated + 1} does not exist: the file is empty. Write it whole, or "
            f"read it again if you expected content."
        )
    count = len(lines)
    return (
        f"Line {stated + 1} is past the end of the file, which has {count} "
        f"line{'' if count == 1 else 's'}. Read the file again and copy an anchor from "
        f"it, e.g. '{_example(lines, count - 1)}'. Here are its last lines:"
    )


def line_hash(line: str) -> str:
    """Hash a line, ignoring trailing whitespace.

    Trailing whitespace is invisible in what the model reads, so letting it change the hash
    would fail anchors for a reason nobody can see.
    """
    digest = hashlib.sha1(line.rstrip().encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii")[:HASH_LENGTH].lower()


#: A key name a model appended as the last item of `lines` after the real content
#: (failure analysis of 2026-10-17, P4: knapsack-4 and meetup-0 wrote it into the file).
#: `end` is never one: it is a real last line in Ruby and Lua.
_KEY_NAME = re.compile(r"(lines(_[a-z_]+)?|at)")


def _stray_key(lines: tuple[str, ...], from_array: bool) -> bool:
    """Whether the last item is a stray key name: a bare `lines` or `at` after items that
    each hold several lines, so it cannot be a line the model meant to write."""
    return (
        from_array
        and len(lines) > 1
        and _KEY_NAME.fullmatch(lines[-1].strip()) is not None
        and all("\n" in line for line in lines[:-1])
    )


def anchor_for(index: int, line: str) -> str:
    return f"{index + 1}:{line_hash(line)}"


class HashLineFormat:
    name = "hashline"

    def render(self, lines: Sequence[str], start: int = 1) -> str:
        return "\n".join(
            f"{start + offset}:{line_hash(line)}|{line}" for offset, line in enumerate(lines)
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit."},
                "ops": {
                    "type": "array",
                    "description": (
                        "Edits applied together, or not at all. Anchors are the "
                        "'LINE:HASH' labels read shows, e.g. '11:a3k'."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "description": "replace, insert_after or delete.",
                            },
                            "start": {"type": "string", "description": "First anchor."},
                            "end": {
                                "type": "string",
                                "description": "Last anchor, inclusive.",
                            },
                            "at": {
                                "type": "string",
                                "description": "Anchor for insert_after.",
                            },
                            "lines": {
                                "type": "array",
                                "description": "New lines.",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["path", "ops"],
            "additionalProperties": False,
        }

    def aliases(self) -> Mapping[str, tuple[str, ...]]:
        return {
            "path": ("file_path", "filename", "file", "filepath"),
            "ops": ("edits", "operations", "changes"),
        }

    def parse(self, arguments: Mapping[str, Any]) -> list[Op] | Failure:
        raw = arguments.get("ops")
        if not isinstance(raw, Sequence) or isinstance(raw, str) or not raw:
            return Failure(
                "no_ops",
                "ops must be a non-empty list of edits, each with an op of replace, "
                "insert_after or delete.",
            )

        ops: list[Op] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                return Failure("bad_op", f"ops[{index}] must be an object.", op_index=index)
            kind = str(item.get("op") or item.get("kind") or "").strip()
            # The op name used as a key (`"insert_after": [...]`) instead of
            # `"op": "insert_after", "lines": [...]` is not ambiguous. Every measured case
            # used the key to hold what `lines` would, with or without an explicit `op`
            # (e.g. `"op": "insert_after", "insert_after": {"lines": [...]}`). `replace` is
            # excluded: as a key it was also seen holding `[old, new]`, so its meaning
            # cannot be inferred.
            alias_source: Any = None
            has_alias = False
            for candidate in ("insert_after", "insert_before"):
                value = item.get(candidate)
                if value not in (None, ""):
                    alias_source = value
                    has_alias = True
                    if not kind:
                        kind = candidate
                    break
            if kind not in {"replace", "insert_after", "insert_before", "delete"}:
                if kind:
                    message = (
                        f"ops[{index}]: op must be replace, insert_after or delete, "
                        f"got {kind!r}."
                    )
                else:
                    message = (
                        f"ops[{index}]: no 'op' given, and 'lines' alone does not say "
                        f"whether this replaces the anchor or inserts after it. Add "
                        f'"op": "replace" or "op": "insert_after".'
                    )
                return Failure("bad_op", message, op_index=index)
            start = str(item.get("start") or item.get("at") or "").strip()
            if not start:
                return Failure(
                    "bad_anchor",
                    f"ops[{index}]: needs an anchor. Use 'at' for insert_after and "
                    f"'start' for the others, in the LINE:HASH form read shows.",
                    op_index=index,
                )

            if kind == "delete":
                # Only `delete` may remove content. Any `lines` it was given (there should
                # be none) are ignored.
                lines: Any = ()
                from_array = False
                repaired = ""
            else:
                repaired = ""
                has_lines_key = "lines" in item
                if has_lines_key:
                    source = item["lines"]
                elif has_alias and kind == candidate:
                    source = alias_source
                else:
                    return Failure(
                        "bad_lines",
                        f"ops[{index}]: give 'lines' with the new content; use "
                        f'"op": "delete" to remove lines instead.',
                        op_index=index,
                    )
                from_array = isinstance(source, Sequence) and not isinstance(source, str)
                if isinstance(source, Mapping):
                    if "lines" not in source:
                        return Failure(
                            "bad_lines",
                            f"ops[{index}]: 'lines' is an object but has no 'lines' key "
                            f"inside it.",
                            op_index=index,
                        )
                    source = source["lines"]
                    from_array = isinstance(source, Sequence) and not isinstance(source, str)
                if isinstance(source, str):
                    source = source.split("\n")
                if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
                    return Failure(
                        "bad_lines",
                        f"ops[{index}]: lines must be a list of strings.",
                        op_index=index,
                    )
                lines = tuple(str(line) for line in source)
                if _stray_key(lines, from_array):
                    lines, repaired = lines[:-1], "stray_key"
                if not lines:
                    return Failure(
                        "bad_lines",
                        f"ops[{index}]: lines is empty; {kind} must add content. Use "
                        f'"op": "delete" to remove lines instead.',
                        op_index=index,
                    )

            ops.append(
                Op(
                    kind=kind,  # type: ignore[arg-type]
                    start=start,
                    end=str(item.get("end") or "").strip(),
                    lines=tuple(lines),
                    lines_from_array=from_array,
                    repaired=repaired,
                )
            )
        return ops

    def _find(
        self,
        anchor: str,
        lines: Sequence[str],
        index: int,
        fresh_read_range: tuple[int, int] | None = None,
    ) -> tuple[int, str, str] | Failure:
        """Resolve one anchor, reporting drift instead of failing on a stale number.

        Returns `(position, drift_note, repair_kind)`. `repair_kind` is `bare_hash`,
        `pipe_content` or `line_number_fresh_read` when that leniency placed the anchor,
        and `""` for an exact match or a stale-number drift (reported in `drift_note`).

        Shapes tried, in order: the whole `N:hash|content` label copied back (the content
        is a free cross-check); `N:hash` itself; a plain line number, only when
        `fresh_read_range` says the file is unchanged since the model's last read and the
        line was inside that read; a bare hash with no line number, when unique; and a
        well-formed `N:H` with a wrong-length hash, which fails with context around line N
        instead of a bare refusal. An all-digit anchor is always a line number, never a
        hash, even at exactly `HASH_LENGTH` characters. A number followed by `|content` is
        refused, and its digits are never tried as a bare hash (final review of
        edit-anchor-leniency).
        """
        with_content = _ANCHOR_WITH_CONTENT.match(anchor)
        if with_content is not None:
            return self._find_with_content(
                with_content.group(1), with_content.group(2), lines, index, fresh_read_range
            )

        match = _ANCHOR.match(anchor)
        if match is not None:
            return self._find_exact(match, lines, index, anchor)

        bare_line = _BARE_LINE_NUMBER.match(anchor)
        if bare_line is not None:
            # An all-digit anchor is only ever a line number, even when it is HASH_LENGTH
            # characters long. Reading it as a hash could silently land on some other
            # line whose hash happens to be those digits (strict re-review of
            # edit-anchor-leniency, item 1).
            stated_line = int(bare_line.group(1))
            line_result = self._find_bare_line(stated_line, lines, index, anchor, fresh_read_range)
            if isinstance(line_result, Failure):
                return self._note_hash_coincidence(line_result, anchor, lines)
            # Resolved as a line number. If the digits are also another line's real hash,
            # both readings are valid, so refuse rather than pick one.
            bare = _BARE_HASH.match(anchor)
            if bare is not None:
                wanted = bare.group(1).lower()
                found_pos = line_result[0]
                collisions = [
                    i for i, line in enumerate(lines)
                    if i != found_pos and line_hash(line) == wanted
                ]
                if collisions:
                    candidates = [anchor_for(i, lines[i]) for i in collisions[:8]]
                    all_candidates = sorted({stated_line, *[i + 1 for i in collisions]})
                    return Failure(
                        "bad_anchor",
                        f"{anchor!r} is ambiguous: read as a line number it means line "
                        f"{stated_line}, but it is also the hash of "
                        f"{'line' if len(collisions) == 1 else f'{len(collisions)} lines'} "
                        f"{', '.join(candidates)}. Use the full anchor for the one you "
                        f"mean.",
                        op_index=index,
                        metadata={"candidates": all_candidates},
                    )
            return line_result

        number_with_content = _LINE_NUMBER_WITH_CONTENT.match(anchor)
        if number_with_content is not None:
            # Must come before `_BARE_HASH`, which "765|..." also matches. The digits
            # before `|` are a line number missing its hash, never a hash: the same rule
            # as the all-digit branch above (final review of edit-anchor-leniency).
            return self._refuse_number_with_content(
                int(number_with_content.group(1)), lines, index, anchor
            )

        bare = _BARE_HASH.match(anchor)
        if bare is not None:
            hash_content = bare.group(2) or ""
            wanted = bare.group(1).lower()
            hash_matches = [i for i, line in enumerate(lines) if line_hash(line) == wanted]
            return self._find_bare(hash_content, hash_matches, lines, index, anchor)

        wrong_length = _ANCHOR_WRONG_LENGTH.match(anchor)
        if wrong_length is not None:
            return self._find_wrong_length(wrong_length, lines, index, anchor)

        return Failure(
            "bad_anchor",
            f"{anchor!r} is not an anchor. Anchors look like '{_example(lines, 0)}' — the "
            f"label read puts in front of each line.",
            op_index=index,
        )

    def _find_exact(
        self, match: re.Match[str], lines: Sequence[str], index: int, anchor: str
    ) -> tuple[int, str, str] | Failure:
        stated = int(match.group(1)) - 1
        wanted = match.group(2).lower()

        if 0 <= stated < len(lines) and line_hash(lines[stated]) == wanted:
            return stated, "", ""

        near = [
            candidate
            for candidate in range(max(0, stated - NEARBY), min(len(lines), stated + NEARBY + 1))
            if line_hash(lines[candidate]) == wanted
        ]
        if len(near) == 1:
            return near[0], f"{stated + 1} -> {near[0] + 1}", ""

        anywhere = [i for i, line in enumerate(lines) if line_hash(line) == wanted]
        if len(anywhere) == 1:
            return anywhere[0], f"{stated + 1} -> {anywhere[0] + 1} (moved far)", ""

        return Failure(
            "stale_anchor",
            f"no line matching {anchor} — the file has changed since it was read."
            + (f" That hash appears on {len(anywhere)} lines." if len(anywhere) > 1 else ""),
            context=self._window(lines, stated),
            op_index=index,
            metadata={"candidates": [i + 1 for i in anywhere[:8]]},
        )

    def _find_with_content(
        self,
        anchor_part: str,
        content: str,
        lines: Sequence[str],
        index: int,
        fresh_read_range: tuple[int, int] | None = None,
    ) -> tuple[int, str, str] | Failure:
        """`N:hash|content`, the whole label `render()` prints, copied back. The anchor
        part resolves as `N:hash` would. The content is then compared, ignoring
        surrounding whitespace, as a sanity check. Empty content (a trailing `|`) is not
        checked."""
        result = self._find(anchor_part, lines, index, fresh_read_range)
        if isinstance(result, Failure):
            return result
        found, drift, _inner_kind = result
        if content.strip() and content.strip() != lines[found].strip():
            return Failure(
                "stale_anchor",
                f"the anchor in {anchor_part!r} points at line {found + 1}, but the "
                f"content copied after '|' does not match what is there now — the file "
                f"has changed since it was read. Here is what is actually there:",
                context=self._window(lines, found),
                op_index=index,
                metadata={"candidates": [found + 1]},
            )
        return found, drift, "pipe_content"

    def _find_bare(
        self,
        content: str,
        matches: list[int],
        lines: Sequence[str],
        index: int,
        anchor: str,
    ) -> tuple[int, str, str] | Failure:
        """A bare hash with no line number, accepted when it is unique in the file. The
        caller computes `matches`."""
        if not matches:
            return Failure(
                "bad_anchor",
                f"{anchor!r} is a bare hash with no line number, and no line hashes to "
                f"it. Read the file again and copy a full anchor, e.g. "
                f"'{_example(lines, 0)}'.",
                op_index=index,
            )
        if len(matches) > 1:
            candidates = [anchor_for(i, lines[i]) for i in matches[:8]]
            return Failure(
                "bad_anchor",
                f"{anchor!r} is a bare hash with no line number, and it matches "
                f"{len(matches)} lines. Use the full anchor for the one you mean: "
                f"{', '.join(candidates)}.",
                op_index=index,
                metadata={"candidates": [i + 1 for i in matches[:8]]},
            )

        found = matches[0]
        if content.strip() and content.strip() != lines[found].strip():
            return Failure(
                "stale_anchor",
                f"the hash in {anchor!r} matches line {found + 1}, but the content "
                f"copied after '|' does not match what is there now. Here is what is "
                f"actually there:",
                context=self._window(lines, found),
                op_index=index,
                metadata={"candidates": [found + 1]},
            )
        return found, f"'{anchor}' matched by hash alone, at line {found + 1}", "bare_hash"

    def _find_wrong_length(
        self, match: re.Match[str], lines: Sequence[str], index: int, anchor: str
    ) -> Failure:
        stated = int(match.group(1)) - 1
        got = match.group(2)
        wrong = (
            f"{anchor!r} has a {len(got)}-character hash, but anchors use "
            f"{HASH_LENGTH} — copy the exact label read shows"
        )
        if not 0 <= stated < len(lines):
            # Past the end there is nothing "at line N" to show: say so, and show the
            # file's last lines instead of an empty window.
            return Failure(
                "stale_anchor",
                f"{wrong}. {_past_end(stated, lines)}",
                context=self._window(lines, len(lines) - 1) if lines else "",
                op_index=index,
            )
        return Failure(
            "stale_anchor",
            f"{wrong}, e.g. '{_example(lines, stated)}'. Here is what is actually at line "
            f"{stated + 1} and around it:",
            context=self._window(lines, stated),
            op_index=index,
            metadata={"candidates": [stated + 1]},
        )

    def _find_bare_line(
        self,
        stated_line: int,
        lines: Sequence[str],
        index: int,
        anchor: str,
        fresh_read_range: tuple[int, int] | None,
    ) -> tuple[int, str, str] | Failure:
        """A plain line number with no hash. A number alone cannot say whether the file
        changed since it was read, which is what the hash is for. So it is accepted only
        when the file is byte-identical to the model's last `read` of it (the caller passes
        `fresh_read_range` only then) and the line was inside what that read showed.
        """
        stated = stated_line - 1
        in_bounds = 0 <= stated < len(lines)
        example_at = stated if in_bounds else 0

        if fresh_read_range is not None:
            shown_from, shown_to = fresh_read_range
            if in_bounds and shown_from <= stated_line <= shown_to:
                return (
                    stated,
                    f"'{anchor}' accepted as a plain line number -- unchanged since it "
                    f"was last read",
                    "line_number_fresh_read",
                )
            # Fresh, but outside what the last read showed. Out of bounds cannot happen
            # here: the file is unchanged since that read, so its line numbers are in range.
            return Failure(
                "stale_anchor",
                f"line {stated_line} was not part of what the last read of this file "
                f"showed (lines {shown_from}-{shown_to}). Read that part of the file, or "
                f"copy the exact anchor, e.g. '{_example(lines, example_at)}'.",
                context=self._window(lines, example_at),
                op_index=index,
                metadata={"candidates": [stated_line]} if in_bounds else {},
            )

        return Failure(
            "stale_anchor",
            f"{anchor!r} is a plain line number with no hash, and the file may have "
            f"changed since it was last read -- a number alone cannot say. Copy the "
            f"exact anchor read shows, e.g. '{_example(lines, example_at)}'.",
            context=self._window(lines, example_at) if in_bounds else "",
            op_index=index,
            metadata={"candidates": [stated_line]} if in_bounds else {},
        )

    def _refuse_number_with_content(
        self, stated_line: int, lines: Sequence[str], index: int, anchor: str
    ) -> Failure:
        """`N|content`: a line number and the line's text with the hash left out. Refused
        even when the file is fresh: 0 of 7,962 archived anchors had this shape, so a
        leniency for it could not be measured. The refusal shows the real anchor for line
        `N`, and names any line whose hash the digits happen to be, as for a refused bare
        number."""
        stated = stated_line - 1
        in_bounds = 0 <= stated < len(lines)
        example_at = stated if in_bounds else 0
        failure = Failure(
            "bad_anchor",
            f"{anchor!r} has a line number but no hash. Copy the exact anchor read shows, "
            f"e.g. '{_example(lines, example_at)}'.",
            context=self._window(lines, example_at) if in_bounds else "",
            op_index=index,
            metadata={"candidates": [stated_line]} if in_bounds else {},
        )
        return self._note_hash_coincidence(failure, anchor.split("|", 1)[0], lines)

    def _note_hash_coincidence(
        self, failure: Failure, anchor: str, lines: Sequence[str]
    ) -> Failure:
        """If a refused all-digit anchor is also some line's real hash, name that line as a
        candidate. Information only: nothing is edited, since a digit anchor is never read
        as a hash."""
        if len(anchor.strip()) != HASH_LENGTH:
            return failure
        wanted = anchor.strip().lower()
        hash_matches = [i for i, line in enumerate(lines) if line_hash(line) == wanted]
        if not hash_matches:
            return failure
        candidates = [anchor_for(i, lines[i]) for i in hash_matches[:8]]
        extra = (
            f" '{anchor}' also happens to be the hash of "
            f"{'line' if len(hash_matches) == 1 else f'{len(hash_matches)} lines'} "
            f"{', '.join(candidates)} -- not used; a number is never read as a hash."
        )
        merged_candidates = sorted(
            {*failure.metadata.get("candidates", []), *(i + 1 for i in hash_matches[:8])}
        )
        return replace(
            failure,
            message=failure.message + extra,
            metadata={**failure.metadata, "candidates": merged_candidates},
        )

    def _window(self, lines: Sequence[str], around: int, radius: int = 7) -> str:
        low = max(0, around - radius)
        high = min(len(lines), around + radius + 1)
        if low >= high:
            return ""
        return self.render(lines[low:high], start=low + 1)

    def resolve(
        self,
        ops: Sequence[Op],
        lines: Sequence[str],
        *,
        fresh_read_range: tuple[int, int] | None = None,
    ) -> Resolved | Failure:
        resolved: list[ResolvedOp] = []
        notes: list[str] = []
        repairs: set[str] = set()

        for index, op in enumerate(ops):
            # Every anchor, hash or fresh bare line number, resolves against `lines` as
            # read, never against an earlier op in the same call. That keeps the edit
            # atomic, and it matches what the model does: both archived cases used the
            # original numbering (strict re-review of edit-anchor-leniency, item 2).
            found = self._find(op.start, lines, index, fresh_read_range)
            if isinstance(found, Failure):
                return found
            start, drift, kind = found
            if kind:
                repairs.add(kind)
            if op.repaired:
                repairs.add(op.repaired)

            end = start
            if op.end:
                found_end = self._find(op.end, lines, index, fresh_read_range)
                if isinstance(found_end, Failure):
                    return found_end
                end, end_drift, end_kind = found_end
                drift = drift or end_drift
                if end_kind:
                    repairs.add(end_kind)
                if end < start:
                    return Failure(
                        "reversed_range",
                        f"ops[{index}]: end anchor {op.end} is before start anchor {op.start}.",
                        op_index=index,
                    )

            if drift:
                notes.append(f"ops[{index}] anchor drifted: {drift}")

            if op.kind == "insert_after":
                resolved.append(
                    ResolvedOp(
                        "insert_after", start + 1, start + 1, op.lines, drift, op.lines_from_array
                    )
                )
            elif op.kind == "insert_before":
                # `apply()` only knows "insert_after", and inserting at `start` instead of
                # `start + 1` is "before this line".
                resolved.append(
                    ResolvedOp("insert_after", start, start, op.lines, drift, op.lines_from_array)
                )
            elif op.kind == "delete":
                resolved.append(ResolvedOp("delete", start, end + 1, (), drift))
            else:
                resolved.append(
                    ResolvedOp("replace", start, end + 1, op.lines, drift, op.lines_from_array)
                )

        overlap = _overlapping(resolved)
        if overlap is not None:
            first, second = resolved[overlap[0]], resolved[overlap[1]]
            return Failure(
                "overlapping_ops",
                f"ops[{overlap[0]}] ({first.kind}) and ops[{overlap[1]}] ({second.kind}) "
                f"cover the same lines. Combine them into one edit.",
                op_index=overlap[1],
            )
        return Resolved(ops=tuple(resolved), notes=tuple(notes), repairs=tuple(sorted(repairs)))


def _overlapping(ops: Sequence[ResolvedOp]) -> tuple[int, int] | None:
    """Any two `replace`/`delete` ranges that share a line, or an insert that lands
    strictly inside one of them.

    Every pair is checked, not only neighbours after sorting: sorting can put an insert
    tied with a range boundary between two overlapping ranges, so they are never compared.
    A real call has a handful of ops, so O(n^2) is cheap.

    An insert exactly at a range's start or end is fine (`apply()`'s tie-break order makes
    it safe). Only a position strictly inside a range overlaps. Two inserts at the same gap
    never conflict; `apply()` orders them.
    """
    for i, first in enumerate(ops):
        for j in range(i + 1, len(ops)):
            second = ops[j]
            if first.kind == "insert_after" and second.kind == "insert_after":
                continue
            if first.kind == "insert_after":
                if second.start < first.start < second.end:
                    return i, j
                continue
            if second.kind == "insert_after":
                if first.start < second.start < first.end:
                    return i, j
                continue
            if first.start < second.end and second.start < first.end:
                return i, j
    return None
