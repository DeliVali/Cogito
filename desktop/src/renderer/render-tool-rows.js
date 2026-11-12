'use strict';

/**
 * DOM for tool rows (`tool-rows.js`'s row models) and the "Cambios de este turno" card
 * (`diff.js`'s parsed diffs). Kept apart from `chat.js` so the pure modules underneath
 * (`tool-rows.js`, `diff.js`, `decisions.js`, `strings.js`) stay importable from Node
 * without a DOM.
 *
 * Every element is built with `createElement`/`textContent`/`createTextNode`, never
 * `innerHTML`: a diff line, a shell target or a file path is arbitrary content the model or
 * the filesystem chose.
 *
 * Only what `tool_start`/`tool_end` carry is shown. A finished row opens (▶, a button with
 * `aria-expanded`) only onto what its call produced: an edit's or write's diff and a
 * command's output (`tool_end.detail`, `core/cogito/loop/detail.py`, drawn by
 * `render-tool-detail.js`), and a grouped read's files with each one's line count
 * (`tool_end.lines`). A call that carried none of that (an older record from history, a
 * failed edit, a command that printed nothing, a search, since no match text is sent) shows
 * its header only.
 */

import { verbFor, toolName, targetLabel, displayText, formatElapsed, formatDuration } from './tool-rows.js';
import { cancelledLabel, CANCELLED_EDIT_NOTE, CANCELLED_ICON, WAITING_APPROVAL, readGroupLabel, ROW_NOTES, DEFAULT_VERB } from './strings.js';
import { parseUnifiedDiff, renderDiffLines, visibleLineCount, COLLAPSE_AT } from './diff.js';
import { CHANGES_CARD } from './strings.js';
import { makeExpander } from './expander.js';
import { sfx } from './sfx.js';
import { buildToolDetail } from './render-tool-detail.js';
import { TOOL_DETAIL, MODEL_PICKER } from './strings.js';

/** The rows a person opened (a read group by its first row). A segment is redrawn whole
 * when a read finishes beside them, and an open row must stay open. Keyed by the row object,
 * which the redraw passes again, never by call id: a local model can reuse `call_1` in every
 * turn, and opening one must not open the others. */
const openRows = new WeakSet();

/** How a row opens and what fetches the rest of its detail: the window's sounds and
 * `window.cogito.toolDetail`, unless a caller (a test) passes its own. */
function detailOptions(options) {
  const bridge = globalThis.window && globalThis.window.cogito;
  return {
    play: options.play || sfx,
    loadDetail: options.loadDetail || (bridge && typeof bridge.toolDetail === 'function' ? bridge.toolDetail : null),
  };
}

/** `onToggle` for a row's expander: build on first open, remember it is open. */
function remember(key, ensure = () => {}) {
  return (isOpen) => {
    if (isOpen) {
      ensure();
      openRows.add(key);
    } else {
      openRows.delete(key);
    }
  };
}

/** The right-hand result as one or more coloured parts: time, `exit 0`, or `+3 −1`. A shell
 * result shows duration and exit code together ("0.8 s · exit 0"; the core always reports
 * both). An edit/write result drops a zero count instead of showing "+0" or "−0". */
