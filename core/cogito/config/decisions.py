"""Saved decisions and notices already shown. Both are global, never keyed by project.

A decision is something a person said no to: "Ahora no" on a proposal to install a
component, "No" on a permission request, or the component's switch turned off in
"Herramientas y permisos". It holds for every project until the person chooses "Reactivar"
(spec 2026-09-28, addendum: "Saved decisions are global"). The file is
``~/.config/cogito/decisions.toml``::

    [[declined]]
    id = "headless-browser"
    what = "install"
    declined_at = "2026-09-28"

``what`` is a kind, not prose: ``install`` (do not install it) or ``use`` (do not use it).
The window builds the sentence ("No instalar {nombre}") from its own fixed strings.

«Permitir» on a permission request is kept in the same file, until the component's switch
is turned off (2026-10-30: "quiero que quede guardado para siempre, solo cuando la desactive
dejaría de usarla")::

    [[allowed]]
    id = "headless-browser"

A notice is a card shown once per program, globally: the missing-program card. Recording
it is not a decision (nothing was declined), so it lives in its own file and never appears
in the list of decisions.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cogito.config import paths
from cogito.config.tomlfile import quote, read, write_atomically
from cogito.errors import ConfigError

#: The two things a person can decline.
KINDS = ("install", "use")


@dataclass(frozen=True, slots=True)
class Decision:
    id: str
    what: str
    #: ISO date, local calendar day. The window shows it as "Rechazado el DD/MM/AAAA".
    declined_at: str


class DecisionStore:
    """The file, re-read on every call: two sessions in one core share it, and a person
    may reactivate from the window while a turn is deciding whether to propose."""

    def __init__(self, path: Path | None = None, *, today: Callable[[], date] = date.today):
        self._path = path if path is not None else paths.decisions_file()
        self._today = today
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def list(self) -> tuple[Decision, ...]:
        with self._lock:
            return self._load()

    def is_declined(self, component_id: str, what: str | None = None) -> bool:
        """Whether ``component_id`` was declined, for ``what`` or for anything at all."""
        return any(d.id == component_id and (what is None or d.what == what) for d in self.list())

    def decline(self, component_id: str, what: str) -> Decision:
        """Record a decline. Declining again replaces the date instead of adding a row.
        Declining its use also forgets that it was allowed."""
        _check_kind(what)
        with self._lock:
            decision = Decision(component_id, what, self._today().isoformat())
            kept = [d for d in self._load() if (d.id, d.what) != (component_id, what)]
            allowed = self._load_allowed()
            if what == "use":
                allowed = [a for a in allowed if a != component_id]
            self._save([*kept, decision], allowed)
            return decision

    def reactivate(self, component_id: str, what: str) -> bool:
        """Forget one decision. False when there was no such decision."""
        with self._lock:
            current = self._load()
            kept = [d for d in current if (d.id, d.what) != (component_id, what)]
            if len(kept) == len(current):
                return False
            self._save(kept, self._load_allowed())
            return True

    def is_allowed(self, component_id: str) -> bool:
        """Whether «Permitir» was answered for this component and its use not declined since."""
        return component_id in self.allowed()

    def allowed(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._load_allowed())

    def allow(self, component_id: str) -> None:
        """Remember «Permitir» for every later session and project."""
        with self._lock:
            allowed = self._load_allowed()
            if component_id in allowed:
                return
            self._save(list(self._load()), [*allowed, component_id])

    def _load_allowed(self) -> list[str]:
        rows = read(self._path).get("allowed", [])
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows
        ):
            raise ConfigError(f"{self._path}: every 'allowed' entry needs an id")
        return [row["id"] for row in rows]

    def _load(self) -> tuple[Decision, ...]:
        rows = read(self._path).get("declined", [])
        if not isinstance(rows, list):
            raise ConfigError(f"{self._path}: 'declined' must be an array of tables")
        loaded = []
        for row in rows:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(key), str) for key in ("id", "what", "declined_at")
            ):
                raise ConfigError(f"{self._path}: every decision needs id, what and declined_at")
            loaded.append(Decision(row["id"], row["what"], row["declined_at"]))
        return tuple(loaded)

    def _save(self, decisions: list[Decision], allowed: list[str]) -> None:
        blocks = [
            f"[[declined]]\nid = {quote(d.id)}\nwhat = {quote(d.what)}\n"
            f"declined_at = {quote(d.declined_at)}\n"
            for d in decisions
        ]
        blocks += [f"[[allowed]]\nid = {quote(component_id)}\n" for component_id in allowed]
        write_atomically(self._path, "\n".join(blocks))


class NoticeStore:
    """Programs whose missing-program card has already been shown, on any project."""

    def __init__(self, path: Path | None = None):
        self._path = path if path is not None else paths.notices_file()
        self._lock = threading.Lock()

    def seen(self) -> tuple[str, ...]:
        with self._lock:
            return self._load()

    def first_time(self, program_id: str) -> bool:
        """True exactly once per program: records it and says whether it was new."""
        with self._lock:
            seen = self._load()
            if program_id in seen:
                return False
            ordered = sorted({*seen, program_id})
            write_atomically(self._path, f"seen = [{', '.join(quote(p) for p in ordered)}]\n")
            return True

    def _load(self) -> tuple[str, ...]:
        seen = read(self._path).get("seen", [])
        if not isinstance(seen, list) or not all(isinstance(p, str) for p in seen):
            raise ConfigError(f"{self._path}: 'seen' must be an array of strings")
        return tuple(sorted(seen))


def _check_kind(what: str) -> None:
    if what not in KINDS:
        raise ValueError(f"unknown decision kind {what!r}; expected one of {', '.join(KINDS)}")
