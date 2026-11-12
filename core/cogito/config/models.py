"""The model catalog: a selectable list, loaded from ``models.toml``.

Every model is reached over an API host, local ones included, so there is one kind of
entry and two wire dialects. An entry with ``serve`` is one Cogito starts itself (Cogito
Motoris); after that it is addressed over ``base_url`` like any other.

Adding a provider means adding a row to the file, not writing code.
"""

from __future__ import annotations

import contextlib
import math
import re
import stat
import threading
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from cogito.errors import ConfigError

WIRE_DIALECTS = frozenset({"openai", "anthropic"})
#: `motoris` is Cogito Motoris (a Strata fork, ~/Proyectos/cogito-motoris), the one engine
#: Cogito starts; its entry names the checkout (`args.motoris`) and the engine JSON its
#: setup wrote (`args.config`, absolute or relative to the checkout).
SERVERS = frozenset({"motoris"})


def max_context_arg(args: object) -> int | None:
    """The ``--max-context`` an engine config's ``args`` list names, or None."""
    if not isinstance(args, list):
        return None
    for index, item in enumerate(args[:-1]):
        if item == "--max-context":
            try:
                value = int(str(args[index + 1]))
            except ValueError:
                return None
            return value if value > 0 else None
    return None


def _motoris_config(args: Mapping[str, object]) -> Mapping | None:
    """A Motoris entry's engine config (``args.config``, absolute or relative to
    ``args.motoris``), or None when the file cannot be read."""
    import json

    path = Path(str(args.get("config", ""))).expanduser()
    if not path.is_absolute():
        path = Path(str(args.get("motoris", ""))).expanduser() / path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, Mapping) else None


def motoris_max_context(args: Mapping[str, object]) -> int | None:
    """The window a Motoris entry's engine config runs, or None when the file cannot be
    read or names none."""
    document = _motoris_config(args)
    return max_context_arg(document.get("args")) if document is not None else None


