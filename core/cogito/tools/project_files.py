"""The project's source files, listed once and cheaply, and the files close to a missing name.

The listing is what `git` calls source (or a pruned walk outside a work tree). The engine
takes it before a session's first shell call to know what counts as source; `read` and
`edit` take it when a path does not exist, to name the real files it was probably meant to
be (CONTRIBUTING 6.4: an error says what to do next).
"""

from __future__ import annotations

import itertools
import os
import subprocess
import time
from pathlib import Path, PurePosixPath

#: One deadline for both `git ls-files` calls together (tracked, then untracked). The
#: tracked listing only reads the index and the untracked one skips ignored and pruned
#: directories, so this is generous even for a large repo. It keeps a hung or slow git (a
#: network-mounted worktree, a corrupt index) from stalling a shell call on top of the
#: command's own timeout.
_GIT_LS_FILES_TIMEOUT = 5.0

#: Directories the baseline listing never enters: version control metadata, dependency
#: trees and build caches. They dominate a real project's file count and never hold source
#: a tool call is checked against, so pruning them is most of what keeps the listing
#: cheap. Any other directory starting with "." is skipped too (editor and tool state).
_DIGEST_PRUNED_DIRS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".next",
        ".cache",
        ".tox",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

#: Cap on how many files the baseline listing collects, so a huge tree that slips past the
#: prune list still costs a bounded amount. The walk enforces it per directory as entries
#: are found, not by truncating a full listing, so one huge directory cannot make the walk
#: cost more than the cap either.
_DIGEST_MAX_ENTRIES = 20_000


#: Extra ignore patterns for the untracked listing, so it prunes like `_walk_paths`: an
#: untracked `node_modules/` or `.venv/` nobody has gitignored yet (a fresh `git init`) is
#: still not source, and git never descends into it.
_GIT_PRUNE_EXCLUDES = tuple(f"--exclude={name}/" for name in sorted(_DIGEST_PRUNED_DIRS | {".*"}))


def resolve_path(root: Path | None, raw: str) -> Path:
    """Turn a model-supplied path into an absolute one, the way `read`/`edit`/`glob`/`grep`
    all should.

    A leading ``~`` or ``~user`` expands to the home directory, as the shell expands it: a
    weak model that writes ``~/.config`` meant the home directory, not a ``~`` entry under
    the project. Without this the path silently became ``<root>/~/.config`` and every such
    call failed with "does not exist" (seen in the app: a `glob` of ``~/.steam/...`` searched
    ``<project>/~/.steam/...``), while the same ``~`` worked in `shell` because that runs a
    real shell. Anything else that is not already absolute stays relative to the project
    root, unchanged.

    ``root`` is read only for a relative path, so an absolute one resolves even when a
    caller has no project root to offer (``root=None``), exactly as the old inline
    ``if not path.is_absolute()`` guard behaved.
    """
    text = raw.strip()
    if text.startswith("~"):
        expanded = os.path.expanduser(text)
        # expanduser leaves an unknown ``~user`` untouched; only trust a real expansion.
        if not expanded.startswith("~"):
            text = expanded
    path = Path(text)
    if not path.is_absolute():
        path = Path(root) / path
    return path


def _git_ls_files(root: Path, *options: str, deadline: float) -> list[str] | None:
    """`git ls-files -z <options> -- .` under `root` as relative paths, or None on any
    failure: not a work tree, `git` missing, a non-zero exit, or past `deadline`.

    `-- .` keeps the listing inside `root`. Without it, `git ls-files` run from a
    subdirectory of a larger repository lists the whole repository, relative to `root`
    (with `../` paths for files outside it). An entry ending in `/` is an untracked nested
    repository (a directory, not a file) and is dropped.
    """
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", *options, "--", "."],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [
        entry.decode("utf-8", errors="replace")
        for entry in result.stdout.split(b"\0")
        if entry and not entry.endswith(b"/")
    ]