function resultParts(row) {
  // Nothing ran: the time it took to say no is not a result.
  if (row.denied) return [{ text: ROW_NOTES.denied }];
  if (row.off) return [{ text: ROW_NOTES.webOff }];
  if (row.name === 'shell' && typeof row.background === 'string') {
    return [{ text: ROW_NOTES.background(row.background) }];
  }
  if (row.name === 'shell') {
    const parts = [];
    if (typeof row.durationMs === 'number') parts.push(formatDuration(row.durationMs));
    if (typeof row.exitCode === 'number') parts.push(`exit ${row.exitCode}`);
    return parts.length ? [{ text: parts.join(' · ') }] : [];
  }
  if (row.name === 'build' && row.findings) {
    return [{ text: ROW_NOTES.compileErrors(row.findings), className: 'diff-remove' }];
  }
  if (row.name === 'grep' && typeof row.matches === 'number') {
    return [{ text: row.matches ? ROW_NOTES.matches(row.matches, row.files ?? 0) : ROW_NOTES.noMatches }];
  }
  if (row.name === 'web_search' && typeof row.matches === 'number') {
    return [{ text: row.matches ? ROW_NOTES.results(row.matches) : ROW_NOTES.noResults }];
  }
  if (row.name === 'glob' && typeof row.files === 'number') {
    return [{ text: ROW_NOTES.files(row.files) }];
  }
  if (row.name === 'delegate' && row.sub && row.sub.calls) {
    const parts = [ROW_NOTES.calls(row.sub.calls)];
    if (typeof row.durationMs === 'number') parts.push(formatElapsed(row.durationMs));
    return [{ text: parts.join(' · ') }];
  }
  if (typeof row.added === 'number' || typeof row.removed === 'number') {
    const parts = [];
    if (row.added) parts.push({ text: `+${row.added}`, className: 'diff-add' });
    if (row.removed) parts.push({ text: `−${row.removed}`, className: 'diff-remove' });
    return parts;
  }
  if (typeof row.durationMs === 'number') {
    return [{ text: formatElapsed(row.durationMs) }];
  }
  return [];
}

/** The verb a finished row starts with. A call that did not do its job says so («No
 * escribió», «No se ejecutó»): refused, rejected or failed, nothing was written or run. A
 * command that ran and exited non-zero did run, and the gate's compile did compile. */
function finishedVerb(row) {
  const verb = verbFor(row.name);
  if (row.status !== 'failed') return verb.past;
  if (row.off) return verb.off || verb.failed || verb.past;
  if (row.name === 'build' || (row.name === 'shell' && typeof row.exitCode === 'number' && !row.denied)) return verb.past;
  return verb.failed || verb.past;
}

/** What a row acted on: its target, or a tool this build has no verb for by its name. */
function objectOf(row) {
  // A running subagent names the call it is making; its task is the tooltip. One that has
  // answered names its task again.
  if (row.name === 'delegate' && row.status === 'running' && row.sub && row.sub.now && !row.sub.done) return row.sub.now;
  const target = targetLabel(row.target);
  if (target) return target;
  return verbFor(row.name) === DEFAULT_VERB ? displayText(toolName(row.name)) : '';
}

/** "Interrumpido · src/checkout.ts · El cambio a medias no se guardó": one line with label,
 * target and, for an edit, the no-partial-write note (strings.js's `cancelledLabel`).
 * `waiting` replaces the running (gerund) form with "Esperando tu permiso"; chat.js's
 * `waitingApprovalCallId` comment says how that is decided. */
function headerLabel(row, { waiting = false } = {}) {
  if (row.status === 'cancelled') {
    const target = targetLabel(row.target);
    const note = row.name === 'edit' ? CANCELLED_EDIT_NOTE : '';
    return cancelledLabel(target, note);
  }
  if (row.status === 'running' && waiting) return WAITING_APPROVAL;
  const verb = row.status === 'running' ? runningVerb(row) : finishedVerb(row);
  const object = objectOf(row);
  return object ? `${verb} ${object}` : verb;
}

/** The same label as `headerLabel`, split the way the design draws it: the verb in the
 * row's weight and ink («Leyó», «Editando», «Interrumpido»), then what it acted on in the
 * row's own grey («src/cart.ts»). */
function headerParts(row, { waiting = false } = {}) {
  if (row.status === 'cancelled') {
    const target = targetLabel(row.target);
    const note = row.name === 'edit' ? CANCELLED_EDIT_NOTE : '';
    const label = cancelledLabel(target, note);
    const [verb, ...rest] = label.split(' · ');
    return { verb, object: rest.join(' · ') };
  }
  if (row.status === 'running' && waiting) return { verb: WAITING_APPROVAL, object: '' };
  return { verb: row.status === 'running' ? runningVerb(row) : finishedVerb(row), object: objectOf(row) };
}

