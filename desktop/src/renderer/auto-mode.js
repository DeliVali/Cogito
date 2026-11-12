'use strict';

/**
 * The composer's automatic mode: a switch under the message, at the left, that lets the
 * guard's questions be answered without a card (`POST /sessions/{id}/auto`, `guard/auto.py`
 * in the core). «Pedir permiso» is off, «Automático» on. What the guard refuses stays
 * refused, and the core still asks about the project folder itself, keys and credentials,
 * Cogito's own files, a whole folder in the home directory, and a deletion whose targets
 * cannot be known.
 *
 * The choice is this window's own and is kept in localStorage, which may be unavailable:
 * then it lasts until the window closes. The core forgets it with the session, so `sync()`
 * sends it again when a project opens and before each message. Every send waits for the
 * one before it, so the core ends on what the switch shows.
 */

import { AUTO_MODE } from './strings.js';

const STORAGE_KEY = 'cogito.auto';

function readStored(storage) {
  try {
    return Boolean(storage && storage.getItem(STORAGE_KEY) === 'on');
  } catch {
    return false;
  }
}

function writeStored(storage, on) {
  try {
    if (!storage) return;
    if (on) storage.setItem(STORAGE_KEY, 'on');
    else storage.removeItem(STORAGE_KEY);
  } catch {
    // Private mode or blocked storage: the choice lasts for this window only.
  }
}

/**
 * `button`: the switch. `send(on)`: tells the core, resolves to a truthy value when it took
 * it. `storage`: `window.localStorage` or null. Returns `{on, setAvailable, sync}`.
 */
function createAutoMode({ button, send, storage = null, play = () => {} }) {
  if (!button) return { on: () => false, setAvailable() {}, sync: async () => {} };

  let on = readStored(storage);
  let busy = false;
  // Sends go out one after another: a sync and a click never cross on the way to the core.
  let queue = Promise.resolve();

  function draw() {
    button.setAttribute('data-on', on ? 'true' : 'false');
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    const [label] = button.querySelectorAll('.auto-mode-label');
    if (label) label.textContent = on ? AUTO_MODE.on : AUTO_MODE.off;
    button.title = on ? AUTO_MODE.onTitle : AUTO_MODE.offTitle;
  }

  function tell(value) {
    const sent = queue.then(async () => {
      try {
        return Boolean(await send(value));
      } catch {
        return false;
      }
    });
    queue = sent;
    return sent;
  }

  button.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    const next = !on;
    on = next;
    draw();
    play('select');
    if (await tell(next)) {
      writeStored(storage, next);
    } else {
      // The core did not take it: the switch shows what is really in force.
      on = !next;
      draw();
      button.title = AUTO_MODE.failed;
      play('cancel');
    }
    busy = false;
  });

  draw();
  return {
    on: () => on,
    setAvailable(available) {
      button.hidden = !available;
    },
    /** Send the choice again, whatever it is: a new session in the core starts off, and
     * the last word the core gets is what the switch shows. */
    async sync() {
      await tell(on);
    },
  };
}

export { createAutoMode };
