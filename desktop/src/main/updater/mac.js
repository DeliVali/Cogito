'use strict';

/**
 * COGITO's own macOS updater.
 *
 * Squirrel.Mac, which electron-updater drives on macOS, refuses unsigned apps, and COGITO
 * ships unsigned until there are users to justify an Apple Developer ID. So on macOS:
 *
 *   check(): gets the newest release from the feed (signature, fields, strictly newer,
 *              notes). Streams the zip for this arch and checks it against the signed
 *              SHA-512 and size before anything is extracted. Runs `ditto -x -k` into a
 *              staging folder next to the bundle (same volume, so the swap is two renames)
 *              and checks that the extracted Info.plist claims the signed version.
 *   install(mode): copies the swap helper out of the asar, spawns it detached and
 *              returns. The caller records the pending update and quits. The helper
 *              (`mac-swap.sh`) waits for this process to exit, swaps, self-tests the new
 *              bundle and rolls back on failure.
 *
 * A bundle running from App Translocation's read-only path, or from a folder this user
 * cannot write, fails visibly (BLOCKED / NOT_WRITABLE) before anything is downloaded.
 */

const fs = require('node:fs');
const path = require('node:path');
const { execFile, spawn } = require('node:child_process');
const { promisify } = require('node:util');

const { UpdateError } = require('./errors');
const { fileFor } = require('./manifest');
const { hashFile } = require('./fetch');

const STAGING_PREFIX = '.COGITO-update-';
const SELF_TEST_TIMEOUT_S = 60;

function dittoExtract(zipPath, destination) {
  return promisify(execFile)('/usr/bin/ditto', ['-x', '-k', zipPath, destination]);
}

function spawnDetached(command, args) {
  spawn(command, args, { detached: true, stdio: 'ignore' }).unref();
}

async function alreadyDownloaded(zipPath, file) {
  try {
    const { sha512, size } = await hashFile(zipPath);
    return sha512 === file.sha512 && size === file.size;
  } catch {
    return false;
  }
}

class MacUpdater {
  constructor({
    feed,
    currentVersion,
    arch,
    bundle,
    workRoot,
    helperSource,
    extract = dittoExtract,
    spawnHelper = spawnDetached,
    launcher = '/usr/bin/open',
    selfTestTimeoutS = SELF_TEST_TIMEOUT_S,
    pid = process.pid,
  }) {
    Object.assign(this, { feed, currentVersion, arch, bundle, workRoot, helperSource, extract, spawnHelper, launcher, selfTestTimeoutS, pid });
    this.parent = path.dirname(bundle.bundlePath);
    this.prepared = null;
  }

  /**
   * `{found: false}` when nothing is newer. Otherwise emits `download-started`, progress
   * and `ready` through `onEvent` and resolves `{found: true, version}`. Any failure after
   * `download-started` rejects with an UpdateError.
   */
  async check(onEvent) {
    const release = await this.feed.latest({ currentVersion: this.currentVersion });
    if (!release) return { found: false };
    const { manifest, notes } = release;
    const { version } = manifest;
    onEvent({ type: 'download-started', version });

    if (this.bundle.blocked) {
      throw new UpdateError('BLOCKED', `${this.bundle.bundlePath} runs from App Translocation; move it to /Applications`);
    }
    try {
      await fs.promises.access(this.parent, fs.constants.W_OK);
    } catch {
      throw new UpdateError('NOT_WRITABLE', `cannot write to ${this.parent}`);
    }
    const file = fileFor(manifest, 'darwin', this.arch);
    if (!file) throw new UpdateError('NO_ARTIFACT', `release ${version} has no macOS ${this.arch} zip`);

    const workdir = path.join(this.workRoot, version);
    await fs.promises.mkdir(workdir, { recursive: true });
    const zipPath = path.join(workdir, file.name);
    if (!(await alreadyDownloaded(zipPath, file))) {
      await this.feed.download(version, file, zipPath, {
        onProgress: (fraction) => onEvent({ type: 'download-progress', percent: fraction * 100 }),
      });
    } else {
      onEvent({ type: 'download-progress', percent: 100 });
    }

    const { staged, stagingDir } = await this.stage(zipPath, version);
    this.prepared = { version, staged, stagingDir, workdir };
    onEvent({
      type: 'ready',
      version,
      security: notes.security,
      notes: { tools: notes.tools, improvements: notes.improvements },
    });
    return { found: true, version };
  }

