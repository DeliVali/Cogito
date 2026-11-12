'use strict';

/**
 * Pure models for the component, program and permission screens (design 4a, 4b, 4e). Each
 * card or row is built from the core's events; `core/cogito/server/components.py`
 * documents every shape. No DOM here: render-components.js and tools-window.js draw these,
 * so the unit suite can check every text.
 *
 * Every sentence comes from strings.js. The values between them come from the event.
 */

import { COMPONENTS, TOOLS_WINDOW, PILLS } from './strings.js';
import { formatElapsed } from './tool-rows.js';

const MB = 1_000_000;

/** "160 MB". Decimal megabytes, the unit the design writes sizes in. */
function formatSize(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '';
  return `${Math.round(bytes / MB)} MB`;
}

/** "61 / 160 MB", the installing card's progress line (4a: «{descargado} / {tamaño}»). */
function progressText(downloaded, total) {
  return COMPONENTS.progress(Math.round(Math.max(0, downloaded) / MB), formatSize(total));
}

/** 0-100, for the bar. */
function progressPercent(downloaded, total) {
  if (!total || total <= 0) return 0;
  return Math.max(0, Math.min(100, (downloaded / total) * 100));
}

/** "18/09/2026" from an ISO date ("2026-09-18"). */
function formatDate(iso) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return match ? `${match[3]}/${match[2]}/${match[1]}` : '';
}

/** "rust-lang.org/tools/install": an official link as the 4b card shows it, host and
 * path with no scheme, no "www." and no trailing slash. Anything that is not https is
 * refused (the main process refuses to open it too). */
function hostPath(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return '';
  }
  if (parsed.protocol !== 'https:') return '';
  const host = parsed.hostname.replace(/^www\./, '');
  const path = parsed.pathname.replace(/\/+$/, '');
  return `${host}${path}`;
}

/** First letter lowered, for the permission card's "Qué habilita: abrir y leer…" (4b). */
function lowerFirst(text) {
  return text ? text.charAt(0).toLocaleLowerCase('es') + text.slice(1) : '';
}

/** 4a, propuesta: `component_proposal`. */
function proposalModel(event) {
  return {
    id: event.id,
    title: COMPONENTS.proposalTitle,
    name: event.name,
    size: formatSize(event.size_bytes),
    sizeBytes: event.size_bytes,
    enablesLabel: COMPONENTS.enables,
    enables: event.description,
    shortcuts: COMPONENTS.proposalShortcuts,
    decline: COMPONENTS.notNow,
    accept: COMPONENTS.install,
  };
}

/** 4a, instalando. */
function installingModel(name, downloaded, total) {
  return {
    title: COMPONENTS.installing(name),
    progress: progressText(downloaded, total),
    percent: progressPercent(downloaded, total),
  };
}

/** 4a, instaló: «Instaló» {nombre} · {tamaño} · {duración}. */
function installedModel(event, name) {
  return {
    verb: COMPONENTS.installed,
    object: name || event.name,
    meta: `${formatSize(event.size_bytes)} · ${formatElapsed(event.duration_ms || 0)}`,
  };
}

/** 4a, falló: «No se pudo instalar {nombre}» · {error real}. */
function failedModel(name, error) {
  return {
    title: COMPONENTS.failedTitle(name),
    error: error || '',
    ignore: COMPONENTS.ignore,
    retry: COMPONENTS.retry,
  };
}

/** The pill a resolved card turns into (design 1o's pill): "Rechazaste · {name}" with the
 * saved-decision line, or "Omitiste · {name}" with none, since nothing was saved. */
function resolutionModel(kind, name) {
  const pill = kind === 'declined' ? PILLS.denied : PILLS.skipped;
  return {
    pill: { kind: kind === 'declined' ? 'denied' : 'skipped', tone: pill.tone, icon: pill.icon, label: pill.label, object: name },
    saved: kind === 'declined' ? COMPONENTS.savedForAllProjects : null,
  };
}

/** 4b, no disponible: `program_missing`, or `GET /system/programs/{id}/help`. The command
 * and the official link never appear together: the link shows only when no package
 * manager can install the program. */
function missingProgramModel(help) {
  const command = typeof help.command === 'string' && help.command ? help.command : null;
  const link = command ? '' : hostPath(help.official_url);
  return {
    id: help.id,
    title: COMPONENTS.missingTitle,
    heading: COMPONENTS.missingHeading(help.name),
    command,
    copy: COMPONENTS.copy,
    noManager: command ? null : COMPONENTS.noManager,
    link: link || null,
    openOfficial: link ? COMPONENTS.openOfficial : null,
    done: COMPONENTS.understood,
  };
}

/** 4b, permiso: `permission_request`, in its two variants. */
function permissionModel(event) {
  const installFirst = event.installed
    ? null
    : {
        label: COMPONENTS.installFirst,
        size: formatSize(event.size_bytes),
        name: event.name,
        enables: COMPONENTS.enablesInline(lowerFirst(event.description)),
      };
  return {
    id: event.id,
    component: event.component,
    title: COMPONENTS.permissionTitle,
    rows: [
      { label: COMPONENTS.tool, value: event.name },
      { label: COMPONENTS.action, value: event.action || '', target: event.target || '' },
      { label: COMPONENTS.scope, value: event.scope || '' },
    ],
    installFirst,
    deny: COMPONENTS.no,
    allow: event.installed ? COMPONENTS.allow : COMPONENTS.installAndAllow,
    answer: event.installed ? 'allow_session' : 'install_and_allow',
    name: event.name,
  };
}

