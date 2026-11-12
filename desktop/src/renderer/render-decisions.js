'use strict';

/**
 * DOM for the decision pill that `decisions.js`'s `pillFor` describes (design 1o: "Sale
 * donde estaba la tarjeta que resolvió": the pill takes the resolved card's place).
 */

import { escapeControlChars } from './sanitize.js';

/** A small icon badge plus a "Permitiste · git push origin main" pill, toned green/grey/blue
 * by `pill.tone` (styles.css's `.pill--green|grey|blue`). `pill.icon` is empty for a pill
 * that is not a decision either way (e.g. a timed-out approval), and then no badge is
 * built. */
function buildPill(pill, documentRef = document) {
  const el = documentRef.createElement('span');
  el.className = `pill pill--${pill.tone}`;

  if (pill.icon) {
    const icon = documentRef.createElement('span');
    icon.className = 'pill-icon';
    icon.append(documentRef.createTextNode(pill.icon));
    el.append(icon);
  }

  const text = documentRef.createElement('span');
  text.className = 'pill-text';
  // `object` is already neutralized (decisions.js's `displayText`). A component pill's name
  // comes from the core and goes through the same rule here. The verb takes the pill's
  // weight and the object its lighter tone (design 1a); together they still read
  // «{verb} · {object}».
  const verb = documentRef.createElement('span');
  verb.className = 'pill-verb';
  verb.append(documentRef.createTextNode(escapeControlChars(pill.label)));
  text.append(verb);
  if (pill.object) {
    const object = documentRef.createElement('span');
    object.className = 'pill-object';
    object.append(documentRef.createTextNode(escapeControlChars(` · ${pill.object}`)));
    text.append(object);
    // The whole command (a heredoc's body, say) is cut from the one-line pill; the native
    // tooltip keeps it (`pillFor`'s `full`), as the tool row's header does.
    if (pill.full && pill.full !== pill.object) text.title = pill.full;
  }

  el.append(text);
  return el;
}

/**
 * Swap `block` (the resolved approval card) for its pill in the same position among its
 * siblings (design 1o: "Sale donde estaba la tarjeta que resolvió", "comes out where the
 * card that resolved it was"). `block.replaceWith` does the swap. This function exists so
 * the unit suite exercises it against the fake-element shape this module's tests use.
 */
function resolveToPill(block, pill, documentRef = document) {
  const el = buildPill(pill, documentRef);
  block.replaceWith(el);
  return el;
}

export { buildPill, resolveToPill };
