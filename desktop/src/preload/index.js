'use strict';

/**
 * The context bridge. This is everything the page can call.
 *
 * Listeners are wrapped instead of passed through, so the renderer gets the event payload
 * and never the Electron `IpcRendererEvent`. That event carries a `sender`, which would
 * give page script a way back into the main process.
 */

const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('cogito', {
  // `{project, projectRoot, model, branch}`, or `{needsProject: true, projects}` when
  // nothing usable was last opened (design 1f). See the comment in src/main/ipc.js.
  open: () => ipcRenderer.invoke('core:open'),
  // `attachments` (design 1n), `[{path} | {text}]`, and `skills` (the composer's picker),
  // `["name", …]`, each passed as null when the message has none. `thinking` (the composer's
  // thinking control) is "off", "normal", "long" or "free", or null where it is hidden.
  send: (text, attachments, skills, thinking) =>
    ipcRenderer.invoke(
      'core:send',
      text,
      attachments && attachments.length ? attachments : null,
      skills && skills.length ? skills : null,
      thinking || null,
    ),
  // `thinking` goes only with a steer that falls back to a new turn (the turn ended first).
  steer: (text, attachments, thinking) =>
    thinking
      ? ipcRenderer.invoke('core:steer', text, attachments && attachments.length ? attachments : null, thinking)
      : attachments && attachments.length
        ? ipcRenderer.invoke('core:steer', text, attachments)
        : ipcRenderer.invoke('core:steer', text),
  interrupt: () => ipcRenderer.invoke('core:interrupt'),
  approve: (id, allowed, scope) => ipcRenderer.invoke('core:approve', { id, allowed, scope }),
  // The composer's automatic mode (`POST /sessions/{id}/auto`): `{ok: true}` once the core
  // took it, `{ok: false}` otherwise.
  setAuto: (on) => ipcRenderer.invoke('core:auto', on === true),
  rewindDryRun: (pointId) => ipcRenderer.invoke('core:rewind-dry-run', pointId),
  rewind: (pointId, { keepConflicts = false, force = false } = {}) =>
    ipcRenderer.invoke('core:rewind', { pointId, keepConflicts, force }),
  redo: (redoId) => ipcRenderer.invoke('core:redo', redoId),
  history: ({ before = null, limit = 60 } = {}) => ipcRenderer.invoke('core:history', { before, limit }),
  // The rest of a tool row's detail: its `detail.ref`, never a path or a URL.
  toolDetail: (ref) => ipcRenderer.invoke('core:tool-detail', ref),
  models: () => ipcRenderer.invoke('core:models'),
  // The model picker: `{changed, model}`, or `{error, ...}` for a switch that did not happen.
  switchModel: (modelId) => ipcRenderer.invoke('core:switch-model', modelId),
  usage: () => ipcRenderer.invoke('core:usage'),
  resume: () => ipcRenderer.invoke('core:resume'),
  // "Reiniciar contexto" (design 1a): start a fresh conversation in the same project. The
  // window session id stays; the old conversation is kept and still readable in history.
  resetContext: () => ipcRenderer.invoke('core:reset-context'),
  // The project picker (design 1f). See the comment on `openProject` in src/main/ipc.js.
  projects: {
    list: () => ipcRenderer.invoke('core:list-projects'),
    pickFolder: () => ipcRenderer.invoke('core:pick-folder'),
    open: (path) => ipcRenderer.invoke('core:open-project', path),
    forget: (path) => ipcRenderer.invoke('core:forget-project', path),
  },
  // The welcome and the connection screen (design 1d-1e), see src/main/connect-ipc.js. A
  // Cogito Motoris folder and config are picked in the main process's own dialogs. A link
  // is named, never given as a URL.
  welcome: {
    get: () => ipcRenderer.invoke('welcome:get'),
    set: (values) => ipcRenderer.invoke('welcome:set', values),
  },
  connect: {
    providers: () => ipcRenderer.invoke('connect:providers'),
    apiModels: (values) => ipcRenderer.invoke('connect:api-models', values),
    pickMotorisFolder: () => ipcRenderer.invoke('connect:pick-motoris-folder'),
    pickMotorisConfig: () => ipcRenderer.invoke('connect:pick-motoris-config'),
    motorisInstall: () => ipcRenderer.invoke('connect:motoris-install'),
    motorisRunning: (values) => ipcRenderer.invoke('connect:motoris-running', values),
    save: (values) => ipcRenderer.invoke('connect:save', values),
    activate: (modelId) => ipcRenderer.invoke('connect:activate', modelId),
    openLink: (which) => ipcRenderer.invoke('connect:open-link', which),
  },
  // Attachments (design 1n), see src/main/attach-ipc.js. The page never reads a file: it
  // names one and the core says what it is. `pathOf` is where a dropped or pasted `File` is
  // on disk ('' when it is not a file on disk). Electron removed `File.path` in 32.
  attach: {
    pick: () => ipcRenderer.invoke('attach:pick'),
    inspect: (spec) => ipcRenderer.invoke('attach:inspect', spec),
    pathOf: (file) => {
      try {
        return webUtils.getPathForFile(file) || '';
      } catch {
        return '';
      }
    },
  },
  // The file explorer and viewer (design 1a, 1p), see src/main/explorer-ipc.js. Paths are
  // relative to the open project; the main process resolves and checks every one.
  explorer: {
    list: (rel) => ipcRenderer.invoke('explorer:list', rel),
    marks: () => ipcRenderer.invoke('explorer:marks'),
    read: (rel) => ipcRenderer.invoke('explorer:read', rel),
    revealValues: (rel) => ipcRenderer.invoke('explorer:reveal-values', rel),
    newFile: (dir, name) => ipcRenderer.invoke('explorer:new-file', dir, name),
    newFolder: (dir, name) => ipcRenderer.invoke('explorer:new-folder', dir, name),
    rename: (rel, name) => ipcRenderer.invoke('explorer:rename', rel, name),
    duplicate: (rel) => ipcRenderer.invoke('explorer:duplicate', rel),
    move: (rel, dir) => ipcRenderer.invoke('explorer:move', rel, dir),
    copyInto: (dir, sources) => ipcRenderer.invoke('explorer:copy-into', dir, sources),
    remove: (rel) => ipcRenderer.invoke('explorer:delete', rel),
    undo: (id) => ipcRenderer.invoke('explorer:undo', id),
    reveal: (rel) => ipcRenderer.invoke('explorer:reveal', rel),
    openSystem: (rel) => ipcRenderer.invoke('explorer:open-system', rel),
    copyPath: (rel, relative) => ipcRenderer.invoke('explorer:copy-path', rel, relative),
    absolute: (rel) => ipcRenderer.invoke('explorer:absolute', rel),
    filesUnder: (rel) => ipcRenderer.invoke('explorer:files-under', rel),
    watch: () => ipcRenderer.invoke('explorer:watch'),
    onChanged: (handler) => ipcRenderer.on('explorer:changed', () => handler()),
    onTrashFailed: (handler) => ipcRenderer.on('explorer:trash-failed', (_event, info) => handler(info)),
  },
  // Terminal tabs beside «Hilo» (src/main/terminal-ipc.js): the core's shells, named by tab
  // id. What a shell prints arrives on `onEvent` (`terminal_data` and the rest).
  terminal: {
    list: () => ipcRenderer.invoke('terminal:list'),
    open: (options) => ipcRenderer.invoke('terminal:open', options || {}),
    get: (id) => ipcRenderer.invoke('terminal:get', id),
    input: (id, data) => ipcRenderer.invoke('terminal:input', id, data),
    resize: (id, cols, rows) => ipcRenderer.invoke('terminal:resize', id, cols, rows),
    close: (id) => ipcRenderer.invoke('terminal:close', id),
    // A link in a terminal's output: the system browser, http(s) only (main checks).
    openLink: (url) => ipcRenderer.invoke('terminal:open-link', url),
  },
  // Browser tabs beside «Hilo» (src/main/browser/): main owns each tab's page and draws it
  // over the rect `bounds` names; null hides every page. `onTabs` hears the whole list,
  // `[{id, title, url, loading, canBack, canForward, favicon?, failed?}]`, whenever it changes.
  browser: {
    list: () => ipcRenderer.invoke('browser:list'),
    bounds: (id, rect) => ipcRenderer.invoke('browser:bounds', id || null, rect || null),
    // The address bar's text, as typed: main makes it an address or answers `{error}`.
    navigate: (id, text) => ipcRenderer.invoke('browser:navigate', id, text),
    back: (id) => ipcRenderer.invoke('browser:back', id),
    forward: (id) => ipcRenderer.invoke('browser:forward', id),
    reload: (id) => ipcRenderer.invoke('browser:reload', id),
    stop: (id) => ipcRenderer.invoke('browser:stop', id),
    close: (id) => ipcRenderer.invoke('browser:close', id),
    open: (url) => (url ? ipcRenderer.invoke('browser:new', url) : ipcRenderer.invoke('browser:new')),
    onTabs: (handler) => ipcRenderer.on('browser:tabs', (_event, tabs) => handler(tabs)),
    // An app shortcut pressed while a page had the keyboard, `{key, code, ctrl, meta, shift,
    // alt}`: main kept it from the page and focused the window.
    onShortcut: (handler) => ipcRenderer.on('browser:shortcut', (_event, shortcut) => handler(shortcut)),
  },
  onReveal: (handler) => ipcRenderer.on('app:reveal', (_event, what) => handler(what)),
  // «Cambiar modelo» from the tray or a notification: open the model menu.
  onOpenModelMenu: (handler) => ipcRenderer.on('app:open-model-menu', () => handler()),
  // The component catalog (design 4a/4b/4e), see src/main/components-ipc.js.
  components: {
    list: () => ipcRenderer.invoke('components:list'),
    install: (id) => ipcRenderer.invoke('components:install', id),
    retry: (id) => ipcRenderer.invoke('components:retry', id),
    ignore: (id) => ipcRenderer.invoke('components:ignore', id),
    decline: (id) => ipcRenderer.invoke('components:decline', id),
    setEnabled: (id, enabled) => ipcRenderer.invoke('components:set-enabled', { id, enabled }),
    programs: () => ipcRenderer.invoke('components:programs'),
    programHelp: (id) => ipcRenderer.invoke('components:program-help', id),
    copyCommand: (id) => ipcRenderer.invoke('components:copy-command', id),
    openOfficial: (id) => ipcRenderer.invoke('components:open-official', id),
    decisions: () => ipcRenderer.invoke('components:decisions'),
    reactivate: (id, what) => ipcRenderer.invoke('components:reactivate', { id, what }),
    searchLevels: () => ipcRenderer.invoke('components:search-levels'),
    answerPermission: (id, decision) => ipcRenderer.invoke('components:answer-permission', { id, decision }),
  },
  // Conexiones: the MCP servers (core/cogito/server/connections.py).
  connections: {
    list: () => ipcRenderer.invoke('connections:list'),
    set: (name, values) => ipcRenderer.invoke('connections:set', { name, values }),
    add: (name, command) => ipcRenderer.invoke('connections:add', { name, command }),
    remove: (name) => ipcRenderer.invoke('connections:remove', name),
    retry: (name) => ipcRenderer.invoke('connections:retry', name),
    discover: () => ipcRenderer.invoke('connections:discover'),
    import: (keys) => ipcRenderer.invoke('connections:import', keys),
    login: (name) => ipcRenderer.invoke('connections:login', name),
  },
  // Habilidades: the Agent Skills the composer's picker lists (core/cogito/server/skills.py).
  skills: {
    list: () => ipcRenderer.invoke('skills:list'),
  },
  // Diseño: the theme, 'light', 'dark' or 'system' (src/main/appearance.js). Both answer
  // `{theme, dark}`; the stylesheets follow through `prefers-color-scheme` on their own.
  appearance: {
    get: () => ipcRenderer.invoke('appearance:get'),
    set: (theme) => ipcRenderer.invoke('appearance:set', theme),
  },
  onEvent: (handler) => ipcRenderer.on('core:event', (_event, payload) => handler(payload)),
  onIdle: (handler) => ipcRenderer.on('core:idle', () => handler()),
  // The core died and is being restarted (src/main/supervisor.js): `{state: 'restarting',
  // attempt, max, code, signal, retryInMs}`, `{state: 'restarted', attempt}` or
  // `{state: 'gave-up', attempts, code, signal}`. Events keep arriving on `onEvent` after it.
  // The context store (src/main/graph-keeper.js): its state now, «Reintentar» (starts it if
  // it is down, answers with what it found) and each change as it happens. `{state:
  // 'unknown' | 'starting' | 'ready' | 'down', reason?, detail?}`.
  graphState: () => ipcRenderer.invoke('graph:state'),
  graphEnsure: () => ipcRenderer.invoke('graph:ensure'),
  onGraphState: (handler) => ipcRenderer.on('graph:state', (_e, payload) => handler(payload)),
  onCoreSupervisor: (handler) => ipcRenderer.on('core:supervisor', (_e, payload) => handler(payload)),
  window: {
    // Which platform's controls to draw (side and style). A sandboxed preload's `process`
    // keeps `platform` (Electron 44.4.3 `process.md`, "Sandbox").
    platform: process.platform,
    close: () => ipcRenderer.invoke('window:close'),
    minimize: () => ipcRenderer.invoke('window:minimize'),
    maximize: () => ipcRenderer.invoke('window:maximize'),
    // `{platform, maximized}` now. `onMaximizedChange` hears every later change, including
    // a double-click on the title bar, which the system handles, not the page.
    state: () => ipcRenderer.invoke('window:state'),
    onMaximizedChange: (handler) => ipcRenderer.on('window:maximized', (_e, value) => handler(Boolean(value))),
    // The window was shown again (from the tray) or restored from minimised.
    onShown: (handler) => ipcRenderer.on('window:shown', () => handler()),
  },
  // Automatic updates (src/main/updater/). The page only asks and reports choices. The main
  // process decides what gets downloaded, verified and installed.
  update: {
    get: () => ipcRenderer.invoke('update:get'),
    onState: (handler) => ipcRenderer.on('update:state', (_event, state) => handler(state)),
    restartNow: () => ipcRenderer.invoke('update:restart', 'now'),
    restartWhenDone: () => ipcRenderer.invoke('update:restart', 'when-done'),
    // «Al salir»: cancels a pending deferred restart back to `ready`. See
    // `updater/state.js`'s `defer-to-quit`.
    deferToQuit: () => ipcRenderer.invoke('update:defer-to-quit'),
    retry: () => ipcRenderer.invoke('update:retry'),
    cardSeen: () => ipcRenderer.invoke('update:card-seen'),
    copyCommand: () => ipcRenderer.invoke('update:copy-command'),
    takeOutcome: () => ipcRenderer.invoke('update:take-outcome'),
  },
});
