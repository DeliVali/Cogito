"""The shape of the graph, and how a journal event becomes part of it.

One vocabulary covers the app's own context (sessions, messages, tool calls), the code
graph from tree-sitter, and the prose graph from Cogito Estella. Estella creates its own
Entity/Literal constraints through its ``ensure_schema``; they are not duplicated here.

Every projection is idempotent: replaying the journal once or five times must give the
same graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cogito.graph.wal import Event

#: Run once per database. Idempotent by construction.
CONSTRAINTS: tuple[str, ...] = (
    "CREATE CONSTRAINT cogito_saved_web_id IF NOT EXISTS "
    "FOR (w:SavedWebResult) REQUIRE w.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_project_id IF NOT EXISTS "
    "FOR (p:Project) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_project_root IF NOT EXISTS "
    "FOR (p:Project) REQUIRE p.root IS UNIQUE",
    "CREATE CONSTRAINT cogito_session_id IF NOT EXISTS "
    "FOR (s:Session) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_message_id IF NOT EXISTS "
    "FOR (m:Message) REQUIRE m.id IS UNIQUE",
    # Still unique: a ToolCall's `id` joins its message's id and the provider's call id
    # (`_TOOL_CALLED`). The provider's own id is `call_id`.
    "CREATE CONSTRAINT cogito_toolcall_id IF NOT EXISTS "
    "FOR (t:ToolCall) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_model_context_id IF NOT EXISTS "
    "FOR (c:ModelContext) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_artifact_id IF NOT EXISTS "
    "FOR (a:Artifact) REQUIRE a.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_file_path IF NOT EXISTS "
    "FOR (f:File) REQUIRE f.path IS UNIQUE",
    "CREATE CONSTRAINT cogito_symbol_identity IF NOT EXISTS "
    "FOR (s:Symbol) REQUIRE (s.file, s.name, s.line) IS UNIQUE",
    "CREATE CONSTRAINT cogito_undo_key IF NOT EXISTS "
    "FOR (u:UndoEntry) REQUIRE u.key IS UNIQUE",
    "CREATE CONSTRAINT cogito_rewind_id IF NOT EXISTS "
    "FOR (r:Rewind) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_build_gate_run_id IF NOT EXISTS "
    "FOR (g:BuildGateRun) REQUIRE g.id IS UNIQUE",
    "CREATE CONSTRAINT cogito_capability_resolution_id IF NOT EXISTS "
    "FOR (r:CapabilityResolution) REQUIRE r.id IS UNIQUE",
)

INDEXES: tuple[str, ...] = (
    "CREATE FULLTEXT INDEX cogito_saved_web_text IF NOT EXISTS "
    "FOR (w:SavedWebResult) ON EACH [w.scope, w.title, w.snippet]",
    # The transcript is walked by position far more often than by id.
    "CREATE INDEX cogito_message_position IF NOT EXISTS "
    "FOR (m:Message) ON (m.session_id, m.seq)",
    "CREATE INDEX cogito_session_created IF NOT EXISTS "
    "FOR (s:Session) ON (s.created_at)",
    "CREATE INDEX cogito_toolcall_name IF NOT EXISTS FOR (t:ToolCall) ON (t.name)",
    "CREATE INDEX cogito_model_context_session IF NOT EXISTS "
    "FOR (c:ModelContext) ON (c.session_id, c.transcript_seq)",
    "CREATE INDEX cogito_symbol_name IF NOT EXISTS FOR (s:Symbol) ON (s.name)",
    # What `recall` falls back to when Estella's scorer is not confident enough.
    "CREATE FULLTEXT INDEX cogito_message_text IF NOT EXISTS "
    "FOR (m:Message) ON EACH [m.text]",
)

_PROJECT_OPENED = (
    """
    MERGE (p:Project {id: $id})
    SET p.root = $root, p.name = $name, p.created_at = coalesce(p.created_at, $at)
    """,
)

_SESSION_STARTED = (
    """
    MATCH (p:Project {id: $project_id})
    MERGE (s:Session {id: $id})
    SET s.title = $title, s.model_id = $model_id,
        s.sampling_seed = $sampling_seed,
        s.prompt_time = $prompt_time,
        s.build_gate = $build_gate,
        s.created_at = coalesce(s.created_at, $at)
    MERGE (p)-[:HAS_SESSION]->(s)
    """,
)

_MESSAGE_APPENDED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (m:Message {id: $id})
    SET m.session_id = $session_id, m.seq = $seq, m.role = $role, m.text = $text,
        m.tokens = $tokens, m.step = $step, m.created_at = coalesce(m.created_at, $at),
        m.continued = $continued
    FOREACH (_ IN CASE WHEN $seq = 0 THEN [1] ELSE [] END | MERGE (s)-[:FIRST]->(m))
    WITH m
    OPTIONAL MATCH (previous:Message {session_id: $session_id, seq: $seq - 1})
    FOREACH (p IN CASE WHEN previous IS NULL THEN [] ELSE [previous] END |
        MERGE (p)-[:NEXT]->(m))
    """,
    # A rewind already in the graph may have hidden this message. A point from an earlier
    # opening hides messages another journal holds, and journals reach the graph in any
    # order (`cogito graph rebuild` goes by file name; a journal written while the graph
    # was down catches up later). The message takes the mark of a standing rewind that
    # lists it, so the hidden set comes out the same whatever the order.
    """
    MATCH (m:Message {id: $id})
    OPTIONAL MATCH (r:Rewind)
    WHERE $id IN coalesce(r.hidden_ids, []) AND r.conversation_restored_at IS NULL
    WITH m, r ORDER BY r.created_at DESC LIMIT 1
    FOREACH (x IN CASE WHEN r IS NULL THEN [] ELSE [r] END | SET m.rewound_by = x.id)
    """,
)

