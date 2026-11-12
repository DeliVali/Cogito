'use strict';

/**
 * Every fixed Spanish string the renderer shows, in one table. UI copy belongs to the app,
 * never the model (CONTRIBUTING.md), and no other module holds a Spanish literal inline.
 *
 * Sources: the design canvas (`COGITO v2.dc.html`, section 1o "Etiquetas del hilo", and
 * 1a/1l for the tool-row and changes-card instances), `app-phase-b-contract.md`'s "Thread
 * labels" section, and the core's SSE docstring (`core/cogito/server/app.py`) for the event
 * shapes this copy answers. Where none of those covers a case, the string is marked GUESS.
 */

/** Running (gerund), finished (past, third person) and not-done (`failed`) verb forms per
 * tool name. `read`, `edit`, `shell` (-> "Ejecutó") and `grep` (-> "Buscó") come from the
 * design's four examples. `write`, `glob`, `recall`, `ask`, `check` and every `failed` form
 * are GUESSES by analogy; the design shows none of them. A `failed` form says the call did
 * not do its job («No escribió hello.py»), since a refused or rejected write wrote nothing.
 * A shell call that ran and exited non-zero keeps «Ejecutó»: it did run. */
const VERBS = {
  read: { gerund: 'Leyendo', past: 'Leyó', failed: 'No pudo leer' },
  edit: { gerund: 'Editando', past: 'Editó', failed: 'No editó' },
  write: { gerund: 'Escribiendo', past: 'Escribió', failed: 'No escribió' },
  shell: { gerund: 'Ejecutando', past: 'Ejecutó', failed: 'No se ejecutó' },
  grep: { gerund: 'Buscando', past: 'Buscó', failed: 'No pudo buscar' },
  glob: { gerund: 'Buscando archivos', past: 'Buscó archivos', failed: 'No pudo buscar archivos' },
  recall: { gerund: 'Buscando en la conversación', past: 'Buscó en la conversación', failed: 'No pudo buscar en la conversación' },
  ask: { gerund: 'Consultando los documentos', past: 'Consultó los documentos', failed: 'No pudo consultar los documentos' },
  ask_document: { gerund: 'Consultando el documento', past: 'Consultó el documento', failed: 'No pudo consultar el documento' },
  check: { gerund: 'Revisando', past: 'Revisó', failed: 'No pudo revisar' },
  // The web tools. The colon is part of the verb: «Buscó: zipfile append mode», «Leyó:
  // docs.python.org» (the person's wording, 2026-10-12).
  // `off`: the person turned the web off in Búsqueda, so nothing was searched or read.
  web_search: { gerund: 'Buscando:', past: 'Buscó:', failed: 'No se pudo buscar:', off: 'No buscó:' },
  web_fetch: { gerund: 'Leyendo:', past: 'Leyó:', failed: 'No se pudo leer:', off: 'No leyó:' },
  // `mcp_find` searches the MCP servers' tools (its query is the row's target); `skill`
  // loads a skill's instructions.
  mcp_find: { gerund: 'Buscando herramientas:', past: 'Buscó herramientas:', failed: 'No pudo buscar herramientas:' },
  skill: { gerund: 'Cargando una skill', past: 'Cargó una skill', failed: 'No pudo cargar la skill' },
  // A line typed into a terminal tab after the person's «Ejecutar», and a read of its screen.
  run_in_terminal: { gerund: 'Ejecutando en la terminal:', past: 'Ejecutó en la terminal:', failed: 'No se ejecutó en la terminal:' },
  read_terminal: { gerund: 'Leyendo la terminal', past: 'Leyó la terminal', failed: 'No pudo leer la terminal' },
  // A subagent the model handed a question to (`delegate`). While it works, its row names the
  // call it is making; its task is the row's tooltip. `done`: it has answered while others
  // the same step started still work, and the rows end together.
  delegate: { gerund: 'Subagente:', past: 'Subagente investigó:', failed: 'El subagente no terminó:', done: 'Subagente terminó:' },
  // The model's own task list (`todo`): the row opens to the list as it wrote it.
  todo: { gerund: 'Actualizando la lista de tareas', past: 'Actualizó la lista de tareas', failed: 'No pudo actualizar la lista de tareas' },
  // Not a model tool: the core's build gate, which compiles what the turn changed before
  // it accepts the answer (`loop/engine.py`, `_BUILD_GATE_UI_NAME`).
  build: { gerund: 'Compilando', past: 'Compiló', failed: 'Compiló' },
};

/** Any other tool: its own name, never «Ejecutó», which says something ran. */
const DEFAULT_VERB = { gerund: 'Usando', past: 'Usó', failed: 'No pudo usar' };

/** The right-hand note of a row: a search's counts, the build gate's errors, a call that
 * was not allowed to run. GUESS: no design source; the register of «64 líneas». */
const ROW_NOTES = {
  matches: (matches, files) =>
    `${matches} coincidencia${matches === 1 ? '' : 's'} en ${files} archivo${files === 1 ? '' : 's'}`,
  noMatches: 'sin coincidencias',
  results: (n) => `${n} resultado${n === 1 ? '' : 's'}`,
  noResults: 'sin resultados',
  files: (n) => (n === 0 ? 'ningún archivo' : `${n} archivo${n === 1 ? '' : 's'}`),
  compileErrors: (n) => `${n} error${n === 1 ? '' : 'es'} de compilación`,
  denied: 'no permitido',
  /** A web call while the web level in Búsqueda is off. */
  webOff: 'web apagada',
  /** How many tool calls a subagent made. */
  calls: (n) => `${n} llamada${n === 1 ? '' : 's'}`,
  /** A subagent's calls that its opened row no longer lists. */
  earlierCalls: (n) => `… ${n} anterior${n === 1 ? '' : 'es'}`,
  /** A command left running in the background because it listens on a port (a server). */
  background: (ports) => (ports ? `sigue en segundo plano · puerto ${ports}` : 'sigue en segundo plano'),
};

/** "Leyó 2 archivos": the grouped label for consecutive finished reads (design 1a). */
function readGroupLabel(count) {
  return `Leyó ${count} archivo${count === 1 ? '' : 's'}`;
}

/** "3 archivos": a bare path count, used when `target.paths` has more than one entry
 * (tool-rows.js's `targetLabel`). */
function pathCountLabel(count) {
  return `${count} archivo${count === 1 ? '' : 's'}`;
}

/** The core's sentinel for a path outside the project (`server/app.py`). `tool_start` and
 * `tool_end` send the literal string `<outside project>` instead of an absolute path;
 * `turn_changes` prefixes the display path with it (`<outside project>/secrets.env`). Both
 * forms map to the same Spanish label. */
const OUTSIDE_PROJECT_SENTINEL = '<outside project>';
const OUTSIDE_PROJECT_LABEL = 'fuera del proyecto';

/** "Interrumpido · src/checkout.ts · El cambio a medias no se guardó": a cancelled call on
 * one line, with the label, the target (when there is one) and, for an edit, the
 * no-partial-write note. `target` and `note` are optional. Never includes the core's
 * `tool_end.error` text (e.g. `"cancelled"`): that field explains the outcome to the
 * model and is not meant for a person. */
function cancelledLabel(target, note) {
  const parts = ['Interrumpido'];
  if (target) parts.push(target);
  if (note) parts.push(note);
  return parts.join(' · ');
}

const CANCELLED_EDIT_NOTE = 'No se guardó el cambio';
/** Design: cancelled rows are drawn grey and dashed, with a ■ (stop square) in place of
 * the running spinner or the finished arrow. */
const CANCELLED_ICON = '■';

/** Shown instead of the gerund while a call's approval is pending. The call has started
 * (`tool_start` always comes before the `approval_request` that gates it) but is not
 * running yet. GUESS: no design source; the wording follows the amber card's "necesita tu
 * permiso". */
const WAITING_APPROVAL = 'Esperando tu permiso';

/** The approval card's copy: buttons (design 1a: "Rechazar"/"Permitir"), the shortcuts
 * footer (design 1o), and the question built from the facts the core sends with
 * `approval_request` (`cli._window_approver`: `action`, `items`, `recovery`, `within`,
 * `listed`, `outside`; see `decisions.js`'s `askCard`). The core's own `summary` is English
 * and only shown when a core sends none of these. */
