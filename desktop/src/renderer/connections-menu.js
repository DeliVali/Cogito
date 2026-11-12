'use strict';

/**
 * The bar's «⧉ N conexiones» chip and its menu (design 1a): each connection with its logo,
 * name, state in words and switch, then «Agregar conexión…» and «Administrar conexiones…».
 * The chip counts the enabled connections; its dot is red when one of them failed, green
 * when any is on, grey with none. Data is `GET /connections`, read when the window loads,
 * when the menu opens, and after the tab changes something (`refresh`).
 *
 * `api`: `{ list(), set(name, values) }` (the preload's `window.cogito.connections`).
 * `onAdd()` and `onManage()` open «Herramientas y permisos» on Conexiones.
 */

import { chipModel, connectionModel } from './connections.js';
import { CONNECTIONS } from './strings.js';
import { escapeControlChars } from './sanitize.js';

const CLOSE_MS = 140;
/** While the core starts, the list is asked again this often, this many times. */
const RETRY_MS = 3000;
const RETRIES = 40;

function node(documentRef, tag, className, text) {
  const element = documentRef.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = escapeControlChars(String(text));
  return element;
}

function attachConnectionsMenu(chip, { api, onAdd, onManage, project = () => '', play = () => {} }, documentRef = document) {
  if (!chip || !api) return { refresh: () => Promise.resolve(), close() {} };
  let connections = [];
  let menu = null;
  let loaded = false;
  let tries = 0;

  const dot = node(documentRef, 'span', 'connections-chip-dot');
  const label = node(documentRef, 'span', 'connections-chip-label');
  // `.chip` draws its own ▾.
  chip.replaceChildren(dot, label);

  function drawChip() {
    const model = chipModel(connections);
    dot.style.background = model.dot;
    label.textContent = escapeControlChars(model.text);
    chip.title = escapeControlChars(model.text);
  }

  async function refresh() {
    let answer = null;
    try {
      answer = await api.list();
    } catch {
      answer = null;
    }
    if (answer && Array.isArray(answer.connections)) {
      connections = answer.connections;
      chip.hidden = false;
      loaded = true;
    } else if (!loaded) {
      // No answer yet (the core is still starting) or a core without the route: no chip,
      // rather than one that says nothing true, and ask again a little later.
      chip.hidden = true;
      if (tries < RETRIES) {
        tries += 1;
        setTimeout(refresh, RETRY_MS);
      }
    }
    drawChip();
    if (menu) fillMenu();
  }

  function close(sound = true) {
    if (!menu) return;
    const closing = menu;
    menu = null;
    closing.classList.add('is-closing');
    chip.setAttribute('aria-expanded', 'false');
    if (sound) play('close');
    setTimeout(() => closing.remove(), CLOSE_MS);
    documentRef.removeEventListener('mousedown', outside, true);
    documentRef.removeEventListener('keydown', onKeydown, true);
  }

  function outside(event) {
    if (menu && !menu.contains(event.target) && !chip.contains(event.target)) close();
  }

  function onKeydown(event) {
    if (menu && event.key === 'Escape') {
      event.preventDefault();
      close();
      chip.focus({ focusVisible: false });
    }
  }

  function row(connection) {
    const model = connectionModel(connection);
    const line = node(documentRef, 'div', `connections-menu-row${model.enabled ? '' : ' is-off'}`);
    const text = node(documentRef, 'span', 'connections-menu-text');
    const status = node(documentRef, 'span', 'connections-menu-status', model.statusText);
    status.style.color = model.ink;
    text.append(node(documentRef, 'span', 'connections-menu-name', model.name), status);
    const toggle = node(documentRef, 'button', `toggle${model.enabled ? ' is-on' : ''}`);
    toggle.type = 'button';
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', String(model.enabled));
    toggle.setAttribute('aria-label', model.name);
    toggle.append(node(documentRef, 'span', 'toggle-knob'));
    toggle.addEventListener('click', async () => {
      const next = !toggle.classList.contains('is-on');
      play(next ? 'open' : 'close');
      toggle.classList.toggle('is-on', next);
      toggle.setAttribute('aria-checked', String(next));
      line.classList.toggle('is-off', !next);
      let answer = null;
      try {
        answer = await api.set(model.name, { enabled: next });
      } catch {
        answer = null;
      }
      if (answer && answer.name) {
        connections = connections.map((c) => (c.name === model.name ? answer : c));
      }
      drawChip();
      if (menu) fillMenu();
    });
    line.append(node(documentRef, 'span', 'conn-logo', model.logo), text, toggle);
    return line;
  }

  function fillMenu() {
    menu.replaceChildren(node(documentRef, 'div', 'menu-heading', CONNECTIONS.menuHeading(project())));
    for (const connection of connections) menu.append(row(connection));
    const add = node(documentRef, 'button', 'menu-item', CONNECTIONS.add);
    add.type = 'button';
    add.setAttribute('role', 'menuitem');
    add.addEventListener('click', () => {
      play('click');
      close(false);
      onAdd();
    });
    const manage = node(documentRef, 'button', 'menu-item', CONNECTIONS.manage);
    manage.type = 'button';
    manage.setAttribute('role', 'menuitem');
    manage.addEventListener('click', () => {
      play('click');
      close(false);
      onManage();
    });
    menu.append(add, manage);
  }

  function open() {
    if (menu) return;
    menu = node(documentRef, 'div', 'menu menu--right connections-menu');
    menu.setAttribute('role', 'menu');
    fillMenu();
    documentRef.body.append(menu);
    const rect = chip.getBoundingClientRect();
    menu.style.top = `${Math.round(rect.bottom + 2)}px`;
    menu.style.right = `${Math.max(8, Math.round(documentRef.documentElement.clientWidth - rect.right))}px`;
    chip.setAttribute('aria-expanded', 'true');
    play('open');
    documentRef.addEventListener('mousedown', outside, true);
    documentRef.addEventListener('keydown', onKeydown, true);
    refresh();
  }

  chip.setAttribute('aria-haspopup', 'menu');
  chip.setAttribute('aria-expanded', 'false');
  chip.addEventListener('click', () => (menu ? close() : open()));
  drawChip();
  refresh();
  return { refresh, close };
}

export { attachConnectionsMenu };
