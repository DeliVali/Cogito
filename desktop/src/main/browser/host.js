'use strict';

/**
 * The browser tabs beside «Hilo»: one WebContentsView each, in the app's window, owned here.
 *
 * Each project's tabs share a persistent partition of their own, never the UI's session, and
 * every partition is hardened the same way: only http(s), ws(s), data, blob and about load
 * (the one block that held against CDP `Page.navigate` to file:, chrome: and devtools:),
 * permissions and devices are denied, certificate errors refused, downloads saved under the
 * project's `.cogito/downloads/` (never through a link, never over a file), and the user
 * agent no longer names the app or Electron. A popup becomes a tab in the same partition,
 * its `window.opener` kept. A model op on a tab the person is not looking at never keeps
 * the keyboard focus, and the app's own shortcuts still reach the window from a page.
 *
 * The model's operations arrive through `command` (rpc.js); the window's through the
 * methods ipc.js calls. Tab ids are `b1`, `b2`, ... per session.
 *
 * Facts this rests on: cogito-lab research/2026-11-05-native-browser (electron.md,
 * cdp-features.md, CONTRACT.md), probed on Electron 44.4.3.
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const { TabDriver, BrowserFailure, TAB_OPS } = require('./ops');
const { shortcutOf } = require('./shortcuts');

/** Schemes a tab may load, checked on every request of the partition. */
const ALLOWED_SCHEME = /^(https?|wss?|data|blob|about):/i;
/** Schemes a frame may navigate to. */
const NAVIGABLE = /^(https?|data|blob|about):/i;
/** Every operation the core may ask for. */
const OPS = Object.freeze(new Set([...TAB_OPS, 'navigate', 'tabs', 'close']));
const MAX_TABS = 30;
/** An answer's body, `{ok, result}` as JSON, is at most this many bytes (the core's cap). */
const MAX_RESULT = 8 * 1024 * 1024;
/** Ops that may move the keyboard focus into their tab. */
const FOCUS_OPS = new Set(['navigate', 'back', 'forward', 'reload', 'click', 'type', 'press', 'select', 'evaluate', 'steps']);
/** Chromium's net error for a navigation that was cancelled or replaced: not a failure. */
const ERR_ABORTED = -3;
const DEFAULT_RECT = { x: 0, y: 0, width: 1280, height: 800 };
const BROADCAST_MS = 100;

const TAB_PREFERENCES = {
  sandbox: true,
  contextIsolation: true,
  nodeIntegration: false,
  nodeIntegrationInSubFrames: false,
  webSecurity: true,
  webviewTag: false,
  allowRunningInsecureContent: false,
};

/** The partition of a project's tabs. */
function partitionFor(root) {
  const digest = crypto.createHash('sha256').update(String(root)).digest('hex');
  return `persist:cogito-browser-${digest.slice(0, 16)}`;
}

/**
 * Chromium's user agent without the app's name or Electron's, and with Chrome's version
 * reduced to `major.0.0.0` as Chrome itself sends it. Only the products a Chrome UA carries
 * stay; the platform in parentheses is kept as it is.
 */
function cleanUserAgent(ua) {
  const tokens = String(ua).match(/\([^)]*\)|[^\s()]+/g) || [];
  return tokens
    .filter((token) => token.startsWith('(') || /^(Mozilla|AppleWebKit|Chrome|Safari|Mobile)(\/|$)/.test(token))
    .map((token) => token.replace(/^Chrome\/(\d+)[.\d]*/, 'Chrome/$1.0.0.0'))
    .join(' ');
}

/**
 * The same for `app.userAgentFallback`, which service and shared workers use instead of
 * their session's. Main calls it before any partition is made.
 */
function cleanAppUserAgent(app) {
  if (app && typeof app.userAgentFallback === 'string') app.userAgentFallback = cleanUserAgent(app.userAgentFallback);
}

