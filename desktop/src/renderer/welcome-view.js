'use strict';

/**
 * The welcome and connection screens as data (design 1d-1e; «Conectar otro servicio…»
 * opens the same 1e): fixed texts, the services «SERVICIO» lists, and what a test's answer
 * shows. Pure, so `cogito-lab`'s `tests/desktop/` imports it in Node; `welcome.js` draws it.
 *
 * Texts come from the design canvas (`COGITO v2.dc.html`, 1d, 1e and 1c) with three kinds
 * of change, each required by the handoff README or COGITO's contract:
 *   - no first person in system text («No pude llegar a» becomes «No se pudo llegar a»),
 *     per the README;
 *   - nothing untrue: «Se guarda cifrada en este equipo» becomes «Se guarda en este equipo,
 *     legible solo por tu usuario» (secrets.toml is owner-only, not encrypted), and the ok
 *     line gives the real count, never a sample model;
 *   - every failure shows the real log (status, endpoint, the service's own words,
 *     `retry-after`), per the README's rule for service errors.
 */

const WELCOME = {
  titleBar: 'bienvenida',
  hello: 'Bienvenido',
  lead: 'Programa y opera tu equipo, proyecto por proyecto.',
  nameLabel: 'TU NOMBRE',
  step1: 'Paso 1 de 2',
  step2: 'Paso 2 de 2',
  next: 'Continuar',
  // «Continuar» with the name empty: said under the field, not only by a shake.
  nameMissing: 'Falta tu nombre',
  connectTitle: 'Conecta un modelo',
  serviceLabel: 'SERVICIO',
  keyLabel: 'CLAVE API',
  urlLabel: 'URL BASE',
  modelLabel: 'MODELO',
  test: 'Probar',
  testing: 'Probando…',
  pickModel: 'Elegir modelo',
  keyNote: 'Se guarda en este equipo',
  back: 'Atrás',
  skip: 'Omitir',
  cancel: 'Cancelar',
  start: 'Empezar',
  connect: 'Conectar',
  retry: 'Reintentar',
  testingCloud: (host) => `Conectando con ${host}…`,
  // Cogito Motoris: started by Cogito from its folder, or already running somewhere.
  motorisInstallTab: 'Iniciar con Cogito',
  motorisRunningTab: 'Ya en marcha',
  folderLabel: 'CARPETA',
  configLabel: 'CONFIGURACIÓN',
  pickFolder: 'Elegir carpeta…',
  pickConfig: 'Elegir archivo .json…',
  optionalKeyLabel: 'CLAVE API (OPCIONAL)',
  motorisUrl: 'http://127.0.0.1:8095/v1',
  testingInstall: 'Leyendo la instalación…',
  // The live region while «Reintentar» asks the core to serve a model again.
  retrying: (model) => `Cargando ${model}…`,
  // A model the core could not serve (`GET /settings/welcome`'s `unserved`, or a refused
  // activation). The generic title comes from the picker's `MODEL_PICKER`.
  missingKeyTitle: (model) => `Falta la clave · ${model}`,
  missingInstallTitle: (model) => `Falta la instalación · ${model}`,
  missingInstallDesc: 'Cogito Motoris no está preparado en esa carpeta.',
};

/** «SERVICIO»'s local row: Cogito Motoris, after the core's API services. */
const motorisService = { id: 'motoris', name: 'Cogito Motoris', tag: 'LOCAL' };

/** «SERVICIO»'s rows: the core's API services (`GET /connect/providers`), then Cogito
 * Motoris. */
function serviceList(providers) {
  const api = (Array.isArray(providers) ? providers : [])
    .filter((p) => p && typeof p.id === 'string' && typeof p.name === 'string')
    .map((p) => ({
      id: p.id,
      name: p.name,
      sub: p.custom_url ? 'Clave · URL' : 'Clave',
      kind: 'api',
      customUrl: Boolean(p.custom_url),
      baseUrl: typeof p.base_url === 'string' ? p.base_url : '',
      keyPage: Boolean(p.key_page),
    }));
  return [...api, { ...motorisService, sub: motorisService.tag, kind: 'motoris' }];
}

/** The hosts the core files as this machine (`provider/connect.is_local_url`): an entry
 * there needs no key. */
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

