'use strict';

/**
 * The core's questions to the browser and main's answers.
 *
 * The core asks on the session's event stream, `{"type": "browser_command", "id", "op",
 * "args"}`; main runs the named operation (host.js) and answers with
 * `POST /sessions/{id}/browser/answers/{id}`, with the token only this process holds:
 * `{"ok": true, "result"}` or `{"ok": false, "error", "kind"}`. Every other event is left
 * alone. An answer the core no longer waits for gets 404, which is not an error here.
 */

const { BrowserFailure } = require('./ops');

const COMMAND_ID = /^[0-9a-f]{32}$/;

class BrowserRpc {
  constructor({ core, host, log = () => {}, fetchImpl = (...args) => fetch(...args) }) {
    this.core = core;
    this.host = host;
    this.log = log;
    this.fetch = fetchImpl;
  }

  /**
   * Handles `event` if it is a browser command for `sessionId` (the session whose stream it
   * came on). Returns whether it was one; the answer is posted later.
   */
  handle(event, sessionId = this.core.sessionId) {
    if (!event || event.type !== 'browser_command') return false;
    if (typeof event.id !== 'string' || !COMMAND_ID.test(event.id) || !sessionId) return true;
    this.run(event, sessionId);
    return true;
  }

  async run(event, sessionId) {
    let body;
    try {
      const result = await this.host.command(event.op, event.args);
      body = { ok: true, result };
    } catch (error) {
      const kind = error instanceof BrowserFailure ? error.kind : 'failed';
      if (!(error instanceof BrowserFailure)) this.log(`${String(event.op).slice(0, 40)}: ${error && error.stack}`);
      body = { ok: false, error: String((error && error.message) || 'failed').slice(0, 2000), kind };
    }
    await this.answer(sessionId, event.id, body);
  }

  async answer(sessionId, id, body) {
    const url = `${this.core.address}/sessions/${encodeURIComponent(sessionId)}/browser/answers/${id}`;
    try {
      const response = await this.fetch(url, {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.core.token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!response.ok && response.status !== 404) this.log(`answer ${id}: the core said ${response.status}`);
    } catch (error) {
      this.log(`answer ${id}: ${error.message}`);
    }
  }
}

module.exports = { BrowserRpc, COMMAND_ID };
