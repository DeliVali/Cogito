'use strict';

/**
 * Attachments (design 1n): the native «+ adjuntar» dialog, and inspecting a dropped or
 * pasted file or a long paste.
 *
 * The page never reads a file. It names one (a path from the dialog here, or the path
 * Electron's `webUtils.getPathForFile` gives the preload for a dropped or pasted `File`),
 * or hands over text it already holds. The core says what it is: name, size and lines, or
 * why it cannot be attached (`POST /sessions/{id}/attachments`, `context.attachments` in
 * the core). The core checks the path again when the message is sent, so nothing here is
 * trusted on the way back.
 *
 * Answers are `{attachment}` or `{refused: {reason, name}}`. A refusal is shown on the
 * chip, never thrown. Any other core error comes back as `{error}`.
 */

const { dialog, ipcMain } = require('electron');

/** Size limits for a path or a text sent to the core. The core refuses text past 20 MB
 * anyway. */
const MAX_PATH_CHARS = 4096;
const MAX_TEXT_CHARS = 20_000_000;
/** A pasted image's base64: 7.5 MB of bytes, the most a provider takes. */
const MAX_IMAGE_CHARS = 10_000_004;
/** At most this many files from one pick or one drop. */
const MAX_PICKED = 20;

/** A spec the core may be asked about, or null for anything else. */
function specOf(value) {
  if (!value || typeof value !== 'object') return null;
  if (typeof value.path === 'string') {
    const path = value.path;
    if (!path || path.length > MAX_PATH_CHARS || path.includes('\0')) return null;
    return { path };
  }
  // A pasted image with no file behind it: its bytes as base64, and the name it is shown by.
  if (typeof value.image === 'string') {
    if (!value.image || value.image.length > MAX_IMAGE_CHARS || !/^[A-Za-z0-9+/=]+$/.test(value.image)) return null;
    const name = typeof value.name === 'string' ? value.name.slice(0, 255) : '';
    return { image: value.image, name };
  }
  if (typeof value.text === 'string') {
    if (!value.text.trim() || value.text.length > MAX_TEXT_CHARS) return null;
    return { text: value.text };
  }
  return null;
}

/** `{reason, name}` when `error` is the core refusing an attachment (422 `{error:
 * 'refused'}`), else null. */
function refusalOf(error) {
  const body = error && error.payload && typeof error.payload === 'object' ? error.payload : {};
  if (body.error !== 'refused' || typeof body.reason !== 'string') return null;
  return { reason: body.reason, name: typeof body.name === 'string' ? body.name : '' };
}

/** What the core answered for one spec, as the window takes it. */
async function inspect(core, spec) {
  try {
    const { attachment } = await core.inspectAttachment(spec);
    return { attachment };
  } catch (error) {
    const refused = refusalOf(error);
    if (refused) return { refused };
    const body = error && error.payload && typeof error.payload === 'object' ? error.payload : {};
    return { error: body.error || (error && error.message) || 'failed', status: error && error.status };
  }
}

function register({ core, window, showOpenDialog = (win, options) => dialog.showOpenDialog(win, options) }) {
  // «+ adjuntar»: the system file dialog, several files at once, never a folder.
  ipcMain.handle('attach:pick', async () => {
    const result = await showOpenDialog(window, {
      title: 'Adjuntar',
      properties: ['openFile', 'multiSelections'],
    });
    if (!result || result.canceled || !Array.isArray(result.filePaths) || !result.filePaths.length) {
      return { canceled: true };
    }
    const items = [];
    for (const path of result.filePaths.slice(0, MAX_PICKED)) {
      items.push({ path, ...(await inspect(core, { path })) });
    }
    return { items };
  });

  // A dropped or pasted file (its path), or a long paste (its text).
  ipcMain.handle('attach:inspect', async (_event, value) => {
    const spec = specOf(value);
    if (!spec) return { error: 'a path or a text is required' };
    return inspect(core, spec);
  });
}

/** The attachments `core:send`/`core:steer` may pass on: specs only, at most `MAX_PICKED`.
 * Null when there are none, so a message without attachments is sent unchanged. Throws on
 * anything that is not a spec, so a message never goes out missing an attachment the
 * person saw on it. */
function attachmentsOf(value) {
  if (value === undefined || value === null) return null;
  if (!Array.isArray(value) || value.length > MAX_PICKED) throw new Error('invalid attachments');
  const specs = value.map(specOf);
  if (specs.some((spec) => spec === null)) throw new Error('invalid attachments');
  return specs.length ? specs : null;
}

module.exports = { register, specOf, attachmentsOf, refusalOf, MAX_PICKED };
