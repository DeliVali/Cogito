"""The lines of a page that share a question's words, with their line numbers.

This is what `web_fetch` falls back to while Cogito Estella has not indexed the page yet
(the app feeds it only between turns, so right after a fetch it never has). No model is
asked and nothing is paraphrased: each passage is a line of the saved copy, quoted, with the
number `read` shows it at, so it can be checked. Lines are scored the way
`graph.recall.DocumentKnowledge.pointers` scores pages: the question's own words, weighted by
how rare each is in the page, with repeats saturating.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from cogito.graph.recall import terms
from cogito.provider import tokens

#: The longest stretch of one line that is quoted; a long paragraph is cut around its match.
_WINDOW_CHARS = 600
#: A line scoring under this share of the best line is left out.
_FLOOR = 0.5
_MAX_PASSAGES = 8
_HEADING = re.compile(r"#{1,6} \S")


@dataclass(frozen=True, slots=True)
class Passage:
    #: 1-based line of the saved copy.
    line: int
    text: str
    #: The nearest heading above it, when the passage is not itself a heading.
    heading: str


def select(lines: list[str], question: str, *, share: float = 1 / 3) -> list[Passage]:
    """The best lines for ``question``, most relevant first. Empty when the question has no
    usable word, or no line holds enough of them: ``share`` of the question's words, at
    least one (a third by default; a search's own reads ask for more, since a page they read
    was not chosen by anyone, `fetch.WebFetchTool.read_for_search`)."""
    wanted = list(dict.fromkeys(terms(question)))
    if not wanted:
        return []
    counts: list[dict[str, int]] = []
    frequency = {term: 0 for term in wanted}
    in_code = False
    headings: list[str] = []
    current = ""
    for line in lines:
        if line.startswith(("```", "~~~")):
            in_code = not in_code
        elif not in_code and _HEADING.match(line):
            current = line.strip()
        headings.append(current)
        found: dict[str, int] = {}
        for word in terms(line):
            if word in frequency:
                found[word] = found.get(word, 0) + 1
        for word in found:
            frequency[word] += 1
        counts.append(found)
    total = sum(1 for line in lines if line.strip())
    need = max(1, math.ceil(len(wanted) * share - 1e-9))
    scored: list[tuple[float, int]] = []
    for position, found in enumerate(counts):
        if len(found) < need:
            continue
        score = 0.0
        for word, count in found.items():
            rarity = math.log(1 + total / frequency[word])
            score += _saturate(count) * rarity * (2.0 if word.isdigit() else 1.0)
        scored.append((score, position))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    scored = [item for item in scored if item[0] >= best * _FLOOR]
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[Passage] = []
    for _, position in scored[:_MAX_PASSAGES]:
        line = lines[position]
        heading = headings[position]
        chosen.append(
            Passage(
                position + 1,
                _window(line, wanted),
                "" if heading == line.strip() else heading[:80],
            )
        )
    return chosen


def _saturate(count: int) -> float:
    """A word repeated in a line counts for less each time (BM25's term saturation, k=1.2),
    so a paragraph that says the page's own subject thirteen times does not outrank the one
    line that holds the question's rare words."""
    return count * 2.2 / (count + 1.2)


def render(found: list[Passage], budget: int) -> str:
    """``found`` as numbered lines, as many as fit ``budget`` tokens (the first one always,
    cut if it must be)."""
    shown: list[str] = []
    for passage in found:
        where = f" ({passage.heading})" if passage.heading else ""
        entry = f"  L{passage.line}{where} {passage.text}"
        if shown and tokens.count("\n".join([*shown, entry])) > budget:
            break
        shown.append(entry)
    if shown and tokens.count(shown[0]) > budget:
        shown[0] = shown[0][: max(budget, 1) * 3]
    return "\n".join(shown)


def _window(line: str, wanted: list[str]) -> str:
    """``line``, or the stretch of it around the first of the question's words."""
    line = line.strip()
    if len(line) <= _WINDOW_CHARS:
        return line
    lowered = line.lower()
    hits = [i for i in (lowered.find(term) for term in wanted) if i >= 0]
    start = max(0, (min(hits) if hits else 0) - _WINDOW_CHARS // 3)
    end = start + _WINDOW_CHARS
    return ("... " if start else "") + line[start:end].strip() + (" ..." if end < len(line) else "")
