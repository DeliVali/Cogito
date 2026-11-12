"""The client the web tools read with.

GET only. No identity: no cookie survives a request, no Authorization header is ever sent,
and the User-Agent says what this is. Every request goes to an address `urls.Guard` checked
for that same request (the transport connects to the checked address and tells the server the
name, so a DNS answer that changes in between is never used), and so does every hop of a
redirect. A redirect to another site is reported, not followed; the model decides whether it
is the page it wants, and it still has to pass the rules in `urls`.

Limits are those of the design (5.2, 2.2): 10 seconds to connect, 30 in all, 5 redirects, a
10 MB body. The same shape as Claude Code's WebFetch (code.claude.com/docs/en/tools-reference,
read 2026-10-12: HTTPS upgrade, a 5-minute deadline, cross-host redirects handed back), with
a shorter deadline, since a person is waiting.
"""

from __future__ import annotations

import codecs
import http.cookiejar
import json
import re
import ssl
import time
from dataclasses import dataclass
from importlib import metadata
from urllib.parse import urljoin

import httpx

from cogito.cancel import CancelToken
from cogito.config.settings import WebSettings
from cogito.tools.web import urls

#: What a page may be asked for: Markdown first (Cloudflare's "Markdown for agents" cut one
#: page from 16,180 tokens of HTML to 3,150, thenewstack.io, secondary coverage, 2026), then
#: HTML, text and PDF.
ACCEPT = "text/markdown, text/html;q=0.9, text/plain;q=0.8, application/pdf;q=0.5"
MAX_BYTES = 10_000_000
DEADLINE_SECONDS = 30.0
CONNECT_SECONDS = 10.0
MAX_REDIRECTS = 5

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)


def user_agent() -> str:
    try:
        version = metadata.version("cogito")
    except metadata.PackageNotFoundError:
        version = "0"
    # "bot" because the Wikimedia User-Agent policy asks a program to say so.
    return f"Cogito-User/{version} (+https://github.com/DeliVali/cogito; bot)"


@dataclass(frozen=True, slots=True)
class Fetched:
    status: int
    headers: httpx.Headers
    body: bytes
    #: The address that was read last (after same-site redirects).
    url: str
    #: Set when the page moved to another site: the address it moved to, not followed.
    redirect: str | None = None


def decode_body(body: bytes, content_type: str = "") -> str:
    """``body`` as text: the charset the header names, else the page's own ``<meta>``, else
    UTF-8 with replacement. A byte-order mark is dropped."""
    if body.startswith(codecs.BOM_UTF8):
        return body[3:].decode("utf-8", errors="replace")
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return body.decode("utf-16", errors="replace")
    named = None
    if (match := _CHARSET.search(content_type)) is not None:
        named = match.group(1)
    elif (meta := _META_CHARSET.search(body[:4096])) is not None:
        named = meta.group(1).decode("ascii", errors="ignore")
    if named:
        try:
            return body.decode(codecs.lookup(named).name, errors="replace")
        except LookupError:
            pass
    return body.decode("utf-8", errors="replace")


class _Pinned(httpx.BaseTransport):
    """Connects to the addresses `guard` allowed for this request, and only those."""

    def __init__(self, inner: httpx.BaseTransport, guard: urls.Guard) -> None:
        self._inner = inner
        self._guard = guard

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        addresses = self._guard.addresses(host, port)
        failure: Exception | None = None
        for address in addresses:
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers=request.headers,
                # A POST's body (the keyless search APIs); empty for a GET.
                content=request.read(),
                extensions={**request.extensions, "sni_hostname": host},
            )
            try:
                return self._inner.handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None
        raise failure

    def close(self) -> None:
        self._inner.close()


