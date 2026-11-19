"""What happens to a track's sound after its instrument, and what moves over time:
effects, and automation.

Effects are LMMS's own, the ones built into 1.2.2 so they exist on every install (the
LADSPA plugins a machine has vary). An `<fxchain>` holds `<effect name=... wet=...>`
elements, each with its controls element; names and ranges are from each effect's
`*Controls.cpp` in LMMS 1.2.2 (read 2026-11-18): ReverbSC, Delay, Eq, DualFilter,
dynamics_processor, waveshaper, Bitcrush, Flanger, stereo_enhancer, BassBooster.
The compressor and the distortion are LMMS's two curve effects with the curve drawn
here: 200 output levels for 200 input levels, as base64 of 32-bit floats.

Automation is a track of type 5 holding an `<automationpattern>` of `<time pos value>`
points and the `<object id>` of what it moves. The moved value stops being an attribute
and becomes a child element carrying that id (`<vol id="..." value="100"/>`), which is
how LMMS itself saves an automated knob (`AutomatableModel::saveSettings`).
"""

from __future__ import annotations

import base64
import math
import random
import struct
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree as ET

TICKS_PER_BEAT = 48
AUTOMATION_TRACK = "5"
#: An automation track this tool wrote is named by what it moves, after this mark.
MARK = "~ "
MAX_EFFECTS = 8
MAX_POINTS = 512
_FILTERS = {
    "lowpass": 0, "highpass": 1, "bandpass": 2, "notch": 4, "moog": 6, "lowpass24": 7,
}
_CURVES = {"step": "0", "linear": "1", "smooth": "2"}

HELP = (
    "A track's `effects` is its chain, in order (and `master_effects` the whole song's); "
    "an empty list clears it. Each is {type, ...}; `mix` 0-1 is how much of the effect "
    "is heard:\n"
    "  reverb: size 0-1, tone Hz (how bright the tail is). delay: time in beats (0.75 is "
    "a dotted eighth), feedback 0-0.95. eq: lowcut Hz, highcut Hz, low dB, mid dB (at "
    "mid_freq Hz), high dB, each -18..18. filter: kind lowpass|highpass|bandpass|notch|"
    "moog|lowpass24, cutoff Hz, resonance. compressor: threshold 0-1, ratio, attack and "
    "release ms, gain. distortion: amount 0-1, drive, gain. bitcrush: rate Hz, bits. flanger: "
    "rate, depth 0-1, feedback 0-1. widen: width 0-1. bassboost: freq 50-200 Hz, gain. "
    "ladspa: an installed plugin, {plugin, set: {control: value}}.\n"
    "`automation` moves a value over the song: a list of {target, points, curve}. "
    "target: `tempo`, `master volume`, or a track's name and `volume`, `pan`, `pitch`, "
    "`cutoff`, `resonance` or one of its synth's attributes (e.g. `Pad cutoff`). points: "
    "`BEAT:VALUE` in order, in the value's own unit (Hz, bpm, 0-200 for volume). curve: "
    "linear (default), step or smooth. Empty points remove it. A filter sweep is a "
    "track with a `sound` filter and `Pad cutoff` moving `0:300 32:6000`.\n"
    'Example: "effects": [{"type": "delay", "time": 0.75, "feedback": 0.5, "mix": 0.3}, '
    '{"type": "reverb", "size": 0.9, "mix": 0.4}], "automation": [{"target": "Pad '
    'cutoff", "points": "0:300 16:2500 32:600", "curve": "smooth"}]'
)


class MixError(ValueError):
    """What is wrong with an effect or an automation, worded for the model."""


# -- effects --------------------------------------------------------------------------


