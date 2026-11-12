"""What the two web tools share inside one session.

`cli.py` builds one `WebState` per session and hands it to both tools, like the delegate
tool's engine factory, so neither tool imports the other or the loop. It holds the addresses
the conversation has seen, the 15-minute page cache, the sites that said "slow down", the
robots.txt answers, and where a long page is offered to the documents index.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib import robotparser

from cogito.cancel import CancelToken
from cogito.config.settings import WebSettings
from cogito.context.offload import ArtifactRef
from cogito.tools.web import http, urls

if TYPE_CHECKING:
    from cogito.tools.web.chain import SearchCache

#: How long a read page is served again without asking the site (Claude Code's WebFetch
#: uses 15 minutes too: code.claude.com/docs/en/tools-reference, read 2026-10-12).
CACHE_SECONDS = 15 * 60
#: How long a site that answered 429 is left alone.
COOLDOWN_SECONDS = 5 * 60
#: The product token robots.txt rules are matched against.
ROBOTS_AGENT = "Cogito-User"
_ROBOTS_BYTES = 500_000
_ROBOTS_SECONDS = 5.0


class DocumentSink(Protocol):
    """Where a page that did not fit the window goes, so `ask_document` can answer from it.
    `cli.py` provides one only when the documents index exists."""

    def offer(self, document: Any) -> bool: ...

    def answer(self, document: Any, question: str, budget: int) -> str | None:
        """What the index says about ``question`` for an offered ``document``, with the lines
        to read, or None when it cannot answer now (still ingesting, off, or unsure). Used
        only when the state retrieves (below); it never waits."""
        ...


@dataclass(frozen=True, slots=True)
class Page:
    """A page read, as text."""

    url: str
    title: str
    text: str
    status: int
    #: "HTML", "Markdown", "text", "JSON", "PDF", ...
    kind: str
    #: The site's own Last-Modified, as epoch seconds.
    modified: float | None = None


@dataclass(slots=True)
class Cached:
    page: Page
    stored_at: float
    artifact: ArtifactRef | None = None


@dataclass(slots=True)
class Robots:
    """robots.txt per site, asked once a session. A site with no robots.txt, or one that
    cannot be read, allows the read: this is one person reading one page, not a crawl, and a
    transient error must not block it (RFC 9309 would disallow on a server error; this
    leans the other way on purpose)."""

    client: http.WebClient
    enabled: bool = True
    _rules: dict[str, robotparser.RobotFileParser | None] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allowed(self, url: str, cancel: CancelToken) -> bool:
        if not self.enabled:
            return True
        normal = urls.normalize(url)
        if normal is None:
            return True
        host = urls.host_of(normal)
        origin = f"{host}:{urls.port_of(url)}"
        with self._lock:
            known = origin in self._rules
            rules = self._rules.get(origin)
        if not known:
            rules = self._load(url, cancel)
            with self._lock:
                self._rules[origin] = rules
        return rules is None or rules.can_fetch(ROBOTS_AGENT, url)

    def _load(self, url: str, cancel: CancelToken) -> robotparser.RobotFileParser | None:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        address = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            got = self.client.get(
                address, cancel=cancel, accept="text/plain", max_bytes=_ROBOTS_BYTES,
                deadline=_ROBOTS_SECONDS,
            )
        except urls.WebRefusal:
            return None
        if got.status != 200 or got.redirect is not None:
            return None
        parser = robotparser.RobotFileParser()
        parser.parse(http.decode_body(got.body, got.headers.get("content-type", "")).splitlines())
        return parser


#: What the model is told when the web level is off (`WebState.allowed`).
WEB_OFF = (
    "The web is turned off in Búsqueda, so nothing was searched or read. Answer without "
    "the web, and say the person can turn it back on there if they want it."
)


class WebState:
    def __init__(
        self,
        settings: WebSettings,
        *,
        client: http.WebClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        documents: DocumentSink | None = None,
        retrieve: bool = False,
        local_search: Callable[[], str | None] | None = None,
        local_engines: tuple[str, ...] = (),
        search_cache: SearchCache | None = None,
        allowed: Callable[[], bool] | None = None,
        saved_search: Callable | None = None,
        save_results: Callable | None = None,
    ) -> None:
        self.settings = settings
        self.saved_search = saved_search
        self.save_results = save_results
        #: Whether the web may be reached now, read on every call (the app's: the Búsqueda
        #: tab's web level). Read once, when a session was built, a conversation already open
        #: went on searching after the person turned it off (QA rig, 2026-10-16). `chat`
        #: passes none: always allowed, as before.
        self.allowed: Callable[[], bool] = allowed or (lambda: True)
        #: The address of the SearXNG the app runs on this machine, or None while there is
        #: none. Only the app passes it; `chat` never does.
        self.local_search = local_search
        #: That SearXNG's engines, which the search chain rotates through.
        self.local_engines = local_engines
        #: Recent search results to reuse (`chain.SearchCache`). Only the app passes one; `chat`
        #: and the benchmark ask the sources every time.
        self.search_cache = search_cache
        #: The last query `web_search` sent (scrubbed), for `web_fetch`'s line matching.
        self.last_query = ""
        self.client = client or http.WebClient(settings)
        self.clock = clock
        self.fetchable = urls.FetchableSet()
        self.documents = documents
        #: True only in the app, with a documents index: a page is indexed whole and
        #: `web_fetch` returns the part asked for, not the head of the page. False (`chat`,
        #: the benchmark) keeps the head-first result under `fetch_budget`.
        self.retrieve = retrieve and documents is not None
        self.robots = Robots(self.client, enabled=settings.robots)
        self._cache: dict[str, Cached] = {}
        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- what the conversation holds -------------------------------------------------------

    def note_person(self, text: str) -> None:
        self.fetchable.add_text(text, urls.PERSON)

    def note_file(self, text: str) -> None:
        self.fetchable.add_text(text, urls.FILE)

    # -- the cache -------------------------------------------------------------------------

    def cached(self, url: str) -> Cached | None:
        with self._lock:
            entry = self._cache.get(url)
            if entry is not None and self.clock() - entry.stored_at > CACHE_SECONDS:
                del self._cache[url]
                return None
            return entry

    def remember(self, *keys: str, page: Page, artifact: ArtifactRef | None) -> None:
        stored = Cached(page, self.clock(), artifact)
        with self._lock:
            for key in keys:
                self._cache[key] = stored

    # -- sites that asked for peace --------------------------------------------------------

    def cool_down(self, host: str) -> None:
        with self._lock:
            self._cooldown[host] = self.clock() + COOLDOWN_SECONDS

    def cooling(self, host: str) -> bool:
        with self._lock:
            return self._cooldown.get(host, 0.0) > self.clock()
