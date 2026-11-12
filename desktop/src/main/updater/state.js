'use strict';

/**
 * The update state machine from design 3a/3b, with no I/O in it.
 *
 *   none | downloading | ready | deferred | restarting | updated | failed
 *   + security: boolean + install: 'self' | 'package-manager' | 'none'
 *
 * `reduce(state, event)` returns the next state and the effects the caller must perform
 * (`'restart'`, `'check'`). The rules come from the design:
 *
 * - An update is never applied mid-task. «Reiniciar ahora» while busy becomes «Reiniciar
 *   al terminar» (deferred), and deferred restarts as soon as the agent is idle.
 * - Ready while idle stays ready. Only the person restarts it (or quitting installs it).
 * - A security update (`security: true`, from the signed release notes) cannot be postponed
 *   to «Al salir». It defers itself as soon as work is running and restarts when idle.
 * - A failed check with nothing found stays silent (none). A failed download,
 *   verification or pre-restart step shows «No se pudo actualizar · Reintentar».
 * - A package-manager install only reaches `ready` (the card). It never restarts.
 *
 * "Busy" has its own small reducer (`reduceActivity`), fed by the core's events through
 * `activityFromCoreEvent`. Busy means any of:
 * - a turn the window asked for, from the request until the turn's `turn_complete` or
 *   `error` (`turn-ended`). Not until its terminal phase: the core sends that before it
 *   says whether it will resume the turn;
 * - any non-terminal phase, including a pending permission (`awaiting_approval`);
 * - a turn the core has scheduled to resume by itself at a provider's `retry-after`
 *   (`resume-scheduled` until `resume-settled`). A restart would kill the core and that
 *   resume with it.
 * The core has no question card yet. When it does, its pending state belongs here too.
 */

const { UPDATE_COPY } = require('./copy');

const PHASES = Object.freeze(['none', 'downloading', 'ready', 'deferred', 'restarting', 'updated', 'failed']);
const TERMINAL_PHASES = new Set(['done', 'interrupted', 'failed']);
const IDLE_PHASES = new Set(['none', 'updated', 'failed']);

const EMPTY_NOTES = Object.freeze({ tools: [], improvements: [] });

function initialState({ install, currentVersion, outcome = null, command = null, packageManager = null }) {
  return {
    phase: outcome?.kind === 'updated' ? 'updated' : 'none',
    install,
    current: currentVersion,
    version: null,
    percent: 0,
    security: false,
    notes: EMPTY_NOTES,
    busy: false,
    packageManager,
    command,
    // The `UpdateError.code` a `failed` event carries (`main/updater/errors.js`), so the
    // window can say why it failed.
    failureReason: null,
  };
}

function requestRestart(state) {
  if (state.busy) return { state: { ...state, phase: 'deferred' }, effects: [] };
  return { state: { ...state, phase: 'restarting' }, effects: ['restart'] };
}

