'use strict';

/**
 * xterm.js 6.0.0 styles itself in two ways the page's CSP (`style-src 'self'`, no
 * 'unsafe-inline') refuses:
 *
 * - three `<style>` elements it creates in `open()` and fills with text: the DOM renderer's
 *   theme and cell size (`DomRenderer._injectCss`, `_updateDimensions`) and the viewport's
 *   scrollbar colours (`Viewport`);
 * - `setAttribute('style', …)` on a cell with a true colour or a contrast-corrected colour
 *   (`DomRendererRowFactory._addStyle`).
 *
 * Both go through CSSOM here instead, which the CSP does not govern: each `<style>` becomes
 * an inert stand-in whose text feeds a constructed stylesheet adopted by the document, and
 * a cell's style is appended to `element.style.cssText`. The policy stays as it is.
 *
 * The private names are 6.0.0's (the vendored build keeps them unminified). A later build
 * that renames them leaves the cell patch out and `openWithinCsp` says so.
 */

/** A `<template>` (never rendered) whose `textContent` is a constructed stylesheet's text. */
function sheetHolder(doc) {
  const node = Document.prototype.createElement.call(doc, 'template');
  const view = doc.defaultView || globalThis;
  const sheet = new view.CSSStyleSheet();
  doc.adoptedStyleSheets = [...doc.adoptedStyleSheets, sheet];
  let text = '';
  Object.defineProperty(node, 'textContent', {
    configurable: true,
    get: () => text,
    set: (value) => {
      text = value == null ? '' : String(value);
      sheet.replaceSync(text);
    },
  });
  const remove = node.remove.bind(node);
  node.remove = () => {
    doc.adoptedStyleSheets = doc.adoptedStyleSheets.filter((item) => item !== sheet);
    remove();
  };
  return node;
}

/** A cell's style through CSSOM. Patched once on the row factory's class. */
function patchCellStyles(term) {
  const factory = term && term._core && term._core._renderService && term._core._renderService._renderer
    ? term._core._renderService._renderer.value && term._core._renderService._renderer.value._rowFactory
    : null;
  const proto = factory ? Object.getPrototypeOf(factory) : null;
  if (!proto || typeof proto._addStyle !== 'function') return false;
  if (!proto.__cogitoCssom) {
    proto._addStyle = function addStyle(element, style) {
      element.style.cssText += `${style};`;
    };
    proto.__cogitoCssom = true;
  }
  return true;
}

/** `term.open(host)` with xterm's styles routed through CSSOM. Returns whether the cell
 * patch applied (false only for an xterm build whose private names changed). */
function openWithinCsp(term, host, documentRef = document) {
  const doc = documentRef;
  const hadOwn = Object.prototype.hasOwnProperty.call(doc, 'createElement');
  const previous = doc.createElement;
  doc.createElement = function createElement(tag, options) {
    if (String(tag).toLowerCase() === 'style') return sheetHolder(doc);
    return previous.call(this, tag, options);
  };
  try {
    term.open(host);
  } finally {
    if (hadOwn) doc.createElement = previous;
    else delete doc.createElement;
  }
  const patched = patchCellStyles(term);
  if (!patched) console.warn('xterm: cell colours need the style attribute, which the CSP refuses');
  return patched;
}

export { openWithinCsp, patchCellStyles };
