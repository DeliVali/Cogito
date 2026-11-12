'use strict';

/**
 * The tray menu's content (design 2b) as plain data. No Electron here, so `cogito-lab`'s
 * `tests/desktop/` reads it directly. `tray.js` turns it into a native menu that the system
 * draws (on Linux it takes the panel's theme); only the content follows the design.
 *
 * What it shows, from the core's `GET /usage` snapshot:
 *   API    the model and «Tokens de hoy».
 *   Local  the model, «Uso de GPU» only when the system reports one, and one line with
 *          tokens/s, today's tokens and «sin costo». The server is named for what it is.
 *   Never how much of a plan is left: «Límite de uso alcanzado» only while it holds, with
 *   «Reintentar ahora». A request too large for the context window reads «No cabe en el
 *   contexto · {servicio}» and offers «Cambiar modelo» instead.
 *
 * The design right-aligns each value beside its label, but a system menu has one text per
 * row. On Linux label and value are joined by a tab: KDE's menus (Qt via dbusmenu)
 * right-align what follows a tab, as with «Mostrar ventana»'s shortcut, and the tab arrives
 * intact (checked on the session bus, 2026-09-30). Elsewhere, where this was not checked,
 * they are joined with ": ". The only bar is GPU use while a local model answers, drawn as
 * a row of block characters so any system menu renders it.
 *
 * Near the per-minute limit (design 2e): «Cerca del límite por minuto · {n} %» under the
 * model while the core reports it (`active.rate_limit`); the icon does not change.
 * The header shows the orb in the state's colour (design «Orbe por estado»), an image
 * `tray.js` draws (`tray-images.js`).
 */

const { formatTokens, formatCount, formatSeconds } = require('../shared/format');

const TRAY_COPY = Object.freeze({
  header: (project) => (project ? `COGITO · ${project}` : 'COGITO'),
  states: Object.freeze({
    idle: 'En reposo',
    working: 'Trabajando',
    permission: 'Permiso',
    error: 'Error',
    done: 'Listo',
  }),
  stoppedByLimit: 'Detenido · límite de uso',
  // For a `no_response` failure (timeout, overload, raised error), as status-hub.js's
  // module comment describes, instead of «Detenido · límite de uso» (2026-09-29 UX audit).
  noResponse: (service) => `La API no respondió · ${service}`,
  // A request refused as too large for the model's context window. Same title as the card
  // (`SERVICE_ERROR.overflowTitle`, renderer/strings.js), plus the service.
  contextOverflow: (service) => `No cabe en el contexto · ${service}`,
  changeModel: 'Cambiar modelo',
  // The usage block's row for the same refusal. The row «Límite de uso alcanzado» with the
  // provider's raw code («context_unfit») named a usage limit that never happened.
  contextRow: 'Contexto',
  contextSizes: (requested, available) => `${formatCount(requested)} de ${formatCount(available)} tokens`,
  contextNoRoom: 'sin espacio',
  usage: (label) => `USO · ${label}`,
  model: 'Modelo',
  tokensToday: 'Tokens de hoy',
  gpu: 'Uso de GPU',
  // Neither figure below is this model's own share, so each says so (2026-09-29 UX audit).
  gpuTooltip: 'GPU más ocupada del equipo',
  tokensPerSecondTooltip: 'tokens/s de la última respuesta',
  // A non-breaking space before «%», as in the design's «80 %».
  rateNear: (percent) => `Cerca del límite por minuto · ${percent} %`,
  limit: 'Límite de uso alcanzado',
  noCost: 'sin costo',
  retry: 'Reintentar ahora',
  show: 'Mostrar ventana',
  quit: 'Salir de COGITO',
});

/** "429 rate_limit_error · retry-after: 42": the provider's own words, nothing added. */
function limitDetail(limit) {
  const head = [limit.status, limit.code].filter((part) => part !== null && part !== undefined && part !== '').join(' ');
  const retry = typeof limit.retry_after === 'number' ? `retry-after: ${formatSeconds(limit.retry_after)}` : '';
  return [head, retry].filter(Boolean).join(' · ');
}

/** `limit`: `usage.active.limit`, or null. `service`: the failed model's service label, for
 * «La API no respondió · {servicio}». */
function statusLine(state, detail, limit, service = '') {
  if (state === 'error' && limit) {
    if (limit.category === 'context_overflow') return TRAY_COPY.contextOverflow(service);
    if (limit.kind === 'no_response') return TRAY_COPY.noResponse(service);
    return TRAY_COPY.stoppedByLimit;
  }
  const label = TRAY_COPY.states[state] || TRAY_COPY.states.idle;
  return detail && (state === 'working' || state === 'permission') ? `${label} · ${detail}` : label;
}

/** A label and its value on one row: right-aligned after a tab on Linux (see above). */
function pairWith(platform) {
  const join = platform === 'linux' ? '\t' : ': ';
  return (label, value) => ({ type: 'label', label: `${label}${join}${value}` });
}

let row = pairWith(process.platform);

const BAR_CELLS = 16;

/** «███████░░░░░░░░░», the share `percent` of 100 in block characters. */
function barRow(percent) {
  const share = Math.max(0, Math.min(100, Number(percent) || 0));
  const full = Math.round((share / 100) * BAR_CELLS);
  return { type: 'label', label: '█'.repeat(full) + '░'.repeat(BAR_CELLS - full), bar: { percent: share } };
}

