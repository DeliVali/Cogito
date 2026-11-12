"""Deleting means moving to a trash, never unlinking.

The freedesktop trash is implemented here rather than delegated to `gio` or `send2trash`
because we need the mapping from the original path to where it went. Without it
`cogito undo` has nothing to restore from.

Where the home trash is unreachable (a different device, or a platform without the spec),
a trash in the project's `.cogito/` is used instead, or in the app's data directory when
no project is given. It is harder to search by hand, but still recoverable.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from cogito.config import paths
from cogito.errors import CogitoError


class TrashError(CogitoError):
    kind = "trash"


@dataclass(frozen=True, slots=True)
class Trashed:
    original: Path
    trashed: Path
    deleted_at: str
    #: True when the freedesktop trash was used and the desktop can restore it too.
    standard: bool


def home_trash() -> Path:
    """The freedesktop home trash: `$XDG_DATA_HOME/Trash`, or `~/.local/share/Trash` when
    XDG_DATA_HOME is unset or not absolute (Trash specification 1.0, "Trash directories";
    a relative value is ignored, per the base directory specification)."""
    data = os.environ.get("XDG_DATA_HOME", "")
    if data and os.path.isabs(data):
        return Path(data) / "Trash"
    return Path.home() / ".local" / "share" / "Trash"



def _same_device(left: Path, right: Path) -> bool:
    try:
        return left.stat().st_dev == right.stat().st_dev
    except OSError:
        return False


def _names(name: str):
    """``name``, then numbered variants that keep the extension readable."""
    yield name
    stem, dot, suffix = name.partition(".")
    for counter in range(1, 10_000):
        yield f"{stem}.{counter}{dot}{suffix}"


def _unique(directory: Path, name: str) -> Path:
    """A free name in ``directory``. A link counts as taken, broken or not: a copy written
    to a dangling link's name lands wherever it points."""
    for candidate in _names(name):
        if not os.path.lexists(directory / candidate):
            return directory / candidate
    raise TrashError(f"cannot find a free name for {name} in {directory}")


def _folders(root: Path) -> tuple[Path, Path]:
    """The trash's `files` and `info`, made if missing, refused when any of the three is a
    link or not the person's own: what is written there must land there."""
    files, info = root / "files", root / "info"
    for folder in (root, files, info):
        folder.mkdir(parents=True, exist_ok=True)
        status = os.lstat(folder)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise TrashError(f"{folder} is not a folder of your own; nothing was moved")
    return files, info


def _claim(files: Path, info: Path, name: str, original: Path, when: datetime) -> Path:
    """Reserve a name in the trash by creating its `.trashinfo` exclusively, never through
    a link (Trash specification 1.0: the info file is made atomically, and its name is the
    claim), and return where the item itself goes. A link planted at the info file's name
    once made the trash write over what it pointed at (review of 2026-11-03)."""
    text = (
        "[Trash Info]\n"
        f"Path={quote(str(original), safe='/')}\n"
        f"DeletionDate={when.strftime('%Y-%m-%dT%H:%M:%S')}\n"
    ).encode()
    for candidate in _names(name):
        if os.path.lexists(files / candidate):
            continue
        try:
            descriptor = os.open(
                info / f"{candidate}.trashinfo",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise TrashError(f"could not write to the trash: {exc}") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text)
        return files / candidate
    raise TrashError(f"cannot find a free name for {name} in {files}")


