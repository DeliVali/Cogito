'use strict';

/**
 * Design 1f, «Proyectos — al abrir la app y con ⌘O»: the full-window screen under the title
 * bar, covering the header bar and the composer. A hero band holds the question, the search
 * field and «Abrir carpeta…»; then «RECIENTES», one row per recent project with the
 * selected one lit and its «Abrir» pill; then the canvas' hint line under the list.
 * «Un proyecto = una carpeta + un hilo continuo».
 *
 * Values are the canvas' (`COGITO v2.dc.html`, section 1f, and 1j's «Proyectos · la
 * carpeta ya no existe» for a missing folder); projects.css has them. What the canvas shows
 * but no real source provides is left out instead of invented:
 *   - the name in «¿En qué trabajamos hoy, Ana?» when the welcome (1d) stored none: the
 *     question is asked without one (`api.name()` gives the stored name);
 *   - «+ Nuevo»: creating a project is not built. «Abrir carpeta…» opens any folder, and
 *     its native dialog can create one;
 *   - the service above each row's time («CLAUDE API»): a recent project records no model;
 *   - «Ubicar carpeta…» on a missing folder: nothing can relink a project to a new folder.
 * The search field is real: it filters the list by name and path as the person types.
 *
 * Keyboard: ↑/↓ move the selection over the rows that can be opened, ↵ opens it (as do a
 * double click and «Abrir»), Esc closes the screen when there is a project to go back to.
 *
 * `api`: `{ list(): Promise<{projects}>, pickFolder(): Promise<{path}|{canceled}>,
 * open(path): Promise<{project,...}|{error, code, restored}>, forget(path): Promise<{projects}>,
 * isBusy(): boolean, onOpened(result), onOpenFailed?(result, path), onOpening?(path),
 * onSettled?(), current?(): string|null, name?(): string|null, play?(kind) }`.
 * `current` names the open project's root (it starts selected); `play` plays the design's
 * sounds (sfx.js by default). `onOpening`/`onSettled` bracket an open in flight.
 *
 * While a project opens, the screen says so and cannot be closed: closing it then showed
 * a project whose core was already gone (QA 2026-10-11). An open that fails says why.
 */

import { pathNode, relativeWhen } from './project-menu.js';
import { escapeControlChars } from './sanitize.js';
import { sfx } from './sfx.js';
import { WINDOW_CHROME } from './strings.js';
import { greeting } from './welcome-view.js';

const STRINGS = {
  title: 'Proyectos',
  // The canvas' «¿En qué trabajamos hoy, Ana?», with the name the welcome stored (design
  // 1d, `config/welcome.py`), or without a name when none was given.
  question: greeting(null),
  search: 'buscar proyecto…',
  searchLabel: 'Buscar proyecto',
  searchGlyph: '⌕',
  recent: 'RECIENTES',
  openFolder: 'Abrir carpeta…',
  open: 'Abrir',
  // Design 1j («Proyectos · la carpeta ya no existe»), verbatim, and true: forgetting a
  // folder only drops it from this list (`RecentProjects.forget`, core/cogito/cli.py). A
  // missing row says it once, here, and not again on its path line (QA 2026-10-13).
  missingNote: 'Carpeta no encontrada',
  forget: 'Quitar de la lista',
  empty: 'Todavía no hay proyectos recientes.',
  // A search that matches no project, while the list itself is not empty.
  noResults: 'Sin resultados',
  close: 'Cancelar',
  busyNote: 'Hay una tarea en curso',
  loadError: 'No se pudo cargar la lista de proyectos',
  // NEW, no design source (QA 2026-10-11): an open in flight, and why one failed, by the
  // core's `code` (server/app.py `POST /sessions`, main/core.js's timeout).
  opening: (name) => `Abriendo ${name}…`,
  failed: {
    not_writable: (name) => `No se pudo abrir ${name}: sin permiso de escritura`,
    not_a_directory: (name) => `No se pudo abrir ${name}: la carpeta no existe`,
    timeout: (name) => `${name} tardó demasiado en abrirse`,
    other: (name) => `No se pudo abrir ${name}`,
  },
  lost: 'El proyecto abierto también se cerró',
  // The canvas' line under the list, verbatim.
  hint: '↑↓ Elegir · ↵ Abrir',
  // The title bar while this screen is up (design 1f: «COGITO — proyectos»).
  titleBar: 'proyectos',
};

