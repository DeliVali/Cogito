"""API keys and the Neo4j password.

Two sources, checked in order: the environment variable an entry names, then a
``secrets.toml`` the settings screen writes with owner-only permissions. The environment
wins so a development shell can override without editing files.

A model entry may also name an ``api_key_file``: its own owner-only file, read when the
client is created. For that entry it replaces ``secrets.toml``, after the environment
variable; see `model_key`.

Values never reach a log, an error message or the graph.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Protocol

from cogito.config import paths
from cogito.config.tomlfile import quote, sync_directory
from cogito.errors import ConfigError

_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600

#: One read-modify-write at a time in this process, so two keys stored at once both land.
_LOCK = threading.RLock()

_REDACTED = "***"


def _secrets_file() -> Path:
    return paths.config_dir() / "secrets.toml"


def _read() -> dict[str, dict[str, str]]:
    target = _secrets_file()
    if not target.exists():
        return {}
    try:
        document = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"secrets.toml is not valid TOML: {exc}") from exc
    return {
        section: {str(k): str(v) for k, v in table.items()}
        for section, table in document.items()
        if isinstance(table, dict)
    }


def _write(document: dict[str, dict[str, str]]) -> None:
    target = paths.ensure(paths.config_dir()) / "secrets.toml"
    lines: list[str] = [
        "# Written by Cogito. Owner-readable only. Do not commit.",
        "",
    ]
    for section, table in sorted(document.items()):
        lines.append(f"[{_key(section)}]")
        for key, value in sorted(table.items()):
            lines.append(f"{_key(key)} = {quote(value)}")
        lines.append("")

    # Owner-only from the start: `mkstemp` creates the file 0600, so no one else can ever
    # read a key on disk, not even between the write and the chmod.
    handle, temporary = tempfile.mkstemp(prefix=".secrets.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, _FILE_MODE)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    sync_directory(target.parent)


#: A TOML bare key. Anything else (say, a model id with a dot) is written quoted, or
#: ``[models] gpt-5.1 = ...`` would read back as a table ``gpt-5`` holding ``1``.
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else quote(name)


def get(section: str, key: str, env_var: str | None = None) -> str | None:
    """Return a stored secret, preferring ``env_var`` when it is set and non-empty."""
    if env_var:
        from_environment = os.environ.get(env_var, "").strip()
        if from_environment:
            return from_environment
    return _read().get(section, {}).get(key) or None


def set_(section: str, key: str, value: str, *, replace: bool = True) -> None:
    """Store a secret. Called by the settings screen, never by the agent loop. With
    ``replace=False`` a value already stored under that name is kept and `ConfigError` is
    raised, so a new key never takes another's place."""
    with _LOCK:
        document = _read()
        table = document.setdefault(section, {})
        if not replace and key in table:
            raise ConfigError(f"secrets.toml already holds [{section}] {key}; nothing was changed")
        table[key] = value
        _write(document)


def names(section: str) -> tuple[str, ...]:
    """The names stored in ``section``, never their values."""
    with _LOCK:
        return tuple(_read().get(section, {}))


def delete(section: str, key: str) -> None:
    with _LOCK:
        document = _read()
        if document.get(section, {}).pop(key, None) is not None:
            _write(document)


def describe(value: str | None) -> str:
    """Render a secret for display: present or absent, never the value itself."""
    return _REDACTED if value else "not set"


class _KeyedEntry(Protocol):
    """What `model_key` reads from a catalog entry (`config.models.ModelEntry`)."""

    id: str
    api_key_env: str | None
    api_key_file: str | None


def missing_key_reason(api_key_env: str | None) -> str:
    """The sentence for an API entry with no key anywhere. The desktop matches it word for
    word to show «Falta la clave de API…» (`desktop/src/renderer/strings.js`, NO_API_KEY),
    so every caller builds it here."""
    name = api_key_env or "an api_key_env"
    return f"no API key: set {name} or store one from the settings screen"


def model_key(entry: _KeyedEntry) -> str | None:
    """The key for one catalog entry, or None when there is none anywhere.

    The environment variable first, as everywhere in this module. Then the entry's
    ``api_key_file`` if it names one. A named file is never skipped silently: a missing,
    unsafe or unreadable one raises `ConfigError` saying which and why. Without a file,
    ``secrets.toml``.
    """
    if not entry.api_key_file:
        return get("models", entry.id, entry.api_key_env)
    if entry.api_key_env:
        from_environment = os.environ.get(entry.api_key_env, "").strip()
        if from_environment:
            return from_environment
    return read_key_file(entry.api_key_file, entry.api_key_env)


#: A shell-style variable name, which is what a ``KEY=VALUE`` line assigns.
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def read_key_file(path: str | Path, name: str | None = None) -> str:
    """The key in ``path``: a ``KEY=VALUE`` line (``export`` and quotes allowed, ``#``
    comments skipped) or the bare key on its own line. With ``name``, the line that assigns
    it; without, the file must hold exactly one key.

    Refused unless it is a regular file owned by this user that no one else can read or
    write (an owner-only mode such as 0600 or 0400). Checked on the open file, not on a
    path that could be swapped between the check and the read. Every error names the file
    and the problem and never includes a byte of its content.
    """
    target = Path(path).expanduser()
    where = f"api_key_file {target}"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        raise ConfigError(f"{where}: no such file") from None
    except OSError as exc:
        raise ConfigError(f"{where}: cannot be opened ({exc.strerror})") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{where}: not a regular file")
        if os.name == "posix":
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ConfigError(f"{where}: owned by another user; it must be yours alone")
            if info.st_mode & 0o077:
                mode = stat.S_IMODE(info.st_mode)
                raise ConfigError(
                    f"{where}: group or others can access it (mode {mode:04o}); "
                    f"it must be owner-only: chmod 600 {target}"
                )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # The decoder's own message quotes the offending byte; this one does not.
        raise ConfigError(f"{where}: not UTF-8 text") from None

    assigned: dict[str, str] = {}
    bare: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        variable, equals, value = line.partition("=")
        if equals and _NAME.match(variable.strip()):
            assigned[variable.strip()] = _unquote(value.strip())
        else:
            bare.append(line)

    if name and name in assigned:
        found = [assigned[name]]
    elif name and assigned:
        raise ConfigError(f"{where}: no {name}= line")
    else:
        found = [*assigned.values(), *bare]
    if not found:
        raise ConfigError(f"{where}: holds no key")
    if len(found) > 1:
        raise ConfigError(
            f"{where}: holds {len(found)} keys; name the one to use with api_key_env"
        )
    if not found[0].strip():
        raise ConfigError(f"{where}: the key is empty")
    return found[0].strip()
