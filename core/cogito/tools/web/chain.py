"""The search chain: which sources one query goes to, in what order, and how the answers merge.

Only the open web is asked (decided by the person on 2026-10-31: "puedes dejar la web
abierta solamente"). The developer APIs it asked first until then (Stack Exchange, npm, crates.io,
PyPI, GitHub, MDN) and Wikipedia match loosely: MDN answered every query with its own pages
and npm with unrelated packages, and those results met the three-result threshold before the
open web was asked.

1. The person's SearXNG instance if `[web] searxng_url` is set (it is asked every time);
   in the app, else the SearXNG it runs itself (`setup/searxng.py`), four of its engines a
   search, the ones asked longest ago, and four more when those brought too little; an
   engine it reports blocked rests (`Budgets.strike`), and none answering is a block, not
   "no results".
2. Only while fewer than three results are in, one at a time: Tavily's keyless search, then
   Firecrawl's (no account or key; free, rate-limited per IP, numbers unpublished; see
   `sources.py`). A refusal rests that source like any other.
3. Only while fewer than three results are in: DuckDuckGo's HTML page. It is built for
   people and its terms neither grant nor forbid programs (the person decided to include it,
   2026-10-12): one request a call, at most one every 2 seconds and 30 a session, an honest
   User-Agent, and the first block (HTTP 202, 403, 429 or a challenge page) ends it for the
   session.
4. Reciprocal rank fusion over the sources that answered (Cormack et al., SIGIR 2009, k=60).

`site` is asked as `site:` and every result off that site is dropped. A source's limits
live in a small file in the user's cache directory, so a restart does not reset them
(`Budgets`). A source out of budget is skipped without a word; only when nothing answered at
all does the model hear about it (`search_unavailable`).
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from cogito.cancel import CancelToken
from cogito.config import paths
from cogito.config.settings import WebSettings
from cogito.graph.recall import terms
from cogito.tools.web import http, sources, urls
from cogito.tools.web.sources import Hit, SourceError

#: Most results one search returns.
MAX_RESULTS = 8
#: Fewer results than this from SearXNG sends the query on to DuckDuckGo.
ENOUGH = 3
#: A SearXNG instance the person set is given this long to answer.
SEARXNG_SECONDS = 4.0
DDG_SECONDS = 10.0
#: Tavily's and Firecrawl's keyless search answered in 1-3 s from the author's machine.
KEYLESS_SECONDS = 8.0
DDG_PER_SESSION = 30
#: The local SearXNG waits for its engines (it gives them 5 s at most).
LOCAL_SECONDS = 8.0
#: How long a source that said "slow down" without saying for how long is left alone.
DEFAULT_REST_SECONDS = 600.0
#: The longest rest a source that keeps refusing is given.
MAX_REST_SECONDS = 86_400.0
#: The engines of the local SearXNG one search asks at once, and how many groups of them
#: before giving up on it. 4 of a 13-engine pool keeps each engine near a third of the
#: searches; a second answering group is asked only when the first brought too little.
#: Groups whose engines all report a block are skipped within the same finite pool.
LOCAL_ENGINES_PER_SEARCH = 4
LOCAL_ROUNDS = 2
#: An engine SearXNG reports blocked rests as long as SearXNG's own suspension for that
#: error (`search.suspended_times` in its settings.yml at the pinned commit): 180 s for
#: "too many requests" and "access denied", 3600 s for a CAPTCHA. A timeout or a crash is
#: no block; the engine is simply asked again later.
_ENGINE_REST = (("captcha", 3600.0), ("too many requests", 180.0), ("access denied", 180.0))
MAX_QUERY_CHARS = 400
_RRF_K = 60

# -- what leaves the machine ---------------------------------------------------------------


class Scrubber:
    """Takes the person out of a query before it leaves the machine: their home folder, their
    user name and the machine's name. It cannot take out code the model pastes into a query;
    the design says so (5.3)."""

    def __init__(
        self, home: str | None = None, user: str | None = None, host: str | None = None
    ) -> None:
        self._home = str(Path.home()) if home is None else home
        if user is None:
            try:
                user = getpass.getuser()
            except (KeyError, OSError):
                user = ""
        self._names = [
            name for name in (user, socket.gethostname() if host is None else host)
            if name and len(name) >= 3
        ]

    def __call__(self, query: str) -> str:
        text = query
        if self._home and len(self._home) > 1:
            text = text.replace(self._home, "~")
            text = text.replace(self._home.replace("/", "\\"), "~")
        for name in self._names:
            text = re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())


# -- limits that outlive a session ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limit:
    per_day: int = 0
    per_minute: int = 0
    interval: float = 0.0


#: DuckDuckGo gets one request every two seconds, from this tool's own rule.
LIMITS = {
    "ddg": Limit(interval=2.0),
}


class Budgets:
    """How much each source has been asked, remembered in ``path`` (None keeps it in
    memory, for a test)."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        if path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._state = {k: v for k, v in loaded.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self._state = {}

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _entry(self, source: str) -> dict:
        entry = self._state.setdefault(source, {})
        if entry.get("day") != self._today():
            entry["day"], entry["count"] = self._today(), 0
        entry["stamps"] = [s for s in entry.get("stamps", []) if self._clock() - s < 60]
        return entry

    def denial(self, source: str) -> str | None:
        """Why this source may not be asked now ("rate-limited"), or None."""
        limit = LIMITS.get(source, Limit())
        with self._lock:
            entry = self._entry(source)
            if entry.get("blocked_until", 0) > self._clock():
                return "rate-limited"
            if limit.per_day and entry["count"] >= limit.per_day:
                return "rate-limited"
            if limit.per_minute and len(entry["stamps"]) >= limit.per_minute:
                return "rate-limited"
        return None

    def wait(self, source: str) -> float:
        """Seconds to wait before this source may be asked again (its minimum interval)."""
        limit = LIMITS.get(source, Limit())
        if not limit.interval:
            return 0.0
        with self._lock:
            last = self._entry(source).get("last", 0.0)
        return max(0.0, limit.interval - (self._clock() - last))

    def spend(self, source: str) -> None:
        with self._lock:
            entry = self._entry(source)
            entry["count"] += 1
            entry["stamps"].append(self._clock())
            entry["last"] = self._clock()
            self._save()

    def block(self, source: str, seconds: float) -> None:
        with self._lock:
            entry = self._entry(source)
            entry["blocked_until"] = max(entry.get("blocked_until", 0), self._clock() + seconds)
            self._save()

    def last(self, source: str) -> float:
        """When this source was last asked (0 when never)."""
        with self._lock:
            return float(self._entry(source).get("last", 0.0))

    def strike(self, source: str, seconds: float) -> None:
        """Block for ``seconds``, doubled for every block since the source last answered."""
        with self._lock:
            entry = self._entry(source)
            entry["strikes"] = entry.get("strikes", 0) + 1
            rest = min(MAX_REST_SECONDS, seconds * 2 ** (entry["strikes"] - 1))
            entry["blocked_until"] = max(entry.get("blocked_until", 0), self._clock() + rest)
            self._save()

    def clear(self, source: str) -> None:
        """The source answered: its next block starts from its base rest again."""
        with self._lock:
            if self._entry(source).pop("strikes", None) is not None:
                self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._state), encoding="utf-8")
            temporary.replace(self._path)
        except OSError:
            pass  # a limit that cannot be remembered is still kept for this session


