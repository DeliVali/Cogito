'use strict';

/**
 * The app's own notification: a small always-on-top window in a screen corner, in the app's
 * OS 9 style, with the title, text and buttons a system notification would have (design
 * 2c). Used where a native notification or its buttons are not available (see
 * `notify-policy.js`'s `chooseBackend`).
 *
 * Shown without taking focus. A permission stays until answered or closed; the others hide
 * after `TOAST_MS`. The spec is passed in the page URL's query, so the page never loads
 * without knowing what to show. The page answers on `toast:action`, which is accepted only
 * from a toast's own window.
 */

const path = require('node:path');
const { BrowserWindow, ipcMain, nativeTheme, screen } = require('electron');

const { backgroundFor } = require('./appearance');

const TOAST_WIDTH = 360;
const TOAST_HEIGHT = 132;
const MARGIN = 16;
const GAP = 10;
const TOAST_MS = 8000;

class ToastWindows {
  constructor({ onAction, platform = process.platform }) {
    this.onAction = onAction;
    this.platform = platform;
    this.toasts = new Map();
    this.seq = 0;
    this._listener = (event, message) => {
      const id = message && Number(message.id);
      const toast = this.toasts.get(id);
      if (!toast || event.sender !== toast.window.webContents) return;
      this._close(id);
      if (message.action !== 'dismiss') this.onAction(toast.spec, String(message.action));
    };
    ipcMain.on('toast:action', this._listener);
  }

  show(spec) {
    const id = ++this.seq;
    const window = new BrowserWindow({
      width: TOAST_WIDTH,
      height: TOAST_HEIGHT,
      frame: false,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      alwaysOnTop: true,
      show: false,
      // The theme's ground (appearance.js), so a dark desktop gets no light flash.
      backgroundColor: backgroundFor(nativeTheme),
      title: 'COGITO',
      webPreferences: {
        preload: path.join(__dirname, '..', 'preload', 'toast.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        // The toast plays a sound (design 2c) with no click in this window, which
        // Chromium's default autoplay policy would silence.
        autoplayPolicy: 'no-user-gesture-required',
      },
    });
    const shown = {
      id,
      title: spec.title,
      body: spec.body,
      actions: spec.actions.map((action) => ({ id: action.id, label: action.label })),
      sticky: Boolean(spec.sticky),
      // When it was created, so «hace …» counts from then, not from when the page loaded.
      shownAt: Date.now(),
      // The dot's colour (toast-view.js).
      ...(typeof spec.kind === 'string' ? { kind: spec.kind } : {}),
    };
    window.loadFile(path.join(__dirname, '..', 'renderer', 'toast.html'), {
      query: { spec: JSON.stringify(shown) },
    });
    window.on('closed', () => {
      if (this.toasts.has(id)) {
        clearTimeout(this.toasts.get(id).timer);
        this.toasts.delete(id);
        this._layout();
      }
    });
    const timer = spec.sticky ? null : setTimeout(() => this._close(id), TOAST_MS);
    this.toasts.set(id, { window, spec, timer });
    this._layout();
    // Shown at once, in its place and without focus, painted in `backgroundColor` until the
    // page draws. Shown on `ready-to-show` it never appeared (QA 2026-10-11): a hidden window
    // on Electron 44.4.3 under KDE Wayland draws no frame, so the event never fires. The
    // main window avoids the same deadlock the same way (`index.js`).
    window.showInactive();
    return { close: () => this._close(id) };
  }

  _close(id) {
    const toast = this.toasts.get(id);
    if (!toast) return;
    clearTimeout(toast.timer);
    this.toasts.delete(id);
    if (!toast.window.isDestroyed()) toast.window.destroy();
    this._layout();
  }

  /** Stacks toasts, newest nearest the corner: top right on macOS (under the menu bar),
   * bottom right elsewhere. A Wayland compositor may ignore the position. */
  _layout() {
    const area = screen.getPrimaryDisplay().workArea;
    const fromTop = this.platform === 'darwin';
    const toasts = [...this.toasts.values()].reverse();
    toasts.forEach((toast, index) => {
      if (toast.window.isDestroyed()) return;
      const offset = MARGIN + index * (TOAST_HEIGHT + GAP);
      const x = area.x + area.width - TOAST_WIDTH - MARGIN;
      const y = fromTop ? area.y + offset : area.y + area.height - TOAST_HEIGHT - offset;
      toast.window.setPosition(Math.round(x), Math.round(y));
    });
  }

  destroy() {
    for (const id of [...this.toasts.keys()]) this._close(id);
    ipcMain.removeListener('toast:action', this._listener);
  }
}

module.exports = { ToastWindows, TOAST_MS };
