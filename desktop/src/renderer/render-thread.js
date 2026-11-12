'use strict';

/**
 * The thread's pieces of DOM, shared by the live turn (`chat.js`) and the history read back
 * on open (`render-history.js`) so the two cannot draw a message differently: the person's
 * bubble, the agent's answer block (its orb and a body for rows, text and cards), the day
 * separator (design 1a/3a, «— HOY —») and the thin marker a compaction fold leaves in the
 * history.
 *
 * `documentRef` defaults to the page's `document`, like the other `render-*` modules, so
 * `cogito-lab` can build these against a fake document. `paintAvatar` is `orb.js`'s `paint`
 * in the window, or anything that marks the element in a test.
 */

import { THREAD } from './strings.js';
import { escapeControlChars } from './sanitize.js';
import { chipView } from './attachments.js';
import { buildSentChips } from './render-attachments.js';

/** A message past either bound starts collapsed: a few lines and «Mostrar todo». */
const LONG_CHARS = 2000;
const LONG_LINES = 20;

function isLong(text) {
  if (typeof text !== 'string') return false;
  if (text.length > LONG_CHARS) return true;
  let lines = 1;
  for (let at = text.indexOf('\n'); at !== -1; at = text.indexOf('\n', at + 1)) {
    lines += 1;
    if (lines > LONG_LINES) return true;
  }
  return false;
}

/** «Mostrar todo» / «Mostrar menos» under a long bubble. The whole text is in the bubble
 * either way; only its height changes. */
function buildToggle(bubble, documentRef) {
  const toggle = documentRef.createElement('button');
  toggle.type = 'button';
  toggle.className = 'said-toggle';
  toggle.textContent = THREAD.showAll;
  toggle.setAttribute('aria-expanded', 'false');
  toggle.addEventListener('click', () => {
    const open = bubble.classList.contains('said--collapsed');
    bubble.classList.toggle('said--collapsed', !open);
    toggle.textContent = open ? THREAD.showLess : THREAD.showAll;
    toggle.setAttribute('aria-expanded', String(open));
  });
  return toggle;
}

/** The person's message: `.said`, the text as written (never markup). With attachments
 * (design 1n), given as the core's facts or the composer's own views, the chips go above
 * the message in one `.said-group`. A message of attachments alone has no bubble. A long
 * message is collapsed in a `.said-group` with its toggle under it, never cut. */
function buildSaid(text, documentRef = document, attachments = []) {
  const bubble = documentRef.createElement('div');
  bubble.className = 'said';
  bubble.textContent = text;
  const long = isLong(text);
  const chips = Array.isArray(attachments) && attachments.length;
  if (!chips && !long) return bubble;
  const group = documentRef.createElement('div');
  group.className = 'said-group';
  if (chips) {
    const views = attachments.map((item) => (item && item.badge !== undefined ? item : chipView(item)));
    group.append(buildSentChips(views, documentRef));
  }
  if (text) group.append(bubble);
  if (long) {
    bubble.classList.add('said--collapsed');
    group.append(buildToggle(bubble, documentRef));
  }
  return group;
}

/** The agent's side of a turn: its small orb and the body everything else goes in. */
function buildAnswer(paintAvatar, documentRef = document) {
  const block = documentRef.createElement('div');
  block.className = 'answer';
  const avatar = documentRef.createElement('span');
  paintAvatar(avatar);
  const body = documentRef.createElement('div');
  body.className = 'answer-body';
  block.append(avatar, body);
  return { block, body };
}

/** «— HOY —», or the date for an earlier day. `key` (YYYY-MM-DD) lets the window tell two
 * separators for the same day apart from two different days. */
function buildDaySeparator(key, label, documentRef = document) {
  const node = documentRef.createElement('div');
  node.className = 'day-separator';
  node.dataset.day = key;
  const left = documentRef.createElement('span');
  left.className = 'day-separator-rule';
  const text = documentRef.createElement('span');
  text.className = 'day-separator-label';
  text.textContent = label;
  const right = documentRef.createElement('span');
  right.className = 'day-separator-rule';
  node.append(left, text, right);
  return node;
}

