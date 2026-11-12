'use strict';

/**
 * The model chip's menu as data (design 1a, and 1c's «Modelo · barra superior»), drawn by
 * `model-menu.js`. Pure, so `cogito-lab`'s `tests/desktop/` can import it in Node.
 *
 * - `GET /models` gives the catalog grouped by origin, in `cogito models list` order.
 *   Inside each origin, models are grouped by service («Cogito Motoris LOCAL», «CLAUDE API»),
 *   the design's section headers. The model serving now (`active`) carries «✓» and its
 *   section's dot is lit.
 * - A model the core marks `missing` (no key, no install, an unimplemented dialect)
 *   cannot be chosen. A service whose models all lack a key is «Sin configurar», with the
 *   design's note.
 * - While a turn runs, every model is disabled with no text: nothing switches the model a
 *   turn is using. While a switch runs, the chosen model reads «Cargando…» and the rest dim.
 */

import { MODEL_PICKER } from './strings.js';

/**
 * `catalog`: `GET /models`'s answer, or null when it could not be read (an empty menu).
 * `busy`: a turn is running. `loading`: the id being switched to, or null.
 * Returns `{sections: [{service, origin, lit, status, note, items: [...]}]}`; each item is
 * `{id, label, check, sub, disabled, loading, dim}`, where `sub` is what the row shows at
 * its right: «Cargando…» while it loads, else nothing.
 */
function _section(service, origin, models, { active, busy, loading }) {
  const items = models.map((model) => {
    const isLoading = loading !== null && model.id === loading;
    return {
      id: model.id,
      label: model.label || model.id,
      check: model.id === active ? MODEL_PICKER.check : '',
      sub: isLoading ? MODEL_PICKER.loading : '',
      disabled: busy || loading !== null || Boolean(model.missing),
      loading: isLoading,
      dim: loading !== null && !isLoading,
    };
  });
  const unset = models.length > 0 && models.every((model) => model.missing === 'key');
  return {
    service,
    origin,
    lit: items.some((item) => item.check !== ''),
    status: unset ? MODEL_PICKER.unsetStatus : '',
    note: unset ? MODEL_PICKER.unsetNote : '',
    items,
  };
}

function pickerView(catalog, { busy = false, loading = null } = {}) {
  const active = catalog && typeof catalog.active === 'string' ? catalog.active : null;
  const sections = [];
  const opts = { active, busy, loading };
  for (const group of (catalog && catalog.groups) || []) {
    const byService = new Map();
    for (const model of group.models || []) {
      // An older core names no service: use the origin's word, as the design writes it.
      const service = model.service || String(group.origin || '').toUpperCase();
      if (!byService.has(service)) byService.set(service, []);
      byService.get(service).push(model);
    }
    for (const [service, models] of byService) {
      // Two connected servers can report the exact same service name («Cogito Motoris
      // LOCAL») at different ports (item B); grouped as one they would mix two
      // servers' models under one header with no way to tell them apart. Split by port,
      // and only then does the header carry it — a single port keeps the plain name.
      const ports = new Set(models.map((model) => model.port).filter((port) => port != null));
      if (ports.size <= 1) {
        sections.push(_section(service, group.origin, models, opts));
        continue;
      }
      const byPort = new Map();
      for (const model of models) {
        const key = model.port ?? '';
        if (!byPort.has(key)) byPort.set(key, []);
        byPort.get(key).push(model);
      }
      for (const [port, portModels] of byPort) {
        const header = port ? `${service} · :${port}` : service;
        sections.push(_section(header, group.origin, portModels, opts));
      }
    }
  }
  return { sections };
}

/**
 * What a failed switch shows in the thread, in the service-error card's style: the title
 * and the core's reason, line by line. Shown for `failed` and for any other refusal the core
 * explains with a log, and for a 404. Null when there is no failure to show: the switch happened, or the
 * menu already shows the refusal (a turn running, a switch in flight, a model that cannot
 * be chosen).
 */
function switchFailureView(answer) {
  if (!answer || !answer.error) return null;
  const model = answer.model || {};
  const name = model.label || model.id || '';
  const log = Array.isArray(answer.log) ? answer.log.filter((line) => typeof line === 'string' && line.trim()) : [];
  // A 404 (an unknown model, or no route) is a failure the menu does not show either.
  if (!log.length && answer.status === 404) log.push(String(answer.error));
  if (answer.error !== 'failed' && !log.length) return null;
  return { title: MODEL_PICKER.failedTitle(name), log };
}

export { pickerView, switchFailureView };
