"""Questions to a model host over HTTP: is it up, what does it serve, does it see images,
does it think by default. Any OpenAI-compatible host can be asked; nothing here starts one."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from cogito.config.models import ModelEntry


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    @property
    def models_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1/models"


def endpoint_of(entry: ModelEntry) -> Endpoint:
    parsed = urlparse(entry.base_url)
    return Endpoint(host=parsed.hostname or "127.0.0.1", port=parsed.port or 8080)


def is_up(entry: ModelEntry, *, timeout: float = 2.0) -> bool:
    """True when something is already answering on this entry's port."""
    try:
        response = httpx.get(endpoint_of(entry).health_url, timeout=timeout)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def served_models(entry: ModelEntry, *, timeout: float = 2.0) -> tuple[str, ...]:
    """The model names the server on this entry's port says it is serving.

    Empty when it cannot be asked. An endpoint that answers `/health` but not `/v1/models`
    is not necessarily broken, so empty means "unknown", never "wrong".
    """
    try:
        response = httpx.get(endpoint_of(entry).models_url, timeout=timeout)
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return ()
    rows = payload.get("data") or payload.get("models") or []
    if not isinstance(rows, list):
        return ()
    names = []
    for row in rows:
        if isinstance(row, dict):
            name = row.get("id") or row.get("model") or row.get("name")
            if name:
                names.append(str(name))
    return tuple(names)


def serves_this_entry(entry: ModelEntry, *, timeout: float = 2.0) -> bool | None:
    """Whether the server on this port is serving *this* entry's model.

    None when that cannot be told, and the caller treats None as yes. Many
    OpenAI-compatible servers report a name unrelated to the one configured here, and
    refusing to run against them would be worse than the problem.
    """
    names = served_models(entry, timeout=timeout)
    if not names:
        return None
    wanted = entry.model.strip().lower()
    return any(wanted == name.strip().lower() for name in names)


#: The conversation `thinks_by_default` asks the server to render. Any would do: its two
#: renderings differ only in the thinking switch.
_THINKING_PROBE = ({"role": "user", "content": "Hello."},)


def sees_images(
    entry: ModelEntry, *, client: httpx.Client | None = None, timeout: float = 3.0
) -> bool | None:
    """Whether this llama-server can take images: `/props` says `modalities.vision` (true
    when a projector, `--mmproj`, is loaded; the bench build b264-7ba604f1 reports
    `{"vision": false, "video": false, "audio": false}` for a text model). None when the
    server cannot answer, so the caller can ask again once it is up. Never raises."""
    where = endpoint_of(entry)
    try:
        response = (client or httpx).get(f"http://{where.host}:{where.port}/props", timeout=timeout)
        if response.status_code != 200:
            return None
        modalities = response.json().get("modalities")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None
    if isinstance(modalities, dict):
        return bool(modalities.get("vision"))
    return False


def thinks_by_default(
    entry: ModelEntry, *, client: httpx.Client | None = None, timeout: float = 5.0
) -> bool:
    """Whether llama-server, as started, has this model think on a plain request.

    Nothing the server reports says so directly: at the bench build (llama.cpp 7ba604f1)
    `/props` carries the chat template and its caps, and none of them says whether
    thinking is on. llama-server decides that at start-up from `--reasoning` and from
    whether the template supports thinking (`server-context.cpp`). `/apply-template`
    renders a conversation as a chat request would, without generating ("same with
    handle_chat_completions, but without inference part"). So the probe is rendered twice,
    with the server's default and with `enable_thinking` off, and the two differ only when
    the default turns thinking on. A template without thinking (qwen3-coder's) and a
    server started with `--reasoning off` (qwen3.5-9b in the bench) render the same both
    times.

    False when the server cannot answer, which leaves the caller to watch for reasoning on
    the wire. Never raises.
    """
    where = endpoint_of(entry)
    url = f"http://{where.host}:{where.port}/apply-template"
    messages = [dict(message) for message in _THINKING_PROBE]
    prompts = []
    try:
        for extra in ({}, {"chat_template_kwargs": {"enable_thinking": False}}):
            body = {"messages": messages, **extra}
            response = (client or httpx).post(url, json=body, timeout=timeout)
            if response.status_code != 200:
                return False
            prompts.append(response.json()["prompt"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return False
    return isinstance(prompts[0], str) and prompts[0] != prompts[1]