const APPROVAL_BUTTONS = { allow: 'Permitir', deny: 'Rechazar' };
const SHORTCUT_APPROVE = '↵ Permitir · Esc Rechazar';
// With a button focused Enter does what that button says, so only Esc is the card's own.
const SHORTCUT_REJECT = 'Esc Rechazar';
const ASK_COPY = {
  // The card's strip (design 1a), without the design's jargon («Guardrail»).
  title: 'Seguridad · necesita tu permiso',
  recoverable: 'va a la papelera',
  notRecoverable: 'no se puede deshacer',
  // A shimmed truncate saves what the file held before emptying it.
  copySaved: 'se guarda una copia en la papelera',
  // A move without overwriting destroys nothing.
  nothingLost: 'no se borra nada',
  unresolvedPrefix: 'no se pudo resolver',
  plural: (count, one, many) => `${count} ${count === 1 ? one : many}`,
  files: ['archivo', 'archivos'],
  folders: ['carpeta', 'carpetas'],
  items: ['elemento', 'elementos'],
  // A folder's contents: «1 archivo», «1000 o más archivos» when the count stopped.
  held: (entries, capped) => (capped ? `${entries} o más archivos` : `${entries} ${entries === 1 ? 'archivo' : 'archivos'}`),
  remove: (what) => `borrar ${what}`,
  removeFolder: (name, held) => `borrar la carpeta ${name} (${held})`,
  overwrite: (what) => `sobrescribir ${what}`,
  move: (what) => `mover ${what}`,
  and: ' y ',
  outside: ' fuera del proyecto',
  // `git clean`, `find -delete`: what it destroys is not named in the command.
  command: 'ejecutar un comando que borra o descarta archivos que no nombra',
  // Annotations on a path line.
  folder: (held) => `carpeta, ${held}`,
  link: 'enlace',
  overwritten: 'se sobrescribe',
  within: (path) => `lo que coincida dentro de ${path}`,
  listed: 'Borraría:',
  listedMore: (count) => `… y ${count} más`,
  // Shown when this window's `approve()` call could not be delivered (the request was
  // resolved elsewhere, or the turn ended). A local fallback for the one case where no
  // `approval_resolved` echo will come to draw the pill.
  noLongerWaiting: 'ya no espera respuesta',
  // A tool from an MCP server that may change something (`action: "external_tool"`).
  external: (label) => `usar ${label}`,
  externalNote: 'puede cambiar algo fuera de la conversación',
};

/** Decision pill copy (design 1o), each with its icon. The design pairs "Omitiste" with ✕,
 * but that reads as an active rejection, which a timeout, interrupt or skip is not, so only
 * "Rechazaste" gets ✕ here (changed after review). `chosen`/`reverted` are the multi-choice
 * and rewind pills from the same reference, kept here so the mapping stays in one place. */
const PILLS = {
  allowed: { label: 'Permitiste', tone: 'green', icon: '✓' },
  denied: { label: 'Rechazaste', tone: 'grey', icon: '✕' },
  chosen: { label: 'Elegiste', tone: 'blue', icon: '✓' },
  // Also used for an approval resolved by timeout or interruption (reason "timeout" /
  // "interrupted", `allowed: null`); see decisions.js's `pillFor`. Grey with no icon: it is
  // neither a rejection nor a choice.
  skipped: { label: 'Omitiste', tone: 'grey', icon: '' },
  // The turn was stopped («Detener») while the card waited: nobody chose, the person stopped.
  stopped: { label: 'Detenido', tone: 'grey', icon: '' },
  reverted: { label: 'Volviste', tone: 'blue', icon: '↺' },
  // A line the person let the model run in a terminal tab (`action: "run_in_terminal"`).
  ran: { label: 'Ejecutaste', tone: 'green', icon: '✓' },
  // Allowed by the composer's automatic mode, without a card (`reason: "auto"`).
  auto: { label: 'Automático', tone: 'green', icon: '✓' },
};

/** The "Cambios de este turno" card (design 1l), and the core's
 * `too_large`/`binary`/`truncated` file states (`server/app.py`). */
const CHANGES_CARD = {
  title: 'Cambios de este turno',
  fileCount: (n) => `${n} archivo${n === 1 ? '' : 's'}`,
  expandAll: 'Expandir todo',
  collapseAll: 'Contraer todo',
  expandMore: (n) => `${n} línea${n === 1 ? '' : 's'} más`,
  // The core truncates once a turn's accumulated diff bytes cross ~256 KB (its
  // docstring), so the text says why it was cut, not only that it was.
  truncatedByServer: 'Se recortó por tamaño',
  binaryNote: 'Archivo binario',
  emptyNote: 'Archivo vacío',
  unchangedNote: 'Sin cambios en el texto',
  tooLargeNote: (beforeKB, afterKB) => `Demasiado grande para mostrar (${beforeKB} KB → ${afterKB} KB)`,
  // GUESS: the mockup has no shell-made change. For a shell-only turn the core sends
  // {"total_files":0,"files":[],"shell_changes_unknown":true}, and the card says so
  // rather than staying silent.
  shellChangesUnknown: 'Los cambios hechos por comandos no se muestran',
};

/** A finished tool row opened onto what its call produced (design 1a, 1l). «Clic para
 * expandir · N líneas más» and «Mostrar menos» are 1l's (`dfMore.label`). The rest is new,
 * fixed and short: the remainder is loading, failed to load, or is only partly here. */
const TOOL_DETAIL = {
  expandMore: CHANGES_CARD.expandMore,
  showLess: 'Mostrar menos',
  loading: 'Cargando…',
  loadFailed: 'No se pudo cargar el resto',
  partial: (shown, total) => `${shown} de ${total} líneas`,
  // A read's file length (design 1a «· 64 líneas», 2f «212 líneas»).
  lines: (n) => `${n} línea${n === 1 ? '' : 's'}`,
};

/**
 * Error copy (contract "Thread labels" section, v3 wording) for the error states this
 * renderer shows: the core could not be reached (session open failed, or a turn's request
 * failed outright). There is no retry loop yet, so no countdown, only the contract's
 * headline. An unclassified mid-turn error has no design text, so its card shows only the
 * core's message, with no headline of the app's own.
 *
 * There is no `noResponse`, `usageLimit`, `folderMissing` or `action` entry: the `error`
 * SSE event carries only a free-text `message`, so nothing could select them. A provider
 * failure already has its own card (`SERVICE_ERROR`). Add them when the core sends a
 * structured error code.
 */
const ERROR_COPY = {
  offline: 'Sin conexión',
  // The core died under the window and the app is restarting it (main/supervisor.js).
  // No design source: fixed system texts in the handoff's forms (gerund while under way,
  // past once done), never the agent's first person.
  coreRestarting: 'El núcleo se cerró · reiniciando',
  coreRestarted: 'SE REINICIÓ EL NÚCLEO',
  coreGaveUp: 'El núcleo se cerró y no se pudo reiniciar',
  // `code` / `signal`, as the operating system reported the exit.
  coreExit: (code, signal) => (signal ? `señal ${signal}` : code != null ? `código de salida ${code}` : ''),
};

/** The composer (design 1l/1a: idle placeholder, "+ adjuntar", "Enviar ↵"; design 1m,
 * "Hablarle mientras trabaja", for the busy placeholder and "■ Detener"). `hint` is a GUESS
 * (the mockup has no line under the composer) in the app's shortcut notation, and lists
 * only the two shortcuts (UX-01). It must not explain the UI:
 * `specs/design/2026-09-28-cogito-v2-mvp/README.md` rules that out ("Nada de ayudas ... :
 * el comportamiento lo explica la propia interfaz"), and the approval card teaches
 * permissions the one time they come up.
 *
 * Enviar's label never changes. While a turn runs it sends a steer instead of a new turn
 * (design 1m: "Enviar nunca detiene"); "■ Detener" is the only control that stops anything.
 */
