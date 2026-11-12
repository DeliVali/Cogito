'use strict';

/**
 * Expand and collapse as the design canvas does (1a's tool rows, 1l's diff): the body's
 * grid row grows from 0fr to 1fr in .24 s while its contents fade in, and the ▶ turns 90°
 * on the menus' spring curve. The motion lives in styles.css
 * (`.tool-block-body.is-expander`); this only flips `.is-open`. Opening plays the canvas'
 * `open` sound and closing plays `close` (`this.sfx(o ? 'close' : 'open')`).
 *
 * The header also becomes a keyboard control (role=button, Enter/Space). `documentRef`
 * and `play` are injectable so the render modules that use this stay testable against
 * `cogito-lab`'s fake DOM. `onToggle(isOpen)` hears every open and close: a tool row uses
 * it to build its content on first open and to stay open across a redraw of its segment.
 */

import { sfx } from './sfx.js';

function makeExpander({ header, arrow, content, open = false, documentRef = document, play = sfx, onToggle = () => {} }) {
  const body = documentRef.createElement('div');
  body.className = 'tool-block-body is-expander';
  const inner = documentRef.createElement('div');
  inner.className = 'tool-block-inner';
  inner.append(content);
  body.append(inner);

  let isOpen = Boolean(open);
  const apply = () => {
    body.classList.toggle('is-open', isOpen);
    if (arrow) arrow.style.transform = isOpen ? 'rotate(90deg)' : 'rotate(0deg)';
    header.setAttribute('aria-expanded', String(isOpen));
  };
  const toggle = () => {
    isOpen = !isOpen;
    onToggle(isOpen);
    play(isOpen ? 'open' : 'close');
    apply();
  };

  header.classList.add('is-expandable');
  header.setAttribute('role', 'button');
  header.tabIndex = 0;
  header.addEventListener('click', toggle);
  header.addEventListener('keydown', (event) => {
    if (event.target !== header || (event.key !== 'Enter' && event.key !== ' ')) return;
    event.preventDefault();
    toggle();
  });
  apply();
  return { body, toggle, isOpen: () => isOpen };
}

export { makeExpander };
