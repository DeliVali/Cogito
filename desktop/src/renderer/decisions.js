'use strict';

/**
 * Maps a resolved approval to the pill that replaces its card (design section 1o,
 * "Decisión tuya"). Pure, no DOM, so it can be unit-tested on its own.
 */

import { ASK_COPY, INSTALL_COPY, PILLS, SHORTCUT_APPROVE, SHORTCUT_REJECT, TERMINAL } from './strings.js';
import { displayText } from './tool-rows.js';

/**
 * `allowed` and `reason` are the `approval_resolved` event's fields
 * (`core/cogito/server/app.py`):
 *   {"type":"approval_resolved","id":"<id>","allowed":true|false|null,
 *    "reason":"user|timeout|interrupted"}
 * Only `reason: "user"` carries a real boolean: a person answered. `"timeout"` and
 * `"interrupted"` always carry `allowed: null`. Nobody decided, so the pill is "Omitiste",
 * never "Rechazaste", which would claim a refusal no one made.
 *
 * `"interrupted"` is the person pressing «Detener» while the card waited: «Detenido».
 *
 * `"auto"` is the composer's automatic mode allowing it without a card: «Automático».
 *
 * `reason` may be absent (an older event shape, or a stream reconnected mid-flight). Then
 * `allowed` alone decides, and `null`/`undefined` still lands on "Omitiste".
 *
 * `object` is what was allowed, refused or skipped: the matching `approval_request`'s
 * `command`, or its `summary`. `approval_resolved` carries only `id`, `allowed` and
 * `reason`, so the caller keeps the request keyed by `id`. The object gets the same
 * path/command sanitizing as every other rendered target.
 */
/** The pill is a one-line receipt «{verb} · {object}». A multi-line command (a heredoc
 * `cat > file << EOF\n…`) must not fill it: the pill names the command by its first line,
 * with « …» when there is more, and the shell row below carries the whole command. A
 * single line longer than the pill is cut. `full` keeps the whole command for the tooltip. */
const PILL_LINE_MAX = 160;

function pillLine(object) {
  const whole = displayText(object);
  const [first] = whole.split('\n');
  if (whole.includes('\n')) return `${first.trimEnd()} …`;
  return first.length > PILL_LINE_MAX ? `${first.slice(0, PILL_LINE_MAX - 1)}…` : first;
}

/** `action` is the request's: a line run in a terminal reads «Ejecutaste», not «Permitiste». */
function pillFor(allowed, reason, object, action = null) {
  const undecided = reason === 'timeout' || reason === 'interrupted' || allowed == null;
  // «Detenido» when the person stopped the turn while the card waited (QA 2026-10-11).
  const kind =
    reason === 'interrupted'
      ? 'stopped'
      : undecided
        ? 'skipped'
        : allowed
          ? reason === 'auto'
            ? 'auto'
            : 'allowed'
          : 'denied';
  const { label, tone, icon } = kind === 'allowed' && action === 'run_in_terminal' ? PILLS.ran : PILLS[kind];
  return { kind, tone, icon, label, object: object ? pillLine(object) : '', full: object ? displayText(object) : '' };
}

function baseName(path) {
  return String(path).split(/[\\/]/).filter(Boolean).pop() || String(path);
}

/** «2 archivos», «1 carpeta», «3 elementos»: the noun for what these items are. */
function counted(items) {
  const [one, many] = items.every((item) => item.kind === 'dir')
    ? ASK_COPY.folders
    : items.every((item) => item.kind === 'file')
      ? ASK_COPY.files
      : ASK_COPY.items;
  return ASK_COPY.plural(items.length, one, many);
}

