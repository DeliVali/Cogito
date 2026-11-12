'use strict';

/**
 * DOM for "Volver a un punto" (rewind, design 1k): the link on a user message, the confirm
 * dialog, the conflict dialog and the result line. Built from `rewind.js`'s pure models.
 * Every piece of file content (a path, a diff line) goes through `textContent`/
 * `createTextNode`, never `innerHTML`, for the same reason as in `diff.js`: a diff is
 * arbitrary file content.
 *
 * Drawn as the canvas draws them (`COGITO v2.dc.html`, section 1k; cards.css has the
 * values). The confirm is an amber card with «Cancelar» (the raised square) and «Volver
 * aquí» (the rose pill); while it runs, the card becomes the «Volviendo a este punto…» pill
 * with its spinner. The conflict is a red card whose diff opens on the canvas' grid-row
 * curve, with «Dejar como está» and the split rose pill «Restaurar los otros N | ▼», whose
 * menu holds both restoring choices. The result is the stamped blue pill «↺ Volviste a las
 * HH:MM · N archivos · Rehacer». Sounds are the canvas' (`rvStart` `open`, `rvCancel`
 * `cancel`, `rvGo` `click` then `done` or `close`, the toggles `open`/`close`, `rvRedo`
 * `select`); `options.play` stands in for sfx.js in a test.
 *
 * Keyboard safety matches the approval card's guard (`chat.js`'s `ask`). Enter/Escape act
 * only when the dialog element itself is the keydown's target, not when the key bubbled
 * from a focused child or another control, and only after a 500 ms grace period from
 * creation. The Enter that just sent a message, or a key still repeating from typing, can
 * never answer a dialog that just appeared: nothing destructive on a bubbled Enter.
 */

import { parseUnifiedDiff } from './diff.js';
import { escapeControlChars } from './sanitize.js';
import { sfx } from './sfx.js';
import { REWIND } from './strings.js';

const KEY_GRACE_MS = 500;
/** The design's close timing for a menu (README: «encogimiento + fade (140 ms)»). */
const CLOSE_MS = 140;

/** Shared keydown guard: true when `event` is a real answer to `card`, not a stray key or
 * a repeat. */
function isRealAnswerKey(event, card, createdAt) {
  if (event.target !== card) return false;
  if (event.repeat) return false;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return false;
  if (Date.now() - createdAt < KEY_GRACE_MS) return false;
  return true;
}

/** Adds or drops one class on `node` through `className` alone (the unit suite's fake DOM
 * has no `classList`). */
function setClass(node, name, on) {
  const names = String(node.className || '').split(/\s+/).filter((part) => part && part !== name);
  if (on) names.push(name);
  node.className = names.join(' ');
}

function attr(node, name, value) {
  if (typeof node.setAttribute === 'function') node.setAttribute(name, value);
}

function span(documentRef, className, text) {
  const node = documentRef.createElement('span');
  if (className) node.className = className;
  if (text != null) node.append(documentRef.createTextNode(text));
  return node;
}

