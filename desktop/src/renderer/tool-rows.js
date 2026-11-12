'use strict';

/**
 * Pure event -> row-model logic for `tool_start`/`tool_end` (contract
 * `app-phase-b-contract.md`; shapes checked against `core/cogito/server/app.py`). Nothing
 * here touches the DOM, so the unit suite in `cogito-lab` (`tests/desktop/`) imports it
 * from plain Node.
 *
 * A "row" is the renderer's model of one tool call, independent of how it is drawn:
 *   { callId, name, status: 'running'|'done'|'failed'|'cancelled', target, startedAt,
 *     durationMs, exitCode, added, removed, error, ok, lines, detail, matches, files,
 *     findings, denied }
 * `lines` is a read's file length. `matches`/`files` are a search's counts, `findings` the
 * build gate's errors, `denied` a call the guard or the person did not let run. `detail` is what a finished row opens onto (the edit's
 * diff, the command's output) as the core sent it (`normalizeDetail`).
 * `render-tool-rows.js` turns a list of rows into DOM.
 */

import { VERBS, DEFAULT_VERB, pathCountLabel, OUTSIDE_PROJECT_SENTINEL, OUTSIDE_PROJECT_LABEL } from './strings.js';
import { escapeControlChars } from './sanitize.js';

/** Verb pair for a tool name. Tools not in the table (`check`, `ask`, `recall`, any new
 * one) get a generic pair so the row still reads. */
function verbFor(name) {
  return VERBS[name] || DEFAULT_VERB;
}

/** A tool's name as a person reads it. An MCP server's tool is registered as
 * `server__tool` (`core/cogito/tools/mcp/hub.py`) and shown as «server · tool». */
function toolName(name) {
  const text = String(name || '');
  const split = text.indexOf('__');
  return split > 0 ? `${text.slice(0, split)} · ${text.slice(split + 2)}` : text;
}

/** Maps the core's `<outside project>` sentinel (`server/app.py`) to Spanish, either as
 * the whole string (`target.paths` in `tool_start`/`tool_end`) or as a prefix
 * (`<outside project>/<basename>` in `turn_changes.files[].path`). */
function mapOutsideProject(text) {
  if (text === OUTSIDE_PROJECT_SENTINEL) return OUTSIDE_PROJECT_LABEL;
  if (text.startsWith(`${OUTSIDE_PROJECT_SENTINEL}/`)) {
    return OUTSIDE_PROJECT_LABEL + text.slice(OUTSIDE_PROJECT_SENTINEL.length);
  }
  return text;
}

/** A path, command or pattern as shown to a person: the outside-project sentinel
 * translated, then bidi and control characters escaped. Every path, command or target that
 * reaches the page goes through here or through decisions.js's `pillFor`. */
function displayText(raw) {
  return escapeControlChars(mapOutsideProject(raw));
}

/** The site of a page address, as the address names it (an internationalised name shows
 * as `xn--…`, so a look-alike domain is not mistaken for the real one). The raw text when it
 * is not an address. */
function hostLabel(raw) {
  try {
    return new URL(raw).hostname || raw;
  } catch {
    return raw;
  }
}

/** A short label for what a call targeted, for the running (gerund) form. */
function targetLabel(target) {
  if (!target) return '';
  if (typeof target.url === 'string') return displayText(hostLabel(target.url));
  if (typeof target.command === 'string') return displayText(target.command);
  if (typeof target.pattern === 'string') return displayText(target.pattern);
  if (typeof target.query === 'string') return displayText(target.query);
  if (Array.isArray(target.paths) && target.paths.length) {
    return target.paths.length === 1
      ? displayText(target.paths[0])
      : pathCountLabel(target.paths.length);
  }
  return '';
}

/** "12 s" under a minute, "1 min 05 s" from a minute on. The design shows only the first
 * form; the minutes form is a GUESS. Used for the live elapsed-time tick and for a finished
 * non-shell call's result. */
function formatElapsed(ms) {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds} s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes} min ${String(seconds).padStart(2, '0')} s`;
}

/** "0.8 s" under a minute (one decimal, as in the design's shell example "Ejecutó npm test
 * / 0.8 s · exit 0"), "1 min 05 s" from a minute on. Used only for a finished shell call's
 * duration; the live tick and other tools use whole-second `formatElapsed`. */
function formatDuration(ms) {
  const totalSeconds = Math.max(0, ms) / 1000;
  if (totalSeconds < 60) return `${(Math.round(totalSeconds * 10) / 10).toFixed(1)} s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = Math.round(totalSeconds - minutes * 60);
  return `${minutes} min ${String(seconds).padStart(2, '0')} s`;
}

/** Two plain ids, `<session>/<artifact>`: the only reference the core's artifacts route
 * accepts (`core/cogito/server/artifacts.py`). */
const DETAIL_REF = /^[A-Za-z0-9_-]{1,64}\/[A-Za-z0-9_-]{1,64}$/;

/** `tool_end.detail` (`core/cogito/loop/detail.py`) as a row stores it, or undefined when it
 * is missing or cannot be drawn truthfully (the row then does not open). A `ref` that is
 * not two plain ids is dropped: the preview still opens, but nothing is fetched. */
function normalizeDetail(raw) {
  if (!raw || typeof raw !== 'object') return undefined;
  const { kind, text, lines, truncated, ref } = raw;
  if (kind !== 'diff' && kind !== 'output') return undefined;
  if (typeof text !== 'string' || !text) return undefined;
  if (!Number.isInteger(lines) || lines < 1) return undefined;
  const detail = { kind, text, lines, truncated: truncated === true };
  if (detail.truncated && typeof ref === 'string' && DETAIL_REF.test(ref)) detail.ref = ref;
  return detail;
}

