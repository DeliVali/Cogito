'use strict';

/**
 * The model's prose as a person reads it in Claude Code: headings, paragraphs, lists (with
 * nesting and task boxes), block quotes, GFM pipe tables, rules, fenced code, and inline
 * `code`, **strong**, *em*, ~~struck~~ and [links](url). Tool rows are drawn elsewhere
 * (`tool-rows.js`, `render-tool-rows.js`).
 *
 * Safe by construction: every piece of the model's text reaches the page through
 * `createTextNode`, never as markup, so nothing it writes can become an element, an
 * attribute or a script. A link is shown as its text and its address, never as an anchor:
 * a click would navigate the app's own window. Text with half-written syntax (a stream not
 * finished yet) renders as what it is so far: an open fence is a code block, an unclosed
 * `*` is an asterisk.
 *
 * Each fenced block gets the collapsible chrome the design uses for a tool block. With
 * `{runnable: true}` (a settled reply in the thread, never a streaming one or the file
 * viewer), a shell block also gets «Ejecutar» (▶): a `.code-run` button carrying what it
 * types in `data-command` (`runnableCommand`) and showing it in its tooltip before the
 * click. The thread listens for it once (chat.js) and opens a terminal tab.
 */

import { MARKDOWN_COPY } from './strings.js';
import { makeExpander } from './expander.js';

/** How many lines a fenced block can show before it starts collapsed. */
const COLLAPSE_AT = 14;

