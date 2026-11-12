'use strict';

/**
 * Automatic updates in the window (design 3a/3b): the chip beside the model, its menu or
 * card, the security strip, the «Actualizando a X» screen, and the thread separator after a
 * restart. Loaded as its own module from `index.html` so the chat module does not need to
 * know about updates.
 *
 * The page draws what the main process's snapshot says and reports choices back through
 * `window.cogito.update`. It never decides whether, what or when to install. Bridge promises
 * are caught, and a window without the update bridge (the lab's older e2e harness, an older
 * preload) shows nothing.
 *
 * Menus open with the design's `cubic-bezier(.2,.9,.3,1.2)` and close on an outside click or
 * Escape with its shrink and fade (140 ms, in `update.css`). Sounds follow the design's
 * `_menu`, `upNow`, `upWhenDone`, `upLater` and `_upDownload`: `open` and `close` for a menu,
 * `window` for «Reiniciar ahora», `select` for «Reiniciar al terminar», `close` for «Al
 * salir», and `open` when a download becomes ready. The download itself is silent («sin
 * sonido ni menú»).
 */

import { updateView, separatorView } from './update.js';
import {
  buildChip,
  buildMenu,
  buildBanner,
  buildRestarting,
  buildCard,
  buildSeparator,
} from './render-update.js';
import { sfx } from './sfx.js';

const CLOSE_MS = 140;
const quietly = (promise) => promise?.catch?.(() => {});