const COMPOSER = {
  // One short invitation, not a manual (the person, 2026-10-12): copy describes a state or
  // invites, it never tells the person how to use the app.
  placeholder: 'Escribe un mensaje…',
  // While a turn runs: the same invitation, shorter. Sending still steers the turn.
  placeholderBusy: 'Añade algo…',
  send: 'Enviar ↵',
  // design 1m, lines 350/483/666, verbatim.
  stop: '■ Detener',
  attachLabel: 'Adjuntar archivos',
  hint: '↵ enviar · ⇧↵ línea nueva',
};

/** The steer message's status line (design 1m, "Tu mensaje": "«En cola» mientras espera,
 * «Hecho» al tomarse en cuenta"). `failed` is a GUESS with no design source, for when this
 * window's request to queue the message never reached the core; same neutral register as
 * `ERROR_COPY.offline`. */
const STEER = {
  queued: 'En cola',
  applied: '✓ Hecho',
  failed: 'No se pudo enviar',
};

/** How long "✓ Hecho" stays before the steer collapses to a plain message (design 1m:
 * "unos 2 s después cambia solo a..."). */
const STEER_SETTLE_MS = 2000;

/** The custom title bar's buttons and the message field's accessible name. Not visible
 * text, but still UI copy (screen-reader labels), so Spanish. */
const WINDOW_CHROME = {
  close: 'Cerrar',
  // NEW, no design source (QA 2026-10-11): the close box's tooltip. Close hides the
  // window (or minimises it where no tray shows); the agent keeps running.
  closeTitle: 'Cerrar · COGITO sigue en marcha',
  // GUESS: the thread's ↓ button, Claude Code's «jump to bottom».
  jumpLatest: 'Ir al final',
  minimize: 'Minimizar',
  maximize: 'Maximizar',
  // The maximise control once the window is maximised: it restores.
  restore: 'Restaurar',
  message: 'Mensaje',
  // The title bar (design 1a: «COGITO — tienda-api»; 1f: «COGITO — proyectos»).
  title: (project) => (project ? `COGITO — ${project}` : 'COGITO'),
};

/** The state under the composer's orb (design 1a's `labels`, verbatim). */
const ORB_STATE = {
  idle: 'En reposo',
  thinking: 'Pensando',
  // GUESS: the orb while a fold summarises the conversation (phase `compacting`).
  compacting: 'Resumiendo',
  responding: 'Respondiendo',
  working: 'Trabajando',
  approval: 'Permiso',
  done: 'Listo',
  error: 'Error',
};

/** The header's project menu (design 1a): «PROYECTOS RECIENTES», the recent projects, and
 * «Abrir carpeta…». Relative times use the design's form («Hace 12 min», «Ayer», «Hace 3
 * días»), computed from the project's real `last_opened`. */
const PROJECT_MENU = {
  heading: 'PROYECTOS RECIENTES',
  openFolder: 'Abrir carpeta…',
  empty: 'Todavía no hay proyectos recientes.',
  // A folder that no longer exists says so once (the picker's 1j wording), and the row
  // offers to drop it from the list, as the picker does.
  missingNote: 'Carpeta no encontrada',
  forget: 'Quitar de la lista',
  loadError: 'No se pudo cargar la lista de proyectos',
  justNow: 'Ahora',
  minutesAgo: (n) => `Hace ${n} min`,
  hoursAgo: (n) => `Hace ${n} h`,
  yesterday: 'Ayer',
  daysAgo: (n) => `Hace ${n} días`,
  // NEW, no design source (QA 2026-10-11): the chip and the composer while a project opens.
  opening: (name) => `Abriendo ${name}…`,
};

/**
 * "Volver a un punto" (rewind), file-only, slice 1. Quoted from the build brief
 * (`app-b2-rewind`), which transcribes the design section "Volver a un punto — desde tus
 * propios mensajes": the message link, the confirm dialog, the result line, and the
 * conflict dialog's title and three choices. `confirmBody`/`resultTitle` are functions
 * because they carry a count or clock that exists only once the dry run (or the rewind)
 * has answered.
 */
const REWIND = {
  // "↺ Volver a este punto · HH:MM" on the person's own message: design canvas
  // (`COGITO v2.dc.html`, section 1k), verbatim.
  link: (clock) => `↺ Volver a este punto · ${clock}`,
  // Canvas, verbatim.
  confirmTitle: '¿Volver a este punto?',
  // Canvas: "Se deshacen los cambios posteriores a este mensaje: 3 archivos y el commit
  // <hash>. El mensaje vuelve al campo de texto." Slice 1 is file-only, so the commit
  // clause is dropped; the rest is verbatim.
  confirmBody: (fileCount) =>
    `Se deshacen los cambios posteriores: ${fileCount} archivo${fileCount === 1 ? '' : 's'}`,
  // When the conversation goes back too (the canvas explainer: «Deshace archivos y commits
  // posteriores y te devuelve el mensaje para editarlo»). Same sentence as the canvas'
  // «…: 3 archivos y el commit 4f2a91c.», with the conversation where the commit was
  // (commit undo is out of scope). With no file to restore, it names the conversation alone.
  confirmBodyConversation: (fileCount) =>
    fileCount > 0
      ? `Se deshacen los cambios posteriores: ${fileCount} archivo${fileCount === 1 ? '' : 's'} ` +
        'y la conversación'
      : 'Se deshace la conversación posterior',
  // Canvas, verbatim ("Volver aquí", not "Volver").
  confirmGo: 'Volver aquí',
  confirmCancel: 'Cancelar',
  // Canvas, verbatim.
  inProgress: 'Volviendo a este punto…',
  redo: 'Rehacer',
  // GUESS: no design source for a failed redo (the canvas shows only success). Neutral
  // register, like ERROR_COPY's headlines.
  redoFailed: 'No se pudo rehacer',
  // GUESS: no design source shows a partial failure either (second strict review, item 3:
  // "failures are invisible"). Neutral and factual like the rest of this table: a count,
  // then exactly which paths, never a diagnosis.
  resultFailed: (n) => `${n} archivo${n === 1 ? '' : 's'} no se ${n === 1 ? 'pudo' : 'pudieron'} restaurar`,
  resultFailedPaths: (paths) => paths.join(', '),
  conflictTitle: 'No se puede volver del todo',
  // Canvas: "<path> tiene cambios posteriores a este punto. Los otros 2 archivos se
  // pueden restaurar." (section 1k). `paths.length` picks singular or plural for the first
  // sentence (the canvas shows only one conflict). The second sentence is dropped when
  // nothing else can be restored, and is singular for exactly one.
  conflictBody: (paths, otherCount) => {
    const subject =
      paths.length === 1
        ? `${paths[0]} tiene cambios posteriores`
        : `${paths.length} archivos tienen cambios posteriores`;
    if (otherCount <= 0) return subject;
    const others =
      otherCount === 1
        ? 'El otro archivo se puede restaurar'
        : `Los otros ${otherCount} archivos se pueden restaurar`;
    return `${subject}. ${others}`;
  },
  conflictLeave: 'Dejar como está',
  conflictKeepOthers: (n) => `Restaurar los otros ${n}`,
  // Canvas subtitle under "Restaurar los otros N": "Conserva tu edición en <path>". Shown
  // only when there is exactly one conflicting path to name.
  conflictKeepOthersSubtitle: (path) => `Conserva tu edición en ${path}`,
  conflictForceAll: 'Volver del todo',
  // Canvas subtitle under "Volver del todo": "Descarta tu edición" / "Descarta tus
  // ediciones" (the plural is a GUESS; the canvas shows only one conflict).
  conflictForceAllSubtitle: (n) => (n === 1 ? 'Descarta tu edición' : 'Descarta tus ediciones'),
  // Canvas (section 1k JS): `rvDiffLabel: S.rvDiff ? 'Ocultar diferencias' :
  // 'Ver diferencias en src/cart.ts'`.
  diffShow: (path) => `Ver diferencias en ${path}`,
  diffHide: 'Ocultar diferencias',
  // Canvas: the diff view's two column headers, "Versión actual" / "Se descarta" and
  // "Punto de las HH:MM" / "Se restaura".
  diffCurrentHeading: 'Versión actual',
  diffCurrentNote: 'Se descarta',
  diffPointHeading: (clock) => `Punto de las ${clock}`,
  diffPointNote: 'Se restaura',
  // Canvas (1k): the result pill's two parts, «Volviste a las 10:42» (bold) and «3 archivos
  // · commit deshecho» (slice 1 has no commit clause), and the diff's «src/cart.ts · línea 21».
  resultTitle: (clock) => `Volviste a las ${clock}`,
  resultDetail: (fileCount) => `${fileCount} archivo${fileCount === 1 ? '' : 's'}`,
  // No file to restore, only the conversation: what went back, not «0 archivos».
  resultConversation: 'conversación',
  // Said once to a screen reader when «Rehacer» brings a rewound conversation back.
  redoneStatus: 'Conversación restaurada',
  diffWhere: (path, line) => `${path} · línea ${line}`,
  // NEW, no design source (2026-10-12): the context store (Neo4j) is down, so the core
  // cannot find what to rewind. Said as what happened, with the one action.
  graphDown: 'El almacén de contexto no está disponible',
  graphRetry: 'Reintentar',
};

