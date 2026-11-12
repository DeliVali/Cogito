"""The catalog a release ships: every component Cogito knows, and the scan rules.

Curated here and checked in cogito-lab before a release, never produced by the model
(spec 2026-09-28, "Where declarations come from"). ``python -m cogito.setup.catalog``
runs the validator and exits non-zero on any problem, so a release cannot ship an
incomplete declaration.

MVP scope: the local SearXNG is installed by the app itself, with no card
(``setup/searxng.py``). The headless browser (Chrome for Testing's ``chrome-headless-shell``)
is not shipped: no tool calls ``web.browser`` yet and its downloads were never pinned, so it
could never install and it kept the release gate from passing (dry run of 2026-10-13). It
comes back with the tool that uses it and a measured pin. The compilers and language tools are ``never`` components: detected
and explained, never installed by Cogito.

Display values (``name``, ``description``, ``scope``) are in Spanish because the window
shows them between its own fixed texts. The design handoff requires these values to come
from the tool's declaration, never from the model or from app code.
"""

from __future__ import annotations

import sys

from cogito.setup import estella, searxng
from cogito.setup.components import (
    Artifact,
    BinaryProbe,
    Component,
    Download,
    PackageRef,
    PathProbe,
    ScanRule,
    validate,
)

# ---------------------------------------------------------------------------
# The local SearXNG ("Búsqueda web local")
# ---------------------------------------------------------------------------

#: The pinned source of upstream's SearXNG plus a venv built from its hashed lock
#: (`setup/searxng.py`). The source is one archive for every platform, so each build below
#: is the same file. No Windows build: upstream does not support it and its `valkeydb.py`
#: imports `pwd` (design, "Platforms"). The service installs it itself in the app, without
#: a card, because the person decided that web search needs no setup (2026-10-12).
SEARXNG = Component(
    id=searxng.COMPONENT_ID,
    name="Búsqueda web local",
    description="Un buscador en este equipo para encontrar páginas de la web.",
    provides=("web.search.local",),
    detect=PathProbe("<components>/searxng/ready.json"),
    consent="auto",
    install=Download(
        version=searxng.COMMIT,
        artifacts={
            key: Artifact(
                url=searxng.ARCHIVE_URL,
                size_bytes=searxng.ARCHIVE_BYTES,
                sha256=searxng.ARCHIVE_SHA256,
                entry=searxng.ENTRY,
            )
            for key in ("linux-x64", "mac-arm64", "mac-x64")
        },
        finish="searxng",
        extra_bytes=searxng.WHEELS_BYTES,
    ),
    scope="Solo busca. Escucha únicamente en este equipo.",
    network=True,
)

# ---------------------------------------------------------------------------
# Cogito Estella ("Memoria de documentos")
# ---------------------------------------------------------------------------

#: Cogito Estella's base install (`setup/estella.py`): the release's wheel from PyPI, pinned
#: by digest and kept as downloaded, plus a venv built from the hashed lock. Pure Python, so
#: every platform takes the same wheel. Installed by the app itself, with no card, only for
#: a person with no `[estella] command` of their own (decided by the person, 2026-10-15).
ESTELLA = Component(
    id=estella.COMPONENT_ID,
    name="Memoria de documentos",
    description="Encuentra en tus notas, páginas y documentos las líneas que responden.",
    provides=("estella.lite",),
    detect=PathProbe(f"<components>/estella/ready-{estella.VERSION}.json"),
    consent="auto",
    install=Download(
        version=estella.VERSION,
        artifacts={
            key: Artifact(
                url=estella.WHEEL_URL,
                size_bytes=estella.WHEEL_BYTES,
                sha256=estella.WHEEL_SHA256,
                entry=estella.WHEEL,
                archive="file",
            )
            for key in ("linux-x64", "mac-arm64", "mac-x64", "win-x64")
        },
        finish="estella",
        extra_bytes=estella.WHEELS_BYTES,
    ),
    scope="Solo lee lo que la conversación ya tiene. Funciona sin conexión una vez instalada.",
    network=True,
)