function question(request) {
  if (request.action === 'command') return ASK_COPY.command;
  const items = Array.isArray(request.items) ? request.items : [];
  const of = (action) => items.filter((item) => item.action === action);
  const moved = of('move');
  const deleted = of('delete');
  const overwritten = of('overwrite');
  const parts = [];
  if (moved.length) parts.push(ASK_COPY.move(ASK_COPY.plural(moved.length, ...ASK_COPY.items)));
  if (deleted.length === 1 && deleted[0].kind === 'dir' && deleted[0].entries != null) {
    const [folder] = deleted;
    parts.push(ASK_COPY.removeFolder(displayText(baseName(folder.path)), ASK_COPY.held(folder.entries, folder.capped)));
  } else if (deleted.length) {
    parts.push(ASK_COPY.remove(counted(deleted)));
  }
  if (overwritten.length) parts.push(ASK_COPY.overwrite(ASK_COPY.plural(overwritten.length, ...ASK_COPY.files)));
  const unresolved = (request.unresolved || []).length;
  if (!parts.length && unresolved) {
    // Only words the shell has yet to expand (`rm "$F"`): counted as written.
    const verb = { overwrite: ASK_COPY.overwrite, move: ASK_COPY.move }[request.action] || ASK_COPY.remove;
    parts.push(verb(ASK_COPY.plural(unresolved, ...ASK_COPY.items)));
  }
  if (!parts.length) return ASK_COPY.command;
  return parts.join(ASK_COPY.and) + (request.outside ? ASK_COPY.outside : '');
}

function recoveryText(request) {
  if (request.recovery === 'kept') return ASK_COPY.nothingLost;
  if (request.recovery === 'trash') return request.action === 'overwrite' ? ASK_COPY.copySaved : ASK_COPY.recoverable;
  if (request.recovery === 'none') return ASK_COPY.notRecoverable;
  return request.recoverable ? ASK_COPY.recoverable : ASK_COPY.notRecoverable;
}

function itemLine(item, request) {
  const notes = [];
  if (item.kind === 'dir' && item.entries != null) notes.push(ASK_COPY.folder(ASK_COPY.held(item.entries, item.capped)));
  if (item.kind === 'link') notes.push(ASK_COPY.link);
  if (item.action === 'overwrite' && request.action !== 'overwrite') notes.push(ASK_COPY.overwritten);
  return [displayText(item.path), ...notes].join(' · ');
}

/**
 * The approval card's text: the question with what can be undone, and the lines under the
 * command. Built from the facts in `approval_request` (`cli._window_approver`), so the card
 * says «no se puede deshacer» whenever anything is destroyed for good, names a file a move
 * overwrites, and lists what `git clean` would remove. A core that sends none of them gets
 * its own `summary` and `recoverable`, as before.
 */
function askCard(request) {
  if (isPackageRequest(request)) return installCard(request);
  if (request.action === 'run_in_terminal') return terminalCard(request);
  if (request.action === 'external_tool') return externalCard(request);
  const command = displayText(request.command || '');
  const structured = typeof request.action === 'string';
  const lines = [command];
  if (structured && Array.isArray(request.items)) {
    for (const item of request.items) lines.push(itemLine(item, request));
  } else {
    for (const target of request.targets || []) lines.push(displayText(target));
  }
  for (const where of request.within || []) lines.push(ASK_COPY.within(displayText(where)));
  const unresolved = request.unresolved || [];
  if (unresolved.length) lines.push(`${ASK_COPY.unresolvedPrefix}: ${unresolved.map(displayText).join(', ')}`);
  const listed = request.listed || [];
  if (listed.length) {
    lines.push(ASK_COPY.listed);
    for (const name of listed) lines.push(`  ${displayText(name)}`);
    if (request.listed_more > 0) lines.push(`  ${ASK_COPY.listedMore(request.listed_more)}`);
  }
  let text;
  if (structured) {
    const asked = question(request);
    text = `¿${asked.charAt(0).toUpperCase()}${asked.slice(1)}? (${recoveryText(request)})`;
  } else {
    text = `${displayText(request.summary || '')} (${recoveryText(request)})`;
  }
  return { text, lines };
}

