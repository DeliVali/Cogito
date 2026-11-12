'use strict';

/**
 * Fixed Spanish texts the main process shows for updates: the tray item and the
 * «Actualización lista» notification (design 2b, 2c), which only slice B's notifier shows.
 * Copied verbatim from the design canvas, never generated.
 *
 * The renderer keeps its own copy of the texts both sides show («Reiniciar para
 * actualizar», «Reiniciar ahora», «Se actualiza al terminar») in `renderer/strings.js`
 * (`UPDATE`), for the CSP reason `shared/phase-map.js` gives: the renderer's same-directory
 * ES modules cannot reach a CommonJS file, and the main process cannot import an ES module
 * synchronously. A lab test checks the two copies agree. Keep them in sync.
 */

const UPDATE_COPY = Object.freeze({
  trayRestart: 'Reiniciar para actualizar',
  // The tray label while a restart is deferred (`state.js`'s `trayItem`). It matches the
  // window's chip (`renderer/strings.js`'s `UPDATE.deferred`). `trayRestart` there would
  // look clickable and do nothing, since `requestRestart` only re-defers while busy.
  deferred: 'Se actualiza al terminar',
  notifyTitle: 'Actualización lista',
  notifyBody: (version) => `COGITO ${version} · se instala al salir`,
  restartNow: 'Reiniciar ahora',
  // The macOS mock of the same notification (design 2c) reads «Reiniciar».
  restartShort: 'Reiniciar',
  later: 'Más tarde',
});

module.exports = { UPDATE_COPY };
