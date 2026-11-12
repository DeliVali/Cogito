"""Changing `[mcp.servers.NAME]` tables in config.toml from the app's Conexiones tab.

Line by line, so the person's comments, order and every other section stay as written:
a key inside the server's table is replaced or added, a new server is appended as its own
table, and a removed one loses its table and nothing else. The result is parsed before it
is written (`tomlfile.write_atomically`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from cogito.config import paths
from cogito.config.tomlfile import quote, write_atomically
from cogito.errors import ConfigError

#: A server name as a TOML bare key, which is also what its tools are prefixed with.
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ANY_HEADER = re.compile(r"^\s*\[")


def _header(name: str) -> re.Pattern[str]:
    escaped = re.escape(name)
    key = rf'(?:{escaped}|"{escaped}")'
    return re.compile(rf"^\s*\[\s*mcp\s*\.\s*servers\s*\.\s*{key}\s*\]\s*(#.*)?$")


def _span(lines: list[str], name: str) -> tuple[int, int] | None:
    header = _header(name)
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
    return start, end


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return quote(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(quote(str(item)) for item in value) + "]"
    raise ConfigError(f"cannot write {type(value).__name__} to config.toml")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def set_fields(name: str, values: dict[str, object], path: Path | None = None) -> None:
    """Replace or add these keys in the server's table."""
    target = path or paths.settings_file()
    lines = _text(target).splitlines(keepends=True)
    span = _span(lines, name)
    if span is None:
        raise ConfigError(f"no MCP server named {name!r} in {target}")
    start, end = span
    for key, value in values.items():
        line = f"{key} = {_value(value)}\n"
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        index = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
        if index is None:
            # After the table's last key, before the blank lines that end it.
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, line)
            end += 1
        else:
            lines[index] = line
    write_atomically(target, "".join(lines))


def add(
    name: str,
    command: str,
    args: Sequence[str],
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    *,
    type: str = "stdio",  # noqa: A002 - the config key's own name
    url: str = "",
    headers: Mapping[str, str] | None = None,
    client_id: str = "",
) -> None:
    """A new server table at the end of the file: a command, or an address."""
    if not NAME.match(name):
        raise ConfigError("a name uses letters, digits, '-' or '_', up to 40")
    target = path or paths.settings_file()
    text = _text(target)
    if _span(text.splitlines(keepends=True), name) is not None:
        raise ConfigError(f"an MCP server named {name!r} already exists")
    prefix = text if not text or text.endswith("\n") else text + "\n"
    separator = "\n" if prefix.strip() else ""
    table = f"[mcp.servers.{name}]\n"
    if type == "stdio":
        table += f"command = {quote(command)}\nargs = {_value(list(args))}\n"
    else:
        table += f"type = {quote(type)}\nurl = {quote(url)}\n"
        if client_id:
            table += f"client_id = {quote(client_id)}\n"
    for key, pairs in (("env", env), ("headers", headers)):
        if pairs:
            inline = ", ".join(f"{quote(k)} = {quote(v)}" for k, v in pairs.items())
            table += f"{key} = {{ {inline} }}\n"
    write_atomically(target, f"{prefix}{separator}{table}")


def remove(name: str, path: Path | None = None) -> None:
    """The server's table, and the blank lines that followed it."""
    target = path or paths.settings_file()
    lines = _text(target).splitlines(keepends=True)
    span = _span(lines, name)
    if span is None:
        raise ConfigError(f"no MCP server named {name!r} in {target}")
    start, end = span
    while start > 0 and not lines[start - 1].strip() and end == len(lines):
        start -= 1
    write_atomically(target, "".join(lines[:start] + lines[end:]))
