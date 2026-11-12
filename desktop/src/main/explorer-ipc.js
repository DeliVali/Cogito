'use strict';

/**
 * The file explorer and viewer (design 1a «Explorador y visor», 1p): the window's channels to
 * `explorer-fs.js`, the system trash, the system's file browser and default apps, the
 * clipboard, and `cogito-file:`, the scheme the viewer reads a PDF through (Chromium's own
 * PDF reader in an iframe: pages, zoom and selectable text with no library).
 *
 * The window names paths relative to the open project (`getRoot`); nothing is resolved from
 * the page. A deletion is moved aside first and sent to the system trash when its «Deshacer»
 * time ends, or when the app quits; it is never deleted outright.
 *
 * Answers are values or `{error: code}` (`explorer-fs.ExplorerError` codes), never thrown.
 */

const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { randomUUID } = require('node:crypto');
const { app, clipboard, ipcMain, net, protocol, shell } = require('electron');
const xfs = require('./explorer-fs');

const SCHEME = 'cogito-file';
/** How long «Deshacer» can bring a deletion back (the canvas's toast, 5000 ms), plus a
 * margin so a click at the last moment still lands. */
const UNDO_MS = 6000;
/** File system changes are told to the window at most this often. */
const WATCH_DEBOUNCE_MS = 250;

/** Must run before the app is ready (Electron's rule for privileged schemes). */
function registerScheme() {
  protocol.registerSchemesAsPrivileged([
    { scheme: SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } },
  ]);
}

function answer(fn) {
  return async (_event, ...args) => {
    try {
      return await fn(...args);
    } catch (error) {
      return { error: error instanceof xfs.ExplorerError ? error.code : 'failed', detail: error.message };
    }
  };
}

