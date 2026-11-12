"""Per-tool output ceilings, applied in one place.

Left alone, tool observations grow to 70-80% of the window, so clipping is not left to
each tool: the loop runs every result through here. A tool cannot opt out and cannot
forget.

What is kept is head plus tail. The start of a command's output says what it did, the end
says how it went, and the middle is mostly noise. What is cut goes to an artifact, and the
pointer travels with the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cogito.context import inbound
from cogito.context.offload import ArtifactRef, ArtifactStore

DEFAULT_HEAD_LINES = 80
DEFAULT_TAIL_LINES = 80

#: Never keep fewer lines than this per side: two lines of context are useless.
_MIN_SIDE_LINES = 5


@dataclass(frozen=True, slots=True)
class Clipped:
    content: str
    artifact: ArtifactRef | None
    original_tokens: int
    kept_tokens: int
    elided_lines: int

    @property
    def was_clipped(self) -> bool:
        return self.artifact is not None


def _pointer(reference: ArtifactRef, elided: int) -> str:
    kilobytes = reference.bytes / 1024
    return (
        f"\n... {elided:,} lines elided ({kilobytes:,.1f} KB). "
        f"Full output: {reference.path}\n"
        f"Read it with read(path, offset, limit) or search it with grep(pattern, path).\n"
    )


def clip(
    content: str,
    *,
    budget_tokens: int,
    store: ArtifactStore | None = None,
    kind: str = "text",
    head_lines: int = DEFAULT_HEAD_LINES,
    tail_lines: int = DEFAULT_TAIL_LINES,
) -> Clipped:
    """Fit ``content`` into ``budget_tokens``, offloading whatever does not fit.

    Whole lines are kept while that fits. A result of a few very long lines (an MCP
    server's JSON on one line, a minified file) cannot fit by lines, so then characters
    are kept from both ends instead (`_clip_chars`). Every count is `inbound.size`: the word
    counter takes a long run of hex or base64 for one word, and a 520,000-character hex line
    passed as 517 tokens."""
    original = inbound.size(content)
    if original <= budget_tokens:
        return Clipped(content, None, original, original, 0)

    if store is None:
        # Nowhere to offload: cut anyway rather than blow the window, and say so.
        lines = content.splitlines()
        kept = lines[:head_lines] + ["... output truncated, no artifact store available"]
        text = "\n".join(kept)
        if inbound.size(text) > budget_tokens:
            text = content[: max(1, budget_tokens) * 2]
            text += "\n... output truncated, no artifact store available"
            return Clipped(text, None, original, inbound.size(text), 0)
        return Clipped(text, None, original, inbound.size(text), max(0, len(lines) - head_lines))

    reference = store.stash(content, kind=kind)
    clipped = _clip_lines(content, budget_tokens, reference, original, head_lines, tail_lines)
    pointer = inbound.size(_pointer(reference, max(1, clipped.elided_lines)))
    if clipped.kept_tokens <= budget_tokens + pointer:
        return clipped
    return _clip_chars(content, budget_tokens, reference, original)


def _clip_lines(
    content: str,
    budget_tokens: int,
    reference: ArtifactRef,
    original: int,
    head_lines: int,
    tail_lines: int,
) -> Clipped:
    """Head and tail lines, halved until they fit or reach `_MIN_SIDE_LINES` a side."""
    lines = content.splitlines()
    head, tail = head_lines, tail_lines

    while True:
        elided = len(lines) - head - tail
        if elided <= 0:
            text = "\n".join(lines)
            return Clipped(text, reference, original, inbound.size(text), 0)

        text = "\n".join(lines[:head]) + _pointer(reference, elided) + "\n".join(lines[-tail:])
        kept = inbound.size(text)
        if kept <= budget_tokens or (head <= _MIN_SIDE_LINES and tail <= _MIN_SIDE_LINES):
            return Clipped(text, reference, original, kept, elided)

        head = max(_MIN_SIDE_LINES, head // 2)
        tail = max(_MIN_SIDE_LINES, tail // 2)


def _clip_chars(content: str, budget_tokens: int, reference: ArtifactRef, original: int) -> Clipped:
    """Characters from both ends, shrunk until they fit. The first try keeps about four
    characters per token of budget; dense text such as JSON has fewer, and when both ends
    would cover the whole of it they are cut to a quarter of it each, never kept whole."""
    side = max(1, budget_tokens) * 2
    while True:
        if 2 * side >= len(content):
            if side <= 1:
                return Clipped(content, reference, original, original, 0)
            side = len(content) // 4 or 1
            continue
        head, tail = content[:side], content[-side:]
        kept = inbound.size(head) + inbound.size(tail)
        if kept <= budget_tokens or side <= 1:
            text = head + _char_pointer(reference, len(content) - 2 * side) + tail
            return Clipped(text, reference, original, inbound.size(text), 0)
        side = max(1, side * 3 // 4)


def _char_pointer(reference: ArtifactRef, elided: int) -> str:
    kilobytes = reference.bytes / 1024
    return (
        f"\n... {elided:,} characters elided ({kilobytes:,.1f} KB). "
        f"Full output: {reference.path}\n"
        f"Read it with read(path, offset, limit) or search it with grep(pattern, path).\n"
    )


#: The pointer `_pointer` and `_char_pointer` write: where the whole output was saved.
_FULL_OUTPUT = re.compile(
    r"\n\.\.\. [\d,]+ (?:lines|characters) elided \([\d,.]+ KB\)\. Full output: (.+)\n"
)


def _whole(content: str, store: ArtifactStore | None) -> str | None:
    """The whole output a clipped ``content`` points to, when that file is in ``store``."""
    found = _FULL_OUTPUT.search(content)
    if found is None or store is None:
        return None
    path = Path(found.group(1))
    try:
        if path.parent.resolve() != store.directory.resolve():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def reclip(
    content: str, *, budget_tokens: int, store: ArtifactStore | None, kind: str = "text"
) -> Clipped:
    """Clip ``content`` again, to about ``budget_tokens`` plus the pointer, for a result
    already in the window that a refused request needs smaller. Whatever is cut is saved
    whole, as `clip` saves it. A result clipped before is clipped again from the whole
    output its pointer names, so the new pointer still leads to all of it."""
    content = _whole(content, store) or content
    return clip(content, budget_tokens=budget_tokens, store=store, kind=kind)


def collapse_repeats(content: str, *, threshold: int = 3) -> str:
    """Fold runs of an identical line into one line plus a count.

    A progress bar or a retry loop can print thousands of identical lines that carry one
    line of information.
    """
    lines = content.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        run = 1
        while index + run < len(lines) and lines[index + run] == lines[index]:
            run += 1
        out.append(lines[index])
        if run >= threshold:
            out.append(f"... previous line repeated {run - 1:,} times")
        elif run > 1:
            out.extend([lines[index]] * (run - 1))
        index += run
    return "\n".join(out)
