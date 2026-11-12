"""The project scan: which capabilities a project needs, from the files in it.

The first of ``need()``'s three callers (spec 2026-09-28). At session start the scan
matches the catalog's ``ScanRule``s against the project tree, and the session asks the
resolver for every capability found. The walk is shallow and never enters dependency or
build directories, because it runs every time a project is opened and a monorepo's
``node_modules`` is not evidence of anything.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

from cogito.setup.components import ScanRule

#: Directories never walked: dependencies, build output, environments, and Cogito's own.
PRUNED = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".cogito",
        "node_modules",
        "target",
        "build",
        "dist",
        "out",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".gradle",
        ".idea",
        ".vscode",
    }
)

#: How far below the project root a marker file still counts.
MAX_DEPTH = 4
#: The most directory entries the walk looks at.
MAX_ENTRIES = 20_000


def _file_names(root: Path, max_depth: int) -> Iterator[str]:
    stack: list[tuple[Path, int]] = [(root, 0)]
    seen = 0
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue  # unreadable, vanished or not a directory: skip it
        for entry in entries:
            seen += 1
            if seen > MAX_ENTRIES:
                return
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 <= max_depth and entry.name not in PRUNED:
                        stack.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    yield entry.name
            except OSError:
                continue


def scan_project(
    root: Path, rules: Sequence[ScanRule], *, max_depth: int = MAX_DEPTH
) -> tuple[str, ...]:
    """Capabilities the project needs, each once, in the order the rules list them."""
    names = set(_file_names(root, max_depth))
    needed: list[str] = []
    for rule in rules:
        if not any(fnmatch.filter(names, pattern) for pattern in rule.patterns):
            continue
        if rule.with_sources and not any(
            fnmatch.filter(names, pattern) for pattern in rule.with_sources
        ):
            continue
        needed.extend(need for need in rule.needs if need not in needed)
    return tuple(needed)
