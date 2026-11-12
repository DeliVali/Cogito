'use strict';

/**
 * The core process, and the only thing in this application that holds its token.
 *
 * `cogito serve` prints one JSON line ({"port": n, "token": "..."}) and then serves.
 * After that it is plain HTTP on loopback, plus a server-sent event stream. The stream is
 * read here, not in the renderer, because EventSource cannot set an Authorization header,
 * so the renderer would need the token in page scripts. Reading it here keeps the token in
 * the main process, out of reach of page content, and the renderer gets parsed events.
 */

const { execFile, spawn, spawnSync } = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const readline = require('node:readline');

/** How long to wait for the readiness line. The first start loads a model. */
const STARTUP_TIMEOUT_MS = 300_000;

/** How long the bundled interpreter alone may take to start. Far below
 * STARTUP_TIMEOUT_MS, so an interpreter macOS blocks is not taken for a model loading. */
const PROBE_TIMEOUT_MS = 30_000;

/** How long opening a project (`POST /sessions`) may take before the window is told it
 * failed. A stalled graph store once held it for minutes with no answer at all. */
const OPEN_TIMEOUT_MS = 90_000;
/** How long a stopped core has to shut down on its own (close its sessions, stop the model
 * server) before its whole process group is killed. A core that hangs on the way out
 * would otherwise keep the model in memory with nothing left to stop it. */
const STOP_GRACE_MS = 15_000;

/** -I: no PYTHON* variables, user site or current directory on sys.path, so nothing on
 * this machine can shadow the bundled packages. -B: never write bytecode into the app (a
 * signed macOS bundle, a read-only AppImage mount). */
const PYTHON_FLAGS = ['-I', '-B'];

/** In a development checkout, `uv run --project` finds `cogito` from source: three levels
 * up from `desktop/src/main` is the repository root. Used only when not packaged (see
 * `resolveCoreLaunch`). */
const DEV_PROJECT_ROOT = path.resolve(__dirname, '..', '..', '..');

/**
 * Where the bundled interpreter sits under an app's resources directory, as
 * packaging/bundle-core.py lays it out. One definition, shared with packaging/after-pack.js
 * (which joins it with the build host's path rules) and packaging/core-check.js.
 */
function bundledInterpreterSegments(platform = process.platform) {
  return platform === 'win32' ? ['core', 'python', 'python.exe'] : ['core', 'python', 'bin', 'python3.12'];
}

/** The bundled interpreter's path for `platform`, in that platform's path syntax. */
function bundledInterpreter(resourcesPath, platform = process.platform) {
  if (typeof resourcesPath !== 'string' || !resourcesPath) throw new Error('no resources directory');
  const paths = platform === 'win32' ? path.win32 : path.posix;
  return paths.join(resourcesPath, ...bundledInterpreterSegments(platform));
}

/**
 * How to spawn the core. A pure function, so both values of `app.isPackaged` can be unit
 * tested without a packaged build.
 *
 * Not packaged: `uv run --project` against this checkout. A packaged build has no
 * checkout (`__dirname` is inside `resources/app.asar`), so it runs the Python it ships in
 * `resources/core/python` with `-m cogito.cli`. Never a `cogito` from PATH: that is another
 * version at best, and on the .deb `/usr/bin/cogito` is this app itself
 * (`linux.executableName`). `probe` is a short run of the interpreter alone, which `Core`
 * makes before the first `serve`.
 *
 * `env.COGITO_CORE_ROOT`, when set, switches even a packaged build to `uv run --project
 * <that path>`, so a machine with its own Cogito checkout can use it without a code change.
 */
function resolveCoreLaunch({
  port,
  isPackaged = false,
  env = process.env,
  resourcesPath = process.resourcesPath,
  platform = process.platform,
} = {}) {
  const launch = resolveCliLaunch(['serve', '--port', String(port)], { isPackaged, env, resourcesPath, platform });
  const { command } = launch;
  return isPackaged && command !== 'uv'
    ? { ...launch, probe: { command, args: [...PYTHON_FLAGS, '-c', ''], timeoutMs: PROBE_TIMEOUT_MS } }
    : { ...launch, probe: null };
}

