"""Runtime settings, with deliberate defaults.

Numbers here come from measurement or from the research the design rests on; where a value
is a guess, the comment says so. Anything tuned later is tuned here, in one place, so a
benchmark run can record exactly which configuration produced it.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from cogito.config import paths
from cogito.errors import ConfigError

#: Per-tool output ceilings in tokens. The loop applies them to every tool, so none can
#: forget to clip. Output over the ceiling is offloaded to an artifact and replaced by a
#: pointer. Without this, tool observations grow to 70-80% of the window.
DEFAULT_TOOL_BUDGETS: dict[str, int] = {
    "shell": 2000,
    "read": 6000,
    "write": 400,
    "edit": 600,
    "grep": 1500,
    "glob": 800,
    "codegraph": 1200,
    "ask": 900,
    "recall": 1000,
    "browse": 800,
    "todo": 100,
    "delegate": 1200,
    "voice_listen": 400,
    "voice_speak": 300,
}


#: How much the model may think in a turn, chosen in the app's composer (the turn's
#: ``thinking``; `cli._with_thinking`). Absent, or "normal", is the session's own budget,
#: `max_reasoning_tokens_per_step`; on Claude "normal" also turns thinking on at the model's
#: own effort. "off" turns thinking off through the chat template, "long" allows
#: `long_reasoning_tokens_per_step`, "free" sends no budget, so the server's own rule
#: applies (Motoris keeps a quarter of `max_tokens` for the answer).
THINKING_LEVELS = frozenset({"off", "normal", "long", "free"})


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    """Stop conditions. Enforced in exactly one place: loop.state.Budget.check."""

    #: A turn's step and time limits; 0 means none (the app: the person stops a turn).
    max_steps: int = 60
    wall_clock_seconds: float = 1800.0
    #: Only shown to the model as tokens left, never a stop; 0 shows nothing (the app).
    max_total_tokens: int = 400_000
    max_output_tokens_per_step: int = 4096
    #: How much of `max_output_tokens_per_step` one reply may spend thinking before
    #: llama-server closes the thought, leaving the rest for the answer
    #: (`provider.openai_api.OpenAIProvider._reasoning_budget`). Only for an entry Cogito
    #: serves, and only for a model its server says thinks or that has been seen to
    #: reason; 0 switches it off.
    #: Measured on gemma-4-12b's quick run at a2653e6, from its WAL: of the samples that
    #: closed their thought and called a tool, 95% thought for at most 1,799 tokens; of
    #: those that thought past 2,048, 124 of 165 (75%) never closed it within 4,096 and
    #: ended with nothing; its largest visible answer was 1,249 tokens, so half the step
    #: fits any answer it gave.
    max_reasoning_tokens_per_step: int = 2048
    #: The thinking budget when the composer asks for more ("long", `THINKING_LEVELS`).
    #: A choice, not a measurement: four times the default, under the app's per-step output
    #: cap on a 32K window (10,922).
    long_reasoning_tokens_per_step: int = 8192
    max_repair_retries: int = 2
    #: How many times *per turn* a reply cut off at `max_output_tokens_per_step` with
    #: nothing usable in it (no text and no tool call, or only a call the limit cut off,
    #: which is never run) gets a notice and a new sample instead of being read as the
    #: model finishing. Past this limit the turn just ends. See
    #: `loop.engine._TRUNCATED_NOTICE`.
    max_truncation_notices: int = 2
    #: APP-ONLY: how many times *per turn*, once `max_truncation_notices` is spent, a
    #: reply the output limit still cuts off (call or text) continues with another sample
    #: instead of stopping on `StopReason.OUTPUT_LIMIT` and waiting for the window's
    #: «Continuar». 0 keeps today's behaviour exactly: the turn stops at the limit and the
    #: button is the only way on. `chat` and every benchmark task never set this, so their
    #: requests are unchanged; `cli._serve_factory` sets it to 3 for the app only (same
    #: pattern as `cli._app_output_cap`). Decided by the user 2026-10-12: the person must
    #: never have to press «Continuar». See `loop.engine._AUTO_CONTINUE_TEXT_NOTICE` /
    #: `_AUTO_CONTINUE_CALL_NOTICE`.
    max_auto_continuations: int = 0
    max_consecutive_tool_errors: int = 3
    #: APP-ONLY: how many times per turn reaching `max_consecutive_tool_errors` appends
    #: `loop.engine._TOOL_ERRORS_NOTICE` to the failing result and clears the streak,
    #: instead of stopping on `StopReason.CONSECUTIVE_TOOL_ERRORS`. 0 keeps the stop exactly
    #: as it was; `chat` and every benchmark task never set this. `cli._build_engine` sets 3
    #: for the app. A weak model in the app had its turn ended by three *different* failing
    #: calls in 5 of 65 turns of one project (2026-10-25), each a new attempt, not a repeat.
    #: OpenHands' stuck detector flags only the same action failing 3+ times
    #: (docs.openhands.dev/sdk/guides/agent-stuck-detector, read 2026-10-26) and Gemini CLI's
    #: loop check only identical calls (loopDetectionService.ts, TOOL_CALL_LOOP_THRESHOLD 5,
    #: read 2026-10-26); here `max_identical_calls` already stops a repeated call and
    #: `max_steps` bounds the rest.
    max_tool_error_notices: int = 0
    #: Two identical calls earn a warning injected as a tool result; three stop the run.
    max_identical_calls: int = 3
    #: Web searches in a row, with no `web_fetch` and no change to the workspace between
    #: them, before the result carries a notice to read a page or answer
    #: (`loop.engine._SEARCH_SPIN_NOTICE`). 0 turns the notice off.
    #:
    #: A model in the app searched again and again with slightly different words, each search
    #: returning results, and never read one or answered: no two calls were identical
    #: (`max_identical_calls`) and none failed (`max_consecutive_tool_errors`). Only an
    #: agent with `web_search` registered can move this, which is the app and `chat` with
    #: `[web] in_chat`; the benchmark has no web tools, so these two settings never act there.
    #: The numbers are ours, not measured. No comparable limit was found in Claude Code or
    #: OpenHands, whose loop guards also key on repeated identical actions; three searches
    #: is one more than a search, a reformulation and a retry, six is twice that.
    searches_notice_at: int = 3
    #: Searches in a row, counted the same way, at which the turn stops with NO_PROGRESS.
    #: 0 turns the stop off.
    max_searches_without_fetch: int = 6
    #: Steps in a row whose reply prose is byte-identical (`RunState.note_reply`). The second
    #: carries a notice in its tool result to answer with what the turn found or change the
    #: approach (`loop.engine._REPEATED_REPLY_NOTICE`); this many stop the turn with
    #: NO_PROGRESS. 0 turns both off. Four, not three: in the QA session that found it
    #: (2026-10-17) the third identical reply was the web search that found the answer, and
    #: a stop there would have ended the turn with no answer at all. Ours, not measured.
    max_identical_replies: int = 4
    #: Fraction of the context window at which compaction runs.
    compact_at: float = 0.75
    #: Window tokens the verbatim literal block may take in a handover. Small on purpose:
    #: it sits beside a capped summary, and a literal is worth keeping because it is exact,
    #: not because a hundred others come with it.
    literal_budget_tokens: int = 320
    #: A tool result the fold keeps verbatim in the handover tail is shrunk to this many
    #: tokens (head+tail plus a pointer to the whole, `compact.shrink_tail`), so one big
    #: output does not keep the seed large and the session re-folding on a small window. 0
    #: leaves the tail whole, so `chat` and every benchmark fold byte for byte as before;
    #: the app opts in (`cli._build_engine`, APP-ONLY). Its measurement is owed (§7, the
    #: long-memory suite).
    handover_result_tokens: int = 0
    #: After a fold, the person's latest request is the one worked on: a message sent
    #: mid-turn is marked as changing the request, a request cut short before a newer one
    #: is not to be resumed, and the handover's goal follows the latest message
    #: (`compact.changed_course`). False keeps `chat` and every benchmark fold byte for
    #: byte; the app opts in (`cli._build_engine`, APP-ONLY). Its measurement is owed.
    fold_latest_request: bool = False
    #: A message sent mid-turn asks the model to answer it as the turn goes on, and keep
    #: working unless it changes the plan (`loop.engine._STEER_CONTINUE_NOTICE`). False
    #: keeps `chat` and every benchmark sending the old wrapper; the app opts in
    #: (`cli._build_engine`, APP-ONLY). Its measurement is owed.
    steer_continues: bool = False
    #: A fold forgets the identical-call count (`RunState.reset_repeats`), as a workspace
    #: change does: after it the model no longer sees what an earlier call showed, so a
    #: call it runs again is a re-read, not a loop. Three identical calls inside one window
    #: still stop the turn. False keeps `chat` and every benchmark counting over the whole
    #: turn; the app opts in (`cli._build_engine`, APP-ONLY). Its measurement is owed.
    fold_resets_repeats: bool = False
    #: The fold's handover is asked for with thinking off (`compact.summarize`). With it
    #: on, Motoris spent most of the handover's 1,400 tokens thinking: 177 of the 185 replies
    #: its log shows cut by `length` were handovers (2026-11-10). False keeps `chat` and
    #: every benchmark folding byte for byte; the app opts in (`cli._build_engine`, APP-ONLY).
    fold_without_thinking: bool = False
    #: How many times *per turn* the build gate (`loop.engine._maybe_run_build_gate`,
    #: `tools/build.py`) may reject END_TURN and send the model back. Rounds
    #: `1..max_build_gate_rounds` each compile and, on failure, inject one repair message.
    #: If it still fails after them, one more round compiles for telemetry only, injects
    #: nothing, and END_TURN is accepted with that state recorded. So 2 means at most two
    #: repair messages per turn, not two compiles. Separate from the other counters, and
    #: reset at the start of every turn (`Engine.run`), so rounds spent on one turn do not
    #: leave the next without a gate. Capped following arXiv 2607.05197 (2026-07-06); most
    #: self-repair gains land in the first two rounds (arXiv 2604.10508, 2026-04-12).
    max_build_gate_rounds: int = 2


@dataclass(frozen=True, slots=True)
class LoopSettings:
    """Switches on the agent loop that are not a budget."""

    #: Compile what this session touched before accepting END_TURN
    #: (`loop.engine._maybe_run_build_gate`). On by default. A benchmark comparison turns it
    #: off for the control arm through `COGITO_BUILD_GATE` (see `_apply_env_overrides`),
    #: not a code path, so both arms run the same code.
    build_gate: bool = True
    #: Say in the stable tier which model answers: its name, the id its server knows it by,
    #: who serves it and its context window (`context.prompt.ModelIdentity`). Off until the
    #: section 7 measurement accepts it, since one more line in every system message changes
    #: what every benchmark task sends. `COGITO_MODEL_IDENTITY` flips it for a benchmark
    #: arm. Off means the line is never built and the prompt is byte-identical to one
    #: without it; the window names the model from the catalog either way.
    model_identity: bool = False
    #: APP-ONLY: a fold's seed carries the current content of the files the session read or
    #: edited most recently (`loop.fold_files`), so the model does not re-read them. Read
    #: only when `serve` opens a project (`cli._build_engine` with ``conversations``), like
    #: `[estella] documents_in_app`; `chat`, and so every benchmark task, never reads it, so
    #: their requests are unchanged. A CONTRIBUTING 7 candidate, not yet measured.
    fold_files_in_app: bool = True
    #: APP-ONLY: the identity asks for one short line before each tool call, saying what it
    #: does and why (`context.prompt._PREAMBLES`), where `chat` and every benchmark task
    #: keep "say what you did, not what you are about to do". The app opts in
    #: (`cli._build_engine`). It changes what the model does; its measurement is owed.
    preambles: bool = False
    #: APP-ONLY: how many `delegate` calls of one step run at the same time
    #: (`loop.engine.Engine._run_delegates_together`). At 1 they run one after another, as
    #: every other call does; `chat` and every benchmark keep 1. The app sets
    #: `cli.DELEGATES_AT_ONCE`.
    parallel_delegates: int = 1
    #: False (APP-ONLY): no per-step status message at the end of the request. The date and
    #: the branch go in the system message instead (`context.prompt.build`), and a status
    #: turn is sent only when it has a step or token count or a task list to show, which
    #: the app never has. Motoris checkpoints a prompt where its last assistant turn starts,
    #: so a status message the next request no longer has made that checkpoint useless on
    #: every step (cogito-lab research/2026-11-10-single-model-review). `chat` and every
    #: benchmark keep True.
    status_turn: bool = True
    #: APP-ONLY: a turn about to end while the model's task list (`tools/todo.py`) still has
    #: items open is sent back once, naming them (`Engine._maybe_continue_open_tasks`).
    #: Without the `todo` tool it never fires. `chat` and every benchmark keep False.
    open_tasks_notice: bool = False


@dataclass(frozen=True, slots=True)
class Neo4jSettings:
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    database: str = "neo4j"
    #: How long a turn may keep buffering to the WAL before it gives up on reconnecting.
    reconnect_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class EstellaSettings:
    """Cogito Estella runs as an MCP subprocess in its own interpreter.

    It is pinned to the CPU on purpose: the GPU is fully committed to the model server,
    and a silent fight over VRAM is worse than a slower encoder.
    """

    enabled: bool = True
    device: str = "cpu"
    #: The `cogito-mcp` executable. Empty means "find it on PATH". `cogito setup` writes
    #: the absolute path of the interpreter it provisioned, because Estella must never be
    #: imported into this process.
    command: str = ""
    #: Token budget handed to `ask`. Estella clamps this to 100-4000 itself.
    ask_budget: int = 600
    #: Below this scorer probability the answer is treated as "probably not in the graph".
    #: Measured: a 0.30 floor flags 5 of 11 failures and only 1 of 39 correct replies.
    escalate_below: float = 0.30
    #: Index a long document the conversation attaches or reads, in the background, and
    #: show the model `ask_document` once one is indexed (`graph.documents`). Off until the
    #: section 7 measurement accepts it (cogito-lab experiments/2026-09-30-doc-cache);
    #: `COGITO_DOCUMENT_ASK` flips it for a benchmark arm. Off means nothing is built (no
    #: Estella process, no journal event, no tool), so every request is byte-identical.
    documents: bool = False
    #: A document is indexed when its text is longer than this many characters: about what
    #: one `read` shows (its 6,000-token budget at ~4 characters a token). A guess, not a
    #: measurement; anything shorter is cheaper to read whole.
    documents_min_chars: int = 24_000
    #: In the desktop app, index what was too large for the window (an offloaded message,
    #: its attached documents) so `ask_document` can answer from it. Only that: with
    #: `documents` off, a `read` or a short attachment indexes nothing. The user decided
    #: this on 2026-10-10 (context-fit design, 14.1 option A), before any measurement.
    #: Read only when `serve` opens a project (`cli._build_engine` with ``conversations``).
    #: `chat`, and so every benchmark task, never reads it, so its requests do not change.
    documents_in_app: bool = True
    #: CPU threads the app's documents Estella may use. It ingests at the lowest priority
    #: and only between turns, beside a model that may decode on the CPU too.
    documents_threads: int = 2
    #: APP-ONLY. Which Cogito Estella the app runs: ``lite`` (the default), ``auto`` or
    #: ``full``. Lite for every model, as the person decided on 2026-10-15 after the full
    #: graph matched lite for claude-haiku-4-5 and qwen3-coder-30b on 14 notes and 20 code
    #: questions, never answered one lite missed, and was slower in every cell (cogito-lab
    #: measurements/2026-10-14-search-grounding-and-estella-lite.md, section 8). ``auto``
    #: gives an API model or a local one of LARGE_MODEL_BILLIONS or more the full graph for
    #: `ask` and `recall` (`cli._estella_plan`); ``full`` gives it to every model. Fetched
    #: pages and offloaded content take the full graph only on a GPU: on 2 CPU threads it
    #: indexed a ~7k-token page in 26-39 s against under 0.3 s lite. Full needs the graph
    #: model (Estella's ``[full]`` extra); without it every graph runs lite. An Estella older
    #: than 0.18, which has no lite, always runs full.
    mode: str = "lite"
    #: APP-ONLY. `ask` indexes the project's source and config as well as its prose
    #: (`graph.recall.CODE_SUFFIXES`), so a project with no notes still gets `ask`. Needs
    #: Cogito Estella 0.19.3 or later, which splits code by line. Off until the code-question
    #: probe measures it (cogito-lab experiments/2026-10-15-estella-integration).
    ask_code: bool = False
    #: APP-ONLY. After the conversation has folded, the harness puts each of the person's
    #: messages to the conversation's record (`Recall.context_for`) and hands the model what
    #: it finds from messages no longer in the window. The model does not call `recall` when
    #: it has forgotten (CONTRIBUTING 1b; long-memory probe 2026-10-17). `chat`, and so every
    #: benchmark task, never reads it.
    auto_recall: bool = True


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    """Speech in and out, through cogito-vox (the `voice` extra).

    Off by default: no benchmark task speaks, so the two tool definitions
    would only cost tokens on every step of every task.
    """

    enabled: bool = False
    #: cogito-vox's engine catalog. Empty means its own lookup: env var,
    #: ./engines.toml, then the per-user config directory.
    engines_file: str = ""
    #: Where local STT/TTS weights live. Empty means cogito-vox's own lookup.
    weights_dir: str = ""
    #: WebRTC AEC3 echo cancellation. Without it the agent's own voice,
    #: picked up by the microphone, can interrupt it unless the user wears
    #: headphones.
    echo_cancellation: bool = True


@dataclass(frozen=True, slots=True)
class WebSettings:
    """`web_search` and `web_fetch` (`tools/web/`).

    In the desktop app the two tools are registered unless the Búsqueda tab turns the web
    level off (`[search] web`). `cogito chat`, and so every benchmark task, registers them
    only when ``in_chat`` is true, so with it false every request is byte-identical to one
    without the feature. While ``in_chat`` is false the table is also left out of
    `Settings.fingerprint()`, so a control run's header does not change.
    """

    #: `cogito chat` registers the tools only when true (the lab's treatment arm;
    #: `COGITO_WEB_IN_CHAT` flips it for one).
    in_chat: bool = False
    #: Token budget of one `web_fetch` result. About 10 kB of text (Anthropic's rule of thumb,
    #: platform.claude.com web-fetch docs, read 2026-10-12: a 10 kB page is ~2,500 tokens).
    #: `[tool_budgets] web_fetch` overrides it.
    fetch_budget: int = 2500
    #: APP-ONLY. In the app every page read goes through the documents index, and the result
    #: is only what is asked for: with a `question`, the passages that answer it, in at most
    #: this many tokens (about `[estella] ask_budget`); the rest stays in the saved copy. The
    #: smaller of this and the tool's own budget applies. `chat`, and so every benchmark
    #: task, never reads it: it keeps `fetch_budget`.
    fetch_answer_budget: int = 800
    #: APP-ONLY. Without a `question`: the outline and the start of the page, in at most
    #: this many tokens, the same way.
    fetch_overview_budget: int = 800
    #: APP-ONLY. How long `web_fetch` with a `question` waits for Cogito Estella to index
    #: the page it just saved, so the answer is Estella's passages and not the page's own
    #: lines. The model is idle while the tool waits, so the page jumps the between-turns
    #: rule (`DocumentIndex.hurry`). Measured 2026-10-14 for a ~7k-token page, Estella
    #: already running: 1.8-2.0 s on the GPU, 17-19 s on 8 CPU threads, 35-39 s on 2; the
    #: first page of a session also waits for Estella to start (about 20 s). 0 never waits.
    fetch_index_wait: float = 20.0
    #: APP-ONLY. `web_search` reads this many of its top results through the same index and
    #: appends the passages that answer the query (`WebFetchTool.read_for_search`): a model
    #: that searched 27 times read one page in the app's records (qwen3.8-flash-motoris,
    #: 2026-10-30) and decided on two-line snippets. Search APIs built for agents return
    #: page content with each result (Exa, Tavily; read 2026-10-30). 0 turns it off.
    search_read_pages: int = 2
    #: APP-ONLY. Tokens one of those pages may add to the search result.
    search_read_budget: int = 600
    #: APP-ONLY. How long the search waits for the index on each page it reads, instead of
    #: `fetch_index_wait`: past it the page's own matching lines are used.
    search_read_wait: float = 3.0
    #: APP-ONLY. How long the search may spend fetching each page it reads, instead of the
    #: 30 s an explicit `web_fetch` allows: a slow site is skipped, not waited on, so two
    #: reads cannot hold a search for a minute.
    search_read_deadline: float = 8.0
    #: Hosts whose plain http address is kept as written, not upgraded to https (an intranet
    #: docs site, or the lab's corpus server). Private addresses are kept anyway; any address
    #: may be read since 2026-11-01.
    allow_hosts: list[str] = field(default_factory=list)
    #: When not empty, every fetch and every search source must be one of these hosts.
    only_hosts: list[str] = field(default_factory=list)
    #: Never fetched or searched.
    blocked_hosts: list[str] = field(default_factory=list)
    #: Honour a site's robots.txt before reading a page from it. Off since 2026-11-01 (the
    #: person: "quita esos bloqueos"); a person may turn it back on.
    robots: bool = False
    #: A SearXNG instance's address (the person's own, or the lab's corpus server). Empty
    #: means no such source; nothing is configured by default.
    searxng_url: str = ""


@dataclass(frozen=True, slots=True)
class SkillsSettings:
    """Agent Skills (`tools/skills.py`). The app registers `skill` whenever a skill is
    installed; `cogito chat`, and so every benchmark task, only with ``in_chat``, so every
    benchmark request is byte-identical to one without the feature."""

    enabled: bool = True
    in_chat: bool = False
    #: Token budget of one loaded skill; the rest goes to an artifact like any tool's. The
    #: specification recommends instructions under 5,000 tokens.
    budget: int = 8000


@dataclass(frozen=True, slots=True)
class McpServerSettings:
    """One MCP server: `[mcp.servers.NAME]`, the same fields Claude Code's `mcpServers`
    entries use. Started over stdio (`command`, `args`, `env`), or reached at an address
    (`type = "http"` or `"sse"`, `url`, `headers`)."""

    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    #: Where the server runs. Empty means the core's own directory.
    cwd: str = ""
    enabled: bool = True
    #: "stdio", "http" (Streamable HTTP, falling back to the older HTTP+SSE) or "sse".
    type: str = "stdio"
    url: str = ""
    #: Sent on every request to `url`; `${VAR}` reads the environment.
    headers: Mapping[str, str] = field(default_factory=dict)
    #: A client the person registered with the server's sign-in themself, for one that
    #: registers none on its own (`--client-id`).
    client_id: str = ""
    #: For a `docker run` server that speaks HTTP inside its container: the port it serves
    #: on there and the path that answered. Written by Cogito the first time it finds them.
    http_port: int = 0
    http_path: str = ""

    @property
    def remote(self) -> bool:
        return self.type != "stdio"


@dataclass(frozen=True, slots=True)
class McpSettings:
    """MCP servers whose tools the model may call (`tools/mcp/`).

    Nothing is registered without a server, and `chat` registers nothing without
    ``in_chat``, so every benchmark task's requests are byte-identical to ones without the
    feature.
    """

    servers: Mapping[str, McpServerSettings] = field(default_factory=dict)
    #: The app always registers the servers' tools; `cogito chat`, and so every benchmark
    #: task, only when this is true, like `[web] in_chat`.
    in_chat: bool = False
    #: At most this many of their tools are shown on one step (`McpHub.shown`).
    max_tools: int = 6
    #: And their definitions cost at most this many tokens per step, at four characters a
    #: token. A choice, not a measured value: about what six mid-sized tools take.
    definitions_budget: int = 6000
    #: A tool's description is cut at this many characters in what the model is shown.
    description_chars: int = 1200
    #: Token budget of one tool's result; the rest goes to an artifact like any tool's.
    result_budget: int = 4000
    #: Seconds a server has to start and list its tools (`npx` may download first).
    start_timeout: float = 120.0
    #: Seconds a new conversation waits for servers still starting. The servers start
    #: together; one not ready by then goes on starting and joins the next conversation.
    connect_wait: float = 20.0
    #: Seconds one call may take. Decompiling a large binary can take minutes.
    call_timeout: float = 600.0


@dataclass(frozen=True, slots=True)
class Settings:
    default_model_id: str | None = None
    #: Wire format for the edit tool. The largest measured lever on agent quality, so it
    #: is one key instead of a code path: hashline | str_replace | whole_file.
    edit_format: str = "hashline"
    budgets: BudgetSettings = field(default_factory=BudgetSettings)
    loop: LoopSettings = field(default_factory=LoopSettings)
    neo4j: Neo4jSettings = field(default_factory=Neo4jSettings)
    estella: EstellaSettings = field(default_factory=EstellaSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    web: WebSettings = field(default_factory=WebSettings)
    mcp: McpSettings = field(default_factory=McpSettings)
    skills: SkillsSettings = field(default_factory=SkillsSettings)
    tool_budgets: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TOOL_BUDGETS))

    def budget_for(self, tool_name: str, declared: int | None = None) -> int:
        """Token ceiling for one tool: the configured value, else the tool's own.

        A tool declares its budget in its ``ToolSpec`` (CONTRIBUTING.md section 6); this file
        only overrides it. Reading only from here would clip a new tool at the fallback
        until someone copied its number into this file.
        """
        configured = self.tool_budgets.get(tool_name)
        if configured is not None:
            return configured
        return declared if declared else 1000

    def fingerprint(self) -> dict[str, object]:
        """The configuration as recorded alongside a benchmark run. ``[web]`` joins it only
        when ``in_chat`` is on: until then the tools are not registered, and a header that
        gained a key would make every control run differ from the runs recorded before."""
        recorded = asdict(self)
        if not self.web.in_chat:
            del recorded["web"]
        if not self.skills.in_chat:
            del recorded["skills"]
        # `chat` registers MCP tools only with `in_chat`, so a header stays as it was until
        # then, servers or not. An env or header value can be a key: only the names are
        # recorded.
        if not (self.mcp.servers and self.mcp.in_chat):
            del recorded["mcp"]
        else:
            for server in recorded["mcp"]["servers"].values():  # type: ignore[index]
                server["env"] = sorted(server["env"])
                server["headers"] = sorted(server["headers"])
        return recorded


def _section(document: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = document.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"config.toml: [{name}] must be a table")
    return value


#: Keys whose string value is one of a few words.
_ALLOWED: dict[tuple[str, str], tuple[str, ...]] = {("estella", "mode"): ("auto", "lite", "full")}

#: An API model, or a local one with at least this many billion parameters, is a large
#: model for `[estella] mode = "auto"`, which is opt-in. Between the 12B the suite measures as small and the
#: 30B it uses as its reference; a choice, not a measured threshold.
LARGE_MODEL_BILLIONS = 20.0


def _checked(current: object, section: str, overrides: Mapping[str, object]) -> dict[str, object]:
    """Reject unknown keys and any value whose type would break something later.

    Caught here, a wrong type names the key; caught later, it surfaces as a confusing
    failure three layers away.
    """
    accepted: dict[str, object] = {}
    for key, value in overrides.items():
        if not hasattr(current, key):
            raise ConfigError(f"config.toml [{section}]: unknown key {key!r}")
        expected = type(getattr(current, key))
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, expected):
            raise ConfigError(
                f"config.toml [{section}].{key}: expected {expected.__name__}, "
                f"got {type(value).__name__}"
            )
        allowed = _ALLOWED.get((section, key))
        if allowed is not None and value not in allowed:
            raise ConfigError(
                f"config.toml [{section}].{key}: expected one of {', '.join(allowed)}, "
                f"got {value!r}"
            )
        accepted[key] = value
    return accepted


def parse(text: str) -> Settings:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc

    settings = Settings()
    top = {k: v for k, v in document.items() if not isinstance(v, Mapping)}
    if top:
        settings = replace(settings, **{k: v for k, v in top.items() if hasattr(settings, k)})

    for name, current in (
        ("budgets", settings.budgets),
        ("loop", settings.loop),
        ("neo4j", settings.neo4j),
        ("estella", settings.estella),
        ("voice", settings.voice),
        ("web", settings.web),
        ("skills", settings.skills),
    ):
        overrides = _checked(current, name, _section(document, name))
        if overrides:
            settings = replace(settings, **{name: replace(current, **overrides)})

    mcp = _section(document, "mcp")
    if mcp:
        settings = replace(settings, mcp=_mcp(mcp))

    tool_budgets = _section(document, "tool_budgets")
    if tool_budgets:
        merged = dict(settings.tool_budgets)
        merged.update({str(k): int(v) for k, v in tool_budgets.items()})  # type: ignore[call-overload]
        settings = replace(settings, tool_budgets=merged)

    return settings


#: The `type` names other clients write, read as the transport Cogito runs: Claude Code's
#: `http`/`sse`, VS Code's and Cursor's `streamable-http` and `streamableHttp`.
_MCP_TYPES = {
    "stdio": "stdio", "local": "stdio",
    "http": "http", "streamable-http": "http", "streamableHttp": "http", "remote": "http",
    "sse": "sse",
}


def _mcp(section: Mapping[str, object]) -> McpSettings:
    raw_servers = section.get("servers") or {}
    if not isinstance(raw_servers, Mapping):
        raise ConfigError("config.toml [mcp].servers: expected a table of servers")
    servers: dict[str, McpServerSettings] = {}
    for name, raw in raw_servers.items():
        where = f"config.toml [mcp.servers.{name}]"
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{where}: expected a table")
        # `ask` was the tab's per-server choice to be asked before every call; the guard now
        # asks only before a tool that may change something, so an older file's key is read
        # and ignored.
        unknown = set(raw) - {
            "command", "args", "env", "cwd", "enabled", "type", "ask", "url", "headers",
            "client_id", "http_port", "http_path",
        }
        if unknown:
            raise ConfigError(f"{where}: unknown key {sorted(unknown)[0]!r}")
        kind = _MCP_TYPES.get(str(raw.get("type", "stdio" if "url" not in raw else "http")))
        if kind is None:
            raise ConfigError(f"{where}.type: expected stdio, http or sse")
        if kind == "stdio" and not isinstance(raw.get("command"), str):
            raise ConfigError(f"{where}: needs a command")
        if kind != "stdio" and not isinstance(raw.get("url"), str):
            raise ConfigError(f"{where}: needs a url")
        args = raw.get("args", [])
        env = raw.get("env", {})
        headers = raw.get("headers", {})
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError(f"{where}.args: expected a list of strings")
        for key, table in (("env", env), ("headers", headers)):
            if not isinstance(table, Mapping) or not all(isinstance(v, str) for v in table.values()):
                raise ConfigError(f"{where}.{key}: expected a table of strings")
        servers[str(name)] = McpServerSettings(
            command=str(raw.get("command", "")),
            args=tuple(args),
            env={str(k): v for k, v in env.items()},
            cwd=str(raw.get("cwd", "")),
            enabled=bool(raw.get("enabled", True)),
            type=kind,
            url=str(raw.get("url", "")),
            headers={str(k): v for k, v in headers.items()},
            client_id=str(raw.get("client_id", "")),
            http_port=int(raw.get("http_port", 0) or 0),
            http_path=str(raw.get("http_path", "")),
        )
    rest = {key: value for key, value in section.items() if key != "servers"}
    overrides = _checked(McpSettings(), "mcp", rest)
    return replace(McpSettings(servers=servers), **overrides)


_ENV_TRUE = frozenset({"1", "true", "yes", "on"})
_ENV_FALSE = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str) -> bool | None:
    """A boolean environment override, or None when unset or empty. A shell can leave a
    variable empty by accident, so empty means "use the file"."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    if normalized in _ENV_TRUE:
        return True
    if normalized in _ENV_FALSE:
        return False
    raise ConfigError(f"{name}: expected true/false, got {raw!r}")


