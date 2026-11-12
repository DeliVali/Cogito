# Cogito

A coding agent with full control of the machine (terminal, files, the web). It runs local
models and API models alike.

The bet is that **the harness beats the model**: tools built well enough that a weak local
model does good work. Every harness change is accepted or rejected by a measurement, never
by opinion.

## What makes it different

### The agent

- **Context lives in a graph.** The
  transcript, tool calls and session history are stored in Neo4j. Compaction moves old
  turns out of the context window without deleting them, and the exact values a tool
  printed (hashes, identifiers, paths) are carried over verbatim instead of paraphrased.
  In the app, once a conversation has been folded, what answers your message from the
  part that left the window is put back in front of the model before it replies.
  [Cogito Estella](https://github.com/DeliVali/cogito-estella) finds the sentences that
  answer a question about your notes, a page or the conversation, without spending LLM
  tokens, in whatever language you ask; the desktop app runs its light version for every
  model and installs it itself (about 600 MB to download, no GPU) when you have none set up. The desktop app starts the managed
  Neo4j when it launches, and says so when it will not start.
- **Nothing is deleted without asking.**
  Deletions wait for your approval, and the card says whether they go to the trash or
  cannot be undone. Operating-system files are never touched, and the agent can read its
  own session record but never change it.
  A deletion that would take them or your home folder with it is refused, not asked.
  In the app, installing or upgrading system packages needs your approval too.
  In the app, «Automático» under the message answers these questions for you when what is
  lost can come back from the trash; it still asks about anything permanent, the project
  folder or all of it, your keys and credentials, Cogito's own settings and memory, and a
  whole folder in your home or all of it.
- **One model list.** Local and API models are reached
  the same way, and adding a provider is one entry in a file. Claude goes through the
  official SDK with prompt caching. The app's picker shows a saved server only while it is
  up, and when the app starts it adds the newest Claude model of each family your key can
  reach. A bolt in the composer sets how much the model thinks: a thinking budget on
  Motoris, the effort level on Claude.
- **It compiles before it says it is done.**
  When the model finishes after changing source, the harness compiles or typechecks what
  changed and sends the errors back.
- **Edits and tool calls are checked before they run.**
  Placeholder edits such as `// ... existing code ...` are refused before anything is
  written, and files keep their own line endings. A tool call cut off by the output limit
  is refused rather than run.
- **A message too large for the model is kept, not refused.**
  It is saved whole, and the model gets its beginning and end plus the path to read the
  rest. A conversation reopened on a smaller window is fitted the same way, and a request a
  server refuses as too large is made to fit before it is sent again. In the desktop app,
  the model can ask about what was saved. Cogito Estella answers once it has indexed it;
  before that, or when Estella is not installed, the answer is the lines to read.
- **It can look things up on the web.**
  `web_search` and `web_fetch` read the public web as text, with no key and nothing to set
  up. In the app they are on for every model, and a search engine of its own (SearXNG,
  downloaded on first use, listening only on this machine) gives them the open web. Each
  search asks four of its thirteen engines in turn, and an engine that blocks this machine
  rests while the others carry on. The same search again within six hours reuses its
  results and asks no engine. Before asking an engine, the app searches known URLs by
  their saved titles and snippets in this project's local graph, showing their dates.
  A single topic word first checks external sources, so a broad saved page does not hide
  a better source; it remains available if those sources fail. `web_fetch` reads the
  current page directly. In the app a search also reads its top two results and adds the
  passages that answer the query. The catalog retains up to 512 entries per
  project for 30 days; unfamiliar content still needs a working external source.
  When a model that needs it states a value nothing it found
  holds, the app sends the answer back once to check it. They only read.
- **MCP servers and skills.** Any MCP server added under `[mcp.servers.NAME]` in
  `config.toml` gives the model its tools: a command (`command`, `args`, `env`, as Claude
  Code's `mcpServers`) or an address (`type = "http"` or `"sse"`, `url`, `headers`), with
  OAuth sign-in from the app («Iniciar sesión») for servers that need it. A `docker run …` server
  is pulled and run as written; when the image serves HTTP inside its container instead of
  stdio, Cogito publishes that port on loopback and connects to it. The app's
  Conexiones form also takes an address, another client's install line (`claude mcp add …`,
  `codex mcp add …`, `gemini mcp add …`, `claude mcp add-json …`) or its JSON
  (`mcpServers`, VS Code's `servers`, Zed's `context_servers`) as pasted, with the name
  optional; «Importar…» brings the servers Claude Code, Claude Desktop, Cursor, Codex or
  Gemini CLI already have, and the model can add one when you ask (`mcp_add`, asked every
  time). A server still starting joins the open conversation when it is ready, and a
  project's `.mcp.json` runs once you approve it. The app shows only the few that fit each message, and the model can
  search for the rest with `mcp_find`; for a local model that set stays the same through the
  conversation, and `mcp_find` returns each match's arguments to call it by name. A tool that may change something asks first, and
  «Permitir» holds for every later conversation and project. Agent
  Skills (one folder each, with its `SKILL.md`, in `~/.cogito/skills` or a project's
  `.cogito/skills`; those in `.agents/skills` and `.claude/skills` are read too) load when a message matches one, when the model asks for it, or when you pick one with
  «/» in the composer; the app lists them under «Habilidades». Ask
  Cogito to install a server or a skill and it knows where each goes; ask it to remove dead
  code and it finds unused functions, checks every use and runs the tests. Both are on in the
  app; `cogito chat` turns them on with `[mcp] in_chat` and `[skills] in_chat`.
- **Images in the chat.** A PNG, JPEG, GIF or WebP dropped, pasted or attached goes to
  the model as an image when it can see one (Claude, or a local model whose host reports vision
  or whose entry sets `args.vision`); any other model is told what was attached and that it cannot see it.
- **Cogito Motoris.** A local entry with `serve = "motoris"` (`args.motoris` is its checkout,
  `args.config` its engine JSON) is started, watched and stopped by Cogito; in the app, one
  that dies is started again (up to 3 times in 10 minutes) and the request asked again. Motoris, a fork
  of [Strata](https://github.com/Niko1221/Strata), places the weights across VRAM, RAM and
  SSD itself, so a model larger than the GPU still runs. Or connect to one already running
  from the connect screen. Cogito no longer starts llama-server; any OpenAI-compatible host
  works with a `base_url`, and `args.server_extensions = true` gives it llama-server's
  request extensions. Older `serve = "strata"` entries are read as `motoris`, with a warning.
- **It can see and use your computer, in the app.** Tool results can carry images; `computer`
  takes screenshots and uses the mouse and keyboard through the desktop's portals (asked
  once), runs several actions in one call and opens programs by name, and `browser` opens web pages in tabs beside «Hilo», on the app's own Chromium (no
  separate download), uses them by their elements (several in one call), reads a page's
  text with its links, and reads their HTML, live DOM,
  scripts, network, console and storage, or runs JavaScript in them.
