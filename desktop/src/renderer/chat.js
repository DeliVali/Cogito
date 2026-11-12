'use strict';

import { Orb } from './orb.js';
import { renderProse } from './markdown.js';
import { startRow, endRow, closeAsInterrupted, withSubagentCall, groupRows, formatElapsed, verbFor, displayText } from './tool-rows.js';
import { renderTurnToolRows, buildSingleRowBlock, buildChangesCard } from './render-tool-rows.js';
import { askCard, pillFor, pillObject, approvalKey, approvalHint } from './decisions.js';
import { permissionModel } from './connections.js';
import { installComponentUi } from './components-ui.js';
import { resolveToPill } from './render-decisions.js';
import {
  rewindLinkLabel,
  buildConfirmModel,
  buildConflictModel,
  buildGraphDownModel,
  graphDownError,
  buildResultModel,
} from './rewind.js';
import { mountGraphChip } from './graph-chip.js';
import { findModel } from './model-status.js';
import { switchFailureView } from './model-picker.js';
import { escapeControlChars } from './sanitize.js';
import { buildServiceErrorCard, buildModelFailureCard, buildStartupFailureCard, settleServiceErrorCard, supersedeServiceErrorCard, offerRetryAfterModelChange } from './render-service-error.js';
import { turnErrorView } from './service-error.js';
import {
  buildSaid,
  buildAnswer,
  buildDaySeparator,
  buildFoldMarker,
  buildBackgroundMarker,
  buildStopLine,
  buildContinuingLine,
  joinedAnswer,
} from './render-thread.js';
import { historyBlocks, dayKey, daySeparator } from './history.js';
import { renderHistory } from './render-history.js';
import { rateNearView, buildRateNearChip } from './rate-limit.js';
import { sfx } from './sfx.js';
import {
  buildRewindLink,
  buildGraphDownLine,
  buildConfirmDialog,
  buildConflictDialog,
  buildResultLine,
} from './render-rewind.js';
import {
  APPROVAL_BUTTONS,
  SHORTCUT_APPROVE,
  ASK_COPY,
  ERROR_COPY,
  COMPOSER,
  STEER,
  STEER_SETTLE_MS,
  WINDOW_CHROME,
  STATUS,
  THREAD,
  REWIND,
  RESET,
  PROJECT_MENU,
  SERVICE_ERROR,
  STOP_COPY,
  OUTPUT_LIMIT_FOLLOWUP,
  CONNECTIONS,
  MODEL_PICKER,
  TERMINAL,
  AUTO_MODE,
} from './strings.js';
import { createProjectsScreen, openFailureNote, folderName } from './projects.js';
import { attachProjectMenu } from './project-menu.js';
import { createWelcome } from './welcome.js';
import { unservedView } from './welcome-view.js';
import { createStartupFailure } from './startup-failure.js';
import { createAttachTray } from './attach-tray.js';
import { createSkillsTray } from './skills-tray.js';
import { createThinkingControl } from './thinking-control.js';
import { createAutoMode } from './auto-mode.js';
import { createJumpLatest } from './jump-latest.js';
import { createResetFlow } from './reset.js';
import { createExplorer } from './explorer.js';
import { createViewer } from './viewer.js';
import { createTerminals } from './terminals.js';
import { createTerminalView } from './terminal-view.js';
import { createBrowsers, replayShortcut } from './browsers.js';
import { createBrowserView } from './browser-view.js';

// Design 1a: the orb anchored at the composer's left, 104px, with its state under it.
const orb = new Orb(document.querySelector('.orb--composer'), 104);
const column = document.querySelector('.column');
const transcript = document.querySelector('.transcript');
const input = document.querySelector('.field textarea');
const send = document.querySelector('.send');
const stop = document.querySelector('.stop');
const attach = document.querySelector('.attach');
// The skills picker's button (skills-tray.js): hidden until a project is open (applyProject).
const skillsButton = document.querySelector('.skills-button');
const contextRing = document.querySelector('.context-ring');
const contextRingFill = contextRing?.querySelector('.context-ring-fill');
const contextRingPct = contextRing?.querySelector('.context-ring-pct');
// r=9 circle circumference, for the fill's stroke-dasharray.
const CONTEXT_RING_C = 2 * Math.PI * 9;
// "Reiniciar contexto" (design 1a): hidden until a project is open (applyProject).
const resetButton = document.querySelector('.reset-context');
// Icon only (the ↻ SVG in index.html); the label is its tooltip and accessible name.
resetButton.title = RESET.buttonTitle;
resetButton.setAttribute('aria-label', RESET.button);
const pip = document.querySelector('.pip');
// The context store's chip: shown only while Neo4j is down or starting (graph-chip.js).
const graphChip = mountGraphChip(document.querySelector('.graph-chip'), window.cogito);
const projectChip = document.querySelector('.project');
// The name inside the chip: a long one ellipsizes there, between the sphere and the ▼.
const projectChipName = projectChip.querySelector('.project-chip-name');
const modelService = document.querySelector('.model-service');
const wordmark = document.querySelector('.wordmark-text');
const titlebar = document.querySelector('.titlebar');
const sendSlot = document.querySelector('.send-slot');
const branchChip = document.querySelector('.branch');
/** Design 2e: «Cerca del límite por minuto · {n} %», beside the model selector while near. */
const rateChip = buildRateNearChip();
document.querySelector('.model').before(rateChip.el);
const modelChipEl = document.querySelector('.model');

/** The service («CLAUDE API») before the model's name, from the catalog's `service`. Empty
 * when the catalog does not say; the service is never guessed. */
function setModelService(service) {
  const text = typeof service === 'string' ? escapeControlChars(service.trim()) : '';
  modelService.textContent = text;
  modelService.hidden = !text;
}

/** The model the chip describes; a switch moves it, and an older model's answer is dropped. */
let chipModel = null;

/** Reads the catalog for the model's service, which the chip shows before its name. */
async function refreshModelChip(label) {
  chipModel = label;
  let model = null;
  try {
    const { groups } = await window.cogito.models();
    model = findModel(groups, label);
  } catch {
    model = null;
  }
  if (chipModel !== label) return; // the model was switched meanwhile
  // The chip reads «CLAUDE API · {model}» (design 1a) once the catalog names the service.
  setModelService(model ? model.service : null);
  thinkingControl.setAvailable(Boolean(model && model.thinking === true));
  modelChipEl.title = label;
}

// Static chrome and composer text comes from strings.js, so the wording lives in one place.
// index.html carries the same text only so the window does not flash English before this
// module runs.
input.placeholder = COMPOSER.placeholder;
input.setAttribute('aria-label', WINDOW_CHROME.message);
send.textContent = COMPOSER.send;
stop.textContent = COMPOSER.stop;
attach.setAttribute('aria-label', COMPOSER.attachLabel);
attach.title = COMPOSER.attachLabel;
document.querySelector('.chrome--close').setAttribute('aria-label', WINDOW_CHROME.close);
document.querySelector('.chrome--min').setAttribute('aria-label', WINDOW_CHROME.minimize);
document.querySelector('.chrome--max').setAttribute('aria-label', WINDOW_CHROME.maximize);
// Tooltips: the frame is drawn here, so the OS gives none. Close does not quit, so its
// tooltip says so.
document.querySelector('.chrome--close').title = WINDOW_CHROME.closeTitle;
document.querySelector('.chrome--min').title = WINDOW_CHROME.minimize;
document.querySelector('.chrome--max').title = WINDOW_CHROME.maximize;

// ---------------------------------------------------------------------------
// Interface sounds (sfx.js). A control that always makes the same sound declares it with
// `data-sfx`; the rest call `sfx()` themselves, since their sound depends on what happened.
// ---------------------------------------------------------------------------
document.addEventListener('click', (event) => {
  const control = event.target instanceof Element ? event.target.closest('[data-sfx]') : null;
  if (control && !control.disabled) sfx(control.dataset.sfx);
});

// ---------------------------------------------------------------------------
// The window's own title bar (design 1a): controls where this platform puts them, and the
// maximise button showing «restore» once maximised. Do not listen for double clicks: the
// bar is a drag region, which Chromium already toggles on double click
// (`WindowEventFilterLinux::OnClickedCaption` on Linux; Windows and macOS do the same), and
// a second toggle would undo the first.
// ---------------------------------------------------------------------------
const maximizeButton = document.querySelector('.chrome--max');
const windowApi = window.cogito.window || {};
titlebar.dataset.platform = windowApi.platform || (/Mac/.test(navigator.platform) ? 'darwin' : 'linux');

function showMaximized(maximized) {
  maximizeButton.classList.toggle('is-maximized', Boolean(maximized));
  const label = maximized ? WINDOW_CHROME.restore : WINDOW_CHROME.maximize;
  maximizeButton.setAttribute('aria-label', label);
  maximizeButton.title = label;
}
windowApi.onMaximizedChange?.(showMaximized);
Promise.resolve(windowApi.state?.())
  .then((state) => showMaximized(Boolean(state && state.maximized)))
  .catch(() => {});

/** «COGITO — {proyecto}» in the bar and as the window's own title. */
function setTitle(project) {
  const title = WINDOW_CHROME.title(project);
  wordmark.textContent = title;
  document.title = title;
}

// ---------------------------------------------------------------------------
// The composer (design 1a): the field grows with its text, up to a few lines, and «Enviar»
// shows only when there is something to send. Its animation is in styles.css (`.send-slot`).
// ---------------------------------------------------------------------------
function syncComposer() {
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
  // Design 1n: an attachment alone is a message too.
  const shown = input.value.trim().length > 0 || (attachTray !== null && attachTray.ready().length > 0);
  if ((sendSlot.dataset.shown === 'true') === shown) return;
  sendSlot.dataset.shown = String(shown);
  // Hidden, it leaves the tab order and the accessibility tree. `.click()` from code (the
  // lab's runners) still reaches it.
  send.tabIndex = shown ? 0 : -1;
  send.setAttribute('aria-hidden', shown ? 'false' : 'true');
}
input.addEventListener('input', syncComposer);

// Design 1n: attachments dropped on the window, pasted, or picked with «+ adjuntar»
// (attach-tray.js), accepted only while the conversation is showing.
// Null while it is being built: building it draws its empty row, which calls `syncComposer`.
let attachTray = null;
/** The file explorer (below); declared first because the tray tells it about its chips. */
let explorer = null;
const explorerPanel = document.querySelector('.explorer');

attachTray = createAttachTray({
  elements: {
    row: document.querySelector('.attach-chips'),
    count: document.querySelector('.attach-count'),
    button: attach,
    input,
    // The drop layer covers the conversation, not the message field or the explorer.
    host: document.querySelector('.thread-area'),
  },
  bridge: window.cogito.attach,
  play: sfx,
  canAttach: () => Boolean(currentProjectRoot) && !document.querySelector('[aria-modal="true"]:not([hidden])'),
  onChange: () => {
    syncComposer();
    if (explorer) explorer.attachmentsChanged();
  },
  // A file dropped on the explorer goes into the project's folder, not into the message.
  ignoreDrag: (event) => event.target instanceof Node && explorerPanel.contains(event.target),
});

// The composer's skills picker (skills-tray.js): the Agent Skills the person activates for
// the next message, each a removable chip like an attachment. App-only, like the whole
// feature; `window.cogito.skills` is absent on a core without the route.
const skillsTray = createSkillsTray({
  elements: {
    button: document.querySelector('.skills-button'),
    row: document.querySelector('.skill-chips'),
    input,
  },
  api: window.cogito.skills || null,
  play: sfx,
  canUse: () => Boolean(currentProjectRoot) && !document.querySelector('[aria-modal="true"]:not([hidden])'),
});

// The composer's thinking control (thinking-control.js): shown for a model whose host takes a
// thinking budget (`refreshModelChip`), sent with each message.
let thinkingStorage = null;
try {
  thinkingStorage = window.localStorage;
} catch {
  thinkingStorage = null;
}
const thinkingControl = createThinkingControl({
  button: document.querySelector('.thinking-button'),
  storage: thinkingStorage,
  play: sfx,
});

// The composer's automatic mode (auto-mode.js): shown with a project, sent to the core when
// it changes, when a project opens and before each message.
const autoMode = createAutoMode({
  button: document.querySelector('.auto-mode'),
  send: async (on) => {
    const answer = await window.cogito.setAuto(on);
    return answer && answer.ok;
  },
  storage: thinkingStorage,
  play: sfx,
});

// ---------------------------------------------------------------------------
// The file explorer and viewer (design 1a «Explorador y visor», 1p). The chat's own
// attachments above stay as they are; the explorer is a view of the project's folder.
// ---------------------------------------------------------------------------

/** What the model changed in the last task, by project-relative path (`turn_changes`). */
let taskChanges = new Map();

function noteTaskChanges(files) {
  taskChanges = new Map((Array.isArray(files) ? files : []).filter((f) => f && f.path).map((f) => [f.path, f]));
}

// Terminal tabs beside «Hilo» (design 2026-11-01): the core's shells, one xterm each.
const terminalView = createTerminalView({ api: window.cogito.terminal, platform: titlebar.dataset.platform });
const threadStage = document.querySelector('.thread-stage');
// Browser tabs beside «Hilo» (design 2026-11-05): main's pages, drawn over the pane's page
// area. Absent where main has no browser.
const browserApi = window.cogito.browser || null;
const browserView = createBrowserView({ api: browserApi || { bounds: () => {} }, play: sfx });

const viewer = createViewer({
  tabsHost: document.querySelector('.file-tabs'),
  pane: document.querySelector('.viewer'),
  api: window.cogito.explorer,
  deps: {
    play: sfx,
    platform: titlebar.dataset.platform,
    renderProse: (el, text) => renderProse(el, text),
    changesFor: (rel) => taskChanges.get(rel) || null,
    onSeeChanges: (rel) => showChangesFor(rel),
    onNewTerminal: () => terminals.openNew(),
    onCloseTerminal: (id) => terminals.close(id),
    terminalElement: (id) => terminalView.element(id),
    onShowTerminal: (id, { focus }) => terminalView.show(id, { focus }),
    onCloseBrowser: (id) => browsers.close(id),
    browserElement: (id) => browserView.element(id),
    onShowBrowser: (id, { focus }) => browserView.show(id, { focus }),
    onHideBrowser: () => browserView.hide(),
  },
});

const browsers = createBrowsers({ api: browserApi, viewer, view: browserView });
if (browserApi) browserApi.onTabs((list) => browsers.apply(list));
// An app shortcut (⌘O, ⌘⇧E) pressed while a page has the keyboard: main hands it back here,
// and the document's own keydown handlers answer it.
if (browserApi && browserApi.onShortcut) browserApi.onShortcut((shortcut) => replayShortcut(shortcut));

const terminals = createTerminals({
  api: window.cogito.terminal,
  viewer,
  view: terminalView,
  // «Ejecutar» on a reply's shell line shows only where a terminal can open.
  onAvailable: (on) => document.body.classList.toggle('has-terminals', on),
  paneSize: () => ({ width: threadStage.clientWidth, height: threadStage.clientHeight - 30 }),
});

/** A reply's ▶ that did not run: the reason in its tooltip for a few seconds. */
const runFailedTimers = new WeakMap();
function flagRunFailed(run, reason) {
  if (!runFailedTimers.has(run)) run.dataset.tip = run.title;
  clearTimeout(runFailedTimers.get(run));
  run.title = reason;
  run.classList.add('is-failed');
  runFailedTimers.set(run, setTimeout(() => {
    runFailedTimers.delete(run);
    run.title = run.dataset.tip;
    delete run.dataset.tip;
    run.classList.remove('is-failed');
  }, 3000));
}

