"""The edit tool.

Most of this exists to keep a weak model out of a retry loop, which is what the
measurements say costs the most: fifteen models moved five to fourteen points on the format
alone, and output tokens fell by about a fifth once they stopped resending failed edits.

Three rules:

**Resolve everything before writing anything.** Every anchor is resolved against the file as
read, and ops apply in descending order so an earlier edit cannot shift a later one. If any
op fails, nothing is written and the error names which one. Partial application is what
sends a model into a spiral.

**Never answer with a rejection alone.** A near miss is repaired. A real miss comes back
with what the file says at the likeliest place.

**Check the file still parses.** If it parsed before and does not after, roll back and say
where. For Python, when the harness's interpreter (which may be older) says no, the
project's own interpreter decides.

A placeholder that stands for code (``// ... existing code ...``) is refused before either
check: it applies cleanly and often parses, and what it deletes is found much later
(`elision.py`).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cogito.guard.undo import UndoStore
from cogito.provider.base import (
    PayloadShape,
    Provenance,
    Reproducibility,
    Retention,
    ToolSpec,
)
from cogito.tools.base import ToolContext, ToolResult
from cogito.tools.edit import diagnose, elision, verify
from cogito.tools.edit.formats.base import EditFormat, Failure, Op, Resolved, apply
from cogito.tools.edit.formats.str_replace import split
from cogito.tools.files import FileTracker, mass_removal, unread_overwrite
from cogito.tools.line_endings import (
    ending_for,
    read_exact,
    restore,
    split_lines,
    to_lf,
    write_exact,
)
from cogito.tools.project_files import (
    directory_message,
    missing_file_message,
    resolve_path,
    unwritable_message,
)

_MAX_NOTES = 6

#: Characters that end a JSON string or structure. When they trail a line of code, the
#: model may have closed its quote one character too late, so the brackets that should have
#: ended the argument ended up inside the value.
_JSON_TAIL = "\"']}"

#: How many trailing characters are worth suspecting. Beyond this it is not a slip.
_MAX_STRAY = 6


def _stray_json_note(lines: list[str], line_number: int | None, path: Path) -> str:
    """Suggest stray JSON punctuation only when removing it fixes the parse.

    As a pattern this matches ordinary code (`data = {"k": [1]}` ends the same way), and a
    hint that points at correct code is worse than none. So strip the trailing punctuation,
    re-parse, and say something only if that fixed it.
    """
    if line_number is None:
        return ""
    # A syntax error is often reported on the line after the one that caused it.
    for index in (line_number - 1, line_number - 2):
        if not 0 <= index < len(lines):
            continue
        original = lines[index].rstrip()
        # Shortest removal first. Stripping greedily also takes the quote the code needs,
        # and then nothing parses either way.
        for length in range(1, _MAX_STRAY + 1):
            if length > len(original) or original[-length] not in _JSON_TAIL:
                break
            trimmed = original[:-length]
            if not trimmed.strip():
                break
            trial = list(lines)
            trial[index] = trimmed
            result = verify.check(path, "\n".join(trial) + "\n")
            if result is not None and result.ok:
                return (
                    f"\n\nRemoving {original[-length:]!r} from the end of line {index + 1} "
                    f"would make it parse. That looks like the JSON brackets that should "
                    f"have closed your arguments, closed inside the string instead — check "
                    f"where the quote ends."
                )
    return ""


def _describe(failure: Failure) -> str:
    body = failure.message
    if failure.context:
        body += "\n\n" + failure.context
    return body


def _uses_insert_key_alias(arguments: Mapping[str, Any], parsed: Sequence[Op]) -> bool:
    """Whether any op's `lines` came from an `insert_after`/`insert_before`-named key
    instead of a `lines` key.

    `HashLineFormat.parse` uses that alias only when `lines` is absent, so a parsed insert
    op with no `lines` key in its raw dict means the alias fired.
    """
    raw_ops = arguments.get("ops")
    if not isinstance(raw_ops, Sequence) or isinstance(raw_ops, str):
        return False
    for op, raw_item in zip(parsed, raw_ops, strict=False):
        if (
            op.kind in ("insert_after", "insert_before")
            and isinstance(raw_item, Mapping)
            and "lines" not in raw_item
        ):
            return True
    return False


def edit_spec(fmt: EditFormat) -> ToolSpec:
    """The edit tool's definition under one format."""
    return ToolSpec(
        name="edit",
        #: A confirmation of a change. The change itself is on disk and in the undo store.
        provenance=Provenance(
            reproducibility=Reproducibility.EPHEMERAL,
            retention=Retention.ACKNOWLEDGEMENT,
            shape=PayloadShape.COMPACT,
        ),
        description=(
            f"Change part of a file in place, in the {fmt.name} format. Anchors are "
            f"the labels read shows, from the file as read before this call's ops. "
            f"All edits in a call apply together, or none do."
        ),
        parameters=fmt.schema(),
        aliases=dict(fmt.aliases()),
        budget_tokens=600,
        mutates=True,
        core=True,
        # Every string but the path is text the edit puts in or matches in the file.
        verbatim=tuple(
            name
            for name, schema in fmt.schema().get("properties", {}).items()
            if schema.get("type") == "string" and name != "path"
        ),
    )