#: Audit evidence only. Never a Message, so neither the transcript nor recall's full-text
#: index can feed harness status back to the model as conversation history.
_MODEL_CONTEXT_RECORDED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (c:ModelContext {id: $id})
    SET c.session_id = $session_id, c.request_id = $request_id, c.model_id = $model_id,
        c.step = $step, c.attempt = $attempt, c.transcript_seq = $transcript_seq,
        c.message_index = $message_index, c.volatile_text = $volatile_text,
        c.sampling_seed = $sampling_seed,
        c.prompt_time = $prompt_time,
        c.created_at = coalesce(c.created_at, $at)
    MERGE (s)-[:RECORDED]->(c)
    """,
)

#: A call belongs to the message that made it. A provider may reuse call ids ("call_0" on
#: every step), so the node's `id` joins the message's id and the call's, and the call's
#: own id is kept as `call_id`. Two messages' calls are never one node, and the uniqueness
#: constraint on `id` is unchanged, so a database an older Cogito shares needs no schema
#: change. (Nodes an older projection made keep the call's bare id.)
_TOOL_CALLED = (
    """
    MATCH (m:Message {id: $message_id})
    MERGE (t:ToolCall {id: $message_id + ':' + $id})
    SET t += $properties, t.call_id = $id, t.message_id = $message_id,
        t.created_at = coalesce(t.created_at, $at)
    MERGE (m)-[:INVOKED]->(t)
    """,
)

_ARTIFACT_WRITTEN = (
    """
    MERGE (a:Artifact {id: $id})
    SET a += $properties, a.created_at = coalesce(a.created_at, $at)
    WITH a
    OPTIONAL MATCH (t:ToolCall {id: coalesce($message_id + ':' + $tool_call_id, $tool_call_id)})
    FOREACH (call IN CASE WHEN t IS NULL THEN [] ELSE [t] END |
        MERGE (call)-[:PRODUCED]->(a))
    """,
)

_FILE_TOUCHED = (
    """
    MATCH (t:ToolCall {id: coalesce($message_id + ':' + $tool_call_id, $tool_call_id)})
    MERGE (f:File {path: $path})
    MERGE (t)-[:TOUCHED]->(f)
    """,
)

#: Compaction closes the parent and seeds a child. History is never rewritten: the old
#: messages stay reachable through the lineage, so compacting does not mean forgetting. The
#: child keeps how many of its first messages are copies (`copies`), which the thread reads
#: instead of comparing texts: a copy can be written again, offloaded, after the fold.
_SESSION_COMPACTED = (
    """
    MATCH (parent:Session {id: $parent_id})
    MATCH (child:Session {id: $child_id})
    MERGE (parent)-[:COMPACTED_TO]->(child)
    SET parent.ended_at = $at, parent.close_reason = 'compacted', child.copies = $copies
    """,
    """
    MATCH (child:Session {id: $child_id})
    MATCH (summary:Message {id: $summary_message_id})
    MERGE (child)-[:SEEDED_BY]->(summary)
    """,
)

_SESSION_DELEGATED = (
    """
    MATCH (parent:Session {id: $parent_id})
    MATCH (child:Session {id: $child_id})
    MERGE (parent)-[:DELEGATED_TO]->(child)
    """,
)

_SESSION_ENDED = (
    """
    MATCH (s:Session {id: $id})
    SET s.ended_at = $at, s.close_reason = $close_reason
    """,
)

#: The app reopened the project and the conversation continues in this session, the live
#: end of its compaction lineage (`graph.resume`). Marks it open and records when. Only a
#: leaf is ever resumed, so a fold's ``close_reason = 'compacted'`` is never cleared.
_SESSION_RESUMED = (
    """
    MATCH (s:Session {id: $id})
    SET s.resumed_at = $at, s.ended_at = NULL, s.close_reason = NULL
    """,
)

#: How a turn ended, stored on the message that opened it: the stop reason, and the outcome
#: a later fold's handover names it by. A restarted app reads these back from the journal
#: (`graph.resume`) to rebuild what each instruction came to, without asking the model.
#: ``turn_cut`` is set only alongside ``stop_reason: "output_limit"`` ("call" | "text",
#: `loop.engine.TurnResult.cut`); history reads it to offer the right «Continuar».
_TURN_ENDED = (
    """
    MATCH (m:Message {id: $message_id})
    SET m.stop_reason = $stop_reason, m.turn_outcome = $outcome, m.turn_failure = $failure,
        m.turn_cut = $cut
    """,
)

#: The todo list is replaced wholesale. It is small, and a diff would cost more code than
#: it saves.
_TODO_SET = (
    """
    MATCH (s:Session {id: $session_id})-[r:HAS_TODO]->(old:Todo)
    DELETE r, old
    """,
    """
    MATCH (s:Session {id: $session_id})
    UNWIND range(0, size($items) - 1) AS position
    WITH s, position, $items[position] AS item
    CREATE (t:Todo {id: $session_id + ':' + toString(position),
                    seq: position, text: item.text, status: item.status})
    MERGE (s)-[:HAS_TODO]->(t)
    """,
)

_APPROVAL_RECORDED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (a:Approval {id: $id})
    SET a.kind = $kind, a.target = $target, a.decision = $decision,
        a.scope = $scope, a.created_at = $at
    MERGE (s)-[:RECORDED]->(a)
    """,
)

