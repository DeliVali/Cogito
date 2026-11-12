"""What a component is: how it is detected, how it is installed, who may install it.

Spec: cogito-lab ``specs/2026-09-28-component-catalog-design.md`` (its addendum overrides
the body). The curated declarations a release ships are in ``catalog.py``. This module
holds the types those declarations use and the validator a release must pass.

A new tool rarely needs a new type. Probes are ``BinaryProbe``, ``HttpProbe`` and
``PathProbe``, combinable with ``|``. Installers are ``Download`` (pinned, verified files),
``UvVenv`` and ``Playwright`` (declared for later capability packs, not built in the MVP),
or ``None`` for detect-only components. ``consent="never"`` is for compilers, runtimes and
third-party applications: Cogito detects them and explains how to install them, but never
installs them itself.

Everything a probe consults on the machine goes through a ``ProbeEnv``, so the whole
catalog can be exercised without running a real binary.
"""

from __future__ import annotations

import platform as platform_module
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cogito.config import paths

#: The three consent classes (spec, "Data model").
CONSENTS = ("auto", "ask", "never")

#: ``auto`` components below this size install in the background without asking. A
#: starting value (spec, "The decision function"). It changes only through a release, and
#: the release notes record the change.
AUTO_INSTALL_MAX_BYTES = 20_000_000

#: What a ``Download`` artifact carries until the lab's pinning script has measured its
#: digest. The validator refuses it and the installer never downloads it.
TODO_SHA256 = "TODO: pinned by cogito-lab's pinning script"

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"(\d+(?:\.\d+)+|\d+)")

#: Platform keys a ``Download`` may carry artifacts for.
PLATFORMS = ("linux-x64", "mac-arm64", "mac-x64", "win-x64", "win-x86")


def platform_key(system: str | None = None, machine: str | None = None) -> str | None:
    """This machine's key in ``PLATFORMS``, or None when no build could exist for it."""
    system = system if system is not None else sys.platform
    machine = (machine if machine is not None else platform_module.machine()).lower()
    x64 = machine in {"x86_64", "amd64"}
    arm64 = machine in {"arm64", "aarch64"}
    if system.startswith("linux"):
        return "linux-x64" if x64 else None
    if system == "darwin":
        return "mac-arm64" if arm64 else "mac-x64" if x64 else None
    if system in {"win32", "cygwin"}:
        return "win-x64" if x64 else "win-x86" if machine in {"x86", "i386", "i686"} else None
    return None


def os_name(system: str | None = None) -> str:
    """``windows``, ``darwin`` or ``linux``: the key package managers and URLs use."""
    system = system if system is not None else sys.platform
    if system in {"win32", "cygwin"}:
        return "windows"
    if system == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detection:
    """What a probe found. ``variant`` names which alternative matched (e.g. ``gcc``)."""

    found: bool
    version: str | None = None
    path: str | None = None
    variant: str | None = None


NOT_FOUND = Detection(False)


def _quiet_environment() -> dict[str, str]:
    """The caller's environment with a fixed locale, so version banners parse the same
    everywhere, and with Homebrew told not to update itself during a query."""
    return {
        **paths.user_environment(),
        "LANG": "C",
        "LC_ALL": "C",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_ANALYTICS": "1",
    }


@dataclass(frozen=True)
class ProbeEnv:
    """Everything a probe may consult. Injected so tests never touch the real machine."""

    #: Looks on the person's PATH, never Cogito's own virtualenv (see
    #: `paths.user_environment`), since «Del sistema» lists this machine's programs.
    which: Callable[[str], str | None] = field(
        default=lambda name: shutil.which(name, path=paths.user_environment().get("PATH"))
    )
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    platform: str = sys.platform
    data_dir: Path = field(default_factory=paths.data_dir)
    components_dir: Path = field(default_factory=paths.components_dir)
    urlopen: Callable[..., Any] = urllib.request.urlopen
    timeout: float = 5.0

    def execute(self, argv: Sequence[str]) -> subprocess.CompletedProcess | None:
        """Run ``argv`` read-only, with a timeout. None when it could not run at all."""
        try:
            return self.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                env=_quiet_environment(),
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None


