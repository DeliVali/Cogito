"""Documents converted once: a content-addressed cache in front of `tools.documents.extract`.

Without it every `read` of a PDF converts the whole PDF again (paging a 300-page report in
ten windows converts it ten times), and an attachment converts it once more. Both callers
go through `extract` here. The cache stores the converter's result, never a rendering of
it, so a `read` window, its header and its page markers are byte for byte the same with or
without the cache.

The key is what the result depends on and nothing else:

- The file's bytes, by SHA-256. Not the path: the same PDF in two projects converts once,
  and a file replaced under the same name does not hit. Not MD5, whose collisions can be
  built on purpose and which costs about the same. Not mtime, which is all pdf-mcp
  (jztan/pdf-mcp, ``cache.py``, read 2026-09-30) keys its SQLite cache by: a file
  rewritten within the filesystem's timestamp granularity would be served stale.
- The conversion: `documents.CONVERTER_VERSION`, bumped with any change to the output, and
  the installed pypdfium2, whose upgrade can change a PDF's text. pdf-mcp versions its
  extraction the same way (``_EXTRACTION_VERSION``).

Hashing twenty megabytes still reads twenty megabytes, so a stat pre-key sits in front of
the hash: the resolved path, size, mtime_ns, ctime_ns, inode and device, mapped to the hash
from the last read of that exact file. An unchanged file is not even opened; any change to
any field means hashing again. ctime is included because no user-space call can set it
back (utime sets mtime). A file modified within the last `_RACY_SECONDS` gets no pre-key:
two writes inside one timestamp tick leave every stat field equal. That is git's "racy
git" problem (Documentation/technical/racy-git.txt), and this is git's answer to it.

Storage:

- Per user (`config.paths.document_cache_dir`), owner-only: directories 0700, files 0600,
  since a converted document is the document.
- Each entry and each pre-key is its own file, written under a private temporary name in
  the same directory and moved into place with `os.replace`, which is atomic: a reader sees
  the old file or the new one, never half of one. Two threads or the app's two processes
  can convert the same document at once; both write the same bytes and the last move wins.
  In-process, a lock per document stops two threads from converting it twice.
- A size cap with least-recently-used eviction: a hit touches the entry's mtime, and after
  a write the oldest entries go until the cache is under `_EVICT_TO` of `MAX_BYTES`.
- An entry that cannot be parsed, or that names another hash or conversion, is ignored and
  converted again. A cache that cannot be written (read-only home, sandbox, full disk)
  makes a read slower, never makes it fail.

Only what the bytes decide is cached: a document's text, and the reasons a document cannot
be read that come from its contents (``encrypted``, ``scanned``, ``corrupt``). A file over
the size limit is refused before anything is read, so it is never hashed or stored.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import stat as stat_module
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cogito.config import paths
from cogito.tools import documents

log = logging.getLogger(__name__)

#: Total disk space the entries may take. A 300-page PDF's text is about a megabyte, so
#: this keeps hundreds of large documents; past it the least recently read go.
MAX_BYTES = 256 * 1024 * 1024
#: Where eviction stops, as a share of `MAX_BYTES`, so a cache at its cap does not evict on
#: every write.
_EVICT_TO = 0.8
#: The most pre-keys kept. Each is a few dozen bytes; past this the oldest go.
MAX_POINTERS = 4096

#: A file changed this recently may change again inside one timestamp tick without any stat
#: field moving, so its pre-key is not recorded and the next read hashes it again.
_RACY_SECONDS = 2.0

#: The layout of an entry file. Bumped if the JSON below changes shape.
_FORMAT = 1

#: Unreadable reasons that come from the document's own bytes, and so are cached.
_CACHED_REASONS = frozenset({"encrypted", "scanned", "corrupt"})

_HASH_CHUNK = 1 << 20


def _pdfium_version() -> str:
    try:
        from importlib.metadata import version

        return version("pypdfium2")
    except Exception:  # noqa: BLE001 - not installed: the converter says so itself
        return "none"


#: What a cached result was converted by, besides the bytes.
VARIANT = f"c{documents.CONVERTER_VERSION}-pdfium{_pdfium_version()}"

Result = documents.Document | documents.Unreadable | None


@dataclass(frozen=True, slots=True)
class Converted:
    """A converter result and the SHA-256 of the bytes it came from (None when the file was
    never hashed: not a document, too large, or unreadable from the filesystem)."""

    result: Result
    sha256: str | None
    #: Whether this came from the cache rather than from the converter just now.
    hit: bool = False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _prekey(resolved: Path, status: os.stat_result) -> str:
    material = "\0".join(
        str(part)
        for part in (
            resolved,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
            status.st_ino,
            status.st_dev,
        )
    )
    return hashlib.sha256(material.encode("utf-8", "surrogateescape")).hexdigest()[:40]


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_size, a.st_mtime_ns, a.st_ctime_ns, a.st_ino, a.st_dev) == (
        b.st_size,
        b.st_mtime_ns,
        b.st_ctime_ns,
        b.st_ino,
        b.st_dev,
    )


def _encode(result: documents.Document | documents.Unreadable, sha: str) -> bytes:
    body: dict[str, Any] = {"format": _FORMAT, "sha256": sha, "variant": VARIANT}
    if isinstance(result, documents.Document):
        body.update(
            result="document",
            kind=result.kind,
            text=result.text,
            pages=result.pages,
            sheets=list(result.sheets),
        )
    else:
        body.update(
            result="unreadable", kind=result.kind, reason=result.reason, detail=result.detail
        )
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _decode(raw: bytes, sha: str) -> documents.Document | documents.Unreadable | None:
    """An entry's result, or None for anything that is not exactly an entry for ``sha``
    under this conversion. The caller treats None as a miss and converts again."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("format") != _FORMAT or body.get("sha256") != sha or body.get("variant") != VARIANT:
        return None
    kind = body.get("kind")
    if kind not in documents.KINDS:
        return None
    if body.get("result") == "document":
        text, pages, sheets = body.get("text"), body.get("pages"), body.get("sheets")
        if not isinstance(text, str):
            return None
        if pages is not None and (not isinstance(pages, int) or isinstance(pages, bool)):
            return None
        if not isinstance(sheets, list) or not all(isinstance(s, str) for s in sheets):
            return None
        return documents.Document(kind, text, pages=pages, sheets=tuple(sheets))
    if body.get("result") == "unreadable":
        reason, detail = body.get("reason"), body.get("detail")
        if reason not in _CACHED_REASONS or not isinstance(detail, str):
            return None
        return documents.Unreadable(kind, reason, detail)
    return None


