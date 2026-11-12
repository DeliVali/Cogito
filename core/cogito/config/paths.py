"""Filesystem locations, resolved once per platform.

Nothing else in the codebase may build a path by hand. There are two kinds of location:
user-wide state under the OS convention (config, downloaded runtimes, model weights), and
per-project state under ``<project>/.cogito``, so deleting a project deletes its history too.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir

APP_NAME = "cogito"

#: Per-project directory name. Never tracked by git: it ignores itself (`ensure_project_dir`).
PROJECT_DIRNAME = ".cogito"

#: Written into a new project directory, as pytest (`.pytest_cache`, since 3.8.1), mypy
#: (`.mypy_cache`), ruff (`.ruff_cache`) and Python 3.13's venv mark theirs, so git ignores
#: the whole directory without the project's .gitignore naming it. `git add -A` never
#: commits a session's journal, and `git clean -fd` never removes it, since it skips ignored
#: files unless given -x or -X (checked against git 2.55.0, 2026-10-01).
_SELF_IGNORE = "# Created by Cogito: its record of this project's sessions.\n*\n"


def config_dir() -> Path:
    """Where ``models.toml`` and ``config.toml`` live."""
    return Path(user_config_dir(APP_NAME))


def data_dir() -> Path:
    """Where provisioned runtimes live: Neo4j, the JRE, the Estella venv."""
    return Path(user_data_dir(APP_NAME))


def cache_dir() -> Path:
    """Where downloaded model weights live. Large, and safe to delete."""
    return Path(user_cache_dir(APP_NAME))


def remote_desktop_token() -> Path:
    """The restore token of the desktop portal's RemoteDesktop session
    (`tools.desktop.portal`): with it the person is not asked again each session."""
    return data_dir() / "remote-desktop-token"


def shells_dir() -> Path:
    """The desktop core's record of the agent shells it runs (`tools.shell`), so a core that
    starts after one that died can stop what that one left running."""
    return Path(user_cache_dir(APP_NAME)) / "shells"


def document_cache_dir() -> Path:
    """Documents already converted to text (`tools.document_cache`), keyed by their bytes,
    so the same PDF in two projects is converted once. Per user, owner-only, and safe to
    delete: anything missing is converted again."""
    return cache_dir() / "documents"


def web_state_file() -> Path:
    """How much each web search source has been asked (`tools.web.chain.Budgets`), so a
    restart does not reset a source's day. Per user, and safe to delete."""
    return cache_dir() / "web-search.json"


def web_results_file() -> Path:
    """Recent search results (`tools.web.chain.SearchCache`), so the same search again soon
    asks no source. Holds queries, so it is owner-only. Per user, and safe to delete."""
    return cache_dir() / "web-search-results.json"


def models_file() -> Path:
    return config_dir() / "models.toml"


def settings_file() -> Path:
    return config_dir() / "config.toml"


def decisions_file() -> Path:
    """Saved decisions ("Ahora no", "No"): global, never per project. See decisions.py."""
    return config_dir() / "decisions.toml"


def last_model_file() -> Path:
    """The model last chosen in the window's picker, which `cogito serve` starts with next."""
    return config_dir() / "last-model.toml"


def notices_file() -> Path:
    """Missing-program cards already shown. Each appears once per program, globally."""
    return config_dir() / "notices.toml"


def components_dir() -> Path:
    """Where components Cogito installs itself live, one directory per component."""
    return data_dir() / "components"


def project_dir(project_root: Path) -> Path:
    return project_root / PROJECT_DIRNAME


def ensure_project_dir(project_root: Path) -> Path:
    """Create the project directory, ignoring itself for git, and return it."""
    directory = ensure(project_dir(project_root))
    marker = directory / ".gitignore"
    if not marker.exists():
        marker.write_text(_SELF_IGNORE, encoding="utf-8")
    return directory


def wal_file(project_root: Path, session_id: str) -> Path:
    """The append-only journal for one session. See graph/wal.py."""
    return project_dir(project_root) / "wal" / f"{session_id}.jsonl"


def artifacts_dir(project_root: Path, session_id: str) -> Path:
    """Where clipped tool output is offloaded so the model can read it back on demand."""
    return project_dir(project_root) / "artifacts" / session_id


def undo_journal() -> Path:
    """What `cogito undo` records from the terminal, which belongs to no session's journal."""
    return data_dir() / "undo" / "cli.jsonl"


def undo_dir(project_root: Path) -> Path:
    """Snapshots taken before a file is overwritten, so the write can be undone."""
    return project_dir(project_root) / "undo"


def estella_graph_dir(project_root: Path) -> Path:
    """Cogito Estella keeps its own graph.json and embedding sidecar here."""
    return project_dir(project_root) / "estella"


def ensure(directory: Path) -> Path:
    """Create ``directory`` and return it. The one place mkdir is allowed."""
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def user_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the person's own tools should see: this process's, minus Cogito's
    own virtualenv.

    `cogito` usually runs through `uv run`, which puts its private virtualenv first on PATH
    and sets VIRTUAL_ENV. Anything started for the person (the agent's shell, the probes
    that list the programs on this machine) must not inherit that, or the agent's `python`
    and `pip` are Cogito's own and «Del sistema» lists Cogito's Python instead of the
    machine's (seen 2026-09-30). Only a real virtualenv is stripped
    (`sys.prefix != sys.base_prefix`), so a Cogito installed into a system Python never
    drops `/usr/bin`.
    """
    env = dict(os.environ if environ is None else environ)
    if sys.prefix == sys.base_prefix:
        return env
    env.pop("VIRTUAL_ENV", None)
    own = Path(sys.prefix).resolve()

    def under_own(entry: str) -> bool:
        try:
            return Path(entry).resolve().is_relative_to(own)
        except OSError:
            return False

    env["PATH"] = os.pathsep.join(
        entry for entry in env.get("PATH", "").split(os.pathsep) if entry and not under_own(entry)
    )
    return env
