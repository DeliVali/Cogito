"""What a tool is.

A tool takes a context and arguments and returns a result. It never sees the loop or the
provider connection, and it does not decide how much of its output survives: the loop clips
every result the same way. CONTRIBUTING.md section 6 is enforced here by construction: a
spec carries its own budget, and a result carries the telemetry recorded on the :ToolCall
node.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cogito.cancel import CancelToken
from cogito.config.settings import Settings
from cogito.context import images as image_info
from cogito.context.offload import ArtifactRef, ArtifactStore
from cogito.provider.base import (
    ImageBlock,
    Provenance,
    ToolSpec,
)


class Capabilities(Protocol):
    """What a tool may ask of the component catalog.

    cli.py injects one per session (``setup.service.SessionCapabilities``), like the delegate
    tool's engine factory, so ``tools/`` never imports ``setup/``. This protocol is the whole
    vocabulary. Nothing asked here reaches the model directly: answers are return values, and
    anything the window shows travels as events.
    """

    def need(self, capability: str) -> Any:
        """A feature's entry point. The result's ``kind`` is ``available``,
        ``unavailable`` (with ``reason``) or ``proposed``."""
        ...

    def escalate(self, capability: str) -> Any:
        """The search cascade's next level. Called by the shared cascade only."""
        ...

    def request_permission(
        self, component: str, action: str, scope: str | None = None, *, target: str | None = None
    ) -> bool:
        """Ask the window before using a permission-class component (the browser)."""
        ...

    def step_stopped(self, reason: str, where: str) -> None:
        """A step that could not complete ("Paso detenido"), shown to the window only."""
        ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool is allowed to know about the run it is part of."""

    project_root: Path
    session_id: str
    settings: Settings
    artifacts: ArtifactStore
    cancel: CancelToken
    #: The session's component catalog, when the run has a window to show proposals and
    #: permission requests to (``cogito serve``). ``None`` under ``cogito chat``.
    capabilities: Capabilities | None = None
    #: The call this run answers, set only for a tool that delegates (`ToolSpec.delegates`):
    #: a subagent's runner names it on the subagent's events, so the window draws them in
    #: that call's row. Empty for every other tool.
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What the model sees, plus what the graph records.

    An error is a result, not an exception: ``is_error`` set, with content that says what to
    do next. A tool that raises has a bug, and so does one that returns a bare "not found".
    """

    content: str
    is_error: bool = False
    error_kind: str = ""
    artifact: ArtifactRef | None = None
    #: Recorded on the :ToolCall node: edit formats, repair names, timings.
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Paths this call read or wrote, linked as (:ToolCall)-[:TOUCHED]->(:File).
    files_touched: tuple[str, ...] = ()
    #: Refines the provenance the tool's spec declared. A `read` knows the path and revision
    #: it returned, which its spec cannot; an error may be compact where the tool is usually
    #: bulk. Fields the harness owns are not on this type.
    provenance: Provenance | None = None
    #: Images the model should see with this result (a screenshot): each a stashed copy
    #: (`ArtifactStore.stash_bytes`), with its marker in ``content``. Sent only to a model
    #: that sees images, and only the newest few (`provider.base.RESULT_IMAGES_KEPT`).
    images: tuple[ImageBlock, ...] = ()

    @classmethod
    def error(cls, content: str, kind: str, **metadata: Any) -> ToolResult:
        return cls(content=content, is_error=True, error_kind=kind, metadata=metadata)


@runtime_checkable
class Tool(Protocol):
    """Callable by the model, testable without a model."""

    spec: ToolSpec

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult: ...


def parameters(
    properties: Mapping[str, Mapping[str, Any]], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Build a JSON Schema object in the shapes llama.cpp's grammar compiler accepts.

    No ``pattern``, ``$ref`` or ``format``: they break the JSON-Schema-to-GBNF conversion,
    and a tool whose calls cannot be grammar-constrained loses that way of rescuing a weak
    model.
    """
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def result_image(
    artifacts: ArtifactStore | None, data: bytes, *, name: str
) -> tuple[ImageBlock | None, str]:
    """An image a tool returns, kept like an attached one: stashed by its digest, read
    only when a request is encoded. Returns the block and its marker, the line that goes in
    the result's text; or no block and a marker saying why (not an image this harness
    reads, too large, or nowhere to keep it)."""
    info = image_info.sniff(data[:65536])
    if info is None:
        return None, f"[{name}: not a PNG, JPEG, GIF or WebP image, so it is not shown]"
    if len(data) > image_info.MAX_BYTES or max(info.width, info.height) > image_info.MAX_SIDE:
        return None, f"[{name}: {info.width}x{info.height}, too large to show]"
    if artifacts is None:
        return None, f"[{name}: {info.width}x{info.height}, nowhere to keep it]"
    ref = artifacts.stash_bytes(data, suffix=f".{info.format}", kind="image")
    block = ImageBlock(
        media_type=info.media_type,
        path=str(ref.path),
        name=name,
        width=info.width,
        height=info.height,
    )
    return block, f"[{name}: {info.width}x{info.height} image, shown with this result]"
