'use strict';

/**
 * The Ed25519 public keys a release's `cogito-update.json` must be signed by. They are
 * pinned in the application so nothing fetched at update time can change which key is
 * trusted.
 *
 * The release workflow refuses to build a release whose signing secret matches no key in
 * this list, and an update signed by any other key is refused.
 *
 * To rotate a key: ship a release that trusts both the old and the new key, sign with the
 * new one from then on, and drop the old key a release later. Keys are SPKI PEM strings,
 * the format `crypto.createPublicKey()` reads and `packaging/update-manifest.js keygen`
 * prints.
 */
const UPDATE_PUBLIC_KEYS = Object.freeze([
  // Generated 2026-10-30 for 0.1.0.
  `-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAVbwy2yb1McxsGe25E+WR9zmhoGdHdob2OlS4i061MlQ=
-----END PUBLIC KEY-----
`,
]);

module.exports = { UPDATE_PUBLIC_KEYS };