- **A task list it keeps, in the app.** For work of several steps the model keeps a list
  (`todo`) you see in the thread; a turn that would end with items still open goes back to
  them once.
- **Subagents, in the app.** The model can hand a self-contained question to a subagent
  (`delegate`), which investigates it in a context of its own with read-only tools and
  returns only its findings, reusing the conversation's cached prompt. One level deep; up to
  three from one reply run at the same time. Its row opens onto the calls it makes, while it
  works and after, and one that reaches its step limit is asked for what it found instead
  of returning nothing. A writer subagent changes a copy of the project in a sandbox and
  hands back a patch.
- **A terminal beside the chat, in the app.** Terminal tabs sit next to «Hilo» (open one
  from the explorer). A shell block in a reply has a ▶ that runs it there; when the model
  needs something only you can do, such as a `sudo` command, it asks with the exact line and
  «Ejecutar», runs it in a tab where you type the password, and reads what it printed.
  Everything else it runs in its own shell, without asking.
- **Servers and long commands keep running.** A command still running after five seconds and
  listening on a port, such as a dev server, is left in the background and the model is told
  its PID and port. In the app, a download or build that outlives its timeout is moved to the
  background too, and when it ends the model is told, in the running turn or in one Cogito
  opens for it. Both stop with the session.
- **The project's folder is beside the chat.** The desktop app's explorer lists the
  folder with git's marks, and opens code, Markdown, images, PDFs, tables and `.env` files
  (values hidden) in a read-only viewer; changes are asked of the model. Files can be
  created, renamed, duplicated, dragged into another folder, dropped in from outside,
  attached to a message, or moved to the system
  trash, always asked and undoable for a few seconds.
