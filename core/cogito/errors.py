"""The error hierarchy. Every error carries a stable ``kind`` for telemetry.

`is_context_overflow` misses these, so they stay ``other`` and are retried like any failure:
vLLM's 400 "This model's maximum context length is N tokens. However, you requested M
tokens ...", which has no ``context_length_exceeded`` code; the Claude API's older 400
"input length and `max_tokens` exceed context limit" (models before 4.5 without the
model-context-window-exceeded behaviour); and the Claude API's 413 ``request_too_large``,
which is its 32 MB request limit, not the context window.
"""

from __future__ import annotations

import re


class CogitoError(Exception):
    """Base class. ``kind`` is written to the graph and grouped in benchmark reports."""

    kind = "cogito"


class ConfigError(CogitoError):
    kind = "config"


class GraphUnavailable(CogitoError):
    """Neo4j is unreachable. The WAL keeps the turn alive; see graph/wal.py."""

    kind = "graph_unavailable"


class ProviderError(CogitoError):
    kind = "provider"


class NoModel(CogitoError):
    """Nothing is served yet: a first launch before a model is connected (design 1d-1e).
    ``POST /sessions`` answers it with a 409, not as a server failure."""

    kind = "no_model"


#: Code substrings for rate limits and spent quotas, whatever the status (Claude API
#: `rate_limit_error`; OpenAI `rate_limit_exceeded`, `slow_down`, `insufficient_quota`,
#: `*_spend_limit_exceeded`, `*_usage_limit_exceeded`, `credit_balance_exhausted`; from each
#: provider's error docs, read 2026-09-28).
_USAGE_LIMIT_MARKS = (
    "rate_limit",
    "slow_down",
    "quota",
    "spend_limit",
    "usage_limit",
    "credit_balance",
)


# The values of `ServiceError.category`. Neither spend kind is a rate limit: waiting does
# not bring access back, so nothing resumes automatically after one.

#: No answer arrived: a deadline passed, the connection was refused or dropped, or the
#: stream stopped early. The window shows it as «La API no respondió».
TIMEOUT = "timeout"
RATE_LIMIT = "rate_limit"
#: The provider stopped the account: a spend cap it enforces, or prepaid credit used up.
SPEND_CAP = "spend_cap"
#: A spend limit the person set themselves in the provider's console.
USER_SPEND_LIMIT = "user_spend_limit"
OVERLOADED = "overloaded"
AUTH = "auth"
#: The server said the request does not fit in the model's context window: llama.cpp's
#: ``exceed_context_size_error``, OpenAI's ``context_length_exceeded``, the Claude API's 400
#: "prompt is too long", Cogito Motoris's 400 "request exceeds the context window". Asking again cannot change that. `overflow_numbers` reads the sizes.
CONTEXT_OVERFLOW = "context_overflow"
OTHER = "other"

CATEGORIES = (
    TIMEOUT, RATE_LIMIT, SPEND_CAP, USER_SPEND_LIMIT, OVERLOADED, AUTH, CONTEXT_OVERFLOW, OTHER
)
#: Categories after which a stopped turn never resumes by itself.
SPEND_CATEGORIES = frozenset({SPEND_CAP, USER_SPEND_LIMIT})
#: Categories an identical request will hit again: spend stays cut until access returns,
#: and an oversized request stays oversized until it changes. The loop asks once for these
#: (`loop.engine._sample`) and the server never resumes after them.
DETERMINISTIC_CATEGORIES = SPEND_CATEGORIES | {CONTEXT_OVERFLOW}

