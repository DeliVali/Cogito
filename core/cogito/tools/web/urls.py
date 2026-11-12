"""The web tools' URL rules: what may be read, and from where.

These are `web_fetch`'s own input validation, like the edit tool's elision guard, and not a
guard rule (`guard/policy.py` keeps its two). They are structural: no wording in a page or in
a prompt can talk the model past them (design 5.2).

- GET only, and never a host on the person's `blocked_hosts`, or outside `only_hosts` when
  that list is set (`Guard`; the address checked is the one connected to);
- any address, whether or not the conversation holds it, private or local ones and ones
  that carry a user name, password or token included (decided by the person on 2026-11-01:
  "no bloquees eso ... para cualquier dirección", then "quita esos bloqueos"). Until then
  only public addresses the conversation held were read, a link inside a fetched page only
  on that page's own host, as Anthropic's `web_fetch` does since Simon Willison showed links
  leaking a user's details through which ones the model followed (2026-07-15,
  https://simonwillison.net/2026/Jul/15/claude-web-fetch-exfiltration/), and no address
  carrying credentials. Those defences are given up: a model a page has misled can now put
  what it read into an address it writes, or read a service on this machine or its network.

This module is plain standard library, so the loop can import it for the window's refusal
row without pulling in an HTTP client.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

#: Where an address came from. The first four are the conversation's own; `PAGE` is a link
#: inside a fetched page. None of them decides a read any more (2026-11-01).
PERSON = "person"
FILE = "file"
SEARCH = "search"
REDIRECT = "redirect"
PAGE = "page"

#: The kinds of error that mean the harness refused the address, not that the site failed.
#: The window draws those as «no permitido» (`loop.engine.is_refusal`). `url_not_seen`,
#: `link_from_page`, `not_public`, `credentials_in_url` and `robots_disallowed` are no longer
#: given unless the person opts back into robots.txt (2026-11-01); older records carry them.
REFUSALS = frozenset(
    {
        "url_not_seen",
        "link_from_page",
        "not_public",
        "bad_url",
        "credentials_in_url",
        "robots_disallowed",
        "blocked_host",
    }
)

#: An address longer than this is refused. Anthropic's `web_fetch` stops at 250 characters
#: (platform.claude.com web-fetch docs, read 2026-10-12); real documentation addresses with
#: long queries need more, and 2,000 is what browsers and servers commonly accept.
MAX_URL_CHARS = 2000

_SCHEMED = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SCHEMELESS = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]|$)", re.IGNORECASE)
_IN_PROSE = re.compile(r"https?://[^\s<>\"'`\]]+", re.IGNORECASE)
_NUMBER = re.compile(r"^(?:0x[0-9a-f]+|0[0-7]*|[1-9][0-9]*)$", re.IGNORECASE)


class WebRefusal(Exception):
    """An address the harness will not read, with what to tell the model."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# -- addresses as text ---------------------------------------------------------------------


def _strip_trailing(text: str) -> str:
    """Punctuation that belongs to the sentence around an address, not to the address. A
    closing bracket stays when the address opened one (a Wikipedia title)."""
    opens = {")": "(", "]": "[", "}": "{", ">": "<"}
    while text:
        last = text[-1]
        unbalanced = last in opens and text.count(opens[last]) < text.count(last)
        if last in ".,;:!?'\"*" or unbalanced:
            text = text[:-1]
        else:
            break
    return text


def normalize(raw: str) -> str | None:
    """``raw`` in the one spelling addresses are compared by, or None when it is not a web
    address. Always https (the tool upgrades http), lower-case host in punycode, default port
    and fragment dropped, user information dropped (`request_form` keeps it to send), an
    empty path as ``/``. The query string is kept exactly."""
    text = _strip_trailing(raw.strip())
    if not text or re.search(r"[\s\x00-\x1f\x7f]", text):
        return None
    if _SCHEMED.match(text):
        pass
    elif _SCHEMELESS.match(text):
        text = "https://" + text
    else:
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if ":" in host:
        host = f"[{host}]"
    default = 80 if parts.scheme.lower() == "http" else 443
    netloc = host if port in (None, default) else f"{host}:{port}"
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{netloc}{parts.path or '/'}{query}"


