'use strict';

/**
 * The composer's skills picker: a button beside «Adjuntar» that lists the project's Agent
 * Skills and activates one for the next message, shown as a removable chip like an
 * attachment (`render-attachments.js`'s `buildChip`, the attach-chip look).
 *
 * - Clicking the button opens a menu (`GET /sessions/{id}/skills`, `window.cogito.skills`)
 *   of the skills not already active, each with its name and one-line description. Picking
 *   one adds a chip and marks it active; the chip's ✕ (or Delete/Backspace on it) removes it.
 * - `active()` is the names to send with the next message; the composer clears them once it
 *   is sent (`clear()`), the way it clears attachments.
 * - With no skills, or before the list has come back, the menu says so. The whole control is
 *   app-only: the model is told about a skill only when the person activates it (or BM25
 *   matches it on its own); nothing here changes `chat` or any benchmark.
 *
 * Sounds, as the canvas plays them: `open`/`close` for the menu, `select` when a skill is
 * picked, `close` when a chip is removed.
 */

import { buildChip } from './render-attachments.js';
import { SKILLS } from './strings.js';
import { escapeControlChars } from './sanitize.js';

/** The menu fades out over this long when it closes (the canvas' value, as the other menus). */
const CLOSE_MS = 140;
/** A removed chip shrinks and fades, then is gone (the attach tray's value). */
const LEAVE_MS = 170;

function node(documentRef, tag, className, text) {
  const element = documentRef.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = escapeControlChars(String(text));
  return element;
}

/** The attach-chip view for a skill: a ✦ badge and the skill's name, no second line. */
function skillView(skill) {
  return { tone: 'skill', badge: SKILLS.badge, name: skill.name, sub: '', label: SKILLS.chipLabel(skill.name) };
}

/**
 * `elements`: `{button, row, input}` — the picker button in `field-main`, the chip row above
 * the message, and the message field (for focus after a keyboard removal). `api`:
 * `{list()}` (the preload's `window.cogito.skills`). `canUse()` says whether the window is
 * showing the conversation. `onChange()` runs after every change (the composer re-reads
 * whether «Enviar» should show). Returns `{active, clear, size, refresh, close}`.
 */
function createSkillsTray({ elements, api, canUse = () => true, play = () => {}, onChange = () => {}, documentRef = document }) {
  const button = elements && elements.button;
  const row = elements && elements.row;
  const input = elements && elements.input;
  if (!button || !row || !api) {
    return { active: () => [], clear() {}, size: () => 0, refresh: () => Promise.resolve(), close() {} };
  }

  /** `{name, chip}` in the order activated. */
  let active = [];
  /** The last `list()` answer: `[{name, description, directory}]`. */
  let skills = [];
  let loaded = false;
  let menu = null;

  row.setAttribute('role', 'list');
  row.setAttribute('aria-label', SKILLS.list);

  function changed() {
    row.hidden = active.length === 0;
    onChange();
  }

  function remove(entry, how) {
    if (!active.includes(entry)) return;
    const index = active.indexOf(entry);
    active = active.filter((other) => other !== entry);
    play('close');
    entry.chip.classList.add('is-leaving');
    if (how === 'keyboard') {
      const next = active[index] || active[index - 1];
      (next ? next.chip : input || button).focus();
    }
    setTimeout(() => entry.chip.remove(), LEAVE_MS);
    changed();
    if (menu) fillMenu();
  }

  function activate(skill) {
    if (active.some((entry) => entry.name === skill.name)) return;
    const entry = { name: skill.name };
    entry.chip = buildChip(skillView(skill), { onRemove: (how) => remove(entry, how) }, documentRef);
    active.push(entry);
    row.append(entry.chip);
    changed();
  }

  async function refresh() {
    let answer = null;
    try {
      answer = await api.list();
    } catch {
      answer = null;
    }
    if (answer && Array.isArray(answer.skills)) {
      skills = answer.skills;
      loaded = true;
    }
    if (menu) fillMenu();
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

  function item(skill) {
    const line = node(documentRef, 'button', 'menu-item skills-menu-item');
    line.type = 'button';
    line.setAttribute('role', 'menuitem');
    line.append(
      node(documentRef, 'span', 'skills-menu-name', skill.name),
      node(documentRef, 'span', 'skills-menu-desc', skill.description || ''),
    );
    line.addEventListener('click', () => {
      play('select');
      activate(skill);
      close(false);
    });
    return line;
  }

  function fillMenu() {
    menu.replaceChildren(node(documentRef, 'div', 'menu-heading', SKILLS.menuHeading));
    const available = skills.filter((skill) => !active.some((entry) => entry.name === skill.name));
    if (!available.length) {
      menu.append(node(documentRef, 'div', 'skills-menu-empty', loaded ? SKILLS.empty : SKILLS.loading));
      return;
    }
    for (const skill of available) menu.append(item(skill));
  }

  function open() {
    if (menu) return;
    menu = node(documentRef, 'div', 'menu skills-menu');
    menu.setAttribute('role', 'menu');
    fillMenu();
    documentRef.body.append(menu);
    // The composer sits at the bottom of the window, so the menu opens upward from the button.
    const rect = button.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.round(rect.left))}px`;
    menu.style.bottom = `${Math.round(documentRef.documentElement.clientHeight - rect.top + 4)}px`;
    button.setAttribute('aria-expanded', 'true');
    play('open');
    documentRef.addEventListener('mousedown', outside, true);
    documentRef.addEventListener('keydown', onKeydown, true);
    refresh();
  }

  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', () => {
    if (!canUse()) return;
    if (menu) close();
    else open();
  });

  changed();

  return {
    /** The names activated for the next message. */
    active: () => active.map((entry) => entry.name),
    /** How many are active. */
    size: () => active.length,
    /** Remove every chip (the message took them). */
    clear() {
      if (!active.length) return;
      for (const entry of active) entry.chip.remove();
      active = [];
      changed();
      if (menu) fillMenu();
    },
    refresh,
    close,
  };
}

export { createSkillsTray, skillView };
