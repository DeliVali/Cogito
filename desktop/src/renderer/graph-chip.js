'use strict';

/**
 * The header chip for the context store (Neo4j). Hidden while it is up, and while nothing
 * is known; shown when it is starting (a retry, or a slow start) and when it is down, where
 * pressing it asks the main process to start it again (`graph-keeper.js`). The state is the
 * main process's: `{state: 'unknown' | 'starting' | 'ready' | 'down'}`.
 *
 * The model is pure, so it can be tested without a DOM.
 */

import { GRAPH } from './strings.js';

/** What the chip shows for `state`: `{hidden, text, title, busy}`. */
function graphChipModel(state) {
  const name = state && state.state;
  // The chip's text ellipsizes at narrow widths, so its tooltip carries the whole state
  // (QA 2026-10-13), and for the pressable one the action as well.
  if (name === 'down') return { hidden: false, text: GRAPH.down, title: `${GRAPH.down} · ${GRAPH.retry}`, busy: false };
  if (name === 'starting') return { hidden: false, text: GRAPH.starting, title: GRAPH.starting, busy: true };
  return { hidden: true, text: '', title: '', busy: false };
}

/**
 * Wires `chip` (a button) to the main process: draws the current state and each change,
 * and a press asks for a retry. `api` is `window.cogito`; an older preload without the
 * graph calls leaves the chip hidden. `noteDown()` is for a request that found the store
 * down before the main process had said so (a rewind), so the chip never contradicts it.
 */
function mountGraphChip(chip, api) {
  // A page without the chip (an older shell) still runs; nothing is drawn.
  if (!chip) return { draw() {}, noteDown() {}, ensure: () => Promise.resolve(null) };
  const draw = (state) => {
    const model = graphChipModel(state);
    chip.hidden = model.hidden;
    chip.textContent = model.text;
    chip.title = model.title;
    chip.disabled = model.busy;
    if (typeof chip.setAttribute === 'function') chip.setAttribute('aria-busy', String(model.busy));
  };
  const asked = (call) => {
    if (typeof call !== 'function') return Promise.resolve(null);
    return Promise.resolve()
      .then(call)
      .catch(() => null);
  };

  chip.addEventListener('click', () => {
    asked(api.graphEnsure).then((state) => {
      if (state) draw(state);
    });
  });
  if (typeof api.onGraphState === 'function') api.onGraphState(draw);
  asked(api.graphState).then((state) => {
    if (state) draw(state);
  });

  return {
    draw,
    noteDown: () => draw({ state: 'down' }),
    ensure: () => asked(api.graphEnsure).then((state) => (state ? (draw(state), state) : null)),
  };
}

export { graphChipModel, mountGraphChip };
