'use strict';

/**
 * The file explorer (design 1a «Explorador y visor»): a 244 px panel at the left of the
 * thread, opened and closed by the button left of the project chip (Ctrl+Shift+E · ⌘⇧E),
 * remembered per project. It lists the project's folder with git's marks (M amber, U green),
 * filters by name, and lets the person manage files: new file or folder, rename (F2),
 * duplicate («{nombre} copia»), copy the path, show it in the system's file browser, open it
 * with the system's app, and move it to the trash (always asked, always undoable for a few
 * seconds). Files dragged onto it are copied into the folder they land on.
 *
 * It never changes a file's content: a click opens the file in the read-only viewer
 * (`viewer.js`), and changes are asked of the model below.
 *
 * Attaching: the chat's own «+ adjuntar» and drag-and-drop stay as they are (chat only,
 * nothing copied into the project). «Adjuntar al mensaje» in the menu, or Ctrl/⌘+click on a
 * row, is one more way in: the panel turns into «SELECCIONA PARA ADJUNTAR» with a box per
 * row, folders mark everything they hold, files that cannot be attached say why, and
 * «Adjuntar {n}» hands the files' paths to the chat's tray.
 *
 * `api` is `window.cogito.explorer` (paths relative to the project; the main process checks
 * every one). The rest of `deps`: `play(kind)`, `platform`, `pathOf(file)`,
 * `onOpenFile(rel)`, `onRemoved(rel)`, `onRenamed(from, to)`, `attachPaths(abs[])`,
 * `attachedPaths()`, `attachLimit`, `onToggle(open)`.
 */

import { EXPLORER } from './strings.js';

const PANEL_LIMIT_ATTACH_BYTES = 20 * 1024 * 1024;
const TOAST_MS = 5000;
const CLOSE_MS = 140;
const ROW_INDENT = 14;
/** The drag type of a row moved inside the explorer (not a file from outside). */
const ROW_TYPE = 'application/x-cogito-path';

/** Extension -> [label, background, ink]. The canvas defines the first ten; the rest take
 * the code or text colours by family. The colours are CSS variables (explorer.css sets
 * their light and dark values), so a badge already drawn follows a theme change. */
