"""The connection screen's side of the core (design 1e, and «Conectar otro servicio…» in the
model menu): which services a person can connect, testing one, and what a new catalog entry
for it holds.

Nothing here comes from a model. The services are a hand-written table. An API service is
tested with the one call that costs nothing (listing its models with the key the person
typed), and the list that comes back is what the person chooses from. Cogito Motoris is
checked without running it (`check_motoris_install`) or by asking the one that runs
(`check_motoris_running`). A server on this machine is asked the window it serves
(`local_context`).

A key is sent only to the service it was typed for, over https (or to this machine). It
never appears in anything this module returns or raises. A provider's own error message is
passed on, with the key and anything shaped like a key replaced by ``***``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path
from urllib.parse import urlparse

import httpx

from cogito.config.models import Price
from cogito.errors import ProviderError

#: Seconds for an API service to answer its model list, and for a local server to answer at
#: all. A service that takes longer is shown as not answering, with what it did.
API_TIMEOUT = 15.0
LOCAL_TIMEOUT = 2.0

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})

#: Where Cogito Motoris listens by default (its `serve/server.py`, port 8095).
MOTORIS_URL = "http://127.0.0.1:8095/v1"


@dataclass(frozen=True, slots=True)
class Provider:
    """One service a person can connect with a key.

    ``models_path`` is appended to ``base_url`` to list the models; it is what the test
    calls. ``key_page`` is where the service's own console makes a key, for «Obtener una
    clave». Every address is https and was read on the service's own site (dates below).
    """

    id: str
    #: What the window calls it: the section header the model menu shows for its models
    #: (`provider.service.describe`), so the two never disagree.
    name: str
    api: str
    base_url: str
    models_path: str
    key_page: str | None
    #: The name a catalog entry's label carries after the model's.
    short: str
    #: "other": the person types the base URL themselves.
    custom_url: bool = False


#: Written by hand, never by a model. Sources, each read 2026-09-29:
#: - Claude: the claude-api reference bundled with Claude Code 2.1.280, ``shared/models.md``
#:   ("Raw HTTP": `x-api-key` and `anthropic-version: 2023-06-01`) and
#:   ``shared/managed-agents-api-reference.md`` (Models pages by `after_id`/`has_more`);
#:   keys at platform.claude.com/settings/keys ("Get your Claude API key").
#: - OpenAI: developers.openai.com/api/reference/resources/models/methods/list (`GET /models`,
#:   `Authorization: Bearer`); keys at platform.openai.com/api-keys (OpenAI quickstart).
#: - DeepSeek: api-docs.deepseek.com ("base_url (OpenAI) https://api.deepseek.com") and its
#:   "Lists Models" page (`GET /models`); keys are made on platform.deepseek.com.
#: - OpenRouter: openrouter.ai/docs, "List models filtered by user provider preferences"
#:   (`GET /api/v1/models/user`, Bearer, 401 for a bad key). Its plain `/models` answers
#:   without a key, so it would pass any key: the user-filtered list is the one that tests
#:   it. Keys at openrouter.ai/keys (its "Authentication" page).
#: - Groq: console.groq.com/docs/models (`GET https://api.groq.com/openai/v1/models`,
#:   Bearer); keys at console.groq.com/keys.
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="anthropic",
        name="CLAUDE API",
        api="anthropic",
        base_url="https://api.anthropic.com",
        models_path="/v1/models",
        key_page="https://platform.claude.com/settings/keys",
        short="Claude",
    ),
    Provider(
        id="openai",
        name="OPENAI API",
        api="openai",
        base_url="https://api.openai.com/v1",
        models_path="/models",
        key_page="https://platform.openai.com/api-keys",
        short="OpenAI",
    ),
    Provider(
        id="deepseek",
        name="DEEPSEEK API",
        api="openai",
        base_url="https://api.deepseek.com",
        models_path="/models",
        key_page="https://platform.deepseek.com",
        short="DeepSeek",
    ),
    Provider(
        id="openrouter",
        name="OPENROUTER API",
        api="openai",
        base_url="https://openrouter.ai/api/v1",
        models_path="/models/user",
        key_page="https://openrouter.ai/keys",
        short="OpenRouter",
    ),
    Provider(
        id="groq",
        name="GROQ API",
        api="openai",
        base_url="https://api.groq.com/openai/v1",
        models_path="/models",
        key_page="https://console.groq.com/keys",
        short="Groq",
    ),
    Provider(
        id="other",
        name="Otro compatible con OpenAI",
        api="openai",
        base_url="",
        models_path="/models",
        key_page=None,
        short="API",
        custom_url=True,
    ),
)


def provider(provider_id: str) -> Provider:
    for known in PROVIDERS:
        if known.id == provider_id:
            return known
    raise ProviderError(f"unknown service {provider_id!r}")


#: What a Claude model costs, per million tokens in USD, from the Claude API's price list
#: (platform.claude.com/docs/en/about-claude/pricing, "Model pricing", read 2026-09-30), for
#: every model the claude-api reference bundled with Claude Code 2.1.280 (``shared/models.md``)
#: gives an id for. Cache writes are the list's 1.25x for every model
#: (`config.models.CACHE_WRITE_FACTOR`). A cache-read rate is written only where it is not
#: the standard 0.1x: Fable 5.1 and Mythos 5.1 at 0.025x ($0.25), Opus 5.5 at 0.05x ($0.20).
#: Sonnet 5 is $2/$10: the list says the launch price "is now the standard price". Sonnet 5.5
#: ($2/$10, reads $0.20) and Haiku 5.5 ($0.10/$0.50 up to 100K prompt tokens, $0.50/$2.50
#: beyond) from the claude-api reference bundled with Claude Code 2.1.304 (cached
#: 2026-10-06), which now gives their ids. Any other model gets no price, and the window
#: marks its spend as not counted, never as ≈ 0.
ANTHROPIC_PRICES: Mapping[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, "USD", cache_read_per_mtok=0.25),
    "claude-mythos-5-1": Price(10.0, 50.0, "USD", cache_read_per_mtok=0.25),
    "claude-fable-5": Price(10.0, 50.0, "USD"),
    "claude-mythos-5": Price(10.0, 50.0, "USD"),
    "claude-opus-5-5": Price(4.0, 20.0, "USD", cache_read_per_mtok=0.20),
    "claude-opus-5": Price(5.0, 25.0, "USD"),
    "claude-opus-4-8": Price(5.0, 25.0, "USD"),
    "claude-opus-4-7": Price(5.0, 25.0, "USD"),
    "claude-opus-4-6": Price(5.0, 25.0, "USD"),
    "claude-opus-4-5": Price(5.0, 25.0, "USD"),
    "claude-sonnet-5-5": Price(2.0, 10.0, "USD", cache_read_per_mtok=0.20),
    "claude-sonnet-5": Price(2.0, 10.0, "USD"),
    "claude-sonnet-4-6": Price(3.0, 15.0, "USD"),
    "claude-sonnet-4-5": Price(3.0, 15.0, "USD"),
    "claude-haiku-5-5": Price(
        0.10,
        0.50,
        "USD",
        above_tokens=100_000,
        above_input_per_mtok=0.50,
        above_output_per_mtok=2.50,
    ),
    "claude-haiku-4-5": Price(1.0, 5.0, "USD"),
}

#: A dated snapshot's own id, as `GET /v1/models` lists it ("claude-haiku-4-5-20251001"):
#: the alias and an eight-digit date. It is priced as its alias (the reference's "Full ID"
#: column pairs each alias with its dated id).
_SNAPSHOT = re.compile(r"^(?P<alias>.+)-(?P<date>\d{8})$")


def curated_price(provider_id: str, model: str) -> Price | None:
    """The price from the table above (a dated snapshot's from its alias), or None: no
    price is ever guessed."""
    if provider_id != "anthropic":
        return None
    if model in ANTHROPIC_PRICES:
        return ANTHROPIC_PRICES[model]
    snapshot = _SNAPSHOT.match(model)
    return ANTHROPIC_PRICES.get(snapshot.group("alias")) if snapshot else None


def price_fields(price: Price | None) -> dict[str, object] | None:
    """A price as the ``price`` table of a catalog entry."""
    if price is None:
        return None
    out: dict[str, object] = {
        "input_per_mtok": price.input_per_mtok,
        "output_per_mtok": price.output_per_mtok,
        "currency": price.currency,
    }
    if price.cache_read_per_mtok is not None:
        out["cache_read_per_mtok"] = price.cache_read_per_mtok
    if price.above_tokens is not None:
        out["above_tokens"] = price.above_tokens
        out["above_input_per_mtok"] = price.above_input_per_mtok
        out["above_output_per_mtok"] = price.above_output_per_mtok
    return out


# -- errors ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Failure:
    """Why a test did not succeed, as the window shows it: a fixed title chosen by ``kind``
    and, under it, the real log (status, endpoint and the service's own message).

    ``kind``: ``no_key``, ``unauthorized`` (401/403), ``unreachable`` (no connection),
    ``timeout``, ``rate_limited`` (429), ``http`` (any other status), ``invalid`` (200, but
    not a model list), ``insecure`` (a key would go over plain http), ``bad_url``,
    ``not_local`` (a server that is not on this machine), ``no_server`` (nothing answers
    Cogito Motoris's `/health`), ``not_motoris`` (something else answers, or a folder or
    file is not a Cogito Motoris one), ``plain_http`` (Cogito Motoris on another machine
    over http).
    """

    kind: str
    endpoint: str = ""
    #: The HTTP method the endpoint was asked with ("GET", "POST"), when it was asked at all.
    method: str = ""
    status: int | None = None
    message: str = ""
    host: str = ""
    retry_after: str | None = None
    log: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "endpoint": self.endpoint,
            "method": self.method,
            "status": self.status,
            "message": self.message,
            "host": self.host,
            "retry_after": self.retry_after,
            "log": list(self.log),
            **dict(self.extra),
        }


class ConnectFailed(ProviderError):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.kind)
        self.failure = failure


#: Words that look like a key: a known prefix, then a long run of key characters. A service
#: that quotes back part of the key it refused («Incorrect API key provided: sk-proj-***1234»)
#: has it replaced too.
_KEYLIKE = re.compile(r"\b(?:sk-|sk_|gsk_|xai-|AIza)[-_A-Za-z0-9*.]{6,}")
_MAX_MESSAGE = 400


def redact(text: str, key: str | None) -> str:
    """``text`` with ``key`` and anything shaped like a key replaced by ``***``, clipped."""
    out = str(text or "")
    if key:
        out = out.replace(key, "***")
    out = _KEYLIKE.sub("***", out)
    return out[:_MAX_MESSAGE]


def _service_message(response: httpx.Response, key: str | None) -> str:
    """The service's own words for a refusal: ``error.message`` (or ``message``/``detail``)
    when the body is JSON, else the body's first line."""
    try:
        body = response.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            parts = [str(error.get(k)) for k in ("type", "code") if error.get(k)]
            text = error.get("message")
            message = ": ".join([*parts[:1], str(text)]) if text else " ".join(parts)
        elif isinstance(error, str):
            message = error
        if not message:
            for key_name in ("message", "detail"):
                if isinstance(body.get(key_name), str):
                    message = body[key_name]
                    break
    if not message:
        message = (response.text or "").strip().splitlines()[0] if response.text.strip() else ""
    return redact(message, key)


# -- API services ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Listed:
    """A service's real model list: ``models`` is ``[{"id", "label"}]`` in its own order."""

    endpoint: str
    models: tuple[Mapping[str, str], ...]


def _is_loopback(url: str) -> bool:
    """This machine: ``localhost`` or any loopback address (127.0.0.0/8, ::1)."""
    import ipaddress

    host = urlparse(url).hostname or ""
    if host in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_local_url(url: str) -> bool:
    """A server a local entry may point at: exactly the hosts the catalog groups as local
    (`config.models`), so the entry lands among the local models with no key to ask for."""
    return (urlparse(url).hostname or "") in _LOOPBACK


def _safe_endpoint(text: str) -> str:
    """``text`` as scheme, host, port and path only. Anything that could hold a credential
    (userinfo, no host, no scheme, unparseable) gives a fixed placeholder, never the input."""
    try:
        parsed = urlparse(text)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return "(dirección no válida)"
    if parsed.scheme not in ("http", "https") or not host or "@" in parsed.netloc:
        return "(dirección no válida)"
    host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}{parsed.path}"[:200]


def check_base_url(url: str, *, with_key: bool) -> str:
    """A base URL a person typed, cleaned: http(s) with a host, no query or fragment, no
    trailing slash. A key is only ever sent over https, or to this machine. No failure
    carries the URL's userinfo, query or fragment."""
    cleaned = str(url or "").strip().rstrip("/")
    try:
        parsed = urlparse(cleaned)
        parsed.port  # noqa: B018 - raises on a bad port
    except ValueError:
        raise ConnectFailed(Failure("bad_url")) from None
    shown = _safe_endpoint(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConnectFailed(Failure("bad_url", endpoint=shown))
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ConnectFailed(Failure("bad_url", endpoint=shown))
    if with_key and parsed.scheme != "https" and not _is_loopback(cleaned):
        raise ConnectFailed(Failure("insecure", endpoint=shown, host=parsed.hostname))
    return cleaned


def motoris_url(url: str, *, with_key: bool) -> str:
    """The base URL a Cogito Motoris is reached at, ending in ``/v1``. The one place that
    normalises it, for the check, the route and the saved entry."""
    cleaned = check_base_url(url or MOTORIS_URL, with_key=with_key)
    return cleaned if cleaned.endswith("/v1") else cleaned + "/v1"


def _headers(service: Provider, key: str | None) -> dict[str, str]:
    if service.api == "anthropic":
        return {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"} if key else {}


def _rows(document: object) -> list[Mapping[str, Any]]:
    rows = document.get("data") if isinstance(document, Mapping) else None
    if rows is None and isinstance(document, Mapping):
        rows = document.get("models")
    if not isinstance(rows, list):
        raise ValueError("no data list")
    return [row for row in rows if isinstance(row, Mapping)]


def list_models(
    service: Provider,
    key: str | None,
    *,
    base_url: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = API_TIMEOUT,
) -> Listed:
    """Ask ``service`` for its models with ``key``: the connection test. Free (a list is
    not a request to a model) and real: a key the service refuses is refused here.

    Raises `ConnectFailed` with what happened, never with the key in it.
    """
    key = (key or "").strip() or None
    if service.custom_url:
        root = check_base_url(base_url or "", with_key=key is not None)
        # The rule saving applies (`cli._Connect.save`): off this machine, an entry with no
        # key could never be used. A test that passes must be one that can be saved.
        if key is None and not is_local_url(root):
            raise ConnectFailed(Failure("no_key", endpoint=root + service.models_path))
    else:
        root = service.base_url
        if key is None:
            raise ConnectFailed(Failure("no_key", endpoint=root + service.models_path))
    endpoint = root + service.models_path
    host = urlparse(endpoint).hostname or ""
    own = client is None
    session = client or httpx.Client(timeout=timeout, follow_redirects=False)
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        after: str | None = None
        for _page in range(20):
            params = {"after_id": after} if after else None
            try:
                response = session.get(endpoint, headers=_headers(service, key), params=params)
            except httpx.TimeoutException:
                raise ConnectFailed(
                    Failure("timeout", endpoint=endpoint, method="GET", host=host)
                ) from None
            except httpx.HTTPError as exc:
                raise ConnectFailed(
                    Failure(
                        "unreachable",
                        endpoint=endpoint,
                        method="GET",
                        host=host,
                        message=redact(type(exc).__name__ + ": " + str(exc), key),
                    )
                ) from None
            if response.status_code != 200:
                status = response.status_code
                kind = (
                    "unauthorized"
                    if status in (401, 403)
                    else "rate_limited"
                    if status == 429
                    else "http"
                )
                raise ConnectFailed(
                    Failure(
                        kind,
                        endpoint=endpoint,
                        method="GET",
                        status=status,
                        host=host,
                        message=_service_message(response, key),
                        retry_after=response.headers.get("retry-after"),
                    )
                )
            try:
                document = response.json()
                rows = _rows(document)
            except ValueError:
                raise ConnectFailed(
                    Failure(
                        "invalid",
                        endpoint=endpoint,
                        method="GET",
                        status=200,
                        host=host,
                        message=redact((response.text or "")[:200], key),
                    )
                ) from None
            for row in rows:
                model_id = str(row.get("id") or row.get("name") or "").strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                label = str(row.get("display_name") or row.get("name") or model_id).strip()
                models.append({"id": model_id, "label": label or model_id})
            more = isinstance(document, Mapping) and document.get("has_more") is True
            after = document.get("last_id") if more else None
            if not (more and isinstance(after, str) and after):
                break
    finally:
        if own:
            session.close()
    return Listed(endpoint=endpoint, models=tuple(models))


# -- Cogito Motoris -------------------------------------------------------------------------


def check_motoris_running(
    url: str,
    key: str | None,
    *,
    client: httpx.Client | None = None,
    timeout: float = API_TIMEOUT,
) -> list[str]:
    """The model ids a Cogito Motoris at ``url`` serves. It must answer `/health` as
    ``service = "strata"`` (cogito-motoris serve/server.py). The key goes to that address
    only, as a bearer. Raises `ConnectFailed`: ``plain_http``, ``no_server``,
    ``not_motoris``."""
    key = (key or "").strip() or None
    # Plain http off this machine is refused for Cogito Motoris whether or not a key is
    # typed, with its own words.
    base = motoris_url(url, with_key=False)
    parsed = urlparse(base)
    root = base[: -len("/v1")]
    host = parsed.hostname or ""
    if parsed.scheme != "https" and not _is_loopback(root):
        raise ConnectFailed(
            Failure(
                "plain_http",
                endpoint=root,
                host=host,
                message="Cogito Motoris en otra máquina necesita https; sin él la clave "
                "viaja sin cifrar.",
            )
        )
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    own = client is None
    session = client or httpx.Client(timeout=timeout, follow_redirects=False)

    def no_server(status: int | None = None, detail: str = "") -> ConnectFailed:
        return ConnectFailed(
            Failure(
                "no_server",
                endpoint=root + "/health",
                method="GET",
                status=status,
                host=host,
                message="No responde en esa dirección.",
                log=(redact(detail, key),) if detail else (),
            )
        )

    try:
        try:
            response = session.get(root + "/health", headers=headers)
        except httpx.HTTPError as exc:
            raise no_server(detail=type(exc).__name__ + ": " + str(exc)) from None
        if response.status_code != 200:
            raise no_server(response.status_code)
        try:
            document = response.json()
        except ValueError:
            document = None
        if not isinstance(document, Mapping) or document.get("service") != "strata":
            raise ConnectFailed(
                Failure(
                    "not_motoris",
                    endpoint=root + "/health",
                    method="GET",
                    status=200,
                    host=host,
                    message="Ahí responde otro servidor, no Cogito Motoris.",
                )
            )
        endpoint = root + "/v1/models"
        try:
            listed = session.get(endpoint, headers=headers)
            if listed.status_code != 200:
                raise no_server(listed.status_code)
            rows = _rows(listed.json())
        except httpx.HTTPError as exc:
            raise no_server(detail=type(exc).__name__ + ": " + str(exc)) from None
        except ValueError:
            raise ConnectFailed(
                Failure("invalid", endpoint=endpoint, method="GET", status=200, host=host)
            ) from None
    finally:
        if own:
            session.close()
    ids: list[str] = []
    for row in rows:
        model_id = str(row.get("id") or row.get("name") or "").strip()
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids


def check_motoris_install(checkout: str, config: str) -> dict[str, Any]:
    """Read a Cogito Motoris install without running it: the checkout has its own Python
    and `serve/server.py`, and the engine config names an ``exe``. Returns the checkout,
    the config as given, the model's name and the window (``--max-context``, or None)."""
    from cogito.config.models import ModelEntry, max_context_arg
    from cogito.provider.local import motoris

    folder_message = "Esa carpeta no tiene Cogito Motoris preparado."
    file_message = "Ese archivo no es una configuración de Cogito Motoris."
    root = Path(str(checkout or "").strip()).expanduser()
    path = Path(str(config or "").strip()).expanduser()
    if not path.is_absolute():
        path = root / path

    def refuse(message: str, target: Path) -> ConnectFailed:
        return ConnectFailed(Failure("not_motoris", endpoint=str(target)[:200], message=message))

    probe = ModelEntry(
        id="motoris", label="", api="openai", base_url=MOTORIS_URL, model="",
        serve="motoris", args={"motoris": str(root)},
    )
    try:
        motoris.python_for(probe)
    except ProviderError:
        raise refuse(folder_message, root) from None
    if not (root / "serve" / "server.py").is_file():
        raise refuse(folder_message, root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise refuse(file_message, path) from None
    exe = document.get("exe") if isinstance(document, Mapping) else None
    if not isinstance(exe, str) or not exe.strip():
        raise refuse(file_message, path)
    name = document.get("model_name")
    model = name.strip() if isinstance(name, str) and name.strip() else path.stem
    ctx = max_context_arg(document.get("args"))
    return {"checkout": str(checkout).strip(), "config": str(config).strip(), "model": model, "ctx": ctx}


# -- a server on this machine ---------------------------------------------------------------


def _root_of(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConnectFailed(Failure("bad_url", endpoint=url.strip()[:200]))
    if parsed.hostname not in _LOOPBACK:
        raise ConnectFailed(Failure("not_local", endpoint=url.strip()[:200], host=parsed.hostname))
    return f"{parsed.scheme}://{parsed.netloc}"


def local_context(
    base_url: str,
    model: str | None = None,
    *,
    client: httpx.Client | None = None,
    timeout: float = LOCAL_TIMEOUT,
) -> int | None:
    """How many tokens one request may hold on the llama.cpp server at ``base_url``: its
    `GET /props` ``default_generation_settings.n_ctx`` (tools/server/README.md, read
    2026-10-11). ``model`` is asked for as ``?model=`` (what router mode wants for its other
    per-model routes), and an answer that names another model (``model_alias`` and the
    file in ``model_path``) is not taken: a server with several models says one window.

    None for a server off this machine, one that does not say (Ollama has no such field:
    its `/api/show` gives the model's trained length, not the window it serves), or any
    failure. Never raises."""
    try:
        root = _root_of(base_url)
    except ConnectFailed:
        return None
    own = client is None
    session = client or httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        response = session.get(f"{root}/props", params={"model": model} if model else None)
        document = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            session.close()
    if not isinstance(document, Mapping):
        return None
    if model and not _props_name(document, model):
        return None
    settings = document.get("default_generation_settings")
    value = settings.get("n_ctx") if isinstance(settings, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _props_name(document: Mapping[str, Any], model: str) -> bool:
    """Whether a `/props` answer may be about ``model``: it names none, or names it as its
    alias or its file (with or without `.gguf`)."""
    alias = document.get("model_alias")
    path = document.get("model_path")
    names = set()
    if isinstance(alias, str) and alias.strip():
        names.add(alias.strip())
    if isinstance(path, str) and path.strip():
        stem = path.strip().replace("\\", "/").rsplit("/", 1)[-1]
        names.update({stem, stem.removesuffix(".gguf")})
    return not names or model in names


#: The services the «SERVICIO» menu offers. Cogito is tuned for Claude and Cogito Motoris
#: (the per-model harness of 2026-10-30), so the person chose to list only the Claude API
#: there for now (2026-10-31); the window adds Cogito Motoris itself. The other services stay
#: defined, so an entry saved with one keeps working and the list can grow back.
SHOWN_PROVIDERS = frozenset({"anthropic"})


def providers_view() -> list[dict[str, Any]]:
    """The services, as the window's «SERVICIO» menu lists them."""
    return [
        {
            "id": known.id,
            "name": known.name,
            "api": known.api,
            "custom_url": known.custom_url,
            "base_url": known.base_url,
            "key_page": bool(known.key_page),
        }
        for known in PROVIDERS
        if known.id in SHOWN_PROVIDERS
    ]
