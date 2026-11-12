"""Repository invariant: every tracked file is accounted for in CONTRIBUTING.md.

The folder map in CONTRIBUTING.md section 3 is the source of truth. This parses it, so a new
folder fails the check until the map lists it. Run with ``python -m cogito.dev.check_tree``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "CONTRIBUTING.md"

#: The only files allowed at the repository root.
TOP_LEVEL_FILES = frozenset(
    {
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        ".gitignore",
        ".python-version",
        "pyproject.toml",
        "uv.lock",
    }
)

_MAP_ROW = re.compile(r"^\|\s*`([^`]+/)`\s*\|")


def mapped_directories() -> set[str]:
    """Return every directory listed in the CONTRIBUTING.md folder map."""
    return {
        match.group(1).rstrip("/")
        for line in CONTRACT.read_text(encoding="utf-8").splitlines()
        if (match := _MAP_ROW.match(line))
    }


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def violations() -> list[str]:
    mapped = mapped_directories()
    if not mapped:
        return ["CONTRIBUTING.md: folder map is empty or unparseable"]

    found: list[str] = []
    for path in tracked_files():
        directory = str(Path(path).parent)
        if directory == ".":
            if path not in TOP_LEVEL_FILES:
                found.append(f"{path}: not an allowed root file — see CONTRIBUTING.md section 3")
            continue
        if not any(directory == owner or directory.startswith(f"{owner}/") for owner in mapped):
            found.append(f"{path}: {directory!r} is not in the CONTRIBUTING.md folder map")
    return found


def main() -> int:
    found = violations()
    for message in found:
        print(message, file=sys.stderr)
    if found:
        print(f"\n{len(found)} tracked path(s) outside the folder map.", file=sys.stderr)
        return 1
    print("Tree OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
