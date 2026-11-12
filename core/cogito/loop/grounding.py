"""Values a final answer states that nothing the turn retrieved holds.

A model asked about something its search did not find can state a value anyway: a port, a
count, a version, an address. On invented subjects (cogito-lab measurements/
2026-10-14-search-grounding-and-estella-lite.md) a smaller model did so in most probe runs
(dolphin3-qwen25-3b 9 of 10, qwen2.5-coder-7b 3 of 10), qwen3.5-9b once in 66 unanswerable
answers, with made-up provenance; every right answer's value was in a tool result.
So the check is mechanical: a value of the answer that appears in no tool result, no
message of the person's and no harness text is unsupported. It reads; it never calls a
model. Checking claims against the evidence spans alone, without an entailment model, is
how the selective-verification work of 2026 counts an unsupported claim.

Which turns are checked, and whether anything is done about it, is the engine's decision
(`Engine._maybe_check_grounding`): only turns that retrieved something, and only for a
model the app checks (`cli._grounding_for`: a local one, unless a pin says otherwise).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Tools that bring information into the turn. A turn without one answered from the
#: project or from the model's own knowledge, and is never checked.
RETRIEVAL_TOOLS: frozenset[str] = frozenset(
    {"web_search", "web_fetch", "ask", "ask_document", "recall"}
)

#: Shapes of a checkable value. Named, as `context.literals.RULES` are, so prose never
#: matches: a word is never a value.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("url", re.compile(r"https?://[^\s)>\]\"'`]+")),
    # SHA-256, MD5, BLAKE2, ISO-8601, utf-8: letters and digits joined, with a digit.
    (
        "identifier",
        re.compile(
            r"(?<![\w-])(?=[\w.-]*\d)(?=[\w.-]*[A-Za-z])"
            r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+(?![\w-])"
        ),
    ),
    # Letters then digits, three characters or more: SHA256, MD5, BLAKE2b, ES2022.
    ("alnum", re.compile(r"(?<![\w-])[A-Za-z]{2,}\d+[A-Za-z0-9]*(?![\w-])")),
    # An environment variable or constant: TARNISH_NO_METRICS.
    ("constant", re.compile(r"(?<![\w])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![\w])")),
    # A version or a decimal: 1.71, 3.12.4.
    ("version", re.compile(r"(?<![\w.])\d+(?:\.\d+)+(?![\w.])")),
    # A number of three digits or more, grouped or not: 7431, 16,384, 16.384, 16 384.
    (
        "number",
        re.compile(
            r"(?<![\w.,])\d{1,3}(?:[,.\u00a0 ]\d{3})+(?![\w.,]?\d)"
            r"|(?<![\w.,])\d{3,}(?![\w.,]?\d)"
        ),
    ),
)

#: A list marker or a heading number at the start of a line is structure, not a value.
_MARKER = re.compile(r"(?m)^\s*(?:\d+[.)]|#+)\s")
_CODE = re.compile(r"```.*?```", re.DOTALL)


def _key(value: str) -> str:
    """One spelling per value: lower case, and a number without its group separators."""
    lowered = value.lower().strip(".,;:")
    if re.fullmatch(r"\d{1,3}(?:[,.\u00a0 ]\d{3})+", lowered):
        return re.sub(r"[,.\u00a0 ]", "", lowered)
    return lowered


def values(text: str) -> list[str]:
    """Every checkable value of ``text``, in order, each once. A fenced block is left out:
    code the model writes is its own work, not a claim about what it found."""
    text = _MARKER.sub(" ", _CODE.sub(" ", text))
    found: list[tuple[int, int, str]] = []
    for _, pattern in RULES:
        for match in pattern.finditer(text):
            found.append((match.start(), -len(match.group(0)), match.group(0)))
    found.sort()
    kept: list[str] = []
    claimed: list[tuple[int, int]] = []
    for start, negative, value in found:
        end = start - negative
        if any(start < b and end > a for a, b in claimed):
            continue
        claimed.append((start, end))
        value = value.rstrip(".,;:!?")
        if value and _key(value) not in {_key(v) for v in kept}:
            kept.append(value)
    return kept


def _haystack(evidence: Iterable[str]) -> str:
    """The evidence as one lower-case text in which grouped numbers also appear ungrouped,
    so 16,384 in a page supports 16384 in an answer and the other way round."""
    text = "\n".join(evidence).lower()
    joined = re.sub(r"(?<=\d)[,.\u00a0 ](?=\d{3}(?!\d))", "", text)
    return text + "\n" + joined


def unsupported(answer: str, evidence: Iterable[str]) -> list[str]:
    """The values of ``answer`` that appear nowhere in ``evidence``."""
    haystack = _haystack(evidence)
    return [v for v in values(answer) if _key(v) not in haystack]