/** The design's close timing (README: «encogimiento + fade (140 ms)»). */
const CLOSE_MS = 140;

function el(documentRef, tag, className, text) {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = escapeControlChars(String(text));
  return node;
}

/** `2026-09-29T10:42:00+00:00` -> a short local date and time. An unparsable value is shown
 * as is instead of hidden: an odd timestamp beats none. The row shows the canvas' relative
 * form («Hace 12 min»); this is its tooltip, and its text when that form is empty. */
function formatWhen(iso) {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return String(iso || '');
  return parsed.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

/** The folder's own name: `/home/ana/calc` -> `calc`. */
function folderName(path) {
  const parts = String(path || '').split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || '');
}

/** Why `path` did not open, from `result.code`. `lost`: the project that was open did not
 * come back either. */
function openFailureNote(result, path, { lost = false } = {}) {
  const say = STRINGS.failed[result && result.code] || STRINGS.failed.other;
  const text = say(escapeControlChars(folderName(path)));
  return lost ? `${text} ${STRINGS.lost}` : text;
}

/** Lower case, without accents: «Tésis» is found by «tesis». */
function fold(text) {
  return String(text || '')
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLocaleLowerCase('es');
}

/** The recent projects the search field lets through: every word of `query` somewhere in
 * the project's name or its path (as displayed), in any order. An empty query keeps all. */
function filterProjects(projects, query) {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (!words.length) return [...projects];
  return projects.filter((entry) => {
    const haystack = `${fold(entry.name)} ${fold(entry.display || entry.path)}`;
    return words.every((word) => haystack.includes(word));
  });
}

/** Where ↑/↓ lands from `index` among `count` rows: one step, stopping at either end (the
 * canvas' rows are a list with a first and a last, not a ring). -1 means "none yet": ↓
 * starts at the first row, ↑ at the last. */
function moveSelection(index, delta, count) {
  if (count <= 0) return -1;
  if (index < 0 || index >= count) return delta > 0 ? 0 : count - 1;
  return Math.max(0, Math.min(count - 1, index + delta));
}

