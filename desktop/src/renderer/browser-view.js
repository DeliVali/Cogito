'use strict';

/**
 * The browser tabs' panes (design 2026-11-05): an address bar with back, forward, reload or
 * stop and the address, and under it an empty page area. The page itself is a view the main
 * process draws over that area, so the window only says where the area is.
 *
 * - While a tab is shown, its area's rect (CSS px, from the window's top left) goes to main
 *   with `api.bounds(id, rect)`: when shown, when the area changes size (debounced), and
 *   when the window resizes (a zoom change does too).
 * - `api.bounds(null, null)` hides every page: when the pane is hidden or another tab is
 *   shown (`hide`), and while a menu or dialog is open over the window (`COVERS`), since the
 *   page paints above anything the window draws.
 * - The address bar sends what was typed; main makes it an address or answers `{error}`
 *   with a code (`navigateError`), shown under the bar until the next edit. What the person
 *   is typing is kept when a new address arrives.
 * - A tab main lists with `failed: {code, description}` shows why its page did not load, under
 *   the bar (the page itself is main's view), until main lists it without one.
 * - On creation and in `clear` every page is hidden once, whatever was sent before: after the
 *   window reloads, main may still be drawing a page this window knows nothing of.
 *
 * `api` is `window.cogito.browser`. `windowRef` gives the timers and observers (the tests'
 * fakes).
 */

import { BROWSER } from './strings.js';

/** How long a resize settles before the new rect is sent. */
const RESIZE_MS = 60;
/** What draws over the pane: the menus and popovers placed on the body, and any open
 * dialog. */
const COVERS = [
  'body > [role="menu"]',
  '[aria-modal="true"]:not([hidden])',
  'body > .update-menu',
  'body > .update-card',
  'body > .update-restarting',
].join(', ');

function el(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
}

/** What a tab is called in the strip: its title, else its host, else «Nueva pestaña». */
function browserTitle(tab) {
  return (tab && (tab.title || hostOf(tab.url))) || BROWSER.newTab;
}

/** The address bar's text for a tab: empty for a blank page. */
function addressOf(url) {
  return !url || url === 'about:blank' ? '' : String(url);
}

/** The Spanish for main's address-bar `{error}` code. */
function navigateError(code) {
  return (typeof code === 'string' && Object.hasOwn(BROWSER.navigateErrors, code) && BROWSER.navigateErrors[code]) || BROWSER.navigateFailed;
}

/** The Spanish for a tab's `failed: {code, description}`, or '' when it loaded. */
function loadFailure(failed) {
  if (!failed || typeof failed !== 'object') return '';
  const code = Number(failed.code);
  let reason = '';
  if (Number.isInteger(code) && Object.hasOwn(BROWSER.loadReasons, String(code))) reason = BROWSER.loadReasons[String(code)];
  else if (code <= -200 && code > -300) reason = BROWSER.certificateInvalid;
  else if (typeof failed.description === 'string') reason = failed.description.trim().slice(0, 200);
  return BROWSER.loadFailed(reason);
}

/** A node's rect in whole CSS px, or null when it has no area. */
function rectOf(node) {
  const box = node.getBoundingClientRect();
  const rect = { x: Math.round(box.left), y: Math.round(box.top), width: Math.round(box.width), height: Math.round(box.height) };
  return rect.width > 0 && rect.height > 0 ? rect : null;
}

