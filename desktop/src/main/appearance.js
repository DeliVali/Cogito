'use strict';

/**
 * The window's theme: «Claro», «Oscuro» or «Sistema» (Diseño, in «Herramientas y
 * permisos»).
 *
 * The choice is Electron's `nativeTheme.themeSource` ('light', 'dark' or 'system'). Setting it
 * makes every page's `prefers-color-scheme` match that theme (Electron 44 `native-theme.md`),
 * so the stylesheets switch through one media query and the page needs no script for it.
 * 'system' removes the override and follows the desktop.
 *
 * It is kept in a small JSON file under `userData`, written the way `window-state.js` writes
 * its own, and applied before the first window exists, so a dark start never shows the light
 * background first. A window's `backgroundColor`, which paints it until the page draws,
 * follows the theme in use (`shouldUseDarkColors`), and changes with it.
 */

const fs = require('node:fs');
const path = require('node:path');

const FILE_NAME = 'appearance.json';
const THEMES = ['light', 'dark', 'system'];
const DEFAULT_THEME = 'system';

/** The page's own ground (`--window` in styles.css), light and dark, for `backgroundColor`. */
const BACKGROUND = { light: '#d6dce6', dark: '#2b313c' };

/** One of `THEMES`, or the default for anything else. */
function normalizeTheme(value) {
  return THEMES.includes(value) ? value : DEFAULT_THEME;
}

/** The colour a window paints before its page draws, for the theme in use now. */
function backgroundFor(nativeTheme) {
  return nativeTheme && nativeTheme.shouldUseDarkColors ? BACKGROUND.dark : BACKGROUND.light;
}

/**
 * The JSON file, `{theme}`. A missing, unreadable or corrupt file reads as the default, never
 * as an error. A write goes to a temporary file renamed over the old one, as in
 * window-state.js, so a crash mid-write cannot leave half a file.
 */
function createStore(file, { fsImpl = fs, log = () => {} } = {}) {
  return {
    file,
    read() {
      try {
        const value = JSON.parse(fsImpl.readFileSync(file, 'utf8'));
        return { theme: normalizeTheme(value && value.theme) };
      } catch {
        return { theme: DEFAULT_THEME };
      }
    },
    write(state) {
      const temporary = `${file}.${process.pid}.tmp`;
      try {
        fsImpl.mkdirSync(path.dirname(file), { recursive: true });
        fsImpl.writeFileSync(temporary, `${JSON.stringify({ theme: normalizeTheme(state && state.theme) })}\n`, 'utf8');
        fsImpl.renameSync(temporary, file);
        return true;
      } catch (error) {
        log(`appearance not saved: ${error.message}`);
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

/**
 * Applies the saved theme and answers the page. Call once, before the first window:
 *
 * - `appearance:get` → `{theme, dark}`, `dark` being whether the dark colours are in use.
 * - `appearance:set` (theme) → the same, after saving and applying it; `{error}` for a
 *   value that is not one of `THEMES`.
 *
 * Every open window's `backgroundColor` follows each change, the desktop's own included
 * while the theme is 'system'. `windows()` lists them (BrowserWindow.getAllWindows).
 */
function setup({ nativeTheme, ipcMain, store, windows = () => [] }) {
  nativeTheme.themeSource = store.read().theme;
  const state = () => ({ theme: normalizeTheme(nativeTheme.themeSource), dark: Boolean(nativeTheme.shouldUseDarkColors) });
  const repaint = () => {
    const color = backgroundFor(nativeTheme);
    for (const window of windows()) {
      if (window && !window.isDestroyed() && typeof window.setBackgroundColor === 'function') window.setBackgroundColor(color);
    }
  };
  nativeTheme.on('updated', repaint);
  ipcMain.handle('appearance:get', () => state());
  ipcMain.handle('appearance:set', (_event, theme) => {
    if (!THEMES.includes(theme)) return { error: 'unknown theme' };
    store.write({ theme });
    nativeTheme.themeSource = theme;
    // Electron emits `updated` for 'light' and 'dark'; 'system' may change nothing it
    // reports, so the windows are repainted here as well.
    repaint();
    return state();
  });
  return { state, repaint };
}

module.exports = {
  FILE_NAME,
  THEMES,
  DEFAULT_THEME,
  BACKGROUND,
  normalizeTheme,
  backgroundFor,
  createStore,
  setup,
};
