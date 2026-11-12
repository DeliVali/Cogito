'use strict';

/**
 * electron-updater (6.8.x) for the NSIS install on Windows and the AppImage on Linux, held
 * to the same standard as COGITO's own macOS updater.
 *
 * electron-updater finds the release (`releases.atom`, `latest*.yml`), downloads it in the
 * background and checks the file against `latest*.yml`. That file is unsigned, and 6.8.x
 * has no signature check of its own (Ed25519 manifests exist only in the 7.0 alpha,
 * electron-builder PR #9877). So no install is allowed until the downloaded file also
 * matches the SHA-512 and size in COGITO's Ed25519-signed `cogito-update.json` for exactly
 * that version, and that version is strictly newer. To keep it that way:
 *
 * - `autoInstallOnAppQuit = false`: electron-updater never installs on its own. "Al salir"
 *   is COGITO calling `quitAndInstall(true, false)` from its own quit handler.
 * - `allowDowngrade = false`, `allowPrerelease = false`.
 *
 * electron-updater follows redirects itself, with no host check (its `ElectronHttpExecutor`,
 * read at tag electron-updater@6.8.10), through the `electron-updater` session partition.
 * `installHostFilter` closes that gap with a `webRequest` filter on that session.
 */

const { UpdateError } = require('./errors');
const { checkUrl, hashFile } = require('./fetch');
const { fileFor, parseVersion, compareVersions } = require('./manifest');

/** The partition electron-updater 6.x uses for every request (`NET_SESSION_NAME`). */
const ELECTRON_UPDATER_PARTITION = 'electron-updater';

function installHostFilter(electronSession, policy) {
  electronSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    let allowed = false;
    try {
      checkUrl(details.url, policy);
      allowed = true;
    } catch {
      allowed = false;
    }
    callback({ cancel: !allowed });
  });
}

function createElectronUpdaterBackend({ autoUpdater, feed, currentVersion, platform, arch, log = () => {} }) {
  let onEvent = () => {};
  let announced = null; // {version, release: Promise<{manifest, notes} | null>}
  let downloading = false;
  let verified = null; // the version whose downloaded file matched the signed manifest

  function fail(error) {
    downloading = false;
    verified = null;
    const reason = error instanceof UpdateError ? error.code : 'DOWNLOAD_FAILED';
    log(`update failed: ${error.message}`);
    onEvent({ type: 'failed', reason });
  }

  async function verifyDownload({ version, downloadedFile }) {
    if (!announced || announced.version !== version) {
      throw new UpdateError('VERSION_MISMATCH', `downloaded ${version}, announced ${announced?.version}`);
    }
    const release = await announced.release;
    if (!release) throw new UpdateError('NOT_NEWER', `${version} is not newer than ${currentVersion}`);
    const file = fileFor(release.manifest, platform, arch);
    if (!file) throw new UpdateError('NO_ARTIFACT', `release ${version} has no ${platform}/${arch} file`);
    const actual = await hashFile(downloadedFile);
    if (actual.size !== file.size || actual.sha512 !== file.sha512) {
      throw new UpdateError('HASH_MISMATCH', `${downloadedFile} is not the file the signed manifest names`);
    }
    return release;
  }

  function start(handler) {
    onEvent = handler;
    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = false;
    autoUpdater.allowDowngrade = false;
    autoUpdater.allowPrerelease = false;
    if ('disableWebInstaller' in autoUpdater) autoUpdater.disableWebInstaller = true;

    autoUpdater.on('update-available', (info) => {
      const version = info?.version;
      if (!parseVersion(version) || compareVersions(version, currentVersion) <= 0) {
        log(`ignoring update-available for ${version}`);
        return;
      }
      verified = null;
      downloading = true;
      const release = feed.release(version, { currentVersion });
      release.catch(() => {}); // surfaced when the download finishes, not as an unhandled rejection
      announced = { version, release };
      onEvent({ type: 'download-started', version });
    });

    autoUpdater.on('download-progress', (progress) => {
      if (downloading && Number.isFinite(progress?.percent)) onEvent({ type: 'download-progress', percent: progress.percent });
    });

    autoUpdater.on('update-downloaded', (event) => {
      verifyDownload(event).then(
        ({ notes }) => {
          downloading = false;
          verified = event.version;
          onEvent({
            type: 'ready',
            version: event.version,
            security: notes.security,
            notes: { tools: notes.tools, improvements: notes.improvements },
          });
        },
        fail,
      );
    });

    autoUpdater.on('error', (error) => {
      // A check that could not reach GitHub stays silent. A broken download does not.
      if (downloading) fail(error);
      else log(`update check failed: ${error?.message}`);
    });
  }

  return {
    start,

    async check() {
      try {
        await autoUpdater.checkForUpdates();
      } catch (error) {
        log(`update check failed: ${error.message}`);
      }
    },

    /**
     * 'restart': install silently and relaunch. 'quit': install silently, stay closed.
     * Synchronous, so it can run inside `before-quit`.
     */
    installSync(mode) {
      if (!verified) throw new UpdateError('NOT_PREPARED', 'no downloaded update has matched the signed manifest');
      autoUpdater.quitAndInstall(true, mode === 'restart');
    },

    async install(mode) {
      return this.installSync(mode);
    },
  };
}

module.exports = { ELECTRON_UPDATER_PARTITION, installHostFilter, createElectronUpdaterBackend };
