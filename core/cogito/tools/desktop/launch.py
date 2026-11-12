"""Opening a program, a file or an address for `computer`'s `launch`, on Linux.

A program is found by its desktop entry, the way a launcher menu finds it: the files under
``applications/`` in ``$XDG_DATA_HOME`` and ``$XDG_DATA_DIRS``, earlier directories first, a
file's ID its path there with "/" read as "-" (Desktop Entry Specification 1.5, "Desktop
File ID", and the XDG Base Directory Specification 0.8, read 2026-11-10). It is started
with `gtk-launch ID` (GTK's own, which runs the entry's Exec as the menu does), else
`kioclient exec FILE` or `gio launch FILE`. A file or an address is opened with
`kioclient exec`, else `xdg-open`, else `gio open`, so it goes to the program the person set
for it. Without this the model opened a program by pressing Super, typing its name, Enter,
waiting and looking: four to six steps of a model that takes about 20 s a step.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: How long a launcher may run before it is taken as started and left running: gtk-launch
#: and kioclient return once the program is spawned, but an xdg-open fallback can run the
#: program itself and never return.
_SPAWN_WAIT = 5.0
#: Candidates named when a name matches several programs, or none.
_NAMED = 8

#: `argv` -> (exit status, or None while it still runs; the end of its error output).
Launcher = Callable[[Sequence[str]], tuple[int | None, str]]


class LaunchError(ValueError):
    """What could not be opened, said so the model can choose again."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One application's desktop entry."""

    id: str
    path: Path
    names: tuple[str, ...]
    generic: tuple[str, ...]
    program: str
    hidden: bool

    @property
    def name(self) -> str:
        return self.names[0] if self.names else self.id


def spawn(argv: Sequence[str]) -> tuple[int | None, str]:
    """Run a launcher in its own session, so the program it starts outlives this core and
    its process group, and wait a little for it to return."""
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                start_new_session=True,
            )
        except OSError as exc:
            return 127, str(exc)
        try:
            status: int | None = process.wait(timeout=_SPAWN_WAIT)
        except subprocess.TimeoutExpired:
            status = None
        errors.seek(0)
        tail = errors.read()[-600:].decode("utf-8", "replace").strip()
    return status, tail


def _data_dirs() -> list[Path]:
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    rest = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    seen: list[Path] = []
    for part in [home, *rest.split(":")]:
        if part and Path(part).is_absolute() and Path(part) not in seen:
            seen.append(Path(part))
    return seen


def _parse(path: Path) -> dict[str, str]:
    """The `[Desktop Entry]` group's keys, localized ones included."""
    values: dict[str, str] = {}
    group = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group == "Desktop Entry" and "=" in line:
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), value.strip())
    return values


def _program(exec_line: str) -> str:
    """The command an Exec line runs, without its folder: `firefox` for
    `/usr/lib/firefox/firefox %u`."""
    words = exec_line.split()
    while words and (words[0] == "env" or "=" in words[0]):
        words = words[1:]
    return Path(words[0].strip('"')).name.lower() if words else ""


def entries() -> list[Entry]:
    """Every application entry, the first directory's winning for an ID."""
    found: dict[str, Entry | None] = {}
    for root in _data_dirs():
        folder = root / "applications"
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.desktop")):
            desktop_id = str(path.relative_to(folder)).replace("/", "-")[: -len(".desktop")]
            if desktop_id in found:
                continue
            values = _parse(path)
            if (
                values.get("Type", "Application") != "Application"
                or values.get("Hidden", "").lower() == "true"
            ):
                # A hidden entry deletes the ID for every later directory too.
                found[desktop_id] = None
                continue
            names = tuple(
                dict.fromkeys(
                    value
                    for key, value in values.items()
                    if (key == "Name" or key.startswith("Name[")) and value
                )
            )
            generic = tuple(
                value
                for key, value in values.items()
                if (key == "GenericName" or key.startswith("GenericName[")) and value
            )
            found[desktop_id] = Entry(
                id=desktop_id,
                path=path,
                names=names,
                generic=generic,
                program=_program(values.get("Exec", "")),
                hidden=values.get("NoDisplay", "").lower() == "true",
            )
    return [entry for entry in found.values() if entry is not None]


