"""How close a model is to its per-minute limits, read from response headers.

Both dialects report the same facts under different names:

- The Claude API sends ``anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-
  {limit,remaining,reset}``. ``reset`` is an RFC 3339 time, and a token ``remaining`` is
  rounded to the nearest thousand (platform.claude.com/docs/en/api/rate-limits, "Response
  headers", read 2026-09-29).
- OpenAI sends ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}``. ``reset`` is a
  duration such as ``1s`` or ``6m0s`` (developers.openai.com/api/docs/guides/rate-limits,
  read 2026-09-29).

This module only reports what each limiter says and which has the least left. The
threshold is `NEAR_BELOW`; the display is the desktop's. Nothing here is sent to a model.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from cogito.provider.base import RateLimit

#: The chip shows while any limiter has less than this fraction left (design 2e).
NEAR_BELOW = 0.10

#: Limiters the headroom is taken over: the lowest of requests and tokens (design 2e). The
#: combined ``tokens`` limiter counts only when the separate input and output ones are
#: absent, as with OpenAI.
_SPLIT = ("requests", "input_tokens", "output_tokens")

_ANTHROPIC = {
    "requests": "requests",
    "tokens": "tokens",
    "input-tokens": "input_tokens",
    "output-tokens": "output_tokens",
}
_OPENAI = {"requests": "requests", "tokens": "tokens"}

#: One Go-style duration part: ``6m0s``, ``1h2m``, ``20ms``, ``1.5s``.
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def _count(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        number = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _rfc3339(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat()


def _duration(value: str | None, now: datetime) -> str | None:
    if not value or not value.strip():
        return None
    text = value.strip()
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(number + unit for number, unit in parts) != text:
        return None
    seconds = sum(float(number) * _UNITS[unit] for number, unit in parts)
    if not math.isfinite(seconds) or seconds > 86_400 * 7:
        return None
    return (now.astimezone(UTC) + timedelta(seconds=seconds)).isoformat()


def from_headers(
    headers: Mapping[str, str], *, now: datetime | None = None
) -> dict[str, RateLimit]:
    """Every limiter the headers report in full (limit and remaining), by name. Empty when
    there are none, as with every local server."""
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    moment = now or datetime.now(UTC)
    found: dict[str, RateLimit] = {}
    for part, name in _ANTHROPIC.items():
        prefix = f"anthropic-ratelimit-{part}-"
        limit = _count(lowered.get(prefix + "limit"))
        remaining = _count(lowered.get(prefix + "remaining"))
        if limit is not None and remaining is not None:
            found[name] = RateLimit(limit, remaining, _rfc3339(lowered.get(prefix + "reset")))
    for part, name in _OPENAI.items():
        if name in found:
            continue
        limit = _count(lowered.get(f"x-ratelimit-limit-{part}"))
        remaining = _count(lowered.get(f"x-ratelimit-remaining-{part}"))
        if limit is not None and remaining is not None:
            reset = _duration(lowered.get(f"x-ratelimit-reset-{part}"), moment)
            found[name] = RateLimit(limit, remaining, reset)
    return found


def headroom(limits: Mapping[str, RateLimit]) -> tuple[float, str] | None:
    """``(fraction left, name)`` of the request or token limiter with the least left. None
    when no limiter with a size was reported."""
    names = [name for name in _SPLIT if name in limits]
    if "tokens" in limits and not any(name in limits for name in _SPLIT[1:]):
        names.append("tokens")
    measured = [
        (fraction, name)
        for name in names
        if (fraction := limits[name].fraction) is not None
    ]
    return min(measured) if measured else None


def percent(fraction: float) -> int:
    """The chip's value in whole percent, rounded down so a limiter under 10 % never reads
    "10 %"."""
    return max(0, min(100, math.floor(fraction * 100)))
