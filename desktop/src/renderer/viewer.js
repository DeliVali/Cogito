'use strict';

/**
 * The read-only file viewer (design 1a «Explorador y visor», 1p): tabs over the thread
 * («Hilo» first, then each file opened from the explorer) and a pane that shows one file.
 * The composer stays below; sending a message goes back to the thread with the tabs kept.
 *
 * It never edits. The bar says «Solo lectura» («Los cambios los hace COGITO: pídelos
 * abajo»), and a file the model changed in this task carries «Modificado en esta tarea · +N
 * −N · Ver cambios», with the changed lines marked in the margin.
 *
 * Formats (1p): code and text with line numbers and colours (`highlight.js`); JSON, with a
 * line that does not parse marked; Markdown as «Vista» or «Fuente»; images on a checkered
 * ground with zoom and «Ajustar»; PDF in Chromium's own reader (pages, zoom, selectable
 * text); CSV/TSV with a fixed header, row numbers and numbers to the right; `.env` with its
 * values hidden until «Mostrar valores» (the main process sends them only then); anything
 * else, over 5 MB or binary, «No se puede mostrar este formato» with its type and size.
 *
 * Every piece of file text reaches the page through `textContent`, never as HTML.
 *
 * Terminal tabs (design 2026-11-01) sit in the same strip: «Terminal N» with ✕ (which ends
 * its shell), and a button at the end of the strip that opens one while the core has
 * terminals (`setTerminalsAvailable`). A terminal's pane is its folder and state over the
 * screen `terminal-view.js` keeps for it; terminals.js says what opened, changed or ended.
 *
 * Browser tabs (design 2026-11-05) sit there too: the page's title (or host) with a dot
 * while it loads, and ✕. A tab the model opens joins the strip without being shown. Its pane
 * is the address bar and page area `browser-view.js` keeps for it; the page itself is drawn
 * by main over that area, so leaving a browser tab for anything else says so
 * (`onHideBrowser`).
 *
 * `api` is `window.cogito.explorer`. `deps`: `play(kind)`, `platform`, `renderProse(el,
 * text)`, `changesFor(rel)` -> `{added, removed, diff} | null`, `onSeeChanges(rel)`,
 * `onShowThread()`, `onShowFile()`, for terminals `onNewTerminal()`,
 * `onCloseTerminal(id)`, `terminalElement(id)` -> its screen's element, `onShowTerminal(id,
 * {focus})`, and for browser tabs `onCloseBrowser(id)`, `browserElement(id)` -> its pane,
 * `onShowBrowser(id, {focus})`, `onHideBrowser()`.
 */

import { EXPLORER, TERMINAL, BROWSER } from './strings.js';
import { tokenize, COLOR } from './highlight.js';
import { parseUnifiedDiff } from './diff.js';
import { formatBytes } from './attachments.js';
import { badgeFor, extOf } from './explorer.js';
import { browserTitle } from './browser-view.js';

const LANG_NAMES = {
  ts: 'TypeScript', tsx: 'TypeScript', js: 'JavaScript', jsx: 'JavaScript', mjs: 'JavaScript', cjs: 'JavaScript',
  py: 'Python', go: 'Go', rs: 'Rust', java: 'Java', c: 'C', cpp: 'C++', h: 'C', hpp: 'C++', cs: 'C#', rb: 'Ruby',
  php: 'PHP', sh: 'Shell', sql: 'SQL', css: 'CSS', html: 'HTML', xml: 'XML', yaml: 'YAML', yml: 'YAML',
  toml: 'TOML', ini: 'INI', txt: 'Texto', log: 'Log',
};
const ZOOM_STEP = 25;
const ZOOM_MIN = 25;
const ZOOM_MAX = 400;
/** Rows of a table drawn at once; a larger file says how many it has in the meta. */
const MAX_TABLE_ROWS = 5000;

