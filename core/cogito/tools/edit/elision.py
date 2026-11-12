"""Code the model leaves out: omission placeholders and mass removals.

Two habits destroy code without failing anything: a placeholder where code was
(``// ... existing code ...``), and a whole file sent back much shorter than the one it
replaces. Either applies cleanly, often still parses, and shows up steps later as a test
failure elsewhere, if at all. Both are checked before anything is written. They cost a scan
of the new text's lines, plus a line diff only when ``write``, or ``edit`` in the whole_file
format, replaces an existing file.

No phrase list here is our own. What counts as a placeholder, and how much removal is too
much, comes from tools that ship the check (survey clones, read 2026-10-07):

- Gemini CLI, fb972b2, ``packages/core/src/tools/omissionPlaceholderDetector.ts``: a line,
  optionally after ``//`` and inside one pair of parentheses, made of one of seven prefixes
  followed by ``...`` and nothing but dots. ``edit.ts`` refuses one in ``new_string`` that is
  not also in ``old_string``; ``write-file.ts`` refuses any. Its tool description also names
  ``'unchanged code'``.
- Continue, 5522c6f, ``core/edit/lazy/deterministic.ts``: a comment whose text matches
  ``/\\.{3}\\s*(.+?)\\s*\\.{3}/`` is a lazy block (``isLazyBlock``), and a whole-file rewrite
  is not applied directly when removed lines are more than 0.3 of its Myers line diff
  (``REMOVAL_PERCENTAGE_THRESHOLD``, over ``core/diff/myers.ts``). Its own markers are
  ``// ... existing code ...`` and ``// ... rest of code ...`` (``tools/definitions/
  editFile.ts``) and ``UNCHANGED CODE`` (``edit/lazy/prompts.ts``).
- Plandex, e2d7720, ``app/server/syntax/structured_edits_apply.go`` ``isRef``: any line
  containing ``... existing code ...``; its prompts also use ``# Existing code...``.
- AutoCodeRover, 585d3e6, ``app/agents/patch_utils.py``: drops the line
  ``# Rest of the code...``.
- Roo Code, b867ec9, ``write_to_file`` description: "placeholders like
  '// rest of code unchanged' are STRICTLY FORBIDDEN".
- Cline v3, e1bdeef, ``integrations/editor/detect-omission.ts``: which lines count as
  comments (``//``, ``#``, ``/*``, ``{/*``, ``<!--``). Its keyword test (any comment line
  holding "rest", "previous", "existing", "remain(s)", "unchanged" or "...") is not used:
  Cline itself commented the call out, and over the archived runs it matches ordinary
  comments ("we only need the previous row") far more often than elisions.

Where this narrows a source, it is to avoid a false refusal, and the code says so at that
spot. A wrong refusal costs a weak model a step and teaches it that correct code is refused.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

#: Continue's `REMOVAL_PERCENTAGE_THRESHOLD`: a whole-file write whose removed lines are more
#: than this share of its line diff is refused (unless the file was seen in full).
REMOVAL_THRESHOLD = Fraction(3, 10)

#: Comment openers, longest first so `{/*` wins over `/*`: Cline v3's comment patterns.
#: Gemini's detector strips only `//`; Python, shell and markup need the rest.
_OPENERS = ("{/*", "<!--", "/*", "//", "#")
#: The closers that go with them, so `/* ... existing code ... */` reads like `//` does.
_CLOSERS = ("*/}", "-->", "*/")

#: Gemini CLI's `OMITTED_PREFIXES`, verbatim.
_GEMINI_PREFIXES = frozenset(
    {
        "rest of",
        "rest of method",
        "rest of methods",
        "rest of code",
        "unchanged code",
        "unchanged method",
        "unchanged methods",
    }
)

#: A placeholder's whole text, once its dots, brackets and the word "the" are set aside,
#: when it sits next to an ellipsis. Gemini's prefixes, plus: "existing code" (Continue,
#: Plandex), "unchanged" (Gemini's description lists "unchanged code"; ``# ... (unchanged)``
#: is the shape it takes in a Python comment).
_PHRASES = _GEMINI_PREFIXES | {"existing code", "unchanged"}

#: The one placeholder sources name without an ellipsis: Roo Code's "rest of code
#: unchanged" (AutoCodeRover's "Rest of the code" is the same once "the" is set aside).
#: Only as a whole comment line; as a bare line it is prose.
_BARE_PHRASES = frozenset({"rest of code unchanged"})

_ELLIPSIS = "..."
#: Continue's `LAZY_COMMENT_REGEX`, anchored at both ends of the comment's text. Continue
#: matches inside any comment, which also catches prose such as "// wait... then retry...".
#: Here only a comment that is entirely the ellipsis-wrapped text counts.
_LAZY = re.compile(r"\.{3}\s*(.+?)\s*\.{3}")
_LETTER = re.compile(r"[^\W\d_]")
_NOT_WORD = re.compile(r"[^\w\s]+")


@dataclass(frozen=True, slots=True)
class Placeholder:
    #: 1-based line number in the text that was scanned.
    line: int
    #: The line as it was sent, stripped.
    text: str
    #: What it is compared by, so the same placeholder in another comment style is the same.
    key: str


def _words(text: str) -> str:
    """Lower case, ellipses and punctuation dropped, "the" dropped, whitespace collapsed."""
    plain = _NOT_WORD.sub(" ", text.replace(_ELLIPSIS, " ").lower())
    return " ".join(word for word in plain.split() if word != "the")


def _key(line: str) -> str | None:
    """The placeholder this line is, normalized, or None if it is not one."""
    text = line.strip()
    if not text:
        return None
    comment = False
    for opener in _OPENERS:
        if text.startswith(opener):
            comment = True
            text = text[len(opener) :].strip()
            for closer in _CLOSERS:
                if text.endswith(closer):
                    text = text[: -len(closer)].strip()
                    break
            break
    # Gemini: one pair of parentheses around the whole of it.
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()

    if _ELLIPSIS not in text:
        if comment and _words(text) in _BARE_PHRASES:
            return _words(text)
        return None

    # Gemini, as written: a known prefix, `...`, then nothing but dots. Also bare, as there.
    start = text.index(_ELLIPSIS)
    prefix = " ".join(text[:start].lower().split())
    suffix = text[start + len(_ELLIPSIS) :].strip()
    if prefix in _GEMINI_PREFIXES and suffix.strip(".") == "":
        return prefix

    words = _words(text)
    lazy = _LAZY.fullmatch(text)
    if not comment:
        # Bare, only Plandex's own marker, and only as the whole line: Plandex matches it
        # anywhere, which takes in a string literal that merely mentions it.
        return words if lazy and words == "existing code" else None
    # Named placeholders next to an ellipsis: `# Rest of the code...`, `# Existing code...`,
    # `# ... (unchanged)`. Only in a comment: bare, with its punctuation set aside, the same
    # words could be a quoted string in a list.
    if words in _PHRASES:
        return words
    # Continue: a comment that is nothing but `... something ...`, the something in words
    # (`# ..........` is a ruler, not a placeholder).
    if lazy and _LETTER.search(lazy.group(1)):
        return f"... {words} ..."
    return None


def placeholders(text: str) -> list[Placeholder]:
    """Every line of ``text`` that stands for code rather than being code."""
    found: list[Placeholder] = []
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if (key := _key(line)) is not None:
            found.append(Placeholder(number, line.strip(), key))
    return found


def introduced(old: str, new: str) -> list[Placeholder]:
    """Placeholders in ``new`` that ``old`` does not already have.

    A file that already holds one (a template, a document about this habit) may be
    rewritten with it intact. This is Gemini's `edit` rule applied to the whole file instead
    of the replaced snippet, which can only let more through.
    """
    added = placeholders(new)
    if not added:
        return []
    already = {found.key for found in placeholders(old)}
    return [found for found in added if found.key not in already]


@dataclass(frozen=True, slots=True)
class Removal:
    """What a whole-file write keeps of the file it replaces, in diff lines."""

    #: Non-blank lines in the file as it is now, and in what replaces it.
    old_lines: int
    new_lines: int
    #: Old lines the write keeps. When `exact` is False, a bound on the side that settles
    #: the threshold: an upper bound when it is over, a lower bound when it is not.
    kept: int
    #: Whether `kept` is the longest common subsequence or only a bound on it.
    exact: bool

    @property
    def removed(self) -> int:
        return self.old_lines - self.kept

    @property
    def share(self) -> Fraction:
        """Continue's ratio: removed lines over every line of the diff, kept, removed and
        added. On the same side of the threshold as the exact ratio, whatever `kept` is."""
        total = self.old_lines + self.new_lines - self.kept
        return Fraction(self.removed, total) if total else Fraction(0)

    @property
    def excessive(self) -> bool:
        return self.share > REMOVAL_THRESHOLD


def _diff_lines(text: str) -> list[str]:
    """The lines a removal is counted in: stripped, and blank lines left out.

    Both are deliberately more lenient than Continue. Continue merges only an adjacent
    removed/added pair that is equal once trimmed; here a re-indented line is never a
    removed one. Continue drops only trailing removed empty lines; here no blank line
    counts, because removing one never loses code. The archived runs hold rewrites that
    undo a file's accidental double spacing (one went from 211 lines to 105, all 85 lines
    of code kept).
    """
    stripped = (line.strip() for line in text.replace("\r\n", "\n").split("\n"))
    return [line for line in stripped if line]


def _distance(a: Sequence[str], b: Sequence[str], limit: int) -> int | None:
    """Myers' greedy shortest edit script length (insertions plus deletions), or None
    when it is longer than ``limit``, which is all the threshold needs to know."""
    n, m = len(a), len(b)
    offset = limit + 1
    furthest = [0] * (2 * limit + 3)
    for d in range(limit + 1):
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and furthest[offset + k - 1] < furthest[offset + k + 1]):
                x = furthest[offset + k + 1]
            else:
                x = furthest[offset + k - 1] + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            furthest[offset + k] = x
            if x >= n and y >= m:
                return d
    return None


def _in_order(a: Sequence[str], b: Sequence[str]) -> int:
    """A common subsequence found greedily: never longer than the longest, so a lower
    bound on what a diff keeps, in O((n + m) log m)."""
    positions: dict[str, list[int]] = {}
    for index, line in enumerate(b):
        positions.setdefault(line, []).append(index)
    found, after = 0, -1
    for line in a:
        if (places := positions.get(line)) is not None:
            at = bisect_right(places, after)
            if at < len(places):
                after = places[at]
                found += 1
    return found


def removal(old: str, new: str) -> Removal:
    """How much of ``old`` a write of ``new`` keeps, decided against `REMOVAL_THRESHOLD`.

    Exact (a Myers diff, as in Continue) when the answer is near the threshold. Two bounds
    settle the rest without a diff, and cover the two cases that would make one slow:

    - The lines both share, counted as multisets, can only overstate what a diff keeps.
      If even that is over, it is over (a file of entirely new text).
    - A common subsequence found greedily can only understate it. If even that is under,
      it is under (new lines added between old ones).
    """
    a, b = _diff_lines(old), _diff_lines(new)
    n, m = len(a), len(b)
    # Common head and tail first: they are in every longest common subsequence.
    head = 0
    while head < n and head < m and a[head] == b[head]:
        head += 1
    tail = 0
    while tail < n - head and tail < m - head and a[n - 1 - tail] == b[m - 1 - tail]:
        tail += 1
    middle_a, middle_b = a[head : n - tail], b[head : m - tail]
    shared = head + tail

    bound = shared + sum((Counter(middle_a) & Counter(middle_b)).values())
    over = Removal(n, m, bound, exact=bound == shared)
    if over.excessive or over.exact:
        return over
    floor = shared + _in_order(middle_a, middle_b)
    under = Removal(n, m, floor, exact=floor == bound)
    if not under.excessive:
        return under

    # removed / (n + m - kept) > 3/10  <=>  7 * distance > 13m - 7n, where the distance
    # n + m - 2 kept is the middle's own (the shared head and tail cost nothing). Searching
    # no further than the largest distance that stays under it keeps this fast.
    limit = (13 * m - 7 * n) // 7
    distance = (
        _distance(middle_a, middle_b, min(limit, len(middle_a) + len(middle_b)))
        if limit >= 0
        else None
    )
    if distance is None:
        # Over the threshold; how far over is not worth an unbounded diff to find out.
        return Removal(n, m, min(bound, (n + m - limit - 1) // 2), exact=False)
    kept = shared + (len(middle_a) + len(middle_b) - distance) // 2
    return Removal(n, m, kept, exact=True)
