'use strict';

/**
 * Finds the model `serve` announced in `GET /models`. Pure, so `cogito-lab`'s
 * `tests/desktop/` can import it from plain Node.
 */

/** The catalog entry `serve` announced. `serve` prints the label, so match that or the id. */
function findModel(groups, labelOrId) {
  for (const group of groups || []) {
    for (const model of group.models || []) {
      if (model.label === labelOrId || model.id === labelOrId) return model;
    }
  }
  return null;
}

export { findModel };