/** The card for a line the model wants to run in a terminal tab: the line as it will be
 * typed, then the tab and the folder it runs in. The button says «Ejecutar» (`allow`). */
function terminalCard(request) {
  const where = TERMINAL.where(displayText(request.tab_title || ''), displayText(request.cwd || ''));
  const lines = [displayText(request.command || '')];
  if (where) lines.push(where);
  return { title: TERMINAL.cardTitle, text: '', lines, allow: TERMINAL.allow };
}

/** The card for an MCP server's tool that may change something: «¿Usar rea · set_comment?»
 * and the arguments as sent. The server, not the guard, says the tool may change things. */
function externalCard(request) {
  const command = displayText(request.command || '');
  // `server · tool {arguments}` (`cli._external_command`): the label ends at the arguments.
  const brace = command.indexOf(' {');
  const label = brace > 0 ? command.slice(0, brace) : command;
  const asked = ASK_COPY.external(label);
  return {
    text: `¿${asked.charAt(0).toUpperCase()}${asked.slice(1)}? (${ASK_COPY.externalNote})`,
    lines: [command],
  };
}

/**
 * The card for a system package request (install, full upgrade, AUR install): its own
 * title, and one line with the package names, the manager and where they come from. No question and no «se puede deshacer»: the
 * title says what is asked, and nothing is deleted. `title` replaces the guardrail strip's.
 */
function isPackageRequest(request) {
  return Object.hasOwn(INSTALL_COPY.titles, request.action);
}

function installCard(request) {
  const packages = (Array.isArray(request.packages) ? request.packages : []).map(displayText);
  const source = INSTALL_COPY.sources[request.source] || displayText(request.source || '');
  // The line the notification names too: what the model asked to run, as written. The
  // core carries it out itself, so the card says what is asked first and this second.
  const lines = [INSTALL_COPY.line(packages, displayText(request.manager || ''), source)];
  if (request.command) lines.push(displayText(request.command));
  return { title: INSTALL_COPY.titles[request.action], text: '', lines };
}

/** What a resolved card's pill names: the command as written, or for a package request the
 * same line the card showed. */
function pillObject(request) {
  if (isPackageRequest(request)) return installCard(request).lines[0];
  // An MCP server's tool: «Permitiste · rea · Annotate native function», not its arguments.
  if (request.action === 'external_tool' && request.server) {
    return `${request.server} · ${request.tool_title || request.tool || ''}`;
  }
  return request.command || request.summary || '';
}

/** The keys the approval card answers (QA 2026-10-13). On the card itself: Enter allows,
 * Esc rejects, and a printed character belongs to the composer. On a control inside it
 * («Rechazar», «Permitir»): Esc still rejects, so the card is never left with keys gone dead
 * after a Tab, and Enter is left to the control, which does what its own label says (a
 * focused «Rechazar» never approves). Any modifier, a held key and the first half second
 * after the card appears (the Enter that sent the message) answer nothing.
 * `onCard` is whether the card itself is the keydown's target; `ageMs` how long it has been
 * up. Returns 'allow', 'deny', 'composer' or null. */
function approvalKey(event, { onCard, ageMs }) {
  if (onCard && event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) return 'composer';
  if (event.repeat) return null;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null;
  if (ageMs < 500) return null;
  if (event.key === 'Escape') return 'deny';
  if (event.key === 'Enter' && onCard) return 'allow';
  return null;
}

/** The hint under a focused card: both keys when the card itself has focus, only Esc when a
 * button does (Enter then does what that button says, so naming «Permitir» would mislead).
 * A terminal line's card names «Ejecutar», its button. */
function approvalHint(onCard, request = null) {
  if (!onCard) return SHORTCUT_REJECT;
  return request && request.action === 'run_in_terminal' ? TERMINAL.shortcut : SHORTCUT_APPROVE;
}

export { pillFor, askCard, pillObject, approvalKey, approvalHint };
