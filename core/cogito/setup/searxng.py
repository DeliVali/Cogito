"""A SearXNG of our own, for the open web.

The desktop app keeps one metasearch engine running on loopback so `web_search` has a source
that needs no key and no setup. It is upstream's own commit, downloaded on first use into the
components directory, with its Python dependencies in a private venv. It is a separate program
that Cogito talks to over HTTP; nothing here imports it. SearXNG is AGPL-3.0-or-later, see
`docs/licenses.md`.

Three parts:

- `finish_install`: after the source is unpacked, build the venv from the hashed lock.
- `render_settings` and friends: the `settings.yml` written at every start.
- `SearxngService`: provision when missing, start, watch, restart a few times, stop. It runs
  in the core (`cogito serve`, the app only); `chat` and the benchmark never build one.

The design, the sources it was built from and what happens when the engines it asks block
it: cogito-lab `research/searxng-local-DESIGN.md`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from cogito.config import paths, search
from cogito.setup.install import InstallFailed
from cogito.setup.searxng_lock import LOCK

log = logging.getLogger(__name__)

COMPONENT_ID = "searxng"

#: SearXNG has no release, no tag and no PyPI package (the `searxng` on PyPI is someone
#: else's MCP server), so the pin is a commit: master on 2026-10-11.
COMMIT = "ad46ebd7d87666edb5f116c5b274b47f689ac6cd"
ARCHIVE_URL = f"https://github.com/searxng/searxng/archive/{COMMIT}.zip"
ARCHIVE_SHA256 = "6c67b661f6bb3eaf8066772e2ed312804f1c3a626ae3b71c874a7817aab9f090"
ARCHIVE_BYTES = 7_220_663
#: What the venv step downloads (30 wheels, 33.9 MB measured on 2026-10-12), rounded up.
WHEELS_BYTES = 36_000_000
#: The one file the unpacked tree must have.
ENTRY = f"searxng-{COMMIT}/searx/webapp.py"

#: The pool `web_search` rotates through, a few engines per search (`tools/web/chain.py`), so
#: no one engine carries every query. Each engine limits each address on its own, so a wider
#: pool is more searches before every engine has said "slow down". From the author's machine
#: on 2026-10-14, 30 general-web engines of this commit were asked 4 programming queries each,
#: one engine per request (cogito-lab measurements/2026-10-14-searxng-engine-pool.md). These
#: answered with on-topic results: mwmbl, yep, yandex, naver, mojeek, privacywall, zapmeta,
#: seznam, yahoo, fynd and bing (bing 20 of its 30 on-topic; on 2026-10-12 it answered for the
#: query's first word only). brave (429) and duckduckgo (CAPTCHA) were blocked from there, but
#: a block is per address, so they stay and the rotation rests them while blocked. Left out:
#: google, qwant, startpage, dogpile and fireball (blocked), the rest (no or off-topic
#: results).
ENGINES = (
    "mwmbl", "yep", "mojeek", "yandex", "privacywall", "zapmeta", "yahoo", "bing", "seznam",
    "fynd", "naver", "brave", "duckduckgo",
)
#: On upstream. Every other engine of the pool is off there, and some (mojeek) also inactive.
_ON_UPSTREAM = ("brave", "duckduckgo")

#: Up this long before it dies, and the restart count starts again.
STABLE_SECONDS = 60.0

STATES = ("unsupported", "off", "preparing", "starting", "up", "failed")


def home(components_dir: Path | None = None) -> Path:
    return (components_dir or paths.components_dir()) / COMPONENT_ID


def source_dir(components_dir: Path | None = None) -> Path:
    return home(components_dir) / COMMIT / f"searxng-{COMMIT}"


def venv_dir(components_dir: Path | None = None) -> Path:
    return home(components_dir) / "venv"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ready_marker(components_dir: Path | None = None) -> Path:
    return home(components_dir) / "ready.json"


def state_dir() -> Path:
    """Per-machine files: the settings written at each start, the secret and the log."""
    return paths.data_dir() / "searxng"


# -- the install step ----------------------------------------------------------------------

_OFFLINE = re.compile(
    r"Temporary failure in name resolution|Name or service not known|Network is unreachable|"
    r"Connection (?:refused|reset|aborted)|ConnectionError|NameResolutionError|"
    r"Could not resolve|timed out|No route to host|ProxyError",
    re.IGNORECASE,
)


def _fail(step: str, result: subprocess.CompletedProcess) -> InstallFailed:
    output = f"{result.stderr or ''}\n{result.stdout or ''}".strip()
    tail = "\n".join(output.splitlines()[-6:])
    if "No space left on device" in output:
        kind = "disk"
    elif _OFFLINE.search(output):
        kind = "offline"
    else:
        kind = "other"
    return InstallFailed(kind, f"{step} failed: {tail}")


def finish_install(
    components_dir: Path | None = None,
    *,
    python: str | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    today: Callable[[], date] = date.today,
    timeout: float = 1200.0,
) -> None:
    """Build the venv the unpacked source runs in, from the hashed lock.

    Nothing is compiled and nothing outside the lock is installed (`--only-binary`,
    `--require-hashes`, `--no-deps`). The source is not pip-installed: it runs from
    `PYTHONPATH`. A frozen version file stops it asking `git` for its own version.
    Raises `InstallFailed`; the caller removes whatever was written.
    """
    base = home(components_dir)
    source = source_dir(components_dir)
    if not (source / "searx" / "webapp.py").is_file():
        raise InstallFailed("other", "the SearXNG source is not unpacked")
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
    lock.write_text(LOCK, encoding="utf-8")
    py = str(venv_python(venv))
    step(
        "installing the dependencies",
        [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
         "--only-binary=:all:", "--require-hashes", "--no-deps", "-r", str(lock)],
    )
    day = today()
    version = f"{day.year}.{day.month}.{day.day}+{COMMIT[:7]}"
    (source / "searx" / "version_frozen.py").write_text(
        "# Written by COGITO: this tree is not a git checkout.\n"
        f'VERSION_STRING = "{version}"\nVERSION_TAG = "{version}"\n'
        f'DOCKER_TAG = "{version}"\nGIT_URL = "https://github.com/searxng/searxng"\n'
        'GIT_BRANCH = "master"\n',
        encoding="utf-8",
    )
    step(
        "checking the dependencies import",
        [py, "-c", "import flask, lxml, curl_cffi, msgspec, yaml, valkey, babel"],
    )
    ready_marker(components_dir).write_text(
        json.dumps({"commit": COMMIT, "installed_at": today().isoformat()}), encoding="utf-8"
    )


# -- the settings written at each start ---------------------------------------------------


def secret_key(directory: Path | None = None) -> str:
    """This machine's secret, made once and kept (0600). SearXNG signs its own cookies and
    links with it; nothing else reads it, and it never leaves the machine."""
    folder = directory or state_dir()
    target = folder / "secret"
    try:
        existing = target.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{64}", existing):
            return existing
    except OSError:
        pass
    paths.ensure(folder)
    value = secrets.token_hex(32)
    partial = folder / f".secret.{os.getpid()}"
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    os.replace(partial, target)
    return value


def render_settings(*, port: int, secret: str, engines: tuple[str, ...] = ENGINES) -> str:
    """`settings.yml` for one local user. JSON scalars are YAML, so values are quoted by
    `json.dumps` and cannot break the file."""
    q = json.dumps
    lines = [
        "# Written by COGITO at every start; edits are overwritten.",
        "use_default_settings:",
        "  engines:",
        f"    keep_only: {q(list(engines))}",
        "general:",
        "  debug: false",
        f"  instance_name: {q('COGITO local search')}",
        "  enable_metrics: false",
        "search:",
        "  safe_search: 0",
        '  autocomplete: ""',
        '  favicon_resolver: ""',
        # Only JSON is served. The HTML results page answers 403; the home page answers, to
        # this machine only.
        '  formats: ["json"]',
        "server:",
        '  bind_address: "127.0.0.1"',
        f"  port: {int(port)}",
        f"  secret_key: {q(secret)}",
        "  base_url: false",
        # One local user: no limiter (it needs Valkey and is for bot traffic), no public
        # instance features, no image proxy.
        "  limiter: false",
        "  public_instance: false",
        "  image_proxy: false",
        '  method: "GET"',
        "valkey:",
        "  url: false",
        "outgoing:",
        "  request_timeout: 3.0",
        "  max_request_timeout: 5.0",
        "engines:",
    ]
    lines += [
        f"  - {{name: {q(name)}, disabled: false, inactive: false}}"
        for name in engines if name not in _ON_UPSTREAM
    ]
    return "\n".join(lines) + "\n"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


_KEEP_ENV = frozenset(
    {"PATH", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SYSTEMROOT",
     "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
     "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
)


def child_environment(
    settings_file: Path, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Built from scratch: a `SEARXNG_*` variable (they override the settings file) or a
    `PYTHONPATH` of the person's cannot reach it. Proxy and CA variables pass through."""
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if k.upper() in _KEEP_ENV}
    env.update(
        SEARXNG_SETTINGS_PATH=str(settings_file),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
    )
    return env


