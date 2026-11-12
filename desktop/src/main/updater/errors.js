'use strict';

/**
 * An updater failure: a code the state machine acts on and a message for the log. The code
 * decides behaviour (a failed check stays silent, a failed verification shows «No se pudo
 * actualizar»). The message is never shown in the window, whose texts are fixed.
 */
class UpdateError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'UpdateError';
    this.code = code;
  }
}

module.exports = { UpdateError };
