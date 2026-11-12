'use strict';

/**
 * Unified-diff parsing and rendering for the "Cambios de este turno" card
 * (`turn_changes.files[].diff`, contract `app-phase-b-contract.md`). The core builds it
 * with `difflib.unified_diff(fromfile=path, tofile=path)` (`core/cogito/loop/changes.py`),
 * which always writes `--- path`/`+++ path` header lines before the first `@@` hunk.
 * Those are not diff content and must never be parsed as removed or added lines.
 *
 * `parseUnifiedDiff` is pure (a string in, a line-model array out), so plain Node can
 * test it. `renderDiffLines` is the only place a diff's text reaches the page. Every
 * character goes through `textContent`/`createTextNode`, never `innerHTML`: a diff is
 * arbitrary file content and may contain `<script>` or other markup (XSS). `documentRef`
 * lets the unit suite pass a fake `document` and check this without a real DOM.
 */

import { escapeControlChars } from './sanitize.js';

/** Parses a diff into one entry per hunk line. `lineNo` is the old-file number for a
 * removed line and the new-file number for a context or added line. The design's diff
 * view has a single number column (section 1l: plain ascending numbers, no separate
 * old/new column). */
function parseUnifiedDiff(diffText) {
  const lines = [];
  if (!diffText) return lines;

  const rawLines = diffText.split('\n');
  // A diff ending in "\n" (every real one does) splits into a trailing empty element.
  // It is not a blank line in the file, so drop it instead of drawing an empty context
  // line.
  if (rawLines.length && rawLines[rawLines.length - 1] === '') rawLines.pop();

  let oldLine = 0;
  let newLine = 0;
  // Everything before the first "@@" is the `--- a/x`/`+++ b/x` header that
  // `difflib.unified_diff` writes first. It is skipped.
  let seenHunk = false;

  for (const raw of rawLines) {
    if (raw.startsWith('@@')) {
      seenHunk = true;
      const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
      lines.push({ type: 'hunk', text: raw });
      continue;
    }
    if (!seenHunk) continue;

    if (raw.startsWith('+')) {
      lines.push({ type: 'add', text: raw.slice(1), lineNo: newLine });
      newLine += 1;
    } else if (raw.startsWith('-')) {
      lines.push({ type: 'remove', text: raw.slice(1), lineNo: oldLine });
      oldLine += 1;
    } else if (raw.startsWith(' ') || raw === '') {
      lines.push({ type: 'context', text: raw.slice(1), lineNo: newLine });
      oldLine += 1;
      newLine += 1;
    }
    // Any other line (e.g. "\ No newline at end of file") is dropped: the design does
    // not show it and it has no line number.
  }
  return lines;
}

/** How many non-hunk lines a diff shows before collapsing behind "Clic para expandir".
 * Matches `markdown.js`'s COLLAPSE_AT so both collapsible blocks behave the same. */
const COLLAPSE_AT = 14;

function visibleLineCount(lines) {
  return lines.filter((line) => line.type !== 'hunk').length;
}

/**
 * Render `lines` (from `parseUnifiedDiff`) into `container`, showing only the first
 * `revealCount` non-hunk lines. To expand, the caller re-renders with a larger
 * `revealCount` (see chat.js's per-file expand handling). This function keeps no
 * truncation state.
 */
function renderDiffLines(container, lines, revealCount, documentRef = document) {
  let shown = 0;
  for (const line of lines) {
    if (line.type === 'hunk') continue;
    if (shown >= revealCount) break;
    shown += 1;

    const row = documentRef.createElement('div');
    row.className = `diff-line diff-line--${line.type}`;

    const gutter = documentRef.createElement('span');
    gutter.className = 'diff-line-no';
    gutter.append(documentRef.createTextNode(String(line.lineNo)));

    const code = documentRef.createElement('span');
    code.className = 'diff-line-text';
    // The canvas' marker column (1a/1l): «+», «−» (U+2212), or a space for context so
    // a line keeps its indentation next to its neighbours.
    // Its own column, so a line that starts at the margin and an indented one line up.
    const marker = line.type === 'add' ? '+' : line.type === 'remove' ? '−' : ' ';
    const sign = documentRef.createElement('span');
    sign.className = 'diff-line-sign';
    sign.append(documentRef.createTextNode(marker));
    code.append(sign, documentRef.createTextNode(escapeControlChars(line.text)));

    row.append(gutter, code);
    container.append(row);
  }
  return shown;
}

export { parseUnifiedDiff, renderDiffLines, visibleLineCount, COLLAPSE_AT };
