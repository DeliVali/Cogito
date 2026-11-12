'use strict';

/**
 * What the window shows for an update (design 3a/3b), as plain data. `updateView(state)`
 * covers the title-bar chip, its menu, the security strip, the restarting screen and the
 * package-manager card. `separatorView(outcome)` covers the thread separator after a
 * restart. «Actualización lista» is shown by the notifier, not here. `render-update.js`
 * builds the DOM; this module never touches it, so every rule here is testable without a
 * browser.
 *
 * `state` is the main process's snapshot (`src/main/updater/controller.js`):
 * `{phase, install, current, version, percent, security, notes, busy, command,
 * failureReason, cardAutoOpen}`. Rules from design section 3a:
 *
 * - downloading shows a quiet indicator, no menu;
 * - ready shows «Reiniciar para actualizar», whose menu offers «Reiniciar ahora» when idle
 *   and «Reiniciar al terminar» while working, plus «Al salir»;
 * - deferred shows «Se actualiza al terminar» and opens the same menu;
 * - security replaces the chip and its menu with the amber strip (no «Al salir»), with
 *   «Reiniciar ahora» only while idle;
 * - a package-manager install shows «Hay una versión nueva», which only opens the card.
 */

import { UPDATE } from './strings.js';

function sections(notes) {
  const list = [];
  if (notes?.tools?.length) list.push({ heading: UPDATE.tools, items: notes.tools });
  if (notes?.improvements?.length) list.push({ heading: UPDATE.improvements, items: notes.improvements });
  return list;
}

/** The reason shown with «No se pudo actualizar». `code` is the `UpdateError` code
 * (`main/updater/errors.js`) carried on the `failed` event (`updater/state.js`'s `reduce`).
 * Codes a person can run into get a written sentence; the rest get a generic one that still
 * names the code. */
function failureReasonText(code) {
  if (!code) return null;
  return UPDATE.failureReasons[code] || UPDATE.failureReasonFallback(code);
}

/** Failures a retry cannot fix. A translocated macOS app (`updater/mac.js`'s `BLOCKED`)
 * must be moved to /Applications first, so «Reintentar» would fail the same way. The chip
 * opens the reason instead. */
const NOT_RETRYABLE = new Set(['BLOCKED']);

function chipFor(state) {
  if (state.install === 'package-manager') {
    return state.phase === 'ready' ? { kind: 'package-manager', label: UPDATE.packageManagerChip, opens: 'card' } : null;
  }
  switch (state.phase) {
    case 'downloading':
      return { kind: 'downloading', label: UPDATE.downloading(state.version, state.percent), percent: state.percent, opens: null };
    case 'ready':
      return state.security ? null : { kind: 'ready', label: UPDATE.ready, opens: 'menu' };
    case 'deferred':
      return state.security ? null : { kind: 'deferred', label: UPDATE.deferred, opens: 'menu' };
    case 'failed': {
      const tooltip = failureReasonText(state.failureReason);
      if (NOT_RETRYABLE.has(state.failureReason)) {
        return { kind: 'failed', label: UPDATE.failedFinal, opens: 'card', tooltip };
      }
      return { kind: 'failed', label: UPDATE.failed, opens: null, action: 'retry', tooltip };
    }
    default:
      return null;
  }
}

function menuFor(state) {
  if (state.install !== 'self' || state.security || !['ready', 'deferred'].includes(state.phase)) return null;
  return {
    title: UPDATE.menuTitle(state.version),
    installed: UPDATE.installed(state.current),
    sections: sections(state.notes),
    // «Al salir» cancels a pending «Reiniciar al terminar» back to plain `ready`
    // (`updater/state.js`'s `defer-to-quit`). Closing the menu is not enough: install-on-quit
    // happens anyway (`updater/controller.js`'s `beforeQuit`), and a pending restart would
    // still fire once the session goes idle.
    later: { label: UPDATE.atQuit, action: 'defer-to-quit' },
    primary: state.busy
      ? { label: UPDATE.restartWhenDone, action: 'restart-when-done' }
      : { label: UPDATE.restartNow, action: 'restart-now' },
  };
}

function bannerFor(state) {
  if (state.install !== 'self' || !state.security || !['ready', 'deferred'].includes(state.phase)) return null;
  return {
    title: UPDATE.securityTitle(state.version),
    text: state.busy ? UPDATE.securityBusy : UPDATE.securityIdle,
    restart: state.busy ? null : UPDATE.restartNow,
  };
}

function cardFor(state) {
  // The card behind a non-retryable failure's chip: the reason, and no button that would
  // only fail again. «Entendido» is the only way out, as on the package-manager card below.
  if (state.install === 'self' && state.phase === 'failed' && NOT_RETRYABLE.has(state.failureReason)) {
    return {
      kind: 'failed',
      title: UPDATE.blockedTitle,
      body: failureReasonText(state.failureReason),
      installed: UPDATE.installed(state.current),
      dismiss: UPDATE.understood,
      autoOpen: false,
    };
  }
  if (state.install !== 'package-manager' || state.phase !== 'ready' || !state.command) return null;
  return {
    kind: 'package-manager',
    title: UPDATE.packageManagerTitle(state.version),
    body: UPDATE.packageManagerBody,
    command: state.command,
    copy: UPDATE.copy,
    installed: UPDATE.installed(state.current),
    dismiss: UPDATE.understood,
    autoOpen: Boolean(state.cardAutoOpen),
  };
}

export function updateView(state) {
  if (!state) return { chip: null, menu: null, banner: null, restarting: null, card: null };
  return {
    chip: chipFor(state),
    menu: menuFor(state),
    banner: bannerFor(state),
    restarting: state.phase === 'restarting' ? { label: UPDATE.restarting(state.version) } : null,
    card: cardFor(state),
  };
}

/** «— ACTUALIZADO A X —» with «Ver novedades», or «— SE MANTUVO X · NO SE PUDO ACTUALIZAR —». */
export function separatorView(outcome) {
  if (!outcome) return null;
  if (outcome.kind === 'kept') return { kind: 'kept', label: UPDATE.kept(outcome.version) };
  if (outcome.kind !== 'updated') return null;
  const notes = sections(outcome.notes);
  return {
    kind: 'updated',
    label: UPDATE.updated(outcome.version),
    show: notes.length ? UPDATE.showNotes : null,
    hide: notes.length ? UPDATE.hideNotes : null,
    sections: notes,
  };
}
