'use strict';

/**
 * «Ir al final»: the round ↓ button above the composer, as Claude Code has one. It shows
 * while the reader is scrolled up away from the tail, and a dot on it says something new
 * arrived below meanwhile (the thread never pulls a reader down on its own; see chat.js's
 * `keepUp`). A click or Enter/Space scrolls to the end, smoothly unless the system asks
 * for less motion, and the button goes once the end is reached.
 *
 * No DOM of its own and no clock: it is handed the elements, so `cogito-lab` runs it
 * against a fake document.
 */

/** Further than this from the end counts as scrolled up. More than chat.js's 40 px
 * "at the bottom", so the button does not blink in and out at the edge. */
const AWAY_PX = 120;

function createJumpLatest({ button, dot, transcript, reducedMotion = () => false, afterJump = () => {} }) {
  let away = false;

  const distance = () => transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;

  function show(visible) {
    button.hidden = !visible;
    button.setAttribute('aria-hidden', visible ? 'false' : 'true');
    button.tabIndex = visible ? 0 : -1;
    if (!visible) dot.hidden = true;
  }

  /** Called on every scroll of the thread, and whenever its content or its window changes
   * size (chat.js watches both): the arrow shows only while there is something below the
   * fold, never over a thread that no longer scrolls. */
  function update() {
    away = distance() > AWAY_PX;
    show(away);
  }

  /** Something was added below while the reader is away from it. `away` is read again from
   * the thread itself: it can be stale when the thread shrank (a rewind took messages out,
   * no scroll event followed) and then there is nothing below to point at. */
  function markNew() {
    if (distance() <= AWAY_PX) {
      update();
      return;
    }
    away = true;
    show(true);
    dot.hidden = false;
  }

  function jump() {
    const top = transcript.scrollHeight;
    if (typeof transcript.scrollTo === 'function') {
      transcript.scrollTo({ top, behavior: reducedMotion() ? 'auto' : 'smooth' });
    } else {
      transcript.scrollTop = top;
    }
    dot.hidden = true;
    afterJump();
  }

  button.addEventListener('click', jump);
  show(false);
  return { update, markNew, jump, isShown: () => !button.hidden };
}

export { createJumpLatest, AWAY_PX };
