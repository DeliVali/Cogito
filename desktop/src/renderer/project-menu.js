'use strict';

/**
 * The header's project menu (design 1a, `projectOpen`): «PROYECTOS RECIENTES», each recent
 * project with its path, when it was last opened and ✓ on the open one, then «Abrir
 * carpeta…». It drops from the project chip at the left of the bar, opens with the
 * handoff's curve, closes on a click outside with the 140 ms shrink + fade (styles.css,
 * `.menu`), and plays the canvas' `open` / `close` / `select` sounds.
 *
 * Every row is a real recent project (`GET /settings/projects`, through `api.list`). A
 * folder that no longer exists cannot be chosen: its row says so once and offers «Quitar de
 * la lista» (`api.forget`), as the full picker does, so a missing entry never has to stay.
 * Nothing switches while a turn runs: every row is disabled, like the project chip.
 *
 * `api`: `{ list(): Promise<{projects}>, pickFolder(): Promise<{path}|{canceled}>,
 * open(path): Promise<result|{error}>, isBusy(): boolean, current(): string|null,
 * onOpened(result), play(kind) }`, plus optional `forget(path): Promise<{projects}>`,
 * `onOpenFailed(result, path)`, `onOpening(path)` and `onSettled()`, the last two around an
 * open in flight.
 */

import { PROJECT_MENU } from './strings.js';
import { escapeControlChars } from './sanitize.js';

const CLOSE_MS = 140;

/** «Hace 12 min», «Ayer», «Hace 3 días»: the canvas' relative form, from `last_opened`. */
function relativeWhen(iso, now = Date.now()) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const minutes = Math.max(0, Math.round((now - then) / 60000));
  if (minutes < 1) return PROJECT_MENU.justNow;
  if (minutes < 60) return PROJECT_MENU.minutesAgo(minutes);
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  if (then >= startOfToday.getTime()) return PROJECT_MENU.hoursAgo(Math.round(minutes / 60));
  const days = Math.ceil((startOfToday.getTime() - then) / 86_400_000);
  return days <= 1 ? PROJECT_MENU.yesterday : PROJECT_MENU.daysAgo(days);
}

function node(documentRef, tag, className, text) {
  const element = documentRef.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = escapeControlChars(String(text));
  return element;
}

/** A displayed path split where its last folder starts: `~/a/b/proj` -> `{head: '~/a/b',
 * tail: '/proj'}`. A trailing separator stays with the tail. Nothing to split gives an
 * empty head. */
function splitPath(display) {
  const text = String(display == null ? '' : display);
  const body = text.replace(/[\\/]+$/, '');
  const cut = Math.max(body.lastIndexOf('/'), body.lastIndexOf('\\'));
  if (cut <= 0) return { head: '', tail: text };
  return { head: text.slice(0, cut), tail: text.slice(cut) };
}

/** A path that gives up its middle, not its end: the folder's own name (the tail) is drawn
 * whole while the head before it takes the «…» (styles.css, `.path-split`). The two spans
 * read as the whole path, so its text and what a screen reader says are unchanged. */
function pathNode(documentRef, className, display) {
  const outer = node(documentRef, 'span', `${className} path-split`);
  const { head, tail } = splitPath(display);
  const headNode = node(documentRef, 'span', 'path-split-head', head);
  // What the tail does not need: its length in `ch`, as the paths are drawn in monospace.
  headNode.style.maxWidth = `calc(100% - ${tail.length}ch)`;
  outer.append(headNode, node(documentRef, 'span', 'path-split-tail', tail));
  return outer;
}

