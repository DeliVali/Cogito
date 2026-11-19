"""LADSPA effect plugins: which are installed, what their controls are, and one as an
LMMS effect.

LADSPA is the plugin format LMMS loads by itself on every platform; 1.2.2 ships a folder
of them and also looks in `LADSPA_PATH` and the system's `ladspa` folders
(src/core/LadspaManager.cpp), so one the person installs later is found too. A plugin
is a shared library whose `ladspa_descriptor(index)` returns its label, name and ports
(ladspa.h). The libraries are read in a separate Python process, one a file, so one that
crashes while loading takes nothing else with it.

In a project a plugin is `<effect name="ladspaeffect">` with a `<key>` of the library's
name and the plugin's label, and `<ladspacontrols>` holding one `<portPN data=...>` per
control, P the processor and N the port's number in the plugin. A plugin with one audio
input runs twice, once a channel, and each run has its own controls
(plugins/LadspaEffect/LadspaEffect.cpp, LadspaControls.cpp; read 2026-11-18). Bounds a
plugin gives per sample rate are multiplied by it, as LMMS does, so they read in Hz.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

#: LMMS's processing rate, which sample-rate dependent bounds are multiplied by.
RATE = 44_100
_SCAN_SECONDS = 15.0
_SHOWN = 60
#: LADSPA_PortDescriptor and LADSPA_PortRangeHintDescriptor bits (ladspa.h).
_INPUT, _CONTROL, _AUDIO = 1, 4, 8
_BELOW, _ABOVE, _TOGGLED, _PER_RATE, _LOG, _INTEGER = 1, 2, 4, 8, 0x10, 0x20
_DEFAULT_MASK = 0x3C0
_DEFAULTS = {0x200: 0.0, 0x240: 1.0, 0x280: 100.0, 0x2C0: 440.0}

#: Run with `python -I -c`: the descriptors of the libraries named, as JSON.
_SCAN = r"""
import ctypes, json, sys
class Hint(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_int), ("low", ctypes.c_float), ("high", ctypes.c_float)]
class Descriptor(ctypes.Structure):
    _fields_ = [("id", ctypes.c_ulong), ("label", ctypes.c_char_p), ("properties", ctypes.c_int),
                ("name", ctypes.c_char_p), ("maker", ctypes.c_char_p),
                ("copyright", ctypes.c_char_p), ("count", ctypes.c_ulong),
                ("ports", ctypes.POINTER(ctypes.c_int)),
                ("names", ctypes.POINTER(ctypes.c_char_p)), ("hints", ctypes.POINTER(Hint))]
found = []
for path in sys.argv[1:]:
    try:
        library = ctypes.CDLL(path)
        describe = library.ladspa_descriptor
    except (OSError, AttributeError):
        continue
    describe.restype = ctypes.POINTER(Descriptor)
    describe.argtypes = [ctypes.c_ulong]
    for index in range(4096):
        pointer = describe(index)
        if not pointer:
            break
        plugin = pointer.contents
        text = lambda raw: (raw or b"").decode("utf-8", "replace")
        ports = [[plugin.ports[n], text(plugin.names[n]), plugin.hints[n].kind,
                  plugin.hints[n].low, plugin.hints[n].high] for n in range(plugin.count)]
        found.append({"path": path, "label": text(plugin.label), "name": text(plugin.name),
                      "ports": ports})