function button(documentRef, className, text) {
  const node = documentRef.createElement('button');
  node.type = 'button';
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

/** The "↺ Volver a este punto · HH:MM" link on a user's message. */
function buildRewindLink(label, onClick, documentRef = document) {
  const link = documentRef.createElement('button');
  link.type = 'button';
  link.className = 'rewind-link';
  link.append(documentRef.createTextNode(label));
  link.addEventListener('click', onClick);
  return link;
}

/**
 * The line shown when the context store is down, in place of the confirm dialog: the model
 * from `rewind.js`'s `buildGraphDownModel` and a «Reintentar» button. `onRetry` takes no
 * arguments; the caller decides what a retry does (ask the main process to start the
 * store, then ask again). Returns the element; `retry` is its button, disabled by the
 * caller while the retry runs.
 */
function buildGraphDownLine(model, { onRetry }, documentRef = document) {
  const line = documentRef.createElement('div');
  line.className = 'rewind-graph-down';
  attr(line, 'role', 'status');
  const retry = button(documentRef, 'rewind-graph-retry', model.retryLabel);
  retry.addEventListener('click', () => {
    retry.disabled = true;
    onRetry();
  });
  line.append(span(documentRef, 'rewind-graph-text', model.text), retry);
  line.retry = retry;
  return line;
}

/**
 * The confirm dialog built from `rewind.js`'s `buildConfirmModel`. `onConfirm`/`onCancel`
 * take no arguments. The caller removes the dialog (`el.remove()`) and handles what comes
 * next (a conflict dialog, a result line, or nothing). Returns `{el, setInProgress}`.
 * While `POST /rewind` is in flight, `setInProgress(true)` disables both buttons and turns
 * the card into the canvas' «Volviendo a este punto…» pill (the confirm button carries that
 * label, `REWIND.inProgress`); `false` gives the dialog back, able to answer again.
 */
function buildConfirmDialog(model, { onConfirm, onCancel }, documentRef = document, { play = sfx } = {}) {
  const createdAt = Date.now();
  const card = documentRef.createElement('div');
  card.className = 'rewind-dialog';
  card.tabIndex = -1;

  const title = documentRef.createElement('div');
  title.className = 'rewind-dialog-title';
  title.append(documentRef.createTextNode(model.title));

  const body = documentRef.createElement('div');
  body.className = 'rewind-dialog-body';
  body.append(documentRef.createTextNode(model.body));

  const buttons = documentRef.createElement('div');
  buttons.className = 'rewind-dialog-buttons';
  const cancel = documentRef.createElement('button');
  cancel.type = 'button';
  cancel.textContent = model.cancelLabel;
  const confirm = documentRef.createElement('button');
  confirm.type = 'button';
  confirm.className = 'is-primary';
  confirm.textContent = model.confirmLabel;
  buttons.append(cancel, confirm);

  let settled = false;
  const settle = (accepted) => {
    if (settled) return; // a key and a click can race; same guard as the approval card
    settled = true;
    play(accepted ? 'click' : 'cancel');
    if (accepted) onConfirm();
    else onCancel();
  };
  cancel.addEventListener('click', () => settle(false));
  confirm.addEventListener('click', () => settle(true));

  card.addEventListener('keydown', (event) => {
    if (!isRealAnswerKey(event, card, createdAt)) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      settle(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      settle(false);
    }
  });

  card.append(title, body, buttons);
  // The canvas' `rvStart`: the dialog opens with `open`.
  play('open');

  const setInProgress = (inProgress) => {
    cancel.disabled = inProgress;
    confirm.disabled = inProgress;
    confirm.textContent = inProgress ? model.inProgressLabel : model.confirmLabel;
    setClass(card, 'is-working', inProgress);
    if (!inProgress) settled = false;
  };

  return { el: card, setInProgress };
}

/** Splits `parseUnifiedDiff`'s single-column line list into the two columns the canvas
 * shows side by side. The current version (about to be discarded) keeps context and
 * removed lines, under old-file numbers; the point's version (about to be restored) keeps
 * context and added lines, under new-file numbers. The server sends no separate
 * before/after text, so only the diff's context window is shown, not the whole file. */
function splitDiffColumns(lines) {
  const left = [];
  const right = [];
  for (const line of lines) {
    if (line.type === 'hunk') continue;
    if (line.type === 'context') {
      left.push(line);
      right.push(line);
    } else if (line.type === 'remove') {
      left.push(line);
    } else if (line.type === 'add') {
      right.push(line);
    }
  }
  return { left, right };
}

/** The line the conflict is about: the current version's first changed line, else the
 * point's (the canvas' «src/cart.ts · línea 21»). */
function firstChangedLine(lines) {
  const changed = lines.find((line) => line.type === 'remove') || lines.find((line) => line.type === 'add');
  return changed ? changed.lineNo : null;
}

function buildDiffColumn(heading, note, lines, side, documentRef) {
  const column = documentRef.createElement('div');
  column.className = `rewind-diff-column rewind-diff-column--${side}`;

  const header = documentRef.createElement('div');
  header.className = 'rewind-diff-column-header';
  const headingEl = documentRef.createElement('span');
  headingEl.className = 'rewind-diff-column-heading';
  headingEl.append(documentRef.createTextNode(heading));
  const noteEl = documentRef.createElement('span');
  noteEl.className = 'rewind-diff-column-note';
  noteEl.append(documentRef.createTextNode(note));
  header.append(headingEl, noteEl);

  const body = documentRef.createElement('div');
  body.className = 'diff-body';
  for (const line of lines) {
    const row = documentRef.createElement('div');
    row.className = `diff-line diff-line--${line.type}`;
    const gutter = documentRef.createElement('span');
    gutter.className = 'diff-line-no';
    gutter.append(documentRef.createTextNode(String(line.lineNo)));
    const code = documentRef.createElement('span');
    code.className = 'diff-line-text';
    code.append(documentRef.createTextNode(escapeControlChars(line.text)));
    row.append(gutter, code);
    body.append(row);
  }

  column.append(header, body);
  return column;
}

/** One conflicting file: a "▶ Ver diferencias en <path>" toggle (canvas: collapsed by
 * default, `rvDiff`/`rvDiffLabel`, its ▶ turning on the spring curve). It opens, on the
 * canvas' grid-row curve, «<path> · línea N» over the two labeled columns that
 * `splitDiffColumns`/`buildDiffColumn` build. Returns `[toggle, diff]`. */
function buildConflictFile(conflict, documentRef, play) {
  const toggle = button(documentRef, 'rewind-diff-toggle', conflict.showLabel);
  attr(toggle, 'aria-expanded', 'false');

  const diff = documentRef.createElement('div');
  diff.className = 'rewind-diff';
  const inner = documentRef.createElement('div');
  inner.className = 'rewind-diff-inner';
  const content = documentRef.createElement('div');
  content.className = 'rewind-diff-content';
  inner.append(content);
  diff.append(inner);

  let built = false;
  let open = false;
  toggle.addEventListener('click', () => {
    open = !open;
    play(open ? 'open' : 'close');
    toggle.textContent = open ? conflict.hideLabel : conflict.showLabel;
    attr(toggle, 'aria-expanded', String(open));
    setClass(diff, 'is-open', open);
    if (open && !built) {
      built = true;
      const lines = parseUnifiedDiff(conflict.diff);
      const { left, right } = splitDiffColumns(lines);
      const where = firstChangedLine(lines);
      if (where !== null) content.append(span(documentRef, 'rewind-diff-where', REWIND.diffWhere(conflict.path, where)));
      const columns = documentRef.createElement('div');
      columns.className = 'rewind-diff-columns';
      columns.append(
        buildDiffColumn(conflict.currentHeading, conflict.currentNote, left, 'current', documentRef),
        buildDiffColumn(conflict.pointHeading, conflict.pointNote, right, 'point', documentRef),
      );
      content.append(columns);
    }
  });

  return [toggle, diff];
}

/** A choice in the canvas' two-line layout: the choice, and a smaller subtitle under it
 * ("Conserva tu edición en …" / "Descarta tu edición"). Without `subtitle` there is no
 * second line at all. */
function buildChoice(className, label, subtitle, documentRef) {
  const node = button(documentRef, className);
  node.append(span(documentRef, 'rewind-choice-label', label));
  if (subtitle) node.append(span(documentRef, 'rewind-choice-subtitle', subtitle));
  return node;
}

/**
 * The conflict dialog ("No se puede volver del todo") with each conflicting file's diff
 * and the choices. `onLeave`/`onKeepOthers`/`onForceAll` take no arguments.
 *
 * With something to keep (`model.choices.keepOthers`): «Dejar como está», then the split
 * pill «Restaurar los otros N | ▼». Its menu offers «Restaurar los otros N» (with «Conserva
 * tu edición en …») and «Volver del todo» (with «Descarta tu edición»). When every file in
 * the plan conflicts there are no "others": «Volver del todo» is the rose pill itself, with
 * its consequence as the tooltip.
 *
 * Escape closes the menu when it is open, and otherwise answers "Dejar como está", the
 * least destructive choice, as Escape does on the approval card. Enter is left unbound on
 * purpose: no choice is the obvious default, and nothing destructive may happen on a
 * bubbled Enter.
 */
function buildConflictDialog(
  model,
  { onLeave, onKeepOthers, onForceAll },
  documentRef = document,
  { play = sfx } = {},
) {
  const createdAt = Date.now();
  const card = documentRef.createElement('div');
  card.className = 'rewind-dialog rewind-conflict-dialog';
  card.tabIndex = -1;

  const title = documentRef.createElement('div');
  title.className = 'rewind-dialog-title';
  title.append(documentRef.createTextNode(model.title));

  // A single conflicting path is set as code where the sentence names it (the canvas'
  // «<code>src/cart.ts</code> tiene cambios…»).
  const body = documentRef.createElement('div');
  body.className = 'rewind-dialog-body';
  const onlyPath = model.conflicts.length === 1 ? model.conflicts[0].path : null;
  if (onlyPath && model.body.startsWith(onlyPath)) {
    body.append(span(documentRef, 'rewind-code', onlyPath), documentRef.createTextNode(model.body.slice(onlyPath.length)));
  } else {
    body.append(documentRef.createTextNode(model.body));
  }

  const files = documentRef.createElement('div');
  files.className = 'rewind-conflict-files';
  for (const conflict of model.conflicts) {
    const file = documentRef.createElement('div');
    file.className = 'rewind-conflict-file';
    file.append(...buildConflictFile(conflict, documentRef, play));
    files.append(file);
  }

  const buttons = documentRef.createElement('div');
  buttons.className = 'rewind-dialog-buttons';

  let menu = null;
  let menuToggle = null;
  let closeTimer = null;
  let menuOpen = false;

  let settled = false;
  const settle = (action) => {
    if (settled) return;
    settled = true;
    closeMenu(false);
    if (action === 'leave') {
      play('cancel');
      onLeave();
    } else if (action === 'keepOthers') {
      play('click');
      onKeepOthers();
    } else if (action === 'forceAll') {
      play('click');
      onForceAll();
    }
  };

  const leave = buildChoice('rewind-choice rewind-choice--leave', model.choices.leave, null, documentRef);
  leave.addEventListener('click', () => settle('leave'));
  buttons.append(leave);

  // -- the split pill and its menu ------------------------------------------------------

  function outside(event) {
    if (!menuOpen) return;
    const target = event.target;
    if (menu.contains(target) || menuToggle.contains(target)) return;
    closeMenu(true);
  }

  function openMenu() {
    if (menuOpen) return;
    menuOpen = true;
    clearTimeout(closeTimer);
    play('open');
    menu.hidden = false;
    setClass(menu, 'is-closing', false);
    setClass(menuToggle, 'is-open', true);
    attr(menuToggle, 'aria-expanded', 'true');
    if (typeof documentRef.addEventListener === 'function') documentRef.addEventListener('pointerdown', outside, true);
  }

  function closeMenu(sound) {
    if (!menuOpen) return;
    menuOpen = false;
    if (sound) play('close');
    setClass(menu, 'is-closing', true);
    setClass(menuToggle, 'is-open', false);
    attr(menuToggle, 'aria-expanded', 'false');
    if (typeof documentRef.removeEventListener === 'function') documentRef.removeEventListener('pointerdown', outside, true);
    closeTimer = setTimeout(() => {
      if (!menuOpen) menu.hidden = true;
    }, CLOSE_MS);
  }

  if (model.choices.keepOthers) {
    const split = documentRef.createElement('span');
    split.className = 'rewind-split';
    const main = buildChoice('rewind-choice rewind-split-main', model.choices.keepOthers, null, documentRef);
    main.addEventListener('click', () => settle('keepOthers'));
    menuToggle = button(documentRef, 'rewind-split-toggle');
    attr(menuToggle, 'aria-haspopup', 'menu');
    attr(menuToggle, 'aria-expanded', 'false');
    attr(menuToggle, 'aria-label', model.choices.forceAll);
    menuToggle.append(span(documentRef, 'rewind-split-arrow', '▼'));
    menuToggle.addEventListener('click', () => (menuOpen ? closeMenu(true) : openMenu()));

    menu = documentRef.createElement('div');
    menu.className = 'rewind-split-menu';
    attr(menu, 'role', 'menu');
    menu.hidden = true;
    const keep = buildChoice('rewind-menu-item', model.choices.keepOthers, model.choices.keepOthersSubtitle, documentRef);
    const force = buildChoice('rewind-menu-item is-danger', model.choices.forceAll, model.choices.forceAllSubtitle, documentRef);
    for (const item of [keep, force]) attr(item, 'role', 'menuitem');
    keep.addEventListener('click', () => settle('keepOthers'));
    force.addEventListener('click', () => settle('forceAll'));
    menu.append(keep, force);

    split.append(main, menuToggle, menu);
    buttons.append(split);
  } else {
    const force = buildChoice('rewind-choice rewind-choice--force is-primary', model.choices.forceAll, null, documentRef);
    if (model.choices.forceAllSubtitle) force.title = model.choices.forceAllSubtitle;
    force.addEventListener('click', () => settle('forceAll'));
    buttons.append(force);
  }

  card.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && menuOpen) {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (!isRealAnswerKey(event, card, createdAt)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      settle('leave');
    }
  });

  card.append(title, body, files, buttons);
  // The canvas' `rvGo` outcome: a rewind that could not go all the way sounds `close`.
  play('close');
  return { el: card };
}