def motoris_parallel(args: Mapping[str, object]) -> int | None:
    """How many requests a Motoris entry's engine decodes at once: its config's
    ``"parallel"``, 1 when it sets none (one at a time is Motoris's default,
    cogito-motoris docs/BATCHING.md), None when the file cannot be read. The engine may run
    fewer than asked; it says so in its log and `/v1/status`."""
    document = _motoris_config(args)
    if document is None:
        return None
    value = document.get("parallel", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: An ISO 4217 code, so the window formats a cost with it and never guesses a symbol.
_CURRENCY = re.compile(r"^[A-Z]{3}$")

#: A label's trailing mention of where the model runs ("- local", "· API", "(local)"),
#: which `ModelEntry.display_label` drops.
_ORIGIN_SUFFIX = re.compile(r"\s*(?:[-\u2013\u2014\u00b7|]\s*(local|api)|\((local|api)\))\s*$", re.IGNORECASE)
_PRICE_KEYS = (
    "input_per_mtok",
    "output_per_mtok",
    "currency",
    "cache_read_per_mtok",
    "cache_write_per_mtok",
    "above_tokens",
    "above_input_per_mtok",
    "above_output_per_mtok",
)
#: The three keys of a second price tier, written all together or not at all.
_TIER_KEYS = ("above_tokens", "above_input_per_mtok", "above_output_per_mtok")

#: Prompt-cache read and write prices relative to the input price, when an entry's price
#: does not set them: reads ~0.1x, writes 1.25x for the default 5-minute TTL. Anthropic's
#: figures, not ours (`shared/prompt-caching.md`, "Economics", in the claude-api reference
#: bundled with Claude Code 2.1.280, read 2026-09-29). The same section lists a model with
#: cheaper reads (0.025x on Claude Fable 5.1). `cache_read_per_mtok` covers that: a rate
#: copied from the price list always wins over the factor.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


@dataclass(frozen=True, slots=True)
class Price:
    """What one entry costs per million tokens, as the user wrote it in ``models.toml``.

    Optional on purpose: the tray shows a cost only when it is known, and a provider's price
    is never guessed or looked up (design 2b, "costo solo si el precio es conocido"). The two
    cache rates are optional too; when absent, the documented factors of the input rate apply.
    """

    input_per_mtok: float
    output_per_mtok: float
    currency: str
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    #: A second tier: a request whose prompt is longer than ``above_tokens`` is billed at
    #: these two rates instead, its cache reads and writes at the usual factors of the
    #: higher input rate (Claude Haiku 5.5: $0.10/$0.50 up to 100K prompt tokens,
    #: $0.50/$2.50 beyond).
    above_tokens: int | None = None
    above_input_per_mtok: float | None = None
    above_output_per_mtok: float | None = None

    def cost(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float:
        """``input_tokens`` is the whole prompt, as `provider.base.Usage` counts it. The two
        cache counts are the parts of it a prompt cache wrote and served."""
        input_rate, output_rate = self.input_per_mtok, self.output_per_mtok
        read_rate = (
            self.cache_read_per_mtok
            if self.cache_read_per_mtok is not None
            else self.input_per_mtok * CACHE_READ_FACTOR
        )
        write_rate = (
            self.cache_write_per_mtok
            if self.cache_write_per_mtok is not None
            else self.input_per_mtok * CACHE_WRITE_FACTOR
        )
        if (
            self.above_tokens is not None
            and self.above_input_per_mtok is not None
            and self.above_output_per_mtok is not None
            and input_tokens > self.above_tokens
        ):
            input_rate, output_rate = self.above_input_per_mtok, self.above_output_per_mtok
            read_rate = input_rate * CACHE_READ_FACTOR
            write_rate = input_rate * CACHE_WRITE_FACTOR
        uncached = max(0, input_tokens - cache_creation_input_tokens - cache_read_input_tokens)
        spent = (
            uncached * input_rate
            + cache_creation_input_tokens * write_rate
            + cache_read_input_tokens * read_rate
            + output_tokens * output_rate
        )
        return spent / 1_000_000


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One selectable model."""

    id: str
    label: str
    api: str
    base_url: str
    model: str
    api_key_env: str | None = None
    #: A file holding the key (``KEY=VALUE`` or the bare key), readable by its owner only.
    #: Read when the client is created (`config.secrets.model_key`), never stored.
    api_key_file: str | None = None
    serve: str | None = None
    args: Mapping[str, object] = field(default_factory=dict)
    #: Sampling parameters for this entry. Absent means the provider default.
    sampling: Mapping[str, object] = field(default_factory=dict)
    #: Decisions the user fixed by hand. The one read is `grounding_strict`
    #: (`pin = { grounding_strict = false }`), whether the app checks this model's answers.
    pins: Mapping[str, object] = field(default_factory=dict)
    #: Optional; see `Price`. None means the cost is unknown and is never shown.
    price: Price | None = None

    @property
    def is_served_by_cogito(self) -> bool:
        """True when Cogito starts the server (Cogito Motoris) itself."""
        return self.serve is not None

    @property
    def takes_server_extensions(self) -> bool:
        """True when the host takes llama-server's request extensions: the thinking budget,
        `/props`, `/apply-template` and grammars. An entry Cogito starts always does
        (Motoris reads the budget and grammars as llama-server does, but answers
        `/apply-template` with a 404). Any other host opts in with
        `args.server_extensions = true`."""
        return self.serve is not None or self.args.get("server_extensions") is True

    @property
    def is_loopback(self) -> bool:
        return (urlparse(self.base_url).hostname or "") in _LOOPBACK_HOSTS

    @property
    def origin(self) -> str:
        """How the UI groups this entry in the picker."""
        return "local" if self.is_loopback else "api"

    @property
    def display_label(self) -> str:
        """The label as the app shows it, without a trailing word that only repeats where
        the model runs (``"Gemma 4 12B - local"``, ``"Claude Haiku 4.5 · API"``). Every place
        that shows a model name already names its service beside it ("llama.cpp LOCAL · …",
        the menu's «CLAUDE API» section), so the person saw «local» twice in one chip
        (2026-09-29). ``label`` itself is unchanged: ``cogito models list`` prints it and a
        benchmark run records it as configuration."""
        label = self.label or self.id
        found = _ORIGIN_SUFFIX.search(label)
        if found is None or (found.group(1) or found.group(2)).lower() != self.origin:
            return label
        return label[: found.start()].rstrip() or label


@dataclass(frozen=True, slots=True)
class Catalog:
    """The loaded list, plus problems worth showing that are not fatal."""

    entries: tuple[ModelEntry, ...]
    warnings: tuple[str, ...] = ()

    def get(self, model_id: str) -> ModelEntry:
        for entry in self.entries:
            if entry.id == model_id:
                return entry
        known = ", ".join(e.id for e in self.entries) or "none registered"
        raise ConfigError(f"unknown model {model_id!r} (known: {known})")

    def grouped(self) -> dict[str, list[ModelEntry]]:
        """Entries grouped by origin, in the order the picker shows them."""
        groups: dict[str, list[ModelEntry]] = {"local": [], "api": []}
        for entry in self.entries:
            groups[entry.origin].append(entry)
        return groups


def _require(table: Mapping[str, object], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: missing required string field {key!r}")
    return value.strip()


def _rate(price: Mapping[str, object], key: str) -> float:
    value = price.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"price {key} must be a number of the currency per million tokens")
    if not math.isfinite(value) or value < 0:
        raise ConfigError(f"price {key} must be zero or more")
    return float(value)


def _check_pins(raw: object, where: str, warnings: list[str]) -> dict[str, object]:
    """The entry's pins. A wrong or unknown one is reported, like a missing api_key_env,
    instead of stopping the whole catalog from loading."""
    if not isinstance(raw, Mapping):
        warnings.append(f"{where}: pin must be a table")
        return {}
    pins: dict[str, object] = {}
    for key, value in raw.items():
        if key != "grounding_strict":
            warnings.append(f"{where}: pin {key!r} is not read; the one pin is grounding_strict")
        elif not isinstance(value, bool):
            warnings.append(f"{where}: pin grounding_strict must be true or false")
        else:
            pins[key] = value
    return pins


def _parse_price(raw: object) -> Price:
    if not isinstance(raw, Mapping):
        raise ConfigError("price must be a table: { input_per_mtok, output_per_mtok, currency }")
    unknown = sorted(set(raw) - set(_PRICE_KEYS))
    if unknown:
        raise ConfigError(
            f"price has unknown key(s) {', '.join(unknown)}; keys are: {', '.join(_PRICE_KEYS)}"
        )
    currency = raw.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        raise ConfigError('price currency must be a three-letter ISO 4217 code, such as "USD"')
    tier = [key for key in _TIER_KEYS if key in raw]
    if tier and len(tier) != len(_TIER_KEYS):
        raise ConfigError(f"price needs all of {', '.join(_TIER_KEYS)} for a second tier, or none")
    above = raw.get("above_tokens")
    if tier and (isinstance(above, bool) or not isinstance(above, int) or above <= 0):
        raise ConfigError("price above_tokens must be a whole number of tokens above zero")
    return Price(
        input_per_mtok=_rate(raw, "input_per_mtok"),
        output_per_mtok=_rate(raw, "output_per_mtok"),
        currency=currency,
        cache_read_per_mtok=(
            _rate(raw, "cache_read_per_mtok") if "cache_read_per_mtok" in raw else None
        ),
        cache_write_per_mtok=(
            _rate(raw, "cache_write_per_mtok") if "cache_write_per_mtok" in raw else None
        ),
        above_tokens=above if tier else None,
        above_input_per_mtok=_rate(raw, "above_input_per_mtok") if tier else None,
        above_output_per_mtok=_rate(raw, "above_output_per_mtok") if tier else None,
    )


def _parse_entry(
    table: Mapping[str, object], index: int
) -> tuple[ModelEntry | None, list[str]]:
    """The entry and its warnings; None when the entry is skipped."""
    where = f"models.toml [[model]] #{index + 1}"
    model_id = _require(table, "id", where)
    where = f"models.toml [[model]] {model_id!r}"

    api = _require(table, "api", where)
    if api not in WIRE_DIALECTS:
        raise ConfigError(
            f"{where}: api must be one of {sorted(WIRE_DIALECTS)}, got {api!r}"
        )

    base_url = _require(table, "base_url", where)
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}: base_url must start with http:// or https://")

    serve = table.get("serve")
    args = table.get("args", {})
    if not isinstance(args, Mapping):
        raise ConfigError(f"{where}: args must be a table")
    args = dict(args)
    warnings: list[str] = []
    if serve == "llama-server":
        return None, [
            f"{model_id}: Cogito no longer starts llama-server, so this entry is skipped. "
            "Start the server yourself and remove serve, or use Cogito Motoris."
        ]
    if serve == "strata":
        if "strata" in args:
            args["motoris"] = args.pop("strata")
        serve = "motoris"
        warnings.append(f'{model_id}: serve = "strata" is read as serve = "motoris"; rename it')
    if serve is not None:
        if serve not in SERVERS:
            raise ConfigError(f"{where}: serve must be one of {sorted(SERVERS)}, got {serve!r}")
        if not (args.get("motoris") and args.get("config")):
            raise ConfigError(
                f"{where}: serve='motoris' requires args.motoris (the Cogito Motoris checkout) "
                "and args.config (the engine JSON its setup wrote)"
            )
        # The engine config is the window the server runs. A `ctx` copied into models.toml
        # when the entry was saved goes stale when Motoris's setup changes it (32K -> 256K,
        # cogito-motoris b081c66f, 2026-10-31), so the config wins when it names one.
        if (window := motoris_max_context(args)) is not None:
            args["ctx"] = window

    sampling = table.get("sampling", {})
    if not isinstance(sampling, Mapping):
        raise ConfigError(f"{where}: sampling must be a table")

    api_key_file = table.get("api_key_file")
    if api_key_file is not None and (not isinstance(api_key_file, str) or not api_key_file.strip()):
        raise ConfigError(f"{where}: api_key_file must be a path to a file")

    pins = _check_pins(table.get("pin", {}), where, warnings)

    price = None
    if "price" in table:
        try:
            price = _parse_price(table["price"])
        except ConfigError as exc:
            # A wrong cost is worse than none, and a mistyped price must not take the catalog
            # down. It is reported, like a bad pin, and no cost is shown.
            warnings.append(f"{where}: {exc}; the cost is not shown")

    entry = ModelEntry(
        id=model_id,
        label=str(table.get("label") or model_id),
        api=api,
        base_url=base_url.rstrip("/"),
        model=_require(table, "model", where),
        api_key_env=table.get("api_key_env") or None,  # type: ignore[arg-type]
        api_key_file=api_key_file.strip() if api_key_file else None,
        serve=serve,  # type: ignore[arg-type]
        args=args,
        sampling=dict(sampling),
        pins=pins,
        price=price,
    )

    if entry.origin == "api" and not (entry.api_key_env or entry.api_key_file):
        warnings.append(
            f"{entry.id}: remote host with no api_key_env; "
            f"set one, or store the key from the settings screen"
        )
    return entry, warnings


def parse(text: str) -> Catalog:
    """Parse ``models.toml`` content. Raises ConfigError with the offending entry named."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"models.toml is not valid TOML: {exc}") from exc

    raw = document.get("model", [])
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise ConfigError("models.toml: expected one or more [[model]] tables")

    entries: list[ModelEntry] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, table in enumerate(raw):
        if not isinstance(table, Mapping):
            raise ConfigError(f"models.toml [[model]] #{index + 1}: expected a table")
        entry, entry_warnings = _parse_entry(table, index)
        warnings.extend(entry_warnings)
        if entry is None:
            continue
        if entry.id in seen:
            raise ConfigError(f"models.toml: duplicate model id {entry.id!r}")
        seen.add(entry.id)
        entries.append(entry)

    return Catalog(entries=tuple(entries), warnings=tuple(warnings))


def load(path: Path | None = None) -> Catalog:
    """Load the catalog from disk. An absent file is an empty catalog, not an error."""
    from cogito.config import paths

    target = path or paths.models_file()
    if not target.exists():
        return Catalog(entries=(), warnings=(f"{target} does not exist yet",))
    return parse(target.read_text(encoding="utf-8"))


class LiveCatalog:
    """The catalog a long-lived process serves from (`cogito serve`), read again when COGITO
    itself adds an entry (`append_entry`, the window's connection screen). Answers what a
    `Catalog` answers (``entries``, ``warnings``, ``get``, ``grouped``) from the last
    successful read, so every holder of this object sees the new entry.

    A file that no longer parses keeps the last good catalog, so a line mistyped while the
    process runs never loses the models it is serving.
    """

    def __init__(self, path: Path | None = None, *, initial: Catalog | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._current = initial if initial is not None else load(path)

    @property
    def entries(self) -> tuple[ModelEntry, ...]:
        return self._current.entries

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._current.warnings

    def get(self, model_id: str) -> ModelEntry:
        return self._current.get(model_id)

    def grouped(self) -> dict[str, list[ModelEntry]]:
        return self._current.grouped()

    def reload(self) -> Catalog:
        """Read the file again; the new catalog, or the previous one if it does not parse."""
        with self._lock:
            # A file that no longer parses keeps the last good catalog.
            with contextlib.suppress(ConfigError):
                self._current = load(self._path)
            return self._current


#: What `render_entry` writes, in this order; ``args`` and ``price`` follow as inline tables.
_ENTRY_FIELDS = (
    "id", "label", "api", "base_url", "model", "serve", "api_key_env", "api_key_file"
)
_TABLE_FIELDS = ("args", "price")
_ID_CHARS = re.compile(r"[^a-z0-9._-]+")
#: One writer at a time in this process, or two racing additions could each drop the other.
_APPEND_LOCK = threading.Lock()


def entry_id(base: str, taken: Iterable[str]) -> str:
    """A catalog id for a new entry: ``base`` lower-cased, anything but letters, digits,
    ``.``, ``_`` and ``-`` turned into a dash, then ``-2``, ``-3``... until no entry has it."""
    slug = _ID_CHARS.sub("-", base.strip().lower()).strip("-.")[:80] or "model"
    existing = set(taken)
    chosen, counter = slug, 2
    while chosen in existing:
        chosen = f"{slug}-{counter}"
        counter += 1
    return chosen


def _toml_value(item: object) -> str:
    from cogito.config.tomlfile import quote

    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, int):
        return str(item)
    if isinstance(item, float):
        if not math.isfinite(item):
            raise ConfigError("a catalog number must be finite")
        return repr(item)
    if isinstance(item, str):
        return quote(item)
    if isinstance(item, Mapping):
        inner = ", ".join(f"{key} = {_toml_value(value)}" for key, value in item.items())
        return "{ " + inner + " }" if inner else "{}"
    raise ConfigError(f"cannot write {type(item).__name__} to models.toml")


def render_entry(fields: Mapping[str, object], *, comment: str = "") -> str:
    """One ``[[model]]`` block for ``fields``, aligned like the starter catalog. ``comment``
    lines go above the table header."""
    unknown = sorted(set(fields) - set(_ENTRY_FIELDS) - set(_TABLE_FIELDS))
    if unknown:
        raise ConfigError(f"cannot write unknown field(s) {', '.join(unknown)}")
    keys = [key for key in _ENTRY_FIELDS if fields.get(key) is not None]
    keys += [key for key in _TABLE_FIELDS if fields.get(key)]
    width = max(len(key) for key in keys)
    lines = [f"# {line.strip()}" for line in comment.splitlines() if line.strip()]
    lines.append("[[model]]")
    lines.extend(f"{key.ljust(width)} = {_toml_value(fields[key])}" for key in keys)
    return "\n".join(lines) + "\n"


def append_entry(
    fields: Mapping[str, object],
    *,
    comment: str = "",
    path: Path | None = None,
    id_base: str | None = None,
    reserved: Iterable[str] = (),
) -> ModelEntry:
    """Add one entry at the end of ``models.toml`` and return it as parsed.

    Everything already in the file (comments, order, blank lines, every other entry) stays
    byte for byte, because the block is only appended: the file before is a prefix of the
    file after, plus a final newline if it had none. A benchmark digests each entry it runs
    (cogito-lab ``bench/extend.py``, ``config_record``), so an addition must never change
    another entry. This is checked: the file is written only if every old entry parses to
    the same value afterwards and the new block parses as exactly one more entry. An absent
    file is created with a one-line header.

    With ``id_base`` the id is chosen here from the file as it is *now* (`entry_id`), never
    from a catalog read earlier, and never one of ``reserved`` (names something else already
    holds, such as a stored key's).
    """
    from cogito.config import paths
    from cogito.config.tomlfile import write_atomically

    target = path or paths.models_file()
    # A catalog symlinked here is written where it really lives. Replacing the link with a
    # copy would silently fork it.
    if target.is_symlink():
        target = target.resolve()
    with _APPEND_LOCK:
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        old = parse(before) if before else Catalog(entries=())
        if id_base is not None:
            fields = {
                **fields,
                "id": entry_id(id_base, [*(entry.id for entry in old.entries), *reserved]),
            }
        new_id = fields.get("id")
        if any(entry.id == new_id for entry in old.entries):
            raise ConfigError(f"models.toml already has an entry {new_id!r}")
        if not before:
            head = "# Cogito model catalog.\n"
        else:
            head = before if before.endswith("\n") else before + "\n"
        after = head + "\n" + render_entry(fields, comment=comment)
        new = parse(after)
        by_id = {entry.id: entry for entry in new.entries}
        for entry in old.entries:
            if by_id.get(entry.id) != entry:
                raise ConfigError(
                    f"adding {new_id!r} would change {entry.id!r}; nothing was written"
                )
        added = by_id.get(str(new_id))
        if added is None or len(new.entries) != len(old.entries) + 1:
            raise ConfigError("the new block did not parse as exactly one more [[model]]")
        write_atomically(target, after)
        if mode is not None:
            # The replacement is a new file (owner-only, from mkstemp): keep the old mode.
            target.chmod(mode)
        return added


STARTER_TOML = """\
# Cogito model catalog.
#
# Every model is reached over an API host, local ones included. Two wire dialects exist:
#   api = "openai"     llama.cpp, Ollama, vLLM, LM Studio, OpenAI, DeepSeek, OpenRouter
#   api = "anthropic"  Claude
#
# Cogito starts Cogito Motoris for an entry with `serve`; any other host is one you run
# yourself. Adding a provider is adding a block here.

# Cogito Motoris, started and stopped by Cogito. `motoris` is its checkout and `config` the
# engine JSON its setup wrote (absolute, or relative to the checkout).
# [[model]]
# id       = "motoris-local"
# label    = "Cogito Motoris"
# api      = "openai"
# base_url = "http://127.0.0.1:8095/v1"
# model    = "motoris"
# serve    = "motoris"
# args     = { motoris = "~/cogito-motoris", config = "strata-q2_0-cogito.json", ctx = 32768 }

# Otro: a server you already run yourself on loopback.
# [[model]]
# id       = "local-other"
# label    = "Local server"
# api      = "openai"
# base_url = "http://127.0.0.1:8080/v1"
# model    = "my-model"

# `price` is optional and only ever yours: what one million input and output tokens cost,
# in a three-letter ISO 4217 currency, copied from your provider's own price list (the
# values below are an example, not anyone's rate). The desktop app shows a cost only when it
# is set; an invalid one is reported by `cogito models list` and ignored, never guessed.
#
#   price = { input_per_mtok = 3.0, output_per_mtok = 15.0, currency = "USD" }
#
# Claude, over the Messages API. The key comes from `api_key_env`, else from
# `api_key_file` (KEY=VALUE or the bare key; chmod 600, or it is refused), else from the
# settings screen.
#
# [[model]]
# id          = "claude-haiku-4-5"
# label       = "Claude Haiku 4.5"
# api         = "anthropic"
# base_url    = "https://api.anthropic.com"
# model       = "claude-haiku-4-5"
# api_key_env = "ANTHROPIC_API_KEY"
# # api_key_file = "~/.config/cogito/anthropic.env"

# [[model]]
# id          = "deepseek"
# label       = "DeepSeek"
# api         = "openai"
# base_url    = "https://api.deepseek.com/v1"
# model       = "deepseek-chat"
# api_key_env = "DEEPSEEK_API_KEY"
"""
