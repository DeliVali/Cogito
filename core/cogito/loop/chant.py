"""A reply stuck repeating itself: the same stretch of text, over and over, while it streams.

A weak or heavily quantized model can lock into a loop inside one reply ("lo que lo lo que
lo ...", seen 2026-10-18 on Qwen3.8-Flash-Next's 1-bit Coder build) and only stops at the
output limit. Nothing in the step-level checks sees it: there is no call to repeat and no
second step. This watches the text as it arrives and says when it has become a loop, so
the request can be cut there.

The method is Gemini CLI's content-loop check (packages/core/src/services/
loopDetectionService.ts, read 2026-10-18): slide a 50-character window over the streamed
text; when one window's exact text has occurred 10 times, the 10 latest occurrences are on
average at most 250 characters apart, and the stretches between them take at most 5
distinct forms, it is a loop. Code fences, tables, list items, headings and divider lines
reset it and code is not watched at all, since those repeat legitimately. Their numbers,
reused as they are; not tuned here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CHUNK = 50
THRESHOLD = 10
MAX_DISTANCE = CHUNK * 5
MAX_PERIODS = THRESHOLD // 2
MAX_HISTORY = 5000

_STRUCTURE = re.compile(r"^\s*(\||[-*+] |\d+[.)] |#{1,6} |> )")
_DIVIDER = re.compile(r"^\s*[-=_*~]{3,}\s*$")


@dataclass
class ChantDetector:
    """Feed it streamed text; `tripped` turns true once the text has become a loop."""

    history: str = ""
    #: Where each window's text was seen, by the window's own text.
    seen: dict[str, list[int]] = field(default_factory=dict)
    next_start: int = 0
    in_code: bool = False
    line: str = ""
    tripped: bool = False
    #: The window whose repeats tripped it, so the cut can be found in the whole reply.
    window: str = ""

    def feed(self, text: str) -> bool:
        """Add streamed text; True once a loop has been found (and from then on)."""
        if self.tripped or not text:
            return self.tripped
        for char in text:
            self.line += char
            if char != "\n":
                continue
            line, self.line = self.line, ""
            if self._structural(line):
                continue
            if self.in_code:
                continue
            self._add(line)
            if self.tripped:
                return True
        # A line still being written is watched too: a loop need not contain a newline.
        if self.line and not self.in_code and not _STRUCTURE.match(self.line):
            pending, self.line = self.line, ""
            self._add(pending)
        return self.tripped

    def _structural(self, line: str) -> bool:
        """Fences, tables, lists, headings, quotes and dividers reset the watch."""
        if "```" in line:
            if line.count("```") % 2:
                self.in_code = not self.in_code
            self._reset()
            return True
        if _STRUCTURE.match(line) or _DIVIDER.match(line):
            self._reset()
            return True
        return False

    def _reset(self) -> None:
        self.history = ""
        self.seen = {}
        self.next_start = 0

    def _add(self, text: str) -> None:
        self.history += text
        if len(self.history) > MAX_HISTORY:
            drop = len(self.history) - MAX_HISTORY
            self.history = self.history[drop:]
            self.next_start = max(0, self.next_start - drop)
            self.seen = {
                key: [p - drop for p in places if p - drop >= 0]
                for key, places in self.seen.items()
            }
        while self.next_start + CHUNK <= len(self.history):
            start = self.next_start
            self.next_start += 1
            window = self.history[start : start + CHUNK]
            places = self.seen.setdefault(window, [])
            places.append(start)
            if len(places) >= THRESHOLD and self._is_loop(places[-THRESHOLD:]):
                self.tripped = True
                self.window = window
                return

    def _is_loop(self, places: list[int]) -> bool:
        if (places[-1] - places[0]) / (THRESHOLD - 1) > MAX_DISTANCE:
            return False
        periods = {
            self.history[places[i] : places[i + 1]] for i in range(len(places) - 1)
        }
        return len(periods) <= MAX_PERIODS


def cut(text: str, detector: ChantDetector) -> str:
    """The reply up to where the run of repeats begins (a few words of the loop's first
    round can remain before the first full window). The reply itself when nothing tripped."""
    if not detector.tripped or not detector.window:
        return text
    places = [m.start() for m in re.finditer(re.escape(detector.window), text)]
    if len(places) < 2:
        return text
    # The run of repeats that ends the reply, walked back while they stay close together.
    run = [places[-1]]
    for place in reversed(places[:-1]):
        if run[-1] - place > MAX_DISTANCE:
            break
        run.append(place)
    run.reverse()
    return text[: run[0]].rstrip() if len(run) > 1 else text