// «Ejecutar» (▶) on a shell block in a reply (markdown.js): one listener for the
// thread, in the capture phase, so the block's header (its expander) does not also toggle.
column.addEventListener(
  'click',
  (event) => {
    const run = event.target && event.target.closest ? event.target.closest('.code-run') : null;
    if (!run || !column.contains(run)) return;
    event.stopPropagation();
    event.preventDefault();
    if (run.disabled) return;
    sfx('click');
    run.disabled = true;
    // Why it did not run, in its tooltip: the core refused the command, no tab opened, or
    // the tab opened and its shell was busy or never ready (`terminal_unsent`, later).
    let reason = TERMINAL.openFailed;
    const onError = (answer) => {
      if (answer && answer.status === 400) reason = TERMINAL.runRefused;
    };
    const onUnsent = (_why, text) => {
      if (run.isConnected) flagRunFailed(run, text);
    };
    terminals.openNew(run.dataset.command, { onError, onUnsent }).then((tab) => {
      run.disabled = false;
      if (!tab) flagRunFailed(run, reason);
    });
  },
  true,
);

const explorerToggle = document.querySelector('.explorer-toggle');
explorer = createExplorer({
  panel: explorerPanel,
  toggle: explorerToggle,
  api: window.cogito.explorer,
  deps: {
    play: sfx,
    platform: titlebar.dataset.platform,
    pathOf: (file) => window.cogito.attach.pathOf(file),
    onOpenFile: (rel) => viewer.open(rel),
    onRemoved: (rel) => viewer.removed(rel),
    onRenamed: (from, to) => viewer.renamed(from, to),
    attachPaths: (paths) => attachTray.addPaths(paths),
    attachedPaths: () => attachTray.paths(),
    changedInTask: (rel) => taskChanges.has(rel),
    onToggle: (open) => document.body.classList.toggle('has-explorer', open),
    onNewTerminal: () => terminals.openNew(),
  },
});

/** «Ver cambios»: back to the thread, at the last changes card, with that file's diff open. */
function showChangesFor(rel) {
  viewer.showThread();
  const cards = column.querySelectorAll('.changes-card');
  const card = cards[cards.length - 1];
  if (!card) return;
  const row = [...card.querySelectorAll('[data-path]')].find((node) => node.dataset.path === rel);
  const target = row || card;
  if (row && row.getAttribute('aria-expanded') === 'false') row.click();
  target.scrollIntoView({ block: 'center' });
}

let running = false;
/** The answer block being built, so all of a turn's parts land in one block. */
let current = null;
/** Guards `turn_complete`/`error` against a duplicate delivery (e.g. a resend after the main
 * process re-subscribes). Neither event carries a turn id, so a duplicate looks like a second
 * turn. Set when a turn starts (`submit()`), cleared once `turn_complete`/`error` is
 * processed; a delivery while it is clear is ignored, so nothing is drawn or counted twice. */
let turnActive = false;
/** The bubble that opened the running turn (`said`'s handle), where a fold at the top of
 * the turn goes. Null between turns. */
let turnOpening = null;
/** The `service_error` the core sends right before the `turn_complete` it explains. */
let pendingServiceError = null;
/** The card of the last turn a provider failure stopped, until the core resumes that turn
 * (`resume_started`) or drops the scheduled resume (`resume_cancelled`). */
let openServiceErrorCard = null;
/** The card of the failure the core is resuming (`resume_started`), kept until that turn ends:
 * if it fails again its new card takes this one's place, so a failing episode shows one card
 * (`supersedeServiceErrorCard`). Cleared when a resumed turn ends without a failure, on an
 * `error`, and when a new turn or a rewind retires the stopped one. */
let episodeServiceErrorCard = null;
/** True while the core has a resume of the stopped turn scheduled: "■ Detener" stays, since
 * it is what cancels it (the core's `/interrupt`). */
let resumePending = false;

/** Approvals still waiting on an `approval_resolved` event, keyed by request id, in the
 * order their cards were created. */
const pendingApprovals = new Map();
/** Every approval id seen in this window's lifetime. `pendingApprovals` drops an id once it
 * resolves, so this set is what catches a duplicate `approval_request` for a resolved id. It
 * also makes reconnection safe: replayed events cannot draw a second card. */
const seenApprovalIds = new Set();

/** Steer messages waiting on `steer_applied`/`steer_dropped`, keyed by the id the core
 * returned when it accepted them (see `saidSteer` and the event switch below). An entry is
 * removed as soon as either event resolves it, so a duplicate delivery finds nothing to do,
 * as with `pendingApprovals`. */
const pendingSteers = new Map();

/** `steer_applied`/`steer_dropped` events for an id not yet in `pendingSteers`, keyed by id.
 * The SSE event (`core:event`) and the reply to `core:steer` travel on separate channels,
 * and the event can arrive first. It waits here until `submit()`'s `window.cogito.steer()`
 * resolves with that id. Dropping it would leave the bubble stuck on STEER.queued, or lose a
 * dropped steer's text. */
const bufferedSteerEvents = new Map();

/** "Volver a un punto": bubbles waiting for the `point_recorded` id that lets them offer a
 * "↺ Volver a este punto" link, in the order this window sent them (the turn's opening
 * message from `submit()`, then each steer from `saidSteer()`). FIFO matching works because
 * the core records points in the same order: the opening message is a turn's first append,
 * and `_apply_steers` drains its queue FIFO (`core/cogito/loop/engine.py`, `_append`/
 * `rewind_point`). */
const pendingRewindLinks = [];

/** Every rewind point id this window has attached a link for, so a duplicate
 * `point_recorded` (e.g. a replay after reconnecting) does not add a second link. */
const rewoundLinksAttached = new Set();

/**
 * Dropped steers go back to the composer in the order they were sent, not the order their
 * IPC acks or `steer_dropped`/`steer_applied` events arrive, which can differ (review
 * app-b2-steer, second round, item 6). `saidSteer` gives each steer a `seq` synchronously,
 * before any IPC round trip, so seq order is send order.
 *
 * A reorder buffer: `highestResolvedSeq` is the largest seq such that every seq up to it is
 * resolved, applied or dropped. Applied steers count too, so a later drop knows nothing
 * earlier is outstanding. A drop's text waits in `pendingDropTexts` until its seq is
 * `<= highestResolvedSeq`; then `flushReadyDrops` writes it to the composer in seq order
 * and forgets it.
 *
 * A slow earlier seq holds later drops back, but only until the turn ends:
 * `settleAnyStillPendingSteers` (on `turn_complete`/`error`) resolves everything left. There
 * is no extra timeout on purpose. The server resolves every accepted steer id exactly once,
 * so the wait is normally one round trip, and a timeout would add another way to get the
 * order wrong.
 */
let nextSteerSeq = 0;
let highestResolvedSeq = -1;
const outOfOrderResolvedSeqs = new Set();
const pendingDropTexts = new Map(); // seq -> text

/**
 * The composer value and its restored part, exactly as `flushReadyDrops` last wrote them.
 * Used only to tell whether anything touched the field since, never to locate or re-parse
 * the person's own text. The check is exact equality: a prefix match treated an edited
 * block as unchanged when the edit kept its start ("CCC" is a prefix of "CCC plus my
 * edit"), and spliced a new drop into the middle of what the person was writing. If the
 * field differs at all, everything in it is treated as the person's own and kept whole.
 */
let lastWrittenComposerValue = null;
let lastRestoredBlock = null;

function markSteerSeqResolved(seq) {
  outOfOrderResolvedSeqs.add(seq);
  while (outOfOrderResolvedSeqs.has(highestResolvedSeq + 1)) {
    highestResolvedSeq += 1;
    outOfOrderResolvedSeqs.delete(highestResolvedSeq);
  }
}

/**
 * Writes every drop no longer blocked by an earlier unresolved steer into the composer, in
 * seq order (see the note above `nextSteerSeq`), and removes it from `pendingDropTexts`.
 *
 * If the field is exactly what this function last wrote (`lastWrittenComposerValue`), the
 * new drops extend the restored block in place, and any text the person had after it stays
 * after it. If the field changed at all, its whole content is treated as the person's and
 * the new block goes in front of it.
 */
function flushReadyDrops() {
  const ready = [...pendingDropTexts.entries()]
    .filter(([seq]) => seq <= highestResolvedSeq)
    .sort(([a], [b]) => a - b);
  if (!ready.length) return;
  for (const [seq] of ready) pendingDropTexts.delete(seq);
  const newBlock = ready.map(([, text]) => text).join('\n\n');

  const intact = lastWrittenComposerValue !== null && input.value === lastWrittenComposerValue;
  const ownTyping = intact ? lastWrittenComposerValue.slice(lastRestoredBlock.length).replace(/^\n\n/, '') : input.value;
  const combinedBlock = intact ? `${lastRestoredBlock}\n\n${newBlock}` : newBlock;

  input.value = ownTyping ? `${combinedBlock}\n\n${ownTyping}` : combinedBlock;
  lastRestoredBlock = combinedBlock;
  lastWrittenComposerValue = input.value;
  syncComposer();
  input.focus();
}

/** The last started call, and the call an open approval gates. A heuristic: an
 * `approval_request` carries no `call_id`, but the core emits `tool_start` strictly before
 * an `approval_request` for the same call, so the most recently started row still running
 * is the best guess. `waitingApprovalCallId` is cleared once the approval resolves. */
let lastStartedCallId = null;
let waitingApprovalCallId = null;

/** Ticks once a second while a tool row of the current turn is running, so its elapsed
 * time advances ("Editando src/checkout.ts · 12 s"). It updates the row's meta text node
 * only: rebuilding the row every second collapsed an expanded read group and reset the
 * scroll. Cleared once nothing is running.
 */
let tickTimer = null;

function atBottom() {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 40;
}

/** «Ir al final» above the composer while the reader is up the thread (jump-latest.js). */
const jumpButton = document.querySelector('.jump-latest');
jumpButton.setAttribute('aria-label', WINDOW_CHROME.jumpLatest);
jumpButton.title = WINDOW_CHROME.jumpLatest;
const jumpLatest = createJumpLatest({
  button: jumpButton,
  dot: jumpButton.querySelector('.jump-latest-dot'),
  transcript,
  reducedMotion: () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true,
  // The button goes once the end is reached; the field is where the person goes next.
  afterJump: () => input.focus({ preventScroll: true }),
});
transcript.addEventListener('scroll', () => jumpLatest.update(), { passive: true });
// A rewind takes messages out and «Rehacer» puts them back without a scroll event, and a
// resize changes what is below the fold: the arrow follows the size, not only the scroll.
if (typeof ResizeObserver === 'function') {
  const sizeWatcher = new ResizeObserver(() => jumpLatest.update());
  sizeWatcher.observe(transcript);
  sizeWatcher.observe(column);
}

/** Only follow the tail when the reader is already there; never yank them back. The
 * person's own send is the exception (`said`, `saidSteer`): sending is asking to see the
 * tail, as in any chat. When the reader is up the thread, «Ir al final» says something new
 * arrived below. */
function keepUp(wasAtBottom) {
  if (wasAtBottom) transcript.scrollTop = transcript.scrollHeight;
  else jumpLatest.markNew();
}

/** Draws «— HOY —» above today's first message. The last day separator in the thread (from
 * history, or one this window drew) says which day the thread has reached. */
function ensureToday() {
  const now = Date.now();
  const separators = column.querySelectorAll('.day-separator');
  const last = separators.length ? separators[separators.length - 1].dataset.day : null;
  if (last === dayKey(now)) return;
  const { key, label } = daySeparator(now, now);
  column.append(buildDaySeparator(key, label));
}

/** Returns `{bubble, text, sentAt}`. `submit()` pushes it onto `pendingRewindLinks`, so the
 * matching `point_recorded` event can attach the message's "↺ Volver a este punto" link. */
function said(text, attached = []) {
  ensureToday();
  const bubble = buildSaid(text, document, attached);
  bubble.dataset.new = '';
  column.append(bubble);
  keepUp(true);
  return { bubble, text, sentAt: Date.now() };
}

/**
 * A steer message's bubble plus its status line (design 1m, "Tu mensaje"): a pulsing
 * "En cola" dot while it waits, "✓ Hecho" once `steer_applied` lands. Returns the handles
 * that the `steer_applied`/`steer_dropped` handlers and `submit()` update.
 */
function saidSteer(text, attached = []) {
  ensureToday();
  const bubble = buildSaid(text, document, attached);
  bubble.dataset.new = '';

  const status = document.createElement('div');
  status.className = 'said-status';
  const dot = document.createElement('span');
  dot.className = 'said-status-dot';
  const label = document.createElement('span');
  label.textContent = STEER.queued;
  status.append(dot, label);

  column.append(bubble, status);
  keepUp(true);
  // Assigned synchronously, before `submit()` awaits the steer's id (see `nextSteerSeq`).
  const seq = nextSteerSeq++;
  // `sentAt`: a steer is also a rewind point once it joins the transcript, and its link
  // shows the time.
  return { text, bubble, status, dot, label, seq, sentAt: Date.now() };
}

/** `steer_applied`: shows STEER.applied, then removes the status line after
 * `STEER_SETTLE_MS` (design 1m). Also resolves the steer's seq: it has no text to restore,
 * but a later drop may be waiting on it.
 *
 * The message joined the conversation here, after everything the turn did so far: its
 * bubble moves to the end of the thread, and what the turn does next starts a new answer
 * below it, as history draws a steer (`history.js`). */
function settleSteerApplied(handle) {
  const wasAtBottom = atBottom();
  const joined = [handle.bubble];
  const link = handle.bubble.nextElementSibling;
  if (link && link !== handle.status && link.classList.contains('rewind-link')) joined.push(link);
  if (handle.bubble.isConnected) column.append(...joined, handle.status);
  if (current) {
    dropLive(current);
    dropThinking(current);
    dropToolCallProgress(current);
    current = null;
  }
  keepUp(wasAtBottom);
  handle.dot.remove();
  handle.label.textContent = STEER.applied;
  handle.status.classList.add('is-done');
  setTimeout(() => handle.status.remove(), STEER_SETTLE_MS);
  markSteerSeqResolved(handle.seq);
  flushReadyDrops();
}

/**
 * `steer_dropped`: the message never reached the transcript, so its bubble is removed and
 * its text queued for `flushReadyDrops` to put back in the composer. It is never resent on
 * its own; server/app.py's module docstring makes the same choice.
 */
function settleSteerDropped(handle) {
  handle.bubble.remove();
  handle.status.remove();
  pendingDropTexts.set(handle.seq, handle.text);
  // Not in the transcript, so no point_recorded will come for it (as in submit()'s
  // IPC-failure path).
  const notJoined = pendingRewindLinks.indexOf(handle);
  if (notJoined !== -1) pendingRewindLinks.splice(notJoined, 1);
  markSteerSeqResolved(handle.seq);
  flushReadyDrops();
}

/** Applies a `steer_applied`/`steer_dropped` event to `handle`. Called from the event
 * switch, and from `submit()` when the event arrived before the id (`bufferedSteerEvents`). */
function settleSteerEvent(handle, event) {
  if (event.type === 'steer_applied') settleSteerApplied(handle);
  else settleSteerDropped(handle);
}

