'use strict';

/**
 * DOM for the history read back on open, from `history.js`'s blocks. It uses only the live
 * turn's renderers: `render-thread.js`'s bubble, answer block, day separator and fold
 * marker; `render-tool-rows.js`'s rows (grouped by `tool-rows.js`'s `groupRows`, so reads
 * collapse into «Leyó N archivos» as they do live) and «Cambios de este turno» card;
 * `markdown.js`'s prose; `render-rewind.js`'s «↺ Volver a este punto» link, which calls back
 * into the window's rewind flow.
 *
 * Returns the top-level nodes in order. The window decides where they go (history is
 * always inserted above what is already there).
 */

import { groupRows } from './tool-rows.js';
import { renderTurnToolRows, buildChangesCard } from './render-tool-rows.js';
import { renderProse } from './markdown.js';
import { buildRewindLink, buildResultLine } from './render-rewind.js';
import { rewindLinkLabel, buildMarkerModel } from './rewind.js';
import { buildSaid, buildAnswer, buildDaySeparator, buildFoldMarker, buildStopLine } from './render-thread.js';
import { buildPill } from './render-decisions.js';

/**
 * `blocks`: `historyBlocks(...).blocks`. `onRewind(pointId, text, sentAt, link)` is the
 * window's `startRewind`; `onRedo(redoId, wrapper)` is its «Rehacer» for a rewind read
 * back (a promise; a rejection shows the line's failure label); `paintAvatar(element)` draws
 * the answer's orb.
 */
function renderHistory(blocks, { onRewind, onRedo = () => Promise.reject(new Error('no redo')), paintAvatar, now = Date.now(), documentRef = document }) {
  const nodes = [];
  for (const block of blocks) {
    if (block.kind === 'day') {
      nodes.push(buildDaySeparator(block.key, block.label, documentRef));
    } else if (block.kind === 'fold') {
      nodes.push(buildFoldMarker(documentRef));
    } else if (block.kind === 'rewound') {
      const wrapper = buildResultLine(
        buildMarkerModel(block.item),
        () => onRedo(block.redoId, wrapper),
        documentRef,
        { quiet: true },
      );
      nodes.push(wrapper);
    } else if (block.kind === 'said') {
      nodes.push(buildSaid(block.text, documentRef, block.attachments));
      if (block.pointId && block.sentAt !== null) {
        const link = buildRewindLink(
          rewindLinkLabel(block.sentAt),
          () => onRewind(block.pointId, block.text, block.sentAt, link),
          documentRef,
        );
        nodes.push(link);
      }
    } else if (block.kind === 'answer') {
      const { block: element, body } = buildAnswer(paintAvatar, documentRef);
      for (const part of block.parts) {
        if (part.kind === 'rows') {
          const wrapper = documentRef.createElement('div');
          wrapper.className = 'tool-rows';
          body.append(wrapper);
          renderTurnToolRows(wrapper, groupRows(part.rows), documentRef, now);
        } else if (part.kind === 'pill') {
          body.append(buildPill(part.pill, documentRef));
        } else if (part.kind === 'prose') {
          const text = documentRef.createElement('div');
          text.className = 'answer-text';
          // A one-line shell block gets «Ejecutar», as a settled live reply does.
          renderProse(text, part.text, documentRef, { runnable: true });
          body.append(text);
        } else if (part.kind === 'stopped' || part.kind === 'failure') {
          body.append(buildStopLine(part, documentRef));
        } else if (part.kind === 'changes') {
          const card = buildChangesCard(part.event, documentRef);
          if (card) body.append(card);
        } else if (part.kind === 'fold') {
          body.append(buildFoldMarker(documentRef));
        } else if (part.kind === 'meta') {
          const line = documentRef.createElement('div');
          line.className = 'meta';
          line.textContent = part.text;
          body.append(line);
        }
      }
      nodes.push(element);
    }
  }
  return nodes;
}

export { renderHistory };
