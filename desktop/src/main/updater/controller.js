'use strict';

/**
 * The updater's composition. Every Electron dependency is passed in so the lab can test it
 * without Electron; `index.js` is the thin adapter that passes the real ones.
 *
 * - Backend events (`download-started`, `download-progress`, `ready`, `failed`,
 *   `available`) and agent activity become state through `state.js`. Every change goes to
 *   the window as one snapshot, and the tray item follows it.
 * - Checks run shortly after start and then every few hours, only while nothing is
 *   downloaded yet. A ready update is never replaced behind the person's back.
 * - Before any install (restart or quit), `pending {from, to, notes}` is recorded. The next
 *   start then says «— ACTUALIZADO A X —» or «— SE MANTUVO X · NO SE PUDO ACTUALIZAR —»
 *   depending on which version comes up. The outcome is handed to the window once.
 * - A restart flags the app as quitting first (otherwise the window's close handler keeps
 *   it alive), then installs, then quits. If the install cannot start, the flag is cleared
 *   and the state becomes failed. The app keeps running as it was.
 * - «Actualización lista» follows `shouldNotify` and goes out through `notify`, slice B's
 *   notifier. It shows its own toast window where the system notification cannot (an
 *   unsigned macOS app, since Electron 42).
 */

const { deriveOutcome } = require('./store');
const { initialState, reduce, trayItem, shouldNotify, createActivity, reduceActivity, isBusy } = require('./state');

const DEFAULT_SCHEDULE = Object.freeze({
  firstCheckMs: 20_000, // after start, so a check never competes with the core loading a model
  intervalMs: 4 * 60 * 60 * 1000,
  restartPaintMs: 600, // long enough for the window to paint «Actualizando a X»
});

const CHECKABLE = new Set(['none', 'updated']);

function createUpdaterController({
  currentVersion,
  installInfo,
  store,
  backend,
  command = null,
  send,
  setTrayItem,
  notify,
  isWindowVisible,
  showWindow,
  setQuitting,
  quit,
  copyText,
  log = () => {},
  schedule = DEFAULT_SCHEDULE,
}) {
  const stored = store.read();
  let outcome = deriveOutcome(stored.pending, currentVersion);
  if (stored.pending) store.write({ pending: null });
  if (outcome) backend?.cleanup?.();

  let state = initialState({
    install: installInfo.install,
    currentVersion,
    outcome,
    command,
    packageManager: installInfo.packageManager,
  });
  let activity = createActivity();
  let installedOnQuit = false;
  const timers = [];

  function snapshot() {
    const { cardShownVersion } = store.read();
    return {
      ...state,
      cardAutoOpen: state.install === 'package-manager' && state.phase === 'ready' && cardShownVersion !== state.version,
    };
  }

  function maybeNotify() {
    const { notifiedVersion } = store.read();
    if (!shouldNotify(state, { windowVisible: isWindowVisible(), notifiedVersion })) return;
    const { version } = state;
    store.write({ notifiedVersion: version });
    notify({
      version,
      onRestart: () => dispatch({ type: 'restart-now' }),
      onShowWindow: showWindow,
    });
  }

  function publish() {
    send('update:state', snapshot());
    setTrayItem(trayItem(state));
    maybeNotify();
  }

  function recordPending() {
    store.write({ pending: { from: currentVersion, to: state.version, notes: state.notes } });
  }

  function restart() {
    timers.push(
      setTimeout(() => {
        // A turn may have started, or a resume been scheduled, while the window painted
        // «Actualizando a X». Never install under it.
        if (state.phase !== 'restarting') return;
        if (isBusy(activity)) {
          dispatch({ type: 'restart-postponed' });
          return;
        }
        try {
          recordPending();
          setQuitting(true);
          backend.installSync('restart');
          quit();
        } catch (error) {
          setQuitting(false);
          store.write({ pending: null });
          log(`restart failed: ${error.message}`);
          dispatch({ type: 'restart-failed', reason: error.code });
        }
      }, schedule.restartPaintMs),
    );
  }

  async function checkNow() {
    if (!backend || !CHECKABLE.has(state.phase)) return;
    try {
      await backend.check();
    } catch (error) {
      log(`update check failed: ${error.message}`);
    }
  }

  function dispatch(event) {
    const { state: next, effects } = reduce(state, event);
    const changed = next !== state;
    state = next;
    if (changed) publish();
    for (const effect of effects) {
      if (effect === 'restart') restart();
      else if (effect === 'check') checkNow();
    }
  }

  return {
    snapshot,
    checkNow,

    start() {
      backend?.start((event) => dispatch(event));
      publish();
      if (!backend) return;
      timers.push(setTimeout(checkNow, schedule.firstCheckMs));
      timers.push(setInterval(checkNow, schedule.intervalMs));
    },

    stop() {
      for (const timer of timers) clearTimeout(timer);
      timers.length = 0;
    },

    noteActivity(event) {
      activity = reduceActivity(activity, event);
      const busy = isBusy(activity);
      if (busy !== state.busy) dispatch({ type: 'busy', busy });
    },

    restartNow() {
      dispatch({ type: 'restart-now' });
    },

    restartWhenDone() {
      dispatch({ type: 'restart-when-done' });
    },

    /** «Al salir»: cancels a pending deferred restart back to plain `ready`. See
     * `state.js`'s `defer-to-quit` for why the two states differ. */
    deferToQuit() {
      dispatch({ type: 'defer-to-quit' });
    },

    retry() {
      dispatch({ type: 'retry' });
    },

    cardSeen() {
      if (state.version) store.write({ cardShownVersion: state.version });
      publish();
    },

    copyCommand() {
      if (state.command) copyText(state.command);
    },

    takeOutcome() {
      const taken = outcome;
      outcome = null;
      return taken;
    },

    windowVisibilityChanged() {
      maybeNotify();
    },

    /** From `before-quit`: an update that is ready installs on the way out ("Al salir"). */
    beforeQuit() {
      if (installedOnQuit || state.install !== 'self' || !['ready', 'deferred'].includes(state.phase)) return;
      installedOnQuit = true;
      try {
        recordPending();
        backend.installSync('quit');
      } catch (error) {
        store.write({ pending: null });
        log(`install on quit failed: ${error.message}`);
      }
    },
  };
}

module.exports = { createUpdaterController, DEFAULT_SCHEDULE };