def chain(raw: Any, tempo: int, where: str, plugins: Any = None) -> ET.Element:
    """An `<fxchain>` for a list of effects. ``plugins`` returns the LADSPA plugins
    installed, by `file/label`, and is called only when an effect asks for one."""
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list) or len(raw) > MAX_EFFECTS:
        raise MixError(f"{where}: `effects` is a list of at most {MAX_EFFECTS} effects.")
    made = ET.Element("fxchain", numofeffects=str(len(raw)), enabled="1" if raw else "0")
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            item = {"type": item}
        if not isinstance(item, Mapping):
            raise MixError(f"{where}: effect {index} is an object like {{\"type\": \"reverb\"}}.")
        kind = str(item.get("type") or item.get("name") or "").lower().replace(" ", "")
        if kind == "ladspa":
            made.append(_plugin_effect(item, f"{where}, ladspa", plugins))
            continue
        builder = _EFFECTS.get(kind)
        if builder is None:
            raise MixError(
                f"{where}: `{kind or '?'}` is not an effect. Effects: {', '.join(_EFFECTS)}, "
                "or `ladspa` for an installed plugin."
            )
        here = f"{where}, {kind}"
        name, controls, default_mix, used = builder(item, tempo, here)
        unknown = [key for key in item if key not in (*used, "type", "name", "mix")]
        if unknown:
            raise MixError(
                f"{here}: no field `{unknown[0]}`. It takes: {', '.join((*used, 'mix'))}."
            )
        effect = ET.SubElement(
            made, "effect", name=name, on="1",
            wet=_text(_number(item, "mix", default_mix, 0, 1, here)), gate="0", autoquit="1",
            autoquit_numerator="4", autoquit_denominator="4", syncmode="0",
        )
        effect.append(controls)
        ET.SubElement(effect, "key")
    return made


def _plugin_effect(item: Mapping[str, Any], where: str, plugins: Any) -> ET.Element:
    """An installed LADSPA plugin as an effect."""
    from . import ladspa

    installed = plugins() if callable(plugins) else (plugins or {})
    if not installed:
        raise MixError(
            f"{where}: no LADSPA plugins were found on this machine. Use the built-in "
            f"effects: {', '.join(_EFFECTS)}."
        )
    unknown = [key for key in item if key not in ("type", "name", "plugin", "set", "mix")]
    if unknown:
        raise MixError(f"{where}: no field `{unknown[0]}`. It takes: plugin, set, mix.")
    try:
        plugin = ladspa.find(str(item.get("plugin") or ""), installed)
        controls, key = ladspa.effect(plugin, item.get("set"), where)
    except ladspa.PluginError as exc:
        raise MixError(str(exc)) from None
    effect = ET.Element(
        "effect", name="ladspaeffect", on="1",
        wet=_text(_number(item, "mix", 1.0, 0, 1, where)), gate="0", autoquit="1",
        autoquit_numerator="4", autoquit_denominator="4", syncmode="0",
    )
    effect.append(controls)
    effect.append(key)
    return effect


def describe(found: ET.Element | None) -> str:
    """A chain's effects by name, for the project as the model reads it."""
    if found is None or found.get("enabled") == "0":
        return ""
    back = {name: kind for kind, name in _NAMES.items()}
    names = []
    for effect in found.findall("effect"):
        name = effect.get("name", "?")
        if name == "ladspaeffect":
            plugin = effect.find("key/attribute[@name='plugin']")
            name = f"ladspa {plugin.get('value')}" if plugin is not None else name
        names.append(back.get(name, name) + ("" if effect.get("on", "1") == "1" else " (off)"))
    return ", ".join(names)


