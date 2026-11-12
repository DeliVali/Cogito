'use strict';

/**
 * All the toast window can do: answer with one of its own buttons. What it shows arrives
 * in the page's own URL. Nothing else of the app is reachable from here.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('toast', {
  act: (id, action) => ipcRenderer.send('toast:action', { id, action }),
});
