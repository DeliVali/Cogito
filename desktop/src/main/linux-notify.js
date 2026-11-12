'use strict';

/**
 * Native Linux notifications with buttons, through `notify-send` (libnotify's own tool).
 *
 * Electron's Linux notifications cannot carry buttons: its libnotify path adds one hard-coded
 * `default` action and never reads `options.actions` (Electron source,
 * `shell/browser/notifications/linux/libnotify_notification.cc`; checked with `dbus-monitor`
 * on KDE Plasma 6.7.5, Electron 44.4.3, 2026-09-28: the server offered `actions`, the call
 * carried only `["default", "View"]`). `notify-send` 0.8+ passes `-A name=Text` through,
 * waits, and prints the chosen action's name.
 *
 * Used only when the server has the `actions` capability and `notify-send` knows
 * `--action`. Otherwise the app's own toast shows the buttons (`notify-policy.js`). Both
 * checks are read-only.
 */

const { execFile, spawn } = require('node:child_process');

const TIMEOUT_MS = 2000;

function defaultExec(file, args, options) {
  return new Promise((resolve, reject) => {
    execFile(file, args, options, (error, stdout) => (error ? reject(error) : resolve({ stdout: String(stdout) })));
  });
}

/** True when both the server and the tool can show buttons. */
async function linuxActionsAvailable({ exec = defaultExec } = {}) {
  try {
    const caps = await exec(
      'gdbus',
      [
        'call', '--session', '--dest', 'org.freedesktop.Notifications',
        '--object-path', '/org/freedesktop/Notifications',
        '--method', 'org.freedesktop.Notifications.GetCapabilities',
      ],
      { timeout: TIMEOUT_MS },
    );
    if (!/'actions'/.test(caps.stdout)) return false;
    const help = await exec('notify-send', ['--help'], { timeout: TIMEOUT_MS });
    return /--action/.test(help.stdout);
  } catch {
    return false;
  }
}

/**
 * Escape a body so it reads as text. Servers with the `body-markup` capability (GNOME, KDE)
 * read `<`, `>` and `&` as tags and entities (Desktop Notifications Specification,
 * "Markup"), so a command such as `cat <a.txt && rm b` would lose words or break the body.
 */
function escapeMarkup(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** `notify-send`'s arguments for one notification spec. */
function notifySendArgs(spec, { iconPath = '', openLabel = 'Mostrar ventana' } = {}) {
  const args = ['--app-name=COGITO', '--print-id'];
  if (iconPath) args.push(`--icon=${iconPath}`);
  // A permission stays until answered: GNOME keeps critical ones on screen, and an expiry
  // of 0 means "never" in the notification spec (KDE honours it).
  if (spec.sticky) args.push('--urgency=critical', '--expire-time=0');
  args.push(`--action=default=${openLabel}`);
  for (const action of spec.actions) args.push(`--action=${action.id}=${action.label}`);
  // The summary is plain text by the specification; only the body is read as markup.
  args.push('--', spec.title, escapeMarkup(spec.body));
  return args;
}

/** `{id, action}` from what `notify-send --print-id` wrote: the id, then the action name. */
function parseOutput(stdout) {
  const lines = String(stdout).split('\n').map((line) => line.trim()).filter(Boolean);
  const id = lines.length && /^\d+$/.test(lines[0]) ? Number(lines[0]) : null;
  const rest = id === null ? lines : lines.slice(1);
  return { id, action: rest.length ? rest[rest.length - 1] : null };
}

/**
 * Show one notification. `onAction(id)` gets the action's id, or `'open'` for a click on the
 * body; it is not called when the notification expired or was dismissed. `onError(error)`
 * fires when `notify-send` could not run at all, so the caller can show the toast instead.
 */
function showWithNotifySend(spec, { onAction, onError, iconPath, spawnFn = spawn, exec = defaultExec } = {}) {
  let out = '';
  let closed = false;
  const child = spawnFn('notify-send', notifySendArgs(spec, { iconPath }), { stdio: ['ignore', 'pipe', 'ignore'] });
  child.stdout.on('data', (chunk) => {
    out += chunk;
  });
  child.on('error', (error) => {
    closed = true;
    if (onError) onError(error);
  });
  child.on('close', () => {
    if (closed) return;
    closed = true;
    const { action } = parseOutput(out);
    if (action && onAction) onAction(action === 'default' ? 'open' : action);
  });
  return {
    /** Take it off the screen (answered elsewhere). Never reports an action. */
    close() {
      if (closed) return;
      closed = true;
      const { id } = parseOutput(out);
      if (id !== null) {
        exec(
          'gdbus',
          [
            'call', '--session', '--dest', 'org.freedesktop.Notifications',
            '--object-path', '/org/freedesktop/Notifications',
            '--method', 'org.freedesktop.Notifications.CloseNotification', String(id),
          ],
          { timeout: TIMEOUT_MS },
        ).catch(() => {});
      }
      child.kill();
    },
  };
}

module.exports = { linuxActionsAvailable, notifySendArgs, parseOutput, showWithNotifySend, escapeMarkup };