- **Light or dark.** «Herramientas y permisos» → «Diseño» sets the theme: Claro, Oscuro, or
  Sistema, which follows the desktop.
- **A failed provider call shows what the provider said.**
  When the provider gives a `retry-after` of an hour or less, the turn resumes on its own
  at that time.
- **It keeps to your budget.** Cogito estimates what an API model
  spends today and this month, and pauses before the next request when you reach the
  budget you set. A provider's spend cap is named as one and never retried.
- **Steer it without stopping it.** A message
  sent while a turn runs joins that turn at its next step instead of restarting it.
- **Voice, when you want it.** Optional speech in and out
  through [cogito-vox](https://github.com/DeliVali/cogito-vox), with echo cancellation and
  barge-in. Off by default.

### The desktop app

- **Two questions to start.** The first launch asks your
  name and a model: an API key, or Cogito Motoris, started from its folder or already
  running. The connect screen lists the API services and a Cogito Motoris row (plain http only on
  loopback).
  Testing the model costs nothing, and the key is stored in an owner-only file.
- **It lives in the tray.** Closing the
  window hides it, and a core that crashes restarts with the same project and
  conversation. The window's orb and the tray icon change colour with what the agent is
  doing. Notifications carry buttons to allow or reject a command, or to see the changes.
- **Attach by dragging or pasting.** A file dropped or
  pasted on the window becomes a chip on the composer. PDF, Word, Excel, PowerPoint and
  OpenDocument files reach the model as text.
- **The work as it happens.** The answer streams as the
  model writes it, with its Markdown rendered, and what the agent says between its steps
  stays where it said it. A message sent while it works joins the turn in place.
- **One project, one thread.** A project is a folder.
  Opening it brings the conversation back where it left off, for you and for the model.
- **Rewind to an earlier message.** Pick
  any of your messages, and the conversation and the files the agent changed after it go
  back to that point. "Rehacer" undoes the rewind until a new turn starts.
- **Switch models from the chip.** The model chip
  lists every configured model. Switching keeps the conversation.
- **It installs its own pieces, pinned.**
  Cogito Estella and the local search engine are downloaded by the app itself, each checked
  against a pinned SHA-256, and each can be switched off in «Herramientas y permisos». For a
  missing compiler it shows you how to install it.
- **It updates itself, never mid-task.** Updates wait until
  the agent is idle, and each one is checked against COGITO's own Ed25519 signature
  before it installs.

## Install

Download the installer for your platform from [Releases](../../releases): Linux (AppImage,
deb), macOS (dmg) or Windows (setup .exe).
Each installer carries COGITO's own Python, so the app starts without Python or `cogito`
on the machine.

- The **deb** adds COGITO's signed APT repository, so it updates with
  `sudo apt update && sudo apt install --only-upgrade cogito`.
- On **Windows** the agent's shell is Git Bash, so install [Git for Windows](https://git-scm.com/download/win)
  first. The installer is not signed yet, so SmartScreen asks once. The context graph
  (Neo4j) and the local search engine are not set up there yet; the app runs without them.
- The **macOS** app is not notarized yet. Run it from `/Applications`, where it can update
  itself, and open it once with "Open Anyway" (System Settings → Privacy & Security).

## Development

```bash
uv sync
uv run cogito doctor          # check the GPU, Neo4j, Estella, sandbox and each Motoris entry
uv run cogito chat            # headless core
uv sync --extra voice         # optional: voice_listen / voice_speak (needs ../cogito-vox)
cd desktop && npm start       # the desktop app
```

## License

Apache-2.0. The app downloads and runs SearXNG (AGPL-3.0-or-later) as a separate program;
see [docs/licenses.md](docs/licenses.md).