/** A running row's verb: a subagent that has answered and waits for the others says so. */
function runningVerb(row) {
  const verb = verbFor(row.name);
  return row.sub && row.sub.done && verb.done ? verb.done : verb.gerund;
}

/** What one of a subagent's calls produced, for its line: a read's line count, else what
 * its own row shows on the right. A call that took under a second and has nothing else to
 * show says nothing («0 s» is not a result). */
function stepMeta(step) {
  if (step.name === 'read' && Number.isInteger(step.lines)) return TOOL_DETAIL.lines(step.lines);
  const parts = resultParts(step);
  const timeOnly = parts.length === 1 && typeof step.durationMs === 'number' && parts[0].text === formatElapsed(step.durationMs);
  if (timeOnly && step.durationMs < 1000) return '';
  return parts.map((part) => part.text).join(' ');
}

/** A subagent's calls, one line each as its own row would say it («Leyó src/x.py · 212
 * líneas», «Buscando BASE» for the one running), oldest first. */
function buildSubagentSteps(sub, documentRef) {
  const list = documentRef.createElement('div');
  list.className = 'tool-block-path tool-sub-steps';
  const line = (text, meta, className) => {
    const el = documentRef.createElement('span');
    el.className = className ? `tool-block-file ${className}` : 'tool-block-file';
    el.append(documentRef.createTextNode(text));
    if (meta) {
      el.append(documentRef.createTextNode(' '));
      const metaEl = documentRef.createElement('span');
      metaEl.className = 'tool-block-file-meta';
      metaEl.append(documentRef.createTextNode(`· ${meta}`));
      el.append(metaEl);
    }
    list.append(el);
  };
  if (sub.earlier) line(ROW_NOTES.earlierCalls(sub.earlier), '', 'tool-sub-step--earlier');
  for (const step of sub.steps) {
    const meta = step.status === 'running' ? '' : stepMeta(step);
    const state = step.status === 'failed' && !step.off ? 'tool-sub-step--failed' : step.status === 'running' ? 'tool-sub-step--running' : '';
    line(headerLabel(step), meta, state);
  }
  return list;
}

/** One header row: an optional expand arrow (the ■ mark for a cancelled row, the spinner
 * for a running one), the label, a spacer, and the result on the right. `expandable` adds
 * the arrow, which the caller's expander wires. Long text (a long shell command) ellipsizes
 * via CSS, and `titleText` puts the full text in the native tooltip. */
function buildHeader({ documentRef, label, parts, titleText, metaParts, expandable, running, cancelled }) {
  const header = documentRef.createElement('div');
  header.className = 'tool-block-header';

  let arrowEl = null;
  if (cancelled) {
    const mark = documentRef.createElement('span');
    mark.className = 'tool-cancelled-icon';
    mark.append(documentRef.createTextNode(CANCELLED_ICON));
    header.append(mark);
  } else {
    if (expandable) {
      arrowEl = documentRef.createElement('span');
      arrowEl.className = 'tool-block-arrow';
      arrowEl.append(documentRef.createTextNode('▶'));
      header.append(arrowEl);
    }
    // A running row that opens (a subagent listing its calls) keeps its spinner.
    if (running) {
      const spinner = documentRef.createElement('span');
      spinner.className = 'tool-spinner';
      header.append(spinner);
    }
  }

  const labelEl = documentRef.createElement('span');
  labelEl.className = 'tool-block-label';
  const { verb, object } = parts || { verb: label, object: '' };
  const verbEl = documentRef.createElement('span');
  verbEl.className = 'tool-block-verb';
  verbEl.append(documentRef.createTextNode(verb));
  labelEl.append(verbEl);
  if (object) {
    // The canvas' 8px gap between the verb and its object, inside one ellipsizing label.
    labelEl.append(documentRef.createTextNode(' '));
    const objectEl = documentRef.createElement('span');
    objectEl.className = 'tool-block-object';
    objectEl.append(documentRef.createTextNode(object));
    labelEl.append(objectEl);
  }
  const fullLabel = object ? `${verb} ${object}` : verb;
  if (titleText && titleText !== fullLabel) labelEl.title = titleText;
  header.append(labelEl);

  const spacer = documentRef.createElement('span');
  spacer.className = 'tool-block-spacer';
  header.append(spacer);

  const metaEl = documentRef.createElement('span');
  metaEl.className = 'tool-block-meta';
  for (const part of metaParts || []) {
    const span = documentRef.createElement('span');
    if (part.className) span.className = part.className;
    span.append(documentRef.createTextNode(part.text));
    metaEl.append(span);
  }
  header.append(metaEl);

  return { header, arrow: arrowEl, metaEl };
}

