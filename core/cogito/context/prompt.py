"""System prompt assembly, in three tiers.

The order matters. The stable tier is byte-identical between turns, so llama.cpp reuses
its KV prefix and API providers cache it. The volatile tier goes last because anything
after it would invalidate that cache on every step.

  stable   identity, tool guidance, environment facts and, when the caller names it, the
           model answering (`ModelIdentity`)
  context  project documents, fenced as data
  volatile timestamp, branch, todo list, remaining budget
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cogito.context.inject_scan import fence
from cogito.provider.base import ToolSpec

#: Read from the project root, in order. The first two that exist are used.
PROJECT_DOCS = ("CLAUDE.md", "AGENTS.md", ".cursorrules")
_MAX_DOC_CHARS = 12_000

_IDENTITY = """\
You are Cogito, a coding agent working directly on this machine. You have a real shell, \
real files, and a real browser; nothing you do is a simulation.

How to work here:
- Find out before you act. Read the code that matters rather than guessing at it.
- Prefer the structured tools over shelling out for the same thing: `read` and `edit` beat \
`cat` and `sed`, `grep` and `glob` beat `find | xargs`. They cost fewer tokens and they \
tell you precisely what went wrong.
- A tool error tells you what to do next. Read it and change approach; repeating the same \
call unchanged will stop the run.
- Say what you did, not what you are about to do. When the work is finished, stop calling \
tools and answer.
"""


#: The part of the identity line that says what the agent has. `chat` and every benchmark
#: task send it as it is; the desktop app builds it from what is registered (`abilities`).
_ABILITIES = "a real shell, real files, and a real browser"


def abilities(*, web: bool) -> str:
    """What the identity line says the agent can do, true of what is registered. There is
    no browser: with the web tools it can search the web and read pages as text, and without
    them it has a shell and files. The line the benchmark sends says "a real browser" and is
    not changed there (a change to the stable prompt of every task is measured first,
    CONTRIBUTING section 7); the design's section 10 has the reasoning."""
    if web:
        return "a real shell, real files, and the web (search and read pages)"
    return "a real shell and real files"


#: The last line of the identity. `chat` and every benchmark task send it as it is; the
#: app asks instead for one short line before each call (`_PREAMBLES`).
_REPORT_AFTER = (
    "- Say what you did, not what you are about to do. When the work is finished, stop calling "
    "tools and answer.\n"
)

#: APP-ONLY: the person reads the turn as it runs and asked to see what each call is for,
#: in a few words (2026-11-01: "muy corto y breve ... voy a ejecutar esto para ver qué hay
#: dentro de este archivo"). Codex asks the same of its model: "Before making tool calls,
#: send a brief preamble", "8-12 words for quick updates", related actions under one, and
#: "connect the dots with what's been done so far" (codex-rs/protocol/src/prompts/
#: base_instructions/default.md, "Preamble messages", read 2026-11-01). Codex leaves out
#: trivial reads; the person asked for every call. Building on the last result also keeps
#: the line from repeating word for word, which `RunState.note_reply` counts as a loop.
_PREAMBLES = """\
- Before each tool call, write one short line in the person's language saying what it \
does and why, building on what the last result showed, then make the call in the same \
reply (for example: "Leo config.py para ver qué puerto usa."). One line of at most 12 words; \
related calls share one line. When the work is finished, stop calling tools and answer.
"""


def _identity(spoken: str | None, *, preambles: bool = False) -> str:
    text = _IDENTITY
    if preambles:
        assert text.endswith(_REPORT_AFTER)
        text = text[: -len(_REPORT_AFTER)] + _PREAMBLES
    if spoken is None:
        return text
    assert _ABILITIES in text
    return text.replace(_ABILITIES, spoken, 1)


_TOOL_PREAMBLE = """\
You have tools. To use one, emit a call on its own line, in exactly this form:

<tool_call>{"name": "TOOL", "arguments": {"ARG": "VALUE"}}</tool_call>

Emit nothing after the closing tag; the result comes back in the next message. Available \
tools:"""


