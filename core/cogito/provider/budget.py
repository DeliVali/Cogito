"""COGITO's own budget: the person's number, counted by COGITO, not reported by a provider.

The budget is ``[budget]`` in config.toml (`config/budget.py`). Spend is the usage ledger's
own estimate (`provider/usage.py`: tokens times the entry's price, cache included) in the
budget's currency. Nothing here ever reaches a model.

What the window is told (design 2f):

- ``budget_status``: on every change (each API call, each change of the budget), to every
  session, since there is one budget for the whole core.
- ``budget_near``: the month crossed 80 % of its budget. A line in the thread, once per
  calendar month (the ledger keeps the month's notice across restarts).
- ``budget_reached``: the pause. Before the next request to an API model while spend is
  at or over a limit, the turn waits on the same machinery as a permission request
  (`server/questions.py`): continue this turn, raise the budget, or stop. It is asked only
  at a step boundary (`loop/engine.py`), never mid-response, so no request is in flight.
  While it waits the phase is the amber permission state, and like a permission card it
  stays until answered (design 2c). Detener and the session closing still end it, and so
  does the core stopping, with the process.
- ``budget_decision``: each answer, as a marker for the thread. The loop also journals it
  (``budget_decided``) so history shows it again.

A local model is never paused. With no budget configured nothing here says or asks
anything.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cogito.config import budget as budget_config
from cogito.config.models import ModelEntry
from cogito.errors import ConfigError
from cogito.provider import service as service_module
from cogito.provider.usage import UsageLedger

log = logging.getLogger(__name__)

#: The fraction of a limit at which the budget is ``near`` (design 2f, "80 %").
NEAR_AT = 0.8

#: What the person can answer a pause with (design 2f): «Continuar esta tarea», «Subir
#: presupuesto», «Detener».
CONTINUE = "continue"
RAISE = "raise"
STOP = "stop"
DECISIONS = (CONTINUE, RAISE, STOP)

#: What the gate tells the loop: send the request, send every request of this turn without
#: asking again, or end the turn.
GO = "go"


@dataclass(frozen=True, slots=True)
class SpendDecision:
    """The gate's answer before one request (`BudgetKeeper.gate`). ``action`` is `GO`,
    `CONTINUE` or `STOP`; ``markers`` holds every decision the person made on the way,
    oldest first, for the loop to journal. Empty on the ordinary path, which journals
    nothing."""

    action: str
    markers: tuple[Mapping[str, Any], ...] = ()


PASS = SpendDecision(GO)


class Questions(Protocol):
    """Where a pause waits for its answer: ``server.questions.Questions``."""

    def open(self) -> str: ...

    def resolve(
        self,
        identifier: str,
        *,
        timeout: float | None = ...,
        abort: Callable[[], bool] | None = None,
    ) -> str | None: ...


def _round(amount: float) -> float:
    return round(amount, 6)


class BudgetKeeper:
    """The budget as configured, what was spent against it, and the pause at 100 %."""

    def __init__(
        self,
        ledger: UsageLedger,
        *,
        path: Path | None = None,
        questions: Questions | None = None,
        broadcast: Callable[[dict[str, Any]], None] | None = None,
        active: Callable[[], ModelEntry | None] | None = None,
        currencies: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._ledger = ledger
        self._priced_in = currencies or (lambda: ())
        self._path = path
        self._questions = questions
        self._broadcast = broadcast
        self._active = active or (lambda: None)
        self._lock = threading.Lock()
        try:
            self._budget = budget_config.load(path)
        except ConfigError as exc:
            # An unreadable table means no budget, logged once: it must never stop the core.
            log.warning("budget: %s; no budget applies until it is saved again", exc)
            self._budget = budget_config.Budget()

    def set_broadcast(self, broadcast: Callable[[dict[str, Any]], None]) -> None:
        self._broadcast = broadcast

    @property
    def budget(self) -> budget_config.Budget:
        with self._lock:
            return self._budget

    # -- what it says ---------------------------------------------------------------------

    def status(self, entry: ModelEntry | None = None) -> dict[str, Any]:
        """The ``budget_status`` fields, for ``entry`` (the served model when None):
        ``state`` is ``none`` with no budget or a local model, else ``ok``, ``near`` (80 %
        of a limit) or ``reached`` (100 %). ``period``, ``spent``, ``limit`` and
        ``percent`` are those of the limit closest to being reached."""
        budget = self.budget
        chosen = entry if entry is not None else self._active()
        spend = self._ledger.spend()
        currency = budget.currency
        spent = {
            "month": spend.month_to_date.get(currency, 0.0),
            "day": spend.today.get(currency, 0.0),
        }
        limits = [
            (period, limit)
            for period, limit in (("month", budget.monthly), ("day", budget.daily))
            if limit is not None
        ]
        binding = max(limits, key=lambda pair: spent[pair[0]] / pair[1], default=None)
        ratio = spent[binding[0]] / binding[1] if binding is not None else 0.0
        if binding is None or (chosen is not None and chosen.origin == "local"):
            state = "none"
        elif ratio >= 1.0:
            state = "reached"
        elif ratio >= NEAR_AT:
            state = "near"
        else:
            state = "ok"
        console = service_module.console(chosen) if chosen is not None else None
        return {
            "state": state,
            "currency": currency,
            "monthly": budget.monthly,
            "daily": budget.daily,
            "spent_month": _round(spent["month"]),
            "spent_today": _round(spent["day"]),
            "tokens_today": spend.api_tokens_today,
            "period": binding[0] if binding is not None else None,
            "spent": _round(spent[binding[0]]) if binding is not None else None,
            "limit": binding[1] if binding is not None else None,
            "percent": round(ratio * 100, 1) if binding is not None else None,
            "month": spend.month,
            "near_notified_month": spend.month if spend.near_notified else None,
            "console_url": console.usage if console is not None else None,
            # Tokens spent on API models with no price. They are in no amount above (never a
            # silent ≈ 0).
            "unpriced_tokens_today": spend.unpriced_tokens_today,
            "unpriced_tokens_month": spend.unpriced_tokens_month,
            # Whether the model served now has a price: None for a local one.
            "priced": (
                chosen.price is not None
                if chosen is not None and chosen.origin == "api"
                else None
            ),
        }

    def currencies(self) -> list[str]:
        """The currencies a budget can be kept in: those the configured models' prices are
        written in, and the budget's own. Spend is counted in the currency it was priced in
        and a limit only sees its own, so a limit in any other currency could never fire."""
        return sorted({*self._priced_in(), self.budget.currency})

    def view(self) -> dict[str, Any]:
        """``GET /settings/budget``: the budget, the currencies it can be kept in, and where
        it stands."""
        return {**self.budget.as_dict(), "currencies": self.currencies(), "status": self.status()}

    def _say(self, event: dict[str, Any]) -> None:
        if self._broadcast is not None:
            self._broadcast(event)

    def update(self, values: Mapping[str, object]) -> dict[str, Any]:
        """``PUT /settings/budget``: save, and tell every session. ``{"error"}`` when the
        values are refused; nothing is saved then."""
        try:
            saved = budget_config.save(values, self._path)
        except ConfigError as exc:
            return {"error": str(exc)}
        with self._lock:
            self._budget = saved
        status = self.status()
        self._say({"type": "budget_status", **status})
        return {**saved.as_dict(), "currencies": self.currencies(), "status": status}

    def after_spend(
        self, entry: ModelEntry | None, *, emit: Callable[[dict[str, Any]], None] | None = None
    ) -> None:
        """An API call was just counted. With a budget, send the month's 80 % line to the
        session that crossed it (``emit``), once per month, then ``budget_status`` to every
        session. Without one, do nothing."""
        budget = self.budget
        if not budget.configured:
            return
        status = self.status(entry)
        monthly = budget.monthly
        if (
            monthly is not None
            and status["near_notified_month"] is None
            and NEAR_AT <= status["spent_month"] / monthly < 1.0
            and self._ledger.mark_near_notified()
        ):
            status["near_notified_month"] = status["month"]
            percent = round(status["spent_month"] / monthly * 100, 1)
            near = {
                "type": "budget_near",
                "period": "month",
                "spent": status["spent_month"],
                "limit": monthly,
                "percent": percent,
                "currency": status["currency"],
                "month": status["month"],
            }
            (emit or self._say)(near)
        self._say({"type": "budget_status", **status})

    # -- the pause ------------------------------------------------------------------------

    def gate(
        self,
        entry: ModelEntry | None,
        *,
        emit: Callable[[dict[str, Any]], None],
        cancel: Any,
        on_waiting: Callable[[], None] | None = None,
    ) -> SpendDecision:
        """Called before each request to ``entry``. Returns `PASS` unless it is an API model
        and a limit is reached; then the turn waits for the person (``budget_reached``,
        answered through the server). ``continue`` holds for the rest of the turn and
        ``stop`` ends it. ``raise`` (answered once the window has saved a higher budget) is
        checked again, so a budget still reached asks again. There is no timeout. An
        interrupt, or the question being let go (the session closing), is a stop with reason
        ``interrupted``: nothing is spent without a yes. ``on_waiting`` is called as each
        question goes out (the amber phase); the loop's own next phase follows the
        answer."""
        if (
            entry is None
            or entry.origin == "local"
            or self._questions is None
            or not self.budget.configured
        ):
            return PASS
        markers: list[dict[str, Any]] = []
        while True:
            status = self.status(entry)
            if status["state"] != "reached":
                return SpendDecision(GO, tuple(markers))
            identifier = self._questions.open()
            if on_waiting is not None:
                on_waiting()
            emit(
                {
                    "type": "budget_reached",
                    "id": identifier,
                    "period": status["period"],
                    "limit": status["limit"],
                    "spent": status["spent"],
                    "currency": status["currency"],
                    "next_step": {"kind": "model_request", "model": entry.display_label},
                }
            )
            answer = self._questions.resolve(
                identifier,
                timeout=None,
                abort=lambda: bool(getattr(cancel, "cancelled", False)),
            )
            if answer not in DECISIONS:
                decided, answer = self._marker(STOP, "interrupted", status), STOP
            else:
                decided = self._marker(answer, "user", status)
            emit({"type": "budget_decision", "id": identifier, **decided})
            markers.append(decided)
            if answer == STOP:
                return SpendDecision(STOP, tuple(markers))
            if answer == CONTINUE:
                return SpendDecision(CONTINUE, tuple(markers))

    def _marker(self, decision: str, reason: str, status: Mapping[str, Any]) -> dict[str, Any]:
        """One decision as the thread shows it: «Continuaste esta tarea · por encima del
        presupuesto», «Subiste tu presupuesto · {limit} este mes», «Detuviste la tarea ·
        presupuesto del mes». A raise carries the limit as saved, not as it was."""
        limit = status["limit"]
        if decision == RAISE:
            budget = self.budget
            limit = budget.monthly if status["period"] == "month" else budget.daily
        return {
            "decision": decision,
            "reason": reason,
            "period": status["period"],
            "limit": limit,
            "spent": status["spent"],
            "currency": status["currency"],
        }
