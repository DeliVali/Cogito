'use strict';

/**
 * Window and application lifetime.
 *
 * Closing the window does not quit. The core can take up to five minutes to start on the
 * first, model-loading run, too long to throw away on a window close. The window is hidden,
 * and the tray (src/main/tray.js) or reactivating the app brings it back. Only the tray
 * menu quits. Where no tray will show (a Linux desktop with no StatusNotifierItem host,
 * such as stock GNOME), closing minimises instead, so the window is never unreachable
 * (src/main/tray-host.js).
 *
 * Because the agent keeps running after the window is gone:
 *
 * - A second launch must not start a second core (another `cogito serve`, another model in
 *   memory, another tray icon). `requestSingleInstanceLock()` below makes the second
 *   process focus the first one's window and exit.
 * - The default Quit accelerator (Cmd+Q on macOS) must not bypass "quit only from the
 *   tray". `Menu.setApplicationMenu(null)` removes the default app menu it is wired to.
 */

const path = require('node:path');
const { app, BrowserWindow, ipcMain, Menu, nativeTheme, screen } = require('electron');

const appearance = require('./appearance');
const { Core, bundledCoreReports, resolveCliLaunch } = require('./core');
const { GraphKeeper } = require('./graph-keeper');
const ipc = require('./ipc');
const { AppTray } = require('./tray');
const { TrayHost } = require('./tray-host');
const { exitIfSelfTest, startUpdater } = require('./updater');
const windowState = require('./window-state');

// The viewer reads a PDF through its own scheme (explorer-ipc.js); Electron accepts a
// privileged scheme only before the app is ready.
require('./explorer-ipc').registerScheme();

// A freshly installed macOS bundle proving it starts (src/main/updater/mac-swap.sh). It
// answers and exits here, before the single-instance lock could turn it away. The marker
// may only be written where the swap helper puts it, in the updater's folder of the app
// data; any other path is refused. A packaged bundle passes only if its own core starts
// and is this version.
exitIfSelfTest(process.argv, app.getVersion(), path.join(app.getPath('userData'), 'updater'), {
  coreReports: app.isPackaged ? (version) => bundledCoreReports(version) : null,
});

// Must run before anything else touches app state: a second instance quits here, before
// any of the top-level code below runs.
if (!app.requestSingleInstanceLock()) {
  app.quit();
  process.exit(0);
}

// Service and shared workers of a browser tab take the app's fallback user agent, not their
// partition's: it loses the app's name and Electron's before any partition is made
// (browser/host.js).
require('./browser/host').cleanAppUserAgent(app);

const core = new Core();
// The context store (Neo4j): started with the app if it is installed and down, since
// nothing else restarts it after a reboot (graph-keeper.js).
const graph = new GraphKeeper({
  launch: () => resolveCliLaunch(['graph', 'up', '--json'], { isPackaged: app.isPackaged }),
  log: (message) => process.stderr.write(`[graph] ${message}\n`),
});
// Whether a tray will really show on this desktop. Until that is known, closing minimises.
const trayHost = new TrayHost();
let window = null;
let tray = null;
// The browser tabs beside «Hilo» (browser/ipc.js), once the window exists.
let browser = null;
// Saves the window's size and position as they change; flushed on quit (window-state.js).
let placement = null;

// On Wayland, the second launch's activation token is what lets this raise the first
// window. Electron's single-instance handler passes it on
// (`ExtractXdgActivationTokenFromCmdLine` in `shell/browser/api/electron_api_app.cc`,
// v44.4.3), and `focus()` presents it.
app.on('second-instance', () => {
  if (!window) return;
  if (window.isMinimized()) window.restore();
  if (!window.isVisible()) window.show();
  window.focus();
});