#: The graph model over the same venv (`setup/estella.py`'s `finish_full_install`): what a
#: large model's full graph needs (`[estella] mode`). Its download is the same pinned wheel,
#: a token the service checks; the work is the hashed lock. Installed by the app itself, with
#: no card, the first time a session runs a large model (decided by the person, 2026-10-15).
ESTELLA_FULL = Component(
    id=estella.FULL_COMPONENT_ID,
    name="Memoria de documentos completa",
    description="Extrae los hechos de la conversación para modelos grandes.",
    provides=("estella.full",),
    detect=PathProbe(f"<components>/estella/full-{estella.VERSION}.json"),
    consent="auto",
    install=Download(
        version=estella.VERSION,
        artifacts={
            key: Artifact(
                url=estella.WHEEL_URL,
                size_bytes=estella.WHEEL_BYTES,
                sha256=estella.WHEEL_SHA256,
                entry=estella.WHEEL,
                archive="file",
            )
            for key in ("linux-x64", "mac-arm64", "mac-x64", "win-x64")
        },
        finish="estella-full",
        extra_bytes=estella.FULL_BYTES,
    ),
    scope="Solo lee lo que la conversación ya tiene. Funciona sin conexión una vez instalada.",
    network=True,
)

# ---------------------------------------------------------------------------
# Programs Cogito never installs
# ---------------------------------------------------------------------------
#
# Package names follow each manager's own index and are checked read-only on the machine
# before a command is shown (setup/packages.py). A name a manager does not know only means
# that manager is skipped. Order within each OS follows the spec addendum.


def _classic(name: str) -> PackageRef:
    return PackageRef(name, ("--classic",))


GIT = Component(
    id="git",
    name="Git",
    description="",
    provides=("vcs.git",),
    detect=BinaryProbe("git"),
    consent="never",
    manual_hint="Git is not installed; install it from https://git-scm.com/downloads",
    official_url={"*": "https://git-scm.com/downloads"},
    packages={
        "winget": PackageRef("Git.Git"),
        "scoop": PackageRef("git"),
        "choco": PackageRef("git"),
        "brew": PackageRef("git"),
        "port": PackageRef("git"),
        "apt": PackageRef("git"),
        "dnf": PackageRef("git"),
        "pacman": PackageRef("git"),
        "zypper": PackageRef("git"),
    },
)

PYTHON = Component(
    id="python",
    name="Python",
    description="",
    provides=("build.python",),
    detect=BinaryProbe("python3") | BinaryProbe("python") | BinaryProbe("py"),
    consent="never",
    manual_hint="Python is not installed; install it from https://www.python.org/downloads/",
    official_url={"*": "https://www.python.org/downloads/"},
    packages={
        "winget": PackageRef("Python.Python.3.13"),
        "scoop": PackageRef("python"),
        "choco": PackageRef("python"),
        "brew": PackageRef("python"),
        "port": PackageRef("python313"),
        "apt": PackageRef("python3"),
        "dnf": PackageRef("python3"),
        "pacman": PackageRef("python"),
        "zypper": PackageRef("python3"),
    },
)

NODE = Component(
    id="node",
    name="Node",
    description="",
    provides=("build.node",),
    detect=BinaryProbe("node"),
    consent="never",
    manual_hint="Node.js is not installed; install it from https://nodejs.org/en/download",
    official_url={"*": "https://nodejs.org/en/download"},
    packages={
        "winget": PackageRef("OpenJS.NodeJS.LTS"),
        "scoop": PackageRef("nodejs-lts"),
        "choco": PackageRef("nodejs-lts"),
        "brew": PackageRef("node"),
        "port": PackageRef("nodejs22"),
        "apt": PackageRef("nodejs"),
        "dnf": PackageRef("nodejs"),
        "pacman": PackageRef("nodejs"),
        "snap": _classic("node"),
    },
)

GO = Component(
    id="go",
    name="Go",
    description="",
    provides=("build.go",),
    detect=BinaryProbe("go", ("version",)),
    consent="never",
    manual_hint="Go is not installed; install it from https://go.dev/dl/",
    official_url={"*": "https://go.dev/dl/"},
    packages={
        "winget": PackageRef("GoLang.Go"),
        "scoop": PackageRef("go"),
        "choco": PackageRef("golang"),
        "brew": PackageRef("go"),
        "port": PackageRef("go"),
        "apt": PackageRef("golang-go"),
        "dnf": PackageRef("golang"),
        "pacman": PackageRef("go"),
        "zypper": PackageRef("go"),
        "snap": _classic("go"),
    },
)

