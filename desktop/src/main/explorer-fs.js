'use strict';

/**
 * The file explorer's side of the disk (design 1a «Explorador y visor», 1p): listing the
 * project's folders, git marks, reading a file for the read-only viewer, and the person's own
 * file management (new file or folder, rename, duplicate, move to the trash with «Deshacer»,
 * and files dropped onto the explorer copied into the project).
 *
 * Every path the window names is relative to the open project and is resolved here, never
 * trusted: it must stay inside the project after symlinks are followed, and it may not name
 * the project's own `.cogito/` (the session's store, which the core also keeps the agent out
 * of) or `.git/`. Neither is ever listed. Nothing here needs Electron, so it is tested alone;
 * `explorer-ipc.js` wires it to the window and to the system trash.
 *
 * The viewer never writes: there is no "save" here. Changing a file's content is the model's
 * job, or the person's own editor's.
 */

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { execFile } = require('node:child_process');

/** Never listed and never touched. */
const HIDDEN = new Set(['.git', '.cogito']);
/** Entries read from one folder; the rest are counted, not listed. */
const MAX_ENTRIES = 2000;
/** Above this, or binary, a file has no preview (1p «Sin vista previa»). */
const MAX_PREVIEW_BYTES = 5 * 1024 * 1024;
/** Bytes read to tell text from binary. */
const SNIFF_BYTES = 8192;

const IMAGE_TYPES = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
  webp: 'image/webp', bmp: 'image/bmp', ico: 'image/x-icon', svg: 'image/svg+xml',
};

class ExplorerError extends Error {
  /** `code` is what the window shows a reason for: `outside`, `hidden`, `missing`,
   * `exists`, `invalid-name`, `root`. */
  constructor(code, message) {
    super(message || code);
    this.code = code;
  }
}

/** The project-relative path `rel` as an absolute path inside `root`, after symlinks, or
 * an ExplorerError. `''` is the root itself. `mustExist` false allows a path whose last part
 * does not exist yet (a new file, a rename target): its parent must. */
function resolveInside(root, rel, { mustExist = true } = {}) {
  if (typeof rel !== 'string' || rel.includes('\0')) throw new ExplorerError('invalid-name');
  const realRoot = fs.realpathSync(root);
  const parts = rel.split(/[\\/]+/).filter(Boolean);
  if (parts.some((part) => part === '..')) throw new ExplorerError('outside');
  if (parts.length && HIDDEN.has(parts[0])) throw new ExplorerError('hidden');
  const joined = path.join(realRoot, ...parts);
  let real;
  try {
    real = fs.realpathSync(joined);
  } catch (error) {
    if (mustExist || error.code !== 'ENOENT') throw new ExplorerError('missing');
    const parent = fs.realpathSync(path.dirname(joined));
    real = path.join(parent, path.basename(joined));
  }
  const inside = real === realRoot || real.startsWith(realRoot + path.sep);
  if (!inside) throw new ExplorerError('outside');
  const relReal = path.relative(realRoot, real);
  const first = relReal.split(path.sep)[0];
  if (HIDDEN.has(first)) throw new ExplorerError('hidden');
  return real;
}

/** A name for a new or renamed entry: one path part, not hidden, nothing a file system
 * refuses everywhere. */
function checkName(name) {
  if (typeof name !== 'string') throw new ExplorerError('invalid-name');
  const trimmed = name.trim();
  if (!trimmed || trimmed === '.' || trimmed === '..' || /[\\/\0]/.test(trimmed)) {
    throw new ExplorerError('invalid-name');
  }
  if (HIDDEN.has(trimmed)) throw new ExplorerError('hidden');
  return trimmed;
}

/** `{entries: [{name, path, dir, size?}], more}` for the folder `rel`, folders first, then
 * files, each by name the way the system's file browser sorts it. */
async function listDir(root, rel = '') {
  const dir = resolveInside(root, rel);
  const realRoot = fs.realpathSync(root);
  const dirents = await fsp.readdir(dir, { withFileTypes: true });
  const visible = dirents.filter((entry) => !(dir === realRoot && HIDDEN.has(entry.name)));
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
  const entries = [];
  for (const entry of visible.slice(0, MAX_ENTRIES)) {
    const full = path.join(dir, entry.name);
    let isDir = entry.isDirectory();
    let size;
    if (entry.isSymbolicLink() || !isDir) {
      try {
        const stat = await fsp.stat(full);
        isDir = stat.isDirectory();
        if (!isDir) size = stat.size;
      } catch {
        continue; // a broken link: nothing to open
      }
    }
    entries.push({
      name: entry.name,
      path: path.relative(realRoot, full).split(path.sep).join('/'),
      dir: isDir,
      ...(size === undefined ? {} : { size }),
    });
  }
  entries.sort((a, b) => (a.dir === b.dir ? collator.compare(a.name, b.name) : a.dir ? -1 : 1));
  return { entries, more: Math.max(0, visible.length - MAX_ENTRIES) };
}