/**
 * How to run any `cogito` subcommand (`cliArgs`, e.g. `['graph', 'up', '--json']`) the way
 * `resolveCoreLaunch` runs `serve`: the same interpreter, the same rules, one place that
 * knows them. Returns `{command, args, cwd}`.
 */
function resolveCliLaunch(
  cliArgs,
  { isPackaged = false, env = process.env, resourcesPath = process.resourcesPath, platform = process.platform } = {},
) {
  const override = typeof env.COGITO_CORE_ROOT === 'string' ? env.COGITO_CORE_ROOT.trim() : '';
  if (override || !isPackaged) {
    const root = override || DEV_PROJECT_ROOT;
    return {
      command: 'uv',
      args: ['run', '--project', root, 'cogito', ...cliArgs],
      cwd: root,
    };
  }
  return {
    command: bundledInterpreter(resourcesPath, platform),
    args: [...PYTHON_FLAGS, '-m', 'cogito.cli', ...cliArgs],
    cwd: undefined,
  };
}

/**
 * Run the bundled interpreter alone, once, before the first `serve`. Resolves when it
 * exits 0. Rejects with a message the person can act on when the file is missing, when it
 * has not exited within `timeoutMs` (macOS can hold an unapproved binary), or when it
 * fails.
 */
function probeInterpreter({ command, args, timeoutMs = PROBE_TIMEOUT_MS }) {
  if (!fs.existsSync(command)) {
    return Promise.reject(new Error(`the bundled core is missing (${command}); reinstall COGITO`));
  }
  return new Promise((resolve, reject) => {
    execFile(command, args, { timeout: timeoutMs, killSignal: 'SIGKILL', windowsHide: true }, (error, _stdout, stderr) => {
      if (!error) {
        resolve();
        return;
      }
      if (error.killed) {
        reject(
          new Error(
            `the bundled core's Python did not start within ${timeoutMs / 1000} s (${command}). ` +
              'On macOS, open COGITO from /Applications once with "Open Anyway".',
          ),
        );
        return;
      }
      const tail = String(stderr || '').trim().split('\n').slice(-5).join('\n');
      // A signal we did not send: macOS kills a binary whose signature it refuses with
      // SIGKILL, and Node's own message for that names no reason.
      const status =
        typeof error.code === 'number'
          ? `exit ${error.code}`
          : error.signal
            ? `killed by ${error.signal}`
            : error.code || error.message;
      reject(new Error(`the bundled core's Python failed to start (${command}, ${status})${tail ? `: ${tail}` : ''}`));
    });
  });
}

/**
 * Whether the bundled core starts and is `version`: `<python> -I -B -m cogito.cli
 * --version` exits 0 and prints exactly `cogito, version <version>`. The macOS update
 * self-test calls it, so a new bundle whose core is broken is rolled back.
 */
function bundledCoreReports(
  version,
  { resourcesPath = process.resourcesPath, platform = process.platform, timeoutMs = PROBE_TIMEOUT_MS, run = spawnSync } = {},
) {
  let python;
  try {
    python = bundledInterpreter(resourcesPath, platform);
  } catch {
    return false;
  }
  const result = run(python, [...PYTHON_FLAGS, '-m', 'cogito.cli', '--version'], {
    encoding: 'utf8',
    timeout: timeoutMs,
    killSignal: 'SIGKILL',
    windowsHide: true,
  });
  if (!result || result.error || result.status !== 0) return false;
  return String(result.stdout).trim() === `cogito, version ${version}`;
}

/** A free loopback port chosen by the OS. A fixed port broke the real app (2026-09-29): a
 * core left over from an earlier run held 8765, every later core failed to bind it, and the
 * window waited forever. The small race between closing this probe and the core binding is
 * the usual get-port trade-off; the app uses the port the core announces. */
function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

/** Whether a loopback `port` accepts a TCP connection within `timeoutMs`, tried every
 * 25 ms. Stops early when `abandoned()` is true (the process went away). */
async function acceptsConnections(port, timeoutMs, abandoned = () => false) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !abandoned()) {
    const connected = await new Promise((resolve) => {
      const socket = net.connect({ host: '127.0.0.1', port });
      socket.once('connect', () => {
        socket.destroy();
        resolve(true);
      });
      socket.once('error', () => resolve(false));
    });
    if (connected) return true;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return false;
}