/** Build one grouped or single row as a `.tool-block` box. `now` sets a running row's
 * elapsed time on the first render only. After that chat.js updates the same meta element's
 * text on each tick instead of rebuilding (see `renderTurnToolRows`'s returned registry), so
 * a tick never collapses an expanded group. `waitingCallId` is the call a pending approval
 * concerns, if any (a heuristic, see chat.js). */
function buildEntry(entry, documentRef, now, options = {}) {
  const { waitingCallId } = options;
  const { play, loadDetail } = detailOptions(options);
  const block = documentRef.createElement('div');
  block.className = 'tool-block';

  if (entry.kind === 'read-group') {
    const rows = entry.rows;
    const totalMs = rows.reduce((sum, row) => sum + (row.durationMs || 0), 0);
    const paths = rows.map((row) => targetLabel(row.target) || row.callId);
    if (rows.length === 1) {
      // Design 2f: one read is «Leyó src/checkout/coupon.ts» with «212 líneas» on the
      // right. Its file is already in the header, so there is nothing to open.
      const [row] = rows;
      const known = Number.isInteger(row.lines);
      const { header } = buildHeader({
        documentRef,
        parts: { verb: verbFor('read').past, object: paths[0] },
        titleText: paths[0],
        metaParts: known ? [{ text: TOOL_DETAIL.lines(row.lines) }] : totalMs ? [{ text: formatElapsed(totalMs) }] : [],
        expandable: false,
      });
      block.append(header);
      return { block, metaEl: null, callId: null };
    }
    // Design 1a: «Leyó 2 archivos src/cart.ts, src/cart.test.ts», files in the header.
    // Expanded, one line per file: «src/cart.ts · 64 líneas».
    const { header, arrow } = buildHeader({
      documentRef,
      parts: { verb: readGroupLabel(rows.length), object: paths.join(', ') },
      metaParts: totalMs ? [{ text: formatElapsed(totalMs) }] : [],
      expandable: true,
    });
    const list = documentRef.createElement('div');
    list.className = 'tool-block-path';
    rows.forEach((row, index) => {
      const file = documentRef.createElement('span');
      file.className = 'tool-block-file';
      file.append(documentRef.createTextNode(paths[index]));
      if (Number.isInteger(row.lines)) {
        file.append(documentRef.createTextNode(' '));
        const count = documentRef.createElement('span');
        count.className = 'tool-block-file-meta';
        count.append(documentRef.createTextNode(`· ${TOOL_DETAIL.lines(row.lines)}`));
        file.append(count);
      }
      list.append(file);
    });
    const key = rows[0];
    const { body } = makeExpander({ header, arrow, content: list, open: openRows.has(key), documentRef, play, onToggle: remember(key) });
    block.append(header, body);
    return { block, metaEl: null, callId: null };
  }

  const row = entry.row;
  const running = row.status === 'running';
  // A tool the person turned off did not fail: its row is drawn plain, with no red and no
  // technical detail.
  const failed = row.status === 'failed' && !row.off;
  const cancelled = row.status === 'cancelled';
  const waiting = running && waitingCallId != null && row.callId === waitingCallId;

  if (running && !waiting) block.classList.add('tool-block--running');
  if (failed) block.classList.add('tool-block--failed');
  if (cancelled) block.classList.add('tool-block--cancelled');
  // The approval card already says the call waits for the person. A second strip with the
  // same words under it is a duplicate signal (QA 2026-10-13), so the row stays in the
  // thread's model, ready to turn into the running row once approved, but is not shown.
  if (waiting) {
    block.classList.add('tool-block--waiting');
    block.hidden = true;
  }

  const metaParts = running && !waiting ? [{ text: formatElapsed(now - (row.startedAt || now)) }] : resultParts(row);

  // The tooltip names everything the row covers: every file of a multi-file row.
  const paths = (row.target && Array.isArray(row.target.paths) && row.target.paths) || [];
  const rawTarget =
    row.target &&
    (row.target.command || (paths.length > 1 ? paths.join('\n') : paths[0]) || row.target.pattern || row.target.query);
  // ▶ only on a row that really opens: a finished call that carried what it produced, or
  // a subagent with calls to list, running or not.
  const steps = row.name === 'delegate' && row.sub && Array.isArray(row.sub.steps) ? row.sub.steps : [];
  const detailed = !running && !cancelled && Boolean(row.detail);
  const opens = detailed || (steps.length > 0 && !waiting);
  const { header, arrow, metaEl } = buildHeader({
    documentRef,
    parts: headerParts(row, { waiting }),
    titleText: rawTarget ? displayText(rawTarget) : undefined,
    metaParts: cancelled ? [] : metaParts,
    expandable: opens,
    running: running && !waiting,
    cancelled,
  });
  block.append(header);
  if (opens) {
    const detail = detailed
      ? buildToolDetail(row.detail, { label: headerLabel(row), documentRef, loadDetail, play })
      : { element: null, ensure: () => {} };
    let content = detail.element;
    if (steps.length) {
      content = documentRef.createElement('div');
      content.append(buildSubagentSteps(row.sub, documentRef));
      if (detail.element) content.append(detail.element);
    }
    // A subagent's row is a new object with every call it makes; its `sub.key` is not.
    const key = steps.length && row.sub.key ? row.sub.key : row;
    const open = openRows.has(key);
    if (open) detail.ensure();
    const { body } = makeExpander({ header, arrow, content, open, documentRef, play, onToggle: remember(key, detail.ensure) });
    block.append(body);
  }

  // A cancelled row's target and no-partial-write note are already in the header label
  // (headerParts above). The core's `tool_end.error` for a cancelled call (often just
  // "cancelled") is never shown; see strings.js's `cancelledLabel`.
  // The gate's error count is on the right and its errors open below; its own line is the
  // core's English for the log.
  // The header already says what happened in the person's language («No se ejecutó ·
  // no permitido»); the core's own line is English written for the model, so it waits
  // behind «Detalle técnico», collapsed, as the service-error card keeps its log.
  if (failed && row.error && row.name !== 'build') {
    const note = documentRef.createElement('div');
    note.className = 'tool-block-note tool-block-note--error';
    const text = documentRef.createElement('div');
    text.className = 'tool-block-note-text';
    text.hidden = true;
    text.append(documentRef.createTextNode(displayText(row.error)));
    const toggle = documentRef.createElement('button');
    toggle.type = 'button';
    toggle.className = 'tool-block-note-toggle';
    toggle.textContent = MODEL_PICKER.technicalDetail;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => {
      text.hidden = !text.hidden;
      toggle.setAttribute('aria-expanded', String(!text.hidden));
    });
    note.append(toggle, text);
    block.append(note);
  }

  return { block, metaEl: running && !waiting ? metaEl : null, callId: row.callId };
}

