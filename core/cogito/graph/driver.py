"""The Neo4j connection, and the store that keeps a turn running when Neo4j is down.

The store is write-ahead: an event is journalled and fsynced before anything tries to reach
the database. If Neo4j is down the turn keeps running and the journal grows. When it comes
back, everything pending is projected in order.

Order matters because projections reference each other (a tool call attaches to its
message, a compaction to both sessions). Pending entries are flushed oldest-first and never
skipped.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from cogito.config.settings import Neo4jSettings
from cogito.errors import GraphUnavailable
from cogito.graph import schema
from cogito.graph.wal import Event, Journal, JournalLock

log = logging.getLogger(__name__)

#: How long one connection may take: the TCP connect, then the whole acquisition with its
#: Bolt handshake. The driver's defaults (30 s and 60 s, neo4j 6.x `_conf.py`) let a frozen
#: Neo4j that still accepts TCP hold a project switch for a minute per attempt (QA
#: 2026-10-11, measured 60.1 s against a socket that never answers; 6.0 s with these).
#: Neo4j here is on loopback, where a handshake takes milliseconds.
CONNECTION_TIMEOUT_SECONDS = 5.0
ACQUISITION_TIMEOUT_SECONDS = 10.0


class GraphDriver:
    """A lazily-connected Bolt driver. Connection failures raise GraphUnavailable."""

    def __init__(self, settings: Neo4jSettings, password: str | None = None) -> None:
        self._settings = settings
        self._password = password
        self._driver: Driver | None = None
        self._lock = threading.Lock()

    def _connect(self) -> Driver:
        if self._driver is not None:
            return self._driver
        with self._lock:
            if self._driver is not None:
                return self._driver
            auth = (self._settings.user, self._password) if self._password else None
            # Declared outside the try so the except can tell "never constructed" from
            # "constructed but unreachable". Only the second holds anything to close.
            driver: Driver | None = None
            try:
                driver = GraphDatabase.driver(
                    self._settings.uri,
                    auth=auth,
                    # Checking whether an optional property is set warns that the property
                    # does not exist. That is expected, and the warning is longer than the
                    # answer.
                    notifications_disabled_classifications=["UNRECOGNIZED"],
                    connection_timeout=CONNECTION_TIMEOUT_SECONDS,
                    connection_acquisition_timeout=ACQUISITION_TIMEOUT_SECONDS,
                )
                driver.verify_connectivity()
            except (ServiceUnavailable, Neo4jError, OSError) as exc:
                # `GraphDatabase.driver()` opens a connection pool before checking
                # connectivity, so a failed `verify_connectivity()` still leaves a pool to
                # release. A failed driver is never cached in `self._driver`, so without
                # this close every call while Neo4j is down (a health check's
                # `is_available`, a `query`/`apply` retry, a polled route like `/projects`)
                # leaks one more pool.
                if driver is not None:
                    driver.close()
                raise GraphUnavailable(f"{self._settings.uri}: {exc}") from exc
            self._driver = driver
            return driver

    def is_available(self) -> bool:
        try:
            self._connect()
        except GraphUnavailable:
            return False
        return True

    def ensure_schema(self) -> None:
        """Create constraints and indexes. Idempotent; safe to call on every start."""
        driver = self._connect()
        with driver.session(database=self._settings.database) as session:
            for statement in (*schema.CONSTRAINTS, *schema.INDEXES):
                # Schema changes cannot share a transaction with each other on every
                # Neo4j version, so each runs on its own.
                session.run(statement)

    def apply(self, statements: Sequence[str], parameters: Mapping[str, Any]) -> None:
        """Run statements together in one write transaction."""
        driver = self._connect()
        try:
            with driver.session(database=self._settings.database) as session:
                session.execute_write(
                    lambda tx: [tx.run(statement, **parameters) for statement in statements]
                )
        except (ServiceUnavailable, OSError) as exc:
            self.close()
            raise GraphUnavailable(str(exc)) from exc

    def query(self, cypher: str, **parameters: Any) -> list[dict[str, Any]]:
        """Run a read query and return plain dictionaries."""
        driver = self._connect()
        try:
            with driver.session(database=self._settings.database) as session:
                return [record.data() for record in session.run(cypher, **parameters)]
        except (ServiceUnavailable, OSError) as exc:
            self.close()
            raise GraphUnavailable(str(exc)) from exc

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                self._driver.close()
                self._driver = None


class Store:
    """Journal first, graph second. The only thing the rest of the code writes through."""

    def __init__(
        self,
        journal: Journal,
        driver: GraphDriver,
        lock: JournalLock | None = None,
        *,
        degraded: bool = False,
    ) -> None:
        self._journal = journal
        self._driver = driver
        #: This writer's hold on its journal (`graph.wal.JournalLock`), let go on `close`.
        self._lock_on_journal = lock
        self._lock = threading.Lock()
        #: True from the start when the caller already found the graph unreachable.
        self._degraded = degraded
        self._stop = threading.Event()
        self._flusher: threading.Thread | None = None

    @property
    def journal(self) -> Journal:
        """The journal this store writes to first."""
        return self._journal

    @property
    def degraded(self) -> bool:
        """True when entries are journalled but not yet in the graph."""
        return self._degraded

    @property
    def pending_count(self) -> int:
        return self._journal.pending_count()

    def record(self, event_kind: str, /, **data: Any) -> Event:
        """Durably record one event, then try to project it and anything before it.

        While degraded, only the journal is written and the background flusher retries the
        graph: a graph that hangs instead of refusing would otherwise cost every event its
        connection timeout.

        The name is positional-only: several event payloads have their own `kind` field,
        and a keyword here would collide with it.
        """
        event = self._journal.append(Event(kind=event_kind, data=data))
        if not self._degraded:
            self.flush()
        return event

    def flush(self) -> int:
        """Project pending entries oldest-first. Returns how many reached the graph."""
        with self._lock:
            projected = 0
            for index, event in self._journal.pending():
                try:
                    statements = schema.statements_for(event)
                except schema.UnknownEventKind:
                    # A log written by a newer version. Skip it rather than wedging the
                    # queue forever; the checkpoint still advances.
                    log.warning("skipping unknown event kind %r", event.kind)
                    self._journal.set_checkpoint(index)
                    continue
                try:
                    self._driver.apply(statements, schema.parameters_for(event))
                except GraphUnavailable as exc:
                    if not self._degraded:
                        log.warning("graph unavailable, journalling only: %s", exc)
                    self._degraded = True
                    return projected
                self._journal.set_checkpoint(index)
                projected += 1

            if self._degraded and projected:
                log.info("graph reconnected, %d pending entries projected", projected)
            self._degraded = False
            return projected

    def rebuild(self) -> int:
        """Replay the whole journal into the graph. Projections are idempotent."""
        with self._lock:
            self._journal.set_checkpoint(-1)
        return self.flush()

    def start_background_flush(self, interval_seconds: float = 5.0) -> None:
        """Retry projection while degraded, so recovery needs no user action."""
        if self._flusher is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                if self._degraded:
                    self.flush()

        self._flusher = threading.Thread(target=loop, name="cogito-graph-flush", daemon=True)
        self._flusher.start()

    def close(self) -> None:
        self._stop.set()
        if self._flusher is not None:
            self._flusher.join(timeout=2.0)
            self._flusher = None
        self._driver.close()
        if self._lock_on_journal is not None:
            self._lock_on_journal.release()