/** The core's answer when the context store (Neo4j) is down: 503 `graph unavailable`. A
 * rewind finds its files there, so none ran. The window shows it as a line, not an error. */
function isGraphDown(error) {
  return Boolean(error) && error.status === 503 && Boolean(error.payload) && error.payload.error === 'graph unavailable';
}

/**
 * Besides being the HTTP client, a `Core` reports its process's lifecycle to its
 * supervisor (supervisor.js) as events:
 *
 * - `'exit'`, `{code, signal, ready, intentional}`: the process ended. `ready` means it had
 *   announced itself (an earlier exit already rejected `start()`). `intentional` means
 *   `stop()` ended it.
 * - `'stop'`: `stop()` was called, whether or not a process was running. A stop is what
 *   the person asked for (a quit, a project switch) and cancels any pending restart.
 */
class Core extends EventEmitter {
  /** The processes `stop()` ended, so their exit is reported as asked for. */
  #stopped = new WeakSet();
  /** How many times `stop()` has been called, so a start that began before a stop never
   * spawns after it. */
  #stops = 0;
  /** Where a packaged build's core lives, and how long its interpreter may take. */
  #resourcesPath;
  #probeTimeoutMs;
  #openTimeoutMs;
  /** The bundled interpreter has started once in this run: a restart skips the probe. */
  #probed = false;

  constructor({
    resourcesPath = process.resourcesPath,
    probeTimeoutMs = PROBE_TIMEOUT_MS,
    openTimeoutMs = OPEN_TIMEOUT_MS,
  } = {}) {
    super();
    this.#resourcesPath = resourcesPath;
    this.#probeTimeoutMs = probeTimeoutMs;
    this.#openTimeoutMs = openTimeoutMs;
    this.process = null;
    this.port = 0;
    this.token = '';
    this.model = '';
    this.sessionId = '';
    this.streamController = null;
    this.starting = null;
  }

  get address() {
    return `http://127.0.0.1:${this.port}`;
  }

