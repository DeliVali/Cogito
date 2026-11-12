'use strict';

/**
 * Routes the core's events to everything outside the window: the tray icon and its usage
 * menu (design 2b), the Windows taskbar, and system notifications (2c). It imports no
 * Electron (tray, notifier, core client and timers are passed in), so `cogito-lab` can
 * drive it with fakes.
 *
 * - `phase`: the tray's five states (`shared/phase-map.js`); "done" settles back to idle.
 * - `tool_start`: the status line's detail ("Trabajando · npm test").
 * - `approval_request`: a permission notification, except for one the automatic mode
 *   already allowed (`auto: true`); `approval_resolved` withdraws it.
 * - `permission_request` (a component's, design 4b): the same notification;
 *   `permission_resolved` withdraws it.
 * - `turn_complete`: "Tarea lista" only for a turn that ended with `end_turn`. A turn the
 *   person interrupted or an internal error stopped gets nothing, because
 *   design 2c has no text for those. The window shows what happened.
 * - `service_error`: the tray goes red, with «Límite de uso alcanzado» / «La API no
 *   respondió» and «Reintentar ahora». Retry resumes the same turn (`POST /resume`) from the
 *   main process, so it works with the window closed. `context_overflow` reads «No cabe en
 *   el contexto» with «Cambiar modelo» instead, since the same request would be refused again.
 * - `resume_started`: withdraws the notification and sets the tray back to working.
 * - `GET /usage` is polled (often while working, rarely at rest) and also right after a turn
 *   ends, the served model changes (`model_changed`), or per-minute headroom crosses 10 %
 *   (`rate_limit_near`, design 2e: a menu row, no icon, no notification).
 */

const { PHASE_TO_TRAY_STATE, DONE_SETTLE_MS } = require('../shared/phase-map');
const {
  permissionNotification,
  componentPermissionNotification,
  doneNotification,
  serviceErrorNotification,
} = require('./notify-policy');

const TERMINAL_PHASES = new Set(['done', 'interrupted', 'failed']);
const POLL_BUSY_MS = 5000;
const POLL_IDLE_MS = 30000;

function toolDetail(event) {
  const target = event.target || {};
  if (target.command) return target.command;
  if (Array.isArray(target.paths) && target.paths.length) return target.paths[0];
  return event.name || '';
}

class StatusHub {
  constructor({
    tray,
    notifier,
    core,
    resume = null,
    project = '',
    toRenderer = () => {},
    focusWindow = () => {},
    timers = { setTimeout, clearTimeout },
    now = () => Date.now(),
    log = (message) => process.stderr.write(`[status] ${message}\n`),
  }) {
    this.tray = tray;
    this.notifier = notifier;
    this.core = core;
    // How «Reintentar ahora» resumes. ipc.js passes one that also tells the updater a turn
    // is starting.
    this.resume = resume || (() => this.core.resume());
    this.project = project;
    this.toRenderer = toRenderer;
    this.focusWindow = focusWindow;
    this.timers = timers;
    this.now = now;
    this.log = log;
    this.state = 'idle';
    this.turnFiles = 0;
    this._settle = null;
    this._poll = null;
    this._stopped = false;
    if (tray && tray.setProject) tray.setProject(project);
  }

  // -- tray state -------------------------------------------------------------------------

  setState(state) {
    if (this._settle) {
      this.timers.clearTimeout(this._settle);
      this._settle = null;
    }
    this.state = state;
    this.tray.setState(state);
    if (state === 'done') {
      this._settle = this.timers.setTimeout(() => {
        this._settle = null;
        this.setState('idle');
      }, DONE_SETTLE_MS);
    }
  }

  /** Shows a notification (the notifier skips it while the window is in front). Never throws. */
  _notify(spec) {
    try {
      Promise.resolve(this.notifier.notify(spec)).catch((error) => this.log(`notify: ${error.message}`));
    } catch (error) {
      this.log(`notify: ${error.message}`);
    }
  }

  // -- events ---------------------------------------------------------------------------------

