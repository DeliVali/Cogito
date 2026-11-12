"""Large documents in the conversation, indexed by Cogito Estella in the background.

A document the person attaches, or one `read` opens, can be far longer than any window: a
300-page report is about a million characters. Paging through it costs the model a window
per three hundred lines. Instead, the harness sends the text to Estella page by page, off
the turn, and answers questions about it with the page each answer came from
(`graph.recall.DocumentKnowledge`, the `ask_document` tool).

Everything here is behind ``[estella] documents``, off until CONTRIBUTING §7 accepts it,
or, in the desktop app only, ``[estella] documents_in_app`` for offloaded content (below).
Without either nothing in this module is built and no request changes.

**Corpus.** Documents stay out of the project's prose corpus (`estella.DOCS`). Estella ranks
by IDF over the whole sentence universe, so a 300-page PDF there would bury the README `ask`
answers from and change what `ask` was measured on. This is the same reason `ask` and
`recall` keep two graphs (`graph.estella`, the note on `DOCS`). Documents get their own
corpus, `documents/<estella version>`, so a new Estella builds a new graph instead of
answering from one an older extractor made.

**Never twice.** Each part (a page, a slide, a sheet, or a run of lines when the document
has none, see `tools.documents.units`) goes to Estella under a source named by the SHA-256
of its text, ``part:<hash>``. A ledger in the corpus directory records which parts, and
which documents (by the SHA-256 of their bytes), it holds. The same document is never
ingested twice under the same Estella, and a document saved again with a few pages changed
sends only those pages. Estella 0.17.0 (`GraphStore.ingest_text`) dedupes an unchanged
source, but only by source name and only for a whole source: it cannot add or remove one
page of a source. That is why the unit of ingestion is the page.

**Provenance.** Estella names the source of every sentence it quotes and, through
`provenance`, of every fact. A source is a part, and a part knows its page and the lines
`read` shows it at, so every line of an answer is labelled with its page.

**Graph.** Each document is a `:Document {sha256}` node, linked to the message that
attached it (``ATTACHED``), whose `read` opened it (``READ_DOCUMENT``), or that was too large
for the window and was offloaded (``OFFLOADED``). It is journalled like every other event
(`graph.schema`), so it is rebuilt from the journal too.

**In the app** (``[estella] documents_in_app``) only offloaded content is offered, and the
index is given ``busy``: parts then go to Estella one at a time, and only while no turn
runs. Estella shares the CPU with the model, which may decode on it too, so a turn never
competes with more than the one part already in. The exception is a document a tool waits
on (`hurry`, `web_fetch` with a question): the model is not decoding while it waits, so
those parts go in at once, ahead of the rest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: The ledger's own file in the corpus directory, beside Estella's graph.json.
LEDGER_NAME = "cogito-ingested.json"
_LEDGER_FORMAT = 1
#: How often the feeder looks again whether a turn still runs, or a part is done.
_FEED_POLL_SECONDS = 0.25


class Ingester(Protocol):
    """What indexing needs from Estella. `estella.EstellaClient` and `NullEstella` provide it."""

    ready: bool
    failure: str

    def submit(
        self, text: str, source: str, done: Callable[[bool], None] | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Part:
    """One part of a document as Estella holds it."""

    label: str
    first_line: int
    last_line: int
    text: str

    @property
    def source(self) -> str:
        """The name Estella keeps it under. It is the text's hash, so the same page is one
        source in every document and every version of one."""
        return "part:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class IndexedDocument:
    """A document the conversation holds, as the index knows it."""

    sha256: str
    kind: str
    #: "PDF, 300 pages", as `read` and the attachment chip say it.
    describe: str
    name: str
    path: str
    pages: int | None
    sheets: tuple[str, ...]
    parts: tuple[Part, ...]
    #: Lines of its text, as `read` numbers them.
    lines: int
    _by_source: dict[str, list[Part]] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        for part in self.parts:
            self._by_source.setdefault(part.source, []).append(part)

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(self._by_source)

    def parts_of(self, source: str) -> list[Part]:
        return self._by_source.get(source, [])

    @property
    def chars(self) -> int:
        return sum(len(part.text) for part in self.parts)

    def short(self) -> str:
        return f"{self.name} ({self.describe})"


class _Ledger:
    """Which parts and which documents the corpus holds, on disk next to the corpus.

    Written atomically and merged on every write, so two processes on one corpus only ever
    add to it. Discarded when Estella's graph file is gone: a ledger naming parts the graph
    no longer holds would stop them from ever being ingested again.
    """

    def __init__(self, directory: Path, estella_version: str) -> None:
        self._path = directory / LEDGER_NAME
        self._graph = directory / "graph.json"
        self._version = estella_version
        self._lock = threading.Lock()
        self.parts: set[str] = set()
        self.documents: set[str] = set()
        self._load()

    def _load(self) -> None:
        self.parts, self.documents = self._on_disk()

    def _on_disk(self) -> tuple[set[str], set[str]]:
        if not self._graph.exists():
            return set(), set()
        try:
            body = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set(), set()
        if (
            not isinstance(body, dict)
            or body.get("format") != _LEDGER_FORMAT
            or body.get("estella") != self._version
        ):
            return set(), set()
        parts, documents = body.get("parts"), body.get("documents")
        return (
            {p for p in parts if isinstance(p, str)} if isinstance(parts, list) else set(),
            {d for d in documents if isinstance(d, str)} if isinstance(documents, list) else set(),
        )

    def add(self, *, part: str | None = None, document: str | None = None) -> None:
        with self._lock:
            if part is not None:
                self.parts.add(part)
            if document is not None:
                self.documents.add(document)
            # Keep what another process added since this one last read the file.
            parts, documents = self._on_disk()
            self.parts |= parts
            self.documents |= documents
            body = {
                "format": _LEDGER_FORMAT,
                "estella": self._version,
                "parts": sorted(self.parts),
                "documents": sorted(self.documents),
            }
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                handle, temporary = tempfile.mkstemp(
                    prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
                )
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    json.dump(body, out)
                os.replace(temporary, self._path)
            except OSError as exc:
                log.debug("documents: ledger not written: %s", exc)


class DocumentIndex:
    """The large documents this session holds, and their ingestion into Estella.

    ``connect`` builds the Estella client the first time a document needs it, so a session
    that never meets a large document never starts one. ``record`` is the store's.
    """

    def __init__(
        self,
        connect: Callable[[], Ingester],
        directory: Path,
        *,
        record: Callable[..., Any],
        estella_version: str,
        min_chars: int,
        busy: Callable[[], bool] | None = None,
    ) -> None:
        self._connect = connect
        #: True while a turn runs. Given, parts are fed one at a time between turns
        #: (`_feed`); None sends every part at once, as the index always did.
        self._busy = busy
        self._queue: deque[tuple[IndexedDocument, str, Part, int]] = deque()
        #: Documents a tool is waiting on (`hurry`): their parts are fed even while a turn
        #: runs, ahead of the rest.
        self._urgent: set[str] = set()
        self._feeder: threading.Thread | None = None
        self._in_flight = 0
        self._client: Ingester | None = None
        self._directory = directory
        self._record = record
        self.estella_version = estella_version
        self._min_chars = min_chars
        self._ledger: _Ledger | None = None
        self._lock = threading.Lock()
        self._documents: dict[str, IndexedDocument] = {}
        self._pending: dict[str, set[str]] = {}
        self._seen: set[tuple[str, str, str]] = set()
        self._changed = threading.Condition(self._lock)
        #: Bumped whenever the set of documents changes, so a tool can tell its own
        #: description is stale without comparing lists.
        self.generation = 0

    # -- what the conversation holds ----------------------------------------------------

    @property
    def client(self) -> Ingester:
        with self._lock:
            if self._client is None:
                self._client = self._connect()
            return self._client

    def documents(self) -> list[IndexedDocument]:
        """Most recently offered last."""
        with self._lock:
            return list(self._documents.values())

    def find(self, wanted: str | None) -> IndexedDocument | None:
        """The document ``wanted`` names (its name, its path, or part of either; the most
        recent match wins), or the one most recently attached or read if nothing is named."""
        documents = self.documents()
        if not documents:
            return None
        if not wanted or not wanted.strip():
            return documents[-1]
        needle = wanted.strip().lower()
        for document in reversed(documents):
            if needle in (document.name.lower(), document.path.lower()):
                return document
        matches = [
            d for d in documents if needle in d.name.lower() or needle in d.path.lower()
        ]
        return matches[-1] if matches else None

    def pending(self, document: IndexedDocument) -> int:
        """Parts of ``document`` not yet in Estella's graph."""
        with self._lock:
            return len(self._pending.get(document.sha256, ()))

    def ingesting(self) -> bool:
        """Whether Estella is taking in a part right now, as far as the feeder knows. An
        ask sent to it then waits behind that part (its tools run one at a time)."""
        with self._lock:
            return self._in_flight > 0

    def hurry(self, document: IndexedDocument) -> None:
        """Feed ``document``'s parts next, even while a turn runs. For a tool that waits on
        the answer: while it waits the model is not decoding, so the parts compete with
        nothing."""
        with self._lock:
            if self._pending.get(document.sha256):
                self._urgent.add(document.sha256)

    def wait(self, document: IndexedDocument, timeout: float) -> bool:
        """Block until every part of ``document`` is ingested, or ``timeout`` passes."""
        with self._changed:
            return self._changed.wait_for(
                lambda: not self._pending.get(document.sha256), timeout=timeout
            )

    # -- taking one in --------------------------------------------------------------------

    def offer(
        self,
        document: IndexedDocument,
        *,
        message_id: str | None,
        via: str,
        record: bool = True,
    ) -> bool:
        """Take in a document the conversation now holds, attached to a message (``via`` is
        ``attachment``), opened by a `read` (``read``), or too large for the window
        (``offload``), or a web page `web_fetch` saved because it did not fit its budget
        (``fetch``). Indexes it if it is large enough and returns whether it did. Content
        that was offloaded or saved is always large enough: it did not fit the window, the
        stronger test. Never waits for Estella. Without ``record`` nothing is journalled: a
        reopened conversation offers again what it already held."""
        if via not in ("offload", "fetch") and document.chars < self._min_chars:
            return False
        with self._lock:
            known = document.sha256 in self._documents
            # Most recent last: a document offered again moves to the end.
            self._documents.pop(document.sha256, None)
            self._documents[document.sha256] = document
            if not known:
                self.generation += 1
            mark = (document.sha256, message_id or "", via)
            first_sighting = mark not in self._seen
            self._seen.add(mark)
        if first_sighting and record:
            self._record(
                "document_seen",
                sha256=document.sha256,
                kind=document.kind,
                name=document.name,
                describe=document.describe,
                pages=document.pages,
                sheets=list(document.sheets),
                parts=len(document.parts),
                message_id=message_id,
                via=via,
            )
        if not known:
            self._ingest(document)
        return True

    def _ledger_now(self) -> _Ledger:
        if self._ledger is None:
            self._ledger = _Ledger(self._directory, self.estella_version)
        return self._ledger

    def _ingest(self, document: IndexedDocument) -> None:
        ledger = self._ledger_now()
        if document.sha256 in ledger.documents:
            return
        todo = [
            (source, parts[0])
            for source, parts in ((s, document.parts_of(s)) for s in sorted(document.sources))
            if source not in ledger.parts and parts[0].text.strip()
        ]
        with self._lock:
            self._pending[document.sha256] = {source for source, _ in todo}
        if not todo:
            self._finish(document, sent=0)
            return
        # In page order, so the first pages are answerable first.
        todo.sort(key=lambda item: item[1].first_line)
        if self._busy is not None:
            with self._lock:
                self._queue.extend((document, source, part, len(todo)) for source, part in todo)
                if self._feeder is None:
                    self._feeder = threading.Thread(
                        target=self._feed, name="cogito-documents-feed", daemon=True
                    )
                    self._feeder.start()
            return
        client = self.client
        for source, part in todo:
            client.submit(part.text, source, done=self._done(document, source, len(todo)))

    def _feed(self) -> None:
        """Hand Estella the queued parts one at a time, each only while no turn runs (a
        document a tool waits on, `hurry`, goes first and does not wait for the turn), until
        none is left. Stops when Estella is off or has failed: those parts stay pending, so
        `ask_document` answers with pointers."""
        while True:
            with self._lock:
                if not self._queue:
                    self._feeder = None
                    return
                at = 0
                if self._busy is not None and self._busy():
                    at = next((i for i, queued in enumerate(self._queue)
                               if queued[0].sha256 in self._urgent), -1)
                if at >= 0:
                    document, source, part, sent = self._queue[at]
            if at < 0:
                time.sleep(_FEED_POLL_SECONDS)
                continue
            client = self.client
            if getattr(client, "failure", ""):
                with self._lock:
                    self._queue.clear()
                    self._feeder = None
                return
            finished = threading.Event()
            callback = self._done(document, source, sent)
            counted = [True]

            def done(ok: bool, callback=callback, finished=finished, counted=counted) -> None:
                # Out of flight before `callback` wakes a caller in `wait`: that caller asks
                # at once, and an ingest still counted makes `indexed_answer` give up.
                with self._lock:
                    if counted[0]:
                        counted[0] = False
                        self._in_flight -= 1
                try:
                    callback(ok)
                finally:
                    finished.set()

            with self._lock:
                del self._queue[at]
                self._in_flight += 1
            try:
                client.submit(part.text, source, done=done)
                while not finished.wait(_FEED_POLL_SECONDS):
                    if getattr(client, "failure", ""):
                        break
            finally:
                with self._lock:
                    if counted[0]:
                        counted[0] = False
                        self._in_flight -= 1

    def _done(self, document: IndexedDocument, source: str, sent: int) -> Callable[[bool], None]:
        def done(ok: bool) -> None:
            if ok:
                self._ledger_now().add(part=source)
            with self._lock:
                remaining = self._pending.get(document.sha256)
                if remaining is None:
                    return
                last = remaining <= {source}
            # Record the document as indexed before its last part leaves `pending`, so a
            # caller in `wait` never wakes to a state just short of finished.
            if last:
                self._finish(document, sent=sent)
            with self._changed:
                remaining.discard(source)
                if not remaining:
                    self._urgent.discard(document.sha256)
                self._changed.notify_all()

        return done

    def _finish(self, document: IndexedDocument, *, sent: int) -> None:
        ledger = self._ledger_now()
        wanted = [s for s in document.sources if self._has_text(document, s)]
        if not all(source in ledger.parts for source in wanted):
            # Estella missed a part (not running, or failed). Answer from what is there;
            # the document is offered again in a later session.
            return
        ledger.add(document=document.sha256)
        self._record(
            "document_indexed",
            sha256=document.sha256,
            estella=self.estella_version,
            parts=len(document.sources),
            ingested=sent,
        )

    @staticmethod
    def _has_text(document: IndexedDocument, source: str) -> bool:
        parts = document.parts_of(source)
        return bool(parts and parts[0].text.strip())


def indexed_document(
    *,
    sha256: str,
    kind: str,
    describe: str,
    name: str,
    path: str,
    pages: int | None,
    sheets: Sequence[str],
    units: Sequence[Any],
    lines: int,
) -> IndexedDocument:
    """An `IndexedDocument` from a converter's units (anything with ``label``,
    ``first_line``, ``last_line`` and ``text``: `tools.documents.Unit`)."""
    return IndexedDocument(
        sha256=sha256,
        kind=kind,
        describe=describe,
        name=name,
        path=path,
        pages=pages,
        sheets=tuple(sheets),
        parts=tuple(Part(u.label, u.first_line, u.last_line, u.text) for u in units),
        lines=lines,
    )