/**
 * "↺ Volviste a las HH:MM · N archivos · Rehacer": the settled state after a rewind,
 * stamped in as the canvas' blue pill (sounding `done`, or `close` when nothing was
 * restored). `onRedo` fires when the person clicks "Rehacer" (the canvas' `select`) and
 * returns a promise or a plain value. A rejection re-enables the button and shows
 * `model.redoFailedLabel` next to it, so a click never silently does nothing. On success
 * the button stays disabled: a redo is single-use.
 *
 * «Rehacer» is drawn only while it applies (`model.redoAvailable`, or `hasRestored` for a
 * model that does not say; if nothing was restored there is nothing to reverse).
 * `wrapper.retractRedo()` removes it once the conversation moves on: a new turn makes a
 * rewind final, and the window must never offer an action that would fail.
 * `model.failedText`, when present, gets its own line underneath so failures are visible.
 * `quiet` is for a marker read back from history, which is not happening now and makes no
 * sound.
 */
function buildResultLine(model, onRedo, documentRef = document, { play = sfx, quiet = false } = {}) {
  const wrapper = documentRef.createElement('div');
  wrapper.className = 'rewind-result-wrapper';

  const line = documentRef.createElement('div');
  line.className = 'rewind-result';
  const icon = span(documentRef, 'rewind-result-icon', '↺');
  attr(icon, 'aria-hidden', 'true');
  line.append(icon, span(documentRef, 'rewind-result-title', model.title), span(documentRef, 'rewind-result-detail', model.detail));

  let retract = () => {};
  const offered = typeof model.redoAvailable === 'boolean' ? model.redoAvailable : model.hasRestored;
  if (offered) {
    const redo = button(documentRef, 'rewind-redo');
    redo.append(documentRef.createTextNode(model.redoLabel));

    const error = documentRef.createElement('span');
    error.className = 'rewind-result-error';

    redo.addEventListener('click', () => {
      redo.disabled = true;
      error.textContent = '';
      play('select');
      Promise.resolve()
        .then(onRedo)
        .then(() => {
          // Stays disabled: the redo succeeded and is single-use on the server too.
        })
        .catch((failure) => {
          redo.disabled = false;
          // The store being down is said as that; «Rehacer» stays pressable as the retry.
          error.textContent = failure && failure.graphDown && model.graphDownLabel ? model.graphDownLabel : model.redoFailedLabel;
        });
    });
    line.append(redo, error);
    retract = () => {
      redo.remove();
      error.remove();
      retract = () => {};
    };
  }
  wrapper.append(line);
  wrapper.retractRedo = () => retract();

  if (model.failedText) {
    const failedLine = documentRef.createElement('div');
    failedLine.className = 'rewind-result-failed';
    failedLine.append(documentRef.createTextNode(model.failedText));
    wrapper.append(failedLine);
  }

  if (!quiet) play(model.hasRestored ? 'done' : 'close');
  return wrapper;
}

export { buildRewindLink, buildGraphDownLine, buildConfirmDialog, buildConflictDialog, buildResultLine };
