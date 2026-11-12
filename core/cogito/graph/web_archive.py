"""Project-scoped saved search evidence, indexed by Neo4j without an external quota.

Neo4j's full-text documentation (read 2026-10-16) describes Lucene field queries and
transactional index updates. Literal terms and an indexed scope filter keep queries
inside this project's evidence before the result limit is applied.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

from neo4j.exceptions import Neo4jError

from cogito.errors import GraphUnavailable
from cogito.graph.driver import GraphDriver

log = logging.getLogger(__name__)

_SEARCH = """
CALL db.index.fulltext.queryNodes('cogito_saved_web_text', $terms)
YIELD node, score
WHERE node.project_id = $project_id
  AND datetime(node.observed_at) >= datetime($now) - duration('P30D')
  AND ($site = '' OR node.host = $site OR node.host ENDS WITH '.' + $site)
RETURN node.url AS url, node.title AS title, node.snippet AS snippet,
       node.credit AS credit, node.observed_at AS observed_at
ORDER BY score DESC, node.observed_at DESC LIMIT $limit
"""


class WebArchive:
    def __init__(self, driver: GraphDriver, project_id: str, record: Callable) -> None:
        self._driver, self._project_id, self._record = driver, project_id, record
        self._scope = hashlib.sha256(project_id.encode()).hexdigest()

    def remember(self, results: Sequence[Mapping[str, str]]) -> None:
        rows = []
        for result in results[:8]:
            url = result.get('url', '')
            try:
                parsed = urlsplit(url)
                host = parsed.hostname
            except ValueError:
                continue
            if parsed.scheme not in ('http', 'https') or not host or parsed.username:
                continue
            rows.append({
                'id': hashlib.sha256(f'{self._project_id}\n{url}'.encode()).hexdigest(),
                'url': url, 'host': host.lower(), 'scope': self._scope,
                'title': result.get('title', '')[:500],
                'snippet': result.get('snippet', '')[:2000],
                'credit': result.get('credit', '')[:1000],
            })
        if rows:
            try:
                self._record('web_results_saved', project_id=self._project_id, results=rows)
            except (OSError, GraphUnavailable, Neo4jError):
                log.warning('could not save web-result evidence')

    def search(self, query: str, site: str, limit: int = 8) -> list[dict[str, str]]:
        words = list(dict.fromkeys(re.findall(r'[^\W_]+', query.casefold())))[:12]
        if not words:
            return []
        # No user-supplied Lucene operators survive tokenization. Match every term in
        # title or snippet, rather than treating a common word as sufficient evidence.
        clauses = [f'(title:"{word}" OR snippet:"{word}")' for word in words]
        terms = f'scope:"{self._scope}" AND (' + ' AND '.join(clauses) + ')'
        try:
            return self._driver.query(
                _SEARCH, terms=terms, project_id=self._project_id, site=site,
                now=datetime.now(UTC).isoformat(),
                limit=max(1, min(8, limit)),
            )
        except (GraphUnavailable, Neo4jError):
            # Offline/populating indexes must leave normal search available.
            return []