print(json.dumps(found))
"""


class PluginError(ValueError):
    """What is wrong with a plugin effect, worded for the model."""


@dataclass(frozen=True, slots=True)
class Control:
    #: The port's number in the plugin.
    port: int
    name: str
    low: float
    high: float
    default: float
    #: "toggle", "integer" or "number".
    kind: str


@dataclass(frozen=True, slots=True)
class Plugin:
    #: The library's file name without its extension, as LMMS keys it.
    file: str
    label: str
    name: str
    #: Audio inputs: 1 runs once a channel, 2 once.
    inputs: int
    controls: tuple[Control, ...]

    @property
    def key(self) -> str:
        return f"{self.file}/{self.label}"


def folders(binary: str = "", env: Mapping[str, str] | None = None) -> list[Path]:
    """Where LMMS looks for LADSPA plugins, in its order."""
    env = os.environ if env is None else env
    places = [Path(part) for part in env.get("LADSPA_PATH", "").split(os.pathsep) if part]
    if binary:
        places.append(Path(binary).resolve().parent.parent / "lib" / "lmms" / "ladspa")
    places += [Path(folder) for folder in (
        "/usr/lib/lmms/ladspa", "/usr/lib64/lmms/ladspa", "/usr/lib/ladspa",
        "/usr/lib64/ladspa", "/usr/local/lib/ladspa", "/usr/local/lib64/ladspa",
    )]
    seen: list[Path] = []
    for folder in places:
        if folder.is_dir() and folder not in seen:
            seen.append(folder)
    return seen


def scan(places: Sequence[Path]) -> dict[str, Plugin]:
    """Every effect plugin in ``places``, by `file/label`. A library that cannot be read,
    or takes too long, is left out."""
    files: dict[str, Path] = {}
    for folder in places:
        for path in sorted(folder.iterdir()):
            if path.suffix in (".so", ".dll", ".dylib") and path.is_file():
                files.setdefault(path.stem, path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_read, files.values()))
    plugins: dict[str, Plugin] = {}
    for found in results:
        for raw in found:
            plugin = _plugin(raw)
            if plugin is not None:
                plugins.setdefault(plugin.key, plugin)
    return plugins


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        ran = subprocess.run(
            [sys.executable, "-I", "-c", _SCAN, str(path)], capture_output=True,
            stdin=subprocess.DEVNULL, timeout=_SCAN_SECONDS, check=False,
        )
        found = json.loads(ran.stdout or b"[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return found if isinstance(found, list) else []


def _plugin(raw: Mapping[str, Any]) -> Plugin | None:
    """A scanned descriptor as a plugin, or None when it is not an effect LMMS can use:
    one or two audio inputs, and an output."""
    ports = raw.get("ports") or []
    inputs = sum(1 for port in ports if port[0] & _AUDIO and port[0] & _INPUT)
    outputs = sum(1 for port in ports if port[0] & _AUDIO and not port[0] & _INPUT)
    if inputs not in (1, 2) or outputs < 1 or not raw.get("label"):
        return None
    controls = []
    for number, (kind, name, hint, low, high) in enumerate(ports):
        if not (kind & _CONTROL and kind & _INPUT):
            continue
        controls.append(_control(number, str(name), int(hint), float(low), float(high)))
    return Plugin(
        file=Path(str(raw.get("path", ""))).stem, label=str(raw["label"]),
        name=str(raw.get("name") or raw["label"]), inputs=inputs, controls=tuple(controls),
    )


def _control(port: int, name: str, hint: int, low: float, high: float) -> Control:
    """A control's range and default, by LMMS's rules for a missing bound and LADSPA's
    for the default a hint names."""
    scale = RATE if hint & _PER_RATE else 1
    low = low * scale if hint & _BELOW and math.isfinite(low) else 0.0
    high = (
        high * scale if hint & _ABOVE and math.isfinite(high)
        else (10.0 if name.upper() == "GAIN" else 1.0) * scale
    )
    if high < low:
        low, high = high, low
    wanted = hint & _DEFAULT_MASK

    def between(share: float) -> float:
        if hint & _LOG and low > 0 and high > 0:
            return math.exp(math.log(low) * (1 - share) + math.log(high) * share)
        return low * (1 - share) + high * share

    if hint & _TOGGLED:
        default = 1.0 if wanted in (0x240, 0x140) or wanted == 0 else 0.0
    elif wanted in _DEFAULTS:
        default = _DEFAULTS[wanted] * scale
    elif wanted in (0x40, 0x80, 0xC0, 0x100, 0x140):
        default = between({0x40: 0.0, 0x80: 0.25, 0xC0: 0.5, 0x100: 0.75, 0x140: 1.0}[wanted])
    else:
        default = (low + high) / 2
    kind = "toggle" if hint & _TOGGLED else "integer" if hint & _INTEGER else "number"
    if kind == "integer":
        default = float(round(default))
    default = min(high, max(low, default))
    return Control(port, name, low, high, default, kind)


def find(name: str, plugins: Mapping[str, Plugin]) -> Plugin:
    """The plugin ``name`` means: `calf/Reverb`, its label or its name."""
    wanted = _fold(name)
    if not wanted:
        raise PluginError("A `ladspa` effect needs `plugin`, e.g. `calf/Reverb`.")
    matches = [
        plugin for plugin in plugins.values()
        if wanted in (_fold(plugin.key), _fold(plugin.label), _fold(plugin.name))
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise PluginError(
            f"`{name}` names several plugins: {', '.join(p.key for p in matches)}. Use one "
            "of those."
        )
    close = difflib.get_close_matches(
        wanted, [_fold(plugin.label) for plugin in plugins.values()], n=5, cutoff=0.6
    )
    near = [plugin.key for plugin in plugins.values() if _fold(plugin.label) in close]
    raise PluginError(
        f"No LADSPA plugin is called `{name}`."
        + (f" Close: {', '.join(near)}." if near else "")
        + " Call lmms with action sounds and `ladspa` plus a word to find one."
    )


def effect(plugin: Plugin, values: Any, where: str) -> tuple[ET.Element, ET.Element]:
    """The `<ladspacontrols>` and `<key>` of ``plugin`` with ``values`` set by control
    name; the rest stay at the plugin's defaults."""
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise PluginError(f"{where}: `set` is an object of control names and values.")
    by_name = {_fold(control.name): control for control in plugin.controls}
    chosen: dict[int, float] = {}
    for name, raw in values.items():
        control = by_name.get(_fold(str(name)))
        if control is None:
            raise PluginError(
                f"{where}: {plugin.key} has no control `{name}`. Its controls: "
                + "; ".join(_described(c) for c in plugin.controls) + "."
            )
        if isinstance(raw, bool):
            raw = 1.0 if raw else 0.0
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise PluginError(f"{where}: `{name}` takes a number.") from None
        if not math.isfinite(number) or not control.low <= number <= control.high:
            raise PluginError(f"{where}: `{name}` is {_described(control)}; got {raw}.")
        chosen[control.port] = number
    runs = 2 // plugin.inputs
    controls = ET.Element("ladspacontrols", ports=str(len(plugin.controls) * runs))
    if runs > 1:
        controls.set("link", "1")
    for run in range(runs):
        for control in plugin.controls:
            port = ET.SubElement(
                controls, f"port{run}{control.port}",
                data=_text(chosen.get(control.port, control.default)),
            )
            if runs > 1 and run == 0:
                port.set("link", "1")
    key = ET.Element("key")
    ET.SubElement(key, "attribute", name="file", value=plugin.file)
    ET.SubElement(key, "attribute", name="plugin", value=plugin.label)
    return controls, key