/** Backstop for `turn_complete`/`error`. The server sends `steer_applied` or `steer_dropped`
 * for every accepted steer before either event, but one missed SSE delivery would leave a
 * bubble on STEER.queued forever. Anything still pending when a turn ends is treated as
 * dropped, and its text goes back to the composer. */
function settleAnyStillPendingSteers() {
  for (const handle of pendingSteers.values()) settleSteerDropped(handle);
  pendingSteers.clear();
}

/** The agent's side of a turn: its avatar, and a body that grows as events arrive.
 * `toolRowsByCallId` (callId -> `{ row, segment }`) spans the whole turn; `segment`
 * (`{ wrapper, rows }` or `null`) is only the current row container (see `ensureSegment`/
 * `breakSegment` for why there can be several). `runningMeta` (callId -> the row's meta
 * `<span>`) is what the tick timer updates, across all segments. */
function answer() {
  const wasAtBottom = atBottom();
  const { block, body } = buildAnswer(paintAvatar);
  block.dataset.new = '';
  column.append(block);
  keepUp(wasAtBottom);
  return {
    block,
    body,
    toolRowsByCallId: new Map(),
    runningMeta: new Map(),
    segment: null,
    pendingChangesCard: null,
    // The reply streaming now (`text_delta`), the model's reasoning streaming now, the
    // tool call streaming now (`tool_call_progress`), and the last reply's words drawn
    // (`assistant_text`), which `turn_complete.final_text` repeats.
    live: null,
    thinking: null,
    toolProgress: null,
    lastProse: null,
    // APP-ONLY auto-continue (long-output fix 3). `continuing`: the quiet «Continuando…»
    // line while a reply the output limit cut off continues on its own (`auto_continuing`).
    // `pendingContinue`: the kind ("text" | "call") that line announced, consumed by the
    // next `text_delta`/`assistant_text` so a text continuation joins the paragraph it
    // follows instead of starting a new one. `lastAnswerText`: that paragraph's element
    // and its words so far, for the join.
    continuing: null,
    pendingContinue: null,
    lastAnswerText: null,
  };
}

/** The answer's small sphere, live and in history alike. It is a still CSS sphere: only the
 * composer's orb follows the agent's state, and a long thread does not run one canvas per
 * answer. */
function paintAvatar(element) {
  element.className = 'answer-avatar';
  element.setAttribute('aria-hidden', 'true');
}

function line(body, className, text) {
  const wasAtBottom = atBottom();
  const element = document.createElement('div');
  element.className = className;
  element.textContent = text;
  body.append(element);
  keepUp(wasAtBottom);
  return element;
}

/** Where one reply's words go: an `.answer-text` after everything the turn drew so far.
 * A call made after it starts a new row container below it. */
function addAnswerText(turn) {
  const element = document.createElement('div');
  element.className = 'answer-text';
  element.dataset.new = '';
  turn.body.append(element);
  breakSegment(turn);
  return element;
}

/** The reply streaming for `sample`, started if need be. One from another sample that never
 * settled (a failed attempt, a reply asked again) is dropped first. APP-ONLY auto-continue:
 * when `turn.pendingContinue` is `"text"`, this sample is a continuation with no call in
 * between, so it reuses the last paragraph (`turn.lastAnswerText`) and its words instead of
 * starting a new one -- one answer, not two. */
function liveReply(turn, sample) {
  if (turn.live && turn.live.sample !== sample) dropLive(turn);
  if (!turn.live) {
    const wasAtBottom = atBottom();
    const joining = turn.pendingContinue === 'text' && turn.lastAnswerText;
    const element = joining ? turn.lastAnswerText.element : addAnswerText(turn);
    element.classList.add('is-streaming');
    turn.live = { sample, element, text: joining ? turn.lastAnswerText.text : '', frame: 0 };
    keepUp(wasAtBottom);
  }
  turn.pendingContinue = null;
  return turn.live;
}

function dropLive(turn) {
  if (!turn.live) return;
  if (turn.live.frame) cancelAnimationFrame(turn.live.frame);
  turn.live.element.remove();
  turn.live = null;
}

/** Redraws the streaming reply as Markdown, at most once a frame. */
function paintLive(turn) {
  const live = turn.live;
  if (!live || live.frame) return;
  live.frame = requestAnimationFrame(() => {
    live.frame = 0;
    if (turn.live !== live) return;
    const wasAtBottom = atBottom();
    live.element.replaceChildren();
    renderProse(live.element, live.text);
    keepUp(wasAtBottom);
  });
}

/** How much of the model's reasoning stays in the page while it streams: its tail. */
const THINKING_KEEP = 4000;

/** The model's reasoning while it streams (`text_delta` of kind `thinking`): its last lines
 * under «Razonando…», gone once its reply settles. History never had it. */
function liveThinking(turn, sample, text) {
  if (turn.thinking && turn.thinking.sample !== sample) dropThinking(turn);
  if (!turn.thinking) {
    const wasAtBottom = atBottom();
    const element = document.createElement('div');
    element.className = 'thinking-live';
    element.dataset.new = '';
    const label = document.createElement('div');
    label.className = 'thinking-live-label';
    label.textContent = THREAD.thinking;
    const box = document.createElement('div');
    box.className = 'thinking-live-text';
    const words = document.createElement('span');
    box.append(words);
    element.append(label, box);
    // No new row container: a call drops this block before its row is drawn, so the row
    // joins the rows above it.
    turn.body.append(element);
    turn.thinking = { sample, element, words, text: '' };
    keepUp(wasAtBottom);
  }
  const thinking = turn.thinking;
  thinking.text = (thinking.text + text).slice(-THINKING_KEEP);
  thinking.words.textContent = escapeControlChars(thinking.text);
}

function dropThinking(turn) {
  if (!turn.thinking) return;
  turn.thinking.element.remove();
  turn.thinking = null;
}

/** The live row's text while a tool call streams, before its `tool_start`: the verb table
 * `tool-rows.js` already draws finished rows from, and the `path` once the partial
 * arguments hold it (`tool_call_progress.path`), with the real count of newlines streamed
 * so far -- never an estimate of the finished file. Before `path` is known there is
 * nothing truer to say than which tool is starting. GUESS: no design source. */
function toolProgressLabel(event) {
  if (event.path) {
    const lines = event.lines === 1 ? '1 línea' : `${event.lines} líneas`;
    return `${verbFor(event.name).gerund} ${event.path}… ${lines}`;
  }
  return `Preparando ${event.name}…`;
}

/** The tool call streaming now (`tool_call_progress`), one row at a time per turn, as
 * `liveThinking`/`liveReply` track theirs. Replaced by the ordinary tool row once the call
 * completes (`tool_start` drops this first) or removed with nothing left behind if the
 * output limit cuts the call off and it is refused (`turn_complete`, a new sample's own
 * `text_delta`/`tool_call_progress` drop it too). Real evidence: a write that took about
 * 100 seconds with nothing shown but "pensando" meanwhile. */
function liveToolCall(turn, event) {
  if (turn.toolProgress && (turn.toolProgress.sample !== event.sample || turn.toolProgress.index !== event.index)) {
    dropToolCallProgress(turn);
  }
  if (!turn.toolProgress) {
    const wasAtBottom = atBottom();
    const element = document.createElement('div');
    element.className = 'tool-progress-live';
    turn.body.append(element);
    breakSegment(turn);
    turn.toolProgress = { sample: event.sample, index: event.index, element };
    keepUp(wasAtBottom);
  }
  turn.toolProgress.element.textContent = toolProgressLabel(event);
}

function dropToolCallProgress(turn) {
  if (!turn.toolProgress) return;
  turn.toolProgress.element.remove();
  turn.toolProgress = null;
}

/** APP-ONLY auto-continue: the quiet «Continuando…» line while a cut reply continues on
 * its own, with nothing to click (the person's decision, 2026-10-12: never a button to
 * press for this). Dropped by whatever the next sample draws -- its own text, a tool row,
 * or the turn simply ending. */
function showContinuing(turn, text) {
  if (turn.continuing) {
    if (text) turn.continuing.textContent = text;
    return;
  }
  const wasAtBottom = atBottom();
  const element = buildContinuingLine(document, text);
  turn.body.append(element);
  turn.continuing = element;
  keepUp(wasAtBottom);
}

function dropContinuing(turn) {
  if (!turn.continuing) return;
  turn.continuing.remove();
  turn.continuing = null;
}

/** `assistant_text`: the reply is in the transcript. What streamed for it is drawn again
 * from the whole text; a reply that streamed nothing is drawn now. APP-ONLY auto-continue:
 * `event.continued` (`loop.engine.AssistantText.continued`) means this reply has no call
 * before it and joins the paragraph before it (`turn.lastAnswerText`) instead of starting
 * a new one -- one answer, not two, in the thread and once it settles. */
function settleReply(turn, event) {
  const wasAtBottom = atBottom();
  dropThinking(turn);
  dropContinuing(turn);
  const joining = Boolean(event.continued) && turn.lastAnswerText;
  const text = joining ? joinedAnswer(turn.lastAnswerText.text, event.text) : event.text;
  // A reply that settles to nothing streamed text the window does not keep (a `<tool_call>`
  // shown as its tool row only, `repair.strip_calls`). Clear what streamed for it and leave
  // no empty bubble behind, instead of drawing an empty answer.
  if (!text.trim()) {
    if (turn.live && turn.live.sample === event.sample) dropLive(turn);
    // A reply already settled can be taken back too: the core sends a reply that only
    // repeated its status line back to the model and settles it empty (`Engine.
    // _maybe_answer_after_status_echo`, QA rig 2026-10-16).
    if (turn.lastAnswerText && turn.lastAnswerText.sample === event.sample && !event.continued) {
      turn.lastAnswerText.element.remove();
      turn.lastAnswerText = null;
      turn.lastProse = '';
    }
    keepUp(wasAtBottom);
    return;
  }
  let element;
  if (turn.live && turn.live.sample === event.sample) {
    element = turn.live.element;
    if (turn.live.frame) cancelAnimationFrame(turn.live.frame);
    turn.live = null;
    element.classList.remove('is-streaming');
    element.replaceChildren();
  } else if (joining) {
    dropLive(turn);
    element = turn.lastAnswerText.element;
    // `renderProse` appends; the element already holds the paragraph's earlier words, so
    // it must be cleared before the joined (whole) text is rendered into it again.
    element.replaceChildren();
  } else {
    dropLive(turn);
    element = addAnswerText(turn);
  }
  // Settled: a one-line shell block gets «Ejecutar» (never while it streams).
  renderProse(element, text, document, { runnable: true });
  turn.lastProse = text;
  turn.lastAnswerText = { element, text, sample: event.sample };
  turn.pendingContinue = null;
  keepUp(wasAtBottom);
}

/** The turn's current row container. A card (an approval, the changes summary, an error)
 * starts a new one after it (`breakSegment`), so a call that starts after the card is drawn
 * below it, not in the old container above it. */
function ensureSegment(turn) {
  if (!turn.segment) {
    const wasAtBottom = atBottom();
    const wrapper = document.createElement('div');
    wrapper.className = 'tool-rows';
    turn.body.append(wrapper);
    // `blocks`: callId -> the DOM block of a single (ungrouped) row in this segment, kept in
    // sync by appendSingleRow/replaceSingleRow so one row can be swapped without touching
    // its neighbours. Rebuilding the segment on every event is quadratic (see
    // renderTurnToolRows's doc comment).
    turn.segment = { wrapper, rows: [], blocks: new Map() };
    keepUp(wasAtBottom);
  }
  return turn.segment;
}

function breakSegment(turn) {
  turn.segment = null;
}

/** Rebuilds one segment from its row list. This is the only path that can change grouping
 * (a finished read may join or start a "Leyó N archivos" group), so it is the only one that
 * touches rows other than the one that changed. Refreshes `turn.runningMeta` and
 * `segment.blocks` for this segment's rows. */
function rerenderSegment(turn, segment) {
  const wasAtBottom = atBottom();
  const { runningMeta, blocks } = renderTurnToolRows(segment.wrapper, groupRows(segment.rows), document, Date.now(), {
    waitingCallId: waitingApprovalCallId,
  });
  for (const row of segment.rows) turn.runningMeta.delete(row.callId);
  for (const [callId, metaEl] of runningMeta) turn.runningMeta.set(callId, metaEl);
  segment.blocks = blocks;
  keepUp(wasAtBottom);
}

/** O(1): appends one new running row without touching the others. Safe because a running
 * row is never grouped (only a finished read groups). */
function appendSingleRow(turn, segment, row) {
  const wasAtBottom = atBottom();
  const { block, metaEl } = buildSingleRowBlock(row, document, Date.now(), {
    waitingCallId: waitingApprovalCallId,
  });
  block.dataset.new = '';
  segment.wrapper.append(block);
  segment.blocks.set(row.callId, block);
  if (metaEl) turn.runningMeta.set(row.callId, metaEl);
  else turn.runningMeta.delete(row.callId);
  keepUp(wasAtBottom);
}

/** O(1): swaps one row's block for its updated version, in place. Used for any `tool_end`
 * that cannot change grouping, i.e. anything but a read finishing cleanly. Appends instead
 * when the row has no block yet (a `tool_end` with no `tool_start`). */
function replaceSingleRow(turn, segment, row) {
  const wasAtBottom = atBottom();
  const oldBlock = segment.blocks.get(row.callId);
  const { block, metaEl } = buildSingleRowBlock(row, document, Date.now(), {
    waitingCallId: waitingApprovalCallId,
  });
  // A finished row replaces its running one with the `settle` animation; a new row rises in
  // like anything new in the thread.
  if (oldBlock) {
    block.classList.add('is-settled');
    oldBlock.replaceWith(block);
  } else {
    block.dataset.new = '';
    segment.wrapper.append(block);
  }
  segment.blocks.set(row.callId, block);
  if (metaEl) turn.runningMeta.set(row.callId, metaEl);
  else turn.runningMeta.delete(row.callId);
  keepUp(wasAtBottom);
}

/** A subagent's `tool_start`/`tool_end` (`parent_call_id`), folded into the running
 * `delegate` row that started it: its count of calls and the latest. The row is redrawn in
 * place, without the settle animation of a row that finished. */
function noteSubagentCall(turn, event) {
  const entry = turn.toolRowsByCallId.get(event.parent_call_id);
  if (!entry || entry.row.status !== 'running') return;
  const updated = withSubagentCall(entry.row, event);
  if (updated === entry.row) return;
  const { segment } = entry;
  segment.rows[segment.rows.indexOf(entry.row)] = updated;
  turn.toolRowsByCallId.set(updated.callId, { row: updated, segment });
  replaceSingleRow(turn, segment, updated);
  const block = segment.blocks.get(updated.callId);
  if (block) {
    block.classList.remove('is-settled');
    // An open list of calls follows the newest one.
    const steps = block.querySelector('.tool-sub-steps');
    if (steps) steps.scrollTop = steps.scrollHeight;
  }
}

/** Takes one row out of the turn, and its container when it was the only row there. */
function removeRow(turn, callId) {
  const entry = turn.toolRowsByCallId.get(callId);
  if (!entry) return;
  const { segment } = entry;
  segment.blocks.get(callId)?.remove();
  segment.blocks.delete(callId);
  segment.rows = segment.rows.filter((row) => row.callId !== callId);
  turn.toolRowsByCallId.delete(callId);
  turn.runningMeta.delete(callId);
  if (!segment.rows.length) {
    segment.wrapper.remove();
    if (turn.segment === segment) turn.segment = null;
  }
}