function createWindow() {
  // The design's 1120×700 on first launch; after that, wherever it was left.
  const store = windowState.createStore(path.join(app.getPath('userData'), windowState.FILE_NAME), {
    log: (message) => process.stderr.write(`[window] ${message}\n`),
  });
  const bounds = windowState.restoreBounds(store.read(), screen.getAllDisplays(), screen.getPrimaryDisplay());
  window = new BrowserWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    // Measured (UX-10, 2026-09-29): the widest content (header with branch/rate/update
    // chips, every tab of the tools window, a service-error card, the composer) opened at
    // this size with zero clipping, per `audit-runner.electron.cjs`'s `checkClip()`
    // (`lab-wt-anthropic/experiments/2026-09-29-ux-audit/`). The only overflow is the
    // `overflow-y: auto` scrollbar of `.tools-panel` and `.transcript`.
    minWidth: 560,
    minHeight: 440,
    // The mockups draw their own title bar, with the striped texture and square window
    // buttons. The system frame would add a second one above it.
    // `titleBarOverlay` (Window Controls Overlay, on Linux since #41769, 2024-06) is not
    // used: it styles only `color`, `symbolColor` and `height` (base-window-options.md,
    // 2026-09); its glyphs follow the system theme since #52531 (2026-07-30, 44.x), so it
    // cannot draw the boxed buttons; their side follows text direction and user settings,
    // not the app (custom-title-bar.md); and on 44.4.x with a hidden window it was one way
    // `ready-to-show` never fired (electron/electron#54025, 2026-09-17).
    frame: false,
    // Shown at once, painted in the page's background until the page draws, instead of
    // created hidden and shown on `ready-to-show`. Hidden, the app stayed invisible on KDE
    // Plasma 6.7.5 Wayland (2026-09-29, Electron 44.4.3, `--disable-gpu`): `isVisible()`
    // was still false 15 s after launch. Cause: a hidden window's navigation invalidated
    // its surface id, so the renderer deferred every frame until the window was visible,
    // and `ready-to-show` waits for a frame. Fixed in Electron 44.4.4 (2026-09-22,
    // electron/electron#54118, backport of #54090). Signal Desktop hit the same deadlock on
    // Electron 24 and fixed it the same way (signalapp/Signal-Desktop#6368, 2023-04).
    // Electron's `browser-window.md` gives this as the alternative to `ready-to-show`: show
    // immediately with a `backgroundColor` close to the page's. Not waiting on any event
    // keeps this independent of the installed patch release. The colour is the theme's
    // (appearance.js), so a dark start does not flash the light ground.
    //
    // Focus on KDE Wayland comes from the launch: Chromium's `WaylandToplevelWindow::Show()`
    // calls `Activate()`, which presents the launcher's `XDG_ACTIVATION_TOKEN`
    // (`base::nix::TakeXdgActivationToken()`: read once, then unset). KWin focuses only a
    // window that presents a valid token (Kai Uwe Broulik, KDE, "On Window Activation",
    // 2025-08-04). Showing in the constructor presents it on the first show.
    backgroundColor: appearance.backgroundFor(nativeTheme),
    show: true,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // The interface sounds (renderer/sfx.js) play before any click, such as the «window»
      // sound on first appearance, and an AudioContext runs without a user gesture only
      // under this policy. It is already the default in Electron 44.4.3
      // (`web-preferences.md`, `web_contents_preferences.cc`); set explicitly so the sounds
      // do not depend on a default that could change.
      autoplayPolicy: 'no-user-gesture-required',
    },
  });

  // Without this, renderer failures are silent: a blocked script or a module that throws
  // just looks slow. Its output goes where the core's output goes.
  window.webContents.on('console-message', (event) => {
    const where = event.lineNumber ? ` (${event.sourceId}:${event.lineNumber})` : '';
    process.stderr.write(`[renderer] ${event.message}${where}\n`);
  });
  window.webContents.on('render-process-gone', (_event, details) => {
    process.stderr.write(`[renderer] gone: ${details.reason}\n`);
  });

  window.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  // Maximised again if it was left maximised. Un-maximising returns to the bounds above.
  if (bounds.maximized) window.maximize();
  placement = windowState.track(window, store);
  // So a launch from a terminal (no activation token on Wayland, no startup notification
  // on X11) still comes to the front where the system allows it. On KDE Wayland it does
  // not change a window the token already activated.
  window.focus();

  window.on('close', (event) => {
    if (app.quitting) return;
    event.preventDefault();
    trayHost.closeWindow(window);
  });

  tray = new AppTray(window);
  trayHost.detect().then(({ reachable, reason }) => {
    process.stderr.write(`[tray] ${reachable ? 'shown' : 'not shown'} (${reason}); closing ${reachable ? 'hides' : 'minimises'}\n`);
  });

  // The updater gets turns and phases from ipc.js, and shows «Actualización lista» and
  // «Reiniciar para actualizar» through the notifier and tray ipc.js sets up, the same path
  // as every other notice. Activity reported before the updater exists is replayed to it.
  let updater = null;
  const earlyActivity = [];
  const onAgentActivity = (event) => (updater ? updater.noteActivity(event) : earlyActivity.push(event));

  // The browser's tabs live in this window; the core asks for them over its event stream.
  browser = require('./browser/ipc').register({ window, core });
  // No default project, in development or packaged. Defaulting to the app's own directory
  // would open the app's tree, or `resources` once packaged (provenance audit). `ipc.js`'s
  // `core:open` picks the most recently opened project (design 1f), or tells the window to
  // show the picker.
  const { notifier, activeProjectRoot } = ipc.register({
    core,
    window,
    tray,
    trayHost,
    onAgentActivity,
    isPackaged: app.isPackaged,
    graph,
    browser,
  });
  // The file explorer and viewer (design 1a, 1p): the open project's folder.
  require('./explorer-ipc').register({ window, getRoot: activeProjectRoot });
  // Terminal tabs beside «Hilo»: the core's shells, reached with the token held here.
  require('./terminal-ipc').register({ core });
  // `COGITO_NO_AUTO_GRAPH=1`: the app starts nothing. The lab's end-to-end runs set it.
  if (String(process.env.COGITO_NO_AUTO_GRAPH || '').trim() !== '1') graph.ensure();
  // The drawn maximise button tracks the window's state, whatever changed it (ipc.js).
  ipc.followMaximized(window);
  // The design's «window» sound (renderer/sfx.js) each time the window comes back (from the
  // tray or the taskbar). The window events decide this, not the page's visibility, which
  // also changes when another window just covers it (native occlusion on Windows and macOS).
  for (const event of ['show', 'restore']) {
    window.on(event, () => {
      if (!window.isDestroyed()) window.webContents.send('window:shown');
    });
  }

  updater = startUpdater({ app, window, tray, notifier });
  for (const event of earlyActivity.splice(0)) updater.noteActivity(event);
}

