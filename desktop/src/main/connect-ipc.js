'use strict';

/**
 * IPC for the welcome and the connection screen (design 1d-1e, and «Conectar otro
 * servicio…» in the model menu): the name, testing a service, a Cogito Motoris install or a
 * running Cogito Motoris, and saving the tested model.
 *
 * Three things never come from the page:
 *   - an address to open. The page names a link (`key:<service>` for «Obtener una clave»)
 *     and the https address is read from the core's table (`provider/connect.py`), as the
 *     component catalog does for its links;
 *   - a file path. A Cogito Motoris folder and its config are picked in this process's
 *     dialogs and remembered here; the page only says «test it» or «save it»;
 *   - anything logged. An API key passes through here in one request body to the core and
 *     is never stored or printed.
 *
 * An HTTP refusal answers `{error, status}` instead of throwing, as in `components-ipc.js`.
 * A core without these routes (an older one, or a test stand-in) answers `welcome:get`
 * with `{unavailable: true}`, and the window shows no welcome.
 */

const { dialog, ipcMain, shell } = require('electron');

const { isHttps } = require('./components-ipc');

/** A service id as `provider/connect.py` writes them. */
const SERVICE_ID = /^[a-z]{1,24}$/;
const MAX_TEXT = 500;

async function settled(promise) {
  try {
    return await promise;
  } catch (error) {
    const body = error && error.payload && typeof error.payload === 'object' ? error.payload : {};
    return { ...body, error: body.error || (error && error.message) || 'failed', status: error && error.status };
  }
}

function text(value, { max = MAX_TEXT, optional = false } = {}) {
  if (value === undefined || value === null || value === '') return optional ? undefined : null;
  if (typeof value !== 'string' || value.length > max || /[\u0000-\u001f\u007f]/.test(value)) return null;
  return value;
}

/** The welcome's values in the form the core takes, or null. */
function welcomeValues(values) {
  if (!values || typeof values !== 'object') return null;
  const out = {};
  if ('name' in values) {
    if (values.name !== null && (typeof values.name !== 'string' || values.name.length > 200)) return null;
    out.name = values.name;
  }
  if ('done' in values) {
    if (typeof values.done !== 'boolean') return null;
    out.done = values.done;
  }
  return Object.keys(out).length ? out : null;
}

/** The body for testing an API service, or null. */
function apiTestBody(values) {
  if (!values || typeof values !== 'object' || !SERVICE_ID.test(String(values.provider || ''))) return null;
  const key = text(values.key, { optional: true });
  const baseUrl = text(values.baseUrl, { optional: true });
  if (key === null || baseUrl === null) return null;
  return { provider: values.provider, key, base_url: baseUrl };
}

/** The body for asking a running Cogito Motoris for its models, or null. */
function motorisRunningBody(values) {
  if (!values || typeof values !== 'object') return null;
  const url = text(values.url);
  const key = text(values.key, { optional: true });
  if (!url || key === null) return null;
  return key === undefined ? { url } : { url, key };
}

/**
 * The body for saving a model, or null. `motoris` is what this process's dialogs picked and
 * its last install test read (the page never names a path). `activate` makes the new model
 * the one served from now on.
 */
function saveBody(values, motoris) {
  if (!values || typeof values !== 'object') return null;
  const label = text(values.label, { max: 120, optional: true });
  if (label === null || typeof values.activate !== 'boolean') return null;
  if (values.kind === 'motoris') {
    const found = motoris && motoris.found;
    if (!found) return null;
    const body = { kind: 'motoris', model: found.model, label, activate: values.activate, checkout: found.checkout, config: found.config };
    if (Number.isInteger(found.ctx) && found.ctx > 0) body.ctx = found.ctx;
    return body;
  }
  const model = text(values.model, { max: 200 });
  if (!model || values.kind !== 'api') return null;
  const base = { kind: 'api', model, label, activate: values.activate };
  if (values.provider === 'motoris') {
    const running = motorisRunningBody(values);
    return running ? { ...base, provider: 'motoris', ...running } : null;
  }
  const test = apiTestBody(values);
  return test ? { ...base, ...test } : null;
}

/** The https address behind a link the page named, from the core's table, or null. */
async function linkUrl(core, which) {
  const answer = await settled(core.connectProviders());
  if (!answer || answer.error) return null;
  const match = /^key:([a-z]{1,24})$/.exec(String(which));
  if (!match) return null;
  return (answer.key_pages && answer.key_pages[match[1]]) || null;
}