  /** Extract the verified zip beside the bundle and check what came out. */
  async stage(zipPath, version) {
    const stagingDir = path.join(this.parent, `${STAGING_PREFIX}${version}`);
    await fs.promises.rm(stagingDir, { recursive: true, force: true });
    await fs.promises.mkdir(stagingDir);
    try {
      await this.extract(zipPath, stagingDir);
      const apps = (await fs.promises.readdir(stagingDir)).filter((name) => name.endsWith('.app'));
      if (apps.length !== 1) throw new UpdateError('EXTRACT_INVALID', `expected one .app in the zip, found ${apps.length}`);
      const staged = path.join(stagingDir, apps[0]);
      const real = await fs.promises.realpath(staged);
      if (!real.startsWith(`${await fs.promises.realpath(stagingDir)}${path.sep}`)) {
        throw new UpdateError('EXTRACT_INVALID', 'the extracted bundle points outside its staging folder');
      }
      const plist = await fs.promises.readFile(path.join(staged, 'Contents', 'Info.plist'), 'utf8');
      const claimed = /<key>CFBundleShortVersionString<\/key>\s*<string>([^<]*)<\/string>/.exec(plist)?.[1];
      if (claimed !== version) throw new UpdateError('EXTRACT_INVALID', `the bundle claims ${claimed}, the release is ${version}`);
      await fs.promises.access(path.join(staged, this.bundle.executableRelative), fs.constants.X_OK);
      return { staged, stagingDir };
    } catch (error) {
      await fs.promises.rm(stagingDir, { recursive: true, force: true });
      if (error instanceof UpdateError) throw error;
      throw new UpdateError('EXTRACT_INVALID', error.message);
    }
  }

  /**
   * Spawn the swap helper for `mode` ('restart' | 'quit'). The caller then quits.
   * Synchronous on purpose: it also runs inside `before-quit`, which cannot wait.
   */
  installSync(mode) {
    const prepared = this.prepared;
    if (!prepared) throw new UpdateError('NOT_PREPARED', 'nothing has been downloaded and verified');
    if (!fs.existsSync(prepared.staged)) throw new UpdateError('NOT_PREPARED', `${prepared.staged} is gone`);
    // Copied out of the asar: /bin/sh cannot read a file inside app.asar, Electron's fs can.
    const helper = path.join(prepared.workdir, 'mac-swap.sh');
    fs.writeFileSync(helper, fs.readFileSync(this.helperSource), { mode: 0o700 });
    const backup = path.join(this.parent, `.COGITO-${this.currentVersion}-previous.app`);
    this.spawnHelper('/bin/sh', [
      helper,
      String(this.pid),
      this.bundle.bundlePath,
      prepared.staged,
      backup,
      this.bundle.executableRelative,
      prepared.workdir,
      mode,
      this.launcher,
      prepared.version,
      String(this.selfTestTimeoutS),
    ]);
  }

  async install(mode) {
    return this.installSync(mode);
  }

  /** After a restart: remove emptied staging folders and old downloads (only our own names). */
  cleanup() {
    try {
      for (const name of fs.readdirSync(this.parent)) {
        if (name.startsWith(STAGING_PREFIX)) fs.rmSync(path.join(this.parent, name), { recursive: true, force: true });
      }
      fs.rmSync(this.workRoot, { recursive: true, force: true });
    } catch {
      // Leftovers cost disk space, not correctness. The next update recreates both.
    }
  }
}

module.exports = { MacUpdater, STAGING_PREFIX };
