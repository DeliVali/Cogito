'use strict';

/**
 * The composer's thinking control: a bolt beside the skills picker that sets how much the
 * model may think before it answers. The bolt's look is the level (`data-level`, skills.css):
 * struck through for «No», an outline for «Normal», filled for «Más», glowing for «Sin
 * límite»; its name is the tooltip. It is sent with each message as `thinking`
 * (`POST /sessions/{id}/turns`, `config.settings.THINKING_LEVELS` in the core):
 *
 * - `off`: thinking off through the chat template.
 * - `normal` («Normal», stored as `null`): the session's own budget; on Claude, thinking on
 *   at the model's own effort.
 * - `long`: a larger budget. `free`: no budget, the server's own rule.
 *
 * Shown only for a model whose host takes a thinking budget (`/models`' `thinking`), so it
 * never offers a choice that does nothing. The choice is this window's own and is kept in
 * localStorage, which may be unavailable: then it lasts until the window closes.
 *
 * Sounds, as the other menus: `open`/`close`, and `select` when a level is picked.
 */

import { THINKING } from './strings.js';
import { escapeControlChars } from './sanitize.js';

/** The menu fades out over this long when it closes (the canvas' value, as the other menus). */
const CLOSE_MS = 140;
const STORAGE_KEY = 'cogito.thinking';
/** In the menu's order; `null` is the session's own budget. */
const LEVELS = ['off', null, 'long', 'free'];

function node(documentRef, tag, className, text) {
  const element = documentRef.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = escapeControlChars(String(text));
  return element;
}

const SVG = 'http://www.w3.org/2000/svg';
const BOLT = 'M13 2.5 4.5 13.5h6.5l-1 8 8.5-11h-6.5l1-8z';
const SLASH = 'M4 4 20 20';

/** The bolt for one menu row, drawn as the button draws that level. Skipped where the
 * document cannot make SVG (the tests' fake DOM). */
function boltIcon(documentRef) {
  if (typeof documentRef.createElementNS !== 'function') return null;
  const svg = documentRef.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', 'thinking-icon');
  for (const [cls, d] of [['thinking-bolt', BOLT], ['thinking-slash', SLASH]]) {
    const path = documentRef.createElementNS(SVG, 'path');
    path.setAttribute('class', cls);
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

function readStored(storage) {
  try {
    const value = storage ? storage.getItem(STORAGE_KEY) : null;
    return LEVELS.includes(value) ? value : null;
  } catch {
    return null;
  }
}

function writeStored(storage, level) {
  try {
    if (!storage) return;
    if (level) storage.setItem(STORAGE_KEY, level);
    else storage.removeItem(STORAGE_KEY);
  } catch {
    // Private mode or blocked storage: the choice lasts for this window only.
  }
}

/**
 * `button`: the control in the composer. `storage`: `window.localStorage` or null.
 * Returns `{level, setAvailable, close}`: `level()` is what to send with the next message
 * (`null` while the control is hidden), `setAvailable(bool)` shows or hides it for the
 * model in use.
 */
function createThinkingControl({ button, storage = null, play = () => {}, documentRef = document }) {
  if (!button) return { level: () => null, setAvailable() {}, close() {} };

  let level = readStored(storage);
  let available = false;
  let menu = null;

  function draw() {
    const key = level ?? 'normal';
    const label = escapeControlChars(THINKING.button(THINKING.names[key]));
    button.setAttribute('data-level', key);
    button.title = label;
    button.setAttribute('aria-label', label);
    button.hidden = !available;
  }

  function close(sound = true) {
    if (!menu) return;
    const closing = menu;
    menu = null;
    closing.classList.add('is-closing');
    button.setAttribute('aria-expanded', 'false');
    if (sound) play('close');
    setTimeout(() => closing.remove(), CLOSE_MS);
    documentRef.removeEventListener('mousedown', outside, true);
    documentRef.removeEventListener('keydown', onKeydown, true);
  }

  function outside(event) {
    if (menu && !menu.contains(event.target) && !button.contains(event.target)) close();
  }

  function onKeydown(event) {
    if (menu && event.key === 'Escape') {
      event.preventDefault();
      close();
      button.focus({ focusVisible: false });
    }
  }

  function item(choice) {
    const key = choice ?? 'normal';
    const line = node(documentRef, 'button', 'menu-item thinking-menu-item');
    line.type = 'button';
    line.setAttribute('data-level', key);
    const icon = boltIcon(documentRef);
    if (icon) line.append(icon);
    line.setAttribute('role', 'menuitemradio');
    line.setAttribute('aria-checked', String(choice === level));
    if (choice === level) line.classList.add('is-current');
    line.append(
      node(documentRef, 'span', 'thinking-menu-name', THINKING.names[key]),
      node(documentRef, 'span', 'thinking-menu-desc', THINKING.descriptions[key]),
    );
    line.addEventListener('click', () => {
      play('select');
      level = choice;
      writeStored(storage, level);
      draw();
      close(false);
    });
    return line;
  }

  function open() {
    if (menu) return;
    menu = node(documentRef, 'div', 'menu thinking-menu');
    menu.setAttribute('role', 'menu');
    menu.append(node(documentRef, 'div', 'menu-heading', THINKING.menuHeading));
    for (const choice of LEVELS) menu.append(item(choice));
    documentRef.body.append(menu);
    // Under the message the composer ends near the window's bottom: open upward.
    const rect = button.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.round(rect.left))}px`;
    menu.style.bottom = `${Math.round(documentRef.documentElement.clientHeight - rect.top + 4)}px`;
    button.setAttribute('aria-expanded', 'true');
    play('open');
    documentRef.addEventListener('mousedown', outside, true);
    documentRef.addEventListener('keydown', onKeydown, true);
  }

  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', () => {
    if (menu) close();
    else open();
  });
  draw();

  return {
    level: () => (available ? level ?? 'normal' : null),
    setAvailable(value) {
      available = Boolean(value);
      if (!available) close(false);
      draw();
    },
    close,
  };
}

export { createThinkingControl, LEVELS };
