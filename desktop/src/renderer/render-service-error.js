'use strict';

/**
 * DOM for `service-error.js`'s card (design 1j). A red header with the fixed title, the
 * provider's real log in a mono box, «Se reanuda en …» with a bar that drains over that long
 * when the provider gave a wait, then «Cambiar modelo» (the raised square) and the blue pill:
 * «Esperar» while the core has a resume scheduled, «Reintentar» otherwise. A request too
 * large for the model's context window gets «Cambiar modelo» alone, since no retry could make
 * it fit. The card says what happened in one Spanish sentence; the provider's own words (the
 * log) wait behind «Detalle técnico», collapsed (`appendTechnicalDetail`). Everything the
 * provider wrote goes through `textContent`, never `innerHTML`.
 * «Esperar» and «Reintentar» play `click` (`play`, sfx.js unless a test stands in);
 * «Cambiar modelo» opens the model menu, which plays its own `open`.
 */

import { serviceErrorView } from './service-error.js';
import { escapeControlChars } from './sanitize.js';
import { SERVICE_ERROR, MODEL_PICKER } from './strings.js';
import { formatWait } from './format.js';
import { sfx } from './sfx.js';

/**
 * Puts a card's technical `log` behind «Detalle técnico»: the log starts hidden, a toggle
 * above it shows and hides it. The provider's words are English and written for an engineer,
 * so the card leads with its own Spanish sentence (the tool rows keep their error the same
 * way). Shared by the service-error cards.
 */
function appendTechnicalDetail(body, log, documentRef = document) {
  log.hidden = true;
  const toggle = documentRef.createElement('button');
  toggle.type = 'button';
  toggle.className = 'service-error-detail-toggle';
  toggle.textContent = MODEL_PICKER.technicalDetail;
  toggle.setAttribute('aria-expanded', 'false');
  toggle.addEventListener('click', () => {
    log.hidden = !log.hidden;
    toggle.setAttribute('aria-expanded', String(!log.hidden));
  });
  body.append(toggle, log);
}

/**
 * `onRetry` runs when «Reintentar» is pressed. The button disables itself, so a failure is
 * retried at most once from here. `onChangeModel` («Cambiar modelo») opens the model menu.
 * «Esperar», shown only while the core has a resume scheduled, collapses the card and leaves
 * that resume alone. `timers` and `now` are for a test's fake clock. Returns the card element.
 */
