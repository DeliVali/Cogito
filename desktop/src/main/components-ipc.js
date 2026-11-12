'use strict';

/**
 * IPC for the component catalog: the loopback routes in `core/cogito/server/components.py`,
 * plus two things only the main process may do: write the clipboard and open a link in the
 * system browser.
 *
 * Both take a program id, never text or a URL from the page. The command and the link are
 * read from the core's catalog here, so page script cannot put arbitrary text on the
 * clipboard or open an arbitrary address through this channel. Only https links open.
 *
 * Answers are the route's JSON on success and `{error, status}` otherwise. An HTTP error
 * status never throws at the renderer; only an unreachable core does.
 */

const { clipboard, ipcMain, shell } = require('electron');

const segment = (value) => encodeURIComponent(String(value));

function isHttps(url) {
  try {
    return new URL(url).protocol === 'https:';
  } catch {
    return false;
  }
}

function register({ core }) {
  async function api(method, route, body) {
    const response = await fetch(`${core.address}${route}`, {
      method,
      headers: { Authorization: `Bearer ${core.token}`, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload = {};
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      payload = { error: text.slice(0, 200) };
    }
    if (!response.ok) {
      return {
        error: payload.error || payload.detail || `${route} answered ${response.status}`,
        status: response.status,
        ...(payload.code ? { code: String(payload.code) } : {}),
      };
    }
    return payload;
  }

  const help = (programId) => api('GET', `/system/programs/${segment(programId)}/help`);

  ipcMain.handle('components:list', () => api('GET', '/components'));
  ipcMain.handle('components:install', (_event, id) => api('POST', `/components/${segment(id)}/install`));
  ipcMain.handle('components:retry', (_event, id) => api('POST', `/components/${segment(id)}/retry`));
  ipcMain.handle('components:ignore', (_event, id) => api('POST', `/components/${segment(id)}/ignore`));
  ipcMain.handle('components:decline', (_event, id) => api('POST', `/components/${segment(id)}/decline`));
  ipcMain.handle('components:set-enabled', (_event, { id, enabled }) =>
    api('PUT', `/components/${segment(id)}/enabled`, { enabled: Boolean(enabled) }),
  );
  ipcMain.handle('components:programs', () => api('GET', '/system/programs'));
  ipcMain.handle('components:program-help', (_event, id) => help(id));
  ipcMain.handle('components:decisions', () => api('GET', '/decisions'));
  ipcMain.handle('components:reactivate', (_event, { id, what }) =>
    api('POST', '/decisions/reactivate', { id: String(id), what: String(what) }),
  );
  ipcMain.handle('components:search-levels', () => api('GET', '/settings/search'));
  // Conexiones (design 4e, 4f, the 1a chip): the MCP servers in config.toml.
  ipcMain.handle('connections:list', () => api('GET', '/connections'));
  ipcMain.handle('connections:set', (_event, { name, values }) =>
    api('PUT', `/connections/${segment(name)}`, values && typeof values === 'object' ? values : {}),
  );
  ipcMain.handle('connections:add', (_event, { name, command }) =>
    api('POST', '/connections', { name: String(name ?? ''), command: String(command ?? '') }),
  );
  ipcMain.handle('connections:remove', (_event, name) => api('DELETE', `/connections/${segment(name)}`));
  ipcMain.handle('connections:retry', (_event, name) => api('POST', `/connections/${segment(name)}/retry`));
  // «Importar»: servers other clients configured, and the open project's `.mcp.json`.
  ipcMain.handle('connections:discover', () =>
    api('GET', `/connections/import${core.sessionId ? `?session=${segment(core.sessionId)}` : ''}`),
  );
  ipcMain.handle('connections:import', (_event, keys) =>
    api('POST', '/connections/import', {
      keys: Array.isArray(keys) ? keys.map(String) : [],
      ...(core.sessionId ? { session: String(core.sessionId) } : {}),
    }),
  );
  // «Iniciar sesión»: the core gives the server's sign-in page, opened here, https only, so
  // the renderer never opens an address itself.
  ipcMain.handle('connections:login', async (_event, name) => {
    const answer = await api('POST', `/connections/${segment(name)}/login`);
    if (!answer || answer.error || !isHttps(answer.url)) return { error: (answer && answer.error) || 'login' };
    await shell.openExternal(answer.url);
    return { opened: true };
  });
  // Habilidades (the composer's skills picker): the Agent Skills the open project offers.
  ipcMain.handle('skills:list', () =>
    api('GET', `/sessions/${segment(core.sessionId)}/skills`),
  );
  ipcMain.handle('components:answer-permission', (_event, { id, decision }) =>
    api('POST', `/sessions/${segment(core.sessionId)}/permissions/${segment(id)}`, { decision: String(decision) }),
  );

  // «Copiar»: only the command the core's catalog gives for this program.
  ipcMain.handle('components:copy-command', async (_event, programId) => {
    const answer = await help(programId);
    if (answer.error || typeof answer.command !== 'string' || !answer.command) return { copied: false };
    clipboard.writeText(answer.command);
    return { copied: true };
  });

  // «Abrir sitio oficial ↗»: the catalog's link for this program, https only.
  ipcMain.handle('components:open-official', async (_event, programId) => {
    const answer = await help(programId);
    const url = answer.official_url;
    if (answer.error || !isHttps(url)) return { opened: false };
    await shell.openExternal(url);
    return { opened: true };
  });
}

module.exports = { register, isHttps };
