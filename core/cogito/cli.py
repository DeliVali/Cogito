"""Command line entry point. The composition root: the only place that wires layers."""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from cogito import __version__
from cogito.config import models as model_config
from cogito.config import paths, secrets
from cogito.config import settings as settings_config
from cogito.errors import CogitoError, ConfigError, GraphUnavailable, NoModel

#: Turns running now, across every session this process holds (`chat`'s own loop and every
#: session `serve`'s factory hands out). The app's document index waits while one runs, so
#: it never competes with a live turn. `_track_turns` updates it around each session's `run`
#: and `resume`.
_active_turns_lock = threading.Lock()
_active_turns = 0


def _turn_started() -> None:
    global _active_turns
    with _active_turns_lock:
        _active_turns += 1


def _turn_ended() -> None:
    global _active_turns
    with _active_turns_lock:
        _active_turns = max(0, _active_turns - 1)


def _any_turn_active() -> bool:
    with _active_turns_lock:
        return _active_turns > 0


def _track_turns(engine_session):
    """Wrap this session's `run` and `resume` so every turn it executes is counted while it
    runs, whether it comes from `chat`'s loop or a `serve` request, new or resumed after a
    provider failure."""

    def tracked(original):
        def call(*args, **kwargs):
            _turn_started()
            try:
                return original(*args, **kwargs)
            finally:
                _turn_ended()

        return call

    engine_session.run = tracked(engine_session.run)
    engine_session.resume = tracked(engine_session.resume)
    return engine_session


OK = "ok"
MISSING = "missing"
WARN = "warn"

_MARK = {OK: "+", WARN: "!", MISSING: "-"}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    state: str
    detail: str = ""
    fix: str = ""


def _render(checks: Iterable[Check]) -> int:
    """Print the report and return the number of things that are actually broken."""
    checks = list(checks)
    width = max(len(c.name) for c in checks)
    broken = 0
    for check in checks:
        click.echo(f" [{_MARK[check.state]}] {check.name.ljust(width)}  {check.detail}")
        if check.state == MISSING:
            broken += 1
    fixes = [c for c in checks if c.fix]
    if fixes:
        click.echo("\nTo fix:")
        for check in fixes:
            click.echo(f"  {check.name}:")
            for line in check.fix.splitlines():
                click.echo(f"    {line}")
    return broken


def _check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    supported = (3, 12) <= sys.version_info[:2] < (3, 14)
    return Check("python", OK if supported else WARN, version)


def _check_binary(name: str, label: str | None = None, required: bool = True) -> Check:
    path = shutil.which(name)
    state = OK if path else (MISSING if required else WARN)
    return Check(label or name, state, path or "not on PATH")


def _check_motoris() -> list[Check]:
    """Whether each entry Cogito serves has its Cogito Motoris checkout and engine config
    (`_missing`), naming the piece that is missing."""
    from cogito.provider.local import motoris

    try:
        catalog = model_config.load()
    except CogitoError:
        return []  # `_check_models` reports the catalog itself
    checks = []
    for entry in catalog.entries:
        if entry.serve != "motoris":
            continue
        name = f"motoris {entry.id}"
        if _missing(entry) is None:
            checks.append(Check(name, OK, str(motoris._checkout(entry))))
            continue
        try:
            motoris.build_command(entry)
        except CogitoError as exc:
            detail = str(exc)
        else:
            detail = f"no serve/server.py in {motoris._checkout(entry)}"
        checks.append(Check(name, MISSING, detail))
    return checks


def _check_sandbox() -> Check:
    """Benchmarks, and later validation runs, need bubblewrap and a user namespace."""
    from cogito.guard import sandbox

    usable, detail = sandbox.available()
    return Check(
        "sandbox (bwrap)",
        OK if usable else WARN,
        detail,
        "" if usable else "install bubblewrap and allow unprivileged user namespaces",
    )


def _check_estella() -> Check:
    """Estella lets `ask` and `recall` answer without spending LLM tokens.

    Without it both tools still answer (`recall` falls back to the graph's full-text
    search), so this is a warning. What is lost is the cheap rung of the ladder, which is
    most of their value.
    """

    settings = settings_config.load()
    if not settings.estella.enabled:
        return Check("estella", WARN, "disabled in config.toml")
    command = _estella_command(settings)
    if not command:
        return Check(
            "estella",
            WARN,
            "not found; ask and recall will fall back to the graph's full-text search",
            fix="the desktop app installs it on its next start; or install cogito-estella "
            "and set [estella].command in config.toml",
        )
    from cogito.graph.estella import (
        estella_version,
        supports_code,
        supports_lite,
        supports_vectors,
    )

    version = estella_version(command)
    detail = f"{command[0]} on {settings.estella.device}, version {version}"
    if not supports_lite(version):
        return Check(
            "estella",
            WARN,
            f"{detail}: older than 0.18, so the app's lite index is off",
            fix="update cogito-estella to 0.20.0 or later",
        )
    features = ["lite"] + (["code"] if supports_code(version) else [])
    features += ["vectors"] if supports_vectors(version) else []
    return Check("estella", OK, f"{detail} ({', '.join(features)})")


def _check_gpu() -> Check:
    if not shutil.which("nvidia-smi"):
        return Check("gpu", WARN, "no nvidia-smi; local models will run on CPU")
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("gpu", WARN, f"nvidia-smi failed: {exc}")
    detail = output or "no device reported"
    # nvidia-smi lists every GPU in the machine; a local model sees only what the CUDA
    # runtime is told to show it.
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        detail += f"\nCUDA_VISIBLE_DEVICES={visible}: local models see only the GPUs it names"
    return Check("gpu", OK, detail)


def _check_neo4j() -> list[Check]:
    from cogito.graph.driver import GraphDriver
    from cogito.setup import neo4j as provisioning

    install = provisioning.detect()
    settings = settings_config.load()

    if not install.present:
        return [
            Check(
                "neo4j",
                MISSING,
                "not installed — the graph is the context store",
                fix="cogito setup neo4j    # downloads and configures it, no root needed",
            )
        ]

    running = provisioning.is_running(install)
    checks = [
        Check(
            "neo4j",
            OK,
            f"{install.kind} install {install.version}, "
            f"{'running' if running else 'stopped'}",
        ),
        Check(
            "neo4j memory",
            OK if install.memory_is_capped else WARN,
            ", ".join(
                f"{k.removeprefix('server.memory.').removesuffix('_size').removesuffix('.size')}"
                f"={v}"
                for k, v in sorted(install.memory.items())
            )
            or "not capped; it can grow into the model server's RAM",
        ),
    ]

    password = secrets.get("neo4j", "password")
    driver = GraphDriver(settings.neo4j, password)
    reachable = driver.is_available()
    driver.close()
    steps = provisioning.missing_steps(install)
    checks.append(
        Check(
            "neo4j bolt",
            OK if reachable else MISSING,
            f"{settings.neo4j.uri} ({'reachable' if reachable else 'unreachable'})"
            + ("" if password else "; no password stored"),
            fix="" if reachable else "\n".join(steps),
        )
    )
    return checks


def _check_models() -> list[Check]:
    try:
        catalog = model_config.load()
    except CogitoError as exc:
        return [Check("models.toml", MISSING, str(exc))]

    if not catalog.entries:
        return [
            Check(
                "models.toml",
                MISSING,
                f"no models registered ({paths.models_file()})",
                fix="cogito models init    # writes a starter catalog you can edit",
            )
        ]

    grouped = catalog.grouped()
    checks = [
        Check(
            "models.toml",
            OK,
            f"{len(grouped['local'])} local, {len(grouped['api'])} api",
        )
    ]
    checks.extend(Check("models.toml", WARN, warning) for warning in catalog.warnings)
    return checks


@click.group()
@click.version_option(__version__, prog_name="cogito")
def main() -> None:
    """Cogito — a coding agent whose harness compensates for the model."""


@main.command()
def doctor() -> None:
    """Report what is present on this machine and what is missing."""
    click.echo(f"cogito {__version__}\n")
    checks: list[Check] = [_check_python()]
    checks.extend(_check_models())
    checks.extend(_check_neo4j())
    checks.append(_check_binary("rg", "ripgrep"))
    checks.append(_check_binary("fd", "fd"))
    checks.append(_check_binary("git", "git"))
    checks.append(_check_sandbox())
    checks.extend(_check_motoris())
    checks.append(_check_estella())
    checks.append(_check_gpu())

    broken = _render(checks)
    click.echo()
    if broken:
        raise SystemExit(f"{broken} required component(s) missing.")
    click.echo("Everything required is present.")


@main.group()
def models() -> None:
    """Inspect and manage the selectable model catalog."""


@models.command("init")
def models_init() -> None:
    """Write a starter models.toml, without overwriting an existing one."""
    target = paths.models_file()
    if target.exists():
        raise click.ClickException(f"{target} already exists; edit it directly")
    paths.ensure(target.parent)
    target.write_text(model_config.STARTER_TOML, encoding="utf-8")
    click.echo(f"Wrote {target}")


@models.command("list")
def models_list() -> None:
    """Show the catalog exactly as the picker groups it."""
    catalog = model_config.load()
    if not catalog.entries:
        raise click.ClickException(
            f"no models registered in {paths.models_file()} — run: cogito models init"
        )
    for origin, entries in catalog.grouped().items():
        if not entries:
            continue
        click.echo(f"\n{origin}:")
        for entry in entries:
            served = "  (cogito serves it)" if entry.is_served_by_cogito else ""
            click.echo(f"  {entry.id:<22} {entry.label}")
            click.echo(f"  {'':<22} {entry.api} -> {entry.base_url}{served}")
    for warning in catalog.warnings:
        click.echo(f"\n! {warning}")


@main.group()
def setup() -> None:
    """Provision the external pieces Cogito needs."""


@setup.command("neo4j")
@click.option("--version", default=None, help="Neo4j version to install.")
@click.option("--password", default=None, help="Bolt password. Generated if omitted.")
@click.option("--yes", is_flag=True, help="Do not ask before downloading.")
@click.option("--configure-only", is_flag=True, help="Only apply config to an existing install.")
def setup_neo4j(
    version: str | None, password: str | None, yes: bool, configure_only: bool
) -> None:
    """Download, configure and start Neo4j under Cogito's own data directory."""
    import secrets as stdlib_secrets

    from cogito.setup import neo4j as provisioning

    wanted = version or provisioning.DEFAULT_VERSION
    install = provisioning.detect(wanted)

    if not install.present:
        if configure_only:
            raise click.ClickException("nothing installed to configure")
        url = provisioning.archive_url(wanted)
        click.echo(f"Neo4j {wanted}")
        click.echo(f"  from   {url}")
        click.echo("  size   ~268 MB, verified against the published sha256")
        click.echo(f"  into   {provisioning.managed_home(wanted)}")
        click.echo("  java   uses the JVM already on this machine; nothing else is installed")
        if not (yes or click.confirm("\nDownload and install?", default=True)):
            raise click.Abort
        with click.progressbar(length=100, label="downloading") as bar:
            state = {"shown": 0}

            def progress(received: int, total: int) -> None:
                if total:
                    percent = int(received * 100 / total)
                    bar.update(percent - state["shown"])
                    state["shown"] = percent

            archive = provisioning.download(wanted, on_progress=progress)
        click.echo(f"extracting {archive.name}")
        provisioning.extract(archive, wanted)
        install = provisioning.detect(wanted)

    provisioning.configure(install)
    click.echo(f"configured {install.config_file}")

    if install.kind == "managed" and not provisioning.is_running(install):
        chosen = password or stdlib_secrets.token_urlsafe(18)
        try:
            provisioning.set_initial_password(install, chosen)
            secrets.set_("neo4j", "password", chosen)
            click.echo("initial password set and stored in secrets.toml")
        except provisioning.ProvisioningError as exc:
            # Already started once: the password is fixed and ours may be wrong.
            click.echo(f"! could not set the initial password: {exc}")

    click.echo(provisioning.start(install))
    click.echo("\nRun `cogito doctor` to confirm Bolt is reachable.")


def _build_provider(entry, settings, *, quiet: bool = False):
    """Turn a catalog entry into a live provider, starting a local server if it owns one."""
    server = _launch_local(entry, settings, quiet=quiet) if entry.is_served_by_cogito else None
    return _connect(entry), server


def _launch_local(entry, settings, *, quiet: bool = False):
    """Start the Cogito Motoris an entry with ``serve`` owns, or attach to one already
    serving it (`MotorisServer`). Returns the server."""
    from cogito.provider.local.motoris import MotorisServer

    log_file = paths.ensure(paths.data_dir() / "logs") / f"{entry.id}.log"
    server = MotorisServer(entry, log_file=log_file)
    click.echo(f"starting {entry.label} (log: {log_file})", err=quiet)
    server.start()
    click.echo(
        "  ready" + ("  [attached to a server already running]" if server.adopted else ""),
        err=quiet,
    )
    return server


def _connect(entry):
    """The provider for an entry's API host. Starts nothing, asks nothing.

    The key is read when the client is created: the entry's environment variable, else its
    `api_key_file`, else the settings screen's store (`secrets.model_key`). A key file that
    is missing or readable by others fails here with the reason."""
    try:
        key = secrets.model_key(entry)
    except CogitoError as exc:
        raise click.ClickException(f"{entry.id}: {exc}") from exc
    if entry.api == "anthropic":
        from cogito.provider.anthropic_api import AnthropicProvider

        return AnthropicProvider(entry, key)
    from cogito.provider.openai_api import OpenAIProvider

    return OpenAIProvider(entry, key)


def _sampling_for(entry, *, seed: int | None = None) -> object:
    """Sampling from the model entry, so the sampler is not a hidden benchmark variable."""
    from dataclasses import replace

    from cogito.provider.base import SamplingParams

    known = {
        "temperature",
        "top_p",
        "top_k",
        "seed",
        "max_tokens",
        "repeat_penalty",
        "repeat_last_n",
        "presence_penalty",
        "frequency_penalty",
        "min_p",
        "dry_multiplier",
        "dry_base",
        "dry_allowed_length",
        "dry_penalty_last_n",
    }
    sampling = SamplingParams(**{k: v for k, v in entry.sampling.items() if k in known})
    return replace(sampling, seed=seed) if seed is not None else sampling


def _estella_command(settings) -> tuple[str, ...]:
    """The Estella this core runs: `[estella] command` when set; else the one the app
    installed (`setup/estella.py`); else `cogito-mcp` on PATH; else () and none runs."""
    from cogito.graph.estella import resolve_command
    from cogito.setup import estella as estella_component

    if settings.estella.command:
        return resolve_command(settings.estella.command)
    return estella_component.command() or resolve_command("")


def _vector_cache(settings):
    """Where the app's own Estella keeps its vector model, or None for a person's own."""
    from cogito.setup import estella as estella_component

    installed = estella_component.command()
    if installed and _estella_command(settings) == installed:
        return estella_component.vector_cache()
    return None


def _conversation_estella(estella, plan: EstellaPlan):
    """`recall`'s Estella: the full graph, which extracts the session's facts, or the lite
    index in a directory of its own (`_estella_plan`)."""
    from cogito.graph.estella import CONVERSATION

    if plan.recall_full:
        return estella.client(CONVERSATION)
    return estella.client(f"{CONVERSATION}-lite", ("--lite",))


def _ask_code(settings) -> bool:
    """`[estella] ask_code`, with an Estella that splits code by line (0.19.3 or later)."""
    from cogito.graph.estella import estella_version, supports_code

    return settings.estella.ask_code and supports_code(estella_version(_estella_command(settings)))


def _graph_model_wanted(entry, settings, components) -> bool:
    """Whether the app adds the graph model to the Estella it installed: `[estella] mode` is
    ``full``, or ``auto`` with a large model (the default, ``lite``, never adds it), that
    Estella has no graph model yet, and the person did not decline it. A person's own Estella is never changed."""
    from cogito.graph.estella import has_graph_model
    from cogito.setup import estella as estella_component

    mode = settings.estella.mode
    if not settings.estella.enabled or mode == "lite":
        return False
    if mode != "full" and not _large_model(entry):
        return False
    command = _estella_command(settings)
    return (
        bool(command)
        and command == estella_component.command()
        and not has_graph_model(command)
        and not components.is_declined(estella_component.FULL_COMPONENT_ID)
    )


def _component_external(load_settings):
    """For the Herramientas tab: whether the person has a component of their own, outside
    the app. Cogito Estella counts when `[estella] command` is set or one is on PATH; the tab
    showed «No instalado» beside a switch that was on and an Estella that answered (QA audit
    2026-10-17, B4)."""
    from cogito.graph.estella import resolve_command
    from cogito.setup import estella as estella_component

    def external(component_id: str) -> bool:
        if component_id != estella_component.COMPONENT_ID:
            return False
        try:
            settings = load_settings()
        except Exception:
            return False
        if settings.estella.command:
            return bool(resolve_command(settings.estella.command))
        return bool(resolve_command(""))

    return external


def _component_unused(load_settings):
    """For the Herramientas tab: whether the settings, read each time it is listed, never
    use a component. The graph model (`estella-full`) only matters when `[estella] mode`
    is ``auto`` or ``full``; with the default, ``lite``, offering its 750 MB changes nothing
    (QA rig, 2026-10-16)."""
    from cogito.setup import estella as estella_component

    def unused(component_id: str) -> bool:
        if component_id != estella_component.FULL_COMPONENT_ID:
            return False
        try:
            return load_settings().estella.mode == "lite"
        except Exception:  # a settings file that cannot be read: offer it, as before
            return False

    return unused


def _estella_install_wanted(settings, components) -> bool:
    """Whether the app installs Cogito Estella: on, none configured or installed or on PATH,
    and not declined. Only a core the desktop app owns asks (`_local_search_wanted`)."""
    from cogito.setup import estella as estella_component

    return (
        settings.estella.enabled
        and not _estella_command(settings)
        and not components.is_declined(estella_component.COMPONENT_ID)
    )


def _grounding_for(entry, settings) -> bool:
    """Whether the app checks this model's answers against what a turn retrieved
    (`loop.grounding`). A `grounding_strict` pin decides; else on for a local model and off
    for an API one (cogito-lab experiments/2026-10-14-grounding-check)."""
    pinned = entry.pins.get("grounding_strict")
    if isinstance(pinned, bool):
        return pinned
    return entry.origin == "local"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_prompt_time(value: str | None) -> datetime | None:
    """Parse one explicit, timezone-aware clock value for reproducible measurements."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise click.BadParameter("must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise click.BadParameter("must include a timezone, for example +00:00")
    return parsed.astimezone(UTC)


def _voice_tools(voice_settings):
    """`voice_listen` and `voice_speak`, built on cogito-vox in this process.

    Nothing opens the microphone or loads a model here. That waits for the first
    call, so enabling voice costs a run nothing until the model uses it.
    """
    try:
        from cogito_vox import VoiceIO
    except ImportError as exc:
        raise click.ClickException(
            "[voice] enabled = true needs cogito-vox: install Cogito with the voice "
            "extra (uv sync --extra voice), or set enabled = false"
        ) from exc
    from cogito.tools.voice import VoiceListenTool, VoiceSpeakTool

    voice = VoiceIO.open(
        catalog_path=Path(voice_settings.engines_file) if voice_settings.engines_file else None,
        weights_dir=Path(voice_settings.weights_dir) if voice_settings.weights_dir else None,
        use_aec=voice_settings.echo_cancellation,
    )
    return VoiceListenTool(voice), VoiceSpeakTool(voice)


#: What the model is told when an asked call did not run, by who decided. The person's no
#: says it was theirs: told «you did not have permission», a model asked the person to
#: approve the install they had just declined (QA rig, 2026-10-16).
_DENIALS = {
    "user": "the person you are working with declined it; nothing ran",
    "nobody": "nobody was available to approve it; nothing ran",
    "interrupted": "the turn was stopped before this was answered; nothing ran",
    "timeout": "nobody answered in time; nothing ran",
}


def _external_command(arguments) -> str:
    """The arguments of an MCP tool call as sent, for the card's «Ver detalles», cut where
    a card stops being readable."""
    import json

    sent = json.dumps(dict(arguments), ensure_ascii=False, indent=1, sort_keys=True)
    return f"{sent[:2000]}{' …' if len(sent) > 2000 else ''}"


_MCP_POOL = None
_MCP_LOCK = threading.Lock()


def _mcp_pool():
    """The MCP servers this process runs, shared by its sessions and the Conexiones tab: a
    server such as a disassembler keeps what it opened across conversations, and starting
    it per conversation would repeat its start (and an `npx` download check) every time."""
    global _MCP_POOL
    from cogito.tools.mcp.hub import McpPool

    with _MCP_LOCK:
        if _MCP_POOL is None:
            catalog = None
            if _app_owned():
                # APP ONLY: each server's tool list from its last start, so a conversation
                # registers its tools at once while it starts (`tools/mcp/catalog.py`).
                from cogito.tools.mcp.catalog import ToolCatalog

                catalog = ToolCatalog()
            _MCP_POOL = McpPool(remember=_mcp_remember, catalog=catalog)
            atexit.register(_MCP_POOL.close)
        return _MCP_POOL


def _mcp_remember(name: str, fields: dict[str, object]) -> None:
    """What a container start found (its HTTP port and path), kept in the server's table so
    the next start goes straight to it."""
    from cogito.config import mcp_servers

    mcp_servers.set_fields(name, fields)


def _mcp_hub(settings, *, stable: bool = False, late: bool = False):
    """One session's view of the pooled servers: its own selection of what each step shows.
    ``stable`` fixes that selection for the conversation, until a fold (`McpHub`). ``late``
    (APP ONLY) lets a server that was not ready join later and says which are not."""
    from cogito.tools.mcp.hub import McpHub

    hub = McpHub(settings.mcp, pool=_mcp_pool(), stable=stable, late=late)
    hub.connect()
    return hub


def _mcp_joiner(hub, registry, guard, settings):
    """APP ONLY. Registers the tools of servers that became ready (`McpHub.join`), with the
    guard's questions for those that may change something, and names them in `mcp_find`."""
    from cogito.tools.mcp.hub import McpFindTool, McpTool

    def join(*, quiet: bool = False):
        added = hub.join(quiet=quiet)
        for offered in added:
            if not registry.has(offered.name):
                registry.register(McpTool(hub, offered))
        if added:
            guard.ask_before(_mcp_asked(hub, settings))
        if added and not registry.has("mcp_find"):
            registry.register(McpFindTool(hub))
        elif hub.relisted and registry.has("mcp_find"):
            registry.get("mcp_find").refresh()
        hub.relisted = False
        return added

    return join


def _with_project_servers(settings, project_root):
    """APP ONLY. The servers of the project's `.mcp.json` that the person approved
    (`tools/mcp/project.py`), beside config.toml's; a name config.toml has wins."""
    from cogito.tools.mcp import project

    found = project.approved(Path(project_root))
    extra = {name: server for name, server in found.items() if name not in settings.mcp.servers}
    if not extra:
        return settings
    return replace(settings, mcp=replace(settings.mcp, servers={**settings.mcp.servers, **extra}))


def _mcp_turn_note(hub, join):
    """APP ONLY. Where the person writes: servers that became ready join, and the model is
    told once which are still starting or failed (`McpHub.note`)."""

    def note(text: str):
        join()
        return hub.note()

    return note


def _turn_notes(*notes):
    """One `turn_note` for every wired note, or None when none is, so a session without
    them passes exactly what it passed before. The MCP note goes first: it opens with the
    harness prefix the thread reads back (`loop.readback`)."""
    wired = [note for note in notes if note is not None]
    if not wired:
        return None
    if len(wired) == 1:
        return wired[0]

    def each(text: str):
        said = [part for note in wired if (part := note(text))]
        return "\n\n".join(said) if said else None

    return each


def _app_owned() -> bool:
    """Whether the desktop app started this core (it passes its own pid,
    `COGITO_PARENT_PID`): the lab's suite holder runs `serve` too, and its sessions must
    send what they always sent."""
    return os.environ.get("COGITO_PARENT_PID", "").strip().isdigit()


def _computer_wanted() -> bool:
    """Whether this machine has a desktop the `computer` tool can reach: Linux with a
    session bus, where the portals answer (`tools.desktop.portal.available`)."""
    if not sys.platform.startswith("linux"):
        return False
    from cogito.tools.desktop.portal import available

    return available()


