'use strict';

/**
 * The system tray icon (design 2b). Left click hides a focused window, and raises one that
 * is hidden, minimised or behind other windows. The menu is the
 * OS's own on purpose: the design says the system draws it (on Linux in the panel's theme),
 * so only its content follows the design. Content and per-state icons are decided in
 * Electron-free modules that `cogito-lab` tests directly: `tray-menu.js` (the usage menu)
 * and `tray-icons.js` (a colour dot on Windows, a monochrome template on macOS, a symbolic
 * glyph on Linux, and the Windows taskbar's progress and overlay). `status-hub.js` feeds it.
 *
 * `assets/` ships via `extraResources` (package.json's `build` config), not inside the asar
 * `files` bundle. Some native tray/icon APIs read the path directly instead of through
 * Node's asar-aware `fs`, so an icon inside `app.asar` can fail to load on some platforms.
 * `ICONS_DIR` resolves against `process.resourcesPath` when packaged, and against the
 * source tree's `assets/` when unpacked.
 *
 * The menu header shows the orb in the state's colour (`tray-images.js`, PNG bytes turned
 * into a `nativeImage` here); the GPU bar is a row of text (`tray-menu.js`). Every other
 * menu action («Reintentar ahora», «Cambiar modelo») goes to `onAction(id)`, which
 * `status-hub.js` handles.
 *
 * Quitting only happens from here. Closing the window hides it (or minimises it where no
 * tray will show, see `tray-host.js`), so the agent keeps running; "Salir de COGITO" is the
 * only path that ends the process. "Mostrar ventana" shows its shortcut only when that
 * shortcut was actually registered.
 */

const path = require('node:path');
const { Tray, Menu, nativeImage, nativeTheme, globalShortcut, app } = require('electron');

const { buildTrayMenu, trayTooltip } = require('./tray-menu');
const { trayIconName, taskbarFor } = require('./tray-icons');
const { orbPng } = require('./tray-images');
const { setLocale } = require('../shared/format');

// Packaged: assets/ was copied next to the app by extraResources (package.json).
// Unpacked (`npm start`): assets/ is still in the source tree, two levels up from here.
const ICONS_DIR = app.isPackaged
  ? path.join(process.resourcesPath, 'assets', 'icons', 'tray')
  : path.join(__dirname, '..', '..', 'assets', 'icons', 'tray');

/** "Mostrar ventana" (design 2b: Ctrl+Shift+Space, ⌃⇧Space on macOS). */
const SHOW_SHORTCUT = 'Control+Shift+Space';

/** A window that lost focus this recently still counts as focused for a tray click: on
 * Windows the click itself takes focus from the window before 'click' fires. */
const FOCUS_GRACE_MS = 300;

class AppTray {
  constructor(
    window,
    { platform = process.platform, onRetry = () => {}, onAction = () => {}, now = Date.now } = {},
  ) {
    this.window = window;
    this.platform = platform;
    this._now = now;
    this._blurredAt = -Infinity;
    if (typeof window.on === 'function') window.on('blur', () => (this._blurredAt = this._now()));
    this.onRetry = onRetry;
    // Every other menu action, by id (tray-menu.js), handled by the status hub.
    this.onAction = onAction;
    this.model = {
      project: '',
      state: 'idle',
      detail: '',
      usage: null,
      update: null,
      shortcut: null,
    };
    this._menuKey = '';
    this._update = null;
    this._icons = new Map();

    // Currency in the tray menu, and in notifications through the same shared module,
    // follows this machine's locale (2026-09-29 UX audit, "Consistency"). Set once here,
    // after `app` is ready (this constructor only runs from `app.whenReady()`), so that
    // `shared/format.js` stays Electron-free.
    if (typeof app.getLocale === 'function') {
      this.locale = app.getLocale();
      setLocale(this.locale);
    }

    try {
      if (globalShortcut.register(SHOW_SHORTCUT, () => this.showWindow())) this.model.shortcut = SHOW_SHORTCUT;
    } catch {
      // Taken by another program or not allowed in this session: the menu shows no shortcut.
    }

    this.tray = new Tray(this._image(this.model.state));
    this.tray.on('click', () => this.toggleWindow());
    this._onTheme = () => this.tray.setImage(this._image(this.model.state));
    nativeTheme.on('updated', this._onTheme);
    this._render();
  }

  _image(state) {
    const name = trayIconName(this.platform, state, { darkPanel: nativeTheme.shouldUseDarkColors });
    return nativeImage.createFromPath(path.join(ICONS_DIR, name));
  }

  _set(changes) {
    Object.assign(this.model, changes);
    this._render();
  }

  /** One of the five states (`tray-icons.js`); anything else falls back to idle. */
  setState(state) {
    if (state === this.model.state) return;
    this.tray.setImage(this._image(state));
    this._taskbar(state);
    this._set({ state });
  }

