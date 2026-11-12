"""Loopback HTTP + SSE API consumed by the desktop app.

Dependencies: graph, loop (and the shared leaves config, errors, ids, cancel).
It does NOT import tools, guard, context or provider.

## SSE events on ``GET /sessions/{id}/events``

Every event is one JSON object with a ``type`` field: ``phase``, ``text_delta``,
``assistant_text``, ``tool_call_progress``, ``compacted``, ``approval_request``,
``approval_resolved``, ``tool_start``, ``tool_end``, ``turn_changes``, ``steer_queued``,
``steer_applied``, ``steer_dropped``, ``point_recorded``, ``rewound``, ``service_error``,
``resume_started``, ``resume_cancelled``, ``rate_limit_near``, ``budget_status``,
``budget_near``, ``budget_reached``, ``budget_decision``, ``turn_complete`` and ``error``.
The component, program, permission and install events are documented in
``server/components.py``, next to their routes.

A turn emits, in order: ``phase`` (sampling); ``text_delta`` events while the model writes,
``tool_call_progress`` events while a tool call is still streaming, and one
``assistant_text`` once its reply is in the transcript; for each tool call, ``tool_start``
just before it runs (and before any ``approval_request`` the guard raises for that call)
and ``tool_end`` when it finishes, failed or cancelled included; then one ``turn_changes``
if the turn touched a file or ran a shell command; then ``turn_complete``, or ``error`` on
an unhandled exception.

The model's words, live (`loop.engine.StreamDelta`, `AssistantText`; joined by
`server.events.StreamEvents`, at most about 60 ms apart while the model writes)::

    {"type":"text_delta","sample":7,"kind":"text|thinking","text":"<a fragment>"}
    {"type":"assistant_text","sample":7,"text":"<the reply's prose, whole>"}

``sample`` numbers the session's requests to the model, each retry its own. A sample with no
``assistant_text`` never joined the transcript (a failed attempt, or a reply the context
window cut off and asked again), and what it streamed is dropped. ``assistant_text`` follows
every reply with text, the turn's last one and a reply ``■ Detener`` cut off included, so
``turn_complete.final_text`` repeats the last one. ``thinking`` fragments are the model's
reasoning, shown while it streams and never stored here. ``compacted`` says a fold just
replaced the conversation with its summary (``{"type":"compacted"}``); ``phase``
``compacting`` comes before it.

A tool call's own progress while it is still streaming (`loop.engine.ToolCallProgress`;
only the OpenAI-shaped dialect sends it, throttled to about 4 a second there)::

    {"type":"tool_call_progress","sample":7,"index":0,"name":"write","path":"core.lua","chars":4021,"lines":88}

``path`` is left out until the partial arguments hold it whole; ``chars`` and ``lines`` are
real counts of what streamed so far, never an estimate of the finished call. The next event
for this ``sample``/``index`` is either the ordinary ``tool_start`` (the call completed) or
nothing at all (the output limit cut it off and it was refused): a window drops the live row
either way once one of those arrives, or once ``turn_complete`` does.

``turn_complete.cut`` is ``"call"`` or ``"text"`` when ``stop_reason`` is
``"output_limit"`` (`loop.engine.TurnResult.cut`): which kind of cut this was, so the
window can offer the right «Continuar» follow-up without asking the model anything new.
Left out for every other stop reason. Journalled the same way on ``turn_ended``
(`server/history.py`'s ``turn_stopped`` item carries it back the same way).

Steering. ``POST /sessions/{id}/steer`` sends a message into the running turn without
stopping it. The message joins that turn once every tool call of the current step has
finished, at the one point before the model is sampled again. It is never spliced between two
tool results of the same step: the OpenAI wire dialect rejects that. ``loop.steer`` and
``loop.engine.Engine._apply_steers`` define the boundary. A steer never cancels a call in
flight; only ``POST /sessions/{id}/interrupt`` does::

    {"type":"steer_queued","id":"<id>","text":"also check the tests"}
    {"type":"steer_applied","id":"<id>","step":3}
    {"type":"steer_dropped","id":"<id>","reason":"turn_ended"}

``steer_queued`` is sent by the ``POST /steer`` route when it accepts the message, before the
engine gets it, so a client never sees the other two for an id it was not told about.
``steer_applied`` is sent from the running turn when the message joins the transcript.
``step`` is the turn's step counter, for display only. It is never sent for a message the
budget was about to stop the turn over (see the check in ``Engine.run``). ``steer_dropped`` is
sent once for each message still queued when the turn stops, and for anything accepted between
the engine finishing and the server noticing (see ``_finalize_turn``). ``reason`` is
``"turn_ended"`` (finished, cut short by a budget, or interrupted) or ``"turn_failed"`` (the
turn raised). Every accepted steer id gets exactly one of ``steer_applied`` and
``steer_dropped``. A dropped message is never carried into a later turn: the desktop puts its
text back in the composer and does not resend it on its own (``desktop/src/renderer/chat.js``).

``tool_start``::

    {"type":"tool_start","call_id":"<id>","name":"read|edit|write|shell|grep|glob|...",
     "target":{"paths":["src/cart.ts"],"command":"npm test","pattern":"applyDiscount",
               "query":"what did the test print"},
     "started_at":"<iso8601>"}

``name: "build"`` is not a model tool. It is the build gate (``loop.engine``,
``_maybe_run_build_gate``) compiling what the turn changed before END_TURN is accepted.
``target.paths`` are the files it was given, and its ``tool_end`` has ``ok: false`` /
``status: "failed"`` when the compile found errors, with ``findings`` (how many) and a
``detail`` listing them. ``status: "skipped"`` means no compiler applied to what was
touched, so nothing was compiled. Its ``call_id`` matches no model call.

``target`` carries only the keys that apply to the tool. Paths are project-relative; a path
outside the project becomes the literal ``"<outside project>"``, never an absolute path.
``command``, ``pattern`` and ``query`` (what ``recall``, ``ask``, ``ask_document`` or
``web_search`` looked up) and ``url`` (the page ``web_fetch`` was asked to read) are clipped
to 200 characters. ``loop.engine._tool_target`` builds
it from the call's arguments, so no tool-specific code lives here.

``approval_request`` (cli.py's ``_window_approver``) carries the facts the card is worded
from: ``id``, ``summary`` (English, a fallback), ``command``, ``targets``, ``unresolved``,
``reason``, ``recoverable``, ``action``, ``recovery``, ``items``, ``targets_unknown``,
``within``, ``listed``, ``listed_more``, ``outside``. For ``action: "install_packages"`` (a
system package install, `guard/packages.py`), ``"upgrade_system"`` (a full upgrade, no
packages) or ``"install_aur"`` (paru or yay) it adds ``packages`` (names), ``manager`` and
``source`` (``"official repositories"`` or ``"AUR"``); ``items`` and ``targets`` are then
empty.

``approval_resolved`` (cli.py's ``_window_approver``, answering the guard's
``approval_request`` with the same ``id``)::

    {"type":"approval_resolved","id":"<id>","allowed":true|false|null,
     "reason":"user|timeout|interrupted"}

``allowed`` is a boolean only for ``reason: "user"``, when a person answered. It is ``null``
when nobody did: ``"timeout"`` (unanswered past its deadline) or ``"interrupted"`` (the turn
was cancelled, or the session closed, while it waited). No answer still means the action does
not run; ``null`` only keeps the window from showing it as a refusal.

``tool_end``::

    {"type":"tool_end","call_id":"<id>","name":"edit","ok":true,
     "target":{"paths":["src/cart.ts"]},"duration_ms":812,
     "exit_code":0,"added":3,"removed":1,
     "status":"done|failed|cancelled","error":"<short reason, present only when not ok>"}

``exit_code`` is shell-only; ``added``/``removed`` (line counts) are edit/write-only. Each is
present only when the tool reported it. ``lines`` is read-only: how many lines the file has.
``matches`` (lines) and ``files`` are a grep's counts; a glob has ``files``; a
``web_search`` has ``matches`` only, its number of results. ``denied: true`` marks a call the
guard refused or the person rejected, which never ran, and a ``web_fetch`` whose address the
web rules refused (`tools/web/urls.py`).
``detail`` is what the row opens onto (an edit's or a write's diff, a command's output
without the tool's ``exit N  cwd`` header, what a search found, or the build gate's errors)
as a bounded preview, present only when there is something to show (`loop.detail`)::

    "detail":{"kind":"diff|output","text":"<at most 200 lines / 16 KiB>","lines":1500,
              "truncated":true,"ref":"<session>/<artifact id>"}

``truncated`` says ``text`` is not all of it; ``ref`` then names the whole, fetched on demand
through ``GET /sessions/{id}/artifacts/{ref}``. Nothing in ``detail`` is sent to a model.
``duration_ms`` starts after the guard's hook resolves, so it excludes time spent waiting on a
person's approval. ``status: cancelled`` means the call was interrupted (an edit is atomic, so
a cancelled edit wrote nothing). It also covers a denial that landed while the turn was being
cancelled, which nobody decided. A denied or unknown-tool call is never clipped: only a real
tool's output counts against the content budget, so the model sees the guard's text whole.

``turn_changes`` is sent once, before ``turn_complete`` (or before ``error``, from whatever was
aggregated so far, if the turn raised), when the turn's edit/write calls changed at least one
file or a shell call ran::

    {"type":"turn_changes","total_files":3,
     "files":[{"path":"src/cart.ts","status":"modified|added|deleted","added":2,"removed":1,
               "diff":"<unified diff, 3 lines of context, clipped to 200 lines>",
               "truncated":false}],
     "shell_changes_unknown":true}

``loop.changes`` builds the diff from ``edit``/``write``'s own before/after text, aggregated
per path over the turn (first "before", last "after"). The internal key is the resolved path,
so `sub/../a.txt` and `a.txt` merge. That text never reaches this module or a model; only the
rendered diff and the line counts do. A path outside the project keeps its absolute path as
the key, so two outside files never collide the way ``tool_start``/``tool_end``'s bare
``"<outside project>"`` would. It is shown as ``"<outside project>/<basename>"``, so two
outside files with the same basename are two entries with the same label. A path created and
removed in the same turn, or edited back to its starting text, is dropped. A path that existed
before the turn and is gone at its end is ``"deleted"``, whatever the last call said. Changes
made through ``shell`` (``sed -i``, ``git``) are never in ``files``. ``shell_changes_unknown``
is ``true`` whenever a shell call ran, so the UI knows the list may be incomplete. A turn where
only shell ran still gets ``{"total_files":0,"files":[],"shell_changes_unknown":true}``.

A file's entry replaces its diff with ``"too_large":true`` plus ``before_bytes``/
``after_bytes`` when either side is over ~1 MB or ~20k lines (``diff`` is then empty), or with
``"binary":true`` when either side does not look like text. Neither is diffed. Once a turn's
diff bytes pass ~256 KB, the remaining files are ``"truncated":true`` with no diff, so the
event stays bounded.

``service_error`` is sent once, before the ``turn_complete`` of a turn that ended with
``stop_reason: "provider_failure"`` (the loop's three attempts all failed), when the engine
kept the last failure (``EngineLike.last_service_error``)::

    {"type":"service_error","service":"CLAUDE API","kind":"limit|no_response",
     "status":429,"code":"rate_limit_error",
     "endpoint":"https://.../chat/completions","message":"<the provider's own words>",
     "retry_after":42.0,"at":"2026-09-28T10:42:00-06:00",
     "category":"timeout|rate_limit|spend_cap|user_spend_limit|overloaded|auth|
                 context_overflow|other",
     "resumes_at":"2026-10-01T00:00:00+00:00"|null,
     "console_url":"https://platform.claude.com/settings/billing"|null}

``service`` is the served model's label (``provider.service``: "CLAUDE API", "Cogito
Motoris LOCAL", "Ollama LOCAL"). ``kind`` is ``limit`` for a 429, a quota/rate code or a spend limit,
else ``no_response``. ``status`` is ``null`` when no response arrived. ``retry_after``
(seconds) is ``null`` unless the provider sent ``retry-after``. ``category`` comes from
``cogito.errors.classify``:

- ``timeout``: no answer at all (a deadline, a refused or dropped connection, a stream cut
  short).
- ``spend_cap``: the provider's own stop. The Claude API's monthly spend cap (a 429 with
  ``error.details.error_code == "enforced_spend_limit_reached"`` and no ``retry-after``),
  prepaid credit used up, or OpenAI's ``insufficient_quota`` (as a code, or as the ``type`` of
  a billing code it does not name).
- ``user_spend_limit``: a limit the person set in the provider's console (the Claude API's 400
  "You have reached your specified ... API usage limits", OpenAI's organization or project
  spend limit).
- ``context_overflow``: the server refused the request as too large for the model's context
  window (llama.cpp's ``exceed_context_size_error``, OpenAI's ``context_length_exceeded``, the
  Claude API's 400 "prompt is too long"). Only this event also carries the provider's numbers,
  ``"requested_tokens":106225,"available_tokens":43008`` (each null when it gave none).

None of the last three is a rate limit, and none ever resumes by itself. ``resumes_at`` is when
access returns, set only when the provider's message says or the rule fixes it (the Claude
monthly cap: 00:00 UTC on the 1st of next month). None of this is sent to a model. The loop
makes three attempts (backoff 1/2/4 s), except that a spend cut or a context overflow is asked
once, since another attempt could only repeat it. A context overflow with earlier history is folded
(compaction) and asked once more before the turn stops on it.

Resuming. A turn the provider stopped can be carried on as the same turn, on the transcript as
it stands (``Engine.resume``). The instruction is not appended again and nothing of the failed
call was kept, so the first request is exactly the one that failed. When the provider gave
``retry-after``, the server schedules one automatic resume for then, so it happens even with
the window hidden or closed. ``service_error`` then carries::

    "resume": {"at": "<iso8601>", "in_seconds": 42.0, "attempt": 1, "max": 3}

It is ``null`` with no ``retry-after`` or one over ``MAX_AUTO_RESUME_DELAY``, for a
``spend_cap``, ``user_spend_limit`` or ``context_overflow`` whatever the headers said, and
after ``max`` automatic resumes of this turn. Then only "Reintentar ahora"
(``POST /sessions/{id}/resume``) is left. Each failure's new ``retry-after`` is honoured. A
pending resume is cancelled by a new message (``POST /turns``), a steer, ``/interrupt``,
``POST /resume`` itself (it resumes now instead), ``DELETE /sessions/{id}/resume`` (a model
switch, say), a rewind or redo, the session's deletion, and the server stopping::

    {"type":"resume_cancelled","reason":"message|steer|interrupt|retry|model|rewind|session|quit|busy"}

When it runs, by the timer or by ``POST /resume``, ``resume_started`` comes first, then the
turn's usual events from ``phase: sampling`` on::

    {"type":"resume_started","auto":true|false}

A turn a spend cut stopped (``category`` ``spend_cap`` or ``user_spend_limit``) ends in phase
``failed``, the tray's error state; any other provider failure ends in ``done``.
``console_url`` on ``service_error`` is the provider's own page for it, from a table in code
(``provider.service.Console``), https only: billing for ``spend_cap`` («Abrir facturación»),
the spend-limit page for ``user_spend_limit`` («Ajustar límite»), else null.

APP ONLY. A Cogito Motoris this core started died under a request; it is started again and
the request asked again (`provider.local.motoris.Revival`), sent on every session's stream
before the restart (``attempt`` null when another request's restart is still loading)::

    {"type":"model_restarting","model_id":"q","attempt":1,"max":3,"exit_code":-11}

If it does not come back, or it died ``max`` times in ten minutes, the turn ends with the
usual ``service_error``.

Per-minute headroom (design 2e). Sent on every session's stream once when the served model's
headroom falls under 10 % of a limiter, and once when it is back at or over 10 %::

    {"type":"rate_limit_near","model_id":"claude","service":"CLAUDE API","percent":8,
     "lowest":"input_tokens","recovered":false,
     "limits":{"requests":{"remaining":940,"limit":1000,"reset":"<iso8601 UTC>"},
               "input_tokens":{...},"output_tokens":{...}}}

``percent`` is the whole percent left, rounded down, of the limiter with least left
(``lowest``) among requests, input tokens and output tokens (requests and tokens where a
provider reports no split). ``limits`` is every limiter the response's headers reported, for
the tooltip. ``recovered: true`` is the same event when it clears. There is no permanent
gauge: ``GET /usage``'s ``active.rate_limit`` carries the same fields only while it is near.

COGITO's own budget (design 2f; ``provider.budget``): the person's number, counted by COGITO.
With no ``[budget]`` configured none of these is sent. ``budget_status`` goes on every
session's stream on every change (each API call, each ``PUT /settings/budget``)::

    {"type":"budget_status","state":"none|ok|near|reached","currency":"USD",
     "monthly":10.0|null,"daily":2.0|null,"spent_month":8.12,"spent_today":0.38,
     "tokens_today":214000,"period":"month|day"|null,"spent":8.12|null,"limit":10.0|null,
     "percent":81.2|null,"month":"2026-09","near_notified_month":"2026-09"|null,
     "console_url":"https://platform.claude.com/usage"|null}

``state`` is ``none`` with no budget or while a local model serves, ``near`` from 80 % of a
limit, ``reached`` from 100 %. ``period``/``spent``/``limit``/``percent`` describe the limit
closest to being reached. Spend is an estimate: tokens times the model's ``price``, cache
included, in ``currency``. A model priced in another currency is not counted. ``budget_near``
goes on the stream of the session whose call crossed 80 % of the month's budget, once per
calendar month::

    {"type":"budget_near","period":"month","spent":8.1,"limit":10.0,"percent":81.0,
     "currency":"USD","month":"2026-09"}

The pause. While a limit is reached, the next request to an API model waits, at a step
boundary and never mid-response. The turn is in phase ``awaiting_approval`` (the amber
permission state) and its session's stream gets::

    {"type":"budget_reached","id":"<question id>","period":"month|day","limit":10.0,
     "spent":10.02,"currency":"USD","next_step":{"kind":"model_request","model":"<label>"}}

It is answered by ``POST /sessions/{id}/budget/{question}``. Each answer is sent as the
thread's marker, and journalled so ``GET /history`` shows it as a ``budget_decision`` item::

    {"type":"budget_decision","id":"<question id>","decision":"continue|raise|stop",
     "reason":"user|interrupted","period":"month|day","limit":20.0,"spent":10.02,
     "currency":"USD"}

``continue`` holds until the turn ends. ``raise``, sent once the window saved a higher budget,
checks again and asks again (a new ``budget_reached``) while still reached. ``stop`` ends the
turn cleanly as ``user_interrupt``. Unlike a permission question, the pause never times out; no
request is in flight while it waits. ``/interrupt`` while it waits, or the session's deletion,
is a stop with ``reason: "interrupted"``. A core that stops ends it with the process, deciding
nothing. None of it reaches the model's transcript. A local model is never paused.

## HTTP routes

Every route requires the bearer token, except ``/health``.

``GET /models``: the selectable catalog, from ``Catalog.grouped()``::

    {"groups":[{"origin":"local",
                "models":[{"id","label","origin","thinking","status"}]},
               {"origin":"api","models":[...]}]}

``thinking`` says whether the composer's thinking control acts on that model (see
``turns``). ``status`` is ``unknown``: the app no longer measures models (2026-10-26).
With a served-model
host (cli.py's ``_ServedModel``) the answer also names the ``active`` model id, and each model
carries its ``service`` label ("Cogito Motoris LOCAL", "CLAUDE API") and ``missing``:
``null``, or what keeps it from being used: ``key`` (an API entry with no key), ``install``
(no Cogito Motoris checkout or engine config for an entry Cogito serves) or ``dialect``. A
saved server Cogito does not start (no ``serve``) is left out while nothing listens at its
address (``cli._offline``), unless it is the active model; a group left with no model is left
out too.

``POST /sessions/{id}/model {"model_id"}``: the window's model picker. One model serves every
session of this core, so every session follows the switch, each on its own transcript. For an
entry Cogito serves with Cogito Motoris, the running server is stopped and the new one
started, which can take as long as loading a model::

    200 {"changed": true|false, "model": {"id","label","origin","service"}}
    400 {"error": "..."}                              # not an object, or no model_id
    404 {"error": "unknown model"} | {"error": "session not found"}
    409 {"error": "turn already running" | "rewinding" | "switching model"}
    422 {"error": "unavailable", "missing": "key|install|dialect"}
    422 {"error": "failed", "model", "log": [...], "restored": bool}
    503 {"error": "model switching unavailable"}

Refused while a turn or a rewind runs in any session. While a switch runs, ``/turns``,
``/resume`` and the rewind routes answer ``409 {"error":"switching model"}``. A switch cancels
every pending automatic resume (``resume_cancelled`` with reason ``model``); "Reintentar
ahora" can still resume the stopped turn, on the new model. ``log`` is the real reason, line
by line. ``restored`` says whether the previous model is serving again. The choice is
remembered for the next ``cogito serve``. On success every session's stream carries::

    {"type":"model_changed","model":{"id","label","origin","service"}}

followed by that session's ``context_status`` against the new model's window (nulls before
any measured request).

``GET /usage``: what the served model has cost today (design 2b), from
``provider.usage.snapshot``: ``{"date","active":{"model_id","model","label","origin",
"service","tokens_today","tokens_per_second","cost_today","gpu","limit",...},"today":{...}}``.
``tokens_per_second`` and ``gpu`` are set only for a local model, ``cost_today`` only for an
API model with a ``price``, ``limit`` only while a usage limit holds. With no source, or one
that fails, the answer is ``{"date":null,"active":null,"today":{}}`` (the latter with
``X-Cogito-Degraded: usage``), never an error: the tray must always draw.

``GET /usage`` also carries ``spend`` (``{"month":"2026-09","today":{"USD":0.38},
"month_to_date":{"USD":8.12}}``, COGITO's estimate per currency, the month rolling over at
local month start), ``budget`` (the ``budget_status`` fields, or null without a budget
source), and ``active.rate_limit`` (above) while it is near.

``GET /settings/budget``: the Gasto tab (design 4e)::

    200 {"monthly":10.0|null,"daily":null,"currency":"USD","currencies":["USD"],
         "status":{<budget_status fields>}}
    503 {"error":"budget unavailable"}

``currencies`` are the ones a budget can be kept in: those the configured models' prices are
written in, plus the budget's own. Spend is counted in its price's currency, so a limit in any
other could never fire.

``PUT /settings/budget {"monthly"?: number|null, "daily"?: number|null, "currency"?: "USD"}``:
save it in config.toml's ``[budget]``. null removes a limit; with neither limit there is no
budget. 200 with the same shape as ``GET``; 400 ``{"error"}`` for an unknown key, an amount
that is not more than zero, or a currency that is not three capital letters.

``POST /sessions/{id}/budget/{question} {"decision":"continue|raise|stop"}``: answer a
``budget_reached`` pause. 200 ``{"status":"answered"}``; 400 for another decision; 404 for an
unknown session or a question this session is not waiting on.

``POST /sessions/{id}/resume``: "Reintentar ahora", carry on the turn a provider failure
stopped, now. ``202 {"status":"resuming"}``; ``409 {"error":"nothing to resume"}`` when the
last turn did not stop on a provider failure (or a later message replaced it); ``409`` while a
turn or a rewind is running, as for ``/turns``. ``DELETE /sessions/{id}/resume
{"reason"?}`` cancels a pending automatic resume: ``{"cancelled": true|false}``.

``POST /sessions {"project_root"}``: open a project. ``201 {"id"}``, the window's session id.
Opening a project continues its conversation (``cli._build_engine``'s ``conversations``,
``graph.resume``): the engine reopens the project's latest session, the live end of its
compaction lineage, with every block the model held in it, so the next turn's request carries
the earlier exchange. A step the app closed in the middle of is closed with a result saying so.
Opening appends nothing to the thread (``GET /history`` answers what it answered before), and
``context_status`` is the last request's measured size from the start. With nothing to
continue, or a conversation another live session of this core is writing to, a new session
starts.

``GET /sessions/{id}/history?before=<index>&limit=<n>``: the project's whole thread, across
every session the app opened it in and each one's compaction lineage, newest page first and
each page oldest first: messages, tool rows, steers, rewind points and each turn's changes,
with a compaction shown as a thin marker. Read from the graph, or replayed from the project's
journal when Neo4j is down. Shapes and paging are in ``server/history.py``::

    200 {"items":[...],"before":"<session id>:<seq>"|null,"source":"graph|wal",
         "context_status":{"used_tokens","window_tokens","percent"}|null}
    400 {"error":"before must be <session id>:<seq>"}    # or "limit must be an integer"
    404 {"error":"session not found"}

``GET /sessions/{id}/artifacts/{session}/{artifact}``: the rest of a tool row's ``detail``, by
the ``ref`` it carried (``server/artifacts.py``). The ref is two plain ids naming a file under
this session's project's ``.cogito/artifacts/``, never a path. A symlink, or anything that
resolves outside that directory, is not served::

    200 {"text":"<its head, at most 1 MiB, whole lines>","lines":1500,"truncated":false}
    400 {"error":"not an artifact reference"}
    404 {"error":"session not found"}            # or "artifact not found"

``context_status`` is how full this session's engine's context window is now (design 1a's
header chip), the same figure a live turn's ``context_status`` SSE event carries, so the
header is right on open or after a project switch without waiting for a turn. It is ``null``
until the engine has sampled once or, for a continued conversation, until one of its requests
was measured: a journal written before sizes were recorded has none to show.

``GET /projects``: every project this graph knows, most recently opened session first::

    {"projects":[{"id","root","name","last_opened_at"}]}

When Neo4j is unreachable this returns ``{"projects": []}`` with header
``X-Cogito-Degraded: graph`` rather than an error. The WAL keeps the rest of the application
usable without a graph, and the project list follows suit.

``GET /settings/projects``: the desktop app's project picker (design 1f), recently opened
projects, newest first. It reads Cogito's config dir, not the graph, so it answers before
Neo4j does::

    {"projects":[{"path","name","last_opened","missing"}]}

``POST /sessions`` records its ``project_root`` here, at the front, once the session has
opened: opening a project is what keeps it recent, and a folder that failed never is. A
failure is JSON with a ``code``: ``400 not_a_directory``, ``403 not_writable`` (no
``.cogito/`` can be made there) or ``500 open_failed``. ``DELETE /settings/projects {"path"}`` is "Quitar de la lista" for one whose
folder no longer exists (``missing: true``); it never touches that project's history. With no
``recent_projects`` configured, ``GET`` answers an empty list and ``DELETE`` answers 503.

``POST /sessions/{id}/turns``: start a new turn. ``409 {"error":"turn already running"}``
while one is in flight: turns are strictly sequential, and steering does not change that.
``409 {"error":"rewinding"}`` while a rewind, redo or dry run is in flight (see the rewind
routes below): a turn and a rewind never run at the same time.

``POST /sessions/{id}/reset``: "reset context" (design 1a). Start a fresh conversation in
the same project — a new empty root session, no handover seed, no instructions, no literals
(`loop.engine.Engine.start_fresh`). The window session id is unchanged; the old conversation
stays whole in the journal and is still readable through ``GET /history``. ``200 {"id"}`` (the
same window id). ``409 {"error":"turn already running"|"rewinding"|"switching model"}`` while a
turn or rewind runs here or a switch is in flight, the model picker's guard. A ``thread_reset``
event and a null ``context_status`` follow, so the window clears the thread and the chip.

``POST /sessions/{id}/attachments {"path"} | {"text"}``: what a file the person chose (an
absolute path), or a long paste, is as an attachment (design 1n). It is read now and never
written. ``200 {"attachment":{"kind":"file","name","path","inside","bytes","lines"}}`` or, for
a paste, ``{"kind":"text","name","bytes","lines","error"}`` (``error``: it looks like an
error's output, `context.attachments.looks_like_error`). ``422 {"error":"refused","reason",
"name"}`` when it cannot be one; ``reason`` is ``folder``, ``unreadable``, ``too_large`` (over
20 MB), ``image``, ``video``, ``audio``, ``pdf`` or ``binary``. 400 for a body that is neither
shape; 503 without an attachment reader.

``POST /sessions/{id}/turns`` and ``/steer`` also take ``"attachments": [{"path"} |
{"text"}]``, at most 20, read again when the message is sent (422 as above for one that
changed since). With attachments the words may be empty. A message without the key is sent to
the model byte for byte as before.

``POST /sessions/{id}/turns`` also takes ``"skills": ["name", …]`` (app-only, design: the
composer's skills picker): the Agent Skills the person activated for this message, force-loaded
for the turn whatever BM25 would have matched (`loop/engine.py`'s ``_add_forced_skills``, built
from `tools/skills.py`'s ``SkillTool.activate``). One already in the window is not re-sent, an
unknown name is passed over. 400 for a ``skills`` that is not a list of strings. A message
without the key is sent to the model byte for byte as before, so `chat` is unaffected.

It also takes ``"thinking": "off"|"normal"|"long"|"free"`` (app-only, the composer's thinking
control):
how much the model may think in this turn (`config.settings.THINKING_LEVELS`). Absent is the
session's own budget, as before. 400 for any other value.

``POST /sessions/{id}/auto {"on": bool}`` (app-only, the composer's automatic mode,
`guard.auto`): with it on, the guard's questions are answered without a card, except those
the mode keeps asking (the project folder itself, keys and credentials, Cogito's own files,
a whole folder in the home directory, a deletion whose targets cannot be known). What the
guard refuses stays refused. Such an answer goes out as ``approval_request`` with ``"auto":
true`` and no ``approval_resolved``; a kept question carries ``"kept_asking"``. It applies
from the next question, a card already up included only once answered. ``{"on": bool}``;
400 for an ``on`` that is not a boolean, 404 for no such session, 409 for a session that
has no automatic mode.

``GET /sessions/{id}/skills`` (app-only, `server/skills.py`): ``{"skills": [{"name",
"description", "directory"}]}`` — the Agent Skills the session's project offers, for the
picker's menu; empty when skills are turned off. 404 for no such session.

``/sessions/{id}/terminals`` (app-only, `server/terminals.py`): the terminal tabs beside
«Hilo»: list, open (optionally typing a line), read one with its scrollback, type, resize and
close. ``{"available": false, "tabs": []}`` for a session without terminals.

``browser_command`` (app-only, `server/browsers.py`): the `browser` tool asks the desktop's
main process, which owns the browser tabs, for one named operation::

    {"type":"browser_command","id":"<32 hex>","op":"navigate|snapshot|click|...","args":{...}}

Main answers on ``POST /sessions/{id}/browser/answers/{answer_id}`` with ``{"ok": true,
"result": {...}}`` or ``{"ok": false, "error", "kind"}``: 200 once handed to the waiting
call, 401 without the token, 404 for an unknown session or an id nobody waits for (late,
duplicate or unknown), 413 over 8 MB, 409 for any other body. Only a session the desktop
app owns has a browser; `chat` never emits this event.

``POST /sessions/{id}/steer {"text"}``: send a message into the running turn without
stopping it::

    202 {"id":"<id>"}                        # accepted; a `steer_queued` event follows
    400 {"error":"..."}                      # not a JSON object, `text` not a string,
                                              # empty after stripping, or over 20,000 chars
    404 {"error":"session not found"}
    409 {"error":"no turn running"}          # nothing is running to steer
    409 {"error":"rewinding"}                # a rewind/redo/dry-run is in flight instead
    429 {"error":"..."}                      # 20 messages already queued for this turn

A 202 only means the message was queued, not that the model has seen it. ``steer_applied``
(delivered) or ``steer_dropped`` (the turn ended first) says what became of it. On the
``"no turn running"`` 409 the desktop falls back to an ordinary ``POST /turns``, for example
when its view of the turn raced with the turn finishing. It must not fall back on the
``"rewinding"`` 409: ``POST /turns`` refuses during a rewind too.

``rewinding`` is a session-wide flag, separate from ``running``. Each of the three rewind
routes below sets it when it starts its blocking work and clears it when done, under the same
lock as ``running``. While it is set no turn starts, steers are refused, and no other rewind,
redo or dry run starts.

``point_recorded`` is sent each time a message exists that a rewind could later target: the
turn's opening message, and each steer once it joins the transcript (alongside its
``steer_applied``)::

    {"type":"point_recorded","id":"<message id>","step":2}

``id`` is the ``point`` a later ``GET``/``POST /rewind/{point}`` takes. The desktop's
"↺ Volver a este punto · HH:MM" link needs it, so it keeps the latest ``point_recorded`` id for
the message it was drawing. Never sent for a tool result: it shares the transcript role
(``"user"``) but is not something a person can rewind to.

``GET /sessions/{id}/rewind/{point}``: "Volver a un punto", dry run. ``point`` is a
``:Message {role:"user"}`` id: the message that opened a turn, or a steer spliced into one
running. Every such message already has one; nothing new is minted. A rewind goes back on the
files (the changes an ``edit``/``write`` made after that point, restorable through the same
``UndoStore``/trash those tools write to) and on the conversation: the model holds what it
held right before the point, and the point's own words go back to the composer. Nothing is
deleted: the messages from the point on are hidden (``conversation_rewound``,
`graph.resume`), and a redo brings them back. ``conversation`` says whether the conversation
goes back too (false only for a point the journals no longer hold)::

    200 {"files":[{"path":"src/cart.ts","status":"modified|added|deleted"}],
         "conflicts":[{"path":"src/cart.ts","diff":"<unified diff>"}],
         "commit_after":false,"conversation":true}
    404 {"error":"rewind point not found"}       # unknown id, or neither in this session's
                                                  # compaction lineage nor in its project's
                                                  # thread (an earlier opening, from history)
    409 {"error":"turn already running"}         # or "rewinding" while another rewind,
                                                  # redo or dry run is in flight, or
                                                  # "switching model"
    500 {"error":"rewind dry run failed"}        # a guard-layer error not shaped as a
                                                  # per-path failure; see POST below
    503 {"error":"graph unavailable"}            # Neo4j is down: the files are found there,
                                                  # so nothing is known and nothing ran. The
                                                  # POST routes and redo answer it too

``commit_after`` is always ``false``: the mockup's commit undo is out of scope. This route
never runs git and does not detect a later commit, so it says nothing about commits rather
than something wrong. A ``conflicts`` entry
means that path's live content no longer matches what the agent last wrote there (a person or
another process changed it). ``POST`` refuses to touch it without ``keep_conflicts`` or
``force``.

``POST /sessions/{id}/rewind/{point} {"keep_conflicts"?, "force"?}``: perform it. Every file
this restores or overwrites is preserved first (trashed, or snapshotted into the undo store
that ``cogito undo`` reads). That includes a file deleted to reach the point's state and any
conflicting file ``force`` overwrites, so nothing here is a permanent loss.
``keep_conflicts``/``force`` must be JSON booleans when present; anything else is a 400, never
coerced::

    200 {"redo_id":"<id>","files":[{"path":...,"status":...}],"kept":[<path>,...],
         "forced":[<path>,...],"failed":[{"path":...,"reason":"..."}],
         "conversation":{"hidden":<n>}|null,"point_text":"<the point's own words>",
         "redo":true|false}
    400 {"error":"force must be a boolean"}
    404 {"error":"rewind point not found"}
    409 {"error":"turn already running"}
    409 {"error":"rewinding"}                # another rewind/redo/dry-run is in flight
    409 {"error":"conflict","files":[...],"conflicts":[...],"commit_after":false}  # neither
                                              # keep_conflicts nor force was set, and at
                                              # least one file is in conflict
    500 {"error":"rewind failed"}

``keep_conflicts:true`` restores every other file and leaves conflicting ones alone (listed in
``kept``, not ``files``). ``force:true`` restores everything; each conflicting file's live
content is preserved first (``forced``). ``failed`` lists any path that could not be touched
(a directory where a file should be, a symlink pointing outside the project). Every other path
still succeeds, so a partial result is a 200 with ``failed`` non-empty, never a 500.
``conversation`` is what the conversation lost (``hidden`` messages: the point and everything
after it) and ``point_text`` the words that go back to the composer. A rewind with no file to
restore still rewinds the conversation. ``redo`` says whether «Rehacer» applies. The engine
then holds what it held before the point (what a restart would read back), so the next turn's
request carries none of what was rewound. A 200 also emits ``rewound``, then
``context_status`` for the conversation as it now stands (all ``null`` when nothing measured
is left)::

    {"type":"rewound","point_id":"<id>","redo_id":"<id>",
     "files":[{"path":...,"status":...}],"conversation":{"hidden":3},
     "point_text":"...","redo":true}

``POST /sessions/{id}/redo/{redo_id}``: "Rehacer". Reverses exactly what one past
``POST /rewind`` did: every path it touched goes back to what it held just before that rewind,
and the conversation back to where it stood (``conversation``). Single-use: a second call for
the same ``redo_id`` is a 409. It works only while the conversation has not moved on. Once a
turn starts after the rewind, or it is rewound again, the rewound branch is final (still in
the store, still hidden) and redo answers 409 ``conversation moved on``; ``GET /history``'s
``rewind`` item then says ``redo: false``::

    200 {"restored":["src/cart.ts", ...],"failed":[{"path":...,"reason":"..."}],
         "conversation":true|false}
    404 {"error":"redo not found"}                # unknown id, or not reachable from this
                                                    # session through any chain of compactions
    409 {"error":"turn already running"}
    409 {"error":"rewinding"}                      # another rewind/redo/dry-run is in flight
    409 {"error":"already redone"}
    409 {"error":"conversation moved on"}
    500 {"error":"redo failed"}
"""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from cogito.cancel import CancelToken
from cogito.config.models import Catalog
from cogito.config.settings import THINKING_LEVELS
from cogito.errors import DETERMINISTIC_CATEGORIES, GraphUnavailable, NoModel
from cogito.graph.driver import GraphDriver
from cogito.graph.history import open_source, wal_source
from cogito.graph.sessions import list_projects
from cogito.ids import new_id
from cogito.loop.changes import build_turn_changes
from cogito.loop.engine import TurnResult
from cogito.loop.state import Phase, RunState, StopReason
from cogito.loop.steer import QueuedSteer

