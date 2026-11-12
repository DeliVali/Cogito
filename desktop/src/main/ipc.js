'use strict';

/**
 * Everything the renderer may reach.
 *
 * Each channel starts work or reports it. The token, the port and the core process stay in
 * the main process, so a bug in page script cannot drive the agent on its own.
 */

const os = require('node:os');
const path = require('node:path');
const { app, dialog, ipcMain } = require('electron');

const { currentBranch } = require('./git');
const { activityFromCoreEvent } = require('./updater/state');
const { Notifier } = require('./notifications');
const { StatusHub } = require('./status-hub');
const attachIpc = require('./attach-ipc');
const { CoreSupervisor } = require('./supervisor');

/**
 * The skills the person activated in the composer, as a clean list of names, or null when
 * there are none. Page script never drives the agent on its own; a non-string or empty name
 * is dropped here before it reaches the core.
 */
/** The composer's thinking level, or null for the session's own budget. */
const THINKING_LEVELS = new Set(['off', 'normal', 'long', 'free']);
function thinkingOf(value) {
  if (value === undefined || value === null) return null;
  if (!THINKING_LEVELS.has(value)) throw new Error('invalid thinking');
  return value;
}

function skillsOf(value) {
  if (value === undefined || value === null) return null;
  if (!Array.isArray(value)) throw new Error('invalid skills');
  const names = value.filter((name) => typeof name === 'string' && name);
  return names.length ? names : null;
}

/**
 * Show `/home/someone/work/thing` as `~/work/thing`. Done here, not in CSS: `direction: rtl`
 * (to keep a long path's tail visible) let bidi move the leading slash to the end, and the
 * window showed `home/user/…/desktop/`, a path that does not exist.
 */
function forDisplay(absolute) {
  const home = os.homedir();
  return absolute === home || absolute.startsWith(`${home}/`)
    ? `~${absolute.slice(home.length)}`
    : absolute;
}

/** The recent list for the project menu. Each entry gets `display`, its path shortened to
 * `~/…`. `path` stays exactly what the core gave, and is what opening or forgetting one
 * sends back. */
function withDisplay(projects) {
  return projects.map((entry) =>
    entry && typeof entry.path === 'string' ? { ...entry, display: forDisplay(entry.path) } : entry,
  );
}

/** The label of the model the core serves now (`GET /models`' `active`), or ''. */
async function servedModelLabel(core) {
  try {
    const { groups = [], active = null } = await core.models();
    for (const group of groups) {
      for (const model of group.models || []) if (model.id === active) return model.label || model.id;
    }
  } catch {
    // No catalog: the chip shows the model as unknown.
  }
  return '';
}

/** Phases that mean a turn has stopped, whatever the reason. */
const TERMINAL_PHASES = new Set(['done', 'interrupted', 'failed']);

/**
 * `onAgentActivity` hears turns, phases and pending resumes (`{type: 'turn-requested' |
 * 'turn-request-failed' | 'turn-ended' | 'resume-scheduled' | 'resume-settled'}`,
 * `{type: 'phase', phase}`). The updater uses it to know the agent is idle before it restarts.
 *
 * `projectRoot`, when given, pins the one project this window opens (the old single-project
 * behaviour, kept for callers such as tests). The app does not pass it. `core:open` then
 * opens the most recently opened project (design 1f), or returns `{needsProject: true,
 * projects}` so the window shows the picker. `core:open-project` (also the picker's «Abrir»
 * and the header chip's switch) opens any path, restarting the core the way a model switch
 * restarts the model server: a fresh `cogito serve` per project, never two at once.
 *
 * `browser` (browser/ipc.js), when given, answers the core's `browser_command` events and
 * closes its tabs whenever the session, the project or the conversation changes.
 */
