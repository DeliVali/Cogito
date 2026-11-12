'use strict';

/**
 * "Reiniciar contexto" (design 1a): the confirm-then-reset flow, pure and DOM-free so it
 * can be tested without a browser. It reuses the rewind dialog's widget (`buildConfirmDialog`
 * in render-rewind.js) and the RESET strings; everything that touches the page is injected,
 * so chat.js wires the real DOM and the tests wire fakes.
 */

import { buildConfirmDialog } from './render-rewind.js';
import { RESET } from './strings.js';

/** The confirm dialog's model, from the RESET strings: the shape `buildConfirmDialog` takes. */
function resetConfirmModel(strings = RESET) {
  return {
    title: strings.confirmTitle,
    body: strings.confirmBody,
    confirmLabel: strings.confirmGo,
    cancelLabel: strings.confirmCancel,
    inProgressLabel: strings.inProgress,
  };
}

/**
 * Drives one "reset context" from the button: show the confirm dialog, and on "Reiniciar"
 * call `POST /sessions/{id}/reset`, then clear the window. A refusal (409: a turn or rewind
 * slipped in, nothing was reset) is said once and the thread is left alone.
 *
 * Injected deps:
 *  - `mount(el)`: place the confirm dialog in the thread.
 *  - `resetContext()`: `window.cogito.resetContext()`; resolves to the core's body
 *    (`{id}` on success, `{error, ...}` on a refusal).
 *  - `clearThread()`: wipe the thread view (column, orb, chip).
 *  - `announce(text)`: the screen-reader status line, for a refusal.
 *  - `onBusyChange()`: called whenever a reset starts or ends, so the caller can disable
 *    the button while one is under way (`busy()` reports it).
 *  - `buildDialog` / `strings`: overridable for tests.
 */
function createResetFlow({
  mount,
  resetContext,
  clearThread,
  announce = () => {},
  onBusyChange = () => {},
  buildDialog = buildConfirmDialog,
  strings = RESET,
}) {
  let inFlight = false;

  function settle() {
    inFlight = false;
    onBusyChange();
  }

  function start() {
    if (inFlight) return;
    inFlight = true;
    onBusyChange();
    const dialog = buildDialog(resetConfirmModel(strings), {
      onConfirm: async () => {
        dialog.setInProgress(true);
        let answer;
        try {
          answer = await resetContext();
        } catch {
          answer = { error: 'failed' };
        }
        dialog.el.remove();
        settle();
        if (answer && answer.error) {
          announce(strings.busy);
          return;
        }
        clearThread();
      },
      onCancel: () => {
        dialog.el.remove();
        settle();
      },
    });
    mount(dialog.el);
    dialog.el.focus?.();
    return dialog;
  }

  return { start, busy: () => inFlight };
}

export { resetConfirmModel, createResetFlow };
