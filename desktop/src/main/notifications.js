'use strict';

/**
 * System notifications (design 2c), shown only while the window is not in front.
 * `notify-policy.js` decides what notifies and with which words and buttons; this module
 * shows them, one of three ways (`chooseBackend`): Electron's `Notification` (Windows,
 * macOS, and Linux without buttons), `notify-send` on Linux when the server can show
 * buttons (`linux-notify.js`), or the app's own toast window (`toast-window.js`) where
 * native notifications or their buttons are not available. Once Electron reports `failed`
 * (macOS: an unsigned app cannot use UNNotification), every later one goes to the toast.
 *
 * One notification per key: a newer one replaces the older, and `withdraw(key)` takes one
 * down when it was answered in the window. A question that waits, asked while the window
 * was in front, is held and shown once the window loses focus, hides or minimizes. A spec with `ownActions` shows those in the
 * app's toast instead of its `actions`. The updater's "Actualización
 * lista" goes through the same `notify({key, kind, title, body, actions, sticky, reveal})`.
 */

const path = require('node:path');
const { app, Notification } = require('electron');

const { chooseBackend, primaryFirst } = require('./notify-policy');
const { escapeMarkup, linuxActionsAvailable, showWithNotifySend } = require('./linux-notify');
const { ToastWindows } = require('./toast-window');
const { setLocale } = require('../shared/format');

const ICON = app.isPackaged
  ? path.join(process.resourcesPath, 'assets', 'icons', 'icon-256.png')
  : path.join(__dirname, '..', '..', 'assets', 'icons', 'icon-256.png');

/** What the app's own toast shows for `spec`: its `ownActions` when it has them. */
function ownToast(spec) {
  return Array.isArray(spec.ownActions) ? { ...spec, actions: spec.ownActions } : spec;
}

class Notifier {
  constructor({ window, onAction, platform = process.platform, toasts = null, log = () => {} }) {
    this.window = window;
    this.onAction = onAction;
    this.platform = platform;
    this.log = log;
    this.toasts = toasts || new ToastWindows({ onAction: (spec, action) => this._act(spec, action) });
    this.shown = new Map();
    this.pending = new Map();
    // Questions that wait (`sticky`) asked while the window was in front, by key. They are
    // offered as soon as the window is left (QA 2026-10-11): a card nobody can see would
    // otherwise wait unseen.
    this.held = new Map();
    if (window && typeof window.on === 'function') {
      for (const leave of ['blur', 'hide', 'minimize']) window.on(leave, () => this.offerHeld());
    }
    this.ticket = 0;
    this.electronFailed = false;
    this.linuxActions = platform === 'linux' ? linuxActionsAvailable() : Promise.resolve(false);
    // As in `tray.js`: money in a notification body follows this machine's locale, not a
    // hard-coded one (2026-09-29 UX audit, "Consistency"). Guarded because some of the
    // lab's minimal fake `electron` modules lack it.
    if (typeof app.getLocale === 'function') setLocale(app.getLocale());
  }

  _inFront() {
    return this.window.isDestroyed() || this.window.isFocused();
  }

  async notify(spec) {
    if (this._inFront()) {
      if (spec.sticky) this.held.set(spec.key, spec);
      return;
    }
    this.held.delete(spec.key);
    const ticket = ++this.ticket;
    this.withdraw(spec.key);
    this.pending.set(spec.key, ticket);
    const linuxActions = await this.linuxActions;
    // Answered, replaced or withdrawn while the capability check was still running.
    if (this.pending.get(spec.key) !== ticket || this._inFront()) return;
    this.pending.delete(spec.key);
    const backend = chooseBackend({
      platform: this.platform,
      spec,
      electronSupported: Notification.isSupported(),
      electronFailed: this.electronFailed,
      linuxActions,
    });
    const handle =
      backend === 'electron'
        ? this._electron(spec)
        : backend === 'notify-send'
          ? this._notifySend(spec)
          : this.toasts.show(ownToast(spec));
    this.shown.set(spec.key, handle);
  }

  /** Shows every held question now that the window is no longer in front. */
  offerHeld() {
    if (this._inFront()) return;
    for (const spec of [...this.held.values()]) {
      Promise.resolve(this.notify(spec)).catch((error) => this.log(`notify: ${error.message}`));
    }
  }

  withdraw(key) {
    this.held.delete(key);
    this.pending.delete(key);
    const handle = this.shown.get(key);
    if (!handle) return;
    this.shown.delete(key);
    handle.close();
  }

  _act(spec, action) {
    this.shown.delete(spec.key);
    this.onAction(spec, action);
  }

  _fallBack(spec) {
    const toast = this.toasts.show(ownToast(spec));
    if (this.shown.has(spec.key)) this.shown.set(spec.key, toast);
    return toast;
  }

  _electron(spec) {
    let fallback = null;
    // macOS shows only the first action and tucks the rest under its own «Opciones», so
    // that one must be the primary, not the safe-first order other surfaces use (2026-09-29
    // UX audit follow-up). Reordered by action id (`primaryFirst`), never by position.
    // `on('action')` below indexes into this same array, so the index the OS reports
    // always maps to the right id.
    const shown = this.platform === 'darwin' ? primaryFirst(spec.actions) : spec.actions;
    const notification = new Notification({
      title: spec.title,
      // On Linux Electron hands the body to libnotify, and the server reads it as markup,
      // as it does notify-send's (linux-notify.js).
      body: this.platform === 'linux' ? escapeMarkup(spec.body) : spec.body,
      icon: ICON,
      actions: shown.map((action) => ({ type: 'button', text: action.label })),
      timeoutType: spec.sticky ? 'never' : 'default',
      urgency: spec.sticky ? 'critical' : 'normal',
    });
    notification.on('click', () => this._act(spec, 'open'));
    notification.on('action', (_event, index) => {
      const action = shown[index];
      if (action) this._act(spec, action.id);
    });
    notification.on('failed', (_event, error) => {
      this.electronFailed = true;
      this.log(`native notification failed (${error}); using the app's own toast`);
      fallback = this._fallBack(spec);
    });
    notification.show();
    return {
      close: () => {
        notification.close();
        if (fallback) fallback.close();
      },
    };
  }

  _notifySend(spec) {
    let fallback = null;
    const handle = showWithNotifySend(spec, {
      iconPath: ICON,
      onAction: (action) => this._act(spec, action),
      onError: (error) => {
        this.log(`notify-send could not run (${error.code || error.message}); using the app's own toast`);
        fallback = this._fallBack(spec);
      },
    });
    return {
      close: () => {
        handle.close();
        if (fallback) fallback.close();
      },
    };
  }

  destroy() {
    for (const key of [...this.shown.keys()]) this.withdraw(key);
    this.toasts.destroy();
  }
}

module.exports = { Notifier, ownToast };
