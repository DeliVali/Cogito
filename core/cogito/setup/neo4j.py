"""Neo4j provisioning.

Two kinds of install are supported, because the machine decides which one exists: a
**system** install from the distribution's package manager, or a **managed** one that
Cogito downloads and owns under its data directory. The shipped installer uses managed on
all three platforms. It needs no root, no third-party package can break it, and removing
Cogito removes it too.

A system install is preferred when present: if the user already runs Neo4j, we use it.

Nothing here ever runs as root.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx

from cogito.config import paths
from cogito.errors import CogitoError

DEFAULT_VERSION = "2026.08.1"
_DIST_URL = "https://dist.neo4j.org/neo4j-community-{version}-unix.tar.gz"

SYSTEM_CONFIG = Path("/etc/neo4j/neo4j.conf")
SERVICE = "neo4j"

#: Sized against the rest of the stack: the model server takes ~20 GB with MoE offload and
#: Estella ~3 GB on CPU. Neo4j is capped so it cannot grow into their memory.
RECOMMENDED_MEMORY: dict[str, str] = {
    "server.memory.heap.initial_size": "2g",
    "server.memory.heap.max_size": "2g",
    "server.memory.pagecache.size": "2g",
}

_CONFIG_MARKER = "# --- set by cogito ---"
_CHUNK = 1 << 20


class ProvisioningError(CogitoError):
    kind = "provisioning"


Kind = Literal["system", "managed", "none"]


@dataclass(frozen=True, slots=True)
class Install:
    """Where Neo4j is, however it got there."""

    kind: Kind
    home: Path | None = None
    version: str | None = None
    config_file: Path | None = None
    memory: dict[str, str] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return self.kind != "none"

    @property
    def memory_is_capped(self) -> bool:
        return all(self.memory.get(key) for key in RECOMMENDED_MEMORY)

    def _script(self, name: str) -> Path:
        if self.kind == "managed" and self.home is not None:
            return self.home / "bin" / name
        return Path(shutil.which(name) or name)

    @property
    def neo4j(self) -> Path:
        return self._script("neo4j")

    @property
    def admin(self) -> Path:
        return self._script("neo4j-admin")


def managed_home(version: str = DEFAULT_VERSION) -> Path:
    return paths.data_dir() / "neo4j" / f"neo4j-community-{version}"


def _run(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _read_memory(config_file: Path) -> dict[str, str]:
    """Read the limits actually in effect, ignoring commented-out defaults."""
    if not config_file.exists():
        return {}
    try:
        lines = config_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if (key := key.strip()) in RECOMMENDED_MEMORY:
            found[key] = value.strip()  # a later entry wins, as Neo4j itself does
    return found


def detect(version: str = DEFAULT_VERSION) -> Install:
    """Find Neo4j: the system's if it has one, otherwise ours, otherwise nothing."""
    if shutil.which("neo4j") and SYSTEM_CONFIG.exists():
        installed = _run(["pacman", "-Q", "neo4j-community"])
        match = re.search(r"\s(\S+)$", installed)
        return Install(
            kind="system",
            home=None,
            version=match.group(1) if match else "unknown",
            config_file=SYSTEM_CONFIG,
            memory=_read_memory(SYSTEM_CONFIG),
        )

    home = managed_home(version)
    config_file = home / "conf" / "neo4j.conf"
    if (home / "bin" / "neo4j").exists():
        return Install(
            kind="managed",
            home=home,
            version=version,
            config_file=config_file,
            memory=_read_memory(config_file),
        )

    return Install(kind="none")


def archive_url(version: str = DEFAULT_VERSION) -> str:
    return _DIST_URL.format(version=version)