  setDetail(detail) {
    if (detail !== this.model.detail) this._set({ detail: detail || '' });
  }

  setProject(project) {
    this._set({ project: project || '' });
  }

  /** The core's `GET /usage` snapshot. */
  setUsage(usage) {
    this._set({ usage });
  }

  /**
   * The updater's hook: `{label, detail?, actionable?, onClick}` puts "Reiniciar para
   * actualizar" (or whatever it passes) in its slot in the menu; `null` removes it.
   * `actionable: false` (the deferred state) draws a plain row instead of a button that
   * would do nothing: `tray-menu.js` makes it a `type: 'label'`, which `_render` draws
   * disabled (2026-09-29 UX final verification, item 3).
   */
  setUpdateItem(item) {
    this._update = item || null;
    this._set({ update: item ? { label: item.label, detail: item.detail || '', actionable: item.actionable !== false } : null });
  }

  _render() {
    const items = buildTrayMenu({ ...this.model, platform: this.platform });
    this.tray.setToolTip(trayTooltip(this.model));
    const key = JSON.stringify(items);
    // Rebuild only when something shown changed: on Linux every rebuild re-exports the
    // whole menu over D-Bus.
    if (key === this._menuKey) return;
    this._menuKey = key;
    const actions = {
      show: () => this.showWindow(),
      quit: () => {
        app.quitting = true;
        app.quit();
      },
      retry: () => this.onRetry(),
      update: () => this._update && this._update.onClick && this._update.onClick(),
    };
    // Windows and Linux read "&" as a mnemonic marker, so a literal one is doubled
    // ("npm test && npm run build" showed one "&", checked on KDE over dbusmenu).
    const literal = (text) => (this.platform === 'darwin' ? text : text.replace(/&/g, '&&'));
    const template = items.map((item) => {
      if (item.type === 'separator') return { type: 'separator' };
      const icon = item.icon ? this._icon(item.icon) : null;
      // A disabled item's image is greyed out (KDE, GNOME, macOS), losing the orb's colour.
      // So a row with an image stays enabled, and clicking it shows the window.
      if (item.type === 'label' && icon) return { label: literal(item.label), icon, click: actions.show };
      if (item.type === 'label') {
        // Windows and macOS draw a MenuItem's `toolTip`; Linux (dbusmenu) has none
        // (2026-09-29 UX audit).
        return { label: literal(item.label), enabled: false, ...(item.tooltip ? { toolTip: item.tooltip } : {}) };
      }
      return {
        label: literal(item.label),
        click: actions[item.id] || (() => this.onAction(item.id)),
        ...(item.accelerator ? { accelerator: item.accelerator, registerAccelerator: false } : {}),
      };
    });
    this.tray.setContextMenu(Menu.buildFromTemplate(template));
  }

  /** The header's orb in the state's colour (cached). Bars are rows of text
   * (`tray-menu.js`) because a system menu draws an icon at 16 px, too small for one.
   *
   * Drawn at 1x as well as 2x: Linux's menu (Chromium's dbusmenu) sends only an image's 1x
   * PNG, and an image with no 1x representation goes out as zero bytes, so Plasma drew no
   * orb (the user's report, 2026-09-29; `icon-data` on the session bus was `@ay []` for a
   * 2x-only image, 585 bytes with a 1x one). */
  _icon(spec) {
    const key = `orb:${spec.state}`;
    if (!this._icons.has(key)) {
      const image = nativeImage.createFromBuffer(orbPng(spec.state, 16), { scaleFactor: 1 });
      image.addRepresentation({ scaleFactor: 2, buffer: orbPng(spec.state, 32) });
      this._icons.set(key, image);
    }
    return this._icons.get(key);
  }

  /** Windows only: the taskbar button's progress and its 16 px overlay dot. */
  _taskbar(state) {
    if (this.platform !== 'win32' || this.window.isDestroyed()) return;
    const spec = taskbarFor(state);
    this.window.setProgressBar(spec.progress, { mode: spec.mode });
    this.window.setOverlayIcon(
      spec.overlay ? nativeImage.createFromPath(path.join(ICONS_DIR, spec.overlay.file)) : null,
      spec.overlay ? spec.overlay.description : '',
    );
  }

  showWindow() {
    if (this.window.isDestroyed()) return;
    if (this.window.isMinimized()) this.window.restore();
    this.window.show();
    this.window.focus();
  }

  toggleWindow() {
    if (this.window.isDestroyed()) return;
    const focused = this.window.isFocused() || this._now() - this._blurredAt < FOCUS_GRACE_MS;
    if (this.window.isVisible() && !this.window.isMinimized() && focused) {
      this.window.hide();
    } else {
      this.showWindow();
    }
  }

  destroy() {
    nativeTheme.removeListener('updated', this._onTheme);
    if (this.model.shortcut) globalShortcut.unregister(this.model.shortcut);
    this.tray.destroy();
  }
}

module.exports = { AppTray };
