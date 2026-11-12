"""GPU use, shown only when the system reports it (design 2b).

Read from the vendor's tool in a subprocess with a timeout: `nvidia-smi
--query-gpu=utilization.gpu --format=csv,noheader,nounits` (one percentage per card), else
on Linux `rocm-smi --showuse --json` (`"GPU use (%)"` per card). Both only query the
driver. With several cards the busiest is reported, since it is the one running the model.
macOS has neither tool. Any failure (missing tool, timeout, unparseable output) means "not
reported", never a guess.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

#: Seconds either tool may take before the menu goes on without a GPU row.
TIMEOUT = 2.0


def _numbers(values: list[str]) -> list[int]:
    found: list[int] = []
    for value in values:
        try:
            number = int(float(str(value).strip().rstrip("%").strip()))
        except ValueError:
            continue
        if 0 <= number <= 100:
            found.append(number)
    return found


def _ask(argv: list[str], run: Callable[..., Any]) -> str | None:
    try:
        result = run(argv, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 0) != 0:
        return None
    return result.stdout or ""


def _nvidia(which: Callable[[str], str | None], run: Callable[..., Any]) -> int | None:
    if not which("nvidia-smi"):
        return None
    output = _ask(
        ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], run
    )
    numbers = _numbers(output.splitlines()) if output is not None else []
    return max(numbers) if numbers else None


def _rocm(which: Callable[[str], str | None], run: Callable[..., Any]) -> int | None:
    if not which("rocm-smi"):
        return None
    output = _ask(["rocm-smi", "--showuse", "--json"], run)
    if output is None:
        return None
    try:
        report = json.loads(output)
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    values = [card.get("GPU use (%)") for card in report.values() if isinstance(card, dict)]
    numbers = _numbers([value for value in values if value is not None])
    return max(numbers) if numbers else None


def gpu_use(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
    platform: str = sys.platform,
) -> int | None:
    """The busiest card's use in percent, or None when the system does not report one."""
    if platform == "darwin":
        return None
    use = _nvidia(which, run)
    if use is None and platform.startswith("linux"):
        use = _rocm(which, run)
    return use


class GpuMeter:
    """`gpu_use`, read at most once per `ttl` seconds so tray polling does not hit the driver."""

    def __init__(
        self,
        read: Callable[[], int | None] = gpu_use,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = 5.0,
    ) -> None:
        self._read = read
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: int | None = None
        self._at: float | None = None

    def __call__(self) -> int | None:
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._ttl:
                self._value = self._read()
                self._at = now
            return self._value
