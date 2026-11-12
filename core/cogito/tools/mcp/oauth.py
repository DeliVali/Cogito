"""Signing in to a remote MCP server: OAuth 2.1 with PKCE, as the MCP specification asks.

Read from the specification's Authorization section (modelcontextprotocol.io, 2025-11-25
revision, read 2026-10-29):

- A 401's `WWW-Authenticate` names the Protected Resource Metadata (RFC 9728) under
  `resource_metadata`; without it the client tries the well-known URI with the endpoint's
  path inserted, then at the root.
- That document's `authorization_servers` names the issuer. Its metadata is tried at the
  RFC 8414 path, then OpenID Connect's, with the issuer's path inserted, then OpenID
  Connect's appended. Metadata without `code_challenge_methods_supported` means no PKCE, and
  the client must refuse.
- Scope: the challenge's `scope`, else the resource's `scopes_supported`, else none.
- The client registers with `registration_endpoint` (RFC 7591) when it has no client of its
  own (`client_id` in the server's table). Client ID Metadata Documents need an HTTPS page
  Cogito does not have, so they are not used.
- `resource` (RFC 8707, the endpoint without a trailing slash) goes in the authorization
  and token requests; redirect URIs are loopback; `state` is checked.

A server that serves no resource metadata at all (written to the 2025-03-26 revision) is
read the way that revision said: the endpoint's origin is the authorization server, with
`/authorize`, `/token` and `/register` as the defaults.

What is kept, per server address, in the owner-only `secrets.toml`: the registered client,
the redirect it registered, and the tokens.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets as random
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from cogito.config import secrets

_SECTION = "mcp-oauth"
_TIMEOUT = 15.0
#: How long the browser's answer is waited for.
LOGIN_WAIT = 300.0
#: A token this close to its expiry is refreshed before it is sent.
_EARLY = 60.0


class AuthError(Exception):
    """Signing in could not be done; the message says why."""


def canonical(url: str) -> str:
    """The server's address as a `resource`: lowercase scheme and host, no fragment, no
    trailing slash."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


@dataclass
class Challenge:
    """What a 401 or 403 said: where the resource metadata is, and which scope it wants."""

    resource_metadata: str = ""
    scope: str = ""
    error: str = ""


_PARAM = re.compile(r'([A-Za-z_]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


def read_challenge(header: str) -> Challenge:
    values = {key.lower(): quoted if quoted else bare for key, quoted, bare in _PARAM.findall(header or "")}
    return Challenge(
        resource_metadata=values.get("resource_metadata", ""),
        scope=values.get("scope", ""),
        error=values.get("error", ""),
    )


# -- what is kept --------------------------------------------------------------------


@dataclass
class Grant:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    token_endpoint: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, url: str) -> Grant | None:
        raw = secrets.get(_SECTION, canonical(url))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        known = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        return cls(**known)

    def save(self, url: str) -> None:
        secrets.set_(_SECTION, canonical(url), json.dumps(self.__dict__, separators=(",", ":")))

    @staticmethod
    def forget(url: str) -> None:
        secrets.delete(_SECTION, canonical(url))

    @property
    def fresh(self) -> bool:
        return bool(self.access_token) and (not self.expires_at or time.time() < self.expires_at - _EARLY)


def bearer(url: str, http: httpx.Client) -> str:
    """The access token for this server, refreshed first when it is about to run out; ""
    when there is none or it can no longer be refreshed."""
    grant = Grant.load(url)
    if grant is None or not grant.access_token:
        return ""
    if grant.fresh:
        return grant.access_token
    return refresh(url, grant, http)


def refresh(url: str, grant: Grant, http: httpx.Client) -> str:
    if not grant.refresh_token or not grant.token_endpoint:
        return ""
    form = {
        "grant_type": "refresh_token",
        "refresh_token": grant.refresh_token,
        "client_id": grant.client_id,
        "resource": canonical(url),
    }
    try:
        answer = _token_request(http, grant, form)
    except AuthError:
        return ""
    _take_tokens(grant, answer)
    grant.save(url)
    return grant.access_token


# -- discovery -----------------------------------------------------------------------


