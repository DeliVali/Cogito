'use strict';

/**
 * The model menu (design 1a, the `.model` chip in the context bar; open states in 1c's
 * «Modelo · barra superior»). In the canvas' order: the model picker (every catalog model,
 * grouped by service, the one serving now checked, from `model-picker.js`), then «Conectar otro servicio…», which opens the connection screen
 * (design 1e, `welcome.js`), then «Herramientas y permisos…» under a rule, which opens
 * design 4e.
 *
 * Choosing a model asks the core to serve it (`picker.pick`, `POST /sessions/{id}/model`).
 * The menu stays open while it loads (the chosen row reads «Cargando…», the others dim)
 * and closes when the core answers. A switch that happened reaches the window as the
 * core's `model_changed` event; one that failed goes to `picker.onFailed`. While a turn
 * runs (`picker.isBusy`) every model is disabled, but «Herramientas y permisos…» never is.
 * `buildToolsMenuItem` is exported for any other menu that carries that entry.
 *
 * Menus close on a click outside with shrink + fade (140 ms) and open with
 * `cubic-bezier(.2,.9,.3,1.2)` (the handoff README's rule). Both live in styles.css (`.menu`).
 */

import { pickerView } from './model-picker.js';
import { escapeControlChars } from './sanitize.js';
import { TOOLS_WINDOW } from './strings.js';

/** Design 1a/1c's entry, verbatim. */
const CONNECT_ITEM = 'Conectar otro servicio…';

const CLOSE_MS = 140;

function buildToolsMenuItem(onChoose, documentRef = document) {
  const item = documentRef.createElement('button');
  item.type = 'button';
  item.className = 'menu-item menu-item--tools';
  item.textContent = TOOLS_WINDOW.menuItem;
  item.addEventListener('click', onChoose);
  return item;
}

function span(documentRef, className, text) {
  const node = documentRef.createElement('span');
  node.className = className;
  if (text) node.textContent = escapeControlChars(String(text));
  return node;
}

/** Draws the picker's sections (`pickerView`) into `container`, replacing what it held. */
function renderPicker(container, view, onChoose, documentRef = document) {
  const sections = view.sections.map((section) => {
    const block = documentRef.createElement('div');
    block.className = 'picker-section';
    const head = documentRef.createElement('div');
    head.className = 'picker-head';
    head.append(
      span(documentRef, section.lit ? 'picker-dot is-lit' : 'picker-dot'),
      span(documentRef, 'picker-service', section.service),
      span(documentRef, 'picker-status', section.status),
    );
    block.append(head);
    for (const item of section.items) {
      const row = documentRef.createElement('button');
      row.type = 'button';
      row.className = 'picker-item';
      row.setAttribute('role', 'menuitem');
      row.dataset.model = item.id;
      row.title = escapeControlChars(item.id);
      row.disabled = item.disabled;
      if (item.loading) row.classList.add('is-loading');
      if (item.dim) row.classList.add('is-dim');
      if (item.check) row.classList.add('is-checked');
      const sub = span(documentRef, 'picker-sub');
      if (item.loading) {
        sub.dataset.tone = 'loading';
        sub.append(span(documentRef, 'picker-spinner'));
      }
      if (item.sub) sub.append(documentRef.createTextNode(escapeControlChars(item.sub)));
      row.append(span(documentRef, 'picker-check', item.check), span(documentRef, 'picker-name', item.label), sub);
      row.addEventListener('click', () => onChoose(item));
      block.append(row);
    }
    if (section.note) block.append(span(documentRef, 'picker-note', section.note));
    return block;
  });
  container.replaceChildren(...sections);
}

/**
 * Make `chip` open the menu below it. Returns `{ open, close, isOpen }`.
 *
 * `onConnect` (optional): «Conectar otro servicio…» opens the connection screen.
 * `picker` (optional; without it the menu carries only «Herramientas y permisos…»):
 * `{ load(): Promise<GET /models answer>, pick(id): Promise<answer>, isBusy(): boolean,
 *    onFailed(answer), onSwitched?(answer) }`.
 */
