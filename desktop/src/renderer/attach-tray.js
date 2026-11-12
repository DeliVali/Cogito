'use strict';

/**
 * The composer's attachments (design 1n): drag, paste or pick, no commands.
 *
 * - A file dropped anywhere on the window, pasted, or chosen with «+ adjuntar» (the
 *   system's dialog, opened by the main process) is named to the core, which says what it
 *   is. The page never reads a file (`bridge` is `window.cogito.attach`).
 * - A paste of more than ten lines becomes a chip instead of filling the field.
 * - Each becomes a chip showing the core's facts, or why it cannot be attached. It is
 *   removed with its ✕ or with Delete/Backspace on it. «N adjuntos» beside «+ adjuntar»
 *   counts the ones that will be sent. A refused chip is only there to say why.
 *
 * Sounds, as the canvas plays them: `open` when a file is dragged over the window,
 * `select` when it is dropped, `close` when a chip is removed.
 */

import { chipView, countLabel, isLongPaste, baseName } from './attachments.js';
import { buildChip, buildDropLayer } from './render-attachments.js';
import { ATTACH } from './strings.js';

/** The largest image a provider accepts (10 MB once base64-encoded on Claude's API,
 * Anthropic's vision guide, 2026-10-17); the core checks it again. */
const MAX_IMAGE_BYTES = 7_500_000;
/** The canvas' removal: the chip shrinks and fades for 160 ms, then is gone at 170 ms. */
const LEAVE_MS = 170;
/** The drop layer fades out over 180 ms (the canvas' `dragOp`). */
const LAYER_LEAVE_MS = 180;

function hasFiles(event) {
  const types = event.dataTransfer && event.dataTransfer.types;
  return Boolean(types) && Array.from(types).includes('Files');
}

/**
 * `elements`: `{row, count, button, input, host}`: the chip row in the balloon, the count
 * beside «+ adjuntar», the button, the message field, and where the drop layer goes.
 * `bridge`: `{pick, inspect, pathOf}`. `canAttach()` says whether the window is showing the
 * conversation (no project picker or dialog over it). `onChange()` after every change.
 */
