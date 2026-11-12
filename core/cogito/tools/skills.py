"""Agent Skills: folders of instructions a model loads when a task needs them.

The format is the Agent Skills specification (agentskills.io/specification, read
2026-10-24): a directory holding `SKILL.md`, YAML frontmatter with `name` and `description`,
then Markdown instructions, with optional `scripts/`, `references/` and `assets/`. Loading
follows its client guide (agentskills.io/client-implementation/adding-skills-support, read
the same day):

- discovery scans `.cogito/skills` (Cogito's own), `.agents/skills` and `.claude/skills`,
  in the project and in the home directory, one folder per skill; a project's skill
  overrides one of the person's with the same name;
- only names and descriptions are shown, in the `skill` tool's description (about 50-100
  tokens a skill, their estimate), so the system prompt does not change;
- `skill` returns the instructions without the frontmatter, wrapped in `<skill_content>`,
  with the skill's directory and its files listed but not read;
- an activation already in the window is not sent again; a fold forgets them, so after one
  the instructions can be loaded again.

Frontmatter is read leniently, as the guide asks: skills written for other clients carry
YAML their parsers happened to accept (an unquoted colon in a description). Only a missing
description leaves a skill out, since the model could not know when to use it.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters
from cogito.tools.ranking import Bm25, terms

log = logging.getLogger(__name__)

_SKIPPED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
#: Bounds on a scan, the guide's suggestion (depth 4-6, 2000 directories).
_MAX_DEPTH = 4
_MAX_DIRS = 2000
#: Files listed with an activation, never read.
_MAX_LISTED = 60
_DESCRIPTION_CHARS = 1024


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    location: Path

    @property
    def directory(self) -> Path:
        return self.location.parent


#: Skills that ship with Cogito (how to add an MCP server or a skill to it). Any skill of
#: the same name the person or a project installs replaces one of these.
BUILTIN = Path(__file__).parent / "builtin_skills"


def skill_roots(project_root: Path, home: Path) -> list[Path]:
    """Where skills live, each in its own folder. Cogito's own place is `.cogito/skills`,
    the person's (`~/.cogito/skills`) and the project's; `.agents/skills` and
    `.claude/skills` are read too, so skills installed for other agents work here. Later
    roots win a name: the project over the person, Cogito's own folder over the shared
    ones, and any of them over a built-in."""
    return [
        BUILTIN,
        home / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".cogito" / "skills",
        project_root / ".claude" / "skills",
        project_root / ".agents" / "skills",
        project_root / ".cogito" / "skills",
    ]


def discover(roots: Iterable[Path]) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    for root in roots:
        for location in _skill_files(root):
            skill = load(location)
            if skill is None:
                continue
            if skill.name in found:
                log.info("skill %s at %s replaces %s", skill.name, location,
                         found[skill.name].location)
            found[skill.name] = skill
    return dict(sorted(found.items()))


def _skill_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for seen, (current, dirs, files) in enumerate(os.walk(root, followlinks=True)):
        if seen >= _MAX_DIRS:
            return
        depth = len(Path(current).relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS and depth < _MAX_DEPTH)
        if "SKILL.md" in files and Path(current) != root:
            yield Path(current) / "SKILL.md"
            dirs[:] = []  # a skill's own folders are its resources, not more skills


def load(location: Path) -> Skill | None:
    try:
        text = location.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields, _ = split_frontmatter(text)
    description = " ".join(str(fields.get("description") or "").split())
    if not description:
        log.warning("skill at %s has no description; left out", location)
        return None
    name = str(fields.get("name") or location.parent.name).strip()
    return Skill(name=name, description=description[:_DESCRIPTION_CHARS], location=location)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The top-level scalar fields of the frontmatter, and the body after it.

    Not a YAML parser: top-level `key: value` lines, quoted or not, and `>`/`|` block
    scalars. Nested maps (`metadata:`) are skipped. A value with a colon in it is kept
    whole, which a strict YAML parser would reject."""
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text.strip()
    fields: dict[str, Any] = {}
    i = 1
    while i < end:
        line = lines[i]
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        i += 1
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            block = []
            while i < end and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                block.append(lines[i].strip())
                i += 1
            joiner = "\n" if value.startswith("|") else " "
            fields[key] = joiner.join(block).strip()
            continue
        if not value:
            while i < end and lines[i].startswith((" ", "\t")):
                i += 1
            continue
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("''", "'")
        fields[key] = value
    return fields, "\n".join(lines[end + 1 :]).strip()


def _resources(directory: Path) -> list[str]:
    listed: list[str] = []
    for current, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS)
        for name in sorted(files):
            path = Path(current) / name
            if path.name == "SKILL.md" and path.parent == directory:
                continue
            listed.append(path.relative_to(directory).as_posix())
            if len(listed) > _MAX_LISTED:
                return listed
    return listed


