'use strict';

/**
 * Every byte the updater reads comes through here.
 *
 * - Only `https://`, only GitHub and its release CDN (`PRODUCTION_POLICY`). GitHub redirects
 *   `/releases/download/...` to `release-assets.githubusercontent.com` (formerly
 *   `objects.githubusercontent.com` / `github-releases.githubusercontent.com`), so those are
 *   the only hosts a redirect may lead to.
 * - Redirects are followed by hand (`redirect: 'manual'`). Each hop is checked against the
 *   policy before it is requested, so a redirect to a foreign host or to plain http is
 *   refused without contacting it.
 * - Every read has a size cap. A download is hashed while it streams to `<name>.partial`,
 *   renamed into place only once its size and SHA-512 match the signed manifest, and
 *   deleted otherwise.
 *
 * Plain `http` is for tests only: loopback hosts only, and only through
 * `makePolicy({allowHttp: true})`, which refuses any other host.
 *
 * This uses Node's `fetch`, not Electron's `net`: it runs outside Electron (so the lab can
 * test it) and its manual redirects expose the real `Location`. It does not read the system
 * proxy settings. electron-updater's own requests (NSIS, AppImage) do.
 */

const crypto = require('node:crypto');
const fs = require('node:fs');

const { UpdateError } = require('./errors');

const RELEASE_HOSTS = Object.freeze([
  'github.com',
  'release-assets.githubusercontent.com',
  'objects.githubusercontent.com',
  'github-releases.githubusercontent.com',
]);

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const MAX_REDIRECTS = 5;
const REQUEST_TIMEOUT_MS = 60_000;
const IDLE_TIMEOUT_MS = 60_000;

function makePolicy({ hosts, allowHttp = false }) {
  const list = [...hosts].map((host) => host.toLowerCase());
  if (allowHttp && !list.every((host) => LOOPBACK_HOSTS.has(host))) {
    throw new UpdateError('POLICY_INVALID', 'plain http is only ever allowed for loopback hosts');
  }
  return Object.freeze({ hosts: new Set(list), allowHttp });
}

const PRODUCTION_POLICY = makePolicy({ hosts: RELEASE_HOSTS });

/** The parsed URL when `policy` allows it; otherwise throws URL_REFUSED. */
function checkUrl(url, policy) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new UpdateError('URL_REFUSED', `not a URL: ${url}`);
  }
  const refuse = (why) => new UpdateError('URL_REFUSED', `${why}: ${parsed.href}`);
  if (parsed.username || parsed.password) throw refuse('credentials in URL');
  if (!policy.hosts.has(parsed.hostname.toLowerCase())) {
    throw refuse('host not allowed');
  }
  if (parsed.protocol === 'https:') {
    if (parsed.port && !policy.allowHttp) throw refuse('non-default port');
    return parsed;
  }
  if (parsed.protocol === 'http:' && policy.allowHttp) return parsed;
  throw refuse('only https is allowed');
}

/** A signal that fires on `signal` or after `ms`, whichever comes first. */
function withTimeout(signal, ms) {
  const timeout = AbortSignal.timeout(ms);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

/**
 * The first 2xx response for `url`, following at most MAX_REDIRECTS redirects, every hop
 * checked against `policy` before it is requested.
 */
async function openUrl(url, { policy, fetchImpl = fetch, signal, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  let current = checkUrl(url, policy);
  for (let hop = 0; ; hop += 1) {
    let response;
    try {
      response = await fetchImpl(current.href, {
        redirect: 'manual',
        signal: withTimeout(signal, timeoutMs),
        headers: { 'User-Agent': 'COGITO-updater', Accept: '*/*' },
      });
    } catch (error) {
      throw new UpdateError('NETWORK', `${current.host}: ${error.message}`);
    }
    if (REDIRECT_STATUSES.has(response.status)) {
      await response.body?.cancel().catch(() => {});
      const location = response.headers.get('location');
      if (!location) throw new UpdateError('HTTP_STATUS', `redirect without Location from ${current.host}`);
      if (hop + 1 > MAX_REDIRECTS) throw new UpdateError('TOO_MANY_REDIRECTS', `more than ${MAX_REDIRECTS} redirects`);
      current = checkUrl(new URL(location, current).href, policy);
      continue;
    }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {});
      const error = new UpdateError('HTTP_STATUS', `${response.status} from ${current.host}${current.pathname}`);
      error.status = response.status;
      throw error;
    }
    return response;
  }
}