/** Whether «Probar» and «Conectar» need a key: a named service always, «Otro compatible»
 * unless its URL is on this machine (the core refuses to save one without, QA 2026-10-11). */
function needsKey(service, url) {
  if (!service || service.kind !== 'api') return false;
  if (!service.customUrl) return true;
  try {
    return !LOCAL_HOSTS.has(new URL(String(url || '').trim()).hostname);
  } catch {
    return true;
  }
}

/** What «Probar» cannot go on without: `'url'` (a service that takes its own address, left
 * empty), `'key'` (a key a remote service needs), or null. The address comes first, since
 * whether a key is needed depends on it (`needsKey`). */
function missingInput(service, { url = '', key = '' } = {}) {
  if (!service || service.kind !== 'api') return null;
  if (service.customUrl && !String(url || '').trim()) return 'url';
  if (!String(key || '').trim() && needsKey(service, url)) return 'key';
  return null;
}

/** A save the core refused (`{error}` from `connect.save`), as a failure `failureView`
 * words in Spanish. Known refusals get their own card; anything else keeps its text as
 * the card's log. */
function saveFailure(saved) {
  const error = String((saved && saved.error) || '');
  if (error === 'a key is required') return { kind: 'no_key' };
  if (error === 'own_server') return { kind: 'own_server' };
  if (error === 'base_url is not a usable address' || error === 'url is not a usable address') return { kind: 'bad_url' };
  return { kind: 'failed', log: error ? [error] : [] };
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return String(url || '');
  }
}

function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

/** The green line under a test that passed: `api`, `motoris-install` (the model and window
 * the install names) or `motoris-running` (how many models it answers with). */
function okLine(kind, answer) {
  if (kind === 'motoris-install') {
    const ctx = Number(answer.ctx);
    // Same form as the Modelo tab (4e: «64k»).
    return Number.isFinite(ctx) && ctx > 0 ? `✓ ${answer.model} · contexto ${Math.round(ctx / 1024)}k` : `✓ ${answer.model}`;
  }
  if (kind === 'motoris-running') return `✓ Cogito Motoris responde: ${plural((answer.models || []).length, 'modelo', 'modelos')}.`;
  return `✓ Conectado · ${plural((answer.models || []).length, 'modelo', 'modelos')}`;
}

/** The real log under a failure: status and endpoint, the service's words, retry-after,
 * and any lines the core sent (a server's own error). */
function failureLog(failure) {
  const lines = [];
  // The request as made, with its real method.
  const request = failure.endpoint ? [failure.method, failure.endpoint].filter(Boolean).join(' ') : '';
  const head = [failure.status, request].filter(Boolean).join(' · ');
  if (head) lines.push(head);
  if (failure.message) lines.push(String(failure.message));
  if (failure.retry_after) lines.push(`retry-after: ${failure.retry_after}`);
  for (const line of failure.log || []) if (typeof line === 'string' && line.trim()) lines.push(line);
  return lines;
}

/**
 * What a failed test shows: `{title, desc, action}` (the design's error card) plus `log`.
 * `action` is `{label, link}` for a link the main process opens (`key:<id>`).
 * `retry: false` marks a card for something missing (no key, no address): nothing was sent,
 * so there is nothing to try again until the field is filled.
 */
function failureView(failure, { service = null } = {}) {
  const f = failure || {};
  const serviceName = service ? service.name : '';
  const host = f.host || hostOf(f.endpoint);
  const log = failureLog(f);
  const keyLink = service && service.keyPage ? { label: 'Obtener clave ↗', link: `key:${service.id}` } : null;
  switch (f.kind) {
    case 'unauthorized':
      return { title: 'Clave no válida', desc: `El servicio respondió ${f.status || 401}.`, action: keyLink, log };
    case 'no_key':
      return { title: 'Falta la clave', desc: '', action: keyLink, log: [], retry: false };
    case 'no_url':
      return { title: 'Falta la URL', desc: '', action: null, log: [], retry: false };
    case 'unreachable':
      return { title: 'Sin conexión', desc: `No se pudo llegar a ${host}.`, action: null, log };
    case 'timeout':
      return { title: `La API no respondió · ${serviceName}`, desc: `${host} no respondió a tiempo.`, action: null, log };
    case 'rate_limited':
      return { title: `Límite de uso alcanzado · ${serviceName}`, desc: f.retry_after ? `Se reanuda en ${f.retry_after} s.` : '', action: null, log };
    case 'invalid':
      return { title: 'La respuesta no es una lista de modelos', desc: '', action: null, log };
    case 'insecure':
      return { title: 'La clave solo viaja por https', desc: 'La URL debe empezar con https://.', action: null, log };
    case 'bad_url':
      return { title: 'URL no válida', desc: 'Debe empezar con https://.', action: null, log: [] };
    case 'busy':
      return { title: 'Hay una tarea en curso', desc: 'El modelo se prueba cuando termine.', action: null, log };
    case 'http':
      return { title: `El servicio respondió ${f.status}`, desc: '', action: null, log };
    default:
      return { title: 'No se pudo conectar', desc: '', action: null, log };
  }
}

