"""The catalog at run time: one ``ComponentService`` per core, one
``SessionCapabilities`` per session.

The window drives ``ComponentService`` through the loopback API: installs and their
progress, "Ignorar", "Ahora no", the "Herramientas" switch, "Del sistema", package help,
"Decisiones guardadas" and the search levels. Every session's resolver also asks it about
a component's state. cli.py injects ``SessionCapabilities`` into ``ToolContext``: ``need``
(a feature's entry point), ``escalate`` (the cascade), ``scan`` (session start),
``request_permission`` and ``step_stopped``.

Nothing here reaches the model. Every outcome is an SSE event for the window, a telemetry
record, or a return value to the tool that asked. The tool decides what, if anything, to
say.

``setup/`` imports only ``config`` and the shared leaves (CONTRIBUTING.md section 2).
What this module needs from higher layers (an event sink, the registry questions wait on,
the turn's cancel token, the store's ``record``) arrives from the composition root as
plain callables and protocols.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from cogito.config import search
from cogito.config.decisions import KINDS, DecisionStore, NoticeStore
from cogito.setup import catalog as default_catalog
from cogito.setup import estella, packages, searxng
from cogito.setup.components import (
    NOT_FOUND,
    SHA256_PATTERN,
    Component,
    Detection,
    Download,
    ProbeEnv,
    ScanRule,
    os_name,
    platform_key,
)
from cogito.setup.install import InstallFailed, install_download, read_marker
from cogito.setup.resolver import Caller, CapabilityResolver, Resolution
from cogito.setup.scan import scan_project

log = logging.getLogger(__name__)

#: Which search level governs a capability's use. The browser is level 3, "Navegador".
LEVEL_BY_CAPABILITY = {"web.browser": "navegador"}

#: The three answers a permission card can give.
PERMISSION_ANSWERS = ("allow_session", "deny", "install_and_allow")


class Questions(Protocol):
    """Where a permission request waits for its answer. ``server.questions.Questions``."""

    def open(self) -> str: ...

    def resolve(
        self, identifier: str, *, abort: Callable[[], bool] | None = None
    ) -> str | None: ...


class _Job:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.ok = False


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


class ComponentService:
    """Application-wide component state. Thread-safe, since the server's threads, every
    turn's thread and the install workers all use it."""

    def __init__(
        self,
        components: Sequence[Component] = default_catalog.CATALOG,
        *,
        scan_rules: Sequence[ScanRule] = default_catalog.SCAN_RULES,
        system_programs: Sequence[str] = default_catalog.SYSTEM_PROGRAMS,
        decisions: DecisionStore | None = None,
        notices: NoticeStore | None = None,
        env: ProbeEnv | None = None,
        platform: str | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        spawn: Callable[[Callable[[], None]], None] = _thread,
        today: Callable[[], date] = date.today,
        finishers: dict[str, Callable[[Path], None]] | None = None,
        unused: Callable[[str], bool] = lambda component_id: False,
        external: Callable[[str], bool] = lambda component_id: False,
    ):
        self._components = {c.id: c for c in components}
        #: Whether the person's settings never use a component (the app's: the graph model
        #: while Estella runs lite). Such a component, not installed, is not offered.
        self._unused = unused
        #: Whether the person already has this component outside the app (their own Cogito
        #: Estella, configured or on PATH): the tab says so instead of «No instalado».
        self._external = external
        self._order = tuple(c.id for c in components)
        self._scan_rules = tuple(scan_rules)
        self._system_programs = tuple(p for p in system_programs if p in self._components)
        self._decisions = decisions if decisions is not None else DecisionStore()
        self._notices = notices if notices is not None else NoticeStore()
        self._env = env if env is not None else ProbeEnv()
        self._os = os_name(self._env.platform)
        self._platform = platform if platform is not None else platform_key(self._env.platform)
        self._opener = opener
        self._spawn = spawn
        self._today = today
        #: Steps a `Download` names in ``finish``, run on the components directory once its
        #: files are unpacked.
        self._finishers = finishers if finishers is not None else {
            "searxng": searxng.finish_install,
            "estella": estella.finish_install,
            "estella-full": estella.finish_full_install,
        }
        self._lock = threading.RLock()
        self._versions: dict[str, int] = {}
        self._ignored: set[str] = set()
        self._jobs: dict[str, _Job] = {}
        self._failures: dict[str, dict[str, str]] = {}
        #: "Instalar y permitir" whose install has not succeeded yet.
        self._allow_after_install: set[str] = set()
        self._help_cache: dict[tuple[str, str], bool] = {}
        self._broadcast: Callable[[dict[str, Any]], None] = lambda event: None

    # -- wiring --------------------------------------------------------------------------

    def set_broadcast(self, broadcast: Callable[[dict[str, Any]], None]) -> None:
        """Where install events go: every open session's stream (see server/app.py)."""
        self._broadcast = broadcast

    def session(
        self,
        project_root: Path,
        *,
        emit: Callable[[dict[str, Any]], None],
        cancel: Any,
        questions: Questions,
        record: Callable[..., Any],
        session_id: Callable[[], str],
        on_waiting: Callable[[bool], None] | None = None,
    ) -> SessionCapabilities:
        return SessionCapabilities(
            self,
            project_root,
            emit=emit,
            cancel=cancel,
            questions=questions,
            record=record,
            session_id=session_id,
            on_waiting=on_waiting,
        )

    def component(self, component_id: str) -> Component | None:
        return self._components.get(component_id)

    @property
    def components(self) -> tuple[Component, ...]:
        return tuple(self._components[i] for i in self._order)

    @property
    def scan_rules(self) -> tuple[ScanRule, ...]:
        return self._scan_rules

    def spawn(self, work: Callable[[], None]) -> None:
        self._spawn(work)

    # -- what the resolver asks (resolver.ComponentState) ---------------------------------

    def detect(self, component: Component) -> Detection:
        if component.detect is None:
            return NOT_FOUND
        return component.detect.check(self._env)

    def is_declined(self, component_id: str, what: str | None = None) -> bool:
        """Declined at all or, with ``what``, declined to install or to use."""
        return self._decisions.is_declined(component_id, what)

    def is_ignored(self, component_id: str) -> bool:
        with self._lock:
            return component_id in self._ignored

    def is_installing(self, component_id: str) -> bool:
        with self._lock:
            return component_id in self._jobs

    def version(self, component_id: str) -> int:
        with self._lock:
            return self._versions.get(component_id, 0)

    def platform(self) -> str | None:
        return self._platform

    def first_notice(self, component_id: str) -> bool:
        return self._notices.first_time(component_id)

    def package_help(self, component: Component) -> dict[str, Any]:
        with self._lock:
            cache = self._help_cache
        return packages.package_help(component, self._env, self._os, cache).as_event()

    def _bump(self, component_id: str) -> None:
        with self._lock:
            self._versions[component_id] = self._versions.get(component_id, 0) + 1

    # -- installs ------------------------------------------------------------------------

    def install(self, component_id: str) -> dict[str, Any]:
        """Start installing in the background. Progress and the outcome are broadcast."""
        component = self._components.get(component_id)
        if component is None:
            return {"error": "not_found"}
        if component.consent == "never" or not isinstance(component.install, Download):
            return {"error": "not_installable"}
        artifact = component.artifact(self._platform)
        if artifact is None:
            return {"error": "no_build"}
        if not SHA256_PATTERN.match(artifact.sha256):
            # Nothing unpinned is ever downloaded (the release gate,
            # `python -m cogito.setup.catalog`, refuses to ship one). Every card following
            # this component (the proposal's, and a permission's «Instalar y permitir»)
            # sees it fail with the design's own words and an empty "error".
            log.warning(
                "%s has no pinned SHA-256 for %s; not installing", component_id, self._platform
            )
            with self._lock:
                self._failures[component.id] = {"kind": "unpinned", "error": ""}
            self._bump(component_id)
            self._broadcast(
                {
                    "type": "component_install_failed",
                    "id": component.id,
                    "name": component.name,
                    "kind": "unpinned",
                    "error": "",
                }
            )
            return {"error": "unpinned"}
        with self._lock:
            if component_id in self._jobs:
                return {"error": "busy"}
            job = self._jobs[component_id] = _Job()
            self._failures.pop(component_id, None)
            self._ignored.discard(component_id)
        self._bump(component_id)
        self._broadcast(
            {
                "type": "component_install_started",
                "id": component.id,
                "name": component.name,
                "size_bytes": artifact.size_bytes,
            }
        )
        self._spawn(lambda: self._run_install(component, job))
        return {"status": "installing"}

    def retry(self, component_id: str) -> dict[str, Any]:
        """ "Reintentar" on a failed card: the same install, again."""
        return self.install(component_id)

    def _run_install(self, component: Component, job: _Job) -> None:
        assert isinstance(component.install, Download)
        artifact = component.install.for_platform(self._platform)
        started = time.monotonic()
        event: dict[str, Any]
        try:
            record = install_download(
                component.id,
                component.install,
                artifact,
                root=self._env.components_dir,
                opener=self._opener,
                today=self._today,
                on_progress=lambda downloaded, total: self._broadcast(
                    {
                        "type": "component_install_progress",
                        "id": component.id,
                        "downloaded": downloaded,
                        "total": total,
                    }
                ),
            )
            self._finish(component)
        except InstallFailed as failure:
            with self._lock:
                self._failures[component.id] = {"kind": failure.kind, "error": failure.text}
            event = {
                "type": "component_install_failed",
                "id": component.id,
                "name": component.name,
                "kind": failure.kind,
                "error": failure.text,
            }
        except Exception as exc:  # a bug, not a network condition; still never a silent hang
            log.exception("installing %s", component.id)
            text = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._failures[component.id] = {"kind": "other", "error": text}
            event = {
                "type": "component_install_failed",
                "id": component.id,
                "name": component.name,
                "kind": "other",
                "error": text,
            }
        else:
            job.ok = True
            with self._lock:
                if component.id in self._allow_after_install:
                    self._allow_after_install.discard(component.id)
                    self._decisions.allow(component.id)
            event = {
                "type": "component_installed",
                "id": component.id,
                "name": component.name,
                "size_bytes": record["size_bytes"],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        finally:
            with self._lock:
                self._jobs.pop(component.id, None)
            self._bump(component.id)
        job.done.set()
        self._broadcast(event)

    def _finish(self, component: Component) -> None:
        """The step a download names (a venv built from the unpacked files). If it fails,
        everything the install wrote is removed, so the component reads as not installed."""
        assert isinstance(component.install, Download)
        name = component.install.finish
        if not name:
            return
        root = self._env.components_dir
        try:
            self._finishers[name](root)
        except BaseException:
            shutil.rmtree(root / component.id, ignore_errors=True)
            raise

    def failure(self, component_id: str) -> dict[str, str] | None:
        """How the last install of this component failed (``kind`` and ``error``), if it did."""
        with self._lock:
            found = self._failures.get(component_id)
            return dict(found) if found else None

    def wait_install(
        self, component_id: str, *, abort: Callable[[], bool] | None = None, timeout: float = 3600.0
    ) -> bool:
        """Block until the running install (if any) ends; True when the component is here."""
        with self._lock:
            job = self._jobs.get(component_id)
        deadline = time.monotonic() + timeout
        while job is not None and not job.done.wait(0.2):
            if (abort is not None and abort()) or time.monotonic() > deadline:
                return False
        component = self._components.get(component_id)
        return component is not None and self.detect(component).found

    def ignore(self, component_id: str) -> dict[str, Any]:
        """ "Ignorar" on a failed card: carry on without it for as long as this core runs."""
        if component_id not in self._components:
            return {"error": "not_found"}
        with self._lock:
            self._ignored.add(component_id)
            self._failures.pop(component_id, None)
        self._bump(component_id)
        return {"status": "ignored"}

    def decline(self, component_id: str) -> dict[str, Any]:
        """ "Ahora no" on a proposal: saved for every project until "Reactivar"."""
        if component_id not in self._components:
            return {"error": "not_found"}
        decision = self._decisions.decline(component_id, "install")
        self._bump(component_id)
        return {
            "decision": {
                "id": decision.id,
                "what": decision.what,
                "declined_at": decision.declined_at,
            }
        }

    def decline_use(self, component_id: str) -> None:
        """ "No" on a permission request: saved for every project until "Reactivar"."""
        # Also forgets «Permitir»: the switch off is the one way to stop a remembered allow.
        self._decisions.decline(component_id, "use")
        self._bump(component_id)

    def set_enabled(self, component_id: str, enabled: bool) -> dict[str, Any]:
        """The Herramientas switch. Off is a saved "do not use"; on reactivates both kinds."""
        if (
            component_id not in self._components
            or self._components[component_id].consent == "never"
        ):
            return {"error": "not_found"}
        if enabled:
            for what in KINDS:
                self._decisions.reactivate(component_id, what)
            self._bump(component_id)
        else:
            self.decline_use(component_id)
        return {"enabled": enabled}

    def _installable(self, component: Component) -> bool:
        """Whether this machine can install it now: a ``Download`` with a real, pinned
        digest for this platform. The desktop's Herramientas tab hides a row that answers
        False here and is not installed ("hide until real", cogito-lab
        experiments/2026-09-29-ux-audit, UX-11)."""
        artifact = component.artifact(self._platform)
        return artifact is not None and bool(SHA256_PATTERN.match(artifact.sha256))

    def list_components(self) -> list[dict[str, Any]]:
        """The Herramientas tab: every component Cogito can install."""
        listed = []
        for component in self.components:
            if component.consent == "never":
                continue
            installed = self.detect(component).found
            marker = read_marker(self._env.components_dir, component.id) if installed else None
            with self._lock:
                installing = component.id in self._jobs
                failure = self._failures.get(component.id)
            state = (
                "installing"
                if installing
                else "installed"
                if installed
                else "failed"
                if failure
                else "not_installed"
            )
            entry: dict[str, Any] = {
                "id": component.id,
                "name": component.name,
                "description": component.description,
                "size_bytes": component.size_bytes(self._platform),
                "installed": installed,
                "installed_at": (marker or {}).get("installed_at"),
                "version": (marker or {}).get("version"),
                "enabled": not self._decisions.is_declined(component.id, "use"),
                "state": state,
                "external": not installed and self._external(component.id),
                "installable": self._installable(component)
                and (installed or not self._unused(component.id)),
            }
            if failure and state == "failed":
                entry.update(failure)
            listed.append(entry)
        return listed

    # -- permissions ---------------------------------------------------------------------

    def is_allowed(self, component_id: str) -> bool:
        return self._decisions.is_allowed(component_id)

    def allow(self, component_id: str) -> None:
        """«Permitir»: remembered for every project until the switch is turned off."""
        self._decisions.allow(component_id)

    def allow_after_install(self, component_id: str) -> None:
        with self._lock:
            self._allow_after_install.add(component_id)

    def level_for(self, component: Component) -> str | None:
        """The search level ("on"/"ask"/"off") that governs this component, if any."""
        for capability in component.provides:
            key = LEVEL_BY_CAPABILITY.get(capability)
            if key is not None:
                return search.load()[key]
        return None

    # -- system programs, help, decisions, search ---------------------------------------

    def system_programs(self) -> dict[str, Any]:
        """The Del sistema tab: each program's version and path, or that it is missing."""
        programs = []
        for program_id in self._system_programs:
            component = self._components[program_id]
            found = self.detect(component)
            programs.append(
                {
                    "id": component.id,
                    "name": component.name,
                    "found": found.found,
                    "version": found.version,
                    "path": found.path,
                    "variant": found.variant,
                }
            )
        return {
            "os": self._os,
            "managers": list(packages.present(self._os, self._env)),
            "programs": programs,
        }

    def program_help(self, program_id: str) -> dict[str, Any]:
        """What the missing-program card shows, the same card "Cómo instalarlo" opens."""
        component = self._components.get(program_id)
        if component is None or component.consent != "never":
            return {"error": "not_found"}
        return self.package_help(component)

    def decisions_list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": d.id,
                "what": d.what,
                "declined_at": d.declined_at,
                "name": self._components[d.id].name if d.id in self._components else d.id,
            }
            for d in self._decisions.list()
        ]

    def reactivate(self, component_id: str, what: str) -> dict[str, Any]:
        if what not in KINDS or not self._decisions.reactivate(component_id, what):
            return {"error": "not_found"}
        self._bump(component_id)
        return {"reactivated": True}

    @staticmethod
    def _levels(values: dict[str, Any]) -> dict[str, Any]:
        levels = {**values, "available": search.AVAILABLE}
        # Present only in the app, where the core runs the local SearXNG.
        local = searxng.current()
        if local is not None:
            levels["local_search"] = local.status()
        return levels

    def search_levels(self) -> dict[str, Any]:
        return self._levels(search.load())