/** A path, a query or a fragment, as a link's href may be: read against the tab's page. */
const RELATIVE = /^(\/(?!\/)|\.\.?\/|[?#])/;

/** An http(s) URL the model asked for, or a refusal; a relative one is read against `base`,
 * the page open in the tab, which must be http(s) itself. */
function webUrl(text, base = '') {
  const raw = String(text);
  let url;
  try {
    url = new URL(raw);
  } catch {
    const relative = RELATIVE.test(raw);
    if (!relative || !/^https?:/i.test(String(base))) {
      const why = relative ? '; a path opens only beside a web page open in the tab' : '';
      throw new BrowserFailure('bad_args', `${JSON.stringify(raw.slice(0, 200))} is not an address${why}`);
    }
    url = new URL(raw, base);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new BrowserFailure('refused', `only http and https addresses open in the browser, not ${url.protocol}`);
  }
  return url.href;
}

/**
 * The address bar's text as a URL: a bare host gets https:// (http:// for a local server),
 * words with spaces go nowhere, and only http(s) opens. Returns `{url}` or `{error}`, whose
 * code the window words: `not_address`, or `not_web` for another scheme.
 */
function normalizeAddress(text) {
  if (typeof text !== 'string') return { error: 'not_address' };
  const raw = text.trim();
  if (!raw || raw.length > 8192 || /\s/.test(raw)) return { error: 'not_address' };
  let candidate = raw;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) {
    // `host:port` reads as a scheme; anything else with a colon before a slash is one.
    if (/^[a-z][a-z0-9+.-]*:/i.test(raw) && !/^[^/:]+:\d+(\/|$)/.test(raw)) return { error: 'not_web' };
    const local = /^(localhost|127\.|\[::1\]|0\.0\.0\.0)/i.test(raw);
    candidate = `${local ? 'http' : 'https'}://${raw}`;
  }
  try {
    const url = new URL(candidate);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return { error: 'not_web' };
    if (!url.hostname) return { error: 'not_address' };
    return { url: url.href };
  } catch {
    return { error: 'not_address' };
  }
}

const answerSize = (result) => Buffer.byteLength(JSON.stringify({ ok: true, result }));
const MIN_TEXT = 64;

/** Every string under `node` but a screenshot's `png`, as `{holder, key}`. */
function strings(node, out = []) {
  if (node === null || typeof node !== 'object') return out;
  for (const key of Object.keys(node)) {
    const value = node[key];
    if (typeof value === 'string') {
      if (key !== 'png') out.push({ holder: node, key });
    } else strings(value, out);
  }
  return out;
}

/** Every array or object below the top level, as `{node, path, size}`. */
function containers(node, path = '', out = []) {
  for (const key of Object.keys(node)) {
    const value = node[key];
    if (value === null || typeof value !== 'object') continue;
    const at = path ? `${path}.${key}` : key;
    out.push({ node: value, path: at, size: Buffer.byteLength(JSON.stringify(value)) });
    containers(value, at, out);
  }
  return out;
}

/**
 * `result` made small enough that the answer carrying it, `{ok, result}` as JSON, fits in
 * `limit` bytes: long strings are cut under a shrinking threshold, then entries are dropped
 * from the largest arrays and objects, named in `left_out` with how many went. Anything cut
 * sets `cut: true`.
 */
function fit(result, limit = MAX_RESULT) {
  if (answerSize(result) <= limit) return result;
  const out = structuredClone(result);
  out.cut = true;
  const cutStrings = (threshold) => {
    for (const { holder, key } of strings(out)) {
      if (holder[key].length > threshold) holder[key] = `${holder[key].slice(0, threshold)}…`;
    }
  };
  // First as little as the longest text needs, then half as long each round.
  const all = strings(out);
  const longest = all.reduce((most, { holder, key }) => Math.max(most, holder[key].length), 0);
  let threshold = Math.max(MIN_TEXT, longest - (answerSize(out) - limit) - 4096);
  while (answerSize(out) > limit) {
    cutStrings(threshold);
    if (threshold <= MIN_TEXT) break;
    threshold = Math.max(MIN_TEXT, Math.floor(threshold / 2));
  }
  const leftOut = {};
  for (let round = 0; answerSize(out) > limit && round < 200; round += 1) {
    const biggest = containers(out)
      .filter((c) => c.path !== 'left_out' && Object.keys(c.node).length > 0)
      .sort((a, b) => b.size - a.size)[0];
    if (!biggest) break;
    const keys = Object.keys(biggest.node);
    const over = answerSize(out) - limit;
    // Enough entries, judged by their average size, and at least one.
    const drop = Math.min(keys.length, Math.max(1, Math.ceil((keys.length * over) / Math.max(1, biggest.size)) + 1));
    if (Array.isArray(biggest.node)) biggest.node.splice(biggest.node.length - drop, drop);
    else for (const key of keys.slice(keys.length - drop)) delete biggest.node[key];
    leftOut[biggest.path] = (leftOut[biggest.path] || 0) + drop;
    out.left_out = leftOut;
  }
  if (answerSize(out) > limit && typeof out.png === 'string') {
    delete out.png;
    out.left_out = { ...leftOut, png: 1 };
  }
  if (answerSize(out) <= limit) return out;
  // Nothing left to shrink: the tab, and that the result did not fit.
  const bare = { cut: true, error: 'the result was too large to send' };
  for (const key of ['tab', 'title', 'url']) if (typeof result[key] === 'string') bare[key] = result[key].slice(0, 2000);
  return bare;
}

/** A downloaded file's name with no path, no leading dot and no control character. */
function safeName(name) {
  const cleaned = String(name || '')
    .replace(/[\u0000-\u001f\u007f/\\<>:"|?*]/g, '_')
    .replace(/^[.\s]+/, '')
    .trim();
  const short = cleaned.length > 200 ? cleaned.slice(cleaned.length - 200) : cleaned;
  return short.replace(/^[.\s]+/, '') || 'download';
}

/** `folder` exists as a real directory of the person's own, or this throws. */
function checkFolder(folder) {
  const stat = fs.lstatSync(folder);
  if (stat.isSymbolicLink()) throw new Error(`${folder} is a link`);
  if (!stat.isDirectory()) throw new Error(`${folder} is not a folder`);
  if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) throw new Error(`${folder} is not the person's own`);
}

/** `folder`, made if missing (never through a link), then checked. */
function ensureFolder(folder) {
  try {
    fs.mkdirSync(folder, { mode: 0o700 });
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }
  checkFolder(folder);
}

/**
 * The project's `.cogito/downloads`, made safely: neither part may be a link, another
 * kind of file or someone else's, and the folder must really be under the project.
 */
function downloadsFolder(root) {
  const dot = path.join(root, '.cogito');
  const folder = path.join(dot, 'downloads');
  ensureFolder(dot);
  ensureFolder(folder);
  if (fs.realpathSync(folder) !== path.join(fs.realpathSync(root), '.cogito', 'downloads')) {
    throw new Error(`${folder} is not under the project`);
  }
  return folder;
}

/** Claims a new file under `folder` for `name`, adding a counter rather than overwriting. */
function claimName(folder, name) {
  const ext = path.extname(name);
  const stem = name.slice(0, name.length - ext.length) || 'download';
  const flags = fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY | (fs.constants.O_NOFOLLOW || 0);
  for (let n = 0; n < 1000; n += 1) {
    const candidate = path.join(folder, n ? `${stem} (${n})${ext}` : `${stem}${ext}`);
    try {
      fs.closeSync(fs.openSync(candidate, flags, 0o600));
      return candidate;
    } catch (error) {
      if (error.code !== 'EEXIST' && error.code !== 'ELOOP') throw error;
    }
  }
  throw new Error('no free name');
}

class BrowserHost {
  /**
   * `electron`: `{WebContentsView, session}` (a fake in the lab). `onTabs(list)` hears the
   * tab list whenever it changes, debounced.
   */
  constructor({ window, electron, onTabs = () => {}, log = () => {}, driverOptions = {}, platform = process.platform }) {
    this.window = window;
    this.electron = electron;
    this.onTabs = onTabs;
    this.log = log;
    this.driverOptions = driverOptions;
    this.tabs = new Map();
    this.seq = 0;
    this.modelTab = null;
    this.visible = null;
    this.rect = { ...DEFAULT_RECT };
    this.sessionId = null;
    this.root = null;
    this.prepared = new Map(); // storage path or session -> project root
    this.hardened = new WeakSet();
    this.timer = null;
    this.platform = platform;
    // A reloaded window draws «Hilo» again: no view may stay painted over it. Only the
    // window's own page loading again counts; a hash change or an iframe in it does not.
    const ui = window.webContents;
    if (ui && typeof ui.on === 'function') {
      ui.on('did-start-navigation', (details) => {
        if (details && details.isMainFrame && !details.isSameDocument) this.setBounds(null, null);
      });
      ui.on('did-navigate', () => this.setBounds(null, null));
    }
  }

  // -- partitions -------------------------------------------------------------------------

  /** The project's partition, hardened once. */
  sessionFor(root) {
    const ses = this.electron.session.fromPartition(partitionFor(root));
    if (this.prepared.has(ses)) return ses;
    this.prepared.set(ses, root);
    if (ses.storagePath) this.prepared.set(ses.storagePath, root);
    ses.setUserAgent(cleanUserAgent(ses.getUserAgent()));
    ses.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !ALLOWED_SCHEME.test(details.url) }));
    ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    ses.setPermissionCheckHandler(() => false);
    if (typeof ses.setDevicePermissionHandler === 'function') ses.setDevicePermissionHandler(() => false);
    for (const event of ['select-hid-device', 'select-serial-port', 'select-usb-device']) {
      ses.on(event, (e, _details, callback) => {
        e.preventDefault();
        callback();
      });
    }
    ses.on('will-download', (e, item, contents) => this.download(e, item, contents, root));
    return ses;
  }

  /** Whether `contents` lives in one of the browser's partitions (index.js asks). */
  owns(contents) {
    const ses = contents && contents.session;
    if (!ses) return false;
    return this.prepared.has(ses) || (Boolean(ses.storagePath) && this.prepared.has(ses.storagePath));
  }

  /** What every page of the browser gets, its popups included. Idempotent. */
  harden(contents) {
    if (this.hardened.has(contents)) return;
    this.hardened.add(contents);
    contents.setWindowOpenHandler((details) => this.openWindow(contents, details));
    contents.on('will-attach-webview', (e) => e.preventDefault());
    contents.on('will-frame-navigate', (e) => {
      if (!NAVIGABLE.test(e.url || '')) e.preventDefault();
    });
    contents.on('certificate-error', (e, _url, _error, _certificate, callback) => {
      e.preventDefault();
      callback(false);
    });
    contents.on('select-bluetooth-device', (e, _devices, callback) => {
      e.preventDefault();
      callback('');
    });
    // A page's «leave this page?» never holds a close or a navigation.
    contents.on('will-prevent-unload', (e) => e.preventDefault());
    // The app's own shortcuts still work while a page has the focus.
    contents.on('before-input-event', (e, input) => this.shortcut(e, input, contents));
  }

  /** Hands one of the app's shortcuts typed in a page to the window, as if typed there. */
  shortcut(e, input, contents) {
    const tab = this.tabOf(contents);
    // A key the model presses in its tab is the page's, whatever it is.
    if (tab && tab.driving) return;
    const keys = shortcutOf(input, this.platform);
    if (!keys) return;
    e.preventDefault();
    const ui = this.window.webContents;
    if (this.window.isDestroyed() || ui.isDestroyed()) return;
    ui.focus();
    ui.send('browser:shortcut', keys);
  }

  download(e, item, contents, root) {
    let target;
    try {
      target = claimName(downloadsFolder(root), safeName(path.basename(item.getFilename() || '')));
    } catch (error) {
      this.log(`download refused: ${error.message}`);
      e.preventDefault();
      return;
    }
    item.setSavePath(target);
    const tab = this.tabOf(contents);
    const note = { path: target, filename: path.basename(target), state: 'progressing' };
    if (tab) tab.driver.note('download', note);
    item.once('done', (_event, state) => {
      note.state = state;
      // The empty file that held the name goes with a download that did not finish.
      if (state === 'completed') return;
      try {
        const stat = fs.lstatSync(target);
        if (stat.isFile() && stat.size === 0) fs.unlinkSync(target);
      } catch {
        // already gone
      }
    });
  }

  // -- tabs -------------------------------------------------------------------------------

  tabOf(contents) {
    if (!contents) return null;
    for (const tab of this.tabs.values()) if (tab.wc === contents) return tab;
    return null;
  }

  /** A new tab; `webContents` adopts a popup's. Never shown here: the window decides. */
  create({ webContents = null, webPreferences = null } = {}) {
    if (!this.root) throw new BrowserFailure('no_tab', 'no project is open');
    if (this.tabs.size >= MAX_TABS) throw new BrowserFailure('too_many_tabs', `at most ${MAX_TABS} tabs; close one first`);
    const { WebContentsView } = this.electron;
    let view;
    if (webContents) {
      view = new WebContentsView({ webContents, webPreferences: webPreferences || {} });
    } else {
      const ses = this.sessionFor(this.root);
      view = new WebContentsView({ webPreferences: { ...TAB_PREFERENCES, session: ses } });
    }
    const wc = view.webContents;
    this.harden(wc);
    this.seq += 1;
    const id = `b${this.seq}`;
    const tab = {
      id,
      view,
      wc,
      favicon: null,
      failed: null,
      driving: 0,
      driver: new TabDriver({ wc, fresh: !webContents, log: this.log, ...this.driverOptions }),
    };
    this.tabs.set(id, tab);
    view.setBounds(this.rect);
    view.setVisible(false);
    this.window.contentView.addChildView(view);
    const changed = () => this.broadcast();
    for (const event of ['did-start-loading', 'did-stop-loading', 'page-title-updated', 'did-navigate', 'did-navigate-in-page']) {
      wc.on(event, changed);
    }
    wc.on('page-favicon-updated', (_e, favicons) => {
      tab.favicon = (favicons || []).find((url) => /^(https?|data):/i.test(url)) || null;
      changed();
    });
    // The last main-frame load that failed, until the next navigation starts.
    wc.on('did-fail-load', (_e, code, description, _url, isMainFrame) => {
      if (isMainFrame === false || code === ERR_ABORTED) return;
      tab.failed = { code, description: String(description || '') };
      changed();
    });
    wc.on('did-start-navigation', (details, _url, isInPlace, isMainFrame) => {
      const main = details && typeof details.isMainFrame === 'boolean' ? details.isMainFrame : isMainFrame;
      const same = details && typeof details.isSameDocument === 'boolean' ? details.isSameDocument : isInPlace;
      if (main && !same && tab.failed) {
        tab.failed = null;
        changed();
      }
    });
    wc.on('render-process-gone', () => {
      tab.driver.reset();
      changed();
    });
    wc.once('destroyed', () => {
      if (this.tabs.get(id) === tab) this.forget(tab);
    });
    this.broadcast();
    return tab;
  }

  openWindow(opener, details) {
    if (!NAVIGABLE.test(details.url || 'about:blank') || this.tabs.size >= MAX_TABS || !this.root) return { action: 'deny' };
    return {
      action: 'allow',
      createWindow: (options) => {
        const tab = this.create({ webContents: options.webContents, webPreferences: options.webPreferences });
        tab.wc.once('did-finish-load', () => tab.driver.ensure().catch((error) => this.log(`popup: ${error.message}`)));
        const from = this.tabOf(opener);
        // A popup of the model's tab is where the model goes on.
        if (from && from.id === this.modelTab) this.modelTab = tab.id;
        return tab.wc;
      },
    };
  }

  forget(tab) {
    this.tabs.delete(tab.id);
    if (this.modelTab === tab.id) this.modelTab = null;
    if (this.visible === tab.id) this.visible = null;
    tab.driver.dispose();
    try {
      this.window.contentView.removeChildView(tab.view);
    } catch {
      // the window is gone
    }
    this.broadcast();
  }

  close(id) {
    const tab = this.tabs.get(id);
    if (!tab) return false;
    this.forget(tab);
    if (!tab.wc.isDestroyed()) tab.wc.close();
    return true;
  }

  closeAll() {
    for (const id of [...this.tabs.keys()]) this.close(id);
    this.seq = 0;
    this.modelTab = null;
    this.broadcast();
  }

  /** A new session or project: its tabs start from nothing. */
  sessionChanged(sessionId, root) {
    if (sessionId === this.sessionId && root === this.root) return;
    this.closeAll();
    this.sessionId = sessionId;
    this.root = root;
  }

  list() {
    return [...this.tabs.values()].map((tab) => {
      const { wc } = tab;
      if (wc.isDestroyed()) return { id: tab.id, title: '', url: '', loading: false, canBack: false, canForward: false };
      const entry = {
        id: tab.id,
        title: wc.getTitle(),
        url: wc.getURL(),
        loading: wc.isLoading(),
        canBack: wc.navigationHistory.canGoBack(),
        canForward: wc.navigationHistory.canGoForward(),
      };
      if (tab.favicon) entry.favicon = tab.favicon;
      if (tab.failed) entry.failed = tab.failed;
      return entry;
    });
  }

  broadcast() {
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.onTabs(this.list());
    }, BROADCAST_MS);
  }

  /** Shows `id` in `rect` (CSS px of the window's page) and hides every other view; null hides all. */
  setBounds(id, rect) {
    if (rect) {
      const zoom = this.window.webContents.getZoomFactor();
      this.rect = {
        x: Math.round(rect.x * zoom),
        y: Math.round(rect.y * zoom),
        width: Math.max(0, Math.round(rect.width * zoom)),
        height: Math.max(0, Math.round(rect.height * zoom)),
      };
    }
    this.visible = id && rect && this.tabs.has(id) ? id : null;
    for (const tab of this.tabs.values()) {
      // Hidden views keep the same size, so a page the model reads is laid out as if seen.
      tab.view.setBounds(this.rect);
      tab.view.setVisible(tab.id === this.visible);
    }
  }

  // -- the window's controls --------------------------------------------------------------

  async openFromWindow(text) {
    let url = null;
    if (text !== undefined && text !== null && text !== '') {
      const normalized = normalizeAddress(text);
      if (normalized.error) return { error: normalized.error };
      url = normalized.url;
    }
    let tab;
    try {
      tab = this.create();
      await tab.driver.ensure();
    } catch (error) {
      this.log(`new tab: ${error.message}`);
      return { error: 'failed' };
    }
    if (url) tab.wc.loadURL(url).catch(() => {});
    return { id: tab.id };
  }

  navigateFromWindow(id, text) {
    const tab = this.tabs.get(id);
    if (!tab || tab.wc.isDestroyed()) return { error: 'failed' };
    const normalized = normalizeAddress(text);
    if (normalized.error) return { error: normalized.error };
    tab.wc.loadURL(normalized.url).catch(() => {});
    return { url: normalized.url };
  }

  control(id, action) {
    const tab = this.tabs.get(id);
    if (!tab || tab.wc.isDestroyed()) return { error: 'no such tab' };
    const { wc } = tab;
    if (action === 'back' && wc.navigationHistory.canGoBack()) wc.navigationHistory.goBack();
    else if (action === 'forward' && wc.navigationHistory.canGoForward()) wc.navigationHistory.goForward();
    else if (action === 'reload') wc.reload();
    else if (action === 'stop') wc.stop();
    return { ok: true };
  }

  // -- the model's operations -------------------------------------------------------------

  where(tab) {
    const destroyed = tab.wc.isDestroyed();
    return { tab: tab.id, title: destroyed ? '' : tab.wc.getTitle(), url: destroyed ? '' : tab.wc.getURL() };
  }

  /** The tab an op means: the one named, else the model's last. */
  target(args) {
    if (args.tab !== undefined && args.tab !== null && args.tab !== '') {
      const tab = typeof args.tab === 'string' ? this.tabs.get(args.tab) : null;
      if (!tab) throw new BrowserFailure('no_tab', `there is no tab ${String(args.tab).slice(0, 20)}`);
      return tab;
    }
    return this.modelTab ? this.tabs.get(this.modelTab) || null : null;
  }

  modelList() {
    return this.list().map((entry) => ({ tab: entry.id, title: entry.title, url: entry.url, current: entry.id === this.modelTab }));
  }

  /** Runs one of the core's operations; throws BrowserFailure. */
  async command(op, args) {
    if (typeof op !== 'string' || !OPS.has(op)) throw new BrowserFailure('bad_args', `unknown operation ${String(op).slice(0, 40)}`);
    if (args === undefined || args === null) args = {};
    if (typeof args !== 'object' || Array.isArray(args)) throw new BrowserFailure('bad_args', 'args must be an object');
    if (op === 'tabs') {
      if (args.select !== undefined && args.select !== null && args.select !== '') {
        const tab = typeof args.select === 'string' ? this.tabs.get(args.select) : null;
        if (!tab) throw new BrowserFailure('no_tab', `there is no tab ${String(args.select).slice(0, 20)}`);
        this.modelTab = tab.id;
      }
      return { tabs: this.modelList() };
    }
    if (op === 'close') {
      if (typeof args.tab !== 'string' || !this.tabs.has(args.tab)) throw new BrowserFailure('no_tab', 'close needs the id of an open tab');
      this.close(args.tab);
      return { tabs: this.modelList() };
    }
    if (op === 'navigate') {
      const current = args.new_tab === true ? (this.modelTab && this.tabs.get(this.modelTab)) || null : this.target(args);
      const url = webUrl(args.url, current && !current.wc.isDestroyed() ? current.wc.getURL() : '');
      let tab = args.new_tab === true ? null : current;
      if (!tab) tab = this.create();
      this.modelTab = tab.id;
      const result = await this.drive(tab, op, () => tab.driver.navigate(url, args.snapshot === true));
      return this.answer(tab, result);
    }
    const tab = this.target(args);
    if (!tab) throw new BrowserFailure('no_tab', 'there is no tab yet');
    if (tab.wc.isDestroyed()) throw new BrowserFailure('no_tab', `tab ${tab.id} was closed`);
    this.modelTab = tab.id;
    const result = await this.drive(tab, op, () => tab.driver.run(op, args));
    // A click that opened a popup moved the model to it. The snapshot taken was the
    // opener's, so it is left out: the core asks the new tab for its own.
    const opened = this.modelTab !== tab.id && this.tabs.get(this.modelTab);
    if (opened) {
      const rest = { ...result };
      delete rest.tree;
      delete rest.snapshot_error;
      return fit({ ...this.where(opened), ...tab.driver.takeNotes(), ...rest, opened: opened.id });
    }
    return this.answer(tab, result);
  }

  /**
   * Runs `fn`, an op on `tab`. While it runs, keys in the tab are the page's; after an op that
   * may move the focus, a tab the person is not looking at gives it back to the window.
   */
  async drive(tab, op, fn) {
    const ui = this.window.webContents;
    const watch = FOCUS_OPS.has(op) && ui && typeof ui.isFocused === 'function';
    const hadFocus = watch && !ui.isDestroyed() && ui.isFocused();
    // The person may be typing in the tab they look at while the model works in another.
    const seen = this.visible && this.tabs.get(this.visible);
    const seenHad = watch && !hadFocus && seen && seen !== tab && this.seenFocused();
    tab.driving += 1;
    try {
      return await fn();
    } finally {
      tab.driving -= 1;
      if (hadFocus && !this.window.isDestroyed() && !ui.isDestroyed() && !ui.isFocused() && !this.seenFocused()) {
        ui.focus();
      } else if (seenHad && !seen.wc.isDestroyed() && !seen.wc.isFocused()) {
        seen.wc.focus();
      }
    }
  }

  /** Whether the tab the person is looking at has the focus (an op there may take it). */
  seenFocused() {
    const tab = this.visible && this.tabs.get(this.visible);
    return Boolean(tab && !tab.wc.isDestroyed() && typeof tab.wc.isFocused === 'function' && tab.wc.isFocused());
  }

  /** The tab's title and address, then the op's own fields: a request's or a script's `url`
   * in `network` and `scripts` with an id is theirs, not the page's. */
  answer(tab, result) {
    return fit({ ...this.where(tab), ...tab.driver.takeNotes(), ...result });
  }
}

module.exports = {
  BrowserHost,
  OPS,
  ALLOWED_SCHEME,
  TAB_PREFERENCES,
  MAX_RESULT,
  partitionFor,
  cleanUserAgent,
  cleanAppUserAgent,
  safeName,
  downloadsFolder,
  normalizeAddress,
  webUrl,
  fit,
};