function ensureTicking() {
  if (tickTimer) return;
  tickTimer = setInterval(() => {
    const turn = current;
    if (!turn || turn.runningMeta.size === 0) {
      clearInterval(tickTimer);
      tickTimer = null;
      return;
    }
    const now = Date.now();
    for (const [callId, metaEl] of turn.runningMeta) {
      const entry = turn.toolRowsByCallId.get(callId);
      if (!entry) continue;
      metaEl.textContent = formatElapsed(now - (entry.row.startedAt || now));
    }
  }, 1000);
}

/** Closes every row still `running` when the turn ends without its `tool_end`. It shows as
 * cancelled ("Interrumpido"), like a call the guard cancelled, instead of spinning forever. */
function closeRunningRows(turn) {
  const touched = new Set();
  for (const entry of turn.toolRowsByCallId.values()) {
    if (entry.row.status !== 'running') continue;
    const updated = closeAsInterrupted(entry.row);
    const idx = entry.segment.rows.findIndex((r) => r.callId === entry.row.callId);
    if (idx !== -1) entry.segment.rows[idx] = updated;
    entry.row = updated;
    touched.add(entry.segment);
  }
  turn.runningMeta.clear();
  for (const segment of touched) rerenderSegment(turn, segment);
}

/** Swap a resolved approval card for its pill, in the same spot in the transcript. */
function replaceWithPill(pending, allowed, reason) {
  const wasAtBottom = atBottom();
  const object = pillObject(pending.request);
  const pill = pillFor(allowed, reason, object, pending.request.action);
  // Focus on the card would fall to the page once it is gone: it goes back to the composer.
  const hadFocus = pending.block.contains(document.activeElement);
  resolveToPill(pending.block, pill);
  if (hadFocus) input.focus({ preventScroll: true });
  keepUp(wasAtBottom);
}

/**
 * The permission card. It shows the command as written and the paths it resolved to: the
 * person has to see what will run, not a summary of it. The card turns into a pill when
 * `approval_resolved` arrives, not on its own clicks, so there is one path whether the
 * answer came from this window, another client or the core's timeout.
 */
/**
 * Design 4g: the permission card for an MCP server's tool. «Necesita tu permiso», the
 * server and the tool's title with «Lectura» or «Hace cambios» (what the server declares),
 * where it is sent, the arguments under «Ver detalles», and «No» · «Permitir en esta
 * sesión» · «Permitir». Answered like any approval: the card waits for `approval_resolved`.
 */
function askExternal(body, request) {
  const wasAtBottom = atBottom();
  const model = permissionModel(request);
  const block = document.createElement('div');
  block.className = 'ask ask--external';
  block.tabIndex = 0;
  const createdAt = Date.now();

  const head = document.createElement('div');
  head.className = 'ask-head';
  const dot = document.createElement('span');
  dot.className = 'ask-dot';
  head.append(dot, document.createTextNode(CONNECTIONS.permission));

  const what = document.createElement('div');
  what.className = 'ask-external-what';
  const logo = document.createElement('span');
  logo.className = 'conn-logo ask-external-logo';
  logo.textContent = model.logo;
  const heading = document.createElement('span');
  heading.className = 'ask-external-heading';
  heading.textContent = displayText(model.heading);
  const kind = document.createElement('span');
  kind.className = `conn-kind${model.changes ? ' is-changes' : ''}`;
  kind.textContent = model.kind;
  what.append(logo, heading, kind);

  const sent = document.createElement('div');
  sent.className = 'ask-external-sent';
  sent.textContent = displayText(model.sent);

  const detailsToggle = document.createElement('button');
  detailsToggle.type = 'button';
  detailsToggle.className = 'link-button ask-external-toggle';
  detailsToggle.textContent = CONNECTIONS.showDetails;
  detailsToggle.setAttribute('aria-expanded', 'false');
  const details = document.createElement('div');
  details.className = 'ask-external-details';
  details.hidden = true;
  const args = document.createElement('pre');
  args.className = 'ask-external-args';
  args.textContent = displayText(model.details);
  const target = document.createElement('span');
  target.className = 'ask-external-target';
  target.textContent = displayText(model.detailsTarget);
  details.append(args, target);
  detailsToggle.addEventListener('click', () => {
    const open = details.hidden;
    sfx('click');
    details.hidden = !open;
    detailsToggle.textContent = open ? CONNECTIONS.hideDetails : CONNECTIONS.showDetails;
    detailsToggle.setAttribute('aria-expanded', String(open));
  });

  const deny = document.createElement('button');
  deny.className = 'ask-button';
  deny.type = 'button';
  deny.textContent = CONNECTIONS.no;
  const allow = document.createElement('button');
  allow.className = 'ask-button ask-button--allow';
  allow.type = 'button';
  allow.textContent = CONNECTIONS.allow;
  const shortcuts = document.createElement('span');
  shortcuts.className = 'ask-shortcuts';
  shortcuts.textContent = SHORTCUT_APPROVE;
  const footer = document.createElement('div');
  footer.className = 'ask-footer';
  footer.append(shortcuts, deny, allow);

  // «Permitir» is remembered for this tool from then on (core/cogito/guard/allowed_tools.py).
  const settle = async (allowed) => {
    if (deny.disabled) return;
    deny.disabled = true;
    allow.disabled = true;
    sfx(allowed ? 'select' : 'cancel');
    const { answered, reason } = await window.cogito.approve(request.id, allowed);
    if (!answered) {
      pendingApprovals.delete(request.id);
      block.classList.add('is-settled');
      sent.textContent = `${sent.textContent}\n→ ${ASK_COPY.noLongerWaiting} — ${reason}`;
    }
  };
  deny.addEventListener('click', () => settle(false));
  allow.addEventListener('click', () => settle(true));
  block.addEventListener('focusin', (event) => {
    block.classList.add('is-focused');
    shortcuts.textContent = approvalHint(event.target === block);
  });
  block.addEventListener('focusout', (event) => {
    if (!block.contains(event.relatedTarget)) block.classList.remove('is-focused');
  });
  block.addEventListener('keydown', (event) => {
    const action = approvalKey(event, { onCard: event.target === block, ageMs: Date.now() - createdAt });
    if (action === 'composer') {
      input.focus();
    } else if (action === 'allow') {
      event.preventDefault();
      settle(true);
    } else if (action === 'deny') {
      event.preventDefault();
      settle(false);
    }
  });

  const inner = document.createElement('div');
  inner.className = 'ask-body';
  inner.append(what, sent, detailsToggle, details, footer);
  block.append(head, inner);
  const active = document.activeElement;
  const free = !active || active === document.body || (active === input && !input.value);
  body.append(block);
  pendingApprovals.set(request.id, { block, request, allow, deny });
  keepUp(wasAtBottom);
  if (free) block.focus();
  return block;
}

function ask(body, request) {
  if (request.action === 'external_tool' && request.server) return askExternal(body, request);
  const wasAtBottom = atBottom();
  const block = document.createElement('div');
  block.className = 'ask';
  block.tabIndex = 0;
  const createdAt = Date.now();

  // Design 1a's card: the amber strip, then the command as written and its resolved paths,
  // then what it does.
  const head = document.createElement('div');
  head.className = 'ask-head';
  const dot = document.createElement('span');
  dot.className = 'ask-dot';

  // The question and its lines come from the facts the core sent (`askCard`), in Spanish,
  // and never promise the trash for something the command destroys.
  const card = askCard(request);
  head.append(dot, document.createTextNode(card.title || ASK_COPY.title));
  const command = document.createElement('div');
  command.className = 'ask-command';
  command.textContent = card.lines.join('\n');

  const text = document.createElement('div');
  text.className = 'ask-text';
  // Why the automatic mode still asks (`kept_asking`, `guard.auto` in the core).
  const kept = AUTO_MODE.kept[request.kept_asking];
  text.textContent = kept ? `${card.text}\n${kept}` : card.text;

  const deny = document.createElement('button');
  deny.className = 'ask-button';
  deny.type = 'button';
  deny.textContent = APPROVAL_BUTTONS.deny;

  const allow = document.createElement('button');
  allow.className = 'ask-button ask-button--allow';
  allow.type = 'button';
  // A terminal line's card says what the button does: «Ejecutar».
  allow.textContent = card.allow || APPROVAL_BUTTONS.allow;

  const shortcuts = document.createElement('span');
  shortcuts.className = 'ask-shortcuts';
  shortcuts.textContent = approvalHint(true, request);

  const footer = document.createElement('div');
  footer.className = 'ask-footer';
  footer.append(shortcuts, deny, allow);

  const settle = async (allowed) => {
    if (deny.disabled) return; // already answered; a key and a click can race
    deny.disabled = true;
    allow.disabled = true;
    // The canvas: «Permitir» plays `select`, «Rechazar» `cancel`.
    sfx(allowed ? 'select' : 'cancel');
    const { answered, reason } = await window.cogito.approve(request.id, allowed);
    if (!answered) {
      // The core could not take the answer (already resolved, or the turn ended). No
      // `approval_resolved` will follow, so the outcome is drawn here.
      pendingApprovals.delete(request.id);
      block.classList.add('is-settled');
      text.textContent = `${text.textContent}\n→ ${ASK_COPY.noLongerWaiting} — ${reason}`;
    }
    // Otherwise wait for `approval_resolved`; the core emits it right after answering.
  };

  deny.addEventListener('click', () => settle(false));
  allow.addEventListener('click', () => settle(true));
  // The shortcut hint shows only while the keys can answer: while focus is on the card or
  // inside it. With a button focused only Esc is the card's own, and the hint says so.
  block.addEventListener('focusin', (event) => {
    block.classList.add('is-focused');
    shortcuts.textContent = approvalHint(event.target === block, request);
  });
  block.addEventListener('focusout', (event) => {
    if (!block.contains(event.relatedTarget)) block.classList.remove('is-focused');
  });

  // Enter answers only when the card itself is the keydown's target, never when the key
  // bubbled up from a focused child such as "Rechazar": that button does what its own label
  // says. Esc rejects wherever focus is inside the card, so a Tab to a button does not leave
  // the card deaf (QA 2026-10-13). Do not add a page-wide fallback: acting on the latest
  // approval regardless of focus approved things nobody chose (Enter on a focused "Rechazar",
  // on the message body, on "Detener"; a strict review BLOCKER). Focus must be in the card.
  block.addEventListener('keydown', (event) => {
    // The first half second is ignored: the Enter that sent the message, or a key repeat
    // from typing, must not answer it. Typing that lands on the card (it took focus while
    // the person started a message) belongs in the composer: the key goes there, and Enter
    // then sends, never approves.
    const action = approvalKey(event, { onCard: event.target === block, ageMs: Date.now() - createdAt });
    if (action === 'composer') {
      input.focus();
    } else if (action === 'allow') {
      event.preventDefault();
      settle(true);
    } else if (action === 'deny') {
      event.preventDefault();
      settle(false);
    }
  });

  const inner = document.createElement('div');
  inner.className = 'ask-body';
  if (card.text) inner.append(command, text, footer);
  else inner.append(command, footer);
  block.append(head, inner);
  // The card takes focus when it appears, so the shortcuts it shows answer it, unless the
  // person is busy elsewhere: only an unfocused page or an empty composer gives it up. The
  // 500 ms guard above keeps the Enter that sent the message from answering it.
  const active = document.activeElement;
  const free = !active || active === document.body || (active === input && !input.value);
  body.append(block);
  pendingApprovals.set(request.id, { block, request, allow, deny });
  keepUp(wasAtBottom);
  if (free) block.focus();
  return block;
}

/**
 * The error card (design 1j): bold headline, plain-text detail, red text on an unfilled
 * border, lighter than the amber approval card since it waits on no decision. `detail` is
 * what the core said; only `title` is the app's own copy (strings.js's ERROR_COPY says
 * which wording goes where).
 */
function errorCard(body, title, detail) {
  const wasAtBottom = atBottom();
  const card = document.createElement('div');
  card.className = 'error-card';
  // No headline of the app's own where the design has none: the real error is the card.
  if (title) {
    const heading = document.createElement('div');
    heading.className = 'error-card-title';
    heading.textContent = title;
    card.append(heading);
  }
  if (detail) {
    const text = document.createElement('div');
    text.className = 'error-card-detail';
    text.textContent = detail;
    card.append(text);
  }
  body.append(card);
  keepUp(wasAtBottom);
  return card;
}

/** Appends `turn.pendingChangesCard` and clears it. It is built on `turn_changes` but drawn
 * here, because the design puts the changes card after the final text. Called from
 * `turn_complete` and `error`, since the core emits `turn_changes` right before either. */
function flushChangesCard(turn) {
  if (!turn.pendingChangesCard) return;
  const wasAtBottom = atBottom();
  turn.body.append(turn.pendingChangesCard);
  turn.pendingChangesCard = null;
  keepUp(wasAtBottom);
}

/** "■ Detener" shows while a turn runs or a resume is pending. */
function updateStopVisibility() {
  stop.hidden = !(running || resumePending);
}

/**
 * "Enviar" never changes label (see strings.js's COMPOSER comment). `running` changes the
 * placeholder (design 1m's "corregir el rumbo" copy while busy), whether "■ Detener" shows,
 * and the project chip. The composer is never disabled: typing while busy is how a person
 * steers.
 */
function setRunning(active) {
  running = active;
  updateStopVisibility();
  input.placeholder = active ? COMPOSER.placeholderBusy : COMPOSER.placeholder;
  // The project chip (design 1f): a switch is refused while a turn runs, so the chip is
  // disabled meanwhile.
  projectChip.classList.toggle('is-busy', active);
  projectChip.setAttribute('aria-disabled', active ? 'true' : 'false');
  if (active) projectMenu.close(false, { returnFocus: false });
  syncResetDisabled();
}

/** The project being opened (its path), or null. Meanwhile the chip says «Abriendo …», no
 * switch or message can start, and the composer keeps what is typed. */
let switchingProject = null;
let chipBeforeSwitch = null;
function setSwitching(path) {
  if (path && !switchingProject) chipBeforeSwitch = projectChipName.textContent;
  switchingProject = path || null;
  const busy = Boolean(switchingProject) || running;
  projectChip.classList.toggle('is-busy', busy);
  projectChip.setAttribute('aria-disabled', busy ? 'true' : 'false');
  if (switchingProject) {
    const opening = PROJECT_MENU.opening(folderName(switchingProject));
    projectChipName.textContent = opening;
    input.placeholder = opening;
    return;
  }
  if (chipBeforeSwitch !== null) projectChipName.textContent = chipBeforeSwitch;
  chipBeforeSwitch = null;
  input.placeholder = running ? COMPOSER.placeholderBusy : COMPOSER.placeholder;
}

function setResumePending(pending) {
  resumePending = pending;
  updateStopVisibility();
}

/** The branch chip (1a): hidden when the project is not a git repository or git is missing,
 * so it never shows a blank chip or an earlier project's branch. */
function setBranch(branch) {
  if (branch) {
    branchChip.textContent = `⎇ ${branch}`;
    branchChip.hidden = false;
  } else {
    branchChip.hidden = true;
    branchChip.textContent = '';
  }
}

/**
 * The header's context chip, «contexto {n} %»: how full the served model's context window
 * is, from the core's `context_status` (a live turn's SSE event, or the session's last one
 * on open or switch, from `GET /sessions/{id}/history`).
 *
 * With no `status`, or a `percent` that is not a number (the model's context size is
 * unknown), the chip is hidden instead of showing 0.
 */