function createBrowserView({ api, play = () => {}, documentRef = document, windowRef = globalThis }) {
  const doc = documentRef;
  const win = windowRef;
  /** id -> `{id, tab, root, back, forward, reload, input, error, failed, area, dirty}`. */
  const pages = new Map();
  /** The tab on screen, or null. */
  let shown = null;
  let covered = false;
  /** The last bounds sent, so the same rect is not sent again. */
  let sent = null;
  let timer = null;
  let sizeObserver = null;
  let coverObservers = [];

  function send(id, rect) {
    const key = id && rect ? `${id} ${rect.x} ${rect.y} ${rect.width} ${rect.height}` : '';
    if (key === sent) return;
    sent = key;
    Promise.resolve(id && rect ? api.bounds(id, rect) : api.bounds(null, null)).catch(() => {});
  }

  /** Sends where the shown tab's page goes now, or null. */
  function place() {
    win.clearTimeout(timer);
    timer = null;
    const page = shown ? pages.get(shown) : null;
    const rect = page && !covered && page.area.isConnected !== false ? rectOf(page.area) : null;
    send(rect ? shown : null, rect);
  }

  function schedule() {
    win.clearTimeout(timer);
    timer = win.setTimeout(place, RESIZE_MS);
  }

  function isCovered() {
    return Boolean(doc.querySelector && doc.querySelector(COVERS));
  }

  /** A menu or dialog opened or closed: the page goes away or comes back at once. */
  function recheck() {
    if (!shown) return;
    covered = isCovered();
    place();
  }

  function watch(page) {
    unwatch();
    if (typeof win.ResizeObserver === 'function') {
      sizeObserver = new win.ResizeObserver(schedule);
      sizeObserver.observe(page.area);
    }
    // Menus and popovers are added to the body and removed; dialogs are shown and hidden.
    if (typeof win.MutationObserver === 'function' && doc.body) {
      const children = new win.MutationObserver(recheck);
      children.observe(doc.body, { childList: true });
      const shownState = new win.MutationObserver(recheck);
      shownState.observe(doc.body, { subtree: true, attributes: true, attributeFilter: ['hidden', 'aria-modal'] });
      coverObservers = [children, shownState];
    }
    if (win.addEventListener) win.addEventListener('resize', schedule);
  }

  function unwatch() {
    if (sizeObserver) sizeObserver.disconnect();
    sizeObserver = null;
    for (const observer of coverObservers) observer.disconnect();
    coverObservers = [];
    if (win.removeEventListener) win.removeEventListener('resize', schedule);
  }

  function showError(page, text) {
    page.error.textContent = text;
    page.error.hidden = false;
  }

  function clearError(page) {
    if (page.error.hidden) return;
    page.error.hidden = true;
    page.error.textContent = '';
  }

  async function submit(page) {
    const text = page.input.value.trim();
    if (!text) return;
    play('click');
    clearError(page);
    const answer = await Promise.resolve(api.navigate(page.id, text)).catch(() => ({ error: 'failed' }));
    if (pages.get(page.id) !== page) return;
    if (answer && answer.error) {
      showError(page, navigateError(answer.error));
      return;
    }
    page.dirty = false;
    if (page.input.blur) page.input.blur();
  }

  function navButton(page, className, label, glyph, run) {
    const button = el(doc, 'button', `browser-nav ${className}`, glyph);
    button.type = 'button';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.addEventListener('click', () => {
      if (button.disabled) return;
      play('click');
      Promise.resolve(run(page)).catch(() => {});
    });
    return button;
  }

  function build(id) {
    const root = el(doc, 'div', 'browser');
    const bar = el(doc, 'div', 'viewer-bar browser-bar');
    const page = { id, tab: { id }, root, dirty: false };
    page.back = navButton(page, 'browser-back', BROWSER.back, '←', () => api.back(id));
    page.forward = navButton(page, 'browser-forward', BROWSER.forward, '→', () => api.forward(id));
    page.reload = navButton(page, 'browser-reload', BROWSER.reload, '↻', () =>
      page.tab.loading ? api.stop(id) : api.reload(id),
    );
    const input = el(doc, 'input', 'browser-address');
    input.type = 'text';
    input.spellcheck = false;
    input.placeholder = BROWSER.placeholder;
    input.setAttribute('aria-label', BROWSER.address);
    input.addEventListener('input', () => {
      page.dirty = true;
      clearError(page);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit(page);
      } else if (event.key === 'Escape') {
        // Back to the tab's address, as a browser's bar does.
        event.preventDefault();
        page.dirty = false;
        input.value = addressOf(page.tab.url);
        clearError(page);
      }
    });
    page.input = input;
    page.error = el(doc, 'div', 'browser-error');
    page.error.hidden = true;
    page.error.setAttribute('role', 'alert');
    page.failed = el(doc, 'div', 'browser-failed');
    page.failed.hidden = true;
    page.failed.setAttribute('role', 'status');
    page.area = el(doc, 'div', 'browser-page');
    bar.append(page.back, page.forward, page.reload, input);
    root.append(bar, page.error, page.failed, page.area);
    return page;
  }

  function draw(page) {
    const tab = page.tab;
    page.back.disabled = !tab.canBack;
    page.forward.disabled = !tab.canForward;
    const label = tab.loading ? BROWSER.stop : BROWSER.reload;
    page.reload.textContent = tab.loading ? '✕' : '↻';
    page.reload.title = label;
    page.reload.setAttribute('aria-label', label);
    page.root.classList.toggle('is-loading', Boolean(tab.loading));
    if (!page.dirty) page.input.value = addressOf(tab.url);
    const failure = loadFailure(tab.failed);
    if (page.failed.textContent !== failure) page.failed.textContent = failure;
    page.failed.hidden = !failure;
  }

  /** The pane for a tab, made once. */
  function ensure(id, tab = null) {
    let page = pages.get(id);
    if (!page) {
      page = build(id);
      pages.set(id, page);
    }
    if (tab) update(id, tab);
    return page;
  }

  /** New state for a tab: address, loading, history. */
  function update(id, tab) {
    const page = pages.get(id);
    if (!page) return;
    page.tab = { ...page.tab, ...tab, id };
    draw(page);
  }

  /** The tab is on screen (its pane is in the viewer): its page goes over the area. `focus`:
   * a blank tab the person opened takes the keyboard in its address bar. */
  function show(id, { focus = false } = {}) {
    const page = pages.get(id);
    if (!page) return;
    shown = id;
    watch(page);
    covered = isCovered();
    place();
    if (focus && !addressOf(page.tab.url)) page.input.focus();
  }

  /** No browser tab on screen: every page hides. */
  function hide() {
    shown = null;
    unwatch();
    win.clearTimeout(timer);
    timer = null;
    send(null, null);
  }

  function dispose(id) {
    const page = pages.get(id);
    if (!page) return;
    if (shown === id) hide();
    page.root.remove();
    pages.delete(id);
  }

  // Nothing was sent yet, so this goes out: every page hidden.
  send(null, null);

  return {
    ensure,
    update,
    element: (id) => (pages.has(id) ? pages.get(id).root : null),
    show,
    hide,
    recheck,
    dispose,
    clear() {
      // Sent again even when the last bounds already hid every page.
      sent = null;
      for (const id of [...pages.keys()]) dispose(id);
      send(null, null);
    },
    has: (id) => pages.has(id),
    shown: () => shown,
  };
}

export { createBrowserView, browserTitle, addressOf, hostOf, navigateError, loadFailure, COVERS, RESIZE_MS };
