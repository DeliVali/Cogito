"""The real name of the server behind a model (design 2b): «Cogito Motoris LOCAL», a local
server by its host, or an API by its service.

- An entry Cogito serves with Cogito Motoris (`serve = "motoris"`) is «Cogito Motoris
  LOCAL», with no request.
- Any other loopback entry is asked once, read-only with a short timeout, for `GET /health`.
  If it answers `service: "strata"` (Motoris's own health document), it is «Cogito Motoris
  LOCAL» too.
- Any other local server is named by its host, e.g. "127.0.0.1:8080 LOCAL". A fixed
  placeholder would be an invented fact (2026-09-29 UX audit, PROVENANCE report).
- A remote entry is named by the service behind its host from a fixed table, else by the
  host. It is never contacted.

Only the service name and the design's words "LOCAL" and "API" come from here. The
desktop writes every sentence around them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from cogito.config.models import ModelEntry

#: Seconds. A local server slower than this to answer /health does not hold up the menu.
HEALTH_TIMEOUT = 2.0

#: A failed check is retried after this long, in case the server was still starting.
_RETRY_SECONDS = 60.0

#: Hosts whose service has a well-known name.
_API_NAMES: tuple[tuple[str, str], ...] = (
    ("anthropic.com", "Claude"),
    ("openai.com", "OpenAI"),
    ("deepseek.com", "DeepSeek"),
    ("openrouter.ai", "OpenRouter"),
    ("groq.com", "Groq"),
    ("mistral.ai", "Mistral"),
    ("generativelanguage.googleapis.com", "Gemini"),
    ("x.ai", "xAI"),
    ("together.xyz", "Together"),
    ("fireworks.ai", "Fireworks"),
    ("cerebras.ai", "Cerebras"),
)


@dataclass(frozen=True, slots=True)
class Console:
    """Provider console pages for what COGITO can only estimate: billing (prepaid credit,
    the provider's spend cap), the user's own spend limit, and usage with the official
    figure. Every address is https and hand-written from the provider's documentation,
    never taken from a response, a model or a config file."""

    billing: str
    spend_limit: str
    usage: str

    def for_category(self, category: str) -> str | None:
        """The page a service error's card links to: billing for a provider's spend cap
        («Abrir facturación»), the limit page for a limit the user set («Ajustar límite»),
        None otherwise."""
        if category == "spend_cap":
            return self.billing
        if category == "user_spend_limit":
            return self.spend_limit
        return None


#: Hosts with a known console, matched by domain like `_API_NAMES`.
#: Claude: platform.claude.com/docs/en/api/rate-limits (read 2026-09-29). The monthly spend
#: cap and "set your own limit" are both on Settings > Billing; usage is on the Usage page.
#: OpenAI: developers.openai.com/api/docs/guides/error-codes (read 2026-09-29) links "Add
#: credits" to .../billing and "organization spend limit" to .../limits. The Usage and Costs
#: dashboards are at .../usage (OpenAI changelog, August 4 entry, read 2026-09-29).
_CONSOLES: tuple[tuple[str, Console], ...] = (
    (
        "anthropic.com",
        Console(
            billing="https://platform.claude.com/settings/billing",
            spend_limit="https://platform.claude.com/settings/billing",
            usage="https://platform.claude.com/usage",
        ),
    ),
    (
        "openai.com",
        Console(
            billing="https://platform.openai.com/settings/organization/billing",
            spend_limit="https://platform.openai.com/settings/organization/limits",
            usage="https://platform.openai.com/settings/organization/usage",
        ),
    ),
)


def console(entry: ModelEntry) -> Console | None:
    """The console of the entry's API, or None for a local entry or an unknown host. Makes
    no requests."""
    if entry.origin != "api":
        return None
    host = urlparse(entry.base_url).hostname or ""
    for domain, known in _CONSOLES:
        if host == domain or host.endswith("." + domain):
            return known
    return None


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    origin: str
    name: str
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"origin": self.origin, "name": self.name, "label": self.label}


def _get(url: str, timeout: float) -> httpx.Response:
    return httpx.get(url, timeout=timeout)


#: base_url -> (ServiceInfo, monotonic time it was decided, final?)
_CACHE: dict[str, tuple[ServiceInfo, float, bool]] = {}
_CACHE_LOCK = threading.Lock()


def _api_name(host: str) -> str:
    for domain, name in _API_NAMES:
        if host == domain or host.endswith("." + domain):
            return name
    return host


def _health_service(entry: ModelEntry, get: Callable[[str, float], Any] = _get) -> str | None:
    """The `service` field of the entry's `/health`, or None when it does not say."""
    parsed = urlparse(entry.base_url)
    try:
        response = get(f"{parsed.scheme}://{parsed.netloc}/health", HEALTH_TIMEOUT)
        if response.status_code != 200:
            return None
        document = response.json()
    except (httpx.HTTPError, ValueError, OSError):
        return None
    name = document.get("service") if isinstance(document, dict) else None
    return name if isinstance(name, str) else None


def describe(
    entry: ModelEntry,
    *,
    get: Callable[[str, float], Any] = _get,
    cache: dict[str, tuple[ServiceInfo, float, bool]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ServiceInfo:
    parsed = urlparse(entry.base_url)
    host = parsed.hostname or entry.base_url
    if entry.origin == "api":
        name = _api_name(host)
        return ServiceInfo("api", name, f"{name.upper() if name != host else name} API")
    motoris = ServiceInfo("local", "Cogito Motoris", "Cogito Motoris LOCAL")
    if entry.serve == "motoris":
        return motoris

    store = _CACHE if cache is None else cache
    with _CACHE_LOCK:
        cached = store.get(entry.base_url)
    if cached is not None and (cached[2] or clock() - cached[1] < _RETRY_SECONDS):
        return cached[0]
    if _health_service(entry, get) == "strata":
        info, final = motoris, True
    else:
        local_host = parsed.netloc or entry.base_url
        info, final = ServiceInfo("local", local_host, f"{local_host} LOCAL"), False
    with _CACHE_LOCK:
        store[entry.base_url] = (info, clock(), final)
    return info