def git_source_paths(root: Path, *, max_entries: int) -> frozenset[str] | None:
    """What `git` considers source under `root`: tracked files plus untracked files it
    does not ignore. None if either listing fails (see `_git_ls_files`) or both are empty.

    Preferred over `_walk_paths`: git knows this project's line between source and build
    output through `.gitignore`, which a list of directory names only approximates. In
    particular a gitignored byproduct left before the session (by a manual build) is
    excluded, where the walk would take it for source.

    Untracked files count because a real source file not yet `git add`-ed (a fresh `git
    init`, any unstaged file) must be source. Otherwise a shell edit to it is invisible and
    an edit -> re-run loop over it is stopped as NO_PROGRESS. The cost is that a stale build
    output that is neither tracked nor ignored counts too, as it does under the walk:
    nothing tells it apart from unstaged source. A false stop costs a working run, while a
    missed one is still bounded by the other budgets. What a build creates during the
    session never joins: the listing is taken once, before the session's first shell call.

    Tracked files are listed first and untracked ones fill what the cap leaves, because
    `git ls-files` sorts its output and one combined listing would let early-sorting
    untracked names push committed source out. An empty result means `root` itself is
    ignored (a scratch directory inside a larger repository). "Nothing here is source" is
    wrong for the directory the agent works in, so the caller walks instead.
    """
    deadline = time.monotonic() + _GIT_LS_FILES_TIMEOUT
    tracked = _git_ls_files(root, "--cached", deadline=deadline)
    if tracked is None:
        return None
    untracked = _git_ls_files(
        root, "--others", "--exclude-standard", *_GIT_PRUNE_EXCLUDES, deadline=deadline
    )
    if untracked is None:
        return None
    paths = frozenset(itertools.islice(itertools.chain(tracked, untracked), max_entries))
    return paths or None


def _walk_paths(root: Path, *, max_entries: int) -> frozenset[str]:
    """Every file under `root` right now, as relative paths, pruned and bounded.

    The fallback when `root` is not a git work tree, git is missing, or git lists nothing
    under `root`: a walk that prunes by directory name. Bounded per directory as entries
    are found, not by truncating a full listing, so one huge directory cannot cost more
    than `max_entries` either.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _DIGEST_PRUNED_DIRS and not name.startswith(".")
        ]
        remaining = max_entries - len(found)
        if remaining <= 0:
            break
        for name in filenames[:remaining]:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return frozenset(found)


def known_source_paths(root: Path, *, max_entries: int = _DIGEST_MAX_ENTRIES) -> frozenset[str]:
    """What counts as source under `root` right now, as relative paths: what `git` tracks
    or would track (`git_source_paths`) in a git work tree, else a pruned, bounded walk.

    This is not the digest. It is the snapshot the engine takes once, at the session's
    first `shell` call and before that call runs. The tree is never listed again, so
    nothing created later (a build's `.class`/`.o`/binary output, a compiler's `.js`) is
    folded in. The set grows only through files a tool call is recorded as touching (see
    `Engine._known_paths`). A listing, by git or by walking, cannot tell "already source"
    from "a command just created it", so it is taken before anything has run. It is per
    session, not per turn, so a byproduct an earlier turn's build left on disk is not taken
    for source when a later turn first calls `shell`.
    """
    listed = git_source_paths(root, max_entries=max_entries)
    return listed if listed is not None else _walk_paths(root, max_entries=max_entries)


#: How many close files a missing path names, and how many entries a directory shows.
MAX_SUGGESTIONS = 5
MAX_DIRECTORY_ENTRIES = 8

#: Files nobody reads as text. A close name is never one of these unless the missing path
#: has the same suffix.
_NOT_TEXT = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svgz",
        ".tif",
        ".tiff",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".mov",
        ".avi",
        ".mkv",
        ".webm",
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tar",
        ".jar",
        ".war",
        ".pyc",
        ".pyo",
        ".class",
        ".o",
        ".obj",
        ".a",
        ".lib",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".bin",
        ".wasm",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".db",
        ".sqlite",
        ".lock",
    }
)


def _within(a: str, b: str, limit: int) -> bool:
    """True when `a` and `b` differ by at most `limit` single-character edits."""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _relative_parts(root: Path, path: Path) -> tuple[str, ...] | None:
    try:
        return PurePosixPath(path.relative_to(root).as_posix()).parts
    except ValueError:
        return None


def _listing(root: Path) -> list[str]:
    """The project's files as `/`-separated relative paths, one bounded listing."""
    return sorted(
        p.replace(os.sep, "/") for p in known_source_paths(root, max_entries=_DIGEST_MAX_ENTRIES)
    )