function setContextStatus(status) {
  if (!contextRing) return;
  const percent = status && typeof status.percent === 'number' ? status.percent : null;
  if (percent === null) {
    contextRing.hidden = true;
    return;
  }
  contextRing.hidden = false;
  // The ring fills clockwise; clamp the drawn fraction to the circle even past 100 %.
  const fraction = Math.max(0, Math.min(1, percent / 100));
  if (contextRingFill) {
    contextRingFill.style.strokeDasharray = `${CONTEXT_RING_C}`;
    contextRingFill.style.strokeDashoffset = `${CONTEXT_RING_C * (1 - fraction)}`;
  }
  if (contextRingPct) contextRingPct.textContent = `${percent} %`;
  // Over the window (a switch to a smaller model): the next turn folds first, or stops.
  // Near it (80 %): amber, the design's colour for something to watch.
  const over = percent > 100;
  contextRing.classList.toggle('is-over', over);
  contextRing.classList.toggle('is-near', !over && percent >= 80);
  const detail = `${over ? `${STATUS.contextOver} · ` : ''}contexto ${percent} % · ${status.used_tokens} de ${status.window_tokens} tokens de la ventana del modelo`;
  contextRing.title = detail;
  contextRing.setAttribute('aria-label', detail);
}

/** Shows the per-minute chip if the limit is already near when a project opens (design 2e).
 * Read on every open or switch, since the rate limiter belongs to the served model. */
function refreshRateLimit() {
  Promise.resolve(window.cogito.usage?.())
    .then((snapshot) => {
      const near = snapshot && snapshot.active ? snapshot.active.rate_limit : null;
      if (near) rateChip.update(rateNearView(near));
    })
    .catch(() => {});
}

/** The provider-failure card (design 1j), shown instead of the harness's English stop text.
 * "Reintentar ahora" asks the core to continue the same turn (`POST /resume`), so the
 * instruction is never sent twice. The card counts down only when the core will resume on
 * its own at the provider's `retry-after`. */
function serviceErrorCard(body, failure) {
  const wasAtBottom = atBottom();
  const card = buildServiceErrorCard(failure, {
    onChangeModel: () => componentUi.openModelMenu(),
    onRetry: () =>
      window.cogito.resume().catch((error) => {
        // Nothing left to resume (the core restarted, or a message replaced the turn):
        // the card has nothing more to offer, and says so instead of going quiet.
        settleServiceErrorCard(card, { resumed: true });
        if (!card.querySelector('.service-error-gone')) {
          const note = document.createElement('div');
          note.className = 'service-error-note service-error-gone';
          note.textContent = SERVICE_ERROR.notResumable;
          card.append(note);
        }
        if (openServiceErrorCard === card) openServiceErrorCard = null;
        sfx('cancel');
        console.warn(`resume: ${error.message}`);
      }),
  });
  openServiceErrorCard = card;
  body.append(card);
  keepUp(wasAtBottom);
}

/** A new turn or a rewind replaced the turn a failure card stopped, and the core drops a
 * stopped turn then. The card stays as the record of the failure, without its buttons, so
 * a later model switch cannot offer «Reintentar» on it again (QA 2026-10-11 bugs 53, 64). */
function retireServiceErrorCard() {
  if (openServiceErrorCard) settleServiceErrorCard(openServiceErrorCard, { resumed: true });
  openServiceErrorCard = null;
  episodeServiceErrorCard = null;
}

/** Where a notification's click lands: the newest card of that kind. */
function reveal(what) {
  const selector = {
    approval: '.ask',
    permission: '.cc-card--amber:not(.cc-card--quiet)',
    changes: '.changes-card',
    error: '.service-error, .error-card',
  }[what];
  const found = selector ? column.querySelectorAll(selector) : [];
  if (found.length) found[found.length - 1].scrollIntoView({ block: 'center' });
}

/** Places a component, program or permission card (components-ui.js): in the running turn's
 * answer, starting a new row segment as the approval card does, or in a block of its own
 * when no turn is running (e.g. the project scan's cards). */
function placeCard(card) {
  const wasAtBottom = atBottom();
  if (!current && turnActive) current = answer();
  if (current) {
    current.body.append(card);
    breakSegment(current);
  } else {
    answer().body.append(card);
  }
  keepUp(wasAtBottom);
}

const componentUi = installComponentUi({
  placeCard,
  modelChip: document.querySelector('.model'),
  api: window.cogito.components,
  // Design 1a and 4e: the MCP servers, «⧉ N conexiones» in the bar.
  connectionsApi: window.cogito.connections || null,
  // Habilidades in «Herramientas y permisos».
  skillsApi: window.cogito.skills || null,
  // Diseño in «Herramientas y permisos»: the theme.
  appearanceApi: window.cogito.appearance || null,
  connectionsChip: document.querySelector('.connections-chip'),
  projectName: () => (document.querySelector('.project-chip-name')?.textContent || '').trim(),
  // The picker reads the catalog and asks for a switch.
  modelApi: window.cogito,
  picker: {
    // Nothing switches the model a turn is using: every model is disabled meanwhile.
    isBusy: () => turnActive || running,
    onFailed: showModelFailure,
    // A model served after a failed start: open the project that failed.
    onSwitched: (answer) => startupFailure.served(answer),
  },
  // Design 1a/1c: «Conectar otro servicio…» opens the connection screen (1e).
  onConnect: () => openConnectScreen(),
});

/** A model the picker could not switch to: the service-error card's style, the real reason,
 * «Cambiar modelo». The model that was serving still is, so the chip does not move. */
function showModelFailure(answer) {
  const view = switchFailureView(answer);
  if (!view) return;
  placeCard(buildModelFailureCard(view, { onChangeModel: () => componentUi.openModelMenu() }));
}

/** True when `GET /settings/welcome`'s `unserved` says the model the core started with
 * failed to start. The model stays selected and the error shows where the model is, not on
 * the connect screen, which would read as if no model existed. */
function isStartupFailure(unserved) {
  return Boolean(unserved && unserved.reason === 'failed' && unserved.model && unserved.model.id);
}

/** The card for `isStartupFailure`: the core's log and «Reintentar». A retry or a picker
 * switch that serves a model opens the project that failed (`startup-failure.js`). */
const startupFailure = createStartupFailure({
  activate: (modelId) => window.cogito.connect.activate(modelId),
  placeCard,
  buildCard: buildStartupFailureCard,
  viewOf: (unserved) => unservedView(unserved, MODEL_PICKER),
  settle: (card) => settleServiceErrorCard(card, { resumed: true }),
  supersede: supersedeServiceErrorCard,
  onServed: (answer, path) => {
    // No project is open, so no stream brings `model_changed`: the header follows the answer.
    const model = answer && answer.model;
    if (model && model.id) {
      document.querySelector('.model-name').textContent = escapeControlChars(String(model.label || model.id));
      setModelService(model.service || null);
      refreshModelChip(model.id);
    }
    pip.classList.remove('is-down');
    if (path) projectsScreen.reopen(path);
    else openApp();
  },
});

window.cogito.onEvent((event) => {
  switch (event.type) {
    case 'phase':
      orb.followPhase(event.phase);
      break;

    // The model's words as it writes them, then each reply whole once it is in the
    // transcript (`server/app.py`). A sample that never settles was a failed attempt.
    case 'text_delta': {
      if (!turnActive || typeof event.text !== 'string' || !event.text) break;
      if (!current) current = answer();
      if (event.kind === 'thinking') {
        dropContinuing(current);
        liveThinking(current, event.sample, event.text);
        break;
      }
      if (current.thinking && current.thinking.sample !== event.sample) dropThinking(current);
      if (current.toolProgress && current.toolProgress.sample !== event.sample) dropToolCallProgress(current);
      dropContinuing(current);
      const live = liveReply(current, event.sample);
      live.text += event.text;
      paintLive(current);
      orb.set('responding');
      break;
    }

    // A tool call is still streaming (`provider.base.ToolCallDelta`, throttled to about 4
    // a second): the live row `tool_start` will replace once the call completes, or that
    // simply disappears if the output limit cuts the call off and it is refused.
    case 'tool_call_progress': {
      if (!turnActive || typeof event.name !== 'string' || !event.name) break;
      if (!current) current = answer();
      dropThinking(current);
      dropContinuing(current);
      liveToolCall(current, event);
      break;
    }

    case 'assistant_text': {
      if (!turnActive || typeof event.text !== 'string') break;
      if (!current) current = answer();
      settleReply(current, event);
      break;
    }

    // APP-ONLY auto-continue (long-output fix 3): a reply the output limit cut off
    // continues on its own instead of stopping on «Continuar». Nothing to click while it
    // does -- only this quiet line, dropped by whatever the next sample draws.
    case 'auto_continuing': {
      if (!turnActive) break;
      if (!current) current = answer();
      current.pendingContinue = event.kind === 'call' ? 'call' : 'text';
      showContinuing(current);
      break;
    }

    // APP-ONLY: the Cogito Motoris the core started stopped under a request; the core
    // starts it again and asks again. Sent to every window, drawn only where a turn runs,
    // as the same quiet line, dropped by whatever the next sample draws or the turn ending.
    case 'model_restarting': {
      if (!turnActive) break;
      if (!current) current = answer();
      showContinuing(current, THREAD.restarting);
      break;
    }

    // A fold replaced the conversation with its summary: the thin rule history draws there,
    // inside the answer mid-turn, or before the turn's message when it opened the turn.
    case 'compacted': {
      const wasAtBottom = atBottom();
      const marker = buildFoldMarker(document);
      marker.dataset.new = '';
      if (current) {
        current.body.append(marker);
        breakSegment(current);
      } else if (turnOpening && turnOpening.bubble.isConnected) {
        turnOpening.bubble.before(marker);
      } else {
        column.append(marker);
      }
      keepUp(wasAtBottom);
      break;
    }

    case 'approval_request': {
      // A duplicate id (a resend, or a replay after reconnecting) draws nothing. Checked
      // against every id ever seen, so a duplicate after resolution cannot leave a second
      // card with live buttons (strict review, item 7).
      if (seenApprovalIds.has(event.id)) break;
      seenApprovalIds.add(event.id);
      if (!current) current = answer();
      // Best guess at the call this approval concerns (see `waitingApprovalCallId`). Its
      // row moves below the card, so the decision reads before what came of it.
      let gated = null;
      if (lastStartedCallId != null) {
        const entry = current.toolRowsByCallId.get(lastStartedCallId);
        if (entry && entry.row.status === 'running') {
          // Allowed by the automatic mode: nothing waits, no `approval_resolved` follows.
          if (!event.auto) waitingApprovalCallId = lastStartedCallId;
          gated = entry.row;
          removeRow(current, gated.callId);
        }
      }
      if (event.auto) {
        // The automatic mode answered: the pill «Automático» where the card would stand.
        const wasAtBottom = atBottom();
        const spot = document.createElement('div');
        current.body.append(spot);
        resolveToPill(spot, pillFor(true, 'auto', pillObject(event), event.action));
        keepUp(wasAtBottom);
      } else {
        ask(current.body, event);
      }
      breakSegment(current);
      if (gated) {
        const segment = ensureSegment(current);
        segment.rows.push(gated);
        current.toolRowsByCallId.set(gated.callId, { row: gated, segment });
        appendSingleRow(current, segment, gated);
      }
      break;
    }

    case 'approval_resolved': {
      const pending = pendingApprovals.get(event.id);
      // No pending card: already settled locally (ask()'s `!answered` path), or a request
      // this window never saw. Nothing to replace.
      if (!pending) break;
      pendingApprovals.delete(event.id);
      replaceWithPill(pending, event.allowed, event.reason);
      if (current && waitingApprovalCallId != null) {
        const entry = current.toolRowsByCallId.get(waitingApprovalCallId);
        waitingApprovalCallId = null;
        if (entry && entry.row.status === 'running') rerenderSegment(current, entry.segment);
      }
      break;
    }

    case 'tool_start': {
      // A subagent's call is drawn inside its `delegate` row, never as one of the turn's.
      if (event.parent_call_id) {
        if (current) noteSubagentCall(current, event);
        break;
      }
      if (!current) current = answer();
      // A reply that was only reasoning and calls: its reasoning has done its job.
      dropThinking(current);
      // The call's own row replaces its live progress row, if it had one.
      dropToolCallProgress(current);
      // APP-ONLY auto-continue: a call's row sits between two paragraphs, so a later one
      // must never join the paragraph from before it (it never asks to: the core resets
      // its own join state the same way, `loop.engine._continue`'s `continued_text`).
      dropContinuing(current);
      current.lastAnswerText = null;
      // A duplicate tool_start for a known id is ignored: one call never gets two rows.
      if (current.toolRowsByCallId.has(event.call_id)) break;
      const segment = ensureSegment(current);
      const row = startRow(event);
      segment.rows.push(row);
      current.toolRowsByCallId.set(row.callId, { row, segment });
      lastStartedCallId = row.callId;
      appendSingleRow(current, segment, row);
      ensureTicking();
      break;
    }

    // A subagent answered while others of the same step still work: its row says so until
    // they all end.
    case 'subagent_done': {
      if (current) noteSubagentCall(current, event);
      break;
    }

    case 'tool_end': {
      if (event.parent_call_id) {
        if (current) noteSubagentCall(current, event);
        break;
      }
      if (!current) current = answer();
      if (event.status === 'skipped') {
        // The build gate found nothing a compiler applies to: no «Compiló» row is left.
        removeRow(current, event.call_id);
        break;
      }
      const entry = current.toolRowsByCallId.get(event.call_id);
      // A tool_end for a row no longer 'running' was already applied once; ignore it.
      if (entry && entry.row.status !== 'running') break;
      const updated = endRow(entry ? entry.row : undefined, event);
      const segment = entry ? entry.segment : ensureSegment(current);
      if (entry) {
        segment.rows[segment.rows.indexOf(entry.row)] = updated;
      } else {
        segment.rows.push(updated);
      }
      current.toolRowsByCallId.set(updated.callId, { row: updated, segment });
      // Only a cleanly finished read can change grouping (it may join or start a "Leyó N
      // archivos" group), so only it rebuilds the segment. Failed or cancelled reads are
      // never grouped (tool-rows.js `groupRows`).
      if (updated.name === 'read' && updated.status === 'done') {
        rerenderSegment(current, segment);
      } else {
        replaceSingleRow(current, segment, updated);
      }
      break;
    }

    case 'turn_changes': {
      noteTaskChanges(event.files);
      if (!current) current = answer();
      current.pendingChangesCard = buildChangesCard(event);
      breakSegment(current);
      break;
    }

    // `steer_queued` is not handled: `submit()` drew the "En cola" bubble before sending
    // (`saidSteer`). The event is for another client watching the same session, or a replay.
    case 'steer_applied':
    case 'steer_dropped': {
      const handle = pendingSteers.get(event.id);
      if (handle) {
        pendingSteers.delete(event.id);
        settleSteerEvent(handle, event);
        break;
      }
      // Not registered yet: the event beat the IPC reply. Kept so `submit()` can apply it
      // once it learns the id (see `bufferedSteerEvents`). An id this window never queued
      // (another client's, a stale replay) leaves one entry nothing will look up.
      bufferedSteerEvents.set(event.id, event);
      break;
    }

    case 'point_recorded': {
      const pending = pendingRewindLinks.shift();
      if (pending) attachRewindLink(pending, event.id);
      break;
    }

    case 'rewound': {
      // Nothing to do: this window draws its own rewinds from the reply to
      // `window.cogito.rewind(...)` (`performRewind`). The event is for another client or a
      // replay; a rewind another client performed is not shown here.
      break;
    }

    case 'turn_complete': {
      // A second turn_complete with no new turn since (a replay or a resend) is ignored.
      if (!turnActive) break;
      // The file on screen may be one the model just changed.
      if (viewer.isShowingFile()) viewer.refreshActive();
      turnActive = false;
      turnOpening = null;
      if (!current) current = answer();
      closeRunningRows(current);
      dropLive(current);
      dropThinking(current);
      dropToolCallProgress(current);
      dropContinuing(current);
      const failure = event.stop_reason === 'provider_failure' ? pendingServiceError : null;
      pendingServiceError = null;
      // Ended before finishing: «Detenido», with why unless «Detener» stopped it, never the
      // core's English explanation and never «Listo» (QA 2026-10-11 bugs 20 and 59). The
      // core's `final_text` for such a stop is that explanation, for the log.
      const stopped = !failure && typeof event.stop_reason === 'string' && event.stop_reason !== 'end_turn';
      if (failure) current.block.classList.add('failed');
      else if (stopped) current.block.classList.add('stopped');
      else if (event.final_text && event.final_text.trim() !== (current.lastProse || '').trim()) {
        // A core that sends no `assistant_text`: the last word, as before.
        settleReply(current, { sample: null, text: event.final_text });
      }
      flushChangesCard(current);
      // The episode's earlier card (this turn is a resume of it) gives way to this one; a
      // resume that did not fail leaves it settled above the answer.
      if (episodeServiceErrorCard && failure) supersedeServiceErrorCard(episodeServiceErrorCard);
      episodeServiceErrorCard = null;
      if (failure) serviceErrorCard(current.body, failure);
      // «Detener» before anything was drawn leaves nothing but the meta line: say what
      // happened, as every other stop does, instead of an empty bubble.
      const silentStop = stopped && event.stop_reason === 'user_interrupt' && current.body.children.length === 0;
      if (stopped && (event.stop_reason !== 'user_interrupt' || silentStop)) {
        const copy = silentStop ? STOP_COPY.user_interrupt_silent : STOP_COPY[event.stop_reason] || STOP_COPY.other;
        const wasAtBottom = atBottom();
        // «Continuar» (long-output fix 2): only on an output-limit stop, and only while
        // nothing else is running -- the same guard `sendFollowUp` itself checks. Which
        // follow-up it sends is the core's own fact (`event.cut`), not guessed here.
        const action =
          event.stop_reason === 'output_limit'
            ? {
                label: 'Continuar',
                onClick: () =>
                  sendFollowUp(OUTPUT_LIMIT_FOLLOWUP[event.cut] || OUTPUT_LIMIT_FOLLOWUP.call),
              }
            : null;
        current.body.append(buildStopLine({ kind: 'stopped', text: copy(event.steps), action }));
        keepUp(wasAtBottom);
      }
      const when = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
      if (!failure) line(current.body, 'meta', `${stopped ? STATUS.turnStopped : STATUS.turnDone} · ${when}`);
      setResumePending(Boolean(failure && failure.resume));
      current = null;
      waitingApprovalCallId = null;
      lastStartedCallId = null;
      settleAnyStillPendingSteers();
      // The phase event ("done") already set the orb (orb.js's settle timer), except for a
      // turn the provider stopped, which ends red.
      setRunning(false);
      if (failure) orb.set('error');
      else if (stopped) orb.set('idle');
      // The canvas' outcome sounds: `done` for a finished turn, `close` for one that stopped.
      sfx(failure || stopped ? 'close' : 'done');
      break;
    }

    case 'service_error':
      if (turnActive) pendingServiceError = event;
      break;

    // The core continues the stopped turn (at the provider's retry-after, or after
    // "Reintentar ahora" here, in the tray or in a notification): the card settles and the
    // window runs a turn again, with no new message.
    // APP-ONLY: a command the shell moved to the background ended. A line says so where
    // the thread is; the model reads the report at its next step, or in the turn the core
    // wakes for it (`wake_started`).
    case 'background_finished': {
      const wasAtBottom = atBottom();
      const marker = buildBackgroundMarker(event, document);
      marker.dataset.new = '';
      if (current) {
        current.body.append(marker);
        breakSegment(current);
      } else {
        column.append(marker);
      }
      keepUp(wasAtBottom);
      break;
    }

    // APP-ONLY: the core started a turn for that report, with no message from the person.
    case 'wake_started': {
      if (turnActive) break;
      turnActive = true;
      current = null;
      setRunning(true);
      orb.set('thinking');
      break;
    }

    case 'resume_started': {
      if (turnActive) break;
      setResumePending(false);
      if (openServiceErrorCard) settleServiceErrorCard(openServiceErrorCard, { resumed: true });
      episodeServiceErrorCard = openServiceErrorCard;
      openServiceErrorCard = null;
      turnActive = true;
      current = null;
      setRunning(true);
      orb.set('thinking');
      break;
    }

    // Terminal tabs (terminals.js): opened, output, state at each prompt, shell ended.
    case 'terminal_opened':
    case 'terminal_data':
    case 'terminal_status':
    case 'terminal_exited':
    case 'terminal_unsent':
      terminals.handle(event);
      break;

    case 'model_changed': {
      // The core serves another model (from any window's picker): the chip and its mark
      // follow, and the thread stays. The rate and context chips described the old model,
      // so they are cleared; the core sends the context figure against the new window
      // right after (`context_status`).
      rateChip.update(rateNearView(null));
      setContextStatus(null);
      // A turn stopped because its request did not fit the old model's window resumes on
      // this one, whose window may take it: its card offers «Reintentar» again.
      if (openServiceErrorCard) offerRetryAfterModelChange(openServiceErrorCard);
      const model = event.model || {};
      const name = model.label || model.id;
      if (name) document.querySelector('.model-name').textContent = escapeControlChars(String(name));
      setModelService(model.service || null);
      // The control described the old model: hidden until the catalog says the new one takes it.
      thinkingControl.setAvailable(false);
      if (model.id) refreshModelChip(model.id);
      // The core says which terminal tabs outlived the switch; the rest show as ended.
      terminals.refresh();
      break;
    }

    // Design 2e: near the per-minute limit, and back.
    case 'rate_limit_near':
      rateChip.update(rateNearView(event));
      break;

    // Design 1a: how full the served model's context window is, after every step of a
    // live turn.
    case 'context_status':
      setContextStatus(event);
      break;

    // "Reiniciar contexto" (design 1a): the core began a fresh conversation in this
    // project. Clear the thread, the orb and the chip; a null `context_status` follows.
    case 'thread_reset':
      resetThreadView();
      break;

    case 'resume_cancelled':
      // No countdown any more; "Reintentar ahora" stays for the person to use.
      setResumePending(false);
      if (openServiceErrorCard) settleServiceErrorCard(openServiceErrorCard, { resumed: false });
      break;

    case 'error': {
      if (!turnActive) break; // same idempotency as turn_complete
      turnActive = false;
      turnOpening = null;
      pendingServiceError = null;
      episodeServiceErrorCard = null;
      if (!current) current = answer();
      closeRunningRows(current);
      dropLive(current);
      dropThinking(current);
      dropToolCallProgress(current);
      current.block.classList.add('failed');
      flushChangesCard(current);
      {
        const view = turnErrorView(event);
        errorCard(current.body, view.title, view.detail);
      }
      current = null;
      waitingApprovalCallId = null;
      lastStartedCallId = null;
      settleAnyStillPendingSteers();
      orb.set('error');
      setRunning(false);
      sfx('close');
      break;
    }

    // Components, programs, permissions and install progress (components-ui.js). An event
    // type this build does not know is ignored, never thrown.
    default:
      componentUi.handleEvent(event);
      break;
  }
});