from . import artifacts as tool_artifacts
from . import browsers as browser_routes
from . import components as component_routes
from . import connect as connect_routes
from . import connections as connection_routes
from . import history as thread_history
from . import skills as skill_routes
from . import terminals as terminal_routes
from .approvals import Approvals
from .events import _SENTINEL, EventChannel
from .questions import Questions

log = logging.getLogger(__name__)

#: `POST /sessions/{id}/steer`'s limits. They bound what an unattended session can pile up:
#: one message big enough to matter to a budget, and a backlog nothing is draining.
_MAX_STEER_TEXT_CHARS = 20_000
_MAX_PENDING_STEERS = 20

#: The most attachments one message may carry. Matches the desktop's cap on one pick or
#: drop (`desktop/src/main/attach-ipc.js`, ``MAX_PICKED``).
MAX_ATTACHMENTS = 20
#: Turns woken in a row for background commands without a message from the person
#: (`_maybe_wake`): a model that keeps starting commands that outlive their timeout cannot
#: run on its own for ever. A choice, not a measurement.
MAX_WAKES = 10

#: Automatic resumes of one turn after a provider failure with ``retry-after``. After this
#: many only "Reintentar ahora" is left.
MAX_AUTO_RESUMES = 3

#: The longest ``retry-after`` (seconds) the server waits out by itself. Longer gets no
#: automatic resume, only "Reintentar ahora": nothing should wake up a day later and spend
#: on the person's behalf.
MAX_AUTO_RESUME_DELAY = 3600.0


