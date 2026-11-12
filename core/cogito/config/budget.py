"""COGITO's own spending budget, stored under ``[budget]`` in config.toml.

The person's limit, counted by COGITO (`provider.budget`), never a figure a provider reported::

    [budget]
    monthly = 10.00     # optional; per local calendar month
    daily = 2.00        # optional; per local calendar day
    currency = "USD"    # three-letter ISO 4217, the currency of the models' prices

With neither ``monthly`` nor ``daily`` there is no budget and sessions are unaffected. Kept
out of ``Settings`` for the reason `config/search.py` gives: ``Settings.fingerprint()`` goes
into a benchmark run header, and benchmarks never run through the metered server this
budget lives in. ``settings.parse`` ignores unknown tables.

Saving rewrites only the ``[budget]`` table. Every other line of the file, comments
included, is kept byte for byte.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cogito.config import paths
from cogito.config.tomlfile import quote, read, write_atomically
from cogito.errors import ConfigError

_TABLE = "budget"
_HEADER = re.compile(r"^\s*\[\s*budget\s*\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
#: The same rule `config.models` applies to a price's currency.
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_LIMITS = ("monthly", "daily")
_KEYS = (*_LIMITS, "currency")
DEFAULT_CURRENCY = "USD"


@dataclass(frozen=True, slots=True)
class Budget:
    """What the person allows COGITO to spend on API models. None is no limit."""

    monthly: float | None = None
    daily: float | None = None
    currency: str = DEFAULT_CURRENCY

    @property
    def configured(self) -> bool:
        return self.monthly is not None or self.daily is not None

    def as_dict(self) -> dict[str, float | str | None]:
        return {"monthly": self.monthly, "daily": self.daily, "currency": self.currency}


def _amount(key: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"[{_TABLE}].{key}: expected an amount of the currency, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"[{_TABLE}].{key}: must be more than zero, got {value!r}")
    return float(value)


def validate(values: Mapping[str, object], *, allow_none: bool) -> dict[str, object]:
    """``values`` as a checked mapping. ``None`` for a limit removes it and is accepted
    only from a caller (the file cannot express it). Unknown keys are refused by name."""
    checked: dict[str, object] = {}
    for key, value in values.items():
        if key not in _KEYS:
            raise ConfigError(f"[{_TABLE}]: unknown key {key!r}; known: {', '.join(_KEYS)}")
        if key == "currency":
            if not isinstance(value, str) or not _CURRENCY.match(value):
                raise ConfigError(
                    f'[{_TABLE}].currency: must be a three-letter ISO 4217 code, such as "USD"'
                )
            checked[key] = value
            continue
        if value is None and not allow_none:
            raise ConfigError(f"[{_TABLE}].{key}: expected an amount of the currency")
        checked[key] = _amount(key, value)
    return checked


def load(path: Path | None = None) -> Budget:
    """The stored budget. No table means no budget."""
    target = path or paths.settings_file()
    table = read(target).get(_TABLE, {})
    if not isinstance(table, dict):
        raise ConfigError(f"{target}: [{_TABLE}] must be a table")
    checked = validate(table, allow_none=False)
    return Budget(
        monthly=checked.get("monthly"),  # type: ignore[arg-type]
        daily=checked.get("daily"),  # type: ignore[arg-type]
        currency=str(checked.get("currency", DEFAULT_CURRENCY)),
    )


def save(values: Mapping[str, object], path: Path | None = None) -> Budget:
    """Merge ``values`` into what is stored and write it back; ``None`` removes a limit.
    Returns the whole saved budget."""
    target = path or paths.settings_file()
    merged = {**load(target).as_dict(), **validate(values, allow_none=True)}
    saved = Budget(
        monthly=merged["monthly"],  # type: ignore[arg-type]
        daily=merged["daily"],  # type: ignore[arg-type]
        currency=str(merged["currency"]),
    )
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    write_atomically(target, _replace_table(original, saved))
    return saved


def _number(value: float) -> str:
    """Two decimals, as a person types money; more only when the amount needs them."""
    text = f"{value:.2f}"
    return text if float(text) == value else repr(value)


def _replace_table(text: str, budget: Budget) -> str:
    body = [
        f"{key} = {_number(amount)}\n"
        for key in _LIMITS
        if (amount := getattr(budget, key)) is not None
    ]
    body.append(f"currency = {quote(budget.currency)}\n")
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _HEADER.match(line)), None)
    if start is None:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        separator = "\n" if prefix.strip() else ""
        return f"{prefix}{separator}[{_TABLE}]\n{''.join(body)}"
    end = next((i for i in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
    # Keep the blank line that separated the table from the next one, if there was one.
    trailing = "\n" if end < len(lines) else ""
    return "".join([*lines[: start + 1], *body, trailing, *lines[end:]])
