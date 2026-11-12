"""Values a tool produced, kept verbatim across a fold.

The summary is prose, and prose loses exact values. In the amnesia probe, asked after a
fold what a command had printed, the model ran the command again and offered that output as
the earlier one. On another run it produced an identifier that appears in exactly one
message in the graph: its own answer. Two of three runs.

Three attempts to fix that by telling the model something failed; telling it does not make
it notice (CONTRIBUTING.md section 1b). So this tells it nothing and keeps the value. A
literal still in the window is one the model cannot get wrong.

What counts as a literal is a set of named rules, not a heuristic: the budget must go to
data, and a rule nobody can name will quietly start matching prose.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from cogito.provider.base import ResultProvenance
from cogito.provider.tokens import count

#: Tools whose result renders what is still on disk. Their values are not kept: a literal
#: is worth a slot only when it cannot be obtained again. A file can be read again; what a
#: command printed cannot be printed again.
#:
#: On a real run, `read` flooded the budget with the edit format's line anchors (`17:dte`,
#: `18:t2i`, hundreds per call), and eviction by age then dropped the step-1 value the
#: store existed to hold.
#:
#: Known limitation: "reproducible" is a property of the workspace, not the tool. Reading a
#: file again gives its current contents, so if something edits it in between, the earlier
#: value is gone and this exclusion dropped it. The amnesia probe never rewrites a file it
#: read, so this path is unmeasured. The test that would settle it: read a value from a
#: file, modify the file, fold, then ask for the earlier value.
REPRODUCIBLE_TOOLS: frozenset[str] = frozenset({"read", "glob", "grep"})

#: The edit format's line addressing, `12:a3|`. A harness artifact shaped exactly like an
#: identifier, so it is excluded by name.
_ANCHOR = re.compile(r"(?<![\w-])\d+:[A-Za-z0-9]{2,4}(?=\|)")

#: The label `recall` puts on every sentence it quotes, `msg:<id>`. The id names a graph node
#: the model cannot pass to any tool (`recall` reads `<session>:<seq>` addresses), and it is
#: shaped exactly like an identifier, so on a real run these filled the block. Excluded by
#: name, like the anchors.
_SOURCE_TAG = re.compile(r"(?<![\w-])msg:[A-Za-z0-9_-]+")

#: Values are delimited so a tool result cannot end its row, forge a caption, or pass for
#: anything but a value. Guillemets are stripped from the value itself, which is what makes
#: the delimiter trustworthy.
_OPEN, _CLOSE = "\u00ab", "\u00bb"

#: The caption above the block. It names what the rows are and asks the model for
#: nothing; no instruction is added.
_CAPTION = "Values produced by tools earlier in this conversation, verbatim:"


#: Anything outside printable single-line text is not a literal and never renders.
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: A literal is worth keeping only if it is shaped like data. Each rule is named, and the
#: name travels with the literal so the graph can be audited by rule as well as by tool.
#:
#: Ordinary prose must not match any of these: a budget filled with English has silently
#: evicted the values it existed to hold.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # A run of hex with at least one digit. The digit keeps English words out: "deadbeef"
    # and "facade" are hex-shaped, "a95543" is not a word.
    ("hash", re.compile(r"(?<![\w-])(?=[0-9a-fA-F]*[0-9])[0-9a-fA-F]{7,}(?![\w-])")),
    # An identifier: letters and digits joined by - _ : . with at least one digit, which
    # separates REF-A95543-ORBIT and orbit-drain-6120 from a hyphenated phrase.
    (
        "identifier",
        re.compile(r"(?<![\w-])(?=[\w:.-]*[0-9])[A-Za-z0-9]+(?:[-_:.][A-Za-z0-9]+){1,6}(?![\w-])"),
    ),
    # A filesystem path: absolute, or relative with an extension. That keeps ordinary
    # slashes ("and/or", "read/write") out.
    ("path", re.compile(r"(?<!\S)(?:/[^\s/]+(?:/[^\s/]+)*|\.{0,2}/?[\w.-]+/[\w./-]*\.\w+)(?!\S)")),
    # A number carrying a unit. A bare number is not kept: it is the most common token in
    # any output and the least identifying.
    (
        "quantity",
        re.compile(
            r"(?<![\w.])\d+(?:\.\d+)?\s?(?:ms|µs|us|ns|s|m|h|d|kb|mb|gb|tb|kib|mib|gib|b|%)"
            r"(?![\w.])",
            re.IGNORECASE,
        ),
    ),
    # Something the output itself quoted: whoever wrote the tool chose to set it apart.
    ("quoted", re.compile(r"\"[^\"\n]{1,120}\"|'[^'\n]{1,120}'")),
)

#: A quote pair is a guess about where a string starts and ends, and in JSON, code or a
#: repr it is often wrong: pairing slips by one quote and the "value" is the text between
#: two strings (`":{"`, `', '`). These are the shapes of a quoted match that is not a
#: value, each named. A real run (48 folds, one session) kept 2,513 of these among its
#: 3,360 evictions, and the survivors took the budget from the values.
_ALNUM = re.compile(r"[A-Za-z0-9\u00c0-\uffff]")
_STRUCTURE_AROUND_QUOTE = re.compile(r"[\"']\s*[,:\]})]|[,:\[{(]\s*[\"']|\\[\"']")
_KEY_AFTER = re.compile(r"\s*:")
#: A field name opens its entry: after `{` or `,` or at the start of a line. The same quote
#: followed by a colon inside a sentence (`ls: cannot access 'ledger.txt': No such file`)
#: is a value, and dropping it lost one on a recorded run.
_KEY_BEFORE = re.compile(r"(?:^|[{,])[ \t]*\Z", re.MULTILINE)
MIN_QUOTED_ALNUM = 3


def not_a_value(inner: str, before: str, after: str) -> str | None:
    """Why a quoted match is not a value, or None when it is one.

    `inner` is the text between the quotes, `before` and `after` what surrounds them.
    """
    if len(_ALNUM.findall(inner)) < MIN_QUOTED_ALNUM:
        return "too_short"  # punctuation, whitespace, a one- or two-letter token
    if inner != inner.strip() or inner.endswith("\\"):
        return "cut_edge"  # indentation or a cut escape: the pair began or ended mid-string
    if _STRUCTURE_AROUND_QUOTE.search(inner):
        return "spans_strings"  # a quote beside a comma, colon or bracket: two strings joined
    if _KEY_AFTER.match(after) and _KEY_BEFORE.search(before[-80:]):
        return "key"  # a field name is schema, not something the tool produced
    return None


#: Tokens of the window this block may take. Small on purpose: it sits next to a summary
#: that is itself capped, and what a literal adds is exactness, not volume.
DEFAULT_BUDGET_TOKENS = 320

#: Anything longer is output that belongs in an artifact, not a literal.
MAX_LITERAL_CHARS = 120

@dataclass(frozen=True, slots=True)
class Literal:
    """One value, and where it came from.

    Without the step and the tool, a literal in the graph cannot be traced to what produced
    it, and unauditable memory is why this project rejected summaries in the first place.
    """

    text: str
    tool: str
    step: int
    kind: str

    def rendered(self) -> str:
        """One row. The value is delimited, never bare, so its edges are unambiguous."""
        return f"  {_OPEN}{self.text}{_CLOSE}    ({self.tool}, step {self.step})"


def _sanitise(text: str) -> str:
    """Make a captured value safe to place in a row, without rewriting what it says.

    Only structure is removed: the delimiters, control characters and the caption. A tool
    result that contains "do not" or "call" keeps them, because the block is data and its
    rows are quotations. What must never survive is anything that could end the row or
    counterfeit the block around it.

    The caption is stripped because a value carrying it would put a second caption inside
    the block. The delimiters already mark such a value as a value, but "the caption
    appears exactly once" is simpler to hold outright than to reason about.
    """
    without_structure = (
        text.replace(_OPEN, "").replace(_CLOSE, "").replace(_CAPTION, "")
    )
    return _UNSAFE.sub("", without_structure).strip()


def extract(content: str) -> list[tuple[str, str]]:
    """Every literal in one tool result, as (kind, text), in the order they appear.

    Overlaps are resolved by position and then by length: the longest match starting
    earliest wins, so `REF-A95543-ORBIT` is kept whole rather than shredded into the hash
    rule's `A95543`.
    """
    # Strip the edit format's anchors first: they are the harness talking to itself, and
    # they look enough like identifiers to crowd out data.
    content = _SOURCE_TAG.sub(" ", _ANCHOR.sub(" ", content))

    found: list[tuple[int, int, str, str]] = []
    for kind, pattern in RULES:
        for match in pattern.finditer(content):
            text = _sanitise(match.group(0))
            if kind == "quoted" and not_a_value(
                text[1:-1], content[: match.start()], content[match.end() :]
            ):
                continue
            if text and len(text) <= MAX_LITERAL_CHARS:
                found.append((match.start(), -len(text), kind, text))
    found.sort()

    kept: list[tuple[str, str]] = []
    claimed: list[tuple[int, int]] = []
    for start, negative_length, kind, text in found:
        end = start - negative_length
        if any(start < done_end and end > done_start for done_start, done_end in claimed):
            continue
        claimed.append((start, end))
        kept.append((kind, text))
    return kept


class LiteralStore:
    """What tools have produced so far, oldest first, under a token budget.

    The engine holds it for the whole conversation and hands it to every fold. The caller
    owns the value and the fold only reads it, which is what stopped the handover nesting
    inside itself. Nothing here reads `messages[0]`.
    """

    def __init__(
        self,
        *,
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        record: Callable[..., Any] | None = None,
    ) -> None:
        self._budget = budget_tokens
        self._record = record or (lambda *a, **k: None)
        self._kept: dict[str, Literal] = {}

    def __len__(self) -> int:
        return len(self._kept)

    def __iter__(self) -> Iterator[Literal]:
        return iter(self._kept.values())

    def remember(
        self,
        content: str,
        *,
        tool: str,
        step: int,
        provenance: ResultProvenance | None = None,
    ) -> None:
        """Take whatever this tool result offered, then evict down to the budget.

        `provenance` is carried but not yet used. The policy that reads it will be its own
        commit, measured on its own.
        """
        del provenance  # unused until the policy commit
        if tool in REPRODUCIBLE_TOOLS:
            # Its values are still on disk; spending the budget on them would evict the
            # ones that are not.
            return
        for kind, text in extract(content):
            if text in self._kept:
                # First sighting wins: a value's provenance is where it came from, not the
                # last place it was echoed.
                continue
            self._kept[text] = Literal(text=text, tool=tool, step=step, kind=kind)
        self._evict()

    def replay(self, inputs: Iterable[tuple[str, str, int]]) -> None:
        """Restore what a conversation remembered before the app restarted: the same
        ``(content, tool, step)`` `remember` calls, in the same order, so the same values
        are kept and the same ones evicted. Evictions are not journalled again; they were
        when they first happened."""
        record, self._record = self._record, lambda *a, **k: None
        try:
            for content, tool, step in inputs:
                self.remember(content, tool=tool, step=step)
        finally:
            self._record = record

    def _evict(self) -> None:
        """Drop the oldest until the block fits, and record every drop.

        A silent drop is the worst failure this module could have: the value the model
        later invents would be one this store held and dropped without telling anyone.
        """
        while self._kept and count(self.render()) > self._budget:
            text, literal = next(iter(self._kept.items()))
            del self._kept[text]
            self._record(
                "literal_evicted",
                text=literal.text,
                tool=literal.tool,
                step=literal.step,
                kind=literal.kind,
                budget_tokens=self._budget,
                remaining=len(self._kept),
            )

    def render(self) -> str:
        """The block as it goes into the seed, or empty when there is nothing to say."""
        return render(self._kept.values())


def render(literals: Iterable[Literal]) -> str:
    rows = [literal.rendered() for literal in literals]
    if not rows:
        return ""
    return "\n".join([_CAPTION, *rows])