class WebClient:
    """Reads one address at a time, under the person's `[web]` settings."""

    def __init__(
        self,
        web: WebSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: urls.Resolver | None = None,
    ) -> None:
        self.settings = web
        self.guard = urls.Guard(
            resolver=resolver,
            allow_hosts=web.allow_hosts,
            only_hosts=web.only_hosts,
            blocked_hosts=web.blocked_hosts,
        )
        self._transport = transport

    def get(
        self,
        url: str,
        *,
        cancel: CancelToken,
        accept: str = ACCEPT,
        max_bytes: int = MAX_BYTES,
        deadline: float = DEADLINE_SECONDS,
    ) -> Fetched:
        """``url`` read, or a `urls.WebRefusal` saying why not. A user name and password in
        ``url`` are sent as basic authentication."""
        started = time.monotonic()
        upgraded = False
        current = url
        if urls.normalize(url) is None:
            raise urls.WebRefusal("bad_url", "That is not a web address web_fetch can read.")
        # Keep a plain http address for a private host (a server on this machine or its
        # network) or one the person allowed; any other is upgraded.
        host = urls.host_of(url)
        if url.lower().startswith("http://") and not (
            self.guard.allows_private(host, urls.port_of(url)) or self.guard.is_private(host)
        ):
            upgraded = True
            current = "https://" + url[len("http://"):]
        for _ in range(MAX_REDIRECTS + 1):
            if cancel.cancelled:
                raise urls.WebRefusal("cancelled", "Stopped by the person.")
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                raise self._timeout(deadline)
            status, headers, body = self._once(
                current, accept, max_bytes, remaining, cancel, upgraded
            )
            if status not in _REDIRECTS or "location" not in headers:
                return Fetched(status, headers, body, current)
            target = urljoin(current, headers["location"])
            normal = urls.normalize(target)
            if normal is None:
                raise urls.WebRefusal(
                    "bad_url", "The page redirects to something that is not a web address."
                )
            if not urls.same_site(urls.host_of(normal), urls.host_of(current)):
                return Fetched(status, headers, b"", current, redirect=target)
            current = target
        raise urls.WebRefusal(
            "too_many_redirects",
            f"The page redirects more than {MAX_REDIRECTS} times. Try another result.",
        )

    def post_json(
        self,
        url: str,
        payload: dict,
        *,
        cancel: CancelToken,
        headers: tuple[tuple[str, str], ...] = (),
        max_bytes: int = MAX_BYTES,
        deadline: float = DEADLINE_SECONDS,
    ) -> Fetched:
        """A JSON ``payload`` posted to ``url``, for a search API that takes one. A redirect
        is returned, never followed: a POST is not sent on to another address."""
        if urls.normalize(url) is None or not url.lower().startswith("https://"):
            raise urls.WebRefusal("bad_url", "That is not an https address.")
        if cancel.cancelled:
            raise urls.WebRefusal("cancelled", "Stopped by the person.")
        body = json.dumps(payload).encode("utf-8")
        status, response_headers, content = self._once(
            url, "application/json", max_bytes, deadline, cancel, False,
            method="POST", body=body,
            extra=(("Content-Type", "application/json"), *headers),
        )
        return Fetched(status, response_headers, content, url)

    # -- one request -----------------------------------------------------------------------

    @staticmethod
    def _timeout(deadline: float) -> urls.WebRefusal:
        return urls.WebRefusal(
            "timeout", f"The site did not answer in {deadline:.0f} s. Try another result."
        )

    def _once(
        self,
        url: str,
        accept: str,
        max_bytes: int,
        remaining: float,
        cancel: CancelToken,
        upgraded: bool,
        *,
        method: str = "GET",
        body: bytes | None = None,
        extra: tuple[tuple[str, str], ...] = (),
    ) -> tuple[int, httpx.Headers, bytes]:
        inner = self._transport or httpx.HTTPTransport(
            verify=True, limits=httpx.Limits(max_keepalive_connections=0)
        )
        timeout = httpx.Timeout(
            connect=min(CONNECT_SECONDS, remaining), read=min(15.0, remaining),
            write=10.0, pool=5.0,
        )
        client = httpx.Client(
            transport=_Pinned(inner, self.guard),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": user_agent(), "Accept": accept, **dict(extra)},
        )
        # Whatever a site sets, none of it is kept or sent back.
        client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        started = time.monotonic()
        try:
            with client.stream(method, url, content=body) as response:
                if response.status_code in _REDIRECTS:
                    return response.status_code, response.headers, b""
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise self._too_large(max_bytes)
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    if cancel.cancelled:
                        raise urls.WebRefusal("cancelled", "Stopped by the person.")
                    if time.monotonic() - started > remaining:
                        raise self._timeout(remaining)
                    size += len(chunk)
                    if size > max_bytes:
                        raise self._too_large(max_bytes)
                    chunks.append(chunk)
                return response.status_code, response.headers, b"".join(chunks)
        except httpx.TimeoutException as exc:
            raise self._timeout(remaining) from exc
        except httpx.ConnectError as exc:
            raise self._connect_failure(exc, upgraded) from exc
        except ssl.SSLError as exc:
            raise self._tls() from exc
        except httpx.HTTPError as exc:
            raise urls.WebRefusal(
                "unreachable", f"The request failed ({type(exc).__name__}). Try another result."
            ) from exc
        finally:
            client.close()
            if self._transport is None:
                inner.close()

    @staticmethod
    def _too_large(limit: int) -> urls.WebRefusal:
        return urls.WebRefusal(
            "too_large",
            f"The page is over {limit // 1_000_000} MB. Look for a smaller page (an API "
            "reference section, the raw file).",
        )

    @staticmethod
    def _tls() -> urls.WebRefusal:
        return urls.WebRefusal(
            "tls_error", "The site's certificate is not valid. web_fetch does not read it."
        )

    def _connect_failure(self, exc: httpx.ConnectError, upgraded: bool) -> urls.WebRefusal:
        text = str(exc)
        if "CERTIFICATE" in text.upper() or "SSL" in text.upper() or isinstance(
            exc.__cause__, ssl.SSLError
        ):
            return self._tls()
        note = (
            " (http was upgraded to https, and the site does not answer there)" if upgraded else ""
        )
        return urls.WebRefusal(
            "unreachable", f"The site did not accept a connection{note}. Try another result."
        )
