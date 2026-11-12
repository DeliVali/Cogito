'use strict';

/**
 * What a finished tool row opens onto (design 1a's expanded «Editó»/«Ejecutó», 1l's diff):
 * the call's `detail` from the core (`core/cogito/loop/detail.py`). That is an edit's diff,
 * drawn by `diff.js` the same way «Cambios de este turno» draws one, or a command's output,
 * line by line as the shell tool read it. The app writes nothing here: the lines are the
 * tool's, and the only words are 1l's «Clic para expandir · N líneas más» / «Mostrar menos»
 * and three fixed notes (`strings.js`'s `TOOL_DETAIL`).
 *
 * The core sends a bounded preview. Past `COLLAPSE_AT` lines the view collapses as 1l does.
 * When the preview is not the whole, «Clic para expandir» fetches the whole once through
 * `loadDetail(ref)` (the window's `window.cogito.toolDetail`, the core's artifacts route) and
 * draws it. The view is built only the first time the row is opened.
 *
 * Every line reaches the page through `textContent`/`createTextNode`, never `innerHTML`,
 * because a diff or an output is arbitrary content (same rule as `diff.js`).
 */

import { parseUnifiedDiff, renderDiffLines, visibleLineCount, COLLAPSE_AT } from './diff.js';
import { escapeControlChars } from './sanitize.js';
import { TOOL_DETAIL } from './strings.js';

/** The command's output, one `.tool-output-line` per line, the first `limit` of them. */
function renderOutputLines(container, lines, limit, documentRef) {
  const shown = Math.min(limit, lines.length);
  for (let index = 0; index < shown; index += 1) {
    const line = documentRef.createElement('span');
    line.className = 'tool-output-line';
    line.append(documentRef.createTextNode(escapeControlChars(lines[index]) || ' '));
    container.append(line);
  }
  return shown;
}

/**
 * `detail`: a row's normalized detail (`tool-rows.js`'s `normalizeDetail`). `label`: the
 * row's label, used as the region's accessible name. Returns `{element, ensure}`: `element`
 * goes in the row's expander, and `ensure()` fills it once, when the row first opens.
 */
function buildToolDetail(detail, { label = '', documentRef = document, loadDetail = null, play = () => {} } = {}) {
  const element = documentRef.createElement('div');
  element.className = `tool-detail tool-detail--${detail.kind}`;
  element.setAttribute('role', 'region');
  if (label) element.setAttribute('aria-label', label);

  let built = false;
  const ensure = () => {
    if (built) return;
    built = true;

    const body = documentRef.createElement('div');
    body.className = detail.kind === 'diff' ? 'diff-lines' : 'tool-output';
    const note = documentRef.createElement('div');
    note.className = 'tool-detail-note';
    note.hidden = true;
    const more = documentRef.createElement('button');
    more.type = 'button';
    more.className = 'tool-detail-more';
    const glyph = documentRef.createElement('span');
    glyph.className = 'tool-detail-more-glyph';
    glyph.setAttribute('aria-hidden', 'true');
    glyph.append(documentRef.createTextNode('▼'));
    const moreLabel = documentRef.createElement('span');
    more.append(glyph, moreLabel);
    element.append(body, more, note);

    // `text` is what we have: the preview, or the whole once fetched. `whole` is true when
    // it is all of it; `outOf` is the full line count when it is only a part.
    let text = detail.text;
    let whole = !detail.truncated;
    let outOf = null;
    let parsed = null;
    let showAll = false;
    let loading = false;
    let failed = false;

    const parse = () => {
      // An output ends on the newline of its last line: that is not another, blank line, and
      // drawn as one it leaves an empty strip under the text (QA 2026-10-13, chat D7).
      parsed = detail.kind === 'diff' ? parseUnifiedDiff(text) : text.replace(/\n+$/, '').split('\n');
    };
    const count = () => (detail.kind === 'diff' ? visibleLineCount(parsed) : parsed.length);
    // Total line count: counted here once the whole is loaded, else the core's count.
    const total = () => (whole ? count() : Math.max(detail.lines, count()));

    const redraw = () => {
      body.replaceChildren();
      const limit = showAll ? Infinity : COLLAPSE_AT;
      const shown =
        detail.kind === 'diff'
          ? renderDiffLines(body, parsed, Math.min(limit, count()), documentRef)
          : renderOutputLines(body, parsed, limit, documentRef);
      const all = total();
      if (all <= COLLAPSE_AT) {
        more.hidden = true;
      } else {
        more.hidden = false;
        const label = loading ? TOOL_DETAIL.loading : showAll ? TOOL_DETAIL.showLess : TOOL_DETAIL.expandMore(all - COLLAPSE_AT);
        moreLabel.replaceChildren(documentRef.createTextNode(label));
        more.setAttribute('aria-expanded', String(showAll));
        glyph.style.transform = showAll ? 'rotate(180deg)' : 'rotate(0deg)';
      }
      let message = '';
      if (failed) message = TOOL_DETAIL.loadFailed;
      else if (showAll && !whole) message = TOOL_DETAIL.partial(shown, outOf ?? detail.lines);
      note.hidden = !message;
      note.replaceChildren(documentRef.createTextNode(message));
    };

    const fetchWhole = () => {
      loading = true;
      failed = false;
      redraw();
      Promise.resolve()
        .then(() => loadDetail(detail.ref))
        .then((answer) => {
          if (!answer || typeof answer.text !== 'string' || answer.error) throw new Error('no artifact');
          text = answer.text;
          whole = answer.truncated !== true;
          // An output takes the artifact's line count. A diff keeps `detail.lines`, which
          // already counts the lines a diff view shows.
          if (!whole) outOf = detail.kind === 'output' && Number.isInteger(answer.lines) ? answer.lines : detail.lines;
          loading = false;
          showAll = true;
          parse();
          play('open');
          redraw();
        })
        .catch(() => {
          loading = false;
          failed = true;
          redraw();
        });
    };

    more.addEventListener('click', (event) => {
      event?.stopPropagation?.();
      if (loading) return;
      if (showAll) {
        showAll = false;
        play('close');
        redraw();
        return;
      }
      if (!whole && outOf === null && detail.ref && typeof loadDetail === 'function') {
        fetchWhole();
        return;
      }
      showAll = true;
      failed = false;
      play('open');
      redraw();
    });

    parse();
    redraw();
  };

  return { element, ensure };
}

export { buildToolDetail, renderOutputLines };
