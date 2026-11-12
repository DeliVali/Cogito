"""Cogito Estella, installed by the app for every person who has not set one up.

Without it, `ask`, the passages `web_fetch` returns and `ask_document` all fall back to
lines matched by words. Until 0.19 Estella needed torch and a graph model (several GB), so
each person installed it by hand. Cogito Estella 0.19.0 (2026-10-14, its CHANGELOG) moved
the graph model to the `[full]` extra: the base install runs the lite index the app uses
(`[estella] mode`), with no torch. 0.20.0 (2026-10-15, its CHANGELOG) added a multilingual
sentence vector beside each lite sentence (multilingual-e5-small, ONNX, int8), so a
question in Spanish finds its passage in an English page: on XQuAD, their measurement,
43 % -> 95.7 % of 1,190 questions. 0.20.1 marks a reply only the vectors found
under 0.3, so the recall ladder escalates it. Measured here: 135 MB of wheels, 505 MB installed,
10.5 s; the model is fetched once at install (a 470 MB float file, quantized to 118 MB kept)
so a session never waits on it.

The download is the release's own wheel from PyPI, pinned by digest, kept as downloaded;
`finish_install` builds a private venv from the hashed lock (`setup/estella_lock.py`) plus
that wheel, so pip checks every file and compiles nothing. A configured
`[estella] command` always wins: this is only for a person who has none.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

from cogito.config import paths
from cogito.setup.estella_full_lock import LOCK as FULL_LOCK
from cogito.setup.estella_lock import LOCK
from cogito.setup.install import InstallFailed

COMPONENT_ID = "estella"
#: The graph model over the same venv (`finish_full_install`), installed for a large model.
FULL_COMPONENT_ID = "estella-full"
#: Where `FULL_LOCK`'s torch comes from: PyTorch's CPU builds, then PyPI for the rest.
FULL_INDEXES = ("https://pypi.org/simple", "https://download.pytorch.org/whl/cpu")
#: The extra's wheels (221 MB measured on 2026-10-15) and the weights Estella downloads from
#: Hugging Face on its first full start (464 MB, plus a 13 MB spaCy model), rounded up.
FULL_BYTES = 750_000_000

VERSION = "0.20.1"
WHEEL = f"cogito_estella-{VERSION}-py3-none-any.whl"
WHEEL_URL = (
    "https://files.pythonhosted.org/packages/06/28/"
    "90c27bee178736e8d2c00151c05e204247d545d9528a259ded0a9b8eb5b0/" + WHEEL
)
WHEEL_SHA256 = "5ba1b2e8fa468f949c906edc566a3900ae3a5d705b8fb7c9dd3aa38376d81514"
WHEEL_BYTES = 133_350
#: What the venv step downloads (80 wheels, 135 MB measured on 2026-10-15) plus the float
#: vector model it quantizes (470 MB, not kept), rounded up.
WHEELS_BYTES = 620_000_000
#: Where Cogito Estella 0.20 keeps its multilingual vector model (`cogito_estella.mcp.
#: vectors.cache_dir`); the app points it inside the component, so removing it removes all.
CACHE_VARIABLE = "COGITO_CACHE"


def home(components_dir: Path | None = None) -> Path:
    return (components_dir or paths.components_dir()) / COMPONENT_ID


def wheel_path(components_dir: Path | None = None) -> Path:
    return home(components_dir) / VERSION / WHEEL


def venv_dir(components_dir: Path | None = None) -> Path:
    return home(components_dir) / "venv"


def ready_marker(components_dir: Path | None = None) -> Path:
    """Named by version, so an install of an older one is not ready and the app installs
    this one over it."""
    return home(components_dir) / f"ready-{VERSION}.json"


def vector_cache(components_dir: Path | None = None) -> Path:
    return home(components_dir) / "cache"


def command(components_dir: Path | None = None) -> tuple[str, ...]:
    """The argv that starts the installed Estella, or () when it is not installed."""
    if not ready_marker(components_dir).is_file():
        return ()
    venv = venv_dir(components_dir)
    script = venv / ("Scripts/cogito-mcp.exe" if os.name == "nt" else "bin/cogito-mcp")
    return (str(script),) if script.is_file() else ()


def finish_install(
    components_dir: Path | None = None,
    *,
    python: str | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    today: Callable[[], date] = date.today,
    timeout: float = 1200.0,
) -> None:
    """Build the venv from the hashed lock and the pinned wheel. Raises `InstallFailed`;
    the caller removes whatever was written."""
    from cogito.setup.searxng import _fail

    base = home(components_dir)
    wheel = wheel_path(components_dir)
    if not wheel.is_file():
        raise InstallFailed("other", "the Cogito Estella wheel is not downloaded")
    venv = venv_dir(components_dir)
    shutil.rmtree(venv, ignore_errors=True)

    def step(name: str, argv: list[str]) -> None:
        try:
            result = run(argv, capture_output=True, text=True, timeout=timeout,
                         stdin=subprocess.DEVNULL, env={**os.environ, "PIP_NO_INPUT": "1"})
        except subprocess.TimeoutExpired as exc:
            raise InstallFailed("offline", f"{name} timed out after {int(timeout)} s") from exc
        except OSError as exc:
            raise InstallFailed("other", f"{name} could not run: {exc}") from exc
        if result.returncode != 0:
            raise _fail(name, result)

    step("creating the virtual environment", [python or sys.executable, "-m", "venv", str(venv)])
    lock = base / "requirements.lock"
    lock.write_text(f"{LOCK}{wheel.as_posix()} --hash=sha256:{WHEEL_SHA256}\n", encoding="utf-8")
    py = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    step(
        "installing Cogito Estella",
        [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
         "--only-binary=:all:", "--require-hashes", "--no-deps", "-r", str(lock)],
    )
    step("checking Cogito Estella imports", [py, "-c", "import cogito_estella.mcp.server"])
    # The vector model now, not at a session's first question. Without it (offline, the
    # host refusing) the lite index still answers, by words only, as before 0.20.
    fetch = [py, "-c", "from cogito_estella.mcp.vectors import ensure_model; ensure_model()"]
    try:
        vectors = run(fetch, capture_output=True, text=True, timeout=timeout,
                      stdin=subprocess.DEVNULL,
                      env={**os.environ, CACHE_VARIABLE: str(vector_cache(components_dir))}
                      ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        vectors = False
    ready_marker(components_dir).write_text(
        json.dumps({"version": VERSION, "installed_at": today().isoformat(), "vectors": vectors}),
        encoding="utf-8",
    )


def full_marker(components_dir: Path | None = None) -> Path:
    return home(components_dir) / f"full-{VERSION}.json"


def finish_full_install(
    components_dir: Path | None = None,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    today: Callable[[], date] = date.today,
    timeout: float = 1800.0,
) -> None:
    """Add the graph model (Estella's ``[full]`` extra) to the base install's venv, from the
    hashed lock, with torch from PyTorch's CPU index. The base must be installed first."""
    from cogito.setup.searxng import _fail

    if not ready_marker(components_dir).is_file():
        raise InstallFailed("other", "Cogito Estella's base is not installed yet")
    venv = venv_dir(components_dir)
    py = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    lock = home(components_dir) / "requirements-full.lock"
    lock.write_text(FULL_LOCK, encoding="utf-8")
    argv = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--only-binary=:all:", "--require-hashes", "--no-deps",
            "--index-url", FULL_INDEXES[0], "--extra-index-url", FULL_INDEXES[1], "-r", str(lock)]
    check = [py, "-c", "import torch, transformers"]
    for name, command in (("installing the graph model", argv),
                          ("checking the graph model imports", check)):
        try:
            result = run(command, capture_output=True, text=True, timeout=timeout,
                         stdin=subprocess.DEVNULL, env={**os.environ, "PIP_NO_INPUT": "1"})
        except subprocess.TimeoutExpired as exc:
            raise InstallFailed("offline", f"{name} timed out after {int(timeout)} s") from exc
        except OSError as exc:
            raise InstallFailed("other", f"{name} could not run: {exc}") from exc
        if result.returncode != 0:
            raise _fail(name, result)
    full_marker(components_dir).write_text(
        json.dumps({"version": VERSION, "installed_at": today().isoformat()}), encoding="utf-8"
    )