/** Git's marks for the project, as `{path: 'M' | 'U' | 'D'}` with project-relative paths:
 * M changed (staged or not), U new (untracked or added), D deleted. `{}` when the project is
 * not in a git work tree or git is not installed. */
function gitMarks(root, { run = execFileText } = {}) {
  return run('git', ['rev-parse', '--show-toplevel'], root)
    .then(async (top) => {
      const out = await run(
        'git',
        ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
        root,
      );
      const realRoot = fs.realpathSync(root);
      const realTop = fs.realpathSync(top.trim());
      const marks = {};
      const records = out.split('\0');
      for (let index = 0; index < records.length; index += 1) {
        const record = records[index];
        if (record.length < 4) continue;
        const x = record[0];
        const y = record[1];
        const file = record.slice(3);
        // A rename's record is followed by its old path, which is not a mark of its own.
        if (x === 'R' || x === 'C') index += 1;
        const abs = path.join(realTop, file);
        const rel = path.relative(realRoot, abs);
        if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) continue;
        const key = rel.split(path.sep).join('/');
        if (HIDDEN.has(key.split('/')[0])) continue;
        let mark = 'M';
        if (x === '?' || x === 'A' || x === 'R' || x === 'C') mark = 'U';
        if (x === 'D' || y === 'D') mark = 'D';
        marks[key] = mark;
      }
      return marks;
    })
    .catch(() => ({}));
}

function execFileText(command, args, cwd) {
  return new Promise((resolve, reject) => {
    execFile(command, args, { cwd, timeout: 5000, maxBuffer: 16 * 1024 * 1024 }, (error, stdout) => {
      if (error) reject(error);
      else resolve(stdout);
    });
  });
}

/** The extension as the viewer's table names it: `.env` and `.env.*` are `env`. */
function kindOf(name) {
  const lower = name.toLowerCase();
  if (lower === '.env' || lower.startsWith('.env.')) return 'env';
  const ext = lower.includes('.') ? lower.slice(lower.lastIndexOf('.') + 1) : '';
  if (ext === 'json' || ext === 'jsonc') return 'json';
  if (ext === 'md' || ext === 'mdx') return 'markdown';
  if (ext === 'csv') return 'csv';
  if (ext === 'tsv') return 'tsv';
  if (ext === 'pdf') return 'pdf';
  if (IMAGE_TYPES[ext]) return 'image';
  return 'text';
}

function isBinary(buffer) {
  return buffer.includes(0);
}

/** What the viewer shows for `rel`: `{kind, name, path, size, ...}`. Text kinds carry
 * `text`, `lines`, `eol` ('LF' | 'CRLF') and `encoding` ('UTF-8'); `image` carries a
 * `data:` URL; `pdf` carries `abs` for the window's own reader; anything too large, binary
 * or not valid UTF-8 is `unsupported` with its size. */
async function readForView(root, rel) {
  const abs = resolveInside(root, rel);
  const stat = await fsp.stat(abs);
  if (stat.isDirectory()) throw new ExplorerError('invalid-name', 'is a folder');
  const name = path.basename(abs);
  const base = { name, path: rel, size: stat.size, modified: stat.mtimeMs };
  const kind = kindOf(name);
  if (stat.size > MAX_PREVIEW_BYTES) return { ...base, kind: 'unsupported', reason: 'size' };
  if (kind === 'pdf') return { ...base, kind: 'pdf', abs };
  const buffer = await fsp.readFile(abs);
  if (kind === 'image') {
    const ext = name.slice(name.lastIndexOf('.') + 1).toLowerCase();
    return { ...base, kind, data: `data:${IMAGE_TYPES[ext]};base64,${buffer.toString('base64')}` };
  }
  if (isBinary(buffer.subarray(0, SNIFF_BYTES))) return { ...base, kind: 'unsupported', reason: 'binary' };
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(buffer);
  } catch {
    return { ...base, kind: 'unsupported', reason: 'encoding' };
  }
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  const eol = /\r\n/.test(text) ? 'CRLF' : 'LF';
  const lines = text === '' ? 0 : text.split(/\r?\n/).length - (text.endsWith('\n') ? 1 : 0);
  if (kind === 'env') {
    // The values stay here until the person asks for them (`revealValues`): the window gets
    // the names, each value as dots.
    const { masked, variables } = maskEnv(text);
    return { ...base, kind, text: masked, variables, masked: true, lines, eol, encoding: 'UTF-8' };
  }
  return { ...base, kind, text, lines, eol, encoding: 'UTF-8' };
}