class Probe:
    """Base class. ``check`` answers whether the thing is here; ``a | b`` tries both."""

    def check(self, env: ProbeEnv) -> Detection:  # pragma: no cover - abstract
        raise NotImplementedError

    def alternatives(self) -> tuple[Probe, ...]:
        return (self,)

    def __or__(self, other: Probe) -> AnyProbe:
        return AnyProbe((*self.alternatives(), *other.alternatives()))


#: macOS ships stubs in /usr/bin that open an installer dialog when run without the
#: Command Line Tools. They run only after ``xcode-select -p`` (which never prompts) says
#: the tools are there.
_MACOS_CLT_STUBS = frozenset(
    f"/usr/bin/{name}"
    for name in ("git", "python3", "gcc", "g++", "clang", "clang++", "cc", "c++", "make")
)
#: /usr/bin/java is the same kind of stub for a JDK; java_home answers without a dialog.
_MACOS_JAVA_STUB = "/usr/bin/java"


@dataclass(frozen=True)
class BinaryProbe(Probe):
    """A command on PATH that answers ``command *args`` with a version banner.

    Found when the command ran and either exited 0 or printed a version. MSVC's ``cl``
    prints its banner and exits non-zero; the Windows Store ``python`` alias does neither.
    ``version_parts`` is how many version components are reported (``21`` for Java,
    ``3.12`` for Python).
    """

    command: str
    args: tuple[str, ...] = ("--version",)
    version_parts: int = 2
    variant: str | None = None

    def check(self, env: ProbeEnv) -> Detection:
        resolved = env.which(self.command)
        if not resolved:
            return NOT_FOUND
        if env.platform == "darwin" and not _macos_stub_ready(resolved, env):
            return NOT_FOUND
        completed = env.execute([resolved, *self.args])
        if completed is None:
            return NOT_FOUND
        banner = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        match = _VERSION.search(banner)
        if completed.returncode != 0 and match is None:
            return NOT_FOUND
        version = ".".join(match.group(1).split(".")[: self.version_parts]) if match else None
        return Detection(True, version, resolved, self.variant)


def _macos_stub_ready(resolved: str, env: ProbeEnv) -> bool:
    if resolved in _MACOS_CLT_STUBS:
        completed = env.execute(["xcode-select", "-p"])
        return completed is not None and completed.returncode == 0
    if resolved == _MACOS_JAVA_STUB:
        completed = env.execute(["/usr/libexec/java_home"])
        return completed is not None and completed.returncode == 0
    return True


@dataclass(frozen=True)
class PathProbe(Probe):
    """A file that exists. ``<data>`` and ``<components>`` expand to Cogito's own
    directories, so a declaration never builds a path by hand."""

    path: str

    def resolve(self, env: ProbeEnv) -> Path:
        expanded = self.path.replace("<components>", str(env.components_dir)).replace(
            "<data>", str(env.data_dir)
        )
        return Path(expanded)

    def check(self, env: ProbeEnv) -> Detection:
        target = self.resolve(env)
        return Detection(True, path=str(target)) if target.exists() else NOT_FOUND


