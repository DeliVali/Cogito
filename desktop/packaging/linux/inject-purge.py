"""Put the purge body into a built .deb's postrm.

fpm 1.17 (bundled by electron-builder 26, fpm@2.1.4) declares --deb-after-purge but never
loads the file: its command only reads the before/after install, remove and upgrade
scripts, so postrm's `after_purge()` stays `:` and a purge leaves the APT source and
keyring behind. The release workflow unpacks the .deb, runs this on DEBIAN/postrm, and
packs it again.

    python3 inject-purge.py <postrm> <after-purge.sh>

Refuses, rather than guessing, when postrm has no empty `after_purge()` to fill.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: fpm's postrm_upgrade.sh.erb renders an empty purge step exactly like this.
EMPTY = "after_purge() {\n    :\n}\n"


def inject(postrm: str, body: str) -> str:
    if postrm.count(EMPTY) != 1:
        raise ValueError("postrm has no single empty after_purge() to fill")
    lines = [line for line in body.splitlines() if line and not line.startswith("#!")]
    indented = "".join(f"    {line}\n" for line in lines)
    return postrm.replace(EMPTY, f"after_purge() {{\n    :\n{indented}}}\n")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    postrm, body = Path(argv[1]), Path(argv[2])
    try:
        postrm.write_text(inject(postrm.read_text(), body.read_text()))
    except ValueError as exc:
        print(f"inject-purge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
