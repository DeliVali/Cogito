'use strict';

/**
 * The terminal tabs' screens (design 2026-11-01): one xterm.js 6 per tab, sized to the
 * viewer's pane with the fit addon. What the shell prints arrives as `terminal_data`
 * (`write`); keys go to the core as typed (`api.input`), one request at a time so they keep
 * their order; a new size goes as `api.resize`.
 *
 * xterm loads with the first tab shown (`load`), not with the page. Output that arrives
 * before a tab is first shown waits for it. A tab the window did not see open (listed when
 * the window opened), or one whose waiting output outgrew `MAX_PENDING`, shows the core's
 * kept output once, from `api.get`, before anything newer.
 *
 * Every `terminal_data` carries `end`, the tab's output so far in UTF-16 code units (a
 * JavaScript string's length), and `api.get` says where its kept output ends. After the
 * kept output, a chunk that ends at or before it is dropped and one that straddles it is
 * cut, so nothing shows twice.
 *
 * Keys: Ctrl+Shift+C copies the selection and Ctrl+Shift+V pastes (Cmd+C and Cmd+V on
 * macOS, which has no Edit menu here); every other key belongs to the shell. No key typed in
 * a terminal reaches the page's own shortcuts (Ctrl+O, Esc…). Typed text goes in pieces of
 * at most `MAX_INPUT` characters, the core's limit; a piece it refuses shows a note.
 *
 * A link in the output (OSC 8) opens in the system browser through main, http(s) only.
 *
 * `api` is `window.cogito.terminal`.
 */

import { openWithinCsp } from './xterm-csp.js';
import { TERMINAL } from './strings.js';

/** Waiting output past this is dropped and read back from the core instead (it keeps
 * 256 KiB). */
const MAX_PENDING = 256 * 1024;
const FONT_SIZE = 13;
const FONT_FAMILY = "'IBM Plex Mono', ui-monospace, monospace";
/** A 13 px IBM Plex Mono cell, for a tab's size before its screen exists. */
const CELL = { width: 7.8, height: 17 };
const RESIZE_MS = 80;
/** The core's limits: one input request, and a size on each side. */
const MAX_INPUT = 65536;
const MIN_SIZE = 2;
const MAX_SIZE = 1000;
/** How long a note stays on a tab. */
const NOTE_MS = 4000;

/** The light theme, as the viewer is: what a screen uses when explorer.css's `--fx-term-*`
 * variables cannot be read. `minimumContrastRatio` keeps white or yellow text readable. */
const THEME = {
  background: '#fbfcfe',
  foreground: '#26304a',
  cursor: '#2c4a80',
  cursorAccent: '#fbfcfe',
  selectionBackground: 'rgba(44, 74, 128, 0.25)',
  black: '#2b3350',
  red: '#b3261e',
  green: '#1f7a3a',
  yellow: '#8a6d00',
  blue: '#1d4f9c',
  magenta: '#8a3a8f',
  cyan: '#0f6f7a',
  white: '#c4ccda',
  brightBlack: '#6a7590',
  brightRed: '#d0453b',
  brightGreen: '#2e9a50',
  brightYellow: '#a88a12',
  brightBlue: '#2f6cc9',
  brightMagenta: '#a656ab',
  brightCyan: '#1f8f9b',
  brightWhite: '#eef2f8',
};

/** xterm theme key -> the explorer.css variable that holds it (light and dark). */
const THEME_VARS = {
  background: '--fx-term-background',
  foreground: '--fx-term-foreground',
  cursor: '--fx-term-cursor',
  cursorAccent: '--fx-term-background',
  selectionBackground: '--fx-term-selection',
  black: '--fx-term-black',
  red: '--fx-term-red',
  green: '--fx-term-green',
  yellow: '--fx-term-yellow',
  blue: '--fx-term-blue',
  magenta: '--fx-term-magenta',
  cyan: '--fx-term-cyan',
  white: '--fx-term-white',
  brightBlack: '--fx-term-bright-black',
  brightRed: '--fx-term-bright-red',
  brightGreen: '--fx-term-bright-green',
  brightYellow: '--fx-term-bright-yellow',
  brightBlue: '--fx-term-bright-blue',
  brightMagenta: '--fx-term-bright-magenta',
  brightCyan: '--fx-term-bright-cyan',
  brightWhite: '--fx-term-bright-white',
};