def _reverb(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    controls = ET.Element("ReverbSCControls", {
        "input_gain": "0", "output_gain": "0",
        "size": _text(_number(item, "size", 0.85, 0, 1, where)),
        "color": _text(_number(item, "tone", 10000, 100, 15000, where)),
    })
    return "reverbsc", controls, 0.3, ("size", "tone")


def _delay(item: Mapping[str, Any], tempo: int, where: str) -> tuple:
    beats = _number(item, "time", 0.75, 0.01, 64, where)
    seconds = min(5.0, max(0.01, beats * 60 / max(1, tempo)))
    controls = ET.Element("Delay", {
        "DelayTimeSamples": _text(seconds), "DelayTimeSamples_numerator": "4",
        "DelayTimeSamples_denominator": "4", "syncmode": "0",
        "FeebackAmount": _text(_number(item, "feedback", 0.4, 0, 0.95, where)),
        "LfoFrequency": "2", "LfoFrequency_numerator": "4", "LfoFrequency_denominator": "4",
        "LfoAmount": "0", "LfoAmount_numerator": "4", "LfoAmount_denominator": "4",
        "OutGain": "0",
    })
    return "delay", controls, 0.3, ("time", "feedback")


def _eq(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    attributes = {
        "Inputgain": "0", "Outputgain": "0", "AnalyseIn": "0", "AnalyseOut": "0",
        "HPres": "0.707", "LPres": "0.707", "LowShelfres": "1.4", "HighShelfres": "1.4",
        "HP12": "1", "HP24": "0", "HP48": "0", "LP12": "1", "LP24": "0", "LP48": "0",
        "HP": "0", "LP": "0",
        "HPactive": "0", "HPfreq": "31", "LPactive": "0", "LPfreq": "18000",
        "Lowshelfactive": "0", "Lowshelfgain": "0", "LowShelffreq": "120",
        "Highshelfactive": "0", "HighShelfgain": "0", "Highshelffreq": "6000",
    }
    for band, freq in (("1", "120"), ("2", "1000"), ("3", "2000"), ("4", "4000")):
        attributes.update({
            f"Peak{band}active": "0", f"Peak{band}gain": "0", f"Peak{band}freq": freq,
            f"Peak{band}bw": "1" if band == "2" else "0.3",
        })
    if "lowcut" in item:
        attributes.update(
            HPactive="1", HPfreq=_text(_number(item, "lowcut", 31, 20, 20000, where))
        )
    if "highcut" in item:
        attributes.update(
            LPactive="1", LPfreq=_text(_number(item, "highcut", 18000, 20, 20000, where))
        )
    if "low" in item:
        attributes.update(
            Lowshelfactive="1", Lowshelfgain=_text(_number(item, "low", 0, -18, 18, where))
        )
    if "high" in item:
        attributes.update(
            Highshelfactive="1", HighShelfgain=_text(_number(item, "high", 0, -18, 18, where))
        )
    if "mid" in item or "mid_freq" in item:
        attributes.update(
            Peak2active="1", Peak2gain=_text(_number(item, "mid", 0, -18, 18, where)),
            Peak2freq=_text(_number(item, "mid_freq", 1000, 20, 20000, where)),
        )
    return "eq", ET.Element("Eq", attributes), 1.0, (
        "lowcut", "highcut", "low", "mid", "mid_freq", "high"
    )


def _filter(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    kind = str(item.get("kind", "lowpass")).lower()
    if kind not in _FILTERS:
        raise MixError(f"{where}: `kind` is one of {', '.join(_FILTERS)}.")
    controls = ET.Element("DualFilterControls", {
        "enabled1": "1", "filter1": str(_FILTERS[kind]),
        "cut1": _text(_number(item, "cutoff", 2000, 1, 20000, where)),
        "res1": _text(_number(item, "resonance", 0.5, 0.01, 10, where)),
        "gain1": "100", "mix": "-1", "enabled2": "0", "filter2": "0", "cut2": "7000",
        "res2": "0.5", "gain2": "100",
    })
    return "dualfilter", controls, 1.0, ("kind", "cutoff", "resonance")


def _compressor(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    threshold = _number(item, "threshold", 0.5, 0.01, 1, where)
    ratio = _number(item, "ratio", 4, 1, 100, where)
    controls = ET.Element("dynamicsprocessor_controls", {
        "inputGain": "1", "outputGain": _text(_number(item, "gain", 1, 0, 5, where)),
        "attack": _text(_number(item, "attack", 10, 1, 500, where)),
        "release": _text(_number(item, "release", 100, 1, 500, where)),
        "stereoMode": "0",
        # Above the threshold the level rises `ratio` times slower.
        "waveShape": _curve(
            lambda level: level if level <= threshold
            else threshold + (level - threshold) / ratio
        ),
    })
    return "dynamicsprocessor", controls, 1.0, ("threshold", "ratio", "attack", "release", "gain")


def _distortion(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    amount = _number(item, "amount", 0.5, 0, 1, where)
    bend = 0.5 + amount * 8
    controls = ET.Element("waveshapercontrols", {
        "inputGain": _text(_number(item, "drive", 1.5, 0, 5, where)),
        # Driven into the curve a sound comes out near full scale: at LMMS's own output
        # gain a saw came out ten times louder (measured 2026-11-18), so it is turned down.
        "outputGain": _text(_number(item, "gain", 0.3, 0, 5, where)),
        "clipInput": "1",
        "waveShape": _curve(lambda level: math.tanh(bend * level) / math.tanh(bend)),
    })
    return "waveshaper", controls, 1.0, ("amount", "drive", "gain")


def _bitcrush(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    bits = _number(item, "bits", 6, 1, 8, where)
    controls = ET.Element("bitcrushcontrols", {
        "ingain": "0", "innoise": "0", "outgain": "0", "outclip": "0", "stereodiff": "0",
        "rate": _text(_number(item, "rate", 8000, 20, 44100, where)),
        "levels": _text(min(256, round(2 ** bits))), "rateon": "1", "depthon": "1",
    })
    return "bitcrush", controls, 1.0, ("rate", "bits")


def _flanger(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    controls = ET.Element("Flanger", {
        "DelayTimeSamples": "0.003",
        "LfoFrequency": _text(_number(item, "rate", 0.25, 0.01, 60, where)),
        "LfoAmount": _text(0.0025 * _number(item, "depth", 0.6, 0, 1, where)),
        "Feedback": _text(_number(item, "feedback", 0.3, 0, 1, where)),
        "WhiteNoise": "0", "Invert": "0",
    })
    return "flanger", controls, 1.0, ("rate", "depth", "feedback")


def _widen(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    controls = ET.Element(
        "stereoenhancercontrols", width=_text(180 * _number(item, "width", 0.4, 0, 1, where))
    )
    return "stereoenhancer", controls, 1.0, ("width",)


def _bassboost(item: Mapping[str, Any], _tempo: int, where: str) -> tuple:
    controls = ET.Element("bassboostercontrols", {
        "freq": _text(_number(item, "freq", 100, 50, 200, where)),
        "gain": _text(_number(item, "gain", 1.5, 0.1, 5, where)), "ratio": "2",
    })
    return "bassbooster", controls, 1.0, ("freq", "gain")


_EFFECTS = {
    "reverb": _reverb, "delay": _delay, "eq": _eq, "filter": _filter,
    "compressor": _compressor, "distortion": _distortion, "bitcrush": _bitcrush,
    "flanger": _flanger, "widen": _widen, "bassboost": _bassboost,
}
#: Each effect's LMMS plugin name, to read a chain back.
_NAMES = {
    "reverb": "reverbsc", "delay": "delay", "eq": "eq", "filter": "dualfilter",
    "compressor": "dynamicsprocessor", "distortion": "waveshaper", "bitcrush": "bitcrush",
    "flanger": "flanger", "widen": "stereoenhancer", "bassboost": "bassbooster",
}


def _curve(shape: Any) -> str:
    """LMMS's graph of 200 levels: the output for inputs 0.005, 0.01, ... 1."""
    levels = [min(1.0, max(0.0, shape((index + 1) / 200))) for index in range(200)]
    return base64.b64encode(struct.pack("<200f", *levels)).decode("ascii")


# -- automation -----------------------------------------------------------------------


def value(element: ET.Element | None, name: str, default: str = "") -> str:
    """A knob's value, whether LMMS wrote it as an attribute or, automated, as a child."""
    if element is None:
        return default
    plain = element.get(name)
    if plain is not None:
        return plain
    child = element.find(name)
    return child.get("value", default) if child is not None else default


def set_value(element: ET.Element, name: str, text: str) -> None:
    """Set a knob where LMMS reads it: the automated child when there is one."""
    child = element.find(name)
    if child is not None and child.get("id") is not None:
        child.set("value", text)
    else:
        element.set(name, text)


def automate(
    root: ET.Element, target: str, places: list[tuple[ET.Element, str]], raw_points: Any,
    curve: Any, low: float, high: float, bar: int,
) -> bool:
    """Move the knobs at ``places`` along ``raw_points``; no points removes the movement.
    Returns whether an automation is there afterwards."""
    container = root.find("song/trackcontainer")
    assert container is not None
    name = MARK + target
    for track in container.findall("track"):
        if track.get("type") == AUTOMATION_TRACK and track.get("name", "").casefold() == (
            name.casefold()
        ):
            container.remove(track)
    points = _points(raw_points, low, high, target)
    if not points:
        # Back to plain knobs, each at the value it had.
        for element, attribute in places:
            child = element.find(attribute)
            if child is not None:
                element.set(attribute, child.get("value", "0"))
                element.remove(child)
        return False
    kind = str(curve or "linear").lower()
    if kind not in _CURVES:
        raise MixError(f"{target}: `curve` is one of {', '.join(_CURVES)}.")
    taken = {int(node.get("id")) for node in root.iter() if (node.get("id") or "").isdigit()}
    last = points[-1][0]
    track = ET.SubElement(container, "track", muted="0", type=AUTOMATION_TRACK, name=name,
                          solo="0")
    ET.SubElement(track, "automationtrack")
    pattern = ET.SubElement(
        track, "automationpattern", name=target, pos="0", mute="0", tens="1",
        prog=_CURVES[kind], len=str(max(bar, -(-(last + 1) // bar) * bar)),
    )
    for tick, level in points:
        ET.SubElement(pattern, "time", pos=str(tick), value=_text(level))
    for element, attribute in places:
        child = element.find(attribute)
        if child is None:
            child = ET.SubElement(element, attribute, scale_type="linear")
        if not (child.get("id") or "").isdigit():
            identity = random.randrange(1, 16_000_000)
            while identity in taken:
                identity = random.randrange(1, 16_000_000)
            taken.add(identity)
            child.set("id", str(identity))
        child.set("value", _text(points[0][1]))
        element.attrib.pop(attribute, None)
        ET.SubElement(pattern, "object", id=child.get("id", ""))
    return True


def movements(root: ET.Element) -> list[str]:
    """Each automation this tool wrote, as `TARGET: points (curve)`, for reading back."""
    back = {number: name for name, number in _CURVES.items()}
    found = []
    for track in root.iterfind("song/trackcontainer/track"):
        name = track.get("name", "")
        if track.get("type") != AUTOMATION_TRACK or not name.startswith(MARK):
            continue
        for pattern in track.findall("automationpattern"):
            start = max(0, int(float(pattern.get("pos", "0") or 0)))
            points = " ".join(
                f"{(start + int(float(time.get('pos', '0')))) / TICKS_PER_BEAT:g}:"
                f"{float(time.get('value', '0')):g}"
                for time in pattern.findall("time")
            )
            kind = back.get(pattern.get("prog", "1"), "linear")
            found.append(f"{name[len(MARK):]}: {points} ({kind})")
    return found


def _points(raw: Any, low: float, high: float, target: str) -> list[tuple[int, float]]:
    if raw is None or raw == "" or raw == []:
        return []
    tokens = raw.replace(",", " ").split() if isinstance(raw, str) else raw
    if not isinstance(tokens, list) or len(tokens) > MAX_POINTS:
        raise MixError(f"{target}: `points` is text like `0:300 16:2500`, in order.")
    points: dict[int, float] = {}
    for token in tokens:
        if isinstance(token, (list, tuple)) and len(token) == 2:
            beat, level = token
        else:
            beat, _, level = str(token).partition(":")
        try:
            at, to = float(beat), float(level)
        except (TypeError, ValueError):
            raise MixError(
                f"{target}: `{token}` is not a point. Write `BEAT:VALUE`, e.g. `16:2500`."
            ) from None
        if not (math.isfinite(at) and math.isfinite(to)) or at < 0:
            raise MixError(f"{target}: `{token}` is not a point. Beats start at 0.")
        if not low <= to <= high:
            raise MixError(f"{target}: {to:g} is outside its range, {low:g} to {high:g}.")
        points[round(at * TICKS_PER_BEAT)] = to
    return sorted(points.items())


def _number(source: Any, key: str, default: float, low: float, high: float, where: str) -> float:
    raw = source.get(key, default) if isinstance(source, Mapping) else default
    if isinstance(raw, bool):
        raise MixError(f"{where}: `{key}` is a number from {low:g} to {high:g}.")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise MixError(f"{where}: `{key}` is a number from {low:g} to {high:g}.") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise MixError(f"{where}: `{key}` is a number from {low:g} to {high:g}; got {raw}.")
    return number


def _text(number: float) -> str:
    return f"{number:.5f}".rstrip("0").rstrip(".") or "0"
