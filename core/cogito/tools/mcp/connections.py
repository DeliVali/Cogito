"""What the app's Conexiones screens read and change (design 4e, 4f, the 1a chip).

Every answer is a plain dict. A refusal sets ``refused`` with an ``error`` and, where the
window words it, a ``code``, in place of an exception: the boundary
``server/connections.py`` reaches through. The settings are read from config.toml on every
call, so the tab shows what the next conversation will use, and changes are written there
(`config.mcp_servers`). Servers come from the process's pool, so the tab and the sessions
see the same processes.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from cogito.config import mcp_servers
from cogito.config.settings import McpServerSettings, McpSettings
from cogito.errors import ConfigError
from cogito.tools.mcp import oauth
from cogito.tools.mcp.hub import McpPool, ServerState, clean_schema
from cogito.tools.mcp.remote import expand

#: Four characters a token, as the hub counts a definition.
_CHARS_PER_TOKEN = 4


def tool_title(tool: Any) -> str:
    """The name a person reads: the tool's own title, else its name with spaces."""
    title = tool.title or str(tool.annotations.get("title") or "")
    return title or tool.name.replace("_", " ").replace("-", " ").capitalize()


class Connections:
    def __init__(
        self,
        pool: McpPool,
        load: Callable[[], McpSettings],
        path: Path | None = None,
        start_wait: float | None = None,
    ) -> None:
        self._pool = pool
        self._load = load
        self._path = path
        #: How long adding, retrying or switching on a server waits for its start before
        #: answering. None waits the whole `start_timeout`; the app passes 0 and reads the
        #: list again until the start ends (`tools-window.js`), so no request is held for up
        #: two minutes while `npx` downloads.
        self._start_wait = start_wait
        #: Sign-ins under way ("") or the reason the last one failed, by server.
        self._logins: dict[str, str] = {}

    # -- reading -----------------------------------------------------------------------

    def list(self) -> dict[str, Any]:
        try:
            settings = self._load()
        except ConfigError as exc:
            return {"error": str(exc), "refused": True}
        return {
            "connections": [
                # The list never waits on a start: a server still starting says so.
                self._entry(name, server, settings, wait=0)
                for name, server in settings.servers.items()
            ]
        }

    def _entry(
        self,
        name: str,
        server: McpServerSettings,
        settings: McpSettings,
        wait: float | None = None,
    ) -> dict[str, Any]:
        state: ServerState | None = None
        if server.enabled:
            state = self._pool.get(name, server, settings.start_timeout, wait=wait)
        signing_in = self._logins.get(name)
        if not server.enabled:
            status = "disabled"
        elif state and state.starting:
            status = "starting"
        elif state and state.needs_login:
            status = "login"
        else:
            status = "failed" if state and state.error else "connected"
        tools = list(state.tools) if state and state.client else []
        size = sum(
            len(tool.name) + len(tool.description) + len(str(clean_schema(tool.input_schema)))
            for tool in tools
        ) // _CHARS_PER_TOKEN
        return {
            "name": name,
            "status": status,
            "enabled": server.enabled,
            # Waiting on a sign-in is said by `status`; only why one failed is an error.
            "error": (signing_in or "") if status == "login" else (state.error if state else ""),
            "signing_in": signing_in == "",
            "remote": server.remote,
            "command": server.url if server.remote else shlex.join([server.command, *server.args]),
            "tools": [
                {"name": tool.name, "title": tool_title(tool), "changes": not tool.read_only}
                for tool in tools
            ],
            # What its tools' definitions can add to one request: the hub shows a few per
            # step within its budget, so never more than that.
            "tokens": min(size, settings.definitions_budget),
            # What the server printed while starting, or why it failed: its last lines.
            "log": list(state.log[-12:]) if state is not None and status in ("starting", "failed") else [],
            "started_at": state.since if state is not None else 0,
            # Values the entry leaves for the person to fill in (names only).
            "needs": missing_values(
                {"env": server.env, "headers": server.headers, "args": server.args, "url": server.url}
            ),
        }

    # -- changing ----------------------------------------------------------------------

    def set(self, name: str, values: Mapping[str, Any]) -> dict[str, Any]:
        fields: dict[str, object] = {}
        if "enabled" in values:
            if not isinstance(values["enabled"], bool):
                return {"error": "enabled must be true or false", "refused": True}
            fields["enabled"] = values["enabled"]
        if not fields:
            return {"error": "nothing to change", "refused": True}
        try:
            mcp_servers.set_fields(name, fields, self._path)
        except ConfigError as exc:
            return {"error": str(exc), "refused": True, "missing": "no MCP server" in str(exc)}
        if fields.get("enabled") is False:
            self._pool.drop(name)
        return self._one(name)

    def add(self, name: str, command: str) -> dict[str, Any]:
        """A server from what the person pasted: a command on this machine, an address
        (`https://…/mcp`), another client's install line (`claude mcp add NAME -- npx …`,
        `claude mcp add --transport http NAME URL`) or its JSON. A line is read for what it
        would save, never run."""
        try:
            name, spec = read_line(command, name)
        except ValueError as exc:
            code = (
                "address" if "address must be" in str(exc)
                else "name" if "names no server" in str(exc)
                else "command"
            )
            return {"error": str(exc), "refused": True, "code": code}
        try:
            mcp_servers.add(
                name,
                str(spec.get("command", "")),
                list(spec.get("args", [])),
                self._path,
                env=spec.get("env") or None,
                type=str(spec.get("type", "stdio")),
                url=str(spec.get("url", "")),
                headers=spec.get("headers") or None,
                client_id=str(spec.get("client_id", "")),
            )
        except ConfigError as exc:
            code = "taken" if "already exists" in str(exc) else "name"
            return {"error": str(exc), "refused": True, "code": code}
        return self._one(name)

    def login(self, name: str) -> dict[str, Any]:
        """Start signing in to a remote server: the address the person's browser opens. The
        core waits on loopback for the answer, keeps the tokens and reconnects."""
        try:
            settings = self._load()
        except ConfigError as exc:
            return {"error": str(exc), "refused": True}
        server = settings.servers.get(name)
        if server is None:
            return {"error": f"no MCP server named {name!r}", "refused": True, "missing": True}
        if not server.remote:
            return {"error": "only a server at an address signs in", "refused": True}
        state = self._pool.peek(name)
        challenge = (state.challenge if state else None) or oauth.Challenge()
        try:
            started = oauth.begin(expand(server.url), challenge, client_id=server.client_id)
        except (oauth.AuthError, httpx.HTTPError) as exc:
            self._logins[name] = str(exc)
            return {"error": str(exc), "refused": True, "code": "login"}
        self._logins[name] = ""

        def wait() -> None:
            try:
                started.finish(oauth.LOGIN_WAIT)
                self._logins.pop(name, None)
            except (oauth.AuthError, httpx.HTTPError) as exc:
                self._logins[name] = str(exc)
            self._pool.drop(name)

        threading.Thread(target=wait, name=f"mcp-login-{name}", daemon=True).start()
        return {"url": started.url, "name": name}

    def remove(self, name: str) -> dict[str, Any]:
        self._pool.drop(name)
        with contextlib.suppress(ConfigError):
            server = self._load().servers.get(name)
            if server is not None and server.remote:
                oauth.Grant.forget(expand(server.url))
        try:
            mcp_servers.remove(name, self._path)
        except ConfigError as exc:
            return {"error": str(exc), "refused": True, "missing": True}
        return {"removed": name}

    def retry(self, name: str) -> dict[str, Any]:
        self._pool.drop(name)
        return self._one(name)

    def discover(self, project_root: Path | None, home: Path | None = None) -> dict[str, Any]:
        """The servers other clients configured, for the import list (`imports.discover`)."""
        from cogito.tools.mcp import imports

        try:
            settings = self._load()
        except ConfigError as exc:
            return {"error": str(exc), "refused": True}
        return {"found": imports.discover(home or Path.home(), project_root, settings.servers)}

    def import_servers(
        self, keys: list[str], project_root: Path | None, home: Path | None = None
    ) -> dict[str, Any]:
        """Add each ticked server: copied into config.toml as its file holds it, or, from
        the project's `.mcp.json`, approved for that project (`project.approve`)."""
        from cogito.tools.mcp import imports, project

        added: list[str] = []
        skipped: list[dict[str, str]] = []
        for key, name, scope, spec in imports.chosen(home or Path.home(), project_root, keys):
            if scope == "project" and project_root is not None:
                project.approve(project_root, name)
                added.append(name)
                continue
            chosen = safe_name(name)
            if spec.get("url") and not _safe_address(str(spec["url"])):
                skipped.append({"key": key, "reason": "address"})
                continue
            try:
                mcp_servers.add(
                    chosen,
                    str(spec.get("command", "")),
                    list(spec.get("args", [])),
                    self._path,
                    env=spec.get("env") or None,
                    type=str(spec.get("type", "stdio")),
                    url=str(spec.get("url", "")),
                    headers=spec.get("headers") or None,
                    client_id=str(spec.get("client_id", "")),
                )
            except ConfigError as exc:
                skipped.append({"key": key, "reason": "taken" if "already exists" in str(exc) else "name"})
                continue
            added.append(chosen)
        found = {key for key, *_ in imports.chosen(home or Path.home(), project_root, keys)}
        skipped.extend({"key": key, "reason": "gone"} for key in keys if key not in found)
        return {"added": added, "skipped": skipped}

    def _one(self, name: str) -> dict[str, Any]:
        try:
            settings = self._load()
        except ConfigError as exc:
            return {"error": str(exc), "refused": True}
        server = settings.servers.get(name)
        if server is None:
            return {"error": f"no MCP server named {name!r}", "refused": True, "missing": True}
        return self._entry(name, server, settings, wait=self._start_wait)