#: How long a search's results are reused, and how many searches are kept. A choice, not a
#: measured value: long enough that an agent repeating a search in one task asks no source,
#: short enough that news is at most this old. The model is told the age of reused results.
CACHE_SECONDS = 6 * 3600.0
CACHE_ENTRIES = 300


class SearchCache:
    """Recent results by query and site, kept in ``path`` (None keeps them in memory). Only
    searches that found something are kept, so a moment when every source was blocked is
    never reused."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        if path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._entries = {k: v for k, v in loaded.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self._entries = {}

    @staticmethod
    def _key(query: str, site: str) -> str:
        return f"{site}\n{' '.join(query.lower().split())}"

    def get(self, query: str, site: str) -> tuple[list[Hit], list[str], float] | None:
        """The hits, the sources' labels and how many seconds ago they were found, or None."""
        with self._lock:
            entry = self._entries.get(self._key(query, site))
            if entry is None or self._clock() - entry.get("at", 0) > CACHE_SECONDS:
                return None
            try:
                hits = [Hit(**hit) for hit in entry["hits"]]
            except (KeyError, TypeError):
                return None
            # Hits from a source no longer asked (the developer APIs, MDN and Wikipedia,
            # removed 2026-10-31) are not reused.
            hits = [hit for hit in hits if hit.source in sources.LABELS]
            if not hits:
                return None
            return hits, list(entry.get("used", [])), self._clock() - float(entry["at"])

    def put(self, query: str, site: str, hits: list[Hit], used: list[str]) -> None:
        if not hits:
            return
        with self._lock:
            now = self._clock()
            self._entries = {
                k: v for k, v in self._entries.items() if now - v.get("at", 0) <= CACHE_SECONDS
            }
            self._entries[self._key(query, site)] = {
                "at": now, "used": used,
                "hits": [{"title": h.title, "url": h.url, "snippet": h.snippet,
                          "source": h.source, "credit": h.credit} for h in hits],
            }
            while len(self._entries) > CACHE_ENTRIES:
                oldest = min(self._entries, key=lambda k: self._entries[k].get("at", 0))
                del self._entries[oldest]
            self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self._entries, handle)
            temporary.replace(self._path)
        except OSError:
            pass  # results that cannot be kept are found again next time