def listing(query: str, plugins: Mapping[str, Plugin]) -> str:
    """What `sounds` says about LADSPA: how many there are, the ones matching words, or
    one plugin's controls."""
    if not plugins:
        return ""
    words = [word for word in query.lower().split() if word != "ladspa"]
    if "ladspa" not in query.lower().split():
        return (
            f"LADSPA effect plugins installed: {len(plugins)}. An effect of type `ladspa` "
            "uses one: {\"type\": \"ladspa\", \"plugin\": \"calf/Reverb\", \"set\": "
            "{CONTROL: VALUE}}. Call sounds with `ladspa` and a word (`ladspa reverb`) to "
            "find one, and with its name to see its controls."
        )
    matching = [
        plugin for plugin in plugins.values()
        if all(word in f"{plugin.key} {plugin.name}".lower() for word in words)
    ]
    one = None
    if len(matching) == 1:
        one = matching[0]
    elif words and "/" in words[0]:
        # A key names one plugin even where its words are in other plugins' names.
        one = next((p for p in plugins.values() if _fold(p.key) == _fold(" ".join(words))), None)
    if one is not None:
        return (
            f"LADSPA plugin {one.key}: {one.name}. Its controls (name: range, default):\n"
            + "\n".join(f"  {_described(control)}" for control in one.controls)
            + f"\nUse it as {{\"type\": \"ladspa\", \"plugin\": \"{one.key}\", \"set\": "
            "{...}, \"mix\": 0-1}; controls left out keep their default."
        )
    if not matching:
        return f"No LADSPA plugin matches `{' '.join(words)}`."
    more = len(matching) - _SHOWN
    return (
        f"LADSPA plugins{' matching `' + ' '.join(words) + '`' if words else ''} "
        "(key: name):\n"
        + "\n".join(f"  {plugin.key}: {plugin.name}" for plugin in matching[:_SHOWN])
        + (f"\n  and {more} more: add a word." if more > 0 else "")
        + "\nCall sounds with `ladspa` and a key to see its controls."
    )


def _described(control: Control) -> str:
    if control.kind == "toggle":
        return f"{control.name}: 0 or 1, default {control.default:g}"
    whole = " (whole)" if control.kind == "integer" else ""
    return (
        f"{control.name}: {_short(control.low)} to {_short(control.high)}{whole}, "
        f"default {_short(control.default)}"
    )


def _short(number: float) -> str:
    return f"{number:.4g}"


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9/]", "", name.lower())


def _text(number: float) -> str:
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"