/** The theme the page's CSS gives now. xterm paints on a canvas, so it needs the values
 * themselves, not `var()`; a variable that is not set keeps `THEME`'s value. */
function themeFrom(doc) {
  const view = doc && doc.defaultView;
  if (!view || typeof view.getComputedStyle !== 'function' || !doc.documentElement) return { ...THEME };
  const style = view.getComputedStyle(doc.documentElement);
  const theme = {};
  for (const [key, name] of Object.entries(THEME_VARS)) {
    const value = String(style.getPropertyValue(name) || '').trim();
    theme[key] = value || THEME[key];
  }
  return theme;
}

function loadXterm() {
  return Promise.all([import('./vendor/xterm/xterm.mjs'), import('./vendor/xterm/addon-fit.mjs')]).then(
    ([xterm, fit]) => ({ Terminal: xterm.Terminal, FitAddon: fit.FitAddon }),
  );
}

/** What a terminal's own key handler decides for a keydown: 'copy', 'paste' (left to the
 * browser, whose paste event xterm reads), 'paste-read' (macOS: read the clipboard and
 * paste it, since no menu makes Cmd+V paste) or null (the shell's). */
function terminalKey(event, platform = '') {
  if (event.type !== 'keydown') return null;
  const key = String(event.key).toLowerCase();
  if (platform === 'darwin' && event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey) {
    if (key === 'c') return 'copy';
    if (key === 'v') return 'paste-read';
    return null;
  }
  if (!event.ctrlKey || !event.shiftKey || event.altKey || event.metaKey) return null;
  if (key === 'c') return 'copy';
  if (key === 'v') return 'paste';
  return null;
}

/** Columns and rows that fit `width` × `height` pixels. */
function sizeFor(width, height) {
  const cols = Math.max(20, Math.min(MAX_SIZE, Math.floor((width - 16) / CELL.width)));
  const rows = Math.max(5, Math.min(MAX_SIZE, Math.floor((height - 12) / CELL.height)));
  return { cols, rows };
}

const clampSize = (value) => Math.max(MIN_SIZE, Math.min(MAX_SIZE, Math.floor(Number(value) || 0)));

/** `text` in pieces of at most `size` UTF-16 units, never splitting a surrogate pair. */
function pieces(text, size = MAX_INPUT) {
  const out = [];
  let at = 0;
  while (at < text.length) {
    let cut = Math.min(text.length, at + size);
    const last = text.charCodeAt(cut - 1);
    if (cut < text.length && last >= 0xd800 && last <= 0xdbff) cut -= 1;
    out.push(text.slice(at, cut));
    at = cut;
  }
  return out;
}

/** The part of a chunk ending at `end` that lies past offset `shown`: all of it, its tail,
 * or '' when the screen already has it. Without offsets, all of it. Offsets count UTF-16
 * code units, as the core does (a JavaScript string's length). A cut that would fall inside
 * a surrogate pair steps back one unit, so the pair is written whole. */
function unseen(data, end, shown) {
  if (typeof end !== 'number' || typeof shown !== 'number') return data;
  if (end <= shown) return '';
  const fresh = end - shown;
  if (fresh >= data.length) return data;
  let cut = data.length - fresh;
  const first = data.charCodeAt(cut);
  if (first >= 0xdc00 && first <= 0xdfff) cut -= 1;
  return data.slice(cut);
}

