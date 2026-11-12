// The model the core started with could not be served, so no project opened. One card says
// so, with «Reintentar». A retry or a picker switch that serves a model opens the project
// that failed, as the welcome's finish did. A failed retry replaces the card instead of
// adding another, and the button stays disabled while a retry runs.

/**
 * `activate(modelId)` serves a model without a session (`connect.activate`).
 * `placeCard(card)` puts a card in the thread. `buildCard(view, {onRetry})` and
 * `viewOf(unserved)` draw it. `settle(card)` keeps a card as a record, `supersede(card)`
 * takes it out. `onServed(answer, path)` opens the app once a model serves.
 */
function createStartupFailure({ activate, placeCard, buildCard, viewOf, settle, supersede, onServed }) {
  let card = null;
  let modelId = null;
  let path = null;
  let retrying = false;

  function setRetryDisabled(disabled) {
    if (!card) return;
    for (const button of card.querySelectorAll('.service-error-retry')) button.disabled = disabled;
  }

  /** Draws the failure, replacing any earlier card. `projectPath` is the project to open
   * once a model serves; left out, the one already remembered is kept. */
  function show(unserved, projectPath) {
    if (projectPath !== undefined) path = projectPath;
    modelId = unserved.model.id;
    const next = buildCard(viewOf(unserved), { onRetry: () => retry() });
    if (card) supersede(card);
    card = next;
    placeCard(next);
  }

  async function retry() {
    if (retrying || !card) return;
    retrying = true;
    setRetryDisabled(true);
    const id = modelId;
    const answer = await Promise.resolve()
      .then(() => activate(id))
      .catch((error) => ({
        error: 'failed',
        model: { id },
        log: [error && error.message ? String(error.message) : ''],
      }));
    retrying = false;
    if (answer && !answer.error) {
      served(answer);
      return;
    }
    show({ reason: 'failed', model: (answer && answer.model) || { id }, log: answer && answer.log });
  }

  /** A model serves now (a retry, or the picker). Returns false when no failure was shown. */
  function served(answer) {
    if (!card) return false;
    settle(card);
    card = null;
    modelId = null;
    const opening = path;
    path = null;
    onServed(answer, opening);
    return true;
  }

  return { show, retry, served, active: () => card !== null };
}

export { createStartupFailure };
