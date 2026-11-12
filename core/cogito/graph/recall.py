"""The retrieval ladder: one cheap answer first, pointers second, bodies last.

Two different questions are asked of the store. *What does the project's documentation
say* is answered from the prose graph. *What did we already do about this* is answered from
the conversation, across the whole compaction lineage. Both climb the same ladder. It lives
here and not in a prompt because a policy the model is asked to follow holds only until the
model has a bad day.

The rungs, with the numbers each comes from (Cogito Estella's 50-question set):

  1. one `ask` at the configured budget                    0.78 accurate,   588 tok
  2. if it abstains or scores under the floor, `ask`
     again at twice the budget (a second ask spends no
     LLM tokens, so it is far cheaper than reading)        0.94 accurate, 1,380 tok
  3. still not confident: hand back *pointers*, never
     bodies, and let the caller fetch the one it wants     0.96 accurate, 2,486 tok

Rung 3 is where the two differ. Documentation points at files, which the model can already
`read`. The conversation points at messages, which nothing else can reach, so `at()` here
is the only way back to one. It walks the lineage, which is why compaction no longer means
forgetting.

A third caller, `DocumentKnowledge`, asks about one long document the conversation holds
(`graph.documents`; off unless ``[estella] documents`` is on). Its rung 3 points at pages,
as the `read` calls that open them.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cogito.errors import GraphUnavailable
from cogito.graph.driver import GraphDriver
from cogito.graph.estella import AskReply

log = logging.getLogger(__name__)

#: Documents worth handing to Estella. Prose only: code goes to the code graph, and a
#: README full of fenced shell blocks already extracts badly enough.
PROSE_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})

#: Source and config handed to Estella as well when `ask` covers code (`[estella] ask_code`,
#: the app only). Cogito Estella 0.19.3 splits a source whose name has one of these by line,
#: with its identifiers' words and its path, instead of reading it as one sentence.
CODE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh",
    ".lua", ".sql", ".dart", ".scala", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg",
    ".gradle", ".vue", ".svelte",
})

#: Files `ask` indexes with code: more than prose's 40, since a project has far more source
#: files than notes and the lite index takes one in well under a tenth of a second.
CODE_FILE_LIMIT = 400

#: Directories never walked when collecting a project's prose.
_SKIP_DIRS = frozenset(
    {".git", ".cogito", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
)

#: A document larger than this is almost always generated (a changelog, a lock summary).
_MAX_DOC_BYTES = 400_000

#: How many messages a pointer list may name. Enough to choose from, small enough to read.
_POINTER_LIMIT = 8

#: Characters of a message shown in a pointer: enough to recognise it, not the whole
#: message.
_SNIPPET_CHARS = 220

#: Lucene reserves these. A question is prose, not a query language, so they are removed
#: instead of escaped: a stray `~` in a sentence must not become a fuzzy search.
_LUCENE_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')

_ADDRESS = re.compile(r"^\s*(?P<session>[A-Za-z0-9_-]+)\s*:\s*(?P<seq>\d+)\s*$")

#: The property a rewind sets on each message it hides (`graph.schema`,
#: `conversation_rewound`). Passed as a parameter, like `graph.history`'s keys, so a
#: database that has never seen a rewind does not warn about an unknown property.
_REWOUND = "rewound_by"

#: Where a sentence in an Estella reply came from: `recall.remember` ingests each message as
#: ``msg:<id>``, and Estella names the source of every sentence it quotes.
_SENTENCE_SOURCE = re.compile(r"(?m)^msg:(?P<id>[A-Za-z0-9_-]+) s\d+:")
#: A fact line's edge ids, ``subject relation object #3,17``.
_FACT_EDGES = re.compile(r"(?m)#(?P<ids>\d+(?:,\d+)*)\s*$")


#: How long a call may wait for ingestion queued just ahead of it before scoring against
#: whatever has landed. On this project's own five-document fixture, encoder warm-up plus
#: ingestion took ~9s, mostly loading the model on the first document. Generous so a larger
#: project can still finish, bounded so a stuck or very large corpus cannot hang a turn.
_INGEST_WAIT_SECONDS = 60.0


class Asker(Protocol):
    """What the ladder needs from Estella. Narrow enough to fake in a test."""

    ready: bool

    def ask(self, question: str, budget: int) -> AskReply | None: ...

    def submit(self, text: str, source: str) -> None: ...

    def drain(self, timeout: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class Answer:
    """One rung's output, plus which rung produced it.

    ``rung`` is telemetry: how often the cheap path was enough, which is what decides
    whether the ladder pays for itself.
    """

    text: str
    rung: str
    confidence: float | None = None
    found: bool = True


def prose_documents(root: Path, limit: int = 40, *, code: bool = False) -> list[Path]:
    """Up to ``limit`` prose files in the project, shallowest first; with ``code``, its
    source and config files too, up to `CODE_FILE_LIMIT`.

    The limit is applied in path order, before the sort. Shallowest first because a README
    at the root describes the project and a note four directories down describes a corner,
    and the budget runs out before the corners.
    """
    suffixes = PROSE_SUFFIXES | CODE_SUFFIXES if code else PROSE_SUFFIXES
    limit = max(limit, CODE_FILE_LIMIT) if code else limit
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= limit:
            break
        if path.suffix.lower() not in suffixes or not path.is_file():
            continue
        parents = path.relative_to(root).parts[:-1]
        if any(part in _SKIP_DIRS or part.startswith(".") for part in parents):
            continue
        try:
            if path.stat().st_size > _MAX_DOC_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return sorted(found, key=lambda p: (len(p.relative_to(root).parts), str(p)))


def ingest_documents(estella: Asker, root: Path, *, code: bool = False) -> tuple[str, ...]:
    """Queue the project's prose (and code, with ``code``). Each goes under its path, which
    tells Estella prose from code. Returns the sources queued, for the caller to report."""
    sources: list[str] = []
    for path in prose_documents(root, code=code):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.debug("skipping %s: %s", path, exc)
            continue
        source = str(path.relative_to(root))
        estella.submit(text, source)
        sources.append(source)
    return tuple(sources)


def climb(
    estella: Asker, question: str, *, budget: int, floor: float
) -> tuple[AskReply | None, str]:
    """Rungs 1 and 2, shared by both callers. Returns the best reply and the rung reached.

    The second ask is not a retry: the first answer was real but unsure. It costs no LLM
    tokens, which is the only reason asking twice beats reading once.
    """
    first = estella.ask(question, budget)
    if first is None:
        return None, "unavailable"
    if first.confident(floor):
        return first, "ask"
    second = estella.ask(question, budget * 2)
    if second is not None and second.confident(floor):
        return second, "ask_wide"
    return (second or first), "escalated"


class Knowledge:
    """`ask` over the project's prose: rung 3 names documents the caller can read."""

    def __init__(
        self, estella: Asker, root: Path, *, budget: int, floor: float, code: bool = False
    ) -> None:
        self._estella = estella
        self._root = root
        self._budget = budget
        self._floor = floor
        #: Index the project's source and config too (`[estella] ask_code`, the app only).
        self._code = code
        self._ingested = False

    def warm(self) -> None:
        """Queue the project's documents once. Later calls do nothing."""
        if self._ingested:
            return
        self._ingested = True
        ingest_documents(self._estella, self._root, code=self._code)

    def ask(self, question: str, budget: int | None = None) -> Answer:
        self.warm()
        # `warm` only queues the documents; Estella ingests them on its own worker thread.
        # Without this wait, a question in a session's first step can be scored against
        # whichever documents landed first (one file of five, in the case that exposed
        # this), and a scorer confident about the wrong document looks the same as one
        # confident about the right one. After the first call the queue is empty, so this
        # returns at once.
        self._estella.drain(_INGEST_WAIT_SECONDS)
        reply, rung = climb(
            self._estella, question, budget=budget or self._budget, floor=self._floor
        )
        if reply is None:
            return Answer(self._unavailable(), "unavailable", found=False)
        if rung in ("ask", "ask_wide"):
            return Answer(reply.body, rung, confidence=reply.confidence)
        return Answer(self._escalate(reply), rung, confidence=reply.confidence, found=False)

    def _names(self) -> str:
        found = prose_documents(self._root, code=self._code)[:6]
        return ", ".join(str(p.relative_to(self._root)) for p in found)

    def _unavailable(self) -> str:
        names = self._names()
        hint = f" The project's documents are: {names}." if names else ""
        return (
            "The prose graph is not answering yet — it loads a model on first use and may "
            f"still be warming up, or it is not installed here.{hint} Read the one that "
            "looks relevant with `read` or `grep`; asking again only helps if it was warming "
            "up."
        )

    def _escalate(self, reply: AskReply) -> str:
        """Rung 3. Say plainly that this is the unsure material, and where to go instead."""
        names = self._names()
        tail = f" Read one of: {names}" if names else ""
        elsewhere = (
            " If what you want was said in this conversation rather than written in the "
            "project's documents, use `recall` instead — this tool only reads the documents."
        )
        if reply.abstained:
            # Nothing was found. Estella's own "no material for ..." line would only repeat
            # that, and would read as content.
            return f"The documentation does not appear to answer that.{tail}{elsewhere}"
        score = "no score" if reply.confidence is None else f"{reply.confidence:.2f}"
        return (
            f"Low confidence ({score}) — this may be right, but check it before relying on "
            f"it.{tail}{elsewhere}\n\n{reply.body}"
        )


