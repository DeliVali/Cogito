"""Offloading tool output to disk.

Anything too large for the window is stored whole and replaced in the context by a pointer
the model can follow. Nothing is truncated silently: the result always says where the rest
went and how to get it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from cogito.config import paths
from cogito.ids import new_id

#: Kinds saved under their content's digest (`ArtifactStore.stash`): a person's message,
#: and its readable form for the document index (`cli._documents_from_offloads`).
ADDRESSED_KINDS = frozenset({"user_message", "user_message_text"})


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    id: str
    path: Path
    sha256: str
    bytes: int
    lines: int
    kind: str

    def as_properties(self) -> dict[str, object]:
        """The shape the graph stores, for the :Artifact node."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "bytes": self.bytes,
            "lines": self.lines,
            "kind": self.kind,
        }


class ArtifactStore:
    """Per-session storage under the project, so deleting a project takes it with it."""

    def __init__(self, project_root: Path, session_id: str) -> None:
        self._directory = paths.artifacts_dir(project_root, session_id)

    @property
    def directory(self) -> Path:
        return self._directory

    def stash(self, content: str, *, kind: str = "text", suffix: str = ".txt") -> ArtifactRef:
        """Write ``content`` whole and return how to find it again. A person's message
        (`ADDRESSED_KINDS`) is named by its SHA-256 and written once, so the same text
        offloaded again (on a reopening, or by a capped seed on every fold) is one file."""
        self._directory.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        addressed = kind in ADDRESSED_KINDS
        identifier = digest if addressed else new_id()
        target = self._directory / f"{identifier}{suffix}"
        # The digest names the content, not its length. A same-size edit must not make
        # us return a reference claiming the original bytes while retaining other text.
        if not (addressed and target.is_file() and target.read_bytes() == encoded):
            target.write_bytes(encoded)
        return ArtifactRef(
            id=identifier,
            path=target,
            sha256=digest,
            bytes=len(encoded),
            lines=content.count("\n") + (0 if content.endswith("\n") or not content else 1),
            kind=kind,
        )

    def stash_bytes(self, data: bytes, *, suffix: str, kind: str = "image") -> ArtifactRef:
        """Keep ``data`` (an attached image) under its SHA-256, written once: the same
        image attached again, or kept by a fold's copy of the tail, is one file. Its path is
        what an `ImageBlock` holds, so the journal never carries the bytes."""
        self._directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        target = self._directory / f"{digest}{suffix}"
        if not (target.is_file() and target.read_bytes() == data):
            target.write_bytes(data)
        return ArtifactRef(
            id=digest, path=target, sha256=digest, bytes=len(data), lines=0, kind=kind
        )
