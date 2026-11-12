'use strict';

/**
 * The DOM of attachments (design 1n): a chip (removable on the composer, or above a sent
 * message) and the layer a file is dropped on. Built from `attachments.js`'s `chipView`.
 * `documentRef` comes last, as in the other `render-*` modules, so `cogito-lab` can build
 * them against a fake document.
 *
 * A composer chip is focusable and announces its name and the line under it to assistive
 * technology. Delete or Backspace removes it, as its ✕ does with a pointer. The ✕ stays out
 * of the tab order, so each chip is one Tab stop.
 */

import { ATTACH } from './strings.js';

function span(documentRef, className, text) {
  const node = documentRef.createElement('span');
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/**
 * One chip. `onRemove` makes it the composer's (focusable, ✕, Delete/Backspace); without it
 * the chip is the sent message's, which nothing removes.
 */
function buildChip(view, { onRemove = null } = {}, documentRef = document) {
  const chip = span(documentRef, `attach-chip attach-chip--${view.tone}`);
  chip.setAttribute('role', 'listitem');
  chip.setAttribute('aria-label', view.label);
  const badge = span(documentRef, 'attach-chip-badge', view.badge);
  badge.setAttribute('aria-hidden', 'true');
  const text = span(documentRef, 'attach-chip-text');
  text.setAttribute('aria-hidden', 'true');
  text.append(span(documentRef, 'attach-chip-name', view.name), span(documentRef, 'attach-chip-sub', view.sub));
  chip.append(badge, text);
  if (typeof onRemove === 'function') {
    chip.tabIndex = 0;
    chip.setAttribute('aria-keyshortcuts', 'Delete Backspace');
    const remove = documentRef.createElement('button');
    remove.type = 'button';
    remove.className = 'attach-chip-remove';
    remove.tabIndex = -1;
    remove.setAttribute('aria-label', ATTACH.remove(view.name));
    remove.textContent = '✕';
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      onRemove('pointer');
    });
    chip.addEventListener('keydown', (event) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') return;
      event.preventDefault();
      onRemove('keyboard');
    });
    chip.append(remove);
  }
  return chip;
}

/** The chips above a sent message, in the order they were attached. */
function buildSentChips(views, documentRef = document) {
  const row = documentRef.createElement('div');
  row.className = 'said-attachments';
  row.setAttribute('role', 'list');
  row.setAttribute('aria-label', ATTACH.list);
  for (const view of views) row.append(buildChip(view, {}, documentRef));
  return row;
}

/** «Suelta para adjuntar», over everything under the bar while a file is dragged over the
 * window. Hidden otherwise. */
function buildDropLayer(documentRef = document) {
  const layer = documentRef.createElement('div');
  layer.className = 'drop-layer';
  layer.hidden = true;
  layer.setAttribute('aria-hidden', 'true');
  const card = documentRef.createElement('div');
  card.className = 'drop-card';
  card.append(span(documentRef, 'drop-title', ATTACH.dropTitle), span(documentRef, 'drop-detail', ATTACH.dropDetail));
  layer.append(card);
  return layer;
}

export { buildChip, buildSentChips, buildDropLayer };