def find(name: str, catalog: Sequence[Entry] | None = None) -> Entry:
    """The one program a person would mean by `name`: its ID, then its name in any language,
    then the last part of a reverse-DNS ID or the command it runs, then a part of its name.
    Several equally good matches are listed, never guessed between."""
    query = name.strip().lower()
    query = query[: -len(".desktop")] if query.endswith(".desktop") else query
    catalog = entries() if catalog is None else catalog
    tiers: list[Callable[[Entry], bool]] = [
        lambda e: e.id.lower() == query,
        lambda e: any(n.lower() == query for n in e.names),
        lambda e: e.id.lower().rsplit(".", 1)[-1] == query or e.program == query,
        lambda e: any(n.lower() == query for n in e.generic),
        lambda e: any(query in n.lower() for n in e.names) or query in e.id.lower(),
    ]
    for matches in tiers:
        hits = [entry for entry in catalog if matches(entry)]
        shown = [entry for entry in hits if not entry.hidden] or hits
        if len(shown) == 1:
            return shown[0]
        if len(shown) > 1:
            listed = "; ".join(f"{e.name} (id {e.id})" for e in shown[:_NAMED])
            raise LaunchError(f"{name!r} matches several programs: {listed}. Launch one by its id")
    known = {n: e for e in catalog if not e.hidden for n in (*e.names[:1], e.id)}
    close = difflib.get_close_matches(name, list(known), n=_NAMED, cutoff=0.5)
    hint = "; close names: " + "; ".join(f"{c} (id {known[c].id})" for c in close) if close else ""
    raise LaunchError(
        f"no installed program is called {name!r}{hint}. A command that is not in the menu "
        "can be started with `shell`"
    )


_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _opener(target: str) -> list[str]:
    for argv in (["kioclient", "exec"], ["xdg-open"], ["gio", "open"]):
        if shutil.which(argv[0]):
            return [*argv, target]
    raise LaunchError("this desktop has neither kioclient, xdg-open nor gio to open it with")


def _starter(entry: Entry) -> list[str]:
    if shutil.which("gtk-launch"):
        return ["gtk-launch", entry.id]
    for argv in (["kioclient", "exec"], ["gio", "launch"]):
        if shutil.which(argv[0]):
            return [*argv, str(entry.path)]
    raise LaunchError("this desktop has neither gtk-launch, kioclient nor gio to start it with")


def plan(
    target: str, project_root: Path, catalog: Sequence[Entry] | None = None
) -> tuple[list[str], str]:
    """The command that opens `target` and what it is, said back to the model."""
    target = target.strip()
    if not target:
        raise LaunchError('launch needs `text`: a program such as "Firefox", a file or an address')
    if _URL.match(target) and not re.match(r"^[a-zA-Z]:[\\/]", target):
        return _opener(target), target
    if target.startswith(("/", "~", "./", "../")):
        path = Path(target).expanduser()
        path = path if path.is_absolute() else project_root / path
        if not path.exists():
            raise LaunchError(f"there is no file at {path}")
        return _opener(str(path)), str(path)
    try:
        entry = find(target, catalog)
    except LaunchError:
        local = project_root / target
        with contextlib.suppress(OSError):
            if local.exists():
                return _opener(str(local)), str(local)
        raise
    return _starter(entry), f"{entry.name} (id {entry.id})"


def launch(
    target: str,
    project_root: Path,
    launcher: Launcher = spawn,
    catalog: Sequence[Entry] | None = None,
) -> str:
    """Open it; the line the result starts with."""
    argv, what = plan(target, project_root, catalog)
    status, errors = launcher(argv)
    if status not in (None, 0):
        detail = f": {errors}" if errors else ""
        raise LaunchError(f"{argv[0]} could not open {what} (exit {status}){detail}")
    return f"launch opened {what}"
