# Cogito

A coding agent built on one bet: the **harness beats the model**. Good enough tools let a
weak local model do good work. Every harness change is accepted or rejected by a number,
never by opinion.

---

## 1. Language rule

**All code, comments, docstrings, identifiers, commit messages, and repository
documentation are in English.** No exceptions. Conversation with the user may be in any
language; the repository is English.

---

## 1b. What this is for

Any model, not one. The harness carries what a weak model cannot: speed of work, token
cost, and (the part that is easy to forget) **no hallucination and no degradation as a
session gets long**.

Those last two are why the context store is a graph and why Cogito Estella is in the
design. Retrieval with provenance can be answered and checked. A model recalling its own
earlier turns from a summary cannot. The model can reach three things built on that:

- **`ask`**: Estella over the project's prose. Measured at roughly 21x fewer net tokens
  than putting the document in the prompt.
- **`recall`**: Estella over the conversation itself, plus the graph's full-text index
  across the compaction lineage, so compaction no longer means forgetting.
- **Compaction is an edge, not a rewrite.** The session closes, a child is seeded with a
  structured handover, and `(parent)-[:COMPACTED_TO]->(child)` records it. Every original
  message stays in the store; only the context window loses it.

Both tools climb one ladder (one cheap `ask`, a wider `ask` when the scorer is unsure,
pointers last), and the ladder lives in `graph/recall.py`, never in a prompt. A retrieval
policy the model is asked to follow holds only until the model has a bad day.

Any claim about hallucination or degradation needs a benchmark task that would catch it.
Without one it is an opinion. Four tasks exist for these two, and the suite has answered
five times: **they work, but exposed to every task they do not pay for themselves.** Every
comparison rejected them: one real, repeated beneficiary (`big-02-doc-question`), never
enough to clear McNemar, and 11-16% more tokens for carrying two definitions through every
step of every task.

So neither is in the default set. The harness decides, never the model:

- **`ask` is registered only where the project has prose to answer from.** 30 of the 37
  benchmark tasks ship no documentation at all. The test is *any* prose, not *enough*:
  the one task that consistently wins with `ask` has a ~2,200-token corpus, so any useful
  threshold would have removed the only measured benefit.
- **`recall` is exposed only after the conversation has actually been folded**
  (`ToolSpec.after_compaction`). Before the first fold there is no lineage, and everything
  it could return is already in the window.

This is not the on-demand tool loading that was measured and rejected (30/33 → 27/33).
That asked the *model* to know what it would need before it knew. These gates read a fact
the harness already holds. The model is asked nothing.

What the gates do not fix, and must not be claimed to fix: across two separate prompt
wordings, the model never called `recall`, even in the one task built so nothing else can
pass.

`bench probe` in `cogito-lab` puts a value beyond the fold and asks for it back. In one run
of three, the model called `recall` and returned the value exactly. In the other two:

- it ran the source command again and reported *that* output as the one from before, and
- it produced a correctly-shaped identifier that appears nowhere in the graph but in its
  own answer. Invented, not misremembered.

So the tool gets called, and when called it works. The open problem is narrower: the model
does not know that it has forgotten. Two of three times it fluently answered a question it
could not answer.

Three prompt-based attempts failed to change that. The third was the least like a rule.
The seed opened with what it had replaced, counted by the harness, not judged by the model:
"this handover replaced 15 earlier message(s), 6 of them tool results; what they contained
is NOT in front of you; call `recall`; running a command again produces a new result, which
is not what it produced then." Against the same scenario and window:

    without the gap   1 recovered, 2 confabulated   `recall` called 1 of 3
    with the gap      0 recovered, 3 confabulated   `recall` called 0 of 3

Rejected and reverted. One run at n=3 is noise, not a demonstrated regression, but there
is no benefit to weigh against it, and naming the tool in plain words produced fewer calls
to it. This matches "AgentAbstain" (arXiv 2607.10059): abstention is independent of
task-solving ability, and a prompt does not confer it.

So no fourth wording. Telling the model it has forgotten does not make it notice, and a
gate cannot either: a gate decides what is offered, not whether a gap is seen. The next
step has to change what the model *can do*, not what it is told.

The first thing built that way works. `context/literals.py` keeps the values a tool
produced (hash, identifier, path, quantity, quoted, each a named rule) verbatim in the
handover, with the tool and step that produced them, under a token budget that records
every eviction. It tells the model nothing; it keeps the value. Ten runs of the probe's
`printed-value` scenario against ten without it:

    confabulated   8/10  ->  3/10
    recovered      2/10  ->  7/10
    abstained      0/10  ->  0/10      the failure becomes a right answer, not a refusal
    `recall` calls 2/10  ->  1/10      unchanged, and not what this measures
    median tokens  96,898 -> 93,482    -3.5%, noise; this is not a saving

Newcombe 95% CI on the confabulation difference is [-74.0, -6.6] pp and excludes zero.
Fisher's exact two-sided p is 0.0698 and does not clear 0.05. They disagree because the
exact test is conservative at this size; both are reported, not only the flattering one.
The mechanism, checked in the graph, carries more weight than either: of the runs where
the value was in the final seed's block, **8 of 8** answered with a value from that block,
and both runs where it was not confabulated. No exceptions either way.

Two limits apply to that number. The one confabulation with the value present came from a
block holding *two* references: the original at step 2 and a rotated one at step 14, after
the model re-ran the script. It answered with the later one. Provenance already tells them
apart, and the model did not use it. And excluding `read`/`glob`/`grep` assumes their
results can be obtained again, which holds only while nothing rewrites the file in
between. That path is unmeasured.

This is also why the suite could not settle `ask`/`recall`: 36 of its 37 tasks are a
single instruction, so nothing ever leaves the window, and the case where `recall` decides
between a right answer and an invented one never arises.

---

## 2. Layer order

Dependencies flow in one direction only. `core/cogito/dev/check_layers.py` walks every
import and fails the build if a module reaches above its layer.

```
config   -> (nothing)          Settings and paths. Any layer may import it.
errors  ids  cancel           Shared leaves. Any layer may import them.
oslocks  procs                Shared leaves: file locks and Windows process control.

graph    -> config             Neo4j + WAL + Estella: the only store
provider -> graph
guard    -> graph
context  -> graph, provider
tools    -> graph, provider, context, guard    (a tool declares a ToolSpec; the
                                               provider serializes one — shared vocabulary)
loop     -> graph, provider, tools, context
server   -> graph, loop
setup    -> config
cli      -> everything         The composition root, and the only place that may
dev      -> (nothing)          Repository invariants. Never imported at runtime.
```

`tools/` never imports `loop/`. The delegate tool receives an engine factory injected at
registry construction. That rule keeps the dependency graph acyclic and every module
testable with fakes.

---

## 3. Folder map

This is a **whitelist**. A new folder needs a row here in the same commit. A pre-commit
hook rejects new top-level files that are not listed.