  onEvent(event) {
    switch (event.type) {
      case 'phase': {
        this.setState(PHASE_TO_TRAY_STATE[event.phase] || 'idle');
        if (TERMINAL_PHASES.has(event.phase)) {
          this.tray.setDetail('');
        }
        break;
      }
      case 'tool_start':
        this.tray.setDetail(toolDetail(event));
        break;
      case 'tool_end':
        // Clear «Trabajando · {cmd}» as soon as the call ends, so it never shows a finished
        // command (2026-09-29 UX audit). A following `tool_start` sets it again.
        this.tray.setDetail('');
        break;
      case 'approval_request':
        this.tray.setDetail(event.command || '');
        // The composer's automatic mode already allowed it: nothing waits for the person.
        if (event.auto) break;
        this._notify(permissionNotification(event, this.project));
        break;
      case 'approval_resolved':
        this.notifier.withdraw(`approval:${event.id}`);
        break;
      case 'permission_request':
        this._notify(componentPermissionNotification(event));
        break;
      case 'permission_resolved':
        this.notifier.withdraw(`permission:${event.id}`);
        break;
      case 'turn_changes':
        this.turnFiles = Number(event.total_files) || 0;
        break;
      case 'service_error':
        this.setState('error');
        this._notify(serviceErrorNotification(event, this.now()));
        this.refreshUsage();
        break;
      case 'rate_limit_near':
        this.refreshUsage();
        break;
      case 'resume_started':
        this.notifier.withdraw('service-error');
        this.setState('working');
        break;
      case 'model_changed':
        // The tray's usage menu names the model and its service (design 2b).
        this.refreshUsage();
        break;
      case 'turn_complete': {
        const files = this.turnFiles;
        this.turnFiles = 0;
        if (event.stop_reason === 'end_turn') {
          this._notify(doneNotification(this.project, files));
        }
        this.refreshUsage();
        break;
      }
      case 'error':
        this.turnFiles = 0;
        this.setState('error');
        break;
      default:
        break;
    }
    if (event.type === 'phase' || event.type === 'turn_complete') this._schedulePoll();
  }

  /**
   * The tray menu's own actions (`tray-menu.js` ids): «Reintentar ahora» and «Cambiar modelo».
   */
  onTrayAction(id) {
    switch (id) {
      case 'retry':
        this.onAction(null, 'retry');
        break;
      case 'change-model':
        this.onAction(null, 'change-model');
        break;
      default:
        break;
    }
  }

  /** Handles a notification or tray button. A spec with its own `onAction` (the updater's
   * «Actualización lista») handles itself. */
  onAction(spec, action) {
    // Never act on a button the notification did not offer. A permission shown cut or
    // incomplete has no Permitir; asking for one anyway only opens the window at the card.
    if ((action === 'allow' || action === 'deny') && !(spec && (spec.actions || []).some((a) => a.id === action))) {
      this.focusWindow();
      this.toRenderer('app:reveal', spec ? spec.reveal : undefined);
      return;
    }
    if (spec && typeof spec.onAction === 'function') {
      try {
        spec.onAction(action);
      } catch (error) {
        this.log(`notification action: ${error.message}`);
      }
      return;
    }
    if ((action === 'allow' || action === 'deny') && spec && spec.permissionId) {
      const decision = action === 'allow' ? 'allow_session' : 'deny';
      Promise.resolve(this.core.answerPermission(spec.permissionId, decision)).catch((error) =>
        this.log(`permission from a notification: ${error.message}`),
      );
    } else if (action === 'allow' || action === 'deny') {
      Promise.resolve(this.core.approve(spec.approvalId, action === 'allow')).catch((error) =>
        this.log(`approve from a notification: ${error.message}`),
      );
    } else if (action === 'retry') {
      Promise.resolve(this.resume()).catch((error) => this.log(`resume: ${error.message}`));
    } else if (action === 'change-model') {
      // «Cambiar modelo»: the picker is the window's model menu.
      this.focusWindow();
      this.toRenderer('app:open-model-menu');
    } else if (action === 'open' || action === 'view-changes') {
      this.focusWindow();
      this.toRenderer('app:reveal', spec ? spec.reveal : undefined);
    }
  }

  // -- usage ------------------------------------------------------------------------------------

  async refreshUsage() {
    try {
      this.tray.setUsage(await this.core.usage());
    } catch (error) {
      this.log(`usage: ${error.message}`);
    }
  }

  _schedulePoll() {
    if (this._stopped) return;
    if (this._poll) this.timers.clearTimeout(this._poll);
    const busy = this.state === 'working' || this.state === 'permission';
    this._poll = this.timers.setTimeout(async () => {
      this._poll = null;
      await this.refreshUsage();
      this._schedulePoll();
    }, busy ? POLL_BUSY_MS : POLL_IDLE_MS);
  }

  /** Once the core is open: the first usage read and polling. */
  async start() {
    this._schedulePoll();
    try {
      const usage = await this.core.usage();
      this.tray.setUsage(usage);
    } catch (error) {
      this.log(`on open: ${error.message}`);
    }
  }

  stop() {
    this._stopped = true;
    for (const timer of [this._poll, this._settle]) if (timer) this.timers.clearTimeout(timer);
    this._poll = null;
    this._settle = null;
  }
}

module.exports = { StatusHub, POLL_BUSY_MS, POLL_IDLE_MS };
