---
name: extend-cogito
description: Use when the person asks to install, add, connect, set up, remove or list an MCP server, MCP tools or an Agent Skill for Cogito, this agent; says where they are configured and how.
---

# Adding MCP servers and skills to Cogito

Install only what the person asked for, from the source they named. An MCP server runs
code on their computer every time Cogito starts it.

## MCP servers, with `mcp_add`

When you have the `mcp_add` tool, use it and do not edit the configuration by hand:

- Pass the install instructions as they are written, in `line`: a command
  (`npx -y some-package@1.2.3`), an address (`https://example.com/mcp`), a `claude mcp add …`
  line, or the JSON a README gives for Claude Desktop, Cursor or VS Code. Give `name` only
  when the line does not name the server.
- `scope` is `user` (every project, the default) or `project` (this project's `.mcp.json`,
  which the team may commit: a key goes there only as `${VARIABLE}`).
- The person is asked before anything is saved or run, with the exact command. Do not ask
  them first yourself.
- If it answers that a value is missing (an API key, a token), ask the person for it, then
  call `mcp_add` again with the value in the line. Never make one up, and never print a key
  back in your answer.
- When it answers that the server is ready, its tools can be called by the names it lists,
  in this same conversation. If it is still starting, its tools join at the person's next
  message; say so.
- If it failed, tell the person what it said; Herramientas → Conexiones shows the error and
  can retry or remove the server.

Without `mcp_add`, write the entry by hand as below. Show the person the exact entry before
writing it and wait for a yes. What you add that way is available from the **next
conversation**; say so when you finish.

## MCP servers, by hand

Cogito starts an MCP server with a command (stdio), or reaches one at an address. They
are configured in `${CONFIG_FILE}`, one TOML table per server under `[mcp.servers.NAME]`:

```toml
[mcp.servers.NAME]
command = "npx"                       # the program to run
args = ["-y", "some-package@1.2.3", "mcp"]
env = { API_TOKEN = "..." }           # optional
cwd = ""                              # optional; where the server runs
enabled = true                        # optional; false keeps the entry but does not start it
```

A server at an address:

```toml
[mcp.servers.NAME]
type = "http"                         # "http" (Streamable HTTP) or "sse" (the older transport)
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ${SOME_TOKEN}" }   # optional; ${VAR} reads the environment
```

- `NAME` uses letters, digits, `_` or `-`. Its tools are named `NAME__tool`.
- A server at an address that needs a sign-in (most hosted ones: Notion, Linear) has no
  token to write: add the table without `headers`, and tell the person to press «Iniciar
  sesión» on it in Herramientas → Conexiones.
- Only `https` addresses, or `http` on this machine.
- A `docker run … IMAGE` line goes in as written (`command = "docker"`, the rest in `args`).
  Cogito pulls the image and, when it serves HTTP inside the container, publishes the port
  and connects by itself; do not add `-p` or a `url` for it.
- Instructions written for Claude Code, Claude Desktop, Cursor or VS Code give the same
  fields as JSON: `"mcpServers": {"NAME": {"command": ..., "args": [...], "env": {...}}}`
  or `{"type": "http", "url": ..., "headers": {...}}`. Translate each one to a table above.
  Drop `"type": "stdio"`. The person can also paste such a JSON or a `claude mcp add …`
  line into the Conexiones form as it is.
- Pin a version (`package@1.2.3`) when the instructions give one.
- Edit the file: read it first, keep everything already in it, and add the table at the end.
  Create the file if it does not exist. Never touch another section.
- Check the file still parses:
  `python3 -c "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))" "${CONFIG_FILE}"`
- Check the server starts if you can: run its command with `--help` or `--version` and
  read the output. A server that needs a download (`npx -y`, `uvx`) fetches it on first run.
- A token the person gives you goes in `env`, or in `headers` for an address. Never print it
  back in your answer.

To remove a server, delete its table, or set `enabled = false` to keep it for later.

## Skills

A skill is a folder holding `SKILL.md` (YAML frontmatter with `name` and `description`,
then instructions) and any files it refers to. Each skill has its own folder, named as its
`name`. Install them in `${SKILLS_DIR}/NAME/`, Cogito's own place for the person's skills.
Cogito also reads skills installed for other agents (`~/.agents/skills`,
`~/.claude/skills`) and a project's own (`.cogito/skills`, `.agents/skills` or
`.claude/skills` inside it); leave those where they are.

To install one:

1. Get the whole folder, not only `SKILL.md`: clone the repository or download the folder
   the person pointed to, into a temporary directory first.
2. Read its `SKILL.md`. Its `name` should match the folder name. Check it has a
   `description`; without one Cogito ignores the skill.
3. Tell the person what it will make you do (one or two sentences) and that its scripts,
   if any, run with your permissions.
4. Move the folder to `${SKILLS_DIR}/NAME/`.

A skill does not install the tools it describes. If it expects an MCP server, add that
server as above.

To remove a skill, the person decides; removing the folder is a deletion and is asked
about like any other.