class SessionCapabilities:
    """What one session's tools may ask of the catalog. Injected by cli.py as
    ``ToolContext.capabilities``; ``tools/`` only ever sees the protocol in tools/base.py."""

    def __init__(
        self,
        service: ComponentService,
        project_root: Path,
        *,
        emit: Callable[[dict[str, Any]], None],
        cancel: Any,
        questions: Questions,
        record: Callable[..., Any],
        session_id: Callable[[], str],
        on_waiting: Callable[[bool], None] | None = None,
    ):
        self._service = service
        self._root = project_root
        self._emit = emit
        self._cancel = cancel
        self._questions = questions
        self._on_waiting = on_waiting or (lambda waiting: None)
        self.resolver = CapabilityResolver(
            service.components, service, emit=emit, record=record, session_id=session_id
        )

    # The three callers of need(): a feature, the cascade, and the scan.

    def need(self, capability: str) -> Resolution:
        """A feature's entry point (e.g. an image attachment asking for vision)."""
        return self.resolver.need(capability, self._root, caller=Caller.FEATURE)

    def escalate(self, capability: str) -> Resolution:
        """The cascade's next level, called by the shared cascade mechanism only."""
        return self.resolver.need(capability, self._root, caller=Caller.CASCADE)

    def scan(self) -> tuple[Resolution, ...]:
        """Session start: every capability the project's files say it needs."""
        needed = scan_project(self._root, self._service.scan_rules)
        return tuple(self.resolver.need(c, self._root, caller=Caller.SCAN) for c in needed)

    def start_scan(self) -> None:
        """The scan, off the thread that opened the session: package-manager index queries
        can take seconds and must never delay the window."""

        def work() -> None:
            try:
                self.scan()
            except Exception:
                log.exception("project scan of %s failed", self._root)

        self._service.spawn(work)

    def step_stopped(self, reason: str, where: str) -> None:
        """ "Paso detenido": a step that cannot complete (a CAPTCHA, say). Window only."""
        self._emit({"type": "step_stopped", "reason": reason, "where": where})

    def request_permission(
        self, component_id: str, action: str, scope: str | None = None, *, target: str | None = None
    ) -> bool:
        """Ask the window before using a permission-class component; True means go ahead.

        "No" is saved for every project; "Permitir" too, until the component's switch is
        turned off; "Instalar y permitir" installs first and then allows the same way.
        It waits until answered; «Detener» or a closing window is a refusal that is not saved.
        """
        service = self._service
        component = service.component(component_id)
        if component is None:
            return False
        level = service.level_for(component)
        if level == "off" or service.is_declined(component.id, "use"):
            return False
        installed = service.detect(component).found
        if installed and (level == "on" or service.is_allowed(component.id)):
            return True
        if not installed:
            if service.is_declined(component.id, "install"):
                return False
            if component.size_bytes(service.platform()) is None:
                return False
        identifier = self._questions.open()
        self._on_waiting(True)
        self._emit(
            {
                "type": "permission_request",
                "id": identifier,
                "component": component.id,
                "name": component.name,
                "description": component.description,
                "size_bytes": component.size_bytes(service.platform()),
                "installed": installed,
                "action": action,
                "target": target,
                "scope": scope or component.scope,
            }
        )
        # Waits until the person answers, as a guard question does (`server/approvals.py`).
        answer = self._questions.resolve(
            identifier,
            timeout=None,
            abort=lambda: bool(getattr(self._cancel, "cancelled", False)),
        )
        self._on_waiting(False)
        if answer not in PERMISSION_ANSWERS:
            reason = "interrupted" if getattr(self._cancel, "cancelled", False) else "timeout"
            self._emit(
                {
                    "type": "permission_resolved",
                    "id": identifier,
                    "decision": None,
                    "reason": reason,
                }
            )
            return False
        self._emit(
            {"type": "permission_resolved", "id": identifier, "decision": answer, "reason": "user"}
        )
        if answer == "deny":
            service.decline_use(component.id)
            return False
        if installed:
            service.allow(component.id)
            return True
        service.allow_after_install(component.id)
        started = service.install(component.id)
        if started.get("error") not in (None, "busy"):
            return False
        return service.wait_install(
            component.id, abort=lambda: bool(getattr(self._cancel, "cancelled", False))
        ) and service.is_allowed(component.id)