def _get_json(http: httpx.Client, url: str) -> dict[str, Any] | None:
    try:
        response = http.get(url, headers={"Accept": "application/json"}, timeout=_TIMEOUT)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _well_known(base: str, name: str) -> list[str]:
    """`name` under `base`: with the path inserted after `/.well-known/name`, then at the
    root, then (OpenID Connect only) appended."""
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    found = []
    if path:
        found.append(f"{origin}/.well-known/{name}{path}")
    found.append(f"{origin}/.well-known/{name}")
    return found


def _resource_metadata(http: httpx.Client, url: str, challenge: Challenge) -> dict[str, Any] | None:
    places = [challenge.resource_metadata] if challenge.resource_metadata else []
    places += _well_known(url, "oauth-protected-resource")
    for place in places:
        found = _get_json(http, place)
        if found and found.get("authorization_servers"):
            return found
    return None


def _server_metadata(http: httpx.Client, issuer: str) -> dict[str, Any] | None:
    parts = urllib.parse.urlsplit(issuer)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if path:
        places = [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{origin}{path}/.well-known/openid-configuration",
        ]
    else:
        places = [
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration",
        ]
    for place in places:
        found = _get_json(http, place)
        if found and found.get("authorization_endpoint") and found.get("token_endpoint"):
            return found
    return None


@dataclass
class Plan:
    """Where to sign in, found before the browser opens."""

    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scope: str


def discover(http: httpx.Client, url: str, challenge: Challenge) -> Plan:
    resource = _resource_metadata(http, url, challenge)
    if resource is not None:
        issuer = str(resource["authorization_servers"][0])
        metadata = _server_metadata(http, issuer)
        if metadata is None:
            raise AuthError(f"the sign-in server {issuer} publishes no OAuth metadata")
        if "S256" not in (metadata.get("code_challenge_methods_supported") or ()):
            raise AuthError("the sign-in server does not support PKCE (S256), which MCP requires")
        scope = challenge.scope or " ".join(resource.get("scopes_supported") or ())
    else:
        # The 2025-03-26 revision: the server's origin is its own authorization server,
        # with these paths when it publishes no metadata.
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        metadata = _server_metadata(http, origin) or {
            "authorization_endpoint": f"{origin}/authorize",
            "token_endpoint": f"{origin}/token",
            "registration_endpoint": f"{origin}/register",
        }
        scope = challenge.scope
    for key in ("authorization_endpoint", "token_endpoint"):
        if not str(metadata[key]).startswith("https://") and not _loopback(str(metadata[key])):
            raise AuthError(f"the sign-in server's {key} is not https")
    return Plan(
        authorization_endpoint=str(metadata["authorization_endpoint"]),
        token_endpoint=str(metadata["token_endpoint"]),
        registration_endpoint=str(metadata.get("registration_endpoint") or ""),
        scope=scope,
    )


def _loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


# -- the sign-in itself ---------------------------------------------------------------


