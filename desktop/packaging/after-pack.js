'use strict';

/**
 * electron-builder `afterPack` hook: refuse a package without its Python core, and
 * ad-hoc codesign the macOS bundle.
 *
 * The core is an `extraResources` entry (electron-builder.yml). electron-builder skips a
 * missing source with only a warning (26.15.3 `copyFiles`), so without this check an
 * installer with no core would ship.
 *
 * COGITO has no Apple Developer ID yet, so `mac.identity` is null and electron-builder
 * skips signing (25.x macPackager.js; 26.x via MacTargetHelper.handleNullIdentity).
 * afterPack still runs before any signing step. Apple Silicon kills an unsigned bundle as
 * "damaged", because the kernel needs a signature to run arm64 code. An ad-hoc signature
 * (`--sign -`) is enough to launch after the one-time "Open Anyway". This runs before the
 * dmg and zip are made, so both carry the signed bundle. Hardened runtime stays off: with
 * an ad-hoc signature its library validation refuses Electron's frameworks.
 *
 * `codesign --deep` does not sign code under Contents/Resources; it only seals those files
 * as data. So every Mach-O file of the core is signed first, deepest path first and the
 * interpreter last, and then the bundle.
 */

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const { bundledInterpreterSegments } = require('../src/main/core');

/** The first four bytes of a Mach-O file, thin (32 and 64 bit, either byte order) or fat. */
const MACHO_MAGICS = new Set(['feedface', 'feedfacf', 'cefaedfe', 'cffaedfe', 'cafebabe']);
/** Files per codesign call, well under any argument-length limit. */
const BATCH = 64;

function resourcesDir(context) {
  if (context.electronPlatformName === 'darwin') {
    return path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`, 'Contents', 'Resources');
  }
  return path.join(context.appOutDir, 'resources');
}

function isMachO(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const head = Buffer.alloc(4);
    return fs.readSync(fd, head, 0, 4, 0) === 4 && MACHO_MAGICS.has(head.toString('hex'));
  } finally {
    fs.closeSync(fd);
  }
}

/** Every Mach-O file under `root`, deepest first, the interpreter last. */
function machOFiles(root, interpreter) {
  const found = [];
  const visit = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) visit(full);
      else if (entry.isFile() && isMachO(full)) found.push(full);
    }
  };
  visit(root);
  const depth = (file) => file.split(path.sep).length;
  found.sort((a, b) => depth(b) - depth(a) || (a < b ? -1 : a > b ? 1 : 0));
  const last = found.indexOf(interpreter);
  if (last !== -1) found.push(...found.splice(last, 1));
  return found;
}

function inBatches(files, run) {
  for (let i = 0; i < files.length; i += BATCH) run(files.slice(i, i + BATCH));
}

exports.default = async function afterPack(context) {
  const platform = context.electronPlatformName;
  const resources = resourcesDir(context);
  // The build host's path rules: electron-builder can pack one platform on another.
  const interpreter = path.join(resources, ...bundledInterpreterSegments(platform));
  if (!fs.existsSync(interpreter)) {
    throw new Error(
      `no bundled core at ${interpreter}; run desktop/packaging/bundle-core.py for this platform ` +
        'and architecture first',
    );
  }
  if (platform !== 'darwin') return;
  const bundle = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const core = machOFiles(path.join(resources, 'core'), interpreter);
  inBatches(core, (files) => execFileSync('codesign', ['--force', '--sign', '-', ...files], { stdio: 'inherit' }));
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', bundle], { stdio: 'inherit' });
  execFileSync('codesign', ['--verify', '--deep', '--strict', bundle], { stdio: 'inherit' });
  inBatches(core, (files) => execFileSync('codesign', ['--verify', '--strict', ...files], { stdio: 'inherit' }));
};

exports.machOFiles = machOFiles;
