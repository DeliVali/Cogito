'use strict';

/**
 * What a chip on the composer (design 1n) says, built from the core's answer about an
 * attachment, never from the file itself, which the page never reads. Pure (no DOM, no
 * bridge), so `cogito-lab` checks every case in plain Node.
 *
 * A chip has the canvas' three parts: a 30 px badge with the file's type (`TS`, `ERR`,
 * `MOV`), the name, and one line under it. An accepted attachment reads «Archivo del
 * proyecto · 1 KB», «Archivo · 3 KB» or «Texto · 14 líneas». A refused one gives its reason
 * in a word or two: «Video · no compatible», «Más de 20 MB», «No se puede leer»,
 * «Escaneado · sin texto». A document (PDF, Word, Excel...) the core turned into text is a
 * file like any other, and its badge says which.
 */

import { ATTACH } from './strings.js';

/** The canvas' threshold: «Pegar texto largo (más de 10 líneas) lo convierte en una pieza
 * adjunta en vez de llenar el campo.» */
const PASTE_LINES = 10;

/** Lines as the core counts them (`context.attachments._lines`): a last line without a
 * newline counts, and a final newline does not start another. */
function lineCount(text) {
  if (!text) return 0;
  const breaks = (text.match(/\n/g) || []).length;
  return breaks + (text.endsWith('\n') ? 0 : 1);
}

/** Whether a paste becomes an attachment instead of text in the field. */
function isLongPaste(text) {
  return typeof text === 'string' && lineCount(text) > PASTE_LINES;
}

/** "820 B", "12 KB", "1,4 MB": decimal units, as the design writes sizes. */
function formatBytes(bytes) {
  const n = Math.max(0, Math.round(Number(bytes) || 0));
  if (n < 1000) return `${n} B`;
  if (n < 1_000_000) return `${Math.max(1, Math.round(n / 1000))} KB`;
  return `${(n / 1_000_000).toFixed(1).replace('.', ',').replace(/,0$/, '')} MB`;
}

/** The badge's text: the extension in upper case, like the canvas' `TS` and `MOV`. */
function extensionOf(name) {
  const base = String(name || '').split(/[\\/]/).pop();
  const dot = base.lastIndexOf('.');
  if (dot <= 0 || dot === base.length - 1) return '';
  const ext = base.slice(dot + 1);
  return /^[A-Za-z0-9]{1,4}$/.test(ext) ? ext.toUpperCase() : '';
}

/** The last part of a path, for a refusal the core names by the file's own name. */
function baseName(path) {
  return String(path || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || String(path || '');
}

/**
 * A chip's view. `item` is `{attachment}` (the core's facts) or `{refused: {reason, name}}`.
 * `fallbackName` names a refusal the core could not name. `tone` is `file`, `error`
 * (a paste that reads as an error) or `refused`.
 */
function chipView(item, fallbackName = '') {
  if (item && item.refused) {
    const { reason } = item.refused;
    const name = item.refused.name || fallbackName;
    let sub;
    if (reason === 'too_large') sub = ATTACH.tooLarge;
    else if (reason === 'image_too_large') sub = ATTACH.imageTooLarge;
    else if (reason === 'unreadable') sub = ATTACH.unreadable;
    else if (reason === 'encrypted') sub = ATTACH.encrypted;
    else if (reason === 'scanned') sub = ATTACH.scanned;
    else if (reason === 'corrupt') sub = ATTACH.corrupt;
    else if (ATTACH.kinds[reason]) sub = `${ATTACH.kinds[reason]} · ${ATTACH.notSupported}`;
    else sub = ATTACH.failed;
    const badge = reason === 'folder' ? '/' : extensionOf(name) || '?';
    return { tone: 'refused', badge, name, sub, label: `${name}, ${sub}` };
  }
  const facts = (item && item.attachment) || item || {};
  if (facts.kind === 'text') {
    const name = facts.error ? ATTACH.pastedError : ATTACH.pastedText;
    const sub = `${ATTACH.text} · ${ATTACH.lines(facts.lines)}`;
    return { tone: facts.error ? 'error' : 'file', badge: facts.error ? 'ERR' : 'TXT', name, sub, label: `${name}, ${sub}` };
  }
  const name = facts.name || fallbackName;
  if (facts.kind === 'image') {
    // An image the model in use cannot see still goes (as a line saying so); the chip says
    // it first, in amber, so nobody waits for an answer about it.
    const badge = extensionOf(name) || 'IMG';
    if (facts.seen === false) {
      const sub = `${ATTACH.kinds.image} · ${ATTACH.unseen}`;
      return { tone: 'warn', badge, name, sub, label: `${name}, ${sub}` };
    }
    const size = facts.width && facts.height ? `${facts.width} × ${facts.height} · ` : '';
    const sub = `${ATTACH.kinds.image} · ${size}${formatBytes(facts.bytes)}`;
    return { tone: 'file', badge, name, sub, label: `${name}, ${sub}` };
  }
  const sub = `${facts.inside ? ATTACH.projectFile : ATTACH.file} · ${formatBytes(facts.bytes)}`;
  return { tone: 'file', badge: extensionOf(name) || 'TXT', name, sub, label: `${name}, ${sub}` };
}

/** «1 adjunto», «3 adjuntos», '' with none: the count beside «+ adjuntar», which never
 * counts a refused one (the canvas' `attCount`). */
function countLabel(items) {
  const n = items.filter((item) => item && !item.refused).length;
  return n ? ATTACH.count(n) : '';
}

export { PASTE_LINES, lineCount, isLongPaste, formatBytes, extensionOf, baseName, chipView, countLabel };
