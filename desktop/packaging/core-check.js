'use strict';

/**
 * Release gate for the Python core inside a packaged app. Run by .github/workflows/
 * release.yml on each OS after electron-builder, before anything is uploaded:
 *
 *   node desktop/packaging/core-check.js --resources DIR --bundle FILE --version X.Y.Z
 *
 * DIR is the packaged app's resources folder (Contents/Resources on macOS) and FILE the
 * bundle.json packaging/bundle-core.py wrote for that target. It runs the app's own launch
 * code from src/main/core.js, so what passes here is what the app does:
 *
 * 1. the packaged tree is the bundle: the same files, exec bits and contents (Mach-O
 *    files excepted, since after-pack.js re-signs them);
 * 2. the interpreter starts (the app's probe) and the core reports this version (the
 *    update self-test's check);
 * 3. every extension module and every top-level package in site-packages imports;
 * 4. `serve` starts with a scratch home and no GPU, answers /health and the project list,
 *    and stops. It never asks for /models or /connect, which can reach a model server;
 * 5. nothing under core/ was created or changed by any of the above.
 *
 * Prints one summary line. Exit 0 when everything passed, 1 when anything failed, 2 on a
 * usage error.
 */

const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { Core, bundledCoreReports, probeInterpreter, resolveCoreLaunch } = require('../src/main/core');

const MACHO_MAGICS = new Set(['feedface', 'feedfacf', 'cefaedfe', 'cffaedfe', 'cafebabe']);
const SERVE_TIMEOUT_MS = 120_000;
const STOP_TIMEOUT_MS = 20_000;

/** Imports every extension module and every top-level module in site-packages; prints
 * {"modules": n, "failed": [...]} as its last line. The pruned Tk set and the test
 * modules are left out. */
const SWEEP = String.raw`
import importlib, json, pkgutil, sys, sysconfig, pathlib
skip = {"antigravity", "this", "idlelib", "tkinter", "turtle", "turtledemo", "lib2to3", "ensurepip", "pydoc_data", "_tkinter"}
stdlib = pathlib.Path(sysconfig.get_path("stdlib"))
native = stdlib / "lib-dynload" if (stdlib / "lib-dynload").is_dir() else pathlib.Path(sys.base_prefix) / "DLLs"
names = {m.name for m in pkgutil.iter_modules([str(native)])}
names |= {m.name for m in pkgutil.iter_modules([sysconfig.get_path("purelib")])}
failed = []
for name in sorted(names):
    if name in skip or name.startswith("_test") or name == "_ctypes_test":
        continue
    try:
        importlib.import_module(name)
    except BaseException as exc:
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
print(json.dumps({"modules": len(names), "failed": failed}))
`;

function usage(message) {
  process.stderr.write(`core-check: ${message}\nusage: node core-check.js --resources DIR --bundle FILE --version X.Y.Z\n`);
  process.exit(2);
}

function parseArgs(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!['--resources', '--bundle', '--version'].includes(key) || value === undefined) usage(`unexpected argument ${key}`);
    options[key.slice(2)] = value;
  }
  for (const key of ['resources', 'bundle', 'version']) if (!options[key]) usage(`--${key} is required`);
  return options;
}

/** Every file under `root` as {relative posix path: absolute path}. Symlinks are listed
 * too: the bundle has none, so one here is a difference. */
function listFiles(root) {
  const found = new Map();
  const visit = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) visit(full);
      else found.set(path.relative(root, full).split(path.sep).join('/'), full);
    }
  };
  visit(root);
  return found;
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function isMachO(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const head = Buffer.alloc(4);
    fs.readSync(fd, head, 0, 4, 0);
    return MACHO_MAGICS.has(head.toString('hex'));
  } finally {
    fs.closeSync(fd);
  }
}

/** Step 1: the packaged tree against bundle.json. Returns the differences. */
function compareWithBundle(home, bundle) {
  const problems = [];
  const files = listFiles(home);
  const expected = bundle.files || {};
  const executables = new Set(bundle.executables || []);
  for (const rel of Object.keys(expected)) if (!files.has(rel)) problems.push(`missing: ${rel}`);
  for (const [rel, full] of files) {
    if (!(rel in expected)) {
      problems.push(`not in the bundle: ${rel}`);
      continue;
    }
    const stats = fs.lstatSync(full);
    if (stats.isSymbolicLink()) {
      problems.push(`a symlink: ${rel}`);
      continue;
    }
    // Windows has no exec bit to compare.
    if (process.platform !== 'win32' && Boolean(stats.mode & 0o111) !== executables.has(rel)) {
      problems.push(`exec bit changed: ${rel}`);
    }
    if (!isMachO(full) && sha256(full) !== expected[rel]) problems.push(`content changed: ${rel}`);
  }
  return problems;
}

/** Every path under `root` with its size and mtime. */
function snapshot(root) {
  const seen = new Map();
  const visit = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      const stats = fs.lstatSync(full);
      seen.set(path.relative(root, full), `${stats.size}:${stats.mtimeMs}`);
      if (entry.isDirectory()) visit(full);
    }
  };
  visit(root);
  return seen;
}

