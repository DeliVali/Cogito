#!/usr/bin/env python3
"""Assemble the Python core an installer ships, for one target, from pinned inputs.

    python3 desktop/packaging/bundle-core.py TARGET [--out-root DIR] [--downloads DIR]
                                                    [--wheel FILE]

    TARGET       linux-x64 | mac-arm64 | mac-x64 | win-x64
    --out-root   Default desktop/build/core (not tracked). Writes <out-root>/<TARGET>/,
                 removed first, and only once every check below has passed.
    --downloads  Archive cache. Default <out-root>/downloads. A cached archive is used
                 only if its SHA-256 is the pinned one; any other is fetched again.
    --wheel      A built cogito wheel (py3-none-any). Default: built from this checkout.

Host: Linux x86_64, python3 >= 3.12 (stdlib only), the uv version pinned in
core-bundle.json, readelf, and network to github.com and pypi.org (or warm caches with
UV_OFFLINE=1).

Writes:
    <out-root>/<TARGET>/python       what electron-builder copies to resources/core/python
    <out-root>/<TARGET>/bundle.json  provenance and a hash of every file; not shipped
    <out-root>/.host/python          the unpruned linux-x64 interpreter that runs uv and
                                     compileall for every target

Exit: 0 built and checked; 1 a step failed (the message names it); 2 usage.

The interpreter is python-build-standalone's install_only_stripped CPython. The runtime
closure comes from uv.lock with its hashes, installed for the target's platform with
`uv pip install --target --python-platform`, so one Linux runner assembles all four
targets. The installed packages must then satisfy the cogito wheel's own dependencies:
`uv export --frozen` does not compare uv.lock with pyproject.toml, so a stale lock would
otherwise ship without a package. Bytecode is precompiled as unchecked-hash: an installer
that touches the sources' mtimes cannot make every start recompile in memory, and nothing
is written at run time.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator
from email.parser import HeaderParser
from pathlib import Path, PurePosixPath

PACKAGING = Path(__file__).resolve().parent
DESKTOP = PACKAGING.parent
REPO = DESKTOP.parent
PINS_FILE = PACKAGING / "core-bundle.json"
BUILD_CONSTRAINTS = PACKAGING / "core-build-constraints.txt"

TARGETS = ("linux-x64", "mac-arm64", "mac-x64", "win-x64")
#: The target whose interpreter runs uv and compileall on the build host.
HOST_TARGET = "linux-x64"
OFFICIAL_BASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download"
#: manylinux_2_17, the glibc floor python-build-standalone itself has. uv's
#: x86_64-unknown-linux-gnu means manylinux_2_28, which would raise it.
LINUX_WHEEL_PLATFORM = "x86_64-manylinux_2_17"
GLIBC_CEILING = (2, 17)
#: The longest path allowed under python/ on Windows. The 20261009 tree's longest is 151;
#: this leaves 100 of MAX_PATH's 260 for the install folder.
WINDOWS_PATH_LIMIT = 160
SHA256 = re.compile(r"^[0-9a-f]{64}$")

#: What each layout drops from the archive: build-time files, Tcl/Tk, IDLE, pip and the
#: other modules a server never imports. Globs relative to python/.
PRUNE = {
    "posix": (
        "bin/*",
        "include",
        "share",
        "lib/pkgconfig",
        "lib/libpython3*",
        "lib/tcl9.0",
        "lib/tk9.0",
        "lib/tcl9",
        "lib/itcl*",
        "lib/libtcl9*",
        "lib/python3.12/idlelib",
        "lib/python3.12/tkinter",
        "lib/python3.12/turtledemo",
        "lib/python3.12/lib2to3",
        "lib/python3.12/pydoc_data",
        "lib/python3.12/ensurepip",
        "lib/python3.12/turtle.py",
        "lib/python3.12/config-3.12-*",
        "lib/python3.12/lib-dynload/_tkinter*",
        "lib/python3.12/site-packages/*",
    ),
    "windows": (
        "Scripts",
        "include",
        "libs",
        "tcl",
        "pythonw.exe",
        "DLLs/_tkinter.pyd",
        "DLLs/tcl86t.dll",
        "DLLs/tk86t.dll",
        "DLLs/_ctypes_test.pyd",
        "DLLs/_test*.pyd",
        "Lib/idlelib",
        "Lib/tkinter",
        "Lib/turtledemo",
        "Lib/lib2to3",
        "Lib/pydoc_data",
        "Lib/ensurepip",
        "Lib/turtle.py",
        "Lib/site-packages/*",
    ),
}
#: Kept even when a prune glob matches it.
KEEP = {"posix": frozenset({"bin/python3.12"}), "windows": frozenset()}

LAYOUT = {
    "posix": {"interpreter": "bin/python3.12", "stdlib": "lib/python3.12"},
    "windows": {"interpreter": "python.exe", "stdlib": "Lib"},
}

#: Expected native code per target: (format, machine). Mach-O machines are cputypes.
NATIVE = {
    "linux-x64": ("elf", 62),
    "mac-arm64": ("macho", 0x0100000C),
    "mac-x64": ("macho", 0x01000007),
    "win-x64": ("pe", 0x8664),
}

#: Written by uv into each dist-info, and not reproducible: the local wheel's absolute
#: build path, and a timestamp.
NOT_SHIPPED_METADATA = ("direct_url.json", "uv_cache.json")
#: uv's lock for the --target folder: empty, and created world-writable.
TARGET_LOCK = ".lock"


class BundleError(Exception):
    """A step failed. The message names the step and what to do."""


def log(message: str) -> None:
    print(f"[bundle-core] {message}", file=sys.stderr, flush=True)


@contextlib.contextmanager
def step(name: str) -> Iterator[None]:
    """Name the step in any failure inside it."""
    log(name)
    try:
        yield
    except BundleError:
        raise
    except (
        OSError,
        tarfile.TarError,
        zipfile.BadZipFile,
        urllib.error.URLError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        raise BundleError(f"{name}: {type(exc).__name__}: {exc}") from exc


def walk(root: Path) -> Iterator[Path]:
    """Every path under `root`, never through a symlink (a linked folder is yielded, not
    entered)."""
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in sorted(folders) + sorted(names):
            yield Path(directory, name)


# -- pins ------------------------------------------------------------------------------------


def load_pins(path: Path = PINS_FILE) -> dict:
    """The pins, refused unless they name exactly the four targets and official sources."""
    pins = json.loads(Path(path).read_text(encoding="utf-8"))
    python = pins.get("python", {})
    targets = pins.get("targets", {})
    if set(targets) != set(TARGETS):
        raise BundleError(
            f"pins: targets must be exactly {', '.join(TARGETS)}, not {sorted(targets)}"
        )
    if python.get("base_url") != OFFICIAL_BASE_URL:
        raise BundleError(f"pins: python.base_url must be {OFFICIAL_BASE_URL}")
    if not str(python.get("version", "")).startswith("3.12."):
        raise BundleError(
            "pins: python.version must be a 3.12 release; the layout names python3.12"
        )
    if python.get("flavor") != "install_only_stripped":
        raise BundleError(
            "pins: python.flavor must be install_only_stripped; the prune lists assume it"
        )
    if not re.fullmatch(r"\d{8}", str(python.get("release", ""))):
        raise BundleError(
            "pins: python.release must be a python-build-standalone release tag (YYYYMMDD)"
        )
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(pins.get("uv", ""))):
        raise BundleError("pins: uv must be an exact version")
    for name, target in targets.items():
        if not SHA256.fullmatch(str(target.get("sha256", ""))):
            raise BundleError(f"pins: {name}.sha256 must be 64 lowercase hex characters")
        if target.get("layout") not in LAYOUT:
            raise BundleError(f"pins: {name}.layout must be one of {sorted(LAYOUT)}")
        if not target.get("triple") or not target.get("wheel_platform"):
            raise BundleError(f"pins: {name} needs a triple and a wheel_platform")
    if targets["linux-x64"]["wheel_platform"] != LINUX_WHEEL_PLATFORM:
        raise BundleError(
            f"pins: linux-x64.wheel_platform must be {LINUX_WHEEL_PLATFORM} (the glibc 2.17 floor)"
        )
    return pins


def archive_name(pins: dict, target: str) -> str:
    python = pins["python"]
    triple = pins["targets"][target]["triple"]
    return f"cpython-{python['version']}+{python['release']}-{triple}-{python['flavor']}.tar.gz"


def archive_url(pins: dict, target: str) -> str:
    python = pins["python"]
    name = urllib.parse.quote(archive_name(pins, target), safe="")
    return f"{python['base_url']}/{python['release']}/{name}"


# -- host --------------------------------------------------------------------------------------


def check_host(
    uv_pin: str,
    *,
    run: Callable = subprocess.run,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Refuse a host this script was not measured on. Returns the uv version."""
    system = system or platform.system()
    machine = machine or platform.machine()
    if system != "Linux" or machine not in ("x86_64", "AMD64"):
        raise BundleError(f"host: needs Linux x86_64, this is {system} {machine}")
    # The script runs on whatever python3 the host has, not only the project's own.
    if sys.version_info < (3, 12):  # noqa: UP036
        version = platform.python_version()
        raise BundleError(f"host: needs python3 >= 3.12 (tarfile's data filter), not {version}")
    if not shutil.which("readelf"):
        raise BundleError("host: needs readelf (binutils) for the glibc check")
    try:
        result = run(["uv", "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise BundleError(f"host: uv {uv_pin} is not on PATH") from exc
    words = (result.stdout or "").split()
    found = words[1] if len(words) > 1 and words[0] == "uv" else (result.stdout or "").strip()
    if found != uv_pin:
        raise BundleError(
            f"host: needs uv {uv_pin} (core-bundle.json), found uv {found or 'nothing'}"
        )
    return found


# -- download and extract ------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(
    url: str,
    sha256: str,
    cache: Path,
    *,
    name: str | None = None,
    opener: Callable = urllib.request.urlopen,
) -> Path:
    """The file at `url`, from `cache` when its hash matches, else downloaded and checked.
    Nothing with another hash is ever left in the cache."""
    cache.mkdir(parents=True, exist_ok=True)
    name = name or urllib.parse.unquote(url.rsplit("/", 1)[-1])
    path = cache / name
    if path.exists():
        if sha256_file(path) == sha256:
            log(f"cached {name}")
            return path
        log(f"cached {name} has another hash; fetching it again")
        path.unlink()
    partial = cache / f"{name}.part"
    digest = hashlib.sha256()
    try:
        with opener(url, timeout=120) as response, open(partial, "wb") as handle:
            for block in iter(lambda: response.read(1 << 20), b""):
                digest.update(block)
                handle.write(block)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise BundleError(f"fetch: {name} has SHA-256 {digest.hexdigest()}, the pin is {sha256}")
    partial.rename(path)
    log(f"fetched {name}")
    return path


def extract(archive: Path, dest: Path) -> Path:
    """Unpack a python-build-standalone archive into `dest`; returns `dest/python`. Every
    member must be under python/, with no absolute path and no `..`; the data filter then
    refuses links that leave `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            parts = PurePosixPath(member.name.replace("\\", "/")).parts
            if (
                member.name.startswith(("/", "\\"))
                or not parts
                or parts[0] != "python"
                or ".." in parts
            ):
                raise BundleError(
                    f"extract: {archive.name} has a member outside python/: {member.name!r}"
                )
        tar.extractall(dest, filter="data")
    root = dest / "python"
    if not root.is_dir():
        raise BundleError(f"extract: {archive.name} has no python/ folder")
    return root


def host_interpreter(
    pins: dict, out_root: Path, downloads: Path, *, opener: Callable = urllib.request.urlopen
) -> Path:
    """The unpruned linux-x64 interpreter under <out-root>/.host, extracted once per pin."""
    sha = pins["targets"][HOST_TARGET]["sha256"]
    host = out_root / ".host"
    marker = host / "sha256"
    python = host / "python" / LAYOUT["posix"]["interpreter"]
    if marker.is_file() and marker.read_text().strip() == sha and python.is_file():
        return python
    archive = fetch(archive_url(pins, HOST_TARGET), sha, downloads, opener=opener)
    shutil.rmtree(host, ignore_errors=True)
    extract(archive, host)
    marker.write_text(sha + "\n")
    return python


# -- prune -------------------------------------------------------------------------------------


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def prune(root: Path, layout: str) -> list[str]:
    """Remove the layout's prune list, then every symlink and __pycache__ left. Returns the
    globs that matched nothing: a bigger bundle, not a broken one, so they are recorded,
    not fatal."""
    keep = KEEP[layout]
    not_found = []
    for pattern in PRUNE[layout]:
        matches = [
            p for p in sorted(root.glob(pattern)) if p.relative_to(root).as_posix() not in keep
        ]
        if not matches:
            not_found.append(pattern)
        for path in matches:
            _remove(path)
    for path in [p for p in walk(root) if p.is_symlink()]:
        path.unlink()
    remove_bytecode(root)
    interpreter = root / LAYOUT[layout]["interpreter"]
    encodings = root / LAYOUT[layout]["stdlib"] / "encodings" / "__init__.py"
    for needed in (interpreter, encodings):
        if not needed.is_file():
            raise BundleError(f"prune: {needed.relative_to(root)} is gone; the prune list is wrong")
    return not_found


# -- install -----------------------------------------------------------------------------------


def site_packages(root: Path, layout: str) -> Path:
    return root / LAYOUT[layout]["stdlib"] / "site-packages"


def export_argv(output: Path) -> list[str]:
    return [
        "uv", "export", "--quiet", "--no-config", "--frozen", "--no-dev", "--no-emit-project",
        "--format", "requirements.txt", "--no-header", "-o", str(output),
    ]  # fmt: skip


def build_argv(host_python: Path, out_dir: Path, repo: Path = REPO) -> list[str]:
    # --no-sources keeps the voice extra's path source out of the build; the build
    # backend comes only from the hashed constraints.
    return [
        "uv", "build", "--wheel", "--no-config", "--no-sources", "--python", str(host_python),
        "--build-constraints", str(BUILD_CONSTRAINTS), "--require-hashes",
        "--out-dir", str(out_dir), str(repo),
    ]  # fmt: skip


def install_argv(
    host_python: Path,
    target_dir: Path,
    wheel_platform: str,
    *,
    requirements: Path | None = None,
    wheel: Path | None = None,
) -> list[str]:
    """One install into `target_dir` for another platform. With `requirements`, every
    package must match its hash; the local wheel's hash is recorded instead."""
    argv = [
        "uv", "pip", "install", "--no-config", "--python", str(host_python),
        "--target", str(target_dir), "--python-platform", wheel_platform,
        "--python-version", "3.12", "--only-binary", ":all:", "--no-deps",
        # Copies, never links into uv's cache: the RECORD edits below must not reach it.
        "--link-mode", "copy",
    ]  # fmt: skip
    if requirements is not None:
        argv += ["--require-hashes", "-r", str(requirements)]
    if wheel is not None:
        argv.append(str(wheel))
    return argv


def install_env(base: dict[str, str], pins: dict, target: str) -> dict[str, str]:
    """The environment for uv: nothing from PYTHON* or UV_* but the offline switch and the
    cache location, and the macOS deployment target for a mac target."""
    keep = {"UV_OFFLINE", "UV_CACHE_DIR"}
    env = {k: v for k, v in base.items() if not (k.startswith(("PYTHON", "UV_")) and k not in keep)}
    env.pop("MACOSX_DEPLOYMENT_TARGET", None)
    if target.startswith("mac-"):
        env["MACOSX_DEPLOYMENT_TARGET"] = pins["macos_deployment_target"]
    return env


def run_checked(
    argv: list[str], *, step: str, cwd: Path | None = None, env: dict | None = None
) -> None:
    result = subprocess.run(argv, cwd=cwd, env=env, check=False)
    if result.returncode != 0:
        raise BundleError(f"{step}: `{' '.join(argv)}` exited {result.returncode}")


def build_wheel(host_python: Path, out_dir: Path, env: dict[str, str]) -> Path:
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    run_checked(build_argv(host_python, out_dir), step="build_wheel", cwd=REPO, env=env)
    wheels = sorted(out_dir.glob("cogito-*-py3-none-any.whl"))
    if len(wheels) != 1:
        raise BundleError(
            f"build_wheel: expected one cogito py3-none-any wheel, found {[w.name for w in wheels]}"
        )
    return wheels[0]


#: A Requires-Dist marker that applies only when an extra is asked for.
EXTRA_MARKER = re.compile(r"\bextra\s*==")


def declared_requirements(wheel: Path) -> list[str]:
    """The Requires-Dist lines of the wheel's METADATA, except those only an extra asks for."""
    with zipfile.ZipFile(wheel) as archive:
        names = [n for n in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
        if len(names) != 1:
            raise BundleError(
                f"check_dependencies: {wheel.name} has {len(names)} METADATA files, not 1"
            )
        headers = HeaderParser().parsestr(archive.read(names[0]).decode("utf-8"))
    declared = [
        r.strip() for r in headers.get_all("Requires-Dist") or [] if not EXTRA_MARKER.search(r)
    ]
    if not declared:
        raise BundleError(f"check_dependencies: {wheel.name} declares no dependencies")
    return declared


def dependencies_argv(
    host_python: Path, target_dir: Path, wheel_platform: str, requirements: Path
) -> list[str]:
    # A dry run with no index and no network: it passes only if what is already installed
    # satisfies every requirement, transitive ones and markers for the target included.
    return [
        "uv", "pip", "install", "--no-config", "--dry-run", "--offline", "--no-index",
        "--python", str(host_python), "--target", str(target_dir),
        "--python-platform", wheel_platform, "--python-version", "3.12",
        "--only-binary", ":all:", "-r", str(requirements),
    ]  # fmt: skip


def check_dependencies(
    host_python: Path,
    site: Path,
    wheel_platform: str,
    wheel: Path,
    scratch: Path,
    env: dict[str, str],
) -> list[str]:
    """Refuse a site-packages that does not satisfy the wheel's own dependencies. The
    install takes uv.lock as it is (--frozen, --no-deps), so this is where a lock older
    than pyproject.toml is caught. Leaves uv's .lock in `site` for `normalize`."""
    declared = declared_requirements(wheel)
    listing = scratch / "declared-requirements.txt"
    listing.write_text("".join(f"{line}\n" for line in declared), encoding="utf-8")
    try:
        result = subprocess.run(
            dependencies_argv(host_python, site, wheel_platform, listing), env=env, check=False
        )
    finally:
        listing.unlink(missing_ok=True)
    if result.returncode != 0:
        raise BundleError(
            f"check_dependencies: the packages installed from uv.lock do not satisfy {wheel.name} "
            "(uv's reason is above). If pyproject.toml changed, run `uv lock` and commit uv.lock"
        )
    return declared


def normalize(site: Path) -> list[str]:
    """Remove what uv writes that must not ship: console scripts whose shebang is the build
    interpreter, the dist-info files in NOT_SHIPPED_METADATA and the target's lock file.
    Their RECORD lines go too. Returns the removed paths, relative to `site`."""
    removed = []
    lock = site / TARGET_LOCK
    if lock.is_file():
        lock.unlink()
        removed.append(TARGET_LOCK)
    scripts = site / "bin"
    if scripts.exists():
        removed += [p.relative_to(site).as_posix() for p in files_under(scripts)]
        shutil.rmtree(scripts)
    for info in sorted(site.glob("*.dist-info")):
        for name in NOT_SHIPPED_METADATA:
            path = info / name
            if path.exists():
                path.unlink()
                removed.append(path.relative_to(site).as_posix())
    gone = set(removed)
    for record in sorted(site.glob("*.dist-info/RECORD")):
        lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = []
        for line in lines:
            fields = next(csv.reader(io.StringIO(line)), [""])
            entry = os.path.normpath(fields[0]).replace(os.sep, "/") if fields and fields[0] else ""
            if entry in gone or entry.startswith("bin/"):
                continue
            kept.append(line)
        if kept != lines:
            # A new file, never a write through an existing inode.
            fresh = record.with_name("RECORD.new")
            fresh.write_text("".join(kept), encoding="utf-8")
            os.replace(fresh, record)
    return removed


# -- bytecode ----------------------------------------------------------------------------------


def compile_argv(host_python: Path, root: Path, stdlib: Path) -> list[str]:
    # -B: the compiler's own imports write no timestamp .pyc that -f would then skip.
    # -s: relative co_filename, so two builds in two folders give the same bytes.
    return [
        str(host_python), "-B", "-m", "compileall", "-f", "-q", "-j0",
        "--invalidation-mode", "unchecked-hash", "-s", str(root), str(stdlib),
    ]  # fmt: skip


def compile_env() -> dict[str, str]:
    # Exactly this, and no -I, which would ignore PYTHONHASHSEED (set and frozenset
    # constants are ordered by it).
    return {"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0", "LANG": "C.UTF-8"}


def remove_bytecode(root: Path) -> None:
    for cache in [
        p for p in walk(root) if p.name == "__pycache__" and p.is_dir() and not p.is_symlink()
    ]:
        shutil.rmtree(cache)


def compile_bytecode(host_python: Path, root: Path, layout: str) -> None:
    remove_bytecode(root)
    stdlib = root / LAYOUT[layout]["stdlib"]
    run_checked(compile_argv(host_python, root, stdlib), step="compile_bytecode", env=compile_env())


# -- checks ------------------------------------------------------------------------------------


def classify_native(head: bytes) -> tuple[str, frozenset[int]] | None:
    """(format, machines) for an ELF, Mach-O (thin or fat) or PE header, else None. `head`
    is the file's first bytes; a PE needs its header, which `native_header` reads."""
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        order = "<" if head[5] == 1 else ">"
        return "elf", frozenset({struct.unpack_from(f"{order}H", head, 18)[0]})
    magic = head[:4]
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") and len(head) >= 8:
        return "macho", frozenset({struct.unpack_from("<I", head, 4)[0]})
    if magic in (b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce") and len(head) >= 8:
        return "macho", frozenset({struct.unpack_from(">I", head, 4)[0]})
    if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf") and len(head) >= 8:
        count = struct.unpack_from(">I", head, 4)[0]
        # A Java class file shares this magic; its next word is a version number >= 45.
        if not 0 < count < 20:
            return None
        size = 20 if magic == b"\xca\xfe\xba\xbe" else 32
        machines = set()
        for index in range(count):
            offset = 8 + index * size
            if len(head) < offset + 4:
                break
            machines.add(struct.unpack_from(">I", head, offset)[0])
        return "macho", frozenset(machines)
    if head[:2] == b"MZ" and len(head) >= 0x40:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if len(head) >= pe + 6 and head[pe : pe + 4] == b"PE\0\0":
            return "pe", frozenset({struct.unpack_from("<H", head, pe + 4)[0]})
    return None


def native_header(path: Path) -> bytes:
    """Enough of a file to classify it: 4 KiB covers ELF, Mach-O fat tables and the PE
    header of every DLL and EXE seen in these archives."""
    with open(path, "rb") as handle:
        return handle.read(4096)


def glibc_needed(path: Path, *, run: Callable = subprocess.run) -> tuple[int, ...]:
    """The highest GLIBC_x.y version `path` needs, from `readelf -V`."""
    result = run(["readelf", "-V", str(path)], capture_output=True, text=True, check=False)
    versions = [
        tuple(int(part) for part in match.group(1).split("."))
        for match in re.finditer(r"Name: GLIBC_(\d+(?:\.\d+)+)", result.stdout or "")
    ]
    return max(versions, default=())


def files_under(root: Path) -> list[Path]:
    """Every file and symlink under `root`, sorted."""
    return sorted(p for p in walk(root) if p.is_symlink() or p.is_file())


def check_tree(root: Path, target: str, layout: str, *, run: Callable = subprocess.run) -> dict:
    """Refuse a tree that would not run, or not run on its target. Returns counts."""
    problems: list[str] = []
    interpreter = root / LAYOUT[layout]["interpreter"]
    if not interpreter.is_file():
        problems.append(f"no interpreter at {interpreter.relative_to(root)}")
    elif layout == "posix" and not os.access(interpreter, os.X_OK):
        problems.append(f"{interpreter.relative_to(root)} is not executable")
    site = site_packages(root, layout)
    for needed in ("cogito/cli.py", "cogito/guard/shims/client.py"):
        if not (site / needed).is_file():
            problems.append(f"site-packages has no {needed}")

    stdlib = root / LAYOUT[layout]["stdlib"]
    files = files_under(root)
    natives = 0
    expected_format, expected_machine = NATIVE[target]
    for path in files:
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            problems.append(f"{rel} is a symlink")
            continue
        if layout == "posix" and path.stat().st_mode & stat.S_IWOTH:
            problems.append(f"{rel} is writable by every user")
        if layout == "windows" and len(rel) > WINDOWS_PATH_LIMIT:
            problems.append(f"{rel} is {len(rel)} characters, over {WINDOWS_PATH_LIMIT}")
        if path.suffix == ".py" and path.is_relative_to(stdlib):
            pyc = path.parent / "__pycache__" / f"{path.stem}.cpython-312.pyc"
            if not pyc.is_file():
                problems.append(f"{rel} has no bytecode")
        if path.suffix == ".pyc":
            header = path.read_bytes()[:8]
            flags = struct.unpack_from("<I", header, 4)[0] if len(header) == 8 else None
            if flags != 1:
                problems.append(f"{rel} is not unchecked-hash bytecode (flags {flags})")
            continue
        native = classify_native(native_header(path))
        if native is None:
            continue
        natives += 1
        kind, machines = native
        if kind != expected_format or expected_machine not in machines:
            problems.append(f"{rel} is {kind} for {sorted(hex(m) for m in machines)}, not {target}")
        elif target == "linux-x64":
            needed = glibc_needed(path, run=run)
            if needed > GLIBC_CEILING:
                version = ".".join(map(str, needed))
                problems.append(
                    f"{rel} needs GLIBC_{version}, above {'.'.join(map(str, GLIBC_CEILING))}"
                )
    if problems:
        shown = "\n  ".join(problems[:40])
        more = f"\n  ... and {len(problems) - 40} more" if len(problems) > 40 else ""
        raise BundleError(f"check_tree: {len(problems)} problem(s) in {target}:\n  {shown}{more}")
    return {"files": len(files), "native": natives}


# -- provenance --------------------------------------------------------------------------------


def is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def tree_digest(entries: dict[str, tuple[str, bool]]) -> str:
    """One SHA-256 over every (path, exec bit, file SHA-256), in path order."""
    digest = hashlib.sha256()
    for rel in sorted(entries):
        sha, executable = entries[rel]
        digest.update(f"{rel}\0{'x' if executable else '-'}\0{sha}\n".encode())
    return digest.hexdigest()


def write_provenance(
    root: Path,
    destination: Path,
    *,
    target: str,
    pins: dict,
    uv_version: str,
    requirements: Path,
    wheel: Path,
    not_found: list[str],
) -> dict:
    entries = {
        p.relative_to(root).as_posix(): (sha256_file(p), is_executable(p))
        for p in files_under(root)
    }
    record = {
        "target": target,
        "python": pins["python"],
        "pin": pins["targets"][target],
        "uv": uv_version,
        "macos_deployment_target": pins["macos_deployment_target"]
        if target.startswith("mac-")
        else None,
        "requirements_sha256": sha256_file(requirements),
        "wheel": {"name": wheel.name, "sha256": sha256_file(wheel)},
        "not_found": not_found,
        "count": len(entries),
        "bytes": sum((root / rel).stat().st_size for rel in entries),
        "executables": sorted(rel for rel, (_, executable) in entries.items() if executable),
        "files": {rel: sha for rel, (sha, _) in sorted(entries.items())},
        "digest": tree_digest(entries),
    }
    destination.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return record


# -- main --------------------------------------------------------------------------------------


def assemble(target: str, out_root: Path, downloads: Path, wheel: Path | None) -> dict:
    with step("load_pins"):
        pins = load_pins()
    with step("check_host"):
        uv_version = check_host(pins["uv"])
    layout = pins["targets"][target]["layout"]
    platform_tag = pins["targets"][target]["wheel_platform"]
    out_root.mkdir(parents=True, exist_ok=True)
    final = out_root / target
    staging = out_root / f".staging-{target}"
    shutil.rmtree(final, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        with step("fetch"):
            host = host_interpreter(pins, out_root, downloads)
            archive = fetch(archive_url(pins, target), pins["targets"][target]["sha256"], downloads)
        with step("extract"):
            root = extract(archive, staging)
        with step("prune"):
            not_found = prune(root, layout)
        uv_env = install_env(dict(os.environ), pins, target)
        requirements = staging / "requirements.txt"
        with step("export_requirements"):
            run_checked(export_argv(requirements), step="export_requirements", cwd=REPO, env=uv_env)
        with step("build_wheel"):
            if wheel is None:
                wheel = build_wheel(
                    host, out_root / "wheel", install_env(dict(os.environ), pins, HOST_TARGET)
                )
            elif not re.fullmatch(r"cogito-[^-]+-py3-none-any\.whl", wheel.name):
                raise BundleError(f"--wheel: {wheel.name} is not a cogito py3-none-any wheel")
        site = site_packages(root, layout)
        with step(f"install for {platform_tag}"):
            run_checked(
                install_argv(host, site, platform_tag, requirements=requirements),
                step="install",
                env=uv_env,
            )
            run_checked(
                install_argv(host, site, platform_tag, wheel=wheel), step="install", env=uv_env
            )
        with step("check_dependencies"):
            check_dependencies(host, site, platform_tag, wheel, staging, uv_env)
        with step("normalize"):
            normalize(site)
        with step("compile_bytecode"):
            compile_bytecode(host, root, layout)
        with step("check_tree"):
            counts = check_tree(root, target, layout)
        with step("write_provenance"):
            record = write_provenance(
                root,
                staging / "bundle.json",
                target=target,
                pins=pins,
                uv_version=uv_version,
                requirements=requirements,
                wheel=wheel,
                not_found=not_found,
            )
            requirements.unlink()
            staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    record["native"] = counts["native"]
    return record


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("--out-root", type=Path, default=DESKTOP / "build" / "core")
    parser.add_argument("--downloads", type=Path, default=None)
    parser.add_argument("--wheel", type=Path, default=None)
    args = parser.parse_args(None if argv is None else list(argv))
    out_root = args.out_root.resolve()
    downloads = (args.downloads or out_root / "downloads").resolve()
    started = time.monotonic()
    try:
        record = assemble(
            args.target, out_root, downloads, args.wheel.resolve() if args.wheel else None
        )
    except BundleError as exc:
        print(f"bundle-core: {exc}", file=sys.stderr)
        return 1
    megabytes = record["bytes"] / 1e6
    print(
        f"bundle-core: {record['target']}: {record['count']} files, {megabytes:.1f} MB, "
        f"{record['native']} native, digest {record['digest']}, {time.monotonic() - started:.0f} s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