// ---------------------------------------------------------------------------
// "Volver a un punto" (rewind): the files and the conversation
// ---------------------------------------------------------------------------

/** Attaches the "↺ Volver a este punto · HH:MM" link right after `pending.bubble`, once
 * `point_recorded` has given the point's id (see `pendingRewindLinks`). */
function attachRewindLink(pending, pointId) {
  if (rewoundLinksAttached.has(pointId)) return;
  rewoundLinksAttached.add(pointId);
  const wasAtBottom = atBottom();
  const link = buildRewindLink(rewindLinkLabel(pending.sentAt), () => {
    startRewind(pointId, pending.text, pending.sentAt, link);
  });
  pending.bubble.after(link);
  keepUp(wasAtBottom);
}

/** Puts `text` into the composer without discarding what is already there: an empty field
 * gets `text`, otherwise it is appended after a blank line. Focuses the field with the
 * caret at the end. */
function putInComposerWithoutClobbering(text) {
  const existing = input.value;
  input.value = existing.trim() ? `${existing}\n\n${text}` : text;
  syncComposer();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

/** A screen reader's one sentence about a change it should not read out whole. */
const threadStatus = document.querySelector('.thread-status');

function announce(text) {
  if (!threadStatus) return;
  // Emptied first, so the same sentence twice is still said twice.
  threadStatus.textContent = '';
  setTimeout(() => {
    threadStatus.textContent = text;
  }, 30);
}

/** Runs `change` with the thread marked busy, so the log does not read every node a redo
 * puts back; `announce` says what happened instead. */
function quietly(change) {
  column.setAttribute('aria-busy', 'true');
  try {
    return change();
  } finally {
    column.removeAttribute('aria-busy');
  }
}

/** Result lines whose «Rehacer» is still offered. A new turn makes every rewind final (the
 * core answers «conversation moved on»), so it retracts them all. */
const redoLines = new Set();

function retractRedos() {
  for (const line of redoLines) line.retractRedo();
  redoLines.clear();
}

/** Takes the thread from `bubble` on out of the page and keeps it, so «Rehacer» can put it
 * back. The result line `line` stays; `bubble` and everything after `line` are taken.
 * Returns `{bubble, after}`, or null (taking nothing) when `bubble` is not a message. */
function takeThreadFrom(bubble, line) {
  // `.said-group`: a message with its attachments (design 1n), which goes back whole.
  const isMessage = bubble && bubble.classList && (bubble.classList.contains('said') || bubble.classList.contains('said-group'));
  if (!isMessage) return null;
  const after = [];
  let node = line.nextSibling;
  while (node) {
    const next = node.nextSibling;
    after.push(node);
    node.remove();
    node = next;
  }
  bubble.remove();
  return { bubble, after };
}

/** Everything a page of history drew with a «Rehacer» still offered joins `redoLines`. */
function trackRedoLines(nodes) {
  for (const node of nodes) {
    if (typeof node.retractRedo === 'function' && node.querySelector?.('.rewind-redo')) {
      redoLines.add(node);
    }
  }
}

/** «Rehacer» on a rewind read from history. The messages it hid were never in the page, so
 * once the core brings them back the thread is reloaded quietly and one sentence is
 * announced. If the core answers «moved on», «Rehacer» is just removed. */
function redoFromHistory(redoId, line) {
  setRewindBusy(true);
  return window.cogito
    .redo(redoId)
    .then((answer) => {
      if (answer && answer.graphDown) {
        graphChip.noteDown();
        throw graphDownError();
      }
      redoLines.delete(line);
      if (answer && answer.movedOn) {
        line.retractRedo();
        return undefined;
      }
      column.setAttribute('aria-busy', 'true');
      return reloadThread().finally(() => {
        column.removeAttribute('aria-busy');
        input.focus();
        announce(REWIND.redoneStatus);
      });
    })
    .finally(() => setRewindBusy(false));
}

/** Rewind requests (dry run, perform, redo) in flight. A counter because the confirm,
 * conflict and choice steps can chain several, and each releases its own claim. */
let rewindsInFlight = 0;

/** Disables "Enviar" while a rewind request is in flight. The core refuses a turn during a
 * rewind (409, its `rewinding` flag in `server/app.py`), which the window would show as
 * "Sin conexión" (second strict review, item 7). */
function setRewindBusy(active) {
  rewindsInFlight += active ? 1 : -1;
  send.disabled = rewindsInFlight > 0;
  syncResetDisabled();
}

// ---------------------------------------------------------------------------
// "Reiniciar contexto" (design 1a): a fresh conversation in the same project.
// ---------------------------------------------------------------------------

/** Clears the thread to an empty window: no messages, the orb idle, the context chip blank.
 * Shared by the button's own success path and the core's `thread_reset` event (another
 * surface could reset the same session). History is not reloaded: the old conversation is
 * still the latest leaf until a new turn lands, so reading it back would repaint what the
 * reset just cleared. The new conversation starts blank and fills as it is used. */
function resetThreadView() {
  column.replaceChildren();
  redoLines.clear();
  // Nothing to page back to: the fresh root holds no messages yet.
  historyPaging.cursor = null;
  historyPaging.started = true;
  historyPaging.loading = false;
  historyPaging.done = true;
  historyPaging.open = true;
  current = null;
  turnOpening = null;
  setContextStatus(null);
  orb.set('idle');
  input.focus();
  jumpLatest.update();
}

/** The reset-context flow (reset.js): the confirm dialog, `POST /reset`, then the clear.
 * The dialog is appended to the thread and kept in view; the button follows `busy()`. */
const resetFlow = createResetFlow({
  mount: (el) => {
    const wasAtBottom = atBottom();
    column.append(el);
    keepUp(wasAtBottom);
  },
  resetContext: () => window.cogito.resetContext(),
  clearThread: resetThreadView,
  announce: (text) => announce(text),
  onBusyChange: () => syncResetDisabled(),
});

/** The reset button is disabled while a turn or a rewind runs (the core refuses a reset
 * then, exactly as it refuses a switch), while no project is open, and while a reset is
 * already under way (`resetFlow.busy()`). */
function syncResetDisabled() {
  resetButton.disabled = !projectLoaded || running || rewindsInFlight > 0 || resetFlow.busy();
}

resetButton.addEventListener('click', () => {
  if (resetButton.disabled) return;
  resetFlow.start();
});

/** Runs the "Volver a un punto" flow for one click on `link`: dry run, confirm dialog, the
 * conflict dialog if the perform answers 409, then the result line with "Rehacer". Each
 * step replaces the previous one in place, as the approval card does (`resolveToPill`).
 * `pointSentAt` is the message's own time, never `Date.now()`: the result line ("Volviste a
 * las HH:MM") and the conflict dialog ("Punto de las HH:MM") name the point.
 */
async function startRewind(pointId, originalText, pointSentAt, link) {
  const wasAtBottom = atBottom();
  link.disabled = true;
  let plan;
  setRewindBusy(true);
  try {
    plan = await window.cogito.rewindDryRun(pointId);
  } catch (error) {
    link.disabled = false;
    return;
  } finally {
    setRewindBusy(false);
  }
  if (plan && plan.graphDown) {
    showRewindGraphDown(link, link, () => startRewind(pointId, originalText, pointSentAt, link));
    keepUp(wasAtBottom);
    return;
  }

  const confirm = buildConfirmDialog(buildConfirmModel(plan), {
    onConfirm: () => performRewind(pointId, originalText, pointSentAt, confirm, link),
    onCancel: () => confirm.el.replaceWith(link),
  });
  link.replaceWith(confirm.el);
  confirm.el.focus();
  keepUp(wasAtBottom);
}

/** The context store is down: `shown` (the link, or a dialog) is replaced by «El almacén
 * de contexto no está disponible» and «Reintentar». A retry asks the main process to start
 * the store, puts `link` back and runs `again`, which asks the core again. Nothing was
 * rewound. */
function showRewindGraphDown(shown, link, again) {
  graphChip.noteDown();
  const notice = buildGraphDownLine(buildGraphDownModel(), {
    onRetry: () => {
      setRewindBusy(true);
      graphChip
        .ensure()
        .catch(() => null)
        .finally(() => {
          setRewindBusy(false);
          notice.replaceWith(link);
          link.disabled = false;
          again();
        });
    },
  });
  shown.replaceWith(notice);
  notice.retry.focus?.();
}

/** The `POST /rewind`, from the confirm dialog's "Volver aquí" or one of the conflict
 * dialog's two restoring choices. `dialog.el` is replaced by the conflict dialog on a 409,
 * or by the result line on success. `link` is the message's «↺ Volver a este punto», which
 * a redo puts back. */
async function performRewind(pointId, originalText, pointSentAt, dialog, link, options = {}) {
  const wasAtBottom = atBottom();
  if (dialog.setInProgress) dialog.setInProgress(true);
  let result;
  setRewindBusy(true);
  try {
    result = await window.cogito.rewind(pointId, options);
  } catch (error) {
    if (dialog.setInProgress) dialog.setInProgress(false);
    return;
  } finally {
    setRewindBusy(false);
  }
  if (result.graphDown) {
    // The store went away between the dry run and the confirm: nothing ran. The person
    // confirms again after the retry.
    showRewindGraphDown(dialog.el, link, () => startRewind(pointId, originalText, pointSentAt, link));
    return;
  }
  if (result.conflict) {
    const conflictDialog = buildConflictDialog(buildConflictModel(result, pointSentAt), {
      onLeave: () => conflictDialog.el.remove(),
      onKeepOthers: () =>
        performRewind(pointId, originalText, pointSentAt, conflictDialog, link, { keepConflicts: true }),
      onForceAll: () =>
        performRewind(pointId, originalText, pointSentAt, conflictDialog, link, { force: true }),
    });
    dialog.el.replaceWith(conflictDialog.el);
    conflictDialog.el.focus();
    keepUp(wasAtBottom);
    return;
  }
  // The core forgot any stopped turn when it rewound.
  retireServiceErrorCard();
  const resultModel = buildResultModel(result, pointSentAt);
  // The message's own bubble sits right before the dialog, as it sat before the link.
  const bubble = dialog.el.previousElementSibling;
  // The words the composer gets back: the core's own record of the point, else this
  // window's copy of what it sent.
  const text = resultModel.pointText ?? originalText;
  let taken = null;
  // Design 1n: the chips this rewind put back on the composer, which a redo takes back.
  let restoredChips = [];
  const line = buildResultLine(resultModel, () => {
    setRewindBusy(true);
    return window.cogito
      .redo(result.redo_id)
      .then((answer) => {
        if (answer && answer.graphDown) {
          graphChip.noteDown();
          throw graphDownError();
        }
        redoLines.delete(line);
        if (answer && answer.movedOn) {
          // A turn or another rewind came after it: the core will not redo it, so stop
          // offering it. Nothing failed.
          line.retractRedo();
          return;
        }
        if (!taken) return;
        // The conversation is back: put the thread back where it was, with the point's
        // «↺ Volver a este punto» in place of the result line. Done quietly, with focus on
        // the link (the result line had it) and one sentence announced.
        link.disabled = false;
        quietly(() => {
          line.replaceWith(taken.bubble, link);
          link.after(...taken.after);
        });
        // Focus without scrolling (it would pull the point's link to mid-thread), then land
        // at the end: «Rehacer» is the person asking for the thread back, as sending is.
        link.focus({ preventScroll: true });
        transcript.scrollTop = transcript.scrollHeight;
        jumpLatest.update();
        announce(REWIND.redoneStatus);
        // The composer only lets go of what this rewind put there, untouched.
        if (input.value === text) {
          input.value = '';
          attachTray.clear(restoredChips);
          syncComposer();
        }
      })
      .finally(() => setRewindBusy(false));
  });
  dialog.el.replaceWith(line);
  // The conversation went back with the files: the point's message and everything after
  // it leave the thread, kept for «Rehacer». The result line stays where it happened.
  if (result.conversation) taken = takeThreadFrom(bubble, line);
  // A new rewind is the only one «Rehacer» still applies to: the core refuses to redo an
  // earlier one once another followed, so none of theirs is offered any more.
  retractRedos();
  if (resultModel.redoAvailable) redoLines.add(line);
  // The message text goes back into the composer only when the rewind restored something
  // (files or the conversation). Not for "Dejar como está", and not when every path failed:
  // then there is nothing to edit and resend (second strict review, item 3). Appended if
  // the person has typed meanwhile.
  if (resultModel.hasRestored) putInComposerWithoutClobbering(text);
  // And what was attached to it, when the conversation went back (design 1n): a file read
  // again from its path, a paste as it was.
  if (resultModel.hasRestored && Array.isArray(result.point_attachments)) {
    attachTray.restore(result.point_attachments).then((ids) => {
      restoredChips = ids;
    });
  }
  keepUp(wasAtBottom);
}

/**
 * Enviar. Idle, it starts a new turn. Busy, it sends a steer, never an interrupt: only
 * "■ Detener" stops (design 1m: "Enviar nunca detiene.").
 */
/** «Continuar» (long-output fix 2): sends a fixed follow-up as a new turn, the composer
 * left untouched. The same new-turn path `submit()` takes with nothing attached; nothing
 * sends if a turn is already running or mid-switch, since the stop line that offered this
 * belongs to a turn already over. */
async function sendFollowUp(text) {
  if (switchingProject || running) return;
  retractRedos();
  retireServiceErrorCard();
  const pending = said(text, []);
  pendingRewindLinks.push(pending);
  turnOpening = pending;
  turnActive = true;
  setRunning(true);
  orb.set('thinking');
  try {
    await autoMode.sync();
    const sent = await window.cogito.send(text, [], null, thinkingControl.level());
    if (sent && sent.refused) {
      takeBackRefused(pending, text, [], sent.refused);
      turnActive = false;
      setRunning(false);
      orb.set('idle');
    }
  } catch (error) {
    const notSent = pendingRewindLinks.indexOf(pending);
    if (notSent !== -1) pendingRewindLinks.splice(notSent, 1);
    turnActive = false;
    const built = answer();
    built.block.classList.add('failed');
    errorCard(built.body, ERROR_COPY.offline, error.message);
    orb.set('error');
    setRunning(false);
  }
}

async function submit() {
  // A project is opening: its core is not ready, and the old one is gone. The text stays.
  if (switchingProject) return;
  const text = input.value.trim();
  // Design 1n: the attachments go with it, as the chips the core accepted (path or text).
  const pieces = attachTray.ready();
  if (!text && !pieces.length) return;
  const specs = pieces.map((piece) => piece.spec);
  const views = pieces.map((piece) => piece.view);
  // The skills the person activated for this message (skills-tray.js). They go with a new
  // turn; the chips clear whichever path the message takes.
  const activatedSkills = skillsTray.active();
  input.value = '';
  attachTray.clear();
  skillsTray.clear();
  syncComposer();
  // The canvas' `send`, for a new turn and a message into a running one alike.
  sfx('send');
  // Design 1a: sending goes back to the thread; the file tabs stay open.
  viewer.showThread();

  if (running) {
    const handle = saidSteer(text, views);
    // A rewind point only if this steer joins the transcript. `settleSteerDropped` and the
    // catch block below remove it again, since then it never will.
    pendingRewindLinks.push(handle);
    try {
      const result = await window.cogito.steer(text, specs, thinkingControl.level());
      if (result.refused) {
        takeBackRefused(handle, text, specs, result.refused);
        handle.status.remove();
        markSteerSeqResolved(handle.seq);
        flushReadyDrops();
      } else if (result.fellBackToTurn) {
        // The turn had already ended (server: 409), so the main process sent this as an
        // ordinary turn. It is the next turn, not a queued steer: drop the status line and
        // set up the turn as the normal path below does.
        handle.status.remove();
        retractRedos();
        retireServiceErrorCard();
        turnActive = true;
        setRunning(true);
        orb.set('thinking');
        // No steer_applied/steer_dropped will come for it, so its seq is resolved here, or a
        // later drop would wait on it forever (see `nextSteerSeq`).
        markSteerSeqResolved(handle.seq);
        flushReadyDrops();
      } else {
        // The event may have arrived before this reply (`bufferedSteerEvents`): settle now
        // if so, otherwise register and let the event switch settle it.
        const buffered = bufferedSteerEvents.get(result.id);
        if (buffered) {
          bufferedSteerEvents.delete(result.id);
          settleSteerEvent(handle, buffered);
        } else {
          pendingSteers.set(result.id, handle);
        }
      }
    } catch (error) {
      // The request never reached the core (offline, say). The bubble is the only record,
      // so it is marked failed. No automatic retry, since the turn may have moved on.
      handle.dot.remove();
      handle.status.classList.add('is-failed');
      handle.label.textContent = STEER.failed;
      // The core never saw it, so neither steer_applied/steer_dropped nor point_recorded
      // will come: remove its pending rewind link and resolve its seq.
      const notQueued = pendingRewindLinks.indexOf(handle);
      if (notQueued !== -1) pendingRewindLinks.splice(notQueued, 1);
      markSteerSeqResolved(handle.seq);
      flushReadyDrops();
    }
    return;
  }

  // A new turn makes every rewind final: «Rehacer» would only be refused from here on.
  retractRedos();
  retireServiceErrorCard();
  const pending = said(text, views);
  pendingRewindLinks.push(pending);
  turnOpening = pending;
  turnActive = true;
  setRunning(true);
  orb.set('thinking');
  try {
    await autoMode.sync();
    const sent = await window.cogito.send(text, specs, activatedSkills, thinkingControl.level());
    if (sent && sent.refused) {
      takeBackRefused(pending, text, specs, sent.refused);
      turnActive = false;
      setRunning(false);
      orb.set('idle');
    }
  } catch (error) {
    // It never reached the core, so no point_recorded will come for it.
    const notSent = pendingRewindLinks.indexOf(pending);
    if (notSent !== -1) pendingRewindLinks.splice(notSent, 1);
    turnActive = false;
    const built = answer();
    built.block.classList.add('failed');
    errorCard(built.body, ERROR_COPY.offline, error.message);
    orb.set('error');
    setRunning(false);
  }
}

/** Design 1n: an attachment changed between the chip and the send (deleted, grown past
 * 20 MB) and the core refused the message. Nothing was sent: the bubble goes, and the words
 * and the chips go back on the composer, the refused one saying why. */
function takeBackRefused(pending, text, specs, refused) {
  const index = pendingRewindLinks.indexOf(pending);
  if (index !== -1) pendingRewindLinks.splice(index, 1);
  pending.bubble.remove();
  putInComposerWithoutClobbering(text);
  attachTray.restore(specs, refused);
}

send.addEventListener('click', submit);

// "■ Detener" is the only control that stops anything. It is kept apart from submit() on
// purpose: Enter on it must never answer an approval card (see the approval-keyboard
// regression test) or send a message.
stop.addEventListener('click', () => {
  sfx('cancel');
  window.cogito.interrupt().catch((error) => {
    // The core could not be reached. Reported in the same words as `submit()`'s offline
    // path, so the stop control never fails silently.
    const built = answer();
    built.block.classList.add('failed');
    errorCard(built.body, ERROR_COPY.offline, error.message);
  });
});

// Enter sends (a turn, or a steer while busy); Shift+Enter is a newline, as in any chat.
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});

