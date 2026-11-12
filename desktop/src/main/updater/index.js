'use strict';

/**
 * Automatic updates, wired into Electron. The logic lives in the modules beside this one.
 * This file only passes them the real Electron pieces.
 *
 * Two calls from `main/index.js`:
 *
 * - `exitIfSelfTest(argv, version, updaterRoot, {coreReports})`, before anything else. The
 *   macOS swap helper runs a freshly installed bundle with `--cogito-update-selftest=<marker>`.
 *   That process writes its version to the marker and exits, proving the new bundle starts
 *   before the old one is deleted. With `coreReports`, the bundle's own Python core must
 *   also report this version, or no marker is written and the helper rolls back. It must
 *   run before the single-instance lock, which would otherwise make it quit without
 *   answering.
 * - `startUpdater({app, window, tray, notifier})`, once the window, the tray and the
 *   notifier `ipc.js` builds exist. «Reiniciar para actualizar» goes in the tray through
 *   `AppTray.setUpdateItem` and «Actualización lista» through `notifier.notify`, like every
 *   other tray item and notice. Returns `{noteActivity}`, which `ipc.js` calls with turns,
 *   phases and scheduled resumes so updates know when the agent is idle.
 *
 * `overrides` is for the lab's Electron end-to-end test (a local release server, a fake
 * bundle, a throwaway key). A shipped app never passes it.
 */

const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');

const { macBackend, packageManagerBackend } = require('./backends');
const { createUpdaterController } = require('./controller');
const { ELECTRON_UPDATER_PARTITION, installHostFilter, createElectronUpdaterBackend } = require('./electron-updater');
const { createFeed } = require('./feed');
const { PRODUCTION_POLICY } = require('./fetch');
const { detectInstallType } = require('./install-type');
const { UPDATE_PUBLIC_KEYS } = require('./keys');
const { MacUpdater } = require('./mac');
const { updateReadyNotification } = require('./notify');
const { upgradeCommand } = require('./package-manager');
const { UpdateStore } = require('./store');

const SELF_TEST_FLAG = '--cogito-update-selftest=';

/**
 * Where a self-test marker may be written: `<userData>/updater/work/<version>/launched`, the
 * only path the swap helper passes (mac.js's `workRoot`, mac-swap.sh's `marker`). Any other
 * path (a crafted command line) is refused and never written.
 */
function selfTestMarkerAllowed(marker, updaterRoot) {
  if (!marker || !path.isAbsolute(marker) || path.basename(marker) !== 'launched') return false;
  const work = path.resolve(updaterRoot, 'work');
  const relative = path.relative(work, path.resolve(marker));
  const parts = relative.split(path.sep);
  if (relative.startsWith('..') || path.isAbsolute(relative) || parts.length !== 2) return false;
  try {
    // The version folder must be a real directory inside `work`, not a link out of it.
    const folder = path.dirname(path.resolve(marker));
    return fs.realpathSync(folder) === path.join(fs.realpathSync(work), parts[0]);
  } catch {
    return false;
  }
}

function exitIfSelfTest(argv, version, updaterRoot, { coreReports = null } = {}) {
  const arg = argv.find((value) => value.startsWith(SELF_TEST_FLAG));
  if (!arg) return;
  const marker = arg.slice(SELF_TEST_FLAG.length);
  if (!updaterRoot || !selfTestMarkerAllowed(marker, updaterRoot)) process.exit(1);
  // A bundle whose core does not start, or is another version, has not proved it works.
  if (coreReports && !coreReports(version)) process.exit(1);
  try {
    fs.writeFileSync(marker, version);
  } catch {
    process.exit(1);
  }
  process.exit(0);
}

function loadAutoUpdater(log) {
  try {
    return require('electron-updater').autoUpdater;
  } catch (error) {
    log(`electron-updater is not installed (${error.message}); run "npm install" in desktop/`);
    return null;
  }
}

