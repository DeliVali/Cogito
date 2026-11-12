'use strict';

/**
 * Which core events become a system notification, with what text and buttons (design 2c),
 * and how it is shown. Pure (no Electron) so `cogito-lab` can read it.
 *
 * The design's rule: notify only what needs the person or closes a piece of work (a
 * permission, a finished task, an error that stops the work). Never every step or tool, and
 * never while the window is in front, where the thread already shows it. A permission stays
 * until answered; the others hide on their own. Text and buttons are the same on every
 * system; only the drawing changes.
 *
 * How it is shown (`chooseBackend`):
 *   Windows, macOS  Electron's `Notification`, whose `actions` both systems support. The
 *                   app's own toast if Electron reports `failed` (macOS 42+ needs a
 *                   code-signed app for UNNotification) or cannot notify at all.
 *   Linux           Electron's libnotify path adds only a `default` action and ignores
 *                   `actions` (its source; checked on KDE Plasma 6.7.5, 2026-09-28). When
 *                   the server has the `actions` capability, every notification goes
 *                   through `notify-send -A`. Without it, one with buttons uses the toast
 *                   and one without uses Electron.
 */

const NOTIFY_COPY = Object.freeze({
  permissionTitle: 'Permiso necesario',
  permissionBody: (project, command) => (project ? `${project} quiere ejecutar ${command}` : command),
  allow: 'Permitir',
  deny: 'Rechazar',
  // A component permission's answers (design 4b's card): «Permitir» / «No», not the guard
  // approval's «Permitir»/«Rechazar» above.
  no: 'No',
  doneTitle: 'Tarea lista',
  // Design 2c: «tienda-api · 3 archivos cambiados». The design's second clause, «12 tests
  // pasan», is never shown: the core never emits `turn_complete.tests_passed`, so there is
  // nothing real to report (2026-09-29 UX audit, INVENTED).
  doneBody: (project, files) => {
    const detail = files > 0 ? `${files} archivo${files === 1 ? '' : 's'} cambiado${files === 1 ? '' : 's'}` : '';
    return detail ? `${project} · ${detail}` : project;
  },
  viewChanges: 'Ver cambios',
  limitTitle: 'Límite de uso alcanzado',
  noResponseTitle: 'La API no respondió',
  // «CLAUDE API · se reanuda automáticamente» while the core has a resume scheduled,
  // «CLAUDE API · la tarea quedó en pausa» when nothing resumes by itself. A notification
  // cannot update itself once shown, unlike the thread's card (`SERVICE_ERROR.resumesIn`,
  // which ticks), so it carries no countdown that would go stale (2026-09-29 UX audit
  // follow-up, "Countdowns tick live").
  errorBody: (service, willResume) => (willResume ? `${service} · se reanuda automáticamente` : `${service} · en pausa`),
  retry: 'Reintentar ahora',
  // A request too large for the model's context window: the card's own title
  // (`SERVICE_ERROR.overflowTitle`, renderer/strings.js) and the one action that can work.
  // Nothing resumes it, so the task «se detuvo», not «quedó en pausa».
  overflowTitle: 'No cabe en el contexto',
  overflowBody: (service) => `${service} · detenido`,
  changeModel: 'Cambiar modelo',
});

/** Ids of the actions that move the work forward. Must stay identical to the copy in the
 * renderer's toast-view.js: both need the same answer to "which one is primary" and cannot
 * share a module (the same CSP reason as every other renderer copy in this app). */
const PRIMARY_ACTIONS = new Set(['allow', 'retry', 'view-changes', 'restart']);

/**
 * `actions` with the primary ones (`PRIMARY_ACTIONS`, matched by `id`, never by position)
 * moved first and the rest after them in their existing order. Used only for macOS, which
 * shows just the first action and tucks the rest under its own «Opciones» (2026-09-29 UX
 * audit follow-up). Every other surface (the card, the tray, Windows, Linux, the toast)
 * keeps the safe-first order. Returns `actions` unchanged when none is primary.
 */
function primaryFirst(actions) {
  const list = Array.isArray(actions) ? actions : [];
  const primary = list.filter((action) => PRIMARY_ACTIONS.has(action.id));
  if (!primary.length) return list;
  const rest = list.filter((action) => !PRIMARY_ACTIONS.has(action.id));
  return [...primary, ...rest];
}

function truncate(text, max = 180) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/**
 * Whether the notification's text shows everything the person would approve: the command
 * exactly as it will run (one line, not cut), with every path it resolved to named in it.
 * A glob, a directory expanded to its files, a path the guard could not resolve, or a
 * command that had to be cut or folded onto one line is approved only in the window, where
 * the card shows all of it.
 */