function register({
  core,
  window,
  projectRoot = null,
  tray,
  trayHost,
  onAgentActivity = () => {},
  isPackaged = false,
  supervision = {},
  graph = null,
  browser = null,
}) {
  const toRenderer = (channel, payload) => {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  };

  // The context store's state (graph-keeper.js), for the header chip: pushed as it changes
  // and readable on load, since the first run can finish before the page is listening.
  // «Reintentar» asks the keeper again and answers with what it found.
  if (graph) {
    graph.on('state', (state) => toRenderer('graph:state', state));
    ipcMain.handle('graph:state', async () => graph.state);
    ipcMain.handle('graph:ensure', async () => graph.ensure({ announce: true }));
  }

  // Whether a turn is running, so `core:open-project` can refuse a switch. The window
  // disables its own control on the same fact; this is the backstop.
  let turnRunning = false;
  const notifyActivity = (activity) => {
    if (activity && activity.type === 'turn-requested') turnRunning = true;
    else if (activity && (activity.type === 'turn-ended' || activity.type === 'turn-request-failed')) {
      turnRunning = false;
    }
    onAgentActivity(activity);
  };

  // Busy from the moment a turn is asked for, not from its first phase event: an update
  // must never restart in the gap between the two.
  const requestingTurn = async (request) => {
    notifyActivity({ type: 'turn-requested' });
    try {
      return await request();
    } catch (error) {
      notifyActivity({ type: 'turn-request-failed' });
      throw error;
    }
  };

  // «Reintentar ahora» (from the window, a notification or the tray) asks for a turn too.
  const resumeTurn = () => requestingTurn(() => core.resume());

  // The tray, the taskbar and system notifications follow the core's events from one place
  // (status-hub.js). The renderer gets the same events on `core:event`.
  const log = (message) => process.stderr.write(`[status] ${message}\n`);
  const focusWindow = () => (tray ? tray.showWindow() : window.show());
  let hub = null;
  const notifier = new Notifier({ window, onAction: (spec, action) => hub.onAction(spec, action), log });
  hub = new StatusHub({
    tray,
    notifier,
    core,
    resume: resumeTurn,
    // No project chosen yet (fresh install, or a folder picked later): tray and
    // notifications show an empty name until `openProject` below sets the real one. Never
    // the packaged build's install directory (an audit finding).
    project: projectRoot ? path.basename(projectRoot) : '',
    toRenderer,
    focusWindow,
    log,
  });
  if (tray) {
    tray.onRetry = () => hub.onAction(null, 'retry');
    tray.onAction = (id) => hub.onTrayAction(id);
  }
  // Quitting: nothing resumes behind the app on its way out (the core is stopped next).
  app.on('before-quit', () => {
    Promise.resolve(core.sessionId ? core.cancelResume('quit') : null).catch(() => {});
  });
  app.on('before-quit', () => {
    hub.stop();
    notifier.destroy();
  });

  // The stream ends only with the session or the core, so an open turn or a scheduled
  // resume will never report its end over it.
  const streamEnded = () => {
    notifyActivity({ type: 'turn-ended' });
    notifyActivity({ type: 'resume-settled' });
  };

  // A stream that ends while the core and its session are still up is attached again: the
  // session's events wait in its queue meanwhile (`server/events.py`), so nothing is lost
  // but the one in flight, and a replayed event draws nothing twice (chat.js). Before, the
  // window stopped hearing the turn until the app was restarted. At most a few times a
  // minute, so a stream that keeps failing is still reported.
  const REATTACH_DELAY_MS = 1000;
  const REATTACH_WINDOW_MS = 60_000;
  const REATTACH_MAX = 5;
  let followedAgain = [];
  function followAgain(sessionId) {
    if (!sessionId || core.sessionId !== sessionId) return false;
    const now = Date.now();
    followedAgain = followedAgain.filter((at) => now - at < REATTACH_WINDOW_MS);
    if (followedAgain.length >= REATTACH_MAX) return false;
    followedAgain.push(now);
    setTimeout(() => {
      if (core.sessionId === sessionId) follow(sessionId);
    }, REATTACH_DELAY_MS);
    return true;
  }

  // Not awaited by its caller: it runs until the session closes (this project's core stops,
  // on quit or a later switch), so the handler can return and let the window render. A
  // failure is reported the same way a turn failure is.
  function attachStream() {
    // The session this stream follows: a browser answer goes back to it, whatever opens next.
    const sessionId = core.sessionId;
    if (browser) browser.sessionChanged(sessionId, activeRoot);
    follow(sessionId);
    hub.start();
  }

  function follow(sessionId) {
    core
      .streamEvents((event) => {
        // The core asking the browser for something: main's to answer, not the window's.
        if (browser && browser.onCoreEvent(event, sessionId)) return;
        toRenderer('core:event', event);
        // Approvals and component permission requests both notify from the hub.
        hub.onEvent(event);
        // Turns, phases and scheduled resumes, for the updater's idle check, in the order
        // `activityFromCoreEvent` gives, which never leaves an idle gap inside a turn.
        for (const activity of activityFromCoreEvent(event)) notifyActivity(activity);
        if (event.type === 'phase' && TERMINAL_PHASES.has(event.phase)) toRenderer('core:idle', {});
      })
      .then((how) => {
        if (how === 'ended' && followAgain(sessionId)) return;
        streamEnded();
      })
      .catch((error) => {
        if (error.name === 'AbortError') return streamEnded(); // the session closed; not a failure
        const failed = () => {
          streamEnded();
          toRenderer('core:event', { type: 'error', message: `event stream: ${error.message}` });
        };
        // A dead core takes its stream with it. The supervisor reports that on
        // `core:supervisor`; a second, vaguer error on top would only confuse.
        if (!supervisor) {
          if (!followAgain(sessionId)) failed();
          return;
        }
        supervisor.streamLost().then((accounted) => {
          if (accounted) return streamEnded();
          if (!followAgain(sessionId)) failed();
        });
      });
  }

  // The project whose session the stream follows, for the supervisor to reopen after a
  // restart. Null until one is open, and while a switch is under way.
  let activeRoot = null;

  // A core that reports its own exits (core.js's `Core`) is restarted when it dies unasked
  // (supervisor.js): the same project's session is reopened and the stream reattached, so
  // the renderer keeps getting `core:event`. The renderer is told on `core:supervisor`.
  const supervisor =
    typeof core.on === 'function'
      ? new CoreSupervisor({
          core,
          recover: async () => {
            await core.start({ isPackaged });
            const root = activeRoot;
            if (!root) return; // nothing was open: a running core is enough
            await core.openSession(root);
            attachStream();
          },
          report: (payload) => toRenderer('core:supervisor', payload),
          log: (message) => process.stderr.write(`[supervisor] ${message}\n`),
          ...supervision,
        })
      : null;

  // Whether `openProject` has opened a project this run. Not read from `core.sessionId`:
  // that is the core's own state, and a test fake does not have to reproduce it.
  let projectOpened = false;
  // What `openProject` answered for the project the core has open now. Null until one is
  // open, and while a switch is under way.
  let opened = null;

  /**
   * After `root` failed to open: reopen `previous` (the project that was open) on the
   * running core, so the window keeps a working session. A core still busy with the
   * failed open (it timed out) is restarted first, so it cannot finish that open later.
   * Marks `error.restored`.
   */
  async function restoreAfter(error, root, previous) {
    error.restored = false;
    if (!previous || previous === root) return error;
    try {
      if (error.code === 'timeout') {
        core.stop();
        await core.start({ isPackaged });
      }
      await core.openSession(previous);
      activeRoot = previous;
      attachStream();
      error.restored = true;
    } catch (cause) {
      process.stderr.write(`[projects] could not reopen ${previous}: ${cause && cause.message}\n`);
    }
    return error;
  }

  /**
   * Open `root` as the active project. Start the core if it is not running; with a project
   * already open, stop that core and start a fresh one, as a model switch restarts the model
   * server instead of hot-swapping it. `hub.project` and `tray.setProject` move to the new
   * name before the new session's events can reach either. When `root` fails to open, the
   * project that was open is reopened (`restoreAfter`) and the error is thrown.
   */
  async function openProject(root) {
    const previous = activeRoot;
    if (projectOpened) {
      core.stop(); // switching: nothing resumes behind the old project
      if (browser) browser.closeAll();
    }
    projectOpened = true;
    activeRoot = null;
    opened = null;
    let { model } = await core.start({ isPackaged });
    try {
      await core.openSession(root);
    } catch (error) {
      throw await restoreAfter(error, root, previous);
    }
    // The served model's own label from the catalog. The core's startup line names the
    // model it started on, which the welcome or the connection screen may have changed
    // since (QA 2026-10-11: the header named the old default after onboarding).
    model = (await servedModelLabel(core)) || model;
    activeRoot = root;
    const branch = await currentBranch(root);
    const name = path.basename(root);
    hub.project = name;
    if (tray && tray.setProject) tray.setProject(name);
    attachStream();
    opened = { project: name, projectRoot: forDisplay(root), model, branch };
    return opened;
  }

  // A reloaded window asks again for the project its core already has open. The core, its
  // session and the event stream (which sends to this same webContents) are all still
  // live, so the window reattaches to them; restarting the core would kill a running turn
  // (QA 2026-10-11 bug 29). `running` tells the window a turn is under way.
  async function reattach() {
    const model = (await servedModelLabel(core)) || opened.model;
    const branch = await currentBranch(activeRoot);
    return { ...opened, model, branch, running: turnRunning };
  }

  ipcMain.handle('core:open', async () => {
    if (opened && activeRoot) return reattach();
    if (projectRoot) return openProject(projectRoot);
    await core.start({ isPackaged });
    let projects = [];
    try {
      ({ projects = [] } = await core.listProjects());
    } catch {
      projects = [];
    }
    const candidate = projects.find((entry) => !entry.missing);
    if (!candidate) return { needsProject: true, projects: withDisplay(projects) };
    try {
      return await openProject(candidate.path);
    } catch (error) {
      // The picker, saying why, instead of a broken window (QA 2026-10-11).
      return {
        needsProject: true,
        projects: withDisplay(projects),
        error: error.message,
        code: error.code || null,
        failed: candidate.path,
      };
    }
  });

  // The project picker (design 1f): the recent list, a native folder dialog, opening a
  // project (first launch or a switch) and dropping one that no longer exists.
  ipcMain.handle('core:list-projects', async () => {
    await core.start({ isPackaged });
    try {
      const { projects = [] } = await core.listProjects();
      return { projects: withDisplay(projects) };
    } catch (error) {
      return { projects: [], error: error.message };
    }
  });

  ipcMain.handle('core:pick-folder', async () => {
    const result = await dialog.showOpenDialog(window, {
      title: 'Abrir carpeta',
      properties: ['openDirectory', 'createDirectory'],
    });
    if (!result || result.canceled || !result.filePaths || !result.filePaths[0]) {
      return { canceled: true };
    }
    return { path: result.filePaths[0] };
  });

  ipcMain.handle('core:open-project', async (_event, projectPath) => {
    if (typeof projectPath !== 'string' || !projectPath.trim()) {
      return { error: 'path must be a string' };
    }
    // Refused, not queued: the design never "switches later". The window disables the
    // control for the same reason; this is the backstop.
    if (turnRunning) return { error: 'busy' };
    try {
      return await openProject(projectPath);
    } catch (error) {
      // `restored`: the project open before is open again; false: no project is open.
      return { error: error.message, code: error.code || null, restored: Boolean(error.restored) };
    }
  });

  ipcMain.handle('core:forget-project', async (_event, projectPath) => {
    if (typeof projectPath !== 'string' || !projectPath.trim()) {
      return { error: 'path must be a string' };
    }
    const answer = await core.forgetProject(projectPath);
    return answer && Array.isArray(answer.projects) ? { ...answer, projects: withDisplay(answer.projects) } : answer;
  });

  // `attachments` (design 1n) only when the message has any; without them both calls are
  // unchanged. A file the core refuses at send time (it changed since it was attached) comes
  // back as `{refused}`, never a throw.
  ipcMain.handle('core:send', async (_event, text, attachments, skills, thinking) => {
    const specs = attachIpc.attachmentsOf(attachments);
    const chosen = skillsOf(skills);
    const level = thinkingOf(thinking);
    if (!specs && !chosen && !level) {
      await requestingTurn(() => core.send(text));
      return { ok: true };
    }
    try {
      // Each argument is added only when it is set, so an attachments-only send is the call
      // it always was.
      await requestingTurn(() =>
        level
          ? core.send(text, specs, chosen, level)
          : chosen
            ? core.send(text, specs, chosen)
            : core.send(text, specs),
      );
    } catch (error) {
      const refused = attachIpc.refusalOf(error);
      if (refused) return { ok: false, refused };
      throw error;
    }
    return { ok: true };
  });

  ipcMain.handle('core:steer', async (_event, text, attachments, thinking) => {
    const specs = attachIpc.attachmentsOf(attachments);
    const level = thinkingOf(thinking);
    if (!specs && !level) return requestingTurn(() => core.steer(text));
    try {
      return await requestingTurn(() => (level ? core.steer(text, specs, level) : core.steer(text, specs)));
    } catch (error) {
      const refused = attachIpc.refusalOf(error);
      if (refused) return { queued: false, refused };
      throw error;
    }
  });

  ipcMain.handle('core:interrupt', async () => {
    await core.interrupt();
    return { ok: true };
  });

  ipcMain.handle('core:approve', async (_event, { id, allowed, scope }) =>
    core.approve(id, Boolean(allowed), scope === 'session' ? 'session' : ''),
  );

  ipcMain.handle('core:auto', async (_event, on) => core.setAuto(on === true));

  ipcMain.handle('core:rewind-dry-run', async (_event, pointId) => core.rewindDryRun(pointId));

  ipcMain.handle('core:rewind', async (_event, { pointId, keepConflicts, force }) =>
    core.rewind(pointId, { keepConflicts: Boolean(keepConflicts), force: Boolean(force) }),
  );

  ipcMain.handle('core:redo', async (_event, redoId) => core.redo(redoId));

  // History on open: the project's thread, a page at a time. The renderer never builds a
  // URL; only the cursor and the page size cross.
  ipcMain.handle('core:history', async (_event, options) => {
    const { before = null, limit = 60 } = options && typeof options === 'object' ? options : {};
    return core.history({
      // Only a cursor the core itself handed out: `<session id>:<seq>`.
      before: typeof before === 'string' && /^[A-Za-z0-9_-]{1,64}:\d{1,12}$/.test(before) ? before : null,
      limit: Number.isInteger(limit) ? limit : 60,
    });
  });

  // The rest of a tool row's detail, on demand. The renderer never builds a URL; only a ref
  // the core handed out crosses: two plain ids, `<session>/<artifact>`.
  ipcMain.handle('core:tool-detail', async (_event, ref) => {
    if (typeof ref !== 'string' || !/^[A-Za-z0-9_-]{1,64}\/[A-Za-z0-9_-]{1,64}$/.test(ref)) {
      return { error: 'not an artifact reference' };
    }
    return core.toolDetail(ref);
  });

  ipcMain.handle('core:models', async () => core.models());

  // The model picker. The core refuses while a turn runs and cancels a pending resume
  // itself (`resume_cancelled`, reason "model"). A switch may stop one local model server
  // and start another, so the updater treats it as work and no restart lands mid-switch.
  ipcMain.handle('core:switch-model', async (_event, modelId) => {
    if (typeof modelId !== 'string' || !modelId.trim() || modelId.length > 200) {
      return { error: 'model_id must be a model id' };
    }
    notifyActivity({ type: 'turn-requested' });
    try {
      return await core.switchModel(modelId);
    } catch (error) {
      // A refusal is an answer the window shows (the core's own body), never a throw.
      const body = error && error.payload && typeof error.payload === 'object' ? error.payload : {};
      return { ...body, error: body.error || (error && error.message) || 'failed', status: error && error.status };
    } finally {
      notifyActivity({ type: 'turn-ended' });
    }
  });

  // The tray's usage (design 2b).
  ipcMain.handle('core:usage', async () => core.usage());
  ipcMain.handle('core:resume', async () => resumeTurn());

  // "Reiniciar contexto" (design 1a): a fresh conversation in the same project. The core
  // refuses (409) while a turn or rewind runs, or a switch is in flight; its body is the
  // window's answer, never a throw.
  ipcMain.handle('core:reset-context', async () => {
    try {
      const answer = await core.resetContext();
      // A fresh conversation starts with no tabs, as a new session does.
      if (browser) browser.closeAll();
      return answer;
    } catch (error) {
      const body = error && error.payload && typeof error.payload === 'object' ? error.payload : {};
      return { ...body, error: body.error || (error && error.message) || 'failed', status: error && error.status };
    }
  });

  // Components, programs, saved decisions, search levels and permission answers.
  require('./components-ipc').register({ core });
  // Attachments (design 1n): the native file dialog and what a dropped or pasted file is.
  attachIpc.register({ core, window });
  // The welcome and the connection screen (design 1d-1e, «Conectar otro servicio…»).
  require('./connect-ipc').register({ core, window, isPackaged, notifyActivity });

  // The frame is drawn in the page, so its three controls come back over IPC. Close hides:
  // the core holds a model, and dropping it on a window close would cost a minute to undo.
  ipcMain.handle('window:close', () => (trayHost ? trayHost.closeWindow(window) : window.hide()));
  ipcMain.handle('window:minimize', () => window.minimize());
  ipcMain.handle('window:maximize', () =>
    window.isMaximized() ? window.unmaximize() : window.maximize(),
  );
  // What the drawn controls need: which side and style (the platform), and whether the
  // maximise button means «restore» right now. Changes arrive on `window:maximized`
  // (`followMaximized` below).
  ipcMain.handle('window:state', () => ({ platform: process.platform, maximized: window.isMaximized() }));

  // For anything else that must tell the person something outside the window. The
  // updater's "Actualización lista" goes through `notifier.notify({...})` like every notice.
  // The open project's real path, for the file explorer (explorer-ipc.js, registered by
  // index.js).
  const activeProjectRoot = () => activeRoot;
  return { notifier, hub, supervisor, activeProjectRoot };
}

/**
 * Send `window:maximized` (a boolean) whenever the window's maximised state may have
 * changed, so the drawn maximise button shows «restore» exactly when it should.
 *
 * The button is not the only way to change it. A double-click on the drawn title bar is
 * handled below the page on every platform: the `-webkit-app-region: drag` area hit-tests
 * as HTCAPTION (Electron v44.4.3 `WebContentsView::NonClientHitTest`), and on Linux
 * Chromium's `WindowEventFilterLinux::OnClickedCaption` toggles maximise there and marks
 * the event handled (main, read 2026-09-29), as Windows and macOS do natively. The
 * compositor, a keyboard shortcut and leaving full screen change it too. A button that
 * tracks only its own clicks goes stale; electron/electron#20070 (2019-09) reports exactly
 * that.
 */
function followMaximized(window) {
  const report = () => {
    if (!window.isDestroyed()) window.webContents.send('window:maximized', window.isMaximized());
  };
  for (const event of ['maximize', 'unmaximize', 'enter-full-screen', 'leave-full-screen']) {
    window.on(event, report);
  }
}

module.exports = { register, followMaximized };
