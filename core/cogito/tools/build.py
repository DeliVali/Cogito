"""The build gate: compile what changed before the harness believes the model is done.

It is not model-callable (no `ToolSpec`, never in `ToolRegistry`) and `loop/engine.py`
triggers it, so it might seem to belong in `loop/`. It lives here because it needs
`guard.policy.outside` for the same sandbox-symlink defence `tools/quality.py` applies to
its linters, and `dev/check_layers.py` lets `tools` import `guard` but not `loop` import it.
CONTRIBUTING.md section 3's `tools/` row covers harness-run checks built from the same
subprocess machinery as `check`; this module is one.

Evidence (CONTRIBUTING.md section 6b). On the quick suite qwen3.5-9b failed 40/72 runs; 24
ended with the model declaring `END_TURN`, and at least 9 of those 24 left code that does
not compile (go build "undefined: catalog", javac "';' expected", tsc TS2304/TS2552, rustc
unresolved imports). Self-judged completion degenerates into accept-all (arXiv 2607.25152,
2026-07-27). Most self-repair gains land in the first two rounds, and logic errors repair
far worse than syntax and name errors (arXiv 2604.10508, 2026-04-12). So the gate only
compiles, never tests, and is capped at two rounds (arXiv 2607.05197, 2026-07-06).
Feedback is structured `path:line: message` findings, never a raw log: verbatim surface
form hurts small models (arXiv 2608.23651, 2026-08-24). For a small model, logic errors are
better left to resampling than in-context repair (arXiv 2607.26117, 2026-07-28; arXiv
2609.00854, 2026-09-01), so no row runs a test command.

Discovery is harness-owned, like `quality.CHECKERS`: one row per language in `ROWS`.
Adding a language is adding a row. A row discovers its files in one of two ways:

- Touched-file rows (Python, and TypeScript without a `tsconfig.json`) compile only the
  files this session is known to have changed: `edit`/`write`, or a shell-made change to a
  file the session already knew about (see `loop.engine._shell_edited_paths`). Never a
  directory walk.
- Whole-project rows (C/C++, Java, Go, Rust, TypeScript with a `tsconfig.json`) run
  whenever their marker exists at the project root (`go.mod`, `Cargo.toml`,
  `tsconfig.json`; C/C++ and Java have no marker file, so any matching source file under
  the root), whatever language the touched files were in. C/C++ is whole-project because
  a touched-file row misses the cases that matter most: an edit to a header only, or a
  renamed function whose untouched caller is never recompiled. Markers are read at the
  project root only, so a `tsconfig.json` vendored inside `node_modules` does not count.

What the `hidden_tests/` exclusion covers. `hidden_tests/` holds a benchmark task's
verifier (`bench/tasks/<id>/hidden_tests/`). `bench/runner.py` copies only `fixture/`, so a
real `cogito chat` workspace never has it; it is excluded anyway, for defence in depth.
The exclusion holds where this module controls discovery: touched-file rows only see
harness-tracked paths, and `_walk_files` (C/C++, Java) prunes any `hidden_tests`
directory. Go, Rust and TypeScript with a tsconfig hand a pattern (`./...`, the crate
root, the tsconfig's `include`) to their own toolchain, which does its own traversal.
`go build ./...` skips `_test.go` and `testdata/`, and `cargo check` (no `--all-targets`)
never reads `tests/`, but a non-test `hidden_tests/*.go` reachable from the module root
would still compile. Excluding it would mean walking the module ourselves (losing
`go build`'s workspace resolution) or running `go list` first, which is not worth it for a
directory a real run never has. No row runs `verify.sh` or any test command.

Every row must leave the project clean. `-fsyntax-only` (C/C++) and `--noEmit` (tsc) never
write output. The other three write by default and are redirected (measured here,
2026-09-24):

- `javac` needs an explicit `-d <dir>`, or it drops `.class` files next to the source.
- `python -m py_compile <file>` always writes `<dir>/__pycache__/<file>.cpython-*.pyc`
  next to the source and ignores `PYTHONDONTWRITEBYTECODE`, which only suppresses implicit
  caching on import (confirmed both ways). Its CLI has no output option, so this row calls
  `py_compile.compile(file, cfile=..., doraise=True)` through `python -c`. This is the one
  place the catalogue departs from CONTRIBUTING's wording: the rule against creating files
  in the project wins.
- `go build ./...` writes a binary into the project root when the module has a `main`
  package. Do not use `-o <dir>/`: aimed at a directory, it silently narrows the build to
  linkable targets on this Go toolchain and skips a broken library package that sits next
  to an unrelated `main` (controller review). `go build -o /dev/null ./...` does not,
  confirmed on a library-only module, a broken library next to `cmd/x/main.go`, and the
  mixed case, so it is used unconditionally.

`cargo check`'s `target/` and Go's `$GOCACHE` are left alone, since the agent's own shell
would create them too. The Rust row may also write or update `Cargo.lock` next to
`Cargo.toml`: any `cargo` run would, and refusing would mean not resolving dependencies.

The redirected rows never pre-create their scratch directory: `javac -d`, `go build -o`
(on a real path; `/dev/null` needs none) and `py_compile.compile(cfile=...)` each create
its parent themselves (confirmed for each). This matters under `--sandbox`: bubblewrap
gives every sandboxed run its own empty `--tmpfs /tmp` (`guard/sandbox/__init__.py`), so a
directory this process created on the host's `/tmp` would not exist inside the sandbox.
A path under `/tmp` is picked, whichever process runs creates it, and this module only
removes it afterwards (best effort).

Exit codes matter. A checker whose output the line parser does not recognise (a
config-level tsc error such as `TS18003`, with no `file(line,col)`; a cargo manifest
failure; a Go toolchain-version refusal) yields no findings, so a nonzero exit with no
parsed findings never reads as `passed`. Its first non-blank output line is checked
against environment-shaped phrases measured on this machine (2026-09-24): `cargo check
--offline` with an uncached dependency prints "error: no matching package named `serde`
found" and later "offline mode"; a `go.mod` needing a newer toolchain than
`GOTOOLCHAIN=local` allows prints "go.mod requires go >= 1.99 (running go 1.27.1;
GOTOOLCHAIN=local)". A match becomes `not_verified` with that line as the reason. Anything
else is project-shaped (`TS18003` is a real, fixable `tsconfig.json` mistake) and becomes
one generic `failed` finding built from that line. gcc/g++'s `fatal error:` (usually a
missing header) counts as an error alongside `error:`, while `warning:` lines are dropped.

Compilers answer in the locale's language unless told otherwise (this machine reports gcc
errors in Spanish), so every subprocess runs under `LC_ALL=C`/`LANG=C`/`LANGUAGE=C`. That
is the only override added to the given environment. Every binary is looked up on
`env["PATH"]`, never this process's `os.environ["PATH"]`, including the Python row's
interpreter (`shutil.which("python3", path=...)`, not `sys.executable`): the project being
compiled need not run under the harness's virtualenv. Output is decoded with
`errors="replace"`, so a stray non-UTF-8 byte that a compiler echoes back from a string
literal becomes part of a finding rather than a `UnicodeDecodeError`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from cogito.guard.policy import outside
from cogito.ids import new_id
from cogito.provider import tokens

#: Per-subprocess ceiling. `run_gate` also bounds each invocation by what is left of the
#: round's `timeout`, which `loop.engine._maybe_run_build_gate` derives from the turn's
#: remaining wall clock. A hung compiler must not hang the turn.
TIMEOUT_SECONDS = 120

#: Caps on the injected repair message. `MAX_PER_FILE` bounds one noisy file.
#: `MAX_FINDINGS_SHOWN` and `_RENDER_TOKEN_BUDGET` both stop `render`, whichever is hit
#: first. A count cap says nothing about size: a controller review (2026-09-24) measured
#: 1,679 tokens for 20 long findings across many files, and `context.budgets.clip` (applied
#: again in `loop.engine`) leaves content of 160 lines or fewer unclipped whatever its
#: token count. So `render` counts tokens itself and keeps whole findings.
MAX_FINDINGS_SHOWN = 20
MAX_PER_FILE = 5
_MESSAGE_CHARS = 200
#: `render`'s token ceiling. Below the injected message's ~600-token allowance
#: (`loop.engine._BUILD_GATE_MESSAGE_TOKENS`), leaving room for the prompt template
#: around it.
_RENDER_TOKEN_BUDGET = 560

RowOutcome = Literal["passed", "failed", "not_verified"]
GateOutcome = Literal["passed", "failed", "not_verified", "skipped_no_edits"]

#: Directories no row looks inside, in a touched-file filter or a project walk.
#: `hidden_tests` is named explicitly; the module docstring says what that does and does
#: not cover.
_IGNORED_DIR_NAMES = frozenset(
    {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".cogito",
        "dist", "build", "target", "hidden_tests",
    }
)

#: After the `path:line[:col]:` prefix, a message must start with one of these to count
#: as an error rather than a warning or note. `fatal error:` (gcc/g++, usually a missing
#: header) is its own prefix. Matching "error" anywhere in the message would also catch a
#: warning that merely mentions the word.
_ERROR_PREFIXES = ("error", "fatal error")

#: Phrases meaning "could not be checked" rather than "wrong": an offline dependency
#: fetch, a missing or too-old toolchain, a network or disk problem. Matched against the
#: first non-blank output line when the exit code was nonzero and nothing matched the
#: row's diagnostic format. Not exhaustive: each phrase was produced on this machine on
#: 2026-09-24 (`cargo check --offline` with an uncached crate; a `go.mod` `go` directive
#: newer than `GOTOOLCHAIN=local` allows) or generalises one. Extend as measured, like
#: `quality.CHECKERS`.
_ENVIRONMENT_MARKERS = (
    "no matching package",
    "failed to fetch",
    "failed to download",
    "failed to get",
    "offline mode",
    "could not find `",
    "unable to get packages from source",
    "dial tcp",
    "network is unreachable",
    "no space left on device",
    "requires go",
    "gotoolchain",
    "cannot find module",
    "no required module provides package",
    "command not found",
    "permission denied",
)

#: Build manifests by language row. If one was edited this session and its row's
#: toolchain then exits nonzero with nothing recognised, the failure is reported against
#: the manifest and never classified `not_verified`. The wording of a real environment
#: problem (cargo's "no matching package", a Go toolchain refusal) also describes a
#: manifest the model just broke: a controller review (2026-09-24) found a typo'd Rust
#: dependency, a missing path dependency and a `go` directive raised past the local
#: toolchain all accepted that way. The bench restores manifests before verifying, so
#: such a mistake always fails there; the gate must not forgive it.
_MANIFEST_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "python": ("pyproject.toml",),
    "cpp": ("CMakeLists.txt",),
    "c": ("CMakeLists.txt",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "go": ("go.mod",),
    "rust": ("Cargo.toml",),
    "typescript": ("tsconfig.json", "package.json"),
}

#: `python -m py_compile` has no output option (see the module docstring), so this calls
#: the same function directly with an explicit `cfile`.
_PY_COMPILE_SCRIPT = (
    "import py_compile, sys\n"
    "try:\n"
    "    py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)\n"
    "except py_compile.PyCompileError as error:\n"
    "    print(error.msg, file=sys.stderr)\n"
    "    sys.exit(1)\n"
    "except OSError as error:\n"
    "    print(error, file=sys.stderr)\n"
    "    sys.exit(1)\n"
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One diagnostic. `path` is project-relative, exactly as the compiler named it."""

    path: str
    line: int
    message: str