@dataclass(frozen=True)
class HttpProbe(Probe):
    """A local service that answers 2xx at ``url``."""

    url: str
    timeout: float = 1.0

    def check(self, env: ProbeEnv) -> Detection:
        try:
            with env.urlopen(self.url, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
        except (urllib.error.URLError, OSError, ValueError):
            return NOT_FOUND
        return Detection(True, path=self.url) if 200 <= status < 300 else NOT_FOUND


@dataclass(frozen=True)
class AnyProbe(Probe):
    """The first alternative that finds something, tried in order."""

    probes: tuple[Probe, ...]

    def alternatives(self) -> tuple[Probe, ...]:
        return self.probes

    def check(self, env: ProbeEnv) -> Detection:
        for probe in self.probes:
            detection = probe.check(env)
            if detection.found:
                return detection
        return NOT_FOUND


# ---------------------------------------------------------------------------
# Installers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Artifact:
    """One platform's file: where it is, how big, its digest, and what inside it runs."""

    url: str
    size_bytes: int
    sha256: str
    #: The executable, relative to the unpacked tree.
    entry: str
    #: ``zip`` is unpacked; ``file`` is kept as downloaded, at ``entry``.
    archive: str = "zip"


@dataclass(frozen=True)
class Download:
    """A pinned, verified download, one artifact per platform that has a build."""

    version: str
    artifacts: Mapping[str, Artifact]
    #: Names a step the service runs after the unpack, for a component that is more than
    #: its files (a Python venv built from them). Empty for a plain download.
    finish: str = ""
    #: What that step downloads on top of the artifact, so the size shown is the real one.
    extra_bytes: int = 0

    def for_platform(self, key: str | None) -> Artifact | None:
        return self.artifacts.get(key) if key else None


@dataclass(frozen=True)
class UvVenv:
    """A Python package in a private venv. Declared for later packs; not built in the MVP."""

    spec: str
    size_bytes: int


@dataclass(frozen=True)
class Playwright:
    """A Playwright-managed browser. Declared for later packs; not built in the MVP."""

    browser: str
    size_bytes: int


Installer = Download | UvVenv | Playwright


# ---------------------------------------------------------------------------
# Components and scan rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackageRef:
    """A package in one package manager's index, plus any flags its install needs
    (``--classic`` for a snap, ``--cask`` for a Homebrew cask)."""

    name: str
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Component:
    """One thing a capability can be provided by.

    ``name``, ``description`` and ``scope`` are what the window shows between its fixed
    texts («Qué habilita», «Alcance»). They come from here, never from the model.
    """

    id: str
    name: str
    description: str
    provides: tuple[str, ...]
    detect: Probe | None
    consent: str
    install: Installer | None = None
    #: Required when ``consent == "never"``: what the harness can tell the person about it.
    manual_hint: str | None = None
    #: Required when ``consent == "never"``: os (``windows``/``darwin``/``linux``, or
    #: ``*`` for all) -> https URL of the official download page.
    official_url: Mapping[str, str] = field(default_factory=dict)
    #: ``never`` components: package manager -> the package that installs it.
    packages: Mapping[str, PackageRef] = field(default_factory=dict)
    #: What using it can and cannot do, shown as «Alcance» on a permission request.
    scope: str | None = None
    #: Using it asks the person first (once per session). Never auto-installed.
    permission: bool = False
    #: Using it reaches the network. Never auto-installed.
    network: bool = False

    def official_url_for(self, os_key: str) -> str | None:
        return self.official_url.get(os_key) or self.official_url.get("*")

    def artifact(self, platform: str | None) -> Artifact | None:
        return self.install.for_platform(platform) if isinstance(self.install, Download) else None

    def size_bytes(self, platform: str | None) -> int | None:
        if isinstance(self.install, Download):
            artifact = self.install.for_platform(platform)
            return artifact.size_bytes + self.install.extra_bytes if artifact else None
        if isinstance(self.install, UvVenv | Playwright):
            return self.install.size_bytes
        return None


@dataclass(frozen=True, slots=True)
class ScanRule:
    """File names that, found in a project, mean it needs these capabilities.

    ``patterns`` are matched (fnmatch) against file names anywhere within the scan's
    depth. ``with_sources``, when set, must also match at least one file, because a
    Makefile alone says nothing about C.
    """

    patterns: tuple[str, ...]
    needs: tuple[str, ...]
    with_sources: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Validation (spec, "Validation"): a release cannot ship an incomplete declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    rule: str
    component: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.component}: {self.message}"


