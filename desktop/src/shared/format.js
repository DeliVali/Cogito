'use strict';

/**
 * Formatters shared by the main process and the renderer (design 2b, 2c, 2d, 2f, 1j):
 * token and money shorthand, the resume date in local time, and waits in whole units.
 * Dependency-free so `cogito-lab` can require it without Electron.
 *
 * The renderer keeps its own copy (`renderer/format.js`) for the CSP reason given in
 * `phase-map.js`. A lab test checks the two agree. Keep them in sync.
 */

/** Spanish month abbreviations as the design writes a date («1 oct, 18:00»). */
const MONTHS = Object.freeze(['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sept', 'oct', 'nov', 'dic']);

/**
 * The locale `formatMoney` uses. Module state so call sites need not pass it: `main/tray.js`
 * and `main/notifications.js` call `setLocale(app.getLocale())` once, and the renderer copy
 * defaults to `navigator.language`. Lab tests never set it, so they see 'en-US'.
 */
let _locale = 'en-US';

/** Any Spanish locale is written the Spanish way (comma for decimals, «US$»). Chromium
 * reports a Latin American machine as 'es-419', whose own convention is a point and the
 * code «USD»: that half-English mix read as untranslated in a Spanish window (QA 2026-10-13). */
function spanishAsSpain(locale) {
  return /^es([-_]|$)/i.test(locale) ? 'es' : locale;
}

function setLocale(locale) {
  if (typeof locale === 'string' && locale) _locale = spanishAsSpain(locale);
}

/** 214k, 1.2M, 640k, 1.5k, 999: the design's shorthand. */
function formatTokens(count) {
  const n = Math.max(0, Math.round(Number(count) || 0));
  const trim = (value) => value.replace(/\.0$/, '');
  if (n < 1000) return String(n);
  if (n < 10_000) return `${trim((n / 1000).toFixed(1))}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${trim((n / 1_000_000).toFixed(1))}M`;
}

/** "106 225", "43 008", "8192": an exact count from the provider, where the «k» shorthand
 * would round it. Digits are grouped by threes from five digits up with a thin space, never
 * a point or comma, per the RAE (rae.es, Ortografía, «Los números enteros y el separador de
 * millares», read 2026-09-30). U+202F NARROW NO-BREAK SPACE also keeps the number on one
 * line. The bundled Open Sans has no glyph for it (cmap checked 2026-09-30), so the page
 * draws that character from a system font. */
function formatCount(count) {
  const digits = String(Math.max(0, Math.round(Number(count) || 0)));
  if (digits.length < 5) return digits;
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, '\u202f');
}

/** "$10.00", "€14.20", "MX$10.00": two decimals, in the amount's currency and the locale
 * `setLocale` was last given. */
function formatMoney(amount, currency) {
  const value = Number(amount) || 0;
  try {
    return new Intl.NumberFormat(_locale, {
      style: 'currency',
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

/** "10,00" in Spanish, "10.00" otherwise: an amount as an input field shows it, in the
 * locale `setLocale` was last given, two decimals and no grouping. */
function formatDecimal(amount) {
  const value = Number(amount) || 0;
  try {
    return new Intl.NumberFormat(_locale, { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false }).format(value);
  } catch {
    return value.toFixed(2);
  }
}

/** «30 sept, 18:00»: the local day and time of `iso`, or '' when unreadable. The hour is
 * shown because a day alone can read as today or tomorrow depending on the UTC offset. */
function formatResumeDate(iso) {
  const when = Date.parse(iso);
  if (!Number.isFinite(when)) return '';
  const date = new Date(when);
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${date.getDate()} ${MONTHS[date.getMonth()]}, ${hh}:${mm}`;
}

/** "42 s", "3 min", "1 h 5 min": whole units, rounded up so it never reads "0 s" early. */
function formatWait(seconds) {
  const total = Math.max(1, Math.ceil(seconds));
  if (total < 60) return `${total} s`;
  const minutes = Math.ceil(total / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

/** A seconds figure from the provider, rounded to one decimal, with no trailing ".0". */
function formatSeconds(value) {
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 10) / 10);
}

module.exports = { MONTHS, setLocale, formatTokens, formatCount, formatMoney, formatDecimal, formatResumeDate, formatWait, formatSeconds };
