'use strict';

/**
 * Terminal tabs beside «Hilo» (design 2026-11-01): the core's shells
 * (`/sessions/{id}/terminals`), listed in the viewer's tab strip (`viewer.js`) and drawn by
 * `terminal-view.js`.
 *
 * - `refresh()` asks the core which tabs exist and whether terminals exist at all here
 *   (`available: false` on Windows: no button, no «Ejecutar»). A tab the window had that is
 *   no longer listed is marked ended, its text kept.
 * - `openNew(command?, {onError, onUnsent}?)` is the explorer's button and a reply's
 *   «Ejecutar»: a new tab, shown, with the command (one or more lines) typed once the shell
 *   is at its prompt. A refusal goes to `onError(answer)`, `{error, status}` or null. When
 *   the core says the command was not typed after all (`terminal_unsent`), the tab shows a
 *   note and `onUnsent(reason, text)` hears why: `busy` or `not_ready`, and the note's text.
 * - `handle(event)` takes the core's `terminal_*` events: a tab opened (shown when the model
 *   ran a line or the person asked), output with its offset (`end`), its state at each
 *   prompt, its shell's end, and a command it was opened with that was not typed.
 *
 * `api` is `window.cogito.terminal`. `viewer` is the viewer's terminal half; `view` the
 * screens. `onAvailable(bool)` hears whether terminals exist; `paneSize()` is `{width,
 * height}` of the pane a new tab will fill.
 */

import { TERMINAL } from './strings.js';

const FIELDS = ['title', 'cwd', 'running', 'opened_by', 'touched', 'exited'];
/** `terminal_unsent`'s reasons, as the tab's note says them. */
const UNSENT = { busy: TERMINAL.unsentBusy, not_ready: TERMINAL.unsentNotReady };

function pick(tab) {
  const out = { id: tab.id };
  for (const key of FIELDS) if (key in tab) out[key] = tab[key];
  if (tab.exit_code !== undefined) out.exitCode = tab.exit_code;
  return out;
}

function createTerminals({ api, viewer, view, onAvailable = () => {}, paneSize = () => null }) {
  let available = false;
  /** id -> the tab as the window knows it. */
  const tabs = new Map();
  /** A project switch makes answers to the last project's requests stale. */
  let generation = 0;
  /** id -> `onUnsent` of the `openNew` that opened it with a command. */
  const unsentHandlers = new Map();

  function setAvailable(value) {
    available = Boolean(value);
    viewer.setTerminalsAvailable(available);
    onAvailable(available);
  }

  /** Adds or updates a tab. `replay`: the window did not see it open, so its kept output is
   * shown once. */
  function add(tab, { focus = false, replay = false } = {}) {
    if (!tab || typeof tab.id !== 'string' || !tab.id) return null;
    const known = tabs.get(tab.id);
    const entry = { ...(known || {}), ...pick(tab) };
    tabs.set(tab.id, entry);
    if (!known) {
      view.ensure(tab.id, { replay });
      viewer.addTerminal(entry, { focus });
    } else {
      viewer.updateTerminal(tab.id, entry);
      if (focus) viewer.showTerminal(tab.id);
    }
    if (entry.exited) view.exited(tab.id);
    return entry;
  }

  function update(id, patch) {
    const entry = tabs.get(id);
    if (!entry) return;
    Object.assign(entry, patch);
    viewer.updateTerminal(id, entry);
    if (entry.exited) view.exited(id);
  }

  async function refresh() {
    const asked = generation;
    const answer = await Promise.resolve(api.list()).catch(() => null);
    if (asked !== generation) return;
    const ok = Boolean(answer && !answer.error);
    setAvailable(ok && answer.available === true);
    if (!ok) return;
    const listed = new Set();
    for (const tab of Array.isArray(answer.tabs) ? answer.tabs : []) {
      if (!tab || typeof tab.id !== 'string') continue;
      listed.add(tab.id);
      add(tab, { replay: !tabs.has(tab.id) });
    }
    for (const [id, entry] of tabs) {
      if (!listed.has(id) && !entry.exited) update(id, { exited: true, running: false });
    }
  }

  /** A new tab, shown; with `command`, what it types. Returns the tab, or null when it could
   * not open (the reason to `onError`). */
  async function openNew(command = null, { onError = () => {}, onUnsent = null } = {}) {
    if (!available) {
      onError(null);
      return null;
    }
    const asked = generation;
    const body = {};
    const pane = paneSize();
    const size = pane ? view.estimateSize(pane.width, pane.height) : null;
    if (size) Object.assign(body, size);
    if (command) body.command = command;
    const answer = await Promise.resolve(api.open(body)).catch(() => null);
    if (asked !== generation) return null;
    if (!answer || answer.error || typeof answer.id !== 'string') {
      onError(answer && answer.error ? answer : null);
      return null;
    }
    const entry = add(answer, { focus: true });
    if (command && onUnsent) unsentHandlers.set(answer.id, onUnsent);
    // The core's `terminal_opened` may have come first: the person asked, so it shows.
    viewer.showTerminal(answer.id);
    return entry;
  }

  function close(id) {
    if (!tabs.has(id)) return;
    tabs.delete(id);
    unsentHandlers.delete(id);
    view.dispose(id);
    viewer.removeTerminal(id);
    Promise.resolve(api.close(id)).catch(() => {});
  }

  /** True when `event` was a terminal's. */
  function handle(event) {
    switch (event && event.type) {
      case 'terminal_opened':
        if (event.tab) add(event.tab, { focus: event.focus === true });
        return true;
      case 'terminal_data':
        if (tabs.has(event.id)) view.write(event.id, event.data, Number.isInteger(event.end) ? event.end : undefined);
        return true;
      case 'terminal_status': {
        const patch = { running: event.running === true };
        if (event.exit_code !== undefined) patch.exitCode = event.exit_code;
        update(event.id, patch);
        return true;
      }
      case 'terminal_exited':
        update(event.id, { exited: true, running: false, exitCode: event.exit_code ?? null });
        unsentHandlers.delete(event.id);
        return true;
      case 'terminal_unsent': {
        const text = UNSENT[event.reason] || UNSENT.busy;
        if (tabs.has(event.id)) view.notify(event.id, text);
        const handler = unsentHandlers.get(event.id);
        unsentHandlers.delete(event.id);
        if (handler) handler(event.reason === 'not_ready' ? 'not_ready' : 'busy', text);
        return true;
      }
      default:
        return false;
    }
  }

  /** Another project or session: its tabs go, and nothing is known until `refresh`. */
  function clear() {
    generation += 1;
    for (const id of tabs.keys()) viewer.removeTerminal(id);
    tabs.clear();
    unsentHandlers.clear();
    view.clear();
    setAvailable(false);
  }

  return {
    refresh,
    openNew,
    close,
    handle,
    clear,
    isAvailable: () => available,
    tab: (id) => tabs.get(id) || null,
  };
}

export { createTerminals };
