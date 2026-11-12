'use strict';

/**
 * The amber chip beside the model that warns of a near API rate limit (design 2e). Not a
 * spend feature: `rate_limit_near` comes from the provider's per-minute limiters.
 *
 * - `rateNearView`: `rate_limit_near` (or `GET /usage`'s `active.rate_limit`) → the chip's
 *   words and tooltip, hidden once `recovered`.
 * - `buildRateNearChip`: its DOM; `update(view)` shows, rewords or hides it.
 *
 * Every text is `strings.js`'s `RATE_LIMIT`. The core's values go through the sanitizer.
 */

import { RATE_LIMIT } from './strings.js';
import { escapeControlChars } from './sanitize.js';
import { formatTokens } from './format.js';

/** The limiters the core counts (`provider/ratelimit.py`): the combined `tokens` one only
 * where no split was reported. */
function countedLimiters(limits) {
  const names = ['requests', 'input_tokens', 'output_tokens'].filter((name) => limits[name]);
  if (limits.tokens && !names.some((name) => name !== 'requests')) names.push('tokens');
  return names;
}

function rateNearView(event) {
  if (!event || event.recovered || typeof event.percent !== 'number') return { visible: false, label: '', tooltip: '' };
  const limits = event.limits && typeof event.limits === 'object' ? event.limits : {};
  const tooltip = countedLimiters(limits)
    .map((name) => {
      const limiter = limits[name];
      if (typeof limiter.remaining !== 'number' || typeof limiter.limit !== 'number') return null;
      return RATE_LIMIT.limiter(RATE_LIMIT.limiters[name], formatTokens(limiter.remaining), formatTokens(limiter.limit));
    })
    .filter(Boolean)
    .join(' · ');
  return { visible: true, label: RATE_LIMIT.rateNear(Math.max(0, Math.floor(event.percent))), tooltip };
}

function buildRateNearChip(documentRef = document) {
  const make = (tag, className) => {
    const node = documentRef.createElement(tag);
    node.className = className;
    return node;
  };
  const chip = make('span', 'rate-near');
  chip.hidden = true;
  const text = make('span', 'rate-near-text');
  chip.append(make('span', 'rate-near-dot'), text);
  return {
    el: chip,
    update(view) {
      chip.hidden = !view.visible;
      text.textContent = escapeControlChars(view.label);
      chip.title = escapeControlChars(view.tooltip);
    },
  };
}

export { rateNearView, buildRateNearChip };
