'use strict';

/**
 * Keeps the context store (the managed Neo4j) up, the way the supervisor keeps the core up.
 *
 * Nothing restarts Neo4j after a reboot, and a core with no store runs journal-only: the
 * window then loses rewind, recall and the thread's lineage without a word (real session,
 * 2026-10-12). So the app asks `cogito graph up` once when it starts, and again when the
 * person presses «Reintentar». That command starts the managed install if there is one and
 * it is down, and waits for Bolt; it never touches a system install, and `chat` and
 * `serve` (so every benchmark run) never run it.
 *
 * It is here and not in `cogito serve` because `serve` is also what the benchmark holds,
 * and because Neo4j outlives the core, which restarts on a crash and on every project
 * switch.
 *
 * It never blocks the window and never throws. State, reported as `'state'` events and read
 * back with `state`:
 *
 *   {state: 'unknown'}                              nothing asked yet
 *   {state: 'starting'}                             `graph up` is taking a while, or a retry
 *   {state: 'ready', how: 'up' | 'started'}
 *   {state: 'down', reason: 'absent' | 'unmanaged' | 'failed', detail}
 */

const { execFile } = require('node:child_process');
const { EventEmitter } = require('node:events');

/** `graph up` waits up to 90 s for Bolt; this leaves room for the interpreter to start. */
const GRAPH_UP_TIMEOUT_MS = 150_000;

/** A start that finds Neo4j already up answers in a second or two; the window says
 * «starting» only if it takes longer, so a normal launch shows nothing. */
const ANNOUNCE_AFTER_MS = 2500;

/** The JSON line `cogito graph up --json` printed, or null. */
function parseAnswer(stdout) {
  const lines = String(stdout || '').split('\n').map((line) => line.trim()).filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const answer = JSON.parse(lines[index]);
      if (answer && typeof answer.state === 'string') return answer;
    } catch {
      // not the answer line
    }
  }
  return null;
}

/** A command's answer, as the state the window shows. */
function stateFrom(answer) {
  if (answer.state === 'up' || answer.state === 'started') return { state: 'ready', how: answer.state };
  const reasons = ['absent', 'unmanaged', 'failed'];
  return {
    state: 'down',
    reason: reasons.includes(answer.state) ? answer.state : 'failed',
    detail: typeof answer.detail === 'string' ? answer.detail : '',
  };
}

class GraphKeeper extends EventEmitter {
  /**
   * `launch()` gives `{command, args, cwd}` for `cogito graph up --json` (core.js's
   * `resolveCliLaunch`), read at each run. `run` is `execFile`, a parameter so a test never
   * starts a process.
   */
  constructor({
    launch,
    run = execFile,
    timeoutMs = GRAPH_UP_TIMEOUT_MS,
    announceAfterMs = ANNOUNCE_AFTER_MS,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
    log = () => {},
  }) {
    super();
    this.launch = launch;
    this.run = run;
    this.timeoutMs = timeoutMs;
    this.announceAfterMs = announceAfterMs;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.log = log;
    this.state = { state: 'unknown' };
    this.inFlight = null;
  }

  #set(state) {
    this.state = state;
    this.emit('state', state);
  }

  /**
   * Make sure the store is up. One run at a time: a second call gets the first one's
   * answer. `announce` shows «starting» at once (a retry the person asked for); otherwise
   * only if the run is slow. Resolves with the final state; never rejects.
   */
  ensure({ announce = false } = {}) {
    if (this.inFlight) return this.inFlight;
    const pending = this.#ensure(announce).finally(() => {
      if (this.inFlight === pending) this.inFlight = null;
    });
    this.inFlight = pending;
    return pending;
  }

  async #ensure(announce) {
    let timer = null;
    if (announce) this.#set({ state: 'starting' });
    else timer = this.setTimer(() => this.#set({ state: 'starting' }), this.announceAfterMs);
    let final;
    try {
      final = await this.#graphUp();
    } finally {
      if (timer) this.clearTimer(timer);
    }
    this.log(`${final.state}${final.reason ? ` (${final.reason}: ${final.detail})` : ''}`);
    this.#set(final);
    return final;
  }

  #graphUp() {
    return new Promise((resolve) => {
      let launch;
      try {
        launch = this.launch();
      } catch (error) {
        resolve({ state: 'down', reason: 'failed', detail: error.message });
        return;
      }
      try {
        this.run(
          launch.command,
          launch.args,
          { cwd: launch.cwd, timeout: this.timeoutMs, killSignal: 'SIGKILL', windowsHide: true },
          (error, stdout, stderr) => {
            // Exit 1 is how a store that will not start answers; the JSON line says why.
            const answer = parseAnswer(stdout);
            if (answer) {
              resolve(stateFrom(answer));
              return;
            }
            const tail = String(stderr || '').trim().split('\n').slice(-3).join(' ');
            const why = error && error.killed ? 'timed out' : (error && error.message) || 'no answer';
            resolve({ state: 'down', reason: 'failed', detail: tail || why });
          },
        );
      } catch (error) {
        resolve({ state: 'down', reason: 'failed', detail: error.message });
      }
    });
  }
}

module.exports = { GraphKeeper, parseAnswer, stateFrom, GRAPH_UP_TIMEOUT_MS, ANNOUNCE_AFTER_MS };
