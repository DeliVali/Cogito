"""Did the edit break the file?

Cheap and always on. It catches the dropped brace or broken indent that otherwise costs
five steps: the model edits, moves on, runs something unrelated, gets a confusing error
elsewhere and works backwards. Parsing before and after turns that into one message with a
line number.

Only a file that parsed before is rolled back when it fails after. A file that was already
broken is being repaired, and the model must be allowed to touch it.

**Python is parsed by this process, which is not the project's interpreter.** The harness
runs on 3.12 or 3.13 (`cli.py` refuses anything else). The project runs whatever `python3`
it has: on the machine where this was found, 3.14, where `except A, B:` (PEP 758) and
`t"..."` (PEP 750) were refused as "that edit would break the file". So when this process
cannot parse a Python file, `project_python` asks the project's interpreter and its verdict
decides. An edit is rolled back only if the file parsed before and does not after for that
interpreter, and neither `edit` nor `write` tells the model a file does not parse when it
does. Only that path costs a subprocess.

Others take the verdict from the task's environment too (read 2026-10-02). SWE-agent's
`windowed_edit_linting` runs flake8 `--select=...E999...` before and after and reverts on
new errors, from a `#!/usr/bin/env python3` script whose `install.sh` does
`pip install flake8` inside the task's environment. OpenHands-ACI's Python linter runs the
runtime's `flake8 --isolated` the same way. Aider (`aider/linter.py`) uses an in-process
`compile()` and `sys.executable -m flake8` (this module's old shape) but only reports,
never rolls back. Ruff (0.11 and 0.12, 2025) judges version-dependent syntax against the
project's declared version (`target-version`, else `requires-python`); doing that here
would need a parser for every Python, and the project's interpreter is already on PATH.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cogito.tools.build import resolve_python

#: How long the project's interpreter gets to answer. Parsing takes milliseconds and an
#: interpreter starts in about 6 ms here, so this only bounds a `python3` that hangs. On a
#: timeout this process's verdict stands.
PROJECT_PYTHON_TIMEOUT_SECONDS = 5.0

#: Exit status for "a SyntaxError, reported on stdout". Not 1, which an interpreter that
#: crashes for any other reason also exits with.
_SYNTAX_ERROR_STATUS = 3

#: `-I`: the project's directory is not on `sys.path` and `PYTHON*` variables are ignored,
#: so nothing in the project can shadow `ast`. `-S`: no `site`, so no `.pth` file runs.
#: The source arrives as UTF-8 bytes and is decoded explicitly, so the parse sees the same
#: `str` this process parsed; neither a coding cookie nor the interpreter's locale can
#: re-decode it. A syntax error comes back as its line, a newline, and its message.
_PROJECT_PYTHON_ARGS = (
    "-I",
    "-S",
    "-c",
    "import ast, sys\n"
    "try:\n"
    "    ast.parse(sys.stdin.buffer.read().decode('utf-8'))\n"
    "except SyntaxError as error:\n"
    "    sys.stdout.write(str(error.lineno or 0) + '\\n' + str(error.msg))\n"
    f"    sys.exit({_SYNTAX_ERROR_STATUS})\n",
)

#: Extension -> checker name. Any other suffix is not checked (`check` returns None).
_PYTHON = {".py", ".pyi"}
_JSON = {".json"}
_NODE = {".js", ".cjs", ".mjs"}

_ERROR_LINE = re.compile(r"^\w*(SyntaxError|ReferenceError|TypeError)\b")
_STDIN_LINE = re.compile(r"^\[stdin\]:(\d+)")


@dataclass(frozen=True, slots=True)
class Check:
    ok: bool
    checker: str
    message: str = ""
    line: int | None = None


def available_for(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _PYTHON:
        return "python"
    if suffix in _JSON:
        return "json"
    if suffix in _NODE and shutil.which("node"):
        return "node"
    return None


def check(path: Path, text: str) -> Check | None:
    """Parse ``text`` as whatever ``path`` claims to be. None when nothing can check it."""
    checker = available_for(path)
    if checker is None:
        return None
    if checker == "python":
        return _python(text)
    if checker == "json":
        return _json(text)
    return _node(path, text)


def _python(text: str) -> Check:
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return Check(False, "python", exc.msg or "syntax error", exc.lineno)
    return Check(True, "python")


#: Set on a tool result whose outcome the project's interpreter decided against this
#: process's parse: an edit kept or rolled back, a warning left out.
DEFERRED = "verify_deferred_to_project_python"


def deferred_metadata(deferred: bool) -> dict[str, bool]:
    """`DEFERRED` only when it happened, so every other call's metadata is unchanged."""
    return {DEFERRED: True} if deferred else {}


def project_python(text: str, *, cwd: Path, env: Mapping[str, str] | None = None) -> Check | None:
    """The project's own `python3`'s verdict on ``text``.

    That is the interpreter the build gate's Python row compiles with
    (`tools.build.resolve_python`), found on ``env``'s PATH (this process's environment
    when None) and run from ``cwd`` so a version manager's shim picks the version the
    project pins. None when it gave no verdict (no interpreter, a timeout, a crash), and
    the caller keeps the one it already had."""
    environment = dict(os.environ if env is None else env)
    try:
        completed = subprocess.run(
            [resolve_python(environment), *_PROJECT_PYTHON_ARGS],
            input=text.encode("utf-8"),
            capture_output=True,
            cwd=cwd,
            env=environment,
            timeout=PROJECT_PYTHON_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if completed.returncode == 0:
        return Check(True, "python")
    if completed.returncode != _SYNTAX_ERROR_STATUS:
        return None
    line, _, message = completed.stdout.decode("utf-8", errors="replace").partition("\n")
    if not line.strip().isdigit():
        return None
    return Check(False, "python", message.strip() or "syntax error", int(line) or None)


def _json(text: str) -> Check:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return Check(False, "json", exc.msg, exc.lineno)
    return Check(True, "json")


def _node(path: Path, text: str) -> Check:
    try:
        result = subprocess.run(
            ["node", "--check", "-"],
            input=text,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return Check(True, "node")  # cannot check is not the same as broken
    if result.returncode == 0:
        return Check(True, "node")

    # node prints the offending source, a blank line, the error, a stack, and its version
    # banner last. Taking the last line would report the Node version as the syntax error.
    stderr = result.stderr.splitlines()
    message = next(
        (fragment.strip() for fragment in stderr if _ERROR_LINE.match(fragment.strip())),
        "",
    )
    line = None
    for fragment in stderr:
        if found := _STDIN_LINE.match(fragment.strip()):
            line = int(found.group(1))
            break
    return Check(False, "node", message[:200] or "syntax error", line)


def window(lines: list[str], around: int | None, radius: int = 3) -> str:
    """The lines either side of a syntax error, numbered, so it can be seen."""
    if around is None or not lines:
        return ""
    centre = max(1, min(around, len(lines)))
    low = max(0, centre - 1 - radius)
    high = min(len(lines), centre + radius)
    width = len(str(high))
    return "\n".join(
        f"{str(low + offset + 1).rjust(width)}| {line}"
        for offset, line in enumerate(lines[low:high])
    )
