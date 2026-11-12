'use strict';

/**
 * Detects whether anything will show our tray icon, and decides what closing the window
 * does as a result.
 *
 * On Linux the icon is a StatusNotifierItem. A desktop may have no host for one (stock
 * GNOME), or a host that rejects ours (Electron issue #53213: 43.4.1+/44.0.0 register a
 * name GNOME's watcher rejects). Closing the window hides it only when the icon is there to
 * bring it back; otherwise it minimises, so the app stays reachable. Windows and macOS
 * always have the notification area and the menu bar.
 *
 * Detection is read-only D-Bus through `gdbus` (glib's tool): does
 * `org.kde.StatusNotifierWatcher` have an owner, and is one of this process's items in its
 * `RegisteredStatusNotifierItems`, either by the `StatusNotifierItem-<pid>-` name Electron
 * registers or by the pid behind a unique name. Any failure, timeout or missing tool counts
 * as "no tray": minimising when a tray exists costs a click, hiding when none does loses
 * the window.
 */

const { execFile } = require('node:child_process');

const WATCHER = 'org.kde.StatusNotifierWatcher';
const TIMEOUT_MS = 2000;

function gdbus(exec, args) {
  return exec('gdbus', ['call', '--session', ...args], { timeout: TIMEOUT_MS });
}

function defaultExec(file, args, options) {
  return new Promise((resolve, reject) => {
    execFile(file, args, options, (error, stdout) => (error ? reject(error) : resolve({ stdout: String(stdout) })));
  });
}

function parseBoolean(stdout) {
  return /^\(true,?\)/.test(String(stdout).trim());
}

function parseItems(stdout) {
  return [...String(stdout).matchAll(/'([^']*)'/g)].map((match) => match[1]);
}

function parsePid(stdout) {
  const match = /\(uint32 (\d+),?\)/.exec(String(stdout).trim());
  return match ? Number(match[1]) : null;
}

async function ownsAnItem(exec, items, pid) {
  if (items.some((item) => item.includes(`StatusNotifierItem-${pid}-`))) return true;
  for (const item of items.slice(0, 20)) {
    const name = item.split('/')[0];
    if (!name.startsWith(':')) continue;
    try {
      const { stdout } = await gdbus(exec, [
        '--dest', 'org.freedesktop.DBus', '--object-path', '/org/freedesktop/DBus',
        '--method', 'org.freedesktop.DBus.GetConnectionUnixProcessID', name,
      ]);
      if (parsePid(stdout) === pid) return true;
    } catch {
      // The name went away or the bus refused: treat it as not ours.
    }
  }
  return false;
}

/**
 * `{reachable, reason}` for this platform. On Linux, waits `waitMs` for the tray created
 * just before to register, then asks.
 */
async function detectTrayHost({
  platform = process.platform,
  pid = process.pid,
  exec = defaultExec,
  waitMs = 1500,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
} = {}) {
  if (platform !== 'linux') return { reachable: true, reason: 'system tray' };
  try {
    const owner = await gdbus(exec, [
      '--dest', 'org.freedesktop.DBus', '--object-path', '/org/freedesktop/DBus',
      '--method', 'org.freedesktop.DBus.NameHasOwner', WATCHER,
    ]);
    if (!parseBoolean(owner.stdout)) return { reachable: false, reason: 'no StatusNotifierWatcher' };
    await sleep(waitMs);
    const registered = await gdbus(exec, [
      '--dest', WATCHER, '--object-path', '/StatusNotifierWatcher',
      '--method', 'org.freedesktop.DBus.Properties.Get', WATCHER, 'RegisteredStatusNotifierItems',
    ]);
    const items = parseItems(registered.stdout);
    if (await ownsAnItem(exec, items, pid)) return { reachable: true, reason: 'registered' };
    return { reachable: false, reason: 'not registered' };
  } catch (error) {
    return { reachable: false, reason: `detection failed: ${error.code || error.message}` };
  }
}

/** What closing the window does. */
function closeAction(platform, reachable) {
  return platform === 'linux' && !reachable ? 'minimize' : 'hide';
}

/** Holds the answer for this process; until it is known, Linux minimises. */
class TrayHost {
  constructor({ platform = process.platform, detect = detectTrayHost } = {}) {
    this.platform = platform;
    this.reachable = platform !== 'linux';
    this.reason = platform !== 'linux' ? 'system tray' : 'not checked yet';
    this._detect = detect;
  }

  async detect() {
    const { reachable, reason } = await this._detect({ platform: this.platform });
    this.reachable = reachable;
    this.reason = reason;
    return { reachable, reason };
  }

  /** Closes as the design means it (never quits) without losing the window. */
  closeWindow(window) {
    if (closeAction(this.platform, this.reachable) === 'minimize') window.minimize();
    else window.hide();
  }
}

module.exports = { WATCHER, detectTrayHost, closeAction, parseItems, TrayHost };
