"""APP-ONLY: the files a fold hands back, as they are on disk when the conversation folds.

The handover says what was done and not what the files now contain, so the next steps of a
folded conversation re-read every file it had just edited. In the person's real session
(2026-10-12, seven `src/data/parts/*.lua` files) that was a grep, an ls and seven `read`
calls straight after the fold. The harness holds which files were read or edited, so it
hands them back; the model is asked nothing.

What is restored: the files the session read or edited most recently, newest first, read
from disk at fold time, in the form `read` shows (labels included, so `edit` anchors work).
At most `MAX_FILES` of them, and at most a quarter of the window in all (`inbound.SHARE`).
A file that does not fit whole is a path pointer, never a cut body. A file that is gone, a
binary file, a document that `read` converts and a directory are left out.

Sources (CONTRIBUTING 6b, read 2026-10-12; their numbers are theirs, not reproduced here):

- Claude Code re-reads the files it modified most recently after a compaction
  (code.claude.com/docs/en/context-window). The figures usually quoted for it, 5 files and
  5,000 tokens each within 50,000, come from third-party readings of its source (the
  compact-guard skill, a Towards AI article), not from Anthropic's documentation.
- Codex CLI rebuilds its history from the user messages (up to 20,000 tokens,
  `COMPACT_USER_MESSAGE_MAX_TOKENS` in codex-rs/core/src/compact.rs) plus the summary. It
  does not re-inject file contents.
- OpenHands' summarizing condenser keeps the first events and the latest ones and summarizes
  the middle; it keeps no file state beyond what the summary says
  (docs.openhands.dev/sdk/arch/condenser).

Cogito's budget is Claude Code's idea (the files just touched) under this project's own
rule for one message: a quarter of the window (`context.inbound`). It is a candidate for the
CONTRIBUTING 7 rule, to be measured later with the recall probes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

from cogito.context import inbound
from cogito.provider.base import Message, ToolResultBlock, ToolUseBlock
from cogito.tools import documents
from cogito.tools.edit.formats.base import EditFormat
from cogito.tools.files import FileTracker, _looks_binary
from cogito.tools.line_endings import read_exact, split_lines, to_lf

log = logging.getLogger(__name__)

#: The tools whose successful calls name a file the model has seen or written.
FILE_TOOLS = frozenset({"read", "edit", "write"})

#: Files a fold may restore, inline or as a pointer.
MAX_FILES = 5

#: Paths remembered. Older ones are forgotten first.
_REMEMBERED = 64

#: A file over this many bytes is not read at all: it cannot fit, and counting its lines
#: is not worth the memory. It is a pointer without a line count.
_NOT_READ_BYTES = 2_000_000

_CAPTION = (
    "Files this conversation read or edited most recently, as they are on disk now (read "
    "when the conversation was folded, so every edit made so far is in them), most recent "
    "first. Each is shown the way `read` shows a file:"
)


class FoldFiles:
    """Which files the session touched, and what a fold hands back of them.

    ``tracker`` is the one `read`/`write`/`edit` share. `render` reads the files and keeps
    what it showed; `commit` marks those files read once the fold has cleared the tracker's
    snapshots (the ON_COMPACT hook), so `edit`/`write` need no second read.
    """

    def __init__(
        self,
        fmt: EditFormat,
        tracker: FileTracker,
        project_root: Path,
        *,
        max_files: int = MAX_FILES,
    ) -> None:
        self._format = fmt
        self._tracker = tracker
        self._root = project_root
        self._max_files = max_files
        #: Oldest first. A path touched again moves to the end.
        self._recent: dict[Path, None] = {}
        self._shown: list[tuple[Path, str, int]] = []

    # -- which files -----------------------------------------------------------------

    def reset(self) -> None:
        """Forget every remembered file: a reset (`Engine.start_fresh`) begins a new
        conversation that has touched none."""
        self._recent.clear()
        self._shown = []

    def note(self, path: str | Path) -> None:
        """``path`` was read, written or edited successfully just now."""
        resolved = self._resolve(path)
        self._recent.pop(resolved, None)
        self._recent[resolved] = None
        while len(self._recent) > _REMEMBERED:
            del self._recent[next(iter(self._recent))]

    def recent(self) -> Iterator[Path]:
        """The remembered files, newest first."""
        return reversed(list(self._recent))

    def replay(self, messages: Sequence[Message]) -> None:
        """Rebuild the list from a restored transcript: its successful `read`, `edit` and
        `write` calls, in order. What a fold already handed back is not in it, so a
        conversation reopened right after a fold restores only what it touched since."""
        calls: dict[str, ToolUseBlock] = {}
        for message in messages:
            for block in message.blocks:
                if isinstance(block, ToolUseBlock) and block.name in FILE_TOOLS:
                    calls[block.id] = block
                elif isinstance(block, ToolResultBlock) and not block.is_error:
                    call = calls.get(block.tool_use_id)
                    target = call.input.get("path") if call is not None else None
                    if isinstance(target, str) and target.strip():
                        self.note(target.strip())

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        try:
            return candidate.resolve()
        except OSError:
            return candidate

    # -- what a fold hands back -------------------------------------------------------

    def render(self, window_tokens: int) -> str:
        """The block for a fold's seed, or "" when there is nothing to hand back. Never
        raises: a restore that fails leaves the fold as it would have been without one."""
        self._shown = []
        try:
            return self._render(window_tokens)
        except Exception as exc:  # the fold must not depend on this
            log.warning("the fold's file restore failed, folding without it: %s", exc)
            self._shown = []
            return ""

    def _render(self, window_tokens: int) -> str:
        budget = inbound.limit_tokens(window_tokens)
        if not budget:
            return ""
        used = inbound.size(_CAPTION)
        blocks: list[str] = []
        shown: list[tuple[Path, str, int]] = []
        for path in self._candidates():
            try:
                if path.stat().st_size > _NOT_READ_BYTES:
                    blocks.append(f"{path}  (too long to include here, read it when you need it)")
                    continue
                text = read_exact(path)
            except OSError:
                continue
            lines = split_lines(to_lf(text))
            header = f"{path}  ({len(lines)} lines)"
            body = self._format.render(lines, start=1)
            block = f"{header}\n{body}" if body else header
            cost = inbound.size(block)
            if used + cost > budget:
                blocks.append(
                    f"{path}  ({len(lines)} lines; too long to include here, "
                    "read it when you need it)"
                )
                continue
            used += cost
            blocks.append(block)
            shown.append((path, text, len(lines)))
        if not blocks:
            return ""
        self._shown = shown
        return "\n\n".join([_CAPTION, *blocks])

    def _candidates(self) -> list[Path]:
        """The newest files that can be shown as text, at most `max_files`."""
        found: list[Path] = []
        for path in self.recent():
            if len(found) >= self._max_files:
                break
            try:
                usable = (
                    path.is_file()
                    and documents.kind_of(path) is None
                    and not _looks_binary(path)
                )
            except OSError:
                usable = False
            if usable:
                found.append(path)
        return found

    def commit(self) -> None:
        """The fold used what `render` returned: those files are in front of the model in
        full, so mark them read (call after the fold has cleared the tracker's snapshots)."""
        for path, text, total in self._shown:
            self._tracker.note_read(path, content=text, shown=(1, total) if total else None)
        self._shown = []
