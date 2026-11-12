"""Checking that code is good, not only that it runs.

One tool, not one per linter. Every tool definition sits in the context window on every
step (170 to 480 tokens each, measured here), so exposing hadolint, kube-linter, checkov,
eslint, ruff and the rest separately would cost more per step than the whole current tool
set. It would also leave the model to work out which linter applies to which file, which
is the harness's job.

`check` looks at what is in the project, runs the checkers that apply and are installed,
and reports findings grouped by file, as grep does.

Two rules:

- A missing checker is reported, never silently skipped. A gate that passes because
  nothing ran is worse than no gate.
- Findings are grouped and capped. A linter on a large repository prints thousands of
  lines, and streaming them into the context defeats the point of running it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cogito.guard.policy import outside, real_path
from cogito.provider.base import (
    PayloadShape,
    Provenance,
    Reproducibility,
    Retention,
    ToolSpec,
)
from cogito.tools.base import ToolContext, ToolResult, parameters

TIMEOUT_SECONDS = 180
MAX_FINDINGS_SHOWN = 40
MAX_PER_FILE = 5

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str
    severity: Severity = "warning"


@dataclass(frozen=True, slots=True)
class Checker:
    """One external tool, and how to tell whether it has anything to say here."""

    name: str
    binary: str
    #: What it analyses. If nothing in the project matches, it is not applicable.
    patterns: tuple[str, ...]
    #: argv after the binary. ``{paths}`` is replaced by the files to check.
    arguments: tuple[str, ...]
    #: How to read its output.
    output: Literal["ruff-json", "hadolint-json", "trivy-json", "lines"]
    #: What it is for, shown when it is missing so the user knows what they are not getting.
    purpose: str
    #: A non-zero exit means findings, not failure, for most linters.
    findings_exit_code: bool = True
    install: str = ""

    def applicable_files(self, root: Path) -> list[Path]:
        found: list[Path] = []
        for pattern in self.patterns:
            found.extend(
                path
                for path in root.glob(pattern)
                if path.is_file() and not _ignored(path, root)
            )
        return sorted(set(found))

    @property
    def installed(self) -> bool:
        return shutil.which(self.binary) is not None


_IGNORED_PARTS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".cogito", "dist", "build"}
)


def _ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in _IGNORED_PARTS for part in relative.parts)


#: The catalogue. Adding a checker is adding a row, not writing a tool.
CHECKERS: tuple[Checker, ...] = (
    Checker(
        name="ruff",
        binary="ruff",
        patterns=("**/*.py",),
        arguments=("check", "--output-format", "json", "{paths}"),
        output="ruff-json",
        purpose="Python lint and style",
        install="uv tool install ruff",
    ),
    Checker(
        name="hadolint",
        binary="hadolint",
        patterns=("**/Dockerfile", "**/Dockerfile.*", "**/*.dockerfile"),
        arguments=("--format", "json", "{paths}"),
        output="hadolint-json",
        purpose="Dockerfile practice: image size, layer order, running as root",
        install="see github.com/hadolint/hadolint",
    ),
    Checker(
        name="kube-linter",
        binary="kube-linter",
        patterns=("**/*.yaml", "**/*.yml"),
        arguments=("lint", "--format", "json", "{paths}"),
        output="lines",
        purpose="Kubernetes manifests: resource limits, restart policy, network",
        install="see github.com/stackrox/kube-linter",
    ),
    Checker(
        name="checkov",
        binary="checkov",
        patterns=("**/*.tf", "**/*.yaml", "**/Dockerfile"),
        arguments=("-d", ".", "--compact", "--quiet"),
        output="lines",
        purpose="Infrastructure as code: Terraform, Kubernetes, Docker policy",
        install="uv tool install checkov",
    ),
    Checker(
        name="trivy",
        binary="trivy",
        patterns=("**/*",),
        arguments=("fs", "--scanners", "vuln,secret,misconfig", "--format", "json", "."),
        output="trivy-json",
        purpose="known vulnerabilities, exposed secrets, misconfiguration",
        install="see aquasecurity.github.io/trivy",
    ),
    Checker(
        name="eslint",
        binary="eslint",
        patterns=("**/*.js", "**/*.ts", "**/*.tsx", "**/*.jsx"),
        arguments=("--format", "json", "{paths}"),
        output="lines",
        purpose="JavaScript and TypeScript lint",
        install="npm install -g eslint",
    ),
    Checker(
        name="mypy",
        binary="mypy",
        patterns=("**/*.py",),
        arguments=("--no-error-summary", "--no-color-output", "{paths}"),
        output="lines",
        purpose="Python type checking",
        install="uv tool install mypy",
    ),
)


def _parse_ruff(stdout: str) -> list[Finding]:
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        Finding(
            path=item.get("filename", ""),
            line=int((item.get("location") or {}).get("row", 0)),
            rule=item.get("code") or "",
            message=item.get("message", ""),
            severity="warning",
        )
        for item in payload
    ]


def _parse_hadolint(stdout: str) -> list[Finding]:
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        Finding(
            path=item.get("file", ""),
            line=int(item.get("line", 0)),
            rule=item.get("code", ""),
            message=item.get("message", ""),
            severity="error" if item.get("level") == "error" else "warning",
        )
        for item in payload
    ]


def _parse_trivy(stdout: str) -> list[Finding]:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    findings: list[Finding] = []
    for result in payload.get("Results", []) or []:
        target = result.get("Target", "")
        for item in result.get("Vulnerabilities", []) or []:
            findings.append(
                Finding(target, 0, item.get("VulnerabilityID", ""),
                        f"{item.get('PkgName','')}: {item.get('Title','')}", "error")
            )
        for item in result.get("Secrets", []) or []:
            findings.append(
                Finding(target, int(item.get("StartLine", 0)), item.get("RuleID", ""),
                        f"exposed secret: {item.get('Title','')}", "error")
            )
        for item in result.get("Misconfigurations", []) or []:
            findings.append(
                Finding(target, 0, item.get("ID", ""), item.get("Title", ""), "warning")
            )
    return findings


_LINE = re.compile(r"^(?P<path>[^:\s]+):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.+)$")


def _parse_lines(stdout: str) -> list[Finding]:
    """The common `path:line: message` shape, for tools without usable JSON."""
    findings: list[Finding] = []
    for raw in stdout.splitlines():
        if match := _LINE.match(raw.strip()):
            findings.append(
                Finding(match["path"], int(match["line"]), "", match["message"].strip())
            )
    return findings


_PARSERS = {
    "ruff-json": _parse_ruff,
    "hadolint-json": _parse_hadolint,
    "trivy-json": _parse_trivy,
    "lines": _parse_lines,
}


@dataclass(frozen=True, slots=True)
class Outcome:
    ran: tuple[str, ...] = ()
    #: Applicable, but the binary is absent. Reported, never treated as clean.
    missing: tuple[tuple[str, str, str], ...] = ()
    not_applicable: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    failed: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: Sandboxed run only: targets whose realpath resolved outside the project (a symlink
    #: out). Dropped and reported here, not skipped silently.
    outside_sandbox: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.findings and not self.failed


def _drop_outside(targets: list[Path], root: Path, skipped_outside: list[str]) -> list[Path]:
    """Sandboxed mode only: a target whose realpath leaves the project is dropped.

    ``root.glob`` and an explicit path both follow symlinks. A linter told to read a
    symlink that resolves outside the project would read whatever it points at, the leak
    a sandboxed run promises to prevent.
    """
    kept: list[Path] = []
    for target in targets:
        resolved = Path(os.path.realpath(target))
        if outside(resolved, [root]):
            skipped_outside.append(str(target))
            continue
        kept.append(target)
    return kept


def run_checkers(
    root: Path,
    paths: Sequence[Path] | None = None,
    only: Sequence[str] = (),
    *,
    sandboxed: bool = False,
    wrapper: Sequence[str] = (),
) -> Outcome:
    """Run every checker that applies to this project and is installed.

    ``sandboxed`` drops any target (explicit or found by ``applicable_files``) whose
    realpath resolves outside ``root``. ``wrapper`` (a bubblewrap prefix from
    ``cogito.guard.sandbox.prefix``) confines the checker subprocess the same way the
    shell tool is confined. Both do nothing on an unsandboxed run.
    """
    ran: list[str] = []
    missing: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    findings: list[Finding] = []
    failed: list[tuple[str, str]] = []
    outside_sandbox: list[str] = []

    for checker in CHECKERS:
        if only and checker.name not in only:
            continue
        targets = [p for p in (paths or []) if any(p.match(g) for g in checker.patterns)]
        if not targets:
            targets = checker.applicable_files(root)
        if sandboxed:
            targets = _drop_outside(targets, root, outside_sandbox)
        if not targets:
            skipped.append(checker.name)
            continue
        if not checker.installed:
            missing.append((checker.name, checker.purpose, checker.install))
            continue

        argv: list[str] = [*wrapper, checker.binary]
        for argument in checker.arguments:
            if argument == "{paths}":
                argv.extend(str(t) for t in targets[:400])
            else:
                argv.append(argument)
        try:
            completed = subprocess.run(
                argv, cwd=root, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failed.append((checker.name, f"{type(exc).__name__}: {exc}"))
            continue

        ran.append(checker.name)
        found = _PARSERS[checker.output](completed.stdout)
        if not found and completed.returncode not in (0, 1) and completed.stderr:
            failed.append((checker.name, completed.stderr.strip().splitlines()[-1][:160]))
        findings.extend(found)

    return Outcome(
        ran=tuple(ran),
        missing=tuple(missing),
        not_applicable=tuple(skipped),
        findings=tuple(findings),
        failed=tuple(failed),
        outside_sandbox=tuple(sorted(set(outside_sandbox))),
    )


def render(outcome: Outcome, root: Path) -> str:
    """Group by file with counts, the way grep does, and cap what is shown."""
    lines: list[str] = []

    if outcome.findings:
        by_file: dict[str, list[Finding]] = {}
        for finding in outcome.findings:
            try:
                key = str(Path(finding.path).resolve().relative_to(root.resolve()))
            except (ValueError, OSError):
                key = finding.path
            by_file.setdefault(key, []).append(finding)

        shown = 0
        for path, group in sorted(by_file.items(), key=lambda kv: -len(kv[1])):
            if shown >= MAX_FINDINGS_SHOWN:
                remaining = len(outcome.findings) - shown
                lines.append(f"... {remaining} more finding(s) in {len(by_file)} file(s)")
                break
            lines.append(f"{path}: {len(group)} finding(s)")
            for finding in group[:MAX_PER_FILE]:
                rule = f"[{finding.rule}] " if finding.rule else ""
                lines.append(f"  {finding.line}: {rule}{finding.message[:110]}")
                shown += 1
            if len(group) > MAX_PER_FILE:
                lines.append(f"  ... {len(group) - MAX_PER_FILE} more in this file")
    else:
        lines.append("No findings.")

    if outcome.ran:
        lines.append(f"\nRan: {', '.join(outcome.ran)}")
    for name, reason in outcome.failed:
        lines.append(f"! {name} failed to run: {reason}")
    # A gate that passes because nothing ran is worse than no gate.
    for name, purpose, install in outcome.missing:
        lines.append(f"! {name} applies here but is not installed ({purpose}). {install}")
    for target in outcome.outside_sandbox:
        lines.append(f"! skipped (resolves outside the sandboxed project): {target}")
    return "\n".join(lines)


class CheckTool:
    spec = ToolSpec(
        name="check",
        #: Diagnostics for a tree that changes under them.
        provenance=Provenance(
            reproducibility=Reproducibility.MUTABLE,
            retention=Retention.EVIDENCE,
            shape=PayloadShape.BULK,
        ),
        description=(
            "Check code quality. Runs whichever linters and scanners apply to this "
            "project and are installed — Python, Dockerfiles, Kubernetes, Terraform, "
            "JavaScript, dependencies and secrets — and reports findings grouped by file. "
            "Run it after changing code, before saying the work is done."
        ),
        parameters=parameters(
            {
                "paths": {
                    "type": "array",
                    "description": "Files to check. Omit for the whole project.",
                    "items": {"type": "string"},
                },
                "only": {
                    "type": "array",
                    "description": "Limit to named checkers, e.g. ruff.",
                    "items": {"type": "string"},
                },
            }
        ),
        aliases={"paths": ("path", "files", "file", "targets")},
        budget_tokens=1200,
        #: Runs the project's linters, which write caches and can run its own config.
        operates=True,
        summary="run the project's linters and report code quality findings",
    )

    def __init__(self, wrapper: Sequence[str] = ()) -> None:
        #: A bubblewrap prefix for a sandboxed run, empty otherwise. Set by
        #: ``_build_engine`` in cli.py, like the shell's wrapper.
        self._wrapper = tuple(wrapper)

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw = arguments.get("paths") or []
        if isinstance(raw, str):
            raw = [raw]
        # `real_path` is the guard's own normalisation (stripped, resolved against the
        # project root, every symlink followed), the same call the guard hook made to
        # allow this call. Building the path any other way can let a call the guard
        # approved run on a different file.
        paths = [real_path(str(p), context.project_root) for p in raw]
        only = arguments.get("only") or []
        if isinstance(only, str):
            only = [only]

        unknown = [name for name in only if name not in {c.name for c in CHECKERS}]
        if unknown:
            return ToolResult.error(
                f"unknown checker(s): {', '.join(unknown)}. "
                f"Available: {', '.join(c.name for c in CHECKERS)}",
                "unknown_checker",
            )

        sandboxed = bool(self._wrapper)
        outcome = run_checkers(
            context.project_root,
            paths,
            [str(o) for o in only],
            sandboxed=sandboxed,
            wrapper=self._wrapper,
        )
        return ToolResult(
            content=render(outcome, context.project_root),
            # Findings are not a tool failure. Only a checker that could not run is an
            # error the model should react to.
            is_error=bool(outcome.failed),
            error_kind="checker_failed" if outcome.failed else "",
            metadata={
                "findings": len(outcome.findings),
                "ran": ",".join(outcome.ran),
                "missing": ",".join(name for name, _, _ in outcome.missing),
                "outside_sandbox": len(outcome.outside_sandbox),
            },
            files_touched=tuple(str(p) for p in paths[:20]),
        )
