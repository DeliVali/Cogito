'use strict';

/**
 * Defends against two things a path, command or pill object can carry that plain-text
 * rendering does not neutralize:
 *
 * - Bidi override/embedding characters (U+202A-U+202E, U+2066-U+2069, U+200E/U+200F) can
 *   make a filename display as something it is not: `src/‮gnp.exe` reads as
 *   `src/exe.png` once the right-to-left override flips the rest of the run. No markup is
 *   involved, so an HTML-injection check does not catch it.
 * - Other C0/C1 control characters (vertical tabs, form feeds, ESC, ...) can break a
 *   single-line row's layout or hide part of the text.
 *
 * Both are replaced with a visible literal escape, `‹U+202E›`, so nothing is silently
 * dropped and the character never has its display effect.
 */

// eslint-disable-next-line no-control-regex -- the whole point is to match control chars
const CONTROL_CHARS =
  /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F‎‏‪-‮⁦-⁩]/g;

/** `\n`, `\r` and `\t` are left alone: they are common in real content (a multi-line
 * error, an indented command), carry no bidi or spoofing risk, and CSS
 * (`white-space: pre-wrap`) already renders them safely. */
function escapeControlChars(text) {
  if (typeof text !== 'string' || !text) return text;
  return text.replace(CONTROL_CHARS, (ch) => {
    const code = ch.codePointAt(0).toString(16).toUpperCase().padStart(4, '0');
    return `‹U+${code}›`;
  });
}

export { escapeControlChars };