function buildServiceErrorCard(
  event,
  {
    onRetry,
    onChangeModel,
    nowMs = Date.now(),
    documentRef = document,
    // Wrapped, not passed as bare references: `timers.setInterval(...)` calls them with
    // `this === timers`, and the real Window methods throw "Illegal invocation" on any
    // receiver but the global object.
    timers = { setInterval: (...args) => setInterval(...args), clearInterval: (...args) => clearInterval(...args) },
    now = Date.now,
    play = sfx,
  } = {},
) {
  const view = serviceErrorView(event, nowMs);
  const card = documentRef.createElement('div');
  card.className = 'service-error';
  card.setAttribute('role', 'alert');

  const head = documentRef.createElement('div');
  head.className = 'service-error-head';
  const dot = documentRef.createElement('span');
  dot.className = 'service-error-dot';
  head.append(dot, documentRef.createTextNode(view.title));

  const body = documentRef.createElement('div');
  body.className = 'service-error-body';

  if (view.said) {
    const said = documentRef.createElement('div');
    said.className = 'service-error-said';
    said.textContent = view.said;
    body.append(said);
  }
  if (view.note) {
    const note = documentRef.createElement('div');
    note.className = 'service-error-note';
    note.textContent = view.note;
    body.append(note);
  }

  if (view.log.length) {
    const log = documentRef.createElement('div');
    log.className = 'service-error-log';
    for (const line of view.log) {
      const row = documentRef.createElement('span');
      row.className = line.faint ? 'service-error-line is-faint' : 'service-error-line';
      if (line.strong) {
        const strong = documentRef.createElement('span');
        strong.className = 'service-error-code';
        strong.textContent = line.strong;
        row.append(strong);
        if (line.text) row.append(documentRef.createTextNode(' '));
      }
      if (line.text) row.append(documentRef.createTextNode(line.text));
      log.append(row);
    }
    appendTechnicalDetail(body, log, documentRef);
  }

  if (view.resume) {
    const resume = documentRef.createElement('div');
    resume.className = 'service-error-resume';
    const label = documentRef.createElement('span');
    label.textContent = view.resume.label;
    const track = documentRef.createElement('span');
    track.className = 'service-error-drain';
    const fill = documentRef.createElement('span');
    // CSSOM, not a style attribute: the page's CSP allows only its own stylesheet.
    fill.style.animationDuration = `${view.resume.seconds}s`;
    track.append(fill);
    resume.append(label, track);
    body.append(resume);
    // The label ticks once a second toward the same deadline the drain bar animates to.
    // `_clearTick` lets `collapseServiceErrorCard`/`settleServiceErrorCard` stop it with the
    // same `timers` this card was built with.
    const dueAt = nowMs + view.resume.seconds * 1000;
    let tickHandle = timers.setInterval(() => {
      const remaining = (dueAt - now()) / 1000;
      if (remaining <= 0) {
        timers.clearInterval(tickHandle);
        return;
      }
      label.textContent = SERVICE_ERROR.resumesIn(formatWait(remaining));
    }, 1000);
    resume._clearTick = () => timers.clearInterval(tickHandle);
  }

  const actions = documentRef.createElement('div');
  actions.className = 'service-error-actions';
  const change = documentRef.createElement('button');
  change.type = 'button';
  change.className = 'service-error-change';
  change.textContent = view.changeModel;
  change.addEventListener('click', () => {
    if (onChangeModel) onChangeModel();
  });
  actions.append(change);
  if (view.wait) {
    const wait = documentRef.createElement('button');
    wait.type = 'button';
    wait.className = 'service-error-wait';
    wait.textContent = view.wait;
    // The core's scheduled resume runs anyway. Only the card's detail goes; the head (what
    // failed) stays as the record of the failure.
    wait.addEventListener('click', () => {
      play('click');
      collapseServiceErrorCard(card);
    });
    actions.append(wait);
  }
  // A request too large for the model's window (`view.retry` null) has no «Reintentar» on
  // the same model. The button is drawn hidden and shown once another model serves the
  // session (`offerRetryAfterModelChange`).
  const retryLabel = view.retry || view.retryAfterModelChange;
  if (retryLabel) {
    const retry = documentRef.createElement('button');
    retry.type = 'button';
    retry.className = 'service-error-retry';
    retry.textContent = retryLabel;
    if (!view.retry) card.dataset.retryAfterModelChange = 'waiting';
    retry.addEventListener('click', () => {
      if (retry.disabled) return;
      retry.disabled = true;
      play('click');
      if (onRetry) onRetry();
    });
    // While the core will resume by itself, «Esperar» is the blue pill and there is nothing
    // to retry (design 1j's 429 card). A dropped resume brings «Reintentar» back
    // (`settleServiceErrorCard`).
    retry.hidden = Boolean(view.wait) || !view.retry;
    actions.append(retry);
  }
  body.append(actions);

  card.append(head, body);
  return card;
}

/**
 * A model the picker could not switch to (`model-picker.js`'s `switchFailureView`), in this
 * card's style: the title, the core's reason line by line in the mono box, with its own
 * spacing, and «Cambiar modelo», which opens the picker again. The model that was serving keeps serving.
 */
function buildModelFailureCard(view, { onChangeModel, documentRef = document } = {}) {
  const card = documentRef.createElement('div');
  card.className = 'service-error service-error--model';
  card.setAttribute('role', 'alert');
  const head = documentRef.createElement('div');
  head.className = 'service-error-head';
  const dot = documentRef.createElement('span');
  dot.className = 'service-error-dot';
  head.append(dot, documentRef.createTextNode(escapeControlChars(view.title)));
  const body = documentRef.createElement('div');
  body.className = 'service-error-body';
  if (view.log.length) {
    const log = documentRef.createElement('div');
    log.className = 'service-error-log';
    for (const line of view.log) {
      const row = documentRef.createElement('span');
      row.className = 'service-error-line';
      row.textContent = escapeControlChars(line);
      log.append(row);
    }
    body.append(log);
  }
  const actions = documentRef.createElement('div');
  actions.className = 'service-error-actions';
  const change = documentRef.createElement('button');
  change.type = 'button';
  change.className = 'service-error-change';
  change.textContent = SERVICE_ERROR.changeModel;
  change.addEventListener('click', () => {
    if (onChangeModel) onChangeModel();
  });
  actions.append(change);
  body.append(actions);
  card.append(head, body);
  return card;
}

/**
 * The model the core started with could not be served (`GET /settings/welcome`'s
 * `unserved`, reason `failed`; `welcome-view.js`'s `unservedView`): the title, the core's
 * log behind «Detalle técnico», collapsed, and «Reintentar». Shown where the model is (the
 * header/thread), never the connect screen, which would read as if no model existed.
 */