/** Build a fresh running row from a `tool_start` event. */
function startRow(event) {
  return {
    callId: event.call_id,
    name: event.name,
    status: 'running',
    target: event.target || {},
    startedAt: event.started_at ? Date.parse(event.started_at) : Date.now(),
    durationMs: null,
    exitCode: undefined,
    added: undefined,
    removed: undefined,
    error: undefined,
    ok: undefined,
    lines: undefined,
    detail: undefined,
    matches: undefined,
    files: undefined,
    findings: undefined,
    denied: false,
    off: false,
    background: undefined,
  };
}

/** A count the core sent, or undefined. */
function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : undefined;
}

/**
 * Fold a `tool_end` event onto its prior row, or build a new row when no `tool_start` was
 * seen for this `call_id`. The build brief requires that "a `tool_end` without its
 * `tool_start` still renders."
 */
function endRow(prior, event) {
  const base = prior || {
    callId: event.call_id,
    name: event.name,
    target: event.target || {},
    startedAt: null,
  };
  return {
    ...base,
    name: event.name || base.name,
    target: event.target && Object.keys(event.target).length ? event.target : base.target,
    status: event.status || (event.ok ? 'done' : 'failed'),
    ok: event.ok,
    durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : base.durationMs,
    exitCode: event.exit_code,
    added: event.added,
    removed: event.removed,
    error: event.error,
    lines: Number.isInteger(event.lines) && event.lines >= 0 ? event.lines : undefined,
    detail: normalizeDetail(event.detail),
    matches: count(event.matches),
    files: count(event.files),
    findings: count(event.findings),
    denied: event.denied === true,
    off: event.off === true,
    background: typeof event.background === 'string' ? event.background : undefined,
    // A subagent's calls: counted live (`withSubagentCall`), or read back from the record.
    sub: count(event.calls) !== undefined ? { ...(base.sub || {}), calls: count(event.calls) } : base.sub,
  };
}

/** How many of a subagent's calls its row keeps to list; older ones are only counted. */
const SUB_STEPS_KEPT = 200;

/**
 * A `delegate` row with a subagent's call folded in (`parent_call_id`): the calls it made
 * so far, and the latest as its own row would name it («Leyendo src/x.py»), kept while the
 * subagent thinks about the next one, which is most of its time. The subagent's calls are
 * not rows of the turn: the row counts them, names the latest, and lists them when opened
 * (`sub.steps`, each a row of its own; `sub.key` is what the open list is remembered by,
 * since the row object changes with every call).
 */
function withSubagentCall(row, event) {
  const sub = row.sub || { calls: 0, now: undefined };
  // It answered; another subagent of the same step still works (`subagent_done`).
  if (event.type === 'subagent_done') return sub.done ? row : { ...row, sub: { ...sub, done: true } };
  const steps = sub.steps || [];
  if (event.type === 'tool_end') {
    // A local server numbers calls per reply, so ids repeat: the latest one still running.
    let at = -1;
    for (let i = steps.length - 1; i >= 0 && at < 0; i -= 1) {
      if (steps[i].callId === event.call_id && steps[i].status === 'running') at = i;
    }
    if (at < 0) return row;
    const next = steps.slice();
    next[at] = endRow(steps[at], event);
    return { ...row, sub: { ...sub, steps: next } };
  }
  if (event.type !== 'tool_start') return row;
  const target = targetLabel(event.target);
  const verb = verbFor(event.name);
  const name = verb === DEFAULT_VERB ? displayText(toolName(event.name)) : '';
  const object = target || name;
  const listed = [...steps, startRow(event)];
  const dropped = Math.max(0, listed.length - SUB_STEPS_KEPT);
  return {
    ...row,
    sub: {
      calls: sub.calls + 1,
      now: object ? `${verb.gerund} ${object}` : verb.gerund,
      steps: dropped ? listed.slice(dropped) : listed,
      earlier: (sub.earlier || 0) + dropped,
      key: sub.key || {},
    },
  };
}

/** What an unfinished row becomes when its turn ends (`turn_complete`/`error`) without a
 * `tool_end`: cancelled, the same label a call the person cancelled gets, so it does not
 * spin forever. No `error` text, since nothing failed. */
function closeAsInterrupted(row) {
  return { ...row, status: 'cancelled', ok: false, error: undefined };
}

/**
 * Group consecutive `read` rows that finished cleanly (`status: 'done'`) into one display
 * entry, as in design 1a ("Leyó 2 archivos"). A failed or cancelled read stays on its own,
 * since grouping would hide the failure. A running row stays on its own until it finishes.
 * Anything else passes through as a single-row entry.
 *
 * Pure: it reads the turn's ordered rows and returns a new array without changing them.
 */
function groupRows(rows) {
  const grouped = [];
  for (const row of rows) {
    const isDoneRead = row.name === 'read' && row.status === 'done';
    const last = grouped[grouped.length - 1];
    if (isDoneRead && last && last.kind === 'read-group') {
      last.rows.push(row);
      continue;
    }
    if (isDoneRead) {
      grouped.push({ kind: 'read-group', rows: [row] });
      continue;
    }
    grouped.push({ kind: 'single', row });
  }
  return grouped;
}

export {
  verbFor,
  toolName,
  hostLabel,
  targetLabel,
  displayText,
  formatElapsed,
  formatDuration,
  startRow,
  endRow,
  closeAsInterrupted,
  withSubagentCall,
  groupRows,
  normalizeDetail,
};
