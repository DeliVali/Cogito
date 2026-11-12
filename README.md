# Cogito

A coding agent with full control of the machine: the terminal, the files, the web, the
browser and the desktop. It has a Python core and an Electron desktop app.

It runs on [Cogito Motoris](https://github.com/DeliVali/cogito-motoris), a local engine,
and on the Claude API. Any OpenAI compatible host works too.

The bet is that **the harness beats the model**: tools built well enough that a modest
local model does good work. Every harness change is kept or reverted by a measurement.
[CONTRIBUTING.md](CONTRIBUTING.md) has the rules and the record.

## What the agent does

### Memory

- The transcript, the tool calls and the session history are stored in a Neo4j graph. When
  the context window fills, old turns leave the window and stay in the graph.
- The exact values a tool printed (hashes, identifiers, paths) are carried across that
  fold as written, never paraphrased.
- After a fold, the app looks up what your message refers to in the part that left the
  window and puts it back in front of the model before it replies.
- [Cogito Estella](https://github.com/DeliVali/cogito-estella) finds the sentences that
  answer a question about your notes, a page or the conversation, in whatever language you
  ask, without spending model tokens.
- A message too large for the model is saved whole. The model gets its beginning, its end
  and the path to read the rest.

### Safety

- A deletion waits for your approval, and the card says whether it goes to the trash or
  cannot be undone. `cogito undo` restores what went to the trash.
- Operating system files, your keys and the session's own record are refused, not asked.
- In the app, installing or upgrading system packages needs your approval.
- «Automático», under the message, answers these questions for you when the loss can come
  back from the trash. It still asks about anything permanent, the project folder, your
  credentials, Cogito's own settings and a whole folder in your home.

### Editing and checking

- When the model finishes after changing source, the harness compiles or typechecks what
  changed and sends the errors back.
- Placeholder edits such as `// ... existing code ...` are refused before anything is
  written. Files keep their own line endings.
- A tool call cut off by the output limit is refused, never run.

### The web

- `web_search` and `web_fetch` need no key and no setup. In the app they are on for every
  model.
- The app downloads its own search engine (SearXNG, listening only on this machine) and
  rotates through thirteen engines, four per search. Tavily, Firecrawl and DuckDuckGo
  answer when it cannot.
- A search reads its top two results and adds the passages that answer the query. The same
  search within six hours reuses its results.
- `web_fetch` reads any address, local ones included, and returns only the part that
  answers what was asked.
- When a local model states a value that nothing it retrieved holds, the app sends the
  answer back once to check it.

### The browser, the screen and the terminal (app only)

- `browser` opens pages in tabs beside «Hilo», on the app's own Chromium. The model uses a
  page by its elements, several actions per call, and can read its text, HTML, scripts,
  network, console and storage, or run JavaScript in it.
- `computer` takes screenshots and uses the mouse and keyboard through the desktop's
  portals, which ask once. It runs several actions per call and opens programs by name.
  Linux only.
- Terminal tabs sit beside «Hilo». When the model needs something only you can do, such as
  a `sudo` command, it asks with the exact line, runs it in a tab where you type the
  password, and reads what it printed. A shell block in a reply has a ▶ that runs it there.

### Long work (app only)

- A turn has no time or step limit. It ends when the model answers, when it loops, or when
  you press «Detener».
- A message sent while a turn runs joins that turn at its next step.
- The model keeps a task list (`todo`) you see in the thread. A turn that would end with
  items open goes back to them once.
- The model can hand a question to a subagent (`delegate`), which works in a context of
  its own and returns only its findings. Up to three run at once. The row opens onto the
  calls the subagent makes, and one that reaches its step limit reports what it found. A
  writer subagent changes a copy of the project in a sandbox and returns a patch.
- A dev server is left running in the background, and the model is told its PID and port.
  A download or build that outlives its timeout moves to the background too, and the model
  is told when it ends.
- Before each tool call the model says in one short line what the call is for.

### MCP servers and skills

- Add a server under `[mcp.servers.NAME]` in `config.toml`, or in the app's Conexiones
  form: a command, an address, a `docker run` line, another client's install line
  (`claude mcp add …`, `codex mcp add …`, `gemini mcp add …`) or its JSON.
- «Importar…» copies the servers Claude Code, Claude Desktop, Cursor, Codex or Gemini CLI
  already have. A project's `.mcp.json` runs once you approve it.
- Remote servers sign in with OAuth from the app.
- The model can add a server itself when you ask (`mcp_add`). You approve every time.
- A server can carry hundreds of tools. The model is shown the few that fit the message
  and finds the rest with `mcp_find`.
- A tool that may change something asks first.
- Agent Skills live in `~/.cogito/skills` or a project's `.cogito/skills`, one folder each
  with its `SKILL.md`. `.agents/skills` and `.claude/skills` are read too. A skill loads
  when a message matches it, when the model asks, or when you pick it with «/».
- Two skills ship with Cogito: one adds servers and skills, the other finds and removes
  dead code.

### Models

- Local and API models share one list, `models.toml`.
- An entry with `serve = "motoris"` is started, watched and stopped by Cogito. In the app,
  one that dies is started again and the request is asked again. Motoris is a fork of
  [Strata](https://github.com/Niko1221/Strata) that places the weights across VRAM, RAM and
  SSD, so a model larger than the GPU still runs.
- Claude goes through the official SDK with prompt caching. When the app starts it adds
  the newest Claude model of each family your key can reach.
- A bolt in the composer sets how much the model thinks: a thinking budget on Motoris, the
  effort level on Claude.
- A PNG, JPEG, GIF or WebP you attach goes to the model as an image when the model can see
  one. A model that cannot is told what was attached.
- Cogito estimates what an API model spends today and this month, and pauses before the
  next request when you reach the budget you set.
- When a provider gives a `retry-after` of an hour or less, the turn resumes at that time.
- Voice in and out is optional, through
  [cogito-vox](https://github.com/DeliVali/cogito-vox). Off by default.

## The desktop app

- The first launch asks your name and a model: a Claude API key, or Cogito Motoris. The
  key is stored in a file only you can read.
- A project is a folder. Opening it brings the conversation back where it left off.
- The answer streams as the model writes it, with its Markdown rendered and each tool call
  as a row you can open.
- Pick any of your messages to rewind: the conversation and the files the agent changed
  after it go back to that point.
- The explorer lists the project's folder with git's marks and opens code, Markdown,
  images, PDFs and tables in a viewer. Files dropped or pasted on the window attach to the
  message. PDF, Word, Excel, PowerPoint and OpenDocument files reach the model as text.
- The model chip switches models and keeps the conversation.
- Closing the window hides it in the tray. A core that crashes restarts with the same
  project and conversation.
- Cogito Estella and the search engine are downloaded by the app, each checked against a
  pinned SHA-256.
- Updates wait until the agent is idle and are checked against Cogito's own Ed25519
  signature before they install.
- «Herramientas y permisos» → «Diseño» sets a light or dark theme.

The app's interface is in Spanish.

## Install

No installer is published yet. The release workflow builds an AppImage and a deb for
Linux, a dmg for macOS and a setup .exe for Windows, each carrying its own Python, and
they will appear under [Releases](../../releases).

Until then, run it from source. You need Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/)
and Node.js.

```bash
git clone https://github.com/DeliVali/Cogito.git
cd Cogito
uv sync
uv run cogito setup           # Neo4j and Cogito Estella, with real sizes shown first
cd desktop && npm install && npm start
```

Linux is where Cogito is developed and tested. On Windows the agent's shell is Git Bash,
and the context graph and the local search engine are not set up there yet. The macOS and
Windows builds have not been tried on real machines.

## Development

```bash
uv run cogito doctor          # the GPU, Neo4j, Estella, the sandbox and each Motoris entry
uv run cogito models list     # the model catalog
uv run cogito chat            # the core without the app
uv run cogito undo            # list what the agent sent to the trash, or restore it
uv sync --extra voice         # optional: voice tools (needs ../cogito-vox)
uv run python -m cogito.dev.check_layers
uv run python -m cogito.dev.check_tree
```

Tests, benchmarks and measurements live in a separate repository, `cogito-lab`.
[CONTRIBUTING.md](CONTRIBUTING.md) explains the layers, the folder map and how a change is
accepted.

## License

Apache-2.0. The app downloads and runs SearXNG (AGPL-3.0-or-later) as a separate program;
see [docs/licenses.md](docs/licenses.md).