const ENV_LINE = /^(\s*(?:export\s+)?[A-Za-z_][\w.-]*\s*=\s*)(.*)$/;

/** `.env` text with every value replaced by dots (at most 16), and how many variables it
 * sets. Comments and blank lines are kept as they are. */
function maskEnv(text) {
  let variables = 0;
  const masked = text
    .split(/(\r?\n)/)
    .map((part) => {
      const match = ENV_LINE.exec(part);
      if (!match || part.trimStart().startsWith('#')) return part;
      variables += 1;
      return match[1] + (match[2] ? '•'.repeat(Math.min(16, Math.max(4, match[2].length))) : '');
    })
    .join('');
  return { masked, variables };
}

/** The real text of a `.env` file, only when the person pressed «Mostrar valores». */
async function revealValues(root, rel) {
  const abs = resolveInside(root, rel);
  if (kindOf(path.basename(abs)) !== 'env') throw new ExplorerError('invalid-name', 'not a .env file');
  const stat = await fsp.stat(abs);
  if (stat.size > MAX_PREVIEW_BYTES) throw new ExplorerError('invalid-name', 'too large');
  const text = await fsp.readFile(abs, 'utf8');
  return { text: text.charCodeAt(0) === 0xfeff ? text.slice(1) : text };
}

/** `{nombre} copia`, then `{nombre} copia 2`, ... before the extension, for a name that is
 * free in `dir`. A dotfile or a folder keeps its whole name as the stem. */
function freeCopyName(dir, name, exists = (candidate) => fs.existsSync(path.join(dir, candidate))) {
  const dot = name.lastIndexOf('.');
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : '';
  for (let n = 1; n < 10000; n += 1) {
    const candidate = `${stem} copia${n === 1 ? '' : ` ${n}`}${ext}`;
    if (!exists(candidate)) return candidate;
  }
  throw new ExplorerError('exists');
}

function relOf(root, abs) {
  return path.relative(fs.realpathSync(root), abs).split(path.sep).join('/');
}

/** Creates an empty file `name` in folder `dirRel`; refuses one that exists. */
async function newFile(root, dirRel, name) {
  const dir = resolveInside(root, dirRel);
  const target = resolveInside(root, path.join(relOf(root, dir), checkName(name)), { mustExist: false });
  const handle = await fsp.open(target, 'wx').catch((error) => {
    throw new ExplorerError(error.code === 'EEXIST' ? 'exists' : 'missing');
  });
  await handle.close();
  return { path: relOf(root, target) };
}

async function newFolder(root, dirRel, name) {
  const dir = resolveInside(root, dirRel);
  const target = resolveInside(root, path.join(relOf(root, dir), checkName(name)), { mustExist: false });
  await fsp.mkdir(target).catch((error) => {
    throw new ExplorerError(error.code === 'EEXIST' ? 'exists' : 'missing');
  });
  return { path: relOf(root, target) };
}

/** Renames `rel` in place (same folder); refuses a name that is taken. */
async function rename(root, rel, name) {
  const from = resolveInside(root, rel);
  if (from === fs.realpathSync(root)) throw new ExplorerError('root');
  const to = resolveInside(root, path.join(path.dirname(relOf(root, from)), checkName(name)), { mustExist: false });
  if (to === from) return { path: relOf(root, to) };
  if (fs.existsSync(to) && to.toLowerCase() !== from.toLowerCase()) throw new ExplorerError('exists');
  await fsp.rename(from, to);
  return { path: relOf(root, to) };
}

/** Moves `rel` into folder `dirRel`, keeping its name. Refuses a name already there, the
 * project's root, and a folder into itself or one of its own folders. Moving to the folder
 * it is already in changes nothing. */
async function move(root, rel, dirRel) {
  const from = resolveInside(root, rel);
  const realRoot = fs.realpathSync(root);
  if (from === realRoot) throw new ExplorerError('root');
  const dir = resolveInside(root, dirRel);
  if (!fs.statSync(dir).isDirectory()) throw new ExplorerError('invalid-name', 'not a folder');
  if (dir === from || dir.startsWith(from + path.sep)) throw new ExplorerError('into-itself');
  if (path.dirname(from) === dir) return { path: relOf(root, from), moved: false };
  const to = path.join(dir, path.basename(from));
  if (fs.existsSync(to)) throw new ExplorerError('exists');
  await fsp.rename(from, to).catch(async (error) => {
    if (error.code !== 'EXDEV') throw error;
    // Another file system under the project (a mount): copy, then remove the original.
    await fsp.cp(from, to, { recursive: true, errorOnExist: true, force: false });
    await fsp.rm(from, { recursive: true });
  });
  return { path: relOf(root, to), moved: true };
}

