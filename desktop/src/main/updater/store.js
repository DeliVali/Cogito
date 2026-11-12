'use strict';

/**
 * What the updater remembers across a restart, in `<userData>/updater/state.json`:
 *
 * - `pending {from, to, notes}`: written just before quitting to install. At the next
 *   start the outcome comes from the version actually running (`deriveOutcome`), so a
 *   failed install that rolled back needs no help from the code that failed. Still running
 *   `from` means "kept", running `to` means "updated". Cleared once read.
 * - `notifiedVersion`: the version «Actualización lista» was last sent for.
 * - `cardShownVersion`: the version the package-manager card last opened by itself for.
 *
 * Writes go to a temporary file renamed over the old one, so a crash mid-write leaves the
 * previous state, never half of one. A file that does not parse, or holds values of the
 * wrong shape, reads as defaults. This file is a convenience, never an authority.
 */

const fs = require('node:fs');
const path = require('node:path');

const { parseVersion } = require('./manifest');

const FILE = 'state.json';
const DEFAULTS = Object.freeze({ pending: null, notifiedVersion: null, cardShownVersion: null });

function versionOrNull(value) {
  return parseVersion(value) ? value : null;
}

function stringList(value) {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'string') : [];
}

function sanitizePending(value) {
  if (!value || typeof value !== 'object') return null;
  const from = versionOrNull(value.from);
  const to = versionOrNull(value.to);
  if (!from || !to) return null;
  const notes = value.notes && typeof value.notes === 'object' ? value.notes : {};
  return { from, to, notes: { tools: stringList(notes.tools), improvements: stringList(notes.improvements) } };
}

class UpdateStore {
  constructor(dir) {
    this.dir = dir;
    this.file = path.join(dir, FILE);
  }

  read() {
    let raw;
    try {
      raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    } catch {
      return { ...DEFAULTS };
    }
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { ...DEFAULTS };
    return {
      pending: sanitizePending(raw.pending),
      notifiedVersion: versionOrNull(raw.notifiedVersion),
      cardShownVersion: versionOrNull(raw.cardShownVersion),
    };
  }

  /** Merge `patch` into the stored state and write it atomically. Returns the new state. */
  write(patch) {
    const next = { ...this.read(), ...patch };
    fs.mkdirSync(this.dir, { recursive: true });
    const temporary = `${this.file}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(next, null, 2)}\n`, { mode: 0o600 });
    fs.renameSync(temporary, this.file);
    return next;
  }
}

/**
 * `{kind: 'updated', version, notes}`, `{kind: 'kept', version, attempted}`, or null when
 * there was no pending install or the running version is neither side of it.
 */
function deriveOutcome(pending, currentVersion) {
  if (!pending) return null;
  if (currentVersion === pending.to) return { kind: 'updated', version: pending.to, notes: pending.notes };
  if (currentVersion === pending.from) return { kind: 'kept', version: pending.from, attempted: pending.to };
  return null;
}

module.exports = { UpdateStore, deriveOutcome };
