'use strict';

/**
 * Who owns this installation. That decides whether COGITO may replace itself.
 *
 * - `self`: an install COGITO put there itself and may overwrite. That is an AppImage (this
 *   process runs from the `APPDIR` the AppImage runtime mounted, see `runningFromAppImage`),
 *   the NSIS install on Windows, or a macOS `.app` bundle. The first two go through
 *   electron-updater. The bundle goes through COGITO's own macOS updater, because
 *   Squirrel.Mac requires an Apple-signed app.
 * - `package-manager`: the executable belongs to the `cogito` package, checked read-only
 *   with `dpkg -S`. dpkg owns those files, and replacing them behind its back would leave
 *   its database wrong, so this install only gets the "update with your package manager"
 *   card.
 * - `none`: a development run, or a layout none of the above describes (an unpacked
 *   tarball, say). No updater runs.
 *
 * A macOS bundle launched from App Translocation's randomized read-only path (quarantined,
 * opened outside /Applications) is `self` but `blocked: 'translocated'`. It cannot write
 * its own bundle, so it says so instead of failing halfway.
 */

const fs = require('node:fs');
const path = require('node:path');

const PACKAGE_NAME = 'cogito';
const DPKG_TIMEOUT_MS = 5000;

function none(reason) {
  return { install: 'none', backend: null, packageManager: null, blocked: null, reason };
}

function selfInstall(backend, extra = {}) {
  return { install: 'self', backend, packageManager: null, blocked: null, reason: '', ...extra };
}

/** The `.app` bundle containing a macOS executable, or null. */
function macBundle(execPath) {
  const match = /^(.*?\.app)\/(Contents\/MacOS\/[^/]+)$/.exec(execPath);
  return match ? { bundlePath: match[1], executableRelative: match[2] } : null;
}

async function ownedByCogito(execPath, execFile) {
  let stdout;
  try {
    ({ stdout } = await execFile('dpkg', ['-S', execPath], { timeout: DPKG_TIMEOUT_MS }));
  } catch (error) {
    return { owned: false, reason: error.code === 'ENOENT' ? 'dpkg is not installed' : `dpkg -S: ${error.message}` };
  }
  for (const line of String(stdout).split('\n')) {
    const colon = line.indexOf(': ');
    if (colon === -1) continue;
    const owners = line.slice(0, colon).split(',').map((name) => name.trim());
    if (owners.includes(PACKAGE_NAME)) return { owned: true, reason: '' };
  }
  return { owned: false, reason: `not owned by the ${PACKAGE_NAME} package` };
}

/**
 * Whether this process was started from the AppImage that `APPIMAGE` names. The AppImage
 * runtime exports `APPIMAGE` and `APPDIR` (its mount point) to the program it starts, and
 * every child inherits them, so a `.deb` COGITO launched from an AppImage'd terminal or
 * file manager sees them too. Only a process whose executable is inside `APPDIR`, with the
 * AppImage file present, is that AppImage.
 */
function runningFromAppImage(env, execPath, exists) {
  const image = env.APPIMAGE;
  const mount = env.APPDIR;
  if (!image || !mount || !path.isAbsolute(image) || !path.isAbsolute(mount)) return false;
  const inside = path.relative(path.resolve(mount), path.resolve(execPath));
  if (!inside || inside.startsWith('..') || path.isAbsolute(inside)) return false;
  return exists(image);
}

/**
 * `{install, backend, packageManager, blocked, reason}` plus, on macOS, `bundlePath` and
 * `executableRelative`. `execFile(file, args, options)` resolves `{stdout}`. It is called
 * only on Linux, and only when the process is not an AppImage.
 */
async function detectInstallType({ platform, env, execPath, isPackaged, execFile, exists = fs.existsSync }) {
  if (!isPackaged) return none('not packaged (development run)');

  if (platform === 'linux') {
    if (runningFromAppImage(env, execPath, exists)) return selfInstall('electron-updater');
    const { owned, reason } = await ownedByCogito(execPath, execFile);
    if (owned) return { install: 'package-manager', backend: null, packageManager: 'apt', blocked: null, reason: '' };
    return none(`not an AppImage, and ${reason}`);
  }

  if (platform === 'win32') return selfInstall('electron-updater');

  if (platform === 'darwin') {
    const bundle = macBundle(execPath);
    if (!bundle) return none('the executable is not inside an .app bundle');
    const blocked = bundle.bundlePath.includes('/AppTranslocation/') ? 'translocated' : null;
    return selfInstall('mac', { ...bundle, blocked });
  }

  return none(`no updater for ${platform}`);
}

module.exports = { detectInstallType, runningFromAppImage, PACKAGE_NAME };