def default_cache() -> SearchCache:
    return SearchCache(paths.web_results_file())


# -- merging ---------------------------------------------------------------------------------


def _key(url: str) -> str:
    normal = urls.normalize(url) or url
    return normal.rstrip("/") if normal.count("/") > 3 else normal


def fuse(lists: list[list[Hit]], limit: int = MAX_RESULTS, k: int = _RRF_K) -> list[Hit]:
    """Reciprocal rank fusion: each result scores 1/(k+rank) in every list that has it, and
    the same address from two sources is one result. Ties keep the order the lists came in."""
    scores: dict[str, float] = {}
    first: dict[str, tuple[int, Hit]] = {}
    for hits in lists:
        for rank, hit in enumerate(hits, start=1):
            key = _key(hit.url)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            first.setdefault(key, (len(first), hit))
    ordered = sorted(scores, key=lambda key: (-scores[key], first[key][0]))
    return [first[key][1] for key in ordered[:limit]]


# -- running -------------------------------------------------------------------------------------


@dataclass(slots=True)
class Attempt:
    source: str
    #: None when the source was not asked or did not answer.
    hits: list[Hit] | None
    #: Why not: offline, rate-limited, blocked, failed, or a session rule. "" when answered
    #: and "skipped" when the person's lists kept it out (never reported).
    reason: str = ""


@dataclass(slots=True)
class Outcome:
    hits: list[Hit]
    used: list[str]
    attempts: list[Attempt] = field(default_factory=list)
    #: How many seconds ago the reused results were found; None when asked now.
    cached_age: float | None = None

    @property
    def answered(self) -> bool:
        return bool(self.hits) or self.cached_age is not None or any(
            a.hits is not None for a in self.attempts
        )

    @property
    def offline(self) -> bool:
        asked = [a for a in self.attempts if a.reason != "skipped" and a.hits is None]
        return bool(asked) and all(a.reason == "offline" for a in asked) and not self.answered


@dataclass(frozen=True, slots=True)
class Plan:
    source: str
    url: str
    parse: Callable[[str], list[Hit]]
    accept: str = "application/json"
    deadline: float = SEARXNG_SECONDS
    #: A JSON body to POST instead of a GET (the keyless search APIs), and its headers.
    payload: dict | None = None
    headers: tuple[tuple[str, str], ...] = ()


