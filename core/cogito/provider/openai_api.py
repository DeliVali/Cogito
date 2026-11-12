"""The OpenAI-shaped wire dialect.

One adapter covers llama.cpp's server, Ollama, vLLM, LM Studio, OpenAI, DeepSeek,
OpenRouter and Groq, since they all speak `/chat/completions`. A new provider is a block in
``models.toml``, not a new file here.

Native tool calls are collected when the endpoint sends them, but local servers often put
the call in the content instead. ``provider/normalize.py`` turns either into something
callable. This module only reports what came back.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from cogito.cancel import CancelToken
from cogito.config.models import ModelEntry
from cogito.errors import ServiceError
from cogito.ids import new_id
from cogito.provider import ratelimit
from cogito.provider.base import (
    Capabilities,
    Chunk,
    Completed,
    Completion,
    Message,
    RequestOptions,
    SamplingParams,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    Timing,
    ToolCallDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
    image_data,
    missing_image,
    recent_result_images,
)
from cogito.provider.host import sees_images, thinks_by_default

_STREAM_DONE = "[DONE]"
_ERROR_BODY_CHARS = 600
#: Seconds `_ask_whether_it_thinks` waits for each of its two template renderings.
_ASK_TIMEOUT = 5.0
#: How often a tool call's progress is yielded while it streams (`ToolCallDelta`), at
#: most. A token-by-token delta would flood the window exactly when the model is already
#: slow; four a second is fast enough to feel live and cheap enough to never be the
#: bottleneck.
_PROGRESS_INTERVAL = 0.25
#: The `path` argument, once the partial JSON holds it whole (key and closing quote),
#: even while the object around it is still open. A heuristic for the live row only: it
#: is never used to run anything, and a call whose `path` is not its first argument, or
#: that takes no `path` at all, simply shows no path yet.
_PARTIAL_PATH = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _partial_path(raw: str) -> str | None:
    match = _PARTIAL_PATH.search(raw)
    if match is None:
        return None
    try:
        decoded = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, str) else None


def _error_fields(body: str) -> tuple[str, str]:
    """The provider's error code and message from an error body. Handles OpenAI's and
    llama.cpp's ``{"error": {"message", "type", "code"}}``, Claude's
    ``{"type": "error", "error": {"type", "message"}}`` and Ollama's ``{"error": "..."}``.
    Any other body is used as the message."""
    try:
        document = json.loads(body)
    except ValueError:
        return "", body.strip()[:_ERROR_BODY_CHARS]
    error = document.get("error") if isinstance(document, dict) else None
    if isinstance(error, str):
        return "", error[:_ERROR_BODY_CHARS]
    if isinstance(error, dict):
        code = error.get("code")
        if not isinstance(code, str) or not code:
            # llama.cpp repeats the HTTP status as a number here; its `type` is the name.
            code = error.get("type") if isinstance(error.get("type"), str) else ""
        message = error.get("message")
        return code, (message if isinstance(message, str) else "")[:_ERROR_BODY_CHARS]
    if isinstance(document, dict) and isinstance(document.get("message"), str):
        return "", document["message"][:_ERROR_BODY_CHARS]
    return "", body.strip()[:_ERROR_BODY_CHARS]


def _error_detail_code(body: str) -> str:
    """``error.details.error_code``, the Claude API's finer code. Only it tells the monthly
    spend cap from a rate limit (both are a 429 ``rate_limit_error``). Read from the
    ``error`` object or the top level; "" when absent."""
    try:
        document = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(document, dict):
        return ""
    error = document.get("error")
    for holder in (error, document):
        if isinstance(holder, dict) and isinstance(holder.get("details"), dict):
            code = holder["details"].get("error_code")
            if isinstance(code, str):
                return code
    return ""


def _error_type(body: str) -> str:
    """``error.type`` of an OpenAI-shaped error body, "" when absent. OpenAI puts the
    specific cause in ``error.code`` and a broader class here, which for any billing failure
    "can still be `insufficient_quota`" (developers.openai.com/api/docs/guides/
    error-codes, read 2026-09-29)."""
    try:
        document = json.loads(body)
    except ValueError:
        return ""
    error = document.get("error") if isinstance(document, dict) else None
    if isinstance(error, dict) and isinstance(error.get("type"), str):
        return error["type"]
    return ""


def _overflow_sizes(body: str) -> tuple[int | None, int | None]:
    """``error.n_prompt_tokens`` and ``error.n_ctx`` from llama.cpp's
    ``exceed_context_size_error`` (tools/server/server-task.cpp,
    `server_task_result_error::to_json`, at 7ba604f1c, read 2026-09-30): the request's size
    and the most one request may hold on that server. (None, None) otherwise."""
    try:
        document = json.loads(body)
    except ValueError:
        return None, None
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, dict):
        return None, None

    def count(key: str) -> int | None:
        value = error.get(key)
        counted = isinstance(value, int) and not isinstance(value, bool) and value > 0
        return value if counted else None

    return count("n_prompt_tokens"), count("n_ctx")


#: The Claude API's spend-limit messages say when access returns, e.g. "You will regain
#: access on 2026-09-01 at 00:00 UTC." (platform.claude.com/docs/en/api/rate-limits, read
#: 2026-09-29).
_REGAIN_ACCESS = re.compile(
    r"regain access on (\d{4})-(\d{2})-(\d{2}) at (\d{1,2}):(\d{2}) UTC", re.IGNORECASE
)
_MONTHLY_CAP_DETAIL = "enforced_spend_limit_reached"


def _resumes_at(message: str, detail_code: str, now: datetime | None = None) -> str | None:
    """When a spend limit lifts, as ISO 8601 in UTC. Uses the time the provider's message
    names. Failing that, and only for the Claude API's monthly spend cap, the first day of
    next month at 00:00 UTC (same page). Otherwise None; a date is never guessed."""
    if match := _REGAIN_ACCESS.search(message):
        year, month, day, hour, minute = (int(part) for part in match.groups())
        try:
            when = datetime(year, month, day, hour, minute, tzinfo=UTC)
        except ValueError:
            when = None
        if when is not None:
            return when.isoformat()
    if detail_code != _MONTHLY_CAP_DETAIL:
        return None
    today = (now or datetime.now(UTC)).astimezone(UTC)
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return datetime(year, month, 1, tzinfo=UTC).isoformat()


#: Largest ``retry-after`` kept, in seconds (one day); longer values are clamped to it.
#: Past an hour nothing resumes on its own anyway (``server.app.MAX_AUTO_RESUME_DELAY``),
#: and an unbounded value would overflow the date arithmetic that schedules a resume.
RETRY_AFTER_CEILING = 86_400.0


def _retry_after(value: str | None) -> float | None:
    """``retry-after`` in seconds, from delta-seconds or an HTTP date (RFC 9110, 10.2.3),
    capped at `RETRY_AFTER_CEILING`. None when absent or unreadable."""
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = max(0.0, (when - datetime.now(UTC)).total_seconds())
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CEILING)


def _service_error(
    model_id: str,
    where: str,
    text: str,
    url: str,
    *,
    status: int | None,
    retry_after: str | None,
) -> ServiceError:
    """The `ServiceError` for an error body, read the same way whether it came as an HTTP
    error response or as an event inside a stream."""
    code, provider_message = _error_fields(text)
    detail_code = _error_detail_code(text)
    requested, available = _overflow_sizes(text)
    return ServiceError(
        f"{model_id}: {where} — {text[:_ERROR_BODY_CHARS]}",
        status=status,
        code=code,
        endpoint=url,
        provider_message=provider_message,
        retry_after=_retry_after(retry_after),
        detail_code=detail_code,
        resumes_at=_resumes_at(provider_message, detail_code),
        error_type=_error_type(text),
        requested_tokens=requested,
        available_tokens=available,
    )


def _status_in_stream_error(event: Mapping[str, Any]) -> int | None:
    """The HTTP status an error event inside a stream names, or None.

    llama.cpp sends an error after the first token as ``data: {"error": {...}}`` and then
    closes the stream; its ``error.code`` is the status it sends when the error comes
    first (tools/server/server-context.cpp, `handle_completions_impl`, at 7ba604f1). vLLM
    (``ErrorInfo.code: int``) and OpenRouter (its "mid-stream errors" docs) put the status
    there too (both read 2026-10-09). OpenAI's ``code`` is a string or null, so such an
    error has no status.
    """
    error = event.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
        return code
    return None


def _server_timing(raw: object) -> Timing | None:
    """llama.cpp's final ``timings`` object, when it is well formed."""
    if not isinstance(raw, dict):
        return None
    tokens, milliseconds = raw.get("predicted_n"), raw.get("predicted_ms")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        return None
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int | float):
        return None
    if milliseconds <= 0:
        return None
    return Timing(output_tokens=tokens, seconds=milliseconds / 1000, source="server")