function attachProjectMenu(chip, api, documentRef = document) {
  const play = api.play || (() => {});
  let menu = null;

  function items() {
    return menu ? [...menu.querySelectorAll('button')].filter((button) => !button.disabled) : [];
  }

  function moveFocus(delta) {
    const list = items();
    if (!list.length) return;
    const at = list.indexOf(documentRef.activeElement);
    list[at === -1 ? 0 : (at + delta + list.length) % list.length].focus();
  }

  function close(sound = true, { returnFocus = true } = {}) {
    if (!menu) return;
    const closing = menu;
    menu = null;
    closing.classList.add('is-closing');
    chip.setAttribute('aria-expanded', 'false');
    if (sound) play('close');
    setTimeout(() => closing.remove(), CLOSE_MS);
    documentRef.removeEventListener('mousedown', outside, true);
    documentRef.removeEventListener('keydown', onKeydown, true);
    if (returnFocus) chip.focus({ focusVisible: false });
  }

  function outside(event) {
    if (menu && !menu.contains(event.target) && !chip.contains(event.target)) close(true, { returnFocus: false });
  }

  function onKeydown(event) {
    if (!menu) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveFocus(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveFocus(-1);
    }
  }

  async function choose(path) {
    if (api.isBusy()) return;
    play('select');
    close(false, { returnFocus: false });
    if (typeof api.onOpening === 'function') api.onOpening(path);
    const result = await Promise.resolve(api.open(path)).catch((error) => ({ error: error.message }));
    if (typeof api.onSettled === 'function') api.onSettled();
    if (!result || result.error) {
      play('cancel');
      // The window says why: the connection screen (no model served), or the picker.
      if (typeof api.onOpenFailed === 'function') api.onOpenFailed(result, path);
      return;
    }
    api.onOpened(result);
  }

  async function openFolder() {
    if (api.isBusy()) return;
    play('click');
    close(false, { returnFocus: false });
    const picked = await Promise.resolve(api.pickFolder()).catch(() => null);
    if (picked && picked.path) choose(picked.path);
  }

  /** Drops a missing folder from the list (the entry only; nothing on disk is touched),
   * then draws the list again. Focus stays in the menu: on the next missing row's button, or
   * on «Abrir carpeta…» when none is left. */
  async function forget(path, index) {
    if (typeof api.forget !== 'function') return;
    play('click');
    await Promise.resolve(api.forget(path)).catch(() => null);
    if (!menu) return;
    const answer = await Promise.resolve(api.list()).catch(() => null);
    if (!menu) return;
    const group = menu.querySelector('.project-menu-recent');
    if (!group) return;
    renderList(group, (answer && answer.projects) || []);
    const buttons = [...menu.querySelectorAll('.project-menu-forget')];
    const next = buttons[Math.min(index, buttons.length - 1)];
    (next || menu.querySelector('.menu-item')).focus({ focusVisible: false });
  }

  function missingRow(entry, display, missingIndex) {
    const row = node(documentRef, 'div', 'project-menu-item project-menu-item--missing');
    row.title = escapeControlChars(String(entry.path));
    const text = node(documentRef, 'span', 'project-menu-text');
    text.append(node(documentRef, 'span', 'project-menu-name', entry.name), pathNode(documentRef, 'project-menu-path', display));
    const line = node(documentRef, 'span', 'project-menu-missing-line');
    line.append(node(documentRef, 'span', 'project-menu-note', PROJECT_MENU.missingNote));
    if (typeof api.forget === 'function') {
      const forgetButton = node(documentRef, 'button', 'project-menu-forget', PROJECT_MENU.forget);
      forgetButton.type = 'button';
      forgetButton.setAttribute('role', 'menuitem');
      forgetButton.addEventListener('click', () => forget(entry.path, missingIndex));
      line.append(forgetButton);
    }
    text.append(line);
    row.append(node(documentRef, 'span', 'project-menu-check', ''), text);
    return row;
  }

  function renderList(group, projects) {
    group.replaceChildren();
    if (!projects.length) {
      group.append(node(documentRef, 'div', 'project-menu-empty', PROJECT_MENU.empty));
      return;
    }
    const busy = Boolean(api.isBusy());
    const current = api.current();
    let missingIndex = 0;
    for (const entry of projects) {
      const display = entry.display || entry.path;
      if (entry.missing) {
        group.append(missingRow(entry, display, missingIndex));
        missingIndex += 1;
        continue;
      }
      const isCurrent = Boolean(current) && (display === current || entry.path === current);
      const row = node(documentRef, 'button', 'project-menu-item');
      row.type = 'button';
      row.setAttribute('role', 'menuitem');
      row.title = escapeControlChars(String(entry.path));
      row.disabled = busy;
      const text = node(documentRef, 'span', 'project-menu-text');
      text.append(node(documentRef, 'span', 'project-menu-name', entry.name), pathNode(documentRef, 'project-menu-path', display));
      row.append(
        node(documentRef, 'span', 'project-menu-check', isCurrent ? '✓' : ''),
        text,
        node(documentRef, 'span', 'project-menu-when', relativeWhen(entry.last_opened)),
      );
      row.addEventListener('click', () => {
        if (isCurrent) close();
        else choose(entry.path);
      });
      group.append(row);
    }
  }

  function open() {
    if (menu || api.isBusy()) return;
    menu = node(documentRef, 'div', 'menu project-menu');
    menu.setAttribute('role', 'menu');
    menu.append(node(documentRef, 'div', 'menu-heading', PROJECT_MENU.heading));
    const recent = node(documentRef, 'div', 'menu-group project-menu-recent');
    menu.append(recent);
    const actions = node(documentRef, 'div', 'menu-group');
    const folder = node(documentRef, 'button', 'menu-item', PROJECT_MENU.openFolder);
    folder.type = 'button';
    folder.setAttribute('role', 'menuitem');
    folder.addEventListener('click', openFolder);
    actions.append(folder);
    menu.append(actions);

    documentRef.body.append(menu);
    const rect = chip.getBoundingClientRect();
    menu.style.top = `${Math.round(rect.bottom + 2)}px`;
    menu.style.left = `${Math.max(8, Math.round(rect.left))}px`;
    chip.setAttribute('aria-expanded', 'true');
    play('open');
    documentRef.addEventListener('mousedown', outside, true);
    documentRef.addEventListener('keydown', onKeydown, true);
    folder.focus({ focusVisible: false });

    Promise.resolve(api.list())
      .then(({ projects = [] } = {}) => {
        if (menu) renderList(recent, projects);
      })
      .catch(() => {
        if (menu) recent.replaceChildren(node(documentRef, 'div', 'project-menu-empty', PROJECT_MENU.loadError));
      });
  }

  chip.setAttribute('aria-expanded', 'false');
  chip.addEventListener('click', () => (menu ? close() : open()));

  return { open, close, isOpen: () => menu !== null };
}

export { attachProjectMenu, relativeWhen, splitPath, pathNode };