def read_line(text: str, name: str = "") -> tuple[str, dict[str, Any]]:
    """The name and what to save, from what the person or the model pasted. A typed name
    wins; else the one the line or JSON gives, made a valid name. ValueError says what is
    wrong, an unsafe address included."""
    spec = _spec_from_text(text.strip(), name.strip())
    given = str(spec.pop("name", "") or "")
    chosen = name.strip() or safe_name(given)
    if not chosen:
        raise ValueError("the line names no server; give it a name")
    if spec.get("url") and not _safe_address(str(spec["url"])):
        raise ValueError("an address must be https, or http on this machine")
    return chosen, spec


def safe_name(name: str) -> str:
    """A name another client gave, as a TOML bare key (`mcp_servers.NAME`): other
    characters become '-', cut at 40."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")
    return cleaned[:40]


def server_settings(spec: Mapping[str, Any]) -> McpServerSettings:
    """What `read_line` or `entry_spec` gives, as the settings a server starts from."""
    return McpServerSettings(
        command=str(spec.get("command", "")),
        args=tuple(str(arg) for arg in spec.get("args", ())),
        env=dict(spec.get("env") or {}),
        type=str(spec.get("type", "stdio")),
        url=str(spec.get("url", "")),
        headers=dict(spec.get("headers") or {}),
        client_id=str(spec.get("client_id", "")),
    )


#: A value a README leaves for the person to fill in (`YOUR_API_KEY`, `<token>`, `xxx`).
_PLACEHOLDER = re.compile(
    r"^(<[^<>]*>|\.\.\.|x{3,}|\*{3,}|changeme|replace[-_ ]?me|todo|"
    r".*\byour[-_ ]?[a-z0-9_-]*(key|token|secret|password|pat|id)\b.*)$",
    re.IGNORECASE,
)
_PLACEHOLDER_WORD = re.compile(
    r"(<[A-Za-z_ -]*(key|token|secret)[A-Za-z_ -]*>|\bYOUR[-_][A-Z0-9_-]*(KEY|TOKEN|SECRET)\b)",
    re.IGNORECASE,
)
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def missing_values(
    spec: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> list[str]:
    """What the line leaves for the person to give before the server can work: an `env`
    or header value that is empty or a placeholder, a `${VAR}` with no default that is not
    set here, a placeholder among the arguments. Names only, never a value."""
    import os

    environ = os.environ if environ is None else environ
    missing = []

    def unset(value: str) -> list[str]:
        return [ref for ref in _REFERENCE.findall(value) if ref not in environ]

    for key, value in dict(spec.get("env") or {}).items():
        value = str(value)
        if not value.strip() or _PLACEHOLDER.match(value.strip()) or unset(value):
            missing.append(key)
    for key, value in dict(spec.get("headers") or {}).items():
        bare = str(value).strip()
        bare = bare.split(None, 1)[1] if bare.lower().startswith("bearer ") and " " in bare else bare
        if not bare or _PLACEHOLDER.match(bare) or unset(bare):
            missing.append(f"header {key}")
    for index, arg in enumerate(spec.get("args") or (), start=1):
        if _PLACEHOLDER_WORD.search(str(arg)) or unset(str(arg)):
            missing.append(f"argument {index} ({_PLACEHOLDER_WORD.sub('…', str(arg))[:40]})")
    for ref in unset(str(spec.get("url", ""))):
        missing.append(ref)
    return missing


def _safe_address(url: str) -> bool:
    """https anywhere; plain http only to this machine, where nothing crosses a network."""
    parts = urllib.parse.urlsplit(expand(url))
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")


def _spec_from_text(line: str, wanted: str = "") -> dict[str, Any]:
    """What a pasted line or JSON says to save: `command`/`args`/`env`, or
    `type`/`url`/`headers`/`client_id`, and the client's own `name` for it when it has one.
    ValueError says what could not be read."""
    if not line:
        raise ValueError("a command or an address is required")
    if line.startswith(("{", "[")):
        return _from_json(line, wanted)
    if "://" in line.split(" ", 1)[0]:
        return {"type": "http", "url": line.split()[0]}
    try:
        words = shlex.split(line)
    except ValueError as exc:
        raise ValueError(f"the command does not parse: {exc}") from exc
    if len(words) >= 4 and words[1:3] == ["mcp", "add-json"]:
        return _from_json(" ".join(words[4:]), wanted or words[3])
    if len(words) >= 3 and words[1:3] == ["mcp", "add"]:
        return _install_line(words[3:])
    return {"command": words[0], "args": words[1:]}


#: Options of `claude mcp add`, `codex mcp add` and `gemini mcp add` that take a value
#: (their `--help`, read 2026-10-29). The flags without one are skipped by themselves.
_VALUED = {
    "-s", "--scope", "--callback-port", "--client-secret", "--timeout", "--description",
    "--include-tools", "--exclude-tools",
}
_TRANSPORTS = {"stdio": "stdio", "http": "http", "streamable-http": "http", "sse": "sse"}


def _install_line(words: list[str]) -> dict[str, Any]:
    """What follows `mcp add` in another client's install line. Options until the client's
    name for the server and the command's first word (or the address); from there, or after
    `--`, every word is the command's."""
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    transport, client_id, url = "", "", ""
    positional: list[str] = []
    index = 0
    while index < len(words) and len(positional) < 2:
        word = words[index]
        index += 1
        option, _, inline = word.partition("=") if word.startswith("--") else (word, "", "")

        def value() -> str:
            nonlocal index
            if inline:
                return inline
            index += 1
            return words[index - 1] if index - 1 < len(words) else ""

        if word == "--":
            break
        if option in ("-t", "--transport"):
            transport = _TRANSPORTS.get(value(), "")
            if not transport:
                raise ValueError("the transport is not stdio, http or sse")
        elif option == "--url":
            url = value()
        elif option in ("-H", "--header"):
            key, _, header = value().partition(":")
            headers[key.strip()] = header.strip()
        elif option == "--bearer-token-env-var":
            headers["Authorization"] = "Bearer ${" + value() + "}"
        elif option == "--client-id":
            client_id = value()
        elif option in ("-e", "--env"):
            # `claude mcp add -e A=1 B=2 name -- …`: the option takes every KEY=VALUE after it.
            pairs = [inline] if inline else []
            while not inline and index < len(words) and "=" in words[index] and not words[index].startswith("-"):
                pairs.append(words[index])
                index += 1
            for pair in pairs:
                key, _, setting = pair.partition("=")
                env[key] = setting
        elif option in _VALUED:
            value()
        elif word.startswith("-"):
            continue
        else:
            positional.append(word)
    rest = positional[1:] + words[index:]
    name = positional[0] if positional else ""
    if url or (rest and "://" in rest[0]) or transport in ("http", "sse"):
        url = url or (rest[0] if rest else "")
        # Options may follow the address: `… github https://… -H "Authorization: …"`.
        trailing = rest[1:] if rest and rest[0] == url else rest
        for index, word in enumerate(trailing):
            following = trailing[index + 1] if index + 1 < len(trailing) else ""
            if word in ("-H", "--header") and following:
                key, _, header = following.partition(":")
                headers[key.strip()] = header.strip()
            elif word == "--client-id" and following:
                client_id = following
        if not url:
            raise ValueError("the line names no address")
        spec: dict[str, Any] = {"name": name, "type": transport if transport in ("http", "sse") else "http", "url": url}
        if headers:
            spec["headers"] = headers
        if client_id:
            spec["client_id"] = client_id
        return spec
    if not rest:
        raise ValueError("the line names no command")
    return {"name": name, "command": rest[0], "args": rest[1:], "env": env}