// Only a curated, recognised local server is known to be free. An unrecognised loopback
// address (`provider.service.describe`'s own-host fallback) could be a paid API behind a
// proxy (2026-09-29 UX audit, "sin costo").
const KNOWN_FREE_LOCAL = new Set(['Cogito Motoris']);

/** A request too large for the window: how big it was against the window, when the core
 * knows both; never the provider's code. */
function contextRow(limit) {
  const requested = Number(limit.requested_tokens);
  const available = Number(limit.available_tokens);
  const sizes = requested > 0 && available > 0 ? TRAY_COPY.contextSizes(requested, available) : TRAY_COPY.contextNoRoom;
  return row(TRAY_COPY.contextRow, sizes);
}

function usageRows(active) {
  const rows = [{ type: 'label', label: TRAY_COPY.usage(active.service?.label || '') }];
  // One model, one name everywhere (2026-09-29 UX audit, "Consistency"): `active.label` is
  // the catalog label the window's chip shows; `active.model` is the raw id.
  rows.push(row(TRAY_COPY.model, active.label || active.model));
  if (active.limit) {
    if (active.limit.category === 'context_overflow') rows.push(contextRow(active.limit));
    else rows.push(row(TRAY_COPY.limit, limitDetail(active.limit)));
  }
  if (active.origin !== 'local' && active.rate_limit && !active.rate_limit.recovered) {
    rows.push({ type: 'label', label: TRAY_COPY.rateNear(active.rate_limit.percent) });
  }
  if (active.origin === 'local') {
    // Neither figure is scoped to this model, so a tooltip says so (Windows, macOS;
    // Electron's Linux menus have no per-item tooltip). 2026-09-29 UX audit.
    if (active.gpu && typeof active.gpu.use_percent === 'number') {
      // A non-breaking space before «%» everywhere (2026-09-29 UX audit follow-up,
      // "Formatting sweep").
      rows.push({ ...row(TRAY_COPY.gpu, `${active.gpu.use_percent} %`), tooltip: TRAY_COPY.gpuTooltip });
      rows.push(barRow(active.gpu.use_percent));
    }
    const parts = [];
    if (typeof active.tokens_per_second === 'number') parts.push(`${Math.round(active.tokens_per_second)} tokens/s`);
    parts.push(`hoy ${formatTokens(active.tokens_today)} tokens`);
    if (KNOWN_FREE_LOCAL.has(active.service?.name)) parts.push(TRAY_COPY.noCost);
    rows.push({ type: 'label', label: parts.join(' · '), tooltip: TRAY_COPY.tokensPerSecondTooltip });
  } else {
    rows.push(row(TRAY_COPY.tokensToday, formatTokens(active.tokens_today)));
  }
  return rows;
}

/**
 * `usage`: the `GET /usage` snapshot, or null before the core has answered. `update`: the
 * updater's `{label, detail?}` item, or null (see `AppTray.setUpdateItem`). `shortcut`: the
 * accelerator that actually reaches "Mostrar ventana", or null when it could not be
 * registered. A shortcut that does nothing is never shown.
 */
function buildTrayMenu({
  platform = process.platform,
  project = '',
  state = 'idle',
  detail = '',
  usage = null,
  update = null,
  shortcut = null,
} = {}) {
  row = pairWith(platform);
  const active = usage && usage.active ? usage.active : null;
  const limit = active && active.limit ? active.limit : null;
  const items = [
    { type: 'label', label: TRAY_COPY.header(project), icon: { kind: 'orb', state } },
    { type: 'label', label: statusLine(state, detail, limit, active?.service?.label || '') },
  ];
  if (active) items.push({ type: 'separator' }, ...usageRows(active));
  items.push({ type: 'separator' });
  if (update) {
    // A deferred update (`update.actionable === false`) is a plain row, which `tray.js`'s
    // `_render` draws as a disabled item, not a button that does nothing (2026-09-29 UX
    // final verification, item 3).
    const label = update.detail ? `${update.label} ${update.detail}` : update.label;
    items.push(update.actionable === false ? { type: 'label', label } : { type: 'action', id: 'update', label });
    items.push({ type: 'separator' });
  }
  if (limit && state === 'error' && limit.category === 'context_overflow') {
    // The same request would be refused again: no «Reintentar ahora», only a model with a
    // larger window.
    items.push({ type: 'action', id: 'change-model', label: TRAY_COPY.changeModel });
  } else if (limit && state === 'error') {
    // Hidden once a resume is under way (state left 'error'), even if the polled
    // `active.limit` is stale: clicking it during the resumed turn only got a 409
    // (2026-09-29 UX audit, DEAD).
    items.push({ type: 'action', id: 'retry', label: TRAY_COPY.retry });
  }
  items.push({ type: 'action', id: 'show', label: TRAY_COPY.show, ...(shortcut ? { accelerator: shortcut } : {}) });
  items.push({ type: 'action', id: 'quit', label: TRAY_COPY.quit });
  return items;
}

/** The tooltip: who, and what it is doing. */
function trayTooltip({ project = '', state = 'idle', detail = '', usage = null } = {}) {
  const active = usage && usage.active ? usage.active : null;
  const limit = active && active.limit ? active.limit : null;
  return `${TRAY_COPY.header(project)} · ${statusLine(state, detail, limit, active?.service?.label || '')}`;
}

module.exports = { TRAY_COPY, buildTrayMenu, trayTooltip, formatTokens, limitDetail };