/** "Reiniciar contexto" (design 1a): the composer button and its confirm dialog, reusing
 * the rewind dialog's models (`buildConfirmDialog`). NEW, no design-canvas source: a small
 * window folds so often that the handover seed contaminates the context, and this is the
 * escape hatch. Said as what happens, not as an instruction (CONTRIBUTING.md). */
const RESET = {
  // The ↻ icon button in the status line, beside the context ring. Its tooltip/aria-label.
  button: 'Reiniciar contexto',
  // Its tooltip: the one-line why.
  buttonTitle: 'Empieza una conversación nueva en este proyecto',
  confirmTitle: '¿Reiniciar el contexto?',
  confirmBody: 'La conversación actual se guarda y empiezas en limpio',
  confirmGo: 'Reiniciar',
  confirmCancel: 'Cancelar',
  inProgress: 'Reiniciando…',
  // Shown when the core refuses (a turn or rewind is running): said as what to do.
  busy: 'Espera a que termine lo que está en marcha',
};

/** The header chip for the context store (Neo4j), shown only while it is not up. NEW, no
 * design source (2026-10-12). */
const GRAPH = {
  down: 'Almacén de contexto no disponible',
  starting: 'Iniciando el almacén de contexto…',
  // Its tooltip while it can be pressed.
  retry: 'Reintentar',
  login: 'Iniciar sesión',
  waitingBrowser: 'Esperando el navegador…',
  loginTitle: (name) => `${name} necesita que inicies sesión`,
  startingTitle: (name) => `Iniciando ${name}…`,
};

/** Small standalone status strings used outside any card. */
const STATUS = {
  starting: 'Iniciando…',
  usage: (k) => `${k}k usados`,
  unknownModel: 'modelo desconocido',
  coreUnavailable: 'núcleo no disponible',
  // NEW, no design source (QA 2026-10-11): the context chip's tooltip when the conversation
  // is larger than the served model's window.
  contextOver: 'No cabe en la ventana de este modelo',
  // Design 1l, shown beside the composer once a turn finishes. Token counts live in the
  // header's usage chip (setUsage in chat.js), and a raw stop_reason such as "end_turn" is
  // internal and never shown (build brief).
  turnDone: 'Listo',
  // GUESS: a turn that ended before finishing (stopped, interrupted, a limit), in place of
  // «Listo». The participle the orb's «Interrumpido» rows use for a call.
  turnStopped: 'Detenido',
};

/** Why a turn stopped before finishing, by the core's `stop_reason`, said once under the
 * turn (the core's own text is English and for the log). GUESS: no design source. A turn
 * stopped with «■ Detener» needs no sentence: «Detenido» says it. */
const STOP_COPY = {
  max_steps: (steps) =>
    Number.isInteger(steps) && steps > 0 ? `Se detuvo tras ${steps} pasos` : 'Se detuvo en el límite de pasos',
  wall_clock: () => 'Se acabó el tiempo',
  token_budget: () => 'Se acabó el presupuesto de tokens',
  consecutive_tool_errors: () => 'Las últimas acciones fallaron',
  no_progress: () => 'Se detuvo sin avanzar',
  // long-output fix 2: a reply the output limit cut off, with its notices exhausted
  // (`StopReason.OUTPUT_LIMIT`) -- never «Listo», the model did not decide it was done.
  output_limit: () => 'Se cortó la respuesta',
  provider_failure: () => 'Falló el servicio del modelo',
  app_closed: () => 'Se cortó al cerrar la app',
  rewound: () => 'Se cortó al volver atrás',
  // «Detener» pressed before anything was drawn (no reply, no tool row): said once so the
  // turn is not an empty bubble holding only «Detenido» (QA 2026-10-13, chat D5). Not a
  // core `stop_reason`: `silentStop` is how `chat.js` and `history.js` ask for it.
  user_interrupt_silent: () => 'Detuviste el turno antes de que respondiera',
  other: () => 'Se detuvo antes de terminar',
};

/** «Continuar» (long-output fix 2): the follow-up an output-limit stop line's button
 * sends, as a new turn, keyed by `turn_complete.cut` ("call" | "text",
 * `loop.engine.TurnResult.cut`) -- the core's own fact, never guessed here. A cut call
 * gets the file-in-parts text, since a model whose cap stays small would only hit the
 * same limit again on a bare "sigue"; a cut reply with no call attempt just continues
 * where it left off. `cut` missing (an older core) falls back to the call text, the
 * safer of the two for an attempt that might have been mid-call. */
const OUTPUT_LIMIT_FOLLOWUP = {
  call:
    'Continúa. El archivo no cupo en una sola respuesta: escríbelo por partes ' +
    '(créalo con el principio nada más y añade el resto con ediciones separadas).',
  text: 'Continúa desde donde te quedaste.',
};

/** A provider failure read back from history, by its category: the live card's headline
 * without the provider's own words, which are not stored. */
const STOPPED_FAILURE = {
  context_overflow: 'No cabe en el contexto',
  spend_cap: 'Límite de gasto alcanzado',
  user_spend_limit: 'Límite de gasto alcanzado',
  rate_limit: 'Límite de uso alcanzado',
  timeout: 'La API no respondió',
};

/** The thread (`render-thread.js`, `history.js`): the day separator above today's first
 * message (design 1a, 3a: «— HOY —»; an earlier day, which the design never shows, gets its
 * date in the app's usual format), and the marker a compaction fold leaves in the history
 * read back on open. */
const THREAD = {
  today: 'HOY',
  daySeparator: (label) => `— ${label} —`,
  // GUESS: the design has no compaction marker. The history shows only that the
  // conversation was folded there (a thin rule), and says so to assistive technology and on
  // hover. The handover text is the model's summary and is never shown.
  folded: 'Conversación resumida',
  // A command the shell moved to the background ended; the turn that follows (if any) is
  // the model reading its report. Generic: the command itself is the tooltip.
  backgroundEnded: (code) =>
    code === null || code === undefined ? 'Se detuvo en segundo plano' : `Terminó en segundo plano · exit ${code}`,
  // GUESS: the model's reasoning while it streams, above its answer.
  thinking: 'Razonando…',
  // APP-ONLY auto-continue (long-output fix 3; GUESS: no design source). The quiet line
  // while a reply the output limit cut off continues on its own: nothing to click, since
  // the person must never have to press «Continuar» (the person's decision, 2026-10-12).
  continuing: 'Continuando…',
  // APP-ONLY: the core starts again a Cogito Motoris that stopped under a request, and asks
  // again once it answers. Same quiet line as «Continuando…».
  restarting: 'Reiniciando el modelo…',
  // GUESS: a long message of the person's, collapsed with its whole text kept (the
  // register of 1l's «Mostrar menos»).
  showAll: 'Mostrar todo',
  showLess: 'Mostrar menos',
};

/** markdown.js's fenced code block chrome, the one piece of `final_text` structure that
 * module renders (see its module comment). GUESS: no design source names a code block;
 * "Código" follows the noun-label style of "Herramienta"/"Aviso del sistema" in design
 * section 1o. */