def _terminal_wanted(in_app: bool) -> bool:
    """APP ONLY: terminal tabs need a session the desktop app owns and a POSIX PTY (Windows'
    ConPTY is not built: there the window shows no terminal button)."""
    return in_app and _app_owned() and os.name == "posix"


def _mcp_switched_off(settings) -> tuple[str, ...]:
    """The MCP servers the person switched off in Conexiones: a skill that names one is not
    offered (`tools.skills.skills_for`)."""
    return tuple(name for name, server in settings.mcp.servers.items() if not server.enabled)


def _mcp_wanted(settings, in_app: bool) -> bool:
    """APP ONLY by default, like the web tools: `chat`, and so every benchmark task,
    registers MCP tools only with `[mcp] in_chat`."""
    return bool(settings.mcp.servers) and (in_app or settings.mcp.in_chat)


def _mcp_asked(hub, settings):
    """The tools the guard asks about, by registered name: those that may change something."""
    from cogito.guard.policy import ExternalTool
    from cogito.tools.mcp.connections import tool_title

    asked = {}
    for offered in hub.offered():
        if offered.remote.destructive:
            asked[offered.name] = ExternalTool(
                server=offered.server,
                tool=offered.remote.name,
                title=tool_title(offered.remote),
                changes=not offered.remote.read_only,
            )
    return asked


def _mcp_add_asked():
    """APP ONLY. `mcp_add` is asked about on every call, automatic mode included: each one
    saves a different program to run in every later conversation."""
    from cogito.guard.policy import ExternalTool

    return ExternalTool(
        server="Cogito", tool="mcp_add", title="Agregar un servidor MCP", changes=True,
        session_only=True, every_time=True,
    )


def _with_fresh_extensions(settings):
    """`[mcp]` and `[skills]` as the config file says now, so a server or skill added while
    the app runs (by the person, or by the model at their request) is there in the next
    conversation without a restart. A file that no longer parses keeps what was loaded."""
    try:
        current = settings_config.load()
    except ConfigError as exc:
        click.echo(f"config.toml: {exc}; keeping the MCP and skills settings already loaded",
                   err=True)
        return settings
    return replace(settings, mcp=current.mcp, skills=current.skills)


def _skills_wanted(settings, in_app: bool) -> bool:
    """APP ONLY by default: `chat`, and so every benchmark task, registers `skill` only with
    `[skills] in_chat`."""
    return settings.skills.enabled and (in_app or settings.skills.in_chat)


def _person_text_listeners(*listeners):
    """One `on_person_text` for every listener that is wired, or None when none is, so a
    session without the web or MCP passes exactly what it passed before."""
    wired = [listener for listener in listeners if listener is not None]
    if not wired:
        return None
    if len(wired) == 1:
        return wired[0]

    def each(text: str) -> None:
        for listener in wired:
            listener(text)

    return each


def _guard_hook(guard, record, session_id: str, installer=None, described=None):
    """Wire the guard into the loop's BEFORE_TOOL seam.

    Refusals fire here so the model learns at once and no process is spawned. Anything a
    PATH shim will intercept is let through, because the shim asks at the point where it
    can also carry the answer out.

    APP ONLY: ``installer`` (`guard.installer.install_packages`) runs an approved system
    package install in place of the model's own command, which could never get a password.
    """
    from cogito.guard.policy import Decision
    from cogito.loop.hooks import HookDecision

    def hook(point, payload):
        call = payload["call"]
        arguments = dict(call.input)
        verdict = guard.evaluate(call.name, arguments)

        if verdict.decision is Decision.REFUSE:
            record(
                "approval_recorded",
                session_id=session_id,
                id=call.id,
                kind=call.name,
                target=str(arguments.get("command") or arguments.get("path") or "")[:400],
                decision="refused",
                scope="",
            )
            return HookDecision("deny", verdict.reason)

        if verdict.decision is Decision.ASK and not guard.defers_to_shim(verdict):
            command = (
                described[call.name](arguments)
                if described and call.name in described
                else _external_command(arguments)
                if verdict.external is not None
                else verdict.terminal.command
                if verdict.terminal is not None
                else str(arguments.get("command", ""))
            )
            response = guard.answer(verdict, command)
            allowed = response.allowed
            # What the pill names when the thread is drawn again: an MCP tool by its server
            # and title, as the live card did, not by its arguments.
            named = (
                f"{verdict.external.server} · {verdict.external.title}"
                if verdict.external is not None
                else command
            )
            record(
                "approval_recorded",
                session_id=session_id,
                id=call.id,
                kind=call.name,
                target=named[:400],
                decision="allowed" if allowed else "denied",
                scope="",
            )
            if not allowed:
                # Nobody refused a question the person stopped, or one nobody answered.
                return HookDecision("deny", _DENIALS.get(response.reason, _DENIALS["user"]))
            if verdict.install is not None:
                if installer is None:
                    return HookDecision("deny", "package installs are not available here")
                outcome = installer(verdict.install, payload["context"].cancel)
                return HookDecision(
                    "result",
                    outcome.text,
                    {
                        "is_error": not outcome.ok,
                        "error_kind": (
                            "" if outcome.ok
                            else "cancelled" if outcome.status == "interrupted"
                            else f"install_{outcome.status}"
                        ),
                        "metadata": {
                            "exit_code": outcome.exit_code,
                            "install": {
                                "manager": verdict.install.manager,
                                "packages": list(verdict.install.packages),
                                "status": outcome.status,
                            },
                        },
                    },
                )
        return None

    return hook


def _follow_compaction(recall):
    """Keep `recall` addressed at the session the conversation is actually in.

    Compaction creates a child session. The lineage walk reaches the old messages either
    way, but pointers are labelled relative to "here", and here has moved.
    """

    def hook(point, payload):
        folded = payload.get("compaction")
        if folded is not None and getattr(folded, "child_session_id", ""):
            recall.follow(folded.child_session_id)
        return None

    return hook


@dataclass(frozen=True)
class EstellaPlan:
    """Which of a session's Estella graphs extract facts with the graph model (full) and
    which only index sentences (lite). Resolved once per session by `_estella_plan`."""

    ask_full: bool
    recall_full: bool
    documents_full: bool


def _large_model(entry) -> bool:
    """No parameter count without the GGUF header; `[estella] mode = "auto"` treats a local
    model as small (2026-10-27)."""
    return entry.origin != "local"


def _estella_plan(entry, settings, in_app: bool) -> EstellaPlan:
    """Full or lite, per graph, never asked. In the app every model gets lite by default
    (`[estella] mode`, decided by the person on 2026-10-15 once full matched lite for large
    models); ``auto`` gives a large model the full graph for `ask` and `recall`, and for
    pages and offloaded content only on a GPU, and ``full`` gives it to every model. Full needs the graph model; without it every graph runs
    lite. `chat`, and so every benchmark task, keeps what it had: the full graph wherever the
    graph model is installed."""
    from cogito.graph.estella import estella_version, has_graph_model, supports_lite

    command = _estella_command(settings)
    if not supports_lite(estella_version(command)):
        return EstellaPlan(ask_full=True, recall_full=True, documents_full=True)
    graph = has_graph_model(command)
    if not in_app:
        return EstellaPlan(ask_full=graph, recall_full=graph, documents_full=graph)
    mode = settings.estella.mode
    full = graph and (mode == "full" or (mode == "auto" and _large_model(entry)))
    return EstellaPlan(
        ask_full=full, recall_full=full, documents_full=full and settings.estella.device == "cuda"
    )


def _ask_estella(estella, plan: EstellaPlan):
    """`ask`'s Estella: the full graph, or the lite index in a directory of its own."""
    from cogito.graph.estella import DOCS

    return estella.client(DOCS) if plan.ask_full else estella.client(f"{DOCS}-lite", ("--lite",))


def _document_index(settings, estella, project_root: Path, record, *, busy=None, lite=False):
    """The session's `graph.documents.DocumentIndex`: Estella's documents corpus for the
    installed Estella, started the first time a long document needs it. ``busy``, in the
    app, says whether a turn runs, so parts are ingested only between turns."""
    from cogito.graph.documents import DocumentIndex
    from cogito.graph.estella import (
        DOCUMENTS,
        estella_version,
        supports_lite,
        version_slug,
    )

    version = estella_version(_estella_command(settings))
    # A lite graph holds sentences only; it keeps its own directory so a full graph is never
    # read as one, nor the other way round.
    lite = lite and supports_lite(version)
    name = f"{DOCUMENTS}/{version_slug(version)}" + ("-lite" if lite else "")
    extra = ("--lite",) if lite else ()
    return DocumentIndex(
        lambda: estella.client(name, extra),
        paths.estella_graph_dir(project_root) / name,
        record=record,
        estella_version=version,
        min_chars=settings.estella.documents_min_chars,
        busy=busy,
    )


def _offer_document(index, path: Path, *, message_id, via: str, record: bool = True) -> bool:
    """Hand ``path`` to the document index if it is a document, and say whether it was one.
    Conversion goes through the user's cache, which the `read` or attachment that brought it
    here just filled, so this is a lookup, not a second conversion."""
    from cogito.graph.documents import indexed_document
    from cogito.tools import document_cache, documents

    converted = document_cache.convert(path)
    found = converted.result
    if not isinstance(found, documents.Document) or converted.sha256 is None:
        return False
    index.offer(
        indexed_document(
            sha256=converted.sha256,
            kind=found.kind,
            describe=found.describe(),
            name=path.name,
            path=str(path),
            pages=found.pages,
            sheets=found.sheets,
            units=documents.units(found),
            lines=len(found.text.rstrip("\n").split("\n")),
        ),
        message_id=message_id,
        via=via,
        record=record,
    )
    return True


def _offer_text(index, store, text: str, label: str, *, message_id, record: bool = True) -> None:
    """Offer pasted text that was offloaded to the document index, as its readable form
    (`tools.documents.from_text`), saved beside the session's other artifacts so the
    pointers `ask_document` gives can be read. Text no longer than one part is left out:
    `read` shows it whole in one call. The name is what the model passes as `document`:
    ``label`` (an attached file's own name), or for a message or a paste, ``label`` and the
    page's title or first line."""
    from cogito.context import attachments
    from cogito.graph.documents import indexed_document
    from cogito.tools import documents

    if store is None or len(text) <= documents.PART_MAX_CHARS:
        return
    readable = documents.from_text(text)
    if not readable.text.strip():
        return
    saved = store.stash(readable.text, kind="user_message_text")
    name = label
    if label in ("message", attachments.PASTED_NAME):
        head = documents.headline(text, readable)
        name = f"{label}: {head}" if head else label
    index.offer(
        indexed_document(
            sha256=saved.sha256,
            kind=readable.kind,
            describe=readable.describe(),
            name=name,
            path=str(saved.path),
            pages=None,
            sheets=(),
            units=documents.units(readable),
            lines=len(readable.text.rstrip("\n").split("\n")),
        ),
        message_id=message_id,
        via="offload",
        record=record,
    )


def _documents_from_offloads(index, project_root: Path, store):
    """``(message_id, text) -> bool``: when ``text`` is the notice of a message that was
    too large for the window (`context.inbound`, a steer's included), offer what it
    replaced to the document index with ``via="offload"`` and return True. The person's
    words and each pasted text go as text; each attached document as itself. Anything else
    returns False. The engine calls it for a message offloaded in place (``on_offload``),
    `_documents_from_attachments` for one offloaded as it was appended, and
    `_reoffer_offloads` for one a reopened conversation still holds, with ``record`` off."""
    import logging

    from cogito.context import attachments, inbound
    from cogito.loop.readback import steer_text

    def offer(message_id: str, text: str, *, record: bool = True) -> bool:
        steer = steer_text(text)
        whole = inbound.saved(steer if steer is not None else text, project_root)
        if whole is None:
            return False
        try:
            words, sent = attachments.split(whole)
            if words.strip():
                _offer_text(index, store, words, "message", message_id=message_id, record=record)
            for item in sent:
                offered = (
                    item.kind == "file"
                    and bool(item.document and item.path)
                    and _offer_document(
                        index,
                        Path(item.path),
                        message_id=message_id,
                        via="offload",
                        record=record,
                    )
                )
                if not offered:
                    _offer_text(
                        index, store, item.content, item.name, message_id=message_id, record=record
                    )
        except Exception:  # indexing is an extra; it never costs the turn
            logging.getLogger(__name__).warning(
                "an offloaded message could not be indexed", exc_info=True
            )
        return True

    return offer


def _reoffer_offloads(offloads, continuation) -> None:
    """Offer the document index, again, what a reopened conversation holds as offload
    notices. The index lives in the process, so without this a restart would hide
    `ask_document` for a page still in the window and never finish indexing it. Nothing is
    journalled, so a restart writes nothing; parts already in Estella are not sent again
    (the index's ledger)."""
    for stored in continuation.transcript:
        if stored.role == "user":
            offloads(stored.id, stored.text, record=False)


def _local_search_wanted() -> bool:
    """Whether this core runs the local SearXNG: it was started by the desktop app, which
    passes its own pid (`COGITO_PARENT_PID`; the lab's suite holder does not), and the
    person did not opt out with `COGITO_NO_LOCAL_SEARCH=1`."""
    owned = os.environ.get("COGITO_PARENT_PID", "").strip().isdigit()
    return owned and os.environ.get("COGITO_NO_LOCAL_SEARCH", "") != "1"


def _web_wanted(settings, in_app: bool) -> bool:
    """Whether this session registers the web tools. `chat` asks `[web] in_chat` and nothing
    else. In the app they are always on (`config.search`)."""
    if not in_app:
        return settings.web.in_chat
    return True


def _estella_present(settings) -> bool:
    """APP-ONLY, read on every step: whether an Estella exists to answer (the person's,
    the app's own install, or one on PATH). Without one, `ask` could only ever say it is
    "warming up" (QA audit 2026-10-17, B4), so it is not offered; the app installing one in
    the background brings it back. `recall` and `ask_document` have their own fallbacks (the
    graph's full-text index, the page's own lines) and stay."""
    try:
        return bool(_estella_command(settings))
    except Exception:
        return True


def _document_memory_on() -> bool:
    """APP-ONLY. «Memoria de documentos» in Herramientas: off is a saved "use" decline of
    Cogito Estella (`config.decisions`), read on every step. With it off the model is not
    offered `ask`, `recall` or `ask_document`, and nothing is looked up for it after a fold.
    Before 2026-10-17 the switch only stopped the app installing Estella (QA audit)."""
    from cogito.config.decisions import DecisionStore
    from cogito.setup import estella as estella_component

    try:
        return not DecisionStore().is_declined(estella_component.COMPONENT_ID, "use")
    except (OSError, ValueError):
        return True


#: How long `web_fetch` waits for Estella to load before it answers from the page's own
#: lines. Short on purpose: a fetch has a fallback and never waits on the index.
_WEB_ANSWER_WAIT_SECONDS = 2.0


class _WebDocuments:
    """Where `web_fetch` offers a page it saved: the session's document index, as a
    document the conversation holds (``via="fetch"``). In the app it also answers a question
    about that page from the index when the index is ready."""

    def __init__(self, index, knowledge=None) -> None:
        self._index = index
        self._knowledge = knowledge

    def offer(self, document) -> bool:
        return self._index.offer(document, message_id=None, via="fetch")

    def ready(self, document, seconds: float, cancel=None) -> bool:
        """Push ``document`` ahead of the between-turns queue and wait until Estella holds
        every part of it, ``seconds`` at most, or until the person stops the turn."""
        found = self._index.find(document.path)
        if found is None or seconds <= 0:
            return False
        self._index.hurry(found)
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            if cancel is not None and cancel.cancelled:
                return False
            if self._index.wait(found, min(0.5, left)):
                return True
        return False

    def answer(self, document, question: str, budget: int) -> str | None:
        if self._knowledge is None:
            return None
        found = self._index.find(document.path)
        if found is None:
            return None
        reply = self._knowledge.indexed_answer(found, question, budget)
        return reply.text if reply is not None else None


def _web_addresses_from_files(web_state):
    """The addresses in what the model read from the project (`read`, `grep`) become
    readable by `web_fetch` (AFTER_TOOL). An address in the model's own words, or in the
    system prompt, never does."""

    def hook(point, payload):
        call, result = payload.get("call"), payload.get("result")
        if call is None or result is None or result.is_error:
            return None
        if call.name in ("read", "grep"):
            web_state.note_file(result.content)
        return None

    return hook


def _shell_needs_person(run_terminal):
    """APP ONLY: tell `run_in_terminal` what a `shell` call ran and printed (AFTER_TOOL)."""

    def hook(point, payload):
        call, result = payload.get("call"), payload.get("result")
        if call is not None and result is not None and call.name == "shell":
            run_terminal.note_shell(str(call.input.get("command") or ""), result.content)
        return None

    return hook


def _reseed_web_addresses(web_state, continuation) -> None:
    """Rebuild, for a reopened conversation, which addresses it held: the person's own
    words, what `read` and `grep` returned, and the addresses of `web_search` results.
    Links found inside a fetched page are not rebuilt: the safe side."""
    from cogito.tools.web import urls as web_urls

    for message in continuation.transcript:
        if message.point in ("instruction", "steer"):
            web_state.note_person(message.text)
    for calls in continuation.calls.values():
        for call in calls:
            name = call.properties.get("name")
            result = str(call.properties.get("result") or "")
            if not call.properties.get("ok", True):
                continue
            if name in ("read", "grep"):
                web_state.note_file(result)
            elif name == "web_search":
                web_state.fetchable.add_text(result, web_urls.SEARCH)


def _documents_from_reads(index):
    """Offer the index a document that `read` just opened (AFTER_TOOL). Only a read that
    succeeded and reported a document; anything else passes through untouched."""
    import logging

    def hook(point, payload):
        call, result = payload.get("call"), payload.get("result")
        if call is None or result is None or call.name != "read" or result.is_error:
            return None
        if "document" not in result.metadata or not result.files_touched:
            return None
        try:
            _offer_document(
                index,
                Path(result.files_touched[0]),
                message_id=payload.get("message_id"),
                via="read",
            )
        except Exception:  # indexing is an extra; it never costs the turn
            logging.getLogger(__name__).warning(
                "a document read could not be indexed", exc_info=True
            )
        return None

    return hook


def _documents_from_attachments(index, project_root: Path, after, offloads, *, attached=True):
    """Every message as `Engine` records it (``on_message``). A message too large to send
    whole goes to ``offloads`` (`_documents_from_offloads`), which offers what it held.
    With ``attached``, a document the person attached to any other message is offered too,
    read back the way the thread reads a message (`loop.readback`). Then ``after``
    (`recall`) gets the message as before."""
    import logging

    from cogito.loop.readback import person_message

    def on_message(message_id: str, text: str) -> None:
        try:
            if not offloads(message_id, text) and attached:
                _, sent = person_message(text, project_root)
                for item in sent:
                    if item.kind == "file" and item.document and item.path:
                        _offer_document(
                            index, Path(item.path), message_id=message_id, via="attachment"
                        )
        except Exception:  # indexing is an extra; it never costs the turn
            logging.getLogger(__name__).warning(
                "an attached document could not be indexed", exc_info=True
            )
        if after is not None:
            after(message_id, text)

    return on_message


def _clear_read_snapshots_on_compact(tracker):
    """After a fold the model no longer sees the old turns' text, including what a `read`
    inside them showed. `edit`'s bare-line-number leniency holds only while that text is in
    view, so compaction drops every read snapshot in the same turn it drops the messages
    (strict review of edit-anchor-leniency, item 4). `was_read` (write's overwrite guard) is
    unaffected: a fold does not touch the files on disk.
    """

    def hook(point, payload):
        tracker.clear_snapshots()
        return None

    return hook


def _allow_unattended(request) -> object:
    """Approve without asking. Only reachable behind --allow-destructive."""
    from cogito.guard.approval import ApprovalResponse

    click.echo(f"  [auto-approved] {request.summary} {request.command}", err=True)
    return ApprovalResponse(allowed=True, note="unattended")


def _terminal_approver(request) -> object:
    """Ask in the terminal. The desktop app answers the same request with a toast."""
    from cogito.guard.approval import ApprovalResponse

    click.echo(f"\n  {request.summary}", err=True)
    click.echo(f"    {request.command}", err=True)
    for target in request.targets[:8]:
        click.echo(f"      {target}", err=True)
    if len(request.targets) > 8:
        click.echo(f"      ... and {len(request.targets) - 8} more", err=True)
    for raw in request.unresolved:
        click.echo(f"      {raw}   (the shell will expand this; it cannot be shown)", err=True)
    for path in request.within:
        click.echo(f"      whatever it matches under {path}", err=True)
    if request.listed:
        click.echo("    It would remove:", err=True)
        for name in request.listed[:8]:
            click.echo(f"      {name}", err=True)
        more = request.listed_more + max(0, len(request.listed) - 8)
        if more:
            click.echo(f"      ... and {more} more", err=True)
    if request.recovery == "trash":
        click.echo("    They go to the trash and can be restored with `cogito undo`.", err=True)
    elif request.recovery == "kept":
        click.echo("    Nothing is deleted: it is only moved.", err=True)
    else:
        click.echo("    This cannot be undone.", err=True)

    options = "[y] yes  [n] no"
    if request.suggested_scope is not None:
        options += f"  [a] yes, and stop asking for {request.suggested_scope}"
    while True:
        answer = click.prompt(f"    {options}", default="n", show_default=False, err=True)
        choice = answer.strip().lower()[:1]
        if choice == "y":
            return ApprovalResponse(allowed=True)
        if choice == "n":
            return ApprovalResponse(allowed=False)
        if choice == "a" and request.suggested_scope is not None:
            return ApprovalResponse(allowed=True, remember_prefix=request.suggested_scope)


# ---------------------------------------------------------------------------
# EngineSession: the extracted composition root
# ---------------------------------------------------------------------------


