"""Installing a ``Download``: fetch, verify, and only then unpack.

Order matters. The file is streamed to a staging directory while its SHA-256 is computed.
If the digest is not the one the catalog pinned, nothing is unpacked and nothing reaches
the component's directory. An artifact with no pinned digest is refused before any
request, so a release installs exactly what the lab measured or nothing.

A failure carries a kind the window can act on: ``disk`` (ENOSPC), ``offline`` (connection
refused or reset, DNS, timeouts) or ``other`` (an HTTP error, a digest mismatch, a broken
archive, a download larger than declared). It also carries the error's own text, which the
card shows verbatim.

Unpacked files get plain modes whatever the archive says: 0755 for directories and
executables, 0644 for the rest. Nothing Cogito installs is writable by anyone else. A
reinstall of the same version moves the working copy aside and puts it back if anything
after that fails, so a failed reinstall never loses the install that worked.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import shutil
import socket
import stat
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, BinaryIO

from cogito.setup.components import SHA256_PATTERN, Artifact, Download

#: The file ``PathProbe("<components>/<id>/installed.json")`` detects.
MARKER = "installed.json"

_CHUNK = 64 * 1024
#: Progress is reported at most this often, plus once at the end.
_PROGRESS_SECONDS = 0.25

_DIR_MODE = 0o755
_EXEC_MODE = 0o755
_FILE_MODE = 0o644


def size_cap(declared: int) -> int:
    """The largest a download may be, in bytes. The declared size plus room for the real
    one to differ (sizes are the design's estimates until the pinning script measures
    them), and never unbounded."""
    return max(declared + declared // 4, declared + 16 * 1024 * 1024)


_OFFLINE = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    socket.gaierror,
    ConnectionError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
)


class InstallFailed(Exception):
    """``kind`` is ``disk``, ``offline``, ``other`` or ``unpinned`` (no SHA-256 is pinned,
    refused before any request, and ``text`` is empty). ``text`` is the real error."""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind = kind
        self.text = text


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop(0)
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        reason = getattr(current, "reason", None)
        pending.extend(
            [
                reason if isinstance(reason, BaseException) else None,
                current.__cause__,
                current.__context__,
            ]
        )
    return seen


def classify(exc: BaseException) -> str:
    chain = _chain(exc)
    if any(isinstance(e, OSError) and e.errno == errno.ENOSPC for e in chain):
        return "disk"
    if isinstance(exc, urllib.error.HTTPError):
        return "other"  # the server answered; the network is fine
    if any(isinstance(e, _OFFLINE) for e in chain):
        return "offline"
    return "other"


def failure_text(exc: BaseException) -> str:
    """The error as it happened, prefixed with its errno name when it has a real one."""
    for e in _chain(exc):
        code = getattr(e, "errno", None)
        if isinstance(e, OSError) and isinstance(code, int) and code in errno.errorcode:
            return f"{errno.errorcode[code]}: {exc}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _open_for_writing(path: Path) -> BinaryIO:
    return open(path, "wb")  # noqa: SIM115 - closed by the caller's with-block


def read_marker(root: Path, component_id: str) -> dict[str, Any] | None:
    """What the last successful install recorded, or None."""
    try:
        return json.loads((root / component_id / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def install_download(
    component_id: str,
    download: Download,
    artifact: Artifact,
    *,
    root: Path,
    on_progress: Callable[[int, int], None] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 30.0,
    today: Callable[[], date] = date.today,
) -> dict[str, Any]:
    """Install ``artifact`` under ``root/<component_id>/<version>/`` and return the marker.

    Raises ``InstallFailed``. By then everything it wrote is gone, and whatever was
    installed before is untouched.
    """
    if not SHA256_PATTERN.match(artifact.sha256):
        # No text for the card: an unpinned build is not an error it can show.
        raise InstallFailed("unpinned", "")
    staging = root / ".staging" / f"{component_id}-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True)
        downloaded = staging / "download"
        digest = _fetch(
            artifact, downloaded, opener=opener, timeout=timeout, on_progress=on_progress
        )
        if digest != artifact.sha256:
            raise InstallFailed(
                "other",
                f"SHA-256 mismatch for {artifact.url}: expected {artifact.sha256}, got {digest}",
            )
        size_bytes = downloaded.stat().st_size
        tree = staging / "tree"
        _unpack(artifact, downloaded, tree)
        if not (tree / artifact.entry).is_file():
            raise InstallFailed("other", f"the archive has no {artifact.entry}")
        home = root / component_id
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, _DIR_MODE)
        target = home / download.version
        previous = staging / "previous"
        if target.exists():
            target.rename(previous)
        try:
            tree.rename(target)
            record = {
                "version": download.version,
                "sha256": digest,
                "entry": f"{download.version}/{artifact.entry}",
                "size_bytes": size_bytes,
                "installed_at": today().isoformat(),
            }
            marker = home / f".{MARKER}.{uuid.uuid4().hex}"
            marker.write_text(json.dumps(record, indent=1), encoding="utf-8")
            os.chmod(marker, _FILE_MODE)
            os.replace(marker, home / MARKER)
        except BaseException:
            # Restore the working install as it was, before staging is cleared.
            if previous.exists():
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                previous.rename(target)
            raise
        return record
    except InstallFailed:
        raise
    except Exception as exc:
        raise InstallFailed(classify(exc), failure_text(exc)) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _fetch(
    artifact: Artifact,
    destination: Path,
    *,
    opener: Callable[..., Any],
    timeout: float,
    on_progress: Callable[[int, int], None] | None,
) -> str:
    digest = hashlib.sha256()
    total = artifact.size_bytes
    cap = size_cap(total)
    received = 0
    last_report = 0.0
    with (
        opener(artifact.url, timeout=timeout) as response,
        _open_for_writing(destination) as stream,
    ):
        announced = _content_length(response)
        if announced is not None and announced > cap:
            raise InstallFailed(
                "other", f"the download is {announced} bytes, more than the {cap} allowed"
            )
        while True:
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            if received + len(chunk) > cap:
                raise InstallFailed("other", f"the download is larger than the {cap} bytes allowed")
            stream.write(chunk)
            digest.update(chunk)
            received += len(chunk)
            now = time.monotonic()
            if on_progress is not None and now - last_report >= _PROGRESS_SECONDS:
                last_report = now
                on_progress(received, max(total, received))
    if on_progress is not None:
        on_progress(received, max(total, received))
    return digest.hexdigest()


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _unpack(artifact: Artifact, downloaded: Path, tree: Path) -> None:
    tree.mkdir()
    os.chmod(tree, _DIR_MODE)
    if artifact.archive == "file":
        target = tree / artifact.entry
        _make_dirs(target.parent, tree)
        downloaded.rename(target)
        target.chmod(_EXEC_MODE)
        return
    base = tree.resolve()
    with zipfile.ZipFile(downloaded) as archive:
        for info in archive.infolist():
            target = (tree / info.filename).resolve()
            if target != base and base not in target.parents:
                raise InstallFailed(
                    "other", f"the archive writes outside its directory: {info.filename}"
                )
            mode = info.external_attr >> 16
            if info.is_dir():
                _make_dirs(target, tree)
                continue
            _make_dirs(target.parent, tree)
            if stat.S_ISLNK(mode):
                link = archive.read(info).decode("utf-8")
                if Path(link).is_absolute() or base not in (target.parent / link).resolve().parents:
                    raise InstallFailed(
                        "other", f"the archive links outside its directory: {info.filename}"
                    )
                target.symlink_to(link)
                continue
            with archive.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink, _CHUNK)
            # The archive decides only whether a file is executable, never who may write it.
            target.chmod(_EXEC_MODE if mode & 0o111 else _FILE_MODE)


def _make_dirs(directory: Path, tree: Path) -> None:
    """``directory`` and every missing parent up to ``tree``, each 0755."""
    missing = []
    current = directory
    while not current.exists() and current != tree:
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        folder.mkdir()
        os.chmod(folder, _DIR_MODE)
