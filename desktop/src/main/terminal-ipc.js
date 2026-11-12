'use strict';

/**
 * The terminal tabs' channels (design 2026-11-01): the window's calls to the core's
 * `/sessions/{id}/terminals` routes, with the token only this process holds. The page names
 * a tab by its id and never sees the port, the token or the session.
 *
 * What the shell prints reaches the window as core events (`terminal_data` and the rest),
 * on `core:event` like every other.
 *
 * Arguments are checked for shape here, with the core's own limits; the core checks what
 * they mean. Answers are the route's JSON, or `{error, status}`; an HTTP error never throws
 * at the renderer.
 *
 * `terminal:open-link` opens a link from a terminal's output in the system browser, http(s)
 * only; no page of the app opens a window of its own (index.js denies `window.open`).
 */

const { ipcMain, shell } = require('electron');

/** A tab id as the core makes them. */
const TAB_ID = /^[A-Za-z0-9_-]{1,64}$/;
/** One input request, the core's limit; the renderer sends a larger paste in pieces. */
const MAX_INPUT = 65536;
/** What the Run button types: one or more lines, the core's limit. */
const MAX_COMMAND = 16000;
// What the core refuses in a command: C0 controls but the line break, DEL, C1, and the
// invisible and bidi format characters.
const REFUSED = /[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/;
/** The longest link handed to the browser. */
const MAX_LINK = 4096;

const size = (value) => (Number.isInteger(value) && value >= 2 && value <= 1000 ? value : null);

/** `text` as an http(s) address to open, or null. */
function webLink(text) {
  if (typeof text !== 'string' || !text || text.length > MAX_LINK) return null;
  try {
    const url = new URL(text);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch {
    return null;
  }
}

function register({ core, ipc = ipcMain, openExternal = (url) => shell.openExternal(url) }) {
  async function api(method, route, body) {
    if (!core.sessionId) return { error: 'no session', status: 0 };
    const response = await fetch(`${core.address}/sessions/${encodeURIComponent(core.sessionId)}/terminals${route}`, {
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
      return { error: payload.error || payload.detail || `terminals answered ${response.status}`, status: response.status };
    }
    return payload;
  }

  /** Runs `fn` for a well-formed tab id, else answers `{error: 'bad tab'}`. */
  const forTab = (fn) => async (_event, id, ...rest) => {
    if (typeof id !== 'string' || !TAB_ID.test(id)) return { error: 'bad tab', status: 400 };
    return fn(encodeURIComponent(id), ...rest);
  };
  const guarded = (fn) => async (...args) => {
    try {
      return await fn(...args);
    } catch (error) {
      return { error: error.message || 'failed', status: 0 };
    }
  };

  ipc.handle('terminal:list', guarded(() => api('GET', '')));
  ipc.handle(
    'terminal:open',
    guarded((_event, options) => {
      const body = {};
      const given = options && typeof options === 'object' ? options : {};
      if (given.command !== undefined) {
        const command = given.command;
        if (typeof command !== 'string' || !command.trim() || command.length > MAX_COMMAND || REFUSED.test(command)) {
          return { error: 'bad command', status: 400 };
        }
        body.command = given.command;
      }
      const cols = size(given.cols);
      const rows = size(given.rows);
      if (cols && rows) {
        body.cols = cols;
        body.rows = rows;
      }
      return api('POST', '', body);
    }),
  );
  ipc.handle('terminal:get', guarded(forTab((id) => api('GET', `/${id}`))));
  ipc.handle(
    'terminal:input',
    guarded(
      forTab((id, data) => {
        if (typeof data !== 'string' || !data || data.length > MAX_INPUT) return { error: 'bad input', status: 400 };
        return api('POST', `/${id}/input`, { data });
      }),
    ),
  );
  ipc.handle(
    'terminal:resize',
    guarded(
      forTab((id, cols, rows) => {
        if (!size(cols) || !size(rows)) return { error: 'bad size', status: 400 };
        return api('POST', `/${id}/resize`, { cols, rows });
      }),
    ),
  );
  ipc.handle('terminal:close', guarded(forTab((id) => api('DELETE', `/${id}`))));
  ipc.handle(
    'terminal:open-link',
    guarded(async (_event, text) => {
      const url = webLink(text);
      if (!url) return { error: 'bad link', status: 400 };
      await openExternal(url);
      return { ok: true };
    }),
  );
}

module.exports = { register, webLink, TAB_ID, MAX_INPUT, MAX_COMMAND };