/**
 * What a Cogito Motoris test that failed shows: `{title, text, log}`, and `retry: false`
 * when nothing was sent. `install` is true for
 * «Iniciar con Cogito», where `not_motoris` means the folder or the file, and the core's own
 * Spanish line says which.
 */
function motorisFailureView(failure, { install = false } = {}) {
  const f = failure || {};
  const log = failureLog(f);
  const host = f.host || hostOf(f.endpoint);
  switch (f.kind) {
    case 'no_server':
      return { title: 'Cogito Motoris no responde', text: `Nada responde en ${host}.`, log };
    case 'not_motoris':
      if (install) {
        return { title: 'No es una instalación de Cogito Motoris', text: typeof f.message === 'string' ? f.message : '', log };
      }
      return { title: 'Otro servidor', text: 'Ahí responde otro servidor, no Cogito Motoris.', log };
    case 'plain_http':
      return { title: 'Hace falta https', text: 'En otra máquina, Cogito Motoris solo se usa por https.', log };
    // Cogito starts a Cogito Motoris at that address already.
    case 'own_server':
      return { title: 'Ya está en la lista', text: 'Ese Cogito Motoris lo arranca Cogito; ya está en la lista.', log: [], retry: false };
    case 'bad_url':
      return { title: 'URL no válida', text: '', log: [] };
    // Nothing was sent: there is nothing to try again until the field is filled.
    case 'no_url':
      return { title: 'Falta la URL', text: '', log: [], retry: false };
    case 'no_key':
      return { title: 'Falta la clave', text: 'En otra máquina hace falta una clave.', log: [], retry: false };
    case 'invalid':
      return { title: 'La respuesta no es una lista de modelos', text: '', log };
    case 'busy':
      return { title: 'Hay una tarea en curso', text: 'El modelo se prueba cuando termine.', log };
    default:
      return { title: 'No se pudo conectar', text: '', log };
  }
}

/**
 * The card for a model the core could not serve. `unserved` is `{reason, model: {id, label},
 * log}` (`GET /settings/welcome`, or a refused activation's answer). `picker` is strings.js'
 * `MODEL_PICKER`, whose title the model menu uses for the same refusal. Any reason but a
 * missing key or install is the generic card, with the core's log.
 */
function unservedView(unserved, picker) {
  const u = unserved || {};
  const model = (u.model && (u.model.label || u.model.id)) || '';
  const log = Array.isArray(u.log) ? u.log.filter((line) => typeof line === 'string' && line.trim()) : [];
  switch (u.reason) {
    case 'missing_key':
      return { title: WELCOME.missingKeyTitle(model), desc: '', log };
    case 'missing_install':
      return { title: WELCOME.missingInstallTitle(model), desc: WELCOME.missingInstallDesc, log };
    default:
      return { title: picker.failedTitle(model), desc: '', log };
  }
}

/** The 1f question, with the name when there is one. */
function greeting(name) {
  const clean = typeof name === 'string' ? name.trim() : '';
  return clean ? `¿En qué trabajamos hoy, ${clean}?` : '¿En qué trabajamos hoy?';
}

export {
  WELCOME,
  motorisService,
  serviceList,
  okLine,
  failureLog,
  failureView,
  motorisFailureView,
  unservedView,
  greeting,
  hostOf,
  needsKey,
  missingInput,
  saveFailure,
};