  /** Spawn the core and resolve once it has announced its port and token. `isPackaged`
   * (`app.isPackaged`) picks the launch command (`resolveCoreLaunch`). A packaged build
   * probes its interpreter first, once per run. */
  async start({ port, isPackaged = false } = {}) {
    // Already running: give the same answer a fresh start gives, model included. Without
    // the model, the header showed «modelo desconocido» after the core was started just to
    // list projects. A start in flight is checked first: until it is ready, `this.process`
    // exists but `this.port` and `this.model` are not known.
    if (this.starting) return this.starting;
    if (this.process) return { port: this.port, model: this.model };
    // Claimed before the first await, so two callers racing here share one core.
    const stops = this.#stops;
    const pending = (async () => {
      const chosen = port ?? (await freePort());
      // A stop while the port was being chosen (a quit, a project switch) ends this start.
      // It must not leave behind a core that nothing will stop.
      if (this.#stops !== stops) throw new Error('the core was stopped before it started');
      const launch = resolveCoreLaunch({ port: chosen, isPackaged, resourcesPath: this.#resourcesPath });
      if (launch.probe && !this.#probed) {
        await probeInterpreter({ ...launch.probe, timeoutMs: this.#probeTimeoutMs });
        this.#probed = true;
        if (this.#stops !== stops) throw new Error('the core was stopped before it started');
      }
      return this.spawnCore({ port: chosen, isPackaged, launch });
    })();
    this.starting = pending;
    try {
      return await pending;
    } finally {
      // Only clear our own: a stop clears `starting`, and a later start may have set a new one.
      if (this.starting === pending) this.starting = null;
    }
  }

  spawnCore({ port, isPackaged, launch = resolveCoreLaunch({ port, isPackaged, resourcesPath: this.#resourcesPath }) }) {
    // Through the public CLI, never package internals: the app uses the same interface the
    // benchmarks drive, so there is one surface to keep working.
    const child = spawn(launch.command, launch.args, {
      cwd: launch.cwd,
      stdio: ['ignore', 'pipe', 'pipe'],
      // Its own process group, so stop() reaches the core behind `uv run` and not only uv.
      detached: true,
      // On Windows, `detached` would otherwise open a console window for the core.
      windowsHide: true,
      // The core watches this PID and shuts down when it is gone, so an app that is killed
      // or crashes does not leave a core holding a port and a model.
      env: { ...process.env, COGITO_PARENT_PID: String(process.pid) },
    });
    this.process = child;
    let ready = false;

    child.stderr.on('data', (chunk) => {
      // The core logs phases and warnings here. Kept visible: a silent core that failed to
      // find a model looks just like one still loading it.
      process.stderr.write(`[core] ${chunk}`);
    });

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`the core did not announce itself within ${STARTUP_TIMEOUT_MS} ms`));
      }, STARTUP_TIMEOUT_MS);

      const lines = readline.createInterface({ input: child.stdout });
      lines.on('line', (line) => {
        let announced;
        try {
          announced = JSON.parse(line);
        } catch {
          return; // Not the readiness line; the core may print other things first.
        }
        if (typeof announced.port !== 'number' || typeof announced.token !== 'string') return;
        lines.close();
        // Keep reading stdout. Closing the reader pauses it, and a core whose stdout is not
        // read blocks on its next write once the pipe fills: its event loop stops, and so
        // does the event stream (real app, 2026-10-29: «event stream: terminated»).
        child.stdout.on('data', (chunk) => process.stderr.write(`[core] ${chunk}`));
        child.stdout.resume();
        // Ready means accepting connections. `cogito serve` now binds before it prints this
        // line (cli.py `serve`), but a core that announced before binding made the first
        // request hit a closed port (real app, 2026-09-30: "fetch failed" on `openSession`).
        acceptsConnections(announced.port, STARTUP_TIMEOUT_MS, () => this.process !== child).then((accepting) => {
          clearTimeout(timer);
          // Stopped between spawning and announcing: do not use its port.
          if (this.process !== child) {
            reject(new Error('the core was stopped before it was ready'));
            return;
          }
          if (!accepting) {
            reject(new Error(`the core announced port ${announced.port} but never accepted a connection on it`));
            return;
          }
          ready = true;
          this.port = announced.port;
          this.token = announced.token;
          this.model = announced.model || '';
          resolve({ port: this.port, model: this.model });
        });
      });

      child.on('exit', (code, signal) => {
        clearTimeout(timer);
        // Only clear our own slot: after a stop and a fresh start, `this.process` is already
        // the next core, and this late exit must not orphan it.
        if (this.process === child) this.process = null;
        this.emit('exit', { code, signal, ready, intentional: this.#stopped.has(child) });
        if (ready) return;
        const error = new Error(`the core exited with code ${code} before it was ready`);
        error.exitCode = code;
        error.signal = signal;
        reject(error);
      });

      // A command that cannot be launched (`uv` not on PATH in development, a bundled
      // interpreter removed after the probe) does not exit, it emits 'error'. Unhandled,
      // Node throws on it and takes the whole app down instead of rejecting this promise.
      child.on('error', (error) => {
        clearTimeout(timer);
        if (this.process === child) this.process = null;
        reject(new Error(`could not start "${launch.command}": ${error.message}`));
      });
    });
  }

  /** `timeoutMs`: give up after that long with an error whose `code` is 'timeout'. */
  async #request(method, route, body, { timeoutMs } = {}) {
    let response;
    let text;
    try {
      response = await fetch(`${this.address}${route}`, {
        method,
        headers: {
          Authorization: `Bearer ${this.token}`,
          'Content-Type': 'application/json',
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined,
      });
      text = await response.text();
    } catch (cause) {
      if (cause && cause.name === 'TimeoutError') {
        const error = new Error(`${route} did not answer within ${timeoutMs} ms`);
        error.code = 'timeout';
        throw error;
      }
      throw cause;
    }
    let payload = {};
    let parsed = true;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      // A body that is not JSON (Starlette's text/plain «Internal Server Error»). Its
      // status says more than a SyntaxError about its first character.
      parsed = false;
      payload = {};
    }
    if (!response.ok) {
      const error = new Error(payload.error || payload.detail || `${route} answered ${response.status}`);
      // `steer()` uses these to tell "no turn running" (409: fall back to a plain turn)
      // from other failures without parsing the message. `rewind()` reads `payload` for its
      // own 409, a conflict whose body has the dry run's {files, conflicts, commit_after}.
      error.status = response.status;
      error.payload = payload;
      if (typeof payload.code === 'string') error.code = payload.code;
      throw error;
    }
    if (!parsed) throw new Error(`${route} answered ${response.status} with a body that is not JSON`);
    return payload;
  }

  async openSession(projectRoot) {
    const { id } = await this.#request(
      'POST',
      '/sessions',
      { project_root: projectRoot },
      { timeoutMs: this.#openTimeoutMs },
    );
    this.sessionId = id;
    return id;
  }

  /** Recently opened projects (`GET /settings/projects`, design 1f), newest first, each
   * `{path, name, last_opened, missing}`. Opening one (`openSession`) records it; there is
   * no separate "touch" call. */
  listProjects() {
    return this.#request('GET', '/settings/projects');
  }

  /** "Quitar de la lista" for a project whose folder no longer exists
   * (`DELETE /settings/projects`). Leaves the project's history alone. */
  forgetProject(projectRoot) {
    return this.#request('DELETE', '/settings/projects', { path: projectRoot });
  }

  /** The model catalog (`GET /models`), for the picker. */
  models() {
    return this.#request('GET', '/models');
  }

  /**
   * The model picker: serve `modelId` from now on (`POST /sessions/{id}/model`). Can take as
   * long as loading a local model. A refusal (409 while a turn runs, 422 when the model could
   * not start) is thrown with the core's body on `payload`. With no session (a failed
   * start opened no project) it is the same switch through `POST /connect/activate`.
   */
  switchModel(modelId) {
    if (!this.sessionId) return this.connectActivate(modelId);
    return this.#request('POST', `/sessions/${this.sessionId}/model`, { model_id: modelId });
  }

  /**
   * "Reiniciar contexto" (`POST /sessions/{id}/reset`): start a fresh conversation in the
   * same project — a new empty root session, no handover seed. The window session id does
   * not change, so this client keeps using it; the old conversation stays in history. A
   * refusal (409 while a turn or rewind runs, or a switch is in flight) is thrown with the
   * core's body on `payload`.
   */
  resetContext() {
    return this.#request('POST', `/sessions/${this.sessionId}/reset`);
  }

  /** The welcome (design 1d-1e, `GET /settings/welcome`): `{name, done, model, working}`. */
  welcome() {
    return this.#request('GET', '/settings/welcome');
  }

  /** Save the welcome's name, its done flag, or both (`PUT /settings/welcome`). */
  setWelcome(values) {
    return this.#request('PUT', '/settings/welcome', values);
  }

  /** The services the connection screen offers, with the links only this process opens
   * (`GET /connect/providers`). */
  connectProviders() {
    return this.#request('GET', '/connect/providers');
  }

  /** Test an API service: fetch its real model list with the key (`POST
   * /connect/api/models`). A refused key answers `{ok: false, failure}` and does not throw.
   * The key goes only in this request's body and is not kept here. */
  connectApiModels(body) {
    return this.#request('POST', '/connect/api/models', body);
  }

  /** Read a Cogito Motoris install without running it (`POST /connect/motoris/install`):
   * `{ok, checkout, config, model, ctx}`, or `{ok: false, failure}`. */
  connectMotorisInstall(body) {
    return this.#request('POST', '/connect/motoris/install', body);
  }

  /** Ask a running Cogito Motoris for its models (`POST /connect/motoris/running`): `{ok,
   * models}`, or `{ok: false, failure}`. A key goes only in this request's body. */
  connectMotorisRunning(body) {
    return this.#request('POST', '/connect/motoris/running', body);
  }

  /** Serve a catalog model without a session (`POST /connect/activate`): «Reintentar» on a
   * model the core could not start with, or whose activation was refused. A refusal is
   * thrown with its body on `payload`. */
  connectActivate(modelId) {
    return this.#request('POST', '/connect/activate', { model_id: modelId });
  }

  /** Add the tested model to the catalog, and serve it when `activate` is set (`POST
   * /connect/save`). A refusal (400) is thrown with its body on `payload`. */
  connectSave(body) {
    return this.#request('POST', '/connect/save', body);
  }

  /** What the served model has cost today (`GET /usage`), for the tray (design 2b). */
  usage() {
    return this.#request('GET', '/usage');
  }

  /**
   * One page of the project's thread (`GET /sessions/{id}/history`): pages go newest first,
   * items in a page oldest first. Returns `{items, before, source}`. `before`
   * (`<session id>:<seq>`) is the cursor for the next older page; `null` means this page
   * reaches the beginning.
   */
  history({ before = null, limit = 60 } = {}) {
    const query = new URLSearchParams({ limit: String(limit) });
    if (typeof before === 'string' && before) query.set('before', before);
    return this.#request('GET', `/sessions/${this.sessionId}/history?${query}`);
  }

  /**
   * The full content a tool row previews (`GET /sessions/{id}/artifacts/{ref}`):
   * `{text, lines, truncated}`. `ref` is the row's `detail.ref`, `<session>/<artifact>`,
   * already checked by the IPC handler; each id is still encoded separately.
   */
  toolDetail(ref) {
    const [owner, artifact] = String(ref).split('/');
    return this.#request(
      'GET',
      `/sessions/${this.sessionId}/artifacts/${encodeURIComponent(owner)}/${encodeURIComponent(artifact)}`,
    );
  }

  /**
   * "Reintentar ahora": continue the turn a provider failure stopped, as the same turn,
   * without resending its instruction (`POST /sessions/{id}/resume`). A 409 (nothing left
   * to resume) is thrown.
   */
  resume() {
    return this.#request('POST', `/sessions/${this.sessionId}/resume`);
  }

  /** Cancel the core's pending automatic resume (on a model switch, say). `reason` is one word. */
  cancelResume(reason = 'cancelled') {
    return this.#request('DELETE', `/sessions/${this.sessionId}/resume`, { reason });
  }

  /** Post a turn. The core answers at once and works on its own thread. `attachments`
   * (design 1n): `[{path} | {text}]`, read by the core. `skills` (the composer's picker):
   * `["name", …]`, the Agent Skills to force-load for this turn. `thinking` (the composer's
   * thinking control): "off", "long" or "free". Without any, the body is just `{text}`, byte
   * for byte as before. */
  async send(text, attachments = null, skills = null, thinking = null) {
    const body = { text };
    if (attachments && attachments.length) body.attachments = attachments;
    if (skills && skills.length) body.skills = skills;
    if (thinking) body.thinking = thinking;
    try {
      return await this.#request('POST', `/sessions/${this.sessionId}/turns`, body);
    } catch (error) {
      // A turn the core woke for a background command (`wake_started`) began between the
      // window seeing no turn and this request: the message joins that turn as a steer,
      // the mirror of `steer`'s fallback, instead of being lost.
      if (error.status === 409 && error.payload?.error === 'turn already running') {
        return this.#request(
          'POST',
          `/sessions/${this.sessionId}/steer`,
          attachments && attachments.length ? { text, attachments } : { text },
        );
      }
      throw error;
    }
  }

  /** Inspect a chosen file or a long paste as an attachment
   * (`POST /sessions/{id}/attachments`). Returns `{attachment}`; a 422 `{error: 'refused',
   * reason, name}` is thrown with its body on `payload`. */
  inspectAttachment(spec) {
    return this.#request('POST', `/sessions/${this.sessionId}/attachments`, spec);
  }

  interrupt() {
    return this.#request('POST', `/sessions/${this.sessionId}/interrupt`);
  }

  /**
   * Send a message into the turn that is running right now, without stopping it.
   *
   * A 409 with `{"error":"no turn running"}` means the turn ended between the window
   * deciding to steer and this request arriving: a race, not a user error. As the build
   * brief asks, the message is then sent as a normal turn (`send(text)`), and the result
   * says so (`fellBackToTurn`) so the renderer drops the "En cola" state and shows the
   * message as the start of the next turn.
   *
   * A 409 with `{"error":"rewinding"}` means a rewind is in flight and must not fall back:
   * `/turns` refuses to start during a rewind too (second strict review, item 1). The two
   * share a status code, so the body decides.
   */
  async steer(text, attachments = null, thinking = null) {
    const body = attachments && attachments.length ? { text, attachments } : { text };
    try {
      const { id } = await this.#request('POST', `/sessions/${this.sessionId}/steer`, body);
      return { queued: true, id };
    } catch (error) {
      if (error.status === 409 && error.payload?.error === 'no turn running') {
        // A new turn after all: it carries the composer's thinking level like any other.
        await (thinking ? this.send(text, attachments, null, thinking) : this.send(text, attachments));
        return { queued: false, fellBackToTurn: true };
      }
      throw error;
    }
  }

  /**
   * Answer a question the guard is holding a turn open for.
   *
   * A 404 is not a transport failure: the question is no longer waiting (it timed out or
   * the turn was interrupted). The window must be able to tell that from "allowed", so it
   * is returned, not swallowed.
   */
  async approve(approvalId, allowed, scope = '') {
    try {
      await this.#request('POST', `/sessions/${this.sessionId}/approvals/${approvalId}`, {
        allowed,
        ...(scope === 'session' ? { scope } : {}),
      });
      return { answered: true };
    } catch (error) {
      return { answered: false, reason: error.message };
    }
  }