function start(api, play = sfx) {
  const contextbar = document.querySelector('.contextbar');
  if (!contextbar) return;
  const slot = document.createElement('span');
  slot.className = 'update-slot';
  contextbar.insertBefore(slot, contextbar.querySelector('.model'));

  let state = null;
  let chipKey = null; // the chip view last drawn, so an unchanged chip is never redrawn
  let popover = null; // {kind: 'menu' | 'card', el, key}
  let strip = null;
  let restarting = null;
  const autoOpened = new Set();

  /** The chip is drawn pressed while its menu or card is open (the design's `upChipBg`). */
  function markChip(open) {
    const chip = slot.firstElementChild;
    if (!chip || chip.tagName !== 'BUTTON') return;
    chip.classList.toggle('is-open', open);
    chip.setAttribute('aria-expanded', String(open));
  }

  function closePopover({ animate = true, sound = false, returnFocus = false } = {}) {
    if (!popover) return;
    const { el } = popover;
    // Focus inside the popover goes back to the chip that opened it, so a keyboard user
    // is not left on a node that is about to leave the page.
    const hadFocus = el.contains(document.activeElement);
    popover = null;
    markChip(false);
    if (returnFocus || hadFocus) {
      const chip = slot.firstElementChild;
      if (chip?.tagName === 'BUTTON') chip.focus();
    }
    if (sound) play('close');
    if (!animate) {
      el.remove();
      return;
    }
    el.classList.add('is-closing');
    setTimeout(() => el.remove(), CLOSE_MS);
  }

  // What a popover shows. `autoOpen` is left out: it flips once the card has been seen,
  // and that must not redraw it.
  const popoverKey = (kind, view) => JSON.stringify(kind === 'menu' ? view.menu : { ...view.card, autoOpen: null });

  /** Arrow keys and Tab move within the popover's buttons, wrapping at both ends. */
  function onPopoverKeydown(event) {
    const keys = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    const step = event.key === 'Tab' ? (event.shiftKey ? -1 : 1) : keys[event.key];
    const buttons = [...event.currentTarget.querySelectorAll('button')];
    if (!step || !buttons.length) return;
    event.preventDefault();
    const at = buttons.indexOf(document.activeElement);
    buttons[at === -1 ? (step > 0 ? 0 : buttons.length - 1) : (at + step + buttons.length) % buttons.length].focus();
  }

  /** `focus`: move keyboard focus into the popover (the person opened it; a card that opens
   * by itself must not take focus). A popover redrawn while focus was inside keeps it. */
  function openPopover(kind, view, { focus = false } = {}) {
    const anchor = slot.firstElementChild;
    if (!anchor) return;
    focus = focus || Boolean(popover?.el.contains(document.activeElement));
    closePopover({ animate: false });
    const el =
      kind === 'menu'
        ? buildMenu(view.menu, {
            onPrimary: (action) => {
              play(action === 'restart-when-done' ? 'select' : 'window');
              closePopover();
              quietly(action === 'restart-when-done' ? api.restartWhenDone() : api.restartNow());
            },
            // «Al salir» sends `defer-to-quit` as well as closing the menu.
            onLater: (action) => {
              closePopover({ sound: true, returnFocus: true });
              quietly(action === 'defer-to-quit' ? api.deferToQuit() : null);
            },
          })
        : buildCard(view.card, {
            onCopy: () => {
              play('click');
              quietly(api.copyCommand());
            },
            onDismiss: () => {
              closePopover({ sound: true, returnFocus: true });
              quietly(api.cardSeen());
            },
          });
    // Under the chip with right edges aligned, as in the design, but kept inside the window
    // (the chip can sit far to the left).
    const rect = anchor.getBoundingClientRect();
    el.style.setProperty('top', `${Math.round(rect.bottom + 3)}px`);
    document.body.append(el);
    const width = el.offsetWidth;
    const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
    el.style.setProperty('left', `${Math.round(left)}px`);
    popover = { kind, el, key: popoverKey(kind, view) };
    markChip(true);
    el.addEventListener('keydown', onPopoverKeydown);
    if (focus) {
      // The primary choice (last button of the menu) or «Entendido» (the card's): the one
      // Enter should confirm.
      const buttons = el.querySelectorAll('button');
      (kind === 'menu' ? el.querySelector('.update-pill--primary') : buttons[buttons.length - 1])?.focus();
    }
  }

  function onChip(chip) {
    if (chip.action === 'retry') {
      play('click');
      quietly(api.retry());
      return;
    }
    if (!chip.opens) return;
    if (popover?.kind === chip.opens) {
      closePopover({ sound: true });
    } else {
      play('open');
      openPopover(chip.opens, updateView(state), { focus: true });
    }
  }

  function renderStrip(view) {
    if (view.banner) {
      const next = buildBanner(view.banner, {
        onRestart: () => {
          play('window');
          quietly(api.restartNow());
        },
      });
      if (strip) strip.replaceWith(next);
      else contextbar.after(next);
      strip = next;
      document.body.classList.add('has-update-strip');
    } else if (strip) {
      strip.remove();
      strip = null;
      document.body.classList.remove('has-update-strip');
    }
  }

  function renderRestarting(view) {
    if (view.restarting && !restarting) {
      closePopover({ animate: false });
      restarting = buildRestarting(view.restarting);
      document.body.append(restarting);
    } else if (!view.restarting && restarting) {
      restarting.remove();
      restarting = null;
    }
  }

  function renderChip(view) {
    const key = view.chip ? JSON.stringify(view.chip) : null;
    if (key === chipKey) return;
    chipKey = key;
    const current = slot.firstElementChild;
    // Only a finished download plays `open` (the design's `_upDownload`).
    if (view.chip?.kind === 'ready' && current?.classList.contains('update-chip--downloading')) play('open');
    // While downloading only the percentage changes. Patch it in place so the chip does not
    // replay its entrance on every progress step.
    if (view.chip?.kind === 'downloading' && current?.classList.contains('update-chip--downloading')) {
      current.lastChild.data = view.chip.label;
      current.firstElementChild.style.setProperty('--update-deg', `${Math.round(view.chip.percent * 3.6)}deg`);
      return;
    }
    slot.replaceChildren();
    if (view.chip) slot.append(buildChip(view.chip, { onClick: () => onChip(view.chip) }));
  }

  function render() {
    const view = updateView(state);
    renderChip(view);

    // Redraw an open menu or card only when its content changed (busy flips «Reiniciar
    // ahora» to «Reiniciar al terminar»), and close it when it no longer applies.
    if (popover) {
      const kind = popover.kind;
      if (!(kind === 'menu' ? view.menu : view.card) || !slot.firstElementChild) closePopover({ animate: false });
      else if (popoverKey(kind, view) !== popover.key) openPopover(kind, view);
    }
    // The package-manager card opens by itself once per version (design 3b).
    if (view.card?.autoOpen && !autoOpened.has(state.version)) {
      autoOpened.add(state.version);
      openPopover('card', view);
      quietly(api.cardSeen());
    }

    renderStrip(view);
    renderRestarting(view);
  }

  document.addEventListener('mousedown', (event) => {
    if (!popover || popover.el.contains(event.target) || slot.contains(event.target)) return;
    closePopover({ sound: true });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && popover) closePopover({ sound: true, returnFocus: true });
  });
  window.addEventListener('resize', () => closePopover({ animate: false }));

  api.onState((next) => {
    state = next;
    render();
  });
  quietly(
    api.get().then((initial) => {
      if (initial && !state) {
        state = initial;
        render();
      }
    }),
  );
  // The separator where the thread resumes after a restart: «— ACTUALIZADO A X —» or
  // «— SE MANTUVO X · NO SE PUDO ACTUALIZAR —». The main process hands it over once.
  quietly(
    api.takeOutcome().then((outcome) => {
      const view = separatorView(outcome);
      if (view) document.querySelector('.column')?.append(buildSeparator(view));
    }),
  );
}

if (window.cogito?.update) start(window.cogito.update);
