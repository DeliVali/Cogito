'use strict';

/**
 * "Herramientas y permisos" (design 4e): a window with its tabs (Herramientas, Conexiones,
 * Del sistema, Búsqueda, Habilidades, Diseño). Opened from the model menu.
 *
 * Drawn inside the main window as an OS 9 window (title bar, close box, hard shadow) over
 * the thread rather than as a second BrowserWindow, so the main process, the preload and the
 * CSP need nothing new. Data is read from the core when the window opens and when a tab is
 * chosen; changes go back through the same loopback routes the cards use.
 * */

import {
  componentRowModel,
  levelModels,
  missingProgramModel,
  programRowModel,
  rowModel,
  tabModels,
  visibleComponents,
} from './components.js';
import { settingModels } from './appearance.js';
import { buildMissingProgramCard, buildRow } from './render-components.js';
import { COMPONENTS, CONNECTIONS, TOOLS_WINDOW } from './strings.js';
import { connectionModel } from './connections.js';
import { escapeControlChars } from './sanitize.js';

function el(documentRef, tag, className, text) {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = escapeControlChars(String(text));
  return node;
}

/** Drawn like the main title bar's ✕ (index.html): a text ✕ would sit under the box
 * `.chrome::before` paints. */
function closeGlyph(documentRef) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = documentRef.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 10 10');
  svg.setAttribute('aria-hidden', 'true');
  const glyph = documentRef.createElementNS(NS, 'path');
  glyph.setAttribute('d', 'M2.3 2.3l5.4 5.4M7.7 2.3l-5.4 5.4');
  svg.append(glyph);
  return svg;
}

/** How long the toggle knob and segment backgrounds take to move (4e: `.18s`, `.12s`). A
 * redraw waits this long so nothing is replaced mid-move. */
const SETTLE_MS = 200;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A folder under the home directory as `~/…`, as the person would write it. */
function homeShort(directory) {
  return String(directory).replace(/^(\/home\/[^/]+|\/Users\/[^/]+|[A-Za-z]:\\Users\\[^\\]+)(?=[/\\]|$)/, '~');
}

async function settled(promise, fallback) {
  try {
    const value = await promise;
    return value && !value.error ? value : fallback;
  } catch {
    return fallback;
  }
}

