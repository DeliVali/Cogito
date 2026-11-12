'use strict';

/**
 * Saves the window's size and position between launches.
 *
 * The first launch opens at the design's size, 1120×700, centred on the primary display
 * and shrunk to fit a smaller one. Later launches open where the window was left, maximised
 * if it was, unless that place is no longer on any display (monitor unplugged, resolution
 * lowered); then it opens as on the first launch.
 *
 * Everything except the store and `track()` is a pure function of plain rectangles, so the
 * placement rules can be unit-tested without a real multi-monitor setup.
 *
 * On Wayland a client can neither read nor set its global position: Electron 44.4.3's
 * `base-window.md` says `getBounds()` returns `{x: 0, y: 0, ...}` there and `getPosition()`
 * `[0, 0]` (electron/electron#40886, 2023-12, still open). The saved x/y are then 0 and the
 * compositor places the window; only size and maximised state carry over, which is all
 * Wayland lets any application keep.
 */

const fs = require('node:fs');
const path = require('node:path');

/** The design's main window, «1a — Ventana principal 1120×700» (cogito-lab's
 * `specs/design/2026-09-28-cogito-v2-mvp/README.md`). */
const DEFAULT_WIDTH = 1120;
const DEFAULT_HEIGHT = 700;
/** index.js's `minWidth`/`minHeight`; the comment there says where they come from. */
const MIN_WIDTH = 560;
const MIN_HEIGHT = 440;

/**
 * How many pixels of a saved window, in each direction, must still be on a display for its
 * position to be kept. Same figure as VS Code's `validateWindowState`
 * (src/vs/platform/windows/electron-main/windows.ts, main as of 2026-09-28), which keeps a
 * window with 128 px still visible. The window is then moved fully inside that display,
 * because its only title bar is the one the page draws.
 */
const MIN_VISIBLE_PX = 128;

/** Resizing or dragging emits dozens of events a second; write once after it settles. */
const SAVE_DEBOUNCE_MS = 400;

const FILE_NAME = 'window-state.json';

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * A display's usable rectangle. Like VS Code's `getWorkingArea` (same file as above), falls
 * back to `bounds` when the work area has zero size, which some Linux X11 sessions report.
 */
function usableArea(display) {
  const area = display && display.workArea;
  if (area && area.width > 0 && area.height > 0) return area;
  const bounds = display && display.bounds;
  if (bounds && bounds.width > 0 && bounds.height > 0) return bounds;
  return null;
}

/** `value` inside [low, high]; when the range is empty, `low` wins. */
function clamp(value, low, high) {
  return Math.max(low, Math.min(value, high));
}

/** A size that fits `area`, never below the window's minimum. The minimum wins when the
 * area is smaller, since the window's `minWidth` would enforce it anyway. */
function fitSize(width, height, area) {
  return {
    width: Math.max(MIN_WIDTH, Math.min(Math.round(width), area.width)),
    height: Math.max(MIN_HEIGHT, Math.min(Math.round(height), area.height)),
  };
}

/** The first launch: 1120×700 centred on `workArea`, shrunk to fit it. */
function defaultBounds(workArea) {
  const { width, height } = fitSize(DEFAULT_WIDTH, DEFAULT_HEIGHT, workArea);
  return {
    x: workArea.x + Math.max(0, Math.floor((workArea.width - width) / 2)),
    y: workArea.y + Math.max(0, Math.floor((workArea.height - height) / 2)),
    width,
    height,
    maximized: false,
  };
}

/** The overlap of two rectangles, `{width, height}` (zero when they do not meet). */
function overlap(a, b) {
  return {
    width: Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)),
    height: Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)),
  };
}

/**
 * Where to open: `saved` (`{x, y, width, height, maximized}`) when enough of it is still on
 * one of `displays`, resized to fit that display's work area and moved fully inside it;
 * otherwise `defaultBounds` on `primary` (the first display when not given).
 */