/**
 * Rebuild `wrapper`'s children from `groupedEntries` (`tool-rows.js`'s `groupRows`). Never
 * called on a tick: the elapsed-time timer updates a running row's meta text directly, so an
 * expanded group stays open. `chat.js` calls this for a `tool_end` that could change grouping
 * (a read finishing). Otherwise (a `tool_start`, or a `tool_end` for anything that never
 * groups) it uses `buildSingleRowBlock` to append or replace one row in place, because
 * rebuilding a segment of N rows on each of its N events is quadratic (the review's 200-turn
 * stress case). Returns `{ runningMeta, blocks }`: `runningMeta` is `Map(callId -> metaEl)`
 * for each running row this pass built, and `blocks` is `Map(callId -> block)` for each
 * single (non-grouped) row, so the caller can keep replacing rows in place after a rebuild.
 */
function renderTurnToolRows(wrapper, groupedEntries, documentRef = document, now = Date.now(), options = {}) {
  wrapper.replaceChildren();
  const runningMeta = new Map();
  const blocks = new Map();
  for (const entry of groupedEntries) {
    const { block, metaEl, callId } = buildEntry(entry, documentRef, now, options);
    wrapper.append(block);
    if (metaEl && callId != null) runningMeta.set(callId, metaEl);
    if (entry.kind === 'single') blocks.set(entry.row.callId, block);
  }
  return { runningMeta, blocks };
}