def download(
    version: str = DEFAULT_VERSION,
    *,
    destination: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Fetch the distribution tarball and verify it against the published sha256.

    A 268 MB download that silently arrived half-complete would surface later as an
    obscure Java error, so the hash is checked before anything is extracted.
    """
    url = archive_url(version)
    target = destination or (paths.ensure(paths.cache_dir()) / f"neo4j-{version}.tar.gz")

    expected = ""
    try:
        response = httpx.get(f"{url}.sha256", timeout=30.0, follow_redirects=True)
        if response.status_code == 200:
            expected = response.text.strip().split()[0]
    except httpx.HTTPError:
        pass  # verified below only if the checksum was published

    digest = hashlib.sha256()
    partial = target.with_suffix(".part")
    try:
        with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
            if response.status_code != 200:
                raise ProvisioningError(f"{url}: HTTP {response.status_code}")
            total = int(response.headers.get("content-length", 0))
            received = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if on_progress:
                        on_progress(received, total)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise ProvisioningError(f"{url}: {exc}") from exc

    if expected and digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise ProvisioningError(
            f"checksum mismatch for {url}: expected {expected}, got {digest.hexdigest()}"
        )

    partial.replace(target)
    return target


def extract(archive: Path, version: str = DEFAULT_VERSION) -> Path:
    """Unpack into the managed location, replacing any previous attempt."""
    home = managed_home(version)
    if home.exists():
        shutil.rmtree(home)
    paths.ensure(home.parent)

    with tempfile.TemporaryDirectory(dir=home.parent) as staging:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        roots = [entry for entry in Path(staging).iterdir() if entry.is_dir()]
        if len(roots) != 1:
            raise ProvisioningError(f"unexpected archive layout: {[r.name for r in roots]}")
        roots[0].replace(home)

    for script in ("neo4j", "neo4j-admin", "cypher-shell"):
        candidate = home / "bin" / script
        if candidate.exists():
            candidate.chmod(candidate.stat().st_mode | 0o111)
    return home


def configure(install: Install, memory: dict[str, str] | None = None) -> None:
    """Apply our settings as one marked block at the end of the config.

    Existing entries for the same keys are commented out first, because Neo4j warns about
    duplicate keys.
    """
    if install.config_file is None:
        raise ProvisioningError("no config file for this installation")

    settings = memory or RECOMMENDED_MEMORY
    text = install.config_file.read_text(encoding="utf-8")
    if _CONFIG_MARKER in text:
        text = text[: text.index(_CONFIG_MARKER)].rstrip() + "\n"

    pattern = re.compile(rf"^({'|'.join(re.escape(k) for k in settings)})=", re.MULTILINE)
    text = pattern.sub(r"#\g<0>", text)

    block = "\n".join(f"{key}={value}" for key, value in settings.items())
    install.config_file.write_text(f"{text}\n{_CONFIG_MARKER}\n{block}\n", encoding="utf-8")


def set_initial_password(install: Install, password: str) -> None:
    """Set the password. Only possible before the database is first started."""
    if install.kind != "managed":
        raise ProvisioningError(
            "a system installation needs root: "
            "sudo neo4j-admin dbms set-initial-password <password>"
        )
    result = subprocess.run(
        [str(install.admin), "dbms", "set-initial-password", password],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "NEO4J_HOME": str(install.home)},
    )
    if result.returncode != 0:
        raise ProvisioningError(
            f"set-initial-password failed: {result.stderr.strip() or result.stdout.strip()}"
        )


def start(install: Install) -> str:
    if install.kind != "managed":
        return f"systemctl start {SERVICE}    # needs root"
    result = subprocess.run(
        [str(install.neo4j), "start"],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "NEO4J_HOME": str(install.home)},
    )
    if result.returncode != 0:
        raise ProvisioningError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def stop(install: Install) -> str:
    if install.kind != "managed":
        return f"systemctl stop {SERVICE}    # needs root"
    result = subprocess.run(
        [str(install.neo4j), "stop"],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "NEO4J_HOME": str(install.home)},
    )
    return result.stdout.strip() or result.stderr.strip()


def is_running(install: Install) -> bool:
    if install.kind == "system":
        return _run(["systemctl", "is-active", SERVICE]) == "active"
    if install.kind == "managed" and install.home is not None:
        output = _run(
            [str(install.neo4j), "status"],
            env={**os.environ, "NEO4J_HOME": str(install.home)},
        )
        return "is running" in output.lower()
    return False


def missing_steps(install: Install) -> list[str]:
    """What still stands between here and a usable graph."""
    if not install.present:
        return ["run: cogito setup neo4j"]
    steps: list[str] = []
    if not install.memory_is_capped:
        steps.append("run: cogito setup neo4j --configure-only")
    if not is_running(install):
        steps.append(
            f"start it: {install.neo4j} start"
            if install.kind == "managed"
            else f"sudo systemctl enable --now {SERVICE}"
        )
    return steps


EnsureState = Literal["up", "started", "absent", "unmanaged", "failed"]


@dataclass(frozen=True, slots=True)
class Ensured:
    """What `ensure_running` found or did. ``detail`` is a short reason, empty when fine."""

    state: EnsureState
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in ("up", "started")


def bolt_port_open(uri: str, timeout: float = 1.0) -> bool:
    """Whether the Bolt port accepts a TCP connection. No driver, no login."""
    parsed = urlparse(uri)
    if not parsed.hostname:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 7687), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_running(
    install: Install,
    *,
    ready: Callable[[], bool],
    wait_seconds: float = 90.0,
    poll_seconds: float = 1.0,
    start_fn: Callable[[Install], str] | None = None,
    running_fn: Callable[[Install], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Ensured:
    """Bring a managed Neo4j up if it is installed and down, then wait until ``ready``.

    Only the desktop app asks for this (`cogito graph up`): nothing restarts Neo4j after a
    reboot, and `chat` and the benchmark must never start it. A system install needs root,
    so it is reported, not started. Never raises: a failure is a state the window shows.
    """
    if ready():
        return Ensured("up")
    if not install.present:
        return Ensured("absent", "Neo4j is not installed")
    if install.kind != "managed":
        return Ensured("unmanaged", f"a system Neo4j: sudo systemctl start {SERVICE}")

    start_it = start_fn or start
    is_up = running_fn or is_running
    try:
        if not is_up(install):
            start_it(install)
    except (CogitoError, OSError, subprocess.SubprocessError) as exc:
        return Ensured("failed", str(exc) or exc.__class__.__name__)

    deadline = clock() + wait_seconds
    while clock() < deadline:
        if ready():
            return Ensured("started")
        sleep(poll_seconds)
    if ready():
        return Ensured("started")
    return Ensured("failed", f"Bolt did not open within {wait_seconds:g} s")