def _apply_env_overrides(settings: Settings) -> Settings:
    """Environment overrides that let a benchmark arm flip a setting without its own config
    file: `COGITO_BUILD_GATE` for `[loop] build_gate`, `COGITO_MODEL_IDENTITY` for `[loop]
    model_identity`, `COGITO_DOCUMENT_ASK` for `[estella] documents`, `COGITO_WEB_IN_CHAT`
    for `[web] in_chat`, and `COGITO_MCP_IN_CHAT` and `COGITO_SKILLS_IN_CHAT` for `[mcp]`
    and `[skills] in_chat`. An older `[models]` table (profiles, background_probe) is
    ignored.

    `cogito-lab/bench/runner.py`'s `agent_environment()` copies the calling process's
    environment into every `cogito chat` it launches, so `bench run` / `bench matrix` can
    compare an arm on and off without changing the lab's runner.
    """
    if (gate := _env_flag("COGITO_BUILD_GATE")) is not None:
        settings = replace(settings, loop=replace(settings.loop, build_gate=gate))
    if (named := _env_flag("COGITO_MODEL_IDENTITY")) is not None:
        settings = replace(settings, loop=replace(settings.loop, model_identity=named))
    if (documents := _env_flag("COGITO_DOCUMENT_ASK")) is not None:
        settings = replace(settings, estella=replace(settings.estella, documents=documents))
    if (web := _env_flag("COGITO_WEB_IN_CHAT")) is not None:
        settings = replace(settings, web=replace(settings.web, in_chat=web))
    if (mcp := _env_flag("COGITO_MCP_IN_CHAT")) is not None:
        settings = replace(settings, mcp=replace(settings.mcp, in_chat=mcp))
    if (skills := _env_flag("COGITO_SKILLS_IN_CHAT")) is not None:
        settings = replace(settings, skills=replace(settings.skills, in_chat=skills))
    return settings


def load(path: Path | None = None) -> Settings:
    """Load settings from disk. An absent file means the defaults, not an error."""
    target = path or paths.settings_file()
    settings = Settings() if not target.exists() else parse(target.read_text(encoding="utf-8"))
    return _apply_env_overrides(settings)