class Recall:
    """`recall` over the conversation, across the compaction lineage.

    Rung 3 here answers with pointers (session, position, role and a snippet). Handing back
    whole messages would make recall cost more than the compaction it exists to make
    survivable.
    """

    def __init__(
        self,
        estella: Asker,
        driver: GraphDriver,
        session_id: str,
        *,
        budget: int,
        floor: float,
    ) -> None:
        self._estella = estella
        self._driver = driver
        self._session_id = session_id
        self._budget = budget
        self._floor = floor

    def follow(self, session_id: str) -> None:
        """Move to the session compaction just created.

        Without this, pointers call the child "an earlier session" and every lookup starts
        its lineage walk one node behind the conversation.
        """
        self._session_id = session_id

    # -- ingestion ------------------------------------------------------------------

    def remember(self, message_id: str, text: str) -> None:
        """Hand one turn to the prose graph, with the message id as its provenance.

        Called after every message is recorded. Costs no LLM tokens and is queued off the
        turn. This is why a compacted-away message can still be answered from later.
        """
        self._estella.submit(text, f"msg:{message_id}")

    # -- the ladder -----------------------------------------------------------------

    def ask(self, query: str, budget: int | None = None) -> Answer:
        # The same race as Knowledge.ask, narrower: only the few messages `remember` just
        # submitted may still be queued. A question asked right after a busy turn could
        # still be scored against a graph missing the message it asks about.
        self._estella.drain(_INGEST_WAIT_SECONDS)
        reply, rung = climb(
            self._estella, query, budget=budget or self._budget, floor=self._floor
        )
        if reply is not None and rung in ("ask", "ask_wide"):
            if not self._rests_on_rewound(reply):
                return Answer(reply.body, rung, confidence=reply.confidence)
            # A rewind took those messages out of the conversation: the person discarded
            # them, and Estella cannot forget what it ingested. The pointers below come from
            # the graph, which leaves them out.
            rung = "rewound"
        pointers = self.search(query)
        if pointers:
            # Say why the cheap rung was skipped: "not sure" and "could not look" call for
            # different things from the reader.
            why = {
                "unavailable": "The conversation graph is not answering yet",
                "rewound": "What it found rests on messages the conversation was taken back "
                "from",
            }.get(rung, "Not confident enough to answer outright")
            head = (
                f"{why}. These earlier messages mention it — read one with "
                'recall(at="<session>:<seq>"):\n\n'
            )
            return Answer(head + pointers, "pointers", found=True)
        if reply is None:
            return Answer(
                "The conversation graph is not answering yet — it loads a model on first "
                "use — and no earlier message matched those words.",
                "unavailable",
                found=False,
            )
        return Answer(
            f"Nothing in this conversation's history matches {query!r}. Try the words that "
            f"were actually used at the time; if you are after something the project's "
            f"documentation states rather than something said here, use `ask`.",
            rung,
            found=False,
        )

    # -- what a rewind took out --------------------------------------------------------

    def rewound(self) -> set[str]:
        """The messages of this lineage a rewind hid and no redo brought back. They stay in
        the store (nothing is ever deleted) but are no longer the conversation: the person
        went back to before them, so `recall` leaves them out too."""
        try:
            rows = self._driver.query(
                "MATCH (s:Session)-[:RECORDED]->(r) WHERE s.id IN $ids AND $label IN labels(r) "
                "UNWIND coalesce(r[$hidden], []) AS id "
                "MATCH (m:Message {id: id}) WHERE m[$rewound] = r.id "
                "RETURN collect(DISTINCT id) AS ids",
                ids=self.lineage(),
                label="Rewind",
                hidden="hidden_ids",
                rewound=_REWOUND,
            )
        except GraphUnavailable as exc:
            log.warning("recall: rewinds unavailable: %s", exc)
            return set()
        return set(rows[0]["ids"]) if rows else set()

    def context_for(self, query: str, *, in_window: frozenset[str]) -> str | None:
        """What the record holds for ``query`` that the model no longer has in front of it:
        the sentences of a confident Estella answer whose messages are outside the window
        (``in_window``, message ids) and not rewound, each with its source. None when
        Estella is unsure, cannot answer, or every sentence is already in view.

        For the harness's own lookup after a fold (`Engine`'s ``recall_context``, the app
        only): the model does not call `recall` when it has forgotten (CONTRIBUTING 1b;
        long-memory probe 2026-10-17, qwen3.5-9b, 0 calls in 19 folds, five values answered
        «No se ha mencionado»), so it is not left to.

        Never waits for an Estella that is not up: it is the harness's own lookup, so one
        still starting, or missing, means nothing is added this turn. Once auto-recall
        really ran (2026-10-17, it had been raising on every call), an Estella that could not
        start held a turn until the client's start timeout."""
        if not getattr(self._estella, "ready", False):
            return None
        self._estella.drain(_INGEST_WAIT_SECONDS)
        reply, rung = climb(self._estella, query, budget=self._budget, floor=self._floor)
        if reply is None or rung not in ("ask", "ask_wide") or self._rests_on_rewound(reply):
            return None
        kept: list[str] = []
        keep = False
        for line in reply.body.splitlines():
            if (found := _SENTENCE_SOURCE.search(line)) is not None:
                keep = found.group("id") not in in_window
            elif not kept and not keep:
                continue  # the facts and the divider before the first sentence
            if keep:
                kept.append(line)
        return "\n".join(kept) if kept else None

    def _rests_on_rewound(self, reply: AskReply) -> bool:
        """Whether an Estella answer quotes, or draws a fact from, a message a rewind hid.
        Sentences name their source; a fact is traced through `provenance` when the client
        can, and taken as tainted when it cannot."""
        hidden = self.rewound()
        if not hidden:
            return False
        if any(m.group("id") in hidden for m in _SENTENCE_SOURCE.finditer(reply.text)):
            return True
        edges = [i for m in _FACT_EDGES.finditer(reply.text) for i in m.group("ids").split(",")]
        if not edges:
            return False
        trace = getattr(self._estella, "provenance", None)
        traced = trace(edges) if callable(trace) else None
        if traced is None:
            return True
        return any(
            source in hidden for source in re.findall(r"msg:([A-Za-z0-9_-]+)", traced)
        )

    # -- rung 3: the graph itself ----------------------------------------------------

    def lineage(self) -> list[str]:
        """Every session this one descends from or was compacted into, plus itself. Also
        any session a rewind took the conversation back into (`CONTINUES_FROM`, a point
        from another opening of the project), with the sessions it was folded from.

        This is what makes compaction a link and not a loss: the child session is a new
        node, and without walking the edge its parent's messages are unreachable. What a
        rewind hid in those sessions stays out (`rewound`, the searches' own filter).
        """
        try:
            rows = self._driver.query(
                "MATCH (s:Session {id: $session_id}) "
                "OPTIONAL MATCH (before:Session)-[:COMPACTED_TO*]->(s) "
                "OPTIONAL MATCH (s)-[:COMPACTED_TO*]->(after:Session) "
                "WITH s, collect(DISTINCT before) AS befores, collect(DISTINCT after.id) AS afters "
                "UNWIND befores + [s] AS own "
                "OPTIONAL MATCH (own)-[:CONTINUES_FROM]->(:Session)"
                "<-[:COMPACTED_TO*0..]-(earlier:Session) "
                "RETURN [b IN befores | b.id] + afters AS others, "
                "collect(DISTINCT earlier.id) AS continued",
                session_id=self._session_id,
            )
        except GraphUnavailable as exc:
            log.warning("recall: lineage unavailable: %s", exc)
            return [self._session_id]
        row = rows[0] if rows else {}
        found = [i for i in [*row.get("others", []), *row.get("continued", [])] if i]
        return [self._session_id, *dict.fromkeys(i for i in found if i != self._session_id)]

    def search(self, query: str, limit: int = _POINTER_LIMIT) -> str:
        """Full-text over the lineage. Pointers and snippets, never whole messages."""
        terms = [t for t in _LUCENE_SPECIAL.sub(" ", query).split() if len(t) > 2]
        if not terms:
            return ""
        try:
            rows = self._driver.query(
                "CALL db.index.fulltext.queryNodes('cogito_message_text', $terms) "
                "YIELD node, score "
                "WHERE node.session_id IN $ids AND node[$rewound] IS NULL "
                "RETURN node.session_id AS session, node.seq AS seq, node.role AS role, "
                "node.step AS step, node.text AS text, score ORDER BY score DESC LIMIT $limit",
                terms=" OR ".join(terms),
                ids=self.lineage(),
                limit=limit,
                rewound=_REWOUND,
            )
        except GraphUnavailable as exc:
            log.warning("recall: full-text search unavailable: %s", exc)
            return ""
        return "\n".join(self._pointer(row) for row in rows)

    def _pointer(self, row: dict[str, Any]) -> str:
        text = " ".join((row.get("text") or "").split())
        if len(text) > _SNIPPET_CHARS:
            text = text[:_SNIPPET_CHARS].rstrip() + "…"
        here = "" if row["session"] == self._session_id else " (earlier session)"
        head = f"{row['session']}:{row['seq']} {row['role']} step {row.get('step', 0)}{here}"
        return f"{head}\n  {text}"

    def at(self, address: str) -> Answer:
        """One message in full, addressed as `<session>:<seq>` from a pointer list."""
        match = _ADDRESS.match(address)
        if match is None:
            return Answer(
                f"{address!r} is not a message address. Use the `<session>:<seq>` form "
                "exactly as it appears in a pointer list.",
                "address",
                found=False,
            )
        session, seq = match.group("session"), int(match.group("seq"))
        if session not in self.lineage():
            return Answer(
                f"Session {session} is not part of this conversation's history.",
                "address",
                found=False,
            )
        try:
            rows = self._driver.query(
                "MATCH (m:Message {session_id: $session, seq: $seq}) "
                "RETURN m.role AS role, m.text AS text, m.step AS step, "
                "m[$rewound] AS rewound",
                session=session,
                seq=seq,
                rewound=_REWOUND,
            )
        except GraphUnavailable as exc:
            return Answer(f"The graph is unreachable: {exc}", "unavailable", found=False)
        if not rows:
            return Answer(f"No message at {address}.", "address", found=False)
        row = rows[0]
        if row.get("rewound"):
            return Answer(
                f"The conversation was taken back to before {address}; that message is no "
                "longer part of it.",
                "address",
                found=False,
            )
        return Answer(f"{session}:{seq} {row['role']} (step {row['step']})\n\n{row['text']}", "at")