@dataclass(frozen=True, slots=True)
class Invocation:
    """One subprocess this round: a language tag, a binary, its arguments and what it
    covers. `"{tempdir}"` in an argument becomes a fresh scratch path before the binary
    runs and is removed (best effort) afterwards. The module docstring says why that path
    is never pre-created here.
    """

    language: str
    binary: str
    arguments: tuple[str, ...]
    #: Project-relative paths this invocation covers, for telemetry. Empty for a
    #: whole-project invocation.
    targets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RowResult:
    """What happened for one invocation, ready to become a WAL event."""

    language: str
    command: tuple[str, ...]
    targets: tuple[str, ...]
    ran: bool
    exit_code: int | None
    findings: tuple[Finding, ...]
    outcome: RowOutcome
    #: Findings a parser recognised but dropped as environment-only noise, counted so they
    #: never silently vanish. Every current parser returns 0.
    ignored: int = 0
    detail: str = ""
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class GateRun:
    """Every row this round produced, plus the roll-up `loop.engine` acts on."""

    rows: tuple[RowResult, ...]

    @property
    def findings(self) -> tuple[Finding, ...]:
        """Every finding across every row, deduplicated, in first-seen order.

        C/C++ is a whole-project row, so a broken header is reported once per `.cpp`/`.c`
        translation unit that includes it. `Finding` is frozen and hashable, so
        `dict.fromkeys` dedupes on `(path, line, message)` and keeps the first position.
        """
        combined = [finding for row in self.rows for finding in row.findings]
        return tuple(dict.fromkeys(combined))

    @property
    def ran_any(self) -> bool:
        """Whether any row's subprocess ran. It says nothing about verification: a row
        can run and still be `not_verified` (a toolchain refused the job, an offline
        dependency could not be fetched). Kept for callers that only ask whether a process
        was spawned; `outcome` decides pass/fail and does not use it.
        """
        return any(row.ran for row in self.rows)

    @property
    def outcome(self) -> GateOutcome:
        """`failed` if there is any finding; `passed` only if at least one row verified
        clean code; `not_verified` otherwise. That includes rows that ran but all came
        back `not_verified`: a Go toolchain refusal runs a subprocess yet verifies nothing,
        so `ran_any` is not enough (found in review, 2026-09-24).
        """
        if self.findings:
            return "failed"
        if any(row.outcome == "passed" for row in self.rows):
            return "passed"
        return "not_verified"


