'use strict';

/**
 * The fixed operations on one browser tab, over that tab's own `webContents.debugger`.
 *
 * Nothing the core or the model sends reaches the debugger as a method, a session id or a
 * raw message: each operation is a handler here that builds its own calls, and `send`
 * refuses any method outside `METHODS`. The only session ids used are the ones this tab's
 * own `Target.setAutoAttach` announced for its child frames. A view's debugger can close
 * the app's window and read the UI's cookies (cogito-lab
 * research/2026-11-05-native-browser/electron.md), which is why the set is closed.
 *
 * Refs and the snapshot come from Playwright's injected script (`injected-script.js`), run
 * in an isolated world per frame and made again after each navigation.
 *
 * After an action the page is given time to settle: until its DOM has not changed for
 * `QUIET_MS` and no fetch the action started is open, at most `SETTLE_MS` (Playwright's MCP
 * backend waits the same way, on requests and a fixed settle; packages/playwright-core/src/
 * tools/backend/utils.ts, read 2026-11-10). The world's observer records the last change;
 * main polls it, since a hidden tab's own timers are throttled.
 */

const INJECTED = require('./injected-script');

/** Every CDP method this file sends. No Target.* but the auto-attach, no Browser.*,
 * Storage.*, Fetch.*, Security.*, IO, Tracing, Memory, breakpoints or pauses. */
const METHODS = Object.freeze(
  new Set([
    'Target.setAutoAttach',
    'Page.enable',
    'Page.setLifecycleEventsEnabled',
    'Page.getFrameTree',
    'Page.navigate',
    'Page.createIsolatedWorld',
    'Page.handleJavaScriptDialog',
    'Runtime.enable',
    'Runtime.evaluate',
    'Runtime.callFunctionOn',
    'Runtime.releaseObjectGroup',
    'DOM.enable',
    'DOM.getDocument',
    'DOM.getOuterHTML',
    'DOM.describeNode',
    'DOM.resolveNode',
    'DOM.scrollIntoViewIfNeeded',
    'DOM.getContentQuads',
    'DOM.getBoxModel',
    'DOM.getNodeForLocation',
    'DOM.getFrameOwner',
    'DOM.focus',
    'Network.enable',
    'Network.getResponseBody',
    'Network.getRequestPostData',
    'Debugger.enable',
    'Debugger.setSkipAllPauses',
    'Debugger.resume',
    'Debugger.getScriptSource',
    'Input.dispatchMouseEvent',
    'Input.dispatchKeyEvent',
    'Input.insertText',
    'Emulation.setFocusEmulationEnabled',
  ]),
);

/** The operations a tab answers here; `navigate`, `tabs` and `close` belong to the host. */
const TAB_OPS = Object.freeze(
  new Set([
    'snapshot',
    'click',
    'hover',
    'type',
    'select',
    'press',
    'scroll',
    'screenshot',
    'back',
    'forward',
    'reload',
    'wait',
    'source',
    'dom',
    'scripts',
    'network',
    'console',
    'storage',
    'evaluate',
    'steps',
    'read',
  ]),
);

/** Ops whose answer may carry the page's snapshot when asked (`snapshot: true`). */
const SNAPSHOT_AFTER = new Set(['click', 'hover', 'type', 'select', 'press', 'scroll', 'back', 'forward', 'reload', 'wait', 'steps']);
/** What one call of `steps` may do, and how many. */
const STEP_ACTIONS = new Set(['click', 'type', 'select', 'press', 'check', 'scroll', 'wait']);
const MAX_STEPS = 10;
const STEP_WAIT_MAX = 5;

const REF = /^(e\d{1,7}|f\d{1,4}e\d{1,7})$/;
const COMMAND_MS = 10000;
const LOAD_MS = 30000;
// Each op answers before the core stops waiting for it (navigate 35 s, the rest 15 s).
const NAVIGATE_MS = 32000;
const OP_MS = 12000;
// `steps` may run ten actions, each settling, and one of them may load a page.
const STEPS_MS = 32000;
// The page is settled once its DOM has been quiet this long, waited for at most SETTLE_MS.
const QUIET_MS = 250;
const SETTLE_MS = 1500;
const QUIET_POLL_MS = 50;
// Characters of the page's text `read` holds at most, before paging.
const READ_MAX = 2000000;
// Once the document is parsed, the rest of the load is waited for this long at most.
const AFTER_DOM_MS = 3000;
const NETWORK_RING = 300;
const CONSOLE_RING = 500;
const CONSOLE_TEXT = 2000;
const MAX_FRAMES = 10;
// Chromium's buffer for response bodies, per tab: `source` and `network` read from it.
const NETWORK_CAPS = { maxTotalBufferSize: 32 * 1024 * 1024, maxResourceBufferSize: 8 * 1024 * 1024 };
const OPTIONS = {
  isUnderTest: false,
  sdkLanguage: 'javascript',
  testIdAttributeName: 'data-testid',
  stableRafCount: 1,
  browserName: 'chromium',
  shouldPrependErrorPrefix: false,
  isUtilityWorld: true,
  customEngines: [],
};
// What Electron itself adds to every page of an unpackaged app; not the page's own words.
const ELECTRON_NOISE = /Electron Security Warning/;

/** An answer the core reads as `{ok: false, kind, error}`. */
class BrowserFailure extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

