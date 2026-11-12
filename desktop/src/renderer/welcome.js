'use strict';

/**
 * Design 1d-1e, «Bienvenida»: step 1 (your name) and step 2 (the service). Shown full-window
 * under the title bar on a first launch, and as the 1e card alone from the model menu's
 * «Conectar otro servicio…». Layout, values and motion are ported from the design canvas
 * (`COGITO v2.dc.html`, 1d/1e) in welcome.css, sounds come from `sfx.js` and texts from
 * `welcome-view.js`. Everything shown is real: services from the core's table, a test's
 * answer from the service itself, models from the service's own list.
 *
 *   const screen = createWelcome(api);
 *   screen.show({ mode: 'first', state });    // state: GET /settings/welcome
 *   screen.show({ mode: 'connect' });          // «Conectar otro servicio…»
 *   screen.show({ mode: 'blocked', state });  // the core serves no model: say why, connect
 *
 * A model the core could not serve (the one it started with, `state.unserved`, or one a save
 * could not activate) shows as a red card at the top of 1e with the core's reason and
 * «Reintentar» (`connect.activate`). The welcome is never marked done or closed on it.
 * Focus survives every redraw, Tab stays inside the screen, and a test's progress and
 * outcome are announced in a polite live region.
 *
 * `api`: `{ welcome: {set}, connect: {providers, apiModels, pickMotorisFolder,
 * pickMotorisConfig, motorisInstall, motorisRunning, save, activate, openLink}, isBusy(),
 * onDone(result), onSwitchRefused?(answer), play?(kind) }`.
 *
 * Cogito Motoris has two tabs: «Iniciar con Cogito» (a folder and its config, picked in the
 * main process's dialogs and read without running anything) and «Ya en marcha» (an address,
 * and a key when it is on another machine).
 * `onDone` hears `{saved}`, `{skipped}` or `{activated}` once the screen closes on its own.
 * A key typed here is sent once to be tested and once to be saved, and the field is cleared
 * when the screen goes.
 */

import { paint } from './orb.js';
import { escapeControlChars } from './sanitize.js';
import { sfx } from './sfx.js';
import { MODEL_PICKER, WINDOW_CHROME } from './strings.js';
import {
  WELCOME,
  failureView,
  hostOf,
  missingInput,
  motorisFailureView,
  okLine,
  saveFailure,
  serviceList,
  unservedView,
} from './welcome-view.js';

/** The design's close timing (README: «encogimiento + fade (140 ms)»). */
const CLOSE_MS = 140;
/** An open list: welcome.css's max-height, the room kept from the window's edge (the 3 px
 * gap to the trigger and a margin), and the least it shrinks to before it scrolls. */
const MENU_MAX_HEIGHT = 264;
const MENU_GAP = 11;
const MENU_MIN_HEIGHT = 96;

/** The last part of a path, for a picked folder or file; the whole path is its title. */
function baseName(path) {
  const parts = String(path || '').split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || '');
}

/** What the Cogito Motoris row holds: the open tab, and the folder and config picked. */
function freshMotoris() {
  return { tab: 'install', checkout: null, config: null };
}

