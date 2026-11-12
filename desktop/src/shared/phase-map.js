'use strict';

/**
 * Maps a `Phase` (core/cogito/loop/state.py) to one of the tray icon's five states
 * (`tray-icons.js`). Read by `status-hub.js`. Dependency-free so a `cogito-lab` test can
 * require it without Electron.
 *
 * The renderer's `orb.js` keeps its own copy of the phase -> orb-state table. The renderer
 * is same-directory scripts under a `script-src 'self'` CSP; a module one directory up
 * raises an origin question that a few lines of object literal are not worth. Keep both
 * copies in sync.
 */

/** Phase -> tray icon file's state segment (desktop/assets/icons/tray/). */
const PHASE_TO_TRAY_STATE = Object.freeze({
  sampling: 'working',
  parsing: 'working',
  compacting: 'working',
  executing: 'working',
  awaiting_approval: 'permission',
  done: 'done',
  interrupted: 'idle',
  failed: 'error',
});

/** Every state the tray icon can be in. */
const TRAY_STATES = Object.freeze(['idle', 'working', 'permission', 'done', 'error']);

/**
 * How long the "done" icon and orb show before falling back to idle. Read by
 * `status-hub.js`. The renderer's `orb.js` keeps its own copy for the CSP reason above.
 * Keep both in sync.
 */
const DONE_SETTLE_MS = 1800;

module.exports = { PHASE_TO_TRAY_STATE, TRAY_STATES, DONE_SETTLE_MS };