const fail = (kind, message) => {
  throw new BrowserFailure(kind, message);
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new BrowserFailure('timeout', `${what} took longer than ${ms / 1000} s`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

const staleRef = (ref) => new BrowserFailure('stale_ref', `${ref} is not in the latest snapshot; take a new snapshot`);

// Records when the document last changed: nodes added or removed, or text. Attributes are
// left out, so an animation that only restyles never keeps the page unsettled.
const OBSERVE = `{
  const quiet = { last: performance.now() };
  new MutationObserver(() => { quiet.last = performance.now(); }).observe(document, { subtree: true, childList: true, characterData: true });
  globalThis.__cogitoQuiet = quiet;
}`;
// How long the document has been unchanged, in ms; null when the observer is missing.
const QUIET = '(() => { const q = globalThis.__cogitoQuiet; return q ? performance.now() - q.last : null; })()';

function injection(frameSeq) {
  const options = JSON.stringify({ ...OPTIONS, frameSeq });
  return `(() => { const module = {}; ${INJECTED}\nglobalThis.__cogito = new (module.exports.InjectedScript())(globalThis, ${options}); try ${OBSERVE} catch {} return true; })()`;
}

// The tree of one frame, and the refs of the iframes in it.
const SNAPSHOT = `(() => {
  const root = document.body || document.documentElement;
  if (!root) return { tree: '', iframes: [] };
  const tree = __cogito.ariaSnapshot(root, { mode: 'ai' });
  const last = __cogito._lastAriaSnapshotForQuery;
  return { tree, iframes: last && last.iframeRefs ? [...last.iframeRefs] : [] };
})()`;
const FIND_REF = 'function (ref) { return __cogito.querySelector(__cogito.parseSelector("aria-ref=" + ref), document, false) || null; }';
const CONTAINS = 'function (node) { for (let n = node; n; n = n.parentNode || n.host) if (n === this) return true; return false; }';
const DESCRIBE = `function () {
  const e = this.nodeType === 1 ? this : this.parentElement;
  if (!e) return 'something else';
  const classes = typeof e.className === 'string' ? e.className.trim().split(/\\s+/).filter(Boolean) : [];
  const d = '<' + e.localName + (e.id ? '#' + e.id : '') + classes.map((c) => '.' + c).join('') + '>';
  const text = (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ');
  return text ? d + ' "' + text.slice(0, 60) + '"' : d;
}`;
const SELECT_ALL = `function () {
  if ((this.localName === 'input' || this.localName === 'textarea') && typeof this.select === 'function') this.select();
  else if (this.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(this);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  }
}`;
const SELECT_OPTION = 'function (text) { return __cogito.selectOptions(this, [{ valueOrLabel: text }]); }';
const STORAGE = `(() => {
  const read = (name) => {
    try {
      const store = globalThis[name];
      const out = {};
      for (let i = 0; i < store.length; i++) out[store.key(i)] = store.getItem(store.key(i));
      return out;
    } catch {
      return {};
    }
  };
  return { local: read('localStorage'), session: read('sessionStorage') };
})()`;
// A wheel at the middle of the page, by script: a hidden tab ignores Input wheel events.
const SCROLL = `(delta) => {
  let box = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  for (; box && box !== document.body && box !== document.documentElement; box = box.parentElement) {
    const overflow = getComputedStyle(box).overflowY;
    if ((overflow === 'auto' || overflow === 'scroll') && box.scrollHeight > box.clientHeight) break;
  }
  const page = document.scrollingElement || document.documentElement;
  const target = box && box !== document.body && box !== document.documentElement ? box : page;
  const before = target.scrollTop;
  target.scrollBy({ top: delta, behavior: 'instant' });
  return target.scrollTop !== before;
}`;
const BY_VALUE = 'function () { return this; }';
// Whether a checkbox, radio or switch is checked; null for anything else.
const CHECKED = `function () {
  if (this.localName === 'input' && (this.type === 'checkbox' || this.type === 'radio')) return this.checked;
  const aria = this.getAttribute && this.getAttribute('aria-checked');
  return aria === 'true' ? true : aria === 'false' ? false : null;
}`;
// A native checkbox drawn by its label: the label to click instead.
const LABEL_OF = 'function () { return (this.labels && this.labels[0]) || null; }';

/**
 * The page's readable text: the main content (<main>, one <article>, or the element whose
 * paragraphs hold most of the page's), else the whole body; `full` takes the body. Headings
 * keep their level as #, list items a dash, table cells a bar, and a link its address
 * inline as [text](url), a path for one on the same site. Hidden elements, scripts, styles
 * and form fields are left out (the snapshot shows the fields).
 */
const READ = `(full, max) => {
  const body = document.body || document.documentElement;
  if (!body) return { text: '', from: 'nothing' };
  const shown = (e) => {
    if (e.hidden || (e.getAttribute && e.getAttribute('aria-hidden') === 'true')) return false;
    if (typeof e.checkVisibility !== 'function' || e.checkVisibility({ visibilityProperty: true })) return true;
    return getComputedStyle(e).display === 'contents';
  };
  const size = (e) => (e.innerText || '').length;
  let root = body;
  let from = 'the whole page';
  if (!full) {
    const total = Math.max(1, size(body));
    const largest = (list) => list.filter(shown).sort((a, b) => size(b) - size(a))[0];
    const main = largest([...document.querySelectorAll('main, [role="main"]')]);
    const articles = [...document.querySelectorAll('article')].filter(shown);
    if (main && size(main) >= total * 0.3) {
      root = main;
      from = 'the main content';
    } else if (articles.length === 1 && size(articles[0]) >= total * 0.3) {
      root = articles[0];
      from = 'the article';
    } else {
      // The parent of most of the page's paragraph text, when one parent holds most of it.
      const by = new Map();
      let all = 0;
      for (const p of document.querySelectorAll('p, pre, blockquote')) {
        const n = (p.textContent || '').trim().length;
        all += n;
        if (p.parentElement) by.set(p.parentElement, (by.get(p.parentElement) || 0) + n);
      }
      let best = null;
      let most = 0;
      for (const [e, n] of by) if (n > most) [best, most] = [e, n];
      if (best && best !== body && all >= 500 && most >= all * 0.6 && shown(best)) {
        root = best;
        from = 'the largest block of text';
      }
    }
  }
  const BLOCK = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'CAPTION', 'DD', 'DETAILS', 'DIALOG', 'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'HEADER', 'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'SECTION', 'SUMMARY', 'TABLE', 'TBODY', 'TFOOT', 'THEAD', 'TR', 'UL']);
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'svg', 'CANVAS', 'IFRAME', 'OBJECT', 'EMBED', 'INPUT', 'SELECT', 'TEXTAREA', 'HEAD', 'LINK', 'META']);
  const lines = [];
  let line = '';
  let length = 0;
  let lists = 0;
  const brk = () => {
    const done = line.replace(/[ \\t]+$/, '');
    // A list item with no text leaves its dash alone: no line.
    if (done.trim() && done.trim() !== '-') {
      lines.push(done);
      length += done.length + 1;
    }
    line = '';
  };
  const add = (text) => {
    if (!text) return;
    if (!line.trim()) text = text.replace(/^ +/, '');
    else if (line.endsWith(' ') && text.startsWith(' ')) text = text.slice(1);
    line += text;
  };
  const address = (a) => {
    let url;
    try {
      url = new URL(a.href);
    } catch {
      return '';
    }
    if (url.protocol === 'javascript:') return '';
    return url.origin === location.origin ? url.pathname + url.search + url.hash : url.href;
  };
  const walk = (node) => {
    if (length > max) return;
    if (node.nodeType === 3) {
      add(node.data.replace(/\\s+/g, ' '));
      return;
    }
    if (node.nodeType !== 1 && node.nodeType !== 11) return;
    const tag = node.tagName || '';
    if (node.nodeType === 1) {
      if (SKIP.has(tag) || !shown(node)) return;
      if (tag === 'BR') return brk();
      if (/^H[1-6]$/.test(tag)) {
        brk();
        if (lines.length) lines.push('');
        add('#'.repeat(Number(tag[1])) + ' ' + (node.innerText || '').replace(/\\s+/g, ' ').trim());
        return brk();
      }
      if (tag === 'PRE') {
        brk();
        for (const raw of (node.innerText || '').split('\\n')) {
          lines.push(raw);
          length += raw.length + 1;
        }
        return;
      }
      if (tag === 'A' && node.hasAttribute('href')) {
        const text = (node.innerText || node.getAttribute('aria-label') || node.title || '').replace(/\\s+/g, ' ').trim();
        const url = address(node);
        if (text) add(url ? '[' + text + '](' + url + ')' : text);
        return;
      }
      if (tag === 'IMG') {
        const alt = (node.getAttribute('alt') || '').trim();
        if (alt) add('[image: ' + alt.replace(/\\s+/g, ' ') + ']');
        return;
      }
      if (tag === 'TD' || tag === 'TH') {
        if (line.trim()) add(' | ');
      }
    }
    const block = BLOCK.has(tag);
    if (block) brk();
    if (tag === 'UL' || tag === 'OL') lists += 1;
    // Set, not added: add() drops the leading spaces of a line.
    if (tag === 'LI') line = '  '.repeat(Math.max(0, lists - 1)) + '- ';
    const children = node.nodeType === 1 && node.shadowRoot ? node.shadowRoot.childNodes
      : tag === 'SLOT' && node.assignedNodes({ flatten: true }).length ? node.assignedNodes({ flatten: true })
      : node.childNodes;
    for (const child of children) walk(child);
    if (tag === 'UL' || tag === 'OL') lists -= 1;
    if (block) brk();
  };
  walk(root);
  brk();
  const text = lines.join('\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
  return { text, from, cut: length > max };
}`;
const FUNCTION_CODE = /^\s*(async\s+)?(function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)/;
const TEXT_MIME = /^(text\/|application\/(json|javascript|ecmascript|xml|xhtml\+xml|x-www-form-urlencoded)|image\/svg\+xml)|\+json|\+xml/;

// Playwright's key names: [code, keyCode, text].
const NAMED_KEYS = {
  Enter: ['Enter', 13, '\r'],
  Tab: ['Tab', 9],
  Escape: ['Escape', 27],
  Backspace: ['Backspace', 8],
  Delete: ['Delete', 46],
  Insert: ['Insert', 45],
  Home: ['Home', 36],
  End: ['End', 35],
  PageUp: ['PageUp', 33],
  PageDown: ['PageDown', 34],
  ArrowUp: ['ArrowUp', 38],
  ArrowDown: ['ArrowDown', 40],
  ArrowLeft: ['ArrowLeft', 37],
  ArrowRight: ['ArrowRight', 39],
  Space: ['Space', 32, ' '],
  Shift: ['ShiftLeft', 16],
  Control: ['ControlLeft', 17],
  Alt: ['AltLeft', 18],
  Meta: ['MetaLeft', 91],
};
for (let n = 1; n <= 12; n += 1) NAMED_KEYS[`F${n}`] = [`F${n}`, 111 + n];
const KEY_ALIASES = {
  esc: 'Escape',
  return: 'Enter',
  del: 'Delete',
  up: 'ArrowUp',
  down: 'ArrowDown',
  left: 'ArrowLeft',
  right: 'ArrowRight',
  ctrl: 'Control',
  cmd: 'Meta',
  command: 'Meta',
  option: 'Alt',
  ' ': 'Space',
};
const MODIFIER_BITS = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };

/** One key of a press: `{key, code, keyCode, text}`, or null for an unknown name. */
function keyOf(name) {
  if (name === 'ControlOrMeta') name = process.platform === 'darwin' ? 'Meta' : 'Control';
  const named = Object.keys(NAMED_KEYS).find((k) => k.toLowerCase() === name.toLowerCase()) || KEY_ALIASES[name.toLowerCase()];
  if (named) {
    const [code, keyCode, text] = NAMED_KEYS[named];
    return { key: named === 'Space' ? ' ' : named, code, keyCode, text };
  }
  if ([...name].length !== 1) return null;
  const upper = name.toUpperCase();
  if (/[a-z]/i.test(name)) return { key: name, code: `Key${upper}`, keyCode: upper.charCodeAt(0), text: name };
  if (/[0-9]/.test(name)) return { key: name, code: `Digit${name}`, keyCode: name.charCodeAt(0), text: name };
  return { key: name, code: '', keyCode: 0, text: name };
}

/** `Control+Shift+a` as its modifiers and its last key, or null when a part is unknown. */
function parseKeys(text) {
  const parts = text === '+' ? ['+'] : text.split(/\+(?!$)/);
  const keys = parts.map(keyOf);
  if (!keys.length || keys.some((k) => !k)) return null;
  const main = keys[keys.length - 1];
  const modifiers = keys.slice(0, -1);
  if (modifiers.some((k) => !(k.key in MODIFIER_BITS))) return null;
  return { modifiers, main };
}

const keyFields = (k) => ({ key: k.key, code: k.code, windowsVirtualKeyCode: k.keyCode });
const isPlain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const center = (q) => ({ x: (q[0] + q[2] + q[4] + q[6]) / 4, y: (q[1] + q[3] + q[5] + q[7]) / 4 });
const area = (q) => {
  let twice = 0;
  for (let i = 0; i < 8; i += 2) twice += q[i] * q[(i + 3) % 8] - q[i + 1] * q[(i + 2) % 8];
  return Math.abs(twice) / 2;
};

function argString(args, name, { max = 100000, required = true } = {}) {
  const value = args[name];
  if (value === undefined || value === null || value === '') {
    if (required) fail('bad_args', `${name} is required`);
    return null;
  }
  if (typeof value !== 'string' || value.length > max) fail('bad_args', `${name} must be a string of at most ${max} characters`);
  return value;
}

function argRef(args, { required = true } = {}) {
  const ref = args.ref;
  if (ref === undefined || ref === null || ref === '') {
    if (required) fail('bad_args', 'ref is required: an element ref from the latest snapshot, such as e12');
    return null;
  }
  if (typeof ref !== 'string' || !REF.test(ref)) fail('bad_args', 'a ref looks like e12 or f1e3, from the latest snapshot');
  return ref;
}

/** Indent the frame's tree under its iframe line in the parent's tree. */
function splice(tree, ref, child) {
  if (!child.trim()) return tree;
  const lines = tree.split('\n');
  const at = lines.findIndex((line) => line.includes(`[ref=${ref}]`));
  if (at < 0) return tree;
  const indent = lines[at].match(/^\s*/)[0];
  if (!lines[at].endsWith(':')) lines[at] += ':';
  const inner = child.split('\n').map((line) => `${indent}  ${line}`);
  lines.splice(at + 1, 0, ...inner);
  return lines.join('\n');
}

/**
 * One tab's debugger and what it has seen: network requests and console messages since the
 * last navigation (bounded rings), scripts once asked for, and the injected script's worlds.
 */
class TabDriver {
  constructor({
    wc,
    fresh = true,
    commandMs = COMMAND_MS,
    loadMs = LOAD_MS,
    navigateMs = NAVIGATE_MS,
    opMs = OP_MS,
    stepsMs = STEPS_MS,
    quietMs = QUIET_MS,
    settleMs = SETTLE_MS,
    log = () => {},
  }) {
    this.wc = wc;
    // A tab of ours that never navigated loads about:blank before attaching (the probes
    // hung otherwise). An adopted popup is left to load what its opener asked for.
    this.fresh = fresh;
    this.commandMs = commandMs;
    this.loadMs = loadMs;
    this.navigateMs = navigateMs;
    this.opMs = opMs;
    this.stepsMs = stepsMs;
    this.quietMs = quietMs;
    this.settleMs = settleMs;
    // When the running op must answer by; every call and wait inside it stops there.
    this.deadline = null;
    // When the main document of the current navigation was parsed, or null.
    this.domReadyAt = null;
    this.log = log;
    this.attached = false;
    this.attaching = null;
    this.mainFrameId = null;
    this.sessions = new Map(); // child session id -> target (frame) id
    this.worlds = new Map(); // `${session}|${frameId}` -> execution context id
    this.frameSeqs = new Map(); // frame id -> seq, so `fNeM` refs stay valid
    this.frames = new Map(); // seq -> {frameId, sessionId}
    this.nextSeq = 1;
    this.requests = [];
    this.byRequest = new Map();
    this.requestSeq = 0;
    this.document = null;
    this.console = [];
    this.scripts = new Map();
    this.scriptSeq = 0;
    this.debuggerOn = false;
    this.lifecycle = new Set();
    this.navStarts = 0;
    this.notes = {};
    // Electron gives '' as the session of the tab's own target. A listener must never throw:
    // Electron shows an uncaught error in main as a modal box, which stalls the app.
    this.onMessage = (_event, method, params, sessionId) => {
      try {
        this.event(method, params || {}, sessionId || undefined);
      } catch (error) {
        this.log(`${method}: ${error.message}`);
      }
    };
    this.onDetach = () => this.reset();
    this.onNavigation = (details, url, isInPlace, isMainFrame) => {
      const main = details && typeof details.isMainFrame === 'boolean' ? details.isMainFrame : isMainFrame;
      const same = details && typeof details.isSameDocument === 'boolean' ? details.isSameDocument : isInPlace;
      if (main && !same) {
        this.navStarts += 1;
        this.domReadyAt = null;
      }
    };
    wc.on('did-start-navigation', this.onNavigation);
  }

  // -- the debugger ---------------------------------------------------------------------

  /** The only way a CDP call leaves this file. */
  send(method, params = {}, sessionId = undefined, ms = this.commandMs) {
    if (!METHODS.has(method)) throw new Error(`${method} is not an operation of the browser`);
    if (sessionId !== undefined && !this.sessions.has(sessionId)) throw new Error('unknown session');
    if (this.wc.isDestroyed()) return Promise.reject(new BrowserFailure('no_tab', 'the tab was closed'));
    const call = this.wc.debugger.sendCommand(method, params, sessionId);
    const left = this.deadline === null ? Infinity : this.deadline - Date.now();
    const limit = Math.min(ms, left);
    return withTimeout(call, Number.isFinite(limit) ? Math.max(250, limit) : this.commandMs, method);
  }

  async ensure() {
    if (this.attached) return;
    if (!this.attaching) {
      this.attaching = this.attach().finally(() => {
        this.attaching = null;
      });
    }
    await this.attaching;
  }

  async attach() {
    const { wc } = this;
    if (wc.isDestroyed()) fail('no_tab', 'the tab was closed');
    if (!wc.getURL()) {
      if (this.fresh) await wc.loadURL('about:blank').catch(() => {});
      else await this.until(() => wc.getURL() || wc.isDestroyed(), 5000);
    }
    const dbg = wc.debugger;
    if (!dbg.isAttached()) dbg.attach('1.3');
    dbg.removeListener('message', this.onMessage);
    dbg.removeListener('detach', this.onDetach);
    dbg.on('message', this.onMessage);
    dbg.on('detach', this.onDetach);
    this.attached = true;
    try {
      await this.send('Page.enable');
      await this.send('Page.setLifecycleEventsEnabled', { enabled: true });
      await this.send('Runtime.enable');
      await this.send('Network.enable', NETWORK_CAPS);
      await this.send('DOM.enable');
      // A hidden tab still takes input as if it had focus.
      await this.send('Emulation.setFocusEmulationEnabled', { enabled: true });
      await this.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
      const { frameTree } = await this.send('Page.getFrameTree');
      this.mainFrameId = frameTree.frame.id;
    } catch (error) {
      this.attached = false;
      throw error;
    }
  }

  /** The debugger went away (the tab closed or crashed): start over on the next op. */
  reset() {
    this.attached = false;
    this.sessions.clear();
    this.worlds.clear();
    this.debuggerOn = false;
  }

  dispose() {
    this.wc.removeListener('did-start-navigation', this.onNavigation);
    if (this.wc.isDestroyed()) return;
    const dbg = this.wc.debugger;
    dbg.removeListener('message', this.onMessage);
    dbg.removeListener('detach', this.onDetach);
    try {
      if (dbg.isAttached()) dbg.detach();
    } catch {
      // already gone
    }
  }

  async until(test, ms) {
    const end = Date.now() + ms;
    while (!test()) {
      if (Date.now() > end) return false;
      await sleep(50);
    }
    return true;
  }

  // -- events ---------------------------------------------------------------------------

  event(method, params, sessionId) {
    switch (method) {
      case 'Target.attachedToTarget': {
        // Only this tab's own child frames; nothing else gets a session.
        const info = params.targetInfo || {};
        if (info.type !== 'iframe' || typeof params.sessionId !== 'string') return;
        this.sessions.set(params.sessionId, info.targetId);
        const child = params.sessionId;
        for (const [name, args] of [['Runtime.enable', {}], ['Page.enable', {}], ['Network.enable', NETWORK_CAPS]]) {
          this.send(name, args, child).catch(() => {});
        }
        return;
      }
      case 'Target.detachedFromTarget':
        this.sessions.delete(params.sessionId);
        this.dropWorlds((key) => key.startsWith(`${params.sessionId}|`));
        return;
      case 'Page.frameNavigated': {
        const frame = params.frame || {};
        if (!sessionId && !frame.parentId) {
          this.mainFrameId = frame.id;
          this.console = [];
          this.scripts.clear();
          this.dropWorlds((key) => key.startsWith('|'));
        } else {
          this.dropWorlds((key) => key === `${sessionId || ''}|${frame.id}`);
        }
        return;
      }
      case 'Runtime.executionContextsCleared':
        this.dropWorlds((key) => key.startsWith(`${sessionId || ''}|`));
        return;
      case 'Runtime.executionContextDestroyed':
        this.dropWorlds((key, id) => id === params.executionContextId && key.startsWith(`${sessionId || ''}|`));
        return;
      case 'Page.lifecycleEvent':
        if (!sessionId && params.frameId === this.mainFrameId) {
          this.lifecycle.add(`${params.loaderId}:${params.name}`);
          if (params.name === 'DOMContentLoaded') this.domReadyAt = Date.now();
        }
        return;
      case 'Page.javascriptDialogOpening':
        // Dismissed at once, so no turn waits on a dialog; the next result says what it was.
        this.notes.dialog = { type: params.type, message: String(params.message || '').slice(0, CONSOLE_TEXT) };
        this.send('Page.handleJavaScriptDialog', { accept: params.type === 'beforeunload' }, sessionId).catch(() => {});
        return;
      case 'Network.requestWillBeSent':
        this.requestSent(params, sessionId);
        return;
      case 'Network.responseReceived': {
        const entry = this.byRequest.get(`${sessionId || ''}|${params.requestId}`);
        if (!entry) return;
        const response = params.response || {};
        entry.status = response.status;
        entry.mime = response.mimeType;
        entry.responseHeaders = response.headers || {};
        if (params.type) entry.type = params.type.toLowerCase();
        return;
      }
      case 'Network.loadingFinished': {
        const entry = this.byRequest.get(`${sessionId || ''}|${params.requestId}`);
        if (entry) entry.size = params.encodedDataLength;
        return;
      }
      case 'Network.loadingFailed': {
        const entry = this.byRequest.get(`${sessionId || ''}|${params.requestId}`);
        if (entry) entry.failed = params.errorText || 'failed';
        return;
      }
      case 'Runtime.consoleAPICalled': {
        const text = (params.args || [])
          .map((a) => {
            if (a.value === undefined) return a.unserializableValue || a.description || a.type;
            return typeof a.value === 'string' ? a.value : JSON.stringify(a.value);
          })
          .join(' ');
        const frame = params.stackTrace && params.stackTrace.callFrames && params.stackTrace.callFrames[0];
        const level = { warning: 'warning', error: 'error', assert: 'error', info: 'info', debug: 'debug' }[params.type] || 'log';
        this.logLine(level, text, frame && frame.url, frame ? frame.lineNumber + 1 : undefined);
        return;
      }
      case 'Runtime.exceptionThrown': {
        const d = params.exceptionDetails || {};
        const text = (d.exception && d.exception.description) || d.text || 'exception';
        this.logLine('error', text, d.url, d.lineNumber !== undefined ? d.lineNumber + 1 : undefined);
        return;
      }
      case 'Debugger.scriptParsed': {
        const url = params.url || '';
        const aux = params.executionContextAuxData || {};
        // Electron's own bundles, eval'd snippets and our isolated world are not the page's.
        if (sessionId || !url || url.startsWith('node:') || /^electron/.test(url) || aux.type === 'isolated') return;
        this.scriptSeq += 1;
        this.scripts.set(`s${this.scriptSeq}`, { scriptId: params.scriptId, url, length: params.length ?? null });
        return;
      }
      case 'Debugger.paused':
        this.send('Debugger.resume', {}).catch(() => {});
        return;
      default:
    }
  }

  dropWorlds(test) {
    for (const [key, id] of this.worlds) if (test(key, id)) this.worlds.delete(key);
  }

  requestSent(params, sessionId) {
    const request = params.request || {};
    const key = `${sessionId || ''}|${params.requestId}`;
    const previous = this.byRequest.get(key);
    if (previous && params.redirectResponse) {
      previous.redirected = true;
      previous.status = params.redirectResponse.status;
      previous.responseHeaders = params.redirectResponse.headers || {};
      previous.mime = params.redirectResponse.mimeType;
    }
    const mainDocument = !sessionId && params.type === 'Document' && params.frameId === this.mainFrameId;
    if (mainDocument && !params.redirectResponse) {
      this.requests = [];
      this.byRequest.clear();
    }
    this.requestSeq += 1;
    const entry = {
      id: `r${this.requestSeq}`,
      seq: this.requestSeq,
      requestId: params.requestId,
      sessionId,
      method: request.method || 'GET',
      url: request.url || '',
      type: (params.type || 'Other').toLowerCase(),
      status: null,
      size: null,
      requestHeaders: request.headers || {},
      hasPostData: Boolean(request.hasPostData || request.postData),
    };
    if (mainDocument) this.document = { entry, loaderId: params.loaderId };
    this.requests.push(entry);
    this.byRequest.set(key, entry);
    while (this.requests.length > NETWORK_RING) {
      const old = this.requests.shift();
      const oldKey = `${old.sessionId || ''}|${old.requestId}`;
      if (this.byRequest.get(oldKey) === old) this.byRequest.delete(oldKey);
    }
  }

  logLine(level, text, url, line) {
    if (ELECTRON_NOISE.test(text)) return;
    const entry = { level, text: text.length > CONSOLE_TEXT ? `${text.slice(0, CONSOLE_TEXT)}…` : text };
    if (url) entry.url = url;
    if (line !== undefined) entry.line = line;
    this.console.push(entry);
    if (this.console.length > CONSOLE_RING) this.console.shift();
  }

  note(name, value) {
    this.notes[name] = value;
  }

  /** The dialog dismissed and the download saved since the last result, once. */
  takeNotes() {
    const notes = this.notes;
    this.notes = {};
    return notes;
  }

  // -- frames and refs --------------------------------------------------------------------

  mainFrame() {
    return { frameId: this.mainFrameId, sessionId: undefined };
  }

  /** The isolated world of `frame`, with the injected script in it. */
  async world(frame) {
    const key = `${frame.sessionId || ''}|${frame.frameId}`;
    if (this.worlds.has(key)) return this.worlds.get(key);
    const seq = frame.sessionId === undefined && frame.frameId === this.mainFrameId ? 0 : this.seqOf(frame);
    const { executionContextId } = await this.send(
      'Page.createIsolatedWorld',
      { frameId: frame.frameId, worldName: 'cogito', grantUniveralAccess: false },
      frame.sessionId,
    );
    const made = await this.send(
      'Runtime.evaluate',
      { contextId: executionContextId, expression: injection(seq), returnByValue: true },
      frame.sessionId,
    );
    if (made.exceptionDetails) fail('failed', 'the page refused the snapshot script');
    this.worlds.set(key, executionContextId);
    return executionContextId;
  }

  seqOf(frame) {
    let seq = this.frameSeqs.get(frame.frameId);
    if (!seq) {
      seq = this.nextSeq;
      this.nextSeq += 1;
      this.frameSeqs.set(frame.frameId, seq);
    }
    this.frames.set(seq, { frameId: frame.frameId, sessionId: frame.sessionId });
    return seq;
  }

  /** `expression`'s value in `frame`'s world; a world lost to a navigation is made again once. */
  async inWorld(frame, expression) {
    for (let attempt = 0; ; attempt += 1) {
      const contextId = await this.world(frame);
      try {
        const answer = await this.send(
          'Runtime.evaluate',
          { contextId, expression, returnByValue: true, awaitPromise: true },
          frame.sessionId,
        );
        if (answer.exceptionDetails) fail('failed', exceptionOf(answer.exceptionDetails).message);
        return answer.result.value;
      } catch (error) {
        if (attempt || !/context/i.test(error.message)) throw error;
        this.worlds.delete(`${frame.sessionId || ''}|${frame.frameId}`);
      }
    }
  }

  /** The element behind a ref: `{ref, frame, objectId}`. */
  async resolve(ref) {
    const seq = /^f(\d+)e/.exec(ref);
    const frame = seq ? this.frames.get(Number(seq[1])) : this.mainFrame();
    if (!frame) throw staleRef(ref);
    const contextId = this.worlds.get(`${frame.sessionId || ''}|${frame.frameId}`);
    if (contextId === undefined) throw staleRef(ref);
    let answer;
    try {
      answer = await this.send(
        'Runtime.callFunctionOn',
        { executionContextId: contextId, functionDeclaration: FIND_REF, arguments: [{ value: ref }] },
        frame.sessionId,
      );
    } catch (error) {
      if (error instanceof BrowserFailure && error.kind !== 'failed') throw error;
      throw staleRef(ref);
    }
    if (answer.exceptionDetails || !answer.result || !answer.result.objectId) throw staleRef(ref);
    return { ref, frame, objectId: answer.result.objectId };
  }

  /** The child frame an iframe ref shows, or null. */
  async frameOfIframe(ref) {
    const target = await this.resolve(ref);
    const { node } = await this.send('DOM.describeNode', { objectId: target.objectId });
    if (!node || !node.frameId) return null;
    const sessionId = [...this.sessions].find(([, targetId]) => targetId === node.frameId)?.[0];
    const frame = { frameId: node.frameId, sessionId };
    this.seqOf(frame);
    return frame;
  }

  /** Where a child frame running in its own process sits in the tab's viewport. */
  async frameOffset(frame) {
    if (frame.sessionId === undefined) return { x: 0, y: 0 };
    const { backendNodeId } = await this.send('DOM.getFrameOwner', { frameId: frame.frameId });
    const { model } = await this.send('DOM.getBoxModel', { backendNodeId });
    return { x: model.content[0], y: model.content[1] };
  }

  /** The middle of a ref's element in viewport coordinates, scrolled into view. */
  async point(target) {
    const sid = target.frame.sessionId;
    await this.send('DOM.scrollIntoViewIfNeeded', { objectId: target.objectId }, sid).catch(() => {});
    const { quads = [] } = await this.send('DOM.getContentQuads', { objectId: target.objectId }, sid).catch(() => ({}));
    const visible = quads.filter((q) => area(q) > 0.99);
    if (!visible.length) fail('not_visible', `${target.ref} is not visible on the page`);
    const local = center(visible.sort((a, b) => area(b) - area(a))[0]);
    const offset = await this.frameOffset(target.frame);
    return { x: local.x + offset.x, y: local.y + offset.y, local };
  }

  /** Refuses a click whose point another element covers, naming it (as Playwright does). */
  async hitTest(target, where) {
    const sid = target.frame.sessionId;
    const at = sid === undefined ? where : where.local;
    const hit = await this.send(
      'DOM.getNodeForLocation',
      { x: Math.round(at.x), y: Math.round(at.y), includeUserAgentShadowDOM: false, ignorePointerEventsNone: true },
      sid,
    ).catch(() => null);
    if (!hit || !hit.backendNodeId) return;
    const contextId = this.worlds.get(`${sid || ''}|${target.frame.frameId}`);
    let hitObject = null;
    try {
      const resolved = await this.send('DOM.resolveNode', { backendNodeId: hit.backendNodeId, executionContextId: contextId }, sid);
      hitObject = resolved.object.objectId;
    } catch {
      hitObject = null;
    }
    if (hitObject) {
      const inside = await this.send(
        'Runtime.callFunctionOn',
        { objectId: target.objectId, functionDeclaration: CONTAINS, arguments: [{ objectId: hitObject }], returnByValue: true },
        sid,
      );
      if (inside.result && inside.result.value === true) return;
    }
    let what = 'another element';
    try {
      const { object } = await this.send('DOM.resolveNode', { backendNodeId: hit.backendNodeId }, sid);
      const described = await this.send(
        'Runtime.callFunctionOn',
        { objectId: object.objectId, functionDeclaration: DESCRIBE, returnByValue: true },
        sid,
      );
      if (described.result && typeof described.result.value === 'string') what = described.result.value;
    } catch {
      // keep the generic name
    }
    fail('covered', `${target.ref} is covered by ${what}`);
  }

  // -- input ------------------------------------------------------------------------------

  async mouse(x, y, click) {
    const base = { x, y, pointerType: 'mouse' };
    await this.send('Input.dispatchMouseEvent', { ...base, type: 'mouseMoved', button: 'none' });
    if (!click) return;
    await this.send('Input.dispatchMouseEvent', { ...base, type: 'mousePressed', button: 'left', buttons: 1, clickCount: 1 });
    await this.send('Input.dispatchMouseEvent', { ...base, type: 'mouseReleased', button: 'left', buttons: 0, clickCount: 1 });
  }

  async keys(text) {
    const parsed = parseKeys(text);
    if (!parsed) fail('bad_args', `${JSON.stringify(text)} is not a key; use names such as Enter, Tab, Escape, ArrowDown or Control+a`);
    let modifiers = 0;
    for (const key of parsed.modifiers) {
      modifiers |= MODIFIER_BITS[key.key];
      await this.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', modifiers, ...keyFields(key) });
    }
    const { main } = parsed;
    // With Control, Alt or Meta held a key types nothing; it is a shortcut.
    const typed = main.text && !(modifiers & ~MODIFIER_BITS.Shift) ? main.text : undefined;
    const down = { type: typed ? 'keyDown' : 'rawKeyDown', modifiers, ...keyFields(main) };
    if (typed) Object.assign(down, { text: typed, unmodifiedText: typed });
    await this.send('Input.dispatchKeyEvent', down);
    await this.send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers, ...keyFields(main) });
    for (const key of parsed.modifiers.reverse()) {
      modifiers &= ~MODIFIER_BITS[key.key];
      await this.send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers, ...keyFields(key) });
    }
  }

  /**
   * Waits for `test` (the load) until `deadline`, or a few seconds after the document was
   * parsed; false when the page is still loading.
   */
  async loaded(test, deadline) {
    const end = Math.min(deadline === null ? Infinity : deadline, Date.now() + this.loadMs);
    for (;;) {
      if (this.wc.isDestroyed() || test()) return true;
      const now = Date.now();
      if (now >= end || (this.domReadyAt !== null && now >= this.domReadyAt + AFTER_DOM_MS)) return false;
      await sleep(50);
    }
  }

  /** Where the page stands before an action: its navigations and its requests so far. */
  mark() {
    return { navigations: this.navStarts, requests: this.requestSeq };
  }

  /** Whether a fetch or XHR made after `seq` is still open. */
  fetching(seq) {
    return this.requests.some((r) => r.seq > seq && (r.type === 'fetch' || r.type === 'xhr') && r.size === null && !r.failed && !r.redirected);
  }

  /**
   * Waits until the document has not changed for `quietMs`, counted from now at the
   * earliest, and no fetch made since `mark` is open; at most `settleMs`, and never past
   * the op's deadline. Ends at once when a navigation starts. A page whose world cannot be
   * read is given `quietMs`, as the old fixed wait did. True when the page settled.
   */
  async quiet(mark) {
    const start = Date.now();
    const deadline = this.deadline === null ? Infinity : this.deadline - 250;
    const end = Math.min(start + this.settleMs, deadline);
    for (;;) {
      if (this.wc.isDestroyed() || this.navStarts !== mark.navigations) return false;
      let idle = null;
      try {
        idle = await this.inWorld(this.mainFrame(), QUIET);
      } catch {
        idle = null;
      }
      const now = Date.now();
      const since = now - start;
      const still = typeof idle === 'number' ? Math.min(idle, since) : since;
      if (still >= this.quietMs && !this.fetching(mark.requests)) return true;
      if (now >= end) return false;
      await sleep(QUIET_POLL_MS);
    }
  }

  /**
   * After an action: the page settles; a navigation it started is waited for, as
   * Playwright's click does, and the new page settles too.
   */
  async settle(mark) {
    await this.quiet(mark);
    if (this.navStarts === mark.navigations) return {};
    const done = await this.loaded(() => !this.wc.isLoading(), this.deadline);
    await this.quiet({ navigations: this.navStarts, requests: mark.requests });
    return done ? {} : { loading: true };
  }

  async waitLoaded(started, mark) {
    await this.until(() => this.wc.isDestroyed() || this.navStarts !== started || this.wc.isLoading(), 1000);
    const done = await this.loaded(() => !this.wc.isLoading(), this.deadline);
    await this.quiet({ navigations: this.navStarts, requests: mark.requests });
    return done ? {} : { loading: true };
  }

  // -- operations -------------------------------------------------------------------------

  /**
   * `url` is http(s), checked by the host. Answers once the page has loaded, or with
   * `loading: true` when it has not by the op's deadline.
   */
  async navigate(url, snapshot = false) {
    this.deadline = Date.now() + this.navigateMs;
    try {
      const result = await this.navigateBy(url);
      if (snapshot === true) await this.withSnapshot(result);
      return result;
    } finally {
      this.deadline = null;
    }
  }

  async navigateBy(url) {
    await this.ensure();
    this.lifecycle.clear();
    this.domReadyAt = null;
    const mark = this.mark();
    let nav;
    try {
      // The server may take long to answer; that is waited for too, up to the deadline.
      nav = await this.send('Page.navigate', { url }, undefined, Infinity);
    } catch (error) {
      if (error instanceof BrowserFailure && error.kind === 'timeout' && !this.wc.isDestroyed()) return { status: null, loading: true };
      throw error;
    }
    if (nav.errorText) {
      // A download aborts the navigation; the host notes it when Chromium hands it over.
      await this.until(() => this.notes.download, 1000);
      if (this.notes.download) return { status: null };
      fail('navigation', `could not open ${url}: ${nav.errorText}`);
    }
    let done = true;
    if (nav.loaderId) done = await this.loaded(() => this.lifecycle.has(`${nav.loaderId}:load`), this.deadline);
    // An app that renders after its load (a fetch, then the DOM) is given time to.
    await this.quiet({ navigations: this.navStarts, requests: mark.requests });
    const doc = this.document;
    const status = doc && nav.loaderId && doc.loaderId === nav.loaderId ? doc.entry.status ?? null : null;
    return done ? { status } : { status, loading: true };
  }

  async run(op, args) {
    if (!TAB_OPS.has(op)) fail('bad_args', `unknown operation ${op}`);
    if (!isPlain(args)) fail('bad_args', 'args must be an object');
    // `wait` sleeps its own seconds, which the core allows for.
    this.deadline = op === 'wait' ? null : Date.now() + (op === 'steps' ? this.stepsMs : this.opMs);
    try {
      const result = await this.runBy(op, args);
      if (args.snapshot === true && SNAPSHOT_AFTER.has(op)) await this.withSnapshot(result);
      return result;
    } finally {
      this.deadline = null;
    }
  }

  async runBy(op, args) {
    await this.ensure();
    switch (op) {
      case 'snapshot':
        return this.snapshot();
      case 'click':
      case 'hover':
        return this.pointer(op, args);
      case 'type':
        return this.type(args);
      case 'select':
        return this.select(args);
      case 'press':
        return this.press(args);
      case 'scroll':
        return this.scroll(args);
      case 'screenshot':
        return this.screenshot(args);
      case 'back':
      case 'forward':
      case 'reload':
        return this.history(op);
      case 'wait': {
        const seconds = args.seconds === undefined ? 1 : args.seconds;
        if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0 || seconds > 10) {
          fail('bad_args', 'seconds is a number from 0 to 10');
        }
        await sleep(seconds * 1000);
        return {};
      }
      case 'source':
        return this.source();
      case 'dom':
        return this.dom(args);
      case 'scripts':
        return this.scriptList(args);
      case 'network':
        return this.network(args);
      case 'console':
        return this.consoleEntries(args);
      case 'storage':
        return this.storage();
      case 'evaluate':
        return this.evaluate(args);
      case 'steps':
        return this.steps(args);
      case 'read':
        return this.read(args);
      default:
        return fail('bad_args', `unknown operation ${op}`);
    }
  }

  /** `result` with the page's snapshot as `tree`, or why it could not be taken. */
  async withSnapshot(result) {
    try {
      result.tree = (await this.snapshot()).tree;
    } catch (error) {
      result.snapshot_error = String(error.message || error).slice(0, 500);
    }
    return result;
  }

  async snapshot() {
    const main = await this.inWorld(this.mainFrame(), SNAPSHOT);
    let tree = (main && main.tree) || '';
    for (const ref of ((main && main.iframes) || []).slice(0, MAX_FRAMES)) {
      // A frame that cannot be read keeps its iframe line.
      try {
        const frame = await this.frameOfIframe(ref);
        if (!frame) continue;
        const child = await this.inWorld(frame, SNAPSHOT);
        tree = splice(tree, ref, (child && child.tree) || '');
      } catch (error) {
        this.log(`iframe ${ref}: ${error.message}`);
      }
    }
    return { tree };
  }

  async pointer(op, args) {
    const mark = this.mark();
    const ref = argRef(args, { required: false });
    if (ref) {
      const target = await this.resolve(ref);
      const where = await this.point(target);
      if (op === 'click') await this.hitTest(target, where);
      await this.mouse(where.x, where.y, op === 'click');
    } else {
      const { x, y } = args;
      const ok = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 100000;
      if (!ok(x) || !ok(y)) fail('bad_args', `${op} needs a ref, or x and y in page pixels`);
      await this.mouse(x, y, op === 'click');
    }
    return op === 'click' ? this.settle(mark) : {};
  }

  async press(args) {
    const key = argString(args, 'key', { max: 100 });
    const mark = this.mark();
    await this.keys(key);
    return this.settle(mark);
  }

  async type(args) {
    const ref = argRef(args);
    const text = args.text === undefined || args.text === null ? '' : args.text;
    if (typeof text !== 'string' || text.length > 1000000) fail('bad_args', 'text must be a string');
    const target = await this.resolve(ref);
    const sid = target.frame.sessionId;
    const mark = this.mark();
    await this.send('DOM.scrollIntoViewIfNeeded', { objectId: target.objectId }, sid).catch(() => {});
    await this.send('DOM.focus', { objectId: target.objectId }, sid).catch(() => fail('failed', `${ref} cannot take text`));
    // What was there is replaced, as Playwright's fill does.
    await this.send('Runtime.callFunctionOn', { objectId: target.objectId, functionDeclaration: SELECT_ALL }, sid);
    if (text) await this.send('Input.insertText', { text });
    else await this.keys('Delete');
    if (args.submit === true) await this.keys('Enter');
    // A field that suggests as it is typed in is given time to.
    return this.settle(mark);
  }

  async select(args) {
    const ref = argRef(args);
    const text = argString(args, 'text', { max: 10000 });
    const target = await this.resolve(ref);
    const mark = this.mark();
    const answer = await this.send(
      'Runtime.callFunctionOn',
      { objectId: target.objectId, functionDeclaration: SELECT_OPTION, arguments: [{ value: text }], returnByValue: true },
      target.frame.sessionId,
    );
    if (answer.exceptionDetails) fail('bad_args', `${ref}: ${exceptionOf(answer.exceptionDetails).message}`);
    const value = answer.result.value;
    if (value === 'error:optionsnotfound') fail('not_found', `${ref} has no option ${JSON.stringify(text)}`);
    if (value === 'error:optionnotenabled') fail('not_found', `that option of ${ref} is disabled`);
    if (typeof value === 'string') fail('failed', `${ref} could not be changed`);
    // A choice may fill the next field (a country, then its regions).
    return { selected: value, ...(await this.settle(mark)) };
  }

  /** Sets a checkbox, radio or switch to `checked` (true by default) by clicking it, or its
   * label when the box itself is hidden, as a person would. */
  async check(args) {
    const ref = argRef(args);
    const want = args.checked === undefined || args.checked === null ? true : args.checked;
    if (typeof want !== 'boolean') fail('bad_args', 'checked is true or false');
    const target = await this.resolve(ref);
    const sid = target.frame.sessionId;
    const state = async () => {
      const answer = await this.send('Runtime.callFunctionOn', { objectId: target.objectId, functionDeclaration: CHECKED, returnByValue: true }, sid);
      return answer.result ? answer.result.value : null;
    };
    const before = await state();
    if (typeof before !== 'boolean') fail('bad_args', `${ref} is not a checkbox, radio or switch`);
    if (before === want) return { checked: want };
    const mark = this.mark();
    let where;
    try {
      where = await this.point(target);
      await this.hitTest(target, where);
    } catch (error) {
      if (!(error instanceof BrowserFailure) || (error.kind !== 'not_visible' && error.kind !== 'covered')) throw error;
      const label = await this.send('Runtime.callFunctionOn', { objectId: target.objectId, functionDeclaration: LABEL_OF }, sid);
      if (!label.result || !label.result.objectId) throw error;
      where = await this.point({ ref, frame: target.frame, objectId: label.result.objectId });
    }
    await this.mouse(where.x, where.y, true);
    const settled = await this.settle(mark);
    if (this.navStarts !== mark.navigations) return { checked: want, ...settled };
    const after = await state().catch(() => null);
    if (after !== want) fail('failed', `${ref} is still ${after ? 'checked' : 'not checked'} after a click`);
    return { checked: want, ...settled };
  }

  async scroll(args) {
    const ref = argRef(args, { required: false });
    if (ref) {
      const target = await this.resolve(ref);
      await this.send('DOM.scrollIntoViewIfNeeded', { objectId: target.objectId }, target.frame.sessionId);
      return {};
    }
    const direction = args.direction === undefined ? 'down' : args.direction;
    if (direction !== 'up' && direction !== 'down') fail('bad_args', 'direction is up or down');
    const sign = direction === 'up' ? -1 : 1;
    const mark = this.mark();
    const moved = await this.inWorld(this.mainFrame(), `(${SCROLL})(Math.round(innerHeight * 0.8) * ${sign})`);
    // What a page loads as it is scrolled (more of a feed) is given time to arrive.
    await this.quiet(mark);
    return { moved: moved === true };
  }

  async screenshot(args) {
    const ref = argRef(args, { required: false });
    let rect;
    if (ref) {
      const target = await this.resolve(ref);
      const sid = target.frame.sessionId;
      await this.send('DOM.scrollIntoViewIfNeeded', { objectId: target.objectId }, sid).catch(() => {});
      const { model } = await this.send('DOM.getBoxModel', { objectId: target.objectId }, sid);
      const offset = await this.frameOffset(target.frame);
      const xs = [model.border[0], model.border[2], model.border[4], model.border[6]];
      const ys = [model.border[1], model.border[3], model.border[5], model.border[7]];
      const zoom = this.wc.getZoomFactor();
      const x = Math.max(0, Math.floor((Math.min(...xs) + offset.x) * zoom));
      const y = Math.max(0, Math.floor((Math.min(...ys) + offset.y) * zoom));
      rect = {
        x,
        y,
        width: Math.max(1, Math.ceil((Math.max(...xs) - Math.min(...xs)) * zoom)),
        height: Math.max(1, Math.ceil((Math.max(...ys) - Math.min(...ys)) * zoom)),
      };
    }
    // capturePage, not Page.captureScreenshot: the CDP call hangs on a hidden view.
    const image = await withTimeout(rect ? this.wc.capturePage(rect) : this.wc.capturePage(), this.commandMs, 'the screenshot');
    if (!image || image.isEmpty()) fail('failed', 'the page has nothing drawn yet');
    return { png: image.toPNG().toString('base64') };
  }

  async history(op) {
    const { wc } = this;
    const nav = wc.navigationHistory;
    if (op === 'back' && !nav.canGoBack()) fail('no_history', 'there is no page to go back to');
    if (op === 'forward' && !nav.canGoForward()) fail('no_history', 'there is no page to go forward to');
    const started = this.navStarts;
    const mark = this.mark();
    this.domReadyAt = null;
    if (op === 'back') nav.goBack();
    else if (op === 'forward') nav.goForward();
    else wc.reload();
    return this.waitLoaded(started, mark);
  }

  async source() {
    const doc = this.document;
    if (!doc) return { error: 'no page was loaded in this tab since it opened' };
    const { entry } = doc;
    const facts = { status: entry.status ?? null, mime: entry.mime || null };
    try {
      const body = await this.send('Network.getResponseBody', { requestId: entry.requestId });
      const text = body.base64Encoded ? Buffer.from(body.body, 'base64').toString('utf8') : body.body;
      return { ...facts, text };
    } catch (error) {
      if (error instanceof BrowserFailure && error.kind === 'timeout') throw error;
      return { ...facts, error: error.message };
    }
  }

  async dom(args) {
    const ref = argRef(args, { required: false });
    if (ref) {
      const target = await this.resolve(ref);
      const { outerHTML } = await this.send('DOM.getOuterHTML', { objectId: target.objectId }, target.frame.sessionId);
      return { html: outerHTML };
    }
    const { root } = await this.send('DOM.getDocument', { depth: 0 });
    const { outerHTML } = await this.send('DOM.getOuterHTML', { nodeId: root.nodeId });
    return { html: outerHTML };
  }

  async scriptList(args) {
    const id = argString(args, 'id', { max: 64, required: false });
    if (!this.debuggerOn) {
      // Turned on when first asked for: it reports the scripts already loaded.
      await this.send('Debugger.enable', {});
      this.debuggerOn = true;
      await this.send('Debugger.setSkipAllPauses', { skip: true });
      await sleep(50);
    }
    const docUrl = this.wc.getURL().split('#')[0];
    if (!id) {
      return {
        scripts: [...this.scripts].map(([key, s]) => ({ id: key, url: s.url, length: s.length, inline: s.url.split('#')[0] === docUrl })),
      };
    }
    const script = this.scripts.get(id);
    if (!script) fail('not_found', `there is no script ${id}; scripts lists them`);
    const { scriptSource } = await this.send('Debugger.getScriptSource', { scriptId: script.scriptId });
    return { id, url: script.url, source: scriptSource };
  }

  async network(args) {
    const id = argString(args, 'id', { max: 64, required: false });
    const filter = argString(args, 'filter', { max: 1000, required: false });
    if (!id) {
      const needle = filter ? filter.toLowerCase() : '';
      return {
        requests: this.requests
          .filter((r) => !needle || r.url.toLowerCase().includes(needle))
          .map((r) => {
            const row = { id: r.id, method: r.method, url: r.url, status: r.status, type: r.type, size: r.size };
            if (r.failed) row.failed = r.failed;
            return row;
          }),
      };
    }
    const entry = this.requests.find((r) => r.id === id);
    if (!entry) fail('not_found', `there is no request ${id} since the page loaded; network lists them`);
    const request = { headers: entry.requestHeaders };
    if (entry.hasPostData) {
      try {
        request.post_data = (await this.send('Network.getRequestPostData', { requestId: entry.requestId }, entry.sessionId)).postData;
      } catch {
        // not held
      }
    }
    const result = {
      id,
      method: entry.method,
      url: entry.url,
      status: entry.status,
      request,
      response: { headers: entry.responseHeaders || {}, mime: entry.mime || null },
    };
    try {
      const body = await this.send('Network.getResponseBody', { requestId: entry.requestId }, entry.sessionId);
      if (body.base64Encoded && TEXT_MIME.test(entry.mime || '')) {
        result.body = Buffer.from(body.body, 'base64').toString('utf8');
      } else {
        result.body = body.body;
        if (body.base64Encoded) result.base64 = true;
      }
    } catch (error) {
      if (error instanceof BrowserFailure && error.kind === 'timeout') throw error;
      result.body_error = error.message;
    }
    return result;
  }

  consoleEntries(args) {
    const filter = argString(args, 'filter', { max: 1000, required: false });
    const needle = filter ? filter.toLowerCase().trim() : '';
    const levels = { warn: 'warning', warning: 'warning', error: 'error', log: 'log', info: 'info', debug: 'debug' };
    const entries = this.console.filter((e) => {
      if (!needle) return true;
      if (levels[needle]) return e.level === levels[needle];
      return e.text.toLowerCase().includes(needle);
    });
    return { entries };
  }

  async storage() {
    const url = this.wc.getURL();
    let cookies = [];
    if (/^https?:/.test(url)) {
      cookies = (await this.wc.session.cookies.get({ url })).map((c) => ({
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path,
        expires: c.expirationDate ?? null,
        httpOnly: Boolean(c.httpOnly),
        secure: Boolean(c.secure),
      }));
    }
    const { local = {}, session = {} } = (await this.inWorld(this.mainFrame(), STORAGE)) || {};
    return { cookies, local, session };
  }

  /**
   * Several actions in one op, each settling as it does alone. Every step is checked before
   * the first runs; they stop at the first that fails, and at a step that leaves the page,
   * since the refs of the rest belonged to the page before.
   */
  async steps(args) {
    const list = args.steps;
    if (!Array.isArray(list) || !list.length) fail('bad_args', 'steps is a list of actions, such as [{"action": "click", "ref": "e3"}]');
    if (list.length > MAX_STEPS) fail('bad_args', `at most ${MAX_STEPS} steps in one call`);
    const plan = list.map((step, i) => stepOf(step, i + 1));
    const start = this.navStarts;
    const done = [];
    let loading = false;
    for (const [i, step] of plan.entries()) {
      const record = { action: step.action };
      if (step.ref) record.ref = step.ref;
      let result;
      try {
        result = await this.step(step);
      } catch (error) {
        record.kind = error instanceof BrowserFailure ? error.kind : 'failed';
        record.error = String(error.message || error).slice(0, 1000);
        done.push(record);
        return { steps: done, total: plan.length, stopped: 'failed', ...(loading ? { loading: true } : {}) };
      }
      if (result.selected !== undefined) record.selected = result.selected;
      if (result.moved !== undefined) record.moved = result.moved;
      if (result.loading) loading = true;
      done.push(record);
      if (this.navStarts !== start && i < plan.length - 1) {
        return { steps: done, total: plan.length, stopped: 'navigated', ...(loading ? { loading: true } : {}) };
      }
    }
    return { steps: done, total: plan.length, ...(loading ? { loading: true } : {}) };
  }

  async step(step) {
    switch (step.action) {
      case 'click':
        return this.pointer('click', { ref: step.ref });
      case 'type':
        return this.type(step);
      case 'select':
        return this.select(step);
      case 'press':
        return this.press(step);
      case 'check':
        return this.check(step);
      case 'scroll':
        return this.scroll(step);
      case 'wait': {
        const left = this.deadline === null ? Infinity : this.deadline - Date.now() - 500;
        await sleep(Math.max(0, Math.min(step.seconds * 1000, left)));
        return {};
      }
      default:
        return fail('bad_args', `unknown step ${step.action}`);
    }
  }

  /** The page's readable text from `offset`, at most `max` characters, cut at a line. */
  async read(args) {
    const full = args.full === true;
    const offset = args.offset === undefined || args.offset === null ? 0 : args.offset;
    const max = args.max === undefined || args.max === null ? 6000 : args.max;
    if (!Number.isInteger(offset) || offset < 0) fail('bad_args', 'offset is a whole number of characters from the start');
    if (!Number.isInteger(max) || max < 200 || max > 100000) fail('bad_args', 'max is a number of characters from 200 to 100000');
    const page = (await this.inWorld(this.mainFrame(), `(${READ})(${full}, ${READ_MAX})`)) || {};
    const text = typeof page.text === 'string' ? page.text : '';
    let end = Math.min(text.length, offset + max);
    if (end < text.length) {
      const line = text.lastIndexOf('\n', end);
      if (line > offset + max / 2) end = line + 1;
    }
    const out = { text: text.slice(offset, end), offset, total: text.length, from: page.from || 'the whole page' };
    if (end < text.length) out.next = end;
    if (page.cut) out.long = true;
    return out;
  }

  async evaluate(args) {
    const code = argString(args, 'code', { max: 1000000 });
    const ref = argRef(args, { required: false });
    const isFunction = FUNCTION_CODE.test(code);
    let answer;
    if (ref) {
      // In the page's own world, where its globals are, with the element as `this`.
      const target = await this.resolve(ref);
      const sid = target.frame.sessionId;
      const { node } = await this.send('DOM.describeNode', { objectId: target.objectId }, sid);
      const { object } = await this.send('DOM.resolveNode', { backendNodeId: node.backendNodeId }, sid);
      answer = await this.send(
        'Runtime.callFunctionOn',
        {
          objectId: object.objectId,
          functionDeclaration: isFunction ? code : `function () { return (\n${code}\n); }`,
          arguments: [{ objectId: object.objectId }],
          awaitPromise: true,
          returnByValue: true,
          userGesture: true,
        },
        sid,
      );
    } else {
      const group = 'cogito-evaluate';
      try {
        answer = await this.send('Runtime.evaluate', {
          expression: isFunction ? `(${code})()` : code,
          replMode: !isFunction,
          awaitPromise: true,
          objectGroup: group,
          userGesture: true,
          timeout: this.commandMs - 500,
        });
        // REPL mode does not wait for a promise that is the last value; this does, and
        // brings any object back as JSON.
        if (!answer.exceptionDetails && answer.result && answer.result.objectId) {
          answer = await this.send('Runtime.callFunctionOn', {
            objectId: answer.result.objectId,
            functionDeclaration: BY_VALUE,
            awaitPromise: true,
            returnByValue: true,
            objectGroup: group,
          });
        }
      } finally {
        this.send('Runtime.releaseObjectGroup', { objectGroup: group }).catch(() => {});
      }
    }
    if (answer.exceptionDetails) return { exception: exceptionOf(answer.exceptionDetails) };
    const result = answer.result || {};
    if (result.type === 'undefined') return {};
    if ('value' in result) return { value: result.value };
    if (result.unserializableValue !== undefined) return { value: result.unserializableValue };
    return { value: result.description ?? null };
  }
}