_UNDO_RECORDED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (u:UndoEntry {key: $key})
    SET u.kind = $kind, u.original_path = $original_path, u.trash_path = $trash_path,
        u.content_hash = $content_hash, u.after_hash = $after_hash, u.created_at = $at
    MERGE (s)-[:RECORDED]->(u)
    """,
)

_UNDO_RESTORED = (
    """
    MATCH (u:UndoEntry {key: $key})
    SET u.restored_at = $at
    """,
)

#: One rewind (or its redo) touches any number of paths. The lists are index-aligned rather
#: than a list of maps because Neo4j properties are flat; `graph.rewind.load_rewind` zips
#: them back. `kept_paths` were left alone (conflicts the user chose to keep).
#: `forced_paths` were conflicts restored anyway, after their live content was preserved.
#: `failed_paths`/`failed_reasons` (also index-aligned) are paths this rewind could not
#: touch. Every path is tried on its own and the outcome is recorded even when the
#: operation is partial (strict review item 4).
_REWIND_PERFORMED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (r:Rewind {id: $id})
    SET r.point_id = $point_id, r.restored_paths = $restored_paths,
        r.restored_keys = $restored_keys, r.kept_paths = $kept_paths,
        r.forced_paths = $forced_paths, r.failed_paths = $failed_paths,
        r.failed_reasons = $failed_reasons, r.created_at = coalesce(r.created_at, $at)
    MERGE (s)-[:RECORDED]->(r)
    """,
)

#: Marks a `:Rewind` as redone. `graph.rewind.load_rewind` reads `redone_at` back so
#: `guard.rewind.redo` can refuse a second redo (strict review item 1: single-use redo).
#: `coalesce` makes a duplicate delivery of the event idempotent, like every other
#: timestamp in this module.
_REDO_PERFORMED = (
    """
    MATCH (r:Rewind {id: $rewind_id})
    SET r.redone_at = coalesce(r.redone_at, $at)
    """,
)