class EngineSession:
    """Wraps all resources built from a provider.

    Provides ``run(text, state) -> TurnResult`` and ``close()``.
    ``close()`` releases: end session, close shell, stop broker, close estella, close store.
    """

    def __init__(
        self,
        project_root: Path,
        settings,
        entry,
        session_id: str,
        project_id: str,
        store,
        guard,
        broker,
        shell_session,
        registry,
        estella,
        recall,
        hooks,
        engine,
        driver=None,
        undo=None,
        tracker=None,
    ):
        self._project_root = project_root
        self._settings = settings
        self._entry = entry
        self._session_id = session_id
        self._project_id = project_id
        self._store = store
        self._guard = guard
        self._broker = broker
        self._shell_session = shell_session
        self._registry = registry
        self._estella = estella
        self._recall = recall
        self._hooks = hooks
        self._engine = engine
        #: Backs "Volver a un punto" (rewind): the same `GraphDriver` and `UndoStore` this
        #: session's tools use. `None` only in tests that build an `EngineSession` without
        #: rewind; only `rewind_dry_run`/`rewind_perform`/`rewind_redo` touch either.
        self._driver = driver
        self._undo = undo
        #: The `FileTracker` that `read`/`write`/`edit` share. `rewind_perform` and
        #: `rewind_redo` change files on disk without those tools, so a read snapshot left
        #: behind would be trusted against content that no longer exists (strict review of
        #: edit-anchor-leniency, item 4). `None` only where `driver`/`undo` are `None` too.
        self._tracker = tracker
        #: `serve`'s per-session `MeteredProvider` (`attach_meter`), for the window's
        #: `service_error` event. None everywhere else: `chat` never meters.
        self._meter = None
        #: Called once from `close`, so `serve`'s register of the conversations its live
        #: engines write to (`_OpenConversations`) releases this one.
        self._on_close: list = []
        #: APP ONLY: the composer's automatic mode (`guard.auto.AutoMode`), which `serve`
        #: sets after building the session. None everywhere else: nothing answers for the
        #: person.
        self.auto = None
        #: The provider as the session was built (or switched to), while a turn runs with the
        #: composer's thinking choice (`_use_thinking`); None while none is in force.
        self._thinking_base = None
        #: The base's `configured` copy for each level, kept so what a copy learns about the
        #: model (that it reasons, that it sees images) lasts across turns at that level.
        self._thinking_copies: dict = {}
        #: APP ONLY: the session's terminal tabs (`tools.terminal.host.TerminalHost`), which
        #: `server/terminals.py` reaches; None where there are none (`chat`, Windows).
        self.terminals = None
        #: APP ONLY: the `browser` tool's bridge to the desktop's main process
        #: (`server.browsers.BrowserBridge`), which `server/browsers.py`'s answers route
        #: reaches; None outside a core the app owns.
        self.browser_bridge = None

    def sees_images(self) -> bool:
        """Whether the model in use takes images (`Engine.sees_images`)."""
        return self._engine.sees_images()

    def attach_meter(self, meter) -> None:
        self._meter = meter

    @property
    def meter(self):
        """This session's `MeteredProvider`, or None where nothing is metered."""
        return self._meter

    @property
    def entry(self):
        """The catalog entry this session's requests go to now: the window's picker moves
        it (`use_model`)."""
        return self._entry

    def use_model(self, entry, provider, meter=None) -> None:
        """Send every later request of this session to `entry`'s model through `provider`, on
        the same transcript (`Engine.use_model`). The window's picker calls it through
        `_ServedModel.switch`. `meter`, when this session is metered, is the one `provider` is
        or wraps, so a provider failure is reported under the new model's name."""
        self._entry = entry
        if meter is not None:
            self._meter = meter
        # The new model's provider carries its own options; a thinking choice is applied
        # again from it at the next turn.
        self._thinking_base = None
        self._thinking_copies = {}
        self._engine.use_model(
            provider,
            model_id=entry.id,
            sampling=_sampling_for(entry),
            identity=_model_identity(entry, self._settings),
        )

    def last_service_error(self) -> dict | None:
        """The last provider failure of this session's turn, as the server's
        ``service_error`` event fields, or None. Matches ``server.app.EngineLike``."""
        failure = self._meter.last_error if self._meter is not None else None
        return failure.as_event() if failure is not None else None

    @property
    def session_id(self) -> str:
        return self._engine.session_id

    @property
    def context_status(self):
        """How full the model's context window is now (see `Engine.context_status`).
        `None` until this session's engine has sampled at least once."""
        return self._engine.context_status

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def pending_count(self) -> int:
        return self._store.pending_count

    @property
    def degraded(self) -> bool:
        return self._store.degraded

    def run(self, text: str, state, attachments=(), skills=(), thinking=None) -> object:
        """Execute one turn and return the result.

        Takes the ``RunState`` from the caller because the caller holds the ``CancelToken``
        it was built from. Chat resets and reuses one token across turns; the server holds
        one per session so its ``/interrupt`` route can cancel a turn in flight. Matches
        ``server.app.EngineLike`` exactly, which is the point of the extraction. ``skills`` is
        the Agent Skills the person activated in the composer (app-only); `chat` passes none.
        ``thinking`` is the composer's thinking level (`config.settings.THINKING_LEVELS`), or
        None for the session's own budget.
        """
        if self._meter is not None:
            # Only this turn's own failure may be reported at its end.
            self._meter.clear_last_error()
        self._use_thinking(thinking)
        extra: dict[str, object] = {}
        if attachments:
            extra["attachments"] = attachments
        if skills:
            extra["skills"] = skills
        return self._settled(self._engine.run(text, state, **extra))

    def wake(self, state) -> object:
        """APP-ONLY. A turn for background commands that ended while none ran
        (`Engine.wake`), started by the server, never by `chat`."""
        if self._meter is not None:
            self._meter.clear_last_error()
        return self._settled(self._engine.wake(state))

    def pending_notice_count(self) -> int:
        """How many background reports wait for the model (`Engine.pending_notice_count`)."""
        return self._engine.pending_notice_count()

    def _use_thinking(self, level) -> None:
        """Send this turn's requests with the thinking ``level`` the composer chose, or, with
        None, as the session was built. A provider without options is left as it is."""
        held = getattr(self, "_thinking_base", None)  # sessions built without __init__ have none
        if level is None and held is None:
            return
        base = held if held is not None else self._engine.provider
        if level is None:
            self._engine.use_provider(base)
            self._thinking_base = None
            return
        options = getattr(base, "options", None)
        if options is None or not hasattr(base, "configured"):
            return
        self._thinking_base = base
        copies = getattr(self, "_thinking_copies", None)
        if copies is None:
            copies = self._thinking_copies = {}
        copy = copies.get(level)
        if copy is None:
            copy = copies[level] = base.configured(_with_thinking(options, level, self._settings))
        self._engine.use_provider(copy)

    def _settled(self, result):
        """Report the failure the turn stopped on (`MeteredProvider.settle`). A later failure
        on the way, such as a fold's summary request, never stands in for it."""
        if self._meter is not None:
            self._meter.settle(getattr(self._engine, "stopped_on", None))
        return result

    @property
    def can_resume(self) -> bool:
        """Matches ``server.app.EngineLike.can_resume``; see ``Engine.can_resume``."""
        return self._engine.can_resume

    def resume(self, state) -> object:
        """Carry on the turn a provider failure stopped, without sending the instruction
        again. Matches ``server.app.EngineLike.resume``; see ``Engine.resume``."""
        if self._meter is not None:
            self._meter.clear_last_error()
        return self._settled(self._engine.resume(state))

    def steer(self, steer_id: str, text: str, attachments=()) -> None:
        """Queue a message for the turn running on this engine (see ``Engine.steer``).
        Matches ``server.app.EngineLike.steer`` exactly, for the same reason as ``run``."""
        if attachments:
            self._engine.steer(steer_id, text, attachments)
        else:
            self._engine.steer(steer_id, text)

    def pending_steer_count(self) -> int:
        """Matches ``server.app.EngineLike.pending_steer_count``; see
        ``Engine.pending_steer_count``."""
        return self._engine.pending_steer_count()

    def drain_leftover_steers(self):
        """Matches ``server.app.EngineLike.drain_leftover_steers``; see
        ``Engine.drain_leftover_steers``."""
        return self._engine.drain_leftover_steers()

    def start_fresh(self) -> str:
        """Begin a fresh conversation in this project ("reset context", design 1a). Matches
        ``server.app.EngineLike.start_fresh``; see ``Engine.start_fresh``. The old session's
        lineage is left whole; the new root's id is returned. Commands the old conversation
        left in the background keep running, but their ends are no longer reported: no
        message of the new one knows them. The same for a line still running in a terminal
        tab, which stays open."""
        shell = getattr(self, "_shell_session", None)
        if shell is not None:
            shell.quiet_background()
        terminals = getattr(self, "terminals", None)
        if terminals is not None:
            terminals.quiet()
        if (drop := getattr(self._engine, "drop_notices", None)) is not None:
            drop()
        return self._engine.start_fresh()

    # -- rewind ("Volver a un punto") ------------------------------------------------
    #
    # `server.app` cannot import `guard` (the server depends only on `graph` and `loop`), so
    # the boundary is crossed here. Every method below returns a plain dict, with an
    # ``"error"`` key (``"not_found"``, ``"conflict"``, ``"already_redone"``, ``"moved_on"``
    # or ``"failed"``) in place of a `guard` exception. The route maps each error kind to a
    # status code without importing `guard`.
    #
    # Every method uses ``self.session_id`` (the engine's current session), never
    # ``self._session_id`` (fixed at construction). A fold reassigns the engine's session,
    # and a point recorded after a fold lives in a session `self._session_id` never learns
    # about (strict review, compaction-lineage finding, item 2).
    #
    # `perform`/`redo` already handle a single path failing without raising (strict review
    # item 4). The ``except (UndoError, TrashError)`` clauses below are the backstop for
    # what is not path-shaped: any guard error that still escapes (item 4: "catch all guard
    # errors, not only RewindError") becomes ``"failed"`` instead of reaching the server as
    # an unhandled exception.

    def _conversation_plan(self, point_id: str):
        """What rewinding the conversation to ``point_id`` would hide, read from the journal
        (`loop.continuation.plan_rewind`); None when the point is not one the conversation
        can go back to."""
        from cogito.graph import resume
        from cogito.loop import continuation

        # Every journal of the project: whatever the thread shows after the point goes back,
        # including another opening of the project.
        replayed = resume.replay(self._project_root, self._store.journal.path, everything=True)
        return continuation.plan_rewind(
            replayed, self.session_id, point_id, self._project_id, project_root=self._project_root
        )

    def _rebuild(self, ended_by: str) -> None:
        """Make the engine what the journal now says the conversation is, exactly what a
        restart would read (`Engine.continue_from`)."""
        from cogito.graph import resume

        self._engine.continue_from(
            resume.load(self._project_root, self._store.journal.path, self.session_id),
            ended_by=ended_by,
        )

    def rewind_dry_run(self, point_id: str) -> dict:
        """Matches ``server.app.EngineLike.rewind_dry_run``; see ``guard.rewind.dry_run``.
        ``conversation`` says whether the conversation goes back too."""
        from cogito.guard import rewind as rewind_module
        from cogito.guard.trash import TrashError
        from cogito.guard.undo import UndoError

        try:
            plan = rewind_module.dry_run(self._driver, self.session_id, point_id)
        except rewind_module.RewindError:
            return {"error": "not_found"}
        except GraphUnavailable:
            # The rewind's files are found in the graph: with it down nothing is known.
            return {"error": "graph_unavailable"}
        except (UndoError, TrashError):
            return {"error": "failed"}
        return {
            **plan.as_dict(self._project_root),
            "conversation": self._conversation_plan(point_id) is not None,
        }

    def rewind_perform(
        self, point_id: str, *, keep_conflicts: bool = False, force: bool = False
    ) -> dict:
        """Matches ``server.app.EngineLike.rewind_perform``; see ``guard.rewind.perform``."""
        from cogito.guard import rewind as rewind_module
        from cogito.guard.trash import TrashError
        from cogito.guard.undo import UndoError
        from cogito.loop import continuation

        conversation = self._conversation_plan(point_id)
        try:
            result = rewind_module.perform(
                self._driver,
                self._undo,
                self._store.record,
                session_id=self.session_id,
                point_id=point_id,
                keep_conflicts=keep_conflicts,
                force=force,
            )
        except rewind_module.RewindConflict as exc:
            return {
                "error": "conflict",
                **exc.plan.as_dict(self._project_root),
                "conversation": conversation is not None,
            }
        except rewind_module.RewindError:
            return {"error": "not_found"}
        except GraphUnavailable:
            # Raised by the first graph read, before any file is touched or journalled.
            return {"error": "graph_unavailable"}
        except (UndoError, TrashError):
            return {"error": "failed"}
        # The files changed under the turn a provider failure stopped: it is not resumed.
        self._engine.forget_resume()
        if self._tracker is not None:
            self._tracker.clear_snapshots()
        answer = result.as_dict(self._project_root)
        if conversation is not None:
            # The conversation goes back with the files, journalled under the same id so
            # «Rehacer» brings both back. The messages are hidden, never deleted.
            self._store.record(
                "conversation_rewound",
                id=result.redo_id,
                session_id=self.session_id,
                point_id=point_id,
                hidden=list(conversation.hidden),
                after_seq=conversation.after_seq,
                continues_from=conversation.continues_from,
            )
            self._rebuild(continuation.REWOUND)
            answer["conversation"] = {"hidden": len(conversation.hidden)}
            answer["point_text"] = conversation.point_text
            if conversation.point_attachments:
                answer["point_attachments"] = list(conversation.point_attachments)
        else:
            answer["conversation"] = None
        answer["redo"] = bool(answer["files"]) or conversation is not None
        return answer

    def rewind_redo(self, redo_id: str) -> dict:
        """Matches ``server.app.EngineLike.rewind_redo``; see ``guard.rewind.redo``."""
        from cogito.graph import resume
        from cogito.guard import rewind as rewind_module
        from cogito.guard.trash import TrashError
        from cogito.guard.undo import UndoError
        from cogito.loop import continuation

        replayed = resume.replay(self._project_root, self._store.journal.path)
        record = replayed.rewinds.get(redo_id)
        if record is not None:
            if record.restored:
                return {"error": "already_redone"}
            if not replayed.redo_available(redo_id):
                # A turn started since (or the conversation was rewound again): the rewound
                # branch is final. It stays in the store, hidden.
                return {"error": "moved_on"}
        try:
            result = rewind_module.redo(
                self._driver,
                self._undo,
                self._store.record,
                session_id=self.session_id,
                redo_id=redo_id,
            )
        except rewind_module.RewindAlreadyRedone:
            if record is None:
                return {"error": "already_redone"}
            # The files were redone but the app closed before the conversation was, so the
            # conversation half is still owed.
            result = rewind_module.RedoResult(restored=())
        except rewind_module.RewindError:
            return {"error": "not_found"}
        except GraphUnavailable:
            return {"error": "graph_unavailable"}
        except (UndoError, TrashError):
            return {"error": "failed"}
        self._engine.forget_resume()
        if self._tracker is not None:
            self._tracker.clear_snapshots()
        answer = result.as_dict(self._project_root)
        answer["conversation"] = record is not None
        if record is not None:
            self._store.record(
                "conversation_restored", id=redo_id, session_id=record.session_id
            )
            self._rebuild(continuation.REWOUND)
        return answer

    def close(self) -> None:
        """Release all resources in the correct order."""
        from cogito.graph import sessions

        sessions.end_session(self._store.record, self._engine.session_id, "end_turn")
        self._shell_session.close()
        self._broker.stop()
        self._estella.close()
        self._store.close()
        callbacks, self._on_close = self._on_close, []
        for callback in callbacks:
            callback(self)


#: How long an opening waits for another writer to release a conversation's journal before
#: starting a new session. Long enough for a core that is exiting (the desktop stops one
#: core and starts the next when it switches projects), short enough that a live one (a
#: running `cogito chat`) does not hold up the window.
CLAIM_WAIT_SECONDS = 3.0


class _OpenConversations:
    """The conversations `serve`'s live engines are writing to, so a second opening of the
    same project never continues one of them too. Two engines appending to one session
    would interleave their messages under the same seqs.

    `claim` finds the conversation and takes it in one step under this register's lock
    (`POST /sessions` builds engines on worker threads, so two openings can race). It also
    takes the journal's own lock (`graph.wal.JournalLock`), which another process holds
    while it writes there: a core still exiting, or a `cogito chat` on the same project. A
    conversation held either way is not continued; the opening starts a new session.

    If building the engine fails after a claim, the reservation and the journal's lock stay
    taken until this process ends, so that project's next openings here start new sessions.
    That is the safe side.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: list[EngineSession] = []
        self._reserved: set[str] = set()

    def held(self) -> frozenset[str]:
        """The sessions they are in now (read live, since a fold moves one) and those
        claimed by an opening still being built."""
        with self._lock:
            return frozenset(live.session_id for live in self._live) | self._reserved

    def claim(self, project_root: Path):
        """``(continuation, lock)`` for the project's conversation, or None when there is
        none to continue or someone else is writing it."""
        from cogito.graph import resume
        from cogito.graph.wal import JournalLock

        with self._lock:
            held = frozenset(live.session_id for live in self._live) | self._reserved
            found = resume.find(project_root, held=held)
            if found is None:
                return None
            lock = JournalLock(found.journal)
            if not lock.acquire(wait=CLAIM_WAIT_SECONDS):
                import logging

                logging.getLogger(__name__).info(
                    "%s is still being written by another process; starting a new session",
                    found.journal,
                )
                return None
            self._reserved.add(found.session_id)
            return found, lock

    def unclaim(self, session_id: str) -> None:
        with self._lock:
            self._reserved.discard(session_id)

    def add(self, engine_session: EngineSession, claimed: str | None = None) -> None:
        with self._lock:
            self._live.append(engine_session)
            if claimed is not None:
                self._reserved.discard(claimed)
        engine_session._on_close.append(self._release)

    def _release(self, engine_session: EngineSession) -> None:
        with self._lock:
            self._live = [live for live in self._live if live is not engine_session]


def _saved_web_callbacks(driver, project_id, record):
    """Adapt the graph's saved evidence to the app's web results; headless stays inert."""
    from cogito.graph.web_archive import WebArchive
    from cogito.tools.web.sources import Hit

    archive = WebArchive(driver, project_id, record)

    def search(query, site):
        return [Hit(row['title'], row['url'], row['snippet'], 'saved',
                    credit=row.get('credit') or '', observed_at=row['observed_at'])
                for row in archive.search(query, site)]

    def save(hits):
        archive.remember([{'title': h.title, 'url': h.url, 'snippet': h.snippet,
                           'credit': h.credit} for h in hits if h.source != 'saved'])

    return search, save


#: A subagent's step limit. The app's own turns have none (the person watches them and has
#: the stop button); a subagent is not watched step by step, so it gets one. A choice, not
#: a measurement.
DELEGATE_MAX_STEPS = 30
#: APP-ONLY: how many of one step's `delegate` calls run at the same time. A choice, under
#: Codex's default of 6 open agent threads (`agents.max_threads`, developers.openai.com/
#: codex/subagents, read 2026-11-06): a Motoris without `"parallel"` answers one request at
#: a time and parks the others' conversations in RAM, so more only queue there.
DELEGATES_AT_ONCE = 3

#: The stops after which a subagent is asked once more for what it found: it was working,
#: and the limit, not a failure, ended it. Never after «Detener» or a provider failure.
_DELEGATE_LAST_WORD_AFTER = frozenset(
    {"max_steps", "wall_clock", "no_progress", "consecutive_tool_errors"}
)
#: A subagent that stopped at a limit is asked this once, so what it read is not lost.
#: Claude Code returns such a subagent's output "marked as partial"
#: (code.claude.com/docs/en/sub-agents, read 2026-11-11), and Gemini CLI gives one "final
#: recovery turn" after its turn or time limit (packages/core/src/agents/local-executor.ts,
#: `executeFinalWarningTurn`, read 2026-11-11). The tool list stays as it was, so the
#: server reuses the prompt it has read; the request is the only thing that says not to call.
_DELEGATE_LAST_WORD = (
    "You have reached your limit ({why}) and cannot call any more tools. Do not "
    "call a tool. Answer now, in the form the task asked for, with what you have found so "
    "far, and say under \"Not found\" what you did not get to."
)

#: What a subagent is told, before its task. Harness text, not the person's.
#: The subagent shares its parent's system message and tool list (`Engine.subagent`), so
#: what is its own is said here: that it only reads, that nobody follows its steps, its
#: step limit, and the shape of its answer. Claude Code and Codex both ask the caller to
#: say what comes back and return only a summary (code.claude.com/docs/en/sub-agents.md;
#: learn.chatgpt.com/docs/agent-configuration/subagents; read 2026-11-10).
_DELEGATE_TASK = (
    "You are a subagent. Another agent handed you the task below. The person does not see "
    "your messages, and your final reply goes back to that agent as your findings.\n"
    "- You can only read: read, grep, glob, the web and the project's documents. The other "
    "tools listed are the calling agent's; a call to one is refused.\n"
    "- Nobody follows your steps: make each call without a line before it.\n"
    "- You have at most {steps} steps. Investigate, then answer.\n"
    "- Answer in under 400 words, in this form:\n"
    "Findings: one line each, with the file path and line numbers (src/app.py:12-30) or the "
    "address, and exact values as the source gives them.\n"
    "Not found: what you looked for and where, if anything is missing.\n"
    "Open questions: what the caller should check or decide, if anything.\n"
    "Do not guess: a value you did not read is not a finding.\n\n"
    "Task:\n{task}"
)


class _Nested:
    """A subagent's tool event as the window gets it: the `delegate` call it ran under is
    named, so its row is drawn there and not as one of the parent's calls."""

    __slots__ = ("_event", "_parent")

    def __init__(self, event, parent: str) -> None:
        self._event = event
        self._parent = parent

    def __getattr__(self, name: str):
        return getattr(self._event, name)

    def as_event(self) -> dict:
        return {**self._event.as_event(), "parent_call_id": self._parent}


class _SubagentDone:
    """A subagent answered (``subagent_done``). Sent through the window's `tool_end` channel,
    whose ``type`` it replaces: the rows of one step's subagents end together, when the last
    one answers, and until then this one's says it has (`Engine._run_delegates_together`)."""

    __slots__ = ("_call",)

    def __init__(self, call: str) -> None:
        self._call = call

    def as_event(self) -> dict:
        return {"type": "subagent_done", "call_id": self._call, "parent_call_id": self._call}


def _child_web_tools(web_state, browser_wanted: bool) -> dict:
    """A subagent's `web_search` and `web_fetch`: the session's web state (client, caches,
    index), but its own last search, which `web_fetch` falls back on."""
    import copy

    from cogito.tools.web.fetch import WebFetchTool
    from cogito.tools.web.search import WebSearchTool

    own = copy.copy(web_state)
    own.last_query = ""
    return {
        "web_search": WebSearchTool(own),
        "web_fetch": WebFetchTool(own, browser=browser_wanted),
    }


def _delegate_hooks(guard, record, installer, web_state):
    """A subagent's hooks, built for each one: the guard before each call, recorded under
    the subagent's session, and the web's addresses after one. None of the parent's fold
    hooks: a subagent's fold must not move the parent's recall or clear its read
    snapshots."""
    from cogito.loop.hooks import HookBus, HookPoint

    def build(session_id: str) -> HookBus:
        hooks = HookBus()
        hooks.subscribe(HookPoint.BEFORE_TOOL, _guard_hook(guard, record, session_id, installer))
        if web_state is not None:
            hooks.subscribe(HookPoint.AFTER_TOOL, _web_addresses_from_files(web_state))
        return hooks

    return build


def _delegate_runner(
    engine,
    *,
    store,
    project_id: str,
    tool_context,
    settings,
    registry,
    fresh_read,
    fresh_web=None,
    hooks,
    on_tool_start=None,
    on_tool_end=None,
):
    """`delegate`'s runner: a child session in the same journal, linked to the parent by
    `DELEGATED_TO`, run to its answer and closed. The graph and the thread keep a delegated
    session out of the project's conversation (`graph.history.thread_order`,
    `graph.resume`); a fold inside the subagent is linked the same way, so its handover is
    never taken for the person's conversation either.

    The subagent's registry holds the parent's tools but a `read` of its own
    (``fresh_read``): the file tracker behind `read`, `write` and `edit` must not learn
    that a file was read when only the subagent saw it. Its web tools are its own too
    (``fresh_web``): `web_fetch` falls back on the last `web_search`, which must be this
    subagent's, not one another subagent running beside it sent."""
    from cogito.tools.delegate import Delegated, NotForSubagent
    from cogito.tools.registry import ToolRegistry

    # The parent's settings otherwise: the system message is the parent's, preamble rule
    # included (the task says to skip the lines), so the server reuses it.
    child_settings = replace(
        settings, budgets=replace(settings.budgets, max_steps=DELEGATE_MAX_STEPS)
    )

    def child_registry() -> ToolRegistry:
        """The parent's tools, with a `read` and web tools of its own, and every tool a
        read-only subagent may not use replaced by one that says so."""
        own = ToolRegistry()
        web_tools = fresh_web() if fresh_web is not None else {}
        allowed = {spec.name for spec in registry.catalog("read_only", depth=1)}
        for name in registry.registered:
            # `todo` is the parent's list: listed, so the tool list stays the parent's and
            # its cached prompt is reused, but refused like any tool a subagent may not use.
            if name not in allowed or name == "todo":
                own.register(NotForSubagent(registry.get(name).spec))
            elif name == "read":
                own.register(fresh_read())
            else:
                own.register(web_tools.get(name) or registry.get(name))
        return own

    def run(task: str, context) -> Delegated:
        return _run_subagent(
            engine,
            store=store,
            project_id=project_id,
            context=context,
            opening=_DELEGATE_TASK.format(task=task, steps=DELEGATE_MAX_STEPS),
            build=lambda child_id: {
                "registry": child_registry(),
                "tool_context": replace(
                    tool_context, session_id=child_id, cancel=context.cancel, call_id=""
                ),
                "settings": child_settings,
                "hooks": hooks(child_id),
            },
            max_steps=DELEGATE_MAX_STEPS,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
        )

    return run