/** One single (never grouped) row, built on its own. `chat.js` uses it to append a new
 * running call or replace a finished one in place without touching the segment's other
 * rows. Never used for a read group: only `renderTurnToolRows`, which decides grouping
 * across the segment, builds those. */
function buildSingleRowBlock(row, documentRef = document, now = Date.now(), options = {}) {
  const { block, metaEl } = buildEntry({ kind: 'single', row }, documentRef, now, options);
  return { block, metaEl };
}

/** A file has nothing to expand when the core skipped diffing it (`binary`, `too_large`) or
 * clipped it away for size (`truncated` with `diff` empty); see `core/cogito/server/app.py`'s
 * docstring. An arrow that opens onto nothing is worse than none. */
function diffAvailable(file) {
  return !file.binary && !file.too_large && !(file.truncated && !file.diff) && Boolean(file.diff);
}

function bytesToKB(bytes) {
  return Math.max(1, Math.round(bytes / 1024));
}

/** The five small squares beside a changed file's counts (design 1a): green for lines
 * added, red for lines removed, grey for the rest. One square per line up to five lines,
 * proportional beyond, as a diffstat draws it. Uses the file's real counts only. */
function buildDiffstat(added, removed, documentRef) {
  const SQUARES = 5;
  const total = added + removed;
  let green = added;
  let red = removed;
  if (total > SQUARES) {
    green = Math.round((added / total) * SQUARES);
    red = Math.round((removed / total) * SQUARES);
    // Never lose a side that changed, and never draw more squares than there are.
    if (added && !green) green = 1;
    if (removed && !red) red = 1;
    while (green + red > SQUARES) {
      if (green >= red) green -= 1;
      else red -= 1;
    }
  }
  const stat = documentRef.createElement('span');
  stat.className = 'change-file-stat';
  stat.setAttribute?.('aria-hidden', 'true'); // decorative: the counts beside it say it
  for (let index = 0; index < SQUARES; index += 1) {
    const square = documentRef.createElement('span');
    square.className = index < green ? 'stat-add' : index < green + red ? 'stat-remove' : 'stat-none';
    stat.append(square);
  }
  return stat;
}

/** One file's row in the "Cambios de este turno" card: name, +/- badge (only when the core
 * reported counts; a binary or too-large file gets neither), and either an expandable diff or
 * a fixed Spanish note in its place. A click toggles the diff. The diff is parsed and built
 * only on first open, so a 200-file turn does not build 200 hidden diffs up front. */
