"""Reading and writing whole files.

`read` renders through the active edit format, so the labels it shows are the ones `edit`
accepts. Because read and edit share one addressing scheme, the format is a single
configuration key rather than two kept in step.

A blind read of a four-thousand-line file would flood the context, so it cannot happen: a
default read past `MAP_THRESHOLD` lines returns a map of the file's declarations plus its
opening, and the model asks for the part it wants.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
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
from cogito.tools import document_cache, documents
from cogito.tools.base import ToolContext, ToolResult, parameters
from cogito.tools.edit import elision
from cogito.tools.edit.formats.base import EditFormat
from cogito.tools.edit.verify import check, deferred_metadata, project_python
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

#: What `write` says when its path is a directory. A directory cannot be written, and
#: removing one is a deletion the guard asks about, so the way forward is another path.
_DIRECTORY_NOT_WRITTEN = (
    "Nothing was written: a directory cannot be written as a file. Name a file instead, "
    "inside it or with another name."
)

DEFAULT_LIMIT = 300
MAX_LIMIT = 2000

#: Past this, the file is mapped rather than dumped.
MAP_THRESHOLD = 1500
MAP_HEAD_LINES = 120

_NULL_SAMPLE = 8192

#: Declaration-looking lines, for the map. A rough syntactic match across the languages
#: this is likely to meet; the code graph replaces it with a real parse.
_DECLARATION = re.compile(
    r"^\s*(?:"
    r"(?:async\s+)?def\s+\w+|class\s+\w+|"
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+|"
    r"(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?\(|"
    r"(?:pub\s+)?(?:async\s+)?fn\s+\w+|struct\s+\w+|impl\b|trait\s+\w+|enum\s+\w+|"
    r"func\s+\w+|type\s+\w+\s+struct|"
    r"(?:public|private|protected|static|final|abstract|\s)*(?:class|interface)\s+\w+"
    r")"
)


@dataclass(frozen=True, slots=True)
class ReadSnapshot:
    """What a `read` of a file showed, so a later `edit` can tell whether a plain line
    number (no hash) is still safe: only when the file is byte-identical to this and the
    line falls inside `shown_from`-`shown_to`."""

    #: The file's exact text at read time, for a byte-identical comparison later.
    content: str
    #: 1-based, inclusive: the lines this read rendered to the model.
    shown_from: int
    shown_to: int


@dataclass(frozen=True, slots=True)
class _View:
    """The lines of one exact text the model has been shown, merged, 1-based inclusive."""

    content: str
    shown: tuple[tuple[int, int], ...]


def _line_count(text: str) -> int:
    """How many lines `read` numbers in ``text`` (see `read_lines`): CRLF and CR count as
    line breaks there, so they do here."""
    return len(split_lines(to_lf(text)))


def _merge(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


@dataclass
class FileTracker:
    """Which files this session has looked at.

    Overwriting a file nobody read is how an agent destroys work it did not know was there.
    One rule, no permission system: read it first.
    """

    seen: set[Path] = field(default_factory=set)
    #: The most recent read of each path, while it is still trusted. A `note_read` call
    #: without content clears it (a write, an edit's post-write bookkeeping, a read that may
    #: have been clipped): nothing trusted was shown of the current text.
    _snapshots: dict[Path, ReadSnapshot] = field(default_factory=dict)
    #: Everything the model has been shown of each file's current text: the lines of every
    #: unclipped read of that same text, or all of it when the model wrote it. A `write`
    #: or whole_file `edit` that removes much of an existing file needs all of it here
    #: (`mass_removal`).
    _views: dict[Path, _View] = field(default_factory=dict)

    def note_read(
        self,
        path: Path,
        *,
        content: str | None = None,
        shown: tuple[int, int] | None = None,
    ) -> None:
        resolved = path.resolve()
        self.seen.add(resolved)
        if content is not None and shown is not None:
            self._snapshots[resolved] = ReadSnapshot(content, shown[0], shown[1])
            view = self._views.get(resolved)
            earlier = list(view.shown) if view is not None and view.content == content else []
            self._views[resolved] = _View(content, _merge([*earlier, shown]))
        else:
            self._snapshots.pop(resolved, None)

    def note_whole(self, path: Path) -> None:
        """The model has the whole of ``path`` as it is on disk now: it wrote it.

        Read from the file rather than the string that produced it, the way `read` and
        `write` read it (`read_exact`, line endings kept): the file's own ending is put back
        on write, and `unseen` compares against the file's exact text.
        """
        resolved = path.resolve()
        try:
            content = read_exact(resolved)
        except OSError:
            self._views.pop(resolved, None)
            return
        total = _line_count(content)
        self._views[resolved] = _View(content, ((1, total),) if total else ())

    def note_edited(self, path: Path, before: str) -> None:
        """An edit changed ``path`` from ``before``. The model had the whole of the result
        only if it had the whole of ``before``: it knows that and its own change."""
        if self.unseen(path, before) == ():
            self.note_whole(path)
        else:
            with suppress(OSError):
                self._views.pop(path.resolve(), None)

    def unseen(self, path: Path, content: str) -> tuple[tuple[int, int], ...] | None:
        """The lines of ``content`` (``path``'s current text) never shown to the model, as
        ranges (empty when it has seen all of it), or None when nothing it was shown is of
        this exact text: never read, changed since, or forgotten in a fold."""
        try:
            view = self._views.get(path.resolve())
        except OSError:
            return None
        if view is None or view.content != content:
            return None
        missing: list[tuple[int, int]] = []
        line = 1
        for start, end in view.shown:
            if start > line:
                missing.append((line, start - 1))
            line = max(line, end + 1)
        total = _line_count(content)
        if line <= total:
            missing.append((line, total))
        return tuple(missing)

    def was_read(self, path: Path) -> bool:
        try:
            return path.resolve() in self.seen
        except OSError:
            return False

    def read_snapshot(self, path: Path) -> ReadSnapshot | None:
        """The most recent read of `path` this session still trusts, or `None` if it was
        never read, or was invalidated by a write or an edit since."""
        try:
            return self._snapshots.get(path.resolve())
        except OSError:
            return None

    def clear_snapshots(self) -> None:
        """Drop every read snapshot and view, keeping `seen` (write's overwrite guard).

        Called on compaction and on rewind (strict review of edit-anchor-leniency, item 4).
        After a fold the model no longer has the read's text in front of it, and a rewind
        can change a file without going through `read`/`write`/`edit`. Either way this
        tracker can no longer say that the file is unchanged since the model last read it,
        or what the model has in front of it.
        """
        self._snapshots.clear()
        self._views.clear()


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(_NULL_SAMPLE)
    except OSError:
        return False


def _unreadable_document(path: Path, found: documents.Unreadable | None) -> ToolResult:
    """Why a document's text cannot be read, and what to do instead (CONTRIBUTING §6.4)."""
    if found is None:
        return ToolResult.error(f"{path} could not be read as a document.", "document_corrupt")
    name = documents.NAMES.get(found.kind, "document")
    detail = f" ({found.detail})" if found.detail else ""
    if found.reason == "encrypted":
        text = (
            f"{path} is a password-protected {name}{detail}: its text cannot be read. Ask "
            "the user for a copy without the password."
        )
    elif found.reason == "scanned":
        text = (
            f"{path} is a {name} with almost no text in it{detail}. Nothing here reads "
            "images, so its contents are unknown: ask the user for a text version, and do "
            "not guess what it says."
        )
    elif found.reason == "too_large":
        text = (
            f"{path} is a {name} too large to convert{detail}. Ask the user for the part "
            "that matters."
        )
    else:
        text = (
            f"{path} looks like a {name} but cannot be read{detail}. It may be damaged; "
            "ask the user for another copy."
        )
    return ToolResult.error(text, f"document_{found.reason}")


def read_lines(path: Path) -> list[str]:
    """The file's lines as `read` shows them: CRLF and CR read as LF (`line_endings`)."""
    return split_lines(to_lf(read_exact(path)))


def outline(lines: list[str]) -> str:
    """What is declared in this file, so a large one can be navigated instead of read."""
    found = [
        f"{index + 1}: {line.strip()[:110]}"
        for index, line in enumerate(lines)
        if _DECLARATION.match(line)
    ]
    return "\n".join(found[:200])


class ReadTool:
    def __init__(
        self,
        fmt: EditFormat,
        tracker: FileTracker,
        convert: Callable[[Path], Any] = document_cache.extract,
    ) -> None:
        self._format = fmt
        self._tracker = tracker
        #: Turns a document into its text through the user's conversion cache
        #: (`tools.document_cache`), so paging through a document converts it once.
        self._convert = convert
        self.spec = ToolSpec(
            name="read",
            #: A view of a file that something may rewrite. Bulky, but sometimes the only
            #: evidence left: measured on a file read once and overwritten afterwards.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.BULK,
            ),
            description=(
                "Read a file. Line labels are what edit's anchors refer to. A large "
                "file returns a map plus its opening; use offset and limit for the rest."
            ),
            parameters=parameters(
                {
                    "path": {"type": "string", "description": "File to read."},
                    "offset": {
                        "type": "integer",
                        "description": "First line, 1-based. Default 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Line count. Default {DEFAULT_LIMIT}.",
                    },
                },
                required=("path",),
            ),
            aliases={
                "path": ("file_path", "filename", "file", "filepath"),
                "offset": ("start", "start_line", "from_line", "line"),
                "limit": ("count", "num_lines", "lines", "end_line"),
            },
            budget_tokens=6000,
            core=True,
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw = str(arguments.get("path", "")).strip()
        if not raw:
            return ToolResult.error("path is required", "missing_path")
        path = resolve_path(context.project_root if context else None, raw)

        if path.is_dir():
            return ToolResult.error(
                directory_message(
                    context.project_root, path, otherwise="Use glob to list it, or name a file."
                ),
                "is_directory",
            )
        if not path.exists():
            return ToolResult.error(
                missing_file_message(
                    context.project_root,
                    path,
                    then="use glob to search.",
                    otherwise="Use glob to find it if you are unsure of the path.",
                ),
                "not_found",
            )
        # A PDF, Word, Excel, PowerPoint or OpenDocument file is read as its text
        # (`tools.documents`); any other file is read as plain text below.
        if documents.kind_of(path) is not None:
            return self._document(path, arguments)
        if _looks_binary(path):
            return ToolResult.error(
                f"{path} is a binary file and cannot be read as text.", "binary"
            )

        try:
            # The file's exact text, line endings included, for the read snapshot `edit`
            # checks a later plain line number against. `lines` loses both the endings and
            # the trailing-newline shape (see `EditTool`'s `raw_before`).
            raw_text: str | None = read_exact(path)
        except OSError as exc:
            return ToolResult.error(f"could not read {path}: {exc}", "unreadable")
        lines = split_lines(to_lf(raw_text))

        total = len(lines)
        offset = max(1, int(arguments.get("offset") or 1))
        asked_for_limit = arguments.get("limit") is not None
        limit = min(int(arguments.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)

        if total > MAP_THRESHOLD and offset == 1 and not asked_for_limit:
            return self._map(path, lines, total, raw_text, context)

        window = lines[offset - 1 : offset - 1 + limit]
        if not window:
            self._tracker.note_read(path)
            return ToolResult.error(
                f"{path} has {total} lines; line {offset} is past the end.", "out_of_range"
            )

        shown_to = offset - 1 + len(window)
        header = f"{path}  ({total} lines"
        if offset > 1 or shown_to < total:
            header += f", showing {offset}-{shown_to}"
        header += ")"
        content = f"{header}\n{self._format.render(window, start=offset)}"
        self._note_read_if_unclipped(path, content, raw_text, (offset, shown_to), context)
        return ToolResult(
            content=content,
            metadata={"lines": total, "shown": len(window), "format": self._format.name},
            files_touched=(str(path),),
        )

    def _document(self, path: Path, arguments: Mapping[str, Any]) -> ToolResult:
        """A document as its text, a window of lines at a time like any file, under a
        header saying what it is (its pages, sheets or slides, each also marked in the text).
        Its lines carry no labels and no read is recorded: the text is a rendering, and
        `edit` or `write` must never take it for the file."""
        found = self._convert(path)
        if isinstance(found, documents.Unreadable) or found is None:
            return _unreadable_document(path, found)
        lines = found.text.rstrip("\n").split("\n")
        total = len(lines)
        offset = max(1, int(arguments.get("offset") or 1))
        limit = min(int(arguments.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)
        window = lines[offset - 1 : offset - 1 + limit]
        if not window:
            return ToolResult.error(
                f"{path} is a {found.describe()} whose text has {total} lines; line "
                f"{offset} is past the end.",
                "out_of_range",
            )
        shown_to = offset - 1 + len(window)
        header = f"{path}  ({found.describe()}; its text, {total} lines"
        if offset > 1 or shown_to < total:
            header += f", showing {offset}-{shown_to}"
        header += ")"
        body = [header, *window]
        if shown_to < total:
            body.append(
                f"[lines {shown_to + 1}-{total} not shown: read(path, offset={shown_to + 1})]"
            )
        metadata: dict[str, Any] = {
            "lines": total,
            "shown": len(window),
            "document": found.kind,
        }
        if found.pages is not None:
            metadata["pages"] = found.pages
        if found.sheets:
            metadata["sheets"] = list(found.sheets)
        return ToolResult(
            content="\n".join(body), metadata=metadata, files_touched=(str(path),)
        )

    def _map(
        self, path: Path, lines: list[str], total: int, raw_text: str, context: ToolContext
    ) -> ToolResult:
        head = self._format.render(lines[:MAP_HEAD_LINES], start=1)
        declarations = outline(lines)
        body = [f"{path}  ({total} lines — too long to show at once)"]
        if declarations:
            body.append("\nWhat it declares:\n" + declarations)
        body.append(f"\nFirst {MAP_HEAD_LINES} lines:\n{head}")
        body.append(
            "\nRead any other part with read(path, offset, limit), or find a line with "
            "grep(pattern, path)."
        )
        content = "\n".join(body)
        head_to = min(MAP_HEAD_LINES, total)
        self._note_read_if_unclipped(path, content, raw_text, (1, head_to), context)
        return ToolResult(
            content=content,
            metadata={"lines": total, "mapped": True, "format": self._format.name},
            files_touched=(str(path),),
        )

    def _note_read_if_unclipped(
        self,
        path: Path,
        content: str,
        raw_text: str,
        shown: tuple[int, int],
        context: ToolContext,
    ) -> None:
        """Record a read snapshot only when this exact `content` is certain to reach the
        model whole.

        After the tool returns, `loop.engine` clips every result to the budget
        `Settings.budget_for` resolves for it, keeping head and tail
        (`context.budgets.clip`). A clipped read can drop lines `shown` claims were shown,
        and the tool cannot know which shrink step `clip` would land on without running it
        (which would stash a second, unused artifact). The one certain case is `clip`'s fast
        path: at or under budget means unclipped. Over budget, no snapshot is recorded (per
        the review finding) rather than a guessed range.
        """
        from cogito.provider import tokens

        budget = context.settings.budget_for(self.spec.name, self.spec.budget_tokens)
        if tokens.count(content) <= budget:
            self._tracker.note_read(path, content=raw_text, shown=shown)
        else:
            self._tracker.note_read(path)


class WriteTool:
    def __init__(
        self,
        tracker: FileTracker,
        undo: UndoStore,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._tracker = tracker
        self._undo = undo
        #: Where the project's own `python3` is found when this process cannot parse what
        #: was written (`verify.project_python`): the shell's environment, as `cli.py`
        #: builds it. None means this process's own.
        self._env = env
        self.spec = ToolSpec(
            name="write",
            #: A confirmation. Irreproducible and worth nothing later; the file is the record.
            provenance=Provenance(
                reproducibility=Reproducibility.EPHEMERAL,
                retention=Retention.ACKNOWLEDGEMENT,
                shape=PayloadShape.COMPACT,
            ),
            description=(
                "Create a file, or replace one completely. To change part of one, use "
                "edit — far cheaper. An existing file must have been read first."
            ),
            parameters=parameters(
                {
                    "path": {"type": "string", "description": "File to write."},
                    "content": {"type": "string", "description": "Its complete contents."},
                },
                required=("path", "content"),
            ),
            aliases={
                "path": ("file_path", "filename", "file", "filepath"),
                "content": ("contents", "text", "body", "file_text", "data"),
            },
            budget_tokens=400,
            mutates=True,
            summary="create a file, or replace one completely",
            verbatim=("content",),
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw = str(arguments.get("path", "")).strip()
        content = arguments.get("content")
        if not raw:
            return ToolResult.error("path is required", "missing_path")
        if not isinstance(content, str):
            return ToolResult.error("content must be the complete text of the file", "no_content")

        path = resolve_path(context.project_root if context else None, raw)

        if path.is_dir():
            # Saying only "is a directory" left a model resending the same write (QA
            # 2026-10-13: three times, after its own `mkdir -p tests/test_calc.py`).
            return ToolResult.error(
                directory_message(
                    context.project_root,
                    path,
                    otherwise=_DIRECTORY_NOT_WRITTEN,
                    then=_DIRECTORY_NOT_WRITTEN,
                ),
                "is_directory",
            )

        existed = path.exists()
        if existed and (refused := unread_overwrite(path, self._tracker, "write")) is not None:
            return refused

        # Captured before the write, for the desktop app's per-turn diff (loop.changes).
        # `None` for a new file, which the diff renders as a create rather than a change
        # against empty text. Metadata only: it is never in `content`, so the model never
        # sees it, and the engine keeps only its hash past this turn.
        before_text: str | None = None
        if existed:
            try:
                before_text = read_exact(path)
            except OSError:
                before_text = None

        # Every check below and the write itself work on LF text (`line_endings`): the
        # endings the model sent are normalized, never written as they came.
        content = to_lf(content)

        if (refused := self._drops_code(path, content, before_text, existed)) is not None:
            return refused

        # Verified before the write, on the same LF text, though it only adds a warning.
        # The order is refusal, verification, write, as in `edit`.
        note = ""
        deferred = False
        if (result := check(path, content)) is not None and not result.ok:
            # This process's Python is not the project's (see `verify`): newer syntax can
            # fail here and be valid there, so the project's interpreter decides. Unless it
            # accepts the text, the warning is this process's own.
            if result.checker == "python":
                project = project_python(content, cwd=context.project_root, env=self._env)
                deferred = project is not None and project.ok
            if not deferred:
                where = f" at line {result.line}" if result.line else ""
                note = f"\nWarning: this does not parse{where} — {result.message}"

        # A replaced file keeps its own line ending, and a new one gets LF unless its
        # neighbours say otherwise (`line_endings`).
        on_disk = restore(content, ending_for(path, before_text))
        # What was there is saved before the write. A new file is recorded only once it
        # exists, and a snapshot whose write then failed is withdrawn: an undo entry for a
        # change that never happened is worse than none.
        entry = None
        written = False
        try:
            if existed:
                entry = self._undo.preserve_overwrite(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_exact(path, on_disk)
            written = True
            if entry is None:
                entry = self._undo.preserve_create(path)
            # Hashed from the file after the write, never from the string; see
            # `note_written`'s docstring for why.
            self._undo.note_written(entry.key, path)
        except OSError as exc:
            if entry is not None and not written:
                self._undo.discard(entry.key)
            return ToolResult.error(unwritable_message(path, exc), "unwritable")

        self._tracker.note_read(path)
        self._tracker.note_whole(path)
        written = content.count("\n") + (0 if content.endswith("\n") or not content else 1)

        verb = "Wrote" if not existed else "Replaced"
        return ToolResult(
            content=f"{verb} {path} ({written} lines). Undo with `cogito undo {entry.key}`.{note}",
            metadata={
                "lines": written,
                "created": not existed,
                "undo_key": entry.key,
                "before": before_text,
                "after": on_disk,
                **deferred_metadata(deferred),
            },
            files_touched=(str(path),),
        )

    def _drops_code(
        self, path: Path, content: str, before: str | None, existed: bool
    ) -> ToolResult | None:
        """A refusal when this write would lose code without anyone deciding to, else None.

        Two cases (`tools/edit/elision.py` gives the source of each rule): a placeholder
        line standing in for code, and a rewrite that drops much of a file the model has
        not seen in full. A model that read every line and still sends a shorter file is
        deleting on purpose, and is let through.
        """
        # Compared in LF, as `content` is (a CR-only file is one line otherwise); what the
        # model was shown is looked up by the file's exact text, as `read` recorded it.
        old = to_lf(before) if before is not None else ""
        if found := elision.introduced(old, content):
            first = found[0]
            consequence = (
                "write replaces the whole file with exactly what you send, so the code "
                "that line stands for would be deleted. Send the complete file with every "
                "line written out, or use edit to change only the lines that need to change."
                if existed
                else "write puts exactly what you send into the file, so that line would be "
                "there instead of the code. Write the code out in full."
            )
            return ToolResult.error(
                f"Nothing was written. Line {first.line} of your content is a placeholder, "
                f"not code:\n  {first.text}\n{consequence}",
                "omission_placeholder",
                line=first.line,
                placeholder=first.text,
            )
        if before is None:
            return None
        return mass_removal(path, before, content, self._tracker, "write")


def unread_overwrite(path: Path, tracker: FileTracker, tool: str) -> ToolResult | None:
    """A refusal when ``tool`` would replace the existing ``path`` unread, else None.

    ``tool`` is `write`, or `edit` in the whole_file format, which also replaces a whole
    file. Only `write` points to a partial edit: in a whole_file session there is none.
    """
    if tracker.was_read(path):
        return None
    then = "then write — or use edit to change part of it" if tool == "write" else f"then {tool}"
    return ToolResult.error(
        f"{path} already exists and has not been read in this session. Read it first, {then}.",
        "unread_overwrite",
    )


def mass_removal(
    path: Path, before: str, content: str, tracker: FileTracker, tool: str
) -> ToolResult | None:
    """A refusal when ``tool`` replacing ``before`` (the file's exact text) with ``content``
    (LF) drops much of a file the model has not seen all of, else None. ``tool`` is as in
    `unread_overwrite`."""
    removal = elision.removal(to_lf(before), content)
    if not removal.excessive:
        return None
    unseen = tracker.unseen(path, before)
    if unseen == ():
        return None
    where = ""
    if unseen:
        spans = ", ".join(f"{a}-{b}" if a != b else f"{a}" for a, b in unseen)
        where = f" (never shown to you: lines {spans}; read with offset={unseen[0][0]})"
    instead = ""
    if tool == "write":
        instead = ", or use edit to change only the lines that need to change"
    return ToolResult.error(
        f"Nothing was written. {path} has {removal.old_lines} non-blank lines and your "
        f"content keeps at most {removal.kept} of them, but you have not seen all of the file "
        f"as it is now{where}. {tool} replaces the whole file, so whatever you have not "
        f"seen and did not send is lost. Read the whole file first and then send its "
        f"complete new content{instead}.",
        "mass_removal",
        old_lines=removal.old_lines,
        kept=removal.kept,
        share=str(removal.share),
    )