function register({ window, getRoot, trashItem = (p) => shell.trashItem(p) }) {
  const root = () => {
    const value = getRoot();
    if (!value) throw new xfs.ExplorerError('missing', 'no project is open');
    return value;
  };
  /** Staged deletions by id: `{item, root, timer}`. */
  const pending = new Map();

  /** Sends a staged deletion to the system trash. Only a trash that succeeded clears the
   * staging folder. When the system refuses (no trash on this file system, no desktop
   * session to ask), the item goes back where it was and the window is told: nothing is
   * ever deleted outright. */
  async function finish(id) {
    const entry = pending.get(id);
    if (!entry) return;
    pending.delete(id);
    clearTimeout(entry.timer);
    try {
      await trashItem(entry.item.staged);
      // A trash that says it worked but left the file where it was (on Linux Electron runs
      // `gio trash`, and any `gio` on PATH is believed) is a refusal, not a success.
      if (fs.existsSync(entry.item.staged)) throw new Error('the item is still where it was');
    } catch (error) {
      let restored = false;
      try {
        await xfs.restore(entry.root, entry.item);
        restored = true;
      } catch {
        // The name was taken meanwhile: it stays in .cogito/explorer-trash, untouched.
      }
      if (!window.isDestroyed()) {
        window.webContents.send('explorer:trash-failed', {
          name: entry.item.name,
          path: entry.item.path,
          restored,
          error: String((error && error.message) || error),
        });
      }
      return;
    }
    fs.rmSync(path.dirname(entry.item.staged), { recursive: true, force: true });
  }

  // The PDF reader's source: only a file inside the open project, only for reading.
  protocol.handle(SCHEME, async (request) => {
    try {
      const url = new URL(request.url);
      const rel = decodeURIComponent(url.pathname).replace(/^\/+/, '');
      const abs = xfs.resolveInside(root(), rel);
      if (xfs.kindOf(path.basename(abs)) !== 'pdf') return new Response('', { status: 404 });
      return net.fetch(pathToFileURL(abs).toString());
    } catch {
      return new Response('', { status: 404 });
    }
  });

  ipcMain.handle('explorer:list', answer((rel = '') => xfs.listDir(root(), rel)));
  ipcMain.handle('explorer:marks', answer(() => xfs.gitMarks(root())));
  ipcMain.handle(
    'explorer:read',
    answer(async (rel) => {
      const view = await xfs.readForView(root(), rel);
      if (view.kind === 'pdf') {
        const { abs: _abs, ...rest } = view;
        const url = `${SCHEME}://project/${rel.split('/').map(encodeURIComponent).join('/')}`;
        return { ...rest, url };
      }
      return view;
    }),
  );
  ipcMain.handle('explorer:reveal-values', answer((rel) => xfs.revealValues(root(), rel)));
  ipcMain.handle('explorer:new-file', answer((dir, name) => xfs.newFile(root(), dir, name)));
  ipcMain.handle('explorer:new-folder', answer((dir, name) => xfs.newFolder(root(), dir, name)));
  ipcMain.handle('explorer:rename', answer((rel, name) => xfs.rename(root(), rel, name)));
  ipcMain.handle('explorer:move', answer((rel, dir) => xfs.move(root(), rel, dir)));
  ipcMain.handle('explorer:duplicate', answer((rel) => xfs.duplicate(root(), rel)));
  ipcMain.handle(
    'explorer:copy-into',
    answer((dir, sources) => xfs.copyInto(root(), dir, Array.isArray(sources) ? sources.slice(0, 200) : [])),
  );
  ipcMain.handle(
    'explorer:delete',
    answer(async (rel) => {
      const base = root();
      const id = randomUUID();
      const item = await xfs.stageDelete(base, rel, id);
      const timer = setTimeout(() => finish(id).catch(() => {}), UNDO_MS);
      pending.set(id, { item, root: base, timer });
      return { id, name: item.name, path: item.path };
    }),
  );
  ipcMain.handle(
    'explorer:undo',
    answer(async (id) => {
      const entry = pending.get(id);
      if (!entry) throw new xfs.ExplorerError('missing', 'too late to undo');
      pending.delete(id);
      clearTimeout(entry.timer);
      return xfs.restore(entry.root, entry.item);
    }),
  );
  ipcMain.handle('explorer:reveal', answer((rel) => shell.showItemInFolder(xfs.resolveInside(root(), rel))));
  ipcMain.handle(
    'explorer:open-system',
    answer(async (rel) => {
      const failure = await shell.openPath(xfs.resolveInside(root(), rel));
      if (failure) throw new Error(failure);
      return { ok: true };
    }),
  );
  ipcMain.handle(
    'explorer:copy-path',
    answer((rel, relative) => {
      const abs = xfs.resolveInside(root(), rel);
      const text = relative ? path.relative(fs.realpathSync(root()), abs).split(path.sep).join('/') || '.' : abs;
      clipboard.writeText(text);
      return { text };
    }),
  );
  // The absolute path the chat's own attachments take (`attach:inspect`), for «Adjuntar al
  // mensaje»: the core checks it again, as for any attached path.
  ipcMain.handle('explorer:absolute', answer((rel) => ({ path: xfs.resolveInside(root(), rel) })));
  // Every file under a folder, for choosing what to attach: `{files: [{path, size, binary}]}`.
  ipcMain.handle('explorer:files-under', answer((rel) => xfs.filesUnder(root(), rel)));

  // Changes on disk (the model's edits, the person's editor, a build) reach the tree.
  let watcher = null;
  let watchedRoot = null;
  let debounce = null;
  function watch(base) {
    if (watcher) watcher.close();
    watcher = null;
    watchedRoot = base;
    if (!base) return;
    try {
      watcher = fs.watch(base, { recursive: true }, (_kind, file) => {
        const first = String(file || '').split(/[\\/]/)[0];
        if (xfs.HIDDEN.has(first)) return;
        clearTimeout(debounce);
        debounce = setTimeout(() => {
          if (!window.isDestroyed()) window.webContents.send('explorer:changed');
        }, WATCH_DEBOUNCE_MS);
      });
      watcher.on('error', () => {});
    } catch {
      watcher = null; // the tree still refreshes on «Actualizar» and after each turn
    }
  }
  ipcMain.handle('explorer:watch', answer(() => {
    const base = getRoot();
    if (base !== watchedRoot) watch(base);
    return { ok: true };
  }));

  // Nothing waits forever: what is still pending when the app quits goes to the trash
  // first, and only then does the app quit.
  let flushing = false;
  app.on('before-quit', (event) => {
    if (watcher) watcher.close();
    if (!pending.size || flushing) return;
    flushing = true;
    event.preventDefault();
    Promise.allSettled([...pending.keys()].map((id) => finish(id))).finally(() => app.quit());
  });

  return { pending, finish };
}

module.exports = { register, registerScheme, SCHEME, UNDO_MS };
