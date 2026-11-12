'use strict';

/**
 * DOM for the cards of design 4a (herramienta necesaria: propuesta, instalando, instaló,
 * falló) and 4b (falta un programa, permiso, paso detenido), drawn from the models in
 * components.js. Nothing here talks to the core: every action is a callback the caller
 * (components-ui.js) wires.
 *
 * The keyboard-safety rule is the approval card's (chat.js, `ask`). Enter and Esc act only
 * when the card itself has focus, not when the key bubbled up from a focused button or
 * from elsewhere in the window. They never act on a key repeat or with a modifier, and
 * never in the first 500 ms after the card appears, so the Enter that sent a message
 * cannot also answer a card that just showed up.
 */

import { buildPill } from './render-decisions.js';
import { escapeControlChars } from './sanitize.js';

const JUST_APPEARED_MS = 500;

/** Every text on these cards goes through here: fixed copy, and names, descriptions,
 * commands, errors and addresses from the core. All of it is neutralized (sanitize.js), as
 * paths and commands are everywhere else in the thread. */
function el(documentRef, tag, className, text) {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = escapeControlChars(String(text));
  return node;
}

function button(documentRef, label, variant, onClick) {
  const node = el(documentRef, 'button', `cc-button${variant ? ` cc-button--${variant}` : ''}`, label);
  node.type = 'button';
  node.addEventListener('click', () => {
    if (!node.disabled) onClick();
  });
  return node;
}

function head(documentRef, tone, title, { spinner = false } = {}) {
  const bar = el(documentRef, 'div', 'cc-head');
  bar.append(el(documentRef, 'span', spinner ? 'cc-spinner' : `cc-dot cc-dot--${tone}`), el(documentRef, 'span', 'cc-title', title));
  return bar;
}

function card(documentRef, tone) {
  return el(documentRef, 'div', `cc-card cc-card--${tone}`);
}

/** Label/value grid (4a «Qué habilita», 4b «Herramienta / Qué va a hacer / Alcance»,
 * «Motivo / Dónde»). A row's `target` is set in monospace after its value, as the design
 * sets the page address. */
function grid(documentRef, rows) {
  const node = el(documentRef, 'div', 'cc-grid');
  for (const row of rows) {
    const value = el(documentRef, 'span', 'cc-value', row.value || '');
    if (row.target) {
      if (row.value) value.append(documentRef.createTextNode(' '));
      value.append(el(documentRef, 'span', 'cc-mono', row.target));
    }
    node.append(el(documentRef, 'span', 'cc-label', row.label), value);
  }
  return node;
}

/**
 * Enter/Esc on a card, under the keyboard-safety rule above. `now` is injectable so the
 * unit suite can check the 500 ms guard without waiting.
 */
function guardKeys(target, { onEnter, onEscape }, now = () => Date.now()) {
  const createdAt = now();
  target.tabIndex = 0;
  target.addEventListener('keydown', (event) => {
    if (event.target !== target) return;
    if (event.repeat) return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (now() - createdAt < JUST_APPEARED_MS) return;
    if (event.key === 'Enter' && onEnter) {
      event.preventDefault();
      onEnter();
    } else if (event.key === 'Escape' && onEscape) {
      event.preventDefault();
      onEscape();
    }
  });
}

/** 4a, propuesta: nombre · tamaño · «Qué habilita» · «Ahora no» / «Instalar». */
function buildProposalCard(model, { onInstall, onDecline }, documentRef = document, now = undefined) {
  const root = card(documentRef, 'blue');
  const body = el(documentRef, 'div', 'cc-body');
  const nameLine = el(documentRef, 'div', 'cc-name-line');
  nameLine.append(el(documentRef, 'span', 'cc-name', model.name), el(documentRef, 'span', 'cc-size', model.size));
  const footer = el(documentRef, 'div', 'cc-buttons');
  const decline = button(documentRef, model.decline, '', () => settle(onDecline));
  const install = button(documentRef, model.accept, 'primary', () => settle(onInstall));
  footer.append(el(documentRef, 'span', 'cc-shortcuts', model.shortcuts), decline, install);
  body.append(nameLine, grid(documentRef, [{ label: model.enablesLabel, value: model.enables }]), footer);
  root.append(head(documentRef, 'blue', model.title), body);

  function settle(action) {
    if (install.disabled) return; // a key and a click can race
    install.disabled = true;
    decline.disabled = true;
    action();
  }
  guardKeys(root, { onEnter: () => settle(onInstall), onEscape: () => settle(onDecline) }, now);
  return { el: root, buttons: { install, decline } };
}

/** 4a, instalando: «Instalando {nombre}» · barra · «{descargado} / {tamaño}». */
function buildInstallingCard(model, documentRef = document) {
  const root = card(documentRef, 'blue');
  const body = el(documentRef, 'div', 'cc-body');
  const progress = el(documentRef, 'div', 'cc-progress');
  const bar = el(documentRef, 'span', 'cc-bar');
  const fill = el(documentRef, 'span', 'cc-bar-fill');
  bar.append(fill);
  const text = el(documentRef, 'span', 'cc-progress-text', model.progress);
  progress.append(bar, text);
  body.append(progress);
  const title = head(documentRef, 'blue', model.title, { spinner: true });
  root.append(title, body);
  const update = (next) => {
    fill.style.width = `${next.percent}%`;
    text.textContent = escapeControlChars(String(next.progress));
  };
  update(model);
  return { el: root, update };
}