function createAttachTray({ elements, bridge, play = () => {}, canAttach = () => true, onChange = () => {}, ignoreDrag = () => false, documentRef = document, windowRef = window }) {
  const { row, count, button, input, host } = elements;
  /** `{id, spec, item, view, chip}` in the order attached. */
  let entries = [];
  let nextId = 1;

  row.setAttribute('role', 'list');
  row.setAttribute('aria-label', ATTACH.list);
  const layer = buildDropLayer(documentRef);
  host.append(layer);

  function changed() {
    row.hidden = entries.length === 0;
    count.textContent = countLabel(entries.map((entry) => entry.item));
    onChange();
  }

  function remove(entry, how) {
    if (!entries.includes(entry)) return;
    const index = entries.indexOf(entry);
    entries = entries.filter((other) => other !== entry);
    play('close');
    entry.chip.classList.add('is-leaving');
    // Keep keyboard focus nearby: the next chip, else the one before, else the field.
    if (how === 'keyboard') {
      const next = entries[index] || entries[index - 1];
      (next ? next.chip : input).focus();
    }
    setTimeout(() => entry.chip.remove(), LEAVE_MS);
    changed();
  }

  /** Draws one chip for `item` (`{attachment}`, `{refused}` or `{error}`) attached as
   * `spec`. Returns its id. */
  function add(spec, item, fallbackName) {
    const answer = item && (item.attachment || item.refused) ? item : { refused: { reason: 'failed', name: '' } };
    const entry = { id: nextId++, spec, item: answer, view: chipView(answer, fallbackName) };
    entry.chip = buildChip(entry.view, { onRemove: (how) => remove(entry, how) }, documentRef);
    entries.push(entry);
    row.append(entry.chip);
    changed();
    return entry.id;
  }

  async function inspectPath(path, name) {
    const answer = await bridge.inspect({ path }).catch(() => null);
    return add({ path }, answer, name || baseName(path));
  }

  /** An image with no file behind it (a screenshot pasted from the clipboard), sent to the
   * core as its bytes. Past the size a model accepts, refused here, before reading it. */
  async function inspectImage(file) {
    const name = file.name || ATTACH.kinds.image;
    if (file.size > MAX_IMAGE_BYTES) return add(null, { refused: { reason: 'image_too_large', name } }, name);
    let image = '';
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      image = btoa(binary);
    } catch {
      return add(null, { refused: { reason: 'unreadable', name } }, name);
    }
    const spec = { image, name };
    const answer = await bridge.inspect(spec).catch(() => null);
    return add(spec, answer, name);
  }

  /** Files from a drop or a paste. An image with no path on disk (a screenshot pasted from
   * the clipboard) goes as its bytes; anything else with no path cannot be named to the
   * core, so it is shown refused, by its type. */
  async function addFiles(files) {
    const ids = [];
    for (const file of Array.from(files || [])) {
      const path = bridge.pathOf(file);
      if (path) {
        ids.push(await inspectPath(path, file.name));
        continue;
      }
      if (/^image\/(png|jpeg|gif|webp)$/.test(String(file.type || ''))) {
        ids.push(await inspectImage(file));
        continue;
      }
      const type = String(file.type || '');
      const reason = type.startsWith('image/') ? 'image' : type.startsWith('video/') ? 'video' : type.startsWith('audio/') ? 'audio' : 'unreadable';
      ids.push(add(null, { refused: { reason, name: file.name || '' } }, file.name || ''));
    }
    return ids;
  }

  async function addText(text) {
    const answer = await bridge.inspect({ text }).catch(() => null);
    return add({ text }, answer, ATTACH.pastedText);
  }

  /** «+ adjuntar»: the system's file dialog, several files at once. */
  async function pick() {
    const result = await bridge.pick().catch(() => null);
    if (!result || result.canceled || !Array.isArray(result.items)) return [];
    return result.items.map((item) => add({ path: item.path }, item, baseName(item.path)));
  }

  button.addEventListener('click', () => {
    if (canAttach()) pick();
  });

  // A paste: files become chips, text of more than ten lines becomes one, and anything
  // else goes into the field as usual.
  input.addEventListener('paste', (event) => {
    const data = event.clipboardData;
    if (!data) return;
    if (data.files && data.files.length) {
      event.preventDefault();
      addFiles(data.files);
      return;
    }
    const text = data.getData('text/plain');
    if (!isLongPaste(text)) return;
    event.preventDefault();
    addText(text);
  });

  // Dropping a file anywhere on the window. The page handles every file drag: without
  // `preventDefault` the browser would open the dropped file in place of the app.
  let layerShown = false;
  let layerTimer = null;
  const showLayer = () => {
    if (layerShown) return;
    layerShown = true;
    clearTimeout(layerTimer);
    layer.hidden = false;
    layer.classList.remove('is-leaving');
    play('open');
  };
  const hideLayer = () => {
    if (!layerShown) return;
    layerShown = false;
    layer.classList.add('is-leaving');
    layerTimer = setTimeout(() => {
      if (!layerShown) layer.hidden = true;
    }, LAYER_LEAVE_MS);
  };
  // `ignoreDrag(event)`: a drag over a part of the window that takes files for itself (the
  // file explorer copies them into the project) is not an attachment.
  windowRef.addEventListener('dragenter', (event) => {
    if (!hasFiles(event)) return;
    if (ignoreDrag(event)) {
      hideLayer();
      return;
    }
    event.preventDefault();
    if (canAttach()) showLayer();
  });
  windowRef.addEventListener('dragover', (event) => {
    if (!hasFiles(event)) return;
    if (ignoreDrag(event)) {
      hideLayer();
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = canAttach() ? 'copy' : 'none';
  });
  windowRef.addEventListener('dragleave', (event) => {
    // Leaving the window, not moving between two of its elements.
    if (event.relatedTarget) return;
    hideLayer();
  });
  windowRef.addEventListener('drop', (event) => {
    if (!hasFiles(event) || ignoreDrag(event)) return;
    event.preventDefault();
    hideLayer();
    if (!canAttach()) return;
    play('select');
    addFiles(event.dataTransfer.files);
  });

  changed();

  return {
    /** Files named by their absolute path (the explorer's «Adjuntar al mensaje»), each
     * inspected by the core like a dropped one. */
    async addPaths(paths) {
      const ids = [];
      for (const path of paths) ids.push(await inspectPath(path, baseName(path)));
      return ids;
    },
    /** The absolute paths already attached and to be sent. */
    paths() {
      return entries.filter((entry) => entry.spec && entry.spec.path && !entry.item.refused).map((entry) => entry.spec.path);
    },
    /** What goes with the message: the spec of every chip that will be sent, and its view
     * for the sent message's chips. */
    ready() {
      return entries
        .filter((entry) => entry.spec && !entry.item.refused)
        .map((entry) => ({ spec: entry.spec, view: entry.view }));
    },
    /** How many chips there are, refused ones included. */
    size() {
      return entries.length;
    },
    /** Removes the chips in `ids`, or every chip when `ids` is null (the message took
     * them, or a rewind's redo took them back). */
    clear(ids = null) {
      const taken = ids ? entries.filter((entry) => ids.includes(entry.id)) : entries;
      if (!taken.length) return;
      entries = entries.filter((entry) => !taken.includes(entry));
      for (const entry of taken) entry.chip.remove();
      changed();
    },
    /** Puts a message's chips back on the composer after a rewind or a refused send. A
     * file is read again by its path, a paste by its text. `refused` (`{reason, name}`)
     * marks the one the core would not take. Returns their ids. */
    async restore(specs, refused = null) {
      const ids = [];
      for (const spec of Array.isArray(specs) ? specs : []) {
        if (spec && typeof spec.path === 'string') {
          if (refused && baseName(spec.path) === refused.name) ids.push(add(spec, { refused }, refused.name));
          else ids.push(await inspectPath(spec.path));
        } else if (spec && typeof spec.text === 'string') {
          ids.push(await addText(spec.text));
        }
      }
      return ids;
    },
    addFiles,
    addText,
    pick,
  };
}

export { createAttachTray };