#: Where other clients keep their servers in the JSON their READMEs show: `mcpServers`
#: (Claude Desktop, Claude Code's `.mcp.json`, Cursor, Gemini CLI, Windsurf, Cline),
#: `servers` (VS Code's `mcp.json`), `context_servers` (Zed) and `mcp` (opencode).
_SERVER_TABLES = ("mcpServers", "servers", "context_servers", "mcp_servers", "mcp")
_URL_KEYS = ("url", "httpUrl", "serverUrl")


def _is_server(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("command", *_URL_KEYS))


def _from_json(text: str, wanted: str) -> dict[str, Any]:
    """The one server a pasted JSON holds: a client's whole table, `{"name": {…}}`, or the
    server object itself. With several, the one named like `wanted`."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the JSON does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("the JSON is not an object")
    for key in _SERVER_TABLES:
        if isinstance(data.get(key), dict):
            data = data[key]
            break
    if _is_server(data):
        entries = {wanted: data}
    else:
        entries = {str(k): v for k, v in data.items() if isinstance(v, dict) and _is_server(v)}
    if not entries:
        raise ValueError("the JSON holds no server")
    if len(entries) > 1:
        if wanted not in entries:
            raise ValueError(f"the JSON holds {', '.join(entries)}; name the one to add")
        entries = {wanted: entries[wanted]}
    [(name, server)] = entries.items()
    return entry_spec(name, server)


def servers_in(table: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every server a client's table holds (`{"NAME": {...}}`), as `entry_spec` reads each;
    one that cannot be read is left out."""
    found = {}
    for name, server in table.items():
        if isinstance(server, dict) and _is_server(server):
            with contextlib.suppress(ValueError):
                found[str(name)] = entry_spec(str(name), server)
    return found