def _first(description: str, limit: int = 200) -> str:
    sentence = re.split(r"(?<=[.!?])\s", description, maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[:limit].rstrip() + " …"


class SkillTool:
    """`skill`: load one skill's instructions by name."""

    def __init__(
        self,
        skills: Mapping[str, Skill],
        budget_tokens: int = 8000,
        variables: Mapping[str, str] | None = None,
        floor: float = 4.5,
        margin: float = 2.0,
    ) -> None:
        self._skills = dict(skills)
        #: `matching`'s two numbers; see there.
        self._floor = floor
        self._margin = margin
        #: `${NAME}` in a built-in skill, filled in when it loads: the paths it names
        #: differ per machine. Other skills are returned as written.
        self._variables = dict(variables or {})
        self._loaded: set[str] = set()
        self._index: Any = None
        # The first sentence of each description: the whole of fourteen installed skills took
        # about 2,600 tokens on every step, their first sentences about a quarter of that.
        catalog = "\n".join(f"- {s.name}: {_first(s.description)}" for s in self._skills.values())
        self.spec = ToolSpec(
            name="skill",
            description=(
                "Load a skill: instructions for a kind of task. When the task matches a "
                "skill below, load it before you start, then follow it. Paths in a skill are "
                "relative to its directory.\n" + catalog
            ),
            parameters=parameters(
                {"name": {"type": "string", "enum": list(self._skills)}}, ("name",)
            ),
            aliases={"name": ("skill", "skill_name", "id")},
            budget_tokens=budget_tokens,
            summary="load a skill's instructions",
            # Instructions read from a file, loaded again after a fold (`forget`); they are
            # not evidence of anything that happened.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.ACKNOWLEDGEMENT,
                shape=PayloadShape.BULK,
            ),
        )

    def matching(self, text: str) -> str | None:
        """The instructions of the one skill the person's message clearly matches, as a
        harness note, or None. The model was handed the catalog and, asked to install an MCP
        server, ran it in the shell instead of loading the skill that says how (QA
        2026-10-24, qwen3.5-9b); section 1b found the same of `recall`. So the harness
        loads it, on BM25 over names and descriptions: the best score at least 4.5 and
        twice the next. Chosen on 25 messages over the fifteen skills on the author's
        machine: it loads for all five that name an MCP install or reverse engineering and
        for none of fifteen everyday coding requests ("write a note" loaded `docs` at 3.0,
        before stopwords were dropped). BM25 scores grow with the catalog, so on a much
        smaller or larger one the floor means less. A hypothesis, not a measurement."""
        if self._index is None:
            self._index = Bm25(
                [terms(f"{s.name} {s.name} {s.description}") for s in self._skills.values()]
            )
        scores = sorted(
            zip(self._index.scores(terms(text)), self._skills, strict=True), reverse=True
        )
        if not scores or scores[0][0] < self._floor:
            return None
        if len(scores) > 1 and scores[0][0] < self._margin * scores[1][0]:
            return None
        name = scores[0][1]
        if name in self._loaded:
            return None
        loaded = self.run({"name": name}, None)  # type: ignore[arg-type]
        if loaded.is_error:
            return None
        return (
            f"[harness: the person's message matches the {name} skill, so its instructions "
            "follow; not a message from the person. Follow them where they apply.]\n"
            + loaded.content
        )

    def activate(self, names: Iterable[str]) -> str | None:
        """APP-ONLY. The instructions of the skills the person activated for this message, as
        a harness note, or None. Unlike `matching`, it loads each named skill outright,
        bypassing BM25: the person picked it in the composer, so it is wanted regardless of
        score. A skill already in the window (`_loaded`) is skipped, so one is never sent
        twice, and an unknown or unreadable name is passed over rather than failing the turn.
        `chat` wires no `activate` call and the picker never reaches it, so every benchmark
        request is byte-identical to one without the feature."""
        parts: list[str] = []
        for name in names:
            if not isinstance(name, str) or name in self._loaded or name not in self._skills:
                continue
            loaded = self.run({"name": name}, None)  # type: ignore[arg-type]
            if loaded.is_error:
                continue
            parts.append(
                f"[harness: the person activated the {name} skill for this message, so its "
                "instructions follow; not a message from the person. Follow them where they "
                "apply.]\n" + loaded.content
            )
        return "\n\n".join(parts) if parts else None

    def forget(self) -> None:
        """After a fold the instructions are no longer in the window."""
        self._loaded.clear()

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        name = str(arguments.get("name") or "").strip()
        skill = self._skills.get(name)
        if skill is None:
            return ToolResult.error(
                f"No skill named {name!r}. Skills: {', '.join(self._skills)}.", "unknown_skill"
            )
        if name in self._loaded:
            return ToolResult(
                content=f"The {name} skill is already loaded above; follow it.",
                metadata={"skill": name, "repeat": True},
            )
        try:
            text = skill.location.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.error(
                f"Could not read {skill.location}: {exc}. Continue without it.", "skill_unreadable"
            )
        _, body = split_frontmatter(text)
        if skill.location.is_relative_to(BUILTIN):
            for key, value in self._variables.items():
                body = body.replace("${" + key + "}", value)
        self._loaded.add(name)
        files = _resources(skill.directory)
        listing = "\n".join(f"  {path}" for path in files[:_MAX_LISTED])
        if len(files) > _MAX_LISTED:
            listing += "\n  … more files not listed"
        parts = [
            f'<skill_content name="{name}">',
            body,
            "",
            f"Skill directory: {skill.directory}",
            "Relative paths in this skill are relative to the skill directory.",
        ]
        if files:
            parts += ["Files (read them when the instructions point to one):", listing]
        parts.append("</skill_content>")
        return ToolResult(
            content="\n".join(parts),
            metadata={"skill": name},
        )


def skills_for(
    project_root: Path, home: Path, *, switched_off: Iterable[str] = ()
) -> dict[str, Skill]:
    """The skills a session offers. One that names an MCP server the person switched off
    (``switched_off``, `config.settings.McpServerSettings.enabled`) is left out: it is
    instructions for that server's tools, which the session does not have, and in its
    catalog it reads as an invitation to do that work by other means. Found on 2026-10-31:
    with REA off, `reverse-engineer-anything` ("... with REA") was still in `skill`'s
    catalog and in reach of the automatic load. A server is named when its name is a whole
    word of the skill's name or description, in any case."""
    found = discover(skill_roots(project_root, home))
    words = [re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE)
             for name in switched_off if name.strip()]
    if not words:
        return found
    return {
        name: skill
        for name, skill in found.items()
        if not any(word.search(f"{skill.name} {skill.description}") for word in words)
    }
