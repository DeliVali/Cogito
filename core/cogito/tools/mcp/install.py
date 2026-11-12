"""`mcp_add`: the model adds an MCP server when the person asks. APP ONLY.

Claude Code's model does this by running `claude mcp add [--scope local|project|user]
[--transport http|sse|stdio] NAME -- command args` or `claude mcp add-json NAME '{...}'` in
its shell (code.claude.com/docs/en/mcp, read 2026-11-10). Here the same text goes to one
tool, read by the parser the Conexiones form uses (`connections.read_line`: a command, an
address, another client's install line or its JSON), so a README's line is passed as it is.

The tool saves the server and starts it, never before the person allows it: the guard asks
on every call with the exact command or address (`ExternalTool.every_time`), automatic mode
included, since the server runs in every later conversation. A line that leaves a key to
fill in (`YOUR_API_KEY`, an empty `env` value, an unset `${VAR}`) is not saved: the model is
told which value is missing, to ask the person rather than invent one.

Scopes, as Claude Code's: "user" is config.toml, for every project; "project" is the
project's `.mcp.json`, which a team may commit, so a value whose name says it is a secret
must be a `${VAR}` reference there.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cogito.config import mcp_servers
from cogito.errors import ConfigError
from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters
from cogito.tools.mcp import project
from cogito.tools.mcp.connections import missing_values, read_line, server_settings
from cogito.tools.mcp.hub import McpHub, Offered, _arguments, _summary, clean_schema

#: How long a call waits for the new server before saying it is still starting. A choice:
#: about what a cached `npx` package takes; a first download goes on in the background.
START_WAIT = 30.0
#: Tools a result lists with their arguments, for a model that calls them by name.
_LISTED = 8
_SECRET_NAME = re.compile(
    r"(key|token|secret|password|passwd|authorization|credential|(^|_)pat($|_))", re.IGNORECASE
)
_REFERENCE_ONLY = re.compile(r"^\s*(bearer\s+)?\$\{[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?\}\s*$", re.IGNORECASE)

SPEC = ToolSpec(
    name="mcp_add",
    description=(
        "Add an MCP server to Cogito when the person asks, and start it. `line` is what "
        "its instructions give, as written: a command (`npx -y pkg`), an address "
        "(`https://…/mcp`), a `claude mcp add …` line or its JSON. The person is asked "
        "first. If it needs a key the person has not given, ask them; never invent one."
    ),
    parameters=parameters(
        {
            "line": {"type": "string", "description": "The command, address, install line or JSON."},
            "name": {
                "type": "string",
                "description": "Name for the server; optional when the line gives one.",
            },
            "scope": {
                "type": "string",
                "enum": ["user", "project"],
                "description": "user: every project (default). project: this project's .mcp.json.",
            },
        },
        ("line",),
    ),
    aliases={
        "line": ("command", "text", "json", "url", "install"),
        "name": ("server", "server_name"),
    },
    budget_tokens=1200,
    mutates=False,
    operates=True,
    summary="add an MCP server and start it",
    provenance=Provenance(
        reproducibility=Reproducibility.MUTABLE,
        retention=Retention.ACKNOWLEDGEMENT,
        shape=PayloadShape.COMPACT,
    ),
)


def plan(arguments: Mapping[str, Any], project_root: Path) -> tuple[str, dict[str, Any], str]:
    """(name, what to save, scope), or ValueError saying what is wrong."""
    line = str(arguments.get("line") or "").strip()
    if not line:
        raise ValueError("give the line: a command, an address, an install line or its JSON")
    scope = str(arguments.get("scope") or "user").strip().lower()
    if scope in ("local", "global"):
        scope = "user"
    if scope not in ("user", "project"):
        raise ValueError("scope is user or project")
    name, spec = read_line(line, str(arguments.get("name") or ""))
    return name, spec, scope


def describe(arguments: Mapping[str, Any], project_root: Path, config_file: Path) -> str:
    """What the card shows: the exact command or address, where it is saved, and the names
    (never the values) of what it sets in the environment or headers."""
    try:
        name, spec, scope = plan(arguments, project_root)
    except ValueError as exc:
        return f"(not readable: {exc})"
    runs = spec.get("url") or " ".join(
        [str(spec.get("command", "")), *(_quoted(a) for a in spec.get("args", ()))]
    )
    where = project.file_for(project_root) if scope == "project" else config_file
    lines = [f"{name}: {runs}", f"saved in {where}"]
    for label, values in (("env", spec.get("env")), ("headers", spec.get("headers"))):
        if values:
            lines.append(f"{label}: {', '.join(sorted(values))}")
    return "\n".join(lines)


def _quoted(word: str) -> str:
    return word if word and not re.search(r"[\s'\"\\$`]", word) else repr(word)


class McpAddTool:
    spec = SPEC

    def __init__(
        self,
        hub: McpHub,
        project_root: Path,
        config_file: Path | None = None,
        on_added: Callable[[], tuple[Offered, ...]] | None = None,
    ) -> None:
        self._hub = hub
        self._project_root = Path(project_root)
        self._config_file = config_file
        #: Joins the new server quietly and registers its tools, so a call by name runs
        #: now (`cli._mcp_joiner`); returns what joined.
        self._on_added = on_added

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        try:
            name, spec, scope = plan(arguments, self._project_root)
        except ValueError as exc:
            return ToolResult.error(f"{exc}.", "mcp_add_unreadable")
        if missing := missing_values(spec):
            return ToolResult.error(
                f"Not added: the line leaves {', '.join(missing)} to fill in. Ask the person "
                "for it, then call mcp_add again with the value in the line (or they can add "
                "the server themselves in Herramientas → Conexiones). Never make one up.",
                "mcp_add_needs_value",
                needs=missing,
            )
        if scope == "project":
            if secret := [
                key for key, value in {**(spec.get("env") or {}), **(spec.get("headers") or {})}.items()
                if _SECRET_NAME.search(key) and not _REFERENCE_ONLY.match(str(value))
            ]:
                return ToolResult.error(
                    f"Not added: {', '.join(secret)} would be written into {project.FILE_NAME}, "
                    "which the project may commit. Use the user scope, or write it as "
                    "${VARIABLE} and have the person set that variable.",
                    "mcp_add_secret_in_project",
                )
        try:
            if scope == "project":
                project.write(self._project_root, name, spec)
                project.approve(self._project_root, name)
            else:
                mcp_servers.add(
                    name,
                    str(spec.get("command", "")),
                    list(spec.get("args", [])),
                    self._config_file,
                    env=spec.get("env") or None,
                    type=str(spec.get("type", "stdio")),
                    url=str(spec.get("url", "")),
                    headers=spec.get("headers") or None,
                    client_id=str(spec.get("client_id", "")),
                )
        except (ConfigError, ValueError) as exc:
            return ToolResult.error(
                f"Not added: {exc}. Choose another name, or ask the person to change or "
                "remove that server in Conexiones.",
                "mcp_add_refused",
            )
        server = server_settings(spec)
        self._hub.add_server(name, server)
        state = self._hub.wait_started(name, START_WAIT, getattr(context, "cancel", None))
        where = "this project's .mcp.json" if scope == "project" else "Cogito's config.toml"
        if state is None or state.starting:
            return ToolResult(
                content=(
                    f"Saved {name} in {where}; it is still starting (a first download can "
                    "take minutes). Its tools join at the person's next message once it is "
                    "ready. Conexiones shows its progress."
                ),
                metadata={"server": name, "status": "starting"},
            )
        if state.client is None:
            return ToolResult.error(
                f"Saved {name} in {where}, but it did not start: {state.error[:600]}. Tell the "
                "person; Conexiones shows the error and can retry or remove it.",
                "mcp_add_failed",
                server=name,
                status="failed",
            )
        joined = (
            self._on_added() if self._on_added is not None else self._hub.join(quiet=True)
        )
        mine = [offered for offered in joined if offered.server == name]
        lines = [f"Added {name} ({where}); it is ready with {len(state.tools)} tools."]
        for offered in mine[:_LISTED]:
            lines.append(f"- {offered.name}: {_summary(offered.remote.description)}")
            lines.append(f"  arguments: {_arguments(clean_schema(offered.remote.input_schema))}")
        if len(mine) > _LISTED:
            lines.append(f"{len(mine) - _LISTED} more; find them with mcp_find.")
        if mine:
            lines.append("Call one by its exact name with those arguments.")
        return ToolResult(content="\n".join(lines), metadata={"server": name, "status": "ready"})
