'use strict';

/**
 * DOM for automatic updates (design 3a/3b), built from `update.js`'s plain views: the
 * title-bar chip, the ready menu, the amber security strip, the «Actualizando a X» screen,
 * the package-manager card, and the thread separator. All text goes in through
 * `createTextNode`: release notes come from a signed file, but they are still data, never
 * markup.
 *
 * Each builder takes a `documentRef` (default: the page's `document`), as `render-rewind.js`
 * does, so the lab can build them against a fake document.
 */

import { sfx } from './sfx.js';

function el(documentRef, tag, className, text) {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.append(documentRef.createTextNode(text));
  return node;
}

function button(documentRef, className, text, onClick) {
  const node = el(documentRef, 'button', className, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

/** A small glass sphere, the same one the design draws in the menu and restart screen. */
function sphere(documentRef, className) {
  const node = el(documentRef, 'span', `update-sphere ${className}`);
  node.append(el(documentRef, 'span', 'update-sphere-shine'));
  return node;
}

function noteSections(documentRef, sections) {
  const box = el(documentRef, 'div', 'update-notes');
  for (const section of sections) {
    box.append(el(documentRef, 'span', 'update-notes-heading', section.heading));
    for (const item of section.items) {
      const row = el(documentRef, 'span', 'update-notes-item');
      row.append(el(documentRef, 'span', 'update-notes-bullet', '•'), el(documentRef, 'span', 'update-notes-text', item));
      box.append(row);
    }
  }
  return box;
}

/**
 * The chip beside the model. Downloading is a plain indicator (no menu, no sound); every
 * other kind is a button that calls `onClick`.
 */
export function buildChip(chip, { onClick }, documentRef = document) {
  if (chip.kind === 'downloading') {
    const node = el(documentRef, 'span', 'update-chip update-chip--downloading');
    node.setAttribute('role', 'status');
    node.title = chip.label; // the label ends in «…» when the bar is narrow
    const ring = el(documentRef, 'span', 'update-chip-progress');
    ring.style.setProperty('--update-deg', `${Math.round(chip.percent * 3.6)}deg`);
    node.append(ring, documentRef.createTextNode(chip.label));
    return node;
  }
  const node = button(documentRef, `update-chip update-chip--${chip.kind}`, null, onClick);
  // The tooltip carries a failed update's real reason; the chip's label is only the fixed
  // «No se pudo actualizar · Reintentar».
  node.title = chip.tooltip || chip.label; // the label ends in «…» when the bar is narrow
  node.append(el(documentRef, 'span', 'update-chip-dot'), documentRef.createTextNode(chip.label));
  return node;
}

/** The ready menu: title, «Instalada: X», notes, «Al salir» and the primary restart choice. */
export function buildMenu(menu, { onPrimary, onLater }, documentRef = document) {
  const node = el(documentRef, 'div', 'update-menu');
  node.setAttribute('role', 'dialog');
  const head = el(documentRef, 'div', 'update-menu-head');
  const titles = el(documentRef, 'span', 'update-menu-titles');
  titles.append(el(documentRef, 'span', 'update-menu-title', menu.title), el(documentRef, 'span', 'update-menu-installed', menu.installed));
  head.append(sphere(documentRef, 'update-sphere--menu'), titles);
  node.append(head);
  if (menu.sections.length) node.append(noteSections(documentRef, menu.sections));
  const actions = el(documentRef, 'div', 'update-menu-actions');
  actions.append(
    button(documentRef, 'update-pill', menu.later.label, () => onLater(menu.later.action)),
    el(documentRef, 'span', 'update-spacer'),
    button(documentRef, 'update-pill update-pill--primary', menu.primary.label, () => onPrimary(menu.primary.action)),
  );
  node.append(actions);
  return node;
}

/** The amber strip under the bar for a security update; it has no close button. */
export function buildBanner(banner, { onRestart }, documentRef = document) {
  const node = el(documentRef, 'div', 'update-strip');
  node.setAttribute('role', 'status');
  const text = el(documentRef, 'span', 'update-strip-text');
  text.append(el(documentRef, 'span', 'update-strip-title', banner.title), documentRef.createTextNode(` ${banner.text}`));
  node.append(el(documentRef, 'span', 'update-strip-dot'), text);
  if (banner.restart) node.append(button(documentRef, 'update-strip-button', banner.restart, onRestart));
  return node;
}

/** «Actualizando a X», over the whole thread, for the moment before the app closes. */
export function buildRestarting(restarting, documentRef = document) {
  const node = el(documentRef, 'div', 'update-restarting');
  const bar = el(documentRef, 'span', 'update-restarting-bar');
  bar.append(el(documentRef, 'span', 'update-restarting-fill'));
  node.append(sphere(documentRef, 'update-sphere--large'), el(documentRef, 'span', 'update-restarting-label', restarting.label), bar);
  return node;
}

/** The package-manager card: no download and no restart, only the command to run. It is
 * also the card behind a non-retryable failure's chip (`card.kind === 'failed'`). That view
 * has no `command`, so the card is just the reason and «Entendido». */
export function buildCard(card, { onCopy, onDismiss }, documentRef = document) {
  const node = el(documentRef, 'div', `update-card update-card--${card.kind}`);
  node.setAttribute('role', 'dialog');
  const head = el(documentRef, 'span', 'update-card-head');
  head.append(el(documentRef, 'span', 'update-card-dot'), documentRef.createTextNode(card.title));
  const body = el(documentRef, 'span', 'update-card-body');
  body.append(el(documentRef, 'span', 'update-card-text', card.body));
  if (card.command) {
    const command = el(documentRef, 'span', 'update-card-command');
    command.append(el(documentRef, 'span', 'update-card-command-text', card.command), button(documentRef, 'update-card-copy', card.copy, onCopy));
    body.append(command);
  }
  const foot = el(documentRef, 'span', 'update-card-foot');
  foot.append(el(documentRef, 'span', 'update-card-installed', card.installed), button(documentRef, 'update-pill', card.dismiss, onDismiss));
  body.append(foot);
  node.append(head, body);
  return node;
}

/**
 * The separator where the thread resumes after a restart. «— ACTUALIZADO A X —» carries
 * «▶ Ver novedades», which unfolds the notes in place; «— SE MANTUVO X · NO SE PUDO
 * ACTUALIZAR —» is drawn in the error tone and has nothing to unfold. Unfolding plays
 * `open`, folding plays `close`.
 */
export function buildSeparator(separator, documentRef = document, { play = sfx } = {}) {
  const node = el(documentRef, 'div', `update-separator update-separator--${separator.kind}`);
  const line = el(documentRef, 'div', 'update-separator-line');
  line.append(el(documentRef, 'span', 'update-separator-rule'), el(documentRef, 'span', 'update-separator-label', separator.label), el(documentRef, 'span', 'update-separator-rule'));
  node.append(line);
  if (!separator.show) return node;

  const label = el(documentRef, 'span', 'update-separator-toggle-label', separator.show);
  const toggle = button(documentRef, 'update-separator-toggle', null, () => {
    const open = !node.classList.contains('is-open');
    // The canvas' `upNotesToggle`.
    play(open ? 'open' : 'close');
    node.classList.toggle('is-open', open);
    label.textContent = open ? separator.hide : separator.show;
    toggle.setAttribute('aria-expanded', String(open));
  });
  toggle.setAttribute('aria-expanded', 'false');
  toggle.append(el(documentRef, 'span', 'update-separator-arrow', '▶'), label);
  const fold = el(documentRef, 'div', 'update-separator-fold');
  const inner = el(documentRef, 'div', 'update-separator-fold-inner');
  inner.append(noteSections(documentRef, separator.sections));
  fold.append(inner);
  node.append(toggle, fold);
  return node;
}