| Path | Owns |
|---|---|
| `core/cogito/` | Package root: `cli.py` (composition root) and the shared leaves `errors.py`, `ids.py`, `cancel.py`, `oslocks.py` (an exclusive file lock on POSIX and Windows) and `procs.py` (process trees, liveness and the kill-on-close job on Windows). Nothing else lives here |
| `core/cogito/config/` | Settings, `models.toml` loading, per-platform paths |
| `core/cogito/graph/` | Neo4j driver, schema, WAL, sessions, transcript, recall, Estella client |
| `core/cogito/provider/` | Model catalog, wire dialects, tool-call normalization and repair, and `host.py`: questions to a host over HTTP (health, models, `/props`, `/apply-template`) |
| `core/cogito/provider/local/` | Only for entries with `serve = "motoris"`: start, supervise and stop Cogito Motoris; the bench lock |
| `core/cogito/loop/` | The agent loop: engine, state, budgets, hooks, interrupts, subagents |
| `core/cogito/tools/` | Everything the model can call, plus harness-run checks built on the same subprocess code (`tools/build.py`'s compile gate, triggered from `loop/`, exposes no `ToolSpec`) |
| `core/cogito/tools/edit/` | The edit tool: formats, failure diagnosis, parse verification |
| `core/cogito/tools/web/` | `web_search` and `web_fetch`: the URL rules (public addresses, credentials, the addresses the conversation holds, robots.txt), the HTTP client that obeys them, the keyless search sources and their chain, and the per-session state both tools share |
| `core/cogito/tools/desktop/` | `computer`: seeing the screen and using the mouse and keyboard, through the desktop's portals (Screenshot, RemoteDesktop with screen sharing) on Linux, and opening programs, files and addresses through their desktop entries (`launch.py`) |
| `core/cogito/tools/terminal/` | `run_in_terminal` and `read_terminal`, and the terminal tabs the app shows beside «Hilo»: the person's own shell on a PTY (POSIX), its prompt marks, and a screen model the model reads |
| `core/cogito/tools/browser/` | `browser`: web pages the model opens in the app's own tabs and uses by their elements' refs, and their HTML, scripts, network, console and storage, through the desktop's main process (`server/browsers.py` carries each named operation there and back) |
| `core/cogito/tools/mcp/` | MCP servers' tools: the stdio, HTTP and container (`docker run`) clients, OAuth sign-in for remote servers, the hub that registers every server tool and picks which few each step shows (`mcp_find` included), each server's tool list from its last start (`catalog.py`), the model's `mcp_add` (`install.py`), a project's `.mcp.json` and its approvals (`project.py`) and the import of other clients' servers (`imports.py`) |
| `core/cogito/tools/builtin_skills/` | Agent Skills that ship with Cogito, one folder each with its `SKILL.md` (how to add an MCP server or a skill to Cogito; how to find and remove dead code) |
| `core/cogito/context/` | Token ledger, per-tool budgets, offload, compaction, prompt assembly |
| `core/cogito/guard/` | The two rules: never delete without approval, never touch OS files or the session's own `.cogito/`. In the app only: asking before a system package install, full upgrade or AUR install, and running the approved request; the composer's automatic mode (`auto.py`), which answers those questions except the ones it keeps asking |
| `core/cogito/guard/platform/` | Per-OS protected paths and package-ownership oracles, and the distribution and package managers the app states to the model |
| `core/cogito/guard/shims/` | PATH shims that intercept `rm` and friends in the agent's shell |
| `core/cogito/guard/sandbox/` | bubblewrap wrapper for runs whose side effects must not escape: benchmarks, validation |
| `core/cogito/server/` | Loopback HTTP + SSE API used by the desktop app |
| `core/cogito/setup/` | First-run provisioning: Neo4j, Estella |
| `core/cogito/dev/` | Repository invariants (layer check). Never imported at runtime |
| `desktop/` | The application's own manifest and packaging: `package.json`, `electron-builder.yml` |
| `desktop/src/main/` | Electron main process: window, tray, IPC, core supervision, the context store's keeper, and the file explorer's access to the open project's folder (`explorer-fs.js`, `explorer-ipc.js`). Holds the core's port and token; the renderer never sees either |
| `desktop/src/main/browser/` | The in-app browser: its tabs as WebContentsViews in the window, each project's partition and what hardens it (schemes, permissions, downloads, the user agent, popups as tabs), the fixed operations the core's `browser` tool asks for over each tab's own `webContents.debugger`, and Playwright's injected script (Apache-2.0, vendored) that builds the snapshot and resolves refs |
| `desktop/src/main/updater/` | Automatic updates: the update state machine, electron-updater wiring (NSIS, AppImage), COGITO's own macOS updater and its bundle-swap helper, the signed release manifest and its pinned public keys |
| `desktop/src/preload/` | Context bridge |
| `desktop/src/renderer/` | Chat, orb, model picker, settings, toasts, setup screen, file explorer and read-only viewer |
| `desktop/src/renderer/vendor/xterm/` | xterm.js and its fit addon (MIT), copied from npm as static ESM files with their licences; `xterm-csp.js` keeps them inside the page's CSP |
| `desktop/src/renderer/fonts/` | The design's bundled type (Open Sans, WOFF2) and its licence; the pages' CSP loads no remote font |
| `desktop/src/shared/` | Small, dependency-free tables read by more than one desktop process (e.g. `phase-map.js`'s Phase -> tray-state map, required by the main process). The renderer keeps its own copy instead of reaching across this boundary, because it runs same-directory scripts under a `script-src 'self'` CSP |
| `desktop/assets/icons/` | 16/24/32/48/256 px, colour and symbolic |
| `desktop/packaging/` | Release-time inputs the app does not load: electron-builder hooks (`after-pack.js`: refuses a package without its core, ad-hoc macOS signing), the bundled core's build script and pins (`bundle-core.py`, `core-bundle.json`, `core-build-constraints.txt`), the release gate for a packaged core (`core-check.js`) and, on Windows, for its shell, guard and job (`windows-smoke.py`), the signed update manifest's CLI (`update-manifest.js`) and each release's `release-notes.json` |
| `desktop/packaging/linux/` | The .deb's maintainer-script bodies: one POSIX `sh` body for install, upgrade and reinstall, and the purge body, with `inject-purge.py`, which puts the purge body into the built .deb's postrm |
| `desktop/packaging/apt/` | COGITO's signed APT repository: the script the .deb runs to register it, the repository builder, and its public key once generated |
| `docs/` | `licenses.md` only: the third-party licence notices a release must carry |
| `.githooks/` | The pre-commit hook that runs the two invariants below |
| `.github/workflows/` | `release.yml` only: builds the installers on a tag, or as a dry run that publishes nothing |

---

## 4. Where does this file go?

- Touches the store (Neo4j, WAL, Estella)? -> `graph/`
- Talks to a model, or parses what a model emitted? -> `provider/`
- The model can call it? -> `tools/`
- Decides what stays in the context window? -> `context/`
- Decides whether an action is allowed? -> `guard/`
- Drives the turn? -> `loop/`
- Serves the UI? -> `server/`
- Installs or downloads something? -> `setup/`

If it fits none of these, the folder map is wrong. Fix the map; do not drop the file
somewhere convenient.

---

## 5. Never committed to this repository

Tests, benchmarks, experiments, measurements, model weights, scratch files. They live in
the sibling repository `cogito-lab`, which installs this one with
`uv pip install -e ../Cogito`.

This repository holds only what is needed to run and ship the application.

---

## 6. Tool authoring contract

Every tool in `tools/` must:

1. Declare a `ToolSpec` with a JSON Schema **and** an argument-alias table, so a model
   trained on another harness still lands (`path <- file_path|filename|file`).
2. Declare a token budget. `context/budgets.py` clips every result the same way in the
   loop; a tool cannot opt out and cannot forget.
3. Overflow to an artifact. Anything clipped is written to `.cogito/artifacts/`, and the
   result carries the pointer plus how to read it back.
4. Return a structured error, never a stack trace and never a bare "not found". An error
   that does not tell the model what to do next is a bug in the tool.
5. Be callable without the loop. Tools take a `ToolContext`, not an engine.

---

## 6b. Where a design comes from

**Nothing here is built from memory.** Before anything is implemented (a tool, a prompt
tier, an edit format, a retrieval policy, a compaction strategy), search the current
literature and current practice, and write down what you found next to the decision. The
field moves fast; a harness built on what was true eighteen months ago competes with
eighteen-month-old software.

What counts as evidence, strongest first:

1. A recent paper or technical report with a method and numbers. Prefer the most recent
   work that measures the thing being decided.
2. A public benchmark result, with the harness and the date it was produced.
3. The source of a comparable implementation, read rather than described.
4. Our own measurement from `cogito-lab`, which outranks all of the above **for this
   codebase** and is the only thing that decides whether a change stays.

Recording it is part of the change:

- The commit message says what was read and what it claimed, in one or two sentences.
- A number taken from someone else's work is written with its source and its date, and is
  labelled as theirs until we have reproduced it here. `docs/` and the comments in this
  repository already do this: where a constant came from a measurement, the comment says
  whose.
- A design decision that contradicts recent published work says so, and says why.
- **"This is how it is usually done" is not a source.** Neither is a recalled figure. A
  claim that cannot be attributed is a hypothesis, and it goes to the suite as one.
- **The stack is re-checked, not remembered.** Before a change touches a technology this
  project depends on (llama.cpp, the local models, Neo4j, Cogito Estella, Laya, Electron,
  uv), search the web for its current release notes, changelog and recent papers first. A
  dependency that moved under us invalidates a measurement as surely as a code change
  does; it has happened here once already.

This rule and the next are a pair. This one says where a candidate change comes from; the
next says whether it survives. Research chooses what to try, the number chooses what to
keep, and the number always wins: an idea with an excellent citation that loses on the
suite is still reverted.

## 7. The measurement rule

Fixed in advance so it cannot be argued after seeing the result.

**Every configuration runs every task five times.** Per task, the pass@1 estimate is
passes/5, the fraction of its five repeats that passed. That is the unbiased pass@1
estimator for a fixed n (Chen et al. 2021, "Evaluating Large Language Models Trained on
Code," arXiv:2107.03374; the paper's estimator is for pass@k, and at k=1 it reduces to
exactly this fraction). Nothing is selected and no task is excluded for disagreeing with
itself. Every fraction is an exact rational (`fractions.Fraction`), never a float. A task's
difference is a multiple of 1/5, and every comparison below (the mean-floor test, the
sign-flip p-values, the gap-widening test) compares that exact value with an exact
threshold. 5 percentage points is exactly 1/20; a float approximation of it is not always.

Five and not three comes from `cogito-lab`'s power study
(`experiments/2026-09-24-power-study.md`, addendum and Decision, 2026-09-24). It was
simulated from the real 24-task baseline with the shipped primitives (the exact sign-flip
test and the thresholds), around which the simulation re-implements the rule. It judges gap
widening by its point estimate only and does not model tokens: where the token clause
matters, it is assumed to supply the improvement. Three-repeat figures in parentheses:
five credits a real +10 pp gain 61.1% of the time (40.5%) and a +20 pp gain 87.6% (71.9%),
and flags a no-op as a regression 20.5% of the time (35.3%). It is worse on four rows: it
accepts a no-op 7.9% of the time (6.0%), and when the token clause supplies an improvement
it lets through a true −8 pp regression of the small model alone 16.2% of the time (14.7%)
and true −4.3 pp regressions 39.9% and 32.8% (30.3% and 24.9%); a true −8 pp regression of
both models slips through 6.8% (7.0%). These are simulated numbers, not yet a measurement
here.
Adaptive designs (three repeats, then two more only where control and treatment differed)
were evaluated there and rejected. Their per-task fraction is biased toward zero, which
loosens every fixed-threshold check (the both-models −8 pp regression slips through
11.1-15.0% of the time), and a hybrid that kept the thresholds on the first three repeats
lost most of the power gain.

> **ACCEPT** a harness change if, on the same suite / seed / prompt time / toolchains,
> with each model run separately:
> (1) **no model regresses (policy V3)**: over every task shared by both runs let
> d_i = frac_after − frac_before; test mean(d) with a one-sided exact sign-flip
> permutation test — an exact dynamic program over the per-task differences scaled to
> integers by their common denominator, exact for every n, with no enumeration cap and no
> Monte Carlo fallback. A model regresses if p_regression <= 0.05, OR if mean(d) < −5 pp,
> independent of p_regression — either clause alone is enough, **and**
> (2) **at least one model improves**: p_improvement < 0.10 on the same test, or median
> total tokens over measured sessions drop by >= 10% with a 95% interval that excludes zero — bootstrapped over
> whole tasks, every repeat of a task together and the same tasks in both runs, since five
> repeats of one task are not five independent observations, **and**
> (3) **the gap does not widen**: for each small model, take reference-minus-small pass@1
> on the tasks shared by all four runs (both models' control and treatment), over the
> same per-task fractions (1) and (2) use; the mean change in that value from control to
> treatment must not grow by more than 5 pp, with a 95% bootstrap interval whose low end
> excludes zero.
> Otherwise **REJECT and revert.**

A session killed at the wall clock, or one that exits without reporting a measurement (stop
reason `timeout` or `no_result`; before 2026-10-01, an empty stop reason with every count at
zero), has no token count: the provider never reports usage for the turn it was killed in.
It is left out of the median-token estimate and its bootstrap in both runs, and it still
counts in pass@1 as a failure. Leaving it out could flatter a change that causes more
kills, so the token clause cannot carry acceptance when the treatment has more such
sessions than the control over the shared tasks. The verdict then says "token clause
not applied: the treatment has N killed sessions against M". `bench compare` and `bench
verdict` report how many sessions each run left out.

The quick suite decides; the broad suite confirms every accepted change, and a change the
broad suite contradicts is reverted. The declared target is a gap of at most 10 pp on the
broad suite. It measures progress and is not an acceptance condition.
`bench verdict` in `cogito-lab` applies this rule.

**A change that can act only on some tasks** is decided on the broad suite instead, over
its *active set*: the broad tasks where it can act at all. Examples are a gate that opens
on a property of the project, or a check row for one language. Where such tasks are scarce
in the quick suite, a verdict read from it is noise. The quick suite holds four Python
tasks of 24, and none of its fixtures is large enough to open the `symbols` gate.
Three conditions keep the active set from being chosen after seeing a result:

1. **The active set comes from a predicate.** It is the output of a mechanical predicate
   over the task fixtures, such as "more than 7 source files a grammar reads" or "the
   fixture holds a Python file". The predicate and the suite file it produces are
   committed to `cogito-lab` with the change, before either run. A lab test regenerates
   the file and fails if it differs. It is never edited by hand and never chosen from
   results.
2. **Inertness outside the active set is proven, not assumed.** A lab test shows that, on
   every task left out, the requests the change would send are byte-identical to the
   control's. A task where that cannot be shown belongs to the active set.
3. **The rule above applies unchanged to the active set.** That means five repeats, every
   model and all three clauses. The tasks left out are inert by proof, so the active set
   stands for the whole broad suite and needs no separate confirmation. An active set too
   small for clause (2)'s test ever to reach p < 0.10 (three tasks or fewer) can still
   pass on tokens. It is labelled as such and is never presented as a pass-rate result.

Decided by the user on 2026-10-03, before any run of the first two changes it covers:
the `symbols` code map and a name check in the Python build row.

**Decisions have three outcomes, not two** (decided by the user on 2026-10-07, before the
evaluation it covers). The quick suite detects only effects of roughly 16 pp at useful
power, while harness changes typically move pass@1 by 2-6 pp, so the clauses above were
reading underpowered results as rejections. From now on:

1. **The baseline is the current accepted state.** It includes reasoning-budget
   (`c180fb4`). Every candidate is rebased onto it and measured alone. Two candidates are
   never combined until each has been measured and accepted on its own.
2. **Conditions are fixed before the runs:** the same tasks, seeds, prompt time, limits
   and concurrency. A model whose sessions die at the runner's wall clock is measuring
   speed, not quality. Lower its concurrency first (gemma-4-12b: 4 → 2 workers), and a
   baseline run must show few timeouts before any candidate is compared on it.
3. **Every candidate gets at least two comparable runs** against the baseline, per model,
   before a decision.
4. **A decision weighs all of the following, never p < 0.10 alone:**
   - the size and sign of the effect, with its confidence interval;
   - consistency across runs and models;
   - tokens, wall time and timed-out sessions;
   - regressions by model and by task category.
5. **The outcome is one of three:**
   - **ACCEPT:** the effect is positive and consistent across at least two runs, there is
     no material regression by model or category, there is a token, time or relevant
     quality gain, and nothing is explained by timeouts or by differing conditions.
   - **REJECT:** there is a regression, the effect is consistently null or negative, or
     the change is not worth its cost.
   - **INCONCLUSIVE:** the direction is positive, but the runs lack the power to show it.

   An inconclusive candidate is not reverted for being inconclusive. A candidate that
   stays positive goes to the broad suite (71 tasks), which detects effects of roughly
   9 pp.

**A deterministic fix is accepted on tests, without a general benchmark** (decided by the
user on 2026-10-08). A deterministic fix corrects a concrete incorrect outcome and is
byte-identical everywhere else. It is accepted when it has all of these:

- a test that fails before the fix and passes after it;
- the full lab suite, with no regressions;
- a localized change;
- an integration test, where one applies.

A change that alters what the model or the agent decides is not deterministic; the rules
above decide it. Eighteen fixes were accepted under this rule: `fenced-json` (a fenced JSON
call ends at the fence that closes its value), `admission-visible-gpu` (VRAM admission
reads the GPUs `CUDA_VISIBLE_DEVICES` makes visible), `elision-guard` (placeholder
elisions and mass removals are refused before anything is written), `preserve-eol` (edit
and write keep a file's own line endings), `verify-python` (the project's `python3`
decides a Python parse the harness's interpreter rejects), `truncated-write` (a tool
call the output limit cut off is never run), `stream-error` (an error event inside a
stream raises instead of ending the turn on the cut text), `coerce-quotes` (coerce
unquotes a wrapped scalar, never text a tool writes or runs), `native-args` (native
arguments that do not parse are read without changing a value, or refused as not JSON,
never run as `{}`), `wholefile-guards` (an edit in whole_file format is refused
where write would refuse it), `offload-integrity` (a content-addressed artifact is reused
only when its bytes match, not its size), `inflight-wait` (a document part leaves flight
before its waiter wakes, so `web_fetch` gets Estella's answer), and `status-echo` (a reply
paragraph that repeats the step's status figures is taken out of what is stored, shown
and returned; 0 of 67,202 recorded replies change), `edit-line-count` (an edit counts
and numbers the lines it would write, not its `lines` items, which may hold breaks),
`stray-key` (a bare `lines` or `at` sent as the last `lines` item after multi-line items is
not written into the file; `end` never is), `long-line-clip` (a result whose lines are
too long to fit by whole lines is clipped by characters from both ends, and a re-clip never
keeps both ends whole), `dense-run-count` (a result is measured against its budget with
the floors for hex, base64 and other unspaced runs, not by words alone) and `shell-replace`
(the shell reads a byte that is not UTF-8 as U+FFFD instead of letting the reader die and
the command wait out its timeout). `long-line-clip` came from the app on 2026-10-25: REA's
`list_names` returned 1.1 million characters of JSON on one line, `clip` had no line to
elide and let it in whole, the request reached 409,199 tokens against 32,768, and
`reclip`, which started at four characters a token where that JSON runs near two, returned
it unchanged, so the turn stopped on `context_unfit` after one refusal. Unlike the others it
acts on the benchmark, but only where a result was over its own budget: 99 recorded results
in 56 of 5,616 archived sessions, most of them `shell` (the largest a `grep -r` that read
`.cogito/wal`, 40,193 tokens against 2,000). `dense-run-count` followed on 2026-10-27:
a shell result held one line of 520,275 characters of quoted hex (a decoded packet), the
word counter `clip` used took it for one word and counted the whole result at 1,643 tokens
against a budget of 2,000, so it went in whole and the server counted the request at
516,232. `clip` now counts with `inbound.size`, whose measured floors already cover hex and
base64 runs, and `inbound` classifies a run without the quotes and separators around it,
which had sent `'0102...',` to the generic 2.5 characters a token instead of hex's 1.1. The
same result now counts 474,133 and is clipped to 2,814 characters. Over 60,520 recorded
results it newly clips one, an `edit` error preview counted 634 tokens instead of 586
against 600, and counts 15 already clipped results differently. The lab's `runner-kill` (a timeout kills the whole agent
session, not only `uv`) changes the instrument's runtime digest: runs before and after it
do not pair. One gap is still open, and it predates both
`fenced-json` and `truncated-write`: a fenced call cut off by the output limit after
exactly one ``` inside its arguments still runs, with its content ending at that fence.

**Three behavioural fixes landed before a measurement** (decided by the user on
2026-10-11). They change what the agent does, so they are not deterministic fixes, and the
user approved them on their recorded-run impact. Bug 47 (a call quoted in prose is never
run) fired in 1 of 10,720 recorded sessions and on 0 of 97,611 journalled calls. Bug 21 (an
edit anchor past the end of the file says so) occurred 0 times. Bug 11 (the build gate
compiles only what the turn changed) acts only in multi-turn sessions: 105 of 10,720, in two
tasks. Bug 20's core half, where a non-zero shell exit no longer counts toward consecutive
tool errors, was held out until the suite measured it: in recorded runs 126 of 585
journalled `consecutive_tool_errors` stops ended on a streak of non-zero exits alone.

**Bug 20's core half landed before a measurement** (decided by the user on 2026-10-13,
"pues corrígelos", after testing three local models). `loop.state.is_tool_failure`: a
`shell` command that ran and exited with a positive status resets the consecutive-error
streak instead of extending it; the result is still an error for the model and the graph.
In the QA of that day it cut Qwen2.5-Coder-7B off mid-debugging in 2 of 5 runs (tests failing
with exit 1 while it fixed them). Unlike the fixes around it, it acts on the benchmark:
126 of 585 recorded stops change. Repetition stays bounded by `max_identical_calls` and
`max_steps`. The suite comparison is owed; the cost to watch is tokens in runs that continue
and still fail.

**A read or edit miss names nearby files, landed before a measurement** (decided by the
user on 2026-10-12). When `read` or `edit` is given a path that does not exist, the error
now also lists real project files with a close name, so a weak model reads the right one
instead of guessing again. This changes what the model sees after a miss, so it is not a
deterministic fix and is not app-gated: it acts on every session, the benchmark included.
In recorded runs a read miss happened in 339 of 8,482 sessions (4.0%), and 30% of misses
were followed by another failing read within one call. No golden moved (the goldens' one
read miss has no close candidate). The suite comparison is still owed.

**A bare JSON tool call that follows prose is read, landed before a measurement** (decided
by the user on 2026-10-13). A model that writes an explanation and then a bare
`{"name": ..., "arguments": ...}` object naming a registered tool now has that call read and
run (`repair.embedded_json`), where before the turn ended with the JSON shown as text. This
changes what the agent does, so it is not a deterministic fix. It is inert on the benchmark:
the benchmark models send native tool calls, which `normalize` reads before any text
extractor, and across 100,868 recorded assistant messages the extractor would have fired 0
times. The goldens are unchanged. The suite comparison is owed only if a measured model
starts emitting this shape. Removed on 2026-10-30 (see "Only `<tool_call>` is read in the
text" below): such a call is now answered with the repair prompt, not run.

**A call echoed inside the result framing is not re-run, inert on the benchmark** (decided
by the user on 2026-10-13). The Qwen and Hermes chat templates wrap every tool result the
model is shown in `<tool_response>`/`<tool_result>`. A weak model can parrot that framing
around its own call instead of `<tool_call>`, and then repeat the wrapper verbatim next to
the answer it has already written; the harness mined the repeated envelope as a fresh call
and looped to `no_progress`, discarding the answer (observed on dolphin3-qwen25-3b: 2 of 12
seeds on a one-file read; 10/12 -> 12/12 after the fix). `repair.tool_response_echo` now
reads such a wrapped envelope as a call and marks it `ECHO_TOOL_RESPONSE`; the loop runs the
first copy (the model does intend it) but drops a later copy whose `fingerprint` is already
in `state.calls` this turn, so the answer ends the turn. `repair.strip_result_framing`
removes the echoed wrapper from the stored reply, the reply the window settles and
`final_text`, and the window clears a streamed reply that settles to nothing; before, the
app drew the wrapper twice and the answer twice (shown in the real app on the QA rig, before
and after). It is inert on the benchmark: the
measured models send native tool calls, which `normalize` reads before any text extractor,
and never emit `<tool_response>` text, so the new extractor and the dedup never fire and the
goldens are unchanged. The suite comparison is owed only if a measured model emits this
shape. Removed on 2026-10-30 with the other text forms (below).

**Three general fixes from testing several local models, inert on the benchmark** (decided
by the user on 2026-10-13: "el harness es generalista ... solo solucionar bugs de manera
general"). Found by having dolphin3-qwen25-3b, Qwen2.5-Coder-7B and Qwen3.5-9B write and run
unit tests, in `cogito chat` and in the real app on the QA rig:

- A text-dialect call missing one comma between two values on separate lines was dropped
  whole and the turn ended with the JSON shown as text. `repair.loose_json` now inserts that
  comma, outside strings and only across a newline (json_repair, mangiucugna, mends the same
  slip). The benchmark models send native calls, so no measured request changes.
- `write` and `edit` given a directory said only "is a directory"; they now say what is in
  it and to name a file, never suggesting a deletion. 0 of 4,234 recorded sessions hit it.
- The same call written several times in one reply's text runs once. A 3B wrote one `write`
  50 times in a reply and NO_PROGRESS stopped a finished turn. Native calls are left as sent
  (two identical parallel calls can be meant). `tool_response_echo` also reads a wrapped call
  to a tool that does not exist, so `normalize` answers "unknown tool", and a reply that only
  invents a tool result inside `<tool_response>` is answered under the repair cap; both used
  to end the turn with an empty reply in the app.
- A call written as text is shown in the app as its tool row only, not also as a code block
  (`repair.strip_calls`, live and in the thread drawn from the store). The transcript the
  model is sent back keeps the text as written: removing it there made the 7B start
  inventing tool results, so only the window's copy is cleaned.

- Braces doubled the way a format-string template escapes them, `{{"name": ...}}`, are read
  as single braces when a reply opens with `{{`, which JSON never does (the 7B, 2 of 10 runs:
  both calls dropped). A Python triple-quoted string, `"content": """..."""`, is read as
  one JSON string (the 7B, 2 of 5 runs: a whole `write` dropped).
- A fenced block (any label) or a reply that is an object, naming a registered tool, that no
  extractor can read is answered with the repair prompt ("could not be read, so it was not
  run", and how to send it) instead of ending the turn on it; 5 of 65 local-model runs that
  day ended that way. It never runs anything, and not after an output-limit cut.

Each has a lab test that fails before and passes after. The suite comparison is owed only if
a measured model starts producing one of these shapes. Since 2026-10-30 these apply inside a
`<tool_call>` only, the one text form still read (below); the `tool_response_echo` parts are
removed, and a fenced block or object is answered as a call written out.

**Sampling carries anti-repetition keys, inert on the benchmark** (decided by the user on
2026-10-13). A quantized model at low temperature can lock into a verbatim loop, because
llama-server leaves every repetition sampler off by default (repeat_penalty 1.0,
dry_multiplier 0.0, confirmed on the bench build b10331 `7ba604f1c`); one did, filling a
turn with `-o -name "..."` repeated to the output limit. `SamplingParams` now carries the
optional keys (`repeat_penalty`, `repeat_last_n`, `presence_penalty`, `frequency_penalty`,
`min_p`, `dry_*`), the OpenAI provider puts each on the wire only when a model's `[sampling]`
sets it, and `cli._sampling_for` reads them from the entry. It is inert on the benchmark:
the measured models set none of these keys, so the request body is byte-identical and the
goldens are unchanged. The suite comparison is owed only if a measured model sets one. Only
the person's own local entries opt in (DRY `dry_multiplier = 0.8`, plus a gentle
`repeat_penalty = 1.05`), chosen for loops because DRY penalizes long verbatim repeats
without touching the short ones code needs.

**Estella for offloaded content ships in the app before it is measured** (decided by the
user on 2026-10-10: "usa Cogito Estella", context-fit design decision 14.1, option A). With
`[estella] documents_in_app`, on by default, the desktop app indexes what was too large for
the window and shows `ask_document` for it. Only `serve` reads the setting; `chat`, and so
every benchmark task, never does, and lab goldens prove its requests unchanged. Its benefit
is not measured: the prepared `doc-01`/`doc-02` tasks and a "pasted page" probe are the
measurement still owed. `[estella] documents` stays off until this section accepts it.

**System package requests ship in the app before they are measured** (decided by the user
on 2026-10-12). In the desktop app only (`Guard(packages=True)`, set under `serve`), a system
package install, a full system upgrade and an install through `paru` or `yay` are allowed
with the person's approval, and the core runs them itself (`guard/installer.py`). Removals,
repositories and keys, and every other use of a package manager stay refused; a `curl | sh`
is left as it was. `chat`, and so every benchmark task, never turns it on, and lab goldens
prove its requests unchanged. What it does to the model's behaviour is not measured.

**Web search and page reading ship in the app before they are measured** (decided by the
user on 2026-10-12, D3 of the web design). The desktop app shows `web_search` and
`web_fetch` to every model, local ones included (since 2026-11-01 `web_fetch` reads any address, below). `cogito
chat`, and so every benchmark task, registers neither unless `[web] in_chat` is true, and
lab goldens prove its requests unchanged. Their benefit and cost are not measured: two more
tool definitions on every step is what section 1b found cost 11-16% more tokens when always
on. Since 2026-10-26 the Búsqueda tab has no switches: documentation and web search are
always on and the browser is asked for in the chat (`config/search.py`; the person: "solo
quiero que a veces en el chat Cogito me pregunte, lo demás siempre déjalo activo"). A
`[search]` table an older Cogito saved is ignored. The corpus tasks, the active set and the distraction run of the design (cogito-lab
research/web-browser-DESIGN.md, section 6.3) are the measurement owed. The same decision
lets DuckDuckGo's HTML page be the last search level, with its limits. It also lets the
app download a pinned SearXNG on first use and run it on loopback as the open-web source
(`setup/searxng.py`); only a core the desktop app started does so, never `chat`, the suite
holder or a benchmark, and lab goldens prove their requests unchanged. On the same day the
person asked that web results be retrieved through Cogito Estella and that reading a page
not be token-heavy. In the app only (`WebState.retrieve`), `web_fetch` saves every page that
is not tiny whole, indexes it, and returns only what was asked for under `[web]
fetch_answer_budget` and `fetch_overview_budget` (800 tokens each): the index's passages, else
the page's own matching lines, else the raw start, each labelled. Since 2026-10-14 a page asked
with a `question` jumps the between-turns rule while the tool waits, since the model is not
decoding then (`DocumentIndex.hurry`, `[web] fetch_index_wait`, 20 s): before, a page fetched
mid-turn was never answered by Estella in that turn, and the model read the raw page. The
line matching also tries the model's last search, written for the web, when the question
(often in Spanish) shares no word with the page. On the same day Cogito Estella 0.18.0 added `--lite`
(sentences indexed with spaCy alone, no graph model): a ~7k-token page indexed in 0.2-0.3 s
instead of 35-39 s on 2 CPU threads, with the same passages answered, and on five Spanish
notes the 9B answered the same with `ask` lite (10/10 right, 4/4 unanswerable declined) in a
median 2.9 s instead of 21.3 s, the 7B 3 right against 4 (cogito-lab measurements/
2026-10-14-search-grounding-and-estella-lite.md). Which graph runs full is the harness's
choice, never asked. On 2026-10-15 the person first chose "large models full, small ones
lite" (`[estella] mode = "auto"`: an API model or a local one of 20B parameters or more, by
its GGUF's `general.size_label`, gets the full graph for `ask` and `recall`, and for pages
only on a GPU), then, the same day, lite for every model once the measurement came in: on
14 notes and 20 code questions the full graph matched lite for claude-haiku-4-5 and
qwen3-coder-30b, never answered one lite missed, and was slower in every cell, with the 30B
calling `ask` in 0-3 of 20 sessions under full (cogito-lab measurements/
2026-10-14-search-grounding-and-estella-lite.md, section 8). Those questions are direct
lookups; whether a graph helps questions that need two hops is not measured. The default
is `lite`; `auto` and `full` stay as settings, and only they make the app add the graph
model (Estella's `[full]` extra, `estella-full`, torch from PyTorch's CPU index: 221 MB of
wheels and 464 MB of weights). `chat` keeps the full graph wherever the graph model is
installed. A person's own Estella is never changed.
Indexing the short files a model reads as well changed nothing, so it stays off. Since 2026-10-15 the app installs Cogito Estella's base itself for a person with no
`[estella] command` and none on PATH (`setup/estella.py`, a catalog component like SearXNG):
0.19 moved torch and the graph model to the `[full]` extra, so the release's wheel, pinned
by digest, plus a hashed lock is all the lite index needs. Cogito Estella 0.20.0 (same day)
keeps a multilingual vector beside each lite sentence (multilingual-e5-small, ONNX, int8; on
XQuAD, their measurement, Spanish questions over English text 43 % -> 95.7 %). The app takes
it as it comes: 80 wheels (135 MB, 505 MB installed) and the vector model fetched at install,
not at a session's first question (a 470 MB file quantized to 129 MB kept, 18 s in all
here); its lite graphs then start with `--no-download`, and without the model they answer by
words, as before. Through `web_fetch`, three Spanish questions about English pages found their
answer 3 of 3 times with vectors and 2 of 3 without (cogito-lab experiments/
2026-10-15-estella-020). The install's marker is named by version, so an older install is
replaced by this one. 0.20.1 gives a reply the vectors alone found a `p` under 0.3, so
Cogito's ladder (floor 0.30) escalates it instead of taking it as an answer; in the app it
changed nothing measured (the 9B 12 of 12 fetches answered by Estella, notes 10 right and 4
declined). A configured command still wins, then that install, then PATH. `chat`, even with the web
tools and `[estella] documents` on, never sets it, and lab goldens prove its requests
unchanged. The token saving in the app is not measured; the owed measurement is the same
page set read both ways.

**The local SearXNG's engines rotate, inert on the benchmark** (decided by the user on
2026-10-14, who asked for searches that do not run out without paid APIs). Rotating the
outgoing address was tried first and dropped: Tor recovered no blocked engine (cogito-lab
measurements/2026-10-14-tor-search.md), a bridge changes only the entry, not the exit an
engine sees, and moving between IPv6 addresses needs a routed prefix this machine does not
have. What works is the engines themselves, which limit each address separately. Of 30
general engines asked from the author's machine, 11 answered on topic; the pool is those
plus brave and duckduckgo (`setup/searxng.py`). Each search asks the four asked longest
ago, four more when those bring fewer than three results, and an engine SearXNG reports
blocked rests for SearXNG's own suspension time, doubled on each repeat (`chain.py`). When
no source answers, the model hears which could not, never a bare "No results". Live, 160
paired searches at about ten a minute: both the old five engines and the rotation brought
three or more on-topic results every time; the old set leaned on yep for 63% of its
results, the rotation on no engine for more than 18%. With yep and yahoo removed after the
fact, the old set kept 44 of 120 searches and the rotation 119 of 120 (cogito-lab
measurements/2026-10-14-searxng-engine-pool.md). No engine was pushed to its limit, so how
many searches a day this allows is not measured. The app also reuses a search's results
for six hours (`SearchCache`, a choice, not a measured value): the model is told their age,
and an empty search is never kept. Only the app passes the pool and the cache; `chat` and
every benchmark task never run the local SearXNG, and lab goldens prove their requests
unchanged.

**In the app, a web search reads its top results** (decided with the user on 2026-10-30:
"quiero fomentar que use la herramienta más seguido y sí busque el contenido de la
información"). In the app's records qwen3.8-flash-motoris called `web_search` 27 times and
`web_fetch` once, so it decided on two-line snippets. Search APIs built for agents return
page content with each result (Exa's text and highlights, Tavily's `include_raw_content`
and Extract; theneuralbase.com/tavily-api/learn/advanced/tavily-vs-exa and
docs.litellm.ai/docs/search/tavily.md, read 2026-10-30, secondary sources). With
`WebState.retrieve`, which only the app sets, `web_search` reads its top `[web]
search_read_pages` (2) results through `web_fetch`'s own pipeline (the same robots, cooldown,
cache and fetchable rules) and appends, per page, the passages that answer the query, under
`search_read_budget` (600 tokens) each: Estella's answer when the index is ready within
`search_read_wait` (3 s, not `fetch_index_wait`'s 20), else the page's own lines that share
the query's words, never the head of the page. A page with nothing that matches, or one that
cannot be read, is one line, never a failed search, and the next result is read in its place
(at most two more). Three changes came from the first real search on the QA rig: the first
result, a Stack Overflow question, refused automated readers, so a Stack Exchange question
is read through the Stack Exchange API (`/questions/N/answers?filter=withbody`, the
accepted answer first, then by votes); an unrelated page matched two of four query words in
its navigation line, so a search's reads need two thirds of the words (`web_fetch` keeps a
third); and each page read gets `search_read_deadline` (8 s, not the 30 an explicit fetch
allows), so two slow sites cannot hold a search for a minute. The description then says when to search
(anything current or otherwise recalled from memory: versions, API changes, an exact error
message) and that the top results come with their passages. `chat` and every benchmark never
set `retrieve`, so their descriptions, budgets and results are byte for byte as before, and
the web goldens are unchanged. Its measurement is owed: a task set whose answer needs a
current fact, counting searches, pages read and correct answers, five repeats with
qwen3.8-flash-motoris on Motoris.

**Saved web evidence remains searchable locally in the app** (requested on 2026-10-16).
`graph/web_archive.py` journals successful results' titles, snippets, URLs and attribution
into project-scoped Neo4j nodes. The full-text index matches literal query terms within
that project's scope, with the domain filter before the result limit. This uses Neo4j's
documented transactional full-text indexing and Lucene field queries (documentation read
2026-10-16: https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/full-text-indexes/).
The catalog is checked before external search requests. Only queries with at least two
distinct content terms may skip the external search; the existing English/Spanish
stopword filter supplies those terms. A single broad term checks external sources first
and uses dated local pointers only when they return no results. This conservative gate
fixes the measured Docker source-selection regression (cogito-lab
measurements/2026-10-16-search-specificity.md); it is not semantic confidence or a claim
that longer queries always select the best source. It trades some request savings for
source quality. Elastic's ranking-evaluation documentation requires query-specific
relevance judgments (https://www.elastic.co/guide/en/elasticsearch/reference/8.19/search-rank-eval.html,
read 2026-10-16); the gate itself is our hypothesis, checked on our recorded queries.
Matching URLs are returned with
their original observation time and an explicit notice that their snippets were not
refreshed. `web_fetch` can then request current HTML directly from the site. Local reads never
refresh that timestamp or enter the six-hour fresh-result cache. There is no external
quota for this local index, and no promise of unlimited new web content. Reads exclude
entries older than 30 days; writes prune those entries and retain the newest 512 per
project, deterministic product defaults rather than measured optima. These bounds cover
the derived catalog, not historical transcript/WAL evidence or total database disk usage;
Neo4j reuses freed store space instead of immediately shrinking its files (Operations
Manual, https://neo4j.com/docs/operations-manual/current/performance/space-reuse/, read
2026-10-16). Only the app
injects the callbacks; headless request goldens remain unchanged. Controlled real desktop
and Neo4j checks live in cogito-lab measurements/2026-10-16-local-web-index.md; model quality
and cross-platform installer measurements remain owed. Fully blocked SearXNG groups also
advance through the finite engine pool without spending a normal empty-result round;
transport failures and genuine empty responses keep the two-round bound. Results are
filtered by the requested domain before deciding whether enough have been found
(SearXNG's API documentation warns that some engines ignore `site:`).

**Answers are checked against what the turn retrieved, only for models that need it, in
the app** (decided by the user on 2026-10-14: "tighten when the model needs it, let it go
when it does not; a frontier model needs no restrictive harness"). On invented subjects a
blind judge (Claude Haiku 4.5) found qwen3.5-9b and gemma-4-12b give a made-up value as the
answer once in 66 unanswerable answers, with made-up provenance; a first count by value
match said about a third, wrongly, since it also counted "not found; SHA-256 is only an
example". Smaller models do it far more: in ten probe runs dolphin3-qwen25-3b 9 times,
qwen2.5-coder-7b 3 (cogito-lab measurements/2026-10-14-search-grounding-and-estella-lite.md,
experiments/2026-10-14-grounding-check).
`loop.grounding` reads the values of a final answer (numbers, versions, identifiers,
constants, addresses, by named rules) and finds those in no tool result, message of the
person's or harness text; over the recorded answers it flags 0 of 147 right answers, 1 of
91 declines and 11 of 18 invented values (most of the rest named a value a result did
hold, for the wrong product). In a turn that called a retrieval tool (`web_search`,
`web_fetch`, `ask`, `ask_document`, `recall`), an answer with such values is sent back once
with them named: say which result holds each, that it is the model's own knowledge, or that
it was not found. Pointing at the missing evidence, not telling the model in advance (1b).
Whether a model is checked: a `grounding_strict` pin, else on for a local model and off
for an API one (the probe's `ground_missing` item could also turn it on until the probe was
removed on 2026-10-30). Measured with the check on, it sent
back 2-3 of 26 answers per local model, each one turn more, and the answers then said
what they had not found; Claude Haiku 4.5 never gave a made-up value with or without it,
and forced on it cost 4 extra turns in 26, which is why an API model is left free. `chat`
and every benchmark never check, so their requests do not change; its
benefit is the measurement owed.

**After a fold, the app hands the model what it no longer sees** (decided by the user on
2026-10-16: no lost information or context after a restart or a full window, through
Cogito Estella). On the long-memory probe (cogito-lab experiments/2026-10-17-long-memory),
qwen3.5-9b with 14 folds called `recall` 0 times in every run and answered 2-6 of 8 planted
values, declining the rest as never mentioned; 1b's finding again, that the model does not
know it has forgotten. So in a session that has folded, each turn the harness asks
Estella's record of the conversation with the person's message (`Recall.context_for`) and,
when the ladder's `ask` rungs answer from a message no longer in the window, adds those
sentences, each naming its message, as a harness note before the model runs. Nothing is
asked of the model. Read before choosing: arXiv 2511.17208 retrieves source-attributed
statements for each question before the model answers, and matches or beats strong
baselines on LoCoMo and LongMemEval_S with much shorter contexts (their claim); arXiv
2609.05441 found agents act on a correctly retrieved value only 55% of the time (their
number), so a value put in front of the model is not yet a value used. Only `serve` reads `[estella]
auto_recall`, on by default; `chat` and every benchmark never do, and lab goldens prove
their requests unchanged. Its measurement is the probe above, before and after.

**Images in a message ship in the app, inert on the benchmark** (decided by the user on
2026-10-17, "soportar imágenes ya", after a JPG was dropped silently and the model said it
saw no image). PNG, JPEG, GIF and WebP within Anthropic's limits (10 MB base64, 8000 px a
side; their vision guide, read 2026-10-17) are attached as themselves (`context.images`
reads format and size from the header, no image library). The text carries an
`<attached_image>` marker, which the thread, rewind, folds and the graph read; the message
carries an `ImageBlock` pointing at the session's content-addressed copy
(`ArtifactStore.stash_bytes`), so the journal and a fold's tail copy hold a path, never the
bytes, which a wire adapter reads only when it encodes the request. Claude gets an `image`
block before the words; llama.cpp an `image_url` data part. Whether the model sees images
is `Capabilities.vision`: every Claude model, `args.vision` on an entry, or a local
llama-server's own `/props` (`modalities.vision`), asked only when an image is attached so
no other session sends one more request. A model that cannot see one gets the marker with a
line saying so, and the chip says it first, in amber. A fold's summary never resends an
image, and its estimate counts one by Claude's patch rule. Shown in the real app: without
vision the model got the marker and no bytes; with `vision = true` the request carried the
105,964-byte PNG as an `image_url` part; a pasted screenshot went through. Benchmarks never
attach anything, so their requests do not change.

**A reply that only repeats itself, step after step, is NO_PROGRESS** (decided by the user
on 2026-10-17, "procede con el bug 1", after seeing it in the app). A model can narrate the
exact same thing every step while nudging one tool argument -- `find -name "*package*"`
then `*update*` then `ls | grep` -- so every call has its own `fingerprint` and the
identical-call streak (`max_identical_calls`) never fires, and `find`/`ls` exit 0 so
nothing counts as an error. In one QA session qwen3-coder-30b/gemma-4-12b spun
up to 9 steps on one preamble that way; to the person it reads as a stuck loop that happens
to "advance". `RunState.note_reply` now counts steps whose cleaned reply prose is
byte-identical to the previous step's. The second carries a notice in its tool result to
answer with what the turn found or change the approach (`_REPEATED_REPLY_NOTICE`), and
`check` stops at `max_identical_replies` (4). The window draws such a repeat only once: a reply that
repeats the turn's previous call-making reply word for word, before another call, is settled
empty live and left out of a reopened thread (`server.history.thread_items`); the model is
still sent every copy. That part changes only what is drawn. A stop at three was tried first and dropped:
in that same session the third identical reply was the web search that found the answer,
and the turn would have ended with none. `reset_repeats` clears the streak the moment the
workspace changes, so a real
edit->re-run->fix loop (which narrates something new each turn) is untouched. Empty prose (a
bare call, no narration) never counts. This changes what the agent does, so it is not a
deterministic fix; its measurement is owed. It is inert on the benchmark only if a measured
model never repeats prose verbatim across steps -- the goldens are unchanged (requests are
untouched; this is a stop decision), and the owed check is whether it fires on any recorded
benchmark run.

**A server the agent starts is left running, landed before a measurement** (asked by the
user on 2026-10-17, "arréglalo", after `python3 -m http.server 8888` spun in the app for
the whole 120 s timeout and was then killed). `ShellSession` waits as before, but once a
command has run 5 s it looks, every second, for a TCP port in LISTEN held by one of the
shell's descendants (read from `/proc`, in the shell's own network namespace). When one
listens, that shell is left running with its output drained into a bounded tail, a fresh
shell takes its place in the same directory, and the model reads the PID, the port and
`kill PID`. Closing the session stops every such shell. The 5 s is Coder's threshold for
backgrounding dev servers (coderd/chattool/execute.go, read 2026-10-17); Claude Code and
Gemini CLI leave the choice to the model (`run_in_background`, `is_background`), which a
weak model does not make, so here the harness decides from a socket. It acts on the
benchmark only where a task starts a server: 0 sessions in the 53 recorded runs did, and
the goldens are unchanged. Inside the benchmark sandbox each shell has its own network namespace, so a
server left running there cannot be reached from the next shell. Its measurement is owed.

**A reply that becomes a loop is cut, landed before a measurement** (asked by the user on
2026-10-18 after Qwen3.8-Flash-Next's 1-bit Coder build wrote "lo que lo lo" to the end of
its reply). `loop.chant` watches the streamed text with Gemini CLI's content-loop check
(packages/core/src/services/loopDetectionService.ts, read 2026-10-18: a 50-character window
seen 10 times, on average at most 250 characters apart, with at most 5 distinct stretches
between them; code, tables, lists, headings and dividers are not watched). When it trips,
the request alone is stopped, the reply is kept up to where the run of repeats begins, the
model is told once under the repair cap and asked again, and a second loop ends the turn
(NO_PROGRESS) on what the reply said before it. Over every recorded reply it tripped on 0
of 5,729 benchmark text blocks and, in the app's own conversations, on exactly the two loops
the person saw (`-o -name` repeated, "lo que lo lo"); its other 55 hits were the QA rig's
deliberate filler. It acts on the benchmark only when a reply loops, which no recorded one
did; the goldens are unchanged. Its measurement is owed.

**MCP servers and Agent Skills ship in the app, inert on the benchmark** (asked by the user
on 2026-10-24, to use REA from Cogito: "tiene que ser genérico"). Any stdio MCP server in
`[mcp.servers.NAME]` (Claude Code's `command`/`args`/`env`) has every tool registered as
`NAME__tool`, but a server can carry hundreds (REA: 207 tools, 3.4 MB of input schemas,
about 180k tokens if all were shown), so the harness shows at most `[mcp] max_tools` (6)
within `definitions_budget` (6,000 tokens): those the person's message ranks best by BM25,
then those recently called or found with `mcp_find`, the fallback search tool. That is
RAG-MCP (arXiv 2505.03275, read 2026-10-24: retrieval took selection accuracy from 13.62%
to 43.13% on their stress test and cut prompt tokens by over half; their numbers). Ranking
is by words, so a Spanish message against English descriptions ranks nothing and the model
has `mcp_find` alone. A tool that is not read-only and does not say it is non-destructive
(the MCP specification's default, 2025-06-18) is asked about before each call; REA marks 31
of its 207 that way. The short list is what the
prompt shows, not what a call may name: a call to any registered MCP tool runs even when it
was not among the few shown (`_runnable` vs `_exposed`), because naming a tool is the
discovery the list saves tokens on, and a native model is still held to the shown tools by
its grammar. For that grammar-bound model, a result -- above all an error -- often names
the tool to call next ("No app is open; call open_binary first"): the hub pins any tool of
that server the result names, so the next step shows a prerequisite the message did not
rank. Both found reproducing a REA session where a local 35B model called analysis
tools without opening a target first and could not recover, 2026-10-25. Skills follow the Agent Skills specification and client guide
(agentskills.io, read 2026-10-24): one folder per skill, with its `SKILL.md`, under
`.cogito/skills` in the project and the home directory (the person's choice), with
`.agents/skills` and `.claude/skills` read too, plus Cogito's own `extend-cogito`, which
says how to add a server or a skill. Their first sentences go in the `skill` tool's
definition (about 620 tokens for the fifteen on the author's machine). Asked to install an
MCP server, qwen3.5-9b had that catalog and ran the server in the shell instead, reporting
it installed; so the harness also loads, as a note after the person's message, the one skill
the message clearly matches by BM25 (best score at least 4.5 and twice the next, stopwords
left out; chosen on 25 messages, where it loaded for the five naming an MCP install or
reverse engineering and for none of fifteen coding requests: a hypothesis, not a
measurement). With it the 9B showed the entry, asked,
and wrote a working `[mcp.servers.filesystem]`. The app re-reads `[mcp]` and `[skills]`
for each new conversation, so what is added is there without a restart. `cogito chat`, and
so every benchmark task, registers neither without `[mcp] in_chat` or `[skills] in_chat`
(`COGITO_MCP_IN_CHAT`, `COGITO_SKILLS_IN_CHAT`), and neither table enters the configuration
fingerprint until then, so configured servers do not unpair recorded runs. Owed: whether
the shown tools and the skill catalog pay for their tokens, and how often the selection
misses the tool a turn needs.

**Other clients' MCP servers connect as written, inert on the benchmark** (asked by the user
on 2026-10-29: "tenemos que hacerlo compatible"). The Conexiones form reads what a README
gives for Claude Code, Codex, Gemini CLI, Claude Desktop, Cursor, VS Code or Zed: an install
line (`claude mcp add …`, `add-json`), its JSON, or a bare address. A pasted `claude mcp
add NAME -- npx …` had been saved with `claude` as the server, which then exited. Remote
servers follow the MCP specification (modelcontextprotocol.io, read 2026-10-29): Streamable
HTTP with the 2024-11-05 HTTP+SSE fallback (Transports, 2025-06-18), and OAuth 2.1 sign-in
(Authorization, 2025-11-25): protected-resource and authorization-server discovery in the
specification's order, PKCE S256 (refused without it), `resource` on every request,
dynamic registration, loopback redirect, `state`. Client ID Metadata Documents need an
HTTPS page Cogito does not host, so registration falls back to RFC 7591 or a configured
`client_id`. Live on that day: Context7 and DeepWiki answered over Streamable HTTP, and the
discovery read Notion's, Linear's and GitHub's sign-in servers (GitHub registers no client,
so it needs a token or a `client_id`). Tokens live in the owner-only `secrets.toml`; header
values never enter the fingerprint. The per-server «Cada uso / Solo cambios» choice was
removed the same day: only a tool that may change something is asked about. `chat`
registers MCP tools only with `[mcp] in_chat`, so the goldens are unchanged.
Servers start together in the background: a conversation waits `[mcp] connect_wait` (20 s,
a choice) and one not ready by then joins the next, and the Conexiones list never waits
(«Iniciando…»). Before, each started in turn under one lock, up to `start_timeout` (120 s)
each, and a Docker image that served HTTP inside its container held the app's start. The
thread that starts a stdio server lives as long as the server, because Linux delivers
`PR_SET_PDEATHSIG` when the starting thread ends, not the process.
A `docker run … IMAGE` server runs the way the image speaks (asked by the user on 2026-10-29:
"tiene que correr el docker automáticamente … y los casos erróneos también"). The
image the person pasted serves Streamable HTTP on port 8080 inside its container, declares
no `EXPOSE`, and its stdio mode fails on mcp 2.x, so the line as written never answered.
`tools/mcp/docker.py` checks the engine (installed, daemon up, socket allowed), pulls a
missing image on its own 15-minute clock, starts the container under a name it chooses and
sends `initialize` on stdin; meanwhile it reads the container's LISTEN sockets with
`docker exec … cat /proc/net/tcp*` (the host's `/proc/PID/net` was not readable here), then
the ports its output names. A listener means HTTP: the container restarts with that port
published on 127.0.0.1 and `/mcp`, then `/sse`, is asked; what worked is written to the
table (`http_port`, `http_path`) so the next start goes straight there (4.9 s first, 1.1 s
after, for that image). Containers are stopped by name on close and at exit. Each failure
says what is wrong: no engine, no daemon, no permission, no image, the container's exit and
its last lines, or neither stdin nor a port answering. The clients whose lines the form
reads run a `command` entry as stdio only, so this is our own design, not one read
elsewhere, and a hypothesis about images in general, checked on that image, `mcp/time`
(stdio) and a fake engine.

**A reply that announces a step and makes no call is sent back once, inert on the
benchmark** (asked by the user on 2026-10-25 after seeing a turn end on «Listo» with the
work half done). In the app (a long session on a local 35B model), right after the sixth
fold of a long turn, the model wrote «Let me disassemble the key methods ...
to understand the validation flow:» and stopped: 41 tokens, the server reported no
truncation, no call. The loop read it as the end of the turn. `loop.engine`'s
`_maybe_continue_announced_step` now sends such a reply back once -- no call, on any step (a
resumed session announced its step on step 1 too) -- asking the model to make the call or, if
done, to answer; a second announcement in the turn is accepted, and the notice is read back as
the harness's, not the person's (`readback.is_harness_notice`). Widened on 2026-10-25: the
announcement does not always end in a colon. «Let me find the two functions
using REA and then run the script» ended on «Listo» with nothing done. So besides a
colon, a reply is sent back when its first line opens by announcing a next action in the first
person (`_ANNOUNCED_ACTION`: «Let me …», «I'll …», «Voy a …», «Déjame …», «Continuando …», and
the like; «let me know» is excluded). The openers are kept narrow against false positives:
over the same 1,358 benchmark turns the broader set it was first tried with fired on two
claude-haiku-4-5 conclusions («I need to stop here», «I need to be direct: …»), so «I need to»
/ «necesito» and the restatement markers were dropped; the tightened set fires on 0.
Gemini CLI decides the same question with an extra model request after every turn without a
call (nextSpeakerChecker.ts, read 2026-10-24: "If your last response explicitly states an
immediate next action you intend to take" -> the model speaks next); this is a fixed test
that costs no request when it does not fire. It changes what the agent does, so it is not a
deterministic fix. It is inert on the benchmark: over 10,083 recorded WALs it fires on 0 of
1,680 ended turns, and the goldens are unchanged (this is a stop decision; no request
changes). In the app's own WALs the widened guard fires on 14 of 62 ended turns (5 on the
colon, 9 on an action opener), every one an announcement left undone, across qwen3.6-35b and
qwen3-coder-30b in one project, dolphin3-qwen25-3b, gemma-4-12b and the ollama-qwen2.5-3b turn
in MechCardGame («Let me continue by enhancing the core part data:»). `chat` runs it too, but
with no measured model known to end a turn this way its suite comparison is owed only if one
does. A separate problem the same session showed is not addressed here: with a 32,768-token
window the fold freed almost nothing (step 42 measured 25,009 tokens in, step 43 after the
fold 23,743), so the session re-crossed `compact_at` (0.75) every one to four steps and
folded six times in nine minutes. The tail it copied held REA results of 5,000 to 16,000
characters; how the 23,743 tokens split between the system prompt, the tool definitions,
the seed and the tail is not measured. Bounding the tail on every fold was declined in the
context-fit design (2026-10-10) because it changes benchmark folds; it is the small-window
context work, decided on the long-memory suite.

**The context ring is hidden for a Claude model whose window the catalog does not know,
inert on the benchmark** (asked by the user on 2026-10-24). The desktop context ring reads
`Capabilities.context_known`; the Anthropic dialect left it at its default (true), so a
Claude model `_TRAITS` does not recognize showed a percent of the 200,000-token fallback
guess, while a local model with no `ctx` correctly showed nothing. `AnthropicProvider`
now reports `context_window_known(entry)` as that field (a recognized model or a declared
`ctx` is known; anything else is not), the only reader of which is `Engine.context_status`.
Nothing sent changes: `max_context` is the same figure and a window a refusal names is still
learned. The byte-identity goldens are unchanged. The orb's state is also no longer drawn as
text under it (the «En reposo / Pensando / Trabajando» pill removed, the state kept only as
the orb element's accessible name), a renderer-only change.

**The fold shrinks a large tool result it keeps verbatim, in the app** (asked by the user on
2026-10-25: a real fix for the post-fold stall, not the `_maybe_continue_announced_step`
backstop). The root cause of a weak local model announcing a step and stopping is the
handover: `compact.py` keeps the last few messages whole, so on a 32k window one big tool
output (an REA dump, 13-16k chars in that session) keeps the seed large, the
session re-folds every few steps, and the model, given a bloated seed, restates its plan and
stops instead of acting. `compact.shrink_tail` re-clips each kept tool result over
`handover_result_tokens` down to that many tokens (head+tail plus a pointer to the whole,
reusing `budgets.reclip`), which stays readable with read/grep and searchable with `recall`.
Read before choosing (CONTRIBUTING §6b): JetBrains "The Complexity Trap" (arXiv 2508.21433 --
masking old observations matches summarization at about half the cost, their claim); Gemini
CLI truncates old tool outputs to ~2 KB and protects the 3 newest turns; OpenAI Codex caps
kept history at 20,000 tokens; Anthropic context-editing clears old tool results keeping ~3.
The shared pattern is to shrink the old tool outputs carried forward, not copy them whole;
this is the small-window case the 2026-10-10 context-fit decision deliberately left out of
the always-on tail cap. `handover_result_tokens` is 0 by default, so `chat` and every
benchmark fold byte for byte as before (the byte-identity goldens are unchanged); only the
app opts in, at 500 tokens (`cli._build_engine`, APP-ONLY). Its measurement is owed and is
the point: the long-memory suite and `bench probe` (which reproduce the post-fold stall)
decide whether the stall rate drops without a quick-suite regression. This is candidate 1 of
the root-cause fix; the verbatim-tail token cap on every fold and the literal budget are the
next candidates, each measured alone.

**Three failing calls in a row get a notice, not a stop, in the app** (asked by the user on
2026-10-26: "no se debería de detener, si fallas reintentas"). `max_consecutive_tool_errors`
(3) ended the turn on any three tool failures in a row, even three different attempts: in 5
of 65 turns of one project a weak model was cut off that way, each call a new path or argument,
none a repeat. OpenHands' stuck detector flags only the same action failing 3+ times
(docs.openhands.dev/sdk/guides/agent-stuck-detector, read 2026-10-26), and Gemini CLI's loop
check only identical calls (loopDetectionService.ts, `TOOL_CALL_LOOP_THRESHOLD` 5, read
2026-10-26); neither stops on different failing calls. In the app the streak now appends
`_TOOL_ERRORS_NOTICE` to the failing result (the errors say what was wrong; look the path or
argument up, or ask the person) and clears the streak, up to `max_tool_error_notices` (3) times
a turn; the next streak stops as before. A repeated identical call is still stopped by
`max_identical_calls`, and `max_steps` bounds the rest. It is 0 by default, so `chat` and every
benchmark stop exactly as before and the goldens are unchanged. Its measurement is owed.

**A turn in the app has no time or step limit** (asked by the user on 2026-10-29, after a turn
ended on «Se acabó el tiempo»: "no debía parar"). A long turn on cogito motoris ran 42 steps and four folds and
stopped at the 30-minute `wall_clock_seconds`; 5 of those minutes were an approval the person
was reading and 5.5 one long command. The person watches an app turn and has the stop
button, so the app sets `wall_clock_seconds` and `max_steps` to 0, which means none, and the
prompt then shows no steps left. What still ends an app turn on its own is a real loop: the
same call (`max_identical_calls`), the same reply (`max_identical_replies`), a reply that
repeats itself (`loop.chant`), or failing calls after their notices. `chat` and every
benchmark keep 60 steps and 1,800 s, so their stops and requests are unchanged.

**File tools expand `~`, and an empty glob says the name is not there** (asked by the user
on 2026-10-25, "ayúdame a que esos errores no pasen", after a turn of failed calls in the
app). `read`, `write`, `edit`, `glob` and `grep` took `~/x` as `<project>/~/x`, so a home
path failed with "does not exist" while the same `~` worked in `shell`;
`project_files.resolve_path` now expands it as the shell does and resolves everything else
as before. Over 364 recorded sessions on the author's machine one call used `~`, and it
failed. That part is a deterministic fix. The second part is not: an empty `glob` reused
grep's message, which calls the pattern a regular expression. That is false for a glob, and
in the app a model trimmed the same file name three ways until `max_identical_calls`
stopped the turn. It now says no file by that name is under the folder, that hidden and
.gitignored files are skipped, and that repeating the search will not find one. In the same
sessions 139 of 217 globs came back empty, and 77 of those were followed by another empty
glob. Grep's message is unchanged. It acts on the benchmark wherever a glob is empty, so its
suite comparison is owed.

**Cogito stops starting llama-server** (decided by the user on 2026-10-27). The lab starts
it and gives Cogito an OpenAI-compatible host with `args.server_extensions = true`; the
byte-identity goldens are unchanged. The run header changed (`[local]`, GGUF fields,
`PROBE_VERSION` 5), so runs before it do not pair with runs after it: one fresh control run
per model.

**The model probe and profiles are removed** (decided by the user on 2026-10-30: "quita
todo lo que tenga que ver con eso que no sea crítico en cómo funciona la app"). The app's
aptitude marks went on 2026-10-26; now `cogito models probe`, the background prober, the
stored profiles and the `[models]` table go too. Profiles were off by default and never
accepted under this section, so every session already ran the off path, and a session's
settings, request options and tools are unchanged; the byte-identity goldens are
unchanged. A model's `pin` table keeps only `grounding_strict`. `chat --json` no longer
reports `profile`, and the lab records every run as `profile: off`.

**After a fold the person's latest request leads, in the app** (decided by the user on
2026-10-30: "le decía a qwen, haz otra cosa ... pero todavía realizaba procesos antiguos";
"la detección automática, es lo que tú haces"). A message sent mid-turn was folded as a bare
`>` line under the first request, still marked `[working on this now]`; a request cut short
before a newer one read as unfinished work to resume; and the handover request never said
the latest message sets the goal. With `[budgets] fold_latest_request`, which only the app
turns on (`compact.changed_course`): the amended request is marked as changed by the
messages under it, which carry a line saying the later one wins where they conflict; a
request cut short and followed by another is marked not to be resumed unless asked; and the
handover request gains one rule, that `goal` and `next_step` are what the person wants now.
No extra model call decides it: where the person changed course is a fact the harness
holds. A conversation that never changed course folds as before. Codex keeps user messages
verbatim with the newest last and has an open report in the same area
(github.com/openai/codex/issues/21291, read 2026-10-30); goal drift with context length is
measured in arXiv 2505.02709 (their numbers). Shown in the real app on the QA rig: after a
mid-turn message, both later fold requests carried the rule and the next seeds the mark.
`chat` and every benchmark fold byte for byte as before (the fix golden, which includes a
steer, is unchanged). Its measurement is owed: a long-session task where a mid-turn change
of course meets a fold.

**Only `<tool_call>` is read in the text; other call forms are answered, not run** (decided
by the user on 2026-10-30, with the two forms shown side by side: "debería ser la normal y
quitar la llamada escrita como texto"; then "no quiero que un flujo se corte a la mitad solo
por eso"; then "haz lo que creas mejor", per-model harness). Cogito now targets Cogito
Motoris (qwen3.8-flash-motoris) and Claude API models, which send native `tool_calls`; the
live catalog sets no `native_tools = false`. Across every app WAL on the author's machine,
text extraction fired only for models no longer in the catalog (`fenced_json` hermes4-14b
11, `embedded_json`/`bare_json` qwen25-coder-14b 4, `xml_tool_call` qwen3-coder-30b-raw 2),
never for qwen3.8-flash-motoris; the benchmark models send native calls. Kept: native calls
and their argument repairs, and `repair.xml_tool_call`, the Qwen/Hermes chat-template form,
in case a server leaves it in the text instead of converting it (no extra request). With it
stay what serves it: its cut-off check, a `<tool_call>` repeated in one reply running once,
the window showing it as its tool row only (live and from the store), and the tool list
described in the prompt for an endpoint without native tools, which asks for that form.
Removed: `xml_function`, `python_tag`, `claude_invoke`, `fenced_json` (and its undelimited
refusal), `bare_json`, `pythonic_call`, `embedded_json`, `tool_response_echo` with
`strip_result_framing` and the invented-result answer. Any other form that clearly names a
registered tool (a ```json or unlabelled block, a reply that is one fenced block, a bare
object, `<function=...>`, `<invoke name=...>`, an unclosed `<tool_call>`) is recognised by
`repair.text_call`, never run, and answered with the repair prompt under its cap, so the
turn goes on; prose, examples in another language and unknown names are left alone, as
before (bug 47). Lab: a reply holding only a fenced `read` gets the repair message, the
turn continues and the native call that follows runs (fails on the old extraction). Inert
on the benchmark's measured models (native calls); the truncated-call golden is compared
for its native session only, since its text session's calls were bare JSON.

**Qwen's `<function=...>` body is read, beside native calls too** (found in the app on
2026-10-31). qwen3.8-flash-motoris wrote a `<tool_call>` with Qwen's own body,
`<function=fetch_results><parameter=pid>2182</parameter>...`, then a native `shell`
call in the same reply. llama-server converts a `<tool_call>` only for a tool the request
listed; this one named an MCP tool the step did not show, and without its server prefix
(`Server__fetch_results`), so it stayed in the text, and Cogito, which read text
only when no native call came, never ran it and drew the XML as the answer. In that
session's record 3 replies held a `<tool_call>` beside a native call, none alone. Now
`xml_tool_call` reads that body as vLLM's Qwen Coder parser does
(rust/src/parser/src/tool/qwen_coder.rs, read 2026-10-31: one wrapping newline off each
value, values converted only by their declared type), `normalize.extract` reads the
`<tool_call>` blocks left in the text beside native calls and runs them first,
`resolve_name` maps a bare MCP tool name to the one `SERVER__name` that ends in it (two
servers with the name: not guessed), and the window hides such a call as it hides one read
alone. A written call to no tool beside valid ones is not reported, as for any failing call
beside valid ones. Lab: a reply with that call and a native one runs both, with `pid` as a
number, and draws neither as text (fails before). On 2026-11-01 the same model wrote a
`<tool_call>` for `kill_process`, which no server has: the harness answered "unknown tool"
and the turn went on, but the window drew the XML as the answer, because only calls to a
registered tool were taken out of what is shown. Now every closed `<tool_call>` the
extractor reads is left out of the window, live (`Extraction.written`) and read back from
the store, whatever it names; the transcript keeps it as written. Only what is drawn
changes. Inert on the benchmark's measured models,
whose replies hold no `<tool_call>` text; the goldens decide.

**A permission card in the app waits until it is answered** (asked by the user on
2026-10-31: "no debería salir omitiste cuando tardó en contestar una solicitud"). A guard
question (a deletion, an MCP tool that may change something, a package install) and a
component's permission card were refused after 300 s, and the window drew «Omitiste» for a
person who had only been slow to answer. They now wait without a deadline, as the budget
pause already did and as a turn in the app has no clock since 2026-10-29: «Detener» and a
closing window still end the wait, nothing runs while it lasts, and the shell already moves
a command's deadline by the time the person was kept waiting. The `rm` shim waits as long as
the broker; a broker that goes away closes its socket. Only the app asks these questions
(`chat` and the benchmark have no window), so their requests do not change. Lab: a wait
outlasts the old deadline and takes the answer, «Detener» and a closing window end it, and
the window approver asks for no deadline (fails before).

**A web search asks only the open web** (decided by the user on 2026-10-31: first "Quitar
MDN", after the model reported its searches returned MDN pages instead of forums, then
"puedes dejar la web abierta solamente"). The chain asked developer APIs first (Stack
Exchange for every query, the project's package registry, MDN in a project with web files,
GitHub) and went on to the open web only when they brought fewer than three results. Those
APIs match loosely. MDN's search answers any query with its own pages ("card game banlist
october 2026" returned Firefox release notes and `will-change`, asked live that day), and
its seven or eight results met the threshold: in one session 57 searches in a
row were answered by MDN alone, and none of its 449 results held a third of the query's
words (the best 27%). With MDN removed, a real search on the QA rig from a project with a
`package.json` was answered by npm alone with six unrelated packages (supabase, mixpanel),
for the same reason. So the chain now asks only the open web: the person's SearXNG, else the
app's own SearXNG with its rotation, then DuckDuckGo while fewer than three results are in;
Stack Exchange, the registries, GitHub, MDN and Wikipedia are no longer asked, `site` is
always sent as `site:`, and a cached result from a removed source is not reused. Reading a
Stack Exchange question the search found still goes through its API (`web_fetch`). Only
`web_search` changes, which `chat` and the benchmark register only with `[web] in_chat`, so
their requests do not change. Lab: a project with web files and a `package.json` asks none of
the removed sources, and cached hits from them are not reused (both fail before).

**Tavily's and Firecrawl's keyless search back SearXNG up** (decided by the user on
2026-10-31: "hay que evitar que falle lo más posible", then "Sí, añádelos"). In the app's
records the local SearXNG answered 104 of 105 searches (the other was cancelled), but when it
is not running yet, or every engine it asks is blocked, only DuckDuckGo was left, and it stops
for the session on its first block. Both APIs answer a POST with no account or key, free and
rate-limited per IP with unpublished numbers (docs.tavily.com/documentation/keyless, read
2026-10-31: the `X-Tavily-Access-Mode: keyless` header; firecrawl.dev/blog/
firecrawl-keyless-launch, 2026-06-16: "capped per IP address each day"). So each is asked
only while fewer than three results are in, Tavily first, then Firecrawl, then DuckDuckGo; a
refusal rests the source (`Budgets`), as for any other. Live on that day: Tavily answered on
topic in about a second; Firecrawl refused the author's machine as "a VPN or proxy" (403),
which the chain treats as a block. A query still leaves the machine scrubbed of the person's
user, home and host names, as before, now to these two companies too. LangSearch and the
paid APIs in the awesome-llm-web-search list were read and left out: all need an account. The
pinned transport now carries a request's body, which a GET never had. Lab: without SearXNG
Tavily answers and nothing else is asked; a refused Tavily falls to Firecrawl, both refused
to DuckDuckGo; enough from SearXNG spends no keyless request (6 of 7 fail before).

**A message sent mid-turn is answered and the work goes on, in the app** (asked by the user
on 2026-10-31: "lo más parecido a como te comportas al manejar múltiples mensajes"). A steer
was wrapped as "Take it into account from here on; it may change what you do next", which
reads every message as a change of plan, so «¿cómo va?» could end the work it asked about;
after a fold it was also marked as the request that replaced the first. Read before
choosing: Claude Code 2.1.304 hands its model such a message after the next tool result as
"The user sent a new message while you were working: ... Address the message above as you
continue this turn." (that session's own transcript, read 2026-10-31); Codex records it as a
plain user message after the tool outputs (codex-rs/core/src/session/turn.rs); Gemini CLI,
behind its off-by-default `modelSteering`, asks to "Re-evaluate the active plan"
(packages/core/src/utils/fastAckHelper.ts), read 2026-10-31. With `[budgets]
steer_continues`, which only the app turns on, the steer carries Claude Code's wording plus
what addressing it means ("answer it, and keep working unless it changes what you should
do"), and the fold says a question among the messages does not change the request.
`readback` reads both wrappers, so older sessions reopen as before. Shown in the real app on
the QA rig: the request carried the new wrapper and the window drew «¿cómo va?» as written.
`chat` and every benchmark send the old wrapper (the fix golden, which includes a steer, is
unchanged). Its measurement is owed: whether Motoris answers a mid-turn question and finishes
the task, against the old wrapper.

**A fold resets the identical-call count, in the app** (asked by the user on 2026-10-31, "sí
hazlo, corrígelo", after a long turn ended on «Se detuvo sin avanzar»). On Motoris's
32,768-token window a fold leaves about 10k tokens (5.5k fixed, the rest the handover), so
that 48-step turn folded four times; one `sed` read ran at calls 11, 24 and 49, each in a new
window, with a fold and a dozen other calls between them. `max_identical_calls` (3) counted
it over the whole turn, and only a workspace change or a steer reset the count, so reading a
file again after the fold had dropped it stopped the turn as a loop. In that session's 2,027
shell calls, 16 repeated a command of the same turn and 12 of those had a fold between them.
OpenHands and Gemini CLI flag only identical calls in a row (read 2026-10-26, above). With
`[budgets] fold_resets_repeats`, which only the app sets, a fold calls `reset_repeats` as a
workspace change does; three identical calls inside one window still stop the turn. `chat`
and every benchmark count as before, so their stops are unchanged. Lab: a read made once in
each of three windows ends the turn normally, and three in one window still stop it (the
first fails before). What it does not fix: the window itself, which makes the model re-read;
a larger context for Motoris is the measurement owed.

**A Motoris entry takes its window from the engine config, and its thinking budget from
the first request** (asked by the user on 2026-10-31: "revisa lo nuevo de cogito motoris
... e intégralo"). Motoris's setup moved this machine to the trained 256K
(cogito-motoris b081c66f: the KV cache streamed from RAM, 32K cells per layer in VRAM;
their numbers: decode 75-88 tok/s at both sizes, needle 9 of 9 up to 250K), and the run
config says `--max-context 262144`, but `models.toml` still carried the `ctx = 32768` the
connection screen copied when the entry was saved, so Cogito folded at about 24.5K. Now a
`serve = "motoris"` entry's `ctx` is the config's `--max-context` whenever the config can
be read and names one (`config.models.motoris_max_context`); a stale `ctx` no longer
wins. Second: the step's thinking budget (`max_reasoning_tokens_per_step`, 2048) reached
Motoris only after a reply had reasoned, because Cogito asks `/apply-template` whether a
model thinks and Motoris answers 404. Motoris reads the budget under llama.cpp's name too
(serve/server.py `reasoning_budget`), and its Qwen thinks by default ("high",
docs/DETAILS.md), so a Motoris entry now always sends it, unless the request turns
thinking off. One request each on a hard prompt, temperature 1, `max_tokens` 4096, so a
sample, not a measurement: no budget thought 6,652 characters in 43.8 s; 2,048, 4,673 in
30.5 s; 256 under either name about 650; `reasoning_effort: low` 7,639 (the levels are
instructions the model was trained with, not a limit, as Strata's docs say); `none`, 0 in
23.1 s. Only entries Cogito starts are touched; the benchmark's llama-server entries send
the same bytes, and the goldens are unchanged. The owed measurement is tokens and wall
time per app turn at 256K against 32K.

**The composer chooses how much the model thinks, in the app** (asked by the user on
2026-10-31: "el control de la inferencia tiene que estar aquí", pointing under the
message; then "un solo símbolo, tal vez un trueno"). A bolt beside the skills picker,
whose look is the level (struck, outline, pale fill, lit blue) and whose name is its
tooltip, offers four levels, kept per window: «No» (`thinking: "off"`, the chat template's `enable_thinking` off), «Normal»
(nothing sent: the session's own `max_reasoning_tokens_per_step`), «Más» (`"long"`,
`long_reasoning_tokens_per_step`, 8,192, a choice, held under the step's output so a
quarter of it, at least 512 tokens, is left to answer, and never below «Normal») and «Sin
límite» (`"free"`, no budget, so Motoris keeps a quarter of `max_tokens` for the answer).
A message sent as a steer that becomes a new turn, because the turn ended first, carries
the level too. It is a budget, not Strata's
`reasoning_effort`: on the sample above `low` thought longer than the default, and
Strata's docs call the levels trained instructions, not a limit. `/models` says which
entries take a budget (`thinking`, `ModelEntry.takes_server_extensions`), and the button
shows only for those. `EngineSession` reconfigures the provider for the turn and restores
the session's own options on a turn without a level. Shown in the real app on the QA rig:
«No» sent `enable_thinking: false`, and the next turns on «Normal» sent neither field.
`chat` and every benchmark send no level, so their requests do not change. Whether people
pick a level, and what each costs in time on Motoris, is not measured.

**The bolt sets Claude's effort, and the app keeps the newest Claude models in the
catalog** (asked by the user on 2026-10-31: "¿y funciona para las APIs de Claude?", then
"hazlo y revisa que también actualice los modelos"). Claude takes no token budget on its
current models; it takes `output_config.effort` and a thinking switch, per model (the
claude-api reference bundled with Claude Code 2.1.304, cached 2026-10-06, "Thinking &
Effort"). `ModelTraits` now carries each model's effort levels, its default and how it is
switched off, and `AnthropicProvider._effort` maps the composer's level: «No» switches
thinking off (`disabled`, or `between_tools` on Sonnet 5.5, where `disabled` is a 400), or,
on a model that always thinks (Fable 5.x, Opus 5.5), sends its lowest effort; «Más» is one
level above the model's default (`medium` on Opus 5.5 and Haiku 5.5, `high` elsewhere);
«Sin límite» is its highest, and at `xhigh` or `max` the request asks for at least 64,000
output tokens, as the reference requires; Haiku 4.5, with no effort, gets a token budget
instead. «Normal» is sent as `normal` and turns thinking on at the model's own effort: before,
Cogito switched it off on Claude unless an entry set `reasoning = true`, so «Normal» and «No»
sent the same request. A model whose levels are not known gets no effort, since a wrong one
is a 400. A fold's handover never takes the turn's level: at `max`, or with a budget of three
quarters of its 1,400 tokens, the summary would be spent thinking. `chat` and every benchmark
send no level, so their requests are as before; Haiku 5.5's day cost is summed per request,
since its second tier is per request. Sonnet 5.5 and Haiku 5.5 joined the traits and the price table (Haiku
5.5 with its second tier above 100K prompt tokens, which `Price` now carries). Then, when
the desktop app starts its core, `provider/claude_models.py` asks the Claude API for its
model list (free) with the key a Claude entry already uses and appends the newest model of
each family the catalog lacks, through `append_entry`, so every other entry stays byte for
byte; nothing is removed. Only the newest per family, as the person chose («generación
actual»). `chat`, the suite holder and every benchmark never run it. Not tried live: the
key the catalog names was refused (401) on 2026-10-31, so the request shapes rest on the
reference and the lab's tests, and the owed check is one turn per level on Haiku 5.5 once
a working key is in place.

**A skill that names a switched-off MCP server is not offered** (found on 2026-10-31, the
person: "REA está apagado pero veo que el modelo está haciendo escaneos ... no debería
envenenar las instrucciones"). With REA off in Conexiones, `reverse-engineer-anything` ("...
with REA") was still in `skill`'s catalog every step and in reach of the automatic load.
That session's record holds no REA call, server instruction or loaded skill: the scans were
the model's own shell work on a file it was asked to extract. Still, a skill for tools the
session does not have reads as an invitation to do that work by other means, so
`skills_for` now leaves out a skill whose name or description names a switched-off server as
a whole word, in the catalog and in the composer's picker alike. On the author's machine it
hides that one skill of fifteen.

**In the app, a command that outlives its timeout is moved to the background, and its end
reaches the model** (asked by the user on 2026-10-31: "cogito puede saber cuando un modelo o
terminal que maneje termine ... después vuelve a comunicar al modelo para los pasos
siguientes"). The shell killed every command at its timeout (120 s by default) unless it
listened on a port, so a 30 GB download died or the model slept in loops checking a file
size. Read before choosing, all on 2026-10-31: Claude Code moves such a command to the
background and "re-invokes you when it exits", in a turn of its own when idle, with a notice
that no human input occurred (its tools reference and changelog, 2.1.205 and 2.1.234);
Gemini CLI injects completions only when configured and labels them data
(`executionLifecycleService.ts`); Codex (`unified_exec`), OpenHands (`terminal_session.py`),
Cline and Aider leave the model to poll. Here the harness decides, as for servers
(`ShellSession(background_at_timeout=...)`, only the app sets it): the command keeps running
in its own shell, the model reads its pid and that it will be told, and is asked not to poll.
When it ends, `on_exit` builds a report (the command, its exit code or that it was stopped,
its last 40 lines, at most 4,000 characters; choices) and `Engine.notify` queues it. A running
turn reads it at its next step, and a reply that answered just then goes on; with no turn
running the server wakes one (`Engine.wake`, `_maybe_wake`), opened by the report alone and
adding no instruction, at most `MAX_WAKES` (10, a choice) in a row without a message from
the person, and never after «Detener». The report says it is the harness's and that the
output is data, not instructions (`_BACKGROUND_NOTICE`), and the window draws a line where
it arrived. Closing the session stops the commands without reporting them. `chat` and every
benchmark kill at the timeout as before, so their requests do not change. Shown in the real
app on the QA rig: a 6-second command with a 2-second timeout was moved, the turn answered,
and four seconds later a turn opened with its report and went on. A review of the change
found, and the lab now covers: the pid is the shell that leads the command's process group,
so the hint is `kill -- -PID` (plain `kill` left the command running); only a command moved
at its timeout is reported, never a server the model stops itself; a command that ends right
at its deadline is finished, not moved (its end marker was being swallowed, and the report
never came); the end is reported at the marker, not when a child it started lets the pipe
close; «Detener» is checked before the timeout; no turn is woken while a provider failure
waits to resume, after «Detener» or a budget stop, or for a conversation that was reset; a
woken turn's end is recorded against its report, not the person's last request; and a
message sent just as a woken turn starts joins it as a steer instead of failing. One gap is
open: a deletion a background command asks about pauses the deadline of the command in the
foreground too, since the guard's waiting clock is the session's. Whether models stop
polling, and how often a woken turn is useful, is not measured.

**In the app, the model can see the screen and use the computer** (asked by the user on
2026-10-31: "¿puede tomar capturas de pantalla de mi escritorio ... así como lo haces tú?",
then "que también tenga herramientas para controlar visualmente la computadora ... manejar el
DOM ... mouse, dar click"). Three pieces, researched first (cogito-lab
research/2026-10-31-computer-use, all read 2026-10-31):

- A tool result can carry images (`ToolResult.images`, `ToolResultBlock.images`, journaled
  by path like an attached image). Claude gets them inside the `tool_result`; llama.cpp's
  dialect, whose `tool` message is text, gets them in one user message after the step's
  results, never between two tool messages. The newest three are sent
  (`RESULT_IMAGES_KEPT`, Anthropic's best-practices post of 2026-05-13 keeps three), and
  older ones are left out five at a time (below); a fold's
  summary never resends one; a model that cannot see is told. An MCP server's image parts,
  which were dropped, now reach the model too.
- `computer` (`tools/desktop`): screenshot, zoom, clicks, drag, scroll, type and keys, through
  xdg-desktop-portal: the Screenshot portal with `interactive=false` and RemoteDesktop with
  screen sharing, `persist_mode=2` and a saved restore token, so KDE asks once and then only
  shows a notification while a session runs (xdg-desktop-portal 1.22.1, portal-kde 6.8.2,
  KWin 6.8.2 sources). KWin's own D-Bus would capture and inject with no prompt; it is not
  used. Coordinates are a 1000x1000 grid over the screen looked at, as Qwen-VL answers
  (arXiv 2511.21631); Agent S4 measured Qwen3.8 at 0 of 6 targets asked in raw pixels and at
  50% on its grid against Claude Sonnet 5's 78% (their gui.py, their numbers), so every
  action returns a fresh screenshot, at most 1280 wide, to check. Claude's native
  `computer_toolset_20260801` is not used yet; the one tool serves both.
- `browser` (`tools/browser`): playwright-python's `ai` accessibility snapshot and refs
  (Playwright 1.64), the engine playwright-mcp uses, but not that server: through the MCP
  hub its screenshots were dropped, BM25 would hide its tools between steps, and a full
  snapshot of a real page measured 15,000-23,000 tokens, which a 32K window cannot take. So
  `snapshot` shows the interactive elements under the tool's budget, `find` returns lines that
  match words, actions go by ref, and `screenshot` returns the page as an image. Playwright's
  Chromium downloaded on first use; since 2026-11-05 the tabs are the app's own (below).

Each tool is asked about once a session and never remembered past it
(`ExternalTool.session_only`), and the desktop's portal asks once more for the screen. Once
allowed, `computer` acts as the person: it can type into a terminal or press a button of
Cogito's own, which the guard's shell and file rules do not see; «Detener» stops it, and the
approval lasts the session only. A review of the change found, and the lab now covers:
result images reached a model that cannot see them after the picker moved the conversation
(now left out, the marker kept); the session's close callbacks were called with an argument
the tools did not take; a zoom's grid, a «+» key, a stop while typing, a partly shared
desktop, a closed tab, a stale ref (which waited the full timeout) and a URL without a
scheme. App only: `chat` and every benchmark
register neither, so their requests do not change. Lab: the encoders, the journal, the
fold, the grid mapping on two monitors with a fake portal, and the browser on a real headless
Chromium; shown in the real app on the QA rig, the browser card and a page's refs. The live
portal flow on the author's KDE Plasma 6.8, once the person had accepted its two dialogs: a
screenshot of both monitors (3840x1331) in 0.2 s; RemoteDesktop started in 7.5 s and saved
its token, and the next session restored it with no dialog; the pointer moved where it was
sent (two screenshots differ from that pixel on). The first try had timed out with nobody
at the desktop, and showed the response signal needs a bus match rule, now added. Whether a ref snapshot beats a screenshot for Qwen3.8-Flash, and how well it clicks,
is the measurement owed.

**A step reuses the prompt Motoris already read** (asked by the user on 2026-11-01: "cada
acción tarda mucho ... ¿hay forma de optimizar?"). In the app's first real computer-use
turn, each action took 0.3-1.2 s and each model step 10-39 s: 6-11 s before the first token,
then the thinking. Motoris's log said why: at 120K tokens, "121467 tokens = 106264 reused +
15203 read in 10976 ms", and the read grew a step at a time until it reset near 18K. Strata's
model is hybrid, so it resumes only at a saved checkpoint, taken at a prompt's last turn and
every 16K tokens (cogito-motoris docs/DETAILS.md and generate.cpp, read 2026-11-01). Two
things broke every turn checkpoint. Each request ends with the step's status turn ("Now:
..."), which the next request does not have; text-only sessions showed it too, with reuse
stuck at the end of the system prompt. And one screenshot was dropped per step, which changed
the prompt three steps back. So Cogito now starts Motoris with `STRATA_CACHE_MESSAGE_BOUNDARY=1`,
which also saves a checkpoint before a short last turn (their doc; the prompt is the same),
unless the person's environment sets it. And older result images are left out five at a time,
as Anthropic's computer-use demo does to keep its prompt cache
(`_maybe_filter_to_n_most_recent_images`, read 2026-11-01), so three to seven are sent. On 20
encoded steps, a request began with the previous one minus its status turn in 2 of 19 steps
before and 16 of 19 after. Where a checkpoint did match, the same log shows 1.3-2 s before
the first token (397 tokens read in 1.29 s); the time per step after this change is not
measured yet. `chat` and every benchmark send no result images and start no
Motoris, so their requests do not change. What is left is the thinking: 671 tokens a step on
average, 8.3 s.

**In the app, the model says in a few words what each call is for** (asked by the user on
2026-11-01: "muy corto y breve ... voy a ejecutar esto para ver qué hay dentro de este
archivo y luego ejecuta la acción", without spending many tokens). The identity's last line,
"Say what you did, not what you are about to do", becomes, in a core the app owns
(`LoopSettings.preambles`): before each tool call, one line in the person's language of at
most 12 words saying what it does and why, building on the last result, with the call in the
same reply; related calls share one line. That is Codex's "Preamble messages" rule ("8-12
words for quick updates", grouped, "connect the dots with what's been done so far";
codex-rs/protocol/src/prompts/base_instructions/default.md, read 2026-11-01), except that
Codex leaves trivial reads out and the person asked for every call. Building on the last
result also keeps the line from repeating word for word, which `RunState.note_reply` counts
as a loop. `chat`, every benchmark and the lab's suite holder keep the old line, so their
requests do not change. It changes what the model does; its cost in tokens and whether the
lines stay short are the measurement owed.

**`web_fetch` reads any address** (decided by the person on 2026-11-01: "no bloquees eso, sí
debería funcionar ... y no solo para github, para cualquier dirección"). It read only the
public addresses the conversation held, and a link inside a fetched page only on that page's own
host, as Anthropic's `web_fetch` does since Simon Willison showed links leaking a user's
details through which ones the model followed (2026-07-15,
simonwillison.net/2026/Jul/15/claude-web-fetch-exfiltration/). In one session in
the app the model was shown `github.com/OWNER/REPO/blob/0.7/ENVIRONMENT.md` and asked for its
`raw.githubusercontent.com` copy, which was refused as never seen, as an issue number it had
written itself had been earlier. The same day the person removed the other three
("quita esos bloqueos"): private, loopback and link-local addresses are read (a plain http
address to one is kept, since a local server rarely speaks https, `Guard.is_private`); a
user name and password in an address are sent as basic authentication, and a parameter
named like a secret is sent as written; and `[web] robots` is off by default, so robots.txt
is not asked unless the person turns it on. What `web_fetch` still refuses is what is not a
web address, an address over 2,000 characters, and the hosts the person blocks or leaves
out (`blocked_hosts`, `only_hosts`). `web_fetch` is shown whenever the web is on, no longer
only once an address has appeared. The defences given up are against exfiltration (a model
a page has misled can put what it read into an address it writes) and against a page
steering the model to this machine's or its network's services, including a cloud
instance's metadata address; the shell could already reach both with `curl`. `chat` and
every benchmark register the web tools only with `[web] in_chat`, so their requests do not
change.

**In the app, the person runs what only they can, in a terminal tab beside «Hilo»** (asked by
the person on 2026-11-01: "en caso de que el modelo necesite permisos yo pueda ejecutarlos con
un botón ... como lo hace Claude Code"). In the app's records 6 commands used sudo, doas or
pkexec and 5 got the guard's "sudo cannot ask for a password ... tell the person the exact
command", with nothing that let the person run it. Read before choosing, all on 2026-11-01:
Claude Code Desktop's terminal panel (code.claude.com/docs/en/desktop.md) and its
`run_in_terminal`/`read_terminal` tools as defined in that session (one complete line, the
person approves the exact line, the screen read as data); Cursor's masked sudo prompt, which
its own forum reports failing on Linux (Oct 2025, Jan 2026); Warp's takeover; Codex's `tty`
plus `write_stdin` and OpenHands' `is_input`, which let the model type into prompts. Here the
PTY is in the core (`tools/terminal`, POSIX only): the person's own login shell, its prompt
marked with a per-host nonce, history expansion off, every line typed as a bracketed paste
once the line editor is up. xterm.js 6.0.0 draws it, vendored, inside the page's CSP
(`xterm-csp.js`); node-pty was not taken, since 1.1.0 has no Linux prebuild and would be the
app's first native npm module. `run_in_terminal` takes only a line the person alone can
complete: it raises privileges (sudo, doas, su, pkexec, run0), asks for a password or a login
(ssh, passwd, a `login` subcommand), or a `shell` result already showed it needs a terminal, a
password or root; anything else is refused before anyone is asked, and the model is told to
use `shell` (the person, the same day, after the model had sent ordinary `python3` lines
there: "solo cuando me diga que si yo puedo hacerlo porque no tiene permiso como de sudo"). The
person approves each line, every time; each opens its own tab, since a shell keeps what an
earlier line left in it (zsh's `print -z`, fish's `commandline -r`, a `bind` of Enter, an
alias named `sudo`); the line waits for the shell's first real prompt and is not typed into a
tab the person already typed in. The tool waits up to 60 s for the prompt to come back and
otherwise reports the end later, as a background command does. The app-only sudo refusal now
points at it. `read_terminal` is shown whether a tab exists or not (since 2026-11-10, below). In the window, a shell block in a
reply has a ▶ that runs it in a new tab (a `console` block only its `$ ` lines; no ▶ for a
block that starts collapsed, or one holding control or bidi characters), and the explorer's
`>_` opens an empty tab. Closing a tab ends its shell and everything it started; closing the
session ends them all. Four review rounds found, and the lab now covers: output never flushed
while a program printed without pause, SIGPIPE ignored in every tab, signals sent to a reaped
pid, a line rewritten by state left in a reused tab, prompt marks forged by program output,
replayed output written twice, and the ▶ running lines a `console` block showed as output.
`chat` and every benchmark register no terminal, so their requests do not change. Windows
(ConPTY) is not built; there the window shows no terminal. Whether models hand sudo work to
the person instead of giving up, and how often, is the measurement owed.

**In the app, a Cogito Motoris that died is started again and the request asked again**
(asked by the person on 2026-11-02, "si hazlo"). At 00:25:33 that day the engine failed
mid-reply ("PleReader: a table read failed", on a machine whose RAM flips bits), the
`serve.py` Cogito had started went with it, and every request after met «ConnectError ...
Connection refused» until the person restarted Cogito. Read before choosing, all on
2026-11-03: Ollama notices a dead runner only on the next request, when `needsReload`'s
ping fails, and loads a new one (server/sched.go); llama-swap marks an upstream that exited
on its own as stopped, fails the requests waiting on it, and starts it on the next request
(internal/process/process_command.go); vLLM's API server exits when its engine dies,
unless `VLLM_KEEP_ALIVE_ON_ENGINE_DEATH`, and leaves the restart to its supervisor
(vllm/entrypoints/launchers/launcher.py); no automatic reload was found documented for LM
Studio, and its users report reloading the model by hand. Motoris itself already restarts
its engine on the next request when only the engine dies (it answers 503, serve/server.py),
which the loop's own attempts reach; what nothing covered was `serve.py` going too. Now, in
a core the app started (`_ServedModel(revive=_app_owned())`), a request to a Motoris this
core started that fails with a refused or cut connection (`ConnectError`,
`RemoteProtocolError`, `ReadError`, `WriteError`) while that process has exited has it
started again with the same command (`motoris.Revival`), waits for its health as a first
start does, and asks again (`RevivingProvider`). A request that failed before any chunk
came is asked again there; one cut after chunks were drawn raises with `retry_after` 0, and
the loop's next attempt asks the whole request under a new sample, so the window drops the
cut reply, as for any failed attempt. At most 3 restarts in 10 minutes, after waits of 0, 5
and 15 s (choices, not measurements: an engine that keeps dying must stop being reloaded
and say so); past that, or if it does not come back, the turn ends on the usual
`service_error`. A second request that failed with the first waits for the same restart.
«Detener» ends the wait and leaves the server loading. A reload slower than the 300 s
wait fails that turn and goes on loading, and a later failure waits for it; one that has
not answered 600 s after it started (a choice) is ended, and the next failure starts it
again within the bound. The process, first start and restart alike, is started from a
thread that lives as long as it does, as for MCP servers: the app runs each turn on a
thread that ends with it, and `PR_SET_PDEATHSIG` killed a restart made in a turn when that
turn ended. A server Cogito attached to and did not start, or any other entry, is never
restarted. Every session's window shows
«Reiniciando el modelo…» (`model_restarting`) in the quiet «Continuando…» line while it
happens. `chat`, the suite holder and every benchmark never wrap the provider (the lab
starts llama-server, not Motoris), so their requests do not change. Lab: a fake Motoris
that dies on its first request answers on the second, byte for byte the same request; one
that dies mid-reply is asked again whole; one that always dies is restarted up to the
bound and then shown; an adopted one is never started; an HTTP error or a timeout restarts
nothing; with each call on a thread that is then joined, a restart and a first start
outlive their thread, «Detener» during a reload leaves it loading, a slow reload is
restarted again on a later crash, and one that never answers is ended and started again
(all fail before). Not tried against the real engine: no crash was provoked on the
person's Motoris.

**In the app, an automatic mode answers the guard's questions, and a skill removes dead
code** (asked by the person on 2026-11-03: "agregar un auto mode ... para que ya no me pida
permisos, solo ten bien declarados los guardrails de seguridad que es no borrar archivos
importantes de la computadora o del sistema operativo"; then, choosing between options,
everything asked today, permanent deletions included, and a dedicated skill). Read before
choosing, 2026-11-03: Claude Code's `bypassPermissions` drops its prompts but keeps its deny
rules and still asks before removing "the filesystem root, your home directory, and your
working directory"; its auto mode sends every other action to a classifier model, blocks
"Irreversibly destroying files that existed before the session", and "does not guarantee
safety" (code.claude.com/docs/en/permission-modes; claude.com/blog/auto-mode, 2026-03-24);
Codex's `approval_policy = "never"` drops only the prompts, and Gemini CLI starts a sandbox
in its YOLO mode. Here a switch under the message («Pedir permiso» / «Automático»,
`auto-mode.js`, kept per window) sets `guard.auto.AutoMode` on the window's session
(`POST /sessions/{id}/auto`), and `cli._window_approver` answers yes without a card, drawing
the pill «Automático» and recording `reason: "auto"`, which nothing remembers past the
answer. What the guard refuses is not a question and stays refused: operating-system and
package-owned files, the protected home roots (`~/.ssh`, `~/.gnupg`, the shell's own files,
the trash), the home directory itself and the session's `.cogito/`.

Permanent deletions are not allowed unasked, against the person's first choice. A first rule
allowed them and judged each command by its text, as the guard's card does; three review
rounds that day, each a hunter reproducing commands through the real guard and a skeptic
reproducing every finding again, found 31 spellings that slipped past it with the project,
credentials or a home folder as the loss: a `~/` pattern read as inside the project, filters
that match everything, braces (`{obra,x}`, `{"obra",zz}`), `~+`, `xargs -I{}`, an option
after `rsync`'s destination, a link made earlier in the same command, several patterns that
together match a whole folder. A reading of bash text does not converge. So, of the
questions about files, the mode answers only those whose loss the trash undoes: what a PATH
shim deletes or truncates (`recovery == "trash"`), which reaches the guard with the names
the shell already expanded, in the folder the command really ran in. Everything that
destroys for good (`/bin/rm`, `mv`, `find -delete`, `rsync --delete`, `>`, `git reset`, a
script) keeps its card, which says why. That is Claude Code's auto-mode line ("Irreversibly
destroying files"), drawn by the trash instead of a classifier. Among what goes to the
trash the mode still asks about the project folder or all of it (the shim's `rm -rf *` names
every entry), keys and credentials the platform does not already refuse (`~/.aws`,
`~/.kube`, `~/.config/gh`, KDE Wallet, browser profiles, token files and the like), Cogito's
own configuration and data, a folder directly in the home directory (a link to a second
disk included) or all of it, and what it cannot place (a brace group, `CDPATH`, a variable,
a command that destroys files it does not name). To judge those, `request_for` now says
where each unresolved target is read from (`unresolved_from`) and which operations sweep
without naming (`sweeps`), and a question carries whether a shim put it (`from_shim`): the
loop can read `rm x; mv a b` as "trash" too. A fourth round attacked the shim itself and found
ways to lose files for good, and a fifth round more in the trash and the shell, most of them
in any mode (below), now closed; what they left ends in the trash: a symlink swapped between the check and the move, a decoy entry so that
"all of the project" is not quite all, a wipe split over many calls (the last one still
asks). Package
installs, MCP tools that may change something and lines for the terminal are allowed, as
chosen. There is no classifier: a page the model read can still steer it into an allowed
action, which the trash bounds for files and nothing bounds for the rest. Nothing outside
the window's approver changes: `chat`, the benchmark and every approver they use never see
the mode, and their requests are byte for byte as before. The skill `clean-dead-code`
(`builtin_skills/`) has the model find candidates with the language's own tool (vulture and
ruff F401/F841, knip and `tsc --noUnusedLocals`, staticcheck U1000 and `deadcode`, `cargo
check`; each run on test projects that day), check every name across the project before
touching it, remove one at a time with `edit` or `rm` (so a removed file goes to the trash),
run the tests, and report what it kept and why; it loads on the person's own sentence and 8
of 8 phrasings asking for it, and on 1 of 9 that do not. Lab: what the mode allows and asks
through the real guard and a home of the test's own, every spelling the rounds found,
refusals never reaching it, the approver's events, the answer recorded and read back by the
history, a server tool allowed by it asked again once it is off, the route, the switch and
its order of sends, and no desktop notification for an action it allowed (all fail before).
Shown in the real app on the QA rig: a card with the mode off; «Automático · rm dos.txt» and
the file in the trash with it on; `rm -rf *` still carded with its reason; `/etc/hosts`
refused; the pill read back after a reload. Whether people leave it on, and what the model
does with it, is not measured.

**The PATH shims hold where the shell could pass them by or the trash could be turned**
(found on 2026-11-03 by the automatic mode's fourth and fifth review rounds; deterministic
fixes, in every mode). Each was a way to lose a file for good after an ordinary «Permitir»,
or with nobody asked:

- The broker judged and acted in its own process on the names a shim passed. `rm -rf ''`
  made `Path(cwd) / ""`, the whole folder, and moved it to the trash; `truncate -s 0
  /proc/self/cwd/id_rsa` from `~/.ssh` was judged and copied as the core's folder while the
  real `truncate` emptied the key, past the guard's refusal of `~/.ssh`; `/dev/fd/../cwd/x`
  did the same once that was refused. `ShimBroker._decide` now refuses, before anyone is
  asked, an empty target, `.` or `..` (as rm does), a lone `-` (truncate opens a file by that
  name; the broker had skipped it as an option, judging and saving only the rest), and a
  path that passes through `/proc` or `/dev/fd`, walked one part at a time as the kernel
  walks it, a link followed before the `..` after it. rm's own last-part link is not
  followed: it removes the link. `rm -f` of what is not there now succeeds, as GNU rm does,
  instead of failing `rm -rf dist && build`.
- The trash wrote each `.trashinfo` with `write_text`, which follows a link a command put at
  that name, and copied into `files/` the same way: two allowed commands overwrote a key in
  place. The info file is now created with `O_CREAT|O_EXCL|O_NOFOLLOW` and its name is the
  claim, as the Trash specification 1.0 has it; a copy goes to a file made new the same way;
  a name is free only when nothing, not even a dangling link, is there; and a `Trash`,
  `files` or `info` that is a link or not the person's own is refused.
- `export PATH=/usr/bin:$PATH`, `hash -p /usr/bin/rm rm`, an alias or a function named `rm`
  in one command sent a later plain `rm` to the real binary while the guard left it to the
  shim. The agent's shell now puts the shims first again, clears the hash table and drops
  such aliases and functions after every command, silently, on the line that closes it (an
  alias is expanded when a line is read). A command that plainly changes which `rm` runs
  (PATH assigned, exported or declared, `hash`, `alias`, `enable`, `eval`, `exec`,
  `BASH_ENV`, a function by that name) is no longer left to a shim: the loop asks, and the
  card does not promise the trash. `source .venv/bin/activate && rm -rf dist` still is.
- A shim word that starts with `~`, `$` or a backtick is a name the shell already expanded;
  the guard read it as the home directory or a variable (a link named `~` to `~/.ssh` passed
  that way, and a stray file named `~` could not be removed). It is judged as `./~` now.
  Undo of a trashed link works again (the copy was taken for gone when its target was).

**Open: a model can still set its shell up so that a plain `rm` skips the shim.** The guard
leaves a plain `rm` to the PATH shim, which asks and moves to the trash; the seventh review
round showed the agent's own bash can be made to run the real binary instead, with nobody
asked, in every mode: `readonly PATH` (the restore then fails), a `trap ... DEBUG` that puts
`/usr/bin` back before each command, a function or alias named `unset` or `unalias` that
disarms the restore, `BASH_ENV` for child shells, and names built at runtime that no text
pattern sees. This predates the automatic mode, which changes nothing about it: the mode
answers only what reaches the shim. Restoring state in a shell the model controls cannot
hold. The fix is a design change, measured under section 7: either the shims bind-mounted
over `/usr/bin/rm` and the rest in the agent shell's own mount namespace (bubblewrap is
already a dependency), so no lookup can miss them, or no deferral at all, with the loop's
answer handed to the broker as a one-time grant.

- A name the shell left holding `*`, `?` or `[` (a link called `k*` to `~/.ssh`, then `rm
  'k*/id_ed25519'`) was read by the guard as a pattern it could not place, which passed the
  refusal of `~/.ssh`. The broker now judges such a name again as the path it really is.
- An undo or rewind put back whatever held the trash name now: once the person had emptied
  the trash and thrown away something else by that name, their own item was moved into the
  project and dropped from their trash. A restore from the home trash now needs the
  `.trashinfo` to still name where the entry came from. And a trashed link is recorded
  where the link was, not where it pointed, so the desktop's «Restore» puts it back there.

What the sixth round left is a race with a second process (a copy across devices while the
folder is written to, a name taken between the claim and the move) or a file emptied in
place, whose copy is in the trash. Lab: each through the broker's socket or a real shell
with the shims (all fail before).

**In the app, the browser is the app's own Chromium, in tabs beside «Hilo», and the model
can read the page whole** (asked by the person on 2026-11-04 and 2026-11-05: "algunas
páginas no me dejan acceder ... porque es una versión de test"; "que lo abra en otra
pestaña, no en una ventana adicional"; "que el modelo pueda ver el html, los script de la
página, prácticamente todo"; then, offered Edge, Brave or Chrome: "necesitamos integrarlo
nativamente con el proyecto ... el instalador tiene que agregar ese navegador, y las tabs
... se tiene que abrir dentro de la aplicación como lo maneja codex o claude code"). The
`browser` tool drove Playwright's Chrome for Testing build in a window of its own; sites
read it by `navigator.webdriver` (Playwright's `--remote-debugging-pipe` sets it), brands
without a browser of their own and two test-only switches (Playwright 1.64.1 probed against
public bot checks, cogito-lab research/2026-11-05-native-browser, read 2026-11-04). Read
before choosing, 2026-11-05: the Claude desktop app's browser pane and ChatGPT's built-in
browser both keep a profile apart from the person's and approve sites; Cursor reads console
and network; none says how it embeds. A screencast of a headless browser into a tab was
designed and set aside: the person asked for a native one, and headless keeps its own
tells. So each tab is now a `WebContentsView` in the app window, on Electron 44.4.3's
Chromium 152, which the installer already carries, with a persistent partition per
project, and the core reaches it only through named operations (`browser_command` events
on the session's stream, answered by the main process on
`POST /sessions/{id}/browser/answers/{id}`). The probes decided the security: a debugger
attached to a view could close Cogito's window (`Target.closeTarget`), drive its UI
(`Target.attachToTarget` and `Runtime.evaluate`) and read its cookies (`Storage.*`), and
CDP `Page.navigate` reached `file:`, `chrome:` and `devtools:` past `will-navigate`. So
main never forwards a protocol method or session id; each operation builds its own calls
from a frozen set, every scheme but http(s), ws(s), data, blob and about is cancelled in the
partition's `webRequest`, permissions are denied, certificate errors refused, downloads kept
under the project's `.cogito/downloads` (a linked folder refused), and no debugging port or
pipe exists. The user agent loses the app's name and `Electron/x` and reads
`Chrome/152.0.0.0`, as Chrome's reduced form; brands stay what Chromium reports, with no
borrowed «Google Chrome». The snapshot, refs, clicks and typing keep the old output
through Playwright's own injected script (1.64.1, Apache-2.0), vendored and run in an
isolated world, so the model sees the same lines as before. New actions read the document
as sent, the live DOM, scripts and their source, the network log and a response's body, the
console and exceptions, cookies and storage, and run JavaScript (REPL semantics, as
chrome-devtools-mcp's `evaluate_script` and Claude's `javascript_tool`); long results go to
the loop's budget and artifacts. Popups and `target=_blank` open as tabs keeping
`window.opener`; a dialog is dismissed and reported; a tab the model opens joins the strip
without taking the focus; app shortcuts reach the app while a page has focus. Playwright
leaves the dependencies. The person then asked that the model use it ("recuerda poder hacer
que el modelo utilice ese navegador para abrir páginas o manejarse a través del DOM"): in
the app's records, outside one project the person keeps out of reading, models had called
`browser` 0 times against `computer` 14 and `web_fetch` 10. So `browser`'s description now
opens by saying it is how to open a page for the person and to use a site, by its elements'
refs rather than screen coordinates; and, only where `browser` is registered,
`web_fetch` adds that a page to show or to use is the browser's and `computer` that a web
page is. `chat` and every benchmark have no `browser`, so both descriptions are byte for byte
as before. Whether models now reach for it is the measurement owed. What stays detectable: Chromium 152 is four majors behind stable,
`window.chrome` is empty, and Google's sign-in refuses embedded browsers. Lab: the mediator
never sends a method outside its set whatever it is given, the scheme block, the user agent,
the transport (token, late and duplicate answers, size, cancel), the tool's actions against a
fake bridge, the window's strip and pane, and a real Electron run of every operation (all
fail before). `chat` and every benchmark register no browser, so their requests do not
change.

**In the app, the model can hand a question to a subagent** (asked by the person on
2026-11-06: "revisa si puedes crear sub agentes, si esto va aquí o va dentro de motoris",
then "Sí, constrúyela"). It goes here: choosing, running and recording a subagent is the
harness's work, and Cogito Motoris only serves the requests. Read before building, all on
2026-11-06: Claude Code's subagents start in a window of their own from a task the parent
writes and return only a summary (code.claude.com/docs/en/sub-agents); Codex stops at depth
1 (`DEFAULT_AGENT_MAX_DEPTH`, codex-rs/core/src/config/mod.rs, "Agent depth limit reached.
Solve the task yourself."); in Gemini CLI a subagent is a tool with its own history and
cannot call another. Motoris serves one request at a time unless its config sets
`"parallel": N`, and its own docs measure a lone request 11-24% slower with slots on a 12
GB card (cogito-motoris docs/BATCHING.md, their numbers), so a subagent runs inside the
call and needs no Motoris change. `delegate` (`tools/delegate.py`) takes a
`task`; `Engine.subagent` builds a second engine on the same model, one level down, with
the `read_only` scenario the registry already had: no `delegate`, nothing that `mutates`,
and nothing that `operates` (a new `ToolSpec` flag: `browser`, `computer`, the voice tools
and `check`, which act on what the person sees or run the project's linters but change no
file the loop tracks, so they are not `mutates`, which would reset the loop's repeat
count). Its task opens with harness text saying the person does not see it and to report
paths, line numbers and exact values in under 400 words; it has 30 steps
(`DELEGATE_MAX_STEPS`, a choice, since the app's own turns have none). It runs in a child
session of the same journal, linked by `DELEGATED_TO`, which the thread and the reopening
already left out; its calls reach the window with `parent_call_id` and are drawn inside the
`delegate` row («Subagente: Leyendo src/x.py», then «N llamadas»), and the row opens to the
findings. A review of the change found, and the lab now covers: the subagent's `read` marked
files read for the parent, which then overwrote a file it never saw past `write`'s guard (it
now gets a `read` of its own); an MCP tool that may change something ran when the subagent
named it, since a named MCP tool runs though not shown (`_runnable` now keeps to the run's
catalog); a fold inside the subagent left a session the app could reopen as the person's
conversation after a crash (the fold is linked as delegated too); «Continuar» on the budget
was asked again inside the subagent and «Detener» there did not end the turn (it asks
through the parent now); its `done` phase reached the window mid-turn; a stopped subagent
returned the harness's stop sentence instead of its last words; it was told it had a shell
and to write a line before each call; and it was shown `recall`, which searches the
parent's conversation. Several at once came the same day ("intenta meter varios en
paralelo"): the `delegate` calls of one reply run at the same time, up to
`DELEGATES_AT_ONCE` (3, a choice, under Codex's default of 6 open agent threads,
`agents.max_threads`, developers.openai.com/codex/subagents, read 2026-11-06), and the
tool's description then says so. As the step reaches a `delegate` call,
`Engine._run_delegates_together` takes it and the `delegate` calls right after it, no more
than run at once, opens them in order (row, guard), runs each on its own thread, and the
step's loop then closes each in turn, so a call the model wrote before them has already run
and what is recorded and sent back is in the model's order. What the subagents share is safe
across threads (the event counter, the journal, the provider's HTTP client, the window's
channel); the budget is asked behind a lock, so «Continuar» is asked once. A review found,
and the lab now covers: all of a step's delegates ran before the calls written ahead of them
(a subagent asked to read a file the same reply wrote found none); their phases overwrote
each other and a budget card's (they are not sent while subagents run together); two calls
a server sent under one id swapped findings and ran one subagent twice (results are kept by
call, not id); and rows beyond the threads spun and counted their wait. A second round found:
after a lone subagent the parent's next call ran under the subagent's last phase, and after
«Continuar» on a budget card the window stayed amber while subagents ran together (the
parent's phase is sent again in both); subagents running together shared `web_fetch`'s
fallback to the last search, so one could get passages chosen by another's query (each
subagent has its own web tools on the session's client and caches); and a reopened thread
lost the row's count of calls (it is recorded with the call). A subagent that answers
first says so in its row («Subagente terminó»), and the rows end together. On a Motoris
without `"parallel"` their requests still queue there, one at a time, and it parks each
conversation in RAM for its next request (docs/DETAILS.md, their design), so what overlaps
is the tools' time, not the model's; on Claude the requests run together. Shown in the real
app on the QA rig: two subagents of 14 s and 4 s ended in a 14 s turn, and «Detener» at 4 s
interrupted the one still working. Not done: subagents that edit, and a per-subagent stop
(Detener stops the whole turn). Its cost on Motoris is the prompt cache: the subagent's
system prompt is read cold (its tool list differs from the parent's), and its steps can push
the parent's checkpoints out, so the parent's next step may read its history again. Its
measurement is owed: the suite has no delegation tasks, and what it costs per app turn
(tokens, wall time, how often the parent's prefix is read again) and whether models call it
are not measured. `chat`, the lab's suite holder and every benchmark register no
`delegate`, and the parent's own requests are unchanged apart from its definition.

**In the app, each request starts with the one before it** (asked by the person on
2026-11-10, "Adelante con el plan", the first phase of cogito-lab
research/2026-11-10-single-model-review). In Motoris's log of the 256K era, reading the
prompt was 46% of the model's time. 48% of that went to requests that resumed only at a
16K periodic mark, and 33% to requests in the same conversation that reused nothing. Motoris
checkpoints a prompt where its last assistant turn starts, and keeps 6
(cogito-motoris docs/DETAILS.md, "Conversation cache", read 2026-11-10). Qwen's chat
template renders the tool list before everything else. Four changes, each only in a core the
desktop started (`_app_owned`), so the lab's suite holder, which runs `serve` too, is
unchanged:

- No status message ends the request (`LoopSettings.status_turn`). In the app it held only
  the clock and the branch, and the next request no longer had it, so the turn checkpoint
  never matched and the message-boundary checkpoint Cogito asks for was the first one
  evicted (887 of 890 misses followed a request holding 6 checkpoints). The date and the
  branch are now in the system message. It changes once a day, or when the branch changes.
  A status is still sent when it has a step or token count or a task list, which the app
  never has.
- A local model's MCP list is fixed for the conversation (`McpHub(stable=True)`, for any
  entry that is not Claude). The tools the person's first message ranks stay shown until a
  fold. `mcp_find` loads nothing. It returns its three best matches with their arguments,
  to be called by name, and a result that names another tool of its server says how to
  call it. Such a call ran already (`McpTool.mcp`, `repair.xml_tool_call`). A fold
  re-chooses the list once, from what was called and found (`refresh`). Claude keeps the
  moving list, since it calls only what it was shown.
- `ask_document` is shown from the start with a description that never changes. It used
  to appear with the first document and name every document after it. Before any document
  is there, a call says so (`DocumentAskTool(stable=True)`). `read_terminal` is shown
  whether a tab is open or not, and it exists only in the app.
- The fold's handover is asked for with thinking off (`BudgetSettings.fold_without_thinking`).
  177 of the 185 replies the log shows cut by `length` were handovers (`max_tokens`
  1,400). Clearing the composer's level alone had also left «Más»'s raised budget on.

Lab: an app session's requests each start with the one before, the same system message,
and no status; `chat` still ends with its status; a stable list survives later messages,
calls and searches, changes at a fold, and `mcp_find` and a result give the arguments;
`ask_document` and `read_terminal` are listed before their first document or tab; the
handover goes with `enable_thinking` false and no budget (all fail before). `chat` and every
benchmark keep the status message, the moving MCP list and their folds, so their requests
do not change.

Measured on Motoris the same night (cogito-lab experiments/2026-11-10-phase1-prefix). One
scripted conversation was replayed through a desktop-owned session on a Motoris started
fresh for each run, ABBA. Each request went to Motoris whole, with the replies scripted so
both sides built the same conversation:

| replay | main: tokens read, read time | this change |
|---|---|---|
| 20 requests, to 34K | 66,045, 60.5-61.8 s | 34,274, 41.7-43.6 s |
| 71 requests, to 168K | 202,784, 226.0-232.4 s | 168,306, 191.5-195.9 s |

Almost all of it is the request after `mcp_find`: main read 28,851 tokens again, which took
17.5 s at 168K, and this change read 91. Dropping the status message saved 50-70 tokens a
request. The 16K-mark resumes seen in the person's log did not occur in the replay, so this
change is not shown to fix them. The handover took 9.0-9.7 s with thinking off, against
18.1-18.6 s with it on, where one of three was cut (three runs each; a first cold read
excluded).

This changes what the model sees. The suite measurement is owed: no benchmark run of
qwen3.8-flash-motoris exists yet to compare against.

**In the app, `computer` runs several actions in one call and opens programs** (asked by
the person on 2026-11-10: "que pueda manejar el modelo las aplicaciones ... abrir
programas", fast, without needing their attention). In the app's records, one project left
out, a Motoris step took a median 20 s and `computer` 0.8 s, with 0.07 s of harness between
them (cogito-lab research/2026-11-10-single-model-review/TOOLS-PLAN.md), so the lever is
fewer model steps. Read on 2026-11-10: Anthropic's docs for `computer_toolset_20260801` run
several calls of one reply in order, "stop at the first failure", and put one screenshot on
the last result; OSWorld-Human (arXiv 2506.16042v2) puts 76-96% of Agent S2's task latency in
planning and reflection, and people's grouped actions took fewer steps (LibreOffice Calc
13.2 to 4.5, their Table 4; their numbers). In `tools/desktop`:

- `actions`: up to 8 (a choice) clicks, keys, typing, scrolls, moves, drags, waits and
  launches, run in order with one screenshot at the end. The list is checked before anything
  runs; the first action that fails stops the rest, and the result names it, says which were
  not run and still shows the screen. The single-action form is unchanged.
- `launch` (`launch.py`): a program by its desktop entry, as a menu finds it (its ID, its
  name in any language, the last part of a reverse-DNS ID, its generic name, then part of its
  name), started with `gtk-launch`, else `kioclient exec` or `gio launch`; a file or address
  goes to `kioclient exec`, `xdg-open` or `gio open`, in a session of its own. Several equal
  matches start none and are listed. Focusing a window by title is left out: on KWin it means
  a script over KWin's D-Bus, which goes around the portal that asks the person.
- Screenshots are PNG at the default level, not `optimize=True`: on a 1280x720 image of text
  lines, 12 ms instead of 76 ms at the same 134 KB; JPEG at 80 took 2 ms but 275 KB and
  blurred the text (synthetic, ours). Keys, typing and moves settle 0.15 s instead of 0.4 s.

The description says to batch and that `launch` opens programs; it costs about 300 more
tokens a step. Typing still sends two portal calls a character. Only a core the desktop
started registers `computer`, so `chat` and every benchmark are unchanged. Lab
(`tests/tools/desktop/test_computer_night_*.py`, a fake portal, the test's own XDG folders
and an injected launcher, so nothing touches the real desktop): batches, failures, bad lists,
a stop, program lookup and its ambiguities, files and addresses, the PNG encoder (all fail
before). Owed: whether Qwen3.8-Flash batches, and steps and wall time per desktop task before
and after. Not tried against the real portal.

**In the app, `browser` runs several actions in one call, reads a page as text and waits for
its DOM to settle** (the same request, 2026-11-10). A browser step on Motoris averaged 43 s
and about 2,500 thinking tokens; an operation takes milliseconds. The interactive snapshot
dropped paragraph text and every link's URL, so reading a page took `evaluate`; a click
waited a fixed 300 ms, so an app that renders after a fetch was read before it had; `scroll`
answered with an image. Read on 2026-11-10: Playwright MCP's README (`browser_fill_form`
fills several fields in one call) and its backend's `waitForCompletion` (a settle, the
navigation's load, the requests the action made, then a settle again); Stagehand's speed
guide. Main's frozen method set is unchanged:

- `steps`: up to 10 (a choice) click, type, select, press, check, scroll and wait, run by
  main as one operation, checked before the first runs, stopping at the first failure or at a
  step that leaves the page, and answering with the page as it is. `check` sets a box or
  radio through its label when the box is hidden.
- A DOM-settled wait replaces the guesses: a MutationObserver in the isolated world (child
  lists and text, not attributes), polled by main every 50 ms; quiet for 250 ms with no fetch
  or XHR from the action still open, at most 1.5 s (choices). In a real Electron 44.4.3, an
  app that renders after a 700 ms fetch was read at 59 ms without its content before, and at
  967 ms with it now.
- `read`: one fixed function in the isolated world returns the main content as text
  (`<main>`, else a lone `<article>`, else the parent of most paragraph text, else the body),
  headings, lists, tables and `<pre>` kept and links as `[text](url)`, paged by 6,000
  characters. The rule is ours, checked on test pages.
- The snapshot comes inside the action's own operation (one round trip, not two), keeps each
  link's `/url:`, and `scroll` and coordinate clicks show it instead of an image.

The definition grew by about 200 tokens a step. Only a core the desktop started registers
`browser`, so `chat` and every benchmark are unchanged. Lab:
`tests/tools/test_night_browser_tool.py`, `tests/desktop/test_night_browser_main.mjs` and the
real-Electron `tests/desktop/e2e/test_night_browser_host.mjs` (fail before; no method outside
the set reaches the debugger). Owed: steps, tokens and wall time on the same browser tasks
before and after, and `read` on real sites.

**In the app, MCP servers start without holding anything up, and the model can add one**
(asked by the person on 2026-11-10: "el mismo soporte que tiene claude code para los mcp
... que el mismo pudiera instalarlos y configurarlos al yo de decirle"). A server not ready
within `connect_wait` silently had no tools for the whole conversation (`hub.failures` was
never read); adding one in Conexiones blocked up to 120 s, polling stopped after 3 minutes,
and every start waited for `tools/list`. Read on 2026-11-10: Claude Code's MCP docs (the
model runs `claude mcp add [--scope] ...` in its shell; project scope in `.mcp.json` with
`${VAR:-default}`; placeholder secrets are the person's to fill), and Codex's
(`startup_readiness = "catalog"`, a cached tool list satisfying startup). Only in a core the
desktop started:

- Late or failed servers: the model is told once, in a harness note after the person's
  message, which servers are still starting or failed and why; a server that becomes ready
  joins at the person's next message or a fold, and only then, since a tool-list change makes
  Motoris read the whole prompt again. A call to a server still starting waits for it within
  `start_timeout`. A start that times out quotes the server's last lines.
- Conexiones answers at once («Iniciando…»), polls with no time limit, shows the start log,
  names values left to fill in, and takes the name from the pasted line or JSON.
- Each server's tool list from its last start is kept (`tools/mcp/catalog.py`, owner-only,
  named by a hash of the start command, so no key is written and a changed command never
  reuses an old list), and a conversation registers those tools at once.
- `${VAR}` and `${VAR:-default}` work in a stdio server's command, args and env (this part
  also in `chat` with `[mcp] in_chat`).
- `mcp_add` (`tools/mcp/install.py`): a command, an address, a `claude mcp add` line or JSON,
  an optional name and a scope (`user`: config.toml; `project`: the project's `.mcp.json`).
  A missing or placeholder key saves nothing and the model is told to ask the person; the
  project scope refuses a literal secret. The person is asked every time, automatic mode
  included (`ExternalTool.every_time`, `MCP_SERVER`): it saves a program that runs in every
  later conversation, chosen by text the model may have read on a page. The card shows the
  exact command and where it is saved, env names but never values. The `extend-cogito` skill
  tells the model to use it. It costs about 185 tokens a step.
- «Importar…» lists, read-only, the servers Claude Code, Claude Desktop, Cursor, Gemini CLI
  and Codex have configured and the project's `.mcp.json`, naming env variables and headers
  without their values; ticked ones are copied into config.toml. A project's `.mcp.json`
  server runs only after the person approves it, by a hash kept outside the project, so a
  changed entry is asked about again.

Lab: `tests/tools/test_night_mcp.py`, `test_night_mcp_add.py`, `test_night_mcp_import.py`,
`tests/desktop/test_night_mcp_ui.mjs` (all but one fail before; that one checks `chat` is
unchanged). Shown in the real app on the QA rig: the "still starting" note and the late join,
cached tools registered at once after a relaunch, the start log, an add without a name, the
`mcp_add` card and the new tool called in the same turn. «Importar» was tried only on fixture
files. Owed: how often a conversation starts with a server not ready, and whether the note
changes what the model does.

**In the app, a subagent reuses the parent's cached prompt, and one can write in a copy of
the project** (asked by the person on 2026-11-10: "quiero una buena efectividad así como lo
maneja claude code, que puedan atacar una tarea varios procesos de pensamiento"). Read on
2026-11-10: Claude Code's subagents (code.claude.com/docs/en/sub-agents.md: own context, a
tool allowlist, a writer in a temporary git worktree, and a fork that keeps the parent's
system prompt and tools "so its first request reuses the parent's prompt cache"); Codex's
`explorer` and `worker` roles (learn.chatgpt.com/docs/agent-configuration/subagents);
cogito-motoris docs/BATCHING.md and DETAILS.md. Measured on a private Motoris (port 8091,
fresh per run, cogito-lab experiments/2026-11-10-subagents), two subagents of four requests
each, two runs a side: the subagents read 23,179 prompt tokens before and 11,835 now, and
finish in 30.1 s before and 23.2 s now; the parent's next request read 4,441 tokens cold
before and 233 now. Run together on one slot they took 23.2 s against 25.8 s one after the
other. `"parallel": 2` does not start with the person's config (the slots cut the expert
cache from 2,040 to 526 and the draft head no longer fits); without the draft head two slots
were 62% slower than one, so Motoris keeps one request at a time on this 12 GB card. Only in
a core the desktop started:

- A read-only subagent is sent the parent's system message and tool list
  (`Engine(listed_tools=...)`); a tool it may not use stays listed and is refused when called
  (`NotForSubagent`, `not_for_subagent`), and its step limit is in its task, not a status
  message, so the server's checkpoint matches. The parent's `todo` is listed and refused too.
- The task asks for `Findings:`, `Not found:` and `Open questions:`, and says no line is
  needed before a call.
- The parent is told how many subagents really run at once on the serving model (a Motoris
  entry's config, `config.models.motoris_parallel`): one request at a time, so what overlaps
  is their tool time.
- `kind: "write"`, offered only in a git project with a commit and a working bubblewrap: the
  subagent works in a `git clone --shared` of the current state (uncommitted and untracked
  files included) under Cogito's cache, its shell in bubblewrap writing only the copy and
  with no network, its file tools confined to the copy (`Guard(confine_to=...)`), and nothing
  deleted in it. The parent gets a patch in its artifacts to apply with `git apply --3way`,
  and the files the writer deleted by name. The person's tree and `.git` are never touched.

Lab: `tests/cli/test_night_subagent_prefix.py`, `tests/tools/test_night_subagent_workcopy.py`,
`tests/cli/test_night_subagent_writer.py` (fail before); `tests/cli/test_delegate_app.py` now
expects the parent's list and system message. Not shown in the real app. Owed: whether
models write good briefs, how often they call a refused tool, and how often a writer's patch
applies.

**In the app, a subagent at its limit is asked for what it found, and its row lists its
calls** (asked by the person on 2026-11-11, after a subagent made 42 calls in 2 min 47 s,
reached its 30 steps and returned nothing: "me gustaría poder ir viendo lo que hace"). Read
on 2026-11-11: Claude Code returns such a subagent's output "marked as partial" and lets
the person open a subagent's transcript (code.claude.com/docs/en/sub-agents); Gemini CLI
gives one "final recovery turn" after `max_turns` or its timeout, telling the model to
finish without other tools (packages/core/src/agents/local-executor.ts,
`executeFinalWarningTurn`); Codex lets the person switch to a subagent's thread
(`/subagents`). Two changes:

- After a stop at a limit (steps, time, a loop, failing calls; never «Detener» or a provider
  failure), `cli._last_word` sends one more request: the same tool list, so the server
  reuses the prompt it read, and a message saying to call no tool and answer with what was
  found. An answer goes back to the parent as a result that says it may be incomplete
  (`Delegated.partial`); a tool call or silence is reported as before. One extra request,
  never two.
- The `delegate` row opens while the subagent works: one line per call as its own row
  would say it («Leyó src/x.py · 212 líneas», a failed one in red), the newest 200, kept
  open across the redraw each call causes, and above the findings once it ends. A reopened
  thread shows the count only: the list is not recorded.

Lab: `tests/cli/test_subagent_last_word.py`, `tests/desktop/test_subagent_steps.mjs` (fail
before). Shown in the real app on the QA rig: the list open at 13 s with four calls, and a
30-step subagent ending with its findings. Only a core the desktop started registers
`delegate`, so `chat` and every benchmark are unchanged. Owed: how often a real model
answers when asked at its limit, and how good that answer is.

Measured the same day and rejected: read-only subagents with thinking off. On three
research tasks of three subagents, five repeats each, both arms scored 45 of 45; without
thinking the median and mean wall time were higher on all three tasks, the subagents made
two to three times the calls, and in 7 of 15 runs the parent delegated again (cogito-lab
experiments/2026-11-11-real-tools).

**In the app, the model keeps a task list, and a turn does not end with items open** (the
person on 2026-11-10: "algo que pueda dejar automatizado para que el modelo trabaje sin
necesitar toda mi atención"). `todo` (`tools/todo.py`) takes the whole list each time, each
item pending, in_progress or done, records `todo_set` for the graph's Todo projection, and
shows the list back in its result, so nothing is added to the system message or a status
turn and the cached prefix holds. A turn that would end while items are still open is sent
back once, naming them (`LoopSettings.open_tasks_notice`, `_OPEN_TASKS_NOTICE`), and a fold
puts the list after the handover. A subagent does not get the parent's list. Read on
2026-11-10: Claude Code gives its model the same kind of list (TodoWrite, and the Task tools
replacing it, code.claude.com/docs/en/agent-sdk/todo-tracking.md) and Cursor's agent keeps
one (docs.cursor.com/agent/planning); no controlled measurement of what a list is worth was
found, so this is a hypothesis. The harness checks the list instead of a prompt rule telling
the model to finish it (§1b). Only a core the desktop started registers `todo`. Lab
(`tests/tools/test_todo_night.py`): the list, the status words models use, the record, the
turn sent back once and only with items open, `chat` without it, the fold seed (all fail
before). Owed: whether long app tasks finish more often with it.

**Automatic mode keeps session tools allowed, and a dropped event stream is followed
again** (2026-11-10, deterministic fixes). In automatic mode a `computer` or `browser` call
was asked about again on every call and allowed again by the mode: one more pill and two
more events each time. The guard now keeps such a session-only grant while the mode is on
(`Guard.auto_on`), asks again once it is off, and never saves it; server tools are still
answered call by call. The mode's brace and CDPATH checks also read a tool's arguments as a
shell command, so an MCP search for `*.{js,ts}` kept a card up; they now apply to shell
questions only. Separately, when the session's event stream ended without the core's
`close`, or failed while the core was alive, the window stopped hearing the session until the
app was restarted; main now attaches again after a second (the events wait in the session's
queue, `server/events.py`, and a replayed one draws nothing twice), and shows the error after
five tries in a minute. Lab: `tests/guard/test_auto_mode_night.py`,
`tests/desktop/test_night_stream_reattach.mjs` (fail before); the supervisor test that
expected the error now expects the session followed again.

**The connection form lists only the Claude API** (decided by the user on 2026-10-31: "de
momento en este listado solo deja la API de Claude"). The other services stay defined in
`connect.PROVIDERS`, so an entry saved with one keeps working; `SHOWN_PROVIDERS` decides
what «SERVICIO» offers, and the window still adds Cogito Motoris itself.

Every configuration runs the suite 5x, and a comparison needs all five repeats of every
task in both runs. A run recorded when this rule required three is brought to five by
`bench extend --to 5`. It runs repeats 3 and 4 of every task only where the run's own
header was recorded (its Cogito revision on a clean tree, the same instrument, host,
workspace, sandbox, graph, toolchains, Cogito configuration, Cogito Estella version, model,
seed, prompt time and worker count). **Every three-repeat run that enters a decision is
extended to five, whatever a legacy read of it showed.** `--legacy-repeats` can read such
a run at three, labels the result as not satisfying this section, and exits 3. What it
shows never decides whether a run is extended: extending only the runs whose three-repeat
reading disappointed would choose the sample after seeing it. A run with a dirty git tree
can never be a baseline. `bench compare` still reports the exact McNemar test over the
tasks that agreed with themselves (the statistic this rule replaced), labelled as
secondary information. It no longer decides anything.

---

## 7b. The README

`README.md` is the page a stranger reads first, so it stays short and true.

- **Concise.** What Cogito is, what makes it different, how to install, how to develop.
  No measurement narrative, no history, no roadmap; those live elsewhere.
- **Updated in the same commit as the change that makes it stale.** A feature that lands,
  a default that changes, a command that is renamed: the README moves with it, never in
  a later clean-up. An accepted improvement is not finished until the README says so.
- A rejected change never reaches the README. Only what shipped is described.

---

## 8. Commands

```bash
uv run cogito doctor                        # check the GPU, Neo4j, Estella, Motoris
uv run cogito setup                         # first-run provisioning, real sizes shown
uv run cogito models list                   # the selectable catalog, grouped by origin
uv run cogito chat --model <id>             # headless core
uv run python -m cogito.dev.check_layers    # imports flow downwards only
uv run python -m cogito.dev.check_tree      # nothing tracked outside the folder map
uv run python -m cogito.setup.catalog       # the component catalog a release may ship
python3 desktop/packaging/bundle-core.py linux-x64   # the bundled core for one target,
                                                     # before electron-builder

cd desktop && npm start                     # the app against the loopback core
```

Install the hook once per clone:

```bash
git config core.hooksPath .githooks
```

Benchmarks and tests run from `../cogito-lab`.