function restoreBounds(saved, displays, primary = displays && displays[0]) {
  const areas = (Array.isArray(displays) ? displays : []).map(usableArea).filter(Boolean);
  const primaryArea = usableArea(primary) || areas[0] || { x: 0, y: 0, width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT };
  const fallback = defaultBounds(primaryArea);
  if (
    !saved ||
    typeof saved !== 'object' ||
    !['x', 'y', 'width', 'height'].every((key) => isFiniteNumber(saved[key])) ||
    saved.width <= 0 ||
    saved.height <= 0
  ) {
    return fallback;
  }

  // The window returns to the display that holds most of it.
  let best = null;
  let bestArea = 0;
  for (const area of areas) {
    const shared = overlap(saved, area);
    if (shared.width < Math.min(MIN_VISIBLE_PX, saved.width)) continue;
    if (shared.height < Math.min(MIN_VISIBLE_PX, saved.height)) continue;
    if (shared.width * shared.height > bestArea) {
      best = area;
      bestArea = shared.width * shared.height;
    }
  }
  if (!best) return { ...fallback, maximized: saved.maximized === true };

  const { width, height } = fitSize(saved.width, saved.height, best);
  return {
    x: Math.round(clamp(saved.x, best.x, best.x + best.width - width)),
    y: Math.round(clamp(saved.y, best.y, best.y + best.height - height)),
    width,
    height,
    maximized: saved.maximized === true,
  };
}

/**
 * The JSON file holding the state. An unreadable or corrupt file reads as nothing saved
 * (first-launch placement), never as an error. A write goes to a temporary file that is
 * then renamed over the old one, so a crash mid-write cannot leave half a file.
 */
function createStore(file, { fsImpl = fs, log = () => {} } = {}) {
  return {
    file,
    read() {
      try {
        const value = JSON.parse(fsImpl.readFileSync(file, 'utf8'));
        return value && typeof value === 'object' ? value : null;
      } catch {
        return null;
      }
    },
    write(state) {
      const temporary = `${file}.${process.pid}.tmp`;
      try {
        fsImpl.mkdirSync(path.dirname(file), { recursive: true });
        fsImpl.writeFileSync(temporary, `${JSON.stringify(state)}\n`, 'utf8');
        fsImpl.renameSync(temporary, file);
        return true;
      } catch (error) {
        log(`window state not saved: ${error.message}`);
        try {
          fsImpl.rmSync(temporary, { force: true });
        } catch {
          // Nothing more to do; the old file, if any, is untouched.
        }
        return false;
      }
    },
  };
}

/** What is saved: the normal (unmaximised) bounds, so a maximised window restores to the
 * size it had before, plus whether it was maximised. */
function snapshot(window) {
  const { x, y, width, height } = window.getNormalBounds();
  return { x, y, width, height, maximized: window.isMaximized() };
}

/**
 * Saves `window`'s state to `store` shortly after it last moved or resized, and at once
 * when it closes. Returns `{flush}` for the app's quit, which must write before the process
 * exits instead of waiting out the debounce.
 */
function track(
  window,
  store,
  { debounceMs = SAVE_DEBOUNCE_MS, setTimer = setTimeout, clearTimer = clearTimeout } = {},
) {
  let timer = null;
  const flush = () => {
    if (timer) clearTimer(timer);
    timer = null;
    if (window.isDestroyed()) return false;
    return store.write(snapshot(window));
  };
  const later = () => {
    if (timer) clearTimer(timer);
    timer = setTimer(flush, debounceMs);
  };
  for (const event of ['resize', 'move', 'maximize', 'unmaximize']) window.on(event, later);
  window.on('close', flush);
  return { flush };
}

module.exports = {
  DEFAULT_WIDTH,
  DEFAULT_HEIGHT,
  MIN_WIDTH,
  MIN_HEIGHT,
  MIN_VISIBLE_PX,
  SAVE_DEBOUNCE_MS,
  FILE_NAME,
  defaultBounds,
  restoreBounds,
  createStore,
  snapshot,
  track,
};