/** The whole body of `url` as a Buffer, refusing anything over `maxBytes`. */
async function fetchBytes(url, { maxBytes, ...options }) {
  const response = await openUrl(url, options);
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) {
    await response.body?.cancel().catch(() => {});
    throw new UpdateError('TOO_LARGE', `${url} declares ${declared} bytes, over ${maxBytes}`);
  }
  const chunks = [];
  let total = 0;
  try {
    for await (const chunk of response.body) {
      total += chunk.length;
      if (total > maxBytes) throw new UpdateError('TOO_LARGE', `${url} is over ${maxBytes} bytes`);
      chunks.push(Buffer.from(chunk));
    }
  } catch (error) {
    if (error instanceof UpdateError) throw error;
    throw new UpdateError('NETWORK', `${url}: ${error.message}`);
  }
  return Buffer.concat(chunks);
}

/**
 * Stream `url` into `destination`, which appears only once its size and SHA-512 match.
 * `onProgress(fraction)` gets a non-decreasing value in [0, 1]. A stall of
 * `idleTimeoutMs` without a byte aborts the download (STALLED).
 */
async function downloadFile(
  url,
  destination,
  { expectedSize, expectedSha512, onProgress, idleTimeoutMs = IDLE_TIMEOUT_MS, signal, ...options },
) {
  const partial = `${destination}.partial`;
  const controller = new AbortController();
  const combined = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
  let stalled = false;
  let idle = null;
  const armIdle = () => {
    clearTimeout(idle);
    idle = setTimeout(() => {
      stalled = true;
      controller.abort();
    }, idleTimeoutMs);
  };

  const hash = crypto.createHash('sha512');
  let handle = null;
  let transferred = 0;
  try {
    armIdle();
    // The whole-request timeout would cut a slow but healthy download. The idle timer
    // above bounds it instead.
    const response = await openUrl(url, { ...options, signal: combined, timeoutMs: 24 * 60 * 60 * 1000 });
    handle = await fs.promises.open(partial, 'w');
    onProgress?.(0);
    for await (const chunk of response.body) {
      armIdle();
      transferred += chunk.length;
      if (transferred > expectedSize) {
        throw new UpdateError('SIZE_MISMATCH', `${url} is longer than the ${expectedSize} bytes declared`);
      }
      hash.update(chunk);
      await handle.write(chunk);
      onProgress?.(transferred / expectedSize);
    }
    await handle.close();
    handle = null;
    if (transferred !== expectedSize) {
      throw new UpdateError('SIZE_MISMATCH', `${url} is ${transferred} bytes, ${expectedSize} declared`);
    }
    if (hash.digest('base64') !== expectedSha512) {
      throw new UpdateError('HASH_MISMATCH', `${url} does not hash to the signed SHA-512`);
    }
    await fs.promises.rename(partial, destination);
    return destination;
  } catch (error) {
    await handle?.close().catch(() => {});
    await fs.promises.rm(partial, { force: true });
    if (stalled) throw new UpdateError('STALLED', `${url}: no data for ${idleTimeoutMs} ms`);
    if (error instanceof UpdateError) throw error;
    throw new UpdateError('NETWORK', `${url}: ${error.message}`);
  } finally {
    clearTimeout(idle);
  }
}

/** `{sha512, size}` of a file on disk, read as a stream. */
async function hashFile(file) {
  const hash = crypto.createHash('sha512');
  let size = 0;
  for await (const chunk of fs.createReadStream(file)) {
    size += chunk.length;
    hash.update(chunk);
  }
  return { sha512: hash.digest('base64'), size };
}

module.exports = {
  RELEASE_HOSTS,
  PRODUCTION_POLICY,
  makePolicy,
  checkUrl,
  openUrl,
  fetchBytes,
  downloadFile,
  hashFile,
};
