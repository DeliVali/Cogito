"""Token counting.

An exact count costs a round trip to the model server, and every tool result needs a
count. So counting is a protocol with two implementations: a cheap estimate used
everywhere, and an exact one when the endpoint offers it and precision matters.

The estimate aims high: undercounting lets a budget overrun silently, while overcounting
only shortens a tool result a little. It can still undercount repetitive, digit-heavy
text (see `provider.probe.ProbeSetup.count_tokens`).
"""

from __future__ import annotations

import re


_WORDISH = re.compile(r"\w+|[^\w\s]", re.UNICODE)

#: BPE tokenizers split a typical word into a bit more than one token, and code and
#: non-English text into more. Against cl100k on mixed source and Spanish prose, 1.3 tokens
#: per word-ish run lands within about 10%, on the high side.
_TOKENS_PER_RUN = 1.3


class Heuristic:
    """Tokenizer-free estimate. The default everywhere a budget is applied."""

    name = "heuristic"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(len(_WORDISH.findall(text)) * _TOKENS_PER_RUN))


#: The process-wide default: cheap, deterministic, and good enough for budgets.
DEFAULT = Heuristic()


def count(text: str) -> int:
    return DEFAULT.count(text)