/** Where a compaction folded the conversation: a dotted rule on each side of a small label,
 * «Conversación resumida», so a sighted reader is told what the line is (QA 2026-10-13,
 * chat D4). The same words are the separator's accessible name and its tooltip. The
 * handover's text is the model's summary, not the thread, and is never shown. */
function buildFoldMarker(documentRef = document) {
  const node = documentRef.createElement('div');
  node.className = 'fold-marker';
  node.setAttribute('role', 'separator');
  node.setAttribute('aria-label', THREAD.folded);
  node.title = THREAD.folded;
  const left = documentRef.createElement('span');
  left.className = 'fold-marker-rule';
  const text = documentRef.createElement('span');
  text.className = 'fold-marker-label';
  text.textContent = THREAD.folded;
  const right = documentRef.createElement('span');
  right.className = 'fold-marker-rule';
  node.append(left, text, right);
  return node;
}

/** APP-ONLY: the thin line that says a command left in the background ended
 * (`background_finished`), drawn like the fold marker; the command is its tooltip. */
function buildBackgroundMarker(event, documentRef = document) {
  const node = buildFoldMarker(documentRef);
  node.classList.add('background-marker');
  const label = THREAD.backgroundEnded(event && event.exit_code);
  node.setAttribute('aria-label', label);
  node.title = escapeControlChars(String((event && event.command) || label));
  // The fold marker's middle child is its label (`buildFoldMarker`).
  const text = node.children[1];
  if (text) text.textContent = label;
  return node;
}

/** APP-ONLY auto-continue (long-output fix 3): the quiet «Continuando…» line while a
 * reply the output limit cut off continues on its own, with nothing to click (the
 * person's decision, 2026-10-12). `chat.js`'s `showContinuing` appends and removes it.
 * `text` is another quiet line drawn the same way: «Reiniciando el modelo…» while the core
 * starts a crashed Cogito Motoris again (`model_restarting`). */
function buildContinuingLine(documentRef = document, text = THREAD.continuing) {
  const node = documentRef.createElement('div');
  node.className = 'continuing-live';
  node.textContent = text;
  return node;
}

/** APP-ONLY auto-continue: the words joined so far when a sample continues the paragraph
 * before it with no call in between (`loop.engine.AssistantText.continued`), or
 * `addition` alone otherwise. The model was asked to continue from exactly where it
 * stopped, without repeating itself (`_AUTO_CONTINUE_TEXT_NOTICE`), so the two are simply
 * laid end to end -- no separator, no de-duplication. */
function joinedAnswer(previous, addition) {
  if (!previous) return addition || '';
  if (!addition) return previous;
  return previous + addition;
}

/** Why a turn stopped before finishing (`STOP_COPY`), live and in history alike: a quiet
 * line, or a red one for a provider failure (`kind: 'failure'`). `action` (long-output
 * fix 2: «Continuar» on an output-limit stop) adds a small button after the text; it
 * disables itself once clicked, since the turn it would continue starts only once. */
function buildStopLine({ kind, text, action }, documentRef = document) {
  const node = documentRef.createElement('div');
  node.className = kind === 'failure' ? 'stop-line stop-line--failure' : 'stop-line';
  const label = documentRef.createElement('span');
  label.textContent = text;
  node.append(label);
  if (action) {
    const button = documentRef.createElement('button');
    button.type = 'button';
    button.className = 'stop-line-action';
    button.textContent = action.label;
    button.addEventListener('click', () => {
      button.disabled = true;
      action.onClick();
    });
    node.append(button);
  }
  return node;
}

export {
  buildSaid,
  buildAnswer,
  buildDaySeparator,
  buildFoldMarker,
  buildBackgroundMarker,
  buildStopLine,
  buildContinuingLine,
  joinedAnswer,
};
