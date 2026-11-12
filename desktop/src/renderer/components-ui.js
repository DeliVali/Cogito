'use strict';

/**
 * The window's side of the component catalog: the cards of design 4a/4b, driven by the
 * core's events, and the model menu (the picker, and «Herramientas y permisos…», 4e).
 *
 * chat.js passes every event its own switch does not handle to `handleEvent`, and provides
 * `placeCard(element)`, which puts a card in the running turn's answer, or in a block of
 * its own when no turn runs (the scan's missing-program card arrives before any turn). The
 * rest lives here so chat.js stays small.
 *
 * Every card resolves in place, as the approval card does (design 1o):
 *   - propuesta → «Instalar» → instalando → a «Instaló» row, or falló → «Ignorar» (an
 *     «Omitiste» pill; nothing saved) / «Reintentar» (instalando again);
 *   - propuesta → «Ahora no» → a «Rechazaste» pill + «guardado en Decisiones guardadas
 *     para todos los proyectos»;
 *   - falta un programa → «Entendido» → a «Falta … aviso visto» row;
 *   - permiso → «No» (pill + saved line) / «Permitir en esta sesión» (a «Permitiste … en
 *     esta sesión» row) / «Instalar y permitir» (instalando, then both rows); an unanswered
 *     one (timeout, interrupt) → an «Omitiste» pill.
 */

import {
  failedModel,
  installedModel,
  installingModel,
  missingProgramModel,
  permissionModel,
  proposalModel,
  resolutionModel,
  rowModel,
  stepStoppedModel,
} from './components.js';
import {
  buildFailedCard,
  buildInstallingCard,
  buildMissingProgramCard,
  buildPermissionCard,
  buildProposalCard,
  buildResolution,
  buildRow,
  buildStepStoppedCard,
} from './render-components.js';
import { attachModelMenu } from './model-menu.js';
import { openToolsWindow } from './tools-window.js';
import { attachConnectionsMenu } from './connections-menu.js';
import { sfx } from './sfx.js';
import { COMPONENTS, PILLS } from './strings.js';

const INSTALL_EVENTS = {
  component_install_started: 'started',
  component_install_progress: 'progress',
  component_installed: 'installed',
  component_install_failed: 'failed',
};

/**
 * `picker` (optional): the model picker's hooks into the thread: `isBusy()`, true while a
 * turn runs, `onFailed(answer)`, called for a switch the core could not make, and
 * `onSwitched(answer)`, called for one it made. The
 * picker lists and switches through `modelApi.models()` and `modelApi.switchModel(id)`.
 */
