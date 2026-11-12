'use strict';

/**
 * The browser's channels to the window, and the one object the rest of main uses.
 *
 * The window draws the tab strip and the pane; main owns the tabs. It hears `browser:tabs`
 * (`[{id, title, url, loading, canBack, canForward, favicon?}]`) whenever a tab changes, and
 * asks with `browser:list`, `browser:bounds`, `browser:navigate`, `browser:back`,
 * `browser:forward`, `browser:reload`, `browser:stop`, `browser:close` and `browser:new`.
 * Only the app's own window may ask, and every argument is checked for shape here; an
 * answer is a value or `{error}`, never a throw. The address bar's channels (`browser:new`,
 * `browser:navigate`) answer an error code the window words: `not_address`, `not_web` or
 * `failed`. Main also sends `browser:shortcut` (`{key, code, ctrl, meta, shift, alt}`) for
 * one of the app's shortcuts typed while a page had the focus (shortcuts.js).
 */

const electron = require('electron');

const { BrowserHost } = require('./host');
const { BrowserRpc } = require('./rpc');

const TAB_ID = /^b\d{1,6}$/;
const ADDRESS_ERRORS = new Set(['not_address', 'not_web', 'failed']);
const MAX_COORD = 100000;

const coordinate = (value, min) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= MAX_COORD;

/** A rect the window sent, or undefined when it is not one. */
function rectOf(value) {
  if (value === null) return null;
  if (!value || typeof value !== 'object') return undefined;
  const { x, y, width, height } = value;
  if (!coordinate(x, -MAX_COORD) || !coordinate(y, -MAX_COORD) || !coordinate(width, 0) || !coordinate(height, 0)) return undefined;
  return { x, y, width, height };
}

const tabId = (value) => (typeof value === 'string' && TAB_ID.test(value) ? value : null);

function register({
  window,
  core,
  ipc = electron.ipcMain,
  modules = { WebContentsView: electron.WebContentsView, session: electron.session },
  log = (message) => process.stderr.write(`[browser] ${message}\n`),
  driverOptions,
}) {
  const host = new BrowserHost({
    window,
    electron: modules,
    log,
    driverOptions,
    onTabs: (list) => {
      if (!window.isDestroyed()) window.webContents.send('browser:tabs', list);
    },
  });
  const rpc = new BrowserRpc({ core, host, log });

  const fromWindow = (fn) => async (event, ...args) => {
    if (!event || event.sender !== window.webContents) return { error: 'not allowed' };
    try {
      return await fn(...args);
    } catch (error) {
      return { error: (error && error.message) || 'failed' };
    }
  };
  const forTab = (fn) => fromWindow((id, ...rest) => {
    const tab = tabId(id);
    if (!tab) return { error: 'bad tab' };
    return fn(tab, ...rest);
  });

  ipc.handle('browser:list', fromWindow(() => host.list()));
  ipc.handle(
    'browser:bounds',
    fromWindow((id, rect) => {
      const shown = id === null || id === undefined ? null : tabId(id);
      if (shown === null && id !== null && id !== undefined) return { error: 'bad tab' };
      const area = rectOf(rect === undefined ? null : rect);
      if (area === undefined) return { error: 'bad rect' };
      host.setBounds(area ? shown : null, area);
      return { ok: true };
    }),
  );
  // The address bar's answers carry only the codes the window words.
  const address = (fn) => async (event, ...args) => {
    const answer = await fromWindow(fn)(event, ...args);
    if (answer && answer.error !== undefined && !ADDRESS_ERRORS.has(answer.error)) return { error: 'failed' };
    return answer;
  };
  ipc.handle(
    'browser:navigate',
    address((id, text) => {
      if (!tabId(id)) return { error: 'failed' };
      return typeof text === 'string' ? host.navigateFromWindow(tabId(id), text) : { error: 'not_address' };
    }),
  );
  for (const action of ['back', 'forward', 'reload', 'stop']) {
    ipc.handle(`browser:${action}`, forTab((id) => host.control(id, action)));
  }
  ipc.handle('browser:close', forTab((id) => (host.close(id) ? { ok: true } : { error: 'no such tab' })));
  ipc.handle(
    'browser:new',
    address((url) => {
      if (url !== undefined && url !== null && typeof url !== 'string') return { error: 'not_address' };
      return host.openFromWindow(url);
    }),
  );

  return {
    host,
    /** A core event from `sessionId`'s stream; true when it was a browser command. */
    onCoreEvent: (event, sessionId) => rpc.handle(event, sessionId),
    sessionChanged: (sessionId, root) => host.sessionChanged(sessionId, root),
    closeAll: () => host.closeAll(),
    owns: (contents) => host.owns(contents),
    harden: (contents) => host.harden(contents),
  };
}

module.exports = { register, rectOf, TAB_ID };