// ---------------------------------------------------------------------------
// The core died under the window (main/supervisor.js restarts it, reopens this project's
// session and reattaches the stream). A running turn died with it, so it ends here with
// the reason, and the dot is red until the core is back.
// ---------------------------------------------------------------------------
function endTurnWithCoreExit(title, detail) {
  if (!turnActive && !running) return false;
  turnActive = false;
  turnOpening = null;
  pendingServiceError = null;
  if (!current) current = answer();
  closeRunningRows(current);
  dropLive(current);
  dropThinking(current);
  dropToolCallProgress(current);
  current.block.classList.add('failed');
  flushChangesCard(current);
  errorCard(current.body, title, detail);
  // An approval card can no longer be answered: the call it held died with the core.
  for (const pending of pendingApprovals.values()) {
    pending.block.classList.add('is-settled');
    pending.allow.disabled = true;
    pending.deny.disabled = true;
  }
  pendingApprovals.clear();
  current = null;
  waitingApprovalCallId = null;
  lastStartedCallId = null;
  settleAnyStillPendingSteers();
  setRunning(false);
  setResumePending(false);
  return true;
}

window.cogito.onCoreSupervisor?.((report) => {
  const state = report && report.state;
  const detail = ERROR_COPY.coreExit(report && report.code, report && report.signal);
  if (state === 'restarting') {
    pip.classList.add('is-down');
    endTurnWithCoreExit(ERROR_COPY.coreRestarting, detail);
    orb.set('error');
    sfx('close');
  } else if (state === 'restarted') {
    pip.classList.remove('is-down');
    orb.set('idle');
    // The shells ended with the old core; the new session lists its own.
    terminals.clear();
    terminals.refresh();
    // Marks where the thread picks up again, drawn like the updater's «— ACTUALIZADO A X —».
    const wasAtBottom = atBottom();
    // Drawn like a day separator but not one: `ensureToday` reads only `.day-separator`.
    const marker = buildDaySeparator('', THREAD.daySeparator(ERROR_COPY.coreRestarted));
    marker.className = 'thread-separator core-restarted';
    delete marker.dataset.day;
    marker.dataset.new = '';
    column.append(marker);
    keepUp(wasAtBottom);
  } else if (state === 'gave-up') {
    pip.classList.add('is-down');
    document.querySelector('.model-name').textContent = STATUS.coreUnavailable;
    if (!endTurnWithCoreExit(ERROR_COPY.coreGaveUp, detail)) {
      const built = answer();
      built.block.classList.add('failed');
      errorCard(built.body, ERROR_COPY.coreGaveUp, detail);
    }
    orb.set('error');
    sfx('close');
  }
});

// A notification's click: open the window at that card.
window.cogito.onReveal?.((what) => reveal(what));
// «Cambiar modelo» from the tray or a notification (design 2d).
window.cogito.onOpenModelMenu?.(() => componentUi.openModelMenu());
document.querySelector('.chrome--close').addEventListener('click', () => windowApi.close());
document.querySelector('.chrome--min').addEventListener('click', () => windowApi.minimize());
maximizeButton.addEventListener('click', () => windowApi.maximize());

// ---------------------------------------------------------------------------
// History on open: the project's thread, read back from the store
// ---------------------------------------------------------------------------

/** Items per page asked of the core; a page never splits a turn, so it may hold more. */
const HISTORY_PAGE = 60;
/** How close to the top of the thread a scroll has to come before the older page loads. */
const HISTORY_EDGE_PX = 120;
/** `open`: the core's session exists (nothing can be read before it does). */
const historyPaging = { open: false, cursor: null, started: false, loading: false, done: false };

/** Merges two separators for the same day (a page boundary, or «— HOY —» drawn for a live
 * message before history arrived) into the first, where the day begins. */
function dedupeDaySeparators() {
  let previous = null;
  for (const node of column.querySelectorAll('.day-separator')) {
    if (node.dataset.day === previous) node.remove();
    else previous = node.dataset.day;
  }
}