#: llama.cpp's type (tools/server/server-common.cpp, `format_error_response`, at 7ba604f1c,
#: the build installed here, read 2026-09-30), sent as ``error.type`` beside a numeric
#: ``error.code``; and OpenAI's ``error.code`` (body quoted in BerriAI/litellm#43013,
#: 2026-09-23; Codex maps the same code to ContextWindowExceeded in
#: codex-rs/codex-api/src/sse/responses_error.rs, read 2026-09-30).
_OVERFLOW_CODES = frozenset({"exceed_context_size_error", "context_length_exceeded"})
#: The Claude API: "If the input alone already exceeds the model's context window, the API
#: returns a 400 invalid_request_error ("prompt is too long")" (platform.claude.com/docs/en/
#: build-with-claude/context-windows, "Context window overflow behavior", read 2026-09-30).
_OVERFLOW_OPENING = "prompt is too long"
#: Cogito Motoris: a 400 ``invalid_request_error`` with no code, "request exceeds the context
#: window: ..." (cogito-motoris serve/server.py, read 2026-10-29). It refuses, never truncates.
_MOTORIS_OVERFLOW_OPENING = "request exceeds the context window"
#: Size wording, requested first and available second: llama.cpp's two messages
#: (server-context.cpp at 7ba604f1c) and the Claude API's observed "prompt is too long:
#: 210445 tokens > 200000 maximum" (shareAI-lab/learn-claude-code#560).
_OVERFLOW_SIZES = (
    re.compile(
        r"(?:request|input) \((\d+) tokens\) (?:exceeds|is larger than) the "
        r"(?:available|max) context size \((\d+) tokens\)"
    ),
    re.compile(r"prompt is too long: (\d+) tokens > (\d+) maximum", re.IGNORECASE),
)
#: Cogito Motoris's two wordings (serve/server.py, read 2026-10-29): the prompt alone leaves no
#: room, or the prompt plus max_tokens does not fit; what was asked for is their sum.
_MOTORIS_NO_ROOM = re.compile(r"prompt \((\d+) tokens\) leaves no room to answer in the context \((\d+)\)")
_MOTORIS_WITH_MAX = re.compile(
    r"prompt \((\d+) tokens\) \+ max tokens \((\d+)\) exceeds the context \((\d+)\)"
)
#: OpenAI's older wording, available first: "This model's maximum context length is N
#: tokens. However, your messages resulted in M tokens." The current wording has no number.
_OPENAI_LEGACY_SIZE = re.compile(
    r"maximum context length is (\d+) tokens\. However, your (?:messages|request|input) "
    r"resulted in (\d+) tokens",
    re.IGNORECASE,
)


def is_context_overflow(status: int | None, code: str, message: str = "") -> bool:
    """Whether a provider's answer says the request does not fit in the context window: a
    code the provider documents for it, or the Claude API's 400 opening. Never llama.cpp's
    500 "Context size has been exceeded.": that is parallel requests filling one shared KV
    cache (server-context.cpp at 7ba604f1c), and it may clear on its own."""
    lowered = code.lower()
    if lowered in _OVERFLOW_CODES:
        return True
    refused = status == 400 or lowered == "invalid_request_error"
    return refused and message.strip().lower().startswith((_OVERFLOW_OPENING, _MOTORIS_OVERFLOW_OPENING))


def overflow_numbers(message: str) -> tuple[int | None, int | None]:
    """(requested, available) tokens as the provider's message states them, or (None,
    None) when it states neither."""
    for pattern in _OVERFLOW_SIZES:
        if match := pattern.search(message):
            return int(match.group(1)), int(match.group(2))
    if match := _OPENAI_LEGACY_SIZE.search(message):
        return int(match.group(2)), int(match.group(1))
    if match := _MOTORIS_NO_ROOM.search(message):
        return int(match.group(1)), int(match.group(2))
    if match := _MOTORIS_WITH_MAX.search(message):
        return int(match.group(1)) + int(match.group(2)), int(match.group(3))
    return None, None

#: Claude API: the tier's monthly spend cap. It is a 429 ``rate_limit_error``, told apart
#: from a rate limit only by ``error.details.error_code`` (platform.claude.com/docs/en/api/
#: rate-limits, "Reaching your spend cap", read 2026-09-29). The message opening is a second
#: sign, for a body without ``details``.
_SPEND_CAP_DETAIL = "enforced_spend_limit_reached"
_SPEND_CAP_OPENING = "you have reached your api usage limits"
#: Claude API: a spend limit the person set, a 400 ``invalid_request_error`` whose message
#: begins with one of these (same page, "Setting your own spend limit").
_USER_LIMIT_OPENINGS = (
    "you have reached your specified api usage limits",
    "you have reached your specified workspace api usage limits",
)
#: Claude API: prepaid credit used up. A 400 ``invalid_request_error`` the error page does
#: not list. The wording is observed, not documented: quoted verbatim in
#: continuedev/continue#5499 and BerriAI/litellm#24320 (read 2026-09-29).
_CREDIT_OPENING = "your credit balance is too low"
#: OpenAI's codes (developers.openai.com/api/docs/guides/error-codes, read 2026-09-29). User
#: limits are spend limits set on the organization or project. Spend caps are what OpenAI
#: enforces itself: no prepaid credit left, its own usage limit, the older ``insufficient_quota``.
_USER_LIMIT_CODES = frozenset(
    {"organization_spend_limit_exceeded", "project_spend_limit_exceeded"}
)
_SPEND_CAP_CODES = frozenset(
    {"credit_balance_exhausted", "organization_usage_limit_exceeded", "insufficient_quota"}
)
#: OpenAI's broader ``error.type`` for every billing failure ("The broader `error.type` can
#: still be `insufficient_quota`", same guide). An unknown code under that type is still spend.
_SPEND_TYPE = "insufficient_quota"

