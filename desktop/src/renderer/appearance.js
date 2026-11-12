'use strict';

/**
 * Pure models for «Diseño» in «Herramientas y permisos»: one row per setting, so later
 * settings are one more entry in `settingModels`. No DOM here: tools-window.js draws these.
 *
 * The theme is applied by the main process (src/main/appearance.js); this only says what the
 * row shows and which choice is on.
 */

import { TOOLS_WINDOW } from './strings.js';

/** The theme's choices, in the order the row shows them. The main process accepts these. */
const THEMES = ['light', 'dark', 'system'];
const DEFAULT_THEME = 'system';

/** «Tema»: a choice of three, `active` on the saved one. A theme not yet read (null) or not
 * known marks none, so the row never claims a choice the main process did not report. */
function themeSettingModel(theme) {
  const known = THEMES.includes(theme) ? theme : null;
  return {
    key: 'theme',
    label: TOOLS_WINDOW.theme.label,
    kind: 'choice',
    options: THEMES.map((value) => ({
      value,
      label: TOOLS_WINDOW.theme.choices[value],
      active: value === known,
    })),
  };
}

/** Every row of «Diseño», top to bottom. `state.theme` is what `appearance:get` answered. */
function settingModels(state = {}) {
  return [themeSettingModel(state.theme ?? null)];
}

export { THEMES, DEFAULT_THEME, themeSettingModel, settingModels };