function buildFileRow(file, documentRef) {
  const wrap = documentRef.createElement('div');
  wrap.className = 'change-file';

  const header = documentRef.createElement('div');
  header.className = 'change-file-header';

  // Appended first so the arrow lands before the path without `prepend`, which not every
  // DOM-like target this runs against implements.
  const available = diffAvailable(file);
  let arrow = null;
  if (available) {
    arrow = documentRef.createElement('span');
    arrow.className = 'tool-block-arrow';
    arrow.append(documentRef.createTextNode('▶'));
    header.append(arrow);
  }

  const path = documentRef.createElement('span');
  path.className = 'change-file-path';
  path.append(documentRef.createTextNode(displayText(file.path)));

  const spacer = documentRef.createElement('span');
  spacer.className = 'tool-block-spacer';

  header.append(path, spacer);

  const hasCounts = typeof file.added === 'number' || typeof file.removed === 'number';
  if (hasCounts) {
    const badge = documentRef.createElement('span');
    badge.className = 'change-file-badge';
    if (file.added) {
      const add = documentRef.createElement('span');
      add.className = 'diff-add';
      add.append(documentRef.createTextNode(`+${file.added}`));
      badge.append(add);
    }
    if (file.removed) {
      if (file.added) badge.append(documentRef.createTextNode(' '));
      const rem = documentRef.createElement('span');
      rem.className = 'diff-remove';
      rem.append(documentRef.createTextNode(`−${file.removed}`));
      badge.append(rem);
    }
    header.append(badge);
    header.append(buildDiffstat(file.added || 0, file.removed || 0, documentRef));
  }

  const body = documentRef.createElement('div');
  body.className = 'change-file-body';
  body.style.display = 'none';

  if (available) {
    let built = false;
    let revealed = 0;
    const diffContainer = documentRef.createElement('div');
    diffContainer.className = 'diff-lines';
    const more = documentRef.createElement('div');
    more.className = 'change-expand-more';
    more.style.cursor = 'pointer';

    const redraw = (lines) => {
      diffContainer.replaceChildren();
      renderDiffLines(diffContainer, lines, revealed, documentRef);
      const remaining = visibleLineCount(lines) - revealed;
      if (remaining > 0) {
        more.replaceChildren(documentRef.createTextNode(CHANGES_CARD.expandMore(remaining)));
        more.style.display = '';
      } else {
        more.style.display = 'none';
      }
    };

    let lines = null;
    const ensureBuilt = () => {
      if (built) return;
      built = true;
      lines = parseUnifiedDiff(file.diff);
      revealed = Math.min(visibleLineCount(lines), COLLAPSE_AT);
      body.append(diffContainer, more);
      redraw(lines);
    };

    more.addEventListener('click', (event) => {
      event.stopPropagation();
      revealed = visibleLineCount(lines);
      redraw(lines);
    });

    if (file.truncated) {
      const note = documentRef.createElement('div');
      note.className = 'change-file-truncated';
      note.append(documentRef.createTextNode(CHANGES_CARD.truncatedByServer));
      body.append(note);
    }

    let open = false;
    const apply = () => {
      arrow.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
      body.style.display = open ? '' : 'none';
      if (open) ensureBuilt();
    };
    apply();
    header.className = 'change-file-header is-expandable';
    header.addEventListener('click', () => {
      open = !open;
      sfx(open ? 'open' : 'close');
      apply();
    });
    wrap._open = () => {
      open = true;
      apply();
    };
    wrap._close = () => {
      open = false;
      apply();
    };
  } else if (file.unrecorded) {
    // Read back from the store (history on open, `history.js`): the diff was never stored,
    // so there is nothing to open or explain. Only the path, and its count when the core
    // knows it exactly.
    wrap._open = () => {};
    wrap._close = () => {};
  } else {
    // No arrow, no toggle: a static note where the diff would have been, never an empty
    // panel. `change-file-note` styles it; `change-file-truncated` lets anything looking for
    // a file's explanatory note find it, whichever reason produced it (binary, too_large,
    // truncated with no diff).
    const note = documentRef.createElement('div');
    note.className = 'change-file-note change-file-truncated';
    note.append(
      documentRef.createTextNode(
        file.binary
          ? CHANGES_CARD.binaryNote
          : file.too_large
            ? CHANGES_CARD.tooLargeNote(bytesToKB(file.before_bytes || 0), bytesToKB(file.after_bytes || 0))
            : file.truncated
              ? CHANGES_CARD.truncatedByServer
              : // No diff and nothing cut: an empty file created or removed, or one whose
                // text came out the same (QA rig, 2026-10-16: an empty __init__.py read
                // «Se recortó por tamaño»).
                file.status === 'modified'
                ? CHANGES_CARD.unchangedNote
                : CHANGES_CARD.emptyNote,
      ),
    );
    body.append(note);
    body.style.display = '';
    wrap._open = () => {};
    wrap._close = () => {};
  }

  wrap.append(header, body);
  return wrap;
}

