'use strict';

import { ORB_STATE } from './strings.js';

/**
 * The orb, with its optional state pill, is the whole status line: no spinner, no
 * progress bar.
 *
 * It is the design's `<liquid-orb>` custom element (canvas 2D, `liquid-orb.js`, loaded as
 * a classic script before this module, so it is registered by the time `paint()` runs).
 * The element handles its seven states (`idle/thinking/responding/working/approval/done/error`),
 * the palette and speed interpolation between them, the glass rim and highlight, and
 * pausing on `document.hidden` or when scrolled out of view. `paint()` only inserts the
 * element and `Orb` only sets its `state` attribute.
 *
 * `done` is transient, as the design draws it: a sparkle that plays once (`liquid-orb.js`'s
 * `cur.ok` overlay, timed off `doneT`), then idle. `Orb` schedules that fallback because the
 * element does not know when its sparkle ends relative to other state changes. `error`
 * holds until the next phase moves it, as in the design.
 *
 * The phase -> state table below is this renderer's own copy, separate from the tray's
 * five-state `desktop/src/shared/phase-map.js` (which `main/status-hub.js` reads with
 * `require()`), so this stays a plain same-directory module under the page's
 * `script-src 'self'` CSP. Keep the two tables in sync; `cogito-lab`'s test covers the
 * shared one.
 */

/** Phase (core/cogito/loop/state.py) -> liquid-orb `state` attribute. */
const PHASE_TO_ORB_STATE = {
  sampling: 'thinking',
  parsing: 'thinking',
  // Drawn as thinking, named apart («Resumiendo»): a fold is not the model answering.
  compacting: 'compacting',
  executing: 'working',
  awaiting_approval: 'approval',
  done: 'done',
  interrupted: 'idle',
  failed: 'error',
};

/** States the orb names but `<liquid-orb>` does not draw on its own: the one it draws. */
const ORB_VISUAL = { compacting: 'thinking' };

/** How long the `done` sparkle plays, if nothing interrupts it, before idle. Mirrors
 * `DONE_SETTLE_MS` in `desktop/src/shared/phase-map.js` (the main process's copy, for the
 * tray icon's done -> idle timer); a local copy for the same CSP reason as the table above.
 * Keep both at 1800. */
const DONE_SETTLE_MS = 1800;

/** Build a `<liquid-orb>` filling `element`. `size` is the CSS pixel square it draws at. */
function paint(element, size = 18) {
  element.classList.add('orb');
  element.innerHTML = '';
  const node = document.createElement('liquid-orb');
  node.setAttribute('state', 'idle');
  node.setAttribute('size', String(size));
  element.append(node);
  return node;
}

class Orb {
  /** The state is never drawn as text (the person's choice of 2026-10-24): the orb's own
   * motion and colour show it. Its words («En reposo», «Pensando»…) are the element's
   * accessible name only. */
  constructor(element, size) {
    this.element = element;
    this.node = paint(element, size);
    element.setAttribute('role', 'img');
    this.state = 'idle';
    /** A label other than the state's own word, while this state holds. */
    this._labelText = null;
    this._settleTimer = null;
    this._showLabel('idle');
  }

  _showLabel(name, text = null) {
    this.element.setAttribute('aria-label', text || ORB_STATE[name] || name);
  }

  /** `label`: words other than the state's own, kept while the state holds. A later
   * `set` of the same state without a label keeps it. */
  set(name, label = null) {
    if (!name) return;
    if (name === this.state) {
      if (label && label !== this._labelText) {
        this._labelText = label;
        this._showLabel(name, label);
      }
      return;
    }
    this.state = name;
    this._labelText = label;
    this.node.setAttribute('state', ORB_VISUAL[name] || name);
    this._showLabel(name, label);
    if (this._settleTimer) {
      clearTimeout(this._settleTimer);
      this._settleTimer = null;
    }
    if (name === 'done') {
      this._settleTimer = setTimeout(() => {
        this._settleTimer = null;
        // Only fall back if nothing else claimed the orb while the sparkle played.
        if (this.state === 'done') this.set('idle');
      }, DONE_SETTLE_MS);
    }
  }

  /** Phases come from loop/state.py. An unknown one leaves the orb alone. */
  followPhase(phase) {
    const name = PHASE_TO_ORB_STATE[phase];
    if (name) this.set(name);
  }
}

export { Orb, paint };
