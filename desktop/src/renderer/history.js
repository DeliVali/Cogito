'use strict';

/**
 * History on open: the project's thread as the core stored it (`GET /sessions/{id}/history`;
 * `core/cogito/server/history.py` has the item shapes), turned into the same blocks a live
 * turn builds. `render-history.js` draws them with the live renderers, so a turn read back
 * from the store looks as it did while it ran:
 *
 * - a person's message (or a steer) is a `said` bubble with its rewind point;
 * - an approval is the pill its card left («Permitiste · rm notas.txt»), above the row of
 *   the call it gated, from what the core recorded with that call;
 * - an answer holds the turn's tool rows (`tool-rows.js` row models, built from each item
 *   as `endRow` builds one from a `tool_end`) and the model's words between them, as
 *   `assistant_text` drew them live, then «Cambios de este turno» (a file's diff only
 *   where the core kept one), then «Listo · HH:MM»;
 * - a turn that did not finish (`turn_stopped`) ends on why, as the live thread said it,
 *   and «Detenido · HH:MM»;
 * - a steer ends the answer it arrived in, and the turn continues in a new answer below
 *   it, where the steer joined the conversation;
 * - a compaction fold is a thin marker, never its text: inside the answer if it happened
 *   mid-turn, between turns otherwise;
 * - APP-ONLY auto-continue (long-output fix 3): an `assistant` item marked `continued`
 *   (`server/history.py`, the same fact as the live `AssistantText.continued`) joins the
 *   paragraph before it instead of starting a new one, exactly as the live thread did;
 * - a rewind («↺ Volviste a las HH:MM · N archivos») is the result line the live rewind
 *   left, with «Rehacer» only while the core says it still applies. What it hid is not in
 *   the page;
 * - «— HOY —» (or the date, for an earlier day) goes before each day's first message.
 *
 * Pure: no DOM and no clock of its own (`now` is passed in), so `cogito-lab` runs it in Node.
 */

import { endRow } from './tool-rows.js';
import { pillFor, pillObject } from './decisions.js';
import { STATUS, THREAD, STOP_COPY, STOPPED_FAILURE } from './strings.js';
import { joinedAnswer } from './render-thread.js';

/** "2026-09-28", in local time. */
function dayKey(whenMs) {
  const when = new Date(whenMs);
  const month = String(when.getMonth() + 1).padStart(2, '0');
  const day = String(when.getDate()).padStart(2, '0');
  return `${when.getFullYear()}-${month}-${day}`;
}

/** «HOY» for today, otherwise the date as the rest of the app writes it («18/09/2026»). */
function dayLabel(key, now) {
  if (key === dayKey(now)) return THREAD.today;
  const [year, month, day] = key.split('-');
  return `${day}/${month}/${year}`;
}

/** The separator block for the day `whenMs` falls on. */
function daySeparator(whenMs, now) {
  const key = dayKey(whenMs);
  return { kind: 'day', key, label: THREAD.daySeparator(dayLabel(key, now)) };
}