function changedPaths(before, after) {
  const changed = [];
  for (const [rel, state] of after) if (before.get(rel) !== state) changed.push(rel);
  for (const rel of before.keys()) if (!after.has(rel)) changed.push(rel);
  return changed.sort();
}

/** A scratch home, no GPU, no automatic model probe: the core then starts with an empty
 * catalog and serves no model. */
function isolate(scratch) {
  const env = {
    HOME: scratch,
    USERPROFILE: scratch,
    APPDATA: path.join(scratch, 'AppData', 'Roaming'),
    LOCALAPPDATA: path.join(scratch, 'AppData', 'Local'),
    XDG_CONFIG_HOME: path.join(scratch, '.config'),
    XDG_DATA_HOME: path.join(scratch, '.local', 'share'),
    XDG_CACHE_HOME: path.join(scratch, '.cache'),
    XDG_STATE_HOME: path.join(scratch, '.local', 'state'),
    CUDA_VISIBLE_DEVICES: '',
    COGITO_NO_AUTO_PROBE: '1',
  };
  for (const [key, value] of Object.entries(env)) process.env[key] = value;
  delete process.env.COGITO_CORE_ROOT;
}

function runSweep(python) {
  return new Promise((resolve, reject) => {
    execFile(python, ['-I', '-B', '-c', SWEEP], { timeout: 120_000, maxBuffer: 8 << 20, windowsHide: true }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(`the import sweep failed: ${error.message}\n${String(stderr).slice(-2000)}`));
        return;
      }
      const last = String(stdout).trim().split('\n').pop();
      try {
        resolve(JSON.parse(last));
      } catch {
        reject(new Error(`the import sweep printed no result: ${String(stdout).slice(-500)}`));
      }
    });
  });
}

function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${what} took more than ${ms / 1000} s`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

async function serveOnce(resources) {
  const core = new Core({ resourcesPath: resources });
  const started = Date.now();
  try {
    const { port } = await withTimeout(core.start({ isPackaged: true }), SERVE_TIMEOUT_MS, 'serve');
    const ready = (Date.now() - started) / 1000;
    if (port === 0 || port === 8080) throw new Error(`serve announced port ${port}`);
    const health = await fetch(`${core.address}/health`);
    const body = await health.json();
    if (health.status !== 200 || body.ok !== true) throw new Error(`/health answered ${health.status} ${JSON.stringify(body)}`);
    const projects = await core.listProjects();
    if (!Array.isArray(projects.projects) || projects.projects.length !== 0) {
      throw new Error(`/settings/projects answered ${JSON.stringify(projects)} with an empty home`);
    }
    return ready;
  } finally {
    if (core.process) {
      const child = core.process;
      const exited = new Promise((resolve) => core.once('exit', resolve));
      core.stop();
      try {
        await withTimeout(exited, STOP_TIMEOUT_MS, 'stopping the core');
      } catch (error) {
        try {
          process.kill(-child.pid, 'SIGKILL');
        } catch {
          child.kill('SIGKILL');
        }
        throw error;
      }
    }
  }
}

async function check({ resources, bundle: bundleFile, version }) {
  const bundle = JSON.parse(fs.readFileSync(bundleFile, 'utf8'));
  const home = path.join(resources, 'core', 'python');
  const coreRoot = path.join(resources, 'core');
  if (!fs.existsSync(home)) throw new Error(`no bundled core at ${home}`);

  const differences = compareWithBundle(home, bundle);
  if (differences.length) {
    throw new Error(`the packaged core is not the bundle (${differences.length}):\n  ${differences.slice(0, 40).join('\n  ')}`);
  }

  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'cogito-core-check-'));
  try {
    isolate(scratch);
    const before = snapshot(coreRoot);

    const { probe } = resolveCoreLaunch({ port: 0, isPackaged: true, env: {}, resourcesPath: resources });
    await probeInterpreter(probe);
    if (!bundledCoreReports(version, { resourcesPath: resources })) {
      throw new Error(`the bundled core does not report "cogito, version ${version}"`);
    }

    const sweep = await runSweep(probe.command);
    if (!(sweep.modules > 0) || sweep.failed.length) {
      throw new Error(`imports failed (${sweep.failed.length} of ${sweep.modules}):\n  ${sweep.failed.join('\n  ')}`);
    }

    const ready = await serveOnce(resources);

    const written = changedPaths(before, snapshot(coreRoot));
    if (written.length) throw new Error(`the core wrote into the app (${written.length}):\n  ${written.slice(0, 40).join('\n  ')}`);

    return `core-check: ok: ${bundle.target || 'core'} ${version}, ${Object.keys(bundle.files || {}).length} files, ` +
      `${sweep.modules} modules import, serve ready in ${ready.toFixed(2)} s, digest ${bundle.digest}`;
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

if (require.main === module) {
  check(parseArgs(process.argv.slice(2))).then(
    (summary) => {
      process.stdout.write(`${summary}\n`);
      process.exit(0);
    },
    (error) => {
      process.stderr.write(`core-check: FAILED: ${error.message}\n`);
      process.exit(1);
    },
  );
}

module.exports = { check, compareWithBundle, SWEEP };
