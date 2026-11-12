"""Turns what the model emitted into callable tool calls.

Two stages. Extraction finds candidates: the native `tool_calls` array if the endpoint
sent one, otherwise a `<tool_call>` block in the text (`repair.EXTRACTORS`). Since
2026-10-30 no other text form is read; one that looks like a call is answered with the
repair prompt, never run (`repair.text_call`). Normalisation then repairs each candidate
(tool name, parameter names, schema types) and validates it: a model trained on another
harness sends `file_path` to a tool whose parameter is `path`, and the endpoint passes it
through. Repairing it is a dict lookup; rejecting it costs a round trip.

Every repair increments a named counter. The repair rate per strategy per model is a
benchmark metric, and shows which dialect a model actually speaks.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from cogito.provider import repair
from cogito.provider.base import Completion, ToolSpec, ToolUseBlock


@dataclass(frozen=True, slots=True)
class Extraction:
    calls: tuple[ToolUseBlock, ...] = ()
    #: Strategy names that fired, in order. Empty means nothing needed repairing.
    repaired_by: tuple[str, ...] = ()
    #: Set when the model clearly meant to call something and it could not be made valid.
    failure: str = ""
    #: Name of a call the output limit cut off before the model closed it. Never in
    #: `calls`; see `_cut_off`.
    cut_off: str = ""
    #: Every failure was a call to a tool withheld this step (`extract`'s ``withheld``):
    #: the model is told the tool is off, not asked to send the call again.
    withheld: bool = False
    #: Every `<tool_call>` read from the reply's text, run or not. The window shows none of
    #: them as text: one to a tool that does not exist is answered like any failed call
    #: (the app, 2026-11-01: `kill_process`, drawn as the answer).
    written: tuple[ToolUseBlock, ...] = ()

    @property
    def wanted_a_call(self) -> bool:
        return bool(self.calls) or bool(self.failure) or bool(self.cut_off)


#: In `repaired_by` of every call read from the reply's text rather than sent natively.
TEXT_SOURCE = "xml_tool_call"


@dataclass
class Counters:
    """Per-strategy hit counts, carried into the benchmark results."""

    hits: dict[str, int] = field(default_factory=dict)

    def hit(self, name: str) -> None:
        self.hits[name] = self.hits.get(name, 0) + 1

    def as_dict(self) -> dict[str, int]:
        return dict(self.hits)


def _candidates(
    completion: Completion, tools: Mapping[str, ToolSpec], counters: Counters | None
) -> tuple[list[ToolUseBlock], list[ToolUseBlock]]:
    """The native calls, and the `<tool_call>` blocks left in the text.

    Both, not one or the other: llama-server converts a `<tool_call>` only for a tool the
    request listed, and leaves one naming any other tool in the reply's text, even beside
    calls it did convert. qwen3.8-flash-motoris called an MCP tool it was not shown that
    way, next to a native `shell` call (the app, 2026-10-31); before, it was never run and
    the window drew it as text.
    """
    native = list(completion.native_tool_calls)
    text = completion.raw_text
    written = []
    for name, extractor in repair.EXTRACTORS:
        if text.strip() and (found := extractor(text, tools)):
            written = list(found)
            if counters:
                counters.hit(f"{name}_beside_native" if native else name)
            break
    if native and counters:
        counters.hit("native")
    return native, written


def _cut_off(completion: Completion, call: ToolUseBlock, source: str) -> bool:
    """Whether the output limit stopped this call before the model closed it.

    Only asked of the last candidate, and only when the reply stopped at the limit, since
    the limit can only cut what the reply ended in. A native call is cut when its arguments
    do not parse: llama-server streams the prefix it has and never closes it. A
    `<tool_call>` is cut when its JSON runs to the end of the reply with a string or bracket
    still open: `repair.parse_loose_json` would close it and write half-finished `content`
    to disk.
    """
    if not (completion.truncated or completion.stop_reason == "max_tokens"):
        return False
    if source == "native":
        try:
            return not isinstance(json.loads(call.raw or "{}"), dict)
        except json.JSONDecodeError:
            return True
    raw = call.raw.strip()
    return bool(raw) and completion.raw_text.rstrip().endswith(raw) and repair.unterminated(raw)


def extract(
    completion: Completion,
    tools: Sequence[ToolSpec],
    *,
    counters: Counters | None = None,
    refuse_cut_off: bool = False,
    withheld: Mapping[str, str] | None = None,
) -> Extraction:
    """Return the tool calls this completion asked for, repaired where that is safe.

    ``refuse_cut_off`` drops a call the output limit cut off (`_cut_off`) and names it in
    `Extraction.cut_off`. The loop sets it because it runs what comes back.

    ``withheld`` names registered tools this step does not show, with what to tell a model
    that calls one anyway (the web the person turned off), instead of "unknown tool".
    """
    by_name = {tool.name: tool for tool in tools}
    native, written = _candidates(completion, by_name, counters)
    # What the model wrote comes first: in the replies seen, the text preceded the calls
    # the server converted.
    candidates = written + native
    source = "native" if native else "xml_tool_call"
    if not candidates:
        # Any other call written in the text is never run, but the turn does not end on it:
        # the loop answers it with the repair prompt (2026-10-30: "no quiero que un flujo se
        # corte a la mitad solo por eso"). Not when the output limit cut the reply: the loop
        # has its own notice for that.
        cut = completion.truncated or completion.stop_reason == "max_tokens"
        name = None if cut else repair.text_call(completion.raw_text, by_name)
        if name is not None:
            if counters:
                counters.hit("text_call_refused")
            return Extraction(
                failure=(
                    f"the {name} call was written out in the text of your reply, so it was "
                    "not run. Make it as a tool call (the tool-calling interface), not as "
                    "text"
                )
            )
        return Extraction()

    # Refused before any repair, so it neither runs nor is reported as a malformed call.
    cut_off = ""
    cut = _cut_off(completion, candidates[-1], source)
    if refuse_cut_off and cut:
        cut_off = candidates.pop().name
        cut = False
        if counters:
            counters.hit("truncated_call_refused")

    if native:
        # Read arguments the dialect could not. A cut call kept here is left as it was.
        read = len(candidates) - 1 if cut else len(candidates)
        candidates[len(written) : read] = [
            repair.native_arguments(call) for call in candidates[len(written) : read]
        ]
    from_text = {id(call) for call in candidates[: len(written)]}

    calls: list[ToolUseBlock] = []
    fired: list[str] = []
    failures: list[str] = []
    withheld_failures = 0

    for candidate in candidates:
        text_source = id(candidate) in from_text
        if repair.LOOSE_JSON in candidate.repaired_by:
            fired.append(repair.LOOSE_JSON)
            if counters:
                counters.hit(repair.LOOSE_JSON)
        call, repairs = repair.normalise(candidate, by_name)
        for name in repairs:
            if counters:
                counters.hit(name)
        fired.extend(repairs)

        spec = by_name.get(call.name)
        if spec is None and withheld and call.name in withheld:
            failures.append(f"{call.name}: {withheld[call.name]}")
            withheld_failures += 1
            if counters:
                counters.hit("withheld_tool")
            continue
        if spec is None:
            known = ", ".join(sorted(by_name)) or "none"
            failures.append(f"unknown tool {call.name!r}; available tools are: {known}")
            if counters:
                counters.hit("unresolved_name")
            continue

        # After the name, so an unknown tool is still reported as unknown.
        if repair.INVALID_JSON in call.repaired_by:
            failures.append(
                f"{call.name}: its arguments were not valid JSON and could not be "
                "repaired, so it was not run. Send it again with the arguments as one valid "
                "JSON object."
            )
            if counters:
                counters.hit(repair.INVALID_JSON)
            continue

        if problem := repair.validate(call, spec):
            failures.append(f"{call.name}: {problem}")
            if counters:
                counters.hit("invalid_arguments")
            continue

        calls.append(
            replace(call, repaired_by=tuple(dict.fromkeys((TEXT_SOURCE, *call.repaired_by))))
            if text_source
            else call
        )

    return Extraction(
        calls=tuple(calls),
        repaired_by=tuple(dict.fromkeys(fired)),
        failure="; ".join(failures),
        cut_off=cut_off,
        withheld=bool(failures) and withheld_failures == len(failures),
        written=tuple(written),
    )