#: Codes for prepaid credit running out, as opposed to a periodic cap or a quota.
_CREDIT_CODES = frozenset({"credit_balance_exhausted"})
#: OpenAI's own usage limit and the generic ``insufficient_quota``. Neither says "monthly",
#: so neither gets the window's "Tope de gasto mensual" title.
_QUOTA_CODES = frozenset({"organization_usage_limit_exceeded", "insufficient_quota"})

SPEND_CAP_REASON_CAP = "cap"
SPEND_CAP_REASON_CREDIT = "credit"
SPEND_CAP_REASON_QUOTA = "quota"
_OVERLOADED_CODES = frozenset(
    {"overloaded_error", "server_is_overloaded", "service_unavailable_error"}
)
_AUTH_CODES = frozenset(
    {"authentication_error", "permission_error", "invalid_api_key", "missing_api_key"}
)


def classify(
    status: int | None,
    code: str,
    message: str = "",
    detail_code: str = "",
    error_type: str = "",
) -> str:
    """One of `CATEGORIES`, from what the provider sent. Spend kinds are checked first: each
    can arrive as a 429 (the Claude spend cap, OpenAI's spend limits) or a 400 (a Claude
    console limit), and neither is a rate limit. ``error_type`` is the error's own ``type``
    when the provider sends one beside a finer ``code`` (OpenAI)."""
    lowered = code.lower()
    said = message.strip().lower()
    if detail_code.lower() == _SPEND_CAP_DETAIL or said.startswith(_SPEND_CAP_OPENING):
        return SPEND_CAP
    if lowered in _USER_LIMIT_CODES or said.startswith(_USER_LIMIT_OPENINGS):
        return USER_SPEND_LIMIT
    if (
        lowered in _SPEND_CAP_CODES
        or said.startswith(_CREDIT_OPENING)
        or error_type.lower() == _SPEND_TYPE
    ):
        return SPEND_CAP
    if is_context_overflow(status, code, message):
        return CONTEXT_OVERFLOW
    if status == 429 or "rate_limit" in lowered or lowered == "slow_down":
        return RATE_LIMIT
    if status in (503, 529) or lowered in _OVERLOADED_CODES:
        return OVERLOADED
    if status in (408, 504) or lowered == "timeout_error":
        return TIMEOUT
    if status in (401, 403) or lowered in _AUTH_CODES:
        return AUTH
    return OTHER


def spend_cap_reason(
    code: str, message: str = "", detail_code: str = "", error_type: str = ""
) -> str | None:
    """Which kind of spend cap this is, when `classify(...)` returned `SPEND_CAP`.

    The window's «Tope de gasto mensual alcanzado» title is true only for a provider's own
    periodic cap (`SPEND_CAP_REASON_CAP`, Claude's tier limit). Prepaid credit running out
    (`_CREDIT`) and OpenAI's usage limit or generic ``insufficient_quota`` (`_QUOTA`) are
    spend caps, but not monthly ones (2026-09-29 UX audit; PROVENANCE, INVENTED). Mirrors
    `classify`'s spend-cap checks, so every `SPEND_CAP` gets a reason; None when none match.
    It does not rule out a user spend limit first, so call it only after `classify`
    returned `SPEND_CAP`, as `ServiceError.spend_cap_reason` does."""
    lowered = code.lower()
    said = message.strip().lower()
    if detail_code.lower() == _SPEND_CAP_DETAIL or said.startswith(_SPEND_CAP_OPENING):
        return SPEND_CAP_REASON_CAP
    if lowered in _CREDIT_CODES or said.startswith(_CREDIT_OPENING):
        return SPEND_CAP_REASON_CREDIT
    if lowered in _QUOTA_CODES or error_type.lower() == _SPEND_TYPE:
        return SPEND_CAP_REASON_QUOTA
    return None