/** One step of `steps`, checked: `{action, ref, text, key, submit, checked, direction, seconds}`. */
function stepOf(step, n) {
  if (!isPlain(step)) fail('bad_args', `step ${n} is not an action`);
  const action = step.action;
  if (typeof action !== 'string' || !STEP_ACTIONS.has(action)) {
    fail('bad_args', `step ${n}: the action is one of ${[...STEP_ACTIONS].join(', ')}`);
  }
  const named = (fn) => {
    try {
      return fn();
    } catch (error) {
      if (error instanceof BrowserFailure) error.message = `step ${n} (${action}): ${error.message}`;
      throw error;
    }
  };
  return named(() => {
    const out = { action };
    if (action === 'click' || action === 'type' || action === 'select' || action === 'check') out.ref = argRef(step);
    if (action === 'scroll') {
      const ref = argRef(step, { required: false });
      if (ref) out.ref = ref;
      const direction = step.direction === undefined || step.direction === null ? 'down' : step.direction;
      if (direction !== 'up' && direction !== 'down') fail('bad_args', 'direction is up or down');
      out.direction = direction;
    }
    if (action === 'type') {
      const text = step.text === undefined || step.text === null ? '' : step.text;
      if (typeof text !== 'string' || text.length > 100000) fail('bad_args', 'text must be a string');
      out.text = text;
      if (step.submit !== undefined && step.submit !== null && typeof step.submit !== 'boolean') fail('bad_args', 'submit is true or false');
      out.submit = step.submit === true;
    }
    if (action === 'select') out.text = argString(step, 'text', { max: 10000 });
    if (action === 'press') out.key = argString(step, 'key', { max: 100 });
    if (action === 'check') {
      if (step.checked !== undefined && step.checked !== null && typeof step.checked !== 'boolean') fail('bad_args', 'checked is true or false');
      out.checked = step.checked !== false;
    }
    if (action === 'wait') {
      const seconds = step.seconds === undefined || step.seconds === null ? 1 : step.seconds;
      if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0 || seconds > STEP_WAIT_MAX) {
        fail('bad_args', `seconds is a number from 0 to ${STEP_WAIT_MAX}`);
      }
      out.seconds = seconds;
    }
    return out;
  });
}

function exceptionOf(details) {
  const description = details.exception && details.exception.description;
  const message = (description ? description.split('\n')[0] : details.text) || 'an error';
  const out = { message };
  if (typeof details.lineNumber === 'number') out.line = details.lineNumber + 1;
  return out;
}

module.exports = {
  TabDriver,
  BrowserFailure,
  METHODS,
  TAB_OPS,
  REF,
  NAVIGATE_MS,
  OP_MS,
  STEPS_MS,
  MAX_STEPS,
  QUIET_MS,
  SETTLE_MS,
  READ,
  parseKeys,
  splice,
  withTimeout,
};
