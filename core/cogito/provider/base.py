"""The provider-neutral conversation model.

A message is a list of typed blocks, as Anthropic models it, because that is the superset:
a flat string plus a side list of tool calls is a lossy projection of it. The OpenAI-shaped
payload is built in the adapter and thrown away; it is never what we keep.

**Tool calls are parsed from the complete assistant message, never from the stream.**
Streaming only paints the UI. Parsing partial JSON and half-open XML tags breeds bugs and
saves no tokens.
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from cogito.cancel import CancelToken
from cogito.errors import ProviderError

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """Reasoning the model exposes on a separate channel. Shown, never fed back as text.

    ``signature`` is set only by a dialect whose API signs its reasoning (the Messages API).
    Such a block must go back to that API unchanged, as a thinking block and never as text,
    with the tool calls it preceded. ``redacted`` marks the API's encrypted form, whose
    payload is the signature itself. Neither is ever shown or stored as text.
    """

    text: str
    signature: str = field(default="", repr=False)
    redacted: bool = field(default=False, repr=False)


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """A tool call, however the model happened to express it.

    ``raw`` keeps exactly what the model emitted and ``repaired_by`` names the strategies
    that fired to make it usable. The benchmark reads its repair rate per strategy per model
    from these two fields.
    """

    id: str
    name: str
    input: Mapping[str, Any]
    raw: str = ""
    repaired_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImageBlock:
    """An image the person attached, by reference: ``path`` is the session's own copy of
    it (`ArtifactStore.stash_bytes`), read only when a wire adapter encodes the request, so
    the journal, a fold's copy of the tail and every rewrite carry a path, not megabytes.
    The message's text holds the image's marker (`context.attachments.block`), which is
    what the graph, the thread and a fold's handover read; this block adds nothing to
    `transcript_text` or `flat_text`."""

    media_type: str
    path: str
    name: str = ""
    width: int = 0
    height: int = 0



@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    #: Images the tool returned (a screenshot), by reference like an attached one
    #: (`ImageBlock`). ``content`` carries each one's marker, so the graph, a fold and a
    #: model that cannot see still read the fact. Left out of repr, so a result without one
    #: prints as it did before the field existed.
    images: tuple[ImageBlock, ...] = field(default=(), repr=False)

Block = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | ImageBlock


def image_data(block: ImageBlock) -> str | None:
    """The image's bytes as base64, read now from the session's copy, or None when the
    copy is gone (a session folder someone cleaned). A wire adapter sends the marker's
    text alone then, which still says what was attached."""
    try:
        with open(block.path, "rb") as handle:
            return base64.b64encode(handle.read()).decode("ascii")
    except OSError:
        return None


def missing_image(block: ImageBlock) -> str:
    return f"[the attached image {block.name or block.path} is no longer available]"


#: How many tool-result images a request carries at least: the newest, older ones as their
#: marker alone. Anthropic's computer-use guidance keeps the last three screenshots (its
#: best-practices post, 2026-05-13, read 2026-10-31); every image is ~1,000-1,800 tokens.
RESULT_IMAGES_KEPT = 3

#: Older images are dropped this many at a time, never one a step, so the prompt changes
#: only every few steps and a prompt cache holds in between (Anthropic's computer-use demo,
#: `_maybe_filter_to_n_most_recent_images`, read 2026-11-01). Dropping one a step broke
#: every checkpoint Motoris had taken after it: each step read 4-18K tokens again (6-11 s at
#: 120K, its log, 2026-11-01). Five, so at most seven are sent, is a choice.
RESULT_IMAGES_DROPPED_TOGETHER = 5


def recent_result_images(
    messages: Sequence[Message],
    keep: int = RESULT_IMAGES_KEPT,
    together: int = RESULT_IMAGES_DROPPED_TOGETHER,
) -> set[int]:
    """The ids (``id()``) of the tool-result images a request sends: the newest ``keep``,
    and up to ``together - 1`` more, since the older ones are left out ``together`` at a
    time. The stored transcript keeps every one; only what is sent drops the older."""
    images = [
        id(image)
        for message in messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
        for image in block.images
    ]
    dropped = max(0, len(images) - keep)
    dropped -= dropped % together
    return set(images[dropped:])


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    blocks: tuple[Block, ...]
    #: True for a message that exists for this one request only: the loop's per-step
    #: status line (the volatile prompt tier). A dialect with explicit cache breakpoints
    #: puts it after all of them. Left out of equality and repr, so a message compares and
    #: prints as it did before the field existed.
    volatile: bool = field(default=False, compare=False, repr=False)

    @classmethod
    def text(cls, role: Role, body: str) -> Message:
        return cls(role=role, blocks=(TextBlock(body),))

    @property
    def images(self) -> tuple[ImageBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ImageBlock))

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))

    def flat_text(self) -> str:
        """Only the prose. What the wire adapters put in a `content` field."""
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    def transcript_text(self) -> str:
        """Everything this message holds, rendered for the graph and the full-text index.

        `flat_text` is the wire view and drops tool calls and their results. Storing that
        would leave the transcript full of empty turns and hide the agent's actual work from
        `recall`.
        """
        parts: list[str] = []
        for block in self.blocks:
            match block:
                case TextBlock(text=text) | ThinkingBlock(text=text):
                    parts.append(text)
                case ToolUseBlock(name=name, input=arguments):
                    rendered = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
                    parts.append(f"[calls {name}({rendered})]")
                case ToolResultBlock(content=content, is_error=is_error):
                    parts.append(f"[{'error' if is_error else 'result'}] {content}")
        return "\n".join(part for part in parts if part)


#: The version of the block encoding below, journalled beside every encoded message. A
#: reader given a version it does not know must not guess at the blocks: it falls back to
#: the message's text, which every version keeps.
BLOCKS_VERSION = 1


class UnknownBlock(ValueError):
    """An encoded block whose ``type`` this version of the encoding does not know."""


def encode_blocks(blocks: Sequence[Block]) -> list[dict[str, Any]]:
    """A message's blocks as plain JSON values, every field kept, for the journal.

    `Message.transcript_text` is a rendering: it flattens a tool call into a line of text,
    drops a call's id and a thinking block's signature, and cannot be turned back into what
    the model was sent. This can: `decode_blocks(encode_blocks(b)) == tuple(b)`. A field at
    its default is left out, so a plain text message costs almost nothing extra. An image
    is encoded by its reference (`ImageBlock`), never its bytes.
    """
    encoded: list[dict[str, Any]] = []
    for block in blocks:
        match block:
            case TextBlock(text=text):
                encoded.append({"type": "text", "text": text})
            case ThinkingBlock(text=text, signature=signature, redacted=redacted):
                entry: dict[str, Any] = {"type": "thinking", "text": text}
                if signature:
                    entry["signature"] = signature
                if redacted:
                    entry["redacted"] = True
                encoded.append(entry)
            case ToolUseBlock(id=call_id, name=name, input=arguments, raw=raw):
                entry = {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}
                if raw:
                    entry["raw"] = raw
                if block.repaired_by:
                    entry["repaired_by"] = list(block.repaired_by)
                encoded.append(entry)
            case ToolResultBlock(tool_use_id=use_id, content=content, is_error=is_error):
                entry = {"type": "tool_result", "tool_use_id": use_id, "content": content}
                if is_error:
                    entry["is_error"] = True
                if block.images:
                    # Written only when there are some, so every journal before them
                    # encodes byte for byte as it did.
                    entry["images"] = encode_blocks(block.images)
                encoded.append(entry)
            case ImageBlock(
                media_type=media_type, path=path, name=name, width=width, height=height
            ):
                entry = {"type": "image", "media_type": media_type, "path": path}
                if name:
                    entry["name"] = name
                if width and height:
                    entry["width"], entry["height"] = width, height
                encoded.append(entry)
            case _:
                raise UnknownBlock(f"no encoding for {type(block).__name__}")
    return encoded


def decode_blocks(encoded: Sequence[Mapping[str, Any]]) -> tuple[Block, ...]:
    """The inverse of `encode_blocks`. Raises `UnknownBlock` on a ``type`` it does not
    know instead of dropping the block: a message missing a block is a different message,
    and one missing a tool call or a result is one no wire dialect accepts."""
    blocks: list[Block] = []
    for entry in encoded:
        if not isinstance(entry, Mapping):
            raise UnknownBlock(f"a block must be an object, not {type(entry).__name__}")
        kind = entry.get("type")
        if kind == "text":
            blocks.append(TextBlock(str(entry.get("text", ""))))
        elif kind == "thinking":
            blocks.append(
                ThinkingBlock(
                    str(entry.get("text", "")),
                    signature=str(entry.get("signature", "")),
                    redacted=bool(entry.get("redacted", False)),
                )
            )
        elif kind == "tool_use":
            arguments = entry.get("input")
            blocks.append(
                ToolUseBlock(
                    id=str(entry.get("id", "")),
                    name=str(entry.get("name", "")),
                    input=dict(arguments) if isinstance(arguments, Mapping) else {},
                    raw=str(entry.get("raw", "")),
                    repaired_by=tuple(str(name) for name in entry.get("repaired_by") or ()),
                )
            )
        elif kind == "tool_result":
            pictures = entry.get("images") or ()
            blocks.append(
                ToolResultBlock(
                    tool_use_id=str(entry.get("tool_use_id", "")),
                    content=str(entry.get("content", "")),
                    is_error=bool(entry.get("is_error", False)),
                    images=tuple(
                        b for b in decode_blocks(pictures) if isinstance(b, ImageBlock)
                    ),
                )
            )
        elif kind == "image":
            blocks.append(
                ImageBlock(
                    media_type=str(entry.get("media_type", "")),
                    path=str(entry.get("path", "")),
                    name=str(entry.get("name", "")),
                    width=int(entry.get("width", 0) or 0),
                    height=int(entry.get("height", 0) or 0),
                )
            )
        else:
            raise UnknownBlock(f"unknown block type {kind!r}")
    return tuple(blocks)


class Reproducibility(StrEnum):
    """Whether asking the same question again gets the same answer.

    This is about the source, not the value's worth (that is `Retention`). Keep the two
    apart: letting a list of tool names decide what the memory keeps is the staleness
    `ToolSpec.mutates` exists to avoid.
    """

    #: The same request returns the same bytes. Rare, and only with the query pinned to a
    #: version or a sequence. An append-only store does not qualify: a later event changes
    #: what a search returns.
    STABLE = "stable"
    #: It can be asked again, but the answer may have changed: a file something else may
    #: rewrite, a search over a store that keeps growing.
    MUTABLE = "mutable"
    #: It cannot be obtained again: what a command printed when it ran.
    EPHEMERAL = "ephemeral"


class Retention(StrEnum):
    """Whether the content is worth competing for the handover's budget.

    Not a size category, on purpose. A large result can hold the only evidence there is
    (measured: a bulky read of a file that was overwritten afterwards, whose content was
    the one thing the conversation was later asked about).
    """

    #: A record of what happened or what was found. Competes for the literal budget.
    EVIDENCE = "evidence"
    #: "Wrote 12 lines", "done". Irreproducible but worth nothing later; it goes to the
    #: external history, never to the budget.
    ACKNOWLEDGEMENT = "acknowledgement"


class PayloadShape(StrEnum):
    """How much there is, which is a separate question from whether it matters."""

    COMPACT = "compact"
    #: Large or repetitive. The full output belongs outside the window, and what is
    #: extracted from it is capped so one result cannot crowd out a conversation.
    BULK = "bulk"


@dataclass(frozen=True, slots=True)
class Provenance:
    """What a tool declares about its results.

    Every field is optional so a result can refine what the tool declared: a `read` knows
    the path and revision it just returned, and the spec cannot. Fields the harness owns
    are left out of this type on purpose: a tool cannot supply `observed_at`, the event
    sequence or the artifact reference, so it cannot get them wrong or forge them.
    """

    content_type: str | None = None
    reproducibility: Reproducibility | None = None
    retention: Retention | None = None
    shape: PayloadShape | None = None
    source_uri: str | None = None
    version: str | None = None
    #: The source's own notion of when, which is not when the harness saw it.
    source_timestamp: float | None = None

    def refined_by(self, other: Provenance | None) -> Provenance:
        """The result's declaration over the tool's, field by field."""
        if other is None:
            return self
        return Provenance(
            content_type=other.content_type or self.content_type,
            reproducibility=other.reproducibility or self.reproducibility,
            retention=other.retention or self.retention,
            shape=other.shape or self.shape,
            source_uri=other.source_uri or self.source_uri,
            version=other.version or self.version,
            source_timestamp=(
                self.source_timestamp
                if other.source_timestamp is None
                else other.source_timestamp
            ),
        )


@dataclass(frozen=True, slots=True)
class ResultProvenance:
    """A declaration the harness has completed. Only the harness can build one.

    `event_sequence` exists because a clock is not enough. Two results in the same second
    look alike by `observed_at`, and "the first command you ran" is about order, not time.
    The sequence is monotonic across the whole conversation, folds included, so
    first/earlier/later/last stay answerable after the messages holding them are gone.
    """

    #: Monotonic across the conversation and assigned by the harness alone.
    event_sequence: int
    tool_call_id: str
    tool: str
    content_type: str
    reproducibility: Reproducibility
    retention: Retention
    shape: PayloadShape
    observed_at: float
    raw_output_reference: str | None = None
    source_uri: str | None = None
    version: str | None = None
    source_timestamp: float | None = None

    @property
    def competes_for_budget(self) -> bool:
        return self.retention is Retention.EVIDENCE


#: What a tool that declares nothing gets. Keeping something recoverable costs a slot;
#: dropping something unrecoverable causes the failure this mechanism exists to prevent.
#: So silence is read the careful way.
DEFAULT_PROVENANCE = Provenance(
    content_type="text/plain",
    reproducibility=Reproducibility.EPHEMERAL,
    retention=Retention.EVIDENCE,
    shape=PayloadShape.COMPACT,
)


def resolve_provenance(
    declared: Provenance | None,
    refinement: Provenance | None,
    *,
    event_sequence: int,
    tool_call_id: str,
    tool: str,
    observed_at: float,
    raw_output_reference: str | None = None,
) -> ResultProvenance:
    """Complete a declaration with the facts only the harness holds."""
    merged = DEFAULT_PROVENANCE.refined_by(declared).refined_by(refinement)
    return ResultProvenance(
        event_sequence=event_sequence,
        tool_call_id=tool_call_id,
        tool=tool,
        content_type=merged.content_type or "text/plain",
        reproducibility=merged.reproducibility or Reproducibility.EPHEMERAL,
        retention=merged.retention or Retention.EVIDENCE,
        shape=merged.shape or PayloadShape.COMPACT,
        observed_at=observed_at,
        raw_output_reference=raw_output_reference,
        source_uri=merged.source_uri,
        version=merged.version,
        source_timestamp=merged.source_timestamp,
    )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What the model is told about a tool, plus what the harness needs to forgive it.

    ``aliases`` maps a canonical parameter to the names other harnesses use for it, so a
    model trained on another tool set still lands instead of failing validation and
    burning a retry.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]
    aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    budget_tokens: int = 1000
    #: True when this tool can change something outside the conversation. Exposure
    #: profiles derive from this, not from a list of names that would go stale when a tool
    #: is renamed or added.
    mutates: bool = False
    #: True when this tool starts another agent. Never exposed below the top level.
    delegates: bool = False
    #: True when this tool does more than read, though it changes no file the loop tracks:
    #: it acts on what the person sees and uses (the screen, a web page in the app's tabs,
    #: the microphone) or runs the project's own code (its linters). A read-only run leaves
    #: it out. Not `mutates`, which also tells the loop a call may have changed the
    #: workspace.
    operates: bool = False
    #: True when the model always sees the full definition. Other tools are announced by
    #: name and loaded on request: a definition sits in the context window on every step,
    #: so a rarely used one is paid for constantly.
    core: bool = False
    #: True when this tool can only answer after the conversation has been folded at least
    #: once. Before that, everything it can reach is already in the window, so its
    #: definition would cost tokens on every step and buy nothing.
    after_compaction: bool = False
    #: One line for the catalog of tools that are not loaded: a dozen tokens, not hundreds.
    summary: str = ""
    #: What this tool's results are, so the core decides retention from a declaration, not
    #: from a list of tool names. A result may refine it.
    provenance: Provenance = field(default_factory=Provenance)
    #: Parameters the tool uses exactly as written: a file's text, an edit's text, a shell
    #: command. Repairs never strip quotes from them, since the quotes are part of the text.
    verbatim: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SamplingParams:
    #: Defaults follow Qwen's published recommendation for agentic coding.
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 40
    #: Optional so ordinary sessions keep each endpoint's random-seed behaviour.
    #: Benchmarks set it and record it on every model attempt.
    seed: int | None = None
    max_tokens: int = 4096
    stop: tuple[str, ...] = ()
    #: Anti-repetition samplers, all optional so an entry that sets none sends nothing and
    #: the wire is byte-identical to before these fields existed. llama-server leaves every
    #: one disabled by default (repeat_penalty 1.0, dry_multiplier 0.0), so a quantized model
    #: at low temperature can lock into a verbatim loop; a config opts in per model. Field
    #: names match llama.cpp's request keys (llama.cpp b10331: repeat_penalty, repeat_last_n,
    #: presence_penalty, frequency_penalty, min_p, dry_multiplier, dry_base,
    #: dry_allowed_length, dry_penalty_last_n). DRY is preferred for loops: it penalizes long
    #: verbatim repeats without touching the short ones code needs.
    repeat_penalty: float | None = None
    repeat_last_n: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    min_p: float | None = None
    dry_multiplier: float | None = None
    dry_base: float | None = None
    dry_allowed_length: int | None = None
    dry_penalty_last_n: int | None = None

    #: An anti-repetition field left unset is left out of the repr, so a params object that
    #: sets none reads exactly as it did before these fields existed. The benchmark's golden
    #: capture records `repr(params)`, so this keeps a default run byte-identical while still
    #: showing any field a model does set -- a measured model that opts in is not hidden.
    _EXTRA_FIELDS = (
        "repeat_penalty",
        "repeat_last_n",
        "presence_penalty",
        "frequency_penalty",
        "min_p",
        "dry_multiplier",
        "dry_base",
        "dry_allowed_length",
        "dry_penalty_last_n",
    )

    def __repr__(self) -> str:
        base = (
            f"SamplingParams(temperature={self.temperature!r}, top_p={self.top_p!r}, "
            f"top_k={self.top_k!r}, seed={self.seed!r}, max_tokens={self.max_tokens!r}, "
            f"stop={self.stop!r}"
        )
        extras = [
            f"{name}={value!r}"
            for name in self._EXTRA_FIELDS
            if (value := getattr(self, name)) is not None
        ]
        return base + (", " + ", ".join(extras) if extras else "") + ")"


@dataclass(frozen=True, slots=True)
class RequestOptions:
    """Per-request switches the composition root may set from the model entry or the
    settings.

    Every field defaults to None, meaning "send nothing about it". A provider with default
    options is byte-identical to one that never heard of them.
    """

    #: Force tool definitions on the wire (True) or described in the prompt (False).
    native_tools: bool | None = None
    parallel_tool_calls: bool | None = None
    #: Thinking on or off, per request, through the chat template.
    reasoning: bool | None = None
    #: The most output tokens one reply may spend thinking before the server closes the
    #: thought and the rest of `max_tokens` goes to the answer. It is a server extension,
    #: so the composition root sets it only for an entry whose host takes them
    #: (`ModelEntry.takes_server_extensions`), and it reaches the wire only for a model known
    #: to reason (`OpenAIProvider._reasoning_budget`).
    reasoning_budget: int | None = None
    #: The composer's thinking level (`config.settings.THINKING_LEVELS`) for a provider with
    #: its own ladder: Claude maps it to ``output_config.effort`` and its thinking switch
    #: (`AnthropicProvider._effort`). Never on the wire as such.
    thinking_level: str | None = None
    #: A client-side deadline in seconds for this one request. Never on the wire: httpx
    #: drops the connection at this point, so a hung request ends near that deadline instead
    #: of at the endpoint's default timeout.
    timeout: float | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this endpoint can do, which decides how hard the harness works."""

    native_tool_calls: bool = True
    parallel_tool_calls: bool = False
    #: llama.cpp only. Used strictly as a last resort; see provider/repair.py.
    supports_grammar: bool = False
    reasoning_channel: bool = False
    max_context: int = 32_768
    #: False when `max_context` is an assumption, not a figure the catalog or the service
    #: gave (an entry with no `ctx`, or a Claude model the catalog does not recognize).
    #: The loop still works to it; the window's context chip
    #: does not show it as the model's real window.
    context_known: bool = True
    #: True when the model takes images (`ImageBlock`): every Claude model; a local
    #: llama-server whose `/props` says `modalities.vision` (a projector is loaded); an
    #: entry with `args.vision = true`. False otherwise, and then an attached image reaches
    #: the model as its marker, saying it cannot be seen, never as bytes it would reject.
    vision: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class Usage:
    """What one request cost, in tokens.

    ``input_tokens`` is the whole prompt, cached or not, as every budget and the context
    window count it. The two cache fields say how much of it a prompt cache wrote or
    served, so a price can charge those tokens at their own rate. This ``input_tokens`` is
    not the Messages API's own, which is only the uncached remainder
    (`shared/prompt-caching.md`, "input_tokens is the uncached remainder only").
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __repr__(self) -> str:
        # The generated repr exactly while nothing was cached (every endpoint without a
        # prompt cache), so a `Usage` recorded before the cache fields existed reads back
        # byte for byte.
        text = f"Usage(input_tokens={self.input_tokens!r}, output_tokens={self.output_tokens!r}"
        if self.cache_creation_input_tokens or self.cache_read_input_tokens:
            text += (
                f", cache_creation_input_tokens={self.cache_creation_input_tokens!r}"
                f", cache_read_input_tokens={self.cache_read_input_tokens!r}"
            )
        return text + ")"


#: ``repetition`` is the harness's own: it cut a reply that had become a loop
#: (`loop.chant`), never something a provider sends.
StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "cancelled", "repetition"
]


@dataclass(frozen=True, slots=True)
class Timing:
    """How fast the model produced its output, as the stream itself reported it.

    ``server`` is the server's own measurement (llama.cpp's final ``timings`` object).
    ``stream`` is this process's clock from the first streamed delta to the last line, over
    the tokens after the first. Read only by the desktop's usage display; nothing that
    decides what a model is sent looks at it.
    """

    output_tokens: int
    seconds: float
    source: Literal["server", "stream"]

    @property
    def tokens_per_second(self) -> float | None:
        if self.output_tokens <= 0 or self.seconds <= 0:
            return None
        return self.output_tokens / self.seconds


@dataclass(frozen=True, slots=True)
class RateLimit:
    """One per-minute limiter as a response's headers reported it (`provider/ratelimit.py`):
    its size, what is left, and when it is whole again. Read only by the desktop's usage
    display; nothing that decides what a model is sent looks at it."""

    limit: int
    remaining: int
    #: When it is fully replenished, ISO 8601 in UTC; None when the response did not say.
    reset: str | None = None

    @property
    def fraction(self) -> float | None:
        """What is left of it, 0 to 1; None for a limiter of size zero."""
        if self.limit <= 0:
            return None
        return max(0.0, min(1.0, self.remaining / self.limit))

    def as_dict(self) -> dict[str, int | str | None]:
        return {"remaining": self.remaining, "limit": self.limit, "reset": self.reset}


@dataclass(frozen=True, slots=True)
class Completion:
    """One finished assistant turn, before any tool-call extraction has happened."""

    message: Message
    usage: Usage
    stop_reason: StopReason
    #: Everything the model emitted as text, kept verbatim so repair strategies can work
    #: on the original rather than on something already reshaped.
    raw_text: str = ""
    #: Tool calls the endpoint itself parsed, when it supports them natively.
    native_tool_calls: tuple[ToolUseBlock, ...] = ()
    model: str = ""
    #: How fast it was produced, when the stream said; see `Timing`.
    timing: Timing | None = None
    #: Why the service stopped, when it said more than the stop reason: a ``refusal``'s
    #: ``category`` and ``explanation``, as the Messages API's ``stop_details`` gives them.
    stop_details: Mapping[str, str | None] | None = None
    #: The per-minute limiters the response's headers reported, by name (``requests``,
    #: ``tokens``, ``input_tokens``, ``output_tokens``); None when it sent none, which is
    #: every local server. See `RateLimit`.
    rate_limits: Mapping[str, RateLimit] | None = None
    #: The endpoint stopped this sample at the output limit. Separate from `stop_reason`,
    #: which says `tool_use` whenever a native call arrived: the limit can cut the last of
    #: those calls off, and `provider/normalize.py` must still know it did.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A fragment for the UI. Never accumulated into a parsing decision."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """A tool call is still streaming: its name, the ``path`` argument once the partial
    JSON holds it whole (key and closing quote, even while the object itself stays open),
    and how much of its arguments has arrived so far. For the window's live row only
    (design need: a tool call can take the model a long time to finish, with nothing
    shown meanwhile); never sent to the model and never part of a `Completion`. A
    dialect throttles how often it yields one (`openai_api`'s ~4/s): a token-by-token
    delta would flood the window exactly when the model is already slow.
    """

    index: int
    name: str
    path: str | None
    #: Characters of arguments streamed so far, real data (len of what arrived), never an
    #: estimate.
    chars: int
    #: Literal ``\\n`` escapes counted in the arguments streamed so far -- a real count of
    #: what the model has sent, not a guess at how long the finished file will be.
    lines: int


@dataclass(frozen=True, slots=True)
class Completed:
    """Always the final chunk of a stream."""

    completion: Completion


Chunk = TextDelta | ThinkingDelta | ToolCallDelta | Completed


@runtime_checkable
class Provider(Protocol):
    """One wire dialect. There are exactly two: OpenAI-shaped and Anthropic-shaped."""

    name: str

    def capabilities(self) -> Capabilities: ...

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        params: SamplingParams,
        cancel: CancelToken,
    ) -> Iterator[Chunk]:
        """Yield deltas for the UI, ending with exactly one Completed chunk."""
        ...


def complete(
    provider: Provider,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    *,
    params: SamplingParams,
    cancel: CancelToken,
    on_delta: Callable[[TextDelta | ThinkingDelta | ToolCallDelta], None] | None = None,
) -> Completion:
    """Drain a stream and return its Completion. The loop's normal entry point.

    ``on_delta`` sees each text and thinking fragment as it arrives, and a tool call's
    progress while it streams, for a window to show. It never changes what is returned."""
    last: Completed | None = None
    for chunk in provider.stream(messages, tools, params=params, cancel=cancel):
        if isinstance(chunk, Completed):
            last = chunk
        elif on_delta is None:
            continue
        elif isinstance(chunk, ToolCallDelta):
            on_delta(chunk)
        elif chunk.text:
            on_delta(chunk)
    if last is None:
        raise RuntimeError(f"provider {provider.name!r} ended its stream without a Completion")
    return last.completion


class StoppedBeforeReply(ProviderError):
    """Stop was pressed while a request was still waiting on the server: in its prefill,
    or hung. Nothing it produced is kept."""


#: How long a stopped request may take to end on its own. A stream already sending
#: lines checks its token on the next one and returns what it has, cut short.
STOP_GRACE_SECONDS = 0.5
_STOP_POLL_SECONDS = 0.1


class _RequestToken(CancelToken):
    """The token one request runs under: cancelled with the turn's token, and also on its
    own once the request is left behind, so that a later turn's reset cannot revive it.
    Cancelling it cancels the turn, as cancelling the turn's own token would."""

    __slots__ = ("_turn",)

    def __init__(self, turn: CancelToken) -> None:
        super().__init__()
        self._turn = turn

    def stop(self, reason: str) -> None:
        """This request only."""
        super().cancel(reason)

    def cancel(self, reason: str = "user interrupted") -> None:
        super().cancel(reason)
        self._turn.cancel(reason)

    @property
    def cancelled(self) -> bool:
        return super().cancelled or self._turn.cancelled

    @property
    def reason(self) -> str:
        return super().reason or self._turn.reason

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.cancelled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            super().wait(min(remaining, _STOP_POLL_SECONDS))
        return True


def complete_stoppable(
    provider: Provider,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    *,
    params: SamplingParams,
    cancel: CancelToken,
    on_delta: Callable[[TextDelta | ThinkingDelta | ToolCallDelta], None] | None = None,
    halt: Callable[[TextDelta | ThinkingDelta], bool] | None = None,
) -> Completion:
    """`complete`, but a stop ends it at once even before the first byte.

    ``halt`` sees each text and thinking fragment; when it returns true this request is
    stopped (never the turn), and what it produced so far comes back with the stop reason
    the provider gives a stop, ``cancelled``. The caller knows why it halted.

    The providers check their token only between streamed lines, and a blocking read
    cannot be interrupted from another thread (see `OpenAIProvider.stream`). So the
    request runs in a worker thread with a token of its own. On stop that token is set,
    the worker gets `STOP_GRACE_SECONDS` to end cleanly, and is otherwise left to finish
    or time out on its own while this raises `StoppedBeforeReply`. Its own token is never
    reset, so a later turn cannot revive it.

    ``on_delta`` is called from the worker thread, and never after the stop: a request
    left behind streams nothing more into the window.
    """
    if cancel.cancelled:
        raise StoppedBeforeReply(f"{provider.name}: stopped before the request was sent")
    own = _RequestToken(cancel)
    outcome: list[Completion | BaseException] = []
    done = threading.Event()

    def deliver(chunk: TextDelta | ThinkingDelta | ToolCallDelta) -> None:
        if own.cancelled:
            return
        if on_delta is not None:
            on_delta(chunk)
        if halt is not None and not isinstance(chunk, ToolCallDelta) and halt(chunk):
            own.stop("repetition")

    def drain() -> None:
        try:
            outcome.append(
                complete(
                    provider,
                    messages,
                    tools,
                    params=params,
                    cancel=own,
                    on_delta=deliver if on_delta is not None or halt is not None else None,
                )
            )
        except BaseException as exc:  # handed to the caller's thread below
            outcome.append(exc)
        finally:
            done.set()

    threading.Thread(target=drain, name="cogito-provider-request", daemon=True).start()
    while not done.wait(_STOP_POLL_SECONDS):
        if cancel.cancelled:
            own.stop(cancel.reason)
            if done.wait(STOP_GRACE_SECONDS):
                break
            raise StoppedBeforeReply(f"{provider.name}: stopped before the reply arrived")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result
