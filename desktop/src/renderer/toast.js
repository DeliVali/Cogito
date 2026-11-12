'use strict';

/**
 * The app's notification window (see `desktop/src/main/toast-window.js`). It shows the
 * title, text and buttons the main process put in this page's URL, already in their final
 * words. It answers with the pressed button's id, `open` for a click on the text, or
 * `dismiss` for ✕. It builds nothing of its own.
 *
 * Layout follows design 2d: a strip in the notice's colour with the time, the title and
 * text, then the buttons (beside the text when there is one, under it when there are more).
 * Sounds: `window` on arrival, `click` for a button, `close` for ✕. The answer waits for the
 * sound to finish, because answering closes this window.
 */

import { toastView, whenLabel } from './toast-view.js';
import { sfx } from './sfx.js';

/** How long `click` and `close` last (sfx.js: 70 ms and 140 ms), rounded up. */
const SOUND_MS = { click: 80, close: 150 };

const spec = JSON.parse(new URLSearchParams(window.location.search).get('spec') || '{}');
const view = toastView(spec);
// When main created the toast (`toast-window.js`), else when this page loaded.
const shownAt = Number.isFinite(spec.shownAt) ? spec.shownAt : Date.now();
let answered = false;
const answer = (action, sound) => {
  if (answered) return;
  answered = true;
  if (!sound) {
    window.toast.act(spec.id, action);
    return;
  }
  sfx(sound);
  setTimeout(() => window.toast.act(spec.id, action), SOUND_MS[sound] || 0);
};

document.body.dataset.kind = view.kind;
document.querySelector('.toast-title').textContent = view.title;
document.querySelector('.toast-body').textContent = view.body;
document.querySelector('.toast-main').addEventListener('click', () => answer('open', 'click'));
document.querySelector('.toast-close').addEventListener('click', () => answer('dismiss', 'close'));

const when = document.querySelector('.toast-when');
const tick = () => {
  when.textContent = whenLabel(Date.now() - shownAt);
};
tick();
setInterval(tick, 15_000);

const actions = document.querySelector('.toast-actions');
for (const button of view.buttons) {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = button.primary ? 'toast-button toast-button--primary' : 'toast-button';
  element.textContent = button.label;
  element.addEventListener('click', () => answer(button.id, 'click'));
  actions.append(element);
}
actions.hidden = view.buttons.length === 0;
// One button sits beside the text, like the design's «Ver». More go under it, right-aligned.
document.querySelector('.toast-row').classList.toggle('is-stacked', view.buttons.length > 1);

sfx('window');
