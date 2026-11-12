'use strict';

/**
 * The in-thread card for a provider failure that stopped the turn (design 1j, "Errores de
 * servicio"), from the core's `service_error` event (core/cogito/server/app.py). Pure and
 * DOM-free, like `rewind.js`; `render-service-error.js` draws it.
 *
 * Every word on it is fixed copy from strings.js (`SERVICE_ERROR`) or the provider's own:
 * status and code, endpoint, message, `retry-after`. The one exception is the core's
 * sentence for a model with no API key (`config/secrets.py`, `missing_key_reason`), shown as
 * «Falta la clave de API…» (`missingKeyReason`).
 *
 * «Se reanuda en» appears only while the core has a resume of this turn scheduled
 * (`event.resume`: the provider sent `retry-after` and the turn has automatic resumes left),
 * counting down to its `at`. The core runs that resume whether the window is open or not.
 * Without one (no `retry-after`, or past the cap) the card offers only «Reintentar».
 */

import { SERVICE_ERROR, missingKeyReason } from './strings.js';
import { escapeControlChars } from './sanitize.js';
import { formatCount, formatWait, formatSeconds as formatRetryAfter } from './format.js';

const clean = (text) => escapeControlChars(String(text));

function parseEndpoint(endpoint) {
  try {
    const url = new URL(endpoint);
    return { host: url.host, path: url.pathname };
  } catch {
    return { host: '', path: '' };
  }
}

const isCount = (value) => typeof value === 'number' && Number.isFinite(value) && value > 0;

/** A request the server refused as too large for the model's context window. Uses the
 * server's own numbers when it gave both, else names the service. */
function overflowTitle(event, service) {
  if (isCount(event.requested_tokens) && isCount(event.available_tokens)) {
    return SERVICE_ERROR.overflowTitle(SERVICE_ERROR.overflowCount(formatCount(event.requested_tokens), formatCount(event.available_tokens)));
  }
  return SERVICE_ERROR.overflowTitle(service);
}

/** An HTTP error status the service sent: it answered, with an error. A request timeout
 * (408) or a gateway's (504) is still «no respondió». */
function answeredWithError(status) {
  return typeof status === 'number' && status >= 400 && status !== 408 && status !== 504;
}

/**
 * `event`: the `service_error` event. `nowMs`: this window's clock, passed in so a test can
 * pin it. Returns `{title, said, note, log: [{strong, text, faint}], resume: {label, seconds} |
 * null, retry, retryAfterModelChange, changeModel, wait}`. `wait` is null unless a resume is
 * scheduled. For a context overflow `retry` is null (the same request on the same model
 * would be refused again) and `retryAfterModelChange` is «Reintentar»: a model with a larger
 * window may take it, and the engine keeps the turn resumable on it (`Engine.use_model`).
 * `said` is the one Spanish sentence of what happened, shown in the card; `log` is the
 * provider's own words and stays behind «Detalle técnico». `note` is null except when the
 * core says its own prompt and tools do not fit (`code: fixed_part_too_large`): then the
 * card says so in one sentence.
 */
function serviceErrorView(event, nowMs = Date.now()) {
  const service = clean(event.service || '');
  const overflow = event.category === 'context_overflow';
  let title;
  let happened;
  if (overflow) {
    title = overflowTitle(event, service);
    happened = SERVICE_ERROR.said.overflow;
  } else if (event.category === 'auth') {
    // A 401/403 is the key, and saying only «no pudo completar» hid that (QA rig, 2026-10-16).
    title = SERVICE_ERROR.authTitle(service);
    happened = SERVICE_ERROR.said.auth;
  } else if (event.kind === 'limit') {
    title = SERVICE_ERROR.limitTitle(service);
    happened = SERVICE_ERROR.said.limit;
  } else if (answeredWithError(event.status)) {
    title = SERVICE_ERROR.errorTitle(service);
    happened = SERVICE_ERROR.said.error;
  } else {
    title = SERVICE_ERROR.noResponseTitle(service);
    happened = SERVICE_ERROR.said.noResponse;
  }
  const { host, path } = parseEndpoint(event.endpoint || '');
  const head = clean([event.status, event.code].filter((part) => part !== null && part !== undefined && part !== '').join(' '));
  const log = [];
  if (head || path) log.push({ strong: head, text: path ? clean(`POST ${path}`) : '', faint: false });
  const message = typeof event.message === 'string' ? missingKeyReason(event.message) || event.message : event.message;
  const said = clean([host, message].filter(Boolean).join(' · '));
  if (said) log.push({ strong: '', text: said, faint: false });
  const hasRetryAfter = typeof event.retry_after === 'number' && Number.isFinite(event.retry_after);
  if (hasRetryAfter) log.push({ strong: '', text: `retry-after: ${formatRetryAfter(event.retry_after)}`, faint: true });

  let resume = null;
  const dueAt = event.resume && !overflow ? Date.parse(event.resume.at) : NaN;
  if (Number.isFinite(dueAt)) {
    const remaining = (dueAt - nowMs) / 1000;
    if (remaining > 0) resume = { label: SERVICE_ERROR.resumesIn(formatWait(remaining)), seconds: remaining };
  }
  const fixedPart = overflow && event.code === 'fixed_part_too_large';
  return {
    title,
    // The core's own sentence for "nothing fits this window" already says it.
    said: fixedPart ? null : happened,
    note: fixedPart ? SERVICE_ERROR.fixedPartNote : null,
    log,
    resume,
    retry: overflow ? null : SERVICE_ERROR.cardRetry,
    retryAfterModelChange: overflow ? SERVICE_ERROR.cardRetry : null,
    changeModel: SERVICE_ERROR.changeModel,
    // «Esperar» only while there is something to wait for: the core's own resume.
    wait: resume ? SERVICE_ERROR.wait : null,
  };
}

/** A turn that failed in the core itself (`error` event): `{title, detail}` for the thread's
 * error card. A removed project folder is named in Spanish with the folder (QA rig,
 * 2026-10-16: the card showed «FileNotFoundError: … .cogito/wal/….jsonl»); anything else
 * keeps the core's own message, as before. */
function turnErrorView(event) {
  if (event && event.code === 'project_missing') {
    return { title: SERVICE_ERROR.projectMissing, detail: clean(event.path || '') };
  }
  return { title: '', detail: event && event.message ? clean(event.message) : '' };
}

export { formatWait, serviceErrorView, turnErrorView };
