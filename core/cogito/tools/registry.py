"""Registration is not exposure.

Everything the application can do is registered once. A run shows the model a smaller
set, chosen by scenario and delegation depth. Every tool definition sits in the context
window on every step, which is why what is registered and what is shown are kept apart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from cogito.errors import CogitoError
from cogito.provider.base import ToolSpec
from cogito.tools.base import Tool

#: What each scenario is for. Which tools that means is derived from the specs themselves.
SCENARIOS: Mapping[str, str] = {
    "default": "everything registered",
    "read_only": "cannot change anything — what a subagent gets to find something out",
    "edit": "read-only plus the ability to act, but not to delegate further",
}


class UnknownTool(CogitoError):
    kind = "unknown_tool"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        name = tool.spec.name
        if name in self._tools:
            raise CogitoError(f"tool {name!r} is already registered")
        self._tools[name] = tool
        return tool

    def register_all(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownTool(
                f"unknown tool {name!r}; available: {', '.join(sorted(self._tools))}"
            ) from exc

    def mcp_specs(self) -> tuple[ToolSpec, ...]:
        """Every registered MCP tool's spec (`McpTool.mcp`), shown this step or not. The
        prompt lists only a few (`expose`), but a call to any one by name still runs: naming
        a tool is the discovery the short listing saves tokens on. Empty without MCP."""
        return tuple(
            tool.spec for tool in self._tools.values() if getattr(tool, "mcp", False)
        )

    def withheld_reasons(self) -> dict[str, str]:
        """Why each tool `expose` leaves out right now is out, for the tools that say: a
        call to one is answered with this instead of "unknown tool" (the web turned off in
        the app's Búsqueda tab). Empty when every registered tool is shown."""
        reasons: dict[str, str] = {}
        for name, tool in self._tools.items():
            available = getattr(tool, "available", None)
            reason = getattr(tool, "withheld_reason", None)
            if callable(available) and callable(reason) and not available():
                if text := reason():
                    reasons[name] = text
        return reasons

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def registered(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def catalog(self, scenario: str = "default", depth: int = 0) -> tuple[ToolSpec, ...]:
        """Every tool this run could use, loaded or not. Used to build the announcement."""
        specs = [tool.spec for tool in self._tools.values()]
        if scenario == "read_only":
            specs = [spec for spec in specs if not (spec.mutates or spec.operates)]
        if scenario != "default" or depth > 0:
            specs = [spec for spec in specs if not spec.delegates]
        if depth > 0:
            # `recall` reads the top-level conversation's lineage, not a subagent's own.
            specs = [spec for spec in specs if not spec.after_compaction]
        return tuple(sorted(specs, key=lambda spec: spec.name))

    def expose(
        self,
        scenario: str = "default",
        depth: int = 0,
        enabled: frozenset[str] | None = None,
    ) -> tuple[ToolSpec, ...]:
        """The specs this run shows the model.

        Every tool definition sits in the context window on every step, so a run shows
        fewer tools than are registered.
        """
        if scenario not in SCENARIOS:
            raise CogitoError(
                f"unknown scenario {scenario!r}; known: {', '.join(sorted(SCENARIOS))}"
            )
        allowed = self.catalog(scenario, depth)
        # A tool can stay hidden until a fact the harness holds makes it useful
        # (`available()`: `ask_document` once a long document is in the conversation).
        # The model does not choose and nothing is loaded on request; it is the same kind
        # of gate as `ToolSpec.after_compaction`. A tool without the method is always shown.
        withheld = {
            name
            for name, tool in self._tools.items()
            if callable(getattr(tool, "available", None)) and not tool.available()  # type: ignore[attr-defined]
        }
        if withheld:
            allowed = tuple(spec for spec in allowed if spec.name not in withheld)
        if enabled is None:
            return allowed
        # Depth was already filtered in catalog(): one level of delegation is the useful
        # one, and deeper is a tree nobody can follow and a budget nobody can predict.
        return tuple(
            spec for spec in allowed if spec.core or spec.name in enabled
        )