def _ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in _IGNORED_DIR_NAMES for part in relative.parts)


def _walk_files(root: Path, *, suffixes: frozenset[str]) -> list[Path]:
    """Every file under `root` with a suffix in `suffixes`, pruned by
    `_IGNORED_DIR_NAMES` and never following a symlinked directory.

    `Path.glob("**/...")` follows symlinked directories on every Python this project
    supports (`recurse_symlinks` is 3.13+); `os.walk` with `followlinks=False` does not.
    A directory symlink back to an ancestor makes a glob walk forever, and one pointing
    outside the project (`vendor -> /usr/share`) scans whatever it points at. Both were
    reproduced against this function (2026-09-24), sandboxed or not. The whole-project
    rows (C/C++, Java) use this instead of a glob. When sandboxed, `_kept_relative` also
    drops a reached file that is itself a symlink leaving the project.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if name not in _IGNORED_DIR_NAMES and not name.startswith(".")
        ]
        for name in filenames:
            if Path(name).suffix in suffixes:
                found.append(Path(dirpath) / name)
    return found


def _kept_relative(paths: Iterable[Path], root: Path, *, sandboxed: bool) -> list[Path]:
    """Normalise to project-relative paths that exist as files right now. Drops anything
    outside `root` and, only when sandboxed, anything a symlink resolves outside it (the
    same defence as `tools/quality.py`'s `_drop_outside`).

    The existence check is required. A path the harness had evidence for (`edit`/`write`'s
    `files_touched`, or a shell-made change to a known path; see
    `loop.engine._shell_edited_paths`) can be gone by the time the gate runs, e.g. a file a
    `write` created and a later `shell rm` removed (reproduced). Compiling it would report
    the compiler's "no such file" as a finding and tell the model to fix a file it deleted
    on purpose.
    """
    root_real = Path(os.path.realpath(root))
    kept: list[Path] = []
    for path in paths:
        absolute = path if path.is_absolute() else root / path
        if sandboxed:
            real = Path(os.path.realpath(absolute))
            if outside(real, [root_real]):
                continue
            absolute = real
        try:
            relative = absolute.relative_to(root_real)
        except ValueError:
            try:
                relative = Path(os.path.realpath(absolute)).relative_to(root_real)
            except ValueError:
                continue
        candidate = root_real / relative
        if _ignored(candidate, root_real) or not candidate.is_file():
            continue
        kept.append(relative)
    return sorted(set(kept))


def _strip_project_prefix(text: str, root: Path) -> str:
    """Replace `root`'s absolute path in `text` with a relative form.

    A compiler message can embed the absolute project path even when every argument was
    relative: tsc's `TS18003` names the `tsconfig.json` by its full path. The C/C++ row
    passes `-I.` rather than `-I<root>` so the include search path does not leak into a
    header's reported path. Both the path as given and its realpath are replaced, since
    they can differ (a symlinked `/tmp`, say). With `-I.`, a header reached through the search
    path is echoed as `./sub/util.h`; the leading `./` is stripped too.
    """
    if not text:
        return text
    for candidate in dict.fromkeys((str(root), str(Path(os.path.realpath(root))))):
        text = text.replace(candidate + os.sep, "").replace(candidate, ".")
    return text.removeprefix("./")


def _first_meaningful_line(*streams: str) -> str:
    for stream in streams:
        for raw in stream.splitlines():
            line = raw.strip()
            if line:
                return line
    return ""


def _looks_like_environment_problem(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _ENVIRONMENT_MARKERS)


_FATAL_MISSING_HEADER = re.compile(
    r"fatal error:\s*(?P<header>\S+):\s*no such file or directory", re.IGNORECASE
)


def _find_by_name(root: Path, name: str) -> bool:
    """Whether a file named `name` exists anywhere under `root`. The same pruned,
    symlink-safe walk as `_walk_files`, but not filtered by suffix: a header can be `.h`,
    `.hpp`, `.hh` or have no suffix.
    """
    for _dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames if d not in _IGNORED_DIR_NAMES and not d.startswith(".")
        ]
        if name in filenames:
            return True
    return False


def _reclassify_missing_headers(
    result: RowResult, *, root: Path, touched: frozenset[str]
) -> RowResult:
    """Move a C/C++ `fatal error: X: No such file or directory` out of `findings` when no
    file named X exists in the project tree and this session never touched that
    translation unit. C/C++ is a whole-project row, so it compiles sources the model never
    went near. A file kept for another platform (`src/win/port.cpp` including
    `<windows.h>`, with no Windows headers anywhere) cannot build here and is not the
    model's fault (reproduced against that layout, 2026-09-24). If the model touched the
    translation unit, the missing header stays a finding: it added the `#include` or
    should have added the header.
    """
    if not result.findings:
        return result
    kept: list[Finding] = []
    reclassified: list[str] = []
    for finding in result.findings:
        match = _FATAL_MISSING_HEADER.search(finding.message)
        header_name = Path(match["header"]).name if match else ""
        if (
            match
            and finding.path not in touched
            and not _find_by_name(root, header_name)
        ):
            reclassified.append(f"{finding.path}: missing header {match['header']}")
            continue
        kept.append(finding)
    if not reclassified:
        return result
    note = "; ".join(reclassified[:3])
    if not kept:
        return replace(
            result, findings=(), outcome="not_verified", detail=f"not verified: {note}"
        )
    if result.detail:
        detail = f"{result.detail} (also not verified: {note})"
    else:
        detail = f"not verified: {note}"
    return replace(result, findings=tuple(kept), detail=detail)


def _is_maven_test_source(relative: Path) -> bool:
    """`src/test/**` is the Maven/Gradle test source root. The harness has no dependency
    manager and no classpath beyond the project's own `.java` files, so a JUnit (or other
    test framework) import there can never compile. Excluded rather than reported as a
    false "package does not exist"."""
    parts = relative.parts
    return any(parts[i] == "src" and parts[i + 1] == "test" for i in range(len(parts) - 1))


def resolve_python(env: Mapping[str, str]) -> str:
    """The `python3` on `env["PATH"]`: the compiled project's interpreter, not
    `sys.executable` (the harness's own, possibly a virtualenv `cogito chat` runs under).
    `edit` asks the same interpreter before it rolls a Python edit back
    (`tools.edit.verify`)."""
    found = shutil.which("python3", path=env.get("PATH"))
    if found is None and os.name == "nt":
        # Windows installs name it `python`; `python3` there is often the Store's stub.
        found = shutil.which("python", path=env.get("PATH"))
    return found or "python3"


_CMAKE_CXX_STANDARD = re.compile(r"CMAKE_CXX_STANDARD\s+\"?(\d+)", re.IGNORECASE)


def _cxx_standard(root: Path) -> str:
    """`-std=` for the C/C++ row: `CMAKE_CXX_STANDARD` from a project-root
    `CMakeLists.txt` when it declares one, else `c++17`."""
    try:
        text = (root / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "c++17"
    match = _CMAKE_CXX_STANDARD.search(text)
    return f"c++{match.group(1)}" if match else "c++17"


#: The argument list of `include_directories(a b c)` or
#: `target_include_directories(tgt PUBLIC a b c)`. The two differ only by a leading
#: target name and scope keyword, which need no special handling: `_cmake_include_dirs`
#: skips any token that is not an existing directory under the project root (a target
#: name, `PUBLIC`, `PRIVATE`, `INTERFACE`, an unresolved `${VARIABLE}` or generator
#: expression).
_CMAKE_INCLUDE_DIRS = re.compile(
    r"(?:include_directories|target_include_directories)\s*\(([^)]*)\)", re.IGNORECASE
)


def _cmake_include_dirs(root: Path) -> list[str]:
    """Extra `-I` paths for the C/C++ row: every existing project-relative directory
    named in a project-root `CMakeLists.txt`'s `include_directories`/
    `target_include_directories` calls, on top of the always-present `-I.`. Without them,
    headers in a directory only CMake knows about are reported missing (reproduced
    2026-09-24 with `target_include_directories(app PRIVATE include)` and sources that
    reach `include/mylib/util.h` only through it).
    """
    try:
        text = (root / "CMakeLists.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    for call in _CMAKE_INCLUDE_DIRS.finditer(text):
        for token in call.group(1).split():
            if "$" in token or "<" in token or ">" in token:
                continue  # a CMake variable or generator expression this module cannot resolve
            if (root / token).is_dir():
                found.append(token)
    return sorted(dict.fromkeys(found))


# -- discovery: one function per row, `plan(root, touched, sandboxed, env) -> [Invocation]`


def _plan_python(
    root: Path, touched: Sequence[Path], sandboxed: bool, env: Mapping[str, str]
) -> list[Invocation]:
    interpreter = resolve_python(env)
    return [
        Invocation(
            "python", interpreter,
            ("-c", _PY_COMPILE_SCRIPT, str(path), "{tempdir}/out.pyc"),
            (str(path),),
        )
        for path in touched
        if path.suffix == ".py"
    ]


def _plan_c_cpp(
    root: Path, touched: Sequence[Path], sandboxed: bool, env: Mapping[str, str]
) -> list[Invocation]:
    """Whole-project: every `.cpp`/`.cc`/`.cxx`/`.c` under the root, not only touched
    files. A header-only edit, or a rename that breaks an untouched caller, shows up only
    when the failing file is recompiled. One invocation per compiler (all C++ files, all C
    files) rather than per file: g++/gcc attribute errors correctly across files in one
    call (confirmed with this command), and a project can hold far more files than a
    touched set.
    """
    cpp_files = _kept_relative(
        _walk_files(root, suffixes=frozenset({".cpp", ".cc", ".cxx"})), root, sandboxed=sandboxed
    )
    c_files = _kept_relative(
        _walk_files(root, suffixes=frozenset({".c"})), root, sandboxed=sandboxed
    )
    include_flags = tuple(f"-I{d}" for d in _cmake_include_dirs(root))
    invocations: list[Invocation] = []
    if cpp_files:
        names = sorted(str(p) for p in cpp_files)
        invocations.append(
            Invocation(
                "cpp", "g++",
                (f"-std={_cxx_standard(root)}", "-fsyntax-only", "-I.", *include_flags, *names),
                tuple(names),
            )
        )
    if c_files:
        names = sorted(str(p) for p in c_files)
        invocations.append(
            Invocation("c", "gcc", ("-fsyntax-only", "-I.", *include_flags, *names), tuple(names))
        )
    return invocations


def _plan_java(
    root: Path, touched: Sequence[Path], sandboxed: bool, env: Mapping[str, str]
) -> list[Invocation]:
    files = _kept_relative(
        _walk_files(root, suffixes=frozenset({".java"})), root, sandboxed=sandboxed
    )
    files = [path for path in files if not _is_maven_test_source(path)]
    if not files:
        return []
    names = sorted(str(p) for p in files)
    return [
        Invocation("java", "javac", ("-d", "{tempdir}", "-proc:none", *names), tuple(names))
    ]


def _plan_go(
    root: Path, touched: Sequence[Path], sandboxed: bool, env: Mapping[str, str]
) -> list[Invocation]:
    if not (root / "go.mod").is_file():
        return []
    return [Invocation("go", "go", ("build", "-o", os.devnull, "./..."), ())]


def _plan_rust(
    root: Path, touched: Sequence[Path], sandboxed: bool, env: Mapping[str, str]
) -> list[Invocation]:
    if not (root / "Cargo.toml").is_file():
        return []
    return [
        Invocation(
            "rust", "cargo",
            ("check", "--offline", "--quiet", "--message-format", "short"),
            (),
        )
    ]


def _plan_typescript(
    root: Path, touched: Sequence[Path], sandboxed: bool, env: Mapping[str, str]
) -> list[Invocation]:
    if (root / "tsconfig.json").is_file():
        return [Invocation("typescript", "tsc", ("--noEmit", "-p", "."), ())]
    files = sorted(str(p) for p in touched if p.suffix == ".ts")
    if not files:
        return []
    # Matches the quick suite's no-tsconfig `verify.sh` (`--target es2022 --module
    # nodenext --moduleResolution nodenext <file>.ts`) so the gate and the verifier agree
    # on what compiles. `--outDir build` becomes `--noEmit`, since this row must not write
    # into the project; tsc reports the same diagnostics either way. No `--skipLibCheck`:
    # verify.sh does not pass it, and review found it masked a failure the verifier also
    # reports.
    arguments = [
        "--noEmit", "--target", "es2022",
        "--module", "nodenext", "--moduleResolution", "nodenext",
    ]
    # Without a tsconfig `types` restriction, tsc already includes every package under a
    # resolvable node_modules/@types. Naming `node` only makes that visible, matching
    # what verify.sh's `tsc` would resolve with the same directory present.
    if (root / "node_modules" / "@types" / "node").is_dir():
        arguments += ["--types", "node"]
    arguments.extend(files)
    return [Invocation("typescript", "tsc", tuple(arguments), tuple(files))]


# -- parsing: one function per output shape, `parse(stdout, stderr, rc, inv, root) -> ` ---
# -- `(findings, ignored_count)` ------------------------------------------------------------

_DIAG_LINE = re.compile(r"^(?P<path>[^:\s]+):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.+)$")

ParseResult = tuple[list[Finding], int]
_Parser = Callable[[str, str, int, Invocation, Path], ParseResult]


def _parse_diagnostic_lines(
    text: str, root: Path, *, require_error_prefix: bool
) -> list[Finding]:
    """The common `path:line[:col]: message` shape: gcc, g++, simple javac cases (see
    `_parse_javac` for "cannot find symbol" and similar), go build, cargo
    (`--message-format short`). `require_error_prefix` drops non-errors: gcc and rustc
    mark a warning the same way with a "warning: " prefix, so the message's own prefix
    (`_ERROR_PREFIXES`) is checked, not the word "error" anywhere in it. `go build` never
    emits a warning here (a Go compile failure has no `-W`), so its row passes
    `require_error_prefix=False`.
    """
    findings: list[Finding] = []
    for raw in text.splitlines():
        match = _DIAG_LINE.match(raw.strip())
        if not match:
            continue
        message = match["message"].strip()
        if require_error_prefix and not message.lower().startswith(_ERROR_PREFIXES):
            continue
        path = _strip_project_prefix(match["path"], root)
        findings.append(Finding(path, int(match["line"]), message[:_MESSAGE_CHARS]))
    return findings


def _generic(*, require_error_prefix: bool) -> _Parser:
    def parse(
        stdout: str, stderr: str, returncode: int, invocation: Invocation, root: Path
    ) -> ParseResult:
        combined = f"{stdout}\n{stderr}"
        findings = _parse_diagnostic_lines(
            combined, root, require_error_prefix=require_error_prefix
        )
        return findings, 0

    return parse


_JAVAC_CONTINUATION = re.compile(r"^\s+(symbol|location):\s*(.+)$")
_JAVAC_SUMMARY = re.compile(r"^\d+\s+(errors?|warnings?)$")
#: How many lines past a diagnostic to search for `symbol:`/`location:`. The source
#: snippet and caret are normally two lines, so this is generous.
_JAVAC_CONTEXT_WINDOW = 8


def _parse_javac(
    stdout: str, stderr: str, returncode: int, invocation: Invocation, root: Path
) -> ParseResult:
    """javac's shape, plus the continuation lines a bare `path:line: message` parse
    drops. "cannot find symbol" and "incompatible types" give the file and line, but which
    symbol or type is only on the following `  symbol:   ...` / `  location: ...` lines.
    Measured 2026-09-24 against a caller broken by an untouched rename: without them the
    model only learns that some symbol could not be found.
    """
    combined = f"{stdout}\n{stderr}"
    lines = combined.splitlines()
    findings: list[Finding] = []
    index = 0
    while index < len(lines):
        match = _DIAG_LINE.match(lines[index].strip())
        if not match or not match["message"].strip().lower().startswith(_ERROR_PREFIXES):
            index += 1
            continue
        message = match["message"].strip()
        path = _strip_project_prefix(match["path"], root)
        line_number = int(match["line"])
        extra: list[str] = []
        cursor = index + 1
        limit = min(len(lines), index + 1 + _JAVAC_CONTEXT_WINDOW)
        while cursor < limit:
            candidate = lines[cursor].strip()
            if _DIAG_LINE.match(candidate) or _JAVAC_SUMMARY.match(candidate):
                break
            if continuation := _JAVAC_CONTINUATION.match(lines[cursor]):
                extra.append(f"{continuation.group(1)}: {continuation.group(2).strip()}")
            cursor += 1
        if extra:
            message = f"{message} ({'; '.join(extra)})"
        findings.append(Finding(path, line_number, message[:_MESSAGE_CHARS]))
        index = cursor if cursor > index + 1 else index + 1
    return findings, 0


_TS_LINE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),\d+\):\s*error\s+(?:(?P<code>TS\d+):\s*)?(?P<message>.+)$"
)


def _parse_tsc(
    stdout: str, stderr: str, returncode: int, invocation: Invocation, root: Path
) -> ParseResult:
    """tsc's shape, `file(line,col): error TSxxxx: message`, on stdout rather than stderr.

    Every diagnostic is kept. Do not ignore codes that seem to fire only because one file
    is compiled alone (TS2591, TS2835, ...): the bench's `verify.sh` runs `tsc` with
    nothing suppressed (no `--skipLibCheck`, no `--types`), so they fail there too, e.g.
    `process.env.DEBUG` without `@types/node` (controller review, 2026-09-24).
    `_plan_typescript` matches its fallback flags to `verify.sh` instead.
    """
    findings: list[Finding] = []
    for raw in f"{stdout}\n{stderr}".splitlines():
        match = _TS_LINE.match(raw.strip())
        if not match:
            continue
        code = match["code"] or ""
        text = f"{code}: {match['message'].strip()}" if code else match["message"].strip()
        path = _strip_project_prefix(match["path"], root)
        findings.append(Finding(path, int(match["line"]), text[:_MESSAGE_CHARS]))
    return findings, 0


_PY_FILE_LINE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')
#: `PyCompileError.msg` does not always use the `File "...", line N` traceback shape.
#: Measured on this Python (2026-09-24): a plain `SyntaxError` from `compile()` uses it,
#: while an `IndentationError` ends its single-line message with `(path, line N)`.
_PY_PAREN_LINE = re.compile(r"\((?P<path>[^,()]+),\s*line\s*(?P<line>\d+)\)\s*$")


def _parse_py_compile(
    stdout: str, stderr: str, returncode: int, invocation: Invocation, root: Path
) -> ParseResult:
    if returncode == 0:
        return [], 0
    combined = f"{stdout}\n{stderr}"
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    message = lines[-1] if lines else "py_compile failed"

    match = _PY_FILE_LINE.search(combined)
    if match:
        path, line = match["path"], int(match["line"])
    elif paren := _PY_PAREN_LINE.search(message):
        path, line = paren["path"], int(paren["line"])
        message = message[: paren.start()].rstrip()  # the (path, line) is now structured
    else:
        path = invocation.targets[0] if invocation.targets else ""
        line = 0

    path = _strip_project_prefix(path, root)
    return [Finding(path, line, message[:_MESSAGE_CHARS])], 0


@dataclass(frozen=True, slots=True)
class Row:
    purpose: str
    plan: Callable[[Path, Sequence[Path], bool, Mapping[str, str]], list[Invocation]]
    parse: _Parser


#: The catalogue. Adding a language is adding a row.
ROWS: tuple[Row, ...] = (
    Row("Python syntax check", _plan_python, _parse_py_compile),
    Row("C/C++ syntax check", _plan_c_cpp, _generic(require_error_prefix=True)),
    Row("Java compile check", _plan_java, _parse_javac),
    Row("Go build check", _plan_go, _generic(require_error_prefix=False)),
    Row("Rust build check", _plan_rust, _generic(require_error_prefix=True)),
    Row("TypeScript typecheck", _plan_typescript, _parse_tsc),
)


def _run_invocation(
    invocation: Invocation,
    parse: _Parser,
    *,
    root: Path,
    wrapper: Sequence[str],
    env: Mapping[str, str],
    timeout: float,
    touched: frozenset[str],
) -> RowResult:
    binary = shutil.which(invocation.binary, path=env.get("PATH"))
    if binary is None:
        return RowResult(
            language=invocation.language,
            command=(invocation.binary, *invocation.arguments),
            targets=invocation.targets,
            ran=False,
            exit_code=None,
            findings=(),
            outcome="not_verified",
            detail=f"not verified: {invocation.binary} missing",
        )

    # /tmp on POSIX, where the benchmark sandbox mounts a private one; Windows has none.
    scratch = (
        os.path.join(tempfile.gettempdir(), f"cogito-build-gate-{new_id()}")
        if os.name == "nt"
        else f"/tmp/cogito-build-gate-{new_id()}"
    )
    uses_scratch = any("{tempdir}" in argument for argument in invocation.arguments)
    arguments = tuple(argument.replace("{tempdir}", scratch) for argument in invocation.arguments)
    argv = [*wrapper, binary, *arguments]

    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, timeout),
            stdin=subprocess.DEVNULL,
            env=dict(env),
        )
    except subprocess.TimeoutExpired:
        return RowResult(
            language=invocation.language,
            command=tuple(argv),
            targets=invocation.targets,
            ran=False,
            exit_code=None,
            findings=(),
            outcome="not_verified",
            detail=f"not verified: timed out after {timeout:.0f}s",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return RowResult(
            language=invocation.language,
            command=tuple(argv),
            targets=invocation.targets,
            ran=False,
            exit_code=None,
            findings=(),
            outcome="not_verified",
            detail=f"not verified: {type(exc).__name__}: {exc}",
        )
    finally:
        if uses_scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    duration_ms = int((time.monotonic() - started) * 1000)
    findings, ignored = parse(
        completed.stdout, completed.stderr, completed.returncode, invocation, root
    )

    detail = ""
    if completed.returncode != 0 and not findings and not ignored:
        # Nothing matched the row's diagnostic shape and nothing was recognised and
        # dropped either (rows with only dropped findings take the `elif` further down).
        # A config-level tsc error, a cargo manifest failure, a Go toolchain refusal: the
        # process failed and must never read as "passed". See the module docstring.
        first_line = _strip_project_prefix(
            _first_meaningful_line(completed.stderr, completed.stdout), root
        )
        manifest = next(
            (m for m in _MANIFEST_BY_LANGUAGE.get(invocation.language, ()) if m in touched),
            None,
        )
        if manifest is not None:
            # A manifest this session edited caused the failure: report it against the
            # manifest, never as not_verified, however much it reads like an environment
            # problem. See `_MANIFEST_BY_LANGUAGE` for the review (2026-09-24) that found
            # this.
            manifest_message = first_line[:_MESSAGE_CHARS] or f"exited {completed.returncode}"
            findings = [Finding(manifest, 0, manifest_message)]
        elif _looks_like_environment_problem(first_line):
            return RowResult(
                language=invocation.language,
                command=tuple(argv),
                targets=invocation.targets,
                ran=True,
                exit_code=completed.returncode,
                findings=(),
                ignored=ignored,
                outcome="not_verified",
                detail=f"not verified: {first_line[:_MESSAGE_CHARS]}",
                duration_ms=duration_ms,
            )
        else:
            generic_path = invocation.targets[0] if invocation.targets else "(project)"
            generic_message = first_line[:_MESSAGE_CHARS] or f"exited {completed.returncode}"
            findings = [Finding(generic_path, 0, generic_message)]

    if findings:
        outcome: RowOutcome = "failed"
    elif completed.returncode != 0:
        # Everything the row recognised was dropped as environment-only noise, yet the
        # process exited nonzero. "passed" would mask a failure the row cannot explain,
        # and "failed" with no findings to show would mislead too.
        outcome = "not_verified"
        detail = f"not verified: {ignored} diagnostic(s) ignored (environment-only)"
    else:
        outcome = "passed"
    return RowResult(
        language=invocation.language,
        command=tuple(argv),
        targets=invocation.targets,
        ran=True,
        exit_code=completed.returncode,
        findings=tuple(findings),
        ignored=ignored,
        outcome=outcome,
        detail=detail,
        duration_ms=duration_ms,
    )


def run_gate(
    root: Path,
    touched: Sequence[Path],
    *,
    wrapper: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> GateRun:
    """Compile what `touched` says changed (and, for whole-project rows, the project).
    Never runs a test and never leaves a new file in `root`; the module docstring says how
    each row keeps to that and what the `hidden_tests/` exclusion covers.

    ``wrapper`` is the bubblewrap prefix `tools/quality.py`'s `CheckTool` runs its linters
    under (`guard.sandbox.prefix(...)`), built by `cli.py` and passed through the engine.
    ``env`` is the environment the caller's shell runs under (PATH, GOCACHE, CARGO_HOME,
    ...). Only a locale override is added, so diagnostics come back in English, and every
    binary, the Python row's interpreter included, is resolved on ``env["PATH"]``, never
    this process's own.

    ``timeout`` is the budget for the whole round, not one invocation, so several
    invocations cannot each spend it and together overrun it (controller review,
    2026-09-24). Each invocation gets what is left of ``timeout`` when it starts, capped at
    `TIMEOUT_SECONDS`. Once the deadline has passed, the remaining invocations are not
    started and read `not_verified` ("round deadline exceeded").
    """
    sandboxed = bool(wrapper)
    relative_touched = _kept_relative(touched, root, sandboxed=sandboxed)
    touched_set = frozenset(str(p) for p in relative_touched)
    base_env = dict(env if env is not None else os.environ)
    merged_env = {**base_env, "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"}

    deadline = time.monotonic() + max(0.0, timeout)
    rows: list[RowResult] = []
    for row in ROWS:
        for invocation in row.plan(root, relative_touched, sandboxed, merged_env):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                rows.append(
                    RowResult(
                        language=invocation.language,
                        command=(invocation.binary, *invocation.arguments),
                        targets=invocation.targets,
                        ran=False,
                        exit_code=None,
                        findings=(),
                        outcome="not_verified",
                        detail="not verified: round deadline exceeded",
                    )
                )
                continue
            result = _run_invocation(
                invocation, row.parse, root=root, wrapper=wrapper, env=merged_env,
                timeout=min(remaining, TIMEOUT_SECONDS), touched=touched_set,
            )
            if invocation.language in ("cpp", "c"):
                result = _reclassify_missing_headers(result, root=root, touched=touched_set)
            rows.append(result)
    return GateRun(tuple(rows))


def render(findings: Sequence[Finding], *, budget_tokens: int = _RENDER_TOKEN_BUDGET) -> str:
    """Group findings by file with counts, like `tools/quality.render`. Whole findings
    are kept, never cut mid-message, until `MAX_FINDINGS_SHOWN` or `budget_tokens` is
    reached, whichever comes first; then one "... N more finding(s) in M file(s)" line.

    Tokens are counted with the project's counter (`cogito.provider.tokens`), not lines or
    characters: a controller review (2026-09-24) measured 1,679 tokens for 20 findings the
    old caps let through, and `context.budgets.clip` does not clip content of 160 lines or
    fewer whatever its token count. The first file's group is always kept, even if it
    alone exceeds the budget, so the message is never empty.
    """
    by_file: dict[str, list[Finding]] = {}
    for finding in findings:
        by_file.setdefault(finding.path, []).append(finding)
    groups = sorted(by_file.items(), key=lambda kv: -len(kv[1]))

    lines: list[str] = []
    shown = 0
    for index, (path, group) in enumerate(groups):
        group_lines = [f"{path}: {len(group)} finding(s)"]
        group_lines.extend(
            f"  {finding.line}: {finding.message}" for finding in group[:MAX_PER_FILE]
        )
        if len(group) > MAX_PER_FILE:
            group_lines.append(f"  ... {len(group) - MAX_PER_FILE} more in this file")
        group_shown = min(len(group), MAX_PER_FILE)

        candidate = [*lines, *group_lines]
        over_count = shown + group_shown > MAX_FINDINGS_SHOWN
        over_budget = tokens.count("\n".join(candidate)) > budget_tokens
        if lines and (over_count or over_budget):
            remaining_findings = len(findings) - shown
            remaining_files = len(groups) - index
            lines.append(f"... {remaining_findings} more finding(s) in {remaining_files} file(s)")
            break

        lines = candidate
        shown += group_shown
    return "\n".join(lines)