def _run_subagent(
    engine,
    *,
    store,
    project_id: str,
    context,
    opening: str,
    build,
    max_steps: int,
    on_tool_start=None,
    on_tool_end=None,
):
    """Run one subagent to its answer in a child session of the journal, linked to the
    parent by `DELEGATED_TO`, and close it. ``build`` is given the child's session id and
    returns `Engine.subagent`'s arguments for it. The graph and the thread keep a delegated
    session out of the project's conversation (`graph.history.thread_order`,
    `graph.resume`); a fold inside the subagent is linked the same way."""
    from cogito.graph import sessions
    from cogito.ids import new_id
    from cogito.loop.state import RunState, StopReason
    from cogito.tools.delegate import Delegated

    parent_id = engine.session_id
    child_id = new_id()
    store.record(
        "session_started",
        id=child_id,
        project_id=project_id,
        title="delegated",
        model_id=engine.model_id,
        sampling_seed=None,
        prompt_time=None,
    )
    store.record("session_delegated", parent_id=parent_id, child_id=child_id)
    parent_call = context.call_id
    made = [0]

    def started(event) -> None:
        made[0] += 1
        if on_tool_start is not None:
            on_tool_start(_Nested(event, parent_call))

    child = engine.subagent(
        session_id=child_id,
        **build(child_id),
        on_tool_start=started,
        on_tool_end=(
            (lambda event: on_tool_end(_Nested(event, parent_call)))
            if on_tool_end is not None
            else None
        ),
        # A fold inside the subagent opens a session that is the subagent's too.
        on_session_changed=lambda folded: store.record(
            "session_delegated", parent_id=parent_id, child_id=folded
        ),
    )
    result = None
    last_word = None
    try:
        result = child.run(opening, RunState(cancel=context.cancel))
        if str(result.stop_reason) in _DELEGATE_LAST_WORD_AFTER and not context.cancel.cancelled:
            last_word = _last_word(child, result, context.cancel, max_steps)
    finally:
        reason = str(result.stop_reason) if result is not None else "failed"
        sessions.end_session(store.record, child.session_id, reason)
        if on_tool_end is not None and parent_call:
            on_tool_end(_SubagentDone(parent_call))
    answer = result.final_text
    if last_word is not None:
        return Delegated(
            answer=last_word.final_text,
            stop_reason=str(result.stop_reason),
            steps=result.steps + 1,
            input_tokens=result.input_tokens + last_word.input_tokens,
            output_tokens=result.output_tokens + last_word.output_tokens,
            session_id=child.session_id,
            calls=made[0],
            partial=True,
        )
    if result.stop_reason is not StopReason.END_TURN:
        # The final text of a stopped run is the harness's explanation; what the subagent
        # had found is its own last words.
        answer = next(
            (
                message.flat_text()
                for message in reversed(child.messages)
                if message.role == "assistant" and message.flat_text().strip()
            ),
            "",
        )
    return Delegated(
        answer=answer,
        stop_reason=str(result.stop_reason),
        steps=result.steps,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        session_id=child.session_id,
        calls=made[0],
    )


def _last_word(child, result, cancel, max_steps: int):
    """Ask a subagent that stopped at a limit for what it found, in one more step, and
    return that turn's result when it answered; None when it did not (it called a tool
    again, said nothing, or was stopped), and the caller then reports the stop as before.

    The turn starts on the last step the limit allows, so a tool call in it is the last
    thing that runs."""
    import logging

    from cogito.loop.state import RunState, StopReason
    from cogito.tools.delegate import stopped_because

    notice = _DELEGATE_LAST_WORD.format(why=stopped_because(str(result.stop_reason)))
    try:
        last = child.run(notice, RunState(cancel=cancel, step=max(0, max_steps - 1)))
    except Exception:  # an extra request; the stop it follows is still reported
        logging.getLogger(__name__).exception(
            "asking a stopped subagent for its findings failed"
        )
        return None
    if last.stop_reason is StopReason.END_TURN and last.final_text.strip():
        return last
    return None


#: A writing subagent's step limit: making a change and running its checks takes more
#: steps than finding something out. A choice, not a measurement.
WRITER_MAX_STEPS = 50

#: What a writing subagent is told, before its task. Harness text, not the person's.
_WRITER_TASK = (
    "You are a subagent. Another agent handed you the task below. The person does not see "
    "your messages, and your final reply goes back to that agent as your report.\n"
    "- You work in a copy of the project, at {copy}: its current state, uncommitted changes "
    "included. Change files there with write and edit. The shell runs there too, without "
    "network, and can write only inside the copy.\n"
    "- Nothing you change reaches the project by itself: the caller gets your changes as a "
    "patch and decides whether to apply it. Do not commit. Nothing is deleted in the copy; "
    "say in your report which files should go.\n"
    "- You have at most {steps} steps.\n"
    "- Make the change, run the project's own checks for it where they run without "
    "network, then answer in under 400 words, in this form:\n"
    "Changed: one line per file, what changed and why.\n"
    "Checked: each command you ran and its result.\n"
    "Open questions: what the caller should check or decide, if anything.\n\n"
    "Task:\n{task}"
)


def _copy_approver(copy: Path):
    """What a writing subagent's guard asks about, answered without a person: a file
    overwritten or moved inside its copy is allowed (the copy is thrown away, and only
    the patch comes back); a deletion is not, since the patch never carries one."""
    from cogito.guard.approval import ApprovalResponse

    inside = copy.resolve()

    def approve(request) -> ApprovalResponse:
        targets = [Path(target) for target in request.targets]
        within = bool(targets) and all(
            target.resolve() == inside or inside in target.resolve().parents
            for target in targets
        )
        if (
            request.action in ("overwrite", "move")
            and within
            and not request.unresolved
            and not request.targets_unknown
            and not request.outside
        ):
            return ApprovalResponse(allowed=True, reason="auto", note="inside the copy")
        return ApprovalResponse(
            allowed=False,
            reason="nobody",
            note=(
                "nothing is deleted in a subagent's copy; say in your report which files "
                "should go"
            ),
        )

    return approve


def _writer_runner(
    engine,
    *,
    store,
    project_id: str,
    project_root: Path,
    tool_context,
    settings,
    edit_format,
    on_tool_start=None,
    on_tool_end=None,
):
    """`delegate`'s runner for kind "write": the subagent works in its own git copy of the
    project (`tools.workcopy`), never in the person's folder.

    Its tools are its own, bound to the copy: a shell under bubblewrap that may write only
    the copy and the shims' socket, with no network (`guard.sandbox`), and read, write,
    edit, grep and glob behind a guard confined to the copy (`Guard(confine_to=...)`, the
    benchmark's containment). When it answers, what it changed is saved as a patch in the
    parent's artifacts and the copy is removed."""
    import shutil
    import sys

    from cogito.config import paths
    from cogito.context.offload import ArtifactStore
    from cogito.guard import sandbox
    from cogito.guard.policy import Guard
    from cogito.guard.shims import ShimBroker
    from cogito.guard.undo import UndoStore
    from cogito.loop.hooks import HookBus, HookPoint
    from cogito.tools import workcopy
    from cogito.tools.edit.tool import EditTool
    from cogito.tools.files import FileTracker, ReadTool, WriteTool
    from cogito.tools.registry import ToolRegistry
    from cogito.tools.search import GlobTool, GrepTool
    from cogito.tools.shell import ShellSession, ShellTool

    writer_settings = replace(
        settings,
        budgets=replace(settings.budgets, max_steps=WRITER_MAX_STEPS),
        loop=replace(settings.loop, preambles=False),
    )

    def run(task: str, context):
        from cogito.ids import new_id

        copy = paths.cache_dir() / "subagents" / new_id()
        try:
            workcopy.prepare(project_root, copy)
        except BaseException:
            shutil.rmtree(copy, ignore_errors=True)
            raise
        guard = Guard(copy, approver=_copy_approver(copy), confine_to=(copy,))
        built: dict = {}

        def build(child_id: str) -> dict:
            undo = UndoStore(copy, child_id, store.record)
            broker = ShimBroker(guard, undo, interpreter=sys.executable, confine_to=(copy,))
            broker_env = broker.start()
            base_env = paths.user_environment()
            env = {**base_env, **broker_env}
            shim_dir = broker_env.get("PATH", "").split(os.pathsep)[0]
            kept = [entry for entry in base_env.get("PATH", "").split(os.pathsep) if entry]
            env["PATH"] = os.pathsep.join([shim_dir, *kept] if shim_dir else kept)
            shell = ShellSession(
                cwd=copy,
                env=env,
                waiting=guard.waiting,
                wrapper=sandbox.prefix(writable=[copy, broker.root]),
            )
            built.update(broker=broker, shell=shell)
            tracker = FileTracker()
            registry = ToolRegistry()
            registry.register_all(
                [
                    ShellTool(shell),
                    ReadTool(edit_format, tracker),
                    WriteTool(tracker, undo, env=env),
                    EditTool(edit_format, tracker, undo, env=env),
                    GrepTool(),
                    GlobTool(),
                ]
            )
            hooks = HookBus()
            hooks.subscribe(HookPoint.BEFORE_TOOL, _guard_hook(guard, store.record, child_id))
            return {
                "registry": registry,
                "tool_context": replace(
                    tool_context,
                    project_root=copy,
                    session_id=child_id,
                    cancel=context.cancel,
                    call_id="",
                    artifacts=ArtifactStore(copy, child_id),
                ),
                "settings": writer_settings,
                "hooks": hooks,
                "abilities": "a real shell (no network) and real files, in a copy of the project",
                "scenario": "edit",
                # The compile gate runs what the copy holds, so it runs in the sandbox too.
                "build_gate_wrapper": sandbox.prefix(writable=[copy]),
            }

        try:
            done = _run_subagent(
                engine,
                store=store,
                project_id=project_id,
                context=context,
                opening=_WRITER_TASK.format(task=task, steps=WRITER_MAX_STEPS, copy=copy),
                max_steps=WRITER_MAX_STEPS,
                build=build,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
            )
            changes = workcopy.collect(copy)
        finally:
            if "shell" in built:
                built["shell"].close()
            if "broker" in built:
                built["broker"].stop()
            workcopy.discard(copy)
        patch = ""
        if changes.patch:
            saved = tool_context.artifacts.stash_bytes(
                changes.patch, suffix=".patch", kind="patch"
            )
            try:
                patch = str(saved.path.relative_to(project_root))
            except ValueError:
                patch = str(saved.path)
        return replace(done, patch=patch, stat=changes.stat, deleted=changes.deleted)

    return run