#: One event per row the build gate ran this round, or one with `language = ''` and
#: `outcome = 'skipped_no_edits'` when it did not run (`loop.engine._maybe_run_build_gate`,
#: `tools/build.py`). Audit evidence like `_MODEL_CONTEXT_RECORDED`: never a Message, so it
#: cannot re-enter the model's context through the transcript or `recall`'s full-text index.
_BUILD_GATE_RUN = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (g:BuildGateRun {id: $id})
    SET g.round = $round, g.language = $language, g.command = $command,
        g.targets = $targets, g.exit_status = $exit_status, g.errors = $errors,
        g.ignored = $ignored, g.duration_ms = $duration_ms,
        g.outcome = $outcome, g.detail = $detail,
        g.gate_enabled = $gate_enabled, g.hash_capped = $hash_capped,
        g.hash_large_skipped = $hash_large_skipped,
        g.created_at = coalesce(g.created_at, $at)
    MERGE (s)-[:RAN_GATE]->(g)
    """,
)

#: One event per call to `setup.resolver.CapabilityResolver.need`: which capability, who
#: asked (scan, cascade or feature), the outcome and its reason, and whether it came from
#: the session's cache. Audit evidence like `_BUILD_GATE_RUN`: never a Message, so a missing
#: compiler or a proposal cannot re-enter the model's context through the transcript or
#: `recall`'s full-text index.
_CAPABILITY_RESOLVED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (r:CapabilityResolution {id: $id})
    SET r.capability = $capability, r.project = $project, r.caller = $caller,
        r.outcome = $outcome, r.component = $component, r.reason = $reason,
        r.cached = $cached, r.created_at = coalesce(r.created_at, $at)
    MERGE (s)-[:RESOLVED]->(r)
    """,
)

