#!/usr/bin/env python3
"""The program the agent's shell runs instead of `rm`.

It runs inside the agent's shell, which may have another interpreter and virtualenv
active, so it imports nothing from Cogito or PyPI. It asks the broker over a unix socket
and does what it is told (loopback TCP with a session secret on Windows).

One file serves every verb in the package's `SHIMMED` (rm, rmdir, shred, srm, unlink,
truncate); the shim passes the verb it stands in for.
"""

from __future__ import annotations

import json
import os
import socket
import sys

_SOCKET = "COGITO_GUARD_SOCKET"
#: None: the broker decides and this side only waits, as long as the person takes to answer
#: (the app waits without a deadline, server/approvals.py). Giving up first would race the
#: broker, and the loser is a file. A broker that goes away closes the socket, which ends
#: the wait.
_TIMEOUT = None


def main(argv: list[str]) -> int:
    # The shim passes the verb as the first argument, because a shell `exec` sets argv[0]
    # to the interpreter's path, not the script's name.
    verb = argv[1] if len(argv) > 1 else "rm"
    arguments = argv[2:]
    address = os.environ.get(_SOCKET, "")
    if not address:
        # No broker: refuse rather than fall through to the real binary. A shim that
        # quietly does the destructive thing when it cannot ask is worse than no shim.
        sys.stderr.write(
            f"{verb}: cogito's guard is not running, so this was not carried out\n"
        )
        return 1

    body: dict[str, object] = {"verb": verb, "cwd": os.getcwd(), "args": arguments}
    target: object = address
    family = getattr(socket, "AF_UNIX", None)
    if address.startswith("tcp:"):
        # Windows: loopback TCP and the session's secret, since Python has no AF_UNIX there.
        _, host, port, token = address.split(":", 3)
        target, family, body["token"] = (host, int(port)), socket.AF_INET, token
    request = json.dumps(body, ensure_ascii=False)
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(_TIMEOUT)
            connection.connect(target)
            connection.sendall(request.encode("utf-8") + b"\n")
            chunks: list[bytes] = []
            while not chunks or not chunks[-1].endswith(b"\n"):
                received = connection.recv(65536)
                if not received:
                    break
                chunks.append(received)
        reply = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{verb}: could not reach cogito's guard ({exc})\n")
        return 1

    if message := reply.get("message"):
        stream = sys.stdout if reply.get("allowed") else sys.stderr
        stream.write(message.rstrip("\n") + "\n")
    return 0 if reply.get("allowed") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
