'use strict';

/**
 * The app's own shortcuts, which still work while a browser tab has the focus.
 *
 * The window listens for these on its document (renderer/chat.js: Ctrl+O, ⌘O on macOS,
 * the projects screen; renderer/explorer.js: Ctrl+Shift+E, ⌘⇧E, the file explorer). A page
 * with the focus would get them instead, so main stops them in the tab's
 * `before-input-event` and sends the window `browser:shortcut`, which it handles as a key
 * typed on its own document. Escape and every other key stay the page's.
 */

/** `key` lower-cased; `mod` is Ctrl, or ⌘ on macOS. */
const APP_SHORTCUTS = Object.freeze([
  Object.freeze({ key: 'o', mod: true, shift: false, alt: false }),
  Object.freeze({ key: 'e', mod: true, shift: true, alt: false }),
]);

/**
 * `input` (Electron's before-input-event) as `{key, code, ctrl, meta, shift, alt}` when it is
 * one of the app's shortcuts on `platform`, else null.
 */
function shortcutOf(input, platform = process.platform) {
  if (!input || input.type !== 'keyDown' || typeof input.key !== 'string') return null;
  const mac = platform === 'darwin';
  const mod = mac ? input.meta : input.control;
  const other = mac ? input.control : input.meta;
  const key = input.key.toLowerCase();
  const hit = APP_SHORTCUTS.some(
    (s) => s.key === key && Boolean(mod) === s.mod && !other && Boolean(input.shift) === s.shift && Boolean(input.alt) === s.alt,
  );
  if (!hit) return null;
  return {
    key: input.key,
    code: typeof input.code === 'string' ? input.code : '',
    ctrl: Boolean(input.control),
    meta: Boolean(input.meta),
    shift: Boolean(input.shift),
    alt: Boolean(input.alt),
  };
}

module.exports = { APP_SHORTCUTS, shortcutOf };
