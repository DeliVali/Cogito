"""The search sources: for each one the request it takes and the parser of its answer.

The search asks the open web only (2026-10-31): SearXNG, the person's own instance when
`[web] searxng_url` is set or the one the app runs; then, while too little came back, the
keyless search APIs of Tavily and Firecrawl; and DuckDuckGo's HTML page last, a page built
for people that the chain reads paced and stops on the first block (`chain.py`). The
developer APIs it once asked first (Stack Exchange, npm, crates.io, PyPI, GitHub, MDN) and
Wikipedia are gone: their searches match loosely, and their unrelated results met the
three-result threshold before the open web was asked.

Stack Exchange's API is still used to read a question the search found (`web_fetch`): the
site blocks automated readers, so its answers come from the API.

A parser takes the response body as text and returns `Hit`s. Anything it cannot read is a
`SourceError` the chain turns into a reason ("offline", "rate-limited", "blocked",
"failed"); an answer with no results is an empty list, not an error.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from cogito.tools.web import urls

#: Names the model sees in "(search: ...)".
LABELS = {
    "ddg": "DuckDuckGo",
    "searxng": "SearXNG",
    "tavily": "Tavily",
    "firecrawl": "Firecrawl",
    "saved": "Saved locally (previous results, not refreshed)",
}


@dataclass(frozen=True, slots=True)
class Hit:
    title: str
    url: str
    snippet: str
    source: str
    #: Attribution a source's licence asks to be kept (Stack Exchange: CC BY-SA, the author).
    credit: str = ""
    observed_at: str = ""


class SourceError(Exception):
    """A source answered in a way that gives no results. ``kind`` is ``rate_limited``,
    ``blocked`` or ``failed``."""

    def __init__(self, kind: str, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


def clean(text: Any, limit: int) -> str:
    """Text from the web as one line the model can read: entities unescaped, control and
    bidirectional-control characters dropped (they reorder or hide what a person sees),
    whitespace folded, cut at ``limit``."""
    if not isinstance(text, str):
        return ""
    text = html.unescape(text)
    text = "".join(
        ch if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf") else " " for ch in text
    )
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SourceError("failed", "the answer was not JSON") from exc


def _list(document: Any, *path: str) -> list[Any]:
    """``document`` followed down ``path``, as a list; [] when a step is missing. Wrong
    types are a failed source."""
    node: Any = document
    for step in path:
        if not isinstance(node, dict):
            raise SourceError("failed", "the answer had an unexpected shape")
        node = node.get(step)
        if node is None:
            return []
    if not isinstance(node, list):
        raise SourceError("failed", "the answer had an unexpected shape")
    return node


def _encode(parameters: list[tuple[str, Any]]) -> str:
    return urlencode(parameters)


# -- Stack Exchange: reading a question the search found ----------------------------------

#: Stack Exchange sites by domain, for a question's address.
_SE_SITES = {"stackoverflow.com": "stackoverflow", "askubuntu.com": "askubuntu",
             "superuser.com": "superuser", "serverfault.com": "serverfault"}


def stackexchange_site(domain: str) -> str | None:
    """The Stack Exchange ``site`` parameter for a domain, or None when it is not one."""
    if domain in _SE_SITES:
        return _SE_SITES[domain]
    if domain.endswith(".stackexchange.com"):
        return domain[: -len(".stackexchange.com")]
    return None


#: A question's address on a Stack Exchange site: its domain and number.
_SE_QUESTION = re.compile(r"^https?://([^/]+)/questions/(\d+)(?:[/?#]|$)", re.IGNORECASE)


def stackexchange_question(url: str) -> tuple[str, int] | None:
    """The API ``site`` and question number for a Stack Exchange question's address, or
    None when it is not one."""
    found = _SE_QUESTION.match(url.strip())
    if found is None:
        return None
    site = stackexchange_site(found.group(1).lower().removeprefix("www."))
    return (site, int(found.group(2))) if site else None


def url_stackexchange_answers(site: str, question: int, *, count: int = 3) -> str:
    """A question's answers with their text, the most voted first."""
    return f"https://api.stackexchange.com/2.3/questions/{question}/answers?" + _encode(
        [("order", "desc"), ("sort", "votes"), ("site", site), ("filter", "withbody"),
         ("pagesize", count)]
    )


def parse_stackexchange_answers(text: str) -> list[tuple[bool, int, str]]:
    """Each answer as (accepted, score, body HTML), the accepted one first, then by votes.
    Raises `SourceError` when the API answers with an error."""
    document = _json(text)
    if isinstance(document, dict) and "error_id" in document:
        raise SourceError("failed", str(document.get("error_message", "")))
    answers = [
        (bool(item.get("is_accepted")), int(item.get("score") or 0), str(item["body"]))
        for item in _list(document, "items")
        if isinstance(item, dict) and isinstance(item.get("body"), str)
    ]
    answers.sort(key=lambda answer: (not answer[0], -answer[1]))
    return answers


# -- DuckDuckGo ----------------------------------------------------------------------------


def url_ddg(query: str) -> str:
    return "https://html.duckduckgo.com/html/?" + _encode([("q", query)])