def _build_engine(
    provider,
    project_root: Path,
    settings,
    entry,
    disabled_tools: tuple[str, ...],
    approver,
    on_phase,
    cancel,
    on_context_status=None,
    sampling_seed: int | None = None,
    prompt_time: datetime | None = None,
    sandbox_hidden: tuple[Path, ...] | None = None,
    on_tool_start=None,
    on_tool_end=None,
    on_steer_applied=None,
    on_user_message=None,
    on_stream=None,
    on_compacted=None,
    on_auto_continue=None,
    capabilities_factory=None,
    spend_gate=None,
    conversations=None,
    on_background_exit=None,
    on_terminal_event=None,
    on_browser_command=None,
) -> EngineSession:
    """Build every resource from a provider and return an EngineSession.

    Extracted from ``chat`` so ``cogito serve`` can reuse it. It wires everything ``chat``
    does: guard, undo, shim broker, shell, edit format, all tools including
    check/ask/recall, Estella pool, compaction hook, on_message=recall.remember, sampling,
    WAL store.

    ``sandbox_hidden`` is None for an ordinary session. For a sandboxed one it names the
    directories that appear empty inside, and four things change together: the shell runs
    under bubblewrap with only the project and the broker's socket writable; `check` runs
    every linter under its own bubblewrap prefix with only the project writable; the guard
    refuses a file tool any path that resolves outside the project; and the broker refuses
    a deletion outside those writable roots before anyone is asked.

    ``capabilities_factory``, when given, builds this session's component catalog access
    (``ToolContext.capabilities``) from the store's ``record`` and the engine's current
    session id. ``_serve_factory`` passes one; ``chat`` does not, so a benchmark's sessions
    carry none.

    ``spend_gate`` is asked before every request to the model (`Engine._spend_allowed`,
    COGITO's own budget). ``_serve_factory`` passes one; ``chat`` does not, so a benchmark's
    sessions are never paused.

    ``conversations`` (an `_OpenConversations`) makes this opening continue the project's
    conversation. Its latest session's live leaf is read back from its journal
    (`graph.resume`), reopened under the same id and appended to the same journal, and the
    engine holds everything the model held before (`Engine.continue_from`). With nothing to
    continue, or a conversation another live engine is writing to, a new session starts.
    ``_serve_factory`` passes one; ``chat`` does not, so ``chat --once`` (a benchmark task)
    and interactive ``chat`` always start a fresh session, byte for byte as before.

    ``on_terminal_event`` receives the terminal tabs' events (``terminal_opened``,
    ``terminal_data``, ``terminal_status``, ``terminal_exited``). The tabs and their two tools
    exist only in a session the desktop app owns, on POSIX (`_terminal_wanted`).

    ``on_browser_command`` carries the `browser` tool's ``browser_command`` events to the
    desktop's main process, which owns the tabs and answers on the session's answers route
    (`server/browsers.py`). The tool exists only in a session the desktop app owns.
    """
    from cogito.context import prompt as prompt_builder
    from cogito.context.offload import ArtifactStore
    from cogito.graph import sessions
    from cogito.graph.driver import GraphDriver, Store
    from cogito.graph.estella import EstellaPool
    from cogito.graph.recall import DocumentKnowledge, Knowledge, Recall, prose_documents
    from cogito.graph.wal import Journal
    from cogito.guard.allowed_tools import AllowedTools
    from cogito.guard.policy import Guard
    from cogito.guard.shims import ShimBroker
    from cogito.guard.undo import UndoStore
    from cogito.ids import new_id
    from cogito.loop.engine import Engine
    from cogito.loop.fold_files import FoldFiles
    from cogito.loop.hooks import HookBus, HookPoint
    from cogito.provider.base import RequestOptions
    from cogito.tools.base import ToolContext
    from cogito.tools.edit.tool import EditTool, build_format
    from cogito.tools.files import FileTracker, ReadTool, WriteTool
    from cogito.tools.knowledge import AskTool, DocumentAskTool, RecallTool
    from cogito.tools.quality import CheckTool
    from cogito.tools.registry import ToolRegistry
    from cogito.tools.search import GlobTool, GrepTool
    from cogito.tools.shell import ShellSession, ShellTool

    options = RequestOptions()
    # APP-ONLY: in the app, the per-step output ceiling is widened for a model whose
    # context window is declared. `chat` (every benchmark task) never passes
    # ``conversations``, so this never runs there and its requests are unchanged.
    in_app = conversations is not None
    if in_app:
        settings = _app_output_cap(entry, settings)
        settings = _app_auto_continue_cap(settings)
        # APP-ONLY: three failing calls in a row get a notice and the turn goes on, up to
        # three times a turn, instead of ending it (`max_tool_error_notices`). `chat`/benchmark
        # keep 0, so they stop exactly as before. Its measurement is owed (§7).
        if settings.budgets.max_tool_error_notices == 0:
            settings = replace(
                settings,
                budgets=replace(settings.budgets, max_tool_error_notices=3),
            )
        # APP-ONLY: shrink a large tool result the fold keeps verbatim, so one big output
        # (an REA dump) does not keep the seed large and the session re-folding on a small
        # window (`compact.shrink_tail`). `chat`/benchmark keep 0, so their folds are
        # byte-identical. Its measurement is owed (§7, the long-memory suite).
        if settings.budgets.handover_result_tokens == 0:
            settings = replace(
                settings,
                budgets=replace(settings.budgets, handover_result_tokens=500),
            )
        # APP-ONLY: after a fold the person's latest request is the one worked on
        # (`compact.changed_course`); a model kept following the first one after being
        # told to do something else (2026-10-30). `chat`/benchmark fold as before.
        settings = replace(settings, budgets=replace(settings.budgets, fold_latest_request=True))
        # APP-ONLY: a message sent mid-turn is answered as the turn goes on, as Claude Code
        # does, instead of being read as a change of plan (2026-10-31). `chat`/benchmark
        # keep the old wrapper.
        settings = replace(settings, budgets=replace(settings.budgets, steer_continues=True))
        # APP-ONLY: a fold resets the identical-call count; a long turn stopped as
        # «Se detuvo sin avanzar» on one read run once in each of three windows (2026-10-31).
        settings = replace(settings, budgets=replace(settings.budgets, fold_resets_repeats=True))
        # APP-ONLY: one short line before each tool call says what it is for; the person
        # follows the turn as it runs (2026-11-01). `chat` and the lab's suite holder, which
        # runs `serve` too, keep their identity.
        if _app_owned():
            # The handover is written without thinking, which cut it short almost every
            # time on Motoris (2026-11-10).
            settings = replace(
                settings, budgets=replace(settings.budgets, fold_without_thinking=True)
            )
            settings = replace(
                settings,
                loop=replace(
                    settings.loop,
                    preambles=True,
                    parallel_delegates=DELEGATES_AT_ONCE,
                    # The date and branch in the system message, no status turn, so the
                    # server's checkpoint at the last turn is the next request's prefix.
                    status_turn=False,
                    # A turn about to end with items open on the model's task list is
                    # sent back once (`tools/todo.py`).
                    open_tasks_notice=True,
                ),
            )
        # APP-ONLY: no time or step limit on a turn. The person watches it and has the stop
        # button (2026-10-29: "no debía parar"); a long turn was cut at 30 min while 10
        # of them went to an approval and one long command. What still ends one on its own is
        # a real loop: the same call, the same reply, or a reply that repeats itself.
        # No token count either: it never stopped a turn, yet a model that saw it fall
        # ended its work as out of budget (the app, 2026-10-30).
        # `chat`/benchmark keep all three, so their stops and requests are unchanged.
        settings = replace(
            settings,
            budgets=replace(
                settings.budgets, wall_clock_seconds=0.0, max_steps=0, max_total_tokens=0
            ),
        )
    # A session with nothing to apply keeps the provider it was handed.
    options = _with_reasoning_budget(entry, settings, options)
    if options != RequestOptions() and hasattr(provider, "configured"):
        provider = provider.configured(options)
    hidden: set[str] = set()

    driver = GraphDriver(settings.neo4j, secrets.get("neo4j", "password"))
    graph_up = driver.is_available()
    if graph_up:
        driver.ensure_schema()
    else:
        click.echo("! neo4j unreachable — journalling to disk and projecting when it returns")

    project_id = sessions.project_id_for(project_root)
    # Before anything writes into it (the journal lock below creates the journal file):
    # it must ignore itself for git from the first event, and exist for a sandbox to
    # mount it read-only.
    record_dir = paths.ensure_project_dir(project_root)
    continuation = None
    rebuilt = None
    journal_lock = None
    if conversations is not None:
        from cogito.loop import continuation as continuation_module

        claimed = None
        try:
            claimed = conversations.claim(project_root)
            if claimed is not None:
                continuation, journal_lock = claimed
                # Rebuilt here, before anything is written, so an entry this version cannot
                # read costs a new session rather than every later opening of the project.
                rebuilt = continuation_module.rebuild(continuation)
        except Exception:  # an unreadable journal must never cost the person the app
            import logging

            logging.getLogger(__name__).warning(
                "could not read %s's conversation back; starting a new one",
                project_root,
                exc_info=True,
            )
            if claimed is not None:
                conversations.unclaim(claimed[0].session_id)
                claimed[1].release()
            continuation = rebuilt = journal_lock = None
    if continuation is not None:
        # The conversation continues in its own session, appended to the journal that
        # holds it. One store replays it in order and, before this opening's events,
        # projects anything the previous process journalled but never got into the graph.
        session_id = continuation.session_id
        journal_path = continuation.journal
    else:
        # The journal file is named after the session, so the id is minted here and the
        # store's first events carry it.
        session_id = new_id()
        journal_path = paths.wal_file(project_root, session_id)
    if journal_lock is None:
        from cogito.graph.wal import JournalLock

        # Every writer holds its journal, `chat` included, so a later opening of the project
        # never continues a conversation still being written (`_OpenConversations.claim`).
        journal_lock = JournalLock(journal_path)
        journal_lock.acquire()
    # Found unreachable above: the store does not wait on it again for each event.
    store = Store(Journal(journal_path), driver, lock=journal_lock, degraded=not graph_up)
    store.start_background_flush()

    store.record("project_opened", id=project_id, root=str(project_root), name=project_root.name)
    sampling = _sampling_for(entry, seed=sampling_seed)
    recorded_prompt_time = (
        prompt_time.isoformat(timespec="seconds") if prompt_time is not None else None
    )
    if continuation is not None:
        store.record(
            "session_resumed",
            id=session_id,
            project_id=project_id,
            model_id=entry.id,
            build_gate=settings.loop.build_gate,
        )
    else:
        store.record(
            "session_started",
            id=session_id,
            project_id=project_id,
            title="",
            model_id=entry.id,
            sampling_seed=sampling.seed,
            prompt_time=recorded_prompt_time,
            # Always recorded, not only when a `build_gate_run` event fires. With this
            # false the gate is fully inert (loop.engine `_maybe_run_build_gate`, first
            # check), so a control-arm session may carry no build-gate telemetry; this
            # field still tells the two bench arms apart.
            build_gate=settings.loop.build_gate,
        )

    # The guard, before anything can run. It is not a permission system: two rules, one
    # decision point, and a trash instead of an unlink.
    undo = UndoStore(project_root, session_id, store.record)
    shell_holder: dict[str, ShellSession] = {}
    sandboxed = sandbox_hidden is not None
    system_facts = _system_facts() if in_app else None
    guard = Guard(
        project_root,
        approver=approver,
        cwd=lambda: shell_holder["session"].cwd if "session" in shell_holder else project_root,
        confine_to=(project_root,) if sandboxed else None,
        # APP ONLY: a system package install is asked about and run by the core
        # (`guard/packages.py`). `chat` and every benchmark task never pass `conversations`.
        packages=in_app,
        package_manager=system_facts.managers[0] if system_facts and system_facts.managers else "",
        # APP ONLY: an MCP tool allowed once is allowed from then on, in every project.
        allowed_tools=AllowedTools(paths.config_dir() / "allowed-tools.json") if in_app else None,
    )
    # The broker adds its own directory: those two are exactly the shell's writable binds.
    broker = ShimBroker(
        guard,
        undo,
        interpreter=sys.executable,
        confine_to=(project_root,) if sandboxed else None,
    )
    # The shell shares the guard's clock: time spent asking a person is not time the
    # command spent hanging, and only the guard knows the difference.
    broker_environment = broker.start()
    wrapper: list[str] = []
    check_wrapper: list[str] = []
    if sandbox_hidden is not None:
        from cogito.guard import sandbox

        # The project and the broker's socket are the only things the shell may write;
        # everything in sandbox_hidden is an empty directory inside. `.cogito/` is
        # mounted read-only over the writable project, so nothing the shell runs can
        # delete or overwrite the session's record.
        wrapper = sandbox.prefix(
            writable=[project_root, broker.root],
            hidden=list(sandbox_hidden),
            read_only=[record_dir],
        )
        # `check` spawns linters directly, never through the broker, so it needs no
        # access to the broker's socket. Only the project is writable.
        check_wrapper = sandbox.prefix(
            writable=[project_root], hidden=list(sandbox_hidden), read_only=[record_dir]
        )
    # No shell (sandboxed for a benchmark, or the person's own in the app) sees Cogito's
    # own virtualenv. `cogito` normally runs through `uv run`, which puts that virtualenv
    # first on PATH, and the agent's `python` and `pip` would then be Cogito's own. The
    # broker's shim directory must still come first, so it is read off
    # `broker_environment` instead of the unfiltered environment.
    base_env = paths.user_environment()
    shell_env = {**base_env, **broker_environment}
    shim_dir = broker_environment.get("PATH", "").split(os.pathsep)[0]
    kept = [entry for entry in base_env.get("PATH", "").split(os.pathsep) if entry]
    shell_env["PATH"] = os.pathsep.join([shim_dir, *kept] if shim_dir else kept)
    # APP-ONLY: a command still running at its timeout is moved to the background, and its
    # end is reported to the model (`Engine.notify`: at the next step of a running turn, or
    # a turn the server wakes) and to the window (`on_background_exit`). `chat` and every
    # benchmark kill it at the timeout, as before.
    engine_holder: dict = {}

    def _background_exit(job, exit_code) -> None:
        report = _background_report(job, exit_code)
        engine = engine_holder.get("engine")
        if engine is not None:
            engine.notify(report)
        if on_background_exit is not None:
            on_background_exit(
                {"command": job.command[:300], "pid": job.pid, "exit_code": exit_code}
            )

    shell_session = ShellSession(
        cwd=project_root,
        env=shell_env,
        waiting=guard.waiting,
        wrapper=wrapper,
        # APP-ONLY: recorded so the next core can stop what a core that died left running.
        registry=paths.shells_dir() if in_app else None,
        background_at_timeout=in_app,
        on_exit=_background_exit if in_app else None,
    )
    shell_holder["session"] = shell_session

    # read and edit share one addressing scheme, which is why the format is a single key.
    edit_format = build_format(settings.edit_format)
    tracker = FileTracker()

    registry = ToolRegistry()
    skipped = {name for name in disabled_tools}

    def install(tool):
        """Register a tool unless this run was told to leave it out or the model's profile
        hides it.

        Leaving one out is how the suite runs with and without a tool, the only evidence
        that a tool earns the context its definition costs on every step.
        """
        if tool.spec.name in skipped:
            skipped.discard(tool.spec.name)
            return
        if tool.spec.name in hidden:
            return
        registry.register(tool)

    install(ShellTool(shell_session))
    install(ReadTool(edit_format, tracker))
    # The shell's environment, so Python this process cannot parse is judged by the same
    # `python3` the agent's shell and the build gate would run (tools/edit/verify.py).
    install(WriteTool(tracker, undo, env=shell_env))
    install(EditTool(edit_format, tracker, undo, env=shell_env))
    install(GrepTool())
    install(GlobTool())
    if settings.voice.enabled:
        for tool in _voice_tools(settings.voice):
            install(tool)
    install(CheckTool(wrapper=check_wrapper))

    # From here the agent can read the store. `ask` reads the project's prose and `recall`
    # reads the conversation, both across the compaction lineage.
    #
    # Nothing is spawned for a tool this run will not register. Estella loads a model per
    # graph and `recall` feeds it every message of every turn, so spawning unconditionally
    # would make the without-them half of an A/B pay the full cost of having them.
    # In the app the documents corpus' Estella runs in the background, beside a model that
    # may decode on the CPU too (`EstellaPool`, ``background_threads``). ``in_app`` was
    # already resolved above, to gate the output-cap widening too.
    estella_plan = _estella_plan(entry, settings, in_app)
    estella = EstellaPool(
        _estella_command(settings),
        paths.estella_graph_dir(project_root),
        device=settings.estella.device,
        enabled=settings.estella.enabled,
        background_threads=settings.estella.documents_threads if in_app else None,
        vector_cache=_vector_cache(settings),
    )
    recall = None
    if "recall" not in skipped and "recall" not in hidden:
        recall = Recall(
            _conversation_estella(estella, estella_plan),
            driver,
            session_id,
            budget=settings.estella.ask_budget,
            floor=settings.estella.escalate_below,
        )
        install(
            RecallTool(
                recall,
                allowed=_document_memory_on if in_app else None,
            )
        )
    # A project with no prose gives `ask` nothing to answer from, yet its definition would
    # be charged on every step. On the 37-task suite, 30 tasks ship no documentation, so
    # this is most of the tool's cost. The test is whether there is any prose at all: the
    # one task that consistently wins with `ask` has a ~2,200-token corpus, which any
    # useful threshold would have excluded.
    ask_code = in_app and _ask_code(settings)
    if (
        "ask" not in skipped
        and "ask" not in hidden
        and prose_documents(project_root, code=ask_code)
    ):
        install(
            AskTool(
                Knowledge(
                    _ask_estella(estella, estella_plan),
                    project_root,
                    budget=settings.estella.ask_budget,
                    floor=settings.estella.escalate_below,
                    code=ask_code,
                ),
                code=ask_code,
                allowed=(
                    (
                        lambda: _document_memory_on()
                        and _estella_present(settings)
                    )
                    if in_app
                    else None
                ),
            )
        )
    # A long document the conversation attaches or reads, indexed in the background and
    # answerable with its pages (`graph.documents`, `ask_document`). Behind
    # `[estella] documents`, off until the suite accepts it. In the app only,
    # `[estella] documents_in_app` indexes what was too large for the window and nothing
    # else; `chat` never passes ``conversations``, so it never reads that setting. When
    # neither applies, nothing below is built (no Estella process, journal event, hook or
    # tool) and every request is byte-identical to one without the feature.
    documents_scope = None
    if "ask_document" not in skipped and "ask_document" not in hidden:
        if settings.estella.documents:
            documents_scope = "all"
        elif in_app and settings.estella.documents_in_app:
            documents_scope = "offloads"
    artifacts = ArtifactStore(project_root, session_id)
    documents_index = None
    if documents_scope is not None:
        documents_index = _document_index(
            settings,
            estella,
            project_root,
            store.record,
            busy=_any_turn_active if in_app else None,
            lite=not estella_plan.documents_full,
        )
        install(
            DocumentAskTool(
                DocumentKnowledge(
                    documents_index,
                    budget=settings.estella.ask_budget,
                    floor=settings.estella.escalate_below,
                ),
                documents_index,
                allowed=_document_memory_on if in_app else None,
                # APP-ONLY: listed from the start with a description that never changes,
                # so a new document does not change the tool list (cogito-lab
                # research/2026-11-10-single-model-review). The lab's suite holder, which
                # runs `serve` too, keeps the tool that appears with the first document.
                stable=in_app and _app_owned(),
            )
        )
    # APP-ONLY: whether the `browser` tool is registered below. The tools that overlap it
    # (`web_fetch`, `computer`) then say a page to show or to use is its.
    browser_wanted = in_app and _app_owned() and on_browser_command is not None
    # `web_search` and `web_fetch`. In the app they are always there (`config.search`);
    # `chat`, and so every benchmark task, registers them only with `[web] in_chat`, so with
    # it false nothing below is built and every request is byte-identical to one without the
    # feature. In the app, with a documents index, every page that is not tiny is indexed
    # and read back through it.
    web_state = None
    if in_app or _web_wanted(settings, in_app):
        from cogito.setup import searxng as local_searxng
        from cogito.tools.web import chain as web_chain
        from cogito.tools.web.fetch import WebFetchTool
        from cogito.tools.web.search import WebSearchTool
        from cogito.tools.web.state import WebState

        # In the app, a page is read through the documents index: indexed whole, and
        # answered from there. `chat` never passes ``retrieve``, so its fetch is unchanged.
        web_knowledge = None
        if in_app and documents_index is not None:
            web_knowledge = DocumentKnowledge(
                documents_index,
                budget=settings.web.fetch_answer_budget,
                floor=settings.estella.escalate_below,
                wait_seconds=_WEB_ANSWER_WAIT_SECONDS,
            )
        saved_search, save_results = (
            _saved_web_callbacks(driver, project_id, store.record) if in_app else (None, None)
        )
        web_state = WebState(
            settings.web,
            documents=(
                _WebDocuments(documents_index, web_knowledge)
                if documents_index is not None else None
            ),
            retrieve=web_knowledge is not None,
            # The SearXNG the app runs. `chat` passes nothing, so its search chain is the
            # one it had.
            local_search=local_searxng.local_url if in_app else None,
            local_engines=local_searxng.ENGINES if in_app else (),
            search_cache=web_chain.default_cache() if in_app else None,
            saved_search=saved_search,
            save_results=save_results,
        )
        install(WebSearchTool(web_state))
        install(WebFetchTool(web_state, browser=browser_wanted))
        if not (registry.has("web_search") or registry.has("web_fetch")):
            web_state = None
    # MCP servers' tools: registered all, shown a few per step (`tools/mcp/hub.py`).
    mcp_hub = None
    mcp_join = None
    # APP ONLY: a late server joins the open conversation, the model is told which are
    # not ready, a project's approved `.mcp.json` servers are read, and `mcp_add` is there.
    mcp_late = in_app and _app_owned()
    if mcp_late:
        settings = _with_project_servers(settings, project_root)
    if mcp_late or _mcp_wanted(settings, in_app):
        from cogito.tools.mcp.hub import McpFindTool, McpTool

        # A local model's chat template renders the tools before everything else, so its
        # list stays the same through the conversation; Claude calls only what it was shown.
        mcp_hub = _mcp_hub(
            settings, stable=in_app and _app_owned() and entry.api != "anthropic", late=mcp_late
        )
        if mcp_hub.offered():
            for offered in mcp_hub.offered():
                install(McpTool(mcp_hub, offered))
            install(McpFindTool(mcp_hub))
            guard.ask_before(_mcp_asked(mcp_hub, settings))
        elif not mcp_late:
            mcp_hub = None
        if mcp_hub is not None and mcp_late:
            mcp_join = _mcp_joiner(mcp_hub, registry, guard, settings)
            from cogito.tools.mcp.install import McpAddTool

            install(
                McpAddTool(
                    mcp_hub, Path(project_root), paths.settings_file(),
                    on_added=lambda: mcp_join(quiet=True),
                )
            )
            guard.ask_before({"mcp_add": _mcp_add_asked()})
    # Agent Skills: their names and descriptions in `skill`'s definition, the instructions
    # loaded when the model asks (`tools/skills.py`).
    skill_tool = None
    if _skills_wanted(settings, in_app):
        from cogito.tools.skills import SkillTool, skills_for

        found = skills_for(project_root, Path.home(), switched_off=_mcp_switched_off(settings))
        if found:
            skill_tool = SkillTool(
                found,
                budget_tokens=settings.skills.budget,
                variables={
                    "CONFIG_FILE": str(paths.settings_file()),
                    "SKILLS_DIR": str(Path.home() / ".cogito" / "skills"),
                },
            )
            install(skill_tool)
    # APP-ONLY: `computer`, to see the screen and use the mouse and keyboard, through the
    # desktop's own portals (`tools/desktop`). Asked about once a session here, and once by
    # the desktop itself the first time. Never in `chat` or a benchmark.
    computer_tool = None
    if in_app and _app_owned() and _computer_wanted():
        from cogito.guard.policy import ExternalTool
        from cogito.tools.desktop.computer import ComputerTool

        computer_tool = ComputerTool(browser=browser_wanted)
        install(computer_tool)
        guard.ask_before(
            {
                "computer": ExternalTool(
                    server="Cogito", tool="computer", title="Usar la pantalla", changes=True,
                    session_only=True,
                )
            }
        )
    # APP-ONLY: `browser`, web pages the model opens and uses by their elements
    # (`tools/browser`), in the app's own tabs: the desktop's main process runs each
    # operation and answers through the session's bridge (`server/browsers.py`). Asked about
    # once a session. Never in `chat` or a benchmark, which have no main process to answer.
    browser_bridge = None
    if browser_wanted:
        from cogito.guard.policy import ExternalTool
        from cogito.server.browsers import BrowserBridge
        from cogito.tools.browser.browser import BrowserTool

        browser_bridge = BrowserBridge(on_browser_command, cancel)
        install(BrowserTool(browser_bridge))
        guard.ask_before(
            {
                "browser": ExternalTool(
                    server="Cogito",
                    tool="browser",
                    title="Usar el navegador",
                    changes=True,
                    session_only=True,
                )
            }
        )
    # APP-ONLY: terminal tabs the person and the model share (`tools/terminal`): the person's
    # own shell on a PTY, no shims, no sandbox. `run_in_terminal` types a line only after the
    # person approves that exact line, every time (`Guard.ask_terminal`); `read_terminal` is
    # shown while a tab exists. Never in `chat`, a benchmark or the lab's suite holder.
    terminal_host = None
    run_terminal = None
    if _terminal_wanted(in_app):
        from cogito.tools.terminal.host import TerminalHost
        from cogito.tools.terminal.tools import ReadTerminalTool, RunInTerminalTool

        terminal_host = TerminalHost(project_root, base_env, on_terminal_event)

        def _terminal_finished(report: str, event: dict) -> None:
            # A line still running when `run_in_terminal` returned, ended: told like a
            # command moved to the background (`_background_exit`).
            engine = engine_holder.get("engine")
            if engine is not None:
                engine.notify(report)
            if on_background_exit is not None:
                on_background_exit(event)

        run_terminal = RunInTerminalTool(terminal_host, on_finished=_terminal_finished)
        install(run_terminal)
        install(ReadTerminalTool(terminal_host))
        guard.ask_terminal(run_terminal.plan)
    # APP-ONLY: `delegate`, a subagent that investigates one question in a context of its
    # own with the read-only tools and returns only its findings (`tools/delegate.py`,
    # `Engine.subagent`), inside the call, one level deep; up to `DELEGATES_AT_ONCE` of a
    # step's calls run at the same time. Built after the engine below (`_run_delegate`),
    # since the subagent is a second engine on its model.
    # Never in `chat`, a benchmark or the lab's suite holder, so their requests do not
    # change. Its measurement is owed (CONTRIBUTING §7).
    # A writing subagent (kind "write") is offered where its copy can be made and its shell
    # sandboxed: a project at the top of a git repository with a commit, and bubblewrap
    # that starts (`tools.workcopy`, `guard.sandbox`). Decided once, so the definition is
    # the same on every request of the conversation.
    delegate_holder: dict = {}
    if in_app and _app_owned():
        from cogito.guard import sandbox as delegate_sandbox
        from cogito.tools import workcopy
        from cogito.tools.delegate import DelegateTool

        delegate_holder["writes"] = (
            not workcopy.refusal(project_root) and delegate_sandbox.available()[0]
        )
        install(
            DelegateTool(
                lambda task, context: delegate_holder["run"](task, context),
                together=settings.loop.parallel_delegates,
                writer=(
                    (lambda task, context: delegate_holder["write"](task, context))
                    if delegate_holder["writes"]
                    else None
                ),
                # How many requests the model's server decodes at once, where Cogito knows:
                # a Motoris entry's config (cogito-motoris docs/BATCHING.md).
                served=(
                    model_config.motoris_parallel(entry.args)
                    if getattr(entry, "serve", None) == "motoris"
                    else None
                ),
            )
        )
    # APP-ONLY: `todo`, the model's own task list for work of several steps
    # (`tools/todo.py`); the loop sends a turn back once when it would end with items open
    # (`LoopSettings.open_tasks_notice`). Never in `chat`, a benchmark or the suite holder.
    if in_app and _app_owned():
        from cogito.tools.todo import TodoList, TodoTool

        install(TodoTool(TodoList(), record=store.record))
    skipped -= {
        "ask", "recall", "ask_document", "web_search", "web_fetch", "skill", "computer",
        "browser", "run_in_terminal", "read_terminal", "delegate", "todo",
    }
    if skipped:
        # Otherwise a misspelled name would read as a successful A/B against a tool that
        # was never removed.
        raise click.ClickException(
            f"--disable-tool named no such tool: {', '.join(sorted(skipped))}; "
            f"registered: {', '.join(registry.registered)}"
        )

    hooks = HookBus()
    if run_terminal is not None:
        # A `shell` result that shows the command needs a terminal, a password or root lets
        # the model hand that command to the person (`RunInTerminalTool.note_shell`).
        hooks.subscribe(HookPoint.AFTER_TOOL, _shell_needs_person(run_terminal))
    installer = None
    if in_app:
        from cogito.guard.installer import install_packages as installer
    described_calls = None
    if mcp_join is not None:
        from cogito.tools.mcp.install import describe as describe_mcp_add

        # The card shows the exact command or address `mcp_add` will save and run.
        described_calls = {
            "mcp_add": lambda arguments: describe_mcp_add(
                arguments, Path(project_root), paths.settings_file()
            )
        }
    hooks.subscribe(
        HookPoint.BEFORE_TOOL,
        _guard_hook(guard, store.record, session_id, installer, described_calls),
    )
    if recall is not None:
        hooks.subscribe(HookPoint.ON_COMPACT, _follow_compaction(recall))
    hooks.subscribe(HookPoint.ON_COMPACT, _clear_read_snapshots_on_compact(tracker))
    if skill_tool is not None:
        hooks.subscribe(HookPoint.ON_COMPACT, lambda point, payload: skill_tool.forget())
    if mcp_join is not None:
        # APP ONLY. A fold re-reads the whole conversation anyway: servers that became
        # ready join there too, and what is still wrong with the others is said again.
        def _join_at_fold(point, payload):
            mcp_join()
            mcp_hub.forget_told()

        hooks.subscribe(HookPoint.ON_COMPACT, _join_at_fold)
    if mcp_hub is not None and mcp_hub.stable:
        # A fold re-reads the whole conversation anyway: the one point where a fixed list
        # may take in what was called and found since.
        hooks.subscribe(HookPoint.ON_COMPACT, lambda point, payload: mcp_hub.refresh())
    if web_state is not None:
        hooks.subscribe(HookPoint.AFTER_TOOL, _web_addresses_from_files(web_state))
    on_message = recall.remember if recall is not None else None
    on_offload = None
    if documents_index is not None:
        offloads = _documents_from_offloads(documents_index, project_root, artifacts)
        if documents_scope == "all":
            hooks.subscribe(HookPoint.AFTER_TOOL, _documents_from_reads(documents_index))
        on_message = _documents_from_attachments(
            documents_index,
            project_root,
            on_message,
            offloads,
            attached=documents_scope == "all",
        )
        on_offload = offloads

    # Built before the engine, read after it: the capabilities' telemetry names the
    # engine's *current* session, which a fold moves.
    engine_ref: dict[str, Engine] = {}
    capabilities = (
        capabilities_factory(
            record=store.record,
            session_id=lambda: (
                engine_ref["engine"].session_id if "engine" in engine_ref else session_id
            ),
        )
        if capabilities_factory is not None
        else None
    )
    tool_context = ToolContext(
        project_root=project_root,
        session_id=session_id,
        settings=settings,
        artifacts=artifacts,
        cancel=cancel,
        capabilities=capabilities,
    )
    engine = engine_holder["engine"] = Engine(
        provider=provider,
        registry=registry,
        tool_context=tool_context,
        settings=settings,
        session_id=session_id,
        project_id=project_id,
        model_id=entry.id,
        record=store.record,
        on_message=on_message,
        turn_note=_turn_notes(
            _mcp_turn_note(mcp_hub, mcp_join) if mcp_join is not None else None,
            skill_tool.matching if skill_tool is not None else None,
        ),
        turn_skills=skill_tool.activate if skill_tool is not None else None,
        recall_context=(
            (
                lambda query, in_window: recall.context_for(query, in_window=in_window)
                if _document_memory_on()
                else None
            )
            if in_app and recall is not None and settings.estella.auto_recall
            else None
        ),
        hooks=hooks,
        sampling=sampling,
        prompt_time=prompt_time,
        on_phase=on_phase,
        on_context_status=on_context_status,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        on_steer_applied=on_steer_applied,
        on_user_message=on_user_message,
        # Keeps `undo`'s session tag current across a fold (see `UndoStore.__init__`'s
        # docstring and the strict review's compaction-lineage finding, item 2).
        on_session_changed=(
            undo.set_session_id
            if on_compacted is None
            else lambda sid: (undo.set_session_id(sid), on_compacted(sid))
        ),
        # The window's live view of each reply; only the server wires it.
        on_stream=on_stream,
        # APP-ONLY: the quiet «Continuando…» line; only `_serve_factory` wires it.
        on_auto_continue=on_auto_continue,
        # The build gate's subprocesses (`tools/build.py`, triggered from `loop/engine.py`)
        # run under the same sandbox prefix and environment as `check`'s linters.
        # `check_wrapper` is empty outside `--sandbox`; `shell_env` is the interactive
        # shell's PATH and cache variables either way.
        build_gate_wrapper=check_wrapper,
        build_gate_env=shell_env,
        # The same for `chat`, every benchmark task and the window, and only with `[loop]
        # model_identity` on, so what the app sends is what the suite measured
        # (cogito-lab's byte-identity goldens pin it with the switch off).
        identity=_model_identity(entry, settings),
        spend_gate=spend_gate,
        # APP-ONLY: what the guard asked and refused during each call is kept with it (the
        # thread read back after a restart draws the same pills), and the wait for a person
        # is taken out of the call's time. `chat` and every benchmark task record and time
        # their calls exactly as before.
        take_answers=guard.ledger.take if in_app else None,
        # App only, like the package requests above: `chat` and every benchmark never
        # check answers, so their requests do not change.
        grounding_check=in_app and _grounding_for(entry, settings),
        on_offload=on_offload,
        # The person's words make the addresses in them readable by `web_fetch`. None unless
        # the web tools are registered.
        on_person_text=_person_text_listeners(
            web_state.note_person if web_state is not None else None,
            mcp_hub.observe if mcp_hub is not None else None,
        ),
        # The identity line says what is registered (the web, or no browser at all). It is
        # built in the app, and in `chat` only with `[web] in_chat`, where the tools are:
        # every other `chat` and benchmark task keeps the line it always sent.
        abilities=(
            # In the app the web tools are always registered and shown only while the web
            # level is on, so the line follows that level on every step too.
            (
                (lambda: prompt_builder.abilities(web=web_state.allowed()))
                if web_state is not None
                else prompt_builder.abilities(web=False)
            )
            if in_app
            else prompt_builder.abilities(web=True)
            if web_state is not None
            else None
        ),
        # APP-ONLY: a fold hands back the files the session touched (`loop.fold_files`).
        # Same gate as `documents_in_app`: `chat`, and so every benchmark task, never
        # passes ``conversations``, so its seed is unchanged.
        fold_files=(
            FoldFiles(edit_format, tracker, project_root)
            if in_app and settings.loop.fold_files_in_app
            else None
        ),
        # APP ONLY: the distribution and package managers, so the model does not guess.
        system_facts=system_facts,
    )
    engine_ref["engine"] = engine
    if registry.has("delegate"):
        delegate_holder["run"] = _delegate_runner(
            engine,
            store=store,
            project_id=project_id,
            tool_context=tool_context,
            settings=settings,
            registry=registry,
            fresh_read=lambda: ReadTool(edit_format, FileTracker()),
            fresh_web=(
                (lambda: _child_web_tools(web_state, browser_wanted))
                if web_state is not None
                else None
            ),
            hooks=_delegate_hooks(guard, store.record, installer, web_state),
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
        )
        if delegate_holder.get("writes"):
            delegate_holder["write"] = _writer_runner(
                engine,
                store=store,
                project_id=project_id,
                project_root=project_root,
                tool_context=tool_context,
                settings=settings,
                edit_format=edit_format,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
            )
    if continuation is not None:
        if web_state is not None:
            _reseed_web_addresses(web_state, continuation)
        if on_offload is not None:
            # Before `continue_from`, which offloads what no longer fits and offers that.
            _reoffer_offloads(on_offload, continuation)
        engine.continue_from(continuation, rebuilt=rebuilt)

    engine_session = _track_turns(
        EngineSession(
            project_root=project_root,
            settings=settings,
            entry=entry,
            session_id=session_id,
            project_id=project_id,
            store=store,
            guard=guard,
            broker=broker,
            shell_session=shell_session,
            registry=registry,
            estella=estella,
            recall=recall,
            hooks=hooks,
            engine=engine,
            driver=driver,
            undo=undo,
            tracker=tracker,
        )
    )
    if computer_tool is not None:
        # The portal's remote desktop session ends with Cogito's.
        engine_session._on_close.append(lambda _session: computer_tool.close())
    if browser_bridge is not None:
        # The window's answers route reaches it; a call still waiting ends with the session.
        engine_session.browser_bridge = browser_bridge
        engine_session._on_close.append(lambda _session: browser_bridge.close())
    if terminal_host is not None:
        # The tabs end with the session: a model switch keeps the session, so they stay.
        engine_session.terminals = terminal_host
        engine_session._on_close.append(lambda _session: terminal_host.close_all())
    if conversations is not None:
        conversations.add(
            engine_session, claimed=continuation.session_id if continuation is not None else None
        )
    return engine_session


def _system_facts():
    """APP ONLY: the machine's distribution and system package managers for the
    environment line (`context.prompt.SystemFacts`). Read from the OS, once per session."""
    from cogito.context.prompt import SystemFacts
    from cogito.guard.platform import facts

    return SystemFacts(facts.os_label(), facts.managers_on_path(), facts.aur_helpers_on_path())


#: What a background command's report carries of its output: its last lines, each and all
#: of them bounded. A choice, not a measurement; the model can read more with `shell`.
_REPORT_LINES = 40
_REPORT_LINE_CHARS = 300
_REPORT_CHARS = 4000