class Cancellable(Protocol):
    def cancel(self) -> None:
        ...


#: ``schedule(delay_seconds, fn)``: run ``fn`` once, later, on some other thread.
Schedule = Callable[[float, Callable[[], None]], Cancellable]


def _thread_timer(delay: float, fn: Callable[[], None]) -> Cancellable:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


class _SessionChannel(EventChannel):
    """A session's event channel that also remembers the ids of the permission requests and
    budget pauses it carried. Only that session may answer them (permissions in
    `server/components.py`, budgets in ``POST /sessions/{id}/budget/{question}``), never
    another window's session that learned an id. No lock: some events are emitted by code
    already holding `sessions_lock`, a set's add is atomic, and the routes only test
    membership."""

    def __init__(self) -> None:
        super().__init__()
        self.permission_ids: set[str] = set()
        self.budget_ids: set[str] = set()
        #: APP-ONLY. Called after a ``background_finished`` event goes out: the server
        #: wakes a turn for it (`_maybe_wake`). Set once the session is registered.
        self.on_background: Callable[[], None] | None = None

    def emit(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "permission_request" and isinstance(event.get("id"), str):
            self.permission_ids.add(event["id"])
        elif kind == "budget_reached" and isinstance(event.get("id"), str):
            self.budget_ids.add(event["id"])
        super().emit(event)
        if kind == "background_finished" and self.on_background is not None:
            self.on_background()


@dataclass(frozen=True, slots=True)
class _PendingResume:
    """One scheduled automatic resume. The timer checks ``token`` when it fires, so a
    resume cancelled or replaced in the meantime never runs."""

    token: object
    handle: Cancellable

# ---------------------------------------------------------------------------
# Protocol for the injected engine factory
# ---------------------------------------------------------------------------


class EngineLike(Protocol):
    """Shape that ``engine_factory`` must return."""

    def run(self, text: str, state: RunState) -> TurnResult:
        """A new turn. A message with attachments (design 1n) is run as
        ``run(text, state, attachments=...)`` with what `AttachmentLoader` read; one that
        activates skills (app-only) as ``run(text, state, skills=[...])`` with the names the
        person picked in the composer; one with neither is always the two-argument call."""
        ...

    def close(self) -> None:
        ...

    @property
    def pending_file_changes(self) -> tuple[Mapping[str, Any], bool]:
        """The current turn's file_changes/shell_ran aggregate, readable even if ``run``
        never returned. ``_run_turn``'s ``except`` branch needs it; see
        ``Engine.pending_file_changes``."""
        ...

    def steer(self, steer_id: str, text: str) -> None:
        """Queue a message for the running turn. The route mints the id (see ``post_steer``
        and ``Engine.steer``). One with attachments is ``steer(steer_id, text, attachments)``.

        Legal whether or not a turn is running: the ``POST /steer`` route decides that, and
        answers 409 without calling this when none is.
        """
        ...

    def pending_steer_count(self) -> int:
        """How many steer messages are queued now, for ``post_steer``'s queue cap. See
        ``Engine.pending_steer_count``."""
        ...

    def drain_leftover_steers(self) -> tuple[QueuedSteer, ...]:
        """Remove and return everything still queued.

        ``_finalize_turn`` calls it when it sets ``running`` back to False, under the lock
        ``post_steer`` reads that flag under. It must drain, not peek: see
        ``Engine.drain_leftover_steers`` for the race a peek leaves open.
        """
        ...

    def rewind_dry_run(self, point_id: str) -> Mapping[str, Any]:
        """What ``GET /sessions/{id}/rewind/{point}`` returns as-is: ``{"error":
        "not_found"|"failed"|"graph_unavailable"}``, or ``{"files","conflicts","commit_after"}``. See
        ``cli.EngineSession.rewind_dry_run`` / ``guard.rewind.dry_run``. Blocking; the
        route runs it through ``asyncio.to_thread``.
        """
        ...

    def rewind_perform(
        self, point_id: str, *, keep_conflicts: bool = False, force: bool = False
    ) -> Mapping[str, Any]:
        """What ``POST /sessions/{id}/rewind/{point}`` returns as-is: ``{"error":
        "not_found"|"failed"|"graph_unavailable"}``, ``{"error": "conflict", "files","conflicts",
        "commit_after"}``, or ``{"redo_id","files","kept","forced","failed","conversation",
        "point_text","redo"}`` on success (``failed`` empty unless a path could not be
        touched at all; ``conversation`` null when only the files went back). See
        ``cli.EngineSession.rewind_perform`` / ``guard.rewind.perform``.
        """
        ...

    def rewind_redo(self, redo_id: str) -> Mapping[str, Any]:
        """What ``POST /sessions/{id}/redo/{redo_id}`` returns as-is: ``{"error":
        "not_found"|"already_redone"|"moved_on"|"failed"|"graph_unavailable"}`` or ``{"restored": [<path>, ...],
        "failed": [...], "conversation": bool}``. See ``cli.EngineSession.rewind_redo`` /
        ``guard.rewind.redo``.
        """
        ...

    def last_service_error(self) -> Mapping[str, Any] | None:
        """The last provider failure this session's turn ran into, as the ``service_error``
        event's fields, or None. Optional: an engine without it never gets the event."""
        ...

    @property
    def can_resume(self) -> bool:
        """True while the last turn's stop on a provider failure can be resumed. Optional:
        an engine without it is never resumed."""
        ...

    def resume(self, state: RunState) -> TurnResult:
        """Carry on the turn a provider failure stopped; see ``Engine.resume``."""
        ...

    def start_fresh(self) -> str:
        """Begin a fresh conversation in the same project, wiping the window; see
        ``Engine.start_fresh``. Returns the new root session id. Reached only by ``POST
        /sessions/{id}/reset``."""
        ...


class AttachmentLoader(Protocol):
    """Reads one attachment as the window names it (``{"path": "/abs"}`` or ``{"text":
    "..."}``) for a session's project; cli.py's `context.attachments.load`. Answers None
    for anything else, an object with ``reason`` and ``as_dict()`` for one that cannot be
    attached, or else the attachment: ``facts()`` for the window, and passed as is to the
    engine's ``run``/``steer``. Blocking: the routes run it off the event loop."""

    def __call__(self, spec: Any, project_root: Path) -> Any:
        ...


class ModelsLike(Protocol):
    """The model this core serves, and the picker's switch (cli.py's `_ServedModel`). One
    per core: every session talks to it."""

    def active_id(self) -> str | None:
        ...

    def facts(self, model_id: str) -> Mapping[str, Any] | None:
        """``{"service", "missing"}`` for one catalog entry, None for an unknown one."""
        ...

    def switch(self, model_id: str) -> Mapping[str, Any]:
        """Blocking: may stop one local model server and start another. See the route."""
        ...


class BudgetLike(Protocol):
    """COGITO's own budget (cli.py's `provider.budget.BudgetKeeper`). Both answer plain
    dicts; ``update`` answers ``{"error"}`` for values it refused."""

    def view(self) -> Mapping[str, Any]:
        ...

    def update(self, values: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class RecentProjectsLike(Protocol):
    """The desktop app's project picker (design 1f): cli.py's `_RecentProjects`, kept in
    Cogito's config dir, not the graph (its docstring says why)."""

    def list(self) -> list[Mapping[str, Any]]:
        ...

    def touch(self, project_root: str) -> None:
        ...

    def forget(self, project_root: str) -> None:
        ...


#: The answers a budget pause takes: the wire contract `provider.budget` reads.
BUDGET_DECISIONS = ("continue", "raise", "stop")


class EngineFactory(Protocol):
    """Signature for the factory injected by cli.py.

    ``cancel`` is the token this session's ``/interrupt`` route cancels. The factory must
    wire it into the engine's cancellation of running tools and provider calls. Otherwise
    an interrupt stops the sampling loop but not a running shell command.
    """

    def __call__(
        self,
        project_root: Path,
        emit: Callable[[dict[str, Any]], None],
        cancel: CancelToken,
    ) -> EngineLike:
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_context_status(engine: Any, channel: EventChannel) -> None:
    """After a rewind, a redo or a model switch changed what the model holds or its
    window, send the header's context chip the new figure, or nulls when there is none (a
    point before any measured request)."""
    try:
        status = getattr(engine, "context_status", None)
    except Exception:
        log.exception("reading context_status raised; the chip keeps what it had")
        return
    empty = {"used_tokens": None, "window_tokens": None, "percent": None}
    channel.emit({"type": "context_status", **(dict(status) if status else empty)})


def _auth_ok(request: Request, token: str) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[7:]
    return hmac.compare_digest(provided, token)


# ---------------------------------------------------------------------------
# Turn runner (worker thread)
# ---------------------------------------------------------------------------


def _turn_changes_event(result: TurnResult) -> dict[str, Any] | None:
    """The ``turn_changes`` event for a finished turn's result, or ``None``. Built by
    `_turn_changes_event_from`, which `_run_turn`'s ``except`` branch also uses: a turn that
    raised has no `TurnResult`, only `engine.pending_file_changes`.
    """
    return _turn_changes_event_from(result.file_changes, result.shell_ran)


def _turn_changes_event_from(
    file_changes: Mapping[str, Any], shell_ran: bool
) -> dict[str, Any] | None:
    """The ``turn_changes`` event for this aggregate, or ``None`` when there is nothing.

    Takes the raw aggregate, not a ``TurnResult``, because ``_run_turn``'s ``except``
    branch builds it from ``engine.pending_file_changes`` when the turn raised.

    A changed file or any shell call is something to say. The harness cannot see a shell
    call's file effects, so a turn where only shell ran still gets
    ``{"total_files": 0, "files": [], "shell_changes_unknown": true}``.
    """
    if not file_changes and not shell_ran:
        return None
    turn = build_turn_changes(file_changes, shell_ran=shell_ran)
    event: dict[str, Any] = {
        "type": "turn_changes",
        "total_files": turn.total_files,
        "files": [
            {
                "path": f.path,
                "status": f.status,
                "added": f.added,
                "removed": f.removed,
                "diff": f.diff,
                "truncated": f.truncated,
                **({"too_large": True} if f.too_large else {}),
                **({"before_bytes": f.before_bytes} if f.before_bytes is not None else {}),
                **({"after_bytes": f.after_bytes} if f.after_bytes is not None else {}),
                **({"binary": True} if f.binary else {}),
            }
            for f in turn.files
        ],
    }
    if turn.shell_ran:
        event["shell_changes_unknown"] = True
    return event


def _pending_file_changes(engine: EngineLike) -> tuple[Mapping[str, Any], bool]:
    """``engine.pending_file_changes``, or nothing for an ``EngineLike`` without it (a
    test double, most likely). That turn still ends with turn_complete or error, not an
    ``AttributeError`` on top of what already went wrong.
    """
    getter = getattr(engine, "pending_file_changes", None)
    if getter is None:
        return {}, False
    return getter


def _drain_leftover_steers(engine: EngineLike) -> tuple[QueuedSteer, ...]:
    """``engine.drain_leftover_steers()``, or nothing for an `EngineLike` without it, for
    the same reason as `_pending_file_changes`."""
    method = getattr(engine, "drain_leftover_steers", None)
    if method is None:
        return ()
    return method()


def _service_error_event(engine: EngineLike, result: TurnResult | None) -> dict[str, Any] | None:
    """The ``service_error`` event for a turn that ended because the provider failed, or
    None. An engine without ``last_service_error``, or whose record cannot be read, only
    loses this event, never the ``turn_complete`` after it."""
    if result is None or result.stop_reason is not StopReason.PROVIDER_FAILURE:
        return None
    getter = getattr(engine, "last_service_error", None)
    if not callable(getter):
        return None
    try:
        record = getter()
    except Exception:
        log.exception("reading the last service error raised; the turn completes without it")
        return None
    if not record:
        return None
    return {"type": "service_error", **record}


def _steer_dropped_events(steers: Sequence[QueuedSteer], *, reason: str) -> list[dict[str, Any]]:
    """One `steer_dropped` event per message that never reached the transcript."""
    return [{"type": "steer_dropped", "id": steer.id, "reason": reason} for steer in steers]


def _finalize_turn(
    session_id: str,
    engine: EngineLike,
    channel: EventChannel,
    sessions: dict[str, dict[str, Any]],
    sessions_lock: threading.Lock,
    *,
    result: TurnResult | None,
    exc: BaseException | None,
    plan_resume: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> None:
    """The one place a turn's background thread finishes: on success, on a caught
    exception, and on an exception raised while handling that one. `_run_turn` calls it
    from its ``finally``, so it runs exactly once on every path. Without that, a second
    failure would leave ``running`` set and the session stuck for good.

    Inside one `sessions_lock` acquisition it sets ``running`` back to False, drains the
    engine's queue (`Engine.drain_leftover_steers`; a peek would let a steer be reported
    dropped and still be applied to the next turn), and emits every ``steer_dropped`` and
    the terminal event (``turn_complete`` or ``error``). Do not split that lock:
    `post_steer` takes it to decide whether a turn is running and `post_turn` to start one,
    so neither can put its events ahead of this turn's last ones. Every steer id
    `post_steer` hands out is then reported exactly once, applied or dropped, and never
    carried into a later turn.

    The outer ``try``/``except`` is the safety net: if draining or emitting raises,
    ``running`` is still cleared and a ``failed`` phase is sent; the cause is only in the
    log.
    """
    try:
        with sessions_lock:
            if session_id in sessions:
                sessions[session_id]["running"] = False
                # «Detener», or a stop on the budget card: no turn is woken until the person
                # writes again (`_maybe_wake`).
                sessions[session_id]["stopped"] = bool(
                    result is not None and result.stop_reason is StopReason.USER_INTERRUPT
                )
            leftover = _drain_leftover_steers(engine)
            undelivered = result.undelivered_steers if result is not None else ()
            reason = "turn_failed" if exc is not None else "turn_ended"
            for event in _steer_dropped_events((*undelivered, *leftover), reason=reason):
                channel.emit(event)
            if exc is None and result is not None:
                if (service_error := _service_error_event(engine, result)) is not None:
                    # Decided here, under the same lock `/turns`, `/steer` and `/resume`
                    # take, so nothing can slip in between the failure and its schedule.
                    service_error["resume"] = _planned(plan_resume, session_id, service_error)
                    channel.emit(service_error)
                turn_complete: dict[str, Any] = {
                    "type": "turn_complete",
                    "stop_reason": result.stop_reason.value,
                    "steps": result.steps,
                    "total_tokens": result.total_tokens,
                    "final_text": result.final_text,
                }
                if result.cut is not None:
                    # "call" | "text": which output-limit stop this was, so the window can
                    # choose between its two «Continuar» follow-ups without asking the
                    # model anything new. Left out for every other stop reason.
                    turn_complete["cut"] = result.cut
                channel.emit(turn_complete)
            else:
                message = f"{type(exc).__name__}: {exc}" if exc is not None else "the turn ended with no result"
                # The engine never reached its terminal phase. Send one, so nothing that
                # follows phases (the tray, the updater's "idle") waits for it forever.
                channel.emit({"type": "phase", "phase": Phase.FAILED.value})
                error: dict[str, Any] = {"type": "error", "message": message}
                # The project folder removed or moved while the app was open: the journal
                # cannot be written, and the raw error named only a .cogito/wal file
                # (QA rig, 2026-10-16). Named, so the window can say what happened.
                root = (sessions.get(session_id) or {}).get("project_root")
                if root and not Path(root).is_dir():
                    error.update(code="project_missing", path=str(root))
                channel.emit(error)
    except Exception:
        log.exception("turn %s: finalizing the turn itself raised", session_id)
        with sessions_lock:
            if session_id in sessions:
                sessions[session_id]["running"] = False
        try:
            channel.emit({"type": "phase", "phase": Phase.FAILED.value})
        except Exception:
            log.exception("turn %s: not even the terminal phase could be sent", session_id)


def _planned(
    plan_resume: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None,
    session_id: str,
    failure: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """``service_error.resume``: the plan, or None. Also None when planning raises, which
    must never cost the ``turn_complete`` after it."""
    if plan_resume is None:
        return None
    try:
        return plan_resume(session_id, failure)
    except Exception:
        log.exception("turn %s: planning its resume raised; none is scheduled", session_id)
        return None


def _run_turn(
    session_id: str,
    text: str,
    engine: EngineLike,
    channel: EventChannel,
    cancel: CancelToken,
    sessions: dict[str, dict[str, Any]],
    sessions_lock: threading.Lock,
    *,
    resume: bool = False,
    plan_resume: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
    attachments: Sequence[Any] = (),
    skills: Sequence[str] = (),
    thinking: str | None = None,
    wake: bool = False,
    after_turn: Callable[[str], None] | None = None,
) -> None:
    """Run one turn on a background thread: a new one with ``text`` and its already-read
    ``attachments``, or with ``resume`` the one a provider failure stopped
    (``Engine.resume``). `_finalize_turn`, in the ``finally``, always marks the session
    idle and tells the client how the turn ended; its docstring says why that must be
    unconditional."""
    state = RunState(cancel=cancel)
    result: TurnResult | None = None
    exc: Exception | None = None
    try:
        channel.emit({"type": "phase", "phase": Phase.SAMPLING.value})
        if resume:
            result = engine.resume(state)
        elif wake:
            # APP-ONLY: background commands ended while no turn ran (`Engine.wake`).
            result = engine.wake(state)  # type: ignore[attr-defined]
        elif attachments or skills or thinking:
            # Only what the message carries, so an attachments-only turn is the same call it
            # always was and a skills-only one adds nothing else.
            extra: dict[str, Any] = {}
            if attachments:
                extra["attachments"] = attachments
            if skills:
                extra["skills"] = skills
            if thinking:
                extra["thinking"] = thinking
            result = engine.run(text, state, **extra)
        else:
            result = engine.run(text, state)
        if (changes_event := _turn_changes_event(result)) is not None:
            channel.emit(changes_event)
    except Exception as caught:
        log.exception("turn %s failed", session_id)
        exc = caught
        # Report what the engine aggregated before it raised: a turn that wrote three
        # files and then failed should not look to the window as if nothing happened.
        # Guarded on its own because this call can raise a second time.
        try:
            file_changes, shell_ran = _pending_file_changes(engine)
            if (changes_event := _turn_changes_event_from(file_changes, shell_ran)) is not None:
                channel.emit(changes_event)
        except Exception:
            log.exception("turn %s: building the failure path's own changes event also raised", session_id)
    finally:
        _finalize_turn(
            session_id,
            engine,
            channel,
            sessions,
            sessions_lock,
            result=result,
            exc=exc,
            plan_resume=plan_resume,
        )
        if after_turn is not None:
            try:
                after_turn(session_id)
            except Exception:
                log.exception("turn %s: what follows the turn raised", session_id)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _status_of(model_status, model_id: str) -> dict[str, str]:
    """Only `status` and a `not-yet` reason ever reach the window, and a status source that
    fails reads as unknown rather than breaking the model list."""
    if model_status is None:
        return {"status": "unknown"}
    try:
        raw = model_status(model_id)
    except Exception:  # noqa: BLE001 - the picker must always load
        return {"status": "unknown"}
    status = {"status": str(raw.get("status", "unknown"))}
    if status["status"] == "not-yet" and raw.get("reason"):
        status["reason"] = str(raw["reason"])
    return status


def create_app(
    engine_factory: EngineFactory,
    token: str,
    approvals: Approvals | None = None,
    *,
    driver: GraphDriver | None = None,
    catalog: Catalog | None = None,
    model_status: Callable[[str], Mapping[str, str]] | None = None,
    usage: Callable[[], Mapping[str, Any]] | None = None,
    schedule: Schedule | None = None,
    now: Callable[[], datetime] | None = None,
    components: component_routes.ComponentsLike | None = None,
    questions: Questions | None = None,
    models: ModelsLike | None = None,
    budget: BudgetLike | None = None,
    recent_projects: RecentProjectsLike | None = None,
    connect: connect_routes.ConnectLike | None = None,
    attachments: AttachmentLoader | None = None,
    connections: connection_routes.ConnectionsLike | None = None,
    skills: skill_routes.SkillsLike | None = None,
) -> FastAPI:
    """Build and return the FastAPI application.

    Parameters
    ----------
    engine_factory:
        Builds one session's engine (`EngineFactory`): ``(project_root, emit, cancel) ->
        EngineLike``. ``POST /sessions`` calls it, once per session.
    token:
        Bearer token for authorising every route except ``/health``.
    approvals:
        Where the guard's questions wait for an answer. The same instance must reach the
        approver the factory builds, or a turn blocks on a question this app cannot
        answer. Made here when the caller supplies none.
    driver:
        Backs ``GET /projects`` and the history route. Not tied to a session: cli.py
        builds one for the app, apart from the driver each engine gets from
        ``_build_engine``. ``None`` makes ``/projects`` always answer degraded.
    catalog:
        Backs ``GET /models``. ``None`` is an empty catalog, same as an absent
        ``models.toml``.
    model_status:
        What the picker shows for one model id: ``{"status": ...}``. The app passes none,
        so every model is ``unknown``.
    usage:
        Backs ``GET /usage``: ``provider.usage.snapshot`` for the served model, bound by
        cli.py. Blocking (it may run ``nvidia-smi``), so the route runs it off the loop.
    schedule, now:
        When an automatic resume runs, and the clock its ``at`` is read from. A daemon
        ``threading.Timer`` and the local clock by default; tests pass fakes.
    components:
        Backs the component, system-program, decision and search-level routes in
        ``server/components.py``; ``None`` leaves them unregistered.
    questions:
        Where permission requests and budget pauses wait for the window's answer. As with
        ``approvals``, the same instance must reach the sessions the factory builds.
    models:
        The served model and its switch (`ModelsLike`): ``GET /models`` marks the active
        one and each entry's service, and ``POST /sessions/{id}/model`` switches it.
        ``None`` answers that route 503 and leaves ``GET /models`` as it was.
    budget:
        COGITO's own budget (`BudgetLike`): ``GET``/``PUT /settings/budget``. Its pauses
        wait in ``questions`` and are answered by ``POST /sessions/{id}/budget/{question}``.
        ``None`` answers the settings routes 503.
    recent_projects:
        The desktop app's project picker (design 1f, `RecentProjectsLike`):
        ``GET``/``DELETE /settings/projects``. ``POST /sessions`` touches it with the new
        session's root, so opening a project keeps it recent without the window asking.
        ``None`` answers ``GET`` with an empty list and ``DELETE`` 503.
    connect:
        The welcome and connection screen (design 1d-1e, `server/connect.py`'s
        `ConnectLike`, cli.py's `_Connect`). Its save switches to the new model through the
        same guarded switch as ``POST /sessions/{id}/model``. ``None`` leaves its routes
        unregistered.
    attachments:
        Reads what the person attached to a message (design 1n, `AttachmentLoader`):
        cli.py's `context.attachments.load`. ``None`` answers ``POST
        /sessions/{id}/attachments`` 503 and refuses a message that carries any.

    ``app.state.broadcast(event)`` puts one event on every open session's stream: what
    belongs to the whole core (the budget, the served model's per-minute headroom).
    """
    approvals = approvals if approvals is not None else Approvals()
    questions = questions if questions is not None else Questions()
    catalog = catalog if catalog is not None else Catalog(entries=())
    schedule = schedule if schedule is not None else _thread_timer
    clock = now if now is not None else (lambda: datetime.now().astimezone())
    # Per-app session store, not shared across app instances.
    sessions: dict[str, dict[str, Any]] = {}
    sessions_lock = threading.Lock()
    # A model switch in flight: app-wide, since every session talks to the one served model.
    # Read and written under `sessions_lock`, like each session's `running`.
    switching = {"on": False}

    app = FastAPI(title="Cogito loopback")

    def broadcast(event: dict[str, Any]) -> None:
        with sessions_lock:
            channels = [sess["channel"] for sess in sessions.values()]
        for channel in channels:
            channel.emit(event)

    app.state.broadcast = broadcast

    # -- resuming a turn a provider failure stopped -----------------------------------------
    #
    # The ``_locked`` functions and `_plan_resume` expect `sessions_lock` held by the
    # caller; `_fire_resume` and `cancel_all_resumes` take it themselves.

    def _cancel_resume_locked(sess: dict[str, Any], reason: str) -> bool:
        pending: _PendingResume | None = sess.get("resume")
        if pending is None:
            return False
        sess["resume"] = None
        pending.handle.cancel()
        sess["channel"].emit({"type": "resume_cancelled", "reason": reason})
        return True

    def _can_resume(engine: Any) -> bool:
        try:
            return bool(getattr(engine, "can_resume", False)) and callable(
                getattr(engine, "resume", None)
            )
        except Exception:
            log.exception("reading can_resume raised; treating it as nothing to resume")
            return False

    def _start_locked(
        session_id: str,
        sess: dict[str, Any],
        *,
        text: str,
        resume: bool,
        attached: Sequence[Any] = (),
        skills: Sequence[str] = (),
        thinking: str | None = None,
        wake: bool = False,
    ) -> None:
        # Reset the cancel token so a previous interrupt does not poison this turn.
        sess["cancel"].reset()
        sess["running"] = True
        kwargs: dict[str, Any] = {
            "resume": resume,
            "plan_resume": _plan_resume,
            "after_turn": _maybe_wake,
        }
        if wake:
            kwargs["wake"] = True
        if attached:
            kwargs["attachments"] = tuple(attached)
        if skills:
            kwargs["skills"] = tuple(skills)
        if thinking:
            kwargs["thinking"] = thinking
        threading.Thread(
            target=_run_turn,
            args=(
                session_id,
                text,
                sess["engine"],
                sess["channel"],
                sess["cancel"],
                sessions,
                sessions_lock,
            ),
            kwargs=kwargs,
            daemon=True,
        ).start()

    async def _read_attachments(
        body: Mapping[str, Any], project_root: Path
    ) -> tuple[list[Any], JSONResponse | None]:
        """Read a message's ``attachments`` now, off the event loop (up to
        `MAX_ATTACHMENTS` files of 20 MB). Answers ``([], None)`` for a message with none,
        else the attachments or the response refusing the message: 400 for anything that is
        not a list of ``{"path"}``/``{"text"}``, 422 ``{"error":"refused","reason","name"}``
        for the first one that cannot be attached (it changed since the window was told
        what it is), 503 when this app cannot read attachments."""
        raw = body.get("attachments")
        if raw is None or raw == []:
            return [], None
        if not isinstance(raw, list) or len(raw) > MAX_ATTACHMENTS:
            return [], JSONResponse(
                status_code=400,
                content={"error": f"attachments must be a list of at most {MAX_ATTACHMENTS}"},
            )
        if attachments is None:
            return [], JSONResponse(status_code=503, content={"error": "attachments unavailable"})

        def read_all() -> list[Any]:
            return [attachments(spec, project_root) for spec in raw]

        loaded = await asyncio.to_thread(read_all)
        for item in loaded:
            if item is None:
                return [], JSONResponse(
                    status_code=400,
                    content={"error": 'each attachment is {"path": ...} or {"text": ...}'},
                )
            refused = getattr(item, "reason", None)
            if refused is not None:
                return [], JSONResponse(status_code=422, content=item.as_dict())
        return loaded, None

    def _maybe_wake(session_id: str) -> None:
        """APP-ONLY. Start a turn for background commands that ended while none ran
        (`Engine.wake`), as Claude Code does ("re-invokes you when it exits"; its tools
        docs, read 2026-10-31). Called when the shell reports an end and when a turn ends,
        so a report that missed the turn's last boundary is not left waiting. Not after
        «Detener»: the person stopped the work, and the report waits for their next message.
        At most `MAX_WAKES` in a row without a message from the person."""
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None or sess["running"] or sess["rewinding"] or switching["on"]:
                return
            engine = sess["engine"]
            pending = getattr(engine, "pending_notice_count", None)
            if pending is None or not pending() or not hasattr(engine, "wake"):
                return
            if sess["cancel"].cancelled or sess.get("stopped") or sess.get("wakes", 0) >= MAX_WAKES:
                return
            # A turn waiting to resume after a provider failure carries on first, and reads
            # the report at its next step; a woken turn would end its chance to resume.
            if sess.get("resume") is not None or _can_resume(engine):
                return
            sess["wakes"] = sess.get("wakes", 0) + 1
            sess["channel"].emit({"type": "wake_started"})
            _start_locked(session_id, sess, text="", resume=False, wake=True)

    def _start_resume_locked(session_id: str, sess: dict[str, Any], *, auto: bool) -> None:
        sess["channel"].emit({"type": "resume_started", "auto": auto})
        _start_locked(session_id, sess, text="", resume=True)

    def _fire_resume(session_id: str, token: object) -> None:
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return
            pending: _PendingResume | None = sess.get("resume")
            if pending is None or pending.token is not token:
                return  # cancelled, or replaced by a later schedule, before it fired
            sess["resume"] = None
            if (
                sess["running"]
                or sess["rewinding"]
                or switching["on"]
                or not _can_resume(sess["engine"])
            ):
                sess["channel"].emit({"type": "resume_cancelled", "reason": "busy"})
                return
            sess["auto_resumes"] += 1
            _start_resume_locked(session_id, sess, auto=True)

    def _plan_resume(session_id: str, failure: Mapping[str, Any]) -> dict[str, Any] | None:
        """Called from `_finalize_turn` with the lock held. Schedules one automatic resume
        when the provider said when (``retry_after``) and this turn has not used up
        `MAX_AUTO_RESUMES`; answers what ``service_error.resume`` says about it."""
        sess = sessions.get(session_id)
        if sess is None:
            return None
        # "■ Detener" arrived between the failure and this plan: the person stopped it.
        if sess["cancel"].cancelled:
            return None
        # A spend cap or a console spend limit is not a rate limit: waiting does not end
        # it, so nothing resumes behind one, whatever `retry-after` said. Nor behind a
        # request too large for the window, which would be refused the same way again.
        if failure.get("category") in DETERMINISTIC_CATEGORIES:
            return None
        delay = failure.get("retry_after")
        if isinstance(delay, bool) or not isinstance(delay, int | float):
            return None
        if not math.isfinite(delay) or delay < 0 or delay > MAX_AUTO_RESUME_DELAY:
            return None
        if sess["auto_resumes"] >= MAX_AUTO_RESUMES or not _can_resume(sess["engine"]):
            return None
        # Everything the answer says is computed before anything is scheduled, so a plan
        # that could not be described is never left running.
        plan = {
            "at": (clock() + timedelta(seconds=float(delay))).isoformat(timespec="seconds"),
            "in_seconds": float(delay),
            "attempt": sess["auto_resumes"] + 1,
            "max": MAX_AUTO_RESUMES,
        }
        token = object()
        # One pending resume at a time; a later failure's retry-after replaces an earlier.
        stale: _PendingResume | None = sess.get("resume")
        if stale is not None:
            stale.handle.cancel()
        sess["resume"] = _PendingResume(
            token, schedule(float(delay), lambda: _fire_resume(session_id, token))
        )
        return plan

    def cancel_all_resumes(reason: str = "quit") -> None:
        """Every pending automatic resume, for the server stopping."""
        with sessions_lock:
            for sess in sessions.values():
                _cancel_resume_locked(sess, reason)

    app.state.cancel_all_resumes = cancel_all_resumes

    def close_all_sessions(reason: str = "quit") -> None:
        """Close every open session, for the server stopping: a running turn is cancelled,
        and each engine closes its shell (with whatever it left running in the background),
        its guard broker and its Estella. Before, only `DELETE /sessions` closed one, which
        the desktop never sends, so an agent's processes outlived the app."""
        with sessions_lock:
            closing = list(sessions.values())
            sessions.clear()
            for sess in closing:
                _cancel_resume_locked(sess, reason)
        approvals.abandon_all()
        questions.abandon_all()
        for sess in closing:
            sess["cancel"].cancel(reason)
        for sess in closing:
            try:
                sess["engine"].close()
            except Exception:
                log.exception("closing a session on %s failed", reason)
            sess["channel"].close()

    app.state.close_all_sessions = close_all_sessions

    if components is not None:
        component_routes.register(
            app,
            authorized=lambda request: _auth_ok(request, token),
            components=components,
            questions=questions,
            sessions=sessions,
            sessions_lock=sessions_lock,
        )

    # -- health ---------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # The desktop's way to stop this core where it cannot send SIGTERM (Windows, whose
    # only outside kill is TerminateProcess, which runs no cleanup). `serve` sets
    # `app.state.request_shutdown`; the same clean exit as a signal follows.
    @app.post("/shutdown")
    async def shutdown(request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        stop = getattr(app.state, "request_shutdown", None)
        if stop is None:
            return JSONResponse(status_code=503, content={"error": "not served"})
        stop()
        return JSONResponse(status_code=202, content={"status": "stopping"})

    # -- models -----------------------------------------------------------------

    @app.get("/models")
    async def get_models(request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")

        def build() -> dict[str, Any]:
            active = models.active_id() if models is not None else None
            groups = []
            for origin, entries in catalog.grouped().items():
                rows = []
                for entry in entries:
                    facts = dict(models.facts(entry.id) or {}) if models is not None else {}
                    # A saved server that is not up is left out; the active model never is.
                    if facts.pop("offline", False) and entry.id != active:
                        continue
                    rows.append(
                        {
                            "id": entry.id,
                            "label": entry.display_label,
                            "origin": entry.origin,
                            # The composer's thinking control acts on a host that takes a
                            # thinking budget (`ModelEntry.takes_server_extensions`) and on
                            # Claude, whose effort it sets (`AnthropicProvider._effort`).
                            "thinking": entry.takes_server_extensions
                            or entry.api == "anthropic",
                            **_status_of(model_status, entry.id),
                            **facts,
                        }
                    )
                if rows:
                    groups.append({"origin": origin, "models": rows})
            return {"groups": groups, "active": active}

        # A local entry's service may be asked its name (`provider.service`, a short timeout).
        return JSONResponse(status_code=200, content=await asyncio.to_thread(build))

    @app.post("/sessions/{session_id}/model")
    async def post_model(session_id: str, request: Request) -> JSONResponse:
        """Switch the served model (the window's picker). See ``POST /sessions/{id}/model``
        in the module docstring."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if models is None:
            return JSONResponse(status_code=503, content={"error": "model switching unavailable"})
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        model_id = body.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
            return JSONResponse(status_code=400, content={"error": "model_id must be a model id"})
        return await _switch(model_id.strip(), session_id=session_id)

    async def _switch(model_id: str, *, session_id: str | None = None) -> JSONResponse:
        """The switch itself, guarded the same way for the picker (a session's route) and the
        connection screen (`server/connect.py`, which may run before any session exists)."""
        # An id the catalog lacks, or an entry that cannot be used, is refused before
        # anything is cancelled or claimed.
        facts = await asyncio.to_thread(models.facts, model_id)
        if facts is None:
            return JSONResponse(status_code=404, content={"error": "unknown model"})
        if facts.get("missing"):
            return JSONResponse(
                status_code=422, content={"error": "unavailable", "missing": facts["missing"]}
            )
        claimed = False
        with sessions_lock:
            if session_id is not None and session_id not in sessions:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            if switching["on"]:
                return JSONResponse(status_code=409, content={"error": "switching model"})
            # One served model for every session: a turn anywhere is a turn on it.
            if any(sess["running"] for sess in sessions.values()):
                return JSONResponse(status_code=409, content={"error": "turn already running"})
            if any(sess["rewinding"] for sess in sessions.values()):
                return JSONResponse(status_code=409, content={"error": "rewinding"})
            if model_id != models.active_id():
                switching["on"] = claimed = True
                # Nothing resumes a turn behind a switch the person chose; "Reintentar
                # ahora" still can, on the new model.
                for sess in sessions.values():
                    _cancel_resume_locked(sess, "model")
        if not claimed:
            # The model already served: nothing to stop, nothing to cancel.
            return _model_answer(await asyncio.to_thread(models.switch, model_id))
        try:
            answer = await asyncio.to_thread(models.switch, model_id)
        finally:
            with sessions_lock:
                switching["on"] = False
        if answer.get("changed"):
            event = {"type": "model_changed", "model": answer.get("model")}
            with sessions_lock:
                followers = [(sess["engine"], sess["channel"]) for sess in sessions.values()]
            for engine, channel in followers:
                channel.emit(event)
                # The header's context chip against the new model's window, at once: most
                # needed when the conversation no longer fits it.
                _emit_context_status(engine, channel)
        return _model_answer(answer)

    app.state.switch_model = _switch

    if connect is not None:
        connect_routes.register(
            app,
            authorized=lambda request: _auth_ok(request, token),
            connect=connect,
            switch=_switch if models is not None else None,
        )

    if connections is not None:
        def _connections_project_root(session_id: str) -> Path | None:
            with sessions_lock:
                sess = sessions.get(session_id)
                return sess["project_root"] if sess is not None else None

        connection_routes.register(
            app, authorized=lambda request: _auth_ok(request, token), connections=connections,
            project_root_for=_connections_project_root,
        )

    if skills is not None:
        def _skills_project_root(session_id: str) -> Path | None:
            with sessions_lock:
                sess = sessions.get(session_id)
                return sess["project_root"] if sess is not None else None

        skill_routes.register(
            app,
            authorized=lambda request: _auth_ok(request, token),
            skills=skills,
            project_root_for=_skills_project_root,
        )

    def _terminals_for(session_id: str) -> tuple[bool, Any]:
        with sessions_lock:
            sess = sessions.get(session_id)
        if sess is None:
            return False, None
        return True, getattr(sess["engine"], "terminals", None)

    # Always registered: a session without terminals (`chat` never serves this app; Windows,
    # or a core the desktop did not start) answers `{"available": false}`.
    terminal_routes.register(
        app, authorized=lambda request: _auth_ok(request, token), host_for=_terminals_for
    )

    def _browser_for(session_id: str) -> tuple[bool, Any]:
        with sessions_lock:
            sess = sessions.get(session_id)
        if sess is None:
            return False, None
        return True, getattr(sess["engine"], "browser_bridge", None)

    # Always registered: a session without a browser answers 404 to every answer.
    browser_routes.register(
        app, authorized=lambda request: _auth_ok(request, token), bridge_for=_browser_for
    )

    def _model_answer(answer: Mapping[str, Any]) -> JSONResponse:
        error = answer.get("error")
        status = 200
        if error == "unknown model":
            status = 404
        elif error is not None:
            status = 422
        return JSONResponse(status_code=status, content=dict(answer))

    # -- usage --------------------------------------------------------------------

    empty_usage = {"date": None, "active": None, "today": {}}

    @app.get("/usage")
    async def get_usage(request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if usage is None:
            return JSONResponse(status_code=200, content=empty_usage)
        try:
            snapshot = await asyncio.to_thread(usage)
        except Exception:
            log.exception("GET /usage: the usage source raised")
            return JSONResponse(
                status_code=200, content=empty_usage, headers={"X-Cogito-Degraded": "usage"}
            )
        return JSONResponse(status_code=200, content=dict(snapshot))

    # -- COGITO's own budget ----------------------------------------------------------

    @app.get("/settings/budget")
    async def get_budget(request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if budget is None:
            return JSONResponse(status_code=503, content={"error": "budget unavailable"})
        return JSONResponse(status_code=200, content=dict(await asyncio.to_thread(budget.view)))

    @app.put("/settings/budget")
    async def put_budget(request: Request) -> JSONResponse:
        """Save the budget: ``monthly``, ``daily`` (a number, or null for none) and
        ``currency``, each optional. Every session hears ``budget_status``."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if budget is None:
            return JSONResponse(status_code=503, content={"error": "budget unavailable"})
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        answer = dict(await asyncio.to_thread(budget.update, body))
        if "error" in answer:
            return JSONResponse(status_code=400, content={"error": str(answer["error"])})
        return JSONResponse(status_code=200, content=answer)

    @app.post("/sessions/{session_id}/budget/{question_id}")
    async def post_budget_decision(
        session_id: str, question_id: str, request: Request
    ) -> JSONResponse:
        """Answer a ``budget_reached`` pause: ``continue`` (this turn), ``raise`` (sent once
        the window saved a higher budget; the budget is checked again) or ``stop``. From
        the window's card or a notification, the same way."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        try:
            body = await request.json()
        except Exception:
            body = None
        decision = body.get("decision") if isinstance(body, dict) else None
        if decision not in BUDGET_DECISIONS:
            return JSONResponse(
                status_code=400,
                content={"error": f"decision must be one of {', '.join(BUDGET_DECISIONS)}"},
            )
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            # Only the session that paused may answer: another session's id is "not waiting".
            asked = question_id in sess.get("budget_ids", ())
        if not asked or not questions.answer(question_id, decision):
            return JSONResponse(
                status_code=404, content={"error": "no question is waiting under that id"}
            )
        return JSONResponse(status_code=200, content={"status": "answered"})

    # -- the desktop app's project picker (design 1f) --------------------------------

    @app.get("/settings/projects")
    async def get_recent_projects(request: Request) -> JSONResponse:
        """Recently opened projects, newest first, from the config dir (see
        `RecentProjectsLike`), so it answers before Neo4j is reachable. Without a source it
        is an empty list, not an error: a fresh install has nothing to show either."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if recent_projects is None:
            return JSONResponse(status_code=200, content={"projects": []})
        return JSONResponse(
            status_code=200, content={"projects": await asyncio.to_thread(recent_projects.list)}
        )

    @app.delete("/settings/projects")
    async def forget_recent_project(request: Request) -> JSONResponse:
        """ "Quitar de la lista" for a project whose folder no longer exists. ``{"path"}``
        only; the project's own history (graph and journal) is never touched."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if recent_projects is None:
            return JSONResponse(status_code=503, content={"error": "recent projects unavailable"})
        try:
            body = await request.json()
        except Exception:
            body = None
        target = body.get("path") if isinstance(body, dict) else None
        if not isinstance(target, str) or not target.strip():
            return JSONResponse(status_code=400, content={"error": "path required"})
        await asyncio.to_thread(recent_projects.forget, target)
        return JSONResponse(
            status_code=200, content={"projects": await asyncio.to_thread(recent_projects.list)}
        )

    # -- projects -----------------------------------------------------------------

    @app.get("/projects")
    async def get_projects(request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        if driver is None:
            return JSONResponse(
                status_code=200,
                content={"projects": []},
                headers={"X-Cogito-Degraded": "graph"},
            )
        try:
            projects = await asyncio.to_thread(list_projects, driver)
        except GraphUnavailable:
            return JSONResponse(
                status_code=200,
                content={"projects": []},
                headers={"X-Cogito-Degraded": "graph"},
            )
        return JSONResponse(status_code=200, content={"projects": projects})

    # -- sessions -------------------------------------------------------------

    @app.post("/sessions")
    async def create_session(request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        body = await request.json()
        project_root = body.get("project_root", "")
        if not project_root:
            return JSONResponse(status_code=400, content={"error": "project_root required"})
        p = Path(project_root).resolve()
        if not p.is_dir():
            return JSONResponse(
                status_code=400,
                content={"error": f"not a directory: {project_root}", "code": "not_a_directory"},
            )
        sid = new_id()
        cancel = CancelToken()
        channel = _SessionChannel()
        try:
            engine = await asyncio.to_thread(engine_factory, p, channel.emit, cancel)
        except NoModel:
            # A first launch before a model is connected (design 1d-1e): nothing to talk to.
            return JSONResponse(status_code=409, content={"error": "no model"})
        except Exception as exc:
            # A folder the session cannot write its `.cogito/` into (/usr, a read-only
            # mount), or anything else that stops the engine. Answered as JSON with a code
            # the app words, never a bare text 500.
            if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                where = exc.filename or str(p)
                return JSONResponse(
                    status_code=403,
                    content={"error": f"cannot write {where}: permission denied", "code": "not_writable"},
                )
            log.exception("POST /sessions: opening %s failed", p)
            return JSONResponse(status_code=500, content={"error": str(exc), "code": "open_failed"})
        if recent_projects is not None:
            # Opening a project keeps it recent (design 1f), only once it has opened: a
            # folder that failed must not become the one every later launch opens. Best
            # effort: a config dir that cannot be written must never cost the session.
            try:
                await asyncio.to_thread(recent_projects.touch, str(p))
            except OSError:
                log.exception("POST /sessions: recording %s as recently opened failed", p)
        channel.on_background = lambda sid=sid: _maybe_wake(sid)
        with sessions_lock:
            sessions[sid] = {
                "engine": engine,
                "channel": channel,
                "cancel": cancel,
                # Whose thread `GET /history` reads back.
                "project_root": p,
                # Only this session may answer the permission questions it asked, and the
                # budget pauses its turns waited on.
                "permission_ids": channel.permission_ids,
                "budget_ids": channel.budget_ids,
                "running": False,
                # A rewind/redo/dry-run in flight. Kept apart from ``running``, which means
                # only "a turn is executing": with one shared flag, a steer sent during a
                # rewind would queue and be applied to an unrelated later turn.
                "rewinding": False,
                # The automatic resume scheduled after a provider failure, if any, and how
                # many this turn has already had (`MAX_AUTO_RESUMES`).
                "resume": None,
                "auto_resumes": 0,
            }
        return JSONResponse(status_code=201, content={"id": sid})

    # -- history ------------------------------------------------------------

    @app.get("/sessions/{session_id}/history")
    async def get_history(session_id: str, request: Request) -> JSONResponse:
        """The project's thread, one page at a time (see ``server/history.py``), plus this
        session's ``context_status`` (design 1a's header chip). The thread reads only the
        store and `context_status` is read off this session's engine, so nothing here sends
        the model anything or changes a later request."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        with sessions_lock:
            sess = sessions.get(session_id)
        if sess is None:
            return JSONResponse(status_code=404, content={"error": "session not found"})
        engine = sess.get("engine")
        context_status = getattr(engine, "context_status", None) if engine is not None else None
        limit = thread_history.DEFAULT_LIMIT
        raw_limit = request.query_params.get("limit")
        if raw_limit:
            try:
                limit = int(raw_limit)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "limit must be an integer"})
        before = request.query_params.get("before") or None
        if before is not None and thread_history.CURSOR.match(before) is None:
            return JSONResponse(
                status_code=400, content={"error": "before must be <session id>:<seq>"}
            )
        project_root: Path = sess["project_root"]

        def build() -> dict[str, Any]:
            try:
                page = thread_history.read_page(
                    open_source(driver, project_root), before=before, limit=limit
                )
            except GraphUnavailable as exc:
                log.warning("history: the graph went away mid-page; the journal: %s", exc)
                page = thread_history.read_page(
                    wal_source(project_root), before=before, limit=limit
                )
            items = thread_history.thread_items(page.entries, project_root, page.markers)
            return {
                "items": items,
                "before": page.before,
                "source": page.source,
                "context_status": dict(context_status) if context_status is not None else None,
            }

        return JSONResponse(status_code=200, content=await asyncio.to_thread(build))

    @app.get("/sessions/{session_id}/artifacts/{owner}/{artifact_id}")
    async def get_artifact(
        session_id: str, owner: str, artifact_id: str, request: Request
    ) -> JSONResponse:
        """The whole of what a tool row previews (``server/artifacts.py``): read from the
        store's own files only; nothing here touches the engine or a model."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        with sessions_lock:
            sess = sessions.get(session_id)
        if sess is None:
            return JSONResponse(status_code=404, content={"error": "session not found"})
        if not tool_artifacts.valid(owner, artifact_id):
            return JSONResponse(status_code=400, content={"error": "not an artifact reference"})
        project_root: Path = sess["project_root"]
        found = await asyncio.to_thread(tool_artifacts.read, project_root, owner, artifact_id)
        if found is None:
            return JSONResponse(status_code=404, content={"error": "artifact not found"})
        return JSONResponse(status_code=200, content=found)

    # -- turns ----------------------------------------------------------------

    @app.post("/sessions/{session_id}/turns")
    async def post_turn(session_id: str, request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        body = await request.json()
        # A JSON body that is not an object (a bare list, string or number) has no
        # `.get`: answer 400, not a 500.
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        text = body.get("text")
        # Same rule as `post_steer`: a non-string `text` is refused here instead of
        # failing deep inside `run()`, and a whitespace-only one is no instruction at all.
        if not isinstance(text, str):
            return JSONResponse(status_code=400, content={"error": "text must be a string"})
        text = text.strip()
        # Attachments (design 1n), read before the session is locked: a message with one
        # may have no words of its own.
        attached: list[Any] = []
        if "attachments" in body:
            with sessions_lock:
                sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            attached, refusal = await _read_attachments(body, sess["project_root"])
            if refusal is not None:
                return refusal
        # Skills the person activated in the composer (app-only); force-loaded for this turn
        # regardless of BM25. Unknown or unreadable names are passed over by the engine.
        activated: list[str] = []
        if "skills" in body:
            raw = body.get("skills")
            if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
                return JSONResponse(
                    status_code=400, content={"error": "skills must be a list of strings"}
                )
            activated = [name for name in raw if name]
        # How much the model may think this turn, from the composer (app-only). Absent is the
        # session's own budget; see the module docstring.
        thinking = body.get("thinking")
        if thinking is not None and (
            not isinstance(thinking, str) or thinking not in THINKING_LEVELS
        ):
            return JSONResponse(
                status_code=400,
                content={"error": f"thinking must be one of {sorted(THINKING_LEVELS)}"},
            )
        if not text and not attached:
            return JSONResponse(status_code=400, content={"error": "text required"})
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            if sess["running"]:
                return JSONResponse(status_code=409, content={"error": "turn already running"})
            if sess["rewinding"]:
                return JSONResponse(status_code=409, content={"error": "rewinding"})
            if switching["on"]:
                return JSONResponse(status_code=409, content={"error": "switching model"})
            # A new instruction replaces the failed turn, and its automatic resumes with it.
            _cancel_resume_locked(sess, "message")
            sess["auto_resumes"] = 0
            sess["wakes"] = 0
            sess["stopped"] = False
            _start_locked(
                session_id,
                sess,
                text=text,
                resume=False,
                attached=attached,
                skills=activated,
                thinking=thinking,
            )
        return JSONResponse(status_code=202, content={"status": "accepted"})

    @app.post("/sessions/{session_id}/auto")
    async def post_auto(session_id: str, request: Request) -> JSONResponse:
        """Turn the composer's automatic mode on or off (app-only; see the module
        docstring)."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        try:
            body = await request.json()
        except Exception:
            body = None
        on = body.get("on") if isinstance(body, dict) else None
        if not isinstance(on, bool):
            return JSONResponse(status_code=400, content={"error": "on must be true or false"})
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            mode = getattr(sess["engine"], "auto", None)
            if mode is None:
                return JSONResponse(
                    status_code=409, content={"error": "this session has no automatic mode"}
                )
            mode.on = on
        return JSONResponse(content={"on": on})

    # -- attachments (design 1n) ----------------------------------------------------

    @app.post("/sessions/{session_id}/attachments")
    async def post_attachment(session_id: str, request: Request) -> JSONResponse:
        """Describe a file the person chose, or a long paste, as an attachment. Read now,
        never written, never sent anywhere. See the module docstring."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        with sessions_lock:
            sess = sessions.get(session_id)
        if sess is None:
            return JSONResponse(status_code=404, content={"error": "session not found"})
        if attachments is None:
            return JSONResponse(status_code=503, content={"error": "attachments unavailable"})
        item = await asyncio.to_thread(attachments, body, sess["project_root"])
        if item is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": 'an attachment is {"path": ...}, {"text": ...} '
                    'or {"image": base64, "name": ...}'
                },
            )
        if getattr(item, "reason", None) is not None:
            return JSONResponse(status_code=422, content=item.as_dict())
        facts = item.facts()
        # An image: whether the model in use will see it, for the chip to say before sending.
        if facts.get("kind") == "image":
            asks = getattr(sess["engine"], "sees_images", None)
            seen = await asyncio.to_thread(asks) if callable(asks) else False
            facts["seen"] = bool(seen)
        return JSONResponse(status_code=200, content={"attachment": facts})

    # -- resume ("Reintentar ahora") --------------------------------------------------

    @app.post("/sessions/{session_id}/resume")
    async def post_resume(session_id: str, request: Request) -> JSONResponse:
        """Carry on the turn a provider failure stopped, now (see "Resuming" in the module
        docstring). Cancels a pending automatic resume, which this replaces."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            if sess["running"]:
                return JSONResponse(status_code=409, content={"error": "turn already running"})
            if sess["rewinding"]:
                return JSONResponse(status_code=409, content={"error": "rewinding"})
            if switching["on"]:
                return JSONResponse(status_code=409, content={"error": "switching model"})
            if not _can_resume(sess["engine"]):
                return JSONResponse(status_code=409, content={"error": "nothing to resume"})
            _cancel_resume_locked(sess, "retry")
            _start_resume_locked(session_id, sess, auto=False)
        return JSONResponse(status_code=202, content={"status": "resuming"})

    @app.delete("/sessions/{session_id}/resume")
    async def delete_resume(session_id: str, request: Request) -> JSONResponse:
        """Cancel a pending automatic resume (for a model switch, say). ``reason``, a short
        word, is passed on in ``resume_cancelled``."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        try:
            body = await request.json()
        except Exception:
            body = {}
        reason = body.get("reason") if isinstance(body, dict) else None
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 40:
            reason = "cancelled"
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            cancelled = _cancel_resume_locked(sess, reason.strip())
        return JSONResponse(status_code=200, content={"cancelled": cancelled})

    # -- steer ------------------------------------------------------------------

    @app.post("/sessions/{session_id}/steer")
    async def post_steer(session_id: str, request: Request) -> JSONResponse:
        """Send a message into the running turn without stopping it (see "Steering" in the
        module docstring). Allowed only while a turn runs; with none it answers 409 and
        never starts one.

        Checks, in order: a JSON object (400; a bare list or scalar has no ``.get``),
        ``text`` a string (400; ``str()`` of a number or list would accept what the client
        never meant), non-empty after stripping unless it carries attachments (400), at most
        ``_MAX_STEER_TEXT_CHARS`` (400, not 413, like every other size check in this API).
        Only then the session (404), its attachments, a rewind in flight (409), whether a
        turn is running (409), and the queue cap (429, ``_MAX_PENDING_STEERS``, so an
        unwatched session cannot build an unbounded backlog).
        """
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        text = body.get("text")
        if not isinstance(text, str):
            return JSONResponse(status_code=400, content={"error": "text must be a string"})
        text = text.strip()
        if not text and not body.get("attachments"):
            return JSONResponse(status_code=400, content={"error": "text required"})
        if len(text) > _MAX_STEER_TEXT_CHARS:
            return JSONResponse(
                status_code=400,
                content={"error": f"text too long (max {_MAX_STEER_TEXT_CHARS} characters)"},
            )
        # Attachments (design 1n), read before the session is locked; the cap above is on
        # the person's own words, an attachment's own is `context.attachments.MAX_BYTES`.
        attached: list[Any] = []
        if "attachments" in body:
            with sessions_lock:
                found = sessions.get(session_id)
            if found is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            attached, refusal = await _read_attachments(body, found["project_root"])
            if refusal is not None:
                return refusal
            if not text and not attached:
                return JSONResponse(status_code=400, content={"error": "text required"})
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            if sess["rewinding"]:
                # Checked first and reported as its own reason. "no turn running" would
                # also be true, but the desktop answers that 409 by starting a plain turn,
                # which `/turns` refuses while `rewinding` is set.
                return JSONResponse(status_code=409, content={"error": "rewinding"})
            if not sess["running"]:
                # Whatever the person meant to add, it is theirs to send now: nothing
                # resumes behind it (the desktop sends it as a new turn next).
                _cancel_resume_locked(sess, "steer")
                return JSONResponse(status_code=409, content={"error": "no turn running"})
            pending_count = getattr(sess["engine"], "pending_steer_count", None)
            if pending_count is not None and pending_count() >= _MAX_PENDING_STEERS:
                return JSONResponse(
                    status_code=429,
                    content={"error": f"too many steer messages queued (max {_MAX_PENDING_STEERS})"},
                )
            # The id is minted here, not in `Engine.steer`, and `steer_queued` goes out
            # before the push below, so a client never sees `steer_applied`/`steer_dropped`
            # for an id it was not told about first. `text` is clipped again to the limit
            # checked above, so this event stays bounded even if the two checks drift.
            steer_id = new_id()
            sess["channel"].emit(
                {"type": "steer_queued", "id": steer_id, "text": text[:_MAX_STEER_TEXT_CHARS]}
            )
            if attached:
                sess["engine"].steer(steer_id, text, attached)
            else:
                sess["engine"].steer(steer_id, text)
        return JSONResponse(status_code=202, content={"id": steer_id})

    # -- rewind ("Volver a un punto") ------------------------------------------

    def _claim_busy(session_id: str) -> tuple[JSONResponse | None, Any, Any]:
        """Shared by the rewind routes below. It sets ``rewinding`` under the lock, not
        only checks ``running``; otherwise ``POST /turns`` could start a turn between the
        check and the route's own work off the lock (`asyncio.to_thread`).

        ``rewinding`` is its own flag, not ``running``: ``POST /steer`` accepts a steer
        while ``running`` is set, so a shared flag would let a steer sent during a rewind
        queue into the engine and be applied to an unrelated later turn. ``/turns`` and
        ``/steer`` check ``rewinding`` explicitly.

        Returns ``(response, engine, channel)``. A non-``None`` response (404/409) must be
        returned at once; otherwise the caller holds ``rewinding`` and must release it with
        ``_release_busy`` in a ``finally``.
        """
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                not_found = JSONResponse(status_code=404, content={"error": "session not found"})
                return not_found, None, None
            if sess["running"]:
                busy = JSONResponse(status_code=409, content={"error": "turn already running"})
                return busy, None, None
            if sess["rewinding"]:
                busy = JSONResponse(status_code=409, content={"error": "rewinding"})
                return busy, None, None
            if switching["on"]:
                busy = JSONResponse(status_code=409, content={"error": "switching model"})
                return busy, None, None
            sess["rewinding"] = True
            return None, sess["engine"], sess["channel"]

    def _cancel_resume_for_rewind(session_id: str) -> None:
        """A rewind or redo changes the files the stopped turn was working on; resuming it
        behind the person's back afterwards would be acting on a state it never saw."""
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is not None:
                _cancel_resume_locked(sess, "rewind")

    def _release_busy(session_id: str) -> None:
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is not None:
                sess["rewinding"] = False

    def _strict_bool(body: dict, key: str) -> tuple[bool, JSONResponse | None]:
        """A JSON boolean, or False when absent. Never coerced from a truthy string,
        number or list, as ``bool(...)`` would."""
        if key not in body:
            return False, None
        value = body[key]
        if not isinstance(value, bool):
            return False, JSONResponse(
                status_code=400, content={"error": f"{key} must be a boolean"}
            )
        return value, None

    def _graph_down() -> JSONResponse:
        """The context store is not reachable: a rewind finds its files there, so none ran.
        A 503 the window turns into «El almacén de contexto no está disponible»."""
        return JSONResponse(status_code=503, content={"error": "graph unavailable"})

    @app.get("/sessions/{session_id}/rewind/{point_id}")
    async def get_rewind(session_id: str, point_id: str, request: Request) -> JSONResponse:
        """Dry run: what rewinding to ``point_id`` would do, without doing it. The confirm
        dialog's "N archivos" count and the conflict dialog's diffs come from here. Refused
        while a turn, another rewind or redo, or a model switch runs: a dry run against
        files being written would be stale before anyone read it.
        """
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        response, engine, _channel = _claim_busy(session_id)
        if response is not None:
            return response
        try:
            result = await asyncio.to_thread(engine.rewind_dry_run, point_id)
        except GraphUnavailable:
            return _graph_down()
        finally:
            _release_busy(session_id)
        if result.get("error") == "graph_unavailable":
            return _graph_down()
        if result.get("error") == "not_found":
            return JSONResponse(status_code=404, content={"error": "rewind point not found"})
        if result.get("error") == "failed":
            return JSONResponse(status_code=500, content={"error": "rewind dry run failed"})
        return JSONResponse(status_code=200, content=result)

    @app.post("/sessions/{session_id}/rewind/{point_id}")
    async def post_rewind(session_id: str, point_id: str, request: Request) -> JSONResponse:
        """Perform the rewind. ``keep_conflicts``/``force`` (optional, default false)
        answer a conflict dialog the client showed from a dry run or from this route's own
        409. Both have the same shape, so a client can skip the ``GET``. Emits ``rewound``
        on success and nothing on a 404 or 409. A partial result (some paths in
        ``failed``) is still a 200: what succeeded did happen, and is reported.
        """
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "a JSON object is required"})
        keep_conflicts, error = _strict_bool(body, "keep_conflicts")
        if error is not None:
            return error
        force, error = _strict_bool(body, "force")
        if error is not None:
            return error

        response, engine, channel = _claim_busy(session_id)
        if response is not None:
            return response
        _cancel_resume_for_rewind(session_id)
        try:
            result = await asyncio.to_thread(
                engine.rewind_perform, point_id, keep_conflicts=keep_conflicts, force=force
            )
        except GraphUnavailable:
            return _graph_down()
        finally:
            _release_busy(session_id)
        error_kind = result.get("error")
        if error_kind == "graph_unavailable":
            return _graph_down()
        if error_kind == "not_found":
            return JSONResponse(status_code=404, content={"error": "rewind point not found"})
        if error_kind == "conflict":
            return JSONResponse(status_code=409, content=result)
        if error_kind == "failed":
            return JSONResponse(status_code=500, content={"error": "rewind failed"})
        rewound: dict[str, Any] = {
            "type": "rewound",
            "point_id": point_id,
            "redo_id": result["redo_id"],
            "files": result["files"],
        }
        for key in ("conversation", "point_text", "redo"):
            if key in result:
                rewound[key] = result[key]
        channel.emit(rewound)
        _emit_context_status(engine, channel)
        return JSONResponse(status_code=200, content=result)

    @app.post("/sessions/{session_id}/redo/{redo_id}")
    async def post_redo(session_id: str, redo_id: str, request: Request) -> JSONResponse:
        """"Rehacer": reverse exactly what one past ``POST /rewind`` did. Single-use: a
        second call for the same ``redo_id`` answers 409, never repeats it."""
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        response, engine, channel = _claim_busy(session_id)
        if response is not None:
            return response
        _cancel_resume_for_rewind(session_id)
        try:
            result = await asyncio.to_thread(engine.rewind_redo, redo_id)
        except GraphUnavailable:
            return _graph_down()
        finally:
            _release_busy(session_id)
        error_kind = result.get("error")
        if error_kind == "graph_unavailable":
            return _graph_down()
        if error_kind == "not_found":
            return JSONResponse(status_code=404, content={"error": "redo not found"})
        if error_kind == "already_redone":
            return JSONResponse(status_code=409, content={"error": "already redone"})
        if error_kind == "moved_on":
            return JSONResponse(status_code=409, content={"error": "conversation moved on"})
        if error_kind == "failed":
            return JSONResponse(status_code=500, content={"error": "redo failed"})
        _emit_context_status(engine, channel)
        return JSONResponse(status_code=200, content=result)

    # -- events (SSE) ---------------------------------------------------------

    @app.get("/sessions/{session_id}/events")
    async def stream_events(session_id: str, request: Request) -> StreamingResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                raise HTTPException(status_code=404, detail="session not found")
            channel = sess["channel"]

        async def event_generator():
            while True:
                event = await asyncio.to_thread(channel.drain, 1.0)
                if event is _SENTINEL:
                    # Stream closed: send `close` and stop.
                    yield "event: close\ndata: {}\n\n"
                    break
                if event is None:
                    # Timeout: send a keepalive.
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- interrupt ------------------------------------------------------------

    @app.post("/sessions/{session_id}/interrupt")
    async def interrupt_turn(session_id: str, request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            # "■ Detener" also stops a resume that has not started yet.
            _cancel_resume_locked(sess, "interrupt")
            sess["cancel"].cancel("user interrupt")
        return JSONResponse(status_code=200, content={"status": "interrupted"})

    # -- reset context --------------------------------------------------------

    @app.post("/sessions/{session_id}/reset")
    async def reset_context(session_id: str, request: Request) -> JSONResponse:
        """Start a fresh conversation in the same project (design 1a's "reset context").

        The engine begins a brand-new root session with an empty window — no handover seed,
        no instructions, no literals (`Engine.start_fresh`). The window's session id (this
        route's ``session_id``) does not change; only the engine's internal conversation
        does, so the client keeps addressing this same session. The old conversation stays
        whole in the journal and is still readable through ``GET /history``.

        Refused with 409 while a turn or a rewind is running here, or a model switch is in
        flight, the same guard the model picker uses: a reset mid-turn would wipe the
        transcript out from under the running loop. A pending resume on the old conversation
        is cancelled, as a switch cancels it. The window clears its thread and the context
        chip on the ``thread_reset`` event and the null ``context_status`` that follow.
        """
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        with sessions_lock:
            sess = sessions.get(session_id)
            if sess is None:
                return JSONResponse(status_code=404, content={"error": "session not found"})
            if switching["on"]:
                return JSONResponse(status_code=409, content={"error": "switching model"})
            if sess["running"]:
                return JSONResponse(status_code=409, content={"error": "turn already running"})
            if sess["rewinding"]:
                return JSONResponse(status_code=409, content={"error": "rewinding"})
            # Nothing of the old conversation carries over, a scheduled resume least of all.
            _cancel_resume_locked(sess, "reset")
            engine = sess["engine"]
            channel = sess["channel"]
        await asyncio.to_thread(engine.start_fresh)
        channel.emit({"type": "thread_reset"})
        # The chip against an empty window: nothing measured yet, so nulls.
        _emit_context_status(engine, channel)
        return JSONResponse(status_code=200, content={"id": session_id})

    # -- approvals ------------------------------------------------------------

    @app.post("/sessions/{session_id}/approvals/{approval_id}")
    async def answer_approval(
        session_id: str, approval_id: str, request: Request
    ) -> JSONResponse:
        """Answer a question the guard is holding a turn open for.

        The turn is blocked in `guard`'s approver on another thread; this only passes the
        decision across. A stale id (already answered, timed out, or from a session that
        has gone away) is a 404, not a silent success: the window must tell "I allowed
        that" from "I was too late and nothing ran".
        """
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        body = await request.json()
        if not isinstance(body.get("allowed"), bool):
            return JSONResponse(status_code=400, content={"error": "allowed must be a boolean"})
        with sessions_lock:
            if session_id not in sessions:
                return JSONResponse(status_code=404, content={"error": "session not found"})
        scope = body.get("scope", "")
        if scope not in ("", "session"):
            return JSONResponse(status_code=400, content={"error": "scope must be \"session\""})
        if not approvals.answer(approval_id, body["allowed"], scope):
            return JSONResponse(
                status_code=404,
                content={"error": "no question is waiting under that id"},
            )
        return JSONResponse(status_code=200, content={"status": "answered"})

    # -- delete session -------------------------------------------------------

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request) -> JSONResponse:
        if not _auth_ok(request, token):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        with sessions_lock:
            sess = sessions.pop(session_id, None)
            if sess is not None:
                _cancel_resume_locked(sess, "session")
        if sess is None:
            return JSONResponse(status_code=404, content={"error": "session not found"})
        # Cancel any running turn before closing the engine, and release anything the
        # guard is still holding open: a question whose window has gone is a refusal.
        sess["cancel"].cancel("session deleted")
        approvals.abandon_all()
        questions.abandon_all()
        sess["engine"].close()
        sess["channel"].close()
        return JSONResponse(status_code=200, content={"status": "deleted"})

    return app