def _register(http: httpx.Client, plan: Plan, redirect_uri: str) -> tuple[str, str]:
    if not plan.registration_endpoint:
        raise AuthError(
            "the server does not register clients itself; register one with it and put its "
            "client_id in the server's table"
        )
    body = {
        "client_name": "Cogito",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        response = http.post(plan.registration_endpoint, json=body, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        raise AuthError(f"registering with the sign-in server failed: {exc}") from exc
    if response.status_code not in (200, 201):
        raise AuthError(f"the sign-in server refused to register Cogito ({response.status_code})")
    data = response.json()
    if not data.get("client_id"):
        raise AuthError("the sign-in server registered no client_id")
    return str(data["client_id"]), str(data.get("client_secret") or "")


def _token_request(http: httpx.Client, grant: Grant, form: dict[str, str]) -> dict[str, Any]:
    auth = (grant.client_id, grant.client_secret) if grant.client_secret else None
    try:
        response = http.post(
            grant.token_endpoint,
            data=form,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"the token request failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise AuthError(f"the token endpoint answered {response.status_code} without JSON") from exc
    if response.status_code != 200 or not data.get("access_token"):
        raise AuthError(f"the token endpoint refused: {data.get('error_description') or data.get('error') or response.status_code}")
    return data


def _take_tokens(grant: Grant, data: dict[str, Any]) -> None:
    grant.access_token = str(data["access_token"])
    # A public client's refresh token rotates (OAuth 2.1, 4.3.1); keep the old one when none
    # is sent back.
    grant.refresh_token = str(data.get("refresh_token") or grant.refresh_token)
    expires = data.get("expires_in")
    grant.expires_at = time.time() + float(expires) if isinstance(expires, (int, float)) else 0.0
    grant.scope = str(data.get("scope") or grant.scope)


def _pkce() -> tuple[str, str]:
    verifier = random.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _Callback(http.server.BaseHTTPRequestHandler):
    answer: dict[str, str] = {}
    arrived: threading.Event

    def do_GET(self) -> None:  # noqa: N802 - the handler's name is the standard library's
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        type(self).answer = {key: values[0] for key, values in query.items()}
        type(self).arrived.set()
        done = "error" not in query
        body = (
            "<!doctype html><meta charset=utf-8><title>Cogito</title>"
            f"<p style='font:16px sans-serif;margin:3em'>{'Listo. Ya puedes volver a Cogito.' if done else 'No se pudo iniciar sesión.'}</p>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


def _listen(port: int) -> http.server.HTTPServer:
    try:
        return http.server.HTTPServer(("127.0.0.1", port), _Callback)
    except OSError:
        return http.server.HTTPServer(("127.0.0.1", 0), _Callback)


@dataclass
class Login:
    """A sign-in under way: the address to open, and a wait for its end."""

    url: str
    finish: Callable[[float], None]


def begin(url: str, challenge: Challenge, client_id: str = "", http: httpx.Client | None = None) -> Login:
    """Find where to sign in, register when needed, and listen on loopback for the browser.
    `finish(timeout)` waits for the answer and keeps the tokens; it raises AuthError."""
    http = http or httpx.Client(follow_redirects=True)
    plan = discover(http, url, challenge)
    kept = Grant.load(url) or Grant()
    previous_port = urllib.parse.urlsplit(kept.redirect_uri).port if kept.redirect_uri else 0
    server = _listen(previous_port or 0)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    grant = Grant(token_endpoint=plan.token_endpoint, redirect_uri=redirect_uri, scope=plan.scope)
    if client_id:
        grant.client_id = client_id
    elif kept.client_id and kept.redirect_uri == redirect_uri and kept.token_endpoint == plan.token_endpoint:
        grant.client_id, grant.client_secret = kept.client_id, kept.client_secret
    else:
        try:
            grant.client_id, grant.client_secret = _register(http, plan, redirect_uri)
        except AuthError:
            server.server_close()
            raise
    verifier, code_challenge = _pkce()
    state = random.token_urlsafe(24)
    query = {
        "response_type": "code",
        "client_id": grant.client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": canonical(url),
    }
    if plan.scope:
        query["scope"] = plan.scope
    separator = "&" if "?" in plan.authorization_endpoint else "?"
    address = plan.authorization_endpoint + separator + urllib.parse.urlencode(query)

    arrived = threading.Event()
    handler = type("Callback", (_Callback,), {"arrived": arrived, "answer": {}})
    server.RequestHandlerClass = handler
    threading.Thread(target=server.serve_forever, name="mcp-oauth-callback", daemon=True).start()

    def finish(timeout: float = LOGIN_WAIT) -> None:
        try:
            if not arrived.wait(timeout):
                raise AuthError("the browser did not come back in time")
            answer = handler.answer
        finally:
            server.shutdown()
            server.server_close()
        if answer.get("state") != state:
            raise AuthError("the sign-in answered for another request (state does not match)")
        if "error" in answer:
            raise AuthError(answer.get("error_description") or answer["error"])
        form = {
            "grant_type": "authorization_code",
            "code": answer.get("code", ""),
            "redirect_uri": redirect_uri,
            "client_id": grant.client_id,
            "code_verifier": verifier,
            "resource": canonical(url),
        }
        _take_tokens(grant, _token_request(http, grant, form))
        grant.save(url)

    return Login(url=address, finish=finish)

