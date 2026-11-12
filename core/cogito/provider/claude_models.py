"""Keep the Claude API's newest models in the catalog, in the app.

When the desktop app starts its core, the Claude API is asked for its model list with the
key a Claude entry already uses (`GET /v1/models`, free: no model runs). For each family
(Fable, Mythos, Opus, Sonnet, Haiku) the newest model the key can reach is added to
``models.toml`` when no entry names it yet, through `config.models.append_entry`, which
leaves every other entry byte for byte. Nothing is ever removed or rewritten: an entry the
person has, older ones included, stays. Decided by the person on 2026-10-31 ("generación
actual", kept up to date when Cogito opens) after their picker lacked Opus 5.5, Sonnet 5.5,
Haiku 5.5 and Fable 5.1.

Only aliases count (``claude-opus-5-5``); a dated snapshot (``claude-haiku-4-5-20251001``)
stands for its alias. Mythos is listed only to keys in Project Glasswing, so it is added
only where the key can use it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path

import httpx

from cogito.config import secrets
from cogito.config.models import ModelEntry, append_entry
from cogito.errors import CogitoError

from . import connect

log = logging.getLogger(__name__)

_FAMILY = re.compile(
    r"^claude-(?P<family>fable|mythos|opus|sonnet|haiku)-(?P<major>\d+)(?:-(?P<minor>\d))?$"
)
_SNAPSHOT = re.compile(r"^(?P<alias>.+)-\d{8}$")
_API_HOST = "https://api.anthropic.com"


def _alias(model: str) -> str:
    snapshot = _SNAPSHOT.match(model)
    return snapshot.group("alias") if snapshot else model


def newest_per_family(models: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """The newest model of each family in ``models`` (rows of `connect.list_models`), by
    version: ``claude-opus-5-5`` is newer than ``claude-opus-5``, which is newer than
    ``claude-opus-4-8``. In the order the families first appear."""
    best: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}
    for row in models:
        found = _FAMILY.match(row.get("id", ""))
        if found is None:
            continue
        version = (int(found.group("major")), int(found.group("minor") or 0))
        family = found.group("family")
        if family not in best or version > best[family][0]:
            best[family] = (version, row)
    return [row for _, row in best.values()]


def _source(entries: Sequence[ModelEntry]) -> ModelEntry | None:
    """The first Claude entry on Anthropic's own API: its key is the one asked with."""
    for entry in entries:
        if entry.api == "anthropic" and entry.base_url.rstrip("/") == _API_HOST:
            return entry
    return None


def sync(
    entries: Sequence[ModelEntry],
    *,
    path: Path | None = None,
    client: httpx.Client | None = None,
    today: date | None = None,
) -> list[ModelEntry]:
    """Add the newest model of each family the key reaches and the catalog lacks; returns
    the entries added. Without a Claude entry, a key, or an answer from the API it adds
    nothing and says why in the log: the picker then shows what it showed before."""
    source = _source(entries)
    if source is None:
        return []
    try:
        key = secrets.model_key(source)
    except CogitoError as exc:
        log.info("claude models: not checked, the key could not be read (%s)", exc)
        return []
    if not key:
        return []
    service = connect.provider("anthropic")
    try:
        listed = connect.list_models(service, key, client=client)
    except connect.ConnectFailed as exc:
        log.info("claude models: not checked (%s)", exc.failure.kind)
        return []
    have = {_alias(entry.model) for entry in entries if entry.api == "anthropic"}
    day = (today or date.today()).isoformat()
    added: list[ModelEntry] = []
    for row in newest_per_family(listed.models):
        model = row["id"]
        if model in have:
            continue
        fields: dict[str, object] = {
            "label": f"{row.get('label') or model} - API",
            "api": "anthropic",
            "base_url": source.base_url,
            "model": model,
            "price": connect.price_fields(connect.curated_price("anthropic", model)),
        }
        # The key is found where the source entry finds it: its own file or variable, so a
        # key the person rotates there reaches these entries too. A key the connection
        # screen kept in secrets.toml is stored again under the new entry's id, as that
        # screen stores one.
        if source.api_key_file:
            fields["api_key_file"] = source.api_key_file
        if source.api_key_env:
            fields["api_key_env"] = source.api_key_env
        in_secrets = not (source.api_key_file or source.api_key_env)
        family = model.split("-")[1].capitalize()
        comment = (
            f"Added by COGITO on {day}: the newest {family} the Claude API lists for this key."
        )
        if in_secrets:
            comment += "\nIts key is in secrets.toml, [models] under this entry's id."
        try:
            entry = append_entry(
                fields,
                comment=comment,
                path=path,
                id_base=f"anthropic-{model}" if in_secrets else model,
                reserved=secrets.names("models"),
            )
            if in_secrets:
                secrets.set_("models", entry.id, key, replace=False)
        except (CogitoError, OSError) as exc:
            log.warning("claude models: %s was not added (%s)", model, exc)
            continue
        have.add(model)
        added.append(entry)
    return added