function buildStartupFailureCard(view, { onRetry, documentRef = document } = {}) {
  const card = documentRef.createElement('div');
  card.className = 'service-error service-error--startup';
  card.setAttribute('role', 'alert');
  const head = documentRef.createElement('div');
  head.className = 'service-error-head';
  const dot = documentRef.createElement('span');
  dot.className = 'service-error-dot';
  head.append(dot, documentRef.createTextNode(escapeControlChars(view.title)));
  const body = documentRef.createElement('div');
  body.className = 'service-error-body';
  if (view.desc) {
    const desc = documentRef.createElement('span');
    desc.className = 'service-error-desc';
    desc.textContent = escapeControlChars(view.desc);
    body.append(desc);
  }
  if (view.log.length) {
    const log = documentRef.createElement('div');
    log.className = 'service-error-log';
    for (const line of view.log) {
      const row = documentRef.createElement('span');
      row.className = 'service-error-line';
      row.textContent = escapeControlChars(line);
      log.append(row);
    }
    appendTechnicalDetail(body, log, documentRef);
  }
  const actions = documentRef.createElement('div');
  actions.className = 'service-error-actions';
  const retry = documentRef.createElement('button');
  retry.type = 'button';
  retry.className = 'service-error-retry';
  retry.textContent = SERVICE_ERROR.cardRetry;
  retry.addEventListener('click', () => {
    if (onRetry) onRetry();
  });
  actions.append(retry);
  body.append(actions);
  card.append(head, body);
  return card;
}

/**
 * «Esperar»: the card's log, countdown and buttons collapse and the head (what failed)
 * stays, so the thread keeps a record of the failure. The core's scheduled resume is
 * untouched.
 */
function collapseServiceErrorCard(card) {
  card.classList.add('is-collapsed');
  for (const row of card.querySelectorAll('.service-error-body')) row.hidden = true;
  // The label is hidden now, so stop its tick.
  for (const row of card.querySelectorAll('.service-error-resume')) row._clearTick?.();
}

/**
 * The card after the core acted on it. `resumed`: the turn is running again, and the card
 * stays as a record without its countdown or buttons. Otherwise the scheduled resume was
 * dropped (a message, Detener, ...): the countdown and «Esperar» go and «Reintentar» comes
 * back.
 */
function settleServiceErrorCard(card, { resumed }) {
  for (const row of card.querySelectorAll('.service-error-resume')) {
    row._clearTick?.();
    row.remove();
  }
  if (!resumed) {
    // Nothing is scheduled now, so «Esperar» goes and «Reintentar» comes back, unless it is
    // waiting for another model (`offerRetryAfterModelChange`).
    for (const wait of card.querySelectorAll('.service-error-wait')) wait.remove();
    if (card.dataset.retryAfterModelChange !== 'waiting') {
      for (const retry of card.querySelectorAll('.service-error-retry')) retry.hidden = false;
    }
  }
  if (resumed) {
    card.classList.add('is-resolved');
    for (const row of card.querySelectorAll('.service-error-actions')) row.remove();
  }
}

/**
 * One card per failing episode. An episode is a stopped turn and the core's resumes of it.
 * When a resumed turn fails again, the new card is the episode's current state and the
 * older, already resumed one goes: three 429s in a row leave one card, not three copies of
 * the same message. The answer block the old card sat in goes with it when the card was all
 * it held. A resume that succeeds ends the episode, and its card stays settled as the
 * record (`settleServiceErrorCard`, resumed).
 */
function supersedeServiceErrorCard(card) {
  for (const row of card.querySelectorAll('.service-error-resume')) row._clearTick?.();
  const body = card.parentNode;
  card.remove();
  if (!body || body.children.length > 0 || !body.classList.contains('answer-body')) return;
  if (body.parentNode) body.parentNode.remove();
}

/**
 * Another model now serves the session (`model_changed`). A card whose request was too large
 * for the old model's window offers «Reintentar» again, since the turn resumes on the new
 * model and a larger window may fit it. Any other card, or one already settled as resumed,
 * is left as it is.
 */
function offerRetryAfterModelChange(card) {
  if (card.dataset.retryAfterModelChange !== 'waiting') return;
  card.dataset.retryAfterModelChange = 'offered';
  for (const retry of card.querySelectorAll('.service-error-retry')) retry.hidden = false;
}

export { appendTechnicalDetail, buildServiceErrorCard, buildModelFailureCard, buildStartupFailureCard, collapseServiceErrorCard, settleServiceErrorCard, supersedeServiceErrorCard, offerRetryAfterModelChange };
