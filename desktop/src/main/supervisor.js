'use strict';

/**
 * Restarts the core when it dies, the way a service manager keeps a daemon up.
 *
 * `cogito serve` can die under the window (an OOM kill, a crash in a native library, a
 * model server taking it down). Without this, the event stream ends, every later request
 * fails, and the window still looks alive. The supervisor restarts the core, reopens the
 * same project's session and reattaches the event stream, so the renderer keeps hearing
 * `core:event` on the same channel. It reports progress on `core:supervisor`:
 *
 *   {state: 'restarting', attempt, max, code, signal, retryInMs}
 *   {state: 'restarted', attempt}
 *   {state: 'gave-up', attempts, code, signal}
 *
 * The policy follows systemd 261's manuals: a start-rate limit (systemd.unit(5),
 * `StartLimitBurst=`/`StartLimitIntervalSec=`), here at most `max` (3) restarts in any
 * rolling `windowMs` (60 s); and an exponential delay before each (systemd.service(5),
 * `RestartSteps=`, ratio 2), here `delays` of 1, 2 and 4 s, so a core that dies at once is
 * not respawned in a tight loop. systemd's defaults (5 starts in 10 s) suit a daemon that
 * starts in milliseconds; a core that may take minutes to load a model gets fewer, slower
 * tries. A restart whose new process exits before announcing itself counts as one more
 * failed attempt, and the backoff continues.
 *
 * A requested stop is never restarted. `core.stop()` (quit, project switch) marks its
 * process's exit as intentional and emits `'stop'`, which cancels a pending restart and
 * abandons one already under way (`generation`).
 */

const RESTART_DELAYS_MS = [1000, 2000, 4000];
const MAX_RESTARTS = 3;
const RESTART_WINDOW_MS = 60_000;
/**
 * How long a lost event stream waits to learn whether the core died. With `uv run` in
 * between, the core's socket closes before `uv` has reaped it and exited, so the stream
 * error can arrive before the exit event. Any other stream failure is reported after this.
 */
const STREAM_GRACE_MS = 2000;

class CoreSupervisor {
  constructor({
    core,
    recover,
    report = () => {},
    log = () => {},
    delays = RESTART_DELAYS_MS,
    max = MAX_RESTARTS,
    windowMs = RESTART_WINDOW_MS,
    graceMs = STREAM_GRACE_MS,
    now = Date.now,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
  }) {
    this.recover = recover;
    this.report = report;
    this.log = log;
    this.delays = delays;
    this.max = max;
    this.windowMs = windowMs;
    this.graceMs = graceMs;
    this.now = now;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;

    /** When each restart in the current window was scheduled. */
    this.restarts = [];
    this.timer = null;
    /** A restart under way (`recover()` not settled yet). */
    this.inFlight = false;
    /** The process a restart brought up died before the restart finished. */
    this.exitDuringAttempt = null;
    /** Bumped by every stop; an attempt from an older generation is abandoned. */
    this.generation = 0;
    this.lastCrashAt = null;
    /** Lost streams waiting to learn whether the core died (`streamLost`). */
    this.waiters = new Set();

    core.on('exit', (info) => this.onExit(info));
    core.on('stop', () => this.onStop());
  }

  onExit({ code = null, signal = null, ready = false, intentional = false } = {}) {
    // A requested stop, or a start that failed before ready (the start rejects on its own).
    if (intentional || !ready) return;
    this.lastCrashAt = this.now();
    this.settleWaiters(true);
    if (this.inFlight) {
      // Counted once, when the attempt settles.
      this.exitDuringAttempt = { code, signal };
      return;
    }
    this.fail({ code, signal });
  }

  onStop() {
    this.generation += 1;
    if (this.timer) this.clearTimer(this.timer);
    this.timer = null;
    this.inFlight = false;
    this.exitDuringAttempt = null;
    // A deliberate stop resets the restart count.
    this.restarts = [];
    this.settleWaiters(true);
  }

  /** The core died, or a restart failed: schedule the next attempt, or give up. */
  fail({ code = null, signal = null }) {
    const at = this.now();
    this.restarts = this.restarts.filter((when) => at - when < this.windowMs);
    if (this.timer) this.clearTimer(this.timer);
    this.timer = null;
    if (this.restarts.length >= this.max) {
      const attempts = this.restarts.length;
      this.log(`core exited (${describe(code, signal)}); ${attempts} restarts in ${this.windowMs / 1000} s, giving up`);
      this.report({ state: 'gave-up', attempts, code, signal });
      return;
    }
    const attempt = this.restarts.length + 1;
    const retryInMs = this.delays[Math.min(attempt - 1, this.delays.length - 1)];
    this.restarts.push(at);
    this.log(`core exited (${describe(code, signal)}); restarting in ${retryInMs} ms (attempt ${attempt} of ${this.max})`);
    this.report({ state: 'restarting', attempt, max: this.max, code, signal, retryInMs });
    const generation = this.generation;
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.attempt(attempt, generation);
    }, retryInMs);
  }

  async attempt(attempt, generation) {
    if (generation !== this.generation) return;
    this.inFlight = true;
    this.exitDuringAttempt = null;
    let failure = null;
    try {
      await this.recover();
    } catch (error) {
      failure = error || new Error('restart failed');
    }
    if (generation !== this.generation) return; // stopped meanwhile, nothing to report
    this.inFlight = false;
    const died = this.exitDuringAttempt;
    this.exitDuringAttempt = null;
    if (failure || died) {
      if (failure) this.log(`restart ${attempt} failed: ${failure.message}`);
      this.fail(
        died || {
          code: Number.isInteger(failure.exitCode) ? failure.exitCode : null,
          signal: typeof failure.signal === 'string' ? failure.signal : null,
        },
      );
      return;
    }
    this.log(`core restarted (attempt ${attempt})`);
    this.report({ state: 'restarted', attempt });
  }

  /**
   * The event stream failed. Resolves `true` when the supervisor accounts for it (the core
   * died, which it reports on `core:supervisor`, or was stopped), and `false` if neither
   * happened within `graceMs`, so the caller reports the stream's own error.
   */
  streamLost() {
    if (this.lastCrashAt !== null && this.now() - this.lastCrashAt <= this.graceMs) return Promise.resolve(true);
    if (this.timer || this.inFlight) return Promise.resolve(true);
    return new Promise((resolve) => {
      const waiter = { resolve, timer: null };
      waiter.timer = this.setTimer(() => {
        this.waiters.delete(waiter);
        resolve(false);
      }, this.graceMs);
      this.waiters.add(waiter);
    });
  }

  settleWaiters(value) {
    for (const waiter of this.waiters) {
      this.clearTimer(waiter.timer);
      waiter.resolve(value);
    }
    this.waiters.clear();
  }
}

function describe(code, signal) {
  if (signal) return `signal ${signal}`;
  return `code ${code}`;
}

module.exports = { CoreSupervisor, RESTART_DELAYS_MS, MAX_RESTARTS, RESTART_WINDOW_MS, STREAM_GRACE_MS };