const skeleton = document.querySelector('.thread-skeleton');

/** Design 1a's shimmer over the thread, while the first page of history is being read. */
function showSkeleton() {
  skeleton.classList.remove('is-leaving');
  skeleton.hidden = false;
}

function hideSkeleton() {
  if (skeleton.hidden) return;
  skeleton.classList.add('is-leaving');
  setTimeout(() => {
    if (skeleton.classList.contains('is-leaving')) skeleton.hidden = true;
  }, 400);
}

/**
 * Loads the next older page of the thread and inserts it above everything drawn, including
 * live turns and the updater's «— ACTUALIZADO A X —», which marks where the thread resumed.
 * The first page opens scrolled to its end; an older one keeps the reader's place. Rewind
 * points in it run the same flow as live ones.
 */
async function loadHistoryPage() {
  if (!historyPaging.open || historyPaging.loading || historyPaging.done || !window.cogito.history) {
    hideSkeleton();
    return;
  }
  historyPaging.loading = true;
  const first = !historyPaging.started;
  historyPaging.started = true;
  try {
    const page = await window.cogito.history({ before: historyPaging.cursor, limit: HISTORY_PAGE });
    // On open, show the session's last known context figure, if any (design 1a). A live
    // turn's events take over later; there is no race, since no turn runs yet.
    if (first) setContextStatus(page?.context_status || null);
    const items = Array.isArray(page?.items) ? page.items : [];
    if (first) {
      // «Modificado en esta tarea» in the viewer: the last turn's changes, when the last
      // turn changed anything.
      let lastUser = -1;
      let lastChanges = -1;
      items.forEach((item, index) => {
        if (item && item.type === 'user') lastUser = index;
        if (item && item.type === 'turn_changes') lastChanges = index;
      });
      noteTaskChanges(lastChanges > lastUser ? items[lastChanges].files : []);
    }
    const now = Date.now();
    const { blocks } = historyBlocks(items, { now });
    const nodes = renderHistory(blocks, { onRewind: startRewind, onRedo: redoFromHistory, paintAvatar, now });
    trackRedoLines(nodes);
    const previousHeight = transcript.scrollHeight;
    const previousTop = transcript.scrollTop;
    const anchor = column.firstChild;
    for (const node of nodes) column.insertBefore(node, anchor);
    dedupeDaySeparators();
    if (first) transcript.scrollTop = transcript.scrollHeight;
    else transcript.scrollTop = previousTop + (transcript.scrollHeight - previousHeight);
    historyPaging.cursor = typeof page?.before === 'string' && page.before ? page.before : null;
    historyPaging.done = historyPaging.cursor === null;
  } catch (error) {
    // No history (an older core, or a store that could not be read): the thread just
    // starts here.
    historyPaging.done = true;
    console.info(`history: ${error.message}`);
  } finally {
    historyPaging.loading = false;
    if (first) hideSkeleton();
  }
  // A short first page leaves nothing to scroll: keep going until there is, or it ends.
  if (!historyPaging.done && transcript.scrollHeight <= transcript.clientHeight) loadHistoryPage();
}

transcript.addEventListener('scroll', () => {
  if (transcript.scrollTop < HISTORY_EDGE_PX) loadHistoryPage();
});

/** Reloads the thread from the start, as on open. Used after «Rehacer» on a rewind from
 * history brings back messages the page never held. */
function reloadThread() {
  column.replaceChildren();
  redoLines.clear();
  historyPaging.cursor = null;
  historyPaging.started = false;
  historyPaging.loading = false;
  historyPaging.done = false;
  historyPaging.open = true;
  showSkeleton();
  return loadHistoryPage();
}

// ---------------------------------------------------------------------------
// Projects (design 1f): opening one (on first open, or a later switch from the header's
// project chip) and applying it to the header and the thread.
// ---------------------------------------------------------------------------

/** Whether `applyProject` has opened a project yet. The first call must not clear `.column`:
 * update-ui.js may already have put a restart marker there, and it must survive. A later
 * call is a switch and clears the previous project's thread. */
let projectLoaded = false;
/** The open project's root as the main process displays it (`~/…`), for the project menu's ✓. */
let currentProjectRoot = null;

/** Applies an opened project to the header and reads its thread from the beginning. Used
 * for the first open and for switches, so the header never keeps a stale value and the
 * thread never mixes two projects. */
function applyProject({ project, projectRoot, model, branch }) {
  setSwitching(null);
  pip.classList.remove('is-down');
  // Just the name, no "project:" label (design 1a: "tienda-api"); CSS draws the sphere and ▾.
  projectChipName.textContent = project;
  projectChip.title = projectRoot;
  currentProjectRoot = projectRoot;
  setTitle(project);
  // The explorer shows this project's folder; the viewer's tabs were the last project's.
  noteTaskChanges([]);
  viewer.clear();
  // Terminals belong to the session: this project's, read again.
  terminals.clear();
  terminals.refresh();
  // Browser tabs belong to the project: main lists this one's.
  browsers.clear();
  browsers.refresh();
  explorerToggle.hidden = false;
  resetButton.hidden = false;
  // The skills picker appears with the project, like «Reiniciar contexto». Its list is this
  // project's, so it is read again now (skills-tray.js).
  skillsButton.hidden = false;
  skillsTray.refresh();
  // A new session in the core starts with the automatic mode off: the window's choice again.
  autoMode.setAvailable(true);
  autoMode.sync();
  explorer.setProject({ key: projectRoot, name: project });
  setBranch(branch || null);
  setModelService(null);
  document.querySelector('.model-name').textContent = model || STATUS.unknownModel;
  if (model) refreshModelChip(model);
  refreshRateLimit();
  // Hidden until a real figure arrives, from the first history page or a turn's
  // `context_status` event. A fresh session's context is unknown, not 0.
  setContextStatus(null);
  if (projectLoaded) column.replaceChildren();
  // A failed launch's «Sin conexión» card belongs to no project (QA 2026-10-11).
  for (const stale of column.querySelectorAll('[data-open-failure]')) stale.remove();
  projectLoaded = true;
  syncResetDisabled();
  // No turn runs across a switch, so an earlier error never carries into this project.
  orb.set('idle');
  redoLines.clear();
  historyPaging.cursor = null;
  historyPaging.started = false;
  historyPaging.loading = false;
  historyPaging.done = false;
  historyPaging.open = true;
  input.focus();
  showSkeleton();
  loadHistoryPage();
}

const projectsScreen = createProjectsScreen(
  {
    // Design 1f greets with the name the welcome stored (1d).
    name: () => welcomeName,
    // Opening restarted the core, and it serves no model: say why, and open it once one is.
    // Any other failure is this screen's own note.
    onOpenFailed: projectOpenFailed,
    onOpening: setSwitching,
    onSettled: () => setSwitching(null),
    list: () => window.cogito.projects.list(),
    pickFolder: () => window.cogito.projects.pickFolder(),
    open: (path) => window.cogito.projects.open(path),
    forget: (path) => window.cogito.projects.forget(path),
    isBusy: () => turnActive || running || Boolean(switchingProject),
    onOpened: applyProject,
    // The open project starts selected in the picker, not whichever row happens to be first.
    current: () => currentProjectRoot,
  },
  document,
);

/** A project that did not open. The main process reopened the one that was open
 * (`restored`), or could not, and then the window has no project to go back to. Returns
 * true when another screen explains it (the core serves no model). */
function projectOpenFailed(result, path) {
  if (result && result.error === 'no model') return showNoModel(path);
  if (result && result.restored === false && currentProjectRoot) {
    currentProjectRoot = null;
    pip.classList.add('is-down');
  }
  return false;
}

// Design 1f («Proyectos — al abrir la app y con ⌘O»): the full screen when the app opens
// with no project, and on Ctrl+O (⌘O on macOS), dismissible once a project is showing.
function openProjectsScreen() {
  if (turnActive || running) return; // no switching while a turn runs (see setRunning)
  sfx('window');
  projectsScreen.show({ dismissible: Boolean(currentProjectRoot) });
}
document.addEventListener('keydown', (event) => {
  const mod = titlebar.dataset.platform === 'darwin' ? event.metaKey : event.ctrlKey;
  if (!mod || event.altKey || event.shiftKey || event.key.toLowerCase() !== 'o') return;
  event.preventDefault();
  openProjectsScreen();
});

// Design 1a: the project chip drops the recent projects and «Abrir carpeta…».
const projectMenu = attachProjectMenu(projectChip, {
  list: () => window.cogito.projects.list(),
  pickFolder: () => window.cogito.projects.pickFolder(),
  open: (path) => window.cogito.projects.open(path),
  // A folder that no longer exists is offered for removal here too, as in the picker.
  forget: (path) => window.cogito.projects.forget(path),
  isBusy: () => turnActive || running || Boolean(switchingProject),
  current: () => currentProjectRoot,
  onOpened: applyProject,
  onOpening: setSwitching,
  onSettled: () => setSwitching(null),
  // The menu has no room to explain a failure: the picker opens saying why.
  onOpenFailed: async (result, path) => {
    const hadProject = Boolean(currentProjectRoot);
    if (await projectOpenFailed(result, path)) return true;
    if (result && result.error === 'busy') return false;
    const lost = hadProject && !currentProjectRoot;
    projectsScreen.show({ dismissible: !lost && hadProject, note: openFailureNote(result, path, { lost }) });
    return true;
  },
  play: sfx,
});

// The `window` sound whenever this window appears: on launch, and each time the main
// process reports it brought back (from the tray, or un-minimised).
sfx('window');
windowApi.onShown?.(() => sfx('window'));

function coreUnavailable(error) {
  pip.classList.add('is-down');
  document.querySelector('.model-name').textContent = STATUS.coreUnavailable;
  setContextStatus(null);
  const built = answer();
  built.block.classList.add('failed');
  // Removed by the next project that opens (`applyProject`).
  built.block.dataset.openFailure = '';
  errorCard(built.body, ERROR_COPY.offline, error.message);
  orb.set('error');
}

function openApp() {
  window.cogito
    .open()
    .then((result) => {
      if (result && result.needsProject) {
        document.querySelector('.model-name').textContent = STATUS.unknownModel;
        setTitle(null);
        // The most recent project did not open: the picker says why (QA 2026-10-11).
        if (result.error === 'no model') {
          Promise.resolve(showNoModel(result.failed)).then((shown) => {
            if (!shown) projectsScreen.show({ dismissible: false, note: openFailureNote(result, result.failed) });
          });
          return;
        }
        const note = result.error ? openFailureNote(result, result.failed) : '';
        projectsScreen.show({ dismissible: false, note });
        return;
      }
      applyProject(result);
      // A reloaded window reattached to a core whose turn is still running (QA 2026-10-11
      // bug 29): its events keep arriving, and its end must settle this window.
      if (result && result.running) {
        turnActive = true;
        setRunning(true);
        orb.set('thinking');
      }
    })
    .catch(coreUnavailable);
}

// ---------------------------------------------------------------------------
// The welcome (design 1d-1e): on a first launch, the name and a model; later, the same
// connection screen from the model menu's «Conectar otro servicio…».
// ---------------------------------------------------------------------------

/** The name the welcome stored, for 1f's question. */
let welcomeName = null;

const welcomeScreen = window.cogito.welcome
  ? createWelcome(
      {
        welcome: window.cogito.welcome,
        connect: window.cogito.connect,
        isBusy: () => turnActive || running,
        onSwitchRefused: showModelFailure,
        onDone: (result) => {
          if (reopenAfterModel) {
            // A project switch found the core serving no model; one is served now.
            const path = reopenAfterModel;
            reopenAfterModel = null;
            welcomeFirst = false;
            projectsScreen.reopen(path);
            return;
          }
          if (welcomeFirst) {
            welcomeFirst = false;
            // The name given in step 1, for 1f's «¿En qué trabajamos hoy, {nombre}?».
            Promise.resolve(window.cogito.welcome.get())
              .then((state) => {
                if (state && typeof state.name === 'string') welcomeName = state.name;
              })
              .catch(() => {})
              .finally(openApp);
            return;
          }
          const model = result && result.saved ? result.saved.model : null;
          // Served now (the core's `model_changed` reaches an open project's stream too).
          if (model && result.saved.switch && result.saved.switch.changed) {
            document.querySelector('.model-name').textContent = escapeControlChars(String(model.label || model.id));
            setModelService(model.service || null);
            refreshModelChip(model.id);
          }
        },
      },
      document,
    )
  : null;
/** True while the first launch's welcome is up: finishing it opens the app. */
let welcomeFirst = false;
/** A project whose opening found the core serving no model: opened again once one is. */
let reopenAfterModel = null;

/** The core serves no model (it could not start with the one it had, or has none at all):
 * the connection screen, not dismissible, saying why (design 1e's card, `unserved`) — or,
 * for a model that failed to start, the inline header/thread card instead, since that model
 * is still "selected" and the connect screen would read as "no model exists at all".
 * Returns true when either screen now explains it, so the caller (a project failing to
 * open) shows nothing further over it. */
async function showNoModel(path = null) {
  if (!welcomeScreen) return false;
  let state = null;
  try {
    state = await window.cogito.welcome.get();
  } catch {
    state = null;
  }
  if (!state || state.unavailable || state.error || state.model) return false;
  // A model that failed to start is still "selected": show the error where the model is,
  // not the connect screen, which read as "no model exists".
  if (isStartupFailure(state.unserved)) {
    startupFailure.show(state.unserved, path);
    // The model did not start: the pip says so.
    pip.classList.add('is-down');
    // Put the model's own name back in the header: it stays "selected", not «Modelo
    // desconocido» as a plain "no model" would read.
    const label = state.unserved.model.label || state.unserved.model.id;
    if (label) document.querySelector('.model-name').textContent = escapeControlChars(String(label));
    return true; // handled inline: the caller shows no further screen over it
  }
  reopenAfterModel = path;
  welcomeFirst = !path;
  document.querySelector('.model-name').textContent = STATUS.unknownModel;
  welcomeScreen.show({ mode: state.done ? 'blocked' : 'first', state });
  return true;
}

function openConnectScreen() {
  if (!welcomeScreen || welcomeScreen.isOpen()) return;
  sfx('window');
  welcomeScreen.show({ mode: 'connect' });
}

/** The first thing the window does: the welcome when it is not over (or no model is left to
 * talk to), else the app as before. A core without the welcome's routes shows none. */
async function boot() {
  let state = null;
  try {
    state = welcomeScreen ? await window.cogito.welcome.get() : null;
  } catch {
    state = null;
  }
  if (state && !state.unavailable && !state.error) {
    welcomeName = state.name || null;
    const startupFailure = isStartupFailure(state.unserved);
    // Not done: the welcome. Done but no model served (the catalog was emptied, or the core
    // left it unserved): the connection screen saying why, never a projects screen that
    // could open nothing. A model that failed to start is the one exception: it stays
    // "selected" and the app opens with the error where the model is.
    if (!state.done || (!state.model && !startupFailure)) {
      welcomeFirst = true;
      document.querySelector('.model-name').textContent = STATUS.unknownModel;
      welcomeScreen.show({ mode: state.done ? 'blocked' : 'first', state });
      return;
    }
    // `openApp()` below opens the most recent project; if the core still has no model to
    // open a session on, its own `needsProject`/`showNoModel` path draws this same card
    // (`isStartupFailure`), so nothing is drawn twice here.
  }
  openApp();
}

boot().catch(coreUnavailable);
