'use strict';

/**
 * Browser tabs beside «Hilo» (design 2026-11-05): the pages main keeps for this project,
 * listed in the viewer's tab strip (`viewer.js`) and drawn by `browser-view.js`.
 *
 * - `apply(list)` takes main's `browser:tabs`, the whole list each time: a tab not seen
 *   before joins the strip without being shown (the model opened it; the person keeps what
 *   they are looking at), a known one is updated, and one no longer listed goes.
 * - `refresh()` asks main for the list (on start and after a project switch).
 * - `openNew(url?)` is the person asking for a tab: it is shown, and a blank one takes the
 *   keyboard in its address bar.
 * - `close(id)` is the tab's ✕.
 * - `replayShortcut(shortcut)` is an app shortcut pressed while a page had the keyboard: main
 *   keeps it from the page and sends it here, and it reaches the document as the keydown it
 *   was, so the same handlers answer it.
 *
 * `api` is `window.cogito.browser`, or null where the window has no browser. `viewer` is the
 * viewer's browser half; `view` the panes.
 */

const FIELDS = ['title', 'url', 'loading', 'canBack', 'canForward', 'favicon'];

function pick(tab) {
  const out = { id: tab.id };
  for (const key of FIELDS) if (key in tab) out[key] = tab[key];
  // Main lists `failed` only until the next navigation starts: absent means it loaded.
  out.failed = tab.failed && typeof tab.failed === 'object' ? tab.failed : null;
  return out;
}

/** Main's `browser:shortcut`, `{key, code, ctrl, meta, shift, alt}`, as a keydown on the
 * document. Returns the event, or null for a shape that names no key. */
function replayShortcut(shortcut, { documentRef = globalThis.document, KeyboardEventRef = globalThis.KeyboardEvent } = {}) {
  if (!shortcut || typeof shortcut.key !== 'string' || !shortcut.key || shortcut.key.length > 32) return null;
  if (!documentRef || typeof KeyboardEventRef !== 'function') return null;
  const event = new KeyboardEventRef('keydown', {
    key: shortcut.key,
    code: typeof shortcut.code === 'string' ? shortcut.code : '',
    ctrlKey: shortcut.ctrl === true,
    metaKey: shortcut.meta === true,
    shiftKey: shortcut.shift === true,
    altKey: shortcut.alt === true,
    bubbles: true,
    cancelable: true,
  });
  documentRef.dispatchEvent(event);
  return event;
}

function createBrowsers({ api, viewer, view }) {
  /** id -> the tab as main last listed it. */
  const tabs = new Map();
  /** Tabs closed here that main may still list once before the close reaches it. */
  const closing = new Set();
  /** A project switch makes answers to the last project's requests stale. */
  let generation = 0;
  /** The tab the person opened, shown as soon as main lists it. */
  let wanted = null;

  function put(raw) {
    const entry = pick(raw);
    const known = tabs.has(raw.id);
    tabs.set(raw.id, entry);
    if (!known) {
      view.ensure(raw.id, entry);
      viewer.addBrowser(entry, { focus: false });
    } else {
      view.update(raw.id, entry);
      viewer.updateBrowser(raw.id, entry);
    }
    if (wanted === raw.id) {
      wanted = null;
      viewer.showBrowser(raw.id, { focus: true });
    }
    return entry;
  }

  function drop(id) {
    tabs.delete(id);
    viewer.removeBrowser(id);
    view.dispose(id);
  }

  function apply(list) {
    if (!Array.isArray(list)) return;
    const listed = new Set();
    for (const raw of list) {
      if (!raw || typeof raw.id !== 'string' || !raw.id) continue;
      listed.add(raw.id);
      if (!closing.has(raw.id)) put(raw);
    }
    for (const id of [...closing]) if (!listed.has(id)) closing.delete(id);
    for (const id of [...tabs.keys()]) if (!listed.has(id)) drop(id);
  }

  async function refresh() {
    if (!api) return;
    const asked = generation;
    const list = await Promise.resolve(api.list()).catch(() => null);
    if (asked === generation) apply(list);
  }

  /** A new tab the person asked for, shown. Returns its id, or null when none opened. */
  async function openNew(url = null) {
    if (!api) return null;
    const asked = generation;
    const answer = await Promise.resolve(api.open(url || undefined)).catch(() => null);
    if (asked !== generation) return null;
    const id = typeof answer === 'string' ? answer : answer && typeof answer.id === 'string' ? answer.id : null;
    if (!id) return null;
    if (answer && typeof answer === 'object') {
      wanted = id;
      put(answer);
    } else if (tabs.has(id)) viewer.showBrowser(id, { focus: true });
    else wanted = id;
    return id;
  }

  function close(id) {
    if (!tabs.has(id)) return;
    closing.add(id);
    drop(id);
    if (!api) return;
    Promise.resolve(api.close(id)).catch(() => {
      // Main kept it: list it again.
      closing.delete(id);
      refresh();
    });
  }

  /** Another project: its tabs go, and nothing is known until `refresh`. */
  function clear() {
    generation += 1;
    wanted = null;
    closing.clear();
    for (const id of [...tabs.keys()]) drop(id);
    view.clear();
  }

  return {
    apply,
    refresh,
    openNew,
    close,
    clear,
    tab: (id) => tabs.get(id) || null,
  };
}

export { createBrowsers, replayShortcut };