/** Fence tags that mean a shell block. */
const SHELL_TAGS = new Set(['bash', 'sh', 'shell', 'console', 'zsh', 'fish']);
/** The core's limit on what «Ejecutar» types. */
const MAX_COMMAND = 16000;
// What the core refuses in a terminal command: C0 controls but the line break (a tab too),
// DEL, C1, and the invisible and bidi format characters that can make the block read
// differently from what runs.
const REFUSED = /[\u0000-\u0009\u000b-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/;
/** How much of the command «Ejecutar»'s tooltip shows. */
const PREVIEW_LINES = 6;
const PREVIEW_WIDTH = 100;

/** The commands a block's «Ejecutar» types, one per line, or null when it gets no button.
 *
 * - A `console` block shows a session: only its `$ ` lines are commands (prompt removed).
 *   A `> ` or bare `>` line right after a command or another such line is the shell's
 *   second prompt, so it continues that command (prompt removed): a here-document, a loop
 *   or a quote over several lines runs whole. After a line ending in `\`, a line without
 *   `>` gets no button. Output lines never run.
 * - Any other shell block runs as shown, line by line, each losing a leading `$ `, except
 *   inside a here-document's body, which is typed as written.
 *
 * Blank lines around the block, a common indent and the block's trailing spaces go; a line's
 * own trailing spaces stay (an escaped space, a here-document's body). A block with a
 * character the core refuses, or longer than it takes, gets no button. */
function runnableCommand(lang, code) {
  const tag = String(lang || '').toLowerCase();
  if (!SHELL_TAGS.has(tag)) return null;
  const text = String(code);
  if (REFUSED.test(text)) return null;
  let lines = text.split('\n');
  if (tag === 'console') {
    const commands = [];
    let inCommand = false;
    let continued = false;
    for (const line of lines) {
      const more = inCommand ? /^\s*>(?: (.*))?$/.exec(line) : null;
      if (more) {
        commands.push(more[1] ?? '');
        continued = commands[commands.length - 1].endsWith('\\');
        continue;
      }
      if (continued) return null;
      const prompt = /^\s*\$ (.*)$/.exec(line);
      inCommand = Boolean(prompt);
      if (!prompt) continue;
      commands.push(prompt[1]);
      continued = prompt[1].endsWith('\\');
    }
    if (continued) return null;
    lines = commands;
  } else {
    lines = dropPrompts(lines);
  }
  while (lines.length && !lines[0].trim()) lines.shift();
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  if (!lines.length) return null;
  const indent = Math.min(...lines.filter((line) => line.trim()).map((line) => /^ */.exec(line)[0].length));
  const command = lines.map((line) => line.slice(indent)).join('\n').trimEnd();
  if (command.length > MAX_COMMAND) return null;
  return command;
}

// A here-document's start: `<<WORD`, `<<-WORD`, `<<'WORD'` or `<<"WORD"`, never a `<<<`
// here-string. A word starts with a letter or `_`, so `1<<2` is a shift, not a body.
const HEREDOC = /(?<!<)<<(?!<)-?\s*(['"]?)([A-Za-z_][\w.-]*)\1/g;

/** `lines` with a leading `$ ` dropped from each, outside here-document bodies. A body ends
 * at a line that is its word alone, ignoring spaces around it. */
function dropPrompts(lines) {
  const pending = [];
  return lines.map((line) => {
    if (pending.length) {
      if (line.trim() === pending[0]) pending.shift();
      return line;
    }
    const out = line.replace(/^(\s*)\$(?: |$)/, '$1');
    for (const match of out.matchAll(HEREDOC)) pending.push(match[2]);
    return out;
  });
}

/** «Ejecutar»'s tooltip: what it will type, its first lines, each cut to a width. */
function runPreview(command) {
  const lines = String(command).split('\n');
  const shown = lines
    .slice(0, PREVIEW_LINES)
    .map((line) => (line.length > PREVIEW_WIDTH ? `${line.slice(0, PREVIEW_WIDTH)}…` : line));
  if (lines.length > PREVIEW_LINES) shown.push('…');
  return `${MARKDOWN_COPY.runTip}:\n${shown.join('\n')}`;
}

const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/;
const HEADING = /^ {0,3}(#{1,6})(?:\s+(.*?))?\s*#*\s*$/;
const RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^ {0,3}> ?(.*)$/;
const ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:\s+(.*))?$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

/** Inline spans, earliest first: a code span, a backslash escape, strong, struck, em, a
 * link, an autolink. Underscores mark emphasis only outside a word, so `snake_case_name`
 * stays as written. */
const INLINE =
  /(`+)([^`][\s\S]*?|[\s\S]*?[^`])\1(?!`)|\\([\\`*_{}[\]()#+\-.!|~>])|\*\*(?=\S)([\s\S]*?\S)\*\*|(?<![\w])__(?=\S)([\s\S]*?\S)__(?![\w])|~~(?=\S)([\s\S]*?\S)~~|\*(?=[^\s*])([\s\S]*?[^\s*])\*(?!\*)|(?<![\w])_(?=[^\s_])([\s\S]*?[^\s_])_(?![\w])|\[([^\]\n]+)\]\(\s*<?((?:[^()\s<>]|\([^()\s<>]*\))+)>?(?:\s+"[^"]*")?\s*\)|<(https?:\/\/[^\s>]+)>/g;

function el(documentRef, tag, className) {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  return node;
}

/** `text` with its inline spans resolved, appended to `parent`. */
function appendInline(parent, text, documentRef = document) {
  const pattern = new RegExp(INLINE.source, 'g');
  let cursor = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > cursor) parent.append(documentRef.createTextNode(text.slice(cursor, match.index)));
    cursor = pattern.lastIndex;
    const [, ticks, code, escaped, strong, strongUnder, struck, em, emUnder, linkText, linkUrl, autolink] = match;
    if (ticks) {
      const node = el(documentRef, 'code', 'inline-code');
      // CommonMark: one space on each side is padding when both are there.
      const body = /^ .* $/.test(code) && code.trim() ? code.slice(1, -1) : code;
      node.append(documentRef.createTextNode(body));
      parent.append(node);
    } else if (escaped) {
      parent.append(documentRef.createTextNode(escaped));
    } else if (strong !== undefined || strongUnder !== undefined) {
      const node = el(documentRef, 'strong');
      appendInline(node, strong ?? strongUnder, documentRef);
      parent.append(node);
    } else if (struck !== undefined) {
      const node = el(documentRef, 'del');
      appendInline(node, struck, documentRef);
      parent.append(node);
    } else if (em !== undefined || emUnder !== undefined) {
      const node = el(documentRef, 'em');
      appendInline(node, em ?? emUnder, documentRef);
      parent.append(node);
    } else if (linkText !== undefined) {
      appendLink(parent, linkText, linkUrl, documentRef);
    } else if (autolink !== undefined) {
      appendLink(parent, autolink, autolink, documentRef);
    }
  }
  if (cursor < text.length) parent.append(documentRef.createTextNode(text.slice(cursor)));
}

/** A link's text, then its address when the text is not already it. Never an anchor. */
function appendLink(parent, text, url, documentRef) {
  const node = el(documentRef, 'span', 'prose-link');
  appendInline(node, text, documentRef);
  node.title = url;
  parent.append(node);
  if (text.trim() !== url) {
    const address = el(documentRef, 'span', 'prose-link-url');
    address.append(documentRef.createTextNode(` (${url})`));
    parent.append(address);
  }
}

/** One paragraph, keeping the model's line breaks. */
function buildParagraph(lines, documentRef) {
  const p = el(documentRef, 'div', 'prose');
  lines.forEach((line, i) => {
    if (i > 0) p.append(documentRef.createElement('br'));
    appendInline(p, line.trim(), documentRef);
  });
  return p;
}

const SVG = 'http://www.w3.org/2000/svg';

/** «Ejecutar»'s mark: a play triangle drawn as a line, the way the composer draws its icons.
 * Its centre of mass sits on the box's centre, so it does not look pushed to one side. */
function playIcon(documentRef) {
  if (typeof documentRef.createElementNS !== 'function') return null;
  const svg = documentRef.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = documentRef.createElementNS(SVG, 'path');
  path.setAttribute('d', 'M8.5 5.5v13L19 12z');
  svg.append(path);
  return svg;
}

/** A collapsible fenced code block, styled like the design's tool-block chrome. */
function buildCodeBlock(lang, code, documentRef, options = {}) {
  const lines = code.replace(/\n$/, '').split('\n');
  const collapsedByDefault = lines.length > COLLAPSE_AT;

  const block = el(documentRef, 'div', 'tool-block');
  const header = el(documentRef, 'div', 'tool-block-header');
  const arrow = el(documentRef, 'span', 'tool-block-arrow');
  arrow.textContent = '▶';
  const label = el(documentRef, 'span', 'tool-block-label');
  const verb = el(documentRef, 'span', 'tool-block-verb');
  verb.textContent = lang ? MARKDOWN_COPY.codeWithLang(lang) : MARKDOWN_COPY.code;
  label.append(verb);
  const meta = el(documentRef, 'span', 'tool-block-meta');
  meta.textContent = MARKDOWN_COPY.lines(lines.length);
  const spacer = el(documentRef, 'span', 'tool-block-spacer');
  header.append(arrow, label, spacer, meta);
  // A block that starts collapsed gets no ▶: it would run lines the person cannot see, with
  // no card to show them (review 2026-11-01).
  const command = options.runnable && !collapsedByDefault ? runnableCommand(lang, code) : null;
  if (command) {
    const run = el(documentRef, 'button', 'code-run');
    run.type = 'button';
    const icon = playIcon(documentRef);
    if (icon) run.append(icon);
    run.title = runPreview(command);
    run.setAttribute('aria-label', MARKDOWN_COPY.runTip);
    run.dataset.command = command;
    header.append(run);
  }

  const pre = el(documentRef, 'pre');
  pre.textContent = lines.join('\n');
  const { body } = makeExpander({ header, arrow, content: pre, open: !collapsedByDefault, documentRef });
  block.append(header, body);
  return block;
}

/** A table row's cells: split on pipes outside code spans, `\|` kept as a pipe. */
function splitCells(line) {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
  const cells = [];
  let cell = '';
  let inCode = 0;
  for (let i = 0; i < row.length; i += 1) {
    const ch = row[i];
    if (ch === '\\' && row[i + 1] === '|') {
      cell += '|';
      i += 1;
    } else if (ch === '`') {
      let run = 1;
      while (row[i + run] === '`') run += 1;
      if (!inCode) inCode = run;
      else if (inCode === run) inCode = 0;
      cell += '`'.repeat(run);
      i += run - 1;
    } else if (ch === '|' && !inCode) {
      cells.push(cell.trim());
      cell = '';
    } else {
      cell += ch;
    }
  }
  cells.push(cell.trim());
  return cells;
}

/** A pipe table starts at a row with a pipe followed by its `---|---` separator. */
function isTableStart(lines, at) {
  const next = lines[at + 1];
  return lines[at].includes('|') && next !== undefined && next.includes('-') && next.includes('|') && TABLE_SEPARATOR.test(next);
}

function buildTable(header, separator, rows, documentRef) {
  const align = splitCells(separator).map((spec) => {
    const left = spec.startsWith(':');
    const right = spec.endsWith(':');
    if (left && right) return 'center';
    if (right) return 'right';
    return left ? 'left' : '';
  });
  const wrap = el(documentRef, 'div', 'prose-table-wrap');
  const table = el(documentRef, 'table', 'prose-table');
  const head = el(documentRef, 'thead');
  const body = el(documentRef, 'tbody');
  const width = splitCells(header).length;
  const addRow = (parent, cells, tag) => {
    const tr = el(documentRef, 'tr');
    for (let i = 0; i < width; i += 1) {
      const cell = el(documentRef, tag);
      if (align[i]) cell.style.textAlign = align[i];
      appendInline(cell, cells[i] ?? '', documentRef);
      tr.append(cell);
    }
    parent.append(tr);
  };
  addRow(head, splitCells(header), 'th');
  for (const row of rows) addRow(body, splitCells(row), 'td');
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function leading(line) {
  return line.length - line.trimStart().length;
}

/** A list from `lines[at]` on: its items, each with its own blocks, nested lists among
 * them. Returns `{node, next}`. */
function buildList(lines, at, documentRef, options) {
  const first = ITEM.exec(lines[at]);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const list = el(documentRef, ordered ? 'ol' : 'ul', 'prose-list');
  if (ordered) {
    const start = parseInt(first[2], 10);
    if (start !== 1) list.setAttribute('start', String(start));
  }
  let i = at;
  while (i < lines.length) {
    const item = ITEM.exec(lines[i]);
    if (!item || item[1].length !== indent || /\d/.test(item[2]) !== ordered) break;
    const contentIndent = indent + item[2].length + 1;
    const body = [item[3] ?? ''];
    i += 1;
    // The item's own lines: indented past its marker, a deeper list, or a lazy
    // continuation of its text. A blank line ends it unless indented content follows.
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        const next = lines[i + 1];
        if (next !== undefined && next.trim() && leading(next) >= contentIndent) {
          body.push('');
          i += 1;
          continue;
        }
        break;
      }
      const nested = ITEM.exec(line);
      if (leading(line) >= contentIndent || (nested && nested[1].length > indent)) {
        body.push(line.slice(Math.min(leading(line), contentIndent)));
      } else if (!nested && !startsBlock(lines, i)) {
        body.push(line.trim());
      } else {
        break;
      }
      i += 1;
    }
    // A blank line between two items of the same list does not end it.
    if (i < lines.length && !lines[i].trim()) {
      let j = i;
      while (j < lines.length && !lines[j].trim()) j += 1;
      const after = j < lines.length ? ITEM.exec(lines[j]) : null;
      if (after && after[1].length === indent && /\d/.test(after[2]) === ordered) {
        list.append(buildItem(body, documentRef, options));
        i = j;
        continue;
      }
    }
    list.append(buildItem(body, documentRef, options));
  }
  return { node: list, next: i };
}

function buildItem(body, documentRef, options) {
  const li = el(documentRef, 'li');
  const task = /^\[([ xX])\]\s+/.exec(body[0]);
  if (task) {
    li.className = 'prose-task';
    const box = el(documentRef, 'span', 'prose-task-box');
    box.append(documentRef.createTextNode(task[1] === ' ' ? '☐' : '☑'));
    li.append(box);
    body[0] = body[0].slice(task[0].length);
  }
  renderBlocks(li, body, documentRef, options);
  return li;
}

/** Whether `lines[i]` opens a block that ends a paragraph. */
function startsBlock(lines, i) {
  const line = lines[i];
  return FENCE_OPEN.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || ITEM.test(line) || isTableStart(lines, i);
}

function closesFence(line, marker) {
  const trimmed = line.trim();
  return leading(line) <= 3 && trimmed.length >= marker.length && /^(`+|~+)$/.test(trimmed) && trimmed[0] === marker[0];
}

/** Renders `lines` into `container`, block by block. Returns how many blocks it appended. */
function renderBlocks(container, lines, documentRef, options = {}) {
  let count = 0;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    let match;
    if ((match = FENCE_OPEN.exec(line))) {
      const marker = match[1];
      const body = [];
      i += 1;
      while (i < lines.length && !closesFence(lines[i], marker)) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // the closing fence, if there is one yet
      container.append(buildCodeBlock(match[2].trim(), body.join('\n'), documentRef, options));
    } else if ((match = HEADING.exec(line))) {
      const level = match[1].length;
      const heading = el(documentRef, 'div', `prose-heading prose-h${level}`);
      heading.setAttribute('role', 'heading');
      heading.setAttribute('aria-level', String(level));
      appendInline(heading, match[2] || '', documentRef);
      container.append(heading);
      i += 1;
    } else if (RULE.test(line)) {
      container.append(el(documentRef, 'hr', 'prose-rule'));
      i += 1;
    } else if (QUOTE.test(line)) {
      const quoted = [];
      while (i < lines.length && lines[i].trim()) {
        const inner = QUOTE.exec(lines[i]);
        if (inner) quoted.push(inner[1]);
        else if (!startsBlock(lines, i)) quoted.push(lines[i]);
        else break;
        i += 1;
      }
      const quote = el(documentRef, 'div', 'prose-quote');
      renderBlocks(quote, quoted, documentRef, options);
      container.append(quote);
    } else if (ITEM.test(line)) {
      const { node, next } = buildList(lines, i, documentRef, options);
      container.append(node);
      i = next;
    } else if (isTableStart(lines, i)) {
      const header = line;
      const separator = lines[i + 1];
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        rows.push(lines[i]);
        i += 1;
      }
      container.append(buildTable(header, separator, rows, documentRef));
    } else {
      const paragraph = [line];
      i += 1;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) {
        paragraph.push(lines[i]);
        i += 1;
      }
      container.append(buildParagraph(paragraph, documentRef));
    }
    count += 1;
  }
  return count;
}

/**
 * Render `text` into `container`. Returns the number of blocks appended, so the caller
 * knows whether anything was rendered (an empty text renders nothing). `options.runnable`:
 * shell blocks get «Ejecutar» (▶).
 */
function renderProse(container, text, documentRef = document, options = {}) {
  if (!text) return 0;
  return renderBlocks(container, String(text).replace(/\r\n?/g, '\n').split('\n'), documentRef, options);
}

export { renderProse, appendInline, runnableCommand, runPreview, MAX_COMMAND };