def _detect(component: Component) -> Iterable[str]:
    if component.detect is None:
        yield "no detect probe; every component must say how to tell it is already here"


def _consent(component: Component) -> Iterable[str]:
    if component.consent not in CONSENTS:
        yield f"consent {component.consent!r} is not one of {', '.join(CONSENTS)}"


def _installable(component: Component) -> Iterable[str]:
    if component.consent in {"auto", "ask"} and component.install is None:
        yield "consent allows installing it, but it declares no installer"


def _sizes(component: Component) -> Iterable[str]:
    installer = component.install
    if isinstance(installer, UvVenv | Playwright) and installer.size_bytes <= 0:
        yield "size_bytes must be declared (> 0)"
    if isinstance(installer, Download):
        for key, artifact in installer.artifacts.items():
            if artifact.size_bytes <= 0:
                yield f"{key}: size_bytes must be declared (> 0)"


def _pinned(component: Component) -> Iterable[str]:
    installer = component.install
    if not isinstance(installer, Download):
        return
    if not installer.artifacts:
        yield "a Download with no artifact for any platform"
    for key, artifact in installer.artifacts.items():
        if key not in PLATFORMS:
            yield f"{key}: unknown platform; known: {', '.join(PLATFORMS)}"
        if not artifact.url.startswith("https://"):
            yield f"{key}: url must be https: {artifact.url}"
        if installer.version not in artifact.url:
            yield f"{key}: url does not pin version {installer.version}: {artifact.url}"
        if not SHA256_PATTERN.match(artifact.sha256):
            yield f"{key}: sha256 is not pinned (64 lowercase hex digits): {artifact.sha256!r}"


def _never_manual_hint(component: Component) -> Iterable[str]:
    if component.consent == "never" and not component.manual_hint:
        yield "consent never needs a manual_hint: what to tell the person instead"


def _never_official_url(component: Component) -> Iterable[str]:
    if component.consent != "never":
        return
    if not component.official_url:
        yield "consent never needs an official_url for when no package manager can install it"
    for key, url in component.official_url.items():
        if not url.startswith("https://"):
            yield f"official_url[{key}] must be https: {url}"


def _never_no_installer(component: Component) -> Iterable[str]:
    if component.consent == "never" and component.install is not None:
        yield "consent never declares an installer; Cogito never installs these itself"


def _permission_scope(component: Component) -> Iterable[str]:
    if component.permission and not component.scope:
        yield "a component that asks permission must declare its scope («Alcance»)"


#: (rule name, per-component check). Table-driven: a new rule is one row.
COMPONENT_RULES: tuple[tuple[str, Callable[[Component], Iterable[str]]], ...] = (
    ("detect", _detect),
    ("consent", _consent),
    ("installable", _installable),
    ("sizes", _sizes),
    ("pinned-downloads", _pinned),
    ("never-manual-hint", _never_manual_hint),
    ("never-official-url", _never_official_url),
    ("never-no-installer", _never_no_installer),
    ("permission-scope", _permission_scope),
)


def validate(
    components: Sequence[Component],
    scan_rules: Sequence[ScanRule] = (),
    cascade: Sequence[str] = (),
) -> list[Problem]:
    """Every problem with these declarations; empty means they may ship."""
    problems: list[Problem] = []
    seen: set[str] = set()
    for component in components:
        if component.id in seen:
            problems.append(Problem("unique-ids", component.id, "declared more than once"))
        seen.add(component.id)
        for rule, check in COMPONENT_RULES:
            problems.extend(Problem(rule, component.id, message) for message in check(component))
    provided = {capability for c in components for capability in c.provides}
    requested = [
        (f"scan rule {', '.join(r.patterns)}", need) for r in scan_rules for need in r.needs
    ]
    requested += [("cascade level", capability) for capability in cascade]
    for source, capability in requested:
        if capability not in provided:
            problems.append(
                Problem(
                    "providers",
                    capability,
                    f"{source} needs {capability!r}, which no component provides",
                )
            )
    return problems