def request_form(raw: str) -> str | None:
    """``raw`` as the address to request: its own scheme (http stays http here; the client
    upgrades it unless the host is private or the person allowed it), no fragment, its user
    information kept so it is sent. None when it is not a web address."""
    if normalize(raw) is None:
        return None
    text = _strip_trailing(raw.strip())
    if not _SCHEMED.match(text):
        text = "https://" + text
    parts = urlsplit(text)
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def find_urls(text: str) -> list[str]:
    """Every web address in ``text``, normalised, each once, in order."""
    found: dict[str, None] = {}
    for match in _IN_PROSE.finditer(text):
        if (url := normalize(match.group(0))) is not None:
            found.setdefault(url, None)
    return list(found)


def host_of(url: str) -> str:
    """The host of an address, lower-case, without brackets or port ('' when none)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def port_of(url: str) -> int:
    try:
        port = urlsplit(url).port
    except ValueError:
        port = None
    if port is not None:
        return port
    return 80 if url.lower().startswith("http://") else 443






def same_site(first: str, second: str) -> bool:
    """The same host, a leading ``www.`` aside (example.com and www.example.com)."""

    def bare(host: str) -> str:
        host = host.lower().rstrip(".")
        return host[4:] if host.startswith("www.") else host

    return bare(first) == bare(second)


# -- addresses as numbers ------------------------------------------------------------------


def literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as an IP address when it is one in any spelling a client would accept:
    dotted, a single decimal, octal or hex number, the short forms (``127.1``), IPv6 with a
    zone, IPv4-mapped IPv6. None for a hostname."""
    text = host.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    parts = text.split(".")
    if not 1 <= len(parts) <= 4 or not all(_NUMBER.match(part) for part in parts):
        return None
    def number(part: str) -> int:
        if part.lower().startswith("0x"):
            return int(part, 16)
        return int(part, 8) if len(part) > 1 and part.startswith("0") else int(part)

    try:
        values = [number(part) for part in parts]
    except ValueError:
        return None
    *head, last = values
    if any(value > 255 for value in head) or last >= 256 ** (5 - len(values)):
        return None
    number = last
    for index, value in enumerate(head):
        number += value << (8 * (3 - index))
    return ipaddress.IPv4Address(number)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why ``ip`` may not be read (loopback, private, link-local, cloud metadata, CGNAT,
    multicast, unspecified, reserved), or None for a public address. An IPv6 address that
    carries an IPv4 one (mapped, 6to4, NAT64) is judged by the IPv4 address inside."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip in _NAT64:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        for inner in embedded:
            if (reason := blocked_reason(inner)) is not None:
                return reason
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return "not a public address"
    return None


def _system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


Resolver = Callable[[str], list[str]]


def _listed(host: str, port: int, entries: Iterable[str]) -> bool:
    """Whether ``host`` (on ``port``) is one of ``entries``: ``host``, ``host:port`` or a
    parent domain. Entries are the person's own configuration."""
    for entry in entries:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == f"{host}:{port}":
            return True
        name, _, entry_port = entry.partition(":")
        if entry_port:
            continue
        if host == name or host.endswith("." + name):
            return True
    return False