function createWelcome(api, documentRef = document) {
  const play = typeof api.play === 'function' ? api.play : sfx;

  function el(tag, className, text) {
    const node = documentRef.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = escapeControlChars(String(text));
    return node;
  }

  function button(className, text, onClick) {
    const node = el('button', className, text);
    node.type = 'button';
    if (onClick) node.addEventListener('click', onClick);
    return node;
  }

  const overlay = el('div', 'welcome-overlay');
  overlay.hidden = true;
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  const stage = el('div', 'welcome-stage');
  // Polite announcements as a test starts and ends (WCAG 2.2, 4.1.3 Status Messages). One
  // region for the screen's whole life: a region inserted together with its text is not read.
  const live = el('div', 'welcome-live');
  live.setAttribute('role', 'status');
  live.setAttribute('aria-live', 'polite');
  overlay.append(stage, live);
  documentRef.body.append(overlay);

  let announceTimer = null;
  /** Says `text` in the live region; no text empties it. The region keeps its last words
   * until something replaces them, so a service switch and a closing screen empty it:
   * «✓ Conectado» must not outlive the attempt that earned it. */
  function announce(text) {
    clearTimeout(announceTimer);
    live.textContent = '';
    if (text) announceTimer = setTimeout(() => (live.textContent = escapeControlChars(String(text))), 30);
  }

  /** Controls carry a `data-focus` name. After a redraw, focus goes to the first of `keys`
   * that is drawn and enabled, never to <body>. */
  let focusAfter = null;
  function focusKey(node, key) {
    node.dataset.focus = key;
    return node;
  }
  function restoreFocus(keys) {
    for (const key of keys) {
      const node = key ? stage.querySelector(`[data-focus="${key}"]`) : null;
      if (node && !node.disabled && node.getClientRects().length) {
        node.focus({ preventScroll: true });
        if (node.tagName === 'INPUT') {
          // Put the caret back at the end of the text.
          try {
            node.setSelectionRange(node.value.length, node.value.length);
          } catch {
            /* input types without selection support throw; keep the browser's caret */
          }
        }
        return true;
      }
    }
    return false;
  }
  let labelCount = 0;
  /** A label span tied to its field by id (WCAG 2.2, 1.3.1 / 4.1.2). */
  function labelled(text, field) {
    labelCount += 1;
    const label = el('span', 'welcome-label', text);
    label.id = `welcome-label-${labelCount}`;
    field.setAttribute('aria-labelledby', label.id);
    return label;
  }

  // -- state --------------------------------------------------------------------------------

  let mode = 'first';
  let welcome = {};
  let services = serviceList([]);
  let serviceId = null;
  /** `state` is `idle | testing | ok | fail`, `answer` is what led there, `fails` counts
   * failures (the canvas alternates two shake keyframes so each failure shakes again). */
  let test = { state: 'idle', answer: null, fails: 0 };
  let models = [];
  let chosenModel = null;
  let motoris = freshMotoris();
  let busySaving = false;
  /** The core's services, fetched once per `show`. */
  let servicesReady = Promise.resolve();
  /** What the core could not serve: `{reason, model: {id, label}, log}`, or null. */
  let unserved = null;
  let retrying = false;
  let savedTitle = null;
  let closeTimer = null;
  /** The open dropdown: `{menu, trigger}`, or null. */
  let openMenu = null;
  let refs = {};

  const service = () => services.find((s) => s.id === serviceId) || services[0];

  function takeTitle() {
    const wordmark = documentRef.querySelector('.wordmark-text');
    if (savedTitle === null) savedTitle = { wordmark: wordmark ? wordmark.textContent : null, document: documentRef.title };
    const text = WINDOW_CHROME.title(WELCOME.titleBar);
    if (wordmark) wordmark.textContent = text;
    documentRef.title = text;
  }

  function giveTitleBack() {
    if (!savedTitle) return;
    const wordmark = documentRef.querySelector('.wordmark-text');
    if (wordmark && savedTitle.wordmark !== null) wordmark.textContent = savedTitle.wordmark;
    documentRef.title = savedTitle.document;
    savedTitle = null;
  }

  // -- a dropdown, as the canvas' «SERVICIO» and «MODELO» menus --------------------------------

  function closeMenu({ sound = true } = {}) {
    if (!openMenu) return;
    const { menu, trigger } = openMenu;
    openMenu = null;
    menu.classList.add('is-closing');
    trigger.setAttribute('aria-expanded', 'false');
    if (sound) play('close');
    setTimeout(() => menu.remove(), CLOSE_MS);
  }

  /** Keeps an open list inside the window: below its trigger when it fits, else on the
   * side with more room, scrolling inside itself (QA 2026-10-11: at 560x440 the last
   * service ran past the window's bottom edge). */
  function placeMenu(menu, trigger) {
    const view = documentRef.defaultView;
    if (!view || typeof trigger.getBoundingClientRect !== 'function') return;
    const box = trigger.getBoundingClientRect();
    const top = typeof overlay.getBoundingClientRect === 'function' ? Math.max(0, overlay.getBoundingClientRect().top) : 0;
    const below = view.innerHeight - box.bottom - MENU_GAP;
    const above = box.top - top - MENU_GAP;
    const wanted = Math.min(menu.scrollHeight, MENU_MAX_HEIGHT);
    const up = wanted > below && above > below;
    menu.classList.toggle('opens-up', up);
    menu.style.maxHeight = `${Math.max(MENU_MIN_HEIGHT, Math.min(MENU_MAX_HEIGHT, up ? above : below))}px`;
  }

  function toggleMenu(wrap, trigger, rows, onPick) {
    if (openMenu && openMenu.trigger === trigger) {
      closeMenu();
      return;
    }
    closeMenu({ sound: false });
    const menu = el('div', 'welcome-menu soft-scroll');
    menu.setAttribute('role', 'listbox');
    for (const row of rows) {
      const option = button('welcome-option');
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', String(Boolean(row.check)));
      option.append(el('span', 'welcome-option-check', row.check ? '✓' : ''), el('span', `welcome-option-name${row.mono ? ' is-mono' : ''}`, row.name), el('span', 'welcome-option-sub', row.sub || ''));
      option.addEventListener('click', () => {
        play('select');
        closeMenu({ sound: false });
        onPick(row);
      });
      menu.append(option);
    }
    wrap.append(menu);
    placeMenu(menu, trigger);
    trigger.setAttribute('aria-expanded', 'true');
    openMenu = { menu, trigger };
    play('open');
    const first = menu.querySelector('[aria-selected="true"]') || menu.querySelector('.welcome-option');
    if (first) first.focus({ preventScroll: false });
  }

  function outside(event) {
    if (!openMenu) return;
    if (openMenu.menu.contains(event.target) || openMenu.trigger.contains(event.target)) return;
    closeMenu();
  }

  function onKeydown(event) {
    if (overlay.hidden) return;
    if (openMenu) {
      const options = [...openMenu.menu.querySelectorAll('.welcome-option')];
      const at = options.indexOf(documentRef.activeElement);
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        const trigger = openMenu.trigger;
        closeMenu();
        trigger.focus();
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const next = at < 0 ? 0 : Math.max(0, Math.min(options.length - 1, at + (event.key === 'ArrowDown' ? 1 : -1)));
        if (options[next]) options[next].focus();
      }
      return;
    }
    if (event.key === 'Tab') {
      // Focus trap for the modal dialog: Tab and Shift+Tab cycle inside it, as
      // `tools-window.js` does for 4e.
      const stops = [...overlay.querySelectorAll('button, input, [tabindex]')].filter(
        (node) => !node.disabled && node.tabIndex !== -1 && node.getClientRects().length,
      );
      if (!stops.length) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const active = documentRef.activeElement;
      if (event.shiftKey ? active === first || !overlay.contains(active) : active === last || !overlay.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
      return;
    }
    if (event.key === 'Escape' && mode === 'connect' && !busySaving) {
      event.preventDefault();
      event.stopPropagation();
      play('close');
      close();
    }
  }

  // -- step 1: the name (1d) -------------------------------------------------------------------

  function renderName() {
    const card = el('div', 'welcome-card welcome-card--name');
    const orbSlot = el('span', 'welcome-orb');
    const orb = paint(orbSlot, 124);
    orb.setAttribute('state', 'responding');
    const block = el('label', 'welcome-block welcome-block--name');
    const input = el('input', 'welcome-field welcome-field--name');
    input.type = 'text';
    input.maxLength = 60;
    input.autocomplete = 'given-name';
    input.spellcheck = false;
    input.value = welcome.name || '';
    focusKey(input, 'name');
    // «Continuar» with the name empty says so here, as well as shaking the field.
    const hint = el('span', 'welcome-hint', WELCOME.nameMissing);
    hint.id = 'welcome-name-hint';
    hint.hidden = true;
    input.addEventListener('input', () => {
      if (hint.hidden) return;
      hint.hidden = true;
      input.removeAttribute('aria-invalid');
      announce('');
    });
    block.append(labelled(WELCOME.nameLabel, input), input, hint);
    const footer = el('div', 'welcome-footer welcome-footer--name');
    const next = focusKey(button('welcome-primary', WELCOME.next, onNext), 'next');
    footer.append(el('span', 'welcome-step', WELCOME.step1), el('span', 'welcome-spacer'), next);
    card.append(orbSlot, el('span', 'welcome-hello', WELCOME.hello), el('span', 'welcome-lead', WELCOME.lead), block, footer);
    stage.replaceChildren(card);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        onNext();
      }
    });
    refs = { nameInput: input };
    input.focus({ preventScroll: true });

    async function onNext() {
      const name = input.value.trim();
      if (!name) {
        play('close');
        shake(input);
        hint.hidden = false;
        input.setAttribute('aria-invalid', 'true');
        input.setAttribute('aria-describedby', hint.id);
        announce(WELCOME.nameMissing);
        input.focus({ preventScroll: true });
        return;
      }
      play('click');
      const answer = await Promise.resolve(api.welcome.set({ name })).catch(() => null);
      welcome = { ...welcome, ...(answer && !answer.error ? answer : { name }) };
      await servicesReady;
      // Focus moves to step 2's first field, not to the button that replaced «Continuar».
      const held = documentRef.activeElement;
      if (held && stage.contains(held) && typeof held.blur === 'function') held.blur();
      renderConnect();
    }
  }

  // -- step 2: the service (1e) -----------------------------------------------------------------

  function resetTest() {
    test = { state: 'idle', answer: null, fails: test.fails };
    models = [];
    chosenModel = null;
    announce('');
  }

  function shake(field) {
    field.classList.remove('is-shaking-1', 'is-shaking-2');
    void field.offsetWidth;
    field.classList.add(test.fails % 2 ? 'is-shaking-2' : 'is-shaking-1');
  }

  /** Sets the field's border and shadow for the test's state. A failure shakes it only the
   * first time it is drawn (the canvas' `shake1`/`shake2`, alternating). */
  function fieldState(field, { fail = test.state === 'fail' } = {}) {
    field.dataset.state = fail ? 'fail' : test.state === 'ok' ? 'ok' : 'idle';
    field.classList.remove('is-shaking-1', 'is-shaking-2');
    if (fail && !test.drawn) field.classList.add(test.fails % 2 ? 'is-shaking-2' : 'is-shaking-1');
  }

  function selectTrigger(name, sub, { mono = true } = {}) {
    const trigger = button('welcome-select');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.append(el('span', `welcome-select-name${mono ? '' : ' is-sans'}`, name), el('span', 'welcome-select-sub', sub || ''), el('span', 'welcome-caret', '▼'));
    return trigger;
  }

  function serviceBlock() {
    const block = el('div', 'welcome-block');
    const wrap = el('div', 'welcome-select-wrap');
    const current = service();
    const trigger = focusKey(selectTrigger(current.name, current.sub), 'service');
    trigger.classList.add('welcome-select--service');
    trigger.addEventListener('click', () =>
      toggleMenu(
        wrap,
        trigger,
        services.map((s) => ({ ...s, check: s.id === serviceId })),
        (row) => {
          if (row.id === serviceId) return;
          serviceId = row.id;
          resetTest();
          // Another service takes another key and address.
          refs = {};
          focusAfter = 'service';
          renderConnect();
        },
      ),
    );
    wrap.append(trigger);
    block.append(labelled(WELCOME.serviceLabel, trigger), wrap);
    return block;
  }

  /** The technical dump of a failure (a status line, an endpoint, the service's own English
   * words) behind «Detalle técnico», collapsed: the card says what happened in Spanish and
   * the person opens this only to copy it. Returns the toggle and the log, to append in that
   * order. */
  function detailToggle(lines) {
    const log = el('span', 'welcome-error-log soft-scroll', lines.join('\n'));
    log.hidden = true;
    const toggle = button('welcome-link welcome-error-detail-toggle', MODEL_PICKER.technicalDetail, () => {
      log.hidden = !log.hidden;
      toggle.setAttribute('aria-expanded', String(!log.hidden));
    });
    toggle.setAttribute('aria-expanded', 'false');
    return [toggle, log];
  }

  /** The testing line, the green line or the red card (the canvas' three `test` states).
   * Each plays its entry motion once, not on every redraw. */
  function statusNode(kind) {
    const node = statusContent(kind);
    if (node && test.drawn) node.classList.add('is-settled');
    return node;
  }

  /** The test's kind: `api`, or the open Cogito Motoris tab. */
  function testKind() {
    const current = service();
    if (current.kind !== 'motoris') return 'api';
    return motoris.tab === 'install' ? 'motoris-install' : 'motoris-running';
  }

  /** The failure card's view for `kind`, as `{title, desc, action, log, retry}`. */
  function failureCard(kind) {
    if (kind === 'api') return failureView(test.answer, { service: service() });
    const view = motorisFailureView(test.answer, { install: kind === 'motoris-install' });
    return { title: view.title, desc: view.text, action: null, log: view.log, retry: view.retry };
  }

  function testingMessage(kind) {
    const current = service();
    if (kind === 'motoris-install') return WELCOME.testingInstall;
    if (kind === 'motoris-running') return WELCOME.testingCloud(hostOf(motorisUrl()));
    return WELCOME.testingCloud(hostOf(current.customUrl ? refs.urlValue || '' : current.baseUrl));
  }

  function statusContent(kind) {
    if (test.state === 'testing') {
      const line = el('span', 'welcome-testing');
      line.append(el('span', 'welcome-spinner'), documentRef.createTextNode(testingMessage(kind)));
      return line;
    }
    if (test.state === 'ok') return el('span', 'welcome-ok', okLine(kind, test.answer || {}));
    if (test.state === 'fail') {
      const view = failureCard(kind);
      const card = el('div', 'welcome-error');
      const title = el('span', 'welcome-error-title');
      title.append(el('span', 'welcome-error-dot'), documentRef.createTextNode(view.title));
      card.append(title);
      if (view.desc) card.append(el('span', 'welcome-error-desc', view.desc));
      if (view.log && view.log.length) card.append(...detailToggle(view.log));
      const actions = el('span', 'welcome-error-actions');
      if (view.retry !== false) actions.append(focusKey(button('welcome-square welcome-square--small', WELCOME.retry, runTest), 'retry'));
      if (view.action) {
        actions.append(
          focusKey(button('welcome-link', view.action.label, () => {
            play('click');
            if (view.action.link) api.connect.openLink(view.action.link);
          }), 'action'),
        );
      }
      if (actions.children.length) card.append(actions);
      return card;
    }
    return null;
  }

  function modelBlock(kind) {
    const ready = test.state === 'ok' && models.length > 0;
    const block = el('div', 'welcome-block welcome-model');
    block.dataset.ready = String(ready);
    const wrap = el('div', 'welcome-select-wrap');
    const chosen = models.find((m) => m.id === chosenModel);
    // Model name only, as on the canvas (`{{ localModel }}` ▼).
    const trigger = focusKey(selectTrigger(chosen ? chosen.name : WELCOME.pickModel, '', { mono: true }), 'model');
    trigger.classList.add('welcome-select--model');
    trigger.disabled = !ready;
    if (!chosen) trigger.classList.add('is-placeholder');
    trigger.addEventListener('click', () =>
      toggleMenu(
        wrap,
        trigger,
        models.map((m) => ({ ...m, mono: true, check: m.id === chosenModel })),
        (row) => {
          chosenModel = row.id;
          focusAfter = 'model';
          renderConnect({ quiet: true });
        },
      ),
    );
    wrap.append(trigger);
    block.append(labelled(WELCOME.modelLabel, trigger), wrap);
    refs.modelTrigger = trigger;
    return block;
  }

  function apiSection(current) {
    const section = el('div', 'welcome-section');
    // «Falta la URL» marks the address field, not the key's.
    const urlMissing = test.state === 'fail' && Boolean(test.answer) && test.answer.kind === 'no_url';
    if (current.customUrl) {
      const urlBlock = el('div', 'welcome-block');
      const urlInput = el('input', 'welcome-field welcome-field--mono');
      urlInput.type = 'text';
      urlInput.spellcheck = false;
      urlInput.autocomplete = 'off';
      urlInput.value = refs.urlValue || '';
      focusKey(urlInput, 'url');
      urlInput.addEventListener('input', () => {
        refs.urlValue = urlInput.value;
      });
      if (urlMissing) fieldState(urlInput);
      urlBlock.append(labelled(WELCOME.urlLabel, urlInput), urlInput);
      section.append(urlBlock);
      refs.urlInput = urlInput;
    }
    const keyBlock = el('div', 'welcome-block');
    const row = el('div', 'welcome-row');
    const keyInput = el('input', 'welcome-field welcome-field--key');
    keyInput.type = 'password';
    keyInput.autocomplete = 'off';
    keyInput.spellcheck = false;
    keyInput.value = refs.keyValue || '';
    focusKey(keyInput, 'key');
    keyInput.addEventListener('input', () => {
      refs.keyValue = keyInput.value;
      if (test.state !== 'idle' && test.state !== 'testing') {
        resetTest();
        renderConnect({ quiet: true, keepFocus: keyInput });
      }
    });
    keyInput.addEventListener('keydown', (event) => {
      // Enter here is «Probar»: the person's own action, and it does send the key to the
      // service to list its models (a request to that host). It never saves or connects;
      // «Empezar»/«Conectar» stay a separate press once a model is chosen.
      if (event.key === 'Enter') {
        event.preventDefault();
        runTest();
      }
    });
    fieldState(keyInput, { fail: test.state === 'fail' && !urlMissing });
    const probe = testButton(WELCOME.test, WELCOME.testing);
    row.append(keyInput, probe);
    keyBlock.append(labelled(WELCOME.keyLabel, keyInput), row);
    const status = statusNode('api');
    if (status) keyBlock.append(status);
    keyBlock.append(el('span', 'welcome-note', WELCOME.keyNote));
    section.append(keyBlock);
    // The canvas' cloud branch has no «MODELO». It rises in once the service has listed its
    // real models.
    if (test.state === 'ok') {
      const block = modelBlock('api');
      if (!test.drawn) block.classList.add('is-arriving');
      section.append(block);
    }
    refs.keyInput = keyInput;
    refs.testField = keyInput;
    return section;
  }

  function motorisUrl() {
    return refs.motorisUrl === undefined ? WELCOME.motorisUrl : refs.motorisUrl;
  }

  /** The two tabs (WAI-ARIA tabs, automatic activation): the open one is the Tab stop,
   * Left and Right move between them. */
  function motorisTabs() {
    const tabs = el('div', 'welcome-tabs');
    tabs.setAttribute('role', 'tablist');
    const order = [
      ['install', WELCOME.motorisInstallTab],
      ['running', WELCOME.motorisRunningTab],
    ];
    const choose = (tab) => {
      if (tab === motoris.tab || test.state === 'testing') return;
      play('select');
      motoris.tab = tab;
      resetTest();
      focusAfter = `tab-${tab}`;
      renderConnect({ quiet: true });
    };
    for (const [tab, label] of order) {
      const selected = motoris.tab === tab;
      const node = focusKey(button('welcome-square welcome-square--small welcome-tab', label, () => choose(tab)), `tab-${tab}`);
      node.setAttribute('role', 'tab');
      node.setAttribute('aria-selected', String(selected));
      node.tabIndex = selected ? 0 : -1;
      node.addEventListener('keydown', (event) => {
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        event.preventDefault();
        choose(motoris.tab === 'install' ? 'running' : 'install');
      });
      tabs.append(node);
    }
    return tabs;
  }

  /** A folder or file picked in the main process's dialog, drawn as a field. */
  function pickField(path, placeholder, key, onClick) {
    const node = focusKey(button('welcome-field welcome-field--mono welcome-field--file', path ? baseName(path) : placeholder, onClick), key);
    if (!path) node.classList.add('is-placeholder');
    node.title = path ? escapeControlChars(path) : '';
    return node;
  }

  function motorisInstallPanel(section) {
    const folderBlock = el('div', 'welcome-block');
    const folder = pickField(motoris.checkout, WELCOME.pickFolder, 'motoris-folder', pickFolder);
    folderBlock.append(labelled(WELCOME.folderLabel, folder), folder);
    const configBlock = el('div', 'welcome-block');
    const row = el('div', 'welcome-row');
    const config = pickField(motoris.config, WELCOME.pickConfig, 'motoris-config', pickConfig);
    fieldState(config);
    const probe = testButton(WELCOME.test, WELCOME.testing);
    probe.disabled = !motoris.checkout || !motoris.config;
    row.append(config, probe);
    configBlock.append(labelled(WELCOME.configLabel, config), row);
    const status = statusNode('motoris-install');
    if (status) configBlock.append(status);
    section.append(folderBlock, configBlock);
    refs.testField = config;
    refs.first = folder;
  }

  function motorisRunningPanel(section) {
    const urlBlock = el('div', 'welcome-block');
    const urlInput = el('input', 'welcome-field welcome-field--mono');
    urlInput.type = 'text';
    urlInput.spellcheck = false;
    urlInput.autocomplete = 'off';
    urlInput.value = motorisUrl();
    focusKey(urlInput, 'url');
    // «Falta la clave» marks the key's field; every other failure is about the address.
    const keyMissing = test.state === 'fail' && Boolean(test.answer) && test.answer.kind === 'no_key';
    fieldState(urlInput, { fail: test.state === 'fail' && !keyMissing });
    const keyInput = el('input', 'welcome-field welcome-field--key');
    keyInput.type = 'password';
    keyInput.autocomplete = 'off';
    keyInput.spellcheck = false;
    keyInput.value = refs.keyValue || '';
    focusKey(keyInput, 'key');
    fieldState(keyInput, { fail: keyMissing });
    for (const [input, store] of [[urlInput, 'motorisUrl'], [keyInput, 'keyValue']]) {
      input.addEventListener('input', () => {
        refs[store] = input.value;
        if (test.state !== 'idle' && test.state !== 'testing') {
          resetTest();
          renderConnect({ quiet: true, keepFocus: input });
        }
      });
      // Enter is «Probar»: it asks that address for its models and never saves.
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          runTest();
        }
      });
    }
    urlBlock.append(labelled(WELCOME.urlLabel, urlInput), urlInput);
    const keyBlock = el('div', 'welcome-block');
    const row = el('div', 'welcome-row');
    row.append(keyInput, testButton(WELCOME.test, WELCOME.testing));
    keyBlock.append(labelled(WELCOME.optionalKeyLabel, keyInput), row);
    const status = statusNode('motoris-running');
    if (status) keyBlock.append(status);
    section.append(urlBlock, keyBlock);
    if (test.state === 'ok') {
      const block = modelBlock('motoris-running');
      if (!test.drawn) block.classList.add('is-arriving');
      section.append(block);
    }
    refs.urlInput = urlInput;
    refs.keyInput = keyInput;
    refs.testField = urlInput;
    refs.first = urlInput;
  }

  function motorisSection() {
    const section = el('div', 'welcome-section');
    section.append(motorisTabs());
    if (motoris.tab === 'install') motorisInstallPanel(section);
    else motorisRunningPanel(section);
    return section;
  }

  /** «Probar». While a test runs it is `aria-disabled`, not `disabled`, so it
   * keeps its tab stop and focus (`disabled` would drop focus to <body>). */
  function testButton(label, busyLabel) {
    const node = focusKey(button('welcome-square welcome-square--row', test.state === 'testing' ? busyLabel : label, runTest), 'test');
    node.setAttribute('aria-disabled', String(test.state === 'testing'));
    return node;
  }

  /** The red card for a model the core could not serve, with «Reintentar». The technical
   * dump (the core's own log) sits behind «Detalle técnico», collapsed by default: the
   * short Spanish summary (`view.desc`) is what the person reads first. */
  function unservedCard() {
    const view = unservedView(unserved, MODEL_PICKER);
    const card = el('div', 'welcome-error welcome-unserved');
    if (unserved.drawn) card.classList.add('is-settled');
    const title = el('span', 'welcome-error-title');
    title.append(el('span', 'welcome-error-dot'), documentRef.createTextNode(view.title));
    card.append(title);
    if (view.desc) card.append(el('span', 'welcome-error-desc', view.desc));
    if (view.log.length) card.append(...detailToggle(view.log));
    const actions = el('span', 'welcome-error-actions');
    const again = focusKey(button('welcome-square welcome-square--small', WELCOME.retry, retryUnserved), 'unserved-retry');
    again.setAttribute('aria-disabled', String(retrying));
    actions.append(again);
    card.append(actions);
    return card;
  }

  async function retryUnserved() {
    if (retrying || !unserved || !unserved.model || !unserved.model.id) return;
    play('click');
    retrying = true;
    announce(WELCOME.retrying(unserved.model.label || unserved.model.id));
    focusAfter = 'unserved-retry';
    renderConnect({ quiet: true });
    const answer = await Promise.resolve(api.connect.activate(unserved.model.id)).catch((error) => ({ error: error.message }));
    retrying = false;
    if (overlay.hidden) return;
    if (answer && !answer.error) {
      play('done');
      const model = answer.model || unserved.model;
      unserved = null;
      if (mode === 'first') await Promise.resolve(api.welcome.set({ done: true })).catch(() => null);
      finish({ activated: model });
      return;
    }
    play('close');
    unserved = { ...unservedFrom(answer, unserved.model), drawn: false };
    announce(unservedView(unserved, MODEL_PICKER).title);
    focusAfter = 'unserved-retry';
    renderConnect({ quiet: true });
  }

  /** Turns a refused switch's answer into the card's `unserved`. */
  function unservedFrom(answer, fallbackModel) {
    const body = answer || {};
    return {
      reason: body.error === 'unavailable' ? `missing_${body.missing || 'key'}` : String(body.error || 'failed'),
      model: body.model || fallbackModel || {},
      log: Array.isArray(body.log) ? body.log : [],
    };
  }

  function ready() {
    const current = service();
    if (test.state !== 'ok') return false;
    if (current.kind === 'motoris' && motoris.tab === 'install') return true;
    return Boolean(chosenModel);
  }

  function renderConnect({ quiet = false, keepFocus = null } = {}) {
    const current = service();
    // The focused control's name, unless a handler already set `focusAfter`.
    const active = documentRef.activeElement;
    const had = focusAfter || (active && stage.contains(active) && active.dataset ? active.dataset.focus : null);
    focusAfter = null;
    const card = el('div', 'welcome-card welcome-card--connect');
    if (quiet) card.classList.add('is-quiet');
    card.append(el('span', 'welcome-heading', WELCOME.connectTitle));
    if (unserved) card.append(unservedCard());
    card.append(serviceBlock());
    const section = current.kind === 'api' ? apiSection(current) : motorisSection();
    card.append(section);
    const footer = el('div', 'welcome-footer');
    if (mode === 'first') footer.append(el('span', 'welcome-step', WELCOME.step2));
    footer.append(el('span', 'welcome-spacer'));
    if (mode === 'first' && welcome.working) footer.append(focusKey(button('welcome-square welcome-square--footer', WELCOME.skip, onSkip), 'skip'));
    // With no model served (`blocked`) there is nothing to go back to: no «Cancelar».
    if (mode !== 'blocked') {
      footer.append(focusKey(button('welcome-square welcome-square--footer', mode === 'first' ? WELCOME.back : WELCOME.cancel, onBack), 'back'));
    }
    const go = focusKey(button('welcome-primary welcome-primary--go', mode === 'first' ? WELCOME.start : WELCOME.connect, onGo), 'go');
    go.dataset.ready = String(ready());
    go.setAttribute('aria-disabled', String(busySaving));
    footer.append(go);
    card.append(footer);
    closeMenu({ sound: false });
    stage.replaceChildren(card);
    test.drawn = true;
    if (unserved) unserved.drawn = true;
    if (had && restoreFocus([had, had === 'retry' || had === 'action' ? 'test' : null, 'go'])) {
      // Focus is back on the same control, or on the one that replaced it.
    } else if (keepFocus) {
      const same = [refs.urlInput, refs.keyInput].find((input) => input && input.dataset.focus === keepFocus.dataset.focus);
      if (same) {
        same.focus({ preventScroll: true });
        const end = same.value.length;
        try {
          same.setSelectionRange(end, end);
        } catch {
          /* a password field may refuse; the caret stays where the browser put it */
        }
      }
    } else if (!quiet) {
      const first = current.kind === 'api' ? refs.urlInput || refs.keyInput : refs.first;
      if (first) first.focus({ preventScroll: true });
    }
  }

  /** What «Probar» cannot go on without, as the failure that says so, or null. */
  function missingFailure(current) {
    if (current.kind === 'motoris') {
      return motoris.tab === 'running' && !motorisUrl().trim() ? { kind: 'no_url' } : null;
    }
    const missing = missingInput(current, { url: refs.urlValue, key: refs.keyValue });
    return missing ? { kind: missing === 'url' ? 'no_url' : 'no_key' } : null;
  }

  async function runTest() {
    if (test.state === 'testing' || busySaving || retrying) return;
    const current = service();
    const kind = testKind();
    const missing = missingFailure(current);
    if (missing) {
      // Nothing to send yet: say what is missing, in the card every failure uses, and put
      // the caret in the empty field.
      play('close');
      test = { state: 'fail', answer: missing, fails: test.fails + 1 };
      models = [];
      chosenModel = null;
      announceTest(kind);
      focusAfter = missing.kind === 'no_url' ? 'url' : 'key';
      renderConnect({ quiet: true });
      return;
    }
    if (kind === 'motoris-install' && !motoris.checkout) return pickFolder();
    if (kind === 'motoris-install' && !motoris.config) return pickConfig();
    play('click');
    test = { state: 'testing', answer: null, fails: test.fails };
    models = [];
    chosenModel = null;
    announce(testingMessage(kind));
    if (!focusAfter) focusAfter = 'test';
    renderConnect({ quiet: true });
    const failed = (error) => ({ error: error.message });
    let answer;
    if (kind === 'api') {
      answer = await Promise.resolve(
        api.connect.apiModels({ provider: current.id, key: (refs.keyValue || '').trim(), baseUrl: current.customUrl ? (refs.urlValue || '').trim() : undefined }),
      ).catch(failed);
    } else if (kind === 'motoris-install') {
      answer = await Promise.resolve(api.connect.motorisInstall()).catch(failed);
    } else {
      const key = (refs.keyValue || '').trim();
      answer = await Promise.resolve(api.connect.motorisRunning({ url: motorisUrl().trim(), ...(key ? { key } : {}) })).catch(failed);
    }
    if (overlay.hidden || service() !== current || testKind() !== kind) return;
    if (answer && answer.ok) {
      play('done');
      test = { state: 'ok', answer, fails: test.fails };
      if (kind === 'api') {
        models = (answer.models || []).map((m) => ({ id: m.id, name: m.id, sub: m.label && m.label !== m.id ? m.label : '', label: m.label || m.id }));
      } else if (kind === 'motoris-running') {
        models = (answer.models || []).filter((id) => typeof id === 'string').map((id) => ({ id, name: id, sub: '', label: id }));
        chosenModel = models.length ? models[0].id : null;
      }
    } else {
      play('close');
      // A 409 means the core holds the model for a turn or another switch. Nothing failed.
      const failure =
        answer && answer.failure
          ? answer.failure
          : answer && answer.status === 409
            ? { kind: 'busy' }
            : { kind: 'failed', log: [answer && answer.error ? String(answer.error) : ''] };
      test = { state: 'fail', answer: failure, fails: test.fails + 1 };
    }
    announceTest(kind);
    renderConnect({ quiet: true });
  }

  /** Announces a test's outcome: the green line, or the error card's title and text. */
  function announceTest(kind) {
    if (test.state === 'ok') announce(okLine(kind, test.answer || {}));
    else if (test.state === 'fail') {
      const view = failureCard(kind);
      announce([view.title, view.desc].filter(Boolean).join('. '));
    }
  }

  /** A folder or config from the main process's dialog. A new folder drops the config. */
  async function pickFolder() {
    await pick(() => api.connect.pickMotorisFolder(), (answer) => {
      motoris.checkout = answer.checkout;
      motoris.config = null;
      return 'motoris-config';
    });
  }

  async function pickConfig() {
    await pick(() => api.connect.pickMotorisConfig(), (answer) => {
      motoris.config = answer.config;
      return 'test';
    });
  }

  async function pick(ask, take) {
    if (test.state === 'testing') return;
    play('click');
    const answer = await Promise.resolve(ask()).catch(() => null);
    if (!answer || answer.canceled || answer.error || overlay.hidden) return;
    resetTest();
    focusAfter = take(answer);
    renderConnect({ quiet: true });
  }

  function onBack() {
    if (busySaving) return;
    if (mode === 'first') {
      play('click');
      closeMenu({ sound: false });
      renderName();
      return;
    }
    play('close');
    close();
  }

  async function onSkip() {
    if (busySaving) return;
    play('select');
    busySaving = true;
    await Promise.resolve(api.welcome.set({ done: true })).catch(() => null);
    busySaving = false;
    finish({ skipped: true });
  }

  async function onGo() {
    if (busySaving || retrying) return;
    const current = service();
    if (!ready()) {
      play('close');
      test = { ...test, fails: test.fails + 1 };
      const target = test.state === 'ok' && refs.modelTrigger && testKind() !== 'motoris-install' ? refs.modelTrigger : refs.testField;
      if (target) shake(target);
      return;
    }
    play('select');
    busySaving = true;
    renderConnect({ quiet: true });
    const activate = mode !== 'connect' || !(api.isBusy && api.isBusy());
    let body;
    if (current.kind === 'api') {
      const model = models.find((m) => m.id === chosenModel);
      body = { kind: 'api', provider: current.id, key: (refs.keyValue || '').trim(), baseUrl: current.customUrl ? (refs.urlValue || '').trim() : undefined, model: chosenModel, label: model ? model.label : chosenModel, activate };
    } else if (motoris.tab === 'install') {
      body = { kind: 'motoris', label: test.answer.model, activate };
    } else {
      const key = (refs.keyValue || '').trim();
      body = { kind: 'api', provider: 'motoris', url: motorisUrl().trim(), ...(key ? { key } : {}), model: chosenModel, label: chosenModel, activate };
    }
    const saved = await Promise.resolve(api.connect.save(body)).catch((error) => ({ error: error.message }));
    busySaving = false;
    if (!saved || saved.error || !saved.model) {
      play('close');
      test = { state: 'fail', answer: saveFailure(saved), fails: test.fails + 1 };
      renderConnect({ quiet: true });
      return;
    }
    refs.keyValue = '';
    if (saved.switch && saved.switch.error) {
      if (mode !== 'connect') {
        // No model is served and this one was refused (it failed to start, or the core held
        // it back). Stay on 1e with the core's reason and «Reintentar». Never mark
        // the welcome done or move on to a projects screen that could open nothing. The entry
        // is saved; the form resets for another service.
        play('close');
        unserved = { ...unservedFrom(saved.switch, saved.model), drawn: false };
        resetTest();
        announce(unservedView(unserved, MODEL_PICKER).title);
        focusAfter = 'unserved-retry';
        renderConnect({ quiet: true });
        return;
      }
      // A project is open and its model still serves. The new model is in the picker, and
      // the picker shows the refusal in the thread.
      api.onSwitchRefused?.(saved.switch);
    }
    finishSaved(saved);
  }

  async function finishSaved(saved) {
    if (mode !== 'connect') await Promise.resolve(api.welcome.set({ done: true })).catch(() => null);
    finish({ saved });
  }

  // -- showing and closing ----------------------------------------------------------------------

  function hide() {
    overlay.hidden = true;
    overlay.classList.remove('is-closing');
    stage.replaceChildren();
    announce('');
    refs = {};
    documentRef.removeEventListener('keydown', onKeydown, true);
    documentRef.removeEventListener('mousedown', outside, true);
  }

  function close() {
    if (overlay.hidden || overlay.classList.contains('is-closing')) return;
    closeMenu({ sound: false });
    giveTitleBack();
    overlay.classList.add('is-closing');
    clearTimeout(closeTimer);
    closeTimer = setTimeout(hide, CLOSE_MS);
  }

  function finish(result) {
    close();
    api.onDone(result);
  }

  /**
   * `mode`: `first` (the welcome: 1d, then 1e, «Paso n de 2»), `connect` (1e alone, from
   * «Conectar otro servicio…») or `blocked` (1e alone and not dismissible, because the core
   * serves no model; `state.unserved` says why). `state`: `GET /settings/welcome`. `step`:
   * `name` or `connect`; the welcome starts at the name unless one is stored.
   */
  async function show({ mode: chosen = 'first', state = {}, step = null } = {}) {
    clearTimeout(closeTimer);
    announce('');
    mode = chosen;
    welcome = { ...state };
    refs = {};
    busySaving = false;
    retrying = false;
    unserved = state && state.unserved && state.unserved.model ? { ...state.unserved, drawn: false } : null;
    motoris = freshMotoris();
    test = { state: 'idle', answer: null, fails: 0 };
    models = [];
    chosenModel = null;
    overlay.classList.remove('is-closing');
    overlay.classList.toggle('welcome-overlay--connect', mode !== 'first');
    overlay.setAttribute('aria-label', mode === 'first' ? WELCOME.hello : WELCOME.connectTitle);
    overlay.hidden = false;
    if (mode === 'first') takeTitle();
    documentRef.addEventListener('keydown', onKeydown, true);
    documentRef.addEventListener('mousedown', outside, true);
    // Services come from the core. Step 1 (1d) does not need them, so it draws at once and
    // step 2 waits for them.
    servicesReady = Promise.resolve(api.connect.providers())
      .catch(() => null)
      .then((providers) => {
        services = serviceList(providers && providers.providers);
        serviceId = services[0] ? services[0].id : 'motoris';
      });
    const first = step || (mode === 'first' && !welcome.name ? 'name' : 'connect');
    if (first === 'name') renderName();
    else {
      await servicesReady;
      renderConnect();
      if (unserved) announce(unservedView(unserved, MODEL_PICKER).title);
    }
  }

  return { show, close, isOpen: () => !overlay.hidden, el: overlay };
}

export { createWelcome };