/** "HH:MM", 24-hour, as in `turn_complete`'s «Listo · HH:MM» line. */
function clock(whenMs) {
  return new Date(whenMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function timeOf(item) {
  const parsed = Date.parse(item.at);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A tool item as the row a live `tool_end` would have left: same fields, same builder. */
function rowFrom(item) {
  return endRow(undefined, {
    call_id: item.call_id,
    name: item.name,
    target: item.target || {},
    ok: item.ok,
    status: item.status,
    duration_ms: item.duration_ms,
    exit_code: item.exit_code,
    added: item.added,
    removed: item.removed,
    error: item.error,
    lines: item.lines,
    detail: item.detail,
    matches: item.matches,
    files: item.files,
    denied: item.denied,
    off: item.off,
    background: item.background,
    calls: item.calls,
  });
}

/** The parts a stopped turn ends on: why, unless «Detener» stopped it, which «Detenido»
 * says. A provider failure is a red line, the live card's headline without the provider's
 * words, which are not stored. */
function stoppedParts(item) {
  const reason = typeof item.reason === 'string' ? item.reason : '';
  if (reason === 'user_interrupt') return [];
  if (reason === 'provider_failure') {
    const title = STOPPED_FAILURE[item.failure] || STOP_COPY.provider_failure();
    return [{ kind: 'failure', text: title }];
  }
  const copy = STOP_COPY[reason] || STOP_COPY.other;
  return [{ kind: 'stopped', text: copy() }];
}

/** A message's attachments (design 1n): `{attachments}` when it has any, else `{}`, so a
 * message without them gets no `attachments` key. */
function attachedTo(item) {
  return Array.isArray(item.attachments) && item.attachments.length ? { attachments: item.attachments } : {};
}

/**
 * `items`: one page, oldest first. `previousDayKey`: the day of whatever sits just before
 * this page in the window (the older page, if any), so a day that spans two pages gets one
 * separator. Returns `{blocks, firstDayKey, lastDayKey}`.
 */
function historyBlocks(items, { now = Date.now(), previousDayKey = null } = {}) {
  const blocks = [];
  let answer = null;
  let rows = null;
  let finalAt = null;
  let stoppedAt = null;
  let lastDay = previousDayKey;
  let firstDay = null;

  const openAnswer = () => {
    if (!answer) {
      answer = { kind: 'answer', parts: [] };
      blocks.push(answer);
    }
    return answer;
  };
  const endRows = () => {
    rows = null;
  };
  const closeTurn = () => {
    if (answer && stoppedAt !== null) answer.parts.push({ kind: 'meta', text: `${STATUS.turnStopped} · ${clock(stoppedAt)}` });
    else if (answer && finalAt !== null) answer.parts.push({ kind: 'meta', text: `${STATUS.turnDone} · ${clock(finalAt)}` });
    answer = null;
    rows = null;
    finalAt = null;
    stoppedAt = null;
  };
  const markDay = (whenMs) => {
    if (whenMs === null) return;
    const key = dayKey(whenMs);
    if (firstDay === null) firstDay = key;
    if (key !== lastDay) blocks.push(daySeparator(whenMs, now));
    lastDay = key;
  };

  for (const item of items) {
    const at = timeOf(item);
    switch (item.type) {
      case 'user':
        closeTurn();
        markDay(at);
        blocks.push({ kind: 'said', text: item.text, pointId: item.point_id, sentAt: at, steer: false, ...attachedTo(item) });
        break;
      case 'steer':
        answer = null;
        rows = null;
        markDay(at);
        blocks.push({ kind: 'said', text: item.text, pointId: item.point_id, sentAt: at, steer: true, ...attachedTo(item) });
        break;
      case 'tool': {
        const turn = openAnswer();
        if (!rows) {
          rows = { kind: 'rows', rows: [] };
          turn.parts.push(rows);
        }
        rows.rows.push(rowFrom(item));
        break;
      }
      case 'approval': {
        // The pill the card left, where the card stood: above the row it gated, which then
        // starts a new group, as `breakSegment` made it live. A request this build cannot
        // word (no facts) draws nothing.
        const request = item.request && typeof item.request === 'object' ? item.request : null;
        if (request) {
          const turn = openAnswer();
          turn.parts.push({ kind: 'pill', pill: pillFor(item.allowed, item.reason, pillObject(request), request.action) });
          endRows();
        }
        break;
      }
      case 'assistant':
        // Every reply's words, where they were said, as `assistant_text` drew them live.
        // APP-ONLY auto-continue: `item.continued` joins this reply onto the paragraph
        // right before it (the last part, when that part is itself a paragraph) instead
        // of starting a new one -- one answer, not two, exactly as the live thread drew it.
        if (typeof item.text === 'string' && item.text) {
          const turn = openAnswer();
          const parts = turn.parts;
          const last = parts[parts.length - 1];
          if (item.continued && last && last.kind === 'prose') {
            last.text = joinedAnswer(last.text, item.text);
          } else {
            parts.push({ kind: 'prose', text: item.text });
          }
          endRows();
        }
        if (item.final) finalAt = at;
        break;
      case 'turn_changes':
        if (Array.isArray(item.files) && item.files.length) {
          // A diff only where the core kept the one call's own. `unrecorded` tells the
          // card to show a file without one and no note about why there is none to open.
          const files = item.files.map((file) => (typeof file.diff === 'string' && file.diff ? file : { ...file, unrecorded: true }));
          openAnswer().parts.push({ kind: 'changes', event: { ...item, files } });
          endRows();
        }
        break;
      case 'turn_stopped': {
        const turn = openAnswer();
        // «Detener» before anything of this turn was drawn: said, never an empty bubble.
        if (item.reason === 'user_interrupt' && turn.parts.length === 0) turn.parts.push({ kind: 'stopped', text: STOP_COPY.user_interrupt_silent() });
        else turn.parts.push(...stoppedParts(item));
        if (turn.parts.some((part) => part.kind === 'failure')) turn.failed = true;
        endRows();
        stoppedAt = at ?? finalAt ?? now;
        break;
      }
      case 'rewind':
        // The conversation was taken back to an earlier point: the turn it cut ends here,
        // and what it hid is not in the page.
        closeTurn();
        blocks.push({
          kind: 'rewound',
          redoId: item.id,
          pointId: item.point_id,
          pointAt: Number.isFinite(Date.parse(item.point_at)) ? Date.parse(item.point_at) : at,
          files: Number.isInteger(item.files) ? item.files : 0,
          redo: item.redo === true,
          item,
        });
        break;
      case 'compaction':
        // Before the turn's last word, the fold goes inside its answer. After it, the fold
        // happened at the top of the next turn, so it goes between the two.
        if (answer && finalAt === null) {
          answer.parts.push({ kind: 'fold' });
          endRows();
        } else {
          closeTurn();
          blocks.push({ kind: 'fold' });
        }
        break;
      default:
        // An item from a newer core that this build does not know: skipped, like an
        // unknown event.
        break;
    }
  }
  closeTurn();
  return { blocks, firstDayKey: firstDay, lastDayKey: lastDay };
}

export { historyBlocks, dayKey, dayLabel, daySeparator, clock };