#: Run with `python -c`: leave when the core that started it is gone (the core can be
#: killed without a chance to stop us), then run `python -m searx.webapp` from the source.
_LAUNCH = """\
import os, runpy, sys, threading, time
parent, source = int(sys.argv[1]), sys.argv[2]
def watch():
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            os._exit(0)
threading.Thread(target=watch, daemon=True).start()
sys.path.insert(0, source)
sys.argv = ["searx.webapp"]
runpy.run_module("searx.webapp", run_name="__main__")
"""


def _healthy(url: str, timeout: float = 1.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + "/healthz", timeout=timeout) as response:
            return int(response.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# -- the service -----------------------------------------------------------------------------


class Components(Protocol):
    """What the service asks of `setup.service.ComponentService`."""

    def component(self, component_id: str) -> Any: ...
    def detect(self, component: Any) -> Any: ...
    def is_declined(self, component_id: str, what: str | None = None) -> bool: ...
    def install(self, component_id: str) -> dict[str, Any]: ...
    def wait_install(
        self, component_id: str, *, abort: Callable[[], bool] | None = ...
    ) -> bool: ...
    def failure(self, component_id: str) -> dict[str, str] | None: ...
    def platform(self) -> str | None: ...


class SearxngService:
    """The local SearXNG, from "not installed" to "answering".

    One background thread does everything, so a start that takes a minute never holds the
    core's ready line. `url()` is what the web chain asks per query: the address while the
    process is alive and answered its health check, otherwise None, and the chain goes on
    without it.
    """

    #: Restarts after a crash or a failed start, then it stays failed until the next launch.
    BACKOFF = (5.0, 30.0)
    #: A machine that was offline at launch tries to provision again this often.
    OFFLINE_RETRY = 600.0

    def __init__(
        self,
        components: Components,
        *,
        components_dir: Path | None = None,
        directory: Path | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        probe: Callable[[str], bool] = _healthy,
        pick_port: Callable[[], int] = free_port,
        level: Callable[[], str] = lambda: search.load()["web"],
        clock: Callable[[], float] = time.monotonic,
        pause: Callable[[float], None] | None = None,
        environ: Mapping[str, str] | None = None,
        python: str | None = None,
        start_timeout: float = 45.0,
        poll: float = 2.0,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._components = components
        self._components_dir = components_dir
        self._directory = directory
        self._popen = popen
        self._probe = probe
        self._pick_port = pick_port
        self._level = level
        self._clock = clock
        self._stop = threading.Event()
        self._pause = pause or (lambda seconds: self._stop.wait(seconds))
        self._environ = environ
        self._python = python
        self._start_timeout = start_timeout
        self._poll = poll
        self._on_state = on_state or (lambda event: None)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._proc: Any = None
        self._port = 0
        self._state = "off"
        self._kind = ""
        self._failures = 0
        self._up_at = 0.0
        self._gave_up = False
        self._retry_at = 0.0
        self._install_blocked_until = 0.0

    # -- what others ask ---------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {"state": self._state}
            if self._state == "failed" and self._kind:
                out["kind"] = self._kind
            return out

    def url(self) -> str | None:
        with self._lock:
            if self._state != "up" or self._proc is None or self._proc.poll() is not None:
                return None
            return f"http://127.0.0.1:{self._port}"

    # -- life ---------------------------------------------------------------------------------

    def start_background(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop, name="cogito-searxng", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc, self._proc = self._proc, None
            self._port = 0
        if proc is not None:
            self._terminate(proc)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._cycle()
                self._pause(self._poll)
        except Exception:  # a bug here must never reach the app
            log.exception("local SearXNG supervisor stopped")
            self._set("failed", "other")

    def _set(self, state: str, kind: str = "") -> None:
        with self._lock:
            if (self._state, self._kind) == (state, kind):
                return
            self._state, self._kind = state, kind
        try:
            self._on_state({"type": "local_search_state", **self.status()})
        except Exception:
            log.exception("local search state listener")

    def _wanted(self) -> bool:
        try:
            if self._level() == "off":
                return False
        except Exception:
            log.warning("config.toml [search] unreadable; local search left off")
            return False
        return not self._components.is_declined(COMPONENT_ID)

    def _cycle(self) -> None:
        platform = self._components.platform()
        component = self._components.component(COMPONENT_ID)
        if component is None or component.artifact(platform) is None:
            self._set("unsupported")
            self._stop.set()
            return
        if not self._wanted():
            self._drop()
            self._failures, self._gave_up, self._retry_at = 0, False, 0.0
            self._set("off")
            return
        with self._lock:
            proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                return
            self._lost()
            return
        if self._gave_up or self._clock() < self._retry_at:
            return
        if not self._components.detect(component).found:
            if self._clock() < self._install_blocked_until:
                return
            if not self._provision():
                return
        self._launch()

    def _provision(self) -> bool:
        self._set("preparing")
        started = self._components.install(COMPONENT_ID)
        if started.get("error") not in (None, "busy"):
            self._install_failed("other")
            return False
        self._components.wait_install(COMPONENT_ID, abort=self._stop.is_set)
        if self._stop.is_set():
            return False
        if self._components.detect(self._components.component(COMPONENT_ID)).found:
            return True
        failure = self._components.failure(COMPONENT_ID) or {}
        self._install_failed(failure.get("kind") or "other")
        return False

    def _install_failed(self, kind: str) -> None:
        self._set("failed", kind)
        # Offline now is not offline for good; anything else needs a new release or room.
        self._install_blocked_until = self._clock() + (
            self.OFFLINE_RETRY if kind == "offline" else float("inf")
        )

    def _launch(self) -> None:
        self._set("starting")
        directory = self._directory or state_dir()
        paths.ensure(directory)
        port = self._pick_port()
        settings_file = directory / "settings.yml"
        settings_file.write_text(
            render_settings(port=port, secret=secret_key(directory)), encoding="utf-8"
        )
        source = source_dir(self._components_dir)
        py = venv_python(venv_dir(self._components_dir))
        log_file = directory / "searxng.log"
        try:
            with log_file.open("wb") as sink:
                proc = self._popen(
                    [str(py), "-c", _LAUNCH, str(os.getpid()), str(source)],
                    cwd=str(source),
                    env=child_environment(settings_file, self._environ),
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                )
        except OSError as exc:
            log.warning("local SearXNG could not start: %s", exc)
            self._failed_start()
            return
        url = f"http://127.0.0.1:{port}"
        deadline = self._clock() + self._start_timeout
        while self._clock() < deadline and not self._stop.is_set():
            if proc.poll() is not None:
                break
            if self._probe(url):
                with self._lock:
                    self._proc, self._port = proc, port
                self._up_at = self._clock()
                self._set("up")
                return
            self._pause(0.3)
        self._terminate(proc)
        log.warning("local SearXNG did not answer; see %s", log_file)
        if not self._stop.is_set():
            self._failed_start()

    def _failed_start(self) -> None:
        self._failures += 1
        if self._failures > len(self.BACKOFF):
            self._gave_up = True
            self._set("failed", "start")
            return
        self._retry_at = self._clock() + self.BACKOFF[self._failures - 1]
        self._set("starting")

    def _lost(self) -> None:
        with self._lock:
            self._proc, self._port = None, 0
        log.warning("local SearXNG exited")
        if self._clock() - self._up_at >= STABLE_SECONDS:
            self._failures = 0  # it ran a while: this is a new trouble, not the same one
        self._failed_start()

    def _drop(self) -> None:
        with self._lock:
            proc, self._proc, self._port = self._proc, None, 0
        if proc is not None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: Any) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                log.warning("local SearXNG would not stop")


# -- the one the app holds ---------------------------------------------------------------------

_current: SearxngService | None = None


def current() -> SearxngService | None:
    """The service `cogito serve` built, or None (`chat`, a test)."""
    return _current


def set_current(service: SearxngService | None) -> None:
    global _current
    _current = service


def local_url() -> str | None:
    """Where the local SearXNG answers right now, for the web chain."""
    service = _current
    return service.url() if service is not None else None
