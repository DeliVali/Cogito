'use strict';

/**
 * Reads and verifies a release from GitHub Releases.
 *
 * `latest()` finds the newest release through `/releases/latest/download/cogito-update.json`
 * (GitHub redirects to the newest release's asset, so no API call and no rate limit). It
 * then fetches that release's signature and notes from the version-pinned
 * `/releases/download/v<version>/` path. A release published between two requests cannot
 * pair one release's manifest with another's signature: the pair verifies, or the check
 * fails closed and is retried later.
 *
 * The version is read from the unverified manifest only to build the pinned URL, and only
 * after the strict `MAJOR.MINOR.PATCH` check, so nothing unverified can put its own path
 * into a URL.
 */

const { UpdateError } = require('./errors');
const { PRODUCTION_POLICY, fetchBytes, downloadFile } = require('./fetch');
const { UPDATE_PUBLIC_KEYS } = require('./keys');
const {
  MANIFEST_NAME,
  SIGNATURE_NAME,
  NOTES_NAME,
  MAX_MANIFEST_BYTES,
  MAX_SIGNATURE_BYTES,
  MAX_NOTES_BYTES,
  parseVersion,
  readVerifiedManifest,
  parseNotes,
} = require('./manifest');

const RELEASES_BASE = 'https://github.com/DeliVali/Cogito/releases';

function claimedVersion(manifestBytes) {
  let claimed;
  try {
    claimed = JSON.parse(manifestBytes.toString('utf8')).version;
  } catch (error) {
    throw new UpdateError('MANIFEST_MALFORMED', `manifest is not JSON: ${error.message}`);
  }
  if (!parseVersion(claimed)) throw new UpdateError('MANIFEST_MALFORMED', 'manifest version is not MAJOR.MINOR.PATCH');
  return claimed;
}

function createFeed({ base = RELEASES_BASE, policy = PRODUCTION_POLICY, fetchImpl, publicKeys = UPDATE_PUBLIC_KEYS } = {}) {
  const get = (url, maxBytes) => fetchBytes(url, { policy, fetchImpl, maxBytes });
  const latestUrl = (name) => `${base}/latest/download/${encodeURIComponent(name)}`;
  const pinnedUrl = (version, name) => `${base}/download/v${version}/${encodeURIComponent(name)}`;

  async function verified(manifestBytes, version, currentVersion) {
    const signatureText = (await get(pinnedUrl(version, SIGNATURE_NAME), MAX_SIGNATURE_BYTES)).toString('utf8');
    const result = readVerifiedManifest({ manifestBytes, signatureText, publicKeys, currentVersion });
    if (!result.ok) {
      if (result.error.code === 'NOT_NEWER') return null;
      throw result.error;
    }
    const notes = parseNotes(await get(pinnedUrl(version, NOTES_NAME), MAX_NOTES_BYTES), result.manifest);
    if (!notes.ok) throw notes.error;
    return { manifest: result.manifest, notes: notes.notes };
  }

  return {
    /** `{manifest, notes}` for the newest release when it is newer than `currentVersion`, else null. */
    async latest({ currentVersion }) {
      const manifestBytes = await get(latestUrl(MANIFEST_NAME), MAX_MANIFEST_BYTES);
      return verified(manifestBytes, claimedVersion(manifestBytes), currentVersion);
    },

    /** The same for one named version (electron-updater has already chosen it). */
    async release(version, { currentVersion }) {
      if (!parseVersion(version)) throw new UpdateError('MANIFEST_MALFORMED', `not a version: ${version}`);
      const manifestBytes = await get(pinnedUrl(version, MANIFEST_NAME), MAX_MANIFEST_BYTES);
      if (claimedVersion(manifestBytes) !== version) {
        throw new UpdateError('MANIFEST_MALFORMED', `the v${version} manifest claims another version`);
      }
      return verified(manifestBytes, version, currentVersion);
    },

    artifactUrl(version, name) {
      return pinnedUrl(version, name);
    },

    /** Download one manifest `file` of `version` to `destination`, checked against its signed size and SHA-512. */
    download(version, file, destination, { onProgress, signal } = {}) {
      return downloadFile(pinnedUrl(version, file.name), destination, {
        policy,
        fetchImpl,
        signal,
        expectedSize: file.size,
        expectedSha512: file.sha512,
        onProgress,
      });
    },
  };
}

module.exports = { RELEASES_BASE, createFeed };
