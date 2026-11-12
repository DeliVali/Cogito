'use strict';

/**
 * The signed statement a release makes about itself. It is the only thing the updater trusts.
 *
 * Every release carries `cogito-update.json` and `cogito-update.json.sig`:
 *
 *     {"schema": 1, "product": "COGITO", "version": "0.2.0",
 *      "notes": {"name": "release-notes.json", "sha512": "<base64>", "size": 412},
 *      "files": [{"platform": "darwin", "arch": "arm64",
 *                 "name": "COGITO-0.2.0-arm64-mac.zip", "sha512": "<base64>", "size": 1234}]}
 *
 * The `.sig` file holds one base64 Ed25519 signature per line, each over
 * `SIGNATURE_DOMAIN + <the exact manifest bytes>`. It is accepted when any line verifies
 * under any key pinned in `keys.js`. Several lines allow a key rotation.
 *
 * Why a manifest instead of a signature over each artifact: the manifest binds the version
 * to every artifact's SHA-512, so an older, genuinely signed zip cannot be replayed as a
 * newer version. A 150 MB download is also hashed as it streams instead of being held in
 * memory for Ed25519 (which cannot stream). electron-updater 7's signed manifests (still
 * alpha, electron-builder PR #9877) sign the same fields for the same reasons. This can
 * move to them once they are stable.
 *
 * Order of checks, all fail-closed: size cap -> signature on the raw bytes -> JSON ->
 * every field -> strictly newer than the running version. Release notes are accepted only
 * when their bytes hash to what the verified manifest pins. That is why their `security`
 * flag can be trusted.
 */

const crypto = require('node:crypto');

const { UpdateError } = require('./errors');

const MANIFEST_NAME = 'cogito-update.json';
const SIGNATURE_NAME = 'cogito-update.json.sig';
const NOTES_NAME = 'release-notes.json';
const SIGNATURE_DOMAIN = 'COGITO-UPDATE-MANIFEST-1\n';

const MAX_MANIFEST_BYTES = 64 * 1024;
const MAX_SIGNATURE_BYTES = 4 * 1024;
const MAX_NOTES_BYTES = 64 * 1024;
const MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_FILES = 16;
const MAX_NOTE_ITEMS = 20;
const MAX_NOTE_LENGTH = 200;

const PLATFORMS = new Set(['darwin', 'win32', 'linux']);
const ARCHES = new Set(['x64', 'arm64']);
const VERSION = /^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$/;
const FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const SHA512_BASE64 = /^[A-Za-z0-9+/]{86}==$/;
const CONTROL = /[\u0000-\u001f\u007f]/;

function fail(code, message) {
  return { ok: false, error: new UpdateError(code, message) };
}

/** `[major, minor, patch]` for a strict `MAJOR.MINOR.PATCH`, or null. No `v`, no prerelease. */
function parseVersion(value) {
  if (typeof value !== 'string') return null;
  const match = VERSION.exec(value);
  return match ? match.slice(1).map(Number) : null;
}

/** -1, 0 or 1. Throws on anything that is not a strict version, so a typo never compares. */
function compareVersions(a, b) {
  const left = parseVersion(a);
  const right = parseVersion(b);
  if (!left || !right) throw new UpdateError('VERSION_MALFORMED', `cannot compare ${a} with ${b}`);
  for (let i = 0; i < 3; i += 1) {
    if (left[i] !== right[i]) return left[i] > right[i] ? 1 : -1;
  }
  return 0;
}

function sha512Base64(bytes) {
  return crypto.createHash('sha512').update(bytes).digest('base64');
}

function signedMessage(manifestBytes) {
  return Buffer.concat([Buffer.from(SIGNATURE_DOMAIN, 'utf8'), manifestBytes]);
}

/** One signature line for `manifestBytes`. Used by the release signer and by tests. */
function signManifest(manifestBytes, privateKeyPem) {
  return crypto.sign(null, signedMessage(manifestBytes), privateKeyPem).toString('base64');
}

/** The pinned keys that parse as Ed25519. Anything else is not a key we trust. */
function trustedKeys(publicKeys) {
  const keys = [];
  for (const pem of publicKeys || []) {
    try {
      const key = crypto.createPublicKey(pem);
      if (key.asymmetricKeyType === 'ed25519') keys.push(key);
    } catch {
      // An unparseable pinned key trusts nothing. It never falls back to "any key".
    }
  }
  return keys;
}

function signatureLines(signatureText) {
  if (typeof signatureText !== 'string') return null;
  if (Buffer.byteLength(signatureText) > MAX_SIGNATURE_BYTES) return null;
  const lines = signatureText.split('\n').map((line) => line.trim()).filter(Boolean);
  return lines.length ? lines : null;
}