function register({
  core,
  window,
  isPackaged = false,
  notifyActivity = () => {},
  showOpenDialog = (win, options) => dialog.showOpenDialog(win, options),
  openExternal = (url) => shell.openExternal(url),
}) {
  /** What this process's dialogs picked: `{checkout, config, found}`. `found` is the last
   * install test's answer for exactly that folder and config. */
  const motoris = { checkout: null, config: null, found: null };
  const started = () => core.start({ isPackaged });

  // Marks work the updater must not restart during (a model loading, a switch).
  async function asWork(run) {
    notifyActivity({ type: 'turn-requested' });
    try {
      return await run();
    } finally {
      notifyActivity({ type: 'turn-ended' });
    }
  }

  ipcMain.handle('welcome:get', async () => {
    await started();
    const answer = await settled(core.welcome());
    if (answer && answer.error) return { unavailable: true, status: answer.status };
    return answer;
  });

  ipcMain.handle('welcome:set', async (_event, values) => {
    const clean = welcomeValues(values);
    if (!clean) return { error: 'invalid', status: 400 };
    await started();
    return settled(core.setWelcome(clean));
  });

  ipcMain.handle('connect:providers', async () => {
    await started();
    const answer = await settled(core.connectProviders());
    if (!answer || answer.error) return answer;
    // The page learns which links exist, never where they go.
    return { providers: answer.providers || [] };
  });

  ipcMain.handle('connect:api-models', async (_event, values) => {
    const body = apiTestBody(values);
    if (!body) return { error: 'invalid', status: 400 };
    await started();
    return settled(core.connectApiModels(body));
  });

  ipcMain.handle('connect:pick-motoris-folder', async () => {
    const picked = await showOpenDialog(window, {
      title: 'Elegir la carpeta de Cogito Motoris',
      properties: ['openDirectory'],
    });
    if (!picked || picked.canceled || !picked.filePaths || !picked.filePaths[0]) return { canceled: true };
    // Another folder: the config and the test that went with the old one no longer apply.
    Object.assign(motoris, { checkout: picked.filePaths[0], config: null, found: null });
    return { checkout: motoris.checkout };
  });

  ipcMain.handle('connect:pick-motoris-config', async () => {
    const picked = await showOpenDialog(window, {
      title: 'Elegir la configuración de Cogito Motoris',
      properties: ['openFile'],
      filters: [{ name: 'JSON', extensions: ['json'] }],
      ...(motoris.checkout ? { defaultPath: motoris.checkout } : {}),
    });
    if (!picked || picked.canceled || !picked.filePaths || !picked.filePaths[0]) return { canceled: true };
    Object.assign(motoris, { config: picked.filePaths[0], found: null });
    return { config: motoris.config };
  });

  // Reads the install without running it: nothing loads, so this is not work to wait for.
  ipcMain.handle('connect:motoris-install', async () => {
    if (!motoris.checkout || !motoris.config) return { error: 'invalid', status: 400 };
    const asked = { checkout: motoris.checkout, config: motoris.config };
    await started();
    const answer = await settled(core.connectMotorisInstall(asked));
    const same = motoris.checkout === asked.checkout && motoris.config === asked.config;
    motoris.found = same && answer && answer.ok ? { ...asked, model: answer.model, ctx: answer.ctx } : null;
    return answer;
  });

  ipcMain.handle('connect:motoris-running', async (_event, values) => {
    const body = motorisRunningBody(values);
    if (!body) return { error: 'invalid', status: 400 };
    await started();
    return settled(core.connectMotorisRunning(body));
  });

  ipcMain.handle('connect:save', async (_event, values) => {
    const body = saveBody(values, motoris);
    if (!body) return { error: 'invalid', status: 400 };
    await started();
    const run = () => settled(core.connectSave(body));
    return body.activate ? asWork(run) : run();
  });

  ipcMain.handle('connect:activate', async (_event, modelId) => {
    const id = text(modelId, { max: 200 });
    if (!id) return { error: 'invalid', status: 400 };
    await started();
    return asWork(() => settled(core.connectActivate(id)));
  });

  ipcMain.handle('connect:open-link', async (_event, which) => {
    const url = await linkUrl(core, which);
    if (!isHttps(url)) return { opened: false };
    await openExternal(url);
    return { opened: true };
  });
}

module.exports = { register, welcomeValues, apiTestBody, motorisRunningBody, saveBody, linkUrl };