const MARKDOWN_COPY = {
  code: 'Código',
  codeWithLang: (lang) => `Código · ${lang}`,
  lines: (n) => `${n} línea${n === 1 ? '' : 's'}`,
  // A shell block in a reply: opens a terminal tab with its commands.
  run: 'Ejecutar',
  runTip: 'Ejecutar en una terminal',
};

/** «no API key: set NAME or store one from the settings screen»: the variable name comes
 * from the core, the sentence is ours. */
const NO_API_KEY = /^no API key: set (.+) or store one from the settings screen$/;

/** The Spanish line for the core's missing-key message, or '' for any other message. */
function missingKeyReason(reason) {
  if (typeof reason !== 'string') return '';
  const key = NO_API_KEY.exec(reason);
  if (key) {
    // «an api_key_env»: the entry names no variable at all.
    return /\s/.test(key[1]) ? 'Falta la clave de API' : `Falta la clave de API (${key[1]})`;
  }
  return '';
}

/** The model chip's menu (design 1a and 1c, «Modelo · barra superior»): the catalog grouped
 * by service, the active model checked. Verbatim where the design has a text: «Cargando…» (1c, «Modelo local ·
 * cargando en memoria»), «Sin configurar» and its note (1c's service states). The card
 * title is NEW (the design has no card for a model that cannot be loaded), written in the
 * register of «Límite de uso alcanzado · {servicio}». */
const MODEL_PICKER = {
  check: '✓',
  loading: 'Cargando…',
  unsetStatus: 'Sin configurar',
  unsetNote: 'Falta la clave',
  failedTitle: (model) => `No se pudo cargar · ${model}`,
  technicalDetail: 'Detalle técnico',
};

/** A provider failure that stopped the turn (design 1j and 2c, "Errores de servicio"),
 * verbatim. `{servicio}` is the core's service label ("CLAUDE API", "Cogito Motoris LOCAL"). */
const SERVICE_ERROR = {
  limitTitle: (service) => `Límite de uso alcanzado · ${service}`,
  noResponseTitle: (service) => `La API no respondió · ${service}`,
  // NEW, no design source (QA 2026-10-11): the service answered, with an HTTP error.
  errorTitle: (service) => `La API respondió con un error · ${service}`,
  // NEW, no design source (QA rig, 2026-10-16): a 401/403, `category: auth`.
  authTitle: (service) => `La clave de API no es válida · ${service}`,
  // NEW, no design source (QA rig, 2026-10-16): the project folder was removed or moved.
  projectMissing: 'La carpeta del proyecto ya no existe',
  resumesIn: (wait) => `Se reanuda en ${wait}`,
  retry: 'Reintentar ahora',
  // Design 1j's two cards, verbatim: «Cambiar modelo» on both, «Esperar» on the one the
  // core will resume by itself.
  changeModel: 'Cambiar modelo',
  wait: 'Esperar',
  // The card's button (design 1j: «Reintentar»). `retry` above is for the tray and the
  // notification («Reintentar ahora», 2b/2c).
  cardRetry: 'Reintentar',
  // NEW, no design source: a request the server refused as too large for the model's
  // context window (`category: context_overflow`). Written in the register of «No se pudo
  // cargar · {modelo}» (`MODEL_PICKER`), with the provider's numbers when it gave them
  // («106 225 de 43 008 tokens»), else the service.
  overflowTitle: (detail) => `No cabe en el contexto · ${detail}`,
  overflowCount: (requested, available) => `${requested} de ${available} tokens`,
  // NEW, no design source: the core's `fixed_part_too_large`. Cogito's own prompt and tool
  // definitions are over the window, so no conversation fits this model.
  fixedPartNote: 'La ventana de este modelo es demasiado pequeña',
  // NEW, no design source: «Reintentar» on a turn the core can no longer resume (a later
  // turn or a rewind replaced it, or the core restarted). QA 2026-10-11 bugs 53 and 64.
  notResumable: 'Ya no se puede reanudar',
  // NEW, no design source (QA 2026-10-13, chat D1): what happened, in one short sentence,
  // where the card used to print the provider's own English. The provider's words (status
  // and code, endpoint, message, `retry-after`) wait behind «Detalle técnico»
  // (`MODEL_PICKER.technicalDetail`), collapsed, as the tool rows keep their error.
  said: {
    limit: 'El servicio rechazó la petición por su límite de uso',
    noResponse: 'El servicio no respondió a la petición',
    error: 'El servicio no pudo completar la petición',
    overflow: 'La petición no cabe en la ventana de este modelo',
    auth: 'El servicio rechazó la clave de API',
  },
};

/**
 * The near-rate-limit chip beside the model (design 2e) and its tooltip («Peticiones: 4 de
 * 50 · Tokens de entrada: 3.1k de 40k»). «Tokens de salida» and the combined «Tokens» follow
 * the canvas's two. A non-breaking space goes before every «%», as in the design's «80 %».
 */
const RATE_LIMIT = {
  rateNear: (percent) => `Cerca del límite por minuto · ${percent} %`,
  limiters: {
    requests: 'Peticiones',
    input_tokens: 'Tokens de entrada',
    output_tokens: 'Tokens de salida',
    tokens: 'Tokens',
  },
  limiter: (name, remaining, limit) => `${name}: ${remaining} de ${limit}`,
};

/**
 * Components, programs and permissions (design 4a, 4b, 4e): the handoff README's "Textos
 * fijos nuevos", verbatim. Only the values in {braces} change, and they come from the core's
 * catalog (name, size, description, scope) or the real error: never from the model, and
 * never a sentence of the app's own about one particular tool.
 */
const COMPONENTS = {
  // 4a: propuesta, instalando, instaló, falló.
  proposalTitle: 'Herramienta necesaria',
  enables: 'Qué habilita',
  // The design's footer on the proposal card (4a canvas), under the keyboard-safety rule.
  proposalShortcuts: '↵ Instalar · Esc Ahora no',
  notNow: 'Ahora no',
  install: 'Instalar',
  installing: (name) => `Instalando ${name}`,
  progress: (downloaded, size) => `${downloaded} / ${size}`,
  installed: 'Instaló',
  failedTitle: (name) => `No se pudo instalar ${name}`,
  ignore: 'Ignorar',
  retry: 'Reintentar',
  login: 'Iniciar sesión',
  waitingBrowser: 'Esperando el navegador…',
  loginTitle: (name) => `${name} necesita que inicies sesión`,
  startingTitle: (name) => `Iniciando ${name}…`,
  // Decisiones (README): «Permitiste» / «Rechazaste» · {nombre} · «guardado en ...».
  savedForAllProjects: 'guardado para todos los proyectos',
  // 4b: no disponible.
  missingTitle: 'Falta un programa',
  missingHeading: (program) => `${program} no está instalado`,
  copy: 'Copiar',
  noManager: 'Ningún gestor de paquetes de este equipo puede instalarlo.',
  openOfficial: 'Abrir sitio oficial ↗',
  understood: 'Entendido',
  // The 4b canvas' resolved row once «Entendido» is pressed.
  missingRow: 'Falta',
  missingSeen: 'aviso visto',
  // 4b: permiso.
  permissionTitle: 'Necesita tu permiso',
  tool: 'Herramienta',
  action: 'Qué va a hacer',
  scope: 'Alcance',
  no: 'No',
  // Remembered for every project until the component's switch is turned off.
  allow: 'Permitir',
  installFirst: 'Primero se instala',
  enablesInline: (description) => `Qué habilita: ${description}`,
  installAndAllow: 'Instalar y permitir',
  // 4b: paso detenido.
  stepStoppedTitle: 'Paso detenido',
  reason: 'Motivo',
  where: 'Dónde',
  stepContinues: 'Se sigue sin este paso.',
};

