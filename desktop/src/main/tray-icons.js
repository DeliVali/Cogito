'use strict';

/**
 * Which image the tray and taskbar show for each state (design 2b), per OS. Pure: `tray.js`
 * loads the files and calls Electron.
 *
 *   Windows  colour sphere with a status dot (`tray-{state}.png`), plus the taskbar button:
 *            progress while working, amber for a permission, red for an error, each with a
 *            16 px overlay dot (Electron `setProgressBar` modes `indeterminate`, `paused`,
 *            `error`; `setOverlayIcon`).
 *   macOS    a monochrome template (`tray-{state}Template.png`, @2x beside it) that the
 *            system inverts in dark mode. Electron treats a file ending in `Template` as one.
 *   Linux    a symbolic monochrome glyph. The item is sent as pixels, so the panel cannot
 *            recolour it: the light glyph for a dark panel, the dark one otherwise.
 */

const ICON_STATES = Object.freeze(['idle', 'working', 'permission', 'error', 'done']);

function trayIconName(platform, state, { darkPanel = true } = {}) {
  const known = ICON_STATES.includes(state) ? state : 'idle';
  if (platform === 'darwin') return `tray-${known}Template.png`;
  if (platform === 'linux') return `tray-${known}-symbolic-on-${darkPanel ? 'dark' : 'light'}.png`;
  return `tray-${known}.png`;
}

/** The Windows taskbar button for a state; `progress` is the value `setProgressBar` takes. */
function taskbarFor(state) {
  switch (state) {
    case 'working':
      return { progress: 2, mode: 'indeterminate', overlay: null };
    case 'permission':
      return { progress: 1, mode: 'paused', overlay: { file: 'overlay-permission.png', description: 'Permiso' } };
    case 'error':
      return { progress: 1, mode: 'error', overlay: { file: 'overlay-error.png', description: 'Error' } };
    default:
      return { progress: -1, mode: 'none', overlay: null };
  }
}

module.exports = { ICON_STATES, trayIconName, taskbarFor };