function attachModelMenu(chip, { onTools, onConnect = null, play = () => {}, picker = null }, documentRef = document) {
  let menu = null;
  let list = null;
  let catalog = null;
  let loading = null;
  // Roving focus (UX-04..08, "menu semantics"): the menuitem that last got focus. Tracked
  // here instead of read from `document.activeElement`, because nothing in this module
  // assumes a real DOM over a fake one.
  let focusedItem = null;
  // True from opening until the person moves focus themselves. The catalog arrives after
  // the menu is drawn, so focus lands on the first item at once and moves to the model
  // serving now (or the first model) when the rows appear, but never over a choice the
  // person already made with the arrow keys.
  let focusIsDefault = false;

  /** Every enabled menuitem, in the order they are drawn: the picker's rows, «Conectar otro
   * servicio…», then «Herramientas y permisos…». */
  function menuItems() {
    if (!menu) return [];
    return [...menu.querySelectorAll('.picker-item, .menu-item--connect, .menu-item--tools')].filter((node) => !node.disabled);
  }

  function focusItem(node) {
    if (!node) return;
    focusIsDefault = false;
    focusedItem = node;
    node.focus();
  }

  function moveFocus(delta) {
    const items = menuItems();
    if (!items.length) return;
    const at = items.indexOf(focusedItem);
    const next = at === -1 ? 0 : (at + delta + items.length) % items.length;
    focusItem(items[next]);
  }

  function close(sound = true, { returnFocus = true } = {}) {
    if (!menu) return;
    const closing = menu;
    menu = null;
    list = null;
    focusedItem = null;
    closing.classList.add('is-closing');
    chip.setAttribute('aria-expanded', 'false');
    if (sound) play('close');
    setTimeout(() => closing.remove(), CLOSE_MS);
    documentRef.removeEventListener('mousedown', outside, true);
    documentRef.removeEventListener('keydown', onKeydown, true);
    // A click outside chose where focus goes next; nothing here should fight that.
    if (returnFocus) chip.focus();
  }

  function outside(event) {
    if (menu && !menu.contains(event.target) && !chip.contains(event.target)) close(true, { returnFocus: false });
  }

  function onKeydown(event) {
    if (!menu) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveFocus(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveFocus(-1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      const items = menuItems();
      if (items.length) focusItem(items[0]);
    } else if (event.key === 'End') {
      event.preventDefault();
      const items = menuItems();
      if (items.length) focusItem(items[items.length - 1]);
    }
  }

  function draw() {
    if (!list || !picker) return;
    const busy = Boolean(picker.isBusy && picker.isBusy());
    renderPicker(list, pickerView(catalog, { busy, loading }), choose, documentRef);
  }

  async function refresh() {
    try {
      catalog = await picker.load();
    } catch {
      catalog = null;
    }
    draw();
    if (menu && focusIsDefault) {
      const items = menuItems();
      const target = items.find((node) => node.classList.contains('is-checked')) || items[0];
      if (target && target !== focusedItem) {
        focusItem(target);
        focusIsDefault = true;
      }
    }
  }

  function choose(item) {
    if (item.disabled || loading !== null) return;
    if (item.check) {
      // The model serving now: nothing to switch.
      close();
      return;
    }
    play('select');
    loading = item.id;
    draw();
    Promise.resolve()
      .then(() => picker.pick(item.id))
      .catch((error) => ({ error: (error && error.message) || 'failed' }))
      .then((answer) => {
        loading = null;
        close(false);
        if (!answer || answer.error) {
          if (picker.onFailed) picker.onFailed(answer || {});
        } else if (picker.onSwitched) {
          picker.onSwitched(answer);
        }
      });
  }

  function open() {
    if (menu) return;
    menu = documentRef.createElement('div');
    menu.className = 'menu menu--right model-menu';
    menu.setAttribute('role', 'menu');
    if (picker) {
      list = documentRef.createElement('div');
      list.className = 'picker';
      menu.append(list);
      // Never reuse the last catalog: which model serves may have changed. The rows appear when the core answers.
      catalog = null;
      draw();
    }
    if (onConnect) {
      const connect = documentRef.createElement('button');
      connect.type = 'button';
      connect.className = 'menu-item menu-item--connect';
      connect.setAttribute('role', 'menuitem');
      connect.textContent = CONNECT_ITEM;
      connect.addEventListener('click', () => {
        // As the canvas' `closeMenus`: play the menu's close sound. The screen opens next
        // and manages its own focus.
        close(true, { returnFocus: false });
        onConnect();
      });
      menu.append(connect);
    }
    const item = buildToolsMenuItem(() => {
      // «Herramientas y permisos…» opens a dialog that sets its own initial focus, so the
      // menu does not take focus back as it closes.
      close(false, { returnFocus: false });
      onTools();
    }, documentRef);
    item.setAttribute('role', 'menuitem');
    menu.append(item);
    // Under the chip, flush with its right edge and kept inside the window (design 1a:
    // `top:31px; right:10px`, growing from the top right; the chip is at the bar's right).
    documentRef.body.append(menu);
    const rect = chip.getBoundingClientRect();
    const width = documentRef.documentElement.clientWidth;
    menu.style.top = `${Math.round(rect.bottom + 2)}px`;
    menu.style.right = `${Math.max(8, Math.round(width - rect.right))}px`;
    chip.setAttribute('aria-expanded', 'true');
    play('open');
    // The first item, not the last: «Herramientas y permisos…» is a rare choice and the
    // rows about to appear above it are the point of the menu.
    focusItem(menuItems()[0] || item);
    focusIsDefault = true;
    documentRef.addEventListener('mousedown', outside, true);
    documentRef.addEventListener('keydown', onKeydown, true);
    if (picker) refresh();
  }

  chip.classList.add('model--menu');
  chip.setAttribute('aria-haspopup', 'menu');
  chip.setAttribute('aria-expanded', 'false');
  chip.addEventListener('click', () => (menu ? close() : open()));
  // The chip is a <button>: Enter and Space already click it.

  return { open, close, isOpen: () => menu !== null };
}

export { attachModelMenu, buildToolsMenuItem, renderPicker };
