'use strict';

/**
 * «Actualización lista» (design 2c) as one notification for slice B's notifier
 * (`main/notifications.js`). It uses the same `notify({key, kind, title, body, actions,
 * sticky})` as every other notice, so each system shows it the usual way: Electron's
 * notification on Windows and macOS, `notify-send` with buttons on Linux, and the app's own
 * toast window where those cannot (an unsigned macOS app's `failed`, a notification server
 * without buttons). When it is sent (once per version, only idle, only with the window
 * hidden) is `state.js`'s `shouldNotify`. This module only builds what it says.
 *
 * Buttons: «Reiniciar ahora» / «Más tarde» («Reiniciar» on macOS, as its mock reads). The
 * status hub passes clicks to the spec's own `onAction`: «Reiniciar ahora» restarts into
 * the update, a click on the notification itself shows the window, and «Más tarde» leaves
 * it for the tray and the window's chip.
 */

const { UPDATE_COPY } = require('./copy');

function updateReadyNotification({ version, platform, onRestart, onShowWindow }) {
  return {
    key: 'update',
    kind: 'update',
    title: UPDATE_COPY.notifyTitle,
    body: UPDATE_COPY.notifyBody(version),
    actions: [
      { id: 'restart', label: platform === 'darwin' ? UPDATE_COPY.restartShort : UPDATE_COPY.restartNow },
      { id: 'later', label: UPDATE_COPY.later },
    ],
    sticky: false,
    onAction(action) {
      if (action === 'restart') onRestart();
      else if (action === 'open') onShowWindow();
    },
  };
}

module.exports = { updateReadyNotification };