def similar_files(root: Path, missing: Path, *, limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Project-relative paths of real files whose name is close to the missing `missing`.

    In order: the same file name anywhere in the tree, the same name with another suffix, a
    name one or two edits away, and a path that contains the name (same suffix only, or
    `api` would offer every `.py` file under an `api/` folder for `api.ts`). Within a group
    the files sharing the most leading directories with the missing path come first, then the
    shallower and then the alphabetical. Nothing the project ignores is listed, since the listing is
    the one `known_source_paths` takes, and a tracked file deleted from disk is dropped.
    """
    name = missing.name.lower()
    if not name:
        return []
    stem = Path(name).stem
    suffix = Path(name).suffix
    wanted = _relative_parts(root, missing)
    wanted_dirs = wanted[:-1] if wanted else ()
    limit_edits = 1 if len(stem) < 10 else 2
    ranked: list[tuple[tuple[int, int, int, int, str], str]] = []
    for path in _listing(root):
        parts = path.split("/")
        base = parts[-1].lower()
        base_suffix = Path(base).suffix
        if base_suffix in _NOT_TEXT and base_suffix != suffix:
            continue
        base_stem = Path(base).stem
        if base == name:
            tier, sub = 0, 0
        elif base_stem == stem:
            tier, sub = 1, 0
        elif len(stem) >= 4 and _within(base_stem, stem, limit_edits):
            tier, sub = 2, 0 if base_suffix == suffix else 1
        elif len(stem) >= 3 and base_suffix == suffix and stem in path.lower():
            tier, sub = 3, 0 if stem in base else 1
        else:
            continue
        shared = 0
        for ours, theirs in zip(wanted_dirs, parts[:-1], strict=False):
            if ours != theirs:
                break
            shared += 1
        ranked.append(((tier, sub, -shared, len(parts), path), path))
    ranked.sort()
    found: list[str] = []
    for _, path in ranked:
        if (root / path).is_file():
            found.append(path)
            if len(found) == limit:
                break
    return found


def directory_entries(
    root: Path, directory: Path, *, limit: int = MAX_DIRECTORY_ENTRIES
) -> tuple[list[str], int]:
    """What is directly in `directory`, as project-relative paths: files first, then
    subdirectories with a trailing `/`. Returns the first `limit` and how many there are."""
    prefix = _relative_parts(root, directory)
    if prefix is None:
        return [], 0
    files: set[str] = set()
    folders: set[str] = set()
    depth = len(prefix)
    for path in _listing(root):
        parts = path.split("/")
        if len(parts) <= depth or tuple(parts[:depth]) != prefix:
            continue
        if len(parts) == depth + 1:
            if (root / path).is_file():
                files.add(path)
        else:
            folders.add("/".join(parts[: depth + 1]) + "/")
    entries = sorted(files) + sorted(folders)
    return entries[:limit], len(entries)


def missing_file_message(root: Path, missing: Path, *, then: str, otherwise: str) -> str:
    """The text of a `not_found` error for a file. With close files it names them and ends
    `Or <then>`; with none it is `otherwise`, the message the tool always had."""
    close = similar_files(root, missing)
    if not close:
        return f"no such file: {missing}. {otherwise}"
    return f"no such file: {missing}. Did you mean: {', '.join(close)}? Or {then}"


def directory_message(
    root: Path,
    directory: Path,
    *,
    otherwise: str,
    then: str = "Read one of them, or use glob to list it.",
) -> str:
    """The text of an `is_directory` error. It lists what is in the directory when the
    project knows of anything there, followed by `then`; otherwise it is `otherwise`."""
    entries, total = directory_entries(root, directory)
    if not entries:
        return f"{directory} is a directory. {otherwise}"
    more = f" (and {total - len(entries)} more)" if total > len(entries) else ""
    return f"{directory} is a directory. It holds: {', '.join(entries)}{more}. {then}"


def unwritable_message(path: Path, exc: OSError) -> str:
    """Why ``path`` could not be written, and what can be done, for `write` and `edit`.

    A bare «[Errno 13] Permission denied» left a model telling the person that making their
    own read-only file writable «requeriría acceso administrativo» (QA rig, 2026-10-16).
    The file's owner and mode decide the advice; nothing here changes a permission."""
    import errno
    import stat

    if exc.errno in (errno.EACCES, errno.EPERM):
        try:
            info = path.stat() if path.exists() else path.parent.stat()
        except OSError:
            info = None
        target = path if path.exists() else path.parent
        noun = "file" if path.exists() else "folder it would go in"
        if info is not None and hasattr(os, "getuid") and info.st_uid == os.getuid():
            return (
                f"{target} is read-only ({stat.filemode(info.st_mode)}), so nothing was changed. "
                f"The {noun} belongs to this user: if the person wants it changed, ask the "
                f"person first, then `chmod u+w {target}` makes it writable (no sudo needed)."
            )
        return (
            f"{target} cannot be written by this user, so nothing was changed: the {noun} "
            "belongs to another user. Tell the person; do not try sudo."
        )
    if exc.errno == errno.ENOSPC:
        return f"the disk is full, so {path} was not written and nothing was changed."
    return f"could not write {path}: {exc.strerror or exc}. Nothing was changed."