/** 4e: "Herramientas y permisos", opened from the model menu. */
const TOOLS_WINDOW = {
  menuItem: 'Herramientas y permisos…',
  title: 'Herramientas y permisos',
  close: 'Cerrar',
  tabs: {
    components: 'Herramientas',
    connections: 'Conexiones',
    system: 'Del sistema',
    search: 'Búsqueda',
    skills: 'Habilidades',
    appearance: 'Diseño',
  },
  // Diseño: settings for how the app looks, one row each (renderer/appearance.js).
  appearanceHeading: 'Diseño',
  theme: {
    label: 'Tema',
    choices: { light: 'Claro', dark: 'Oscuro', system: 'Sistema' },
  },
  // Where a web search goes, named once so the person can see it (design 4.2, privacy). The
  // chain is `core/cogito/tools/web/chain.py`.
  webSources:
    'La consulta va a la web abierta, por DuckDuckGo.',
  // With the app's own buscador running (core/cogito/setup/searxng.py) the query also goes
  // to the engines it asks, four at a time by turns (`tools/web/chain.py`); the person is
  // told which, and what state it is in. A lab test pins this list to `searxng.ENGINES`.
  webSourcesLocal:
    'La consulta va a la web abierta: un buscador local la envía, por turnos, a algunos de estos: Mwmbl, Yep, Mojeek, Yandex, PrivacyWall, ZapMeta, Yahoo, Bing, Seznam, Fynd, Naver, Brave y DuckDuckGo.',
  localSearch: {
    preparing: 'Preparando el buscador local…',
    starting: 'Iniciando el buscador local…',
    up: 'Buscador local activo.',
    failed: 'El buscador local no está disponible.',
    failedOffline: 'Sin conexión para preparar el buscador local.',
  },
  componentsHeading: 'Herramientas',
  // The date is tied to what precedes it (no-break spaces), so a narrow row wraps before
  // «COGITO», never leaving the date alone on a line.
  installedBy: (date) => (date ? `Instalado por COGITO\u00a0·\u00a0${date}` : 'Instalado por COGITO'),
  notInstalled: 'No instalado',
  // The person's own copy, outside the app (their Cogito Estella, configured or on PATH).
  external: 'Ya está en este equipo',
  systemHeading: 'Del sistema',
  noVersion: '—',
  howToInstall: 'Cómo instalarlo',
  searchHeading: 'Niveles de búsqueda',
  levels: [
    {
      key: 'documentacion',
      n: '1',
      name: 'Documentación',
      description: 'El proyecto y la documentación de sus librerías.',
    },
    {
      key: 'web',
      n: '2',
      name: 'Búsqueda web',
      description: 'Busca en la web y lee páginas de texto.',
    },
  ],
  // The Agent Skills this project and the home folder offer (core/cogito/server/skills.py).
  skillsHeading: 'Habilidades',
  skillsEmpty: 'No hay habilidades.',
};

export {
  VERBS,
  DEFAULT_VERB,
  ROW_NOTES,
  STOP_COPY,
  OUTPUT_LIMIT_FOLLOWUP,
  STOPPED_FAILURE,
  readGroupLabel,
  pathCountLabel,
  OUTSIDE_PROJECT_SENTINEL,
  OUTSIDE_PROJECT_LABEL,
  cancelledLabel,
  CANCELLED_EDIT_NOTE,
  CANCELLED_ICON,
  WAITING_APPROVAL,
  APPROVAL_BUTTONS,
  SHORTCUT_APPROVE,
  SHORTCUT_REJECT,
  ASK_COPY,
  PILLS,
  CHANGES_CARD,
  TOOL_DETAIL,
  ERROR_COPY,
  COMPOSER,
  STEER,
  STEER_SETTLE_MS,
  REWIND,
  RESET,
  GRAPH,
  WINDOW_CHROME,
  ORB_STATE,
  PROJECT_MENU,
  STATUS,
  MARKDOWN_COPY,
  THREAD,
  MODEL_PICKER,
  missingKeyReason,
  SERVICE_ERROR,
  RATE_LIMIT,
  COMPONENTS,
  TOOLS_WINDOW,
};

/**
 * Automatic updates (design 3a/3b), verbatim from the design canvas (`COGITO v2.dc.html`,
 * section `t3`) and the README's fixed-texts list; none is a guess. CSS draws the section
 * headings uppercase, as the canvas does. The main process keeps its own copy of the texts
 * it shows (the tray item, and «Actualización lista», which only the notifier shows:
 * `src/main/updater/copy.js`, for the same CSP reason as `shared/phase-map.js`); a lab test
 * checks that the texts both sides show agree.
 */
const UPDATE = {
  // A non-breaking space before «%».
  downloading: (version, percent) => `Descargando ${version} · ${percent} %`,
  ready: 'Reiniciar para actualizar',
  deferred: 'Se actualiza al terminar',
  // Lowercase after «·» is for a descriptive fragment (e.g. «Detenido · límite de uso»).
  // This whole chip is a button that calls `retry()` (`update-ui.js`'s `onChip`), and
  // «Reintentar» is that action's label, so it keeps its capital (design 3b).
  failed: 'No se pudo actualizar · Reintentar',
  // The same chip when a retry cannot help (it opens the reason's card instead).
  failedFinal: 'No se pudo actualizar',
  menuTitle: (version) => `COGITO ${version} está lista`,
  installed: (version) => `Instalada: ${version}`,
  tools: 'Herramientas nuevas',
  improvements: 'Mejoras',
  atQuit: 'Al salir',
  restartNow: 'Reiniciar ahora',
  restartWhenDone: 'Reiniciar al terminar',
  securityTitle: (version) => `Actualización de seguridad ${version}.`,
  securityBusy: 'Se instalará al terminar esta tarea.',
  securityIdle: 'Lista para instalar.',
  restarting: (version) => `Actualizando a ${version}`,
  updated: (version) => `— ACTUALIZADO A ${version} —`,
  kept: (version) => `— SE MANTUVO ${version} · NO SE PUDO ACTUALIZAR —`,
  showNotes: 'Ver novedades',
  hideNotes: 'Ocultar novedades',
  packageManagerChip: 'Hay una versión nueva',
  packageManagerTitle: (version) => `COGITO ${version}`,
  packageManagerBody: 'Hay una versión nueva disponible.',
  copy: 'Copiar',
  understood: 'Entendido',
  // The reason shown with «No se pudo actualizar», by `UpdateError.code`
  // (`main/updater/errors.js`, carried on the `failed` event). These are the codes a person
  // can run into; any other code gets `failureReasonFallback`, which still names it.
  // `update.js`'s `failureReasonText` chooses between them.
  blockedTitle: 'No se puede actualizar',
  failureReasons: {
    // From mac.js: the app runs from the read-only temporary copy macOS makes of an
    // unnotarized or untrusted download (App Translocation). Writing the new version next
    // to it, or replacing it, cannot work until it runs from a real folder.
    BLOCKED: 'COGITO se ejecuta desde una copia temporal de macOS y no puede actualizarse desde ahí.',
    NOT_WRITABLE: 'COGITO no tiene permiso para escribir donde está instalada.',
    HASH_MISMATCH: 'La descarga no coincide con la firma.',
    NETWORK: 'No se pudo conectar con el servidor de actualizaciones.',
    STALLED: 'La descarga se detuvo antes de terminar.',
  },
  failureReasonFallback: (code) => `No se pudo completar la actualización (${code}).`,
};

export { UPDATE };

/**
 * Attachments (design 1n): the composer's chips, the count beside «+ adjuntar», the drop
 * layer and the reasons a file cannot be attached. One or two words each, never a sentence,
 * never the model's. From the canvas: «Suelta para adjuntar», «Archivo del proyecto», «Error
 * pegado», «Texto · 14 líneas», «Video · no compatible», «1 adjunto». Its drop caption names
 * «imágenes o capturas», which the MVP cannot read, so it says «Archivos» alone. «Texto
 * pegado», «Archivo», the size and the reasons other than a format have no design source.
 */
