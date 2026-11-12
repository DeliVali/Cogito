'use strict';

/**
 * "Volver a un punto" (rewind): the dialog models, pure and DOM-free so they can be tested
 * alone (like `decisions.js` apart from `render-decisions.js`).
 *
 * Each `build*Model` takes what `core/cogito/server/app.py`'s dry-run/perform/redo
 * responses carry (that module's docstring has the shapes) and returns the strings and data
 * `render-rewind.js` draws. Nothing here touches the DOM, calls `window.cogito` or reads the
 * clock. `sentAt`/`whenMs` are passed in, so a test can pin them, and so the result line and
 * the conflict dialog's diff heading show the point's own time (when the message was sent),
 * not when the response arrived.
 */

import { REWIND } from './strings.js';

/** "HH:MM", 24-hour, no seconds: the one clock format this feature (and `turn_complete`'s
 * "Listo · HH:MM") shows a person. */
function formatClock(whenMs) {
  return new Date(whenMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** The "↺ Volver a este punto · HH:MM" link's label. `sentAt` is when this window sent the
 * message (the composer submit, or the steer), not a server timestamp: neither
 * `point_recorded` nor `message_appended` carries one, and that moment is what a person
 * means by "this point". */
function rewindLinkLabel(sentAt) {
  return REWIND.link(formatClock(sentAt));
}

/**
 * The confirm dialog, from a `GET /rewind/{point}` dry run
 * (`{files, conflicts, commit_after}`). `fileCount` is every file the rewind would touch,
 * conflicting or not: the brief's wording ("cambios posteriores a este mensaje: N
 * archivos") counts what changed, not what is safe to change automatically. Known conflicts
 * do not skip the confirm step: the person still chooses to rewind at all before the
 * conflict dialog asks how.
 */
function buildConfirmModel(plan) {
  const fileCount = plan.files.length;
  // Whether the conversation goes back with the files. A core that knows how says so
  // (`conversation: true`); an older one omits it, and only its files go back.
  const conversation = plan.conversation === true;
  return {
    title: REWIND.confirmTitle,
    body: conversation ? REWIND.confirmBodyConversation(fileCount) : REWIND.confirmBody(fileCount),
    conversation,
    confirmLabel: REWIND.confirmGo,
    cancelLabel: REWIND.confirmCancel,
    inProgressLabel: REWIND.inProgress,
    fileCount,
    hasConflicts: plan.conflicts.length > 0,
  };
}

/**
 * The conflict dialog, from a 409 `{error:"conflict", files, conflicts, commit_after}` (or
 * an equivalent dry run): "No se puede volver del todo", because some files changed since
 * the point and the rewind will not guess. `otherCount` is what "Restaurar los otros N"
 * promises: every file in `files` that is not in `conflicts`. `pointSentAt` is the point's
 * own clock (never `Date.now()`), for the diff's "Punto de las HH:MM" heading.
 */
function buildConflictModel(plan, pointSentAt) {
  const conflictPaths = new Set(plan.conflicts.map((c) => c.path));
  const otherCount = plan.files.filter((f) => !conflictPaths.has(f.path)).length;
  const pointClock = formatClock(pointSentAt);
  const onlyConflict = plan.conflicts.length === 1 ? plan.conflicts[0].path : null;

  return {
    title: REWIND.conflictTitle,
    body: REWIND.conflictBody(
      plan.conflicts.map((c) => c.path),
      otherCount,
    ),
    conflicts: plan.conflicts.map((c) => ({
      path: c.path,
      diff: c.diff,
      showLabel: REWIND.diffShow(c.path),
      hideLabel: REWIND.diffHide,
      currentHeading: REWIND.diffCurrentHeading,
      currentNote: REWIND.diffCurrentNote,
      pointHeading: REWIND.diffPointHeading(pointClock),
      pointNote: REWIND.diffPointNote,
    })),
    otherCount,
    choices: {
      leave: REWIND.conflictLeave,
      keepOthers: otherCount > 0 ? REWIND.conflictKeepOthers(otherCount) : null,
      // Names a path only when there is exactly one conflict. With several,
      // "conserva tu edición en" would have to name several paths at once, which the
      // canvas never shows, so it is left out.
      keepOthersSubtitle: otherCount > 0 && onlyConflict ? REWIND.conflictKeepOthersSubtitle(onlyConflict) : null,
      forceAll: REWIND.conflictForceAll,
      forceAllSubtitle: REWIND.conflictForceAllSubtitle(plan.conflicts.length),
    },
  };
}

/**
 * The line that stands in for the confirm dialog when the core answers that the context
 * store is down (`{graphDown: true}`): what happened, and «Reintentar». Nothing was
 * rewound.
 */
function buildGraphDownModel() {
  return { text: REWIND.graphDown, retryLabel: REWIND.graphRetry };
}

/** The error a «Rehacer» click raises when the store is down, so the line shows the
 * reason beside the button, which stays pressable. */
function graphDownError() {
  const error = new Error(REWIND.graphDown);
  error.graphDown = true;
  return error;
}

/** «N archivos», or «conversación» when the conversation alone went back. */
function detailFor(files, conversation) {
  return files === 0 && conversation ? REWIND.resultConversation : REWIND.resultDetail(files);
}

/**
 * The result line, from a successful `POST /rewind/{point}`
 * (`{redo_id, files, kept, forced}`): "Volviste a las HH:MM · N archivos · Rehacer".
 * `whenMs` is the point's own clock (never `Date.now()`): the response carries no
 * timestamp, and "Volviste a las" names the point's time, not when the request finished.
 */
function buildResultModel(result, whenMs) {
  const failed = result.failed || [];
  // The conversation went back too: something came back even with no file restored.
  const rewound = Boolean(result.conversation);
  const hasRestored = result.files.length > 0 || rewound;
  const detail = detailFor(result.files.length, rewound);
  return {
    text: `${REWIND.resultTitle(formatClock(whenMs))} · ${detail}`,
    // The same line in the canvas' two parts: the pill's bold title and its detail.
    title: REWIND.resultTitle(formatClock(whenMs)),
    detail,
    // A failure count plus exactly which paths, next to the result they belong to. `null`
    // when everything succeeded.
    failedText:
      failed.length > 0
        ? `${REWIND.resultFailed(failed.length)}: ${REWIND.resultFailedPaths(failed.map((f) => f.path))}`
        : null,
    redoLabel: REWIND.redo,
    redoFailedLabel: REWIND.redoFailed,
    graphDownLabel: REWIND.graphDown,
    redoId: result.redo_id,
    // Whether anything was restored. chat.js uses it to decide whether to put the original
    // message back in the composer: nothing restored means nothing to re-edit and resend.
    // The conversation going back counts, since it undid what was said after the point.
    hasRestored,
    // Whether «Rehacer» applies: the core's `redo`. An older core that does not send it
    // could redo whatever it restored.
    redoAvailable: typeof result.redo === 'boolean' ? result.redo : hasRestored,
    // The point's own words, for the composer (`point_text`); null when the core sent none.
    pointText: typeof result.point_text === 'string' ? result.point_text : null,
  };
}

/**
 * The same line for a rewind read back from history (a `rewind` item of `GET /history`,
 * see `core/cogito/server/history.py`): the point's own time, the files it restored, and
 * «Rehacer» only while the core says it still applies (`redo`).
 */
function buildMarkerModel(item) {
  const pointMs = Date.parse(item.point_at);
  const clock = formatClock(Number.isFinite(pointMs) ? pointMs : Date.parse(item.at));
  const files = Number.isInteger(item.files) ? item.files : 0;
  // A rewind the history keeps always took the conversation back.
  const detail = detailFor(files, true);
  return {
    text: `${REWIND.resultTitle(clock)} · ${detail}`,
    title: REWIND.resultTitle(clock),
    detail,
    failedText: null,
    redoLabel: REWIND.redo,
    redoFailedLabel: REWIND.redoFailed,
    graphDownLabel: REWIND.graphDown,
    redoId: item.id,
    hasRestored: true,
    redoAvailable: item.redo === true,
    pointText: null,
  };
}

export {
  formatClock,
  rewindLinkLabel,
  buildConfirmModel,
  buildConflictModel,
  buildGraphDownModel,
  graphDownError,
  buildResultModel,
  buildMarkerModel,
};