function verifySignature(manifestBytes, signatureText, keys) {
  const lines = signatureLines(signatureText);
  if (!lines) return 'SIGNATURE_MISSING';
  const message = signedMessage(manifestBytes);
  for (const line of lines) {
    if (!/^[A-Za-z0-9+/]{86}==$/.test(line)) continue;
    const signature = Buffer.from(line, 'base64');
    for (const key of keys) {
      if (crypto.verify(null, message, key, signature)) return null;
    }
  }
  return 'SIGNATURE_INVALID';
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasExactly(object, keys) {
  const actual = Object.keys(object).sort();
  return actual.length === keys.length && [...keys].sort().every((key, i) => key === actual[i]);
}

function isSize(value, max) {
  return Number.isInteger(value) && value > 0 && value <= max;
}

/** Why `object` is not a valid manifest, or null when it is. */
function manifestProblem(object) {
  if (!isPlainObject(object)) return 'not an object';
  if (!hasExactly(object, ['schema', 'product', 'version', 'notes', 'files'])) return 'unexpected fields';
  if (object.schema !== 1) return `schema ${object.schema}`;
  if (object.product !== 'COGITO') return `product ${object.product}`;
  if (!parseVersion(object.version)) return `version ${object.version}`;

  const notes = object.notes;
  if (!isPlainObject(notes) || !hasExactly(notes, ['name', 'sha512', 'size'])) return 'notes entry';
  if (notes.name !== NOTES_NAME) return `notes name ${notes.name}`;
  if (!SHA512_BASE64.test(notes.sha512) || !isSize(notes.size, MAX_NOTES_BYTES)) return 'notes hash or size';

  if (!Array.isArray(object.files) || object.files.length === 0 || object.files.length > MAX_FILES) {
    return 'files list';
  }
  const seen = new Set();
  for (const file of object.files) {
    if (!isPlainObject(file) || !hasExactly(file, ['platform', 'arch', 'name', 'sha512', 'size'])) return 'file entry';
    if (!PLATFORMS.has(file.platform) || !ARCHES.has(file.arch)) return `target ${file.platform}/${file.arch}`;
    if (!FILE_NAME.test(file.name) || file.name.includes('..')) return `file name ${file.name}`;
    if (!SHA512_BASE64.test(file.sha512)) return `hash of ${file.name}`;
    if (!isSize(file.size, MAX_ARTIFACT_BYTES)) return `size of ${file.name}`;
    const target = `${file.platform}/${file.arch}`;
    if (seen.has(target)) return `two files for ${target}`;
    seen.add(target);
  }
  return null;
}

/**
 * The manifest, verified and validated, or `{ok: false, error}` with one of:
 * MANIFEST_TOO_LARGE, NO_TRUSTED_KEY, SIGNATURE_MISSING, SIGNATURE_INVALID,
 * MANIFEST_MALFORMED, NOT_NEWER.
 */
function readVerifiedManifest({ manifestBytes, signatureText, publicKeys, currentVersion }) {
  if (!Buffer.isBuffer(manifestBytes) || manifestBytes.length > MAX_MANIFEST_BYTES) {
    return fail('MANIFEST_TOO_LARGE', `manifest over ${MAX_MANIFEST_BYTES} bytes`);
  }
  const keys = trustedKeys(publicKeys);
  if (!keys.length) return fail('NO_TRUSTED_KEY', 'no Ed25519 public key is pinned in keys.js');

  const signatureError = verifySignature(manifestBytes, signatureText, keys);
  if (signatureError) return fail(signatureError, `${MANIFEST_NAME} is not signed by a pinned key`);

  let object;
  try {
    object = JSON.parse(manifestBytes.toString('utf8'));
  } catch (error) {
    return fail('MANIFEST_MALFORMED', `manifest is not JSON: ${error.message}`);
  }
  const problem = manifestProblem(object);
  if (problem) return fail('MANIFEST_MALFORMED', `manifest rejected: ${problem}`);

  if (!parseVersion(currentVersion)) {
    return fail('VERSION_MALFORMED', `the running version ${currentVersion} is not MAJOR.MINOR.PATCH`);
  }
  if (compareVersions(object.version, currentVersion) <= 0) {
    return fail('NOT_NEWER', `${object.version} is not newer than ${currentVersion}`);
  }
  return { ok: true, manifest: object };
}

/** The manifest's artifact for this platform and architecture, or null. */
function fileFor(verified, platform, arch) {
  return verified.files.find((file) => file.platform === platform && file.arch === arch) || null;
}

function noteList(value) {
  if (!Array.isArray(value) || value.length > MAX_NOTE_ITEMS) return null;
  for (const item of value) {
    if (typeof item !== 'string') return null;
    const trimmed = item.trim();
    if (!trimmed || trimmed.length > MAX_NOTE_LENGTH || CONTROL.test(item)) return null;
  }
  return value.map((item) => item.trim());
}

/**
 * `release-notes.json`, accepted only when its bytes match the hash and size the verified
 * manifest pins: `{ok: true, notes: {version, security, tools, improvements}}`, or
 * `{ok: false, error}` with NOTES_MISMATCH or NOTES_MALFORMED.
 */
function parseNotes(notesBytes, verified) {
  if (!Buffer.isBuffer(notesBytes) || notesBytes.length !== verified.notes.size) {
    return fail('NOTES_MISMATCH', 'release notes are not the size the manifest pins');
  }
  if (sha512Base64(notesBytes) !== verified.notes.sha512) {
    return fail('NOTES_MISMATCH', 'release notes do not hash to what the manifest pins');
  }
  let object;
  try {
    object = JSON.parse(notesBytes.toString('utf8'));
  } catch (error) {
    return fail('NOTES_MALFORMED', `release notes are not JSON: ${error.message}`);
  }
  if (!isPlainObject(object) || !hasExactly(object, ['schema', 'version', 'security', 'tools', 'improvements'])) {
    return fail('NOTES_MALFORMED', 'release notes have unexpected fields');
  }
  const tools = noteList(object.tools);
  const improvements = noteList(object.improvements);
  if (object.schema !== 1 || object.version !== verified.version || typeof object.security !== 'boolean' || !tools || !improvements) {
    return fail('NOTES_MALFORMED', 'release notes rejected');
  }
  return { ok: true, notes: { version: object.version, security: object.security, tools, improvements } };
}

module.exports = {
  MANIFEST_NAME,
  SIGNATURE_NAME,
  NOTES_NAME,
  SIGNATURE_DOMAIN,
  MAX_MANIFEST_BYTES,
  MAX_SIGNATURE_BYTES,
  MAX_NOTES_BYTES,
  parseVersion,
  compareVersions,
  sha512Base64,
  signManifest,
  readVerifiedManifest,
  manifestProblem,
  fileFor,
  parseNotes,
};