/** Open the window (or focus it, if it is already open). Returns `{ close, show(tab) }`. */
function openToolsWindow(
  {
    api,
    connectionsApi = null,
    // `list()` answers the Agent Skills of the open project (`GET /sessions/{id}/skills`).
    skillsApi = null,
    // `get()` and `set(theme)`, each answering `{theme, dark}` (src/main/appearance.js).
    appearanceApi = null,
    play = () => {},
    initialTab = 'components',
    onClose = () => {},
    // Told after a connection is added, removed or switched, so the bar's chip follows.
    onConnectionsChanged = () => {},
    returnFocusTo = null,
    // «Agregar conexión…» from the chip's menu: open Conexiones on its form.
    adding = false,
  },
  documentRef = document,
) {
  const existing = documentRef.querySelector('.tools-overlay');
  if (existing && existing.toolsWindow) {
    existing.toolsWindow.show(initialTab, { adding });
    return existing.toolsWindow;
  }

  const state = {
    tab: initialTab,
    components: [],
    programs: [],
    skills: [],
    search: null,
    // `GET /connections`: null until it answers (or when the core has no such route).
    connections: null,
    // Which connection's details are open, and whether the «Agregar conexión» form is.
    connectionOpen: null,
    adding,
    // Diseño's settings: `{theme}`, the theme null until `get()` answers. Null with no api,
    // which leaves the tab out.
    appearance: appearanceApi ? { theme: null } : null,
  };
  // `draw()` falls back to the first shown tab (UX-11/12/13) only after the first
  // `refresh()` has answered. Before that, `state.components` and `.search` are empty
  // starting values, not a real "nothing here". Without this, opening on «Herramientas»
  // (the default `initialTab`) would bounce to «Del sistema» on the first paint and stay
  // there after the components arrive.
  let loaded = false;

  // UX-07: the element focused when the window opened, so closing it (Escape, «✕», the
  // last decision made) returns keyboard focus there, as a modal dialog should. For the
  // model menu this is its «Herramientas y permisos…» item, still focused when its click
  // handler gets here, but that item leaves the page as the menu closes, so the caller names
  // the control that opened the menu (`returnFocusTo`) and focus goes there instead.
  const active = documentRef.activeElement && documentRef.activeElement !== documentRef.body
    ? documentRef.activeElement
    : null;
  const trigger = returnFocusTo || active;

  const overlay = el(documentRef, 'div', 'tools-overlay');
  const win = el(documentRef, 'div', 'tools-window');
  win.setAttribute('role', 'dialog');
  win.setAttribute('aria-modal', 'true');
  win.setAttribute('aria-label', TOOLS_WINDOW.title);
  win.tabIndex = -1;

  const titlebar = el(documentRef, 'div', 'tools-titlebar');
  const closeBox = el(documentRef, 'button', 'chrome chrome--close');
  closeBox.type = 'button';
  closeBox.setAttribute('aria-label', TOOLS_WINDOW.close);
  closeBox.append(closeGlyph(documentRef));
  titlebar.append(closeBox, el(documentRef, 'span', 'tools-title', TOOLS_WINDOW.title), el(documentRef, 'span', 'tools-titlebar-end'));

  const body = el(documentRef, 'div', 'tools-body');
  const tabs = el(documentRef, 'nav', 'tools-tabs');
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-orientation', 'vertical');
  const panel = el(documentRef, 'div', 'tools-panel soft-scroll');
  panel.id = 'tools-panel';
  panel.setAttribute('role', 'tabpanel');
  body.append(tabs, panel);
  win.append(titlebar, body);
  overlay.append(win);

  function close() {
    if (!overlay.isConnected) return;
    play('close');
    overlay.classList.add('is-closing');
    documentRef.removeEventListener('keydown', onKey, true);
    setTimeout(() => overlay.remove(), 140);
    onClose();
    if (trigger && trigger.isConnected && typeof trigger.focus === 'function') trigger.focus();
  }

  // Focus trap (UX-07): Tab/Shift+Tab cycle within the dialog's focusable controls.
  // `draw()` replaces the panel outright, so only the current tab's content is in the DOM
  // and this walks what is really shown.
  function focusable() {
    return [...win.querySelectorAll('button, [href], input, select, textarea, [tabindex]')].filter(
      (node) => !node.disabled && node.tabIndex !== -1 && node.offsetParent !== null,
    );
  }

  function onKey(event) {
    if (!overlay.isConnected) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const stops = focusable();
    if (!stops.length) return;
    const first = stops[0];
    const last = stops[stops.length - 1];
    const active = documentRef.activeElement;
    if (event.shiftKey) {
      if (active === first || !win.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !win.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  }

  closeBox.addEventListener('click', close);
  documentRef.addEventListener('keydown', onKey, true);

  // -- tabs ------------------------------------------------------------------------------

  // What `tabModels` would draw now. Components and search are left out while hidden
  // (UX-11/12/13), so `state.tab` is never redirected to a button that does not exist.
  // `draw()` computes this once per redraw for both `state.tab` and `drawTabs`, so the two
  // always see the same list.
  function currentTabs() {
    return tabModels(state);
  }

  // Roving tabindex (WAI-ARIA tablist, vertical): only the active tab is a Tab stop.
  // Up/Down/Home/End move to another tab and select it, wrapping at either end.
  function onTabKey(event, index, models) {
    const count = models.length;
    let next;
    if (event.key === 'ArrowDown') next = (index + 1) % count;
    else if (event.key === 'ArrowUp') next = (index - 1 + count) % count;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = count - 1;
    else return;
    event.preventDefault();
    play('click');
    show(models[next].key);
    const target = tabs.children[next];
    if (target) target.focus();
  }

  function drawTabs(models) {
    // Same tabs as already drawn: update the active one and the counts in place, so the
    // design's `transition: background .12s` plays instead of a rebuilt button appearing.
    const drawn = [...tabs.children];
    if (drawn.length === models.length && drawn.every((button, index) => button.dataset.tab === models[index].key)) {
      drawn.forEach((button, index) => {
        const active = models[index].key === state.tab;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-selected', String(active));
        button.tabIndex = active ? 0 : -1;
        const count = button.querySelector('.tools-tab-count');
        if (count && count.textContent !== models[index].count) count.textContent = escapeControlChars(String(models[index].count));
      });
      return;
    }
    tabs.replaceChildren();
    models.forEach((tab, index) => {
      const active = tab.key === state.tab;
      const button = el(documentRef, 'button', `tools-tab${active ? ' is-active' : ''}`);
      button.type = 'button';
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', String(active));
      button.setAttribute('aria-controls', panel.id);
      button.id = `tools-tab-${tab.key}`;
      button.tabIndex = active ? 0 : -1;
      button.dataset.tab = tab.key;
      button.append(el(documentRef, 'span', 'tools-tab-label', tab.label), el(documentRef, 'span', 'tools-tab-count', tab.count));
      button.addEventListener('click', () => {
        if (state.tab === tab.key) return;
        play('click');
        show(tab.key);
      });
      button.addEventListener('keydown', (event) => onTabKey(event, index, models));
      tabs.append(button);
    });
  }

  function heading(title, note) {
    const head = el(documentRef, 'div', 'tools-heading');
    head.append(el(documentRef, 'span', 'tools-heading-title', title));
    if (note) head.append(el(documentRef, 'span', 'tools-heading-note', note));
    return head;
  }

  function list() {
    return el(documentRef, 'div', 'tools-list');
  }

  // -- Herramientas ----------------------------------------------------------------------

  function drawComponents(target) {
    const rows = list();
    for (const component of state.components) {
      const model = componentRowModel(component);
      const row = el(documentRef, 'div', `tools-row tools-row--component${model.enabled ? '' : ' is-off'}`);
      const text = el(documentRef, 'span', 'tools-row-text');
      text.append(el(documentRef, 'span', 'tools-row-name', model.name), el(documentRef, 'span', 'tools-row-description', model.description));
      const toggle = el(documentRef, 'button', `toggle${model.enabled ? ' is-on' : ''}`);
      toggle.type = 'button';
      toggle.setAttribute('role', 'switch');
      toggle.setAttribute('aria-checked', String(model.enabled));
      toggle.setAttribute('aria-label', model.name);
      toggle.append(el(documentRef, 'span', 'toggle-knob'));
      toggle.addEventListener('click', async () => {
        const next = !component.enabled;
        play(next ? 'open' : 'close');
        component.enabled = next;
        // In place: the knob slides on the design's spring (.18s) and the text fades (.15s).
        toggle.classList.toggle('is-on', next);
        toggle.setAttribute('aria-checked', String(next));
        row.classList.toggle('is-off', !next);
        const [answer] = await Promise.all([settled(api.setEnabled(component.id, next), null), pause(SETTLE_MS)]);
        if (!answer) component.enabled = !next;
        await refresh(['components']);
      });
      row.append(
        text,
        el(documentRef, 'span', 'tools-row-source', model.source),
        el(documentRef, 'span', 'tools-row-size', model.size),
        toggle,
      );
      rows.append(row);
    }
    target.append(heading(TOOLS_WINDOW.componentsHeading), rows);
  }

  // -- Conexiones (design 4e → Conexiones, 4f) --------------------------------------------

  function button(className, text) {
    const node = el(documentRef, 'button', className, text);
    node.type = 'button';
    return node;
  }

  function toggleSwitch(on, label, onChange) {
    const toggle = el(documentRef, 'button', `toggle${on ? ' is-on' : ''}`);
    toggle.type = 'button';
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', String(on));
    toggle.setAttribute('aria-label', label);
    toggle.append(el(documentRef, 'span', 'toggle-knob'));
    toggle.addEventListener('click', () => onChange(!toggle.classList.contains('is-on'), toggle));
    return toggle;
  }

  /** Replace one connection in the list with the core's answer, then redraw. */
  function settleConnection(name, answer) {
    // A connection comes back with its name; a refusal does not (its `error` is the
    // reason, where a connection's `error` is its start log).
    if (answer && answer.name && state.connections) {
      state.connections = state.connections.map((c) => (c.name === name ? answer : c));
    }
    draw();
    // A retry or a switch answers at once with «starting»; read it again until it ends.
    if (connectionsApi) watchStarting();
  }

  /** «Iniciar sesión»: main opens the server's sign-in page; the list is read again until
   * the browser comes back and the server connects, or five minutes pass. */
  function loginButton(model) {
    const signIn = button('cc-button cc-button--primary', model.signingIn ? CONNECTIONS.waitingBrowser : CONNECTIONS.login);
    signIn.disabled = model.signingIn;
    signIn.addEventListener('click', async () => {
      play('click');
      signIn.disabled = true;
      signIn.textContent = CONNECTIONS.waitingBrowser;
      const answer = await settled(connectionsApi.login(model.name), null);
      if (!answer || !answer.opened) {
        settleConnection(model.name, await settled(connectionsApi.retry(model.name), null));
        return;
      }
      watchLogin(model.name);
    });
    return signIn;
  }

  /** A server still starting is read again until it has (the list never waits on one).
   * No limit: a first `npx` or Docker download can take longer than any fixed wait, and
   * the start log shows it is still going. Every 2 s for the first minute, then every 5 s. */
  let startingTimer = null;
  function watchStarting(since = Date.now()) {
    if (startingTimer || !(state.connections || []).some((c) => c.status === 'starting')) return;
    startingTimer = setTimeout(async () => {
      startingTimer = null;
      const answer = await settled(connectionsApi.list(), null);
      if (answer && answer.connections) {
        state.connections = answer.connections;
        draw();
        onConnectionsChanged();
      }
      watchStarting(since);
    }, Date.now() - since < 60 * 1000 ? 2000 : 5000);
  }

  function watchLogin(name) {
    const until = Date.now() + 5 * 60 * 1000;
    const tick = async () => {
      const answer = await settled(connectionsApi.list(), null);
      const found = answer && (answer.connections || []).find((c) => c.name === name);
      if (found && state.connections) {
        state.connections = state.connections.map((c) => (c.name === name ? found : c));
        draw();
        onConnectionsChanged();
      }
      if (found && found.status === 'login' && found.signing_in && Date.now() < until) setTimeout(tick, 2000);
    };
    setTimeout(tick, 2000);
  }

  function drawConnectionCard(connection) {
    const model = connectionModel(connection);
    const card = el(documentRef, 'div', `conn-card${model.enabled ? '' : ' is-off'}`);
    const row = el(documentRef, 'div', 'conn-row');
    const text = el(documentRef, 'span', 'conn-text');
    const title = el(documentRef, 'span', 'conn-title');
    const status = el(documentRef, 'span', 'conn-status', model.statusText);
    status.style.color = model.ink;
    const dot = el(documentRef, 'span', 'conn-dot');
    dot.style.background = model.dot;
    status.prepend(dot);
    title.append(el(documentRef, 'span', 'conn-name', model.name), status);
    text.append(title);
    if (model.does) text.append(el(documentRef, 'span', 'conn-does', model.does));
    row.append(el(documentRef, 'span', 'conn-logo', model.logo), text);
    if (model.status === 'failed' && connectionsApi) {
      const retry = button('cc-button', CONNECTIONS.retry);
      retry.addEventListener('click', async () => {
        play('click');
        retry.disabled = true;
        settleConnection(model.name, await settled(connectionsApi.retry(model.name), null));
      });
      row.append(retry);
    }
    if (model.status === 'login' && connectionsApi) row.append(loginButton(model));
    const open = state.connectionOpen === model.name;
    const details = button('link-button conn-details-toggle', open ? CONNECTIONS.hide : CONNECTIONS.details);
    details.setAttribute('aria-expanded', String(open));
    details.addEventListener('click', () => {
      play(open ? 'close' : 'open');
      state.connectionOpen = open ? null : model.name;
      draw();
    });
    row.append(
      details,
      toggleSwitch(model.enabled, model.name, async (next, toggle) => {
        play(next ? 'open' : 'close');
        toggle.classList.toggle('is-on', next);
        toggle.setAttribute('aria-checked', String(next));
        card.classList.toggle('is-off', !next);
        const [answer] = await Promise.all([
          settled(connectionsApi.set(model.name, { enabled: next }), null),
          pause(SETTLE_MS),
        ]);
        settleConnection(model.name, answer);
        onConnectionsChanged();
      }),
    );
    card.append(row);
    if (model.error) card.append(el(documentRef, 'pre', 'conn-error', model.error));
    if (model.log) card.append(el(documentRef, 'pre', 'conn-log', model.log));
    if (model.needs) card.append(el(documentRef, 'span', 'conn-needs', model.needs));
    if (open) card.append(drawConnectionDetails(model));
    return card;
  }

  function drawConnectionDetails(model) {
    const box = el(documentRef, 'div', 'conn-more');
    if (model.actions.length) {
      const section = el(documentRef, 'div', 'conn-section');
      const list = el(documentRef, 'div', 'conn-actions');
      for (const action of model.actions) {
        const line = el(documentRef, 'span', 'conn-action');
        line.append(
          el(documentRef, 'span', 'conn-action-name', action.name),
          el(documentRef, 'span', `conn-kind${action.changes ? ' is-changes' : ''}`, action.kind),
        );
        list.append(line);
      }
      section.append(el(documentRef, 'span', 'conn-label', CONNECTIONS.whatItDoes), list);
      box.append(section);
    }
    const side = el(documentRef, 'div', 'conn-section');
    const remove = button('cc-button conn-remove', CONNECTIONS.remove);
    remove.addEventListener('click', async () => {
      // A second press confirms: the first one asks, in the button itself.
      if (!remove.classList.contains('is-confirming')) {
        play('click');
        remove.classList.add('is-confirming');
        remove.textContent = CONNECTIONS.removeQuestion(model.name);
        return;
      }
      play('close');
      remove.disabled = true;
      const answer = await settled(connectionsApi.remove(model.name), null);
      if (answer && answer.removed && state.connections) {
        state.connections = state.connections.filter((c) => c.name !== model.name);
        state.connectionOpen = null;
      }
      draw();
      onConnectionsChanged();
    });
    side.append(
      remove,
      el(documentRef, 'span', 'conn-tech', model.tech),
    );
    box.append(side);
    return box;
  }

  function drawAddConnection(target) {
    const form = el(documentRef, 'form', 'conn-add');
    form.noValidate = true;
    const field = (label, placeholder, note) => {
      const wrap = el(documentRef, 'label', 'conn-field');
      const input = el(documentRef, 'input', 'conn-input');
      input.type = 'text';
      input.placeholder = placeholder;
      input.spellcheck = false;
      wrap.append(el(documentRef, 'span', 'conn-field-label', label), input);
      if (note) wrap.append(el(documentRef, 'span', 'conn-field-note', note));
      return { wrap, input };
    };
    const name = field(CONNECTIONS.name, CONNECTIONS.namePlaceholder, CONNECTIONS.nameOptional);
    const command = field(CONNECTIONS.command, CONNECTIONS.commandPlaceholder);
    const problem = el(documentRef, 'span', 'conn-problem');
    problem.setAttribute('role', 'alert');
    const cancel = button('cc-button', CONNECTIONS.cancel);
    const connect = el(documentRef, 'button', 'cc-button cc-button--primary', CONNECTIONS.connect);
    connect.type = 'submit';
    const actions = el(documentRef, 'span', 'conn-form-actions');
    actions.append(el(documentRef, 'span', 'conn-spacer'), cancel, connect);
    form.append(el(documentRef, 'span', 'conn-form-title', CONNECTIONS.addTitle), name.wrap, command.wrap, problem, actions);
    cancel.addEventListener('click', () => {
      play('close');
      state.adding = false;
      draw();
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const chosen = name.input.value.trim();
      // Left empty, the core takes the name the pasted line or JSON gives.
      if (chosen && !/^[A-Za-z0-9_-]{1,40}$/.test(chosen)) {
        problem.textContent = CONNECTIONS.refused.name;
        name.input.focus();
        return;
      }
      if (!command.input.value.trim()) {
        problem.textContent = CONNECTIONS.refused.command;
        command.input.focus();
        return;
      }
      play('click');
      problem.textContent = '';
      connect.disabled = true;
      connect.textContent = CONNECTIONS.connecting;
      let answer;
      try {
        answer = await connectionsApi.add(chosen, command.input.value.trim());
      } catch (error) {
        answer = { error: error.message };
      }
      if (!answer || !answer.name) {
        connect.disabled = false;
        connect.textContent = CONNECTIONS.connect;
        problem.textContent = CONNECTIONS.refused[answer && answer.code] || CONNECTIONS.refused.other;
        play('cancel');
        return;
      }
      play('done');
      state.connections = [...(state.connections || []).filter((c) => c.name !== answer.name), answer];
      state.adding = false;
      state.added = answer.name;
      draw();
      onConnectionsChanged();
      watchStarting();
    });
    target.append(form);
    setTimeout(() => name.input.focus(), 0);
  }

  /** 4f step 3, for the one just added: «{nombre} conectado» and what it can do, or the
   * real error when it did not start. */
  function drawAdded(connection) {
    const model = connectionModel(connection);
    const box = el(documentRef, 'div', 'conn-added');
    if (model.status === 'failed') {
      box.classList.add('is-failed');
      box.append(el(documentRef, 'span', 'conn-added-title', CONNECTIONS.failedTitle(model.name)), el(documentRef, 'pre', 'conn-error', model.error));
      if (model.log) box.append(el(documentRef, 'pre', 'conn-log', model.log));
    } else if (model.status === 'starting') {
      box.append(el(documentRef, 'span', 'conn-added-title', CONNECTIONS.startingTitle(model.name)));
      if (model.log) box.append(el(documentRef, 'pre', 'conn-log', model.log));
    } else if (model.status === 'login') {
      box.append(el(documentRef, 'span', 'conn-added-title', CONNECTIONS.loginTitle(model.name)));
      if (model.error) box.append(el(documentRef, 'pre', 'conn-error', model.error));
    } else {
      const head = el(documentRef, 'span', 'conn-added-head');
      head.append(el(documentRef, 'span', 'conn-logo', model.logo), el(documentRef, 'span', 'conn-added-title', CONNECTIONS.connected(model.name)), el(documentRef, 'span', 'conn-added-check', '✓'));
      box.append(head);
    }
    const ok = button('cc-button cc-button--primary', CONNECTIONS.done);
    ok.addEventListener('click', () => {
      play('close');
      state.added = null;
      draw();
    });
    const actions = el(documentRef, 'span', 'conn-form-actions');
    actions.append(el(documentRef, 'span', 'conn-spacer'));
    if (model.status === 'login' && connectionsApi) actions.append(loginButton(model));
    actions.append(ok);
    box.append(actions);
    return box;
  }

  /** «Importar»: the servers other apps configured, ticked one by one; what is ticked is
   * copied into Cogito's configuration as those files hold it (the note says so). */
  function drawImport(target) {
    const box = el(documentRef, 'div', 'conn-add conn-import');
    box.append(el(documentRef, 'span', 'conn-form-title', CONNECTIONS.importTitle));
    const { found, picked } = state.importing;
    if (found && !found.length) box.append(el(documentRef, 'span', 'conn-field-note', CONNECTIONS.importNone));
    const list = el(documentRef, 'div', 'conn-import-list');
    for (const row of found || []) {
      const line = el(documentRef, 'label', `conn-import-row${row.taken ? ' is-taken' : ''}`);
      const tick = el(documentRef, 'input', 'conn-import-tick');
      tick.type = 'checkbox';
      tick.disabled = Boolean(row.taken);
      tick.checked = picked.has(row.key);
      tick.addEventListener('change', () => {
        if (tick.checked) picked.add(row.key);
        else picked.delete(row.key);
        draw();
      });
      const words = el(documentRef, 'span', 'conn-text');
      words.append(
        el(documentRef, 'span', 'conn-name', row.name),
        el(documentRef, 'span', 'conn-does', `${row.source} · ${row.command}`),
      );
      const notes = [];
      if (row.taken) notes.push(CONNECTIONS.importTaken);
      else if (row.scope === 'project') notes.push(CONNECTIONS.importProject);
      if (row.env && row.env.length) notes.push(CONNECTIONS.importSets(row.env));
      if (row.needs && row.needs.length) notes.push(CONNECTIONS.needs(row.needs));
      if (notes.length) words.append(el(documentRef, 'span', 'conn-field-note', notes.join(' · ')));
      line.append(tick, words);
      list.append(line);
    }
    if (found && found.length) {
      box.append(list, el(documentRef, 'span', 'conn-field-note', CONNECTIONS.importNote));
    }
    if (state.importing.skipped) {
      box.append(el(documentRef, 'span', 'conn-problem', CONNECTIONS.importSkipped(state.importing.skipped)));
    }
    const cancel = button('cc-button', CONNECTIONS.cancel);
    cancel.addEventListener('click', () => {
      play('close');
      state.importing = null;
      draw();
    });
    const add = button('cc-button cc-button--primary', CONNECTIONS.importAdd);
    add.disabled = !picked.size;
    add.addEventListener('click', async () => {
      play('click');
      add.disabled = true;
      const answer = await settled(connectionsApi.import([...picked]), null);
      const skipped = answer && Array.isArray(answer.skipped) ? answer.skipped.length : picked.size;
      const listed = await settled(connectionsApi.list(), null);
      if (listed && listed.connections) state.connections = listed.connections;
      state.importing = skipped ? { found: state.importing.found, picked: new Set(), skipped } : null;
      play(skipped ? 'cancel' : 'done');
      draw();
      onConnectionsChanged();
      watchStarting();
    });
    const actions = el(documentRef, 'span', 'conn-form-actions');
    actions.append(el(documentRef, 'span', 'conn-spacer'), cancel, add);
    box.append(actions);
    target.append(box);
  }

  function drawConnections(target) {
    const head = el(documentRef, 'div', 'conn-heading');
    const words = el(documentRef, 'span', 'conn-heading-text');
    words.append(el(documentRef, 'span', 'tools-heading-title', CONNECTIONS.heading));
    head.append(words);
    if (connectionsApi && !state.adding && !state.importing) {
      if (connectionsApi.discover) {
        const importing = button('cc-button', CONNECTIONS.importButton);
        importing.addEventListener('click', async () => {
          play('open');
          state.importing = { found: null, picked: new Set(), skipped: 0 };
          state.added = null;
          draw();
          const answer = await settled(connectionsApi.discover(), null);
          if (state.importing) state.importing.found = (answer && answer.found) || [];
          draw();
        });
        head.append(importing);
      }
      const add = button('cc-button cc-button--primary', CONNECTIONS.addButton);
      add.addEventListener('click', () => {
        play('open');
        state.adding = true;
        state.added = null;
        draw();
      });
      head.append(add);
    }
    target.append(head);
    if (state.adding) drawAddConnection(target);
    if (state.importing) drawImport(target);
    const added = state.added && (state.connections || []).find((c) => c.name === state.added);
    if (added) target.append(drawAdded(added));
    const list = state.connections || [];
    const cards = el(documentRef, 'div', 'conn-list');
    for (const connection of list) {
      if (added && connection.name === added.name) continue;
      cards.append(drawConnectionCard(connection));
    }
    if (list.length) target.append(cards);
    else if (!state.adding) target.append(el(documentRef, 'div', 'tools-empty', CONNECTIONS.empty));
  }

  // -- Del sistema -----------------------------------------------------------------------

  function drawSystem(target) {
    const rows = list();
    for (const program of state.programs) {
      const model = programRowModel(program);
      const row = el(documentRef, 'div', `tools-row tools-row--program${model.missing ? ' is-missing' : ''}`);
      row.append(
        el(documentRef, 'span', 'tools-row-name tools-program-name', model.name),
        el(documentRef, 'span', 'tools-program-version', model.version),
        el(documentRef, 'span', 'tools-program-path', model.path),
      );
      const action = el(documentRef, 'span', 'tools-program-action');
      row.append(action);
      rows.append(row);
      if (model.missing) {
        const how = el(documentRef, 'button', 'link-button', TOOLS_WINDOW.howToInstall);
        how.type = 'button';
        action.append(how);
        const slot = el(documentRef, 'div', 'tools-program-help');
        rows.append(slot);
        how.addEventListener('click', async () => {
          if (slot.firstChild) return;
          play('open');
          const help = await settled(api.programHelp(program.id), null);
          if (!help) return;
          // «Cómo instalarlo» opens the same card the thread shows (4b), with the same rule.
          const card = buildMissingProgramCard(
            missingProgramModel(help),
            {
              onCopy: () => {
                play('click');
                api.copyCommand(program.id);
              },
              onOpenOfficial: () => {
                play('click');
                api.openOfficial(program.id);
              },
              onDone: () => {
                play('close');
                slot.replaceChildren(buildRow(rowModel(COMPONENTS.missingRow, help.name, COMPONENTS.missingSeen), documentRef));
              },
            },
            documentRef,
          );
          slot.replaceChildren(card.el);
        });
      }
    }
    target.append(heading(TOOLS_WINDOW.systemHeading), rows);
  }

  // -- Búsqueda --------------------------------------------------------------------------

  function drawSearch(target) {
    const rows = list();
    for (const level of levelModels(state.search?.local_search)) {
      const row = el(documentRef, 'div', 'tools-row tools-row--level');
      const text = el(documentRef, 'span', 'tools-row-text');
      text.append(el(documentRef, 'span', 'tools-row-name', level.name), el(documentRef, 'span', 'tools-row-description', level.description));
      if (level.note) text.append(el(documentRef, 'span', 'tools-row-meta', level.note));
      if (level.status) text.append(el(documentRef, 'span', 'tools-row-meta', level.status));
      row.append(el(documentRef, 'span', 'tools-level-number', level.n), text);
      rows.append(row);
    }
    target.append(heading(TOOLS_WINDOW.searchHeading), rows);
  }

  // -- Habilidades ---------------------------------------------------------------------------

  function drawSkills(target) {
    const rows = list();
    for (const skill of state.skills) {
      const row = el(documentRef, 'div', 'tools-row tools-row--skill');
      const text = el(documentRef, 'span', 'tools-row-text');
      text.append(el(documentRef, 'span', 'tools-row-name', skill.name));
      if (skill.description) {
        const description = el(documentRef, 'span', 'tools-row-description', skill.description);
        description.title = escapeControlChars(String(skill.description));
        text.append(description);
      }
      if (skill.directory) text.append(el(documentRef, 'span', 'tools-row-meta', homeShort(skill.directory)));
      row.append(text);
      rows.append(row);
    }
    target.append(
      heading(TOOLS_WINDOW.skillsHeading),
      state.skills.length ? rows : el(documentRef, 'div', 'tools-empty', TOOLS_WINDOW.skillsEmpty),
    );
  }

  // -- Diseño ---------------------------------------------------------------------------------

  /** A choice of a few, drawn as the OS 9 segmented control: a radio group whose arrow keys
   * move the choice (WAI-ARIA radio group), with only the chosen option as a Tab stop. */
  function choiceControl(model, onChoose) {
    const group = el(documentRef, 'span', 'segmented tools-setting-choice');
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', model.label);
    const options = model.options;
    const focusIndex = Math.max(0, options.findIndex((option) => option.active));
    options.forEach((option, index) => {
      const node = el(documentRef, 'button', `segmented-option${option.active ? ' is-active' : ''}`, option.label);
      node.type = 'button';
      node.setAttribute('role', 'radio');
      node.setAttribute('aria-checked', String(option.active));
      node.tabIndex = index === focusIndex ? 0 : -1;
      node.dataset.setting = model.key;
      node.dataset.value = option.value;
      node.addEventListener('click', () => {
        if (!option.active) onChoose(option.value, node);
      });
      node.addEventListener('keydown', (event) => {
        let next;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % options.length;
        else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + options.length) % options.length;
        else return;
        event.preventDefault();
        onChoose(options[next].value, group.children[next]);
      });
      group.append(node);
    });
    return group;
  }

  async function chooseTheme(theme) {
    if (!appearanceApi || !state.appearance) return;
    play('click');
    const before = state.appearance.theme;
    // At once, so the chosen segment lights before the main process answers.
    state.appearance = { ...state.appearance, theme };
    draw();
    const answer = await settled(appearanceApi.set(theme), null);
    const settledTheme = answer && answer.theme ? answer.theme : before;
    if (settledTheme === state.appearance.theme) return;
    // Refused or not saved: back to what the main process holds, focus kept on that choice.
    state.appearance = { ...state.appearance, theme: settledTheme };
    const focused = panel.contains(documentRef.activeElement);
    draw();
    if (focused) panel.querySelector(`[data-setting="theme"][data-value="${settledTheme}"]`)?.focus();
  }

  const CHOOSE = { theme: chooseTheme };

  function drawAppearance(target) {
    const rows = list();
    rows.classList.add('tools-settings');
    for (const setting of settingModels(state.appearance || {})) {
      const row = el(documentRef, 'div', `tools-row tools-row--setting tools-row--${setting.key}`);
      const text = el(documentRef, 'span', 'tools-row-text');
      text.append(el(documentRef, 'span', 'tools-row-name', setting.label));
      row.append(
        text,
        choiceControl(setting, (value, node) => {
          CHOOSE[setting.key]?.(value);
          // The redraw replaces the control: keep focus on the same choice.
          const again = panel.querySelector(`[data-setting="${setting.key}"][data-value="${value}"]`);
          (again || node)?.focus();
        }),
      );
      rows.append(row);
    }
    target.append(heading(TOOLS_WINDOW.appearanceHeading), rows);
  }

  // -- drawing and loading -----------------------------------------------------------------

  const DRAW = {
    components: drawComponents,
    connections: drawConnections,
    system: drawSystem,
    search: drawSearch,
    skills: drawSkills,
    appearance: drawAppearance,
  };

  /** The tab's content box. A tab change gets a new one (with the design's `metaSwap`
   * entrance); a redraw of the same tab empties and reuses it, so a refresh never replays
   * the entrance. */
  let content = null;
  let enteringTimer = null;

  function draw() {
    const models = currentTabs();
    // A hidden tab (UX-11/12/13) is never selected: fall back to the first shown tab, as
    // for a stale `state.tab` after a refresh. Only once `loaded` is set (see its comment).
    if (loaded && models.length && !models.some((tab) => tab.key === state.tab)) state.tab = models[0].key;
    drawTabs(models);
    const scroll = panel.scrollTop;
    if (content && content.isConnected && content.dataset.tab === state.tab) {
      content.replaceChildren();
    } else {
      content = el(documentRef, 'div', `tools-tab-content tools-tab-content--${state.tab} is-entering`);
      content.dataset.tab = state.tab;
      panel.replaceChildren(content);
      // Rows drawn while the tab enters rise with it (4e's decisions: `rise .25s`); rows
      // from later redraws do not.
      const entering = content;
      clearTimeout(enteringTimer);
      enteringTimer = setTimeout(() => entering.classList.remove('is-entering'), 300);
    }
    panel.setAttribute('aria-labelledby', `tools-tab-${state.tab}`);
    (DRAW[state.tab] || drawSystem)(content);
    panel.scrollTop = scroll;
  }

  async function refresh(which = ['components', 'connections', 'system', 'skills', 'search', 'appearance']) {
    const loads = {
      connections: async () => {
        if (!connectionsApi) return;
        const answer = await settled(connectionsApi.list(), null);
        state.connections = answer ? answer.connections || [] : state.connections;
        watchStarting();
      },
      components: async () => {
        const answer = (await settled(api.list(), { components: [] })).components || [];
        state.components = visibleComponents(answer);
      },
      system: async () => {
        state.programs = (await settled(api.programs(), { programs: [] })).programs || [];
      },
      skills: async () => {
        if (!skillsApi) return;
        state.skills = (await settled(skillsApi.list(), { skills: [] })).skills || [];
      },
      search: async () => {
        state.search = await settled(api.searchLevels(), null);
      },
      appearance: async () => {
        if (!appearanceApi) return;
        const answer = await settled(appearanceApi.get(), null);
        if (answer && answer.theme) state.appearance = { ...state.appearance, theme: answer.theme };
      },
    };
    await Promise.all(which.map((name) => loads[name]()));
    loaded = true;
    if (overlay.isConnected) draw();
  }

  function show(tab, { adding: add = false } = {}) {
    if (add) state.adding = true;
    state.tab = tab;
    draw();
    if (tab === 'connections') refresh(['connections']);
    if (tab === 'skills') refresh(['skills']);
    // The buscador local prepares and starts after the window opens: ask again on entry.
    if (tab === 'search') refresh(['search']);
  }

  documentRef.body.append(overlay);
  overlay.toolsWindow = { close, show };
  play('window');
  draw();
  win.focus();
  refresh();
  return overlay.toolsWindow;
}

export { openToolsWindow };