/** Copies `rel` beside itself as `{nombre} copia`. */
async function duplicate(root, rel) {
  const from = resolveInside(root, rel);
  if (from === fs.realpathSync(root)) throw new ExplorerError('root');
  const dir = path.dirname(from);
  const to = path.join(dir, freeCopyName(dir, path.basename(from)));
  await fsp.cp(from, to, { recursive: true, errorOnExist: true, force: false });
  return { path: relOf(root, to) };
}

/** Copies files dropped onto the explorer (absolute paths from anywhere) into folder
 * `dirRel`. A name already there gets `{nombre} copia`. Returns the new paths. */
async function copyInto(root, dirRel, sources) {
  const dir = resolveInside(root, dirRel);
  const created = [];
  for (const source of sources) {
    if (typeof source !== 'string' || !path.isAbsolute(source)) continue;
    const name = path.basename(source);
    if (HIDDEN.has(name)) continue;
    const realSource = fs.realpathSync(source);
    // Dropping a folder into itself would copy forever.
    if (dir === realSource || dir.startsWith(realSource + path.sep)) continue;
    const taken = fs.existsSync(path.join(dir, name));
    const to = path.join(dir, taken ? freeCopyName(dir, name) : name);
    await fsp.cp(realSource, to, { recursive: true, errorOnExist: true, force: false });
    created.push(relOf(root, to));
  }
  return { paths: created };
}

/** The folder a deletion waits in until its «Deshacer» time is over. Inside the project's
 * own `.cogito/` so the move is a rename on the same file system, and the explorer never
 * lists it. */
function pendingDir(root) {
  return path.join(fs.realpathSync(root), '.cogito', 'explorer-trash');
}

/** Moves `rel` out of the project into the pending folder: `{id, name, path}`. Nothing is
 * deleted here; `explorer-ipc.js` sends it to the system trash when the undo time ends. */
async function stageDelete(root, rel, id) {
  const from = resolveInside(root, rel);
  if (from === fs.realpathSync(root)) throw new ExplorerError('root');
  const holder = path.join(pendingDir(root), id);
  await fsp.mkdir(holder, { recursive: true });
  const staged = path.join(holder, path.basename(from));
  await fsp.rename(from, staged);
  return { id, name: path.basename(from), path: relOf(root, from), staged };
}

/** Puts a staged deletion back where it was. Refuses when something now has its name. */
async function restore(root, item) {
  const target = resolveInside(root, item.path, { mustExist: false });
  if (fs.existsSync(target)) throw new ExplorerError('exists');
  await fsp.rename(item.staged, target);
  await fsp.rm(path.dirname(item.staged), { recursive: true, force: true });
  return { path: item.path };
}

/** Every file under folder `rel` (hidden folders skipped), for choosing what to attach:
 * `{files: [{path, name, size, binary}], more}`. `binary` is read only for a file small
 * enough to attach. */
async function filesUnder(root, rel, { limit = 2000, attachLimit = 20 * 1024 * 1024 } = {}) {
  const start = resolveInside(root, rel);
  const realRoot = fs.realpathSync(root);
  const files = [];
  let more = 0;
  const stack = [start];
  while (stack.length) {
    const dir = stack.pop();
    let dirents;
    try {
      dirents = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of dirents) {
      if (dir === realRoot && HIDDEN.has(entry.name)) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
        continue;
      }
      if (!entry.isFile()) continue;
      if (files.length >= limit) {
        more += 1;
        continue;
      }
      const stat = await fsp.stat(full);
      let binary = false;
      if (stat.size <= attachLimit) {
        const handle = await fsp.open(full, 'r');
        try {
          const { buffer, bytesRead } = await handle.read(Buffer.alloc(SNIFF_BYTES), 0, SNIFF_BYTES, 0);
          binary = isBinary(buffer.subarray(0, bytesRead));
        } finally {
          await handle.close();
        }
      }
      files.push({
        path: path.relative(realRoot, full).split(path.sep).join('/'),
        name: entry.name,
        size: stat.size,
        binary,
      });
    }
  }
  files.sort((a, b) => a.path.localeCompare(b.path));
  return { files, more };
}

module.exports = {
  filesUnder,
  move,
  maskEnv,
  revealValues,
  ExplorerError,
  HIDDEN,
  MAX_PREVIEW_BYTES,
  resolveInside,
  checkName,
  listDir,
  gitMarks,
  kindOf,
  readForView,
  freeCopyName,
  newFile,
  newFolder,
  rename,
  duplicate,
  copyInto,
  pendingDir,
  stageDelete,
  restore,
};