# -- one document --------------------------------------------------------------------------

#: How long `DocumentKnowledge.ask` waits for Estella to load its model, early in a session,
#: when the document is already in its index. It never waits for a document still being
#: indexed: those parts come back as pointers, which the model can `read` at once.
_DOCUMENT_WAIT_SECONDS = 20.0

#: How many pages a pointer list names.
_PAGE_POINTERS = 5

#: Pages an answer's "to check it" list names at most.
_CITED_PAGES = 5

#: A page scoring under this share of the best page's score is left out of the pointers.
_POINTER_FLOOR = 0.6

#: Lines one pointer asks `read` for: a page of the converted text is rarely longer.
_POINTER_LINES = 80

#: An Estella sentence, ``<source> s<index>: "<sentence>"``. A sentence may hold newlines
#: (spaCy keeps a PDF's line breaks inside one, and a table's rows are one "sentence"), so
#: it runs from its opening line to the line before the next sentence's.
_SENTENCE_LINE = re.compile(r'^(?P<source>\S+) s(?P<index>\d+): "(?P<text>.*)"$', re.DOTALL)
_SENTENCE_START = re.compile(r'^\S+ s\d+: "')
#: The edge ids at the end of a fact line, before Estella's own ellipsis when it capped them.
_FACT_IDS = re.compile(r"\s*#(?P<ids>\d+(?:,\d+)*)(?:\s*…)?\s*$")
#: One edge of a `provenance` reply: ``#<id> <s> <r> <o>  <source> s<index> [a:b]/[c:d]``.
_PROVENANCE_EDGE = re.compile(r"^#(?P<id>\d+) .*  (?P<source>\S+) s\d+ \[", re.MULTILINE)
#: Estella's divider before class-only facts (`cogito_estella.mcp.store.DIVIDER`).
_CLASS_ONLY = "~ class-only, verify with provenance:"