_RESULT_IMAGES_NOTE = "[the images the tool results above returned]"


def _encode_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Project block messages onto the flat OpenAI shape."""
    encoded: list[dict[str, Any]] = []
    kept = recent_result_images(messages)
    for message in messages:
        results = [b for b in message.blocks if isinstance(b, ToolResultBlock)]
        if results:
            # Tool results are their own messages in this dialect, one per call.
            for result in results:
                encoded.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.tool_use_id,
                        "content": result.content,
                    }
                )
            # A `tool` message carries text only in this dialect, so the images the
            # results returned follow all of them in one user message, never between two
            # tool messages (which strict endpoints refuse). Only the newest few are sent.
            parts: list[dict[str, Any]] = []
            for result in results:
                for image in result.images:
                    if id(image) not in kept:
                        continue
                    data = image_data(image)
                    if data is None:
                        parts.append({"type": "text", "text": missing_image(image)})
                        continue
                    url = f"data:{image.media_type};base64,{data}"
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            if parts:
                encoded.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _RESULT_IMAGES_NOTE},
                            *parts,
                        ],
                    }
                )
            continue

        text = message.flat_text()
        tool_uses = message.tool_uses
        if message.role == "assistant" and tool_uses:
            encoded.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": use.id,
                            "type": "function",
                            "function": {
                                "name": use.name,
                                "arguments": json.dumps(dict(use.input), ensure_ascii=False),
                            },
                        }
                        for use in tool_uses
                    ],
                }
            )
        elif message.role == "user" and message.images:
            # An image as an OpenAI content part (llama.cpp's server reads `image_url` with
            # a data URL); a message without one keeps its plain string content.
            parts: list[dict[str, Any]] = []
            for image in message.images:
                data = image_data(image)
                if data is None:
                    text = f"{missing_image(image)}\n{text}"
                    continue
                url = f"data:{image.media_type};base64,{data}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            parts.append({"type": "text", "text": text})
            encoded.append({"role": "user", "content": parts})
        else:
            encoded.append({"role": message.role, "content": text})
    return encoded


def _is_loopback(url: str) -> bool:
    host = httpx.URL(url).host if url else ""
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def _encode_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }
        for tool in tools
    ]


class _ToolCallAccumulator:
    """Reassembles streamed tool-call fragments, which arrive keyed by index."""

    def __init__(self) -> None:
        self._slots: dict[int, dict[str, str]] = {}

    def feed(self, fragments: Sequence[Mapping[str, Any]]) -> None:
        for fragment in fragments:
            index = int(fragment.get("index", 0))
            slot = self._slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if identifier := fragment.get("id"):
                slot["id"] = str(identifier)
            function = fragment.get("function") or {}
            if name := function.get("name"):
                slot["name"] = str(name)
            if arguments := function.get("arguments"):
                slot["arguments"] += str(arguments)

    def progress(self, index: int) -> ToolCallDelta | None:
        """This slot's live row, or None before it has a name (llama-server sends the
        name with the first fragment, so this is only ever momentary)."""
        slot = self._slots.get(index)
        if slot is None or not slot["name"]:
            return None
        raw = slot["arguments"]
        return ToolCallDelta(
            index=index,
            name=slot["name"],
            path=_partial_path(raw),
            chars=len(raw),
            lines=raw.count("\\n"),
        )

    def finish(self) -> tuple[ToolUseBlock, ...]:
        """Emit blocks. Arguments that do not parse are left raw for `repair.native_arguments`."""
        blocks: list[ToolUseBlock] = []
        for _, slot in sorted(self._slots.items()):
            if not slot["name"]:
                continue
            try:
                arguments = json.loads(slot["arguments"] or "{}")
                if not isinstance(arguments, dict):
                    arguments = {}
            except json.JSONDecodeError:
                arguments = {}
            blocks.append(
                ToolUseBlock(
                    id=slot["id"] or new_id(),
                    name=slot["name"],
                    input=arguments,
                    raw=slot["arguments"],
                )
            )
        return tuple(blocks)


class OpenAIProvider:
    """An endpoint that speaks `/chat/completions`."""

    def __init__(
        self,
        entry: ModelEntry,
        api_key: str | None = None,
        *,
        timeout: float = 600.0,
        client: httpx.Client | None = None,
        options: RequestOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entry = entry
        self._api_key = api_key
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
        self._options = options or RequestOptions()
        #: Read once per streamed line, for `Completion.timing` only.
        self._clock = clock
        #: True for a `configured` copy, whose connection belongs to the original provider.
        self._shared = False
        #: Whether this model reasons, taken from observation, never from configuration. A
        #: template that can think does not mean the server lets it (qwen3.5-9b's can, and
        #: runs with `--reasoning off`), and a model that never reasons must keep sending
        #: the same bytes. `_reasoned` turns True once a reply streams `reasoning_content`.
        #: `_thinks_by_default` is a Cogito-run server's answer, asked before the first
        #: request that could carry a budget (None until asked).
        self._reasoned = False
        self._thinks_by_default: bool | None = None
        #: Whether the model takes images: `args.vision` when the entry says, else a local
        #: server's own `/props` answer, asked when first needed and kept once it answers.
        self._vision: bool | None = None
        self.name = entry.id

    @property
    def options(self) -> RequestOptions:
        """The per-request options this provider sends (`configured` makes another)."""
        return self._options

    def configured(self, options: RequestOptions) -> OpenAIProvider:
        """The same endpoint with other per-request options, sharing this connection.

        Asking for the options it already has returns this provider itself.
        """
        if options == self._options:
            return self
        copy = OpenAIProvider(
            self._entry,
            self._api_key,
            timeout=self._timeout,
            client=self._client,
            options=options,
            clock=self._clock,
        )
        copy._shared = True
        # What this provider has learned about the model holds for the copy too: the
        # composer's thinking control makes one mid-session (`cli.EngineSession`).
        copy._reasoned = self._reasoned
        copy._thinks_by_default = self._thinks_by_default
        copy._vision = self._vision
        return copy

    def capabilities(self) -> Capabilities:
        context = int(self._entry.args.get("ctx", 32_768))  # type: ignore[arg-type]
        # `native_tools = false` sends no tool definitions. Calls written as text are not
        # read (since 2026-10-30), so such an entry cannot call tools.
        native = (
            bool(self._entry.args.get("native_tools", True))
            if self._options.native_tools is None
            else self._options.native_tools
        )
        return Capabilities(
            native_tool_calls=native,
            parallel_tool_calls=True,
            supports_grammar=self._entry.takes_server_extensions,
            max_context=context,
            context_known="ctx" in self._entry.args,
            # Only what the entry says: asking the server is `sees_images`, done only when
            # an image is attached, so no other session sends one more request.
            vision=bool(self._entry.args.get("vision", False)),
        )

    def sees_images(self) -> bool:
        """Whether an attached image can be sent: `args.vision` when the entry says, else a
        local server's own `/props` answer, asked once it is needed and kept once given."""
        if "vision" in self._entry.args:
            return bool(self._entry.args["vision"])
        if self._vision is not None:
            return self._vision
        if not self._entry.takes_server_extensions and not _is_loopback(self._entry.base_url):
            self._vision = False
            return False
        answer = sees_images(self._entry, client=self._client)
        if answer is not None:
            self._vision = answer
        return bool(answer)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        params: SamplingParams,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._entry.model,
            "messages": _encode_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": params.temperature,
            "top_p": params.top_p,
            "max_tokens": params.max_tokens,
        }
        if params.seed is not None:
            payload["seed"] = params.seed
        if params.stop:
            payload["stop"] = list(params.stop)
        # Anti-repetition samplers go on the wire only when a model opts in, so an entry that
        # sets none sends the same body as before. The key names are llama.cpp's own.
        for key in (
            "repeat_penalty",
            "repeat_last_n",
            "presence_penalty",
            "frequency_penalty",
            "min_p",
            "dry_multiplier",
            "dry_base",
            "dry_allowed_length",
            "dry_penalty_last_n",
        ):
            value = getattr(params, key)
            if value is not None:
                payload[key] = value
        if tools and self.capabilities().native_tool_calls:
            payload["tools"] = _encode_tools(tools)
            payload["tool_choice"] = "auto"
            if self._options.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = self._options.parallel_tool_calls
        if self._options.reasoning is not None:
            # llama-server applies this per request through the chat template's own switch.
            payload["chat_template_kwargs"] = {"enable_thinking": self._options.reasoning}
        if (budget := self._reasoning_budget(params)) is not None:
            payload["thinking_budget_tokens"] = budget
        return payload

    def _reasoning_budget(self, params: SamplingParams) -> int | None:
        """The thinking budget this request carries, or None to send nothing about it.

        llama-server reads `thinking_budget_tokens` from the request body and forces the
        template's end-of-thinking tag once the budget is spent (llama.cpp 7ba604f1, the
        bench build: `tools/server/server-common.cpp`, `common/reasoning-budget.cpp`;
        Gemma 4's tags are `<|channel>thought` and `<channel|>`). The rest of `max_tokens`
        is left for the answer, so a reply cannot spend every token on reasoning. That
        happened in 124 of gemma-4-12b's 1,278 samples in its quick run at a2653e6, 53% of
        that run's output tokens.

        Sent only when the model is known to reason (the server thinks by default, this
        request turns thinking on, or a reply already reasoned), so a model that never
        reasons sends the same bytes all session. Not sent when the request turns thinking
        off, or when the budget is not below `max_tokens`, which is then the tighter limit.
        """
        budget = self._options.reasoning_budget
        if budget is None or self._options.reasoning is False or not 0 < budget < params.max_tokens:
            return None
        if self._reasoned or self._options.reasoning is True or self._thinks_by_default:
            return budget
        # Motoris has no `/apply-template` to ask (404 on 2026-10-31), and its Qwen thinks
        # by default ("high"; cogito-motoris docs/DETAILS.md). It reads the budget under
        # llama.cpp's name too (serve/server.py `reasoning_budget`), so it always gets one.
        if self._entry.serve == "motoris":
            return budget
        return None

    def _ask_whether_it_thinks(self, params: SamplingParams) -> None:
        """Ask a server that takes server extensions whether this model thinks by default
        (`provider.host.thinks_by_default`), before the first request a budget
        could change. Waiting for reasoning on the wire would leave each session's first
        request unbudgeted, and every benchmark task is its own session. Asked once, only
        when the answer could change the request. If the server cannot answer, the wire
        decides later.
        """
        budget = self._options.reasoning_budget
        if (
            self._thinks_by_default is not None
            or self._reasoned
            or self._options.reasoning is not None
            or self._entry.serve == "motoris"
            or not self._entry.takes_server_extensions
            or budget is None
            or not 0 < budget < params.max_tokens
        ):
            return
        timeout = min(_ASK_TIMEOUT, self._options.timeout or _ASK_TIMEOUT)
        self._thinks_by_default = thinks_by_default(
            self._entry, client=self._client, timeout=timeout
        )

    def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        params: SamplingParams,
        cancel: CancelToken,
    ) -> Iterator[Chunk]:
        url = f"{self._entry.base_url}/chat/completions"
        text_parts: list[str] = []
        calls = _ToolCallAccumulator()
        usage = Usage()
        finish_reason = "end_turn"
        # The live row only (`ToolCallDelta`); -inf so the first fragment always shows
        # something at once rather than waiting out the first interval, whatever the
        # clock's own starting value is.
        last_progress_at = float("-inf")
        # For the desktop's tokens/s only: when the first produced delta and the last line
        # arrived, and the server's own timing if it sent one.
        first_delta_at: float | None = None
        last_line_at: float | None = None
        server_timing: Timing | None = None
        # For the desktop's per-minute headroom only: the rate-limit headers of a
        # successful response. A local server sends none.
        rate_limits: dict[str, Any] = {}
        self._ask_whether_it_thinks(params)

        # A blocking read (the prefill, before the first SSE line) never reaches the
        # `cancel.cancelled` check below. Closing the response from another thread does not
        # interrupt it: that needs `shutdown()`, which httpx does not expose, and `close()`
        # was measured not to work. So a hung request is bounded by a per-request deadline,
        # `RequestOptions.timeout`, when a caller sets one.
        kwargs: dict[str, Any] = {
            "json": self._payload(messages, tools, params),
            "headers": self._headers(),
        }
        if self._options.timeout is not None:
            kwargs["timeout"] = self._options.timeout

        try:
            with self._client.stream("POST", url, **kwargs) as response:
                if response.status_code >= 400:
                    text = response.read().decode("utf-8", "replace")
                    raise _service_error(
                        self._entry.id,
                        f"HTTP {response.status_code} from {url}",
                        text,
                        url,
                        status=response.status_code,
                        retry_after=response.headers.get("retry-after"),
                    )
                rate_limits = ratelimit.from_headers(response.headers)

                for line in response.iter_lines():
                    if cancel.cancelled:
                        finish_reason = "cancelled"
                        break
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == _STREAM_DONE:
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("error"):
                        # The server failed after the stream began. What streamed before
                        # this is cut off, so it is raised as a failure, never returned. A
                        # truthy "error" is the OpenAI SDK's own test (openai-python
                        # `_streaming.py`, read 2026-10-09).
                        raise _service_error(
                            self._entry.id,
                            f"error in the stream from {url}",
                            data,
                            url,
                            status=_status_in_stream_error(event),
                            retry_after=None,
                        )
                    last_line_at = self._clock()

                    if raw_usage := event.get("usage"):
                        usage = Usage(
                            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
                            output_tokens=int(raw_usage.get("completion_tokens", 0)),
                        )
                    if "timings" in event:
                        server_timing = _server_timing(event["timings"])

                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        produced = (
                            delta.get("content")
                            or delta.get("reasoning_content")
                            or delta.get("tool_calls")
                        )
                        if first_delta_at is None and produced:
                            first_delta_at = last_line_at
                        if reasoning := delta.get("reasoning_content"):
                            self._reasoned = True
                            yield ThinkingDelta(str(reasoning))
                        if content := delta.get("content"):
                            text_parts.append(str(content))
                            yield TextDelta(str(content))
                        if fragments := delta.get("tool_calls"):
                            calls.feed(fragments)
                            # Reuses this line's own clock read (`last_line_at`, always
                            # set by now) rather than asking the clock again: a second
                            # read here would double what a scripted stream's `clock`
                            # provides per line, for timing tests that are not about this
                            # feature at all.
                            if last_line_at - last_progress_at >= _PROGRESS_INTERVAL:
                                last_progress_at = last_line_at
                                seen: set[int] = set()
                                for fragment in fragments:
                                    index = int(fragment.get("index", 0))
                                    if index in seen:
                                        continue
                                    seen.add(index)
                                    if progress := calls.progress(index):
                                        yield progress
                        if reason := choice.get("finish_reason"):
                            finish_reason = str(reason)
        except httpx.HTTPError as exc:
            if cancel.cancelled:
                # The deadline above ended this. Treat it as a cancellation, not a
                # transport failure.
                finish_reason = "cancelled"
            else:
                raise ServiceError(
                    f"{self._entry.id}: {type(exc).__name__}: {exc}",
                    code=type(exc).__name__,
                    endpoint=url,
                    provider_message=str(exc)[:_ERROR_BODY_CHARS],
                ) from exc

        raw_text = "".join(text_parts)
        native = calls.finish()
        yield Completed(
            Completion(
                message=Message(
                    role="assistant",
                    blocks=(TextBlock(raw_text),) if raw_text else (),
                ),
                usage=usage,
                stop_reason=_normalise_stop(finish_reason, native),
                raw_text=raw_text,
                native_tool_calls=native,
                model=self._entry.model,
                timing=server_timing or _stream_timing(first_delta_at, last_line_at, usage),
                rate_limits=rate_limits or None,
                truncated=finish_reason == "length",
            )
        )

    def close(self) -> None:
        if not self._shared:
            self._client.close()


def _stream_timing(first: float | None, last: float | None, usage: Usage) -> Timing | None:
    """Tokens after the first one, over the time from the first delta to the last line."""
    if first is None or last is None or last <= first or usage.output_tokens <= 1:
        return None
    return Timing(output_tokens=usage.output_tokens - 1, seconds=last - first, source="stream")


def _normalise_stop(reason: str, native: Sequence[ToolUseBlock]) -> Any:
    # `tool_use` wins over `length`, and readers of `stop_reason` rely on that. The length
    # stop is kept in `Completion.truncated`.
    if reason == "cancelled":
        return "cancelled"
    if native or reason == "tool_calls":
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "stop":
        return "end_turn"
    return "end_turn"