const BADGES = (() => {
  const code = ['var(--fx-badge-code)', 'var(--fx-badge-code-ink)'];
  const text = ['var(--fx-badge-text)', 'var(--fx-badge-text-ink)'];
  const md = ['var(--fx-badge-text)', 'var(--fx-badge-md-ink)'];
  const json = ['var(--fx-badge-json)', 'var(--fx-badge-json-ink)'];
  const data = ['var(--fx-badge-data)', 'var(--fx-badge-data-ink)'];
  const table = {
    ts: ['TS', ...code], tsx: ['TSX', ...code], js: ['JS', ...code], jsx: ['JSX', ...code],
    mjs: ['JS', ...code], cjs: ['JS', ...code],
    json: ['{}', ...json], jsonc: ['{}', ...json],
    md: ['MD', ...md], mdx: ['MD', ...md],
    pdf: ['PDF', 'var(--fx-badge-pdf)', 'var(--fx-badge-pdf-ink)'],
    csv: ['CSV', ...data], tsv: ['TSV', ...data],
    env: ['ENV', 'var(--fx-badge-env)', 'var(--fx-badge-json-ink)'],
    db: ['DB', ...text], sqlite: ['DB', ...text], sql: ['SQL', ...text],
    py: ['PY', ...code], go: ['GO', ...code], rs: ['RS', ...code], java: ['JV', ...code],
    c: ['C', ...code], cpp: ['C++', ...code], h: ['H', ...code], hpp: ['H', ...code],
    cs: ['C#', ...code], rb: ['RB', ...code], php: ['PHP', ...code], sh: ['SH', ...code],
    css: ['CSS', ...code], html: ['HTM', ...code], xml: ['XML', ...text], yaml: ['YML', ...text],
    yml: ['YML', ...text], toml: ['TML', ...text], ini: ['INI', ...text], txt: ['TXT', ...text],
    log: ['LOG', ...text], lock: ['LCK', ...text],
  };
  for (const ext of ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg']) table[ext] = ['IMG', ...data];
  return table;
})();
const DIR_BADGE = ['▣', 'var(--fx-badge-dir)', 'var(--fx-badge-dir-ink)'];
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg', 'tiff', 'heic']);
const NO_TEXT_EXT = new Set([
  'db', 'sqlite', 'sqlite3', 'zip', 'gz', 'tgz', 'xz', 'bz2', '7z', 'rar', 'tar', 'exe', 'dll', 'so', 'dylib', 'bin',
  'o', 'a', 'class', 'jar', 'wasm', 'ttf', 'otf', 'woff', 'woff2', 'mp3', 'wav', 'ogg', 'flac', 'mp4', 'mov', 'avi',
  'mkv', 'webm', 'iso', 'dmg', 'pyc',
]);

function extOf(name) {
  const lower = String(name).toLowerCase();
  if (lower === '.env' || lower.startsWith('.env.')) return 'env';
  const dot = lower.lastIndexOf('.');
  return dot > 0 ? lower.slice(dot + 1) : '';
}

/** [label, background, ink] for an entry. */
function badgeFor(entry) {
  if (entry.dir) return DIR_BADGE;
  const ext = extOf(entry.name);
  if (BADGES[ext]) return BADGES[ext];
  const label = ext ? ext.slice(0, 3).toUpperCase() : '·';
  return [label, 'var(--fx-badge-text)', 'var(--fx-badge-text-ink)'];
}

/** Why a file cannot be attached, as a key of `EXPLORER.reasons`, or null. */
/** The image formats a model can be sent (PNG, JPEG, GIF, WebP); the rest stay refused. */
const SENDABLE_IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp']);

function fileReason(entry, { attached = false, binary = false } = {}) {
  if (attached) return 'attached';
  const ext = extOf(entry.name);
  if (ext === 'env') return 'secrets';
  if (SENDABLE_IMAGE_EXT.has(ext)) {
    return typeof entry.size === 'number' && entry.size > 7_500_000 ? 'tooLarge' : null;
  }
  if (IMAGE_EXT.has(ext)) return 'image';
  if (typeof entry.size === 'number' && entry.size > PANEL_LIMIT_ATTACH_BYTES) return 'tooLarge';
  if (binary || NO_TEXT_EXT.has(ext)) return 'unsupported';
  return null;
}

function parentOf(rel) {
  const slash = rel.lastIndexOf('/');
  return slash < 0 ? '' : rel.slice(0, slash);
}

function nameOf(rel) {
  return rel.slice(rel.lastIndexOf('/') + 1);
}

function isUnder(rel, dir) {
  return dir === '' || rel === dir || rel.startsWith(dir + '/');
}

function el(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function createExplorer({ panel, toggle, api, deps = {}, documentRef = document, windowRef = window, storage = windowRef.localStorage }) {
  const doc = documentRef;
  const play = deps.play || (() => {});
  const platform = deps.platform || 'linux';
  const attachLimit = deps.attachLimit || 20;
  const isMac = platform === 'darwin';

  const state = {
    key: null, // the open project's path, for the remembered open state
    name: '',
    rootAbs: '',
    open: false,
    cache: new Map(), // folder rel -> entries
    expanded: new Set(),
    marks: {},
    selected: null,
    filter: '',
    all: null, // every file, loaded only while filtering
    renaming: null, // {rel} or {create: 'file'|'folder', dir}
    menu: null,
    selecting: false,
    checked: new Set(),
    folderFiles: new Map(), // folder rel -> files under it (for selection)
    more: new Map(), // folder rel -> entries past the main process's limit
    dropTarget: null,
  };
  /** The row being dragged inside the explorer, while it is. */
  let dragging = null;
  /** A background refresh came in while a name was being typed. */
  let dirty = false;
  /** The next draw scrolls the selected row into view. */
  let revealSelected = false;

  // ---------- the panel's own DOM ----------
  panel.textContent = '';
  panel.classList.add('explorer');
  const head = el(doc, 'div', 'explorer-head');
  const heading = el(doc, 'span', 'explorer-heading', EXPLORER.heading);
  head.append(heading);
  const actions = [
    ['+', EXPLORER.newFile, () => startCreate('file')],
    ['▣', EXPLORER.newFolder, () => startCreate('folder')],
    ['↻', EXPLORER.refresh, () => { play('click'); refresh(); }],
    ['⊟', EXPLORER.collapseAll, () => { play('close'); state.expanded.clear(); render(); }],
  ];
  // A new terminal tab beside «Hilo»; shown only where terminals exist (`body.has-terminals`).
  if (deps.onNewTerminal) {
    actions.push(['>_', EXPLORER.newTerminal, () => { play('click'); deps.onNewTerminal(); }, 'explorer-action--terminal']);
  }
  for (const [glyph, label, run, extra] of actions) {
    const button = el(doc, 'button', extra ? `explorer-action ${extra}` : 'explorer-action', glyph);
    button.type = 'button';
    button.title = label;
    button.setAttribute('aria-label', label);
    button.addEventListener('click', run);
    head.append(button);
  }
  const filterWrap = el(doc, 'div', 'explorer-filter');
  const filterInput = el(doc, 'input');
  filterInput.type = 'text';
  filterInput.placeholder = EXPLORER.filter;
  filterInput.setAttribute('aria-label', EXPLORER.filter);
  filterInput.spellcheck = false;
  filterWrap.append(filterInput);
  const projectLine = el(doc, 'div', 'explorer-project');
  const scroller = el(doc, 'div', 'explorer-tree soft-scroll');
  scroller.tabIndex = 0;
  scroller.setAttribute('role', 'tree');
  scroller.setAttribute('aria-label', EXPLORER.heading);
  const footer = el(doc, 'div', 'explorer-footer');
  footer.hidden = true;
  const toastBox = el(doc, 'div', 'explorer-toast');
  toastBox.hidden = true;
  toastBox.setAttribute('role', 'status');
  panel.append(head, filterWrap, projectLine, scroller, footer, toastBox);

  // ---------- open / close, remembered per project ----------
  function storageKey() {
    return state.key ? `cogito.explorer.open:${state.key}` : null;
  }
  function setOpen(open, { remember = true, sound = false } = {}) {
    state.open = open;
    panel.hidden = !open;
    toggle.classList.toggle('is-open', open);
    toggle.setAttribute('aria-pressed', String(open));
    if (sound) play(open ? 'open' : 'close');
    closeMenu(false);
    if (remember && storageKey()) {
      try {
        storage.setItem(storageKey(), open ? '1' : '0');
      } catch {
        // per-viewer convenience only
      }
    }
    if (deps.onToggle) deps.onToggle(open);
    if (open) refresh();
  }
  toggle.title = EXPLORER.toggle(isMac ? '⌘' : 'Ctrl');
  toggle.setAttribute('aria-label', EXPLORER.heading);
  toggle.addEventListener('click', () => setOpen(!state.open, { sound: true }));
  doc.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.selecting && state.open && !state.renaming && !state.menu) {
      event.preventDefault();
      play('cancel');
      leaveSelecting();
      return;
    }
    const mod = isMac ? event.metaKey : event.ctrlKey;
    if (mod && event.shiftKey && (event.key === 'E' || event.key === 'e') && state.key) {
      event.preventDefault();
      setOpen(!state.open, { sound: true });
    }
  });

  // ---------- loading ----------
  async function loadDir(rel) {
    const answer = await api.list(rel).catch(() => null);
    if (!answer || answer.error) {
      state.cache.set(rel, []);
      return;
    }
    state.cache.set(rel, answer.entries || []);
    state.more.set(rel, answer.more || 0);
  }

  let refreshing = null;
  /** Re-reads every folder on screen and git's marks, then draws. */
  async function refresh() {
    if (!state.key || !state.open) return;
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const dirs = ['', ...[...state.expanded]];
      await Promise.all(dirs.map((dir) => loadDir(dir)));
      for (const dir of [...state.expanded]) {
        if (!state.cache.has(dir)) state.expanded.delete(dir);
      }
      const marks = await api.marks().catch(() => ({}));
      state.marks = marks && !marks.error ? marks : {};
      if (state.filter) await loadAll();
      // A name being typed is never redrawn from under the person (the model's edits reach
      // here many times a second): the tree is drawn when the name is done.
      if (state.renaming) dirty = true;
      else render();
    })().finally(() => {
      refreshing = null;
    });
    return refreshing;
  }

  async function loadAll() {
    const answer = await api.filesUnder('').catch(() => null);
    state.all = answer && !answer.error ? answer.files : [];
  }

  // ---------- the rows on screen ----------
  /** `[{entry, depth}]` in the order drawn. */
  function visibleRows() {
    const rows = [];
    if (state.filter) {
      const q = state.filter.toLowerCase();
      const files = (state.all || []).filter((file) => file.name.toLowerCase().includes(q));
      const dirs = new Set();
      for (const file of files) {
        let dir = parentOf(file.path);
        while (dir) {
          dirs.add(dir);
          dir = parentOf(dir);
        }
      }
      const walk = (dir, depth) => {
        const kids = [];
        for (const d of dirs) if (parentOf(d) === dir) kids.push({ name: nameOf(d), path: d, dir: true });
        for (const f of files) if (parentOf(f.path) === dir) kids.push({ name: f.name, path: f.path, dir: false, size: f.size });
        kids.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name, undefined, { numeric: true }) : a.dir ? -1 : 1));
        for (const kid of kids) {
          rows.push({ entry: kid, depth });
          if (kid.dir) walk(kid.path, depth + 1);
        }
      };
      walk('', 0);
      return rows;
    }
    const walk = (dir, depth) => {
      for (const entry of state.cache.get(dir) || []) {
        rows.push({ entry, depth });
        if (entry.dir && state.expanded.has(entry.path)) walk(entry.path, depth + 1);
      }
    };
    walk('', 0);
    return rows;
  }

  function attachedSet() {
    const paths = deps.attachedPaths ? deps.attachedPaths() : [];
    const prefix = state.rootAbs ? state.rootAbs.replace(/[\\/]+$/, '') + '/' : '';
    const rels = new Set();
    for (const abs of paths) {
      const normal = String(abs).replace(/\\/g, '/');
      if (prefix && normal.startsWith(prefix)) rels.add(normal.slice(prefix.length));
    }
    return rels;
  }

  /** A checked folder or file covers `rel`. */
  function coveredByCheck(rel) {
    for (const checked of state.checked) if (isUnder(rel, checked)) return checked;
    return null;
  }

  function rowReason(entry, attached) {
    if (entry.dir) {
      const files = state.folderFiles.get(entry.path);
      if (!files) return null;
      const valid = files.filter((file) => !fileReason(file, { attached: attached.has(file.path), binary: file.binary }));
      return valid.length ? null : 'empty';
    }
    return fileReason(entry, { attached: attached.has(entry.path) });
  }

  function render() {
    dirty = false;
    const keepTop = scroller.scrollTop;
    projectLine.textContent = `▾ ${state.name}`;
    heading.textContent = state.selecting ? EXPLORER.selecting : EXPLORER.heading;
    panel.classList.toggle('is-selecting', state.selecting);
    const rows = visibleRows();
    const attached = attachedSet();
    scroller.textContent = '';
    if (state.filter && !rows.length) {
      scroller.append(el(doc, 'span', 'explorer-empty', EXPLORER.noMatches));
    }
    rows.forEach(({ entry, depth }, index) => {
      scroller.append(buildRow(entry, depth, index, attached));
      // A folder with more than the main process lists says how many more it holds.
      const more = !state.filter && entry.dir && state.expanded.has(entry.path) ? state.more.get(entry.path) : 0;
      if (more) scroller.append(moreRow(more, depth + 1));
    });
    if (!state.filter && state.more.get('')) scroller.append(moreRow(state.more.get(''), 0));
    // A new file or folder is typed in a row of its own, first inside its folder.
    if (state.renaming && state.renaming.create) placeCreateRow();
    // Redrawing keeps the reader's place; an action that made or moved something shows it.
    scroller.scrollTop = keepTop;
    if (revealSelected) {
      revealSelected = false;
      const row = state.selected ? scroller.querySelector(`.explorer-row[data-path="${CSS.escape(state.selected)}"]`) : null;
      if (row) row.scrollIntoView({ block: 'nearest' });
    }
    renderFooter(attached);
  }

  function moreRow(n, depth) {
    const row = el(doc, 'div', 'explorer-more-row', EXPLORER.more(n));
    row.style.paddingLeft = `${8 + depth * ROW_INDENT + 16}px`;
    return row;
  }

  function buildRow(entry, depth, index, attached) {
    const row = el(doc, 'div', 'explorer-row');
    row.setAttribute('role', 'treeitem');
    row.dataset.path = entry.path;
    row.dataset.index = String(index);
    row.style.paddingLeft = `${8 + depth * ROW_INDENT}px`;
    if (entry.dir) row.setAttribute('aria-expanded', String(state.filter ? true : state.expanded.has(entry.path)));
    row.setAttribute('aria-selected', String(state.selected === entry.path));
    if (state.selected === entry.path) row.classList.add('is-selected');
    if (state.dropTarget === entry.path) row.classList.add('is-drop-target');
    const mark = state.marks[entry.path];
    if (mark === 'M') row.classList.add('is-modified');
    if (mark === 'U') row.classList.add('is-new');

    if (state.selecting) row.append(buildCheck(entry, attached));
    const chevron = el(doc, 'span', 'explorer-chevron', entry.dir ? '▶' : '');
    if (entry.dir && (state.filter || state.expanded.has(entry.path))) chevron.classList.add('is-open');
    if (entry.dir) {
      chevron.addEventListener('click', (event) => {
        event.stopPropagation();
        play('click');
        toggleDir(entry.path);
      });
    }
    const [label, bg, ink] = badgeFor(entry);
    const badge = el(doc, 'span', 'explorer-badge', label);
    badge.style.background = bg;
    badge.style.color = ink;
    row.append(chevron, badge);

    if (state.renaming && !state.renaming.create && state.renaming.rel === entry.path) {
      row.append(buildRenameInput(state.renaming.value || entry.name, (value) => commitRename(entry, value)));
    } else {
      row.append(el(doc, 'span', 'explorer-name', entry.name));
    }

    const tag = el(doc, 'span', 'explorer-tag');
    const reason = state.selecting ? rowReason(entry, attached) : null;
    if (reason) {
      tag.textContent = EXPLORER.reasons[reason];
      tag.classList.add('is-reason');
      tag.title = EXPLORER.reasons[reason];
    } else if (!entry.dir && (attached.has(entry.path) || [...attached].some((a) => isUnder(entry.path, a)))) {
      tag.textContent = '◆';
      tag.classList.add('is-attached');
    } else if (mark === 'M' || mark === 'U') {
      tag.textContent = mark;
      tag.classList.add(mark === 'M' ? 'is-m' : 'is-u');
    }
    row.append(tag);

    const more = el(doc, 'button', 'explorer-more', '⋯');
    more.type = 'button';
    more.tabIndex = -1;
    more.setAttribute('aria-label', entry.name);
    more.addEventListener('click', (event) => {
      event.stopPropagation();
      if (lastMenuClosed.path === entry.path && Date.now() - lastMenuClosed.at < 300) return;
      select(entry.path);
      openMenu(entry, row);
    });
    row.append(more);

    // Dragged onto a folder, a row moves there (`moveTo`).
    row.draggable = !state.selecting && !state.filter;
    row.addEventListener('dragstart', (event) => {
      if (state.renaming) {
        event.preventDefault();
        return;
      }
      dragging = entry.path;
      event.dataTransfer.setData(ROW_TYPE, entry.path);
      event.dataTransfer.effectAllowed = 'move';
      closeMenu(false);
    });
    row.addEventListener('dragend', () => {
      dragging = null;
      clearDropTarget();
    });
    row.addEventListener('click', (event) => onRowClick(event, entry));
    row.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      select(entry.path);
      openMenu(entry, row);
    });
    return row;
  }

  function buildCheck(entry, attached) {
    const box = el(doc, 'span', 'explorer-check');
    const reason = rowReason(entry, attached);
    const covered = coveredByCheck(entry.path);
    const partial = entry.dir && !covered && [...state.checked].some((rel) => isUnder(rel, entry.path) && rel !== entry.path);
    if (reason) {
      box.classList.add('is-blocked');
      box.title = EXPLORER.reasons[reason];
    } else if (covered) {
      box.classList.add('is-checked');
      box.textContent = '✓';
    } else if (partial) {
      box.classList.add('is-checked');
      box.textContent = '–';
    }
    return box;
  }

  function placeCreateRow() {
    const { dir, create } = state.renaming;
    const parentRow = dir ? scroller.querySelector(`.explorer-row[data-path="${CSS.escape(dir)}"]`) : null;
    const depth = dir ? Number(parentRow ? parentRow.style.paddingLeft.replace('px', '') : 8) : 8;
    const row = el(doc, 'div', 'explorer-row is-selected is-creating');
    row.style.paddingLeft = `${dir ? depth + ROW_INDENT : 8}px`;
    const entry = create === 'folder' ? { name: '', dir: true } : { name: EXPLORER.defaultFile, dir: false };
    const [label, bg, ink] = badgeFor(entry);
    const badge = el(doc, 'span', 'explorer-badge', label);
    badge.style.background = bg;
    badge.style.color = ink;
    row.append(el(doc, 'span', 'explorer-chevron', ''), badge);
    const typed = state.renaming.value;
    row.append(buildRenameInput(typed || (create === 'folder' ? EXPLORER.defaultFolder : EXPLORER.defaultFile), (value) => commitCreate(value)));
    if (parentRow) parentRow.after(row);
    else scroller.prepend(row);
  }

  function buildRenameInput(value, commit) {
    const input = el(doc, 'input', 'explorer-rename');
    input.type = 'text';
    input.value = value;
    input.spellcheck = false;
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      if (save) commit(input.value.trim());
      else {
        state.renaming = null;
        render();
      }
    };
    input.addEventListener('click', (event) => event.stopPropagation());
    input.addEventListener('keydown', (event) => {
      event.stopPropagation();
      if (event.key === 'Enter') {
        event.preventDefault();
        play('select');
        finish(true);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', () => finish(true));
    queueMicrotask(() => {
      input.focus();
      // The name without its extension is selected, as a file browser does.
      const dot = value.lastIndexOf('.');
      input.setSelectionRange(0, dot > 0 ? dot : value.length);
    });
    return input;
  }

  // ---------- actions ----------
  function select(rel) {
    state.selected = rel;
    for (const row of scroller.querySelectorAll('.explorer-row')) {
      row.classList.toggle('is-selected', row.dataset.path === rel);
      row.setAttribute('aria-selected', String(row.dataset.path === rel));
    }
  }

  async function toggleDir(rel) {
    if (state.expanded.has(rel)) state.expanded.delete(rel);
    else {
      state.expanded.add(rel);
      if (!state.cache.has(rel)) await loadDir(rel);
    }
    render();
  }

  function onRowClick(event, entry) {
    // The second click of a double click would close the folder the first one opened, or
    // open the file twice.
    if (event.detail > 1) return;
    closeMenu(false);
    const mod = isMac ? event.metaKey : event.ctrlKey;
    if (mod || state.selecting) {
      if (!state.selecting) enterSelecting();
      toggleCheck(entry);
      return;
    }
    select(entry.path);
    if (entry.dir) {
      play('click');
      toggleDir(entry.path);
    } else {
      play('open');
      if (deps.onOpenFile) deps.onOpenFile(entry.path, entry);
    }
  }

  function targetDir() {
    if (!state.selected) return '';
    const entry = findEntry(state.selected);
    return entry && entry.dir ? entry.path : parentOf(state.selected);
  }

  function findEntry(rel) {
    for (const entries of state.cache.values()) {
      const found = entries.find((entry) => entry.path === rel);
      if (found) return found;
    }
    return null;
  }

  async function startCreate(kind, dir = targetDir()) {
    play('open');
    closeMenu(false);
    if (state.filter) {
      state.filter = '';
      filterInput.value = '';
    }
    if (dir && !state.expanded.has(dir)) {
      state.expanded.add(dir);
      if (!state.cache.has(dir)) await loadDir(dir);
    }
    state.renaming = { create: kind, dir };
    render();
  }

  async function commitCreate(value) {
    const { create, dir } = state.renaming || {};
    state.renaming = null;
    if (!value) {
      render();
      return;
    }
    const answer = create === 'folder' ? await api.newFolder(dir, value) : await api.newFile(dir, value);
    if (answer && answer.error) {
      play('cancel');
      showToast(errorText(answer.error));
      state.renaming = { create, dir, value };
      render();
      return;
    }
    await loadDir(dir);
    state.selected = answer.path;
    revealSelected = true;
    render();
    if (create === 'file' && deps.onOpenFile) deps.onOpenFile(answer.path, { name: nameOf(answer.path), path: answer.path, dir: false, size: 0 });
  }

  async function commitRename(entry, value) {
    state.renaming = null;
    if (!value || value === entry.name) {
      render();
      return;
    }
    const answer = await api.rename(entry.path, value);
    if (answer && answer.error) {
      play('cancel');
      showToast(errorText(answer.error));
      state.renaming = { rel: entry.path, value };
      render();
      return;
    }
    const dir = parentOf(entry.path);
    if (state.expanded.has(entry.path)) {
      state.expanded.delete(entry.path);
      state.expanded.add(answer.path);
    }
    await loadDir(dir);
    state.selected = answer.path;
    revealSelected = true;
    render();
    if (deps.onRenamed) deps.onRenamed(entry.path, answer.path);
  }

  async function duplicate(entry) {
    play('select');
    const answer = await api.duplicate(entry.path);
    if (answer && answer.error) {
      showToast(errorText(answer.error));
      return;
    }
    await loadDir(parentOf(entry.path));
    state.selected = answer.path;
    revealSelected = true;
    render();
  }

  function errorText(code) {
    if (code === 'exists') return EXPLORER.taken;
    if (code === 'into-itself') return EXPLORER.intoItself;
    if (code === 'invalid-name' || code === 'hidden') return EXPLORER.badName;
    return EXPLORER.failed;
  }

  // ---------- context menu ----------
  function revealLabel() {
    return EXPLORER.reveal[platform] || EXPLORER.reveal.linux;
  }

  /** Which row's menu a click just closed, so the same click on its ⋯ does not reopen it. */
  const lastMenuClosed = { path: null, at: 0 };

  function closeMenu(sound = true) {
    const menu = state.menu;
    if (!menu) return;
    state.menu = null;
    lastMenuClosed.path = menu.dataset.path || null;
    lastMenuClosed.at = Date.now();
    if (sound) play('close');
    menu.classList.add('is-closing');
    setTimeout(() => menu.remove(), CLOSE_MS);
  }

  /** `entry.root` is the project itself (a right click on the panel's empty space or its
   * name): only what makes sense for the folder that holds everything. `at` places the menu
   * at the pointer instead of under a row. */
  function openMenu(entry, row, fromKeyboard = false, at = null) {
    closeMenu(false);
    play('open');
    const menu = el(doc, 'div', 'menu explorer-menu');
    menu.setAttribute('role', 'menu');
    menu.dataset.path = entry.root ? '' : entry.path;
    const item = (label, run, { key = '', danger = false } = {}) => {
      const button = el(doc, 'button', `menu-item explorer-menu-item${danger ? ' is-danger' : ''}`);
      button.type = 'button';
      button.setAttribute('role', 'menuitem');
      button.append(el(doc, 'span', 'explorer-menu-label', label));
      if (key) button.append(el(doc, 'span', 'explorer-menu-key', key));
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        closeMenu(false);
        run();
      });
      menu.append(button);
    };
    const separator = () => menu.append(el(doc, 'span', 'explorer-menu-separator'));
    if (!entry.dir) {
      item(EXPLORER.open, () => {
        play('open');
        if (deps.onOpenFile) deps.onOpenFile(entry.path, entry);
      }, { key: EXPLORER.keys.open });
      item(EXPLORER.openSystem, () => {
        play('click');
        api.openSystem(entry.path);
      });
    }
    item(revealLabel(), () => {
      play('click');
      api.reveal(entry.path);
    });
    separator();
    item(EXPLORER.newFile, () => startCreate('file', entry.dir ? entry.path : parentOf(entry.path)));
    item(EXPLORER.newFolder, () => startCreate('folder', entry.dir ? entry.path : parentOf(entry.path)));
    separator();
    if (entry.root) {
      item(EXPLORER.copyPath, async () => {
        play('click');
        await api.copyPath('', false);
        showToast(EXPLORER.pathCopied);
      });
      item(EXPLORER.refresh, () => {
        play('click');
        refresh();
      });
      placeMenu(menu, row, fromKeyboard, at);
      return;
    }
    item(EXPLORER.rename, () => startRename(entry), { key: EXPLORER.keys.rename });
    item(EXPLORER.duplicate, () => duplicate(entry));
    item(EXPLORER.copyPath, async () => {
      play('click');
      await api.copyPath(entry.path, false);
      showToast(EXPLORER.pathCopied);
    });
    item(EXPLORER.copyRelative, async () => {
      play('click');
      await api.copyPath(entry.path, true);
      showToast(EXPLORER.relativeCopied);
    });
    separator();
    item(state.selecting ? EXPLORER.addToSelection : EXPLORER.attach, () => attachFromMenu(entry));
    separator();
    item(EXPLORER.remove, () => askDelete(entry), {
      key: isMac ? EXPLORER.keys.remove.darwin : EXPLORER.keys.remove.other,
      danger: true,
    });
    placeMenu(menu, row, fromKeyboard, at);
  }

  function placeMenu(menu, row, fromKeyboard, at) {
    doc.body.append(menu);
    const panelRect = panel.getBoundingClientRect();
    const height = menu.offsetHeight;
    const wanted = at ? at.y : row.getBoundingClientRect().bottom + 2;
    const top = Math.max(4, Math.min(wanted, windowRef.innerHeight - height - 8));
    const left = at ? Math.min(at.x, windowRef.innerWidth - menu.offsetWidth - 8) : panelRect.left + 34;
    menu.style.left = `${Math.max(4, left)}px`;
    menu.style.top = `${top}px`;
    state.menu = menu;
    // Opened from the keyboard, the first item takes the focus; a pointer draws no ring.
    if (fromKeyboard) {
      const first = menu.querySelector('.menu-item');
      if (first) first.focus({ preventScroll: true });
    }
  }

  doc.addEventListener(
    'pointerdown',
    (event) => {
      if (state.menu && !state.menu.contains(event.target)) closeMenu(true);
    },
    true,
  );

  function startRename(entry) {
    play('click');
    state.renaming = { rel: entry.path };
    render();
  }

  // A right click that lands on no row (the space under the last one, an empty project) or
  // on the project's name: the project's own menu, so a first file or folder can be made.
  const rootMenu = (event) => {
    if (event.target instanceof Element && event.target.closest('.explorer-row, input')) return;
    event.preventDefault();
    select(null);
    openMenu({ root: true, dir: true, path: '', name: state.name }, projectLine, false, { x: event.clientX, y: event.clientY });
  };
  scroller.addEventListener('contextmenu', rootMenu);
  projectLine.addEventListener('contextmenu', rootMenu);

  // ---------- delete, with «Deshacer» ----------
  function askDelete(entry) {
    play('open');
    const overlay = el(doc, 'div', 'explorer-dialog-layer');
    const dialog = el(doc, 'div', 'explorer-dialog');
    dialog.setAttribute('role', 'alertdialog');
    dialog.setAttribute('aria-modal', 'true');
    const title = el(doc, 'div', 'explorer-dialog-title', EXPLORER.deleteTitle);
    const body = el(doc, 'div', 'explorer-dialog-body');
    const question = el(doc, 'div', 'explorer-dialog-question', EXPLORER.deleteQuestion(entry.name));
    const where = el(doc, 'div', 'explorer-dialog-path', entry.path);
    body.append(question, where);
    const changed = deps.changedInTask ? deps.changedInTask(entry.path) : false;
    if (changed) {
      const warn = el(doc, 'div', 'explorer-dialog-warn');
      warn.append(el(doc, 'span', 'explorer-dot'), el(doc, 'span', '', EXPLORER.changedInTask));
      body.append(warn);
    }
    const buttons = el(doc, 'div', 'explorer-dialog-buttons');
    const cancel = el(doc, 'button', 'cc-button', EXPLORER.cancel);
    cancel.type = 'button';
    const confirm = el(doc, 'button', 'cc-button cc-button--danger', EXPLORER.moveToTrash);
    confirm.type = 'button';
    buttons.append(cancel, confirm);
    body.append(buttons);
    dialog.append(title, body);
    overlay.append(dialog);
    doc.body.append(overlay);
    const composer = doc.querySelector('.composer');
    if (composer) overlay.style.bottom = `${windowRef.innerHeight - composer.getBoundingClientRect().top}px`;
    const close = () => {
      overlay.remove();
      doc.removeEventListener('keydown', onKey, true);
      scroller.focus({ preventScroll: true });
    };
    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        play('cancel');
        close();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        confirm.click();
      }
    };
    doc.addEventListener('keydown', onKey, true);
    cancel.addEventListener('click', () => {
      play('cancel');
      close();
    });
    overlay.addEventListener('click', (event) => {
      if (event.target !== overlay) return;
      play('cancel');
      close();
    });
    confirm.addEventListener('click', async () => {
      play('close');
      close();
      const answer = await api.remove(entry.path);
      if (answer && answer.error) {
        showToast(errorText(answer.error));
        return;
      }
      state.expanded.forEach((rel) => {
        if (isUnder(rel, entry.path)) state.expanded.delete(rel);
      });
      if (state.selected && isUnder(state.selected, entry.path)) state.selected = null;
      await loadDir(parentOf(entry.path));
      render();
      if (deps.onRemoved) deps.onRemoved(entry.path);
      showToast(EXPLORER.trashed(answer.name), async () => {
        play('select');
        const back = await api.undo(answer.id);
        if (back && back.error) {
          showToast(back.error === 'exists' ? EXPLORER.taken : EXPLORER.undoLate);
          return;
        }
        await loadDir(parentOf(entry.path));
        state.selected = entry.path;
        revealSelected = true;
        render();
      });
    });
    confirm.focus();
  }

  let toastTimer = null;
  /** One toast at a time, inside the panel. «Deshacer» only when there is something to undo. */
  function showToast(text, undo = null) {
    clearTimeout(toastTimer);
    toastBox.textContent = '';
    toastBox.append(el(doc, 'span', 'explorer-toast-text', text));
    if (undo) {
      const button = el(doc, 'button', 'explorer-toast-undo', EXPLORER.undo);
      button.type = 'button';
      button.addEventListener('click', () => {
        clearTimeout(toastTimer);
        toastBox.hidden = true;
        undo();
      });
      toastBox.append(button);
    }
    toastBox.hidden = false;
    toastBox.classList.remove('is-in');
    void toastBox.offsetWidth;
    toastBox.classList.add('is-in');
    toastTimer = setTimeout(() => {
      toastBox.hidden = true;
    }, TOAST_MS);
  }

  // ---------- attaching from the explorer ----------
  function enterSelecting() {
    state.selecting = true;
    state.checked.clear();
  }

  function leaveSelecting() {
    state.selecting = false;
    state.checked.clear();
    render();
  }

  async function ensureFolderFiles(rel) {
    if (state.folderFiles.has(rel)) return state.folderFiles.get(rel);
    const answer = await api.filesUnder(rel).catch(() => null);
    const files = answer && !answer.error ? answer.files : [];
    state.folderFiles.set(rel, files);
    return files;
  }

  async function toggleCheck(entry) {
    const attached = attachedSet();
    if (entry.dir) await ensureFolderFiles(entry.path);
    const reason = rowReason(entry, attached);
    if (reason) {
      play('cancel');
      render();
      return;
    }
    play('click');
    select(entry.path);
    const covering = coveredByCheck(entry.path);
    if (covering === entry.path) {
      state.checked.delete(entry.path);
    } else if (covering) {
      // Unchecking something inside a checked folder: the folder's other children stay.
      state.checked.delete(covering);
      const siblings = state.cache.get(covering) || [];
      for (const sibling of siblings) if (sibling.path !== entry.path && !isUnder(entry.path, sibling.path)) state.checked.add(sibling.path);
      if (parentOf(entry.path) !== covering) state.checked.add(parentOf(entry.path));
    } else {
      for (const rel of [...state.checked]) if (isUnder(rel, entry.path)) state.checked.delete(rel);
      state.checked.add(entry.path);
    }
    render();
  }

  async function attachFromMenu(entry) {
    const attached = attachedSet();
    const already = entry.dir ? false : attached.has(entry.path);
    if (already) {
      play('cancel');
      showToast(EXPLORER.alreadyAttached(entry.name));
      return;
    }
    play('open');
    if (!state.selecting) enterSelecting();
    if (entry.dir) await ensureFolderFiles(entry.path);
    if (!rowReason(entry, attached) && !coveredByCheck(entry.path)) {
      for (const rel of [...state.checked]) if (isUnder(rel, entry.path)) state.checked.delete(rel);
      state.checked.add(entry.path);
    }
    state.selected = entry.path;
    render();
  }

  /** What «Adjuntar {n}» would send: `{valid: [rel], skipped: [{name, reason}]}`. */
  async function collect(attached) {
    const valid = new Set();
    const skipped = [];
    for (const rel of state.checked) {
      const entry = findEntry(rel) || { name: nameOf(rel), path: rel, dir: state.folderFiles.has(rel) };
      if (entry.dir) {
        for (const file of await ensureFolderFiles(rel)) {
          const reason = fileReason(file, { attached: attached.has(file.path), binary: file.binary });
          if (reason) skipped.push({ name: file.name, reason });
          else valid.add(file.path);
        }
      } else if (!fileReason(entry, { attached: attached.has(rel) })) {
        valid.add(rel);
      }
    }
    return { valid: [...valid], skipped };
  }

  let footerToken = 0;
  async function renderFooter(attached) {
    footer.hidden = !state.selecting;
    if (!state.selecting) return;
    const token = ++footerToken;
    const { valid, skipped } = await collect(attached);
    if (token !== footerToken) return;
    const already = attached.size;
    const n = valid.length;
    const room = Math.max(0, attachLimit - already);
    const over = n > room;
    footer.textContent = '';
    const top = el(doc, 'div', 'explorer-footer-top');
    top.append(el(doc, 'span', 'explorer-footer-count', n ? EXPLORER.files(n) : EXPLORER.pick));
    if (skipped.length) {
      const skip = el(doc, 'span', 'explorer-footer-skipped', EXPLORER.skipped(skipped.length));
      skip.title = skipped.map((s) => `${s.name} · ${EXPLORER.reasons[s.reason]}`).join('\n');
      top.append(skip);
    }
    const limit = el(
      doc,
      'div',
      'explorer-footer-limit',
      over ? EXPLORER.overLimit(attachLimit, n - room) : EXPLORER.ofLimit(already + n, attachLimit),
    );
    const buttons = el(doc, 'div', 'explorer-footer-buttons');
    const cancel = el(doc, 'button', 'cc-button', EXPLORER.cancel);
    cancel.type = 'button';
    cancel.addEventListener('click', () => {
      play('cancel');
      leaveSelecting();
    });
    const confirm = el(doc, 'button', 'cc-button cc-button--primary', EXPLORER.attachN(n));
    confirm.type = 'button';
    const enabled = n > 0 && !over;
    confirm.classList.toggle('is-disabled', !enabled);
    confirm.setAttribute('aria-disabled', String(!enabled));
    confirm.addEventListener('click', async () => {
      if (!enabled) {
        play('cancel');
        return;
      }
      play('select');
      const base = state.rootAbs.replace(/[\\/]+$/, '');
      const paths = valid.map((rel) => `${base}/${rel}`);
      leaveSelecting();
      if (deps.attachPaths) await deps.attachPaths(paths);
      render();
    });
    buttons.append(cancel, confirm);
    footer.append(top, limit, buttons);
  }

  // ---------- filter ----------
  // Esc empties it; Enter opens the first file found; ↓ goes on into the list.
  filterInput.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && filterInput.value) {
      event.preventDefault();
      event.stopPropagation();
      filterInput.value = '';
      state.filter = '';
      state.all = null;
      render();
    } else if (event.key === 'Enter') {
      const first = visibleRows().find((row) => !row.entry.dir);
      if (!first) return;
      event.preventDefault();
      select(first.entry.path);
      play('open');
      if (deps.onOpenFile) deps.onOpenFile(first.entry.path, first.entry);
    } else if (event.key === 'ArrowDown') {
      const first = scroller.querySelector('.explorer-row');
      if (!first) return;
      event.preventDefault();
      select(first.dataset.path);
      scroller.focus({ preventScroll: true });
    }
  });
  filterInput.addEventListener('input', async () => {
    state.filter = filterInput.value.trim();
    if (state.filter && !state.all) await loadAll();
    if (!state.filter) state.all = null;
    render();
  });

  // ---------- keyboard on the tree ----------
  scroller.addEventListener('keydown', (event) => {
    if (state.renaming) return;
    const rows = [...scroller.querySelectorAll('.explorer-row')];
    const index = rows.findIndex((row) => row.dataset.path === state.selected);
    const entryAt = (i) => {
      const path = rows[i] && rows[i].dataset.path;
      return path ? findEntry(path) || (state.all || []).find((f) => f.path === path) : null;
    };
    const current = index >= 0 ? entryAt(index) : null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const next = Math.max(0, Math.min(rows.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
      if (rows[next]) {
        select(rows[next].dataset.path);
        rows[next].scrollIntoView({ block: 'nearest' });
      }
    } else if (event.key === 'ArrowRight' && current && current.dir && !state.expanded.has(current.path)) {
      event.preventDefault();
      toggleDir(current.path);
    } else if (event.key === 'ArrowLeft' && current && current.dir && state.expanded.has(current.path)) {
      event.preventDefault();
      toggleDir(current.path);
    } else if (event.key === 'Enter' && current) {
      event.preventDefault();
      onRowClick(new MouseEvent('click'), current);
    } else if ((event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) && current) {
      event.preventDefault();
      const row = rows[index];
      if (row) openMenu(current, row, true);
    } else if (event.key === 'F2' && current) {
      event.preventDefault();
      startRename(current);
    } else if ((event.key === 'Delete' || (isMac && event.key === 'Backspace' && event.metaKey)) && current) {
      event.preventDefault();
      askDelete(current);
    } else if (event.key === 'Escape' && state.selecting) {
      event.preventDefault();
      play('cancel');
      leaveSelecting();
    }
  });

  // ---------- dropping: files from outside are copied in, rows are moved ----------
  function hasFiles(event) {
    const types = event.dataTransfer && event.dataTransfer.types;
    return Boolean(types) && Array.from(types).includes('Files');
  }
  function isRowDrag(event) {
    const types = event.dataTransfer && event.dataTransfer.types;
    return Boolean(dragging) && Boolean(types) && Array.from(types).includes(ROW_TYPE);
  }
  function dropDirFor(event) {
    const row = event.target instanceof Element ? event.target.closest('.explorer-row') : null;
    if (!row || !row.dataset.path) return '';
    const entry = findEntry(row.dataset.path);
    return entry && entry.dir ? entry.path : parentOf(row.dataset.path);
  }
  /** Whether a dragged row may land in `dir`: not into itself or one of its own folders,
   * and not where it already is. */
  function canMoveInto(dir) {
    if (!dragging) return false;
    if (isUnder(dir, dragging) && dir !== '') return false;
    if (dragging === dir) return false;
    return parentOf(dragging) !== dir;
  }
  function clearDropTarget() {
    state.dropTarget = null;
    panel.classList.remove('is-drop-root');
    for (const row of scroller.querySelectorAll('.is-drop-target')) row.classList.remove('is-drop-target');
  }
  function showDropTarget(dir) {
    if (dir === state.dropTarget) return;
    state.dropTarget = dir;
    panel.classList.toggle('is-drop-root', dir === '');
    for (const row of scroller.querySelectorAll('.explorer-row')) row.classList.toggle('is-drop-target', row.dataset.path === dir && dir !== '');
  }
  let hoverTimer = null;
  panel.addEventListener('dragover', (event) => {
    const files = hasFiles(event);
    const rowDrag = isRowDrag(event);
    if (!files && !rowDrag) return;
    const dir = dropDirFor(event);
    if (rowDrag && !canMoveInto(dir)) {
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
      clearDropTarget();
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = files ? 'copy' : 'move';
    showDropTarget(dir);
    // Held over a closed folder, it opens, so a row can go deeper.
    clearTimeout(hoverTimer);
    if (dir && !state.expanded.has(dir)) {
      hoverTimer = setTimeout(() => {
        if (state.dropTarget === dir && !state.expanded.has(dir)) toggleDir(dir);
      }, 700);
    }
  });
  panel.addEventListener('dragleave', (event) => {
    if (event.relatedTarget && panel.contains(event.relatedTarget)) return;
    clearTimeout(hoverTimer);
    clearDropTarget();
  });
  panel.addEventListener('drop', async (event) => {
    clearTimeout(hoverTimer);
    if (isRowDrag(event)) {
      event.preventDefault();
      const dir = dropDirFor(event);
      const rel = dragging;
      const allowed = canMoveInto(dir);
      dragging = null;
      clearDropTarget();
      if (allowed) await moveTo(rel, dir);
      return;
    }
    if (!hasFiles(event)) return;
    event.preventDefault();
    const dir = dropDirFor(event);
    clearDropTarget();
    const sources = Array.from(event.dataTransfer.files || [])
      .map((file) => (deps.pathOf ? deps.pathOf(file) : ''))
      .filter(Boolean);
    if (!sources.length) return;
    play('select');
    const answer = await api.copyInto(dir, sources);
    if (answer && answer.error) {
      showToast(errorText(answer.error));
      return;
    }
    if (dir && !state.expanded.has(dir)) state.expanded.add(dir);
    await loadDir(dir);
    if (answer.paths && answer.paths[0]) state.selected = answer.paths[0];
    revealSelected = true;
    render();
  });

  /** Moves `rel` into folder `dir`; «Deshacer» moves it back. Open tabs follow it. */
  async function moveTo(rel, dir, { undoable = true } = {}) {
    const from = parentOf(rel);
    const answer = await api.move(rel, dir);
    if (!answer || answer.error) {
      play('cancel');
      showToast(errorText(answer ? answer.error : 'failed'));
      return;
    }
    if (!answer.moved) return;
    play('select');
    for (const open of [...state.expanded]) {
      if (isUnder(open, rel)) {
        state.expanded.delete(open);
        state.expanded.add(answer.path + open.slice(rel.length));
      }
    }
    if (dir && !state.expanded.has(dir)) state.expanded.add(dir);
    await Promise.all([loadDir(from), loadDir(dir)]);
    state.selected = answer.path;
    revealSelected = true;
    render();
    if (deps.onRenamed) deps.onRenamed(rel, answer.path);
    if (!undoable) return;
    showToast(EXPLORER.moved(nameOf(rel), dir ? nameOf(dir) : state.name), () => moveTo(answer.path, from, { undoable: false }));
  }

  if (api.onChanged) api.onChanged(() => refresh());
  // The system would not take it: the item is back in its folder (or kept aside), never gone.
  if (api.onTrashFailed) {
    api.onTrashFailed((info) => {
      showToast(EXPLORER.trashFailed(info.name));
      refresh();
    });
  }

  return {
    /** A project was opened: its path (the remembered open state's key), name and absolute
     * root (to tell which attached files are its own). */
    async setProject({ key, name }) {
      state.key = key;
      state.name = name || '';
      state.cache.clear();
      state.expanded.clear();
      state.folderFiles.clear();
      state.marks = {};
      state.selected = null;
      state.filter = '';
      state.all = null;
      state.selecting = false;
      state.checked.clear();
      filterInput.value = '';
      const abs = await api.absolute('').catch(() => null);
      state.rootAbs = abs && abs.path ? abs.path : '';
      if (api.watch) api.watch();
      let open = true;
      try {
        const saved = storage.getItem(storageKey());
        if (saved === '0') open = false;
      } catch {
        // default open, as the design draws it
      }
      setOpen(open, { remember: false });
    },
    refresh,
    toggle: () => setOpen(!state.open, { sound: true }),
    isOpen: () => state.open,
    contains: (node) => panel.contains(node),
    /** Re-draws the ◆ marks after the chat's attachments changed. */
    attachmentsChanged: () => {
      if (state.open) render();
    },
  };
}

export { createExplorer, badgeFor, fileReason, extOf };