/** 4a, falló: «No se pudo instalar {nombre}» · {error real} · «Ignorar» / «Reintentar». */
function buildFailedCard(model, { onIgnore, onRetry }, documentRef = document) {
  const root = card(documentRef, 'red');
  const body = el(documentRef, 'div', 'cc-body');
  const footer = el(documentRef, 'div', 'cc-buttons');
  const ignore = button(documentRef, model.ignore, '', () => settle(onIgnore));
  const retry = button(documentRef, model.retry, 'primary', () => settle(onRetry));
  footer.append(el(documentRef, 'span', 'cc-spacer'), ignore, retry);
  // The real error when there is one (ENOSPC…, ETIMEDOUT…). Nothing when the failure has no
  // text of its own (a build with no pinned download), and never a sentence of ours.
  if (model.error) body.append(el(documentRef, 'div', 'cc-error', model.error));
  body.append(footer);
  root.append(head(documentRef, 'red', model.title), body);
  function settle(action) {
    if (retry.disabled) return;
    retry.disabled = true;
    ignore.disabled = true;
    action();
  }
  return { el: root, buttons: { ignore, retry } };
}

/** A finished row in the tool-row look: ▶ «Instaló» Navegador sin interfaz · 160 MB · 14 s. */
function buildRow(model, documentRef = document) {
  const root = el(documentRef, 'div', 'cc-row');
  root.append(
    el(documentRef, 'span', 'cc-row-arrow', '▶'),
    el(documentRef, 'span', 'cc-row-verb', model.verb),
    el(documentRef, 'span', 'cc-row-object', model.object),
    el(documentRef, 'span', 'cc-spacer'),
  );
  if (model.meta) root.append(el(documentRef, 'span', 'cc-row-meta', model.meta));
  return root;
}

/** The pill a resolved card becomes (design 1o's pill), plus the saved-decision line when
 * something was saved: «Rechazaste · {nombre}» · «guardado en Decisiones guardadas…». */
function buildResolution(model, documentRef = document) {
  const root = el(documentRef, 'div', 'cc-resolution');
  root.append(buildPill(model.pill, documentRef));
  if (model.saved) root.append(el(documentRef, 'span', 'cc-saved', model.saved));
  return root;
}

/** 4b, no disponible: «Falta un programa» · «{programa} no está instalado» · the command
 * with «Copiar», or «Ningún gestor…» with the official link and «Abrir sitio oficial ↗» ·
 * «Entendido». */
function buildMissingProgramCard(model, { onCopy, onOpenOfficial, onDone }, documentRef = document) {
  const root = card(documentRef, 'grey');
  const body = el(documentRef, 'div', 'cc-body');
  body.append(el(documentRef, 'div', 'cc-heading', model.heading));
  const buttons = {};
  if (model.command) {
    const box = el(documentRef, 'div', 'cc-command');
    buttons.copy = button(documentRef, model.copy, 'small', onCopy);
    box.append(el(documentRef, 'span', 'cc-command-text', model.command), buttons.copy);
    body.append(box);
  } else {
    body.append(el(documentRef, 'div', 'cc-note', model.noManager));
    if (model.link) {
      const official = el(documentRef, 'div', 'cc-official');
      buttons.open = button(documentRef, model.openOfficial, 'square', onOpenOfficial);
      official.append(el(documentRef, 'span', 'cc-mono cc-link', model.link), buttons.open);
      body.append(official);
    }
  }
  const footer = el(documentRef, 'div', 'cc-buttons');
  buttons.done = button(documentRef, model.done, '', onDone);
  footer.append(el(documentRef, 'span', 'cc-spacer'), buttons.done);
  body.append(footer);
  root.append(head(documentRef, 'grey', model.title), body);
  return { el: root, buttons };
}

/** 4b, permiso, both variants. With `installFirst`, the install block comes first and the
 * allow button is «Instalar y permitir». */
function buildPermissionCard(model, { onDeny, onAllow }, documentRef = document) {
  const root = card(documentRef, 'amber');
  const body = el(documentRef, 'div', 'cc-body');
  if (model.installFirst) {
    const first = el(documentRef, 'div', 'cc-install-first');
    const line = el(documentRef, 'div', 'cc-install-first-line');
    line.append(
      el(documentRef, 'span', 'cc-install-first-label', model.installFirst.label),
      el(documentRef, 'span', 'cc-size', model.installFirst.size),
    );
    first.append(
      line,
      el(documentRef, 'span', 'cc-install-first-name', model.installFirst.name),
      el(documentRef, 'span', 'cc-install-first-enables', model.installFirst.enables),
    );
    body.append(first);
  }
  const footer = el(documentRef, 'div', 'cc-buttons');
  const deny = button(documentRef, model.deny, 'danger', () => settle(onDeny));
  const allow = button(documentRef, model.allow, 'primary', () => settle(onAllow));
  footer.append(el(documentRef, 'span', 'cc-spacer'), deny, allow);
  body.append(grid(documentRef, model.rows), footer);
  root.append(head(documentRef, 'amber', model.title), body);
  function settle(action) {
    if (allow.disabled) return;
    allow.disabled = true;
    deny.disabled = true;
    action();
  }
  return { el: root, buttons: { deny, allow } };
}

/** 4b, paso detenido: «Motivo» · «Dónde» · «Se sigue sin este paso.» Nothing to answer. */
function buildStepStoppedCard(model, documentRef = document) {
  const root = card(documentRef, 'amber');
  root.classList.add('cc-card--quiet');
  const body = el(documentRef, 'div', 'cc-body');
  body.append(grid(documentRef, model.rows), el(documentRef, 'div', 'cc-note', model.footer));
  root.append(head(documentRef, 'amber', model.title), body);
  return { el: root };
}

export {
  guardKeys,
  buildProposalCard,
  buildInstallingCard,
  buildFailedCard,
  buildRow,
  buildResolution,
  buildMissingProgramCard,
  buildPermissionCard,
  buildStepStoppedCard,
  JUST_APPEARED_MS,
};