def render_tools(specs: Sequence[ToolSpec]) -> str:
    """Describe tools in the prompt, for endpoints that accept no tool definitions.

    Some servers refuse a `tools` parameter, and base models never had one. The call format
    asked for here, `<tool_call>`, is the one text form the loop reads (`repair.EXTRACTORS`).
    """
    if not specs:
        return ""
    lines = [_TOOL_PREAMBLE]
    for spec in specs:
        properties = spec.parameters.get("properties", {})
        required = set(spec.parameters.get("required", ()))
        arguments = ", ".join(
            f"{name}: {body.get('type', 'any')}" + ("" if name in required else " (optional)")
            for name, body in properties.items()
        )
        lines.append(f"\n{spec.name}({arguments})\n  {spec.description}")
        if required:
            example = {name: f"<{name}>" for name in properties if name in required}
            lines.append(
                "  example: <tool_call>"
                + json.dumps({"name": spec.name, "arguments": example}, ensure_ascii=False)
                + "</tool_call>"
            )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Which model answers, as the person sees it in the app, so the model can say it.

    Asked "which model are you?", a model told only "You are Cogito" said it was not a
    language model like GPT or Claude at all (the desktop app, 2026-09-29). Claude Code's
    system prompt states "You are powered by the model named <name>. The exact model ID is
    <id>", and OpenCode's `environment()` (packages/opencode/src/session/system.ts, read
    2026-09-30) says the same with the provider in the id. This is that sentence plus who
    serves the model and its context window, facts this harness already holds.

    `name` is the catalog entry's display label, `model` the name the server knows it by,
    `service` the service's own name ("llama.cpp", "Ollama", "Claude") and `local` whether
    it runs on this machine.
    """

    name: str
    model: str
    service: str
    local: bool


def identity(model: ModelIdentity, *, context_window: int | None = None) -> str:
    served = f"served locally by {model.service}" if model.local else f"served by the {model.service} API"
    line = (
        f"You are powered by the model named {model.name}. "
        f"The exact model ID is {model.model}, {served}."
    )
    if context_window:
        line += f" Its context window is {context_window:,} tokens."
    return line


@dataclass(frozen=True, slots=True)
class SystemFacts:
    """What the desktop app knows about the machine, stated in the environment line so the
    model does not guess the distribution (a model on CachyOS ran `sudo apt update`, seen
    2026-10-12). Built by `cli` from `guard.platform.facts`; None everywhere else."""

    #: "CachyOS", "macOS 15.1", "Windows 11".
    os_label: str
    #: The system package managers found on PATH, in a fixed order.
    managers: tuple[str, ...] = ()
    #: The AUR helpers the installer can run (paru, yay) found on PATH.
    aur_helpers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    stable: str
    tools: str = ""
    context: str = ""
    volatile: str = ""
    #: Anything the injection scan flagged while reading project documents.
    findings: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """Every tier in one string. For inspection; the loop does not send this."""
        parts = (self.stable, self.tools, self.context, self.volatile)
        return "\n\n".join(part for part in parts if part)

    def render_static(self) -> str:
        """The tiers that stay byte-identical from one step to the next.

        Only this is the system message. The system message is message 0, so a timestamp
        in it that changes every step makes every step a cache miss for the whole
        transcript behind it. Measured on this machine with the local model, the same token
        count took 9.13s per step with the volatile tier in the system message and 0.27s
        without (34x).
        """
        parts = (self.stable, self.tools, self.context)
        return "\n\n".join(part for part in parts if part)

    def render_volatile(self) -> str:
        """The per-step status, labelled so the model does not read it as the user."""
        if not self.volatile:
            return ""
        return f"[Harness status for this step, not a message from the user]\n{self.volatile}"


_STATUS_TOKENS = re.compile(r"Remaining budget: .*?(\d{1,3}(?:,\d{3})+|\d{4,}) tokens")
_STATUS_NOW = re.compile(r"^Now: (\S+)$", re.MULTILINE)


def strip_status_echo(text: str, status: str) -> str:
    """``text`` without the paragraphs that repeat the step's status (`render_volatile`).

    The status is a message of its own at the end of the conversation, labelled as not from
    the person, and a model can still answer it: in the real app (2026-10-16) Claude Haiku
    4.5 opened a reply with «He recibido el estado del harness. Tengo disponibles 59 pasos y
    aproximadamente 395,074 tokens». Such a paragraph is found by the status's own figures,
    the remaining tokens (however the digits are grouped) or the clock, which only the
    harness wrote. A reply that would be left empty is kept whole: the person may have asked
    what is left."""
    if not status or not text:
        return text
    needles = _status_needles(status)
    if not needles:
        return text
    kept, paragraphs = _echo_split(text, needles)
    if len(kept) == len(paragraphs) or not any(p.strip() for p in kept):
        return text
    return "\n\n".join(kept).strip()


def _status_needles(status: str) -> list[re.Pattern[str]]:
    """The status's own figures as patterns: the remaining tokens however grouped, and the
    clock."""
    needles: list[re.Pattern[str]] = []
    if not status:
        return needles
    if found := _STATUS_TOKENS.search(status):
        digits = found.group(1).replace(",", "")
        groups: list[str] = []
        head = len(digits) % 3 or 3
        groups.append(digits[:head])
        groups.extend(digits[i : i + 3] for i in range(head, len(digits), 3))
        needles.append(re.compile(r"(?<!\d)" + r"[,.\s\u202f\u00a0]?".join(groups) + r"(?!\d)"))
    if found := _STATUS_NOW.search(status):
        needles.append(re.compile(re.escape(found.group(1))))
    return needles


def is_status_echo(text: str, status: str) -> bool:
    """Whether every paragraph of ``text`` repeats the step's status (`strip_status_echo`):
    a reply that answered the status line and nothing else."""
    needles = _status_needles(status)
    if not needles or not text.strip():
        return False
    kept, _ = _echo_split(text, needles)
    return not any(p.strip() for p in kept)


def _echo_split(text: str, needles: list[re.Pattern[str]]) -> tuple[list[str], list[str]]:
    paragraphs = re.split(r"\n[ \t]*\n", text)
    return [p for p in paragraphs if not any(n.search(p) for n in needles)], paragraphs


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_branch(root: Path) -> str:
    """The branch name, including in a repository with no commits yet.

    There `rev-parse --abbrev-ref HEAD` answers a literal "HEAD", which reads as a detached
    head and is wrong; `symbolic-ref` knows the branch's name.
    """
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        return branch
    return _git(root, "symbolic-ref", "--short", "HEAD")


def environment(root: Path, system: SystemFacts | None = None) -> str:
    if system is None:
        machine = f"{platform.system()} {platform.release()}"
    else:
        machine = f"{system.os_label} ({platform.system()} {platform.release()})"
        if system.managers:
            noun = "package manager" if len(system.managers) == 1 else "package managers"
            machine += f", {noun} {', '.join(system.managers)}"
            # Stated as a fact: a model told nothing said "restricted environment" and sent
            # the person to type the install (seen 2026-10-12).
            aur = "pacman" in system.managers and bool(system.aur_helpers)
            what = "installs, upgrades and AUR installs" if aur else "installs and upgrades"
            machine += f" ({what} run after the person approves them; no sudo needed)"
    return (
        f"Environment: {machine}, shell bash, "
        f"python {platform.python_version()}\n"
        f"Working directory: {root}"
    )


def project_context(root: Path) -> tuple[str, tuple[str, ...]]:
    """Fence whichever project documents exist. Returns the text and any findings."""
    blocks: list[str] = []
    findings: list[str] = []
    for name in PROJECT_DOCS:
        candidate = root / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")[:_MAX_DOC_CHARS]
        except OSError:
            continue
        scanned = fence(text, source=name)
        blocks.append(scanned.text)
        findings.extend(scanned.findings)
        if len(blocks) == 2:
            break
    return "\n\n".join(blocks), tuple(findings)


def volatile(
    *,
    root: Path,
    todos: str = "",
    steps_left: int | None = None,
    tokens_left: int | None = None,
    current_time: datetime | None = None,
    clock: bool = True,
) -> str:
    """The per-step status. ``clock`` False leaves out the time and the branch, which
    `build` then puts in the system message (`LoopSettings.status_turn`)."""
    parts = []
    if clock:
        now = current_time or datetime.now(UTC)
        parts.append(f"Now: {now.astimezone(UTC).isoformat(timespec='seconds')}")
        if branch := _git_branch(root):
            parts.append(f"Git branch: {branch}")
    if todos:
        parts.append(f"Task list:\n{todos}")
    budget = []
    if steps_left is not None:
        budget.append(f"{steps_left} steps")
    if tokens_left is not None:
        budget.append(f"{tokens_left:,} tokens")
    if budget:
        parts.append("Remaining budget: " + ", ".join(budget))
    return "\n".join(parts)


def build(
    root: Path,
    *,
    todos: str = "",
    steps_left: int | None = None,
    tokens_left: int | None = None,
    tools: Sequence[ToolSpec] = (),
    current_time: datetime | None = None,
    model: ModelIdentity | None = None,
    context_window: int | None = None,
    abilities: str | None = None,
    system: SystemFacts | None = None,
    preambles: bool = False,
    status_turn: bool = True,
) -> SystemPrompt:
    """`model`, when given, adds `identity`'s line to the stable tier; without it there is
    no identity line. `preambles` asks for a short line before each call (the app).

    `status_turn` False (the app) puts the date and the branch in the stable tier and
    leaves them out of the status, so a step with no counts and no task list sends no
    status message. The date changes once a day and the branch when the model changes it,
    so the system message stays the same from one step to the next."""
    context, findings = project_context(root)
    stable = f"{_identity(abilities, preambles=preambles)}\n{environment(root, system)}"
    if not status_turn:
        now = current_time or datetime.now(UTC)
        stable += f"\nToday: {now.astimezone().date().isoformat()}"
        if branch := _git_branch(root):
            stable += f"\nGit branch: {branch}"
    if model is not None:
        stable += "\n" + identity(model, context_window=context_window)
    return SystemPrompt(
        stable=stable,
        tools=render_tools(tools),
        context=context,
        volatile=volatile(
            root=root,
            todos=todos,
            steps_left=steps_left,
            tokens_left=tokens_left,
            current_time=current_time,
            clock=status_turn,
        ),
        findings=findings,
    )