def _background_report(job, exit_code) -> str:
    """What the model reads when a command it left in the background ends: which command,
    how it ended and its last lines (`ShellSession.on_exit`)."""
    command = " ".join(job.command.split())
    if len(command) > 300:
        command = command[:300] + " …"
    ended = (
        f"ended with exit {exit_code}" if exit_code is not None else "was stopped before it ended"
    )
    lines = [line.rstrip("\n")[:_REPORT_LINE_CHARS] for line in list(job.tail)[-_REPORT_LINES:]]
    tail = "\n".join(lines).strip()
    if len(tail) > _REPORT_CHARS:
        tail = "…\n" + tail[-_REPORT_CHARS:]
    head = f"`{command}` (pid {job.pid}) {ended}."
    return f"{head}\nIts last lines:\n{tail}" if tail else f"{head} It printed nothing."


def _app_output_cap(entry, settings):
    """APP-ONLY: ``settings`` with a wider per-step output ceiling, for a model whose
    context window is a known fact and large enough that the default 4,096-token cap
    (`BudgetSettings.max_output_tokens_per_step`) cuts off an ordinary file write.

    Seen on a real session (qwen3-coder-30b, ctx 43008): a whole-file write took two
    samples, each cut at exactly 4,096 tokens; the first was refused (truncated-write),
    the second's cut text was read as the model finishing (fixed separately in
    `loop.engine`, the `StopReason.OUTPUT_LIMIT` case). Widening the cap here is the other
    half: give a long write the room to finish in one sample.

    Not tuned to one model: ``entry.api`` picks which catalog fact decides whether the
    window is known, never a model name.

    - Every OpenAI-shaped host (llama.cpp, Ollama, vLLM, LM Studio, OpenAI itself,
      DeepSeek, OpenRouter, Groq): known exactly when the entry declares its own ``ctx``
      (`config/models.py`'s ``[model.args]``) -- the same fact
      `provider.openai_api.OpenAIProvider.capabilities` reads for `context_known`.
    - The Anthropic dialect: known for a declared ``ctx`` too, or for a model
      `provider.anthropic_api._TRAITS` recognizes (sourced from Anthropic's own docs, not
      guessed here) -- `anthropic_api.context_window_known`, the same fact
      `AnthropicProvider.capabilities` reports as `context_known`. This function asks the
      catalog directly.

    A model neither source names a window for is left at today's default, whatever host or
    vendor it is: a guessed window is not evidence the real one is large enough to widen
    the cap safely.

    The cap is ``max(4096, min(16384, window // 3))``: never smaller than today's default,
    and never above a fixed ceiling rather than a fraction of a possibly huge window. 16,384
    is a safety margin, not a documented figure: this repository has no current, attributed
    source for each provider's own maximum output tokens per model (CONTRIBUTING section
    6b), so instead of guessing one per vendor, the ceiling is kept low enough that it is
    expected to sit under every mainstream provider's own limit, Claude and OpenAI included.
    An operator who knows a tighter real figure for one entry can declare a narrower ``ctx``
    than the model's real window; this function never asks a service for its own output
    limit and never raises the cap past the window itself.

    Only `_build_engine`'s app sessions (``in_app``) call this, the same gate
    `documents_in_app` reads; `chat`, and so every benchmark task, never does, so its
    budgets and requests are byte-identical to before this change.
    """
    from dataclasses import replace

    if entry.api == "anthropic":
        from cogito.provider.anthropic_api import context_window, context_window_known

        if not context_window_known(entry):
            return settings
        window = context_window(entry)
    else:
        declared = entry.args.get("ctx")
        if not (isinstance(declared, int) and not isinstance(declared, bool) and declared > 0):
            return settings
        window = declared
    cap = max(4096, min(16_384, window // 3))
    if cap <= settings.budgets.max_output_tokens_per_step:
        return settings
    return replace(settings, budgets=replace(settings.budgets, max_output_tokens_per_step=cap))


#: APP-ONLY (long-output fix: auto-continue). How many times a turn may continue
#: automatically past the output limit before the window's «Continuar» button is the only
#: way on. Matches Claude Code's own non-interactive continuation cap (code.claude.com/docs,
#: "Non-interactive mode", read 2026-10-12): three in a row. Decided by the person
#: 2026-10-12, not yet measured (CONTRIBUTING section 7's three-outcome rule): the person
#: must never have to press «Continuar».
_APP_MAX_AUTO_CONTINUATIONS = 3


def _app_auto_continue_cap(settings):
    """APP-ONLY: ``settings`` with automatic continuation past the output limit turned on
    (`BudgetSettings.max_auto_continuations`), same gate as `_app_output_cap`
    (``_build_engine``'s ``in_app``). `chat` and every benchmark task never call this, so
    the setting stays 0 and their requests are unchanged."""
    from dataclasses import replace

    if settings.budgets.max_auto_continuations:
        return settings
    return replace(
        settings,
        budgets=replace(
            settings.budgets, max_auto_continuations=_APP_MAX_AUTO_CONTINUATIONS
        ),
    )


def _with_thinking(options, level, settings):
    """``options`` for one turn at the composer's thinking ``level``
    (`config.settings.THINKING_LEVELS`). "off" turns thinking off through the chat template;
    "long" raises a budget the session already sends to `long_reasoning_tokens_per_step`,
    leaving a quarter of the step's output (at least 512 tokens) for the answer, as Motoris
    does without a budget, and never below the session's own; "free" drops it, so the
    server's own rule applies. A model whose host takes no budget (`_with_reasoning_budget`
    set none) keeps its budget options for "long" and "free". Every level also rides as
    `RequestOptions.thinking_level`, which Claude maps to its own effort ladder
    (`AnthropicProvider._effort`); llama.cpp's dialect never sends it."""
    from dataclasses import replace

    options = replace(options, thinking_level=level)
    if level == "off":
        return replace(options, reasoning=False, reasoning_budget=None)
    if options.reasoning_budget is None:
        return options
    if level == "long":
        step = settings.budgets.max_output_tokens_per_step
        room = step - max(512, step // 4)
        longer = min(settings.budgets.long_reasoning_tokens_per_step, room)
        budget = max(options.reasoning_budget, longer)
        return replace(options, reasoning_budget=budget)
    if level == "free":
        return replace(options, reasoning_budget=None)
    return options


def _with_reasoning_budget(entry, settings, options):
    """``options`` with the step's reasoning budget for an entry whose host takes
    server extensions, else unchanged. The budget is a llama-server request field, so only
    such an entry gets one, and the provider sends it only for a model known to reason
    (`OpenAIProvider._reasoning_budget`). Called when a session is built (`_build_engine`)
    and when the window's picker moves a session to another model (`_ServedModel`), so a
    switch to a local model is budgeted like a session started on it."""
    from dataclasses import replace

    if getattr(entry, "takes_server_extensions", False) and (
        budget := settings.budgets.max_reasoning_tokens_per_step
    ) > 0:
        return replace(options, reasoning_budget=budget)
    return options


def _model_identity(entry, settings):
    """How a session names `entry` in the prompt (`context.prompt.ModelIdentity`): the name
    the window shows, the name its server knows it by, and who serves it. None while
    `[loop] model_identity` is off (the default until it is measured); the engine then says
    nothing about the model, and nothing about the entry is read or asked here."""
    if not settings.loop.model_identity:
        return None
    from cogito.context.prompt import ModelIdentity
    from cogito.provider import service

    info = service.describe(entry)
    return ModelIdentity(
        name=entry.display_label,
        model=entry.model,
        service=info.name,
        local=entry.origin == "local",
    )


def _window_approver(approvals, emit, cancel, auto=None):
    """Ask the window and wait on the turn's own thread for the answer.

    APP ONLY: with the composer's automatic mode on (``auto``, `guard.auto.AutoMode`), a
    question it does not keep asking is allowed here, unasked: the window gets the request
    with ``"auto": true``, which it draws as the pill «Automático», and no card.

    The guard calls this from inside the tool hook, so blocking here is what holds the
    action back; there is nowhere else to hold it. The request goes out in the terms the
    person has to check: the command as written, the paths it resolved to, the ones it
    could not resolve, and whether the content could be recovered afterwards.

    Anything that is not a clear yes is a no. No answer, a closed window or an interrupted
    turn all end here as a refusal with a note saying which, because "nothing ran because
    you said no" and "nothing ran because nobody was there" are different facts to someone
    reading the transcript later. `approval_resolved` carries the same distinction:
    `reason` is `"user"` when a person answered (true or false), or
    `"timeout"`/`"interrupted"` when nobody did. Then `allowed` goes out `null`, never a
    made-up `false`, so the window can tell "refused" from "nobody was there" without
    reading `reason`. `ApprovalResponse.allowed`, what the guard acts on, stays `False`
    either way: the default is refusal, and only the window's record of why changes.
    """
    from dataclasses import replace

    from cogito.guard.approval import ApprovalResponse
    from cogito.guard.policy import INSTALL_ACTIONS
    from cogito.ids import new_id
    from cogito.loop.state import Phase

    def described(request) -> dict:
        """The ``approval_request`` event for ``request``, without its id."""
        return {
            "type": "approval_request",
            "summary": request.summary,
            "command": request.command,
            "targets": [str(path) for path in request.targets],
            "unresolved": list(request.unresolved),
            "reason": request.reason,
            "recoverable": request.recoverable,
            # The facts the window words the card from, in its own language.
            "action": request.action,
            "recovery": request.recovery,
            "items": [
                {
                    "path": str(item.path),
                    "action": item.action,
                    "kind": item.kind,
                    "entries": item.entries,
                    "capped": item.capped,
                }
                for item in request.items
            ],
            "targets_unknown": request.targets_unknown,
            "within": [str(path) for path in request.within],
            "listed": list(request.listed),
            "listed_more": request.listed_more,
            "outside": request.outside,
            # An install, a full upgrade or an AUR install asks about packages, not paths
            # (`guard/packages.py`).
            **(
                {
                    "packages": list(request.packages),
                    "manager": request.manager,
                    "source": request.source,
                }
                if request.action in INSTALL_ACTIONS.values()
                else {}
            ),
            # An MCP server's tool (design 4g): the server, the tool, its title and
            # whether it may change something; `command` holds the arguments.
            **(
                {
                    "server": request.server,
                    "tool": request.tool,
                    "tool_title": request.tool_title,
                    "changes": request.changes,
                    # Asked about every time (`mcp_add`): no «Permitir siempre».
                    **({"every_time": True} if request.every_time else {}),
                }
                if request.action == "external_tool"
                else {}
            ),
            # A line typed into the person's terminal (APP ONLY): the tab it goes to and
            # where its shell runs; `command` is the line, and the button is «Ejecutar».
            **(
                {"tab_title": request.tab_title, "cwd": request.cwd}
                if request.action == "run_in_terminal"
                else {}
            ),
            # APP ONLY: why the automatic mode still asks (`guard.auto`).
            **({"kept_asking": request.kept_asking} if request.kept_asking else {}),
        }

    def approve(request) -> ApprovalResponse:
        if auto is not None and auto.on:
            kept = auto.kept_asking(request)
            if kept is None:
                emit({**described(request), "id": f"auto-{new_id()}", "auto": True})
                return ApprovalResponse(
                    allowed=True, note="allowed by automatic mode", reason="auto"
                )
            request = replace(request, kept_asking=kept)
        identifier = approvals.open()
        emit({"type": "phase", "phase": Phase.AWAITING_APPROVAL.value})
        emit({**described(request), "id": identifier})
        # Waits until the person answers: «Detener» or a closing window ends it, never the
        # clock (`server/approvals.py`).
        allowed = approvals.resolve(identifier, timeout=None, abort=lambda: cancel.cancelled)
        if allowed is None:
            outcome_reason = "interrupted" if cancel.cancelled else "timeout"
            emit(
                {
                    "type": "approval_resolved",
                    "id": identifier,
                    "allowed": None,
                    "reason": outcome_reason,
                }
            )
            note = (
                "the turn was interrupted before this was answered; nothing ran"
                if cancel.cancelled
                else "nobody answered in time; nothing ran"
            )
            return ApprovalResponse(allowed=False, note=note, reason=outcome_reason)
        scope = approvals.scope(identifier) if allowed else ""
        emit(
            {
                "type": "approval_resolved",
                "id": identifier,
                "allowed": allowed,
                "reason": "user",
                **({"scope": scope} if scope else {}),
            }
        )
        if allowed:
            emit({"type": "phase", "phase": Phase.EXECUTING.value})
        # An allowed MCP tool is remembered (`guard/allowed_tools.py`); the guard ignores
        # this for every other kind of question.
        return ApprovalResponse(allowed=allowed, remember_tool=allowed)

    return approve


def _session_capabilities(components, questions, project_root: Path, emit, cancel):
    """The ``capabilities_factory`` ``_build_engine`` takes, for one window session, and
    the dict it leaves the built object in.

    A permission request waits on the window the same way an approval does, so it moves
    the phase the same way: ``awaiting_approval`` while the card is up (the tray and the
    orb turn amber), ``executing`` once it is answered.
    """
    from cogito.loop.state import Phase

    built: dict[str, object] = {}

    def on_waiting(waiting: bool) -> None:
        phase = Phase.AWAITING_APPROVAL if waiting else Phase.EXECUTING
        emit({"type": "phase", "phase": phase.value})

    def make(*, record, session_id):
        built["capabilities"] = components.session(
            project_root,
            emit=emit,
            cancel=cancel,
            questions=questions,
            record=record,
            session_id=session_id,
            on_waiting=on_waiting,
        )
        return built["capabilities"]

    return make, built


def _spend_gate(budget, holder, first_entry, emit, cancel):
    """The ``spend_gate`` ``_build_engine`` takes, for one window session: COGITO's budget,
    asked before each request about the model the session serves at that moment (the
    picker may have moved it). While it waits for the person, the phase is the amber
    permission state, as for a permission request."""
    from cogito.loop.state import Phase

    def waiting() -> None:
        emit({"type": "phase", "phase": Phase.AWAITING_APPROVAL.value})

    def gate():
        engine_session = holder.get("session")
        entry = engine_session.entry if engine_session is not None else first_entry
        return budget.gate(entry, emit=emit, cancel=cancel, on_waiting=waiting)

    return gate


def _same_address(one: str, other: str) -> bool:
    """True when two base URLs reach the same server: the same port, and the same host or
    both on this machine (127.0.0.1, localhost and ::1 are one host here)."""
    from urllib.parse import urlparse

    from cogito.provider import connect

    def where(url: str):
        try:
            parsed = urlparse(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        return ("loopback" if connect.is_local_url(url) else host, port)

    found = where(one)
    return found is not None and found == where(other)


def _missing(entry) -> str | None:
    """What keeps an entry from being used now, as a kind the window knows: ``key`` (an API
    entry with no key in its environment variable, its `api_key_file` or the settings
    screen's store, or with a key file that cannot be used) or ``install`` (an entry Cogito
    serves whose Cogito Motoris checkout or engine JSON is missing). None when nothing does. Reads files only; asks no
    server."""
    if entry.origin == "api":
        try:
            key = secrets.model_key(entry)
        except CogitoError:
            key = None
        if not key:
            return "key"
    if entry.is_served_by_cogito:
        from cogito.provider.local import motoris

        try:
            motoris.build_command(entry)
        except CogitoError:
            return "install"
        if not (motoris._checkout(entry) / "serve" / "server.py").is_file():
            return "install"
    return None


def _offline(entry) -> bool:
    """Whether an entry Cogito does not start itself (no `serve`: a server someone else runs,
    saved from the connection screen) has nothing listening at its address now. Such an
    entry is left out of the picker until its server is up again (the person, 2026-10-26: a
    saved server that is not live is not shown). API entries and the ones Cogito serves are
    never checked. A TCP connect only, with a short timeout, so the picker stays quick."""
    import socket
    from urllib.parse import urlparse

    if entry.origin == "api" or entry.is_served_by_cogito:
        return False
    parsed = urlparse(entry.base_url)
    if parsed.hostname is None:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        socket.create_connection((parsed.hostname, port), timeout=_OFFLINE_TIMEOUT_S).close()
    except OSError:
        return True
    return False


#: How long `_offline` waits for a saved server to accept a connection. A server on this
#: machine or the local network answers in milliseconds; a choice, not a measured value.
_OFFLINE_TIMEOUT_S = 0.3


class _Skills:
    """Lists the Agent Skills a project offers, for the composer's picker
    (`server/skills.py`'s `SkillsLike`). App-only: `chat` passes no skills lister to
    `create_app`, so a benchmark run never reaches the route. Settings are re-read on each
    call (like `Connections`'s config `load`), so turning skills off empties the picker at
    once. Discovery is the same `skills_for` the engine uses, so the picker and `skill`'s
    catalog list the same skills."""

    def __init__(self, load: Callable[[], Any]):
        self._load = load

    def list(self, project_root: Path) -> dict[str, Any]:
        settings = self._load()
        if not settings.skills.enabled:
            return {"skills": []}
        from cogito.tools.skills import skills_for

        found = skills_for(
            Path(project_root), Path.home(), switched_off=_mcp_switched_off(settings)
        )
        return {
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "directory": str(skill.directory),
                }
                for skill in found.values()
            ]
        }


class _RecentProjects:
    """Recently opened projects (design 1f, the desktop app's «Proyectos» screen), one row
    per project root, in Cogito's config dir. Global like ``last-model.toml`` and
    ``decisions.toml``, and kept out of the graph on purpose: ``GET /projects`` reports
    every project the graph has seen but returns an empty list whenever Neo4j is down, and
    the picker has to work before Neo4j does (during setup, or with the store unreachable).
    ``recent-projects.toml``::

        [[project]]
        path = "/home/ana/work/tienda-api"
        last_opened = "2026-09-29T10:42:00+00:00"

    ``list`` reports each entry with ``name`` (the folder's basename) and ``missing`` (its
    folder no longer exists), newest first, so the window can offer "Quitar de la lista"
    for one that vanished instead of dropping it silently. ``touch`` records a path as
    opened now, moving it to the front; ``forget`` removes it by path. Neither touches the
    project's own thread (in the graph and its journal).
    """

    def __init__(self, path: Path | None = None, *, now=None):
        self._path = path if path is not None else paths.config_dir() / "recent-projects.toml"
        self._now = now if now is not None else lambda: datetime.now(UTC)

    def _read(self) -> list[dict[str, str]]:
        from cogito.config import tomlfile
        from cogito.errors import ConfigError

        try:
            document = tomlfile.read(self._path)
        except (ConfigError, OSError):
            return []
        entries = document.get("project")
        if not isinstance(entries, list):
            return []
        out = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            root = entry.get("path")
            when = entry.get("last_opened")
            if isinstance(root, str) and root.strip() and isinstance(when, str) and when.strip():
                out.append({"path": root, "last_opened": when})
        return out

    def _write(self, entries: list[dict[str, str]]) -> None:
        from cogito.config import tomlfile

        lines = ["# Recently opened projects (design 1f); order carries no meaning.\n"]
        for entry in entries:
            lines.append("\n[[project]]\n")
            lines.append(f"path = {tomlfile.quote(entry['path'])}\n")
            lines.append(f"last_opened = {tomlfile.quote(entry['last_opened'])}\n")
        tomlfile.write_atomically(self._path, "".join(lines))

    def list(self) -> list[dict[str, Any]]:
        """Newest first: ``{"path", "name", "last_opened", "missing"}`` per entry."""
        entries = sorted(self._read(), key=lambda e: e["last_opened"], reverse=True)
        return [
            {
                "path": entry["path"],
                "name": Path(entry["path"]).name or entry["path"],
                "last_opened": entry["last_opened"],
                "missing": not Path(entry["path"]).is_dir(),
            }
            for entry in entries
        ]

    def touch(self, project_root: str) -> None:
        """Record ``project_root`` as opened now, moving it to the front."""
        entries = [e for e in self._read() if e["path"] != project_root]
        entries.append({"path": project_root, "last_opened": self._now().isoformat()})
        self._write(entries)

    def forget(self, project_root: str) -> None:
        """Drop ``project_root`` from the list ("Quitar de la lista" for a folder that no
        longer exists). Never touches the project's own history."""
        entries = [e for e in self._read() if e["path"] != project_root]
        self._write(entries)


def _initial_model(catalog, settings, model_id: str | None = None, *, last=None):
    """The entry `serve` starts with: ``--model``, else the one last chosen in the window's
    picker while the catalog still has it (`config/last_model.py`), else the configured
    default, else the first entry. None for an empty catalog. `last` stands in for the file
    in tests."""
    from cogito.config import last_model

    if model_id:
        return catalog.get(model_id)
    remembered = last() if last is not None else last_model.load()
    if remembered and any(entry.id == remembered for entry in catalog.entries):
        return catalog.get(remembered)
    chosen = settings.default_model_id
    if chosen:
        return catalog.get(chosen)
    return catalog.entries[0] if catalog.entries else None


def _first_model(entry, settings, *, bench=None, build=None):
    """What `serve` starts with: ``(entry, provider, server, unserved)``.

    ``entry`` None (an empty catalog) is served as nothing. A local entry is never started,
    connected or attached to while cogito-lab's benchmark holds the GPU (`bench`,
    `provider.local.benchlock.holder`): the benchmark's own model server may be answering
    on the same port with the same model, and `MotorisServer` would adopt it. An entry that
    fails to start (a missing checkout, a server that dies, a key file that cannot be used)
    is not served either. Both leave ``unserved``: ``{"reason": "bench_running"|"failed",
    "model": {"id","label","origin"}, "log": [...]}``, so the window can say why.
    """
    from cogito.errors import ProviderError

    if entry is None:
        return None, None, None, None
    about = {"id": entry.id, "label": entry.display_label, "origin": entry.origin}
    holder = bench() if bench is not None and entry.origin == "local" else None
    if holder is not None:
        return None, None, None, {
            "reason": "bench_running",
            "model": about,
            "log": [f"{holder.path} held by pid {holder.pid or 'unknown'}"],
        }
    try:
        build = build or (lambda e: _build_provider(e, settings, quiet=True))
        provider, server = build(entry)
    except (ProviderError, CogitoError, click.ClickException) as exc:
        text = exc.message if isinstance(exc, click.ClickException) else str(exc)
        log_lines = [line for line in text.splitlines() if line.strip()]
        return None, None, None, {"reason": "failed", "model": about, "log": log_lines}
    return entry, provider, server, None


class _ServedModel:
    """The one model this core serves, and the window's model picker switching it (design
    1a, the model chip's menu).

    One at a time: a local model is a Cogito Motoris holding the device memory and a port,
    and every session this core opens talks to it. `switch(model_id)`:

    - For an entry Cogito serves (``serve = "motoris"``), the server Cogito started for the
      current model is stopped first, since it holds the memory and the port the next one
      needs. The new one starts through `MotorisServer` (`_launch_local`). A server this
      core only attached to (someone else's, `MotorisServer.adopted`) is never stopped, and
      `MotorisServer` refuses to start another model on its port.
    - When the new model cannot start (its checkout is gone, the server died), the previous
      model is started again and stays active. The answer carries the real reason.
    - Any other entry is only connected (`_connect`). A server Cogito was running for the
      previous model is stopped once the new provider exists, since nothing uses it now.
    - Every session this core opened follows (`EngineSession.use_model`): later requests go
      to the new model on the same transcript. The choice is remembered (`remember`, the
      `config/last_model.py` file under `serve`) so the next `serve` starts with it, and
      `on_switched` gets the new id afterwards.

    A switch counts as a running turn (`_turn_started`), so the document index waits for
    it. The server route refuses a switch while any turn or rewind runs; nothing here
    checks that again. `launch` and `connect` are `_launch_local` and `_connect` unless a
    test passes fakes; this class starts Cogito Motoris no other way.
    """

    def __init__(
        self,
        entry,
        settings,
        catalog,
        *,
        provider,
        server=None,
        ledger=None,
        launch=None,
        connect=None,
        remember=None,
        on_switched=None,
        describe=None,
        budget=None,
        broadcast=None,
        bench=None,
        revive=False,
    ) -> None:
        from cogito.provider import service as service_module

        #: Who holds cogito-lab's benchmark lock, or None (`provider.local.benchlock.holder`).
        #: No local model starts while a benchmark measures on this GPU. `serve` passes the
        #: real one; without it nothing is checked.
        self._bench = bench
        #: COGITO's budget and the every-session broadcast; each meter passes them what it
        #: observed (`provider.usage.MeteredProvider`).
        self.budget = budget
        self.broadcast = broadcast
        self._settings = settings
        self._catalog = catalog
        self._ledger = ledger
        self._launch = launch or (lambda chosen: _launch_local(chosen, settings, quiet=True))
        self._connect = connect or _connect
        self._remember = remember
        self._describe = describe or service_module.describe
        self.on_switched = on_switched
        #: Held for a whole switch, and by the session factory while it builds and attaches
        #: a session on the current model, so neither lands inside the other.
        self.lock = threading.RLock()
        self._sessions: weakref.WeakSet = weakref.WeakSet()
        #: APP ONLY (a core the desktop app started): a Cogito Motoris this core started that
        #: dies under a request is started again and the request asked again
        #: (`provider.local.motoris.RevivingProvider`). `chat` and the lab never set it.
        self._revive = revive
        self._entry = entry
        self._provider = self._revivable(entry, provider)
        self._server = server
        #: Why the model `serve` started with is not served (`_first_model`), until a switch
        #: serves one; None otherwise.
        self.unserved = None

    def _revivable(self, entry, provider):
        """`provider`, wrapped so a crash of the Cogito Motoris this core started for
        `entry` is restarted and asked again; unchanged unless `revive` is on and `entry`
        is one Cogito starts. The server is looked up when a request fails, so a restart
        after a failed switch is covered, and a server this core only attached to
        (`MotorisServer.adopted`) is never restarted (`MotorisServer.exited`)."""
        if not self._revive or provider is None or entry is None or not entry.is_served_by_cogito:
            return provider
        from cogito.provider.local.motoris import Revival, RevivingProvider

        def server_of():
            current = self._entry
            if current is None or current.id != entry.id:
                return None
            return self._server

        def notify(event: dict) -> None:
            if self.broadcast is not None:
                self.broadcast({**event, "model_id": entry.id})

        return RevivingProvider(provider, Revival(server_of, notify=notify))

    @property
    def entry(self):
        return self._entry

    @property
    def provider(self):
        return self._provider

    def active_id(self) -> str | None:
        """The model served now; None before the first one (a first launch, design 1d-1e)."""
        return self._entry.id if self._entry is not None else None

    @property
    def server_pid(self) -> int | None:
        """The Cogito Motoris this core started for the model served now, if any."""
        server = self._server
        if server is None or getattr(server, "adopted", False):
            return None
        return getattr(server, "pid", None)

    def current(self) -> tuple:
        """``(provider, entry, service label)`` of the model served now. Raises `NoModel`
        before the first model is connected: there is nothing to build a session on."""
        with self.lock:
            if self._entry is None:
                raise NoModel("no model is served yet; connect one first")
            return self._provider, self._entry, self._describe(self._entry).label

    def attach(self, engine_session) -> None:
        """A session built on the model served now, to follow the next switch."""
        self._sessions.add(engine_session)

    def describe(self, entry) -> dict:
        return {
            "id": entry.id,
            "label": entry.display_label,
            "origin": entry.origin,
            "service": self._describe(entry).label,
        }

    def facts(self, model_id: str) -> dict | None:
        """What the picker shows about one entry: the service it belongs to, its port (so
        the picker can tell two servers of the same service name apart, item B), what keeps
        it from being used (`_missing`) and whether a server it only attaches to is down
        (`_offline`). None for an id the catalog does not have."""
        from urllib.parse import urlparse

        try:
            entry = self._catalog.get(model_id)
        except CogitoError:
            return None
        return {
            "service": self._describe(entry).label,
            "missing": _missing(entry),
            "port": urlparse(entry.base_url).port,
            "offline": _offline(entry),
        }

    def switch(self, model_id: str) -> dict:
        """Serve `model_id` from now on; see the class docstring. The answer is
        ``{"changed", "model"}``, or ``{"error": "unknown model"}``, or ``{"error":
        "unavailable", "missing", "model"}``, or ``{"error": "failed", "model", "log",
        "restored"}``."""
        try:
            chosen = self._catalog.get(model_id)
        except CogitoError:
            return {"error": "unknown model"}
        with self.lock:
            if self._entry is not None and chosen.id == self._entry.id:
                return {"changed": False, "model": self.describe(chosen)}
            missing = _missing(chosen)
            if missing is not None:
                return {"error": "unavailable", "missing": missing, "model": self.describe(chosen)}
            holder = self._bench() if self._bench is not None and chosen.origin == "local" else None
            if holder is not None:
                return {
                    "error": "bench_running",
                    "model": self.describe(chosen),
                    "log": [f"{holder.path} held by pid {holder.pid or 'unknown'}"],
                }
            _turn_started()
            try:
                answer = self._replace(chosen)
            finally:
                _turn_ended()
        if answer.get("changed") and self.on_switched is not None:
            try:
                self.on_switched(chosen.id)
            except Exception:  # noqa: BLE001 - what follows a switch never undoes it
                import logging

                logging.getLogger(__name__).exception("after switching to %s", chosen.id)
        return answer

    def _replace(self, chosen) -> dict:
        from cogito.errors import ProviderError

        previous, previous_server = self._entry, self._server
        stopped_previous = False
        started = None
        try:
            if chosen.is_served_by_cogito:
                if previous_server is not None and not getattr(previous_server, "adopted", False):
                    previous_server.stop()
                    stopped_previous = True
                started = self._launch(chosen)
            provider = self._revivable(chosen, self._connect(chosen))
        except (ProviderError, CogitoError, click.ClickException) as exc:
            if started is not None:
                started.stop()
            restored = True
            if stopped_previous:
                restored = self._restart(previous)
            return self._failure(chosen, exc, restored)
        if previous_server is not None and not chosen.is_served_by_cogito:
            if previous is not None and _same_address(previous.base_url, chosen.base_url):
                # The chosen entry points at this server: stopping it would serve nothing.
                started = previous_server
            else:
                previous_server.stop()
        self._entry, self._provider, self._server = chosen, provider, started
        self.unserved = None
        for engine_session in list(self._sessions):
            engine_session.use_model(
                chosen,
                *self._for_session(chosen, provider, getattr(engine_session, "meter", None)),
            )
        if self._remember is not None:
            try:
                self._remember(chosen.id)
            except (CogitoError, OSError):
                import logging

                logging.getLogger(__name__).warning("could not remember %s as chosen", chosen.id)
        return {"changed": True, "model": self.describe(chosen)}

    def _restart(self, previous) -> bool:
        """Start again the server the switch stopped, so the previous model stays. If
        cogito-lab's benchmark took the GPU meanwhile, it is left stopped."""
        from cogito.errors import ProviderError

        if self._bench is not None and self._bench() is not None:
            self._server = None
            return False
        try:
            self._server = self._launch(previous)
        except (ProviderError, CogitoError, click.ClickException):
            import logging

            logging.getLogger(__name__).exception("could not start %s again", previous.id)
            self._server = None
            return False
        return True

    def _for_session(self, entry, provider, previous=None) -> tuple:
        """``(provider, meter)`` for one session on `entry`. Metered the way `_serve_factory`
        meters a new session, following the session's `previous` meter so its stream, the
        broadcast and the budget stay its own. Configured with the new model's request
        options: its reasoning budget. The session's tools and settings were resolved once
        and do not change."""
        from cogito.provider.base import RequestOptions

        served = meter = None
        if self._ledger is not None:
            from cogito.provider.usage import MeteredProvider

            label = self._describe(entry).label
            if isinstance(previous, MeteredProvider):
                served = meter = previous.follow(provider, entry=entry, service=label)
            else:
                served = meter = MeteredProvider(
                    provider,
                    self._ledger,
                    model_id=entry.id,
                    service=label,
                    entry=entry,
                    broadcast=self.broadcast,
                    budget=self.budget,
                )
        served = served if served is not None else provider
        options = _with_reasoning_budget(entry, self._settings, RequestOptions())
        if options != RequestOptions() and hasattr(served, "configured"):
            served = served.configured(options)
        return served, meter

    def _failure(self, chosen, exc, restored: bool) -> dict:
        text = exc.message if isinstance(exc, click.ClickException) else str(exc)
        answer = {
            "error": "failed",
            "model": self.describe(chosen),
            "log": [line for line in text.splitlines() if line.strip()],
            "restored": restored,
        }
        return answer


class _Connect:
    """The window's welcome and connection screen (design 1d-1e, and «Conectar otro
    servicio…» in the model menu), built from `config.welcome`, `provider.connect`, the
    catalog and the served model. Every method returns a plain dict for `server/connect.py`.

    - A test never makes a model request that costs anything. An API service is asked for
      its model list with the key. Cogito Motoris is read from its install (`motoris_install`,
      runs nothing) or asked on `/health` (`motoris_running`).
    - `save` stores an API key in ``secrets.toml`` (owner-only, `config.secrets`, under
      ``[models]`` by the new entry's id, which is where `secrets.model_key` looks for an
      entry with neither ``api_key_env`` nor ``api_key_file``). It then appends the entry to
      ``models.toml`` without changing a byte of what was there (`append_entry`) and reads
      the catalog again. The key never appears in an answer, a log line or an error.
    - A price is written only from `provider.connect`'s curated table; otherwise none.
    """

    def __init__(self, catalog, served, settings, *, welcome_path=None, models_path=None,
                 bench=None, client=None) -> None:
        from cogito.provider.local import benchlock

        self._catalog = catalog
        self._served = served
        self._settings = settings
        self._welcome_path = welcome_path
        self._models_path = models_path
        self._bench = bench or benchlock.holder
        self._client = client
        self._save_lock = threading.Lock()

    # -- the welcome -----------------------------------------------------------------------

    def welcome(self) -> dict:
        from cogito.config import welcome

        state = welcome.load(self._welcome_path)
        entry = self._served.entry
        return {
            **state.as_dict(),
            "model": self._served.describe(entry) if entry is not None else None,
            # A model the window can go on with: served, and nothing keeping it from use.
            "working": entry is not None and _missing(entry) is None,
            # Why the model `serve` started with is not served (`_first_model`), or None.
            "unserved": getattr(self._served, "unserved", None),
        }

    def set_welcome(self, values) -> dict:
        from cogito.config import welcome
        from cogito.errors import ConfigError

        unknown = sorted(set(values) - {"name", "done"})
        if unknown:
            return {"error": f"unknown key(s) {', '.join(unknown)}"}
        state = welcome.load(self._welcome_path)
        name, done = state.name, state.done
        if "name" in values:
            try:
                name = welcome.clean_name(values["name"])
            except ConfigError as exc:
                return {"error": str(exc)}
        if "done" in values:
            if not isinstance(values["done"], bool):
                return {"error": "done must be true or false"}
            done = values["done"]
        welcome.save(welcome.Welcome(name=name, done=done), self._welcome_path)
        return self.welcome()

    # -- the services ----------------------------------------------------------------------

    def providers(self) -> dict:
        from cogito.provider import connect

        return {
            "providers": connect.providers_view(),
            "key_pages": {p.id: p.key_page for p in connect.PROVIDERS if p.key_page},
        }

    @staticmethod
    def _failed(exc) -> dict:
        return {"ok": False, "failure": exc.failure.as_dict()}

    def api_models(self, body) -> dict:
        from cogito.provider import connect

        try:
            service = connect.provider(str(body.get("provider") or ""))
        except CogitoError:
            return {"error": "unknown service"}
        key = body.get("key")
        if key is not None and not isinstance(key, str):
            return {"error": "key must be text"}
        try:
            listed = connect.list_models(
                service, key, base_url=body.get("base_url"), client=self._client
            )
        except connect.ConnectFailed as exc:
            return self._failed(exc)
        return {"ok": True, "endpoint": listed.endpoint, "models": [dict(m) for m in listed.models]}

    def motoris_install(self, body) -> dict:
        from cogito.provider import connect

        checkout, config = self._text(body, "checkout", 1000), self._text(body, "config", 1000)
        if checkout is None or config is None:
            return {"error": "checkout and config are required"}
        try:
            found = connect.check_motoris_install(checkout, config)
        except connect.ConnectFailed as exc:
            return self._failed(exc)
        return {"ok": True, **found}

    def _own_motoris(self, url: str) -> bool:
        """True when `url` is the address of a Cogito Motoris this core starts: a `serve`
        entry of the catalog, or the server it runs now. Saving it again as «Ya en marcha»
        would add a second entry whose switch stops the first one's server."""
        addresses = [entry.base_url for entry in self._catalog.entries if entry.is_served_by_cogito]
        server = getattr(self._served, "_server", None)
        entry = getattr(self._served, "_entry", None)
        if server is not None and not getattr(server, "adopted", False) and entry is not None:
            addresses.append(entry.base_url)
        return any(_same_address(url, address) for address in addresses)

    @staticmethod
    def _own_failure(url: str):
        from urllib.parse import urlparse

        from cogito.provider import connect

        return connect.ConnectFailed(
            connect.Failure(
                "own_server",
                host=urlparse(url).hostname or "",
                message="Ese Cogito Motoris lo arranca Cogito; ya está en la lista.",
            )
        )

    def motoris_running(self, body) -> dict:
        from cogito.provider import connect

        url = self._text(body, "url", 1000) or connect.MOTORIS_URL
        key = body.get("key")
        if key is not None and not isinstance(key, str):
            return {"error": "key must be text"}
        try:
            if self._own_motoris(url):
                raise self._own_failure(url)
            models = connect.check_motoris_running(url, key, client=self._client)
        except connect.ConnectFailed as exc:
            return self._failed(exc)
        return {"ok": True, "models": models}

    # -- saving ----------------------------------------------------------------------------

    @staticmethod
    def _text(body, key: str, limit: int = 200) -> str | None:
        value = body.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return None
        if any(ord(char) < 0x20 for char in value):
            return None
        return value.strip()

    def save(self, body) -> dict:
        """Add the entry the window tested; ``{"model"}`` or ``{"error"}``."""
        from cogito.config import models as catalog_config
        from cogito.errors import ConfigError
        from cogito.provider import connect

        kind = body.get("kind")
        model = self._text(body, "model")
        if kind not in ("api", "motoris") or model is None:
            return {"error": "kind and model are required"}
        label = self._text(body, "label", 120) or model
        today = datetime.now().astimezone().date().isoformat()
        key: str | None = None
        price = None
        if kind == "motoris":
            checkout, config = self._text(body, "checkout", 1000), self._text(body, "config", 1000)
            if checkout is None or config is None:
                return {"error": "checkout and config are required"}
            args: dict[str, object] = {"motoris": checkout, "config": config}
            ctx = body.get("ctx")
            if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
                args["ctx"] = ctx
            id_base = model
            fields = {
                "label": label,
                "api": "openai",
                "base_url": connect.MOTORIS_URL,
                "model": model,
                "serve": "motoris",
                "args": args,
            }
            comment = f"Added by COGITO's connection screen on {today}."
        elif body.get("provider") == "motoris":
            # Cogito Motoris on another machine: an OpenAI-compatible host reached with a key.
            key = body.get("key") if isinstance(body.get("key"), str) else None
            key = key.strip() if key else None
            try:
                base_url = connect.motoris_url(str(body.get("url") or ""), with_key=key is not None)
            except connect.ConnectFailed:
                return {"error": "url is not a usable address"}
            if key is None and not connect.is_local_url(base_url):
                return {"error": "a key is required"}
            if self._own_motoris(base_url):
                return {"error": "own_server"}
            id_base = f"motoris-{model}"
            fields = {
                "label": label,
                "api": "openai",
                "base_url": base_url,
                "model": model,
                "price": None,
            }
            comment = f"Added by COGITO's connection screen on {today}."
            if key is not None:
                comment += "\nIts key is in secrets.toml, [models] under this entry's id."
        else:
            try:
                service = connect.provider(str(body.get("provider") or ""))
            except CogitoError:
                return {"error": "unknown service"}
            key = body.get("key") if isinstance(body.get("key"), str) else None
            key = key.strip() if key else None
            if service.custom_url:
                try:
                    base_url = connect.check_base_url(
                        str(body.get("base_url") or ""), with_key=key is not None
                    )
                except connect.ConnectFailed:
                    return {"error": "base_url is not a usable address"}
                # A service off this machine with no key could never be used (`_missing`).
                if key is None and not connect.is_local_url(base_url):
                    return {"error": "a key is required"}
            else:
                base_url = service.base_url
                if key is None:
                    return {"error": "a key is required"}
            price = connect.curated_price(service.id, model)
            id_base = f"{service.id}-{model}"
            fields = {
                "label": label,
                "api": service.api,
                "base_url": base_url,
                "model": model,
                "price": connect.price_fields(price),
            }
            comment = f"Added by COGITO's connection screen on {today}."
            if key is not None:
                comment += "\nIts key is in secrets.toml, [models] under this entry's id."
            if connect.is_local_url(base_url):
                window = connect.local_context(base_url, model, client=self._client)
                if window is not None:
                    fields["args"] = {"ctx": window}
        # One save at a time: the id is chosen from models.toml as it is now, never from an
        # earlier catalog read, and never a name a stored key already has. The key is stored
        # only once its entry exists, and never over another value.
        with self._save_lock:
            try:
                reserved = secrets.names("models")
                entry = catalog_config.append_entry(
                    fields, comment=comment, path=self._models_path, id_base=id_base,
                    reserved=reserved,
                )
            except (ConfigError, OSError) as exc:
                return {"error": f"models.toml was not changed: {exc}"}
            if key is not None:
                try:
                    secrets.set_("models", entry.id, key, replace=False)
                except (ConfigError, OSError) as exc:
                    self._catalog.reload()
                    return {
                        "error": f"{entry.id} was added to models.toml, but its key could not "
                        f"be stored: {exc}"
                    }
        self._catalog.reload()
        return {"model": self._served.describe(entry)}


def _serve_factory(
    provider,
    settings,
    entry,
    approvals,
    components=None,
    questions=None,
    *,
    ledger=None,
    service="",
    model_host=None,
    budget=None,
    broadcast=None,
    refresh=None,
):
    """Build a per-session factory for the loopback API.

    Returns a factory that ``create_app`` calls for each new session, matching
    ``server.app.EngineFactory``: ``(project_root, emit, cancel) -> EngineLike``. Phase
    events stream back to the client over ``emit``, and the session's ``cancel`` token goes
    straight into ``_build_engine``, so the ``/interrupt`` route (which cancels that token)
    stops a tool in flight and not only the bookkeeping around it.

    Destructive actions are asked in the window (`_window_approver`), not refused. A
    loopback connection has no terminal, but refusing on its behalf would leave the desktop
    app as the one surface that could never be given permission for anything.

    With a `ledger`, each session's provider is a `provider.usage.MeteredProvider`. It
    forwards every call unchanged and only observes usage, timing and failures, for the
    tray (`GET /usage`) and the `service_error` event (`service` is the label it carries).

    With ``components`` (``setup.service.ComponentService``) and ``questions``, each session
    also gets component catalog access in ``ToolContext.capabilities``, and the project scan
    starts in the background as soon as the session exists. A missing compiler reaches the
    window as ``program_missing``, never as text sent to a model.

    With a ``model_host`` (`_ServedModel`), each session is built on the model it serves
    *now* (``provider``, ``entry`` and ``service`` are only the first one's) and follows it
    when the window's picker switches. The session itself is built the same way.

    With a ``budget`` (`provider.budget.BudgetKeeper`), each metered session prices what an
    API model spends against COGITO's budget and asks it before every request to the model.
    The pause at 100 % waits on ``questions`` in the amber phase, and a local model is never
    paused. ``broadcast`` carries what belongs to every session (the budget's status, the
    per-minute headroom). Without a budget, a session is unchanged.
    """

    # Opening a project continues its conversation (`_build_engine`'s ``conversations``).
    conversations = _OpenConversations()

    def factory(project_root: Path, emit, cancel):
        if model_host is None:
            return build(provider, entry, service, project_root, emit, cancel)
        # A switch never lands between reading the served model and following it.
        with model_host.lock:
            engine_session = build(*model_host.current(), project_root, emit, cancel)
            model_host.attach(engine_session)
            return engine_session

    def build(provider, entry, service, project_root: Path, emit, cancel):
        # A full graph (`[estella] mode` full, or auto with a large model) needs the graph
        # model; the app adds it in the background, and the session runs lite until it is in.
        if components is not None and _graph_model_wanted(entry, settings, components):
            components.install("estella-full")
        from cogito.server.events import StreamEvents

        served = meter = provider
        if ledger is not None:
            from cogito.provider.usage import MeteredProvider

            served = meter = MeteredProvider(
                provider,
                ledger,
                model_id=entry.id,
                service=service,
                entry=entry,
                emit=emit,
                broadcast=broadcast,
                budget=budget,
            )
        make, built = (
            _session_capabilities(components, questions, project_root, emit, cancel)
            if components is not None
            else (None, {})
        )
        holder: dict[str, EngineSession] = {}
        # APP ONLY: the composer's automatic mode, off until the window turns it on
        # (`POST /sessions/{id}/auto`).
        from cogito.guard.auto import AutoMode

        auto = AutoMode(project_root)
        spend_gate = (
            _spend_gate(budget, holder, entry, emit, cancel)
            if budget is not None and ledger is not None
            else None
        )
        engine_session = _build_engine(
            served,
            project_root,
            # `serve` passes `_with_fresh_extensions`: `[mcp]` and `[skills]` as the file
            # says now. A caller that hands its own settings gets exactly those.
            refresh(settings) if refresh is not None else settings,
            entry,
            disabled_tools=(),
            approver=_window_approver(approvals, emit, cancel, auto),
            on_phase=lambda phase: emit({"type": "phase", "phase": str(phase)}),
            on_context_status=lambda status: emit({"type": "context_status", **status}),
            on_tool_start=lambda ev: emit({"type": "tool_start", **ev.as_event()}),
            on_tool_end=lambda ev: emit({"type": "tool_end", **ev.as_event()}),
            on_steer_applied=lambda ev: emit({"type": "steer_applied", **ev.as_event()}),
            on_user_message=lambda ev: emit({"type": "point_recorded", **ev.as_event()}),
            on_stream=StreamEvents(emit),
            on_compacted=lambda _session: emit({"type": "compacted"}),
            # APP-ONLY (long-output fix: auto-continue): the quiet «Continuando…» line.
            on_auto_continue=lambda kind: emit({"type": "auto_continuing", "kind": kind}),
            on_background_exit=lambda event: emit({"type": "background_finished", **event}),
            on_terminal_event=emit,
            on_browser_command=emit,
            cancel=cancel,
            capabilities_factory=make,
            spend_gate=spend_gate,
            conversations=conversations,
        )
        holder["session"] = engine_session
        engine_session.auto = auto
        # A session-only tool the mode allowed stays allowed while the mode is on.
        guard = getattr(engine_session, "_guard", None)
        if guard is not None:
            guard.auto_on = lambda: auto.on
        if meter is not provider:
            engine_session.attach_meter(meter)
        if "capabilities" in built:
            built["capabilities"].start_scan()
        return engine_session

    return factory


@main.command()
@click.option("--model", "model_id", default=None, help="Model id from the catalog.")
@click.option(
    "--once",
    multiple=True,
    help=(
        "Run one turn and exit. Repeat it to run several turns in the same session, "
        "which is the only way to exercise anything that spans turns. Used by benchmarks."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="With --once, print the result as JSON on stdout. Everything else goes to stderr.",
)
@click.option("--root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option(
    "--edit-format",
    type=click.Choice(["hashline", "str_replace", "whole_file"]),
    default=None,
    help="The edit tool's wire format when no pin or profile chooses one.",
)
@click.option("--max-steps", type=int, default=None, help="Override the step budget.")
@click.option(
    "--seed",
    type=click.IntRange(min=0, max=4_294_967_295),
    default=None,
    help="Fix the per-request sampling seed and record it for reproducible measurements.",
)
@click.option(
    "--prompt-time",
    default=None,
    help="Replace the prompt clock with one timezone-aware ISO-8601 value.",
)
@click.option(
    "--compact-at",
    type=float,
    default=None,
    help=(
        "Fraction of the context window at which the conversation is folded; a profile "
        "scales it to the model's effective context. Lowering it is how a benchmark "
        "reaches compaction without paying for a full window."
    ),
)
@click.option(
    "--disable-tool",
    "disabled_tools",
    multiple=True,
    help=(
        "Do not register this tool. The other half of an A/B: the same suite with and "
        "without one tool is what says whether that tool earns its place."
    ),
)
@click.option(
    "--allow-destructive",
    is_flag=True,
    help="Approve deletions automatically. Only for disposable environments.",
)
@click.option(
    "--sandbox",
    is_flag=True,
    help=(
        "Run the agent's shell under bubblewrap: read-only filesystem, the project "
        "writable, no network. For benchmarks; the interactive shell is never wrapped."
    ),
)
@click.option(
    "--sandbox-hide",
    "sandbox_hide",
    multiple=True,
    type=click.Path(file_okay=False),
    help="With --sandbox, a directory that appears empty inside. Repeatable.",
)
def chat(
    model_id: str | None,
    once: tuple[str, ...],
    as_json: bool,
    root: str,
    edit_format: str | None,
    max_steps: int | None,
    seed: int | None,
    prompt_time: str | None,
    compact_at: float | None,
    disabled_tools: tuple[str, ...],
    allow_destructive: bool,
    sandbox: bool,
    sandbox_hide: tuple[str, ...],
) -> None:
    """Talk to the agent in this project."""
    import json
    import sys
    import time
    from dataclasses import replace as replace_dataclass

    from cogito.cancel import CancelToken
    from cogito.guard.approval import deny_all
    from cogito.loop.state import RunState

    if sandbox_hide and not sandbox:
        # Hiding a directory from a shell that is not sandboxed hides nothing, and a run
        # that believes it did is worse than a refusal.
        raise click.UsageError("--sandbox-hide only takes effect with --sandbox")
    if sandbox:
        from cogito.guard import sandbox as sandbox_module

        usable, why = sandbox_module.available()
        if not usable:
            raise click.ClickException(f"--sandbox cannot run here: {why}")

    project_root = Path(root).resolve()
    fixed_prompt_time = _parse_prompt_time(prompt_time)
    settings = settings_config.load()
    if edit_format:
        settings = replace_dataclass(settings, edit_format=edit_format)
    if max_steps:
        settings = replace_dataclass(
            settings, budgets=replace_dataclass(settings.budgets, max_steps=max_steps)
        )
    if compact_at is not None:
        settings = replace_dataclass(
            settings, budgets=replace_dataclass(settings.budgets, compact_at=compact_at)
        )
    catalog = model_config.load()
    chosen = model_id or settings.default_model_id
    entry = catalog.get(chosen) if chosen else (catalog.entries[0] if catalog.entries else None)
    if entry is None:
        raise click.ClickException("no models registered — run: cogito models init")
    provider, server = _build_provider(entry, settings, quiet=as_json)

    # Who approves a destructive command: the terminal if there is a live user to ask,
    # else --allow-destructive, else deny_all. _build_engine wires this into the guard,
    # the only thing between the model and `rm`.
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive and not allow_destructive:
        click.echo(
            "! not a terminal: destructive commands will be refused, since there is no one "
            "to ask. Pass --allow-destructive to run unattended.",
            err=True,
        )

    # One token for the whole session, reset between turns. The same object backs
    # tool-execution cancellation (in _build_engine's ToolContext) and the sampling
    # loop's cancel check.
    cancel = CancelToken()
    engine_session = _build_engine(
        provider,
        project_root,
        settings,
        entry,
        disabled_tools=disabled_tools,
        approver=(
            _terminal_approver
            if interactive
            else (_allow_unattended if allow_destructive else deny_all)
        ),
        on_phase=lambda phase: click.echo(f"  [{phase}]", err=True),
        cancel=cancel,
        sampling_seed=seed,
        prompt_time=fixed_prompt_time,
        sandbox_hidden=(
            tuple(Path(p) for p in sandbox_hide) if sandbox else None
        ),
    )

    def turn(text: str, index: int = 0) -> None:
        cancel.reset()
        started = time.monotonic()
        result = engine_session.run(text, RunState(cancel=cancel))
        elapsed_ms = int((time.monotonic() - started) * 1000)

        if as_json:
            # stdout carries the measurement and nothing else, so a runner can parse it
            # without stripping progress output.
            click.echo(
                json.dumps(
                    {
                        # The engine's session, not the one opened at start-up: compaction
                        # moves it, and a benchmark recording the old id would look up a
                        # session the rest of the turn did not happen in.
                        "session_id": engine_session.session_id,
                        "turn": index,
                        "project_id": engine_session.project_id,
                        "model_id": entry.id,
                        "sampling_seed": _sampling_for(entry, seed=seed).seed,
                        "prompt_time": (
                            fixed_prompt_time.isoformat(timespec="seconds")
                            if fixed_prompt_time is not None
                            else None
                        ),
                        "edit_format": settings.edit_format,
                        "stop_reason": str(result.stop_reason),
                        "steps": result.steps,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "total_tokens": result.total_tokens,
                        "wall_ms": elapsed_ms,
                        "compactions": result.compactions,
                        "repairs": dict(result.repairs),
                        "final_text": result.final_text,
                        "pending_events": engine_session.pending_count,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            click.echo(f"\n{result.final_text}\n")
        click.echo(
            f"  {result.stop_reason} · {result.steps} steps · "
            f"{result.total_tokens:,} tokens · repairs {result.repairs or '{}'}"
            + (
                f" · {engine_session.pending_count} events not yet in the graph"
                if engine_session.degraded
                else ""
            ),
            err=True,
        )

    try:
        if once:
            # Several turns share one session on purpose: compaction, lineage and recall
            # only exist between turns, so a one-turn harness cannot measure them.
            for index, text in enumerate(once):
                turn(text, index)
        else:
            click.echo(
                f"{entry.label} · project {project_root.name} · "
                f"{settings.edit_format} edits · Ctrl-D to leave\n"
            )
            while True:
                try:
                    text = click.prompt("›", prompt_suffix=" ")
                except (click.Abort, EOFError):
                    break
                if text.strip():
                    turn(text)
    finally:
        if server is not None:
            server.stop()
        # Closes the engine's session, not the one opened above: compaction may have moved
        # it, and closing the parent again would leave the live child open forever.
        engine_session.close()


@main.command()
@click.option("--limit", default=20, help="How many entries to show.")
@click.argument("key", required=False)
@click.option("--overwrite", is_flag=True, help="Replace whatever is at the path now.")
def undo(limit: int, key: str | None, overwrite: bool) -> None:
    """List recoverable deletions, or restore one by key."""
    from cogito.graph.driver import GraphDriver
    from cogito.guard import undo as undo_module

    settings = settings_config.load()
    driver = GraphDriver(settings.neo4j, secrets.get("neo4j", "password"))
    if not driver.is_available():
        raise click.ClickException(
            f"the graph is unreachable at {settings.neo4j.uri}; run `cogito doctor`"
        )

    entries = undo_module.recent(driver, limit=limit)
    if key is None:
        if not entries:
            click.echo("Nothing to undo.")
            return
        for entry in entries:
            if entry.kind == "create":
                # Nothing was saved: undoing a create sends the file to the trash.
                state = "undo removes it"
            elif entry.saved_path is not None and entry.saved_path.exists():
                state = "recoverable"
            else:
                state = "gone from the trash"
            click.echo(f"  {entry.key}  {entry.kind:<9} {entry.original_path}  ({state})")
        click.echo("\nRestore one with: cogito undo <key>")
        return

    match = next((e for e in entries if e.key == key or e.key.startswith(key)), None)
    if match is None:
        raise click.ClickException(f"no undo entry starting with {key!r}")

    from cogito.graph.driver import Store
    from cogito.graph.wal import Journal

    # Recorded against the session that made the entry, in that project's record, so the
    # restoration and whatever it preserves reach the graph like any other.
    found = undo_module.owner(driver, match.key)
    session_id, project_root = found or ("cli", match.original_path.parent)
    journal = paths.undo_journal()
    paths.ensure(journal.parent)
    recorder = Store(Journal(journal), driver)
    store = undo_module.UndoStore(project_root, session_id, recorder.record)
    store.adopt(match)
    try:
        result = store.restore(match.key, overwrite=overwrite)
    except undo_module.UndoError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Restored {result.path}")
    preserved = next((e for e in store.entries if e.key == result.preserved_key), None)
    if preserved is not None and preserved.kind != "create":
        # A `create` only says nothing was there: there is nothing to get back.
        click.echo(
            f"  (what was there before is itself recoverable: cogito undo {preserved.key})"
        )


@main.group()
def graph() -> None:
    """Inspect and repair the context store."""


@graph.command("status")
@click.option("--root", type=click.Path(exists=True, file_okay=False), default=".")
def graph_status(root: str) -> None:
    """Show what is journalled on disk but not yet in the graph."""
    from pathlib import Path

    from cogito.graph.wal import Journal

    project_root = Path(root).resolve()
    directory = paths.project_dir(project_root) / "wal"
    journals = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    if not journals:
        click.echo(f"No journals under {directory}")
        return
    total = 0
    for path in journals:
        pending = Journal(path).pending_count()
        total += pending
        click.echo(f"  {path.stem}  {pending} pending")
    click.echo(f"\n{total} event(s) not yet in the graph.")


@graph.command("up")
@click.option("--wait", "wait_seconds", default=90.0, show_default=True, help="Seconds to wait for Bolt.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON line: state and detail.")
def graph_up(wait_seconds: float, as_json: bool) -> None:
    """Start the managed Neo4j if it is installed and not running, and wait for Bolt.

    Only the desktop app runs this, when it starts: nothing restarts Neo4j after a reboot.
    `chat` and `serve` never start it, so a benchmark run is unchanged. Exits 0 when the
    graph answers (``up``, ``started``), 1 otherwise (``absent``, ``unmanaged``, ``failed``).
    """
    import json

    from cogito.graph.driver import GraphDriver
    from cogito.setup import neo4j as provisioning

    settings = settings_config.load()
    driver = GraphDriver(settings.neo4j, secrets.get("neo4j", "password"))
    try:
        result = provisioning.ensure_running(
            provisioning.detect(),
            # The port first: while the JVM boots a driver per poll would only fail slower.
            ready=lambda: provisioning.bolt_port_open(settings.neo4j.uri) and driver.is_available(),
            wait_seconds=wait_seconds,
        )
    finally:
        driver.close()
    if as_json:
        click.echo(json.dumps({"state": result.state, "detail": result.detail}, ensure_ascii=False))
    else:
        click.echo(f"{result.state}{': ' + result.detail if result.detail else ''}")
    if not result.ok:
        raise SystemExit(1)


@graph.command("rebuild")
@click.option("--root", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--all", "replay_all", is_flag=True, help="Replay every entry, not just pending.")
def graph_rebuild(root: str, replay_all: bool) -> None:
    """Project journalled events into the graph.

    Projections are idempotent, so replaying is safe whether the graph is empty, partial,
    or already complete.
    """
    from pathlib import Path

    from cogito.graph.driver import GraphDriver, Store
    from cogito.graph.wal import Journal

    project_root = Path(root).resolve()
    settings = settings_config.load()
    driver = GraphDriver(settings.neo4j, secrets.get("neo4j", "password"))
    if not driver.is_available():
        raise click.ClickException(
            f"the graph is unreachable at {settings.neo4j.uri}; run `cogito doctor`"
        )
    driver.ensure_schema()

    directory = paths.project_dir(project_root) / "wal"
    journals = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    if not journals:
        click.echo(f"No journals under {directory}")
        return

    total = 0
    for path in journals:
        store = Store(Journal(path), driver)
        projected = store.rebuild() if replay_all else store.flush()
        total += projected
        click.echo(f"  {path.stem}  {projected} projected")
    click.echo(f"\n{total} event(s) now in the graph.")
    driver.close()


@main.command()
@click.option("--model", "model_id", default=None, help="Model id from the catalog.")
@click.option("--port", default=8765, help="Port to listen on.")
@click.option("--host", default="127.0.0.1", help="Host to bind to (only 127.0.0.1 allowed).")
def serve(model_id: str | None, port: int, host: str) -> None:
    """Start the loopback API server."""
    import socket

    if host != "127.0.0.1":
        raise click.ClickException("only --host 127.0.0.1 is allowed")

    settings = settings_config.load()
    # Read again whenever the window adds a model (design 1e, `_Connect`), so everything
    # below that is handed the catalog sees the new entry.
    catalog = model_config.LiveCatalog()
    # `chat`'s choice (--model, else the configured default, else the first entry) with one
    # step before the default: the model last chosen in the window's picker.
    entry = _initial_model(catalog, settings, model_id)

    # Bind before anything else and before the ready line, which both clients (the desktop's
    # main process, the lab's suite holder) read as "listening". When the line came first, a
    # port held by another process was still announced: uvicorn failed to bind, and on exit
    # this process stopped the llama-server `_build_provider` had just started. Every
    # `cogito chat` the suite holder spawned then raced to start its own model server, and
    # the winner stopped it under the others' requests when it finished (cogito-lab, 2x RTX
    # 4090 runs of 2026-10-07 after 23:09 UTC: `provider_failure` in 13-53 sessions per
    # run). Bound first, a taken port is an error before any model loads.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn's own bind
    try:
        listener.bind((host, port))
        listener.listen()
    except OSError as exc:
        listener.close()
        raise click.ClickException(
            f"cannot listen on {host}:{port}: {exc.strerror or exc}. Another process holds "
            f"that port; stop it, or pass --port with a free one."
        ) from exc
    port = listener.getsockname()[1]

    try:
        _serve_on(listener, entry, settings, catalog, host=host, port=port)
    finally:
        listener.close()


def _serve_on(listener, entry, settings, catalog, *, host: str, port: int) -> None:
    """`serve`, from the bound `listener` on: serve a model or none, announce, serve."""
    import functools
    import json
    import secrets as stdlib_secrets
    import sys

    import uvicorn

    from cogito.config import last_model
    from cogito.context import attachments as attachment_reader
    from cogito.graph.driver import GraphDriver
    from cogito.provider import gpu, service
    from cogito.provider.local import benchlock
    from cogito.server.app import create_app
    from cogito.server.approvals import Approvals
    from cogito.server.questions import Questions
    from cogito.setup import searxng
    from cogito.setup.service import ComponentService
    from cogito.tools import document_cache

    # No model at all is a first launch, not an error: the core serves the window's welcome
    # and connection screen (design 1d-1e) until one is connected, and a session requested
    # before then is refused (`NoModel`). A model that cannot be served now (a local one
    # while cogito-lab's benchmark holds the GPU, or one that fails to start) is handled the
    # same way: the core starts without it and the window says why (`unserved`).
    entry, provider, _server_process, unserved = _first_model(
        entry, settings, bench=benchlock.holder
    )
    from cogito.provider.budget import BudgetKeeper
    from cogito.provider.usage import UsageLedger, snapshot

    # What the tray shows (design 2b): the day's tokens, tokens/s, GPU and a usage limit,
    # counted through a meter that forwards every call unchanged.
    ledger = UsageLedger()
    # What belongs to every session (the budget's status, the per-minute headroom) goes to
    # every session's stream once the app that holds them exists.
    fanout: dict[str, object] = {}

    def broadcast(event: dict) -> None:
        target = fanout.get("emit")
        if callable(target):
            target(event)

    # Where permission requests and budget pauses wait for the window, one for the whole
    # core: the pause is asked by the budget below, and answered by the app's route.
    questions = Questions()
    # The model served from now on, which the window's picker switches; every session, the
    # probes and the tray follow it.
    served = _ServedModel(
        entry,
        settings,
        catalog,
        provider=provider,
        server=_server_process,
        ledger=ledger,
        remember=last_model.save,
        broadcast=broadcast,
        bench=benchlock.holder,
        # APP ONLY: a crashed Cogito Motoris is started again and the request asked again.
        revive=_app_owned(),
    )
    served.unserved = unserved
    # COGITO's own budget (design 2f): config.toml's [budget], counted by this ledger.
    budget = BudgetKeeper(
        ledger,
        questions=questions,
        broadcast=broadcast,
        active=lambda: served.entry,
        currencies=lambda: (e.price.currency for e in catalog.entries if e.price is not None),
    )
    served.budget = budget
    service_info = service.describe(entry) if entry is not None else None
    gpu_meter = gpu.GpuMeter()

    def usage():
        if served.entry is None:
            return {"date": None, "active": None, "today": {}}
        return snapshot(
            served.entry,
            ledger,
            service=service.describe(served.entry),
            gpu=gpu_meter,
            budget=budget,
        )

    token = stdlib_secrets.token_urlsafe(32)
    # One registry for both ends: the approver the factory builds waits on it, and the
    # approvals route answers on it. Two instances would leave every turn blocked on a
    # question nothing could answer.
    approvals = Approvals()
    # Same reasoning for permission requests (`questions`, above), and one component
    # service for the whole core: installs, "Permitir en esta sesión" and the resolver's
    # state are application-wide, and every session's scan and resolver read the same one.
    components = ComponentService(
        unused=_component_unused(settings_config.load),
        external=_component_external(settings_config.load),
    )
    # The SearXNG that gives `web_search` the open web (`setup/searxng.py`): provisioned on
    # first use, started in the background so the ready line never waits for it, and stopped
    # below. Only a core the desktop app owns builds one: `chat` never does, and neither does
    # the `serve` the lab holds a model with for a suite.
    local_search = None
    if _local_search_wanted():
        local_search = searxng.SearxngService(
            components, on_state=lambda event: broadcast(event)
        )
        searxng.set_current(local_search)
        local_search.start_background()
    # The Claude API's newest models (`provider/claude_models.py`): asked once per start, in
    # the background, with the key a Claude entry already uses; any it adds reach the picker
    # through the catalog's reload. Only a core the desktop app started does this: `chat`,
    # the lab's suite holder and every benchmark never touch models.toml.
    if os.environ.get("COGITO_PARENT_PID", "").strip().isdigit():
        def _sync_claude_models() -> None:
            from cogito.provider import claude_models

            if claude_models.sync(catalog.entries):
                catalog.reload()

        threading.Thread(
            target=_sync_claude_models, name="cogito-claude-models", daemon=True
        ).start()
    # Cogito Estella for a person who has none (`setup/estella.py`): installed in the
    # background on the first start, so `ask`, page passages and `ask_document` work from
    # the next session on. Same owner rule as the SearXNG above.
    if _local_search_wanted() and _estella_install_wanted(settings, components):
        components.install("estella")
    # A driver separate from the one each session's `_build_engine` opens: `/projects` is
    # answered before any session exists, and stays answerable (degraded, not broken) if
    # Neo4j is down for the whole run.
    driver = GraphDriver(settings.neo4j, secrets.get("neo4j", "password"))
    # The desktop app's project picker (design 1f): config-dir state, not the graph's (see
    # `_RecentProjects` for why). `POST /sessions` touches it.
    recent_projects = _RecentProjects()
    from cogito.tools.mcp.connections import Connections

    # APP ONLY: adding, retrying or switching on a server answers at once with
    # «starting», and the window reads the list again until the start ends; the lab's
    # suite holder keeps the answer that waits for the start.
    connections = Connections(
        _mcp_pool(), lambda: settings_config.load().mcp, start_wait=0.0 if _app_owned() else None
    )
    skills_lister = _Skills(lambda: settings_config.load())
    app = create_app(
        _serve_factory(
            provider,
            settings,
            entry,
            approvals,
            components,
            questions,
            ledger=ledger,
            service=service_info.label if service_info is not None else "",
            model_host=served,
            budget=budget,
            broadcast=broadcast,
            refresh=_with_fresh_extensions,
        ),
        token,
        approvals,
        driver=driver,
        catalog=catalog,
        connections=connections,
        skills=skills_lister,
        usage=usage,
        components=components,
        questions=questions,
        models=served,
        budget=budget,
        recent_projects=recent_projects,
        connect=_Connect(catalog, served, settings),
        # Files and pastes the person attaches to a message (design 1n): read, never written;
        # a document as its text, by the converter `read` uses too.
        attachments=functools.partial(attachment_reader.load, convert=document_cache.extract),
    )
    fanout["emit"] = app.state.broadcast

    # The label as well as the port: the window shows which model is answering, and asking
    # over HTTP afterwards would cost a round trip for something already known.
    label = entry.display_label if entry is not None else ""
    print(json.dumps({"port": port, "token": token, "model": label}))
    sys.stdout.flush()

    # The desktop app passes its own PID: a core must not outlive the window that owns it.
    # A core left behind by an app that died keeps its port, and every later start fails
    # to bind (seen in the real app, 2026-09-29).
    parent = os.environ.get("COGITO_PARENT_PID", "").strip()
    if parent.isdigit():
        watch_parent(int(parent))

    # What a core that died (crashed, or killed after its grace period) left running in its
    # agent shells: a server, a model, anything holding memory or the GPU.
    import logging

    from cogito.tools.shell import reap_orphans

    if reaped := reap_orphans(paths.shells_dir()):
        logging.getLogger(__name__).info(
            "stopped %d shell(s) a previous core left running", reaped
        )

    try:
        # `uvicorn.run(app, host=, port=)` less its own bind, which `serve` has done.
        # A short graceful window: the desktop waits for this process to exit and kills its
        # group after its own grace period, and the sessions still have to be closed below.
        server = uvicorn.Server(
            # No access log: the app polls, so one line per request went to stdout all day.
            uvicorn.Config(
                app, host=host, port=port, timeout_graceful_shutdown=3, access_log=False
            )
        )
        # uvicorn re-raises the SIGTERM it shut down on once it has restored the handler it
        # found (uvicorn 0.53 `Server.capture_signals`). Under Python's default that kills
        # the process before the `finally` below, so a quit never closed the agent's
        # shells or stopped the model server. A handler that does nothing lets it return.
        import signal

        previous_term = signal.signal(signal.SIGTERM, lambda *_: None)
        app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
        server.run(sockets=[listener])
        if not server.started:
            sys.exit(3)  # uvicorn's STARTUP_FAILURE, as `uvicorn.run` exits with
    finally:
        # The cleanup below is what stops the agent's shells and the model server. A second
        # SIGTERM must not cut it short: in development the desktop waits on `uv`, which
        # exits at once, and then this process's parent watcher sends one (seen in the QA
        # rig, 2026-10-17: a server the agent started survived the quit). It has a bounded
        # time instead, after which the process exits whatever is left.
        finished = _finish_within(_SHUTDOWN_SECONDS)
        try:
            # Nothing resumes behind a server that is going away.
            app.state.cancel_all_resumes("quit")
            # Every session's shell, with what the agent left running in it, and its Estella.
            app.state.close_all_sessions("quit")
            if local_search is not None:
                local_search.stop()
                searxng.set_current(None)
            # No session owns this driver, so nothing else closes it when the server stops,
            # whether by a clean shutdown or by uvicorn unwinding after a signal.
            driver.close()
        finally:
            # Cleanup ended in time: nothing is left for the deadline to cut, and a caller
            # that ran `serve` in its own process (a test) keeps running with its signals.
            finished()
            with contextlib.suppress(NameError, ValueError, TypeError):
                signal.signal(signal.SIGTERM, previous_term)



#: How long `serve`'s cleanup may take once the server has stopped (`_finish_within`).
_SHUTDOWN_SECONDS = 20.0


def _finish_within(seconds: float):
    """From here on, ignore SIGTERM and SIGINT, and exit after `seconds` whatever is still
    running: shutdown work is finished, never interrupted, and never hangs. Returns the call
    that ends this once the work is done in time: the deadline is cancelled and the signals
    handled as before. Without it a `serve` run inside a test process ended that process 20 s
    later, mid-suite."""
    import signal

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):  # not the main thread
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
    timer = threading.Timer(seconds, lambda: os._exit(0))
    timer.daemon = True
    timer.start()

    def finished() -> None:
        timer.cancel()
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)

    return finished


def watch_parent(pid: int, *, interval: float = 2.0, on_gone=None) -> threading.Thread:
    """Watch `pid` from a daemon thread; once it no longer exists, call `on_gone`.

    The default shuts this process down the way a terminal's Ctrl+C would (SIGTERM, which
    uvicorn turns into a clean shutdown), so a server whose owner died unwinds its own
    `finally` blocks instead of being killed mid-write.
    """
    import signal
    import time

    def default_gone() -> None:
        if os.name == "nt":
            # `os.kill` here would be TerminateProcess, which skips every `finally`;
            # raising the signal runs uvicorn's own handler, as a console's Ctrl+C would.
            signal.raise_signal(signal.SIGTERM)
            return
        os.kill(os.getpid(), signal.SIGTERM)

    def alive() -> bool:
        if os.name == "nt":
            # `os.kill(pid, 0)` is not a liveness probe on Windows: 0 is CTRL_C_EVENT.
            from cogito import procs

            return procs.alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else
        return True

    def run() -> None:
        while alive():
            time.sleep(interval)
        (on_gone or default_gone)()

    thread = threading.Thread(target=run, name="cogito-parent-watch", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    main()
