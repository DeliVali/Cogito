"""The capability resolver: is a capability available, unavailable, or worth proposing?

``CapabilityResolver.need(capability, project, *, caller)`` follows the spec's decision
function (cogito-lab ``specs/2026-09-28-component-catalog-design.md``), with its
addendum's global decisions::

    component = the one providing `capability`
    if component.detect() succeeds       -> Available
    if component.consent == "never"      -> Unavailable(manual_hint)   + program_missing, once
    if declined (install or use)         -> Unavailable("declined")
    if ignored after a failed install    -> Unavailable("ignored")
    if an install is already running     -> Unavailable("installing")
    if no build exists for this machine  -> Unavailable("no_build_for_platform")
    if consent == "auto", < 20 MB, and neither network nor permission class
                                         -> install in the background; Unavailable("installing")
    else                                 -> component_proposal; Proposed

It has exactly three callers, named by ``Caller`` and enforced by a test over the whole
package: the project scan at session start, the cascade escalation the web-search work
will call, and a feature's own entry point. Nothing it decides reaches the model. A
missing compiler is a ``program_missing`` event for the window, a proposal is a
``component_proposal`` event, and the result goes back to the caller only.

There is one resolver per session, so results are cached per session. A cached entry is
invalidated when the component's state changes (installed, ignored, declined,
reactivated). Every call, cached or not, is recorded as ``capability_resolved`` with its
reason.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol

from cogito.ids import new_id
from cogito.setup.components import AUTO_INSTALL_MAX_BYTES, Component, Detection


class Caller(StrEnum):
    """The only three callers of ``need()`` (spec, "The only three callers of need()")."""

    SCAN = "scan"
    CASCADE = "cascade"
    FEATURE = "feature"


@dataclass(frozen=True, slots=True)
class Available:
    capability: str
    component: str
    version: str | None = None
    path: str | None = None

    kind: ClassVar[str] = "available"
    reason: ClassVar[str] = "detected"


@dataclass(frozen=True, slots=True)
class Unavailable:
    capability: str
    component: str | None
    reason: str

    kind: ClassVar[str] = "unavailable"


@dataclass(frozen=True, slots=True)
class Proposed:
    capability: str
    component: Component

    kind: ClassVar[str] = "proposed"
    reason: ClassVar[str] = "proposed"


Resolution = Available | Unavailable | Proposed


class ComponentState(Protocol):
    """What the resolver asks the application. ``service.ComponentService`` answers."""

    def detect(self, component: Component) -> Detection: ...

    def is_declined(self, component_id: str) -> bool: ...

    def is_ignored(self, component_id: str) -> bool: ...

    def is_installing(self, component_id: str) -> bool: ...

    def install(self, component_id: str) -> dict[str, Any]: ...

    def version(self, component_id: str) -> int: ...

    def platform(self) -> str | None: ...

    def first_notice(self, component_id: str) -> bool: ...

    def package_help(self, component: Component) -> dict[str, Any]: ...


class CapabilityResolver:
    """One per session: its cache is the session's."""

    def __init__(
        self,
        components: Sequence[Component],
        state: ComponentState,
        *,
        emit: Callable[[dict[str, Any]], None],
        record: Callable[..., Any],
        session_id: Callable[[], str],
    ):
        self._providers: dict[str, Component] = {}
        for component in components:
            for capability in component.provides:
                self._providers.setdefault(capability, component)
        self._state = state
        self._emit = emit
        self._record = record
        self._session_id = session_id
        self._cache: dict[tuple[str, str], tuple[Resolution, int]] = {}

    def need(self, capability: str, project: Path, *, caller: Caller) -> Resolution:
        caller = Caller(caller)  # anything but the three callers is a ValueError
        component = self._providers.get(capability)
        key = (capability, str(project))
        version = self._state.version(component.id) if component else 0
        cached = self._cache.get(key)
        if cached is not None and cached[1] == version:
            result, was_cached = cached[0], True
        else:
            result, was_cached = self._resolve(capability, component, caller), False
            self._cache[key] = (result, version)
        self._record(
            "capability_resolved",
            id=new_id(),
            session_id=self._session_id(),
            capability=capability,
            project=str(project),
            caller=caller.value,
            outcome=result.kind,
            component=component.id if component else None,
            reason=result.reason,
            cached=was_cached,
        )
        return result

    def _resolve(self, capability: str, component: Component | None, caller: Caller) -> Resolution:
        if component is None:
            return Unavailable(capability, None, "no_provider")
        detection = self._state.detect(component)
        if detection.found:
            return Available(capability, component.id, detection.version, detection.path)
        if component.consent == "never":
            if self._state.first_notice(component.id):
                self._emit(
                    {
                        "type": "program_missing",
                        **self._state.package_help(component),
                        "capability": capability,
                    }
                )
            return Unavailable(capability, component.id, component.manual_hint or "not installed")
        for blocked, reason in (
            (self._state.is_declined, "declined"),
            (self._state.is_ignored, "ignored"),
            (self._state.is_installing, "installing"),
        ):
            if blocked(component.id):
                return Unavailable(capability, component.id, reason)
        size = component.size_bytes(self._state.platform())
        if size is None:
            return Unavailable(capability, component.id, "no_build_for_platform")
        quiet = not component.permission and not component.network
        if component.consent == "auto" and size < AUTO_INSTALL_MAX_BYTES and quiet:
            self._state.install(component.id)
            return Unavailable(capability, component.id, "installing")
        self._emit(
            {
                "type": "component_proposal",
                "id": component.id,
                "name": component.name,
                "description": component.description,
                "size_bytes": size,
                "capability": capability,
                "caller": caller.value,
            }
        )
        return Proposed(capability, component)