#: Words that carry no question (English and Spanish, the two the app is used in).
_STOP_WORDS = (
    "the and for are was were what which who whom whose when where why how does did has "
    "have had that this these those with from into than then there their them they its "
    "per not but can could would should will shall may might must you your our about "
    "las los del una uno unos unas que qué cual cuál cuales cuáles como cómo cuando cuándo "
    "donde dónde para por con sin sobre entre este esta estos estas ese esa eso fue fueron "
    "era son está están hay del al lo le les su sus"
)
_STOP = frozenset(_STOP_WORDS.split())
#: The same words, for ranking elsewhere (`tools/ranking.py`).
STOP_WORDS = _STOP
_WORD = re.compile(r"\w+", re.UNICODE)


def _terms(text: str) -> list[str]:
    return [
        w
        for w in (m.group(0).lower() for m in _WORD.finditer(text))
        if (len(w) > 2 or w.isdigit()) and w not in _STOP
    ]


#: The public name of the question-word filter, for callers outside the graph layer (the
#: web tool matches a page's lines against a question with the same words `pointers` uses).
terms = _terms


class ScopedAsker:
    """Estella's `ask`, answered from one document's parts only.

    Estella has no source filter (0.17.0: `ask(question, budget)` ranks the whole graph), so
    the scope is applied to its reply. A sentence stays when its source is one of the
    document's parts, a fact when `provenance` traces one of its edges to one, and every
    line that stays is labelled with its page. A fact that cannot be traced is dropped,
    never shown unscoped. The scorer's confidence is kept only when its best sentence is
    one of this document's; otherwise the reply counts as unsure and the ladder escalates.
    """

    def __init__(self, estella: Any, document: Any) -> None:
        self._estella = estella
        self._document = document

    @property
    def ready(self) -> bool:
        return bool(getattr(self._estella, "ready", False))

    def submit(self, text: str, source: str) -> None:  # pragma: no cover - never ingests
        return None

    def drain(self, timeout: float) -> bool:  # pragma: no cover - the index waits instead
        return True

    def ask(self, question: str, budget: int) -> AskReply | None:
        reply = self._estella.ask(question, budget)
        if reply is None or reply.abstained:
            return reply
        return self.scope(question, reply)

    def _labels(self, sources: list[str]) -> str:
        labels: list[str] = []
        for source in sources:
            for part in self._document.parts_of(source):
                if part.label not in labels:
                    labels.append(part.label)
        return ", ".join(labels)

    def scope(self, question: str, reply: AskReply) -> AskReply:
        own = self._document.sources
        lines = reply.text.splitlines()
        header = lines[0] if lines and lines[0].startswith("entities:") else None
        rest = lines[1:] if header is not None else lines
        if "--" in rest:
            cut = rest.index("--")
            facts, sentences = rest[:cut], rest[cut + 1 :]
        else:
            facts, sentences = rest, []

        entries: list[str] = []
        for line in sentences:
            if _SENTENCE_START.match(line) or not entries:
                entries.append(line)
            else:
                entries[-1] += "\n" + line
        kept_sentences: list[str] = []
        best_kept = False
        for position, line in enumerate(entries):
            match = _SENTENCE_LINE.match(line)
            if match is None or match.group("source") not in own:
                continue
            best_kept = best_kept or position == 0
            label = self._labels([match.group("source")])
            kept_sentences.append(f'[{label}] "{match.group("text")}"')

        kept_facts = self._scope_facts(facts, own)
        if not kept_sentences and not any(f != _CLASS_ONLY for f in kept_facts):
            return AskReply(text=f"no material for '{question}'", abstained=True)
        confidence = reply.confidence
        if confidence is not None and not best_kept:
            confidence = 0.0
        body = [*kept_facts, *(["--", *kept_sentences] if kept_sentences else [])]
        text = "\n".join([header, *body] if header is not None else body)
        return AskReply(
            text=text,
            entities=reply.entities,
            confidence=confidence,
            scorer=reply.scorer,
        )

    def _scope_facts(self, facts: list[str], own: frozenset[str]) -> list[str]:
        edges: dict[str, list[str]] = {}
        for line in facts:
            if (found := _FACT_IDS.search(line)) is not None:
                edges[line] = found.group("ids").split(",")
        if not edges:
            return []
        trace = getattr(self._estella, "provenance", None)
        wanted = sorted({i for ids in edges.values() for i in ids}, key=int)
        traced = trace(wanted) if callable(trace) else None
        if not traced:
            return []
        source_of = {m.group("id"): m.group("source") for m in _PROVENANCE_EDGE.finditer(traced)}
        kept: list[str] = []
        pending_divider = False
        for line in facts:
            if line == _CLASS_ONLY:
                pending_divider = True
                continue
            ids = edges.get(line)
            if not ids:
                continue
            sources = [source_of[i] for i in ids if source_of.get(i) in own]
            if not sources:
                continue
            if pending_divider:
                kept.append(_CLASS_ONLY)
                pending_divider = False
            fact = _FACT_IDS.sub("", line)
            kept.append(f"{fact} [{self._labels(list(dict.fromkeys(sources)))}]")
        return kept


