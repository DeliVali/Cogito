'use strict';

/**
 * What the toast window draws for one notification spec. Pure, so `cogito-lab` can test it
 * without a window. The button that moves the work forward (Permitir, Reintentar ahora, Ver
 * cambios) is the primary one and goes last, on the right, as on the
 * approval card.
 */

import { escapeControlChars } from './sanitize.js';

const PRIMARY = new Set(['allow', 'retry', 'view-changes', 'restart']);

const KIND_OF = { allow: 'permission', retry: 'error', 'view-changes': 'done' };

function toastView(spec) {
  const actions = Array.isArray(spec.actions) ? spec.actions : [];
  const label = (action) => ({ ...action, label: escapeControlChars(String(action.label || '')) });
  const buttons = [
    ...actions.filter((action) => !PRIMARY.has(action.id)).map((action) => ({ ...label(action), primary: false })),
    ...actions.filter((action) => PRIMARY.has(action.id)).map((action) => ({ ...label(action), primary: true })),
  ];
  const kind = spec.kind || actions.map((action) => KIND_OF[action.id]).find(Boolean) || 'info';
  // Title and body carry text from the core (a command, a service name), so they are
  // escaped like the thread's rows.
  return {
    title: escapeControlChars(String(spec.title || '')),
    body: escapeControlChars(String(spec.body || '')),
    buttons,
    kind,
  };
}

/** The strip's time label: «ahora» (design 2d), then «hace N min» after the first minute. */
function whenLabel(elapsedMs) {
  const minutes = Math.floor(Math.max(0, Number(elapsedMs) || 0) / 60_000);
  return minutes < 1 ? 'ahora' : `hace ${minutes} min`;
}

export { toastView, whenLabel };