function installComponentUi({
  placeCard,
  modelChip,
  api,
  modelApi = null,
  connectionsApi = null,
  skillsApi = null,
  // Diseño: `get()` and `set(theme)` for the window's theme (src/main/appearance.js).
  appearanceApi = null,
  connectionsChip = null,
  projectName = () => '',
  picker = null,
  onConnect = null,
  play = sfx,
  documentRef = document,
}) {
  // Every way into «Herramientas y permisos» carries the Conexiones tab's api, and tells the
  // bar's chip when a connection changed there.
  let connectionsMenu = null;
  const tools = (extra = {}) =>
    openToolsWindow(
      {
        api,
        connectionsApi,
        skillsApi,
        appearanceApi,
        play,
        onConnectionsChanged: () => connectionsMenu && connectionsMenu.refresh(),
        ...extra,
      },
      documentRef,
    );
  /** component id -> Set of handler objects waiting on that component's install events. */
  const watchers = new Map();
  /** Components with a proposal card waiting for an answer, so each gets only one card. */
  const openProposals = new Set();
  /** Permission request ids already drawn, and the settle function of those still open. */
  const seenPermissions = new Set();
  const openPermissions = new Map();

  function watch(id, handlers) {
    if (!watchers.has(id)) watchers.set(id, new Set());
    watchers.get(id).add(handlers);
    return () => watchers.get(id)?.delete(handlers);
  }

  function notify(event) {
    const kind = INSTALL_EVENTS[event.type];
    for (const handlers of [...(watchers.get(event.id) || [])]) handlers[kind]?.(event);
  }

  async function call(promise) {
    try {
      return (await promise) || {};
    } catch (error) {
      return { error: error.message || String(error) };
    }
  }

  /** A slot in the thread that can be swapped for the next state of the same card. */
  function slot(initial) {
    let current = initial;
    return {
      get el() {
        return current;
      },
      replace(next) {
        current.replaceWith(next);
        current = next;
      },
    };
  }

  /**
   * Instalando → Instaló | Falló, inside `place`. `installedExtra` adds rows after
   * «Instaló» (the permission's «Permitiste … en esta sesión»).
   */
  function followInstall(place, { id, name, sizeBytes, installedExtra = [] }) {
    let unwatch = () => {};

    const showFailed = (error) => {
      unwatch();
      const failed = buildFailedCard(
        failedModel(name, error),
        {
          onIgnore: async () => {
            play('click');
            await call(api.ignore(id));
            place.replace(buildResolution(resolutionModel('skipped', name), documentRef));
          },
          onRetry: () => {
            play('select');
            start(() => api.retry(id));
          },
        },
        documentRef,
      );
      place.replace(failed.el);
    };

    const start = (request) => {
      const installing = buildInstallingCard(installingModel(name, 0, sizeBytes), documentRef);
      place.replace(installing.el);
      unwatch();
      unwatch = watch(id, {
        progress: (event) => installing.update(installingModel(name, event.downloaded, event.total)),
        installed: (event) => {
          unwatch();
          play('done');
          const rows = documentRef.createElement('div');
          rows.className = 'cc-rows';
          rows.append(
            buildRow(installedModel(event, name), documentRef),
            ...installedExtra.map((model) => buildRow(model, documentRef)),
          );
          place.replace(rows);
        },
        failed: (event) => {
          play('close');
          showFailed(event.error);
        },
      });
      if (request) {
        call(request()).then((answer) => {
          // "busy": an install of the same component is already running, and its events
          // finish this card too. Any other refusal is a route code ("unpinned",
          // "no_build"), not a message to show: the card says only «No se pudo instalar».
          if (answer.error && answer.error !== 'busy') showFailed('');
        });
      }
    };

    return { start, showFailed };
  }

  // -- 4a: herramienta necesaria --------------------------------------------------------

  function showProposal(event) {
    if (openProposals.has(event.id)) return;
    openProposals.add(event.id);
    let place;
    const card = buildProposalCard(
      proposalModel(event),
      {
        onInstall: () => {
          play('select');
          openProposals.delete(event.id);
          followInstall(place, { id: event.id, name: event.name, sizeBytes: event.size_bytes }).start(() =>
            api.install(event.id),
          );
        },
        onDecline: async () => {
          play('cancel');
          openProposals.delete(event.id);
          const answer = await call(api.decline(event.id));
          if (!answer.error) place.replace(buildResolution(resolutionModel('declined', event.name), documentRef));
        },
      },
      documentRef,
    );
    place = slot(card.el);
    placeCard(card.el);
    play('open');
  }

  // -- 4b: falta un programa -------------------------------------------------------------

  function showMissingProgram(event) {
    let place;
    const card = buildMissingProgramCard(
      missingProgramModel(event),
      {
        onCopy: () => {
          play('click');
          call(api.copyCommand(event.id));
        },
        onOpenOfficial: () => {
          play('click');
          call(api.openOfficial(event.id));
        },
        onDone: () => {
          play('close');
          place.replace(buildRow(rowModel(COMPONENTS.missingRow, event.name, COMPONENTS.missingSeen), documentRef));
        },
      },
      documentRef,
    );
    place = slot(card.el);
    placeCard(card.el);
    play('open');
  }

  // -- 4b: permiso -------------------------------------------------------------------------

  function allowedRow(name) {
    return rowModel(PILLS.allowed.label, name, COMPONENTS.savedForAllProjects);
  }

  function showPermission(event) {
    if (seenPermissions.has(event.id)) return;
    seenPermissions.add(event.id);
    const model = permissionModel(event);
    let place;
    let settled = false;

    // Draws what an answer means, whether it came from this card or from the core.
    const settle = (decision) => {
      if (settled) return;
      settled = true;
      openPermissions.delete(event.id);
      if (decision === 'deny') {
        place.replace(buildResolution(resolutionModel('declined', event.name), documentRef));
      } else if (decision === 'allow_session' && event.installed) {
        place.replace(buildRow(allowedRow(event.name), documentRef));
      } else if (decision === 'allow_session' || decision === 'install_and_allow') {
        followInstall(place, {
          id: event.component,
          name: event.name,
          sizeBytes: event.size_bytes,
          installedExtra: [allowedRow(event.name)],
        }).start(null);
      } else {
        place.replace(buildResolution(resolutionModel('skipped', event.name), documentRef));
      }
    };

    const answer = async (decision) => {
      settle(decision);
      const delivered = await call(api.answerPermission(event.id, decision));
      if (delivered.error) {
        // Too late: the core stopped waiting. Nothing was allowed or saved.
        place.replace(buildResolution(resolutionModel('skipped', event.name), documentRef));
      }
    };

    const card = buildPermissionCard(
      model,
      {
        onDeny: () => {
          play('cancel');
          answer('deny');
        },
        onAllow: () => {
          play('select');
          answer(model.answer);
        },
      },
      documentRef,
    );
    place = slot(card.el);
    openPermissions.set(event.id, settle);
    placeCard(card.el);
    play('open');
  }

  function resolvePermission(event) {
    const settle = openPermissions.get(event.id);
    if (settle) settle(event.decision);
  }

  // -- 4b: paso detenido -----------------------------------------------------------------

  function showStepStopped(event) {
    placeCard(buildStepStoppedCard(stepStoppedModel(event), documentRef).el);
    play('open');
  }

  // -- the model menu and 4e ---------------------------------------------------------------

  const modelMenu = modelChip
    ? attachModelMenu(
        modelChip,
        {
          onTools: () => tools({ returnFocusTo: modelChip }),
          onConnect,
          play,
          picker:
            picker && modelApi && typeof modelApi.switchModel === 'function'
              ? {
                  load: () => modelApi.models(),
                  pick: (id) => modelApi.switchModel(id),
                  isBusy: picker.isBusy,
                  onFailed: picker.onFailed,
                  onSwitched: picker.onSwitched,
                }
              : null,
        },
        documentRef,
      )
    : null;

  /** True when the event was this module's to handle. */
  function handleEvent(event) {
    switch (event?.type) {
      case 'component_proposal':
        showProposal(event);
        return true;
      case 'program_missing':
        showMissingProgram(event);
        return true;
      case 'permission_request':
        showPermission(event);
        return true;
      case 'permission_resolved':
        resolvePermission(event);
        return true;
      case 'step_stopped':
        showStepStopped(event);
        return true;
      case 'component_install_started':
      case 'component_install_progress':
      case 'component_installed':
      case 'component_install_failed':
        notify(event);
        return true;
      default:
        return false;
    }
  }

  /** Opens the model menu under the model chip (the service-error card's «Cambiar modelo»). */
  function openModelMenu() {
    if (modelMenu && !modelMenu.isOpen()) modelMenu.open();
  }

  // -- 1a: «⧉ N conexiones» --------------------------------------------------------------

  connectionsMenu = attachConnectionsMenu(
    connectionsChip,
    {
      api: connectionsApi,
      project: projectName,
      play,
      onAdd: () => tools({ initialTab: 'connections', adding: true, returnFocusTo: connectionsChip }),
      onManage: () => tools({ initialTab: 'connections', returnFocusTo: connectionsChip }),
    },
    documentRef,
  );

  return { handleEvent, openModelMenu, refreshConnections: () => connectionsMenu.refresh() };
}

export { installComponentUi };
