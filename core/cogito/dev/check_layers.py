"""Repository invariant: imports only flow downwards through the layers.

Run with ``python -m cogito.dev.check_layers``. Exits non-zero on any violation.
The allowed edges below copy CONTRIBUTING.md section 2. Change both in the same commit.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = PACKAGE_ROOT.name

#: Leaf layers with no dependencies. Any layer may import them.
UNIVERSAL = frozenset({"cancel", "config", "errors", "ids", "oslocks", "procs"})

#: layer -> layers it may import, on top of UNIVERSAL.
ALLOWED: dict[str, frozenset[str]] = {
    "config": frozenset(),
    "errors": frozenset(),
    "ids": frozenset(),
    "cancel": frozenset(),
    "oslocks": frozenset(),
    "procs": frozenset(),
    "graph": frozenset(),
    "provider": frozenset({"graph"}),
    "guard": frozenset({"graph"}),
    "context": frozenset({"graph", "provider"}),
    "tools": frozenset({"graph", "provider", "context", "guard"}),
    "loop": frozenset({"graph", "provider", "tools", "context"}),
    "server": frozenset({"graph", "loop"}),
    "setup": frozenset(),
    "dev": frozenset(),
    # The composition root wires the layers together. COMPOSITION_ROOTS exempts it.
    "cli": frozenset(),
}

#: Exempt layers: they exist to assemble everything else.
COMPOSITION_ROOTS = frozenset({"cli"})


def layer_of(module_path: Path) -> str:
    """Return the layer a source file belongs to, from its path inside the package."""
    relative = module_path.relative_to(PACKAGE_ROOT)
    if len(relative.parts) == 1:
        return relative.stem
    return relative.parts[0]


def imported_layers(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """Yield ``(layer, lineno)`` for every in-package import in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{PACKAGE_NAME}."):
                    yield alias.name.split(".")[1], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: stays inside its own layer
                continue
            if node.module and node.module.startswith(f"{PACKAGE_NAME}."):
                yield node.module.split(".")[1], node.lineno


def violations() -> list[str]:
    found: list[str] = []
    for source in sorted(PACKAGE_ROOT.rglob("*.py")):
        if source.parent == PACKAGE_ROOT and source.name == "__init__.py":
            continue  # the package root declares no layer of its own
        origin = layer_of(source)
        if origin in COMPOSITION_ROOTS:
            continue
        permitted = ALLOWED.get(origin)
        if permitted is None:
            found.append(f"{source}: unknown layer {origin!r} — add it to CONTRIBUTING.md and here")
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for target, lineno in imported_layers(tree):
            if target == origin or target in UNIVERSAL or target in permitted:
                continue
            found.append(
                f"{source}:{lineno}: {origin!r} may not import {target!r} "
                f"(allowed: {', '.join(sorted(permitted | UNIVERSAL)) or 'nothing'})"
            )
    return found


def main() -> int:
    found = violations()
    for message in found:
        print(message, file=sys.stderr)
    if found:
        print(f"\n{len(found)} layer violation(s).", file=sys.stderr)
        return 1
    print("Layers OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