class Chain:
    def __init__(
        self,
        client: http.WebClient,
        web: WebSettings,
        *,
        budgets: Budgets,
        scrubber: Scrubber,
        sleep: Callable[[float, CancelToken], None] | None = None,
        local_searxng: Callable[[], str | None] | None = None,
        local_engines: tuple[str, ...] = (),
        cache: SearchCache | None = None,
        saved_search: Callable[[str, str], list[Hit]] | None = None,
        save_results: Callable[[list[Hit]], None] | None = None,
    ) -> None:
        self._client = client
        #: Recent results to reuse. None asks the sources every time.
        self._cache = cache
        self._saved_search = saved_search
        self._save_results = save_results
        self._local = local_searxng
        #: The local SearXNG's engines, rotated a few at a time. Empty asks it with its own
        #: defaults, all at once.
        self._local_engines = local_engines
        self._web = web
        self._budgets = budgets
        self._scrub = scrubber
        self._sleep = sleep or (lambda seconds, cancel: cancel.wait(seconds))
        self._ddg_requests = 0
        self._ddg_stopped = ""
        self._lock = threading.Lock()

    # -- planning ----------------------------------------------------------------------------

    def scrub(self, query: str) -> str:
        return self._scrub(query)

    def _local_base(self) -> str | None:
        """The local SearXNG's address, when the app has one running and the person has not
        set their own instance (which wins, and is asked above)."""
        if self._local is None or self._web.searxng_url:
            return None
        base = self._local()
        if not base:
            return None
        self._client.guard.trust(urls.host_of(base), urls.port_of(base))
        return base

    def _rested_engines(self, tried: set[str]) -> tuple[str, ...]:
        """The next engines to ask: not blocked, not asked for this query yet, the one asked
        longest ago first (ties keep the pool's order)."""
        free = [
            engine for engine in self._local_engines
            if engine not in tried and self._budgets.denial(f"searxng:{engine}") is None
        ]
        free.sort(key=lambda engine: self._budgets.last(f"searxng:{engine}"))
        return tuple(free[:LOCAL_ENGINES_PER_SEARCH])

    def _ask_local(
        self, base: str, asked: str, enough: int, cancel: CancelToken, *, site: str = ""
    ) -> list[Attempt]:
        """Ask at most two ordinary groups, moving past fully blocked groups in the pool.

        Each engine is tried once at most. Without a pool, ask once with its defaults.
        """
        if not self._local_engines:
            plan = Plan("searxng", sources.url_searxng(base, asked), sources.parse_searxng,
                        deadline=LOCAL_SECONDS)
            return self._run([plan], cancel, site=site)
        attempts: list[Attempt] = []
        tried: set[str] = set()
        found = 0
        round_number = 0
        while round_number < LOCAL_ROUNDS:
            engines = self._rested_engines(tried)
            if not engines:
                if not attempts:
                    attempts.append(Attempt("searxng", None, "rate-limited"))
                break
            tried.update(engines)
            refused: list[tuple[str, str]] = []

            def parse(text: str, refused: list[tuple[str, str]] = refused) -> list[Hit]:
                refused.extend(sources.searxng_unresponsive(text))
                return sources.parse_searxng(text)

            for engine in engines:
                self._budgets.spend(f"searxng:{engine}")
            plan = Plan("searxng", sources.url_searxng(base, asked, engines), parse,
                        deadline=LOCAL_SECONDS)
            attempt = self._run([plan], cancel, site=site)[0]
            fully_blocked = False
            if attempt.hits is not None:
                attempt = self._note_engines(engines, refused, attempt)
                blocked = {
                    engine for engine, reason in refused
                    if any(word in reason.lower() for word, _ in _ENGINE_REST)
                }
                fully_blocked = attempt.hits is None and set(engines) <= blocked
            attempts.append(attempt)
            found += len(attempt.hits or [])
            if found >= enough:
                break
            # Every engine enters `tried` once, so a blocked group can only advance to
            # untried engines. Timeouts and transport failures still spend a normal round.
            if not fully_blocked:
                round_number += 1
        return attempts

    def _note_engines(
        self, engines: tuple[str, ...], refused: list[tuple[str, str]], attempt: Attempt
    ) -> Attempt:
        """Rest the engines SearXNG reported blocked; an answer with no results where every
        engine asked was blocked is a block, not an empty search."""
        blocked: dict[str, str] = {}
        for engine, reason in refused:
            lowered = reason.lower()
            rest = next((seconds for word, seconds in _ENGINE_REST if word in lowered), None)
            if rest is not None and engine in engines:
                self._budgets.strike(f"searxng:{engine}", rest)
                blocked[engine] = lowered
        for engine in engines:
            if engine not in blocked and engine not in dict(refused):
                self._budgets.clear(f"searxng:{engine}")
        if not attempt.hits and refused and {e for e, _ in refused} >= set(engines):
            if not blocked:
                return Attempt(attempt.source, None, "failed")
            limited = all("too many requests" in r for r in blocked.values())
            return Attempt(attempt.source, None, "rate-limited" if limited else "blocked")
        return attempt

    def _permitted(self, plan: Plan) -> bool:
        return self._client.guard.permits_host(urls.host_of(plan.url), urls.port_of(plan.url))

    # -- running -----------------------------------------------------------------------------

    def search(self, query: str, site: str, cancel: CancelToken) -> Outcome:
        if self._cache is not None and (kept := self._cache.get(query, site)) is not None:
            hits, used, age = kept
            return Outcome(hits, used, cached_age=age)
        # Known addresses come first: finding one locally must not spend an external
        # search request. web_fetch can then ask that address for its current page.
        saved: list[Hit] = []
        if self._saved_search is not None and not cancel.cancelled:
            saved = [h for h in self._saved_search(query, site)
                     if urls.normalize(h.url) is not None
                     and (not site or _on_site(h.url, site))
                     and self._client.guard.permits_host(urls.host_of(h.url), urls.port_of(h.url))]
            # One topic word does not establish the intended subtopic. The Docker
            # probe (cogito-lab, 2026-10-16) found a broad Compose page displaced the
            # useful storage reference. Two distinct content terms are a conservative
            # gate, not a semantic confidence score; stopwords add no specificity.
            if saved and len(set(terms(query))) >= 2 and not cancel.cancelled:
                return Outcome(fuse([saved]), [sources.LABELS['saved']])
        outcome = self._search(query, site, cancel)
        if self._cache is not None:
            self._cache.put(query, site, outcome.hits, outcome.used)
        if outcome.hits and self._save_results is not None:
            self._save_results(outcome.hits)
        if not outcome.hits and saved and not cancel.cancelled:
            # A weak local match is still a dated pointer when external sources fail.
            return Outcome(fuse([saved]), [sources.LABELS['saved']], outcome.attempts)
        return outcome

    def _search(self, query: str, site: str, cancel: CancelToken) -> Outcome:
        attempts: list[Attempt] = []
        lists: list[list[Hit]] = []
        asked = f"{query} site:{site}" if site else query
        # With `site`, one result on it is enough: the post-filter has already dropped the
        # rest. Without, ENOUGH.
        wanted = 1 if site else ENOUGH
        total = 0
        if self._web.searxng_url:
            address = sources.url_searxng(self._web.searxng_url, asked)
            plan = Plan("searxng", address, sources.parse_searxng)
            for attempt in self._run([plan], cancel, site=site):
                attempts.append(attempt)
                if attempt.hits is not None:
                    lists.append(attempt.hits)
                    total += len(attempt.hits)
        base = self._local_base() if total < wanted else None
        if base is not None:
            # The SearXNG this app runs, before DuckDuckGo.
            for attempt in self._ask_local(base, asked, wanted - total, cancel, site=site):
                attempts.append(attempt)
                if attempt.hits is not None:
                    lists.append(attempt.hits)
                    total += len(attempt.hits)
        # The keyless search APIs, one at a time and only while too little came back: their
        # per-IP allowance is unpublished, so it is kept for when SearXNG falls short.
        for plan in self._keyless(asked):
            if total >= wanted:
                break
            for attempt in self._run([plan], cancel, site=site):
                attempts.append(attempt)
                if attempt.hits is not None:
                    lists.append(attempt.hits)
                    total += len(attempt.hits)
        if total < wanted:
            duck = Plan("ddg", sources.url_ddg(asked), sources.parse_ddg, "text/html", DDG_SECONDS)
            for attempt in self._run([duck], cancel, site=site):
                attempts.append(attempt)
                if attempt.hits is not None:
                    lists.append(attempt.hits)
        # An address that is not a plain web address (a newline in it, `javascript:`) is no
        # result: it would be printed to the model and could never be read.
        merged = [h for h in fuse(lists) if urls.normalize(h.url) is not None]
        if site:
            merged = [h for h in merged if _on_site(h.url, site)]
        used = []
        for hit in merged:
            if (label := sources.LABELS[hit.source]) not in used:
                used.append(label)
        return Outcome(merged, used, attempts)

    @staticmethod
    def _keyless(asked: str) -> list[Plan]:
        return [
            Plan("tavily", sources.TAVILY_URL, sources.parse_tavily, deadline=KEYLESS_SECONDS,
                 payload=sources.tavily_payload(asked), headers=sources.TAVILY_HEADERS),
            Plan("firecrawl", sources.FIRECRAWL_URL, sources.parse_firecrawl,
                 deadline=KEYLESS_SECONDS, payload=sources.firecrawl_payload(asked)),
        ]

    def _run(
        self, plans: list[Plan], cancel: CancelToken, *, site: str = ""
    ) -> list[Attempt]:
        if not plans:
            return []
        if len(plans) == 1:
            results = [self._ask(plans[0], cancel)]
        else:
            with ThreadPoolExecutor(max_workers=len(plans)) as pool:
                futures = [pool.submit(self._ask, plan, cancel) for plan in plans]
                results = [future.result() for future in futures]
        # SearXNG's Search API warns that engines can ignore `site:` (read 2026-10-16,
        # docs.searxng.org/dev/search_api.html). Filter before counting enough results or
        # fusing the top eight, so unrelated hits cannot stop rotation or hide a valid hit.
        if site:
            for attempt in results:
                if attempt.hits is not None:
                    attempt.hits = [hit for hit in attempt.hits if _on_site(hit.url, site)]
        return results

    def _ask(self, plan: Plan, cancel: CancelToken) -> Attempt:
        source = plan.source
        if not self._permitted(plan):
            return Attempt(source, None, "skipped")
        if source == "ddg":
            with self._lock:
                if self._ddg_stopped:
                    return Attempt(source, None, self._ddg_stopped)
                if self._ddg_requests >= DDG_PER_SESSION:
                    return Attempt(source, None, "limit reached for this session")
        if (reason := self._budgets.denial(source)) is not None:
            return Attempt(source, None, reason)
        if (pause := self._budgets.wait(source)) > 0:
            self._sleep(pause, cancel)
        if source == "ddg":
            with self._lock:
                self._ddg_requests += 1
        self._budgets.spend(source)
        try:
            if plan.payload is not None:
                got = self._client.post_json(
                    plan.url, plan.payload, cancel=cancel, headers=plan.headers,
                    max_bytes=2_000_000, deadline=plan.deadline,
                )
            else:
                got = self._client.get(
                    plan.url, cancel=cancel, accept=plan.accept, max_bytes=2_000_000,
                    deadline=plan.deadline,
                )
        except urls.WebRefusal as refusal:
            if refusal.kind == "cancelled":
                raise
            if refusal.kind == "unreachable":
                return Attempt(source, None, "offline")
            if refusal.kind == "timeout":
                return Attempt(source, None, "timed out")
            if refusal.kind in ("not_public", "blocked_host"):
                return Attempt(source, None, "skipped")
            return Attempt(source, None, "failed")
        return self._answer(plan, got)

    def _stop_ddg(self) -> str:
        with self._lock:
            self._ddg_stopped = "blocked for this session"
        return self._ddg_stopped

    def _answer(self, plan: Plan, got: http.Fetched) -> Attempt:
        source = plan.source
        status = got.status
        if source == "ddg" and status in (202, 403, 429):
            return Attempt(source, None, self._stop_ddg())
        if status == 429:
            wait = got.headers.get("retry-after", "")
            self._budgets.block(source, float(wait) if wait.isdigit() else DEFAULT_REST_SECONDS)
            return Attempt(source, None, "rate-limited")
        if status == 403:
            remaining = got.headers.get("x-ratelimit-remaining")
            reset = got.headers.get("x-ratelimit-reset", "")
            if remaining == "0" and reset.isdigit():
                self._budgets.block(source, max(1.0, float(reset) - time.time()))
                return Attempt(source, None, "rate-limited")
            self._budgets.block(source, DEFAULT_REST_SECONDS)
            return Attempt(source, None, "blocked")
        if status != 200:
            return Attempt(source, None, "failed")
        body = http.decode_body(got.body, got.headers.get("content-type", ""))
        try:
            hits = plan.parse(body)
        except SourceError as error:
            if source == "ddg" and error.kind == "blocked":
                return Attempt(source, None, self._stop_ddg())
            if error.kind == "rate_limited":
                self._budgets.block(source, error.retry_after or DEFAULT_REST_SECONDS)
                return Attempt(source, None, "rate-limited")
            return Attempt(source, None, "blocked" if error.kind == "blocked" else "failed")
        return Attempt(source, hits)


def _on_site(url: str, site: str) -> bool:
    host = urls.host_of(url)
    return host == site or host.endswith("." + site)


def default_budgets() -> Budgets:
    return Budgets(paths.web_state_file())