class Guard:
    """Decides which addresses a host may be connected to, and gives back the ones checked.

    The caller connects to what `addresses` returned, not to the name, so a DNS answer
    that changes between the check and the connection (rebinding) is never used.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allow_hosts: Iterable[str] = (),
        only_hosts: Iterable[str] = (),
        blocked_hosts: Iterable[str] = (),
    ) -> None:
        self._resolve = resolver or _system_resolver
        self._allow = tuple(allow_hosts)
        self._only = tuple(only_hosts)
        self._blocked = tuple(blocked_hosts)
        self._trusted: set[str] = set()

    def trust(self, host: str, port: int) -> None:
        """Let this one ``host:port`` be private, as the person's ``allow_hosts`` would. For the
        local SearXNG the app runs itself: its port is random and no other private address
        is opened by it."""
        self._trusted.add(f"{host.lower().rstrip('.')}:{port}")

    def allows_private(self, host: str, port: int = 443) -> bool:
        """Whether the person listed this host in ``allow_hosts``."""
        host = host.lower().rstrip(".")
        return f"{host}:{port}" in self._trusted or _listed(host, port, self._allow)

    def permits_host(self, host: str, port: int = 443) -> bool:
        """Whether the person's lists let this host be used at all (not its addresses)."""
        host = host.lower().rstrip(".")
        if _listed(host, port, self._blocked):
            return False
        return not self._only or _listed(host, port, self._only)

    def addresses(self, host: str, port: int) -> list[str]:
        host = host.strip("[]").lower().rstrip(".")
        if _listed(host, port, self._blocked):
            raise WebRefusal(
                "blocked_host", f"{host} is on the list of sites web_fetch never reads."
            )
        if self._only and not _listed(host, port, self._only):
            raise WebRefusal(
                "not_public",
                f"web_fetch is limited to {', '.join(self._only)} here; {host} is not one of them.",
            )
        literal = literal_ip(host)
        if literal is not None:
            found = [str(literal)]
        else:
            try:
                found = self._resolve(host)
            except OSError as exc:
                raise WebRefusal(
                    "unreachable", f"{host} could not be found ({exc}). Try another result."
                ) from exc
            if not found:
                raise WebRefusal("unreachable", f"{host} could not be found. Try another result.")
        for address in found:
            try:
                ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError as exc:
                raise WebRefusal("unreachable", f"{host} answered with {address!r}.") from exc
        return found

    def is_private(self, host: str) -> bool:
        """Whether ``host`` is, or resolves to, a private, loopback or link-local address:
        such a server rarely speaks https, so a plain http address to it is kept as written.
        False when it cannot be resolved."""
        host = host.strip("[]").lower().rstrip(".")
        literal = literal_ip(host)
        try:
            found = [str(literal)] if literal is not None else self._resolve(host)
        except OSError:
            return False
        for address in found:
            try:
                ip = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError:
                continue
            if blocked_reason(ip) is not None:
                return True
        return False


# -- what the conversation holds -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    #: "" when ok; else one of `REFUSALS`.
    kind: str = ""
    message: str = ""
    #: The normalised address, when there is one.
    url: str = ""


_MESSAGES = {
    "bad_url": (
        "That is not a web address web_fetch can read (file:, data: and the rest are refused, "
        "and so is one over {limit} characters). Pass a full address such as "
        "https://docs.python.org/3/."
    ),
}




@dataclass(slots=True)
class _Entry:
    origins: set[str] = field(default_factory=set)


class FetchableSet:
    """Every address this conversation holds, and where each came from."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def any(self) -> bool:
        """Whether web_fetch has anything it could read now."""
        with self._lock:
            return bool(self._entries)

    def add(self, found: Iterable[str], origin: str) -> None:
        for raw in found:
            url = normalize(raw)
            if url is None:
                continue
            with self._lock:
                entry = self._entries.setdefault(url, _Entry())
                entry.origins.add(origin)

    def add_text(self, text: str, origin: str) -> None:
        self.add(find_urls(text), origin)

    def verdict(self, raw: str) -> Verdict:
        if len(raw) > MAX_URL_CHARS:
            return Verdict(False, "bad_url", _MESSAGES["bad_url"].format(limit=MAX_URL_CHARS))
        url = normalize(raw)
        if url is None:
            return Verdict(False, "bad_url", _MESSAGES["bad_url"].format(limit=MAX_URL_CHARS))
        return Verdict(True, url=url)