def trash(path: Path, *, project_root: Path | None = None) -> Trashed:
    """Move ``path`` out of the way, recoverably. Returns where it went."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        raise TrashError(f"no such file or directory: {path}")

    when = datetime.now().astimezone()
    root = home_trash()

    # The home trash and the file must share a device: a rename across devices fails, and
    # copying a large directory just to delete it is not what was asked for.
    if _same_device(path.parent, Path.home()) or _same_device(path.parent, root.parent):
        files, info = _folders(root)
        # The entry itself, as rm removes it: a link is recorded where the link was, not
        # where it pointed, or the desktop's «Restore» would put it back over its target.
        destination = _claim(files, info, path.name, _entry_path(path), when)
        try:
            path.rename(destination)
        except OSError as exc:
            (info / f"{destination.name}.trashinfo").unlink(missing_ok=True)
            raise TrashError(f"could not move {path} to the trash: {exc}") from exc
        return Trashed(path, destination, when.isoformat(), standard=True)

    fallback = paths.ensure(
        (paths.project_dir(project_root) if project_root else paths.data_dir()) / "trash"
    )
    destination = _unique(fallback, path.name)
    try:
        shutil.move(str(path), str(destination))
    except OSError as exc:
        raise TrashError(f"could not move {path} to {fallback}: {exc}") from exc
    return Trashed(path, destination, when.isoformat(), standard=False)


def _copy_new(source: Path, destination: Path) -> None:
    """Copy ``source`` to a file made new at ``destination``, never through a link put
    there in the meantime, keeping its times and mode as `shutil.copy2` does."""
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with open(source, "rb") as reader, os.fdopen(descriptor, "wb") as writer:
        shutil.copyfileobj(reader, writer)
    shutil.copystat(source, destination)


def copy_to_trash(path: Path, *, project_root: Path | None = None) -> Trashed:
    """Put a copy of the regular file ``path`` in the trash, leaving ``path`` where it is.

    For a file about to be changed in place (`truncate`) instead of removed: the trash
    then holds what it was, under its own name, as a deletion would have left it.
    """
    path = Path(path)
    if not path.is_file():
        raise TrashError(f"not a regular file: {path}")

    when = datetime.now().astimezone()
    root = home_trash()
    if _same_device(path.parent, Path.home()) or _same_device(path.parent, root.parent):
        files, info = _folders(root)
        destination = _claim(files, info, path.name, path.resolve(), when)
        try:
            _copy_new(path, destination)
        except OSError as exc:
            (info / f"{destination.name}.trashinfo").unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise TrashError(f"could not copy {path} to the trash: {exc}") from exc
        return Trashed(path, destination, when.isoformat(), standard=True)

    fallback = paths.ensure(
        (paths.project_dir(project_root) if project_root else paths.data_dir()) / "trash"
    )
    destination = _unique(fallback, path.name)
    try:
        _copy_new(path, destination)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise TrashError(f"could not copy {path} to {fallback}: {exc}") from exc
    return Trashed(path, destination, when.isoformat(), standard=False)


def _entry_path(path: Path) -> Path:
    """``path`` with its folder resolved and its own name kept."""
    return Path(os.path.realpath(path.parent)) / path.name


def _recorded_original(info_file: Path) -> str | None:
    """The `Path=` a `.trashinfo` holds, undone from its percent-encoding, or None."""
    try:
        text = info_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Path="):
            return unquote(line[len("Path="):])
    return None


def restore(entry: Trashed, *, overwrite: bool = False) -> Path:
    """Put a trashed file back where it came from.

    The trash reuses a name once it is emptied, so what holds the name now may be
    something the person threw away since. It is put back only while its `.trashinfo`
    still says it came from where this entry did (review of 2026-11-03: an undo moved the
    person's own trashed folder into the project and dropped it from their trash)."""
    if not entry.trashed.exists() and not entry.trashed.is_symlink():
        raise TrashError(f"no longer in the trash: {entry.trashed}")
    files = home_trash() / "files"
    if entry.standard and _entry_path(entry.trashed).parent == Path(os.path.realpath(files)):
        recorded = _recorded_original(home_trash() / "info" / f"{entry.trashed.name}.trashinfo")
        expected = {str(entry.original), str(_entry_path(entry.original))}
        # What an earlier Cogito recorded: the path with its last link followed.
        with contextlib.suppress(OSError):
            expected.add(str(entry.original.resolve()))
        if recorded is None or recorded not in expected:
            raise TrashError(
                f"the trash holds something else under {entry.trashed.name} now; "
                "nothing was restored"
            )
    if entry.original.exists() and not overwrite:
        raise TrashError(
            f"{entry.original} exists again; pass overwrite to replace what is there now"
        )
    entry.original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(entry.trashed), str(entry.original))
    if entry.standard:
        info = home_trash() / "info" / f"{entry.trashed.name}.trashinfo"
        info.unlink(missing_ok=True)
    return entry.original
