'use strict';

/**
 * Conexiones (design 4e, 4f, 4g, the 1a chip): what the screens show for the MCP servers
 * `GET /connections` lists. Pure, so it can be tested without a DOM. Every value comes from
 * the core's answer: the server's name, its state, its tools' titles and whether each one
 * may change something (the server's `readOnlyHint`; without one, «Hace cambios»).
 */

import { CONNECTIONS } from './strings.js';

/** The logo the design draws: one or two letters from the name, on a quiet tile. */
function logoText(name) {
  const words = String(name || '').split(/[\s_\-.]+/).filter(Boolean);
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/** Status colours of the design's cards: dot, then text. */
const STATUS_COLOURS = {
  connected: ['#5fae78', '#2f6e45'],
  failed: ['#d0584b', '#a4392e'],
  login: ['#d39a3a', '#8a6016'],
  starting: ['#9aa3b5', '#5d6577'],
  disabled: ['#9aa3b5', '#8b93a3'],
};

/** The start log a card shows: while starting, and after a failure unless the error
 * already quotes its last line. */
function startLog(connection, status) {
  const lines = Array.isArray(connection.log) ? connection.log.map(String) : [];
  if (!lines.length || (status !== 'starting' && status !== 'failed')) return '';
  if (status === 'failed' && String(connection.error || '').includes(lines[lines.length - 1])) return '';
  return lines.join('\n');
}

function connectionModel(connection) {
  const status = STATUS_COLOURS[connection.status] ? connection.status : 'failed';
  const tools = Array.isArray(connection.tools) ? connection.tools : [];
  const lower = (text) => String(text).charAt(0).toLowerCase() + String(text).slice(1);
  const reads = tools.filter((tool) => !tool.changes).map((tool) => lower(tool.title));
  const writes = tools.filter((tool) => tool.changes).map((tool) => lower(tool.title));
  const [dot, ink] = STATUS_COLOURS[status];
  return {
    name: connection.name,
    logo: logoText(connection.name),
    status,
    statusText: CONNECTIONS.status[status],
    dot,
    ink,
    enabled: Boolean(connection.enabled),
    does: status === 'failed' || status === 'starting' ? '' : CONNECTIONS.does(reads, writes),
    error: status === 'failed' || status === 'login' ? String(connection.error || '') : '',
    signingIn: status === 'login' && Boolean(connection.signing_in),
    actions: tools.map((tool) => ({
      name: tool.title,
      kind: tool.changes ? CONNECTIONS.changes : CONNECTIONS.read,
      changes: Boolean(tool.changes),
    })),
    tech: CONNECTIONS.tech(String(connection.command || ''), Number(connection.tokens) || 0),
    // What the server printed while it starts, or before it failed: its last lines.
    log: startLog(connection, status),
    // Values the entry leaves to fill in (their names only).
    needs: Array.isArray(connection.needs) && connection.needs.length ? CONNECTIONS.needs(connection.needs.map(String)) : '',
  };
}

/** The chip in the bar: «⧉ N conexiones» over the enabled ones, red dot when one of them
 * failed, green when any is on, grey with none. */
function chipModel(connections) {
  const list = Array.isArray(connections) ? connections : [];
  const active = list.filter((connection) => connection.enabled);
  const failed = active.some((connection) => connection.status === 'failed');
  return {
    text: CONNECTIONS.chip(active.length),
    dot: failed ? '#d0584b' : active.length ? '#5fae78' : '#9aa3b5',
    many: active.length >= 5,
  };
}

/** The permission card for an MCP tool (design 4g), from `approval_request`. */
function permissionModel(request) {
  const server = String(request.server || '');
  const title = String(request.tool_title || request.tool || '');
  const changes = request.changes !== false;
  return {
    logo: logoText(server),
    heading: `${server} · ${title}`,
    kind: changes ? CONNECTIONS.changes : CONNECTIONS.read,
    changes,
    // `mcp_add` (asked every time): the command or address it will save and run is the
    // card's own line, not a detail behind «Ver detalles».
    sent: request.every_time ? String(request.command || '') : CONNECTIONS.sentTo(server, changes),
    everyTime: Boolean(request.every_time),
    details: String(request.command || ''),
    detailsTarget: `${request.tool || ''} → ${server}`,
  };
}

export { chipModel, connectionModel, logoText, permissionModel };