function el(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** `{line: 'added' | 'modified'}` for the new file's lines, from the task's diff: an added
 * line that replaces removed ones is modified, the rest are added. */
function changeMarks(diffText) {
  const marks = new Map();
  let removedRun = 0;
  for (const line of parseUnifiedDiff(diffText || '')) {
    if (line.type === 'remove') removedRun += 1;
    else if (line.type === 'add') {
      marks.set(line.lineNo, removedRun > 0 ? 'modified' : 'added');
      if (removedRun > 0) removedRun -= 1;
    } else removedRun = 0;
  }
  return marks;
}

/** The 1-based line a JSON parse error points at, or null. */
function jsonErrorLine(text) {
  try {
    JSON.parse(text);
    return null;
  } catch (error) {
    const message = String(error && error.message);
    const lineCol = /line (\d+)/i.exec(message);
    if (lineCol) return Number(lineCol[1]);
    const position = /position (\d+)/i.exec(message);
    if (position) return text.slice(0, Number(position[1])).split('\n').length;
    return 1;
  }
}

/** Rows of a CSV (`,`) or TSV (tab) text, quotes as RFC 4180 writes them. */
function parseTable(text, separator) {
  const rows = [];
  let row = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"' && cell === '') quoted = true;
    else if (ch === separator) {
      row.push(cell);
      cell = '';
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
    } else cell += ch;
  }
  if (cell !== '' || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

const NUMBER = /^-?\d+(?:[.,]\d+)?%?$/;

/** A terminal tab's key in `active`: a path never holds a NUL. */
const TERMINAL_KEY = '\u0000terminal:';
const terminalKey = (id) => `${TERMINAL_KEY}${id}`;
const isTerminalKey = (key) => typeof key === 'string' && key.startsWith(TERMINAL_KEY);
const BROWSER_KEY = '\u0000browser:';
const browserKey = (id) => `${BROWSER_KEY}${id}`;
const isBrowserKey = (key) => typeof key === 'string' && key.startsWith(BROWSER_KEY);

/** A favicon the page's CSP lets it draw: only a data: image. */
const drawableIcon = (url) => typeof url === 'string' && /^data:image\//i.test(url);

/** A terminal's state for its bar: running, ended (with its code), or the last command's
 * code when it failed. */
function terminalMeta(tab) {
  if (tab.exited) return TERMINAL.ended(tab.exitCode ?? null);
  if (tab.running) return TERMINAL.running;
  if (tab.exitCode != null && tab.exitCode !== 0) return TERMINAL.lastCode(tab.exitCode);
  return '';
}

/** How far a wheel event moves the tab strip sideways: its own horizontal delta when it has
 * one (a touchpad), else the vertical one, so a plain mouse wheel scrolls the tabs. Lines
 * and pages are turned into pixels. */
function wheelShift({ deltaX = 0, deltaY = 0, deltaMode = 0 }) {
  const delta = Math.abs(deltaX) > Math.abs(deltaY) ? deltaX : deltaY;
  if (deltaMode === 1) return delta * 16;
  if (deltaMode === 2) return delta * 400;
  return delta;
}

function createViewer({ tabsHost, pane, api, deps = {}, documentRef = document }) {
  const doc = documentRef;
  const play = deps.play || (() => {});
  const platform = deps.platform || 'linux';
  /** `{rel, name, view, zoom, mdMode, revealed}` for a file, `{kind: 'terminal', id, title,
   * cwd, running, exited, exitCode}` for a terminal, `{kind: 'browser', id, title, url,
   * loading, favicon}` for a browser tab, in the order opened. */
  let tabs = [];
  let active = null; // a file's rel, `terminalKey(id)`, `browserKey(id)`, or null for «Hilo»
  /** id -> a browser tab's node in the strip, so a page loading does not redraw the strip. */
  let browserNodes = new Map();
  let loadToken = 0;
  let terminalsAvailable = false;
  /** The shown terminal's state line, updated in place. */
  let terminalMetaNode = null;

  tabsHost.classList.add('file-tabs');
  tabsHost.setAttribute('role', 'tablist');
  // The strip hides its scrollbar, so the wheel is the way along it.
  tabsHost.addEventListener(
    'wheel',
    (event) => {
      const shift = wheelShift(event);
      if (!shift || tabsHost.scrollWidth <= tabsHost.clientWidth) return;
      event.preventDefault();
      tabsHost.scrollLeft += shift;
    },
    { passive: false },
  );
  pane.classList.add('viewer');

  function tabOf(rel) {
    return tabs.find((tab) => !tab.kind && tab.rel === rel) || null;
  }

  function terminalOf(id) {
    return tabs.find((tab) => tab.kind === 'terminal' && tab.id === id) || null;
  }

  function browserOf(id) {
    return tabs.find((tab) => tab.kind === 'browser' && tab.id === id) || null;
  }

  const keyOf = (tab) => (tab.kind === 'terminal' ? terminalKey(tab.id) : tab.kind === 'browser' ? browserKey(tab.id) : tab.rel);

  /** Every change of the tab on screen goes through here: leaving a browser tab hides its
   * page, which main draws above the window. */
  function setActive(key) {
    const leaving = isBrowserKey(active) && !isBrowserKey(key);
    active = key;
    pane.classList.toggle('viewer--browser', isBrowserKey(key));
    if (leaving && deps.onHideBrowser) deps.onHideBrowser();
  }

  function terminalNode(tab) {
    const isActive = active === terminalKey(tab.id);
    const state = `${tab.exited ? ' is-ended' : ''}${tab.running ? ' is-running' : ''}`;
    const node = el(doc, 'div', `file-tab file-tab--terminal${isActive ? ' is-active' : ''}${state}`);
    node.setAttribute('role', 'tab');
    node.setAttribute('aria-selected', String(isActive));
    node.title = tab.cwd || tab.title || '';
    const close = el(doc, 'button', 'file-tab-close', '✕');
    close.type = 'button';
    close.setAttribute('aria-label', TERMINAL.closeTab(tab.title || ''));
    close.addEventListener('click', (event) => {
      event.stopPropagation();
      play('close');
      if (deps.onCloseTerminal) deps.onCloseTerminal(tab.id);
    });
    node.append(el(doc, 'span', 'file-tab-badge file-tab-badge--terminal', '>_'), el(doc, 'span', 'file-tab-name', tab.title || ''), close);
    node.addEventListener('click', () => {
      play('click');
      showTerminal(tab.id);
    });
    return node;
  }

  /** A browser tab's strip entry, drawn from `tab`. */
  function paintBrowser(node, tab) {
    const name = browserTitle(tab);
    node.title = tab.url || name;
    node.classList.toggle('is-loading', Boolean(tab.loading));
    const parts = node.browserParts;
    parts.name.textContent = name;
    parts.dot.hidden = !tab.loading;
    parts.close.setAttribute('aria-label', BROWSER.closeTab(name));
    if (drawableIcon(tab.favicon)) {
      parts.icon.src = tab.favicon;
      parts.icon.hidden = false;
      parts.badge.hidden = true;
    } else {
      parts.icon.hidden = true;
      parts.badge.hidden = false;
    }
  }

  function browserNode(tab) {
    const isActive = active === browserKey(tab.id);
    const node = el(doc, 'div', `file-tab file-tab--browser${isActive ? ' is-active' : ''}`);
    node.setAttribute('role', 'tab');
    node.setAttribute('aria-selected', String(isActive));
    const icon = el(doc, 'img', 'file-tab-icon');
    icon.alt = '';
    icon.draggable = false;
    const badge = el(doc, 'span', 'file-tab-badge file-tab-badge--browser');
    badge.setAttribute('aria-hidden', 'true');
    const dot = el(doc, 'span', 'file-tab-loading');
    dot.title = BROWSER.loading;
    const close = el(doc, 'button', 'file-tab-close', '✕');
    close.type = 'button';
    close.addEventListener('click', (event) => {
      event.stopPropagation();
      play('close');
      if (deps.onCloseBrowser) deps.onCloseBrowser(tab.id);
    });
    const name = el(doc, 'span', 'file-tab-name');
    node.browserParts = { icon, badge, dot, name, close };
    node.append(icon, badge, name, dot, close);
    node.addEventListener('click', () => {
      play('click');
      showBrowser(tab.id);
    });
    paintBrowser(node, tab);
    browserNodes.set(tab.id, node);
    return node;
  }

  function renderTabs() {
    browserNodes = new Map();
    tabsHost.hidden = tabs.length === 0;
    tabsHost.textContent = '';
    if (tabsHost.hidden) return;
    const thread = el(doc, 'button', `file-tab file-tab--thread${active === null ? ' is-active' : ''}`);
    thread.type = 'button';
    thread.setAttribute('role', 'tab');
    thread.setAttribute('aria-selected', String(active === null));
    thread.append(el(doc, 'span', 'file-tab-dot'), el(doc, 'span', 'file-tab-name', EXPLORER.thread));
    thread.addEventListener('click', () => {
      play('click');
      showThread();
    });
    tabsHost.append(thread);
    for (const tab of tabs) {
      if (tab.kind === 'terminal') {
        tabsHost.append(terminalNode(tab));
        continue;
      }
      if (tab.kind === 'browser') {
        tabsHost.append(browserNode(tab));
        continue;
      }
      const node = el(doc, 'div', `file-tab${active === tab.rel ? ' is-active' : ''}`);
      node.setAttribute('role', 'tab');
      node.setAttribute('aria-selected', String(active === tab.rel));
      node.title = tab.rel;
      const [label, , ink] = badgeFor({ name: tab.name, dir: false });
      const badge = el(doc, 'span', 'file-tab-badge', label);
      badge.style.color = ink;
      const close = el(doc, 'button', 'file-tab-close', '✕');
      close.type = 'button';
      close.setAttribute('aria-label', EXPLORER.closeTab(tab.name));
      close.addEventListener('click', (event) => {
        event.stopPropagation();
        play('close');
        closeTab(tab.rel);
      });
      node.append(badge, el(doc, 'span', 'file-tab-name', tab.name), close);
      node.addEventListener('click', () => {
        play('click');
        show(tab.rel);
      });
      tabsHost.append(node);
    }
    const current = tabsHost.querySelector ? tabsHost.querySelector('.file-tab.is-active') : null;
    if (current && current.scrollIntoView) current.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  function showThread() {
    setActive(null);
    pane.hidden = true;
    renderTabs();
    if (deps.onShowThread) deps.onShowThread();
  }

  async function open(rel) {
    let tab = tabOf(rel);
    if (!tab) {
      tab = { rel, name: rel.slice(rel.lastIndexOf('/') + 1), view: null, zoom: 100, fit: false, mdMode: 'view', revealed: null };
      tabs.push(tab);
    }
    tab.view = null; // re-read on every open: the model may have changed it
    tab.revealed = null;
    await show(rel);
  }

  async function show(rel) {
    const tab = tabOf(rel);
    if (!tab) return;
    setActive(rel);
    renderTabs();
    pane.hidden = false;
    if (deps.onShowFile) deps.onShowFile(rel);
    const token = ++loadToken;
    if (!tab.view) {
      const view = await api.read(rel).catch(() => null);
      if (token !== loadToken) return;
      tab.view = view && !view.error ? view : { kind: 'missing', name: tab.name, path: rel };
    }
    draw(tab);
  }

  /** Shows the tab with `key`: a file, a terminal or a browser tab. `focus`: a terminal
   * takes the keyboard (not when it only comes forward because another tab closed). */
  function showKey(key, { focus = true } = {}) {
    if (isTerminalKey(key)) showTerminal(key.slice(TERMINAL_KEY.length), { focus });
    else if (isBrowserKey(key)) showBrowser(key.slice(BROWSER_KEY.length), { focus });
    else show(key);
  }

  function dropTab(key) {
    const index = tabs.findIndex((tab) => keyOf(tab) === key);
    if (index < 0) return;
    tabs.splice(index, 1);
    if (active === key) {
      if (tabs.length) showKey(keyOf(tabs[tabs.length - 1]), { focus: false });
      else showThread();
    } else renderTabs();
  }

  function closeTab(rel) {
    dropTab(rel);
  }

  // ---------- terminals ----------
  function showTerminal(id, { focus = true } = {}) {
    const tab = terminalOf(id);
    if (!tab) return;
    loadToken += 1; // a file still loading must not draw over it
    setActive(terminalKey(id));
    renderTabs();
    pane.hidden = false;
    drawTerminal(tab);
    if (deps.onShowTerminal) deps.onShowTerminal(id, { focus });
  }

  function drawTerminal(tab) {
    pane.textContent = '';
    const bar = el(doc, 'div', 'viewer-bar');
    terminalMetaNode = el(doc, 'span', 'viewer-meta', terminalMeta(tab));
    bar.append(el(doc, 'span', 'viewer-path', tab.cwd || ''), terminalMetaNode);
    pane.append(bar);
    const screen = deps.terminalElement ? deps.terminalElement(tab.id) : null;
    if (screen) pane.append(screen);
  }

  // ---------- browser tabs ----------
  /** `focus`: the person opened it, so a blank one takes the keyboard. */
  function showBrowser(id, { focus = false } = {}) {
    const tab = browserOf(id);
    if (!tab) return;
    loadToken += 1;
    setActive(browserKey(id));
    renderTabs();
    pane.hidden = false;
    pane.textContent = '';
    const page = deps.browserElement ? deps.browserElement(id) : null;
    if (page) pane.append(page);
    if (deps.onShowBrowser) deps.onShowBrowser(id, { focus });
  }

  // ---------- the pane ----------
  function draw(tab) {
    const view = tab.view;
    pane.textContent = '';
    pane.append(buildBar(tab));
    const changes = deps.changesFor ? deps.changesFor(tab.rel) : null;
    if (changes && view.kind !== 'missing') pane.append(buildChangedStrip(tab.rel, changes));
    const body = buildBody(tab, changes);
    pane.append(body);
  }

  function buildBar(tab) {
    const view = tab.view;
    const bar = el(doc, 'div', 'viewer-bar');
    bar.append(el(doc, 'span', 'viewer-path', tab.rel.split('/').join('  ›  ')));
    if (view.kind === 'markdown') {
      const seg = el(doc, 'div', 'viewer-seg');
      for (const [mode, label] of [['view', EXPLORER.mdView], ['source', EXPLORER.mdSource]]) {
        const button = el(doc, 'button', `viewer-seg-option${tab.mdMode === mode ? ' is-active' : ''}`, label);
        button.type = 'button';
        button.addEventListener('click', () => {
          play('click');
          tab.mdMode = mode;
          draw(tab);
        });
        seg.append(button);
      }
      bar.append(seg);
    }
    if (view.kind === 'image') {
      const group = el(doc, 'div', 'viewer-zoom');
      const readout = el(doc, 'span', 'viewer-zoom-readout', tab.fit ? EXPLORER.fit : `${tab.zoom} %`);
      const step = (delta) => {
        play('click');
        tab.fit = false;
        tab.zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, tab.zoom + delta));
        draw(tab);
      };
      const minus = el(doc, 'button', 'viewer-button', '−');
      minus.type = 'button';
      minus.addEventListener('click', () => step(-ZOOM_STEP));
      const plus = el(doc, 'button', 'viewer-button', '+');
      plus.type = 'button';
      plus.addEventListener('click', () => step(ZOOM_STEP));
      const fit = el(doc, 'button', 'viewer-button', EXPLORER.fit);
      fit.type = 'button';
      fit.addEventListener('click', () => {
        play('click');
        tab.fit = !tab.fit;
        if (!tab.fit) tab.zoom = 100;
        draw(tab);
      });
      group.append(minus, readout, plus, fit);
      bar.append(group);
    }
    bar.append(el(doc, 'span', 'viewer-meta', metaOf(tab)));
    const readOnly = el(doc, 'span', 'viewer-readonly', EXPLORER.readOnly);
    readOnly.title = EXPLORER.readOnlyTip;
    bar.append(readOnly);
    return bar;
  }

  function metaOf(tab) {
    const view = tab.view;
    const size = formatBytes(view.size);
    switch (view.kind) {
      case 'text': {
        const lang = LANG_NAMES[extOf(view.name)] || 'Texto';
        return [lang, EXPLORER.lines(view.lines), view.encoding, view.eol].join(' · ');
      }
      case 'json':
        return ['JSON', EXPLORER.lines(view.lines)].join(' · ');
      case 'markdown':
        return ['Markdown', size].join(' · ');
      case 'image':
        return [tab.natural ? `${tab.natural[0]} × ${tab.natural[1]}` : '', extOf(view.name).toUpperCase(), size]
          .filter(Boolean)
          .join(' · ');
      case 'pdf':
        return ['PDF', size].join(' · ');
      case 'csv':
      case 'tsv': {
        const rows = parseTable(view.text, view.kind === 'tsv' ? '\t' : ',').filter((r) => r.some((c) => c !== ''));
        const columns = rows.reduce((max, r) => Math.max(max, r.length), 0);
        return [view.kind.toUpperCase(), EXPLORER.rows(Math.max(0, rows.length - 1)), EXPLORER.columns(columns)].join(' · ');
      }
      case 'env':
        return EXPLORER.variables(view.variables || 0);
      default:
        return '';
    }
  }

  function buildChangedStrip(rel, changes) {
    const strip = el(doc, 'div', 'viewer-changed');
    strip.append(el(doc, 'span', 'viewer-dot'));
    strip.append(el(doc, 'span', 'viewer-changed-text', EXPLORER.changedStrip(changes.added || 0, changes.removed || 0)));
    if (changes.diff && deps.onSeeChanges) {
      const link = el(doc, 'button', 'viewer-link', EXPLORER.seeChanges);
      link.type = 'button';
      link.addEventListener('click', () => {
        play('click');
        deps.onSeeChanges(rel);
      });
      strip.append(link);
    }
    return strip;
  }

  function buildBody(tab, changes) {
    const view = tab.view;
    switch (view.kind) {
      case 'text':
        return codeView(view.text, extOf(view.name), changes);
      case 'json':
        return codeView(view.text, extOf(view.name), changes, { errorLine: extOf(view.name) === 'json' ? jsonErrorLine(view.text) : null });
      case 'markdown':
        return tab.mdMode === 'source' ? codeView(view.text, 'md', changes, { markdown: true }) : markdownView(view.text);
      case 'image':
        return imageView(tab);
      case 'pdf':
        return pdfView(view);
      case 'csv':
      case 'tsv':
        return tableView(view.text, view.kind === 'tsv' ? '\t' : ',');
      case 'env':
        return envView(tab, changes);
      case 'missing':
        return unsupportedView({ name: tab.name, size: 0, path: tab.rel }, true);
      default:
        return unsupportedView(view);
    }
  }

  function codeView(text, ext, changes, { errorLine = null, markdown = false } = {}) {
    const box = el(doc, 'div', 'viewer-code soft-scroll');
    const marks = changeMarks(changes && changes.diff);
    let lines = markdown
      ? String(text).split(/\r?\n/).map((line) => [{ t: line, c: /^\s*#/.test(line) ? 'keyword' : 'name' }])
      : tokenize(text, ext);
    if (String(text).endsWith('\n')) lines = lines.slice(0, -1);
    const frag = doc.createDocumentFragment();
    lines.forEach((tokens, index) => {
      const number = index + 1;
      const row = el(doc, 'div', 'viewer-line');
      const mark = errorLine === number ? 'error' : marks.get(number);
      if (mark) row.classList.add(`is-${mark}`);
      if (mark === 'error') row.title = EXPLORER.jsonInvalid;
      row.append(el(doc, 'span', 'viewer-gutter', String(number)), el(doc, 'span', 'viewer-mark'));
      const code = el(doc, 'span', 'viewer-text');
      if (!tokens.length) code.append(doc.createTextNode(' '));
      for (const token of tokens) {
        const span = el(doc, 'span', '', token.t);
        span.style.color = COLOR[token.c] || COLOR.name;
        if (token.c === 'comment') span.style.fontStyle = 'italic';
        code.append(span);
      }
      row.append(code);
      frag.append(row);
    });
    box.append(frag);
    return box;
  }

  function markdownView(text) {
    const box = el(doc, 'div', 'viewer-md soft-scroll');
    const inner = el(doc, 'div', 'viewer-md-inner');
    if (deps.renderProse) deps.renderProse(inner, text);
    else inner.textContent = text;
    box.append(inner);
    return box;
  }

  function imageView(tab) {
    const box = el(doc, 'div', 'viewer-image soft-scroll');
    const img = el(doc, 'img', 'viewer-img');
    img.alt = tab.name;
    img.src = tab.view.data;
    img.draggable = false;
    img.addEventListener('load', () => {
      const first = !tab.natural;
      tab.natural = [img.naturalWidth, img.naturalHeight];
      if (first) {
        const meta = pane.querySelector('.viewer-meta');
        if (meta) meta.textContent = metaOf(tab);
      }
      size();
    });
    const size = () => {
      if (!tab.natural) return;
      if (tab.fit) {
        img.style.width = '';
        img.style.maxWidth = '100%';
        img.style.maxHeight = '100%';
      } else {
        img.style.maxWidth = 'none';
        img.style.maxHeight = 'none';
        img.style.width = `${Math.round((tab.natural[0] * tab.zoom) / 100)}px`;
      }
    };
    box.append(img);
    return box;
  }

  function pdfView(view) {
    const box = el(doc, 'div', 'viewer-pdf');
    const frame = el(doc, 'iframe', 'viewer-pdf-frame');
    frame.title = view.name;
    frame.src = view.url;
    box.append(frame);
    return box;
  }

  function tableView(text, separator) {
    const box = el(doc, 'div', 'viewer-table soft-scroll');
    const rows = parseTable(text, separator).filter((r) => r.some((c) => c !== ''));
    if (!rows.length) return box;
    const columns = rows.reduce((max, r) => Math.max(max, r.length), 0);
    const [header, ...data] = rows;
    const numeric = Array.from({ length: columns }, (_, j) => {
      const values = data.map((r) => (r[j] || '').trim()).filter(Boolean);
      return values.length > 0 && values.every((v) => NUMBER.test(v));
    });
    const grid = el(doc, 'div', 'viewer-grid');
    grid.style.gridTemplateColumns = `42px repeat(${columns}, minmax(110px, 1fr))`;
    grid.append(el(doc, 'div', 'viewer-th viewer-corner'));
    for (let j = 0; j < columns; j += 1) grid.append(el(doc, 'div', 'viewer-th', header[j] || ''));
    data.slice(0, MAX_TABLE_ROWS).forEach((row, i) => {
      grid.append(el(doc, 'div', 'viewer-rownum', String(i + 1)));
      for (let j = 0; j < columns; j += 1) {
        const cell = el(doc, 'div', `viewer-td${numeric[j] ? ' is-number' : ''}`, row[j] || '');
        grid.append(cell);
      }
    });
    box.append(grid);
    return box;
  }

  function envView(tab, changes) {
    const box = el(doc, 'div', 'viewer-env');
    const banner = el(doc, 'div', 'viewer-env-banner');
    banner.append(el(doc, 'span', 'viewer-dot viewer-dot--amber'), el(doc, 'span', 'viewer-env-text', EXPLORER.hiddenValues));
    const toggle = el(doc, 'button', 'viewer-button', tab.revealed !== null ? EXPLORER.hideValues : EXPLORER.showValues);
    toggle.type = 'button';
    toggle.addEventListener('click', async () => {
      play('click');
      if (tab.revealed !== null) tab.revealed = null;
      else {
        const answer = await api.revealValues(tab.rel).catch(() => null);
        if (!answer || answer.error) return;
        tab.revealed = answer.text;
      }
      draw(tab);
    });
    banner.append(toggle);
    box.append(banner, codeView(tab.revealed !== null ? tab.revealed : tab.view.text, 'env', changes));
    return box;
  }

  function unsupportedView(view, missing = false) {
    const box = el(doc, 'div', 'viewer-unsupported');
    const ext = extOf(view.name);
    box.append(el(doc, 'span', 'viewer-unsupported-badge', (ext || '·').toUpperCase().slice(0, 4)));
    box.append(el(doc, 'div', 'viewer-unsupported-title', EXPLORER.cannotShow));
    if (!missing) box.append(el(doc, 'div', 'viewer-unsupported-meta', [ext.toUpperCase(), formatBytes(view.size)].filter(Boolean).join(' · ')));
    const buttons = el(doc, 'div', 'viewer-unsupported-buttons');
    const reveal = el(doc, 'button', 'cc-button', EXPLORER.reveal[platform] || EXPLORER.reveal.linux);
    reveal.type = 'button';
    reveal.addEventListener('click', () => {
      play('click');
      api.reveal(view.path);
    });
    const openApp = el(doc, 'button', 'cc-button cc-button--primary', EXPLORER.openSystem);
    openApp.type = 'button';
    openApp.addEventListener('click', () => {
      play('click');
      api.openSystem(view.path);
    });
    buttons.append(reveal, openApp);
    if (!missing) box.append(buttons);
    return box;
  }

  /** Re-reads the file on screen, keeping the reader's place. Not while text is selected
   * in it: redrawing would drop the selection the person is about to copy. */
  async function refreshActive() {
    const tab = active ? tabOf(active) : null;
    if (!tab || pane.hidden) return;
    const selection = doc.getSelection ? doc.getSelection() : null;
    if (selection && !selection.isCollapsed && pane.contains(selection.anchorNode)) return;
    const body = pane.querySelector('.viewer-code, .viewer-md, .viewer-table, .viewer-image');
    const place = body ? [body.scrollTop, body.scrollLeft] : null;
    const view = await api.read(tab.rel).catch(() => null);
    if (active !== tab.rel) return;
    tab.view = view && !view.error ? view : { kind: 'missing', name: tab.name, path: tab.rel };
    // Values the person chose to see stay shown, read again.
    if (tab.revealed !== null) {
      const shown = await api.revealValues(tab.rel).catch(() => null);
      tab.revealed = shown && !shown.error ? shown.text : null;
    }
    draw(tab);
    const again = pane.querySelector('.viewer-code, .viewer-md, .viewer-table, .viewer-image');
    if (again && place) [again.scrollTop, again.scrollLeft] = place;
  }

  // A change on disk (the model writing, the person's editor) reaches the file on screen.
  if (api.onChanged) api.onChanged(() => refreshActive());

  return {
    open,
    showThread,
    isShowingFile: () => active !== null && !isTerminalKey(active) && !isBrowserKey(active),
    isShowingTerminal: () => isTerminalKey(active),
    isShowingBrowser: () => isBrowserKey(active),
    /** A browser tab main listed. Never shown unless `focus` (the person opened it). */
    addBrowser(tab, { focus = false } = {}) {
      if (!tab || browserOf(tab.id)) return;
      tabs.push({ ...tab, kind: 'browser' });
      if (focus) showBrowser(tab.id, { focus: true });
      else renderTabs();
    },
    /** New state for a browser tab (title, address, loading), painted in place. */
    updateBrowser(id, patch) {
      const tab = browserOf(id);
      if (!tab) return;
      Object.assign(tab, patch, { kind: 'browser', id });
      const node = browserNodes.get(id);
      if (node) paintBrowser(node, tab);
      else renderTabs();
    },
    removeBrowser(id) {
      dropTab(browserKey(id));
    },
    showBrowser,
    /** Whether the core has terminals here: the strip's button shows only then. */
    setTerminalsAvailable(value) {
      terminalsAvailable = Boolean(value);
      renderTabs();
    },
    /** A terminal tab the core opened; `focus` shows it. */
    addTerminal(tab, { focus = false } = {}) {
      if (!tab || terminalOf(tab.id)) return;
      tabs.push({ ...tab, kind: 'terminal' });
      if (focus) showTerminal(tab.id);
      else renderTabs();
    },
    /** New state for a terminal tab (running, its code, ended). */
    updateTerminal(id, patch) {
      const tab = terminalOf(id);
      if (!tab) return;
      Object.assign(tab, patch, { kind: 'terminal', id });
      renderTabs();
      if (active === terminalKey(id) && terminalMetaNode) terminalMetaNode.textContent = terminalMeta(tab);
    },
    removeTerminal(id) {
      dropTab(terminalKey(id));
    },
    showTerminal,
    /** The explorer renamed or moved `from` to `to`. */
    renamed(from, to) {
      for (const tab of tabs) {
        if (tab.kind) continue;
        if (tab.rel === from || tab.rel.startsWith(from + '/')) {
          if (active === tab.rel) active = to + tab.rel.slice(from.length);
          tab.rel = to + tab.rel.slice(from.length);
          tab.name = tab.rel.slice(tab.rel.lastIndexOf('/') + 1);
        }
      }
      renderTabs();
    },
    /** The explorer moved `rel` to the trash: its tabs close, and the view goes to «Hilo»
     * if one of them was showing. */
    removed(rel) {
      const wasActive = active !== null && !isTerminalKey(active) && !isBrowserKey(active) && (active === rel || active.startsWith(rel + '/'));
      tabs = tabs.filter((tab) => tab.kind || !(tab.rel === rel || tab.rel.startsWith(rel + '/')));
      if (wasActive) showThread();
      else renderTabs();
    },
    /** Re-reads the file on screen (the model or an editor changed it). */
    refreshActive,
    /** A project switch: every tab closes, terminals and browser tabs too (they were that
     * session's and that project's). */
    clear() {
      tabs = [];
      showThread();
    },
  };
}

export { createViewer, changeMarks, jsonErrorLine, parseTable, wheelShift, terminalMeta };