const ATTACH = {
  count: (n) => (n === 1 ? '1 adjunto' : `${n} adjuntos`),
  dropTitle: 'Suelta para adjuntar',
  dropDetail: 'Archivos · hasta 20 MB cada uno',
  projectFile: 'Archivo del proyecto',
  file: 'Archivo',
  pastedError: 'Error pegado',
  pastedText: 'Texto pegado',
  text: 'Texto',
  lines: (n) => (n === 1 ? '1 línea' : `${n} líneas`),
  notSupported: 'no compatible',
  kinds: { image: 'Imagen', video: 'Video', audio: 'Audio', pdf: 'PDF', binary: 'Binario', folder: 'Carpeta' },
  tooLarge: 'Más de 20 MB',
  imageTooLarge: 'Imagen · demasiado grande',
  unreadable: 'No se puede leer',
  // A document the core could not turn into text (`tools.documents`).
  encrypted: 'Con contraseña',
  scanned: 'Escaneado · sin texto',
  corrupt: 'Archivo dañado',
  failed: 'No se pudo adjuntar',
  // An image attached while the model in use cannot see images (the core's `seen: false`).
  unseen: 'el modelo en uso no ve imágenes',
  list: 'Adjuntos',
  remove: (name) => `Quitar ${name}`,
};

export { ATTACH };

/**
 * The approval cards for a system package request (`approval_request` with `action:
 * "install_packages"`, `"upgrade_system"` or `"install_aur"`, `cli._window_approver`). A
 * card states what is asked, not how to answer it: a title, then one line «paquetes ·
 * gestor · origen» (a full upgrade has no packages: «gestor · origen»). Buttons and
 * shortcuts are the approval card's own (`APPROVAL_BUTTONS`, `SHORTCUT_APPROVE`).
 */
const INSTALL_COPY = {
  title: 'Instalar paquetes del sistema',
  titles: {
    install_packages: 'Instalar paquetes del sistema',
    upgrade_system: 'Actualizar el sistema',
    install_aur: 'Instalar del AUR',
  },
  // The core names the source in English («official repositories»); an unknown one is shown
  // as it came.
  sources: { 'official repositories': 'repositorios oficiales', AUR: 'AUR' },
  line: (packages, manager, source) => [packages.join(', '), manager, source].filter(Boolean).join(' · '),
};

export { INSTALL_COPY };

/**
 * The file explorer and viewer (design 1a «Explorador y visor», 1p), from the handoff's
 * «Textos fijos nuevos · Explorador», verbatim. `reveal` is the system file browser's own
 * name per platform (1p). The error lines for a name that is taken or not allowed, and the
 * notice when «Deshacer» is too late, have no design source: they say what happened, in
 * the same short form.
 */
const EXPLORER = {
  toggle: (mod) => `Explorador · ${mod}+Shift+E`,
  heading: 'EXPLORADOR',
  selecting: 'SELECCIONA PARA ADJUNTAR',
  newFile: 'Nuevo archivo',
  newTerminal: 'Nueva terminal',
  newFolder: 'Nueva carpeta',
  refresh: 'Actualizar',
  collapseAll: 'Contraer todo',
  filter: 'Filtrar archivos',
  noMatches: 'Sin coincidencias',
  more: (n) => `… y ${n} más`,
  defaultFile: 'sin-titulo',
  defaultFolder: 'nueva-carpeta',
  open: 'Abrir',
  openSystem: 'Abrir con la app del sistema',
  reveal: { win32: 'Mostrar en el Explorador', darwin: 'Mostrar en Finder', linux: 'Mostrar en Archivos' },
  rename: 'Renombrar',
  duplicate: 'Duplicar',
  copyPath: 'Copiar ruta',
  copyRelative: 'Copiar ruta relativa',
  attach: 'Adjuntar al mensaje',
  addToSelection: 'Agregar a la selección',
  remove: 'Eliminar',
  keys: { open: '↵', rename: 'F2', remove: { darwin: '⌘⌫', other: 'Supr' } },
  deleteTitle: 'Eliminar',
  deleteQuestion: (name) => `¿Mover «${name}» a la papelera?`,
  changedInTask: 'Modificado en esta tarea',
  moveToTrash: 'Mover a la papelera',
  cancel: 'Cancelar',
  trashed: (name) => `${name} movido a la papelera`,
  undo: 'Deshacer',
  undoLate: 'Ya está en la papelera del sistema',
  trashFailed: (name) => `No se pudo mover ${name} a la papelera`,
  pathCopied: 'Ruta copiada',
  moved: (name, dir) => `${name} movido a ${dir}`,
  intoItself: 'No se puede mover una carpeta dentro de sí misma',
  relativeCopied: 'Ruta relativa copiada',
  alreadyAttached: (name) => `${name} ya está adjunto`,
  taken: 'Ese nombre ya existe',
  badName: 'Nombre no válido',
  failed: 'No se pudo completar',
  // Selection mode (attach from the explorer).
  pick: 'Marca archivos o carpetas',
  files: (n) => (n === 1 ? '1 archivo' : `${n} archivos`),
  skipped: (n) => (n === 1 ? '1 omitido' : `${n} omitidos`),
  ofLimit: (total, limit) => `${total} de ${limit} archivos de contexto`,
  overLimit: (limit, n) => `Límite de ${limit} archivos · quita ${n}`,
  attachN: (n) => (n ? `Adjuntar ${n}` : 'Adjuntar'),
  reasons: {
    attached: 'Ya adjunto',
    tooLarge: 'Más de 20 MB',
    unsupported: 'No compatible',
    secrets: 'Contiene secretos',
    image: 'Imagen no compatible',
    empty: 'Nada que adjuntar',
  },
  folderChip: (name, n) => [`${name}/`, n === 1 ? '1 archivo' : `${n} archivos`],
  // Viewer.
  thread: 'Hilo',
  readOnly: 'Solo lectura',
  readOnlyTip: 'Los cambios los hace COGITO: pídelos abajo',
  changedStrip: (added, removed) => `Modificado en esta tarea · +${added} −${removed}`,
  seeChanges: 'Ver cambios',
  mdView: 'Vista',
  mdSource: 'Fuente',
  fit: 'Ajustar',
  hiddenValues: 'Valores ocultos',
  showValues: 'Mostrar valores',
  hideValues: 'Ocultar valores',
  cannotShow: 'No se puede mostrar este formato',
  closeTab: (name) => `Cerrar ${name}`,
  lines: (n) => (n === 1 ? '1 línea' : `${n} líneas`),
  rows: (n) => (n === 1 ? '1 fila' : `${n} filas`),
  columns: (n) => (n === 1 ? '1 columna' : `${n} columnas`),
  variables: (n) => `Variables · ${n}`,
  jsonInvalid: 'JSON no válido',
};

export { EXPLORER };

/**
 * Conexiones: the MCP servers (design 4e → Conexiones, 4f, 4g, the 1a chip), from the
 * handoff's «Textos fijos nuevos · Conexiones». Short and generic, the same for every
 * server (the person, 2026-10-24); lines that would not be true here are left out: only
 * a tool that makes changes is asked about, with no choice per server (the person,
 * 2026-10-29), there is no keychain and no
 * «Clave» field, and no service is signed into in the browser («Cuenta», «Reconectar»).
 */