class EditTool:
    """One tool; its wire format is a configuration key, so formats can be measured."""

    def __init__(
        self,
        fmt: EditFormat,
        tracker: FileTracker,
        undo: UndoStore,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._format = fmt
        self._tracker = tracker
        self._undo = undo
        #: The environment the project's own `python3` is found on before a Python edit is
        #: rolled back (`verify.project_python`): the shell's, as `cli.py` builds
        #: it. None means this process's own.
        self._env = env
        self.spec = edit_spec(fmt)

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        started = time.monotonic()
        raw = str(arguments.get("path", "")).strip()
        if not raw:
            return self._failed("path is required", "missing_path", started)

        path = resolve_path(context.project_root if context else None, raw)
        if not path.exists():
            return self._failed(
                missing_file_message(
                    context.project_root,
                    path,
                    then="use write to create it.",
                    otherwise="Use write to create it.",
                ),
                "not_found",
                started,
            )
        if path.is_dir():
            return self._failed(
                directory_message(
                    context.project_root,
                    path,
                    otherwise="edit changes a file. Name a file inside it, or use glob to list it.",
                    then="edit changes a file: name one of them.",
                ),
                "is_directory",
                started,
            )

        try:
            # The file's own text, with its line endings and its final newline (or lack
            # of one) intact. `before_text` below loses both, since `verify.check` needs
            # LF with a trailing newline. Metadata shows what was on disk; loop.changes
            # relies on this for its "\ No newline..." marker.
            raw_before = read_exact(path)
        except OSError as exc:
            return self._failed(f"could not read {path}: {exc}", "unreadable", started)
        # Every op works on these LF lines, the ones `read` showed; the file's own ending
        # is restored only when it is written (`line_endings`).
        lines = split_lines(to_lf(raw_before))

        parsed = self._format.parse(arguments)
        if isinstance(parsed, Failure):
            return self._failed(_describe(parsed), parsed.kind, started, op=parsed.op_index)

        # whole_file sends the file's complete new text, as `write` does, so `write`'s two
        # guards apply: no replacing a file never read, no mass removal of one not seen
        # whole. Anchors and old text already show what the other formats replace.
        whole = self._format.name == "whole_file"
        if whole and (refused := unread_overwrite(path, self._tracker, "edit")) is not None:
            return self._refused(refused, started)

        # Every leniency this call uses, reported to the model as `(repaired: <kind>)` on a
        # line of its own, outside `notes[:_MAX_NOTES]`, so the notes cap never drops it.
        repair_kinds: set[str] = set()
        if _uses_insert_key_alias(arguments, parsed):
            repair_kinds.add("insert_key")

        # A plain line number cannot show the file is unchanged since it was read (that is
        # why hashline hashes lines). So one is accepted only if the file is byte-identical
        # to the model's latest `read` of this path in this session. `None` (including
        # "never read") means a format that accepts plain line numbers must refuse one.
        snapshot = self._tracker.read_snapshot(path)
        fresh_read_range = (
            (snapshot.shown_from, snapshot.shown_to)
            if snapshot is not None and snapshot.content == raw_before
            else None
        )
        resolved = self._format.resolve(parsed, lines, fresh_read_range=fresh_read_range)
        notes: list[str] = []

        if isinstance(resolved, Failure):
            if resolved.kind != "not_found":
                return self._failed(
                    _describe(resolved),
                    resolved.kind,
                    started,
                    op=resolved.op_index,
                    repaired=",".join(sorted(repair_kinds)),
                )
            # A miss is where the tokens go. Try to place it safely before giving up.
            rescue = diagnose.rescue(
                split(str(arguments.get("old", ""))), parsed[0].lines, lines
            )
            if isinstance(rescue, Failure):
                return self._failed(
                    _describe(rescue),
                    rescue.kind,
                    started,
                    op=rescue.op_index,
                    repaired=",".join(sorted(repair_kinds)),
                )
            resolved = rescue.resolved
            repair_kinds.add(rescue.name)
            notes.append(rescue.note)

        assert isinstance(resolved, Resolved)
        notes.extend(resolved.notes)
        repair_kinds.update(resolved.repairs)

        before_text = "\n".join(lines) + "\n"
        before = verify.check(path, before_text)
        updated = apply(lines, resolved)
        # A CR the model sent inside a line is a line break once the file is read back, so
        # it is treated as one here and never written as a stray CR.
        text = to_lf("\n".join(updated) + ("\n" if updated else ""))
        # A `lines` item may itself hold line breaks, so the file's lines are counted and
        # numbered from the text, never from the items.
        final = text[:-1].split("\n") if text else []

        # Before the parse check: a placeholder often breaks the parse too, and "you left
        # code out" is the message that says what to do about it (`elision.py`).
        if found := elision.introduced(to_lf(raw_before), text):
            first = found[0]
            return self._failed(
                f"Nothing was written. Your new text puts a placeholder where code belongs; "
                f"line {first.line} of the file would read:\n  {first.text}\n"
                f"edit writes your text into the file exactly as sent, so the code that line "
                f"stands for would be deleted. Send the real lines, or make the edit cover "
                f"only the lines that change and leave the rest of the file out of it.",
                "omission_placeholder",
                started,
                repaired=",".join(sorted(repair_kinds)),
            )
        if (
            whole
            and (refused := mass_removal(path, raw_before, text, self._tracker, "edit")) is not None
        ):
            return self._refused(refused, started, repaired=",".join(sorted(repair_kinds)))

        after = verify.check(path, text)
        # A `lines` array longer than its span gets only an explanatory note below
        # (`diagnose.mismatched_lines_note`), never an automatic rejoin. A rejoin fixed none
        # of the archived calls it could apply to, and one that parses can still misread a
        # genuinely broken multi-line string (strict re-review of edit-anchor-leniency,
        # item 3).
        #
        # This process's Python is the harness's, not the project's (see `verify`): a
        # construct newer than the harness is a syntax error here and valid there. Only
        # when this process cannot parse the result does the project's interpreter decide,
        # so an edit that parses here never costs a subprocess. When both reject it, the
        # model reads this process's error.
        deferred = False
        if after is not None and not after.ok and after.checker == "python":
            project_after = verify.project_python(text, cwd=context.project_root, env=self._env)
            if project_after is not None and project_after.ok:
                deferred, after = True, project_after
            elif project_after is not None and before is not None and not before.ok:
                # Unparseable here before the edit too, but perhaps not for the project.
                # If it parsed there, this edit broke it, and the project's interpreter says
                # where: this process's error may name a line that is valid.
                project_before = verify.project_python(
                    before_text, cwd=context.project_root, env=self._env
                )
                if project_before is not None and project_before.ok:
                    deferred, before, after = True, project_before, project_after
        if before is not None and before.ok and after is not None and not after.ok:
            where = f" at line {after.line}" if after.line else ""
            return self._failed(
                f"that edit would break the file{where}: {after.message}\n"
                f"Nothing was written. This is how the file would have looked:\n\n"
                f"{verify.window(final, after.line)}"
                f"{_stray_json_note(final, after.line, path)}"
                f"{diagnose.mismatched_lines_note(resolved)}",
                "would_not_parse",
                started,
                repaired=",".join(sorted(repair_kinds)),
                deferred=deferred,
            )

        on_disk = restore(text, ending_for(path, raw_before))
        entry = None
        written = False
        try:
            entry = self._undo.preserve_overwrite(path)
            write_exact(path, on_disk)
            written = True
            # Hashed from the file on disk after the write, never from the string (strict
            # review, app-b2-rewind, item 9).
            self._undo.note_written(entry.key, path)
        except OSError as exc:
            # A snapshot for a write that never happened is withdrawn, not left to undo.
            if entry is not None and not written:
                self._undo.discard(entry.key)
            return self._failed(unwritable_message(path, exc), "unwritable", started)

        self._tracker.note_read(path)
        if whole:
            # The model sent every line of it, so it has the whole file, as after `write`.
            self._tracker.note_whole(path)
        else:
            self._tracker.note_edited(path, raw_before)
        changed = sum(1 for op in resolved.ops for _ in range(max(1, op.end - op.start)))
        repaired_by = ",".join(sorted(repair_kinds))
        summary = [
            f"Edited {path}: {len(resolved.ops)} change(s), "
            f"{len(final) - len(lines):+d} lines. Undo with `cogito undo {entry.key}`."
        ]
        summary.extend(f"  {note}" for note in notes[:_MAX_NOTES])
        if after is not None and not after.ok:
            summary.append(
                f"  Note: the file still does not parse at line {after.line} — "
                f"{after.message}. It did not parse before this edit either."
            )
        if repaired_by:
            # Outside `notes[:_MAX_NOTES]` on purpose: a model that never sees which
            # leniency saved its call cannot learn to send the canonical form.
            summary.append(f"  (repaired: {repaired_by})")

        return ToolResult(
            content="\n".join(summary),
            metadata={
                "format": self._format.name,
                "ops": len(resolved.ops),
                "lines_touched": changed,
                "repaired_by": repaired_by,
                "drifted": sum(1 for op in resolved.ops if op.drift),
                "ok": True,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "undo_key": entry.key,
                # The file's text before and after, for the desktop app's per-turn diff
                # (loop.changes). Metadata only, so it never reaches the model, and the
                # engine keeps only a hash of it past this turn.
                "before": raw_before,
                "after": on_disk,
                **verify.deferred_metadata(deferred),
            },
            files_touched=(str(path),),
        )

    def _failed(
        self,
        message: str,
        kind: str,
        started: float,
        *,
        op: int | None = None,
        repaired: str = "",
        deferred: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            content=message,
            is_error=True,
            error_kind=kind,
            metadata={
                "format": self._format.name,
                "failure_kind": kind,
                "op_index": op if op is not None else -1,
                "repaired_by": repaired,
                "ok": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                **verify.deferred_metadata(deferred),
                **(extra or {}),
            },
        )

    def _refused(self, refused: ToolResult, started: float, *, repaired: str = "") -> ToolResult:
        """One of `write`'s refusals as this tool's failure: its text and kind, plus the
        metadata every failed edit carries."""
        return self._failed(
            refused.content,
            refused.error_kind,
            started,
            repaired=repaired,
            extra=refused.metadata,
        )


#: Name -> format. The value of `edit_format` in config.toml selects one.
def build_format(name: str) -> EditFormat:
    from cogito.tools.edit.formats.hashline import HashLineFormat
    from cogito.tools.edit.formats.str_replace import StrReplaceFormat
    from cogito.tools.edit.formats.whole_file import WholeFileFormat

    formats: dict[str, type] = {
        "hashline": HashLineFormat,
        "str_replace": StrReplaceFormat,
        "whole_file": WholeFileFormat,
    }
    try:
        return formats[name]()  # type: ignore[return-value]
    except KeyError as exc:
        raise ValueError(
            f"unknown edit format {name!r}; known: {', '.join(sorted(formats))}"
        ) from exc