JAVA = Component(
    id="java",
    name="Java",
    description="",
    provides=("build.java",),
    detect=BinaryProbe("java", ("-version",), version_parts=1),
    consent="never",
    manual_hint="A Java JDK is not installed; install one from https://adoptium.net/",
    official_url={"*": "https://adoptium.net/"},
    packages={
        "winget": PackageRef("EclipseAdoptium.Temurin.21.JDK"),
        "scoop": PackageRef("temurin21-jdk"),
        "choco": PackageRef("temurin21"),
        "brew": PackageRef("openjdk@21"),
        "port": PackageRef("openjdk21"),
        "apt": PackageRef("openjdk-21-jdk"),
        "dnf": PackageRef("java-21-openjdk-devel"),
        "pacman": PackageRef("jdk21-openjdk"),
        "zypper": PackageRef("java-21-openjdk-devel"),
    },
)

C_COMPILER = Component(
    id="cc",
    name="C/C++",
    description="",
    provides=("build.cc",),
    detect=(
        BinaryProbe("gcc", variant="gcc")
        | BinaryProbe("clang", variant="clang")
        | BinaryProbe("cl", (), variant="MSVC")
    ),
    consent="never",
    manual_hint=(
        "No C/C++ compiler (gcc, clang or MSVC) is installed; see "
        "https://gcc.gnu.org/install/ or your platform's build tools"
    ),
    # Which compiler is "the" one differs by OS, so the official page does too.
    official_url={
        "windows": "https://visualstudio.microsoft.com/visual-cpp-build-tools/",
        "darwin": "https://developer.apple.com/xcode/resources/",
        "linux": "https://gcc.gnu.org/install/",
    },
    packages={
        "winget": PackageRef("Microsoft.VisualStudio.2022.BuildTools"),
        "scoop": PackageRef("gcc"),
        "choco": PackageRef("mingw"),
        "brew": PackageRef("llvm"),
        "apt": PackageRef("build-essential"),
        "dnf": PackageRef("gcc-c++"),
        "pacman": PackageRef("gcc"),
        "zypper": PackageRef("gcc-c++"),
    },
)

RUST = Component(
    id="rust",
    name="Rust",
    description="",
    provides=("build.rust",),
    detect=BinaryProbe("cargo") | BinaryProbe("rustc"),
    consent="never",
    manual_hint="Rust (cargo) is not installed; install it with rustup: https://www.rust-lang.org/tools/install",
    official_url={"*": "https://www.rust-lang.org/tools/install"},
    packages={
        "winget": PackageRef("Rustlang.Rustup"),
        "scoop": PackageRef("rustup"),
        "choco": PackageRef("rustup.install"),
        "brew": PackageRef("rustup"),
        "port": PackageRef("rust"),
        "apt": PackageRef("rustup"),
        "dnf": PackageRef("rustup"),
        "pacman": PackageRef("rustup"),
        "zypper": PackageRef("rustup"),
        "snap": _classic("rustup"),
    },
)

#: Everything, in the order "Herramientas y permisos" lists them.
CATALOG: tuple[Component, ...] = (
    SEARXNG, ESTELLA, ESTELLA_FULL, GIT, PYTHON, NODE, GO, JAVA, C_COMPILER, RUST,
)

#: The programs the "Del sistema" tab lists, in its own order.
SYSTEM_PROGRAMS: tuple[str, ...] = ("git", "python", "node", "go", "java", "cc", "rust")

_C_SOURCES = ("*.c", "*.h", "*.cc", "*.cpp", "*.cxx", "*.hh", "*.hpp", "*.hxx")

SCAN_RULES: tuple[ScanRule, ...] = (
    ScanRule(("Cargo.toml",), ("build.rust",)),
    ScanRule(("go.mod",), ("build.go",)),
    ScanRule(("package.json",), ("build.node",)),
    ScanRule(("pyproject.toml", "setup.py"), ("build.python",)),
    ScanRule(("pom.xml", "build.gradle", "build.gradle.kts"), ("build.java",)),
    ScanRule(("CMakeLists.txt", "Makefile", "makefile", "GNUmakefile"), ("build.cc",), _C_SOURCES),
)

#: Capabilities the search cascade escalates to. Its third level, the browser
#: (``web.browser``), returns with the component that provides it; until then the cascade
#: stops at the web search the app already ships.
CASCADE: tuple[str, ...] = ()


def by_id() -> dict[str, Component]:
    return {component.id: component for component in CATALOG}


def main() -> int:
    problems = validate(CATALOG, SCAN_RULES, CASCADE)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problem(s): this catalog cannot ship.", file=sys.stderr)
        return 1
    print("Catalog OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