/** 4b, paso detenido: `step_stopped`. */
function stepStoppedModel(event) {
  return {
    title: COMPONENTS.stepStoppedTitle,
    rows: [
      { label: COMPONENTS.reason, value: event.reason || '' },
      { label: COMPONENTS.where, value: '', target: event.where || '' },
    ],
    footer: COMPONENTS.stepContinues,
  };
}

/** A finished row in the tool-row look the design uses for «Instaló», «Permitiste» and
 * «Falta»: `{verb, object, meta}`. */
function rowModel(verb, object, meta) {
  return { verb, object, meta };
}

/** 4e, Herramientas: which components the tab shows. A component the core cannot install
 * here (`installable: false`, from `setup.service.ComponentService`: no real pinned
 * digest for this platform) is hidden unless it is already installed. The catalog already
 * holds only `consent in {"auto", "ask"}` components (`list_components`), so this is the
 * only filter the window needs. */
function visibleComponents(components) {
  return (components || []).filter((component) => component.installable !== false || component.installed);
}

/** 4e, Herramientas: one row per component Cogito can install. */
function componentRowModel(component) {
  return {
    id: component.id,
    name: component.name,
    description: (component.description || '').replace(/\.$/, ''),
    source: component.installed
      ? TOOLS_WINDOW.installedBy(formatDate(component.installed_at))
      : component.external
        ? TOOLS_WINDOW.external
        : TOOLS_WINDOW.notInstalled,
    size: formatSize(component.size_bytes),
    enabled: component.enabled !== false,
  };
}

/** 4e, Del sistema: `{name, version, path, missing}` for one program. */
function programRowModel(program) {
  const name = program.variant ? `${program.name} · ${program.variant}` : program.name;
  return {
    id: program.id,
    name,
    version: program.found && program.version ? program.version : TOOLS_WINDOW.noVersion,
    path: program.found ? program.path || '' : TOOLS_WINDOW.notInstalled,
    missing: !program.found,
  };
}

/** 4e, Decisiones guardadas: "No instalar {nombre}" · "Rechazado el {fecha}". */
/**
 * 4e: the tabs and the badge beside each name (UX-09). A badge is a live count of the
 * tab's own list, or empty where the tab has no list. It is never a constant or a price.
 * A tab whose content is hidden (UX-11/12's component row, UX-13's search cascade) is
 * left out of the array, so `tools-window.js` never selects it or draws its button.
 *
 *   components: hidden until `visibleComponents` has a real row.
 *   system: every row the machine scan produced, found or not.
 *   search: hidden until `search.available` (a real web-search tool exists). Its levels
 *     are fixed, so it has no badge.
 *   skills: a live count, "0" included.
 *   appearance: «Diseño», last, where the window can read and set the theme (`appearance`
 *     is that state, `{theme}`). Its rows are settings, not a list of things, so no badge.
 */
function tabModels({ components = [], programs = [], skills = [], search = null, connections = null, appearance = null } = {}) {
  const tabs = [];
  const shown = visibleComponents(components);
  if (shown.length > 0) {
    tabs.push({ key: 'components', label: TOOLS_WINDOW.tabs.components, count: String(shown.length) });
  }
  // Design 4e: «Conexiones» right after Herramientas. Shown once the core has answered
  // (an older core without the route leaves it out).
  if (connections) {
    tabs.push({ key: 'connections', label: TOOLS_WINDOW.tabs.connections, count: String(connections.length) });
  }
  tabs.push({ key: 'system', label: TOOLS_WINDOW.tabs.system, count: String(programs.length) });
  if (search && search.available) {
    tabs.push({ key: 'search', label: TOOLS_WINDOW.tabs.search, count: '' });
  }
  tabs.push({ key: 'skills', label: TOOLS_WINDOW.tabs.skills, count: String(skills.length) });
  if (appearance) {
    tabs.push({ key: 'appearance', label: TOOLS_WINDOW.tabs.appearance, count: '' });
  }
  return tabs;
}

/** The line under the web row about the app's own buscador, from `local_search` in
 * `GET /settings/search`: its state, or '' where there is none to speak of (an older core,
 * a build without one, a platform it does not run on, or switched off). */
function localSearchStatus(local) {
  const copy = TOOLS_WINDOW.localSearch;
  switch (local?.state) {
    case 'preparing':
    case 'starting':
    case 'up':
      return copy[local.state];
    case 'failed':
      return local.kind === 'offline' ? copy.failedOffline : copy.failed;
    default:
      return '';
  }
}

/** 4e, Búsqueda: the levels, which are always on (`core/cogito/config/search.py`), so a
 * row has no switch. The browser level is left out: it is asked for in the chat, and a
 * row that does nothing is not real data. `local` is `local_search` from
 * `GET /settings/search`; the web row names the buscador's engines only while it is up,
 * because only then does a query go there. */
function levelModels(local = null) {
  return TOOLS_WINDOW.levels.map((level) => {
    const web = level.key === 'web';
    return {
      key: level.key,
      n: level.n,
      name: level.name,
      description: level.description,
      note: web ? (local?.state === 'up' ? TOOLS_WINDOW.webSourcesLocal : TOOLS_WINDOW.webSources) : '',
      status: web ? localSearchStatus(local) : '',
    };
  });
}

export {
  formatSize,
  progressText,
  progressPercent,
  formatDate,
  hostPath,
  lowerFirst,
  proposalModel,
  installingModel,
  installedModel,
  failedModel,
  resolutionModel,
  missingProgramModel,
  permissionModel,
  stepStoppedModel,
  rowModel,
  visibleComponents,
  componentRowModel,
  programRowModel,
  tabModels,
  levelModels,
  localSearchStatus,
};