/** The whole "Cambios de este turno" card from a `turn_changes` event. Returns `null` when
 * there is nothing to show: the event is only emitted when files changed, but a malformed or
 * empty payload must not throw. */
function buildChangesCard(event, documentRef = document) {
  const files = Array.isArray(event.files) ? event.files : [];
  if (!files.length) return null;

  const card = documentRef.createElement('div');
  card.className = 'changes-card';

  const header = documentRef.createElement('div');
  header.className = 'changes-card-header';
  const title = documentRef.createElement('span');
  title.className = 'changes-card-title';
  title.append(documentRef.createTextNode(CHANGES_CARD.title));
  const count = documentRef.createElement('span');
  count.className = 'changes-card-count';
  count.append(documentRef.createTextNode(CHANGES_CARD.fileCount(event.total_files ?? files.length)));
  const spacer = documentRef.createElement('span');
  spacer.className = 'tool-block-spacer';
  const totals = documentRef.createElement('span');
  totals.className = 'changes-card-totals';
  const totalAdded = files.reduce((sum, file) => sum + (file.added || 0), 0);
  const totalRemoved = files.reduce((sum, file) => sum + (file.removed || 0), 0);
  if (totalAdded) {
    const add = documentRef.createElement('span');
    add.className = 'diff-add';
    add.append(documentRef.createTextNode(`+${totalAdded}`));
    totals.append(add);
  }
  if (totalRemoved) {
    if (totalAdded) totals.append(documentRef.createTextNode(' '));
    const rem = documentRef.createElement('span');
    rem.className = 'diff-remove';
    rem.append(documentRef.createTextNode(`−${totalRemoved}`));
    totals.append(rem);
  }
  header.append(title, count, spacer, totals);
  card.append(header);

  const rows = files.map((file) => buildFileRow(file, documentRef));
  rows.forEach((row) => card.append(row));

  const footer = documentRef.createElement('div');
  footer.className = 'changes-card-footer';
  const expandAll = documentRef.createElement('span');
  expandAll.className = 'changes-expand-all';
  expandAll.style.cursor = 'pointer';
  let allOpen = false;
  expandAll.append(documentRef.createTextNode(CHANGES_CARD.expandAll));
  expandAll.addEventListener('click', () => {
    allOpen = !allOpen;
    sfx(allOpen ? 'open' : 'close');
    rows.forEach((row) => (allOpen ? row._open() : row._close()));
    expandAll.replaceChildren(
      documentRef.createTextNode(allOpen ? CHANGES_CARD.collapseAll : CHANGES_CARD.expandAll),
    );
  });
  // A card read back from the store has no diff to expand at all.
  if (!files.every((file) => file.unrecorded)) footer.append(expandAll);

  if (event.shell_changes_unknown) {
    const note = documentRef.createElement('span');
    note.className = 'changes-card-note';
    note.append(documentRef.createTextNode(CHANGES_CARD.shellChangesUnknown));
    footer.append(note);
  }
  if (footer.children.length) card.append(footer);

  return card;
}

export { renderTurnToolRows, buildSingleRowBlock, buildChangesCard, resultParts, headerLabel, headerParts, diffAvailable, buildDiffstat };