function reduce(state, event) {
  const same = { state, effects: [] };
  const self = state.install === 'self';

  switch (event.type) {
    case 'busy': {
      const next = { ...state, busy: Boolean(event.busy) };
      if (!self) return { state: next, effects: [] };
      if (!next.busy && state.phase === 'deferred') return { state: { ...next, phase: 'restarting' }, effects: ['restart'] };
      if (next.busy && state.phase === 'restarting') return { state: { ...next, phase: 'deferred' }, effects: [] };
      if (next.busy && state.phase === 'ready' && state.security) return { state: { ...next, phase: 'deferred' }, effects: [] };
      return { state: next, effects: [] };
    }

    case 'download-started':
      if (!self || !IDLE_PHASES.has(state.phase)) return same;
      return { state: { ...state, phase: 'downloading', version: event.version, percent: 0, security: false, notes: EMPTY_NOTES, failureReason: null }, effects: [] };

    case 'download-progress': {
      if (state.phase !== 'downloading') return same;
      const percent = Math.max(state.percent, Math.min(100, Math.max(0, Math.floor(event.percent))));
      return { state: { ...state, percent }, effects: [] };
    }

    case 'ready': {
      if (!self || state.phase !== 'downloading') return same;
      const next = { ...state, phase: 'ready', version: event.version, percent: 100, security: Boolean(event.security), notes: event.notes || EMPTY_NOTES };
      if (next.security && next.busy) next.phase = 'deferred';
      return { state: next, effects: [] };
    }

    case 'available':
      if (state.install !== 'package-manager') return same;
      return { state: { ...state, phase: 'ready', version: event.version, notes: event.notes || EMPTY_NOTES }, effects: [] };

    case 'failed':
      if (!['downloading', 'ready', 'deferred', 'restarting'].includes(state.phase)) return same;
      return { state: { ...state, phase: 'failed', failureReason: event.reason || null }, effects: [] };

    case 'restart-now':
    case 'restart-when-done':
      if (!self || !['ready', 'deferred'].includes(state.phase)) return same;
      return requestRestart(state);

    // «Al salir» cancels a pending «Reiniciar al terminar» back to plain `ready`, which
    // still installs on quit (`controller.js`'s `beforeQuit`). The two states differ:
    // `deferred` restarts as soon as the agent goes idle, `ready` waits for a person or a
    // quit. Already `ready`, it is a no-op: nothing is pending.
    case 'defer-to-quit':
      if (!self || !['ready', 'deferred'].includes(state.phase)) return same;
      if (state.phase === 'ready') return same;
      return { state: { ...state, phase: 'ready' }, effects: [] };

    case 'restart-failed':
      if (state.phase !== 'restarting') return same;
      return { state: { ...state, phase: 'failed', failureReason: event.reason || null }, effects: [] };

    // The restart's last check just before it installs found that work started meanwhile.
    case 'restart-postponed':
      if (state.phase !== 'restarting') return same;
      return { state: { ...state, busy: true, phase: 'deferred' }, effects: [] };

    case 'retry':
      if (state.phase !== 'failed') return same;
      return { state: { ...state, phase: 'none', percent: 0, failureReason: null }, effects: ['check'] };

    default:
      return same;
  }
}

/**
 * The tray's «Reiniciar para actualizar» item, or null when there is nothing to restart
 * into. While `deferred` it shows «Se actualiza al terminar», like the window's chip. The
 * restart label would look clickable and do nothing, since `requestRestart` only re-defers
 * while busy. `actionable: false` tells `tray-menu.js` to draw a plain row, not a button.
 */
function trayItem(state) {
  if (state.install !== 'self' || !['ready', 'deferred'].includes(state.phase)) return null;
  if (state.phase === 'deferred') return { label: UPDATE_COPY.deferred, sublabel: state.version, actionable: false };
  return { label: UPDATE_COPY.trayRestart, sublabel: state.version, actionable: true };
}

/** «Actualización lista» goes out once per version, only idle, only with the window hidden. */
function shouldNotify(state, { windowVisible, notifiedVersion }) {
  return (
    state.install === 'self' &&
    state.phase === 'ready' &&
    !state.busy &&
    !windowVisible &&
    notifiedVersion !== state.version
  );
}

function createActivity() {
  return { requested: false, phase: null, resumePending: false };
}

function reduceActivity(activity, event) {
  switch (event.type) {
    case 'turn-requested':
      return { ...activity, requested: true };
    case 'turn-request-failed':
    case 'turn-ended':
      return { ...activity, requested: false };
    case 'phase':
      return { ...activity, phase: event.phase };
    case 'resume-scheduled':
      return { ...activity, resumePending: true };
    case 'resume-settled':
      return { ...activity, resumePending: false };
    default:
      return activity;
  }
}

/**
 * What one core event means for "busy", in the order it must be applied. A resume that
 * starts becomes a requested turn before the pending resume is settled, so no moment in
 * between reads as idle. A turn ends with its `turn_complete` or `error`, after the
 * `service_error` that says whether it resumes.
 */
function activityFromCoreEvent(event) {
  switch (event && event.type) {
    case 'phase':
      return [{ type: 'phase', phase: event.phase }];
    case 'service_error':
      return event.resume ? [{ type: 'resume-scheduled' }] : [];
    case 'resume_started':
      return [{ type: 'turn-requested' }, { type: 'resume-settled' }];
    case 'resume_cancelled':
      return [{ type: 'resume-settled' }];
    case 'turn_complete':
    case 'error':
      return [{ type: 'turn-ended' }];
    default:
      return [];
  }
}

function isBusy(activity) {
  return (
    activity.requested ||
    Boolean(activity.resumePending) ||
    (activity.phase !== null && !TERMINAL_PHASES.has(activity.phase))
  );
}

module.exports = {
  PHASES,
  initialState,
  reduce,
  trayItem,
  shouldNotify,
  createActivity,
  reduceActivity,
  activityFromCoreEvent,
  isBusy,
};
