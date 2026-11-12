"""The Anthropic-shaped wire dialect: Claude over the Messages API.

The second of the two dialects, reached through the official ``anthropic`` SDK. Its
contract matches the OpenAI dialect's point for point: the same `Provider` protocol,
deltas for the UI and exactly one `Completed`, stop reasons in the same vocabulary, native
tool calls reported for `provider/normalize.py` to repair, `RequestOptions.timeout` as the
request's deadline, a `ServiceError` with the provider's own status, code, message and
``retry-after``, and a cancelled stream that ends in a ``cancelled`` completion.

Every rule about the API below comes from the claude-api reference bundled with Claude Code
2.1.280 (read 2026-09-29), named by file and section where it is applied: ``shared/`` for
the concepts, ``python/claude-api/`` for the SDK.

Four decisions shape this module:

- **Retries belong to Cogito.** The SDK retries 429, 5xx and dropped connections on its own
  (``python/claude-api/README.md``, "Retries"). Here ``max_retries=0``: the loop's own
  attempts, `ServiceError` and the server's resume are the retry policy, and a second one
  underneath would multiply it.
- **Tool calls come from the finished message, never the stream** (`provider/base.py`).
  So a tool's input stays buffered, the API's default, and the server validates each
  parameter before emitting it. ``shared/tool-use-concepts.md`` ("Eager input streaming")
  recommends ``eager_input_streaming`` for every streamed request with tools. It buys
  earlier fragments of a tool's input, which nothing here reads, and costs the server's
  validation, so it is not used.
- **Prompt caching is a prefix match** (``shared/prompt-caching.md``). One breakpoint on
  the static system prompt caches the tools and the system together. One on the last block
  of the transcript caches the conversation, so the next step reads everything before it
  and writes only what it adds. The loop's per-step status line (`Message.volatile`) is the
  one thing that changes every step, and it always comes after both.
- **The key is passed explicitly, or no client is built.** Given no key, the SDK would look
  for credentials of its own (environment, profiles on disk). A request that cannot say
  whose key it spends is refused before it is made, with the same sentence the rest of
  Cogito uses for a missing key.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx2

from cogito.cancel import CancelToken
from cogito.config import secrets
from cogito.config.models import ModelEntry
from cogito.errors import ProviderError, ServiceError
from cogito.provider import ratelimit
from cogito.provider.base import (
    Capabilities,
    Chunk,
    Completed,
    Completion,
    ImageBlock,
    Message,
    RequestOptions,
    SamplingParams,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
    image_data,
    recent_result_images,
    missing_image,
)
from cogito.provider.openai_api import (
    _ERROR_BODY_CHARS,
    _error_detail_code,
    _error_fields,
    _resumes_at,
    _retry_after,
    _stream_timing,
)

log = logging.getLogger(__name__)

#: How often the caller checks its cancel token while it waits for the next event. The
#: stream is read on its own thread, so this bounds how late a cancellation is noticed,
#: whatever the socket is doing (including the silence of a long prefill).
_POLL_SECONDS = 0.05

#: A 5-minute cache entry, the default TTL. Each request sharing the prefix within one turn
#: starts well under five minutes after the last, the case where the 5-minute TTL is
#: cheaper (``shared/prompt-caching.md``, "Choosing the TTL").
_EPHEMERAL: Mapping[str, str] = {"type": "ephemeral"}

#: Content blocks a ``cache_control`` may sit on (``shared/prompt-caching.md``, "API
#: reference"). A thinking block is not one of them.
_CACHEABLE = frozenset({"text", "tool_use", "tool_result"})

#: The API's error types and the status each one is sent with (``shared/error-codes.md``,
#: "Error Code Summary"). Read only for an ``error`` event inside a stream that already
#: began with 200, where the HTTP status says nothing about what went wrong.
_STATUS_OF_TYPE: Mapping[str, int] = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "billing_error": 402,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "overloaded_error": 529,
}

#: The Messages API's stop reasons (the SDK's ``StopReason``) in this harness's vocabulary.
#: ``pause_turn`` is for server-side tools, which are never offered here, and
#: ``model_context_window_exceeded`` is output cut short by the window: a truncation.
_STOP_REASONS: Mapping[str, str] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "refusal",
    "pause_turn": "end_turn",
    "model_context_window_exceeded": "max_tokens",
}

#: The smallest thinking budget the API accepts; it must also stay below ``max_tokens``
#: (``python/claude-api/README.md``, "Extended Thinking": "must be < max_tokens, min 1024").
_MIN_THINKING_BUDGET = 1024
#: The smallest ``max_tokens`` for effort ``xhigh`` or ``max`` (the claude-api reference
#: bundled with Claude Code 2.1.304, ``shared/model-migration.md``: "set max_tokens >= 64000
#: (otherwise output truncates mid-thought)").
_HIGH_EFFORT_MAX_TOKENS = 64_000

#: Tool-use ids the API accepts. A transcript can hold ids minted by another dialect (the
#: picker switches models mid-session), so every id is mapped onto this alphabet, the same
#: way for a call and for its result.
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


# --------------------------------------------------------------------------------------
# What each model accepts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelTraits:
    """What one Claude model accepts, from the reference, never guessed from its name.

    ``thinking`` is how it is switched:

    - ``budget``: ``{"type": "enabled", "budget_tokens": N}``; off unless asked for.
    - ``adaptive``: ``{"type": "adaptive"}``; off unless asked for.
    - ``adaptive_on``: adaptive, and on by default, so off is ``{"type": "disabled"}``.
    - ``always``: it always thinks; ``disabled`` is a 400, so off is sending nothing.
    """

    thinking: str
    #: Its thinking text is omitted unless ``display: "summarized"`` asks for it.
    summarized_on_request: bool
    #: Whether ``temperature``/``top_p``/``top_k`` are accepted at all.
    sampling: bool
    #: Its context window in tokens, the ``ctx`` an entry does not set.
    context: int
    #: The ``output_config.effort`` levels it takes, lowest first; empty where it takes none.
    efforts: tuple[str, ...] = ()
    #: The effort it runs at when none is sent.
    default_effort: str | None = None
    #: How an ``adaptive_on`` model is switched off: ``disabled``, or ``between_tools`` where
    #: ``disabled`` is a 400 (Sonnet 5.5).
    off: str = "disabled"


_ONE_MILLION = 1_000_000
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: ``xhigh`` arrived with Opus 4.7; the 4.6 line has the other four.
_EFFORTS_46 = ("low", "medium", "high", "max")
#: For a model the reference gives no window for; an entry's ``ctx`` overrides it.
_DEFAULT_CONTEXT = 200_000

#: Sources, all in the bundled reference: thinking from ``python/claude-api/README.md``
#: ("Extended Thinking"), ``shared/error-codes.md`` ("Model-specific 400s") and
#: ``shared/model-migration.md`` (Opus 5 "thinking is on by default" and disabled only at
#: effort high or lower, the default; Sonnet 5 adaptive on by default; Fable 5.x "Thinking
#: is always on"; Opus 5.5 "thinking can't be disabled"). The summarized-display default
#: from the README's note on ``display``. Sampling from ``python/claude-api/sdk-upgrade.md``
#: (Step 6: rejected on Opus 4.7 and later, non-default values rejected on Sonnet 5,
#: accepted by the 4.6/4.5 line and the Claude 4 models). Windows from
#: ``shared/models.md``'s tables; the legacy models have none listed there. Effort levels
#: and defaults from the reference's "Thinking & Effort" table (the claude-api skill bundled
#: with Claude Code 2.1.304, cached 2026-10-06): every 5.x and Opus 4.7/4.8 take ``low``
#: through ``max``, the 4.6 line has no ``xhigh``, Opus 5.5 and Haiku 5.5 default to
#: ``medium`` and the rest to ``high``; Haiku 4.5 takes no effort. Sonnet 5.5 refuses
#: ``disabled`` and is switched off with ``between_tools``; Haiku 5.5 thinks by default,
#: ``disabled`` accepted at ``high`` or below.
_TRAITS: Mapping[str, ModelTraits] = {
    "claude-fable-5-1": ModelTraits("always", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-mythos-5-1": ModelTraits("always", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-fable-5": ModelTraits("always", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-mythos-5": ModelTraits("always", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-opus-5-5": ModelTraits("always", True, False, _ONE_MILLION, _EFFORTS, "medium"),
    "claude-opus-5": ModelTraits("adaptive_on", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-sonnet-5-5": ModelTraits(
        "adaptive_on", True, False, _ONE_MILLION, _EFFORTS, "high", off="between_tools"
    ),
    "claude-sonnet-5": ModelTraits("adaptive_on", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-haiku-5-5": ModelTraits("adaptive_on", True, False, _ONE_MILLION, _EFFORTS, "medium"),
    "claude-opus-4-8": ModelTraits("adaptive", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-opus-4-7": ModelTraits("adaptive", True, False, _ONE_MILLION, _EFFORTS, "high"),
    "claude-opus-4-6": ModelTraits("adaptive", False, True, _ONE_MILLION, _EFFORTS_46, "high"),
    "claude-sonnet-4-6": ModelTraits("adaptive", False, True, _ONE_MILLION, _EFFORTS_46, "high"),
    "claude-haiku-4-5": ModelTraits("budget", False, True, 200_000),
    "claude-opus-4-5": ModelTraits("budget", False, True, _DEFAULT_CONTEXT),
    "claude-sonnet-4-5": ModelTraits("budget", False, True, _DEFAULT_CONTEXT),
    "claude-opus-4-1": ModelTraits("budget", False, True, _DEFAULT_CONTEXT),
    "claude-opus-4-0": ModelTraits("budget", False, True, _DEFAULT_CONTEXT),
    "claude-opus-4": ModelTraits("budget", False, True, _DEFAULT_CONTEXT),
    "claude-sonnet-4-0": ModelTraits("budget", False, True, _DEFAULT_CONTEXT),
    "claude-sonnet-4": ModelTraits("budget", False, True, _DEFAULT_CONTEXT),
}

#: A model the table does not know: adaptive thinking (the current recommendation) and no
#: sampling parameters, which the newest models reject.
_UNKNOWN = ModelTraits("adaptive", False, False, _DEFAULT_CONTEXT)


def traits(model: str) -> ModelTraits:
    """The traits of an alias (``claude-haiku-4-5``) or of a dated snapshot of one
    (``claude-haiku-4-5-20251001``). Matching is exact, so ``claude-opus-5-5`` is never
    read as ``claude-opus-5``."""
    name = model.strip().lower()
    if name in _TRAITS:
        return _TRAITS[name]
    head, _, date = name.rpartition("-")
    if head in _TRAITS and len(date) == 8 and date.isdigit():
        return _TRAITS[head]
    return _UNKNOWN


def context_window(entry: ModelEntry) -> int:
    """The entry's ``ctx`` when it sets one, else the model's own window."""
    declared = entry.args.get("ctx")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    return traits(entry.model).context


def context_window_known(entry: ModelEntry) -> bool:
    """Whether `context_window` is a fact (a declared ``ctx``, or a model this table
    recognizes) rather than ``_UNKNOWN``'s 200,000-token guess -- the same distinction
    `provider.openai_api.OpenAIProvider.capabilities` makes from ``"ctx" in entry.args``
    for its own `Capabilities.context_known`. This dialect's `capabilities()` reports it
    as that field, and `cli._app_output_cap` (APP-ONLY) calls this function directly."""
    declared = entry.args.get("ctx")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return True
    return traits(entry.model) is not _UNKNOWN


# --------------------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------------------


def _wire_id(identifier: str) -> str:
    return _UNSAFE_ID.sub("_", identifier) or "toolu_cogito"


def _rendered_call(use: ToolUseBlock) -> str:
    """A call as text, the way `Message.transcript_text` renders it."""
    arguments = ", ".join(f"{key}={value!r}" for key, value in use.input.items())
    return f"[calls {use.name}({arguments})]"


def _thinking_block(block: ThinkingBlock) -> dict[str, Any]:
    if block.redacted:
        return {"type": "redacted_thinking", "data": block.signature}
    return {"type": "thinking", "thinking": block.text, "signature": block.signature}


def _image(block: ImageBlock) -> dict[str, Any]:
    data = image_data(block)
    if data is None:
        return {"type": "text", "text": missing_image(block)}
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": block.media_type, "data": data},
    }


def _content(
    message: Message, role: str, tools_on_wire: bool, kept: frozenset[int] = frozenset()
) -> list[dict[str, Any]]:
    """One message's blocks, in the order the API wants them.

    A user turn puts its tool results before any text. An assistant turn puts its signed
    reasoning first, then its prose, then its calls. When no tool definitions go on the
    wire (a compaction summary, a model told its tools in the prompt), calls and results
    are sent as the text they read as. The reference only shows ``tool_use`` blocks in
    requests that define ``tools`` and does not say whether the API accepts them
    otherwise; text is valid either way.
    """
    thinking: list[dict[str, Any]] = []
    first: list[dict[str, Any]] = []
    pictures: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for block in message.blocks:
        match block:
            case TextBlock(text=text):
                if text.strip():
                    texts.append({"type": "text", "text": text})
            case ImageBlock() if role == "user":
                data = image_data(block)
                if data is None:
                    texts.append({"type": "text", "text": missing_image(block)})
                else:
                    pictures.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": block.media_type,
                                "data": data,
                            },
                        }
                    )
            case ThinkingBlock() if role == "assistant" and block.signature:
                thinking.append(_thinking_block(block))
            case ToolUseBlock() if role == "assistant":
                if tools_on_wire:
                    calls.append(
                        {
                            "type": "tool_use",
                            "id": _wire_id(block.id),
                            "name": block.name,
                            "input": dict(block.input),
                        }
                    )
                else:
                    calls.append({"type": "text", "text": _rendered_call(block)})
            case ToolResultBlock(tool_use_id=use_id, content=content, is_error=is_error):
                if tools_on_wire:
                    result: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": _wire_id(use_id),
                        "content": content,
                    }
                    # A screenshot rides inside its result, after the words, as the
                    # computer-use tool returns one; only the newest few are sent.
                    sent = [_image(image) for image in block.images if id(image) in kept]
                    if sent:
                        result["content"] = [{"type": "text", "text": content}, *sent]
                    if is_error:
                        result["is_error"] = True
                    first.append(result)
                else:
                    label = "error" if is_error else "result"
                    first.append({"type": "text", "text": f"[{label}] {content}"})
    if role == "assistant":
        return [*thinking, *texts, *calls]
    # An image before the words about it, as Anthropic's vision guide recommends.
    return [*first, *pictures, *texts]


def _mark(block: dict[str, Any]) -> None:
    block["cache_control"] = dict(_EPHEMERAL)


def encode(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    *,
    tools_on_wire: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """``(system, messages, tools)`` as the Messages API takes them, with breakpoints placed.

    Rendered order is tools, then system, then messages (``shared/prompt-caching.md``, "The
    one invariant"). Two of the four allowed breakpoints are used:

    1. The last system block (the last tool when there is no system): tools and system
       together, the part that never changes.
    2. The last cacheable block of the last message that will still be there next step,
       only when the request offers tools (the agent loop, whose next step re-sends it). A
       compaction summary or a context probe offers none and is asked once; marking its
       tail would pay the write premium on bytes nothing reads back ("Automatic vs
       explicit breakpoints"). A volatile message is never marked and always sits after
       the mark.

    Below a model's minimum cacheable prefix (4096 tokens on Haiku 4.5, 512 to 4096
    elsewhere) a breakpoint is a silent no-op, "no error, just
    ``cache_creation_input_tokens: 0``". So the marks are placed without counting tokens
    and cost nothing where they cannot cache.
    """
    system: list[dict[str, Any]] = []
    encoded: list[dict[str, Any]] = []
    last_kept = -1
    kept = frozenset(recent_result_images(messages))
    for message in messages:
        if message.role == "system":
            text = message.flat_text()
            if text.strip():
                system.append({"type": "text", "text": text})
            continue
        role = "assistant" if message.role == "assistant" else "user"
        content = _content(message, role, tools_on_wire, kept)
        if message.volatile:
            # The loop's per-step status. As plain user text after a tool result, Claude
            # Haiku 4.5 answered it instead of the person in 7 of 8 turns of one conversation
            # in the real app (2026-10-16). Claude models read harness text inside
            # `<system-reminder>` as not the user's; Claude Code sends its own that way.
            content = [
                {**block, "text": f"<system-reminder>\n{block['text']}\n</system-reminder>"}
                if block.get("type") == "text"
                else block
                for block in content
            ]
        if not content:
            # The API refuses a message with nothing in it; an empty assistant turn (only
            # unsigned reasoning, say) carries nothing a later request needs.
            continue
        encoded.append({"role": role, "content": content})
        if not message.volatile:
            last_kept = len(encoded) - 1

    wire_tools = (
        [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.parameters),
            }
            for tool in tools
        ]
        if tools_on_wire
        else []
    )

    if system:
        _mark(system[-1])
    elif wire_tools:
        _mark(wire_tools[-1])
    if tools and last_kept >= 0:
        cacheable = [b for b in encoded[last_kept]["content"] if b["type"] in _CACHEABLE]
        if cacheable:
            _mark(cacheable[-1])
    return system, encoded, wire_tools


# --------------------------------------------------------------------------------------
# Reading the stream on its own thread
# --------------------------------------------------------------------------------------


def _shut_socket(response: Any) -> None:
    """Best effort: shut the socket under a response, which wakes a read blocked on it in
    another thread. Closing alone does not reliably do that (measured for the OpenAI
    dialect; its comment explains). `shutdown` does, when the transport exposes it."""
    # Only a courtesy: a transport without the extension, or a socket already gone, leaves
    # the `stream.close()` in `_Pump.abandon` to do what it can.
    with contextlib.suppress(Exception):
        stream = response.extensions.get("network_stream")
        sock = stream.get_extra_info("socket") if stream is not None else None
        if isinstance(sock, socket.socket):
            sock.shutdown(socket.SHUT_RDWR)


class _Pump:
    """One SDK stream, read on its own thread and handed over through a queue.

    The caller waits on the queue a few milliseconds at a time and checks its cancel token
    in between, so a cancellation is noticed at once whether or not the socket has anything
    to say (a long prefill, a buffered tool input and a stalled server are all silence).
    Abandoning shuts the socket and closes the stream, so the reading thread ends as soon
    as the transport lets it; nothing it reads afterwards is used.
    """

    def __init__(self, open_stream: Callable[[], Any]) -> None:
        self._open = open_stream
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._stream: Any = None
        self._abandoned = False
        #: The response's headers once it began, for the per-minute limits.
        self.headers: dict[str, str] = {}
        self._thread = threading.Thread(
            target=self._run, name="cogito-claude-stream", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def next(self, timeout: float) -> tuple[str, Any]:
        return self._queue.get(timeout=timeout)

    def _run(self) -> None:
        try:
            with self._open() as stream:
                with self._lock:
                    if self._abandoned:
                        return
                    self._stream = stream
                # Observed only; a response without the headers (a fake, a proxy) has none.
                with contextlib.suppress(Exception):
                    self.headers = dict(stream.response.headers.items())
                for event in stream:
                    if self._abandoned:
                        return
                    self._queue.put(("event", event))
                try:
                    snapshot = stream.current_message_snapshot
                except AssertionError:
                    snapshot = None
                self._queue.put(("done", snapshot))
        except BaseException as exc:  # noqa: BLE001 - handed to the caller, which decides
            self._queue.put(("error", exc))

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            stream = self._stream
        if stream is not None:
            _shut_socket(stream.response)
            with contextlib.suppress(Exception):
                stream.close()


# --------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------


class AnthropicProvider:
    """A Claude model behind the Messages API."""

    def __init__(
        self,
        entry: ModelEntry,
        api_key: str | None = None,
        *,
        timeout: float = 600.0,
        http_client: httpx2.Client | None = None,
        options: RequestOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
        _client: anthropic.Anthropic | None = None,
    ) -> None:
        self._entry = entry
        self._timeout = timeout
        self._options = options or RequestOptions()
        self._clock = clock
        self._traits = traits(entry.model)
        # A base URL written with the OpenAI habit of a trailing /v1: the SDK adds its own.
        base = entry.base_url.rstrip("/")
        self._base_url = base[: -len("/v1")] if base.endswith("/v1") else base
        self._endpoint = f"{self._base_url}/v1/messages"
        self._client = _client
        if self._client is None and api_key:
            self._client = anthropic.Anthropic(
                api_key=api_key,
                base_url=self._base_url,
                max_retries=0,
                timeout=httpx2.Timeout(timeout, connect=10.0),
                http_client=http_client,
            )
        #: True for a `configured` copy: the client belongs to the provider it came from.
        self._shared = False
        self.name = entry.id

    @property
    def options(self) -> RequestOptions:
        """The per-request options this provider sends (`configured` makes another)."""
        return self._options

    def configured(self, options: RequestOptions) -> AnthropicProvider:
        """The same endpoint with other per-request options, sharing this client. Asking
        for the options it already has returns the provider itself."""
        if options == self._options:
            return self
        copy = AnthropicProvider(
            self._entry,
            timeout=self._timeout,
            options=options,
            clock=self._clock,
            _client=self._client,
        )
        copy._shared = True
        return copy

    # -- what it can do ------------------------------------------------------------------

    def _thinking_requested(self) -> bool:
        level = self._options.thinking_level
        if level == "off":
            return False
        if level in ("normal", "long", "free"):
            return True
        if self._options.reasoning is not None:
            return self._options.reasoning
        return self._entry.args.get("reasoning") is True

    def _effort(self) -> str | None:
        """``output_config.effort`` for the composer's thinking level, or None to send none
        (the model's own default). «No» on a model that always thinks is its lowest level;
        «Normal» thinks at its default; «Más» is one above it; «Sin límite» its highest.
        A model with no effort levels, or thinking off on one that can switch it off,
        sends none."""
        levels = self._traits.efforts
        level = self._options.thinking_level
        if not levels or level is None:
            return None
        if level == "off":
            return levels[0] if self._traits.thinking == "always" else None
        if level == "free":
            return levels[-1]
        if level == "long":
            default = self._traits.default_effort
            at = levels.index(default) if default in levels else len(levels) - 1
            return levels[min(at + 1, len(levels) - 1)]
        return None

    def capabilities(self) -> Capabilities:
        native = (
            bool(self._entry.args.get("native_tools", True))
            if self._options.native_tools is None
            else self._options.native_tools
        )
        return Capabilities(
            native_tool_calls=native,
            parallel_tool_calls=True,
            supports_grammar=False,
            max_context=context_window(self._entry),
            # Only the context ring reads this (`Engine.context_status`): a model the table
            # does not recognize is not shown against the 200,000-token guess. What is sent
            # does not change; the window a refusal names is learned either way.
            context_known=context_window_known(self._entry),
            # Every Claude model takes images (Anthropic's vision guide, 2026-10-17).
            vision=True,
        )

    # -- the request ---------------------------------------------------------------------

    def _thinking(self, max_tokens: int) -> dict[str, Any] | None:
        """The ``thinking`` field for this model, or None to send none; see `ModelTraits`."""
        kind = self._traits.thinking
        wanted = self._thinking_requested()
        display = {"display": "summarized"} if self._traits.summarized_on_request else {}
        if kind == "always":
            return {"type": "adaptive", **display} if wanted and display else None
        if not wanted:
            return {"type": self._traits.off} if kind == "adaptive_on" else None
        if kind != "budget":
            return {"type": "adaptive", **display}
        configured = self._entry.args.get("thinking_budget")
        budget = (
            configured
            if isinstance(configured, int) and not isinstance(configured, bool)
            else max_tokens // 2
        )
        if self._options.thinking_level == "free":
            # «Sin límite» on a model with a token budget: three quarters of the reply, so a
            # quarter is left to answer (as Motoris does without a budget).
            budget = max(budget, max_tokens * 3 // 4)
        budget = min(budget, max_tokens - 1)
        if budget < _MIN_THINKING_BUDGET:
            log.warning(
                "%s: thinking needs max_tokens above %d; this request goes without it",
                self._entry.id,
                _MIN_THINKING_BUDGET,
            )
            return None
        return {"type": "enabled", "budget_tokens": budget}

    def _sampling(self, params: SamplingParams, thinking: Mapping[str, Any] | None) -> dict:
        """Only what ``models.toml`` sets, only on a model that takes it, and never while it
        thinks. On the Claude 4 line ``temperature`` and ``top_p`` are one or the other
        (both at once is a 400: ``shared/model-migration.md``, "Sampling parameters:
        temperature OR top_p, not both"), and ``temperature`` wins. Leaving them out while
        thinking is a precaution, not a documented rule: the local reference is silent on
        it, and a request without them cannot be refused for them."""
        if not self._traits.sampling:
            return {}
        if thinking is not None and thinking.get("type") != "disabled":
            return {}
        chosen = self._entry.sampling
        body: dict[str, Any] = {}
        if "temperature" in chosen:
            body["temperature"] = params.temperature
        elif "top_p" in chosen:
            body["top_p"] = params.top_p
        if "top_k" in chosen:
            body["top_k"] = params.top_k
        return body

    def request(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        params: SamplingParams,
    ) -> dict[str, Any]:
        """Everything `messages.stream` is called with. Public so a test can snapshot it."""
        tools_on_wire = bool(tools) and self.capabilities().native_tool_calls
        system, encoded, wire_tools = encode(messages, tools, tools_on_wire=tools_on_wire)
        request: dict[str, Any] = {
            "model": self._entry.model,
            "max_tokens": params.max_tokens,
            "messages": encoded,
        }
        if system:
            request["system"] = system
        if wire_tools:
            request["tools"] = wire_tools
            if self._options.parallel_tool_calls is False:
                request["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        if params.stop:
            request["stop_sequences"] = list(params.stop)
        thinking = self._thinking(params.max_tokens)
        if thinking is not None:
            request["thinking"] = thinking
        if (effort := self._effort()) is not None:
            request["output_config"] = {"effort": effort}
            if effort in ("xhigh", "max"):
                # At these levels the reference asks for max_tokens of at least 64,000, or
                # the reply is cut off mid-thought (claude-api skill, model-migration.md).
                request["max_tokens"] = max(params.max_tokens, _HIGH_EFFORT_MAX_TOKENS)
        # The SDK's 1.x signatures no longer take sampling parameters; the API still does
        # for the models that accept them, through `extra_body`
        # (``python/claude-api/sdk-upgrade.md``, Step 6).
        if sampling := self._sampling(params, thinking):
            request["extra_body"] = sampling
        if self._options.timeout is not None:
            request["timeout"] = self._options.timeout
        return request

    # -- streaming -----------------------------------------------------------------------

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        params: SamplingParams,
        cancel: CancelToken,
    ) -> Iterator[Chunk]:
        client = self._client
        if client is None:
            reason = secrets.missing_key_reason(self._entry.api_key_env)
            raise ServiceError(
                f"{self._entry.id}: {reason}",
                code="missing_api_key",
                endpoint=self._endpoint,
                provider_message=reason,
            )
        request = self.request(messages, tools, params)

        text_parts: list[str] = []
        raw_inputs: dict[int, str] = {}
        usage = Usage()
        stopped = False
        cancelled = False
        snapshot: Any = None
        failure: BaseException | None = None
        first_delta_at: float | None = None
        last_event_at: float | None = None

        pump = _Pump(lambda: client.messages.stream(**request))
        pump.start()
        finished = False
        try:
            while True:
                try:
                    kind, value = pump.next(_POLL_SECONDS)
                except queue.Empty:
                    if cancel.cancelled:
                        cancelled = True
                        break
                    continue
                if cancel.cancelled:
                    cancelled = True
                    break
                if kind == "error":
                    finished = True
                    failure = value
                    break
                if kind == "done":
                    finished = True
                    snapshot = value
                    break

                event = value
                last_event_at = self._clock()
                if event.type == "message_start":
                    usage = _usage(event.message.usage, usage)
                elif event.type == "message_delta":
                    usage = _usage(event.usage, usage)
                elif event.type == "message_stop":
                    stopped = True
                elif event.type == "content_block_delta":
                    if first_delta_at is None:
                        first_delta_at = last_event_at
                    delta = event.delta
                    if delta.type == "text_delta":
                        text_parts.append(delta.text)
                        yield TextDelta(delta.text)
                    elif delta.type == "thinking_delta":
                        yield ThinkingDelta(delta.thinking)
                    elif delta.type == "input_json_delta":
                        so_far = raw_inputs.get(event.index, "")
                        raw_inputs[event.index] = so_far + delta.partial_json
        finally:
            if not finished:
                # Cancelled, or the caller stopped reading: nothing more is wanted from it.
                pump.abandon()

        timing = _stream_timing(first_delta_at, last_event_at, usage)
        if cancelled or (failure is not None and cancel.cancelled):
            # The same answer the OpenAI dialect gives: what arrived, and no calls. A
            # stopped turn runs nothing it had not finished asking for.
            text = "".join(text_parts)
            yield Completed(
                Completion(
                    message=Message("assistant", (TextBlock(text),) if text else ()),
                    usage=usage,
                    stop_reason="cancelled",
                    raw_text=text,
                    model=self._entry.model,
                    timing=timing,
                )
            )
            return
        if failure is not None:
            mapped = self._failure(failure)
            if mapped is failure:
                raise failure
            raise mapped from failure
        if not stopped or snapshot is None:
            detail = "the stream ended before message_stop"
            raise ServiceError(
                f"{self._entry.id}: IncompleteStream: {detail}",
                code="IncompleteStream",
                endpoint=self._endpoint,
                provider_message=detail,
            )
        limits = ratelimit.from_headers(pump.headers)
        yield Completed(self._completion(snapshot, raw_inputs, timing, limits or None))

    def _completion(
        self,
        snapshot: Any,
        raw_inputs: Mapping[int, str],
        timing: Any,
        rate_limits: Mapping[str, Any] | None = None,
    ) -> Completion:
        thinking: list[ThinkingBlock] = []
        texts: list[str] = []
        calls: list[ToolUseBlock] = []
        for index, block in enumerate(snapshot.content):
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "thinking":
                thinking.append(ThinkingBlock(block.thinking, signature=block.signature or ""))
            elif block.type == "redacted_thinking":
                thinking.append(ThinkingBlock("", signature=block.data, redacted=True))
            elif block.type == "tool_use":
                calls.append(_tool_call(block, raw_inputs.get(index, "")))

        stop_reason = _STOP_REASONS.get(snapshot.stop_reason or "", "end_turn")
        details = None
        if snapshot.stop_details is not None:
            details = {
                "category": snapshot.stop_details.category,
                "explanation": snapshot.stop_details.explanation,
            }
        if stop_reason == "refusal":
            # A refusal can cut a tool call off mid-input: never run that turn's tools
            # (``shared/tool-use-concepts.md``, "Eager input streaming", point 2).
            log.warning(
                "%s: the model refused (category %s)",
                self._entry.id,
                details.get("category") if details else None,
            )
            calls = []

        raw_text = "".join(texts)
        blocks: tuple[Any, ...] = (*thinking, *((TextBlock(raw_text),) if raw_text else ()))
        return Completion(
            message=Message("assistant", blocks),
            usage=_usage(snapshot.usage, Usage()),
            stop_reason=stop_reason,  # type: ignore[arg-type]
            raw_text=raw_text,
            native_tool_calls=tuple(calls),
            model=self._entry.model,
            timing=timing,
            stop_details=details,
            rate_limits=rate_limits,
        )

    # -- errors --------------------------------------------------------------------------

    def _failure(self, exc: BaseException) -> BaseException:
        """The SDK's typed errors as the same `ServiceError` the OpenAI dialect raises.
        Anything else is not a service failure and is raised as it is."""
        endpoint = self._endpoint
        if isinstance(exc, anthropic.APIStatusError):
            if exc.body is None:
                body = ""
            elif isinstance(exc.body, str):
                body = exc.body
            else:
                body = json.dumps(exc.body, ensure_ascii=False)
            code, message = _error_fields(body)
            code = code or (exc.type or "")
            detail_code = _error_detail_code(body)
            status: int | None = exc.status_code
            if status is None or status < 400:
                # An `error` event inside a stream that had begun with 200 (an overload
                # mid-answer): the type names the failure, and the table its status.
                status = _STATUS_OF_TYPE.get(code)
            said = body.strip()[:_ERROR_BODY_CHARS]
            where = (
                f"HTTP {status} from {endpoint}"
                if status is not None
                else f"error from {endpoint}"
            )
            return ServiceError(
                f"{self._entry.id}: {where} — {said}",
                status=status,
                code=code,
                endpoint=endpoint,
                provider_message=message,
                retry_after=_retry_after(exc.response.headers.get("retry-after")),
                detail_code=detail_code,
                resumes_at=_resumes_at(message, detail_code),
            )
        if isinstance(exc, anthropic.APIConnectionError | httpx2.HTTPError):
            cause = exc.__cause__ if isinstance(exc.__cause__, httpx2.HTTPError) else exc
            name = type(cause).__name__
            text = str(cause) or str(exc)
            return ServiceError(
                f"{self._entry.id}: {name}: {text}",
                code=name,
                endpoint=endpoint,
                provider_message=text[:_ERROR_BODY_CHARS],
            )
        if isinstance(exc, ValueError):
            # The SDK's parser could not read a tool's input at all, and stopped the stream
            # there (``python/claude-api/streaming.md``). Buffered input is validated by the
            # server first, so this is a malformed response; the loop's own attempts retry.
            return ProviderError(f"{self._entry.id}: a tool call's input was not valid JSON")
        return exc

    def close(self) -> None:
        if not self._shared and self._client is not None:
            self._client.close()


def _usage(raw: Any, previous: Usage) -> Usage:
    """The API's usage in this harness's terms: ``input_tokens`` is the whole prompt, the
    cache counts are parts of it (see `provider.base.Usage`). Counts a message delta leaves
    out keep what the message start said."""
    if raw is None:
        return previous
    uncached = getattr(raw, "input_tokens", None)
    written = getattr(raw, "cache_creation_input_tokens", None)
    read = getattr(raw, "cache_read_input_tokens", None)
    output = getattr(raw, "output_tokens", None)
    creation = written if written is not None else previous.cache_creation_input_tokens
    reads = read if read is not None else previous.cache_read_input_tokens
    if uncached is None:
        uncached = previous.input_tokens - (
            previous.cache_creation_input_tokens + previous.cache_read_input_tokens
        )
    return Usage(
        input_tokens=uncached + creation + reads,
        output_tokens=output if output is not None else previous.output_tokens,
        cache_creation_input_tokens=creation,
        cache_read_input_tokens=reads,
    )


def _tool_call(block: Any, raw: str) -> ToolUseBlock:
    """The SDK parsed the input with a partial JSON parser, which reads a truncated object
    as a valid shorter one (``shared/tool-use-concepts.md``, "Eager input streaming"). So
    its reading is kept only when the raw input is complete JSON. Otherwise the arguments
    are empty and the raw text is kept for the repair layer, as the OpenAI dialect leaves
    arguments that do not parse."""
    arguments = dict(block.input) if isinstance(block.input, Mapping) else {}
    if raw.strip():
        try:
            json.loads(raw)
        except ValueError:
            arguments = {}
    return ToolUseBlock(id=block.id, name=block.name, input=arguments, raw=raw)