class DocumentKnowledge:
    """`ask_document`: a question about one document the conversation holds, answered from
    its Estella index with the page of every line. When the index is unsure, still indexing
    or not running, the answer is the pages most likely to hold it, as `read` calls.

    The same ladder as `ask` and `recall` (`climb`): one ask at the configured budget, a
    wider one when unsure, pointers last. Rung 3's pointers come from the document's own
    text, which the harness already holds (`tools.document_cache`), scored by the
    question's words. They never come from the model, and they are available from the first
    second, before Estella has ingested anything.
    """

    def __init__(
        self,
        index: Any,
        *,
        budget: int,
        floor: float,
        wait_seconds: float = _DOCUMENT_WAIT_SECONDS,
    ) -> None:
        self._index = index
        self._budget = budget
        self._floor = floor
        self._wait = wait_seconds

    def ask(self, question: str, document: str | None = None, budget: int | None = None) -> Answer:
        found = self._index.find(document)
        if found is None:
            names = ", ".join(d.name for d in self._index.documents()) or "none"
            return Answer(
                f"No document in this conversation matches {document!r}. The documents are: "
                f"{names}. Pass one of those names as `document`, or leave it out for the "
                "most recent.",
                "address",
                found=False,
            )
        head = f"From {found.short()}:"
        # While parts of it are not in yet, or Estella is taking one in, an ask would wait
        # behind that ingest (Estella runs its tools one at a time) for up to its call
        # timeout. The pointers come from the document's own text instead, at once.
        ingesting = getattr(self._index, "ingesting", None)
        if self._index.pending(found) or (callable(ingesting) and ingesting()):
            return self._pointers_only(found, question, head, None, "pointers")
        estella = self._index.client
        # Estella loads its model on first use. Early in a session, a few seconds' wait
        # gets an answer where not waiting gets "not answering yet". A failed or disabled
        # Estella returns False at once, and the pointers follow.
        if not getattr(estella, "ready", False):
            wait_ready = getattr(estella, "wait_ready", None)
            if callable(wait_ready):
                wait_ready(self._wait)
        scoped = ScopedAsker(estella, found)
        reply, rung = climb(scoped, question, budget=budget or self._budget, floor=self._floor)
        if reply is not None and rung in ("ask", "ask_wide"):
            return Answer(
                f"{head}\n{reply.body}{self._cited(found, reply.body)}",
                rung,
                confidence=reply.confidence,
            )
        return self._pointers_only(found, question, head, reply, rung)

    def indexed_answer(self, found: Any, question: str, budget: int) -> Answer | None:
        """An Estella answer for ``found``, only when one is ready now: every part already
        in the index, no ingest under way, Estella up and confident. Anything else is None
        at once, so a caller that has its own fallback never waits on the index (`ask`
        returns pointers instead, and the web tool's fallback is the page text)."""
        ingesting = getattr(self._index, "ingesting", None)
        if self._index.pending(found) or (callable(ingesting) and ingesting()):
            return None
        estella = self._index.client
        if not getattr(estella, "ready", False):
            wait_ready = getattr(estella, "wait_ready", None)
            if callable(wait_ready):
                wait_ready(self._wait)
        reply, rung = climb(ScopedAsker(estella, found), question, budget=budget, floor=self._floor)
        if reply is None or rung not in ("ask", "ask_wide"):
            return None
        return Answer(
            f"{reply.body}{self._cited(found, reply.body)}", rung, confidence=reply.confidence
        )

    def _pointers_only(
        self, found: Any, question: str, head: str, reply: AskReply | None, rung: str
    ) -> Answer:
        """The answer without the index: the pages most likely to hold it, as `read`
        calls, with what an unsure ``reply`` found."""
        why = (
            "The document index is not answering yet"
            if reply is None
            else "Not confident enough to answer from the index"
        )
        waiting = self._index.pending(found)
        if waiting:
            why += f" ({waiting} of {len(found.sources)} parts still being indexed)"
        unsure = ""
        if reply is not None and not reply.abstained and "--" in reply.body.splitlines():
            # Only the sentences, each with its page. On the real Estella, an unsure reply's
            # facts were class-only guesses spread over pages that did not hold the answer.
            quoted = reply.body.splitlines()
            sentences = quoted[quoted.index("--") + 1 :]
            if sentences:
                unsure = "\n\nWhat it found, unsure:\n" + "\n".join(sentences)
        pointers = self.pointers(found, question, reply.body if reply is not None else "")
        if not pointers:
            return Answer(
                f"{head}\n{why}, and no page mentions those words. Read it with "
                f'read(path="{found.path}"), or ask with the words the document would use.'
                f"{unsure}",
                "unavailable" if reply is None else rung,
                found=False,
            )
        return Answer(
            f"{head}\n{why}. The pages most likely to hold it -- read one:\n{pointers}{unsure}",
            "pointers",
            confidence=reply.confidence if reply is not None else None,
            found=False,
        )

    @staticmethod
    def _read_call(document: Any, part: Any) -> str:
        limit = min(part.last_line - part.first_line + 1, _POINTER_LINES)
        return f'read(path="{document.path}", offset={part.first_line}, limit={max(limit, 1)})'

    def _cited(self, document: Any, body: str) -> str:
        """Where to read each page an answer cites, so any line of it can be checked: the
        pages of its quoted sentences first (the evidence, in the scorer's order), then
        those its facts name, `_CITED_PAGES` at most."""
        quoted = [m.group(1) for m in re.finditer(r'(?m)^\[([^\[\]]+)\] "', body)]
        named = re.findall(r"\[([^\[\]]+)\]", body)
        cited: list[Any] = []
        for label_group in [*quoted, *named]:
            for label in label_group.split(", "):
                for part in document.parts:
                    if part.label == label and part not in cited:
                        cited.append(part)
        if not cited:
            return ""
        lines = [
            f"  {part.label}: {self._read_call(document, part)}"
            for part in cited[:_CITED_PAGES]
        ]
        return "\n\nTo check it, read the page:\n" + "\n".join(lines)

    def pointers(self, document: Any, question: str, unsure: str = "") -> str:
        """The parts whose text shares the most of the question's words, weighted by how
        rare each word is in the document, with the line that holds most of them."""
        wanted = list(dict.fromkeys(_terms(question)))
        if not wanted:
            return ""
        parts = [p for p in document.parts if p.text.strip()]
        counted = [_count(part.text) for part in parts]
        total = len(parts)
        frequency = {t: sum(1 for c in counted if t in c) for t in wanted}
        scored: list[tuple[float, int]] = []
        for position, counts in enumerate(counted):
            score = 0.0
            for term in wanted:
                if counts.get(term):
                    rarity = math.log(1 + total / frequency[term])
                    weight = 2.0 if term.isdigit() else 1.0
                    score += (1 + math.log(counts[term])) * rarity * weight
            if score > 0:
                scored.append((score, position))
        # A page the unsure reply cited only breaks ties. On the real Estella (cogito-lab
        # experiments/2026-09-30-doc-cache), an unsure reply to a table question cited eight
        # pages through one class-only fact, none holding the answer, and ranking them
        # first buried the one page whose words matched.
        quoted = {
            label
            for group in re.findall(r"\[([^\[\]]+)\]", unsure)
            for label in group.split(", ")
        }
        scored.sort(key=lambda item: (-item[0], -(parts[item[1]].label in quoted), item[1]))
        # Drop pages that match far less than the best one: beside it they are noise.
        best = scored[0][0] if scored else 0.0
        scored = [(score, position) for score, position in scored if score >= best * _POINTER_FLOOR]
        lines: list[str] = []
        for _, position in scored[:_PAGE_POINTERS]:
            part = parts[position]
            lines.append(
                f"  {part.label}: \"{_snippet(part.text, wanted)}\"\n"
                f"    {self._read_call(document, part)}"
            )
        return "\n".join(lines)


def _count(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for term in _terms(text):
        counts[term] = counts.get(term, 0) + 1
    return counts


def _snippet(text: str, wanted: list[str], width: int = 160) -> str:
    best, best_hits = "", -1
    for line in text.splitlines():
        hits = len(set(_terms(line)) & set(wanted))
        if hits > best_hits:
            best, best_hits = line, hits
    best = " ".join(best.split())
    return best if len(best) <= width else best[: width - 1].rstrip() + "…"