def entry_spec(name: str, server: Mapping[str, Any]) -> dict[str, Any]:
    """One server object of another client's JSON, as `add` saves it."""
    url = next((str(server[key]) for key in _URL_KEYS if server.get(key)), "")
    if url:
        kind = str(server.get("type") or server.get("transport") or "http")
        spec: dict[str, Any] = {"name": name, "type": "sse" if kind == "sse" else "http", "url": url}
        headers = server.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError(f"{name!r} has headers of the wrong shape")
        if headers:
            spec["headers"] = {str(k): str(v) for k, v in headers.items()}
        oauth_table = server.get("oauth") or {}
        if isinstance(oauth_table, dict) and oauth_table.get("clientId"):
            spec["client_id"] = str(oauth_table["clientId"])
        return spec
    command = server.get("command")
    args = server.get("args") or []
    env = server.get("env") or server.get("environment") or {}
    # Zed's older form: `"command": {"path", "args", "env"}`.
    if isinstance(command, dict):
        args = command.get("args") or args
        env = command.get("env") or env
        command = command.get("path")
    # opencode: `"command": ["npx", "-y", "pkg"]`.
    if isinstance(command, list):
        command, args = (command[0] if command else ""), [*command[1:], *args]
    if not isinstance(command, str) or not command:
        raise ValueError(f"{name!r} has no command")
    if not isinstance(args, list) or not isinstance(env, dict):
        raise ValueError(f"{name!r} has args or env of the wrong shape")
    return {
        "name": name,
        "command": command,
        "args": [str(a) for a in args],
        "env": {str(k): str(v) for k, v in env.items()},
    }