function createProjectsScreen(api, documentRef = document) {
  const play = typeof api.play === 'function' ? api.play : sfx;

  const overlay = el(documentRef, 'div', 'projects-overlay');
  overlay.hidden = true;
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', STRINGS.title);

  // -- the hero band: the question, the search field and the buttons ---------------------
  const hero = el(documentRef, 'div', 'projects-hero');
  const question = el(documentRef, 'span', 'projects-title', STRINGS.question);
  const tools = el(documentRef, 'div', 'projects-tools');
  const search = el(documentRef, 'label', 'projects-search');
  const glyph = el(documentRef, 'span', 'projects-search-glyph', STRINGS.searchGlyph);
  glyph.setAttribute('aria-hidden', 'true');
  const searchInput = el(documentRef, 'input', 'projects-search-input');
  searchInput.type = 'text';
  searchInput.placeholder = STRINGS.search;
  searchInput.setAttribute('aria-label', STRINGS.searchLabel);
  searchInput.autocomplete = 'off';
  searchInput.spellcheck = false;
  search.append(glyph, searchInput);

  const openFolderButton = el(documentRef, 'button', 'projects-open-folder', STRINGS.openFolder);
  openFolderButton.type = 'button';
  // Not in the canvas (its ✕ is the window's): a way back with a pointer when there is a
  // project to go back to. Esc does the same from the keyboard.
  const closeButton = el(documentRef, 'button', 'projects-close', STRINGS.close);
  closeButton.type = 'button';
  tools.append(search, openFolderButton, closeButton);
  hero.append(question, tools);

  // -- the list ------------------------------------------------------------------------
  const scroller = el(documentRef, 'div', 'projects-scroll soft-scroll');
  const note = el(documentRef, 'div', 'projects-note');
  note.hidden = true;
  const sectionLabel = el(documentRef, 'span', 'projects-section-label', STRINGS.recent);
  const list = el(documentRef, 'div', 'projects-list');
  list.id = 'projects-list';
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', STRINGS.recent);
  searchInput.setAttribute('aria-controls', list.id);
  // «Sin resultados»: the search matched nothing, though there are projects.
  const noResults = el(documentRef, 'div', 'projects-empty projects-no-results', STRINGS.noResults);
  noResults.hidden = true;
  noResults.setAttribute('role', 'status');
  const hint = el(documentRef, 'span', 'projects-hint', STRINGS.hint);
  hint.hidden = true;
  scroller.append(note, sectionLabel, list, noResults, hint);

  overlay.append(hero, scroller);

  let dismissible = false;
  let projects = [];
  /** The selected row's project path; null when nothing openable is shown. */
  let selectedPath = null;
  /** The title bar's text before this screen took it, to give back on Esc. */
  let savedTitle = null;
  let closeTimer = null;
  /** An open in flight: the screen cannot close until it settles. */
  let opening = false;

  function setNote(text) {
    note.textContent = text || '';
    note.hidden = !text;
  }

  function busy() {
    return Boolean(api.isBusy());
  }

  /** Every control that switches projects, disabled while a turn runs. */
  function setDisabled(disabled) {
    openFolderButton.disabled = disabled;
    for (const button of list.querySelectorAll('button')) button.disabled = disabled;
  }

  // -- the title bar («COGITO — proyectos» while this screen is up) ----------------------

  function takeTitle() {
    const wordmark = documentRef.querySelector('.wordmark-text');
    if (savedTitle === null) savedTitle = { wordmark: wordmark ? wordmark.textContent : null, document: documentRef.title };
    const text = WINDOW_CHROME.title(STRINGS.titleBar);
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

  // -- rows ----------------------------------------------------------------------------

  function rows() {
    return [...list.querySelectorAll('.projects-row')];
  }

  /** The rows ↑/↓/↵ can reach: shown by the filter, and not a missing folder. */
  function reachable() {
    return rows().filter((row) => !row.hidden && !row.classList.contains('projects-row--missing'));
  }

  function markSelected({ reveal = false } = {}) {
    let selected = null;
    for (const row of rows()) {
      const on = row.dataset.path === selectedPath;
      row.classList.toggle('is-selected', on);
      if (row.getAttribute('role') === 'option') row.setAttribute('aria-selected', String(on));
      if (on) selected = row;
    }
    if (selected) {
      searchInput.setAttribute('aria-activedescendant', selected.id);
      if (reveal && typeof selected.scrollIntoView === 'function') selected.scrollIntoView({ block: 'nearest' });
    } else {
      searchInput.removeAttribute('aria-activedescendant');
    }
  }

  function select(path, { sound = false, reveal = false } = {}) {
    if (path === selectedPath) return;
    selectedPath = path;
    if (sound) play('select');
    markSelected({ reveal });
  }

  function buildMissingRow(entry, index) {
    const row = el(documentRef, 'div', 'projects-row projects-row--missing');
    row.id = `projects-row-${index}`;
    row.dataset.path = entry.path;
    row.title = escapeControlChars(String(entry.path));
    const line = el(documentRef, 'div', 'projects-row-line');
    const info = el(documentRef, 'span', 'projects-row-info');
    info.append(
      el(documentRef, 'span', 'projects-row-name', entry.name),
      pathNode(documentRef, 'projects-row-path', entry.display || entry.path),
    );
    line.append(el(documentRef, 'span', 'projects-row-sphere'), info);
    const actions = el(documentRef, 'div', 'projects-row-actions');
    const forget = el(documentRef, 'button', 'projects-row-forget', STRINGS.forget);
    forget.type = 'button';
    forget.addEventListener('click', () => {
      play('click');
      onForget(entry.path);
    });
    actions.append(forget);
    row.append(line, el(documentRef, 'span', 'projects-row-note', STRINGS.missingNote), actions);
    return row;
  }

  function buildRow(entry, index) {
    if (entry.missing) return buildMissingRow(entry, index);
    const row = el(documentRef, 'div', 'projects-row');
    row.id = `projects-row-${index}`;
    row.dataset.path = entry.path;
    row.setAttribute('role', 'option');
    row.setAttribute('aria-selected', 'false');
    row.title = escapeControlChars(String(entry.path));
    const info = el(documentRef, 'span', 'projects-row-info');
    info.append(
      el(documentRef, 'span', 'projects-row-name', entry.name),
      pathNode(documentRef, 'projects-row-path', entry.display || entry.path),
    );
    const meta = el(documentRef, 'span', 'projects-row-meta');
    const when = el(documentRef, 'span', 'projects-row-when', relativeWhen(entry.last_opened) || formatWhen(entry.last_opened));
    when.title = formatWhen(entry.last_opened);
    meta.append(when);
    const open = el(documentRef, 'button', 'projects-row-open', STRINGS.open);
    open.type = 'button';
    open.addEventListener('click', (event) => {
      event.stopPropagation();
      if (open.disabled) return;
      play('click');
      onOpen(entry.path);
    });
    row.append(el(documentRef, 'span', 'projects-row-sphere'), info, meta, open);
    row.addEventListener('click', () => select(entry.path, { sound: true }));
    row.addEventListener('dblclick', (event) => {
      if (event.target instanceof Element && event.target.closest('button')) return;
      // The first click of the two already played the canvas' `select`.
      onOpen(entry.path);
    });
    return row;
  }

  /** Applies the search filter to the rows already drawn. Rows are hidden, not rebuilt, so
   * the ones that stay do not replay their entrance on every key. */
  function applyFilter() {
    const kept = new Set(filterProjects(projects, searchInput.value).map((entry) => entry.path));
    for (const row of rows()) row.hidden = !kept.has(row.dataset.path);
    const openableRows = reachable();
    hint.hidden = openableRows.length === 0;
    noResults.hidden = !projects.length || kept.size > 0;
    if (!openableRows.some((row) => row.dataset.path === selectedPath)) {
      selectedPath = openableRows.length ? openableRows[0].dataset.path : null;
    }
    markSelected();
  }

  function render() {
    list.replaceChildren();
    if (!projects.length) {
      list.append(el(documentRef, 'div', 'projects-empty', STRINGS.empty));
      noResults.hidden = true;
      hint.hidden = true;
      selectedPath = null;
      markSelected();
      return;
    }
    projects.forEach((entry, index) => list.append(buildRow(entry, index)));
    applyFilter();
    setDisabled(busy());
  }

  /** The current project starts selected (when it is in the list); otherwise the first. */
  function preselect() {
    const current = typeof api.current === 'function' ? api.current() : null;
    const match = current
      ? projects.find((entry) => !entry.missing && (entry.display === current || entry.path === current))
      : null;
    selectedPath = match ? match.path : null;
  }

  async function refresh({ keepSelection = false } = {}) {
    try {
      const answer = await api.list();
      projects = (answer && answer.projects) || [];
      if (!keepSelection) preselect();
      render();
    } catch {
      projects = [];
      list.replaceChildren(el(documentRef, 'div', 'projects-empty', STRINGS.loadError));
      noResults.hidden = true;
      hint.hidden = true;
    }
  }

  // -- showing and closing -------------------------------------------------------------

  function hide() {
    overlay.hidden = true;
    overlay.classList.remove('is-closing');
    documentRef.removeEventListener('keydown', onKeydown, true);
  }

  function close() {
    if (opening || !dismissible || overlay.hidden || overlay.classList.contains('is-closing')) return;
    play('close');
    giveTitleBack();
    overlay.classList.add('is-closing');
    documentRef.removeEventListener('keydown', onKeydown, true);
    clearTimeout(closeTimer);
    closeTimer = setTimeout(hide, CLOSE_MS);
  }

  function onKeydown(event) {
    if (overlay.hidden) return;
    // Only what happens inside this screen (or with nothing focused at all).
    const target = event.target;
    if (target instanceof Node && target !== documentRef.body && !overlay.contains(target)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const openableRows = reachable();
      const at = openableRows.findIndex((row) => row.dataset.path === selectedPath);
      const next = moveSelection(at, event.key === 'ArrowDown' ? 1 : -1, openableRows.length);
      if (next >= 0) select(openableRows[next].dataset.path, { reveal: true });
      return;
    }
    if (event.key === 'Enter' && !event.repeat) {
      // A focused button answers Enter itself («Abrir carpeta…», «Quitar de la lista»).
      if (target instanceof Element && target.closest('button')) return;
      if (!selectedPath) return;
      event.preventDefault();
      play('select');
      onOpen(selectedPath);
    }
  }

  async function onOpen(path) {
    if (busy() || opening) return;
    opening = true;
    setDisabled(true);
    closeButton.disabled = true;
    setNote(STRINGS.opening(escapeControlChars(folderName(path))));
    if (typeof api.onOpening === 'function') api.onOpening(path);
    // `api.open` (an IPC round trip) can reject as well as answer `{error}`: a lost
    // connection to the core throws, a refusal the main process caught does not.
    const result = await Promise.resolve(api.open(path)).catch((error) => ({ error: error.message }));
    opening = false;
    closeButton.disabled = false;
    if (typeof api.onSettled === 'function') api.onSettled();
    if (!result || result.error) {
      play('cancel');
      setDisabled(busy());
      // A refusal another screen explains (the core serving no model: the connection
      // screen says why) is left to it. A running turn gets this screen's own note.
      const handled = typeof api.onOpenFailed === 'function' ? await api.onOpenFailed(result, path) : false;
      if (handled) {
        setNote('');
        return;
      }
      if (result && result.error === 'busy') {
        setNote(STRINGS.busyNote);
        return;
      }
      // The project that was open did not come back: nothing to return to.
      const lost = dismissible && typeof api.current === 'function' && !api.current();
      if (lost) {
        dismissible = false;
        closeButton.hidden = true;
      }
      setNote(openFailureNote(result, path, { lost }));
      return;
    }
    savedTitle = null; // the opened project's title replaces this screen's
    hide();
    setDisabled(false);
    api.onOpened(result);
  }

  async function onForget(path) {
    const at = rows().findIndex((row) => row.dataset.path === path);
    try {
      await api.forget(path);
    } finally {
      await refresh({ keepSelection: true });
      keepFocusAfterForget(at);
    }
  }

  /** A removed row took the focus with it. It goes to the button of the missing row that
   * took its place (or the one before it), else back to the search field: never <body>. */
  function keepFocusAfterForget(at) {
    const all = rows();
    const missing = all.filter((row) => row.classList.contains('projects-row--missing') && !row.hidden);
    const target = missing.find((row) => all.indexOf(row) >= at) || missing[missing.length - 1];
    const button = target ? target.querySelector('.projects-row-forget') : null;
    (button || searchInput).focus({ preventScroll: true });
  }

  openFolderButton.addEventListener('click', async () => {
    if (busy()) return;
    play('click');
    const picked = await Promise.resolve(api.pickFolder()).catch(() => null);
    if (picked && picked.path) onOpen(picked.path);
  });
  closeButton.addEventListener('click', () => {
    play('click');
    close();
  });
  searchInput.addEventListener('input', applyFilter);

  /** Show the screen. `options.dismissible` (default true): whether it can be closed
   * without picking a project. False on first launch, when there is nothing to fall
   * back to. `options.note`: a line to show at once, such as why a project did not open. */
  function show({ dismissible: closable = true, note: shownNote = '' } = {}) {
    clearTimeout(closeTimer);
    const alreadyUp = !overlay.hidden && !overlay.classList.contains('is-closing');
    dismissible = closable;
    closeButton.hidden = !closable;
    overlay.classList.remove('is-closing');
    overlay.hidden = false;
    takeTitle();
    question.textContent = escapeControlChars(greeting(typeof api.name === 'function' ? api.name() : null));
    documentRef.removeEventListener('keydown', onKeydown, true);
    documentRef.addEventListener('keydown', onKeydown, true);
    if (!alreadyUp) {
      searchInput.value = '';
      list.replaceChildren();
      noResults.hidden = true;
      hint.hidden = true;
    }
    const isBusy = busy();
    setDisabled(isBusy);
    setNote(isBusy ? STRINGS.busyNote : shownNote);
    searchInput.focus({ preventScroll: true });
    refresh({ keepSelection: alreadyUp });
  }

  documentRef.body.append(overlay);
  /** Open `path` again (after the core could serve a model), as if chosen here. */
  function reopen(path) {
    return onOpen(path);
  }
  return { show, close, reopen, isOpen: () => !overlay.hidden, el: overlay };
}

export { createProjectsScreen, formatWhen, filterProjects, moveSelection, openFailureNote, folderName };