class _DuckResults(HTMLParser):
    """The result list of DuckDuckGo's HTML page: an ``a.result__a`` for the title and
    address, an element with ``result__snippet`` for the text. Advertisements
    (``result--ad``) are skipped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._ad = False
        self._capture: str | None = None
        self._depth = 0
        self._text: list[str] = []
        self.text_seen = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {name: value or "" for name, value in attrs}
        classes = found.get("class", "").split()
        if tag == "div" and "result" in classes and "results_links" in classes:
            self._current = {"title": "", "url": "", "snippet": ""}
            self._ad = "result--ad" in classes
            self.results.append(self._current)
            return
        if self._capture is not None:
            if tag == self._capture_tag:
                self._depth += 1
            return
        if self._current is None:
            return
        if tag == "a" and "result__a" in classes:
            self._current["url"] = found.get("href", "")
            self._start("title", tag)
        elif "result__snippet" in classes:
            self._start("snippet", tag)

    _capture_tag = ""

    def _start(self, field: str, tag: str) -> None:
        self._capture = field
        self._capture_tag = tag
        self._depth = 1
        self._text = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None or tag != self._capture_tag:
            return
        self._depth -= 1
        if self._depth == 0 and self._current is not None:
            self._current[self._capture] = "".join(self._text)
            self._capture = None

    def handle_data(self, data: str) -> None:
        self.text_seen += data
        if self._capture is not None:
            self._text.append(data)


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo links results through ``//duckduckgo.com/l/?uddg=<address>``; the address
    is the real one."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        return target
    return href


def parse_ddg(text: str) -> list[Hit]:
    parser = _DuckResults()
    parser.feed(text)
    parser.close()
    hits: list[Hit] = []
    for result in parser.results:
        title = clean(result["title"], 100)
        address = _unwrap_ddg(result["url"])
        if not title or urls.normalize(address) is None:
            continue
        if "duckduckgo.com" in urls.host_of(address):
            continue  # an advertisement or an internal link, not a page
        hits.append(Hit(title, address, clean(result["snippet"], 200), "ddg"))
    if not hits and re.search(r"anomaly|bots use DuckDuckGo|unusual traffic", text, re.IGNORECASE):
        raise SourceError("blocked", "DuckDuckGo asked for a challenge")
    return hits


# -- SearXNG -------------------------------------------------------------------------------


def url_searxng(base: str, query: str, engines: tuple[str, ...] = ()) -> str:
    parameters: list[tuple[str, Any]] = [("q", query), ("format", "json")]
    if engines:
        parameters.append(("engines", ",".join(engines)))
    return base.rstrip("/") + "/search?" + _encode(parameters)


def searxng_unresponsive(text: str) -> list[tuple[str, str]]:
    """The engines SearXNG could not get an answer from, with its reason ("CAPTCHA",
    "Suspended: too many requests", "timeout", ...)."""
    try:
        items = _list(_json(text), "unresponsive_engines")
    except SourceError:
        return []
    found: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item):
            found.append((item[0], item[1]))
    return found


def parse_searxng(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for item in _list(_json(text), "results"):
        if not isinstance(item, dict) or not item.get("url") or not item.get("title"):
            continue
        snippet = clean(item.get("content"), 200)
        hits.append(Hit(clean(item["title"], 100), str(item["url"]), snippet, "searxng"))
    return hits


# -- Keyless search APIs: Tavily and Firecrawl ---------------------------------------------
#
# Both answer a POST without an account or key, free and rate-limited per IP; neither
# publishes the numbers (docs.tavily.com/documentation/keyless, read 2026-10-31: the
# `X-Tavily-Access-Mode: keyless` header and no Authorization; firecrawl.dev/blog/
# firecrawl-keyless-launch, 2026-06-16: "free and capped per IP address each day"). Firecrawl
# refuses an address it takes for a VPN or proxy with 403, as it did from the author's
# machine on 2026-10-31.

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_HEADERS = (("X-Tavily-Access-Mode", "keyless"),)
FIRECRAWL_URL = "https://api.firecrawl.dev/v2/search"


def tavily_payload(query: str) -> dict[str, Any]:
    return {"query": query, "max_results": 8}


def parse_tavily(text: str) -> list[Hit]:
    document = _json(text)
    if isinstance(document, dict) and "results" not in document:
        # A limit is answered in words for an agent, not with results (their keyless page).
        raise SourceError("rate_limited", "no results list in the answer")
    hits: list[Hit] = []
    for item in _list(document, "results"):
        if not isinstance(item, dict) or not item.get("url") or not item.get("title"):
            continue
        snippet = clean(item.get("content"), 200)
        hits.append(Hit(clean(item["title"], 100), str(item["url"]), snippet, "tavily"))
    return hits


def firecrawl_payload(query: str) -> dict[str, Any]:
    return {"query": query, "limit": 8}


def parse_firecrawl(text: str) -> list[Hit]:
    document = _json(text)
    if isinstance(document, dict) and document.get("success") is False:
        raise SourceError("blocked", clean(document.get("error"), 120))
    hits: list[Hit] = []
    for item in _list(document, "data", "web"):
        if not isinstance(item, dict) or not item.get("url") or not item.get("title"):
            continue
        snippet = clean(item.get("description"), 200)
        hits.append(Hit(clean(item["title"], 100), str(item["url"]), snippet, "firecrawl"))
    return hits