const CONNECTIONS = {
  chip: (n) => (n ? `⧉ ${n} ${n === 1 ? 'conexión' : 'conexiones'}` : '⧉ Sin conexiones'),
  menuHeading: (project) => (project ? `CONEXIONES · ${project}` : 'CONEXIONES'),
  add: 'Agregar conexión…',
  manage: 'Administrar conexiones…',
  heading: 'Conexiones',
  addButton: '+ Agregar conexión',
  status: {
    connected: 'Conectado',
    failed: 'No se pudo conectar',
    login: 'Falta iniciar sesión',
    starting: 'Iniciando…',
    disabled: 'Desactivado',
  },
  // «Puede {acciones}»: the first two reads and the first change, by the tools' own titles.
  does: (reads, writes) => {
    const parts = reads.slice(0, 2);
    const head = parts.join(', ');
    if (!writes.length) return head ? `Puede ${head}` : '';
    return head ? `Puede ${head} y ${writes[0]}` : `Puede ${writes[0]}`;
  },
  details: 'Detalles',
  hide: 'Ocultar',
  whatItDoes: 'QUÉ PUEDE HACER',
  read: 'Lectura',
  changes: 'Hace cambios',
  remove: 'Quitar',
  retry: 'Reintentar',
  login: 'Iniciar sesión',
  waitingBrowser: 'Esperando el navegador…',
  loginTitle: (name) => `${name} necesita que inicies sesión`,
  startingTitle: (name) => `Iniciando ${name}…`,
  // The small technical line: the command and what its tools can add to one message.
  tech: (command, tokens) =>
    `Servidor MCP · ${command}${tokens ? ` · ≈${tokens >= 1000 ? `${Number((tokens / 1000).toFixed(1))}k` : tokens} tokens por mensaje` : ''}`,
  empty: 'Sin conexiones',
  // 4f, «Manual» only: COGITO has no list of services yet.
  addTitle: 'Agregar conexión',
  name: 'Nombre',
  namePlaceholder: 'Base de datos local',
  command: 'Comando, dirección o JSON',
  commandPlaceholder: 'uvx mcp-server-postgres o https://…/mcp',
  cancel: 'Cancelar',
  connect: 'Conectar',
  connecting: 'Conectando…',
  connected: (name) => `${name} conectado`,
  done: 'Listo',
  failedTitle: (name) => `No se pudo conectar con ${name}`,
  disable: 'Desactivar',
  // Why «Conectar» did not add it, by the core's `code`.
  refused: {
    name: 'Nombre no válido',
    taken: 'Ese nombre ya existe',
    address: 'La dirección debe ser https',
    command: 'Comando no válido',
    other: 'No se pudo agregar',
  },
  removeQuestion: (name) => `¿Quitar ${name}?`,
  needs: (names) => `Falta un valor: ${names.join(', ')}`,
  // «Importar»: servers other apps already have, read from their files, never changed.
  importButton: 'Importar…',
  importTitle: 'Importar de otras apps',
  importNote: 'Se copian a la configuración de Cogito tal como están en esos archivos, claves incluidas.',
  importNone: 'No hay servidores en otras apps',
  importAdd: 'Agregar seleccionadas',
  importTaken: 'Ya está en Cogito',
  importProject: 'Solo en este proyecto',
  importSets: (names) => `Usa ${names.join(', ')}`,
  importSkipped: (n) => `${n} no se pudo agregar`,
  nameOptional: 'Opcional si el texto ya lo nombra',
  // 4g: the permission card.
  permission: 'Necesita tu permiso',
  sentTo: (server, changes) => `Se envía a ${server}${changes ? '' : ' · No cambia nada'}`,
  showDetails: 'Ver detalles',
  hideDetails: 'Ocultar detalles',
  no: 'No',
  allow: 'Permitir',
};

export { CONNECTIONS };

// The composer's skills picker (skills-tray.js): short, generic copy that says what the
// control does, never step-by-step instructions to the person.
const SKILLS = {
  // The badge glyph on a skill chip (the attach-chip look); the «/» echoes the picker button.
  badge: '/',
  list: 'Habilidades activadas',
  menuHeading: 'HABILIDADES',
  // While the list has not come back yet, and when the project has none.
  loading: 'Cargando…',
  empty: 'Sin habilidades',
  // The chip and its ✕.
  chipLabel: (name) => `Habilidad ${name}`,
  remove: (name) => `Quitar ${name}`,
};

export { SKILLS };

// The composer's thinking control (thinking-control.js): how much the model thinks before it
// answers. Short, as the other composer controls.
const THINKING = {
  menuHeading: 'PENSAMIENTO',
  button: (name) => `Pensar: ${name}`,
  names: { off: 'No', normal: 'Normal', long: 'Más', free: 'Sin límite' },
  descriptions: {
    off: 'Responde de inmediato',
    normal: 'Piensa un poco antes de responder',
    long: 'Piensa más en tareas difíciles',
    free: 'Piensa todo lo que necesite',
  },
};

export { THINKING };

// The composer's automatic mode (auto-mode.js): the switch under the message, and the line
// a card carries when the mode still asks (`approval_request`'s `kept_asking`, `guard.auto`).
const AUTO_MODE = {
  off: 'Pedir permiso',
  on: 'Automático',
  offTitle: 'Pregunta antes de borrar, instalar o usar herramientas que hacen cambios',
  onTitle:
    'Aprueba sin preguntar lo que se puede deshacer. Nunca toca archivos del sistema; pregunta ' +
    'por lo que no va a la papelera, la carpeta del proyecto, tus credenciales y las carpetas de tu inicio',
  failed: 'No se pudo cambiar el modo',
  kept: {
    permanent: 'El modo automático solo aprueba sin preguntar los borrados que van a la papelera.',
    project: 'El modo automático pregunta siempre antes de borrar la carpeta del proyecto.',
    credentials: 'El modo automático pregunta siempre por tus llaves y credenciales.',
    cogito: 'El modo automático pregunta siempre por la configuración y la memoria de Cogito.',
    home_folder: 'El modo automático pregunta siempre por una carpeta entera de tu inicio.',
    unknown: 'El modo automático pregunta siempre cuando no se sabe qué se borraría.',
    mcp_server: 'El modo automático pregunta siempre antes de agregar un servidor MCP.',
  },
};

export { AUTO_MODE };

// Terminal tabs beside «Hilo» (viewer.js, terminals.js) and the card for a line the model
// wants to run in one (`approval_request` with `action: "run_in_terminal"`).
const TERMINAL = {
  newTab: 'Nueva terminal',
  closeTab: (title) => `Cerrar ${title}`,
  running: 'Ejecutando',
  lastCode: (code) => `Código ${code}`,
  ended: (code) => (code == null ? 'Terminada' : `Terminada · código ${code}`),
  openFailed: 'No se pudo abrir la terminal',
  runRefused: 'La terminal no aceptó el comando',
  inputRefused: 'No se pudo enviar a la terminal',
  loadFailed: 'No se pudo cargar la terminal',
  // The line a tab was opened with was not typed (`terminal_unsent`).
  unsentBusy: 'La terminal está ocupada',
  unsentNotReady: 'La terminal no estuvo lista',
  cardTitle: 'Ejecutar en la terminal',
  where: (title, cwd) => [title, cwd].filter(Boolean).join(' · '),
  allow: 'Ejecutar',
  shortcut: '↵ Ejecutar · Esc Rechazar',
};

export { TERMINAL };

// Browser tabs beside «Hilo» (viewer.js, browsers.js, browser-view.js): pages the model or
// the person opens, drawn by the main process over the pane's page area.
const BROWSER = {
  newTab: 'Nueva pestaña',
  closeTab: (title) => `Cerrar ${title}`,
  loading: 'Cargando',
  back: 'Atrás',
  forward: 'Adelante',
  reload: 'Recargar',
  stop: 'Detener',
  address: 'Dirección',
  placeholder: 'Escribe una dirección',
  navigateFailed: 'No se pudo abrir la dirección',
  // Main's `{error}` codes for the address bar; any other is `navigateFailed`.
  navigateErrors: {
    not_address: 'No es una dirección',
    not_web: 'Solo se abren direcciones http y https',
    failed: 'No se pudo abrir la dirección',
  },
  // A page that did not load: the reason by Chromium's net error code
  // (net/base/net_error_list.h, read 2026-11-05), else Chromium's own name for it.
  loadFailed: (reason) => (reason ? `No se pudo cargar la página: ${reason}` : 'No se pudo cargar la página'),
  loadReasons: {
    '-6': 'el archivo no existe',
    '-7': 'tardó demasiado',
    '-10': 'acceso denegado',
    '-20': 'bloqueada',
    '-100': 'el servidor cerró la conexión',
    '-101': 'la conexión se reinició',
    '-102': 'el servidor rechazó la conexión',
    '-105': 'no se encontró el servidor',
    '-106': 'sin conexión a internet',
    '-109': 'no se puede llegar a la dirección',
    '-118': 'la conexión tardó demasiado',
    '-137': 'no se encontró el servidor',
    '-310': 'demasiadas redirecciones',
    '-324': 'el servidor no envió nada',
  },
  certificateInvalid: 'el certificado del sitio no es válido',
};

export { BROWSER };