#: A decision on Cogito's own budget (`provider.budget`, journalled by `loop.engine`):
#: continue the turn, raise the budget, or stop. Harness control, never a Message, for the
#: same reason as `_BUILD_GATE_RUN`. ``after_seq`` places it in the thread right after that
#: message of its session. The label gets no constraint: a database an older Cogito shares
#: keeps the schema it had, and a session holds only a handful of these.
_BUDGET_DECIDED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (d:BudgetDecision {id: $id})
    SET d.session_id = $session_id, d.after_seq = $after_seq, d.decision = $decision,
        d.reason = $reason, d.period = $period, d.limit = $limit, d.spent = $spent,
        d.currency = $currency, d.created_at = coalesce(d.created_at, $at)
    MERGE (s)-[:RECORDED]->(d)
    """,
)

#: A rewind took the conversation back to a point (`graph.resume`, "Volver a este punto").
#: Messages from the point on are hidden (marked, never deleted). The rewind's node, the
#: same `:Rewind` its files were restored under, records where in its session it happened
#: (`after_seq`) and which messages it hid, so the history can place its marker and a redo
#: can bring back exactly those.
_CONVERSATION_REWOUND = (
    """
    MATCH (m:Message) WHERE m.id IN $hidden
    SET m.rewound_by = $id
    """,
    """
    MATCH (s:Session {id: $session_id})
    MERGE (r:Rewind {id: $id})
    SET r.point_id = $point_id, r.after_seq = $after_seq, r.hidden_ids = $hidden,
        r.created_at = coalesce(r.created_at, $at)
    MERGE (s)-[:RECORDED]->(r)
    """,
    # Back to a point in another opening of the project: the conversation continues from
    # that session, and `recall` follows the edge into it (`Recall.lineage`).
    """
    MATCH (s:Session {id: $session_id})
    OPTIONAL MATCH (p:Session {id: $continues_from})
    FOREACH (x IN CASE WHEN p IS NULL OR p = s THEN [] ELSE [p] END |
        MERGE (s)-[:CONTINUES_FROM {rewind_id: $id}]->(x))
    """,
)

#: A redo brought a rewound conversation back: only what that rewind hid.
_CONVERSATION_RESTORED = (
    """
    MATCH (r:Rewind {id: $id})
    UNWIND coalesce(r.hidden_ids, []) AS hidden
    MATCH (m:Message {id: hidden}) WHERE m.rewound_by = $id
    REMOVE m.rewound_by
    """,
    """
    MATCH (r:Rewind {id: $id})
    SET r.conversation_restored_at = coalesce(r.conversation_restored_at, $at)
    """,
    """
    MATCH (:Session)-[c:CONTINUES_FROM {rewind_id: $id}]->(:Session)
    DELETE c
    """,
)

_WEB_RESULTS_SAVED = (
    """
    MATCH (p:Project {id: $project_id})
    UNWIND $results AS result
    MERGE (w:SavedWebResult {id: result.id})
    MERGE (p)-[:SAVED_WEB_RESULT]->(w)
    WITH w, result
    WHERE w.observed_at IS NULL OR w.observed_at <= $at
    SET w.project_id = $project_id, w.scope = result.scope, w.url = result.url,
        w.host = result.host, w.title = result.title, w.snippet = result.snippet,
        w.credit = result.credit, w.observed_at = $at
    """,
    # Derived address catalog only: conversation evidence and its journal stay intact.
    # Thirty days / 512 entries are conservative product defaults, not measured optima.
    """
    MATCH (w:SavedWebResult {project_id: $project_id})
    WHERE datetime(w.observed_at) < datetime($at) - duration('P30D')
    DETACH DELETE w
    """,
    """
    MATCH (w:SavedWebResult {project_id: $project_id})
    WITH w ORDER BY w.observed_at DESC, w.id DESC SKIP 512
    DETACH DELETE w
    """,
)

#: A long document the conversation holds (`graph.documents`, only with ``[estella]
#: documents`` on, or offloaded content in the app). One node per document, keyed by the
#: SHA-256 of its bytes, linked to the message that attached it, whose `read` opened it, or
#: that was offloaded. Like `BudgetDecision`, the label gets
#: no constraint, so a database an older Cogito shares keeps its schema.
_DOCUMENT_SEEN = (
    """
    MERGE (d:Document {sha256: $sha256})
    SET d.kind = $kind, d.name = $name, d.describe = $describe, d.pages = $pages,
        d.sheets = $sheets, d.parts = $parts, d.created_at = coalesce(d.created_at, $at)
    """,
    """
    MATCH (d:Document {sha256: $sha256})
    MATCH (m:Message {id: $message_id})
    FOREACH (_ IN CASE WHEN $via = 'attachment' THEN [1] ELSE [] END |
        MERGE (m)-[:ATTACHED]->(d))
    FOREACH (_ IN CASE WHEN $via = 'read' THEN [1] ELSE [] END |
        MERGE (m)-[:READ_DOCUMENT]->(d))
    FOREACH (_ IN CASE WHEN $via = 'offload' THEN [1] ELSE [] END |
        MERGE (m)-[:OFFLOADED]->(d))
    """,
)

#: Records that every part of a document is in the documents corpus of Estella `$estella`.
_DOCUMENT_INDEXED = (
    """
    MATCH (d:Document {sha256: $sha256})
    SET d.indexed_by = CASE
            WHEN $estella IN coalesce(d.indexed_by, []) THEN d.indexed_by
            ELSE coalesce(d.indexed_by, []) + $estella
        END,
        d.indexed_at = coalesce(d.indexed_at, $at)
    """,
)

#: A value the literal store dropped to stay under its budget (`context.literals`). A
#: silent drop is the failure that store exists to prevent, so every one is kept. Audit
#: evidence like `_BUILD_GATE_RUN`: never a Message, so it cannot re-enter the model's
#: context. Keyed by the journal event's id, so a replay merges the same node.
_LITERAL_EVICTED = (
    """
    MATCH (s:Session {id: $session_id})
    MERGE (e:LiteralEviction {id: $id})
    SET e.session_id = $session_id, e.text = $text, e.tool = $tool, e.step = $step,
        e.kind = $kind, e.budget_tokens = $budget_tokens, e.remaining = $remaining,
        e.created_at = coalesce(e.created_at, $at)
    MERGE (s)-[:EVICTED]->(e)
    """,
)

#: event kind -> the statements that project it, run together in one transaction.
PROJECTIONS: Mapping[str, tuple[str, ...]] = {
    "project_opened": _PROJECT_OPENED,
    "session_started": _SESSION_STARTED,
    "message_appended": _MESSAGE_APPENDED,
    "model_context_recorded": _MODEL_CONTEXT_RECORDED,
    "tool_called": _TOOL_CALLED,
    "artifact_written": _ARTIFACT_WRITTEN,
    "file_touched": _FILE_TOUCHED,
    "session_compacted": _SESSION_COMPACTED,
    "session_delegated": _SESSION_DELEGATED,
    "session_ended": _SESSION_ENDED,
    "session_resumed": _SESSION_RESUMED,
    "turn_ended": _TURN_ENDED,
    "todo_set": _TODO_SET,
    "approval_recorded": _APPROVAL_RECORDED,
    "undo_recorded": _UNDO_RECORDED,
    "undo_restored": _UNDO_RESTORED,
    "rewind_performed": _REWIND_PERFORMED,
    "redo_performed": _REDO_PERFORMED,
    "conversation_rewound": _CONVERSATION_REWOUND,
    "conversation_restored": _CONVERSATION_RESTORED,
    "build_gate_run": _BUILD_GATE_RUN,
    "capability_resolved": _CAPABILITY_RESOLVED,
    "budget_decided": _BUDGET_DECIDED,
    "document_seen": _DOCUMENT_SEEN,
    "web_results_saved": _WEB_RESULTS_SAVED,
    "document_indexed": _DOCUMENT_INDEXED,
    "literal_evicted": _LITERAL_EVICTED,
}


class UnknownEventKind(KeyError):
    """A journal entry no projection knows how to apply."""


def statements_for(event: Event) -> tuple[str, ...]:
    try:
        return PROJECTIONS[event.kind]
    except KeyError as exc:
        raise UnknownEventKind(
            f"no projection for event kind {event.kind!r}; "
            f"known: {', '.join(sorted(PROJECTIONS))}"
        ) from exc


def parameters_for(event: Event) -> dict[str, Any]:
    """Journal payload plus the event's own timestamp, which every projection may use."""
    parameters = {**dict(event.data), "at": event.at}
    if event.kind == "turn_ended":
        # Journals before the failure's kind was kept.
        parameters.setdefault("failure", None)
        # Journals before the output-limit call/text distinction existed.
        parameters.setdefault("cut", None)
    if event.kind in {"session_started", "model_context_recorded"}:
        # Old journals predate reproducible sampling. They stay replayable and record what
        # is known: no seed, rather than an invented one.
        parameters.setdefault("sampling_seed", None)
        parameters.setdefault("prompt_time", None)
    if event.kind == "undo_recorded":
        # Old journals predate the after-write hash that rewind's conflict check needs.
        # Replay leaves it empty rather than inventing a hash nothing computed.
        parameters.setdefault("after_hash", "")
    if event.kind == "rewind_performed":
        # Old journals predate per-path failure reporting (strict review item 4). Replay
        # says nothing failed, which is what happened before the field existed.
        parameters.setdefault("failed_paths", [])
        parameters.setdefault("failed_reasons", [])
    if event.kind in {"artifact_written", "file_touched"}:
        # Old journals name only the call's id, and their calls' nodes carry it bare.
        parameters.setdefault("message_id", None)
    if event.kind == "session_started":
        # Old journals predate the build gate. None, not a guessed default: the setting a
        # pre-gate session ran under cannot be known.
        parameters.setdefault("build_gate", None)
    if event.kind == "conversation_rewound":
        # Old journals predate a rewind saying where the conversation continues from: none.
        parameters.setdefault("continues_from", None)
    if event.kind == "document_seen":
        parameters.setdefault("message_id", None)
    if event.kind == "session_compacted":
        # Old journals predate the count of a fold's copies; the history then finds them.
        parameters.setdefault("copies", None)
    if event.kind == "literal_evicted":
        # The journal event's own id keys the node. Journals before this projection carry
        # no session; their records match no session and project nothing.
        parameters.setdefault("id", event.id)
        parameters.setdefault("session_id", None)
    if event.kind == "message_appended":
        # A message's blocks stay in the journal and are not projected. No graph query
        # reads them: `recall`, the full-text index and the history read `text`. The one
        # reader that needs them, a restarted app rebuilding the model's transcript
        # (`graph.resume`), reads the journal, which is written first and is complete even
        # when the graph is behind or down. Projecting them would store every message body
        # twice more for no query.
        parameters.pop("blocks", None)
        parameters.pop("blocks_version", None)
        # APP-ONLY auto-continue (`BudgetSettings.max_auto_continuations`): `Engine._append`
        # only sends `continued` when true (sparse, like `point`); a journal from before
        # this existed, and every other message, project as not a continuation.
        parameters.setdefault("continued", False)
    return parameters