// No page opens a window of its own: a link xterm or Chromium would open with
// `window.open` would load a remote page in a window of the app. Links from a terminal go
// to the system browser through `terminal:open-link` instead. A browser tab's popup becomes
// another tab, and its hardening is installed here, since `did-create-window` does not fire
// for a popup adopted as a view (cogito-lab research/2026-11-05-native-browser/electron.md).
app.on('web-contents-created', (_event, contents) => {
  if (browser && browser.owns(contents)) browser.harden(contents);
  else contents.setWindowOpenHandler(() => ({ action: 'deny' }));
});

app.whenReady().then(() => {
  // No menu bar. The window draws its own title bar, and an app menu would only give the
  // default accelerators (Cmd+Q on macOS) a Quit item, which "quit only from the tray"
  // rules out.
  Menu.setApplicationMenu(null);
  // The saved theme («Diseño»), applied before the first window paints.
  appearance.setup({
    nativeTheme,
    ipcMain,
    store: appearance.createStore(path.join(app.getPath('userData'), appearance.FILE_NAME), {
      log: (message) => process.stderr.write(`[appearance] ${message}\n`),
    }),
    windows: () => BrowserWindow.getAllWindows(),
  });
  createWindow();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
  else window?.show();
});

// The core is a child process holding a model in memory. Nothing else will reap it. Quitting
// waits for it to exit (it closes the agent's shells and stops the model server on the way
// out), and `Core.stop` kills it if it takes longer than its grace period, so no model,
// server or GPU process the session started outlives the app.
let coreStopped = false;
app.on('before-quit', (event) => {
  app.quitting = true;
  // Write now, not after the save's debounce: the process may be gone by then.
  placement?.flush();
  tray?.destroy();
  browser?.closeAll();
  if (coreStopped) return;
  event.preventDefault();
  for (const win of BrowserWindow.getAllWindows()) win.hide();
  coreStopped = true;
  core.stop().finally(() => app.quit());
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
