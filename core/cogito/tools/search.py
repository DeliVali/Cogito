"""`grep` and `glob`: search file contents with ripgrep and file names with fd.

Both respect .gitignore by default. Grep output is grouped by file with match counts, not
raw lines, because a raw search result spends the model's context on formatting.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cogito.provider.base import (
    PayloadShape,
    Provenance,
    Reproducibility,
    Retention,
    ToolSpec,
)
from cogito.tools.base import ToolContext, ToolResult, parameters
from cogito.tools.project_files import resolve_path


def _relative(file_path: str, root: Path) -> str:
    """Paths relative to the project root. The prefix is paid on every line of every result."""
    try:
        return str(Path(file_path).resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return file_path


def _nothing_found(pattern: str, where: object, context: ToolContext) -> ToolResult:
    """A search that matched nothing is an answer, not a failure.

    As an error it would count toward the loop's consecutive-error limit, and three
    harmless empty searches in a row would stop the run.
    """
    return ToolResult(
        content=(
            f"No matches for {pattern!r} under {where}. "
            f"The pattern is a regular expression — check for characters that need "
            f"escaping, or search for something shorter."
        ),
        metadata={"matches": 0},
    )


def _no_files(pattern: str, where: object) -> ToolResult:
    """`glob` found no file. Not an error, for the same reason as `_nothing_found`.

    It used to share grep's message, which calls the pattern a regular expression. For a
    glob that is false, and it sent a model on to retry the same name with the suffix
    trimmed until the identical-call limit stopped the turn (seen in the app: three
    `**/NAME` globs for a file that was never there). An empty listing is an answer: the
    name is not under that folder.
    """
    return ToolResult(
        content=(
            f"No file matches {pattern!r} under {where} (hidden and .gitignored files are "
            f"skipped). No file by that name is there; repeating this search will not find "
            f"one. List the folder to see what it holds, or say it was not found."
        ),
        metadata={"matches": 0},
    )


class GrepTool:
    def __init__(self) -> None:
        self.spec = ToolSpec(
            name="grep",
            #: A search over a tree that keeps changing.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.BULK,
            ),
            description=(
                "Search for a pattern across files using ripgrep. Results are grouped by "
                "file with match counts; only the first few lines per file are shown to "
                "preserve context budget. Use glob to limit to certain filenames."
            ),
            parameters=parameters(
                {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file. Default: project root.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Filename glob, e.g. *.py",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Context lines, 0-10. Default 0.",
                    },
                    "max_files": {
                        "type": "integer",
                        "description": "Max files. Default 12.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Case-sensitive. Default false.",
                    },
                },
                required=("pattern",),
            ),
            aliases={
                "pattern": ("regex", "query", "term", "search"),
                "path": ("dir", "directory", "file", "where"),
                "glob": ("filename", "file_pattern", "extension"),
                "context": ("context_lines", "lines"),
                "max_files": ("max", "limit"),
                "case_sensitive": ("case", "sensitive"),
            },
            budget_tokens=1500,
            summary="search file contents for a regular expression",
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        pattern = str(arguments.get("pattern", "")).strip()
        if not pattern:
            return ToolResult.error("pattern is required", "missing_pattern")

        search_path = arguments.get("path", "")
        if search_path:
            path = resolve_path(context.project_root if context else None, str(search_path))
        else:
            path = context.project_root

        if not path.exists():
            return ToolResult.error(
                f"search path does not exist: {path}. Use glob to find paths if you are unsure.",
                "not_found",
            )

        rg_path = shutil.which("rg")
        if not rg_path:
            return ToolResult.error(
                "ripgrep (rg) is not installed or not in PATH. "
                "Install it from https://github.com/BurntSushi/ripgrep or use the system package manager.",
                "missing_binary",
            )

        max_files = int(arguments.get("max_files", 12))
        context_lines = max(0, min(10, int(arguments.get("context", 0))))
        case_sensitive = bool(arguments.get("case_sensitive"))
        glob_pattern = arguments.get("glob")

        cmd = [rg_path, "--json", "--color", "never"]

        if not case_sensitive:
            cmd.append("-i")

        if context_lines > 0:
            cmd.extend(["-B", str(context_lines), "-A", str(context_lines)])

        if glob_pattern:
            cmd.extend(["--glob", str(glob_pattern)])

        # `-e` and `--` keep a pattern that starts with `-` (or looks like a ripgrep
        # flag, e.g. `--pre=<script>`) from being parsed as an option. Without them a
        # model-supplied pattern could point the search outside the project or make rg
        # run any program through `--pre`.
        cmd.extend(["-e", pattern, "--", str(path)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                cwd=context.project_root,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.error(
                f"search timed out (30s limit) for pattern: {pattern}. "
                "Try a more specific pattern or narrower path/glob.",
                "timeout",
            )
        except Exception as exc:
            return ToolResult.error(
                f"search failed: {exc}. Check that pattern is a valid regex.",
                "search_error",
            )

        # rg exits 1 when nothing matched; 2 and above are real errors.
        if result.returncode == 1:
            return _nothing_found(pattern, path, context)
        if result.returncode not in (0, 1):
            return ToolResult.error(
                f"ripgrep exited with code {result.returncode}: {result.stderr}",
                "rg_error",
            )

        files_seen: dict[str, list[tuple[int, str]]] = defaultdict(list)
        line_count: dict[str, int] = defaultdict(int)

        for line in result.stdout.splitlines():
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") == "match":
                file_path = entry.get("data", {}).get("path", {}).get("text", "")
                line_num = entry.get("data", {}).get("line_number", 0)
                text = entry.get("data", {}).get("lines", {}).get("text", "")

                if file_path:
                    truncated = text[:120].rstrip()
                    if len(text) > 120:
                        truncated += "…"

                    line_count[file_path] += 1
                    if len(files_seen[file_path]) < 5:
                        files_seen[file_path].append((line_num, truncated))

        if not files_seen:
            return _nothing_found(pattern, path, context)

        lines = []
        shown_files = 0
        total_files = len(files_seen)
        remaining_files = max(0, total_files - max_files)
        remaining_matches = 0

        for file_path in sorted(files_seen.keys()):
            if shown_files >= max_files:
                remaining_matches += line_count[file_path]
                continue

            count = line_count[file_path]
            shown_path = _relative(file_path, context.project_root)
            lines.append(f"{shown_path}: {count} match{'es' if count > 1 else ''}")

            for line_num, text in files_seen[file_path]:
                lines.append(f"  {line_num}: {text}")

            shown_files += 1

        if remaining_files > 0:
            lines.append(f"({remaining_files} more files, {remaining_matches} more matches)")

        content = "\n".join(lines)
        return ToolResult(
            content=content,
            metadata={
                "files_matched": total_files,
                "shown": shown_files,
                "matches": sum(line_count.values()),
            },
            files_touched=(),
        )


class GlobTool:
    def __init__(self) -> None:
        self.spec = ToolSpec(
            name="glob",
            #: A listing of a tree that keeps changing.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.BULK,
            ),
            description=(
                "List files matching a glob pattern using fd. Results are relative to "
                "the project root, sorted, and capped at 200. Respects .gitignore by default."
            ),
            parameters=parameters(
                {
                    "pattern": {
                        "type": "string",
                        "description": "Glob, e.g. **/*.py. ** recurses.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Default: project root.",
                    },
                },
                required=("pattern",),
            ),
            aliases={
                "pattern": ("glob", "filename", "file_pattern", "match"),
                "path": ("dir", "directory", "where", "root"),
            },
            budget_tokens=800,
            summary="list files matching a glob pattern",
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        pattern = str(arguments.get("pattern", "")).strip()
        if not pattern:
            return ToolResult.error("pattern is required", "missing_pattern")

        search_path = arguments.get("path", "")
        if search_path:
            path = resolve_path(context.project_root if context else None, str(search_path))
        else:
            path = context.project_root

        if not path.exists():
            return ToolResult.error(
                f"search path does not exist: {path}",
                "not_found",
            )

        fd_path = shutil.which("fd")
        if not fd_path:
            return ToolResult.error(
                "fd is not installed or not in PATH. "
                "Install it from https://github.com/sharkdp/fd or use the system package manager.",
                "missing_binary",
            )

        cmd = [fd_path, "--type", "f", "--color", "never"]

        # fd needs --glob for a glob pattern, and --full-path for one that contains "/"
        # or starts with "**".
        needs_glob = "*" in pattern or "?" in pattern or "[" in pattern
        needs_full_path = "/" in pattern or pattern.startswith("**")

        if needs_glob:
            cmd.append("--glob")
        if needs_full_path:
            cmd.append("--full-path")

        # `--` keeps a pattern starting with `-` from being parsed as an fd option.
        cmd.extend(["--", pattern, str(path)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                cwd=context.project_root,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.error(
                f"search timed out (30s limit) for pattern: {pattern}. "
                "Try a simpler pattern or narrower path.",
                "timeout",
            )
        except Exception as exc:
            return ToolResult.error(
                f"search failed: {exc}",
                "search_error",
            )

        if result.returncode == 1 or not result.stdout.strip():
            return _no_files(pattern, path)

        if result.returncode not in (0, 1):
            return ToolResult.error(
                f"fd exited with code {result.returncode}: {result.stderr}",
                "fd_error",
            )

        files: list[str] = []
        for file_path in sorted(result.stdout.splitlines()):
            if not file_path:
                continue
            try:
                rel = Path(file_path).relative_to(context.project_root)
                files.append(str(rel))
            except ValueError:
                files.append(file_path)

        if not files:
            return _no_files(pattern, path)

        shown = min(len(files), 200)
        remainder = max(0, len(files) - 200)

        lines = files[:200]
        if remainder > 0:
            lines.append(f"({remainder} more files)")

        content = "\n".join(lines)
        return ToolResult(
            content=content,
            metadata={"total_files": len(files), "shown": shown},
            files_touched=(),
        )