class ServiceError(ProviderError):
    """A model service answered with an error status, or could not be reached.

    Structured for the window (design 2c and 1j): what failed, where, what the service said,
    and when it says to come back. ``str()`` is the one-line message a plain
    ``ProviderError`` would carry and ``kind`` is inherited, so the loop, its logs and
    telemetry groupings are unaffected. Only the desktop and the server's resume read the
    fields. That is why the kind of failure (rate limit, spend cap, console spend limit)
    is `category`, never ``kind``.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "",
        endpoint: str = "",
        provider_message: str = "",
        retry_after: float | None = None,
        detail_code: str = "",
        resumes_at: str | None = None,
        error_type: str = "",
        requested_tokens: int | None = None,
        available_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        #: The HTTP status, or None when no response arrived at all.
        self.status = status
        #: The provider's own error code or type (``rate_limit_error``), or the transport
        #: failure's class name (``ConnectError``). "" when neither said one.
        self.code = code
        self.endpoint = endpoint
        self.provider_message = provider_message
        #: Seconds, from the ``retry-after`` header. None when the provider did not say.
        self.retry_after = retry_after
        #: A finer code some providers put inside the error (the Claude API's
        #: ``error.details.error_code``). "" when there was none.
        self.detail_code = detail_code
        #: When access returns, ISO 8601 with offset: from the provider, or from the rule
        #: that stopped it (a monthly spend cap). None when unknown.
        self.resumes_at = resumes_at
        #: The error's own ``type`` where a provider sends one beside a finer ``code``
        #: (OpenAI's ``insufficient_quota``). "" when it did not, or it is the code itself.
        self.error_type = error_type
        #: A context overflow's sizes when the body has them as separate fields (llama.cpp's
        #: ``n_prompt_tokens`` and ``n_ctx``). See `requested_tokens`.
        self._requested = requested_tokens
        self._available = available_tokens

    @property
    def requested_tokens(self) -> int | None:
        """Size of the refused request in the provider's count, for a `CONTEXT_OVERFLOW`
        whose provider said it. None otherwise."""
        if self.category != CONTEXT_OVERFLOW:
            return None
        if self._requested is not None:
            return self._requested
        return overflow_numbers(self.provider_message)[0]

    @property
    def available_tokens(self) -> int | None:
        """How much one request may hold, for a `CONTEXT_OVERFLOW` whose provider said it.
        None otherwise."""
        if self.category != CONTEXT_OVERFLOW:
            return None
        if self._available is not None:
            return self._available
        return overflow_numbers(self.provider_message)[1]

    @property
    def category(self) -> str:
        """One of `CATEGORIES`; see `classify`. With no response it is ``timeout``, unless
        the request never left because there was no key to send (``auth``)."""
        if self.status is None:
            return AUTH if self.code == "missing_api_key" else TIMEOUT
        return classify(
            self.status, self.code, self.provider_message, self.detail_code, self.error_type
        )

    @property
    def spend_cap_reason(self) -> str | None:
        """`spend_cap_reason(...)` for this error while `category` is `SPEND_CAP`, else
        None (a spend limit the person set, a rate limit, ...)."""
        if self.category != SPEND_CAP:
            return None
        return spend_cap_reason(self.code, self.provider_message, self.detail_code, self.error_type)

    @property
    def is_usage_limit(self) -> bool:
        """A rate limit or a spent quota, as opposed to a service that did not answer. A
        console spend limit counts too, though it arrives as a 400."""
        code = self.code.lower()
        return (
            self.status == 429
            or any(mark in code for mark in _USAGE_LIMIT_MARKS)
            or self.category in SPEND_CATEGORIES
        )


#: `ContextTooSmall.code` when Cogito's own system prompt and tool definitions, counted
#: alone, are over the window: no model with that window can take any request.
FIXED_PART_TOO_LARGE = "fixed_part_too_large"
#: `ContextTooSmall.code` when the loop tried everything that can make a refused request
#: fit (`loop.engine.Engine._answer_overflow`) and it still does not. It says nothing about
#: the cause, only the refusal's numbers.
CONTEXT_UNFIT = "context_unfit"


class ContextTooSmall(ServiceError):
    """The loop stopped a turn because its request cannot be made to fit the model's
    context window. Raised by no provider: the loop builds it from the refusal it could not
    answer, and the window reports it like a provider failure (`provider.usage.
    MeteredProvider.settle`). Always a `CONTEXT_OVERFLOW`, so nothing resumes it by itself
    and the window offers another model."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        requested_tokens: int | None,
        available_tokens: int | None,
        endpoint: str = "",
    ) -> None:
        super().__init__(
            message,
            code=code,
            endpoint=endpoint,
            provider_message=message,
            requested_tokens=requested_tokens,
            available_tokens=available_tokens,
        )

    @property
    def category(self) -> str:
        return CONTEXT_OVERFLOW

