"""Ranking text against a query without a model: the words in it, and BM25 over them.
Used to choose which MCP tools a step shows (`tools/mcp/hub.py`) and which skill a message
matches (`tools/skills.py`)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

from cogito.graph.recall import STOP_WORDS

_WORD = re.compile(r"[^\W_]+")


def terms(text: str) -> list[str]:
    """Lower-case words, `snake_case` and `camelCase` split, with a light plural strip.
    Words of one or two letters and the English and Spanish words that carry no topic
    (`graph.recall.STOP_WORDS`) are left out: "write a note" matched a skill on "a"."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).replace("_", " ").lower()
    words = []
    for word in _WORD.findall(spaced):
        if word in STOP_WORDS or (len(word) <= 2 and not word.isdigit()):
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return words


class Bm25:
    """Okapi BM25 (k1 1.2, b 0.75, the usual constants) over a fixed set of documents."""

    def __init__(self, documents: Sequence[Sequence[str]]) -> None:
        self._docs = [Counter(doc) for doc in documents]
        self._lengths = [len(doc) for doc in documents]
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        frequency: Counter[str] = Counter()
        for doc in self._docs:
            frequency.update(doc.keys())
        n = len(self._docs)
        self._idf = {
            term: math.log(1 + (n - count + 0.5) / (count + 0.5))
            for term, count in frequency.items()
        }

    def scores(self, query: Iterable[str]) -> list[float]:
        terms = [term for term in set(query) if term in self._idf]
        result = []
        for doc, length in zip(self._docs, self._lengths, strict=True):
            score = 0.0
            for term in terms:
                tf = doc.get(term, 0)
                if tf:
                    norm = 1.2 * (0.25 + 0.75 * length / (self._average or 1))
                    score += self._idf[term] * tf * 2.2 / (tf + norm)
            result.append(score)
        return result
