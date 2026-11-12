#!/usr/bin/env node
'use strict';

/**
 * Builds the release's signed manifest. Runs only in CI (.github/workflows/release.yml).
 * It uses the app's own `src/main/updater/manifest.js`, so what is published is checked by
 * the same code that will read it.
 *
 *   keygen <private-key-file>
 *       Once, on the maintainer's machine. Writes a new Ed25519
 *       private key (PKCS#8 PEM, mode 600, never overwrites) and prints the public key to
 *       pin in src/main/updater/keys.js.
 *   build --version V --notes release-notes.json --out DIR --asset PLATFORM/ARCH=FILE ...
 *       Writes DIR/cogito-update.json (each asset's name, SHA-512 and size, and the notes'
 *       hash) and copies the notes to DIR/release-notes.json.
 *   sign --manifest DIR/cogito-update.json
 *       Signs with the key in $COGITO_UPDATE_SIGNING_KEY; writes cogito-update.json.sig.
 *   verify --dir DIR [--public-key-file F]
 *       The app's own check against the keys pinned in keys.js (or F, for tests): the
 *       signature, every field, and the notes' hash. Run before anything is uploaded.
 *   check-key [--public-key-file F]
 *       Refuses a $COGITO_UPDATE_SIGNING_KEY whose public half the app does not pin. Every
 *       installed copy would refuse a release signed with it.
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const updater = path.join(__dirname, '..', 'src', 'main', 'updater');
const {
  MANIFEST_NAME,
  SIGNATURE_NAME,
  NOTES_NAME,
  parseVersion,
  sha512Base64,
  signManifest,
  manifestProblem,
  readVerifiedManifest,
  parseNotes,
} = require(path.join(updater, 'manifest.js'));
const { UPDATE_PUBLIC_KEYS } = require(path.join(updater, 'keys.js'));

class UsageError extends Error {}

function options(argv) {
  const parsed = { assets: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    const value = argv[i + 1];
    if (!flag.startsWith('--') || value === undefined) throw new UsageError(`unexpected argument ${flag}`);
    const name = flag.slice(2);
    if (name === 'asset') parsed.assets.push(value);
    else parsed[name] = value;
    i += 1;
  }
  return parsed;
}

function required(opts, name) {
  if (!opts[name]) throw new UsageError(`--${name} is required`);
  return opts[name];
}

function pinnedKeys(opts) {
  return opts['public-key-file'] ? [fs.readFileSync(opts['public-key-file'], 'utf8')] : [...UPDATE_PUBLIC_KEYS];
}

function signingKey() {
  const pem = process.env.COGITO_UPDATE_SIGNING_KEY;
  if (!pem) throw new UsageError('COGITO_UPDATE_SIGNING_KEY is not set');
  const key = crypto.createPrivateKey(pem);
  if (key.asymmetricKeyType !== 'ed25519') throw new UsageError('COGITO_UPDATE_SIGNING_KEY is not an Ed25519 key');
  return key;
}

function keygen([file]) {
  if (!file) throw new UsageError('usage: keygen <private-key-file>');
  if (fs.existsSync(file)) throw new UsageError(`${file} exists; refusing to overwrite a key`);
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  fs.writeFileSync(file, privateKey.export({ type: 'pkcs8', format: 'pem' }), { mode: 0o600, flag: 'wx' });
  process.stdout.write(publicKey.export({ type: 'spki', format: 'pem' }));
}

function build(argv) {
  const opts = options(argv);
  const version = required(opts, 'version');
  if (!parseVersion(version)) throw new UsageError(`--version ${version} is not MAJOR.MINOR.PATCH`);
  const out = required(opts, 'out');
  const notesBytes = fs.readFileSync(required(opts, 'notes'));
  const files = opts.assets.map((spec) => {
    const match = /^([a-z0-9]+)\/([a-z0-9]+)=(.+)$/.exec(spec);
    if (!match) throw new UsageError(`--asset ${spec} is not PLATFORM/ARCH=FILE`);
    const bytes = fs.readFileSync(match[3]);
    return { platform: match[1], arch: match[2], name: path.basename(match[3]), sha512: sha512Base64(bytes), size: bytes.length };
  });
  const manifest = {
    schema: 1,
    product: 'COGITO',
    version,
    notes: { name: NOTES_NAME, sha512: sha512Base64(notesBytes), size: notesBytes.length },
    files,
  };
  const problem = manifestProblem(manifest);
  if (problem) throw new UsageError(`the manifest would be rejected by the app: ${problem}`);
  const notes = parseNotes(notesBytes, manifest);
  if (!notes.ok) throw new UsageError(`release notes rejected: ${notes.error.message}`);
  fs.mkdirSync(out, { recursive: true });
  fs.writeFileSync(path.join(out, MANIFEST_NAME), `${JSON.stringify(manifest, null, 2)}\n`);
  fs.writeFileSync(path.join(out, NOTES_NAME), notesBytes);
  process.stdout.write(`${path.join(out, MANIFEST_NAME)}: ${version}, ${files.length} artifact(s), security=${notes.notes.security}\n`);
}

function sign(argv) {
  const manifestFile = required(options(argv), 'manifest');
  const signature = signManifest(fs.readFileSync(manifestFile), signingKey());
  fs.writeFileSync(path.join(path.dirname(manifestFile), SIGNATURE_NAME), `${signature}\n`);
}

function verify(argv) {
  const opts = options(argv);
  const dir = required(opts, 'dir');
  const manifestBytes = fs.readFileSync(path.join(dir, MANIFEST_NAME));
  const result = readVerifiedManifest({
    manifestBytes,
    signatureText: fs.readFileSync(path.join(dir, SIGNATURE_NAME), 'utf8'),
    publicKeys: pinnedKeys(opts),
    currentVersion: '0.0.0',
  });
  if (!result.ok) throw new UsageError(`${result.error.code}: ${result.error.message}`);
  const notes = parseNotes(fs.readFileSync(path.join(dir, NOTES_NAME)), result.manifest);
  if (!notes.ok) throw new UsageError(`${notes.error.code}: ${notes.error.message}`);
  process.stdout.write(`verified ${result.manifest.version} against ${pinnedKeys(opts).length} pinned key(s)\n`);
}

function checkKey(argv) {
  const opts = options(argv);
  const derived = crypto.createPublicKey(signingKey()).export({ type: 'spki', format: 'der' });
  const pinned = pinnedKeys(opts).map((pem) => {
    try {
      return crypto.createPublicKey(pem).export({ type: 'spki', format: 'der' });
    } catch {
      return null;
    }
  });
  if (!pinned.some((der) => der && der.equals(derived))) {
    throw new UsageError('the public half of COGITO_UPDATE_SIGNING_KEY is not pinned in src/main/updater/keys.js');
  }
  process.stdout.write('the signing key is pinned\n');
}

const COMMANDS = { keygen, build, sign, verify, 'check-key': checkKey };

function main(argv) {
  const [command, ...rest] = argv;
  const run = COMMANDS[command];
  if (!run) throw new UsageError(`usage: update-manifest.js ${Object.keys(COMMANDS).join('|')} ...`);
  run(rest);
}

try {
  main(process.argv.slice(2));
} catch (error) {
  process.stderr.write(`update-manifest: ${error.message}\n`);
  process.exit(1);
}