/** Whether a field the person types in has the keyboard, outside `pane`. */
function typingElsewhere(doc, pane) {
  const active = doc.activeElement;
  if (!active || active === doc.body || active === doc.documentElement) return false;
  if (pane && typeof pane.contains === 'function' && pane.contains(active)) return false;
  if (active.isContentEditable === true) return true;
  const tag = String(active.tagName || '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag !== 'INPUT') return false;
  const type = String(active.type || 'text').toLowerCase();
  return !['button', 'submit', 'reset', 'checkbox', 'radio', 'range', 'color', 'file', 'image'].includes(type);
}

const isWebLink = (text) => {
  try {
    const url = new URL(String(text));
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch {
    return false;
  }
};

function createTerminalView({ api, documentRef = document, load = loadXterm, platform = '' }) {
  const doc = documentRef;
  /** id -> screen. */
  const screens = new Map();
  let lib = null;
  let loading = null;
  let fontsReady = null;

  // A theme change (Diseño, or the system's own when the setting follows it) repaints every
  // open screen with the new colours.
  const scheme = doc.defaultView && typeof doc.defaultView.matchMedia === 'function'
    ? doc.defaultView.matchMedia('(prefers-color-scheme: dark)')
    : null;
  if (scheme && typeof scheme.addEventListener === 'function') {
    scheme.addEventListener('change', () => {
      const theme = themeFrom(doc);
      for (const screen of screens.values()) {
        if (screen.term && screen.term.options) screen.term.options.theme = theme;
      }
    });
  }

  function library() {
    if (lib) return Promise.resolve(lib);
    if (!loading) {
      loading = Promise.resolve()
        .then(load)
        .then(
          (loaded) => {
            lib = loaded;
            return lib;
          },
          (error) => {
            // The next show tries again.
            loading = null;
            throw error;
          },
        );
    }
    return loading;
  }

  /** The cell size is measured when xterm opens: the bundled font must be in by then. */
  function fonts() {
    if (!fontsReady) {
      fontsReady = doc.fonts && doc.fonts.load
        ? doc.fonts.load(`${FONT_SIZE}px ${FONT_FAMILY}`).catch(() => null)
        : Promise.resolve(null);
    }
    return fontsReady;
  }

  function ensure(id, { replay = false } = {}) {
    let screen = screens.get(id);
    if (screen) return screen;
    const host = doc.createElement('div');
    host.className = 'terminal-screen';
    // Keys typed here are the shell's: none reaches the page's own shortcuts.
    host.addEventListener('keydown', (event) => event.stopPropagation());
    screen = {
      id,
      host,
      term: null,
      fit: null,
      mounting: null,
      // Waiting output: `{data, end}` in arrival order.
      pending: [],
      pendingSize: 0,
      replay,
      replaying: null, // the replay running, as a promise
      // The core's offset of the last output on screen, once known.
      shown: null,
      note: null,
      exited: false,
      input: { buffer: '', sending: false },
      resizeTimer: 0,
      observer: null,
      disposed: false,
    };
    screens.set(id, screen);
    return screen;
  }

  /** The part of a chunk the screen does not show yet; advances the screen's offset. */
  function fresh(screen, data, end) {
    const part = unseen(data, end, screen.shown);
    if (typeof end === 'number') screen.shown = Math.max(screen.shown ?? end, end);
    return part;
  }

  /** Waiting chunks on screen, past what it already shows, in one write. */
  function putAll(screen, queued) {
    const text = queued.map(({ data, end }) => fresh(screen, data, end)).join('');
    if (text) screen.term.write(text);
  }

  function write(id, data, end) {
    const screen = screens.get(id);
    if (!screen || typeof data !== 'string' || !data) return;
    const at = Number.isInteger(end) ? end : undefined;
    if (screen.term && !screen.replaying && !screen.replay && !screen.pending.length) {
      putAll(screen, [{ data, end: at }]);
      return;
    }
    screen.pending.push({ data, end: at });
    screen.pendingSize += data.length;
    if (screen.pendingSize > MAX_PENDING) {
      // Too much to hold: the core's kept output has it all.
      screen.pending = [];
      screen.pendingSize = 0;
      screen.replay = true;
    }
  }

  /** What waited, in order. Never while a replay runs: it writes them after the kept output. */
  function flush(screen) {
    if (screen.replaying) return;
    const queued = screen.pending;
    screen.pending = [];
    screen.pendingSize = 0;
    putAll(screen, queued);
  }

  /** A short line over the tab's screen, gone after a few seconds (`sticky`: until the
   * screen opens). */
  function note(screen, text, { sticky = false } = {}) {
    if (screen.note) screen.note.remove();
    const node = doc.createElement('div');
    node.className = 'terminal-note';
    node.setAttribute('role', 'status');
    node.textContent = text;
    screen.host.append(node);
    screen.note = node;
    if (!sticky) {
      setTimeout(() => {
        if (screen.note === node) {
          node.remove();
          screen.note = null;
        }
      }, NOTE_MS);
    }
  }

  /** Keys to the shell, in order: one request at a time, what was typed meanwhile next. */
  function send(screen, data) {
    if (screen.exited || screen.disposed) return;
    screen.input.buffer += data;
    pump(screen);
  }

  async function pump(screen) {
    if (screen.input.sending) return;
    screen.input.sending = true;
    while (screen.input.buffer && !screen.disposed) {
      const batch = screen.input.buffer;
      screen.input.buffer = '';
      for (const piece of pieces(batch)) {
        let answer;
        try {
          answer = await api.input(screen.id, piece);
        } catch {
          answer = { error: 'failed' };
        }
        if (answer && answer.error) {
          // The rest of this batch would reach the shell with a hole in it.
          if (!screen.exited && !screen.disposed) note(screen, TERMINAL.inputRefused);
          break;
        }
      }
    }
    screen.input.sending = false;
  }

  function scheduleResize(screen) {
    clearTimeout(screen.resizeTimer);
    screen.resizeTimer = setTimeout(() => {
      if (!screen.term || screen.disposed || screen.exited) return;
      Promise.resolve(api.resize(screen.id, clampSize(screen.term.cols), clampSize(screen.term.rows))).catch(() => {});
    }, RESIZE_MS);
  }

  function paste(screen) {
    try {
      Promise.resolve(navigator.clipboard.readText())
        .then((text) => {
          if (text && screen.term && !screen.disposed) screen.term.paste(text);
        })
        .catch(() => {});
    } catch {
      // No clipboard here.
    }
  }

  function openLink(text) {
    if (!isWebLink(text) || !api.openLink) return;
    Promise.resolve(api.openLink(String(text))).catch(() => {});
  }

  function refit(screen) {
    if (!screen.fit || !screen.host.isConnected || !screen.host.clientWidth || !screen.host.clientHeight) return;
    try {
      screen.fit.fit();
    } catch {
      // Not measurable yet (hidden); the next resize fits it.
    }
  }

  function copy(screen) {
    const text = screen.term.getSelection();
    if (!text) return;
    const fallback = () => doc.execCommand && doc.execCommand('copy');
    try {
      Promise.resolve(navigator.clipboard.writeText(text)).catch(fallback);
    } catch {
      fallback();
    }
  }

  async function mount(screen) {
    if (screen.term) return;
    if (!screen.mounting) {
      screen.mounting = (async () => {
        const { Terminal, FitAddon } = await library();
        await fonts();
        if (screen.disposed) return;
        const term = new Terminal({
          fontFamily: FONT_FAMILY,
          fontSize: FONT_SIZE,
          fontWeightBold: '600',
          lineHeight: 1.1,
          cursorBlink: true,
          scrollback: 5000,
          minimumContrastRatio: 4.5,
          allowProposedApi: false,
          theme: themeFrom(doc),
          disableStdin: screen.exited,
          // OSC 8 links: the system browser, never a window of the app's own.
          linkHandler: {
            allowNonHttpProtocols: false,
            activate: (event, text) => {
              if (event && event.preventDefault) event.preventDefault();
              openLink(text);
            },
            hover: (_event, text) => {
              screen.host.title = isWebLink(text) ? String(text) : '';
            },
            leave: () => {
              screen.host.title = '';
            },
          },
        });
        const fit = new FitAddon();
        term.loadAddon(fit);
        openWithinCsp(term, screen.host, doc);
        term.attachCustomKeyEventHandler((event) => {
          const action = terminalKey(event, platform);
          if (action === 'copy') {
            event.preventDefault();
            copy(screen);
            return false;
          }
          if (action === 'paste-read') {
            event.preventDefault();
            paste(screen);
            return false;
          }
          // Paste: the browser's own, which xterm reads from the paste event.
          return action !== 'paste';
        });
        term.onData((data) => send(screen, data));
        term.onResize(() => scheduleResize(screen));
        screen.term = term;
        screen.fit = fit;
        if (typeof ResizeObserver === 'function') {
          screen.observer = new ResizeObserver(() => refit(screen));
          screen.observer.observe(screen.host);
        }
      })().catch((error) => {
        // Tried again on the next show.
        screen.mounting = null;
        throw error;
      });
    }
    await screen.mounting;
  }

  /** Puts the core's kept output on screen, then what arrived meanwhile, each part once. */
  async function replayOnce(screen) {
    screen.replay = false;
    // Without offsets, what waited before the read is in the kept output; what arrives
    // during it may not be.
    const before = screen.pending.length;
    const answer = await Promise.resolve(api.get(screen.id)).catch(() => null);
    if (screen.disposed) return;
    const kept = answer && typeof answer.scrollback === 'string' ? answer.scrollback : '';
    const end = answer && Number.isInteger(answer.end) ? answer.end : null;
    if (kept) screen.term.write(kept);
    let queued = screen.pending;
    if (end !== null) screen.shown = end;
    else if (answer) queued = queued.slice(before);
    screen.pending = [];
    screen.pendingSize = 0;
    putAll(screen, queued);
  }

  /** One replay at a time; a show during it waits for it. Runs again if the queue outgrew
   * itself meanwhile. */
  function replay(screen) {
    if (!screen.replaying) {
      screen.replaying = (async () => {
        try {
          while (screen.replay && !screen.disposed) await replayOnce(screen);
        } finally {
          screen.replaying = null;
        }
      })();
    }
    return screen.replaying;
  }

  /** The tab is on screen (its host is in the pane): open xterm if needed, fit, focus. The
   * terminal takes the keyboard only if the person is not typing in another field and it is
   * still the tab on screen. */
  async function show(id, { focus = true } = {}) {
    const screen = screens.get(id);
    if (!screen) return;
    try {
      await mount(screen);
    } catch {
      if (!screen.disposed) note(screen, TERMINAL.loadFailed, { sticky: true });
      return;
    }
    if (!screen.term || screen.disposed) return;
    if (screen.note) {
      screen.note.remove();
      screen.note = null;
    }
    if (screen.replay || screen.replaying) await replay(screen);
    else flush(screen);
    if (screen.disposed) return;
    refit(screen);
    scheduleResize(screen);
    if (!focus || screen.exited || screen.host.isConnected === false) return;
    if (typingElsewhere(doc, screen.host.parentNode)) return;
    screen.term.focus();
  }

  /** The shell ended: its text stays, typing stops. */
  function exited(id) {
    const screen = screens.get(id);
    if (!screen) return;
    screen.exited = true;
    if (screen.term) {
      screen.term.options.disableStdin = true;
      screen.term.options.cursorBlink = false;
    }
  }

  function dispose(id) {
    const screen = screens.get(id);
    if (!screen) return;
    screen.disposed = true;
    clearTimeout(screen.resizeTimer);
    if (screen.observer) screen.observer.disconnect();
    if (screen.term) screen.term.dispose();
    screen.host.remove();
    screens.delete(id);
  }

  /** A size for a tab about to open, from the pane it will fill. */
  function estimateSize(width, height) {
    if (!width || !height) return null;
    return sizeFor(width, height);
  }

  /** A short note over a tab's screen, from outside (`terminals.js`). */
  function notify(id, text) {
    const screen = screens.get(id);
    if (screen && !screen.disposed) note(screen, text);
  }

  return {
    ensure,
    element: (id) => (screens.get(id) ? screens.get(id).host : null),
    write,
    notify,
    show,
    exited,
    dispose,
    clear() {
      for (const id of [...screens.keys()]) dispose(id);
    },
    has: (id) => screens.has(id),
    estimateSize,
  };
}

export { createTerminalView, terminalKey, sizeFor, pieces, unseen, MAX_PENDING, MAX_INPUT, THEME, themeFrom };
