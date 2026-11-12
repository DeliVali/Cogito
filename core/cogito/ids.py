"""Identifier minting.

Identifiers sort lexicographically by creation time, so the graph can order nodes without
a timestamp index and WAL replay is deterministic.
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
_RANDOM_BYTES = 10


def _encode(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_id() -> str:
    """Return a 26-character ULID: 48 bits of milliseconds, 80 bits of randomness."""
    timestamp = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(_RANDOM_BYTES), "big")
    return _encode(timestamp, 10) + _encode(randomness, 16)
