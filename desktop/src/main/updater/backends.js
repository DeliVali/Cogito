'use strict';

/**
 * The controller drives every backend through one shape:
 * `{start(onEvent), check(), installSync(mode), cleanup?()}`. These adapters give that shape
 * to the macOS updater and to the package-manager check. electron-updater's backend already
 * has it.
 */

const { UpdateError } = require('./errors');

/** COGITO's own macOS updater. A failure after `download-started` becomes `failed`. */
function macBackend(mac, log = () => {}) {
  let onEvent = () => {};
  return {
    start(handler) {
      onEvent = handler;
    },
    async check() {
      try {
        await mac.check(onEvent);
      } catch (error) {
        log(`macOS update failed: ${error.message}`);
        onEvent({ type: 'failed', reason: error.code || 'CHECK_FAILED' });
      }
    },
    installSync: (mode) => mac.installSync(mode),
    cleanup: () => mac.cleanup(),
  };
}

/**
 * For a package-manager install. It only checks whether a newer signed release exists, for
 * the card. It never downloads or installs.
 */
function packageManagerBackend(feed, currentVersion, log = () => {}) {
  let onEvent = () => {};
  return {
    start(handler) {
      onEvent = handler;
    },
    async check() {
      try {
        const release = await feed.latest({ currentVersion });
        if (!release) return;
        const { tools, improvements } = release.notes;
        onEvent({ type: 'available', version: release.manifest.version, notes: { tools, improvements } });
      } catch (error) {
        log(`update check failed: ${error.message}`);
      }
    },
    installSync() {
      throw new UpdateError('NOT_SELF', 'a package-manager install is never replaced by COGITO');
    },
  };
}

module.exports = { macBackend, packageManagerBackend };
