"""Today's model usage and cost, and the last provider failure.

The desktop tray shows (design 2b): for an API model, tokens today and a cost only when the
price is known; for a local model, tokens per second, tokens today and "sin costo"; and
"Límite de uso alcanzado" only when a limit was actually hit. This module keeps those
facts and nothing else.

`MeteredProvider` collects them. `cli.py`'s `serve` wraps the served provider in one per
session. It forwards every call unchanged and only observes the final `Completed` chunk
(usage, timing) and any `ProviderError`. It never changes what is sent, never retries,
never swallows errors, and nothing it keeps feeds a prompt. `cogito chat` (every benchmark
task) is not metered.

The day's counters live in a small JSON file in the data directory, keyed by the local
calendar date, so the first access after local midnight starts a new day. Writes are
atomic read-merge-writes under a lock, so two processes sharing the file keep each other's
counts. Tokens per second and the last usage limit are kept in this process's memory.

API cost is COGITO's own estimate, never a provider figure: tokens times the entry's
`price`, with prompt-cache writes and reads at their own rates (`config.models.Price.cost`).
It is summed per currency for the day and for the local calendar month, in the same file.
A local model records no spend. An API model without a price records tokens but no cost.
Nothing about spend is written until something is spent, so a local-only machine never
gets spend fields in its file.

The per-minute headroom (`provider/ratelimit.py`) is also in memory: the latest limits per
model and whether each is near, so ``rate_limit_near`` fires once per crossing and once on
recovery.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from cogito.cancel import CancelToken
from cogito.config import paths
from cogito.config.models import ModelEntry
from cogito.errors import (
    CONTEXT_OVERFLOW,
    OTHER,
    ContextTooSmall,
    ProviderError,
    ServiceError,
)
from cogito.provider import ratelimit
from cogito.provider import service as service_module
from cogito.provider.base import (
    Chunk,
    Completed,
    Completion,
    Message,
    RateLimit,
    SamplingParams,
    Timing,
    ToolSpec,
    Usage,
)

try:  # POSIX only. On Windows a single `serve` process is the only writer.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_VERSION = 1

#: Per-model prompt-cache counts, stored only once one is non-zero.
_CACHE_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")


#: Spend is kept to this many decimals: far below any currency's smallest unit, so summing
#: millions of tiny requests does not drift.
_SPEND_PLACES = 9


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _month_of(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


def usage_file() -> Path:
    return paths.data_dir() / "usage.json"


@dataclass(frozen=True, slots=True)
class DayUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    #: Of ``input_tokens``, what a prompt cache wrote and served. Used only for cost, since
    #: each has its own rate (`config.models.Price.cost`). Zero without a prompt cache, and
    #: then not written to the file.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    #: What the day's requests cost, summed as each was billed, or None for a day recorded
    #: before it was kept.
    cost: float | None = None

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Spend:
    """Spend per currency today and this month, and today's API-model tokens. COGITO's own
    estimate (see the module docstring)."""

    date: str
    month: str
    today: Mapping[str, float]
    month_to_date: Mapping[str, float]
    #: Input and output tokens of every API model today, priced or not. Never local ones.
    api_tokens_today: int = 0
    #: The 80 % notice of COGITO's budget was already given this month (`provider.budget`).
    near_notified: bool = False
    #: Tokens of unpriced API models, today and this month. They are not in the amounts
    #: above, and the window says so instead of letting a total imply they were free.
    unpriced_tokens_today: int = 0
    unpriced_tokens_month: int = 0


@dataclass(frozen=True, slots=True)
class ServiceFailure:
    """One provider failure as the window shows it (design 2c): the service, whether it was
    a usage limit or no answer, the status, the provider's code and message, the endpoint,
    and `retry-after` only when the provider gave one.

    ``kind`` is the window's two-way split. ``category`` is what the failure is
    (`cogito.errors.CATEGORIES`): a spend cap and a console spend limit are both ``limit``
    to the window, and neither resumes on its own. ``resumes_at`` is when access returns,
    set only when the provider said or its rule fixes it.
    """

    service: str
    kind: str
    status: int | None
    code: str
    endpoint: str
    message: str
    retry_after: float | None
    #: Local time with its offset, so "Se reanuda en" can be counted from it.
    at: str
    category: str = OTHER
    resumes_at: str | None = None
    #: The provider's page for this failure (`provider.service.Console`): billing for a
    #: spend cap, the limit page for a limit the user set. None otherwise.
    console_url: str | None = None
    #: `errors.spend_cap_reason`: "cap" | "credit" | "quota" | None. Lets the window avoid
    #: calling prepaid credit or a quota a "monthly" cap.
    spend_cap_reason: str | None = None
    #: A `CONTEXT_OVERFLOW`'s sizes as the provider gave them: the refused request and the
    #: most one request may hold there. None when not given, and for any other failure.
    requested_tokens: int | None = None
    available_tokens: int | None = None

    @classmethod
    def from_exception(cls, error: ProviderError, *, service: str, now: datetime) -> ServiceFailure:
        at = now.isoformat(timespec="seconds")
        if isinstance(error, ServiceError):
            return cls(
                service=service,
                kind="limit" if error.is_usage_limit else "no_response",
                status=error.status,
                code=error.code,
                endpoint=error.endpoint,
                message=error.provider_message,
                retry_after=error.retry_after,
                at=at,
                category=error.category,
                resumes_at=error.resumes_at,
                spend_cap_reason=error.spend_cap_reason,
                requested_tokens=error.requested_tokens,
                available_tokens=error.available_tokens,
            )
        return cls(
            service=service,
            kind="no_response",
            status=None,
            code="",
            endpoint="",
            message=str(error),
            retry_after=None,
            at=at,
        )

    def as_event(self) -> dict[str, Any]:
        """The ``service_error`` event's fields. ``requested_tokens`` and
        ``available_tokens`` are included only for a context overflow."""
        event: dict[str, Any] = {
            "service": self.service,
            "kind": self.kind,
            "status": self.status,
            "code": self.code,
            "endpoint": self.endpoint,
            "message": self.message,
            "retry_after": self.retry_after,
            "at": self.at,
            "category": self.category,
            "resumes_at": self.resumes_at,
            "console_url": self.console_url,
            "spend_cap_reason": self.spend_cap_reason,
        }
        if self.category == CONTEXT_OVERFLOW:
            event["requested_tokens"] = self.requested_tokens
            event["available_tokens"] = self.available_tokens
        return event


class UsageLedger:
    """Tokens today per model and spend today and this month per currency (persisted), plus
    the last tokens/s, last usage limit and latest per-minute limits (in memory)."""

    def __init__(
        self, path: Path | None = None, *, now: Callable[[], datetime] = _local_now
    ) -> None:
        self._path = path or usage_file()
        self._now = now
        self._lock = threading.Lock()
        #: What this process last read or wrote, used when the disk fails.
        self._memory: dict[str, Any] = {"date": "", "models": {}}
        self._rate: dict[str, float] = {}
        self._limits: dict[str, ServiceFailure] = {}
        self._rate_limits: dict[str, dict[str, RateLimit]] = {}
        #: Per model: under `ratelimit.NEAR_BELOW` at the last response that reported limits.
        self._near: dict[str, bool] = {}

    def date(self) -> str:
        return self._now().date().isoformat()

    def month(self) -> str:
        """The local calendar month, ``YYYY-MM``, the key for month-to-date spend."""
        return _month_of(self._now())

    def _period(self) -> tuple[str, str]:
        moment = self._now()
        return moment.date().isoformat(), _month_of(moment)

    # -- persistence --------------------------------------------------------------------

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        if fcntl is None:
            yield
            return
        paths.ensure(self._path.parent)
        with open(self._path.with_name(self._path.name + ".lock"), "w", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self, day: str, month: str) -> dict[str, Any]:
        """The stored day, or a fresh one when the file is missing or from an earlier day.
        An unreadable file falls back to this process's memory. A new day in the same month
        keeps the month's section; a new month starts it over."""
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"date": day, "models": {}}
        except (OSError, ValueError):
            if self._memory["date"] == day:
                return json.loads(json.dumps(self._memory))
            return self._fresh(day, month, self._memory)
        if not isinstance(stored, dict):
            return {"date": day, "models": {}}
        if stored.get("date") != day or not isinstance(stored.get("models"), dict):
            return self._fresh(day, month, stored)
        if not self._month_section(stored, month):
            stored.pop("month", None)
        return stored

    @staticmethod
    def _month_section(document: Mapping[str, Any], month: str) -> dict[str, Any] | None:
        section = document.get("month")
        if isinstance(section, dict) and section.get("id") == month:
            return section
        return None

    def _fresh(self, day: str, month: str, previous: Mapping[str, Any]) -> dict[str, Any]:
        document: dict[str, Any] = {"date": day, "models": {}}
        if (section := self._month_section(previous, month)) is not None:
            document["month"] = section
        return document

    def _write(self, document: dict[str, Any]) -> None:
        paths.ensure(self._path.parent)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({**document, "version": _VERSION}, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise

    # -- recording ------------------------------------------------------------------------

    def record(
        self,
        model_id: str,
        usage: Usage,
        timing: Timing | None = None,
        *,
        cost: tuple[float, str] | None = None,
        api: bool = False,
    ) -> None:
        """One completed call. Also clears that model's usage limit, since it answered.

        ``cost`` is ``(amount, currency)`` for an API model with a price, else None.
        ``api`` adds its tokens to the day's API tokens. A local model passes neither.
        """
        day, month = self._period()
        with self._lock:
            if timing is not None and (rate := timing.tokens_per_second) is not None:
                self._rate[model_id] = rate
            self._limits.pop(model_id, None)
            try:
                with self._file_lock():
                    document = self._read(day, month)
                    self._add(document, model_id, usage, month=month, cost=cost, api=api)
                    self._write(document)
            except OSError as exc:
                log.warning("usage: could not persist today's counts (%s); kept in memory", exc)
                document = (
                    self._memory
                    if self._memory["date"] == day
                    else self._fresh(day, month, self._memory)
                )
                self._add(document, model_id, usage, month=month, cost=cost, api=api)
            self._memory = document

    @staticmethod
    def _add(
        document: dict[str, Any],
        model_id: str,
        usage: Usage,
        *,
        month: str,
        cost: tuple[float, str] | None,
        api: bool,
    ) -> None:
        counts = document["models"].setdefault(model_id, {})
        counts["input_tokens"] = int(counts.get("input_tokens", 0)) + usage.input_tokens
        counts["output_tokens"] = int(counts.get("output_tokens", 0)) + usage.output_tokens
        counts["requests"] = int(counts.get("requests", 0)) + 1
        for key in _CACHE_KEYS:
            if spent := getattr(usage, key):
                counts[key] = int(counts.get(key, 0)) + spent
        if api and usage.total:
            document["api_tokens"] = int(document.get("api_tokens", 0)) + usage.total
        if api and cost is None and usage.total:
            document["unpriced_tokens"] = int(document.get("unpriced_tokens", 0)) + usage.total
            section = UsageLedger._month_section(document, month)
            if section is None:
                section = document["month"] = {"id": month, "spend": {}}
            section["unpriced_tokens"] = int(section.get("unpriced_tokens", 0)) + usage.total
        if cost is None or not cost[0] > 0:
            return
        amount, currency = cost
        # Kept per model too: a price with a second tier is billed request by request, so
        # the day's summed tokens cannot be priced again as one request (`DayUsage.cost`).
        counts["cost"] = round(float(counts.get("cost", 0.0)) + amount, _SPEND_PLACES)
        today = document.setdefault("spend", {})
        today[currency] = round(float(today.get(currency, 0.0)) + amount, _SPEND_PLACES)
        section = UsageLedger._month_section(document, month)
        if section is None:
            section = document["month"] = {"id": month, "spend": {}}
        spend = section.setdefault("spend", {})
        spend[currency] = round(float(spend.get(currency, 0.0)) + amount, _SPEND_PLACES)

    def mark_near_notified(self) -> bool:
        """Record that this month's 80 % budget notice was given. True only for the call
        that recorded it, so it happens once per month across processes sharing the file."""
        day, month = self._period()
        with self._lock:
            try:
                with self._file_lock():
                    document = self._read(day, month)
                    section = self._month_section(document, month)
                    if section is not None and section.get("near_notified"):
                        return False
                    if section is None:
                        section = document["month"] = {"id": month, "spend": {}}
                    section["near_notified"] = True
                    self._write(document)
            except OSError as exc:
                log.warning("usage: could not persist the budget notice (%s); kept in memory", exc)
                document = (
                    self._memory
                    if self._memory["date"] == day
                    else self._fresh(day, month, self._memory)
                )
                section = self._month_section(document, month)
                if section is not None and section.get("near_notified"):
                    return False
                if section is None:
                    section = document["month"] = {"id": month, "spend": {}}
                section["near_notified"] = True
            self._memory = document
        return True

    def record_failure(
        self, model_id: str, failure: ServiceFailure, *, settled: bool = False
    ) -> None:
        """Keep the last failure of either kind until the next success. The window (design
        2c) shows both: «Límite de uso alcanzado» for a usage limit and «La API no
        respondió» for a timeout, overload or outage (`kind == "no_response"`), each with
        «Reintentar ahora». An outage does not replace a usage limit already held, since
        the limit is still why the user has to wait.

        Outages are kept so the window can tell one from a plain "Error", as
        `desktop/src/main/status-hub.js` expects (2026-09-29 UX audit).

        ``settled`` marks the failure a turn stopped on (`MeteredProvider.settle`), which
        replaces whatever came before.
        """
        with self._lock:
            existing = self._limits.get(model_id)
            if (
                not settled
                and existing is not None
                and existing.kind == "limit"
                and failure.kind != "limit"
            ):
                return
            self._limits[model_id] = failure

    def clear_limit(self, model_id: str) -> None:
        """Clear the tray's «Límite de uso alcanzado» (or «La API no respondió») for
        ``model_id`` as a retry starts, before it is known to succeed.

        Otherwise the tray shows "reached" through a resume already under way (2026-09-29
        UX final verification, item 5). `MeteredProvider.clear_last_error` calls this at
        the start of `resume` (`resume_started`) and of a fresh `run`. If the retry fails,
        `record_failure` sets it again.
        """
        with self._lock:
            self._limits.pop(model_id, None)

    # -- reading --------------------------------------------------------------------------

    def _document(self) -> tuple[str, str, dict[str, Any]]:
        day, month = self._period()
        with self._lock:
            try:
                with self._file_lock():
                    document = self._read(day, month)
            except OSError:
                document = (
                    self._memory
                    if self._memory["date"] == day
                    else self._fresh(day, month, self._memory)
                )
        return day, month, document

    def spend(self) -> Spend:
        """Today's and this month's spend per currency, as recorded (see `Spend`)."""
        day, month, document = self._document()
        section = self._month_section(document, month) or {}

        def amounts(raw: object) -> dict[str, float]:
            if not isinstance(raw, Mapping):
                return {}
            return {
                str(currency): float(amount)
                for currency, amount in raw.items()
                if isinstance(amount, int | float) and not isinstance(amount, bool)
            }

        tokens = document.get("api_tokens", 0)

        def count(raw: object) -> int:
            return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0

        return Spend(
            date=day,
            month=month,
            today=amounts(document.get("spend")),
            month_to_date=amounts(section.get("spend")),
            api_tokens_today=int(tokens) if isinstance(tokens, int) else 0,
            near_notified=bool(section.get("near_notified")),
            unpriced_tokens_today=count(document.get("unpriced_tokens")),
            unpriced_tokens_month=count(section.get("unpriced_tokens")),
        )

    def today(self) -> dict[str, DayUsage]:
        _day, _month, document = self._document()
        return {
            model_id: DayUsage(
                int(counts.get("input_tokens", 0)),
                int(counts.get("output_tokens", 0)),
                int(counts.get("requests", 0)),
                int(counts.get("cache_creation_input_tokens", 0)),
                int(counts.get("cache_read_input_tokens", 0)),
                float(counts["cost"]) if isinstance(counts.get("cost"), (int, float)) else None,
            )
            for model_id, counts in document["models"].items()
            if isinstance(counts, Mapping)
        }

    def tokens_per_second(self, model_id: str) -> float | None:
        with self._lock:
            return self._rate.get(model_id)

    def limit(self, model_id: str) -> ServiceFailure | None:
        with self._lock:
            return self._limits.get(model_id)

    # -- per-minute limits ----------------------------------------------------------------

    def observe_rate_limits(
        self, model_id: str, limits: Mapping[str, RateLimit]
    ) -> dict[str, Any] | None:
        """Keep the latest limits ``model_id`` reported, and return the ``rate_limit_near``
        event when this response crossed `ratelimit.NEAR_BELOW` either way. None while it
        stays on the same side, so the event fires once per crossing."""
        room = ratelimit.headroom(limits)
        with self._lock:
            self._rate_limits[model_id] = dict(limits)
            if room is None:
                return None
            near = room[0] < ratelimit.NEAR_BELOW
            was = self._near.get(model_id, False)
            self._near[model_id] = near
        if near == was:
            return None
        return self._near_event(model_id, limits, room, near=near)

    def rate_limit_near(self, model_id: str) -> dict[str, Any] | None:
        """The ``rate_limit_near`` fields while ``model_id`` is near its limits, else None.
        Never a permanent gauge."""
        with self._lock:
            if not self._near.get(model_id):
                return None
            limits = dict(self._rate_limits.get(model_id, {}))
        room = ratelimit.headroom(limits)
        if room is None:
            return None
        event = self._near_event(model_id, limits, room, near=True)
        event.pop("type")
        return event

    @staticmethod
    def _near_event(
        model_id: str, limits: Mapping[str, RateLimit], room: tuple[float, str], *, near: bool
    ) -> dict[str, Any]:
        fraction, lowest = room
        return {
            "type": "rate_limit_near",
            "model_id": model_id,
            "percent": ratelimit.percent(fraction),
            "lowest": lowest,
            "recovered": not near,
            "limits": {name: limit.as_dict() for name, limit in sorted(limits.items())},
        }


class _LastError:
    """One session's last provider failure, shared by its `configured` copies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value: ServiceFailure | None = None

    def set(self, value: ServiceFailure | None) -> None:
        with self._lock:
            self.value = value


class MeteredProvider:
    """A provider that counts what passes through it without changing any of it.

    Given the served ``entry``, it also prices API spend (`Price.cost`, cache included),
    links a spend failure to the provider's console, and passes on what it saw: headroom
    crossings to ``broadcast`` (every session, since one model serves them all) and each
    API call to ``budget`` (`provider.budget.BudgetKeeper`), which decides what COGITO's
    budget tells this session's ``emit``. Without an entry it only counts tokens.
    """

    def __init__(
        self,
        inner: Any,
        ledger: UsageLedger,
        *,
        model_id: str,
        service: str = "",
        entry: ModelEntry | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
        broadcast: Callable[[dict[str, Any]], None] | None = None,
        budget: Any = None,
        now: Callable[[], datetime] = _local_now,
        _last: _LastError | None = None,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._model_id = model_id
        self._service = service
        self._entry = entry
        self._emit = emit
        self._broadcast = broadcast
        self._budget = budget
        self._now = now
        self._last = _last or _LastError()
        self._api = entry is not None and entry.origin == "api"
        #: Only an API model is priced. A local one costs nothing, whatever its entry says.
        self._price = entry.price if self._api and entry is not None else None
        self._console = service_module.console(entry) if entry is not None else None
        self.name = inner.name

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this class does not define.
        return getattr(self._inner, name)

    def capabilities(self):
        return self._inner.capabilities()

    def stream(
        self,
        messages: list[Message] | tuple[Message, ...],
        tools: list[ToolSpec] | tuple[ToolSpec, ...],
        *,
        params: SamplingParams,
        cancel: CancelToken,
    ) -> Iterator[Chunk]:
        try:
            for chunk in self._inner.stream(messages, tools, params=params, cancel=cancel):
                if isinstance(chunk, Completed):
                    self._observe(chunk.completion)
                yield chunk
        except ProviderError as error:
            self._fail(error)
            raise

    def _cost(self, usage: Usage) -> tuple[float, str] | None:
        if self._price is None:
            return None
        amount = self._price.cost(
            usage.input_tokens,
            usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
        )
        return amount, self._price.currency

    def _observe(self, completion: Completion) -> None:
        self._last.set(None)
        try:
            self._ledger.record(
                self._model_id,
                completion.usage,
                completion.timing,
                cost=self._cost(completion.usage),
                api=self._api,
            )
        except Exception:  # noqa: BLE001 - counting must never cost a turn
            log.exception("usage: recording a completion failed")
        if completion.rate_limits:
            try:
                crossed = self._ledger.observe_rate_limits(self._model_id, completion.rate_limits)
                if crossed is not None and self._broadcast is not None:
                    self._broadcast({**crossed, "service": self._service})
            except Exception:  # noqa: BLE001 - a display must never cost a turn
                log.exception("usage: reading the per-minute limits failed")
        if self._api and self._budget is not None:
            try:
                self._budget.after_spend(self._entry, emit=self._emit)
            except Exception:  # noqa: BLE001 - the budget's display must never cost a turn
                log.exception("usage: updating the budget failed")

    def _fail(self, error: ProviderError) -> None:
        try:
            failure = ServiceFailure.from_exception(error, service=self._service, now=self._now())
            if self._console is not None:
                failure = replace(failure, console_url=self._console.for_category(failure.category))
            # Stored on the error so `settle` can report the failure a turn stopped on even
            # after later calls failed differently.
            error.service_failure = failure  # type: ignore[attr-defined]
            self._last.set(failure)
            self._ledger.record_failure(self._model_id, failure)
        except Exception:  # noqa: BLE001 - never replace the provider's own error
            log.exception("usage: recording a provider failure failed")

    def settle(self, error: BaseException | None) -> None:
        """The turn stopped on ``error``, so the window and tray report it, whatever failed
        afterwards. Otherwise a fold's summary request that timed out after the turn's
        request was refused as too large would be reported instead. Does nothing for an
        error this meter never saw, except the loop's own `ContextTooSmall`: built from a
        refusal the loop could not answer, it is what the turn stopped on."""
        failure = getattr(error, "service_failure", None)
        if not isinstance(failure, ServiceFailure) and isinstance(error, ContextTooSmall):
            failure = ServiceFailure.from_exception(error, service=self._service, now=self._now())
        if not isinstance(failure, ServiceFailure):
            return
        self._last.set(failure)
        self._ledger.record_failure(self._model_id, failure, settled=True)

    @property
    def last_error(self) -> ServiceFailure | None:
        """This session's last failure, until a later call succeeds."""
        return self._last.value

    def clear_last_error(self) -> None:
        """Called at the start of both `run` and `resume` (`cli.py`'s session wrapper), so a
        failure from a turn that already ended never leaks into this one. Also clears the
        ledger's per-model limit (`UsageLedger.clear_limit`), so the tray does not show
        "reached" during a retry."""
        self._last.set(None)
        self._ledger.clear_limit(self._model_id)

    @property
    def options(self) -> Any:
        """The inner provider's per-request options, or None when it has none."""
        return getattr(self._inner, "options", None)

    def configured(self, options: Any) -> MeteredProvider:
        """The inner provider's `configured` copy, metered the same way and sharing this
        session's last error. Returns itself when the inner provider does."""
        configure = getattr(self._inner, "configured", None)
        if configure is None:
            return self
        copy = configure(options)
        if copy is self._inner:
            return self
        return MeteredProvider(
            copy,
            self._ledger,
            model_id=self._model_id,
            service=self._service,
            entry=self._entry,
            emit=self._emit,
            broadcast=self._broadcast,
            budget=self._budget,
            now=self._now,
            _last=self._last,
        )

    def follow(self, inner: Any, *, entry: ModelEntry, service: str) -> MeteredProvider:
        """A meter for the model the picker switched this session to. Same ledger, session
        stream, broadcast and budget; the new model's price and console; its own last
        error."""
        return MeteredProvider(
            inner,
            self._ledger,
            model_id=entry.id,
            service=service,
            entry=entry,
            emit=self._emit,
            broadcast=self._broadcast,
            budget=self._budget,
            now=self._now,
        )

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


def snapshot(
    entry: ModelEntry,
    ledger: UsageLedger,
    *,
    service: Any,
    gpu: Callable[[], int | None],
    budget: Any = None,
) -> dict[str, Any]:
    """What `GET /usage` returns for the model this process serves (design 2b, 2e, 2f).

    API: tokens today, and a cost only when the entry has a price, with prompt-cache writes
    and reads at their own rates. Local: tokens per second, tokens today, and GPU use only
    when the system reports it; never a cost ("sin costo"). A usage limit only while it
    holds, and per-minute headroom only while near. ``spend`` is today's and this month's
    estimate per currency. ``budget`` is COGITO's budget
    (`provider.budget.BudgetKeeper.status`) when one is given.
    """
    today = ledger.today()
    day = today.get(entry.id, DayUsage())
    local = entry.origin == "local"
    rate = ledger.tokens_per_second(entry.id) if local else None
    use = gpu() if local else None
    limit = ledger.limit(entry.id)
    spend = ledger.spend()
    console = service_module.console(entry)
    return {
        "date": ledger.date(),
        "active": {
            "model_id": entry.id,
            "model": entry.model,
            "label": entry.display_label,
            "origin": entry.origin,
            "service": service.as_dict(),
            "tokens_today": day.total,
            "input_tokens_today": day.input_tokens,
            "output_tokens_today": day.output_tokens,
            "requests_today": day.requests,
            "tokens_per_second": round(rate, 1) if rate is not None else None,
            "cost_today": (
                {
                    "amount": round(
                        # A tiered price was billed per request; the summed tokens are not
                        # one request.
                        day.cost
                        if entry.price.above_tokens is not None and day.cost is not None
                        else entry.price.cost(
                            day.input_tokens,
                            day.output_tokens,
                            cache_creation_input_tokens=day.cache_creation_input_tokens,
                            cache_read_input_tokens=day.cache_read_input_tokens,
                        ),
                        6,
                    ),
                    "currency": entry.price.currency,
                }
                if not local and entry.price is not None
                else None
            ),
            "gpu": {"use_percent": use} if use is not None else None,
            "limit": limit.as_event() if limit is not None else None,
            "rate_limit": ledger.rate_limit_near(entry.id) if not local else None,
            # From the fixed console table, not tied to a failure. Unlike `limit`'s
            # `console_url`, which lasts only while that failure is in memory, this survives
            # a core restart (2026-09-29 UX audit follow-up).
            "console": (
                {"billing": console.billing, "spend_limit": console.spend_limit, "usage": console.usage}
                if console is not None
                else None
            ),
        },
        "spend": {
            "month": spend.month,
            "today": dict(spend.today),
            "month_to_date": dict(spend.month_to_date),
        },
        "budget": budget.status(entry) if budget is not None else None,
        "today": {
            model_id: {
                "input_tokens": counts.input_tokens,
                "output_tokens": counts.output_tokens,
                "requests": counts.requests,
            }
            for model_id, counts in today.items()
        },
    }