function approvalShownInFull(request) {
  // A line for a terminal runs in the person's own shell, unsandboxed, in a tab the window
  // shows (and may ask for a password there): answered only in the window.
  if (request.action === 'run_in_terminal') return false;
  const raw = typeof request.command === 'string' ? request.command : '';
  const command = raw.trim();
  if (!command || /[\r\n]/.test(command) || truncate(command) !== command) return false;
  if (Array.isArray(request.unresolved) && request.unresolved.length) return false;
  // `git clean`, `find -delete`: what it destroys is not in the text, so nothing is shown
  // whole (QA 2026-10-11, bug 31).
  if (request.targets_unknown) return false;
  if (Array.isArray(request.within) && request.within.length) return false;
  if (Array.isArray(request.listed) && request.listed.length) return false;
  const targets = Array.isArray(request.targets) ? request.targets : [];
  return targets.every((target) => {
    const name = String(target).split(/[\\/]/).filter(Boolean).pop() || '';
    return name !== '' && command.includes(name);
  });
}

function permissionNotification(request, project) {
  const command = truncate(request.command || request.summary || '');
  // Permitir from outside the window only for what the notification itself shows whole;
  // otherwise its click opens the window at the card (`reveal`).
  const answerable = approvalShownInFull(request);
  return {
    key: `approval:${request.id}`,
    kind: 'permission',
    title: NOTIFY_COPY.permissionTitle,
    body: NOTIFY_COPY.permissionBody(project, command),
    actions: answerable
      ? [
          { id: 'allow', label: NOTIFY_COPY.allow },
          { id: 'deny', label: NOTIFY_COPY.deny },
        ]
      : [],
    sticky: true,
    reveal: 'approval',
    approvalId: request.id,
  };
}

/**
 * A component's permission request (design 4b, «Necesita tu permiso»). Same notification as
 * a guard approval, since design 2c lists one permission notification. The body is the
 * request's own words (tool · what it will do · where). The buttons («Permitir»,
 * «No») appear only for a component already installed. The not-installed variant
 * installs first, and what it installs and its size are on the card, so that one only
 * brings the window forward.
 */
function componentPermissionNotification(request) {
  const installed = request.installed !== false;
  return {
    key: `permission:${request.id}`,
    kind: 'permission',
    title: NOTIFY_COPY.permissionTitle,
    body: truncate([request.name, request.action, request.target].filter(Boolean).join(' · ')),
    // The card's own labels (design 4b, `COMPONENTS.allow`/`.no`), not the guard
    // approval's «Permitir»/«Rechazar»: the two answer different questions (2026-09-29 UX
    // audit, "Consistency").
    actions: installed
      ? [
          { id: 'allow', label: NOTIFY_COPY.allow },
          { id: 'deny', label: NOTIFY_COPY.no },
        ]
      : [],
    sticky: true,
    reveal: 'permission',
    permissionId: request.id,
  };
}

function doneNotification(project, files) {
  return {
    key: 'turn',
    kind: 'done',
    title: NOTIFY_COPY.doneTitle,
    body: NOTIFY_COPY.doneBody(project, files),
    actions: files > 0 ? [{ id: 'view-changes', label: NOTIFY_COPY.viewChanges }] : [],
    sticky: false,
    reveal: 'changes',
  };
}

/** From the core's `service_error`. «se reanuda automáticamente» only while the core has a
 * resume of that turn scheduled (`event.resume`), which it runs itself, window or not. A
 * request too large for the model's context window is never resent as it was, so its
 * notification offers «Cambiar modelo», not «Reintentar ahora». */
function serviceErrorNotification(event, nowMs = Date.now()) {
  if (event.category === 'context_overflow') {
    return {
      key: 'service-error',
      kind: 'error',
      title: NOTIFY_COPY.overflowTitle,
      body: NOTIFY_COPY.overflowBody(event.service || ''),
      actions: [{ id: 'change-model', label: NOTIFY_COPY.changeModel }],
      sticky: false,
      reveal: 'error',
    };
  }
  const dueAt = event.resume ? Date.parse(event.resume.at) : NaN;
  const willResume = Number.isFinite(dueAt) && dueAt - nowMs > 0;
  return {
    key: 'service-error',
    kind: 'error',
    title: event.kind === 'limit' ? NOTIFY_COPY.limitTitle : NOTIFY_COPY.noResponseTitle,
    body: NOTIFY_COPY.errorBody(event.service || '', willResume),
    actions: [{ id: 'retry', label: NOTIFY_COPY.retry }],
    sticky: false,
    reveal: 'error',
  };
}

/** `'electron' | 'notify-send' | 'toast'` for one notification. */
function chooseBackend({ platform, spec, electronSupported = true, electronFailed = false, linuxActions = false }) {
  const hasButtons = spec.actions.length > 0;
  if (platform === 'linux') {
    // When the server takes actions, notify-send is used even for a notification with no
    // buttons: Electron's libnotify path hard-codes an English "View" default action that
    // cannot be relabelled, and notify-send's is ours (`openLabel`, 'Mostrar ventana').
    // 2026-09-29 UX audit, "Consistency".
    if (linuxActions) return 'notify-send';
    return hasButtons ? 'toast' : electronSupported ? 'electron' : 'toast';
  }
  return electronSupported && !electronFailed ? 'electron' : 'toast';
}

module.exports = {
  NOTIFY_COPY,
  primaryFirst,
  permissionNotification,
  approvalShownInFull,
  componentPermissionNotification,
  doneNotification,
  serviceErrorNotification,
  chooseBackend,
};