  /**
   * Turn the composer's automatic mode on or off (`POST /sessions/{id}/auto`). Returns
   * `{ok}`, never a throw: the switch goes back when the core did not take it.
   */
  async setAuto(on) {
    try {
      const answer = await this.#request('POST', `/sessions/${this.sessionId}/auto`, { on: on === true });
      return { ok: Boolean(answer) && answer.on === (on === true) };
    } catch {
      return { ok: false };
    }
  }

  /**
   * Answer a component's permission request (design 4b) with `allow_session`, `deny` or
   * `install_and_allow`, through the same route the window's card uses
   * (`POST /sessions/{id}/permissions/{question}`). Returns `{answered}` like `approve`; a
   * 404 means the question is no longer waiting.
   */
  async answerPermission(questionId, decision) {
    try {
      await this.#request(
        'POST',
        `/sessions/${this.sessionId}/permissions/${encodeURIComponent(questionId)}`,
        { decision: String(decision) },
      );
      return { answered: true };
    } catch (error) {
      return { answered: false, reason: error.message };
    }
  }

  /**
   * "Volver a un punto": what a rewind to `pointId` would do, without doing it. Answers
   * `{files, conflicts, commit_after}` (see `core/cogito/server/app.py`'s module
   * docstring). A missing point (404) throws; the renderer has nothing useful to offer for
   * an id the core no longer knows. With the context store down it answers
   * `{graphDown: true}` (the core's 503), which the window shows as a line with «Reintentar».
   */
  async rewindDryRun(pointId) {
    try {
      return await this.#request('GET', `/sessions/${this.sessionId}/rewind/${pointId}`);
    } catch (error) {
      if (isGraphDown(error)) return { graphDown: true };
      throw error;
    }
  }

  /**
   * Perform the rewind. On success, the raw `{redo_id, files, kept, forced}` payload.
   *
   * A 409 is not a transport failure: a file changed since the point and the core refused
   * to guess (`error: "conflict"`). Its body has the dry run's
   * `{files, conflicts, commit_after}` shape, so it is returned, not thrown, as
   * `{conflict: true, files, conflicts, commit_after}`. The renderer builds the conflict
   * dialog from it without a second round trip. Like the dry run, `{graphDown: true}` when
   * the context store is down.
   */
  async rewind(pointId, { keepConflicts = false, force = false } = {}) {
    try {
      return await this.#request('POST', `/sessions/${this.sessionId}/rewind/${pointId}`, {
        keep_conflicts: keepConflicts,
        force,
      });
    } catch (error) {
      if (error.status === 409 && error.payload) return { conflict: true, ...error.payload };
      if (isGraphDown(error)) return { graphDown: true };
      throw error;
    }
  }

  /**
   * "Rehacer": undo what one past `rewind()` did, to the files and the conversation.
   * Answers `{restored, conversation}`. A 409 «conversation moved on» is not a failure: a
   * turn or another rewind followed, so the rewound branch is final. It is returned as
   * `{movedOn: true}` and the window quietly removes «Rehacer». `{graphDown: true}` when
   * the context store is down.
   */
  async redo(redoId) {
    try {
      return await this.#request('POST', `/sessions/${this.sessionId}/redo/${redoId}`);
    } catch (error) {
      if (error.status === 409 && error.payload?.error === 'conversation moved on') {
        return { movedOn: true };
      }
      if (isGraphDown(error)) return { graphDown: true };
      throw error;
    }
  }

  /**
   * Read the event stream until the session closes, handing each event to `onEvent`.
   *
   * Server-sent events arrive as blocks separated by a blank line. Only complete blocks are
   * parsed: a chunk can end anywhere, even mid-JSON, so the tail is kept until its
   * terminator arrives.
   */
  async streamEvents(onEvent) {
    this.streamController = new AbortController();
    const response = await fetch(`${this.address}/sessions/${this.sessionId}/events`, {
      headers: { Authorization: `Bearer ${this.token}` },
      signal: this.streamController.signal,
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';

    while (true) {
      const { done, value } = await reader.read();
      // Ended without the core's `close`: the connection went, not the session.
      if (done) return 'ended';
      pending += decoder.decode(value, { stream: true });

      let split;
      while ((split = pending.indexOf('\n\n')) !== -1) {
        const block = pending.slice(0, split);
        pending = pending.slice(split + 2);
        if (block.startsWith(':')) continue; // keepalive
        if (block.startsWith('event: close')) return 'closed';
        const data = block
          .split('\n')
          .filter((line) => line.startsWith('data: '))
          .map((line) => line.slice(6))
          .join('');
        if (!data) continue;
        try {
          onEvent(JSON.parse(data));
        } catch {
          onEvent({ type: 'error', message: `unreadable event: ${data.slice(0, 200)}` });
        }
      }
    }
  }

  /** Stop the core. Resolves once it has exited: on its own within `graceMs`, else after
   * its process group is killed. */
  stop({ graceMs = STOP_GRACE_MS } = {}) {
    this.#stops += 1;
    this.starting = null;
    this.streamController?.abort();
    const child = this.process;
    this.process = null;
    let exited = Promise.resolve();
    if (child) {
      this.#stopped.add(child);
      exited = new Promise((resolve) => {
        if (child.exitCode !== null || child.signalCode !== null) {
          resolve();
          return;
        }
        const timer = setTimeout(() => {
          signalGroup(child, 'SIGKILL');
          resolve();
        }, graceMs);
        timer.unref?.();
        child.once('exit', () => {
          clearTimeout(timer);
          resolve();
        });
      });
      if (process.platform === 'win32') {
        // Windows has no SIGTERM a process can catch from outside: child.kill() is
        // TerminateProcess, which runs none of the core's cleanup. The core is asked to
        // stop over its own API instead; the grace timer above still ends the tree.
        askToStop(this.address, this.token).catch(() => signalGroup(child, 'SIGTERM'));
      } else {
        signalGroup(child, 'SIGTERM');
      }
    }
    // Tells the supervisor not to restart what the person stopped, including a restart
    // already waiting out its backoff.
    this.emit('stop');
    return exited;
  }
}

/** POST /shutdown with the core's token: the clean stop on Windows. */
async function askToStop(address, token) {
  const response = await fetch(`${address}/shutdown`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(3000),
  });
  if (!response.ok) throw new Error(`shutdown answered ${response.status}`);
}

/** Signal the whole process group (uv and the core under it), else the child alone. On
 * Windows a SIGKILL ends the whole tree with taskkill, since there are no process groups;
 * the core's own kill-on-close job ends what it started either way. */
function signalGroup(child, signal) {
  if (process.platform === 'win32') {
    if (signal === 'SIGKILL') {
      try {
        spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
        return;
      } catch {
        // fall through to child.kill
      }
    }
    try {
      child.kill(signal);
    } catch {
      // Already gone.
    }
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch {
    try {
      child.kill(signal);
    } catch {
      // Already gone.
    }
  }
}

module.exports = {
  Core,
  STOP_GRACE_MS,
  resolveCoreLaunch,
  resolveCliLaunch,
  bundledInterpreter,
  bundledInterpreterSegments,
  probeInterpreter,
  bundledCoreReports,
  freePort,
  acceptsConnections,
  PROBE_TIMEOUT_MS,
};
