"""What one tool call produced, for the window's tool row to open onto.

Design 1a/1l: an «Editó» row expands to that edit's diff, an «Ejecutó» row to the
command's output. None of it is sent to the model. The detail is built from what the tool
returned before `context.budgets.clip` touched it, and it rides on ``tool_end`` and on the
call's journal record (so a row read back after a restart opens the same way). It is built
only when something listens (``Engine(on_tool_end=...)``, wired only by the server), so
``cogito chat`` and a benchmark never build, stash or journal it.

Shape, on ``tool_end`` and on ``GET /history``'s tool item alike::

    {"kind": "diff"|"output", "text": "<preview>", "lines": <lines in the whole>,
     "truncated": false|true, "ref": "<session>/<artifact id>"}

``diff`` is the call's own unified diff (`loop.changes.whole_diff`: 3 lines of context,
each line clipped to 400 characters). Its ``lines`` counts the lines a diff view shows
(added, removed and context), the same rule as the desktop's ``parseUnifiedDiff``.
``output`` is what the command printed: stdout and stderr as the shell tool reads them, as
one stream, ANSI stripped and repeated lines folded by the tool. It drops the header line
the tool adds for the model (``exit N  cwd /abs/path``): the exit code is the row's own
field, and the absolute path is the host's layout. Its ``lines`` counts its lines.

``text`` is the head of the whole, at most `PREVIEW_LINES` lines and `PREVIEW_BYTES`
bytes. When that is not all of it, ``truncated`` is true, the whole is written to the
session's artifacts (`context.offload.ArtifactStore`, as its own file, exactly the text
the window shows), and ``ref`` names it for ``GET /sessions/{id}/artifacts/{ref}``. A
failed write leaves ``ref`` out, and the row still opens onto its preview. A call with
nothing to show (a failed or binary edit, a command that printed nothing, a denied or
cancelled call) has no detail, and its row does not open.

The bounds: every harness compared keeps far more than it shows and fetches the rest on
demand (Claude Code shows 3 wrapped lines and ``ctrl+o``; Codex ``PREVIEW_LINES = 3``,
``MAX_PREVIEW_LINE_BYTES = 16 KiB``; OpenCode's TUI 10 lines, its tool output file past 2000
lines / 50 KiB; read 2026-09-30, cogito-lab ``experiments/2026-09-30-tool-detail/
NOTES.md``). How far the window collapses is up to the window (`desktop/.../diff.js`'s
``COLLAPSE_AT``); this bound only keeps one event small.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from cogito.context.offload import ArtifactStore
from cogito.loop.changes import PathChange, whole_diff

#: The most of a detail one event carries.
PREVIEW_LINES = 200
PREVIEW_BYTES = 16 * 1024

KINDS = ("diff", "output")
#: ``<session>/<artifact id>``: the artifact directory's own name and the file's stem.
REF = re.compile(r"^[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}$")
#: The shell tool's own first line (`tools/shell.py`): the harness talking to the model.
_SHELL_HEADER = re.compile(r"^exit -?\d+  cwd ")


def _cut_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8", "surrogatepass")[:limit].decode("utf-8", "ignore")


def _preview(text: str) -> str:
    """The head of ``text``: whole lines while they fit, or the first line cut at the byte
    bound when even that one does not."""
    kept: list[str] = []
    size = 0
    for line in text.split("\n")[:PREVIEW_LINES]:
        cost = len(line.encode("utf-8", "surrogatepass")) + (1 if kept else 0)
        if size + cost > PREVIEW_BYTES:
            if not kept:
                kept.append(_cut_bytes(line, PREVIEW_BYTES))
            break
        kept.append(line)
        size += cost
    return "\n".join(kept)


def _diff_lines(diff: str) -> int:
    """The lines a diff view shows: after the first hunk header, every added, removed and
    context line (``parseUnifiedDiff``'s rule), never the file headers, the hunk headers or
    a ``\\ No newline at end of file`` marker."""
    shown = 0
    seen_hunk = False
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if line.startswith("@@"):
            seen_hunk = True
            continue
        if seen_hunk and (line == "" or line[0] in "+- "):
            shown += 1
    return shown


def _build(kind: str, whole: str, lines: int, store: ArtifactStore | None) -> dict[str, Any]:
    text = _preview(whole)
    detail: dict[str, Any] = {"kind": kind, "text": text, "lines": lines, "truncated": False}
    if text == whole or text == whole.rstrip("\n"):
        return detail
    detail["truncated"] = True
    if store is not None:
        try:
            reference = store.stash(whole, kind=f"detail-{kind}")
        except OSError:
            return detail
        detail["ref"] = f"{reference.path.parent.name}/{reference.id}"
    return detail


def diff_detail(
    path: str, before: str | None, after: str | None, store: ArtifactStore | None
) -> dict[str, Any] | None:
    """An edit's or a write's own diff, or None when there is none to show."""
    if before is None and after is None:
        return None
    diff = whole_diff(path, PathChange(before=before, after=after))
    if not diff:
        return None
    lines = _diff_lines(diff)
    if not lines:
        return None
    return _build("diff", diff, lines, store)


def output_detail(content: str, store: ArtifactStore | None) -> dict[str, Any] | None:
    """A command's output from the shell tool's own result text, or None when it printed
    nothing."""
    head, _, body = content.partition("\n")
    if not _SHELL_HEADER.match(head):
        body = content
    body = body.rstrip("\n")
    if not body.strip():
        return None
    return _build("output", body, body.count("\n") + 1, store)


# -- the journal --------------------------------------------------------------------------


def to_properties(
    detail: Mapping[str, Any] | None, *, added: int | None = None, removed: int | None = None
) -> dict[str, Any]:
    """Flat properties for the call's ``tool_called`` record (the graph keeps only
    primitives on a node): ``detail_*`` for the detail, and an edit's or write's live
    ``+N``/``−M`` as ``lines_added``/``lines_removed``. Those are counted from text that
    is never stored, so a row read back could not show them otherwise."""
    properties: dict[str, Any] = {}
    if isinstance(added, int) and isinstance(removed, int):
        properties["lines_added"] = added
        properties["lines_removed"] = removed
    if not detail:
        return properties
    properties.update(
        {
            "detail_kind": detail["kind"],
            "detail_text": detail["text"],
            "detail_lines": detail["lines"],
            "detail_truncated": bool(detail.get("truncated")),
        }
    )
    if detail.get("ref"):
        properties["detail_ref"] = detail["ref"]
    return properties


def from_properties(properties: Mapping[str, Any]) -> dict[str, Any] | None:
    """The detail a call's record kept, as its live ``tool_end`` carried it. None for a
    record that kept none (written before details existed, or with no one listening) or
    whose fields are not what `to_properties` writes; such a row does not open."""
    kind = properties.get("detail_kind")
    text = properties.get("detail_text")
    lines = properties.get("detail_lines")
    if kind not in KINDS or not isinstance(text, str) or not text:
        return None
    if not isinstance(lines, int) or isinstance(lines, bool) or lines < 1:
        return None
    truncated = properties.get("detail_truncated")
    detail: dict[str, Any] = {
        "kind": kind,
        "text": text,
        "lines": lines,
        "truncated": truncated is True,
    }
    ref = properties.get("detail_ref")
    if detail["truncated"] and isinstance(ref, str) and REF.match(ref):
        detail["ref"] = ref
    return detail