def _cacheable(result: Result) -> bool:
    if isinstance(result, documents.Document):
        return True
    return isinstance(result, documents.Unreadable) and result.reason in _CACHED_REASONS


class DocumentCache:
    """The cache in one directory. Every method is safe from any thread, and from another
    process sharing the directory."""

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes: int = MAX_BYTES,
        max_pointers: int = MAX_POINTERS,
        convert=documents.extract,
    ) -> None:
        self.directory = directory
        self._max_bytes = max_bytes
        self._max_pointers = max_pointers
        self._convert = convert
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._evicting = threading.Lock()

    # -- the one call ---------------------------------------------------------------

    def extract(self, path: Path) -> Result:
        """What `documents.extract(path)` returns, from the cache when the bytes were
        converted before."""
        return self.convert(path).result

    def convert(self, path: Path) -> Converted:
        """`extract`, plus the SHA-256 the result is keyed by."""
        if documents.kind_of(path) is None:
            return Converted(None, None)
        try:
            resolved = path.resolve()
            before = resolved.stat()
        except OSError:
            return Converted(self._convert(path), None)
        if not stat_module.S_ISREG(before.st_mode) or before.st_size > documents.MAX_BYTES:
            # Refused on its size before anything is read: nothing to key, nothing to keep.
            return Converted(self._convert(path), None)

        prekey = _prekey(resolved, before)
        sha = self._read_pointer(prekey)
        if sha is not None:
            found = self._read_entry(sha)
            if found is not None:
                return Converted(found, sha, hit=True)
        try:
            sha = sha256_file(resolved)
        except OSError:
            return Converted(self._convert(path), None)

        with self._lock_for(sha):
            found = self._read_entry(sha)
            if found is not None:
                self._remember(prekey, sha, before, resolved)
                return Converted(found, sha, hit=True)
            result = self._convert(path)
            try:
                after = resolved.stat()
            except OSError:
                return Converted(result, None)
            if not _same_file(before, after):
                # The file changed while it was hashed or converted. The result may match
                # neither version, so it is returned but not cached.
                return Converted(result, None)
            if _cacheable(result):
                assert result is not None
                self._write_entry(sha, result)
                self._remember(prekey, sha, before, resolved)
            return Converted(result, sha)

    def identify(self, path: Path) -> str | None:
        """The SHA-256 of ``path``'s bytes, through the pre-key when the file is unchanged."""
        try:
            resolved = path.resolve()
            status = resolved.stat()
        except OSError:
            return None
        prekey = _prekey(resolved, status)
        sha = self._read_pointer(prekey)
        if sha is not None:
            return sha
        try:
            sha = sha256_file(resolved)
        except OSError:
            return None
        self._remember(prekey, sha, status, resolved)
        return sha

    # -- layout ---------------------------------------------------------------------

    def _entries(self) -> Path:
        return self.directory / "entries"

    def _pointers(self) -> Path:
        return self.directory / "stat"

    def entry_path(self, sha: str) -> Path:
        return self._entries() / sha[:2] / f"{sha}-{VARIANT}.json"

    def _pointer_path(self, prekey: str) -> Path:
        return self._pointers() / prekey

    def _lock_for(self, sha: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(sha)
            if lock is None:
                lock = self._locks[sha] = threading.Lock()
            return lock

    # -- reading ----------------------------------------------------------------------

    def _read_pointer(self, prekey: str) -> str | None:
        try:
            raw = self._pointer_path(prekey).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
            return None
        return raw

    def _read_entry(self, sha: str) -> documents.Document | documents.Unreadable | None:
        entry = self.entry_path(sha)
        try:
            raw = entry.read_bytes()
        except OSError:
            return None
        found = _decode(raw, sha)
        if found is None:
            log.info("document cache: ignoring an unreadable entry %s", entry.name)
            return None
        # The LRU clock: a hit makes the entry the newest.
        with contextlib.suppress(OSError):
            os.utime(entry)
        return found

    # -- writing ----------------------------------------------------------------------

    def _ensure(self, directory: Path) -> bool:
        try:
            self.directory.parent.mkdir(parents=True, exist_ok=True)
            inside = directory.relative_to(self.directory).parts
            chain = [self.directory] + [
                self.directory.joinpath(*inside[: depth + 1]) for depth in range(len(inside))
            ]
            for folder in chain:
                folder.mkdir(mode=0o700, exist_ok=True)
                # mkdir's mode is filtered through the umask, and a directory made by an
                # older run may be wider: owner-only either way.
                if stat_module.S_IMODE(folder.stat().st_mode) != 0o700:
                    folder.chmod(0o700)
        except OSError as exc:
            log.debug("document cache: %s cannot be created: %s", directory, exc)
            return False
        return True

    def _atomic_write(self, target: Path, data: bytes) -> bool:
        if not self._ensure(target.parent):
            return False
        try:
            # mkstemp creates the file 0600 whatever the umask.
            handle, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
        except OSError as exc:
            log.debug("document cache: cannot write in %s: %s", target.parent, exc)
            return False
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
            os.replace(temporary, target)
        except OSError as exc:
            log.debug("document cache: %s not written: %s", target.name, exc)
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            return False
        return True

    def _write_entry(self, sha: str, result: documents.Document | documents.Unreadable) -> None:
        if self._atomic_write(self.entry_path(sha), _encode(result, sha)):
            self._evict()

    def _remember(self, prekey: str, sha: str, status: os.stat_result, resolved: Path) -> None:
        if time.time() - status.st_mtime_ns / 1e9 < _RACY_SECONDS:
            return
        if self._atomic_write(self._pointer_path(prekey), sha.encode("ascii")):
            self._trim_pointers()

    # -- eviction -----------------------------------------------------------------------

    def _evict(self) -> None:
        """Least recently used entries out until the cache is back under its cap."""
        if not self._evicting.acquire(blocking=False):
            return
        try:
            found: list[tuple[float, int, Path]] = []
            total = 0
            try:
                folders = list(os.scandir(self._entries()))
            except OSError:
                return
            for folder in folders:
                try:
                    if not folder.is_dir(follow_symlinks=False):
                        continue
                    for entry in os.scandir(folder.path):
                        if not entry.name.endswith(".json"):
                            continue
                        status = entry.stat(follow_symlinks=False)
                        found.append((status.st_mtime, status.st_size, Path(entry.path)))
                        total += status.st_size
                except OSError:
                    continue
            if total <= self._max_bytes:
                return
            goal = self._max_bytes * _EVICT_TO
            for _, size, path in sorted(found):
                if total <= goal:
                    break
                try:
                    path.unlink()
                except OSError:
                    continue
                total -= size
        finally:
            self._evicting.release()

    def _trim_pointers(self) -> None:
        try:
            pointers = list(os.scandir(self._pointers()))
        except OSError:
            return
        if len(pointers) <= self._max_pointers:
            return
        aged: list[tuple[float, str]] = []
        for pointer in pointers:
            try:
                aged.append((pointer.stat(follow_symlinks=False).st_mtime, pointer.path))
            except OSError:
                continue
        aged.sort()
        for _, path in aged[: len(aged) - int(self._max_pointers * _EVICT_TO)]:
            try:
                os.unlink(path)
            except OSError:
                continue


_shared: dict[Path, DocumentCache] = {}
_shared_guard = threading.Lock()


def shared() -> DocumentCache:
    """The cache in this user's cache directory, one object per directory per process so
    its per-document locks are shared by every caller."""
    directory = paths.document_cache_dir()
    with _shared_guard:
        cache = _shared.get(directory)
        if cache is None:
            cache = _shared[directory] = DocumentCache(directory)
        return cache


def extract(path: Path) -> Result:
    """`documents.extract`, through the user's cache: the converter both `read` and the
    attachments use."""
    return shared().extract(path)


def convert(path: Path) -> Converted:
    """`extract` with the document's SHA-256."""
    return shared().convert(path)