function startUpdater({ app, window, tray, notifier = null, overrides = {} }) {
  const { ipcMain, clipboard, session } = require('electron');
  const log = overrides.log || ((message) => process.stderr.write(`[updater] ${message}\n`));
  const currentVersion = overrides.currentVersion || app.getVersion();
  const platform = overrides.platform || process.platform;
  const arch = overrides.arch || process.arch;
  const root = overrides.storeDir || path.join(app.getPath('userData'), 'updater');
  const store = new UpdateStore(root);
  const feed = overrides.feed || createFeed();
  const keysPinned = Boolean(overrides.feed) || UPDATE_PUBLIC_KEYS.length > 0;

  function makeBackend(info) {
    if (overrides.backend !== undefined) return overrides.backend;
    if (info.install === 'none') return null;
    if (!keysPinned) {
      log('no update key is pinned in keys.js; updates are off until one is');
      return null;
    }
    if (info.install === 'package-manager') return packageManagerBackend(feed, currentVersion, log);
    if (info.backend === 'mac') {
      const mac = new MacUpdater({
        feed,
        currentVersion,
        arch,
        bundle: info,
        workRoot: path.join(root, 'work'),
        helperSource: path.join(__dirname, 'mac-swap.sh'),
        ...overrides.mac,
      });
      return macBackend(mac, log);
    }
    const autoUpdater = loadAutoUpdater(log);
    if (!autoUpdater) return null;
    autoUpdater.logger = { info: log, warn: log, error: log, debug: () => {} };
    installHostFilter(session.fromPartition(ELECTRON_UPDATER_PARTITION, { cache: false }), PRODUCTION_POLICY);
    return createElectronUpdaterBackend({ autoUpdater, feed, currentVersion, platform, arch, log });
  }

  const send = (channel, payload) => {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  };
  const isWindowVisible = () => !window.isDestroyed() && window.isVisible() && !window.isMinimized();
  const showWindow = () => {
    if (window.isDestroyed()) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  };

  const activityBeforeReady = [];
  let controller = null;
  const ready = (async () => {
    const info =
      overrides.installInfo ||
      (await detectInstallType({
        platform,
        env: process.env,
        execPath: process.execPath,
        isPackaged: app.isPackaged,
        execFile: promisify(execFile),
      }));
    log(`install: ${info.install}${info.reason ? ` (${info.reason})` : ''}`);
    controller = createUpdaterController({
      currentVersion,
      installInfo: info,
      store,
      backend: makeBackend(info),
      command: upgradeCommand(info.packageManager),
      send,
      setTrayItem: (item) =>
        tray?.setUpdateItem(
          item
            ? { label: item.label, detail: item.sublabel, actionable: item.actionable, onClick: () => controller.restartNow() }
            : null,
        ),
      notify: ({ version, onRestart, onShowWindow }) => {
        if (!notifier) return;
        const spec = updateReadyNotification({ version, platform, onRestart, onShowWindow });
        Promise.resolve(notifier.notify(spec)).catch((error) => log(`notify: ${error.message}`));
      },
      isWindowVisible,
      showWindow,
      setQuitting: (value) => {
        app.quitting = value;
      },
      quit: overrides.quit || (() => app.quit()),
      copyText: (text) => clipboard.writeText(text),
      log,
      ...(overrides.schedule ? { schedule: overrides.schedule } : {}),
    });
    controller.start();
    for (const event of activityBeforeReady.splice(0)) controller.noteActivity(event);
    return controller;
  })();
  ready.catch((error) => log(`updater did not start: ${error.message}`));

  const on = (channel, handler) => ipcMain.handle(channel, async (_event, argument) => handler(await ready, argument));
  on('update:get', (c) => c.snapshot());
  on('update:restart', (c, when) => (when === 'when-done' ? c.restartWhenDone() : c.restartNow()));
  on('update:defer-to-quit', (c) => c.deferToQuit());
  on('update:retry', (c) => c.retry());
  on('update:card-seen', (c) => c.cardSeen());
  on('update:copy-command', (c) => c.copyCommand());
  on('update:take-outcome', (c) => c.takeOutcome());

  for (const event of ['show', 'hide', 'minimize', 'restore']) {
    window.on(event, () => controller?.windowVisibilityChanged());
  }
  app.on('before-quit', () => controller?.beforeQuit());

  return {
    ready,
    noteActivity(event) {
      if (controller) controller.noteActivity(event);
      else activityBeforeReady.push(event);
    },
  };
}

module.exports = { exitIfSelfTest, selfTestMarkerAllowed, startUpdater, SELF_TEST_FLAG };
