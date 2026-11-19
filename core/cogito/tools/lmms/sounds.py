"""The sound of a track: one of LMMS's presets, or a synth the model designs.

A preset is an `.xpf` file LMMS ships under `presets/<Synth>/` (TripleOscillator,
Monstro, Watsyn, BitInvader, LB302, OpulenZ, Organic, SID, Nescaline, Kicker, Vibed,
AudioFileProcessor) or the person saved under `~/lmms/presets`: an `<instrumenttrack>`
taken whole, so every synth LMMS has is reachable by a name.

A designed sound is written on TripleOscillator, LMMS's three-oscillator synth, and on
the envelopes, filter and LFOs every LMMS instrument track has, so the same shaping
(attack, release, a filter that opens, a tremolo) also works on a preset or a SoundFont
instrument. Units are the model's, turned into LMMS's knob values here, from LMMS 1.2.2:

- a time knob is 0-2 and lasts 5 * knob^2 seconds (`SECS_PER_ENV_SEGMENT`, `expKnobVal`;
  src/core/EnvelopeAndLfoParameters.cpp), so 20 s at most;
- an LFO's speed knob is 0.001-1 and one cycle lasts 20 * knob seconds, a hundredth of
  that with `x100` (`SECS_PER_LFO_OSCILLATION`, same file);
- the filter's cutoff is 1-14000 Hz and its resonance 0.01-10, and the filter types are
  an index into the list in src/core/InstrumentSoundShaping.cpp;
- an oscillator's wave and how one modulates the next are the enums of
  include/Oscillator.h; its volume is 0-200, coarse tuning in semitones, fine in cents.

`set` reaches any attribute by its own name, for the synths this module has no words for.
"""

from __future__ import annotations

import difflib
import math
import re
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

WAVES = {"sine": 0, "triangle": 1, "saw": 2, "square": 3, "moog": 4, "exp": 5, "noise": 6}
#: How an oscillator acts on the next one.
MIXES = {"pm": 0, "am": 1, "mix": 2, "sync": 3, "fm": 4}
FILTERS = {
    "lowpass": 0, "highpass": 1, "bandpass": 2, "notch": 4, "moog": 6, "lowpass24": 7,
    "vowel": 14,
}
LFO_SHAPES = {"sine": 0, "triangle": 1, "saw": 2, "square": 3, "random": 5}
_ENVELOPE_KEYS = ("attack", "decay", "sustain", "release")
_SOUND_KEYS = ("oscillators", "mix", *_ENVELOPE_KEYS, "filter", "tremolo", "set")
#: An oscillator's `volume` of 100 as LMMS's own 0-200 value. At 100 a single saw
#: clipped a render (LMMS 1.2.2, measured 2026-11-18); LMMS's default is 33 an oscillator.
_LEVEL = 0.35
_MAX_PRESET_BYTES = 4 * 1024 * 1024
#: What an `<instrumenttrack>` holds besides its instrument.
_TRACK_PARTS = ("eldata", "chordcreator", "arpeggiator", "midiport", "fxchain", "instrument")
#: Attributes of an envelope that say nothing a `set` would change.
_ENVELOPE_NOISE = ("userwavefile", "lspd_", "syncmode", "ctlenvamt", "pdel", "lpdel")

HELP = (
    "A track's `instrument` may be any preset below, by its name. A track's `sound` "
    "designs or shapes one; every field is optional:\n"
    "  oscillators: 1-3 of {wave: sine|triangle|saw|square|moog|exp|noise, octave: -2..2, "
    "semitones: -12..12, detune: cents 0-100 (spreads it left and right), volume: 0-100}. "
    "Giving this makes the track a new synth; without it the track's instrument stays "
    "and is only shaped.\n"
    "  mix: how each oscillator acts on the next: mix (added), fm, pm, am, sync. One "
    "name, or two.\n"
    "  attack, decay, release: seconds (0-20); sustain: 0-1. The volume envelope: a pad "
    "has a slow attack and release, a pluck no sustain.\n"
    "  filter: {type: lowpass|highpass|bandpass|notch|moog|lowpass24|vowel, cutoff: Hz "
    "1-14000, resonance: 0.01-10, envelope: -1..1 (how far the cutoff moves over the "
    "note), attack/decay/sustain/release of that movement, lfo: {rate: Hz, depth: -1..1, "
    "shape: sine|triangle|saw|square|random}}.\n"
    "  tremolo: {rate: Hz, depth: -1..1, shape}: the volume wavering.\n"
    "  set: {attribute: value} for any attribute of the synth by LMMS's own name; call "
    "sounds with a preset's name to see its attributes.\n"
    'Example: {"oscillators": [{"wave": "saw", "detune": 12}, {"wave": "saw", "octave": '
    '-1}], "attack": 0.8, "release": 1.5, "filter": {"type": "lowpass", "cutoff": 900, '
    '"resonance": 2, "lfo": {"rate": 0.2, "depth": 0.4}}}'
)


class SoundError(ValueError):
    """What is wrong with a sound, worded for the model."""


# -- presets ----------------------------------------------------------------------------


def preset_dirs(binary: str = "") -> list[Path]:
    """Where presets are: the person's own first, then the ones LMMS ships."""
    dirs = [Path.home() / "lmms" / "presets"]
    if binary:
        dirs.append(Path(binary).resolve().parent.parent / "share" / "lmms" / "presets")
    dirs += [Path("/usr/share/lmms/presets"), Path("/usr/local/share/lmms/presets")]
    seen: list[Path] = []
    for folder in dirs:
        if folder.is_dir() and folder not in seen:
            seen.append(folder)
    return seen


def presets(dirs: Sequence[Path]) -> dict[str, list[Path]]:
    """Every preset by its synth: `{"TripleOscillator": [.../SuperSawLead.xpf, ...]}`."""
    found: dict[str, list[Path]] = {}
    for folder in dirs:
        for path in sorted(folder.glob("*/*.xpf")):
            if not _is_preset(path):
                continue
            group = found.setdefault(path.parent.name, [])
            if all(other.stem != path.stem for other in group):
                group.append(path)
    return found


def _is_preset(path: Path) -> bool:
    """Whether the file is a preset this module loads. Two of LMMS 1.2.2's own are in
    the `channelsettings` layout of LMMS 0.0.9, which is left out."""
    try:
        with path.open("rb") as file:
            head = file.read(2048)
    except OSError:
        return False
    return b"instrumenttracksettings" in head or not head.lstrip().startswith(b"<")


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9/]", "", name.lower())


def find_preset(name: str, dirs: Sequence[Path]) -> Path | None:
    """The preset ``name`` means: `SuperSawLead`, or `Monstro/Growl` where two synths
    share a name. None when it names none; an error when it names several."""
    wanted = _fold(name)
    if not wanted:
        return None
    matches = [
        path
        for synth, paths in presets(dirs).items()
        for path in paths
        if wanted in (_fold(path.stem), _fold(f"{synth}/{path.stem}"))
    ]
    if len(matches) > 1:
        raise SoundError(
            f"`{name}` is a preset of several synths: "
            + ", ".join(f"{path.parent.name}/{path.stem}" for path in matches)
            + ". Name it with its synth."
        )
    return matches[0] if matches else None


def near(name: str, dirs: Sequence[Path]) -> list[str]:
    """Preset names close to ``name``, for the error that says it is not one."""
    stems = {_fold(path.stem): path.stem for paths in presets(dirs).values() for path in paths}
    close = difflib.get_close_matches(_fold(name), list(stems), n=5, cutoff=0.6)
    return [stems[key] for key in close]


def load_preset(path: Path) -> ET.Element:
    """The preset's `<instrumenttrack>`, to put in a track in place of its own."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SoundError(f"The preset {path} could not be read: {exc}.") from None
    if len(data) > _MAX_PRESET_BYTES:
        raise SoundError(f"The preset {path} is too large to be one.")
    if not data.lstrip().startswith(b"<"):
        try:
            data = zlib.decompress(data[4:])
        except zlib.error:
            raise SoundError(f"The preset {path} is not an LMMS preset.") from None
    if b"<!ENTITY" in data:
        raise SoundError(f"The preset {path} declares entities and was not read.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise SoundError(f"The preset {path} is not an LMMS preset.") from None
    holder = root.find("instrumenttracksettings/instrumenttrack")
    if holder is None:
        raise SoundError(f"The preset {path} holds no instrument.")
    if holder.find("instrument") is None:
        # Presets from before LMMS 1.0 hold the synth's element itself. LMMS loads the
        # first child it has no other name for as the instrument
        # (InstrumentTrack::loadTrackSpecificSettings, "compat code"); so does this.
        old = next((child for child in holder if child.tag not in _TRACK_PARTS), None)
        if old is None:
            raise SoundError(f"The preset {path} holds no instrument.")
        place = list(holder).index(old)
        holder.remove(old)
        wrapper = ET.Element("instrument", name=old.tag)
        wrapper.append(old)
        holder.insert(place, wrapper)
    return holder


def catalog(query: str, dirs: Sequence[Path]) -> str:
    """What `sounds` answers: how a sound is written and the presets, or one preset's
    own attributes when ``query`` names it."""
    known = presets(dirs)
    words = [_fold(word) for word in query.split() if _fold(word)]
    if words:
        exact = None
        try:
            exact = find_preset(query, dirs)
        except SoundError:
            exact = None
        if exact is not None:
            return _preset_text(exact)
        lines = []
        for synth, paths in known.items():
            names = [
                path.stem for path in paths
                if all(word in _fold(f"{synth}/{path.stem}") for word in words)
            ]
            if names:
                lines.append(f"{synth}: {', '.join(names)}")
        if not lines:
            close = near(query, dirs)
            return (
                f"No preset matches `{query}`."
                + (f" Close: {', '.join(close)}." if close else "")
                + " Call sounds without a query for all of them."
            )
        return f"Presets matching `{query}` (use a name as a track's `instrument`):\n" + "\n".join(
            lines
        )
    lines = [HELP, ""]
    if not known:
        lines.append(
            "No presets were found on this machine (LMMS's presets folder is missing); "
            "the built-in instruments and `sound` still work."
        )
    else:
        lines.append("Presets, by synth (call sounds with a name to see its attributes):")
        lines += [
            f"{synth} ({len(paths)}): {', '.join(path.stem for path in paths)}"
            for synth, paths in known.items()
        ]
    return "\n".join(lines)


def _preset_text(path: Path) -> str:
    holder = load_preset(path)
    instrument = holder.find("instrument")
    assert instrument is not None
    kind = instrument.get("name", "?")
    inner = instrument.find(kind)
    lines = [f"Preset {path.parent.name}/{path.stem}: the synth `{kind}`. Its attributes:"]
    if inner is not None:
        lines.append(_attributes(inner, skip=("userwavefile",)))
    shaping = holder.find("eldata")
    if shaping is not None:
        lines.append(
            "Filter (`set` names: fwet 1 on, ftype, fcut Hz, fres): " + _attributes(shaping)
        )
        for tag, word in (("elvol", "vol"), ("elcut", "cut")):
            part = shaping.find(tag)
            if part is not None:
                lines.append(
                    f"{'Volume' if word == 'vol' else 'Cutoff'} envelope and LFO (`set` names "
                    f"`{word}.att`, `{word}.lamt`, ...): "
                    + _attributes(part, skip=_ENVELOPE_NOISE)
                )
    lines.append("Change one with a track's `sound`: {\"set\": {\"NAME\": VALUE}}.")
    return "\n".join(lines)


def _attributes(element: ET.Element, skip: Sequence[str] = ()) -> str:
    return " ".join(
        f"{name}={value}" for name, value in sorted(element.attrib.items())
        if not any(name.startswith(prefix) for prefix in skip)
    )


# -- a designed sound -----------------------------------------------------------------


def shape(holder: ET.Element, sound: Mapping[str, Any]) -> None:
    """Apply ``sound`` to an `<instrumenttrack>`: a new synth when it gives oscillators,
    and the envelope, filter and LFOs it names."""
    unknown = [key for key in sound if key not in _SOUND_KEYS]
    if unknown:
        raise SoundError(
            f"`sound` has no field `{unknown[0]}`. Its fields: {', '.join(_SOUND_KEYS)}. "
            "Call lmms with action sounds for what each takes."
        )
    shaping = _shaping(holder)
    volume = shaping.find("elvol")
    cutoff = shaping.find("elcut")
    assert volume is not None and cutoff is not None
    if "oscillators" in sound or "mix" in sound:
        _oscillators(holder, sound.get("oscillators"), sound.get("mix"))
        # A synth built here has no envelope of its own: the track's is switched on.
        volume.set("amt", "1")
        for name, default in (("att", 0.0), ("hold", 0.0), ("dec", 0.5), ("sustain", 1.0),
                              ("rel", _time(0.1, "release"))):
            volume.set(name, _text(default))
    if any(key in sound for key in _ENVELOPE_KEYS):
        _envelope(volume, sound, "sound")
        volume.set("amt", "1")
        volume.set("hold", "0")
    if "tremolo" in sound:
        _lfo(volume, sound["tremolo"], "tremolo")
    if "filter" in sound:
        _filter(shaping, cutoff, sound["filter"])
    if "set" in sound:
        _set(holder, sound["set"])


def _shaping(holder: ET.Element) -> ET.Element:
    """The track's `<eldata>` with its three envelopes, made where a preset lacks them."""
    shaping = holder.find("eldata")
    if shaping is None:
        shaping = ET.SubElement(holder, "eldata", fres="0.5", ftype="0", fcut="14000", fwet="0")
    for tag in ("elvol", "elcut", "elres"):
        if shaping.find(tag) is None:
            ET.SubElement(shaping, tag, {
                "att": "0", "hold": "0.5", "dec": "0.5", "sustain": "0.5", "rel": "0.1",
                "amt": "0", "pdel": "0", "lpdel": "0", "latt": "0", "lspd": "0.1",
                "lamt": "0", "lshp": "0", "x100": "0", "ctlenvamt": "0", "userwavefile": "",
                "lspd_numerator": "4", "lspd_denominator": "4", "lspd_syncmode": "0",
            })
    return shaping


def _oscillators(holder: ET.Element, raw: Any, mix: Any) -> None:
    if raw is None:
        raw = [{"wave": "saw"}]
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
        raise SoundError("`oscillators` is a list of one to three, e.g. [{\"wave\": \"saw\"}].")
    attributes: dict[str, str] = {}
    for index in range(3):
        item = raw[index] if index < len(raw) else None
        if item is not None and not isinstance(item, Mapping):
            raise SoundError(
                f"Oscillator {index + 1} must be an object like {{\"wave\": \"saw\"}}."
            )
        where = f"oscillator {index + 1}"
        wave = str((item or {}).get("wave", "sine")).lower()
        if wave not in WAVES:
            raise SoundError(f"{where}: `{wave}` is not a wave. Waves: {', '.join(WAVES)}.")
        coarse = 12 * _number(item, "octave", 0, -2, 2, where) + _number(
            item, "semitones", 0, -12, 12, where
        )
        detune = _number(item, "detune", 0, 0, 100, where)
        level = _number(item, "volume", 100 / len(raw), 0, 100, where) if item else 0
        attributes.update({
            f"wavetype{index}": str(WAVES[wave]),
            f"coarse{index}": _text(max(-24, min(24, coarse))),
            f"finel{index}": _text(detune), f"finer{index}": _text(-detune),
            f"vol{index}": _text(level * _LEVEL), f"pan{index}": "0",
            f"phoffset{index}": "0", f"stphdetun{index}": "0", f"userwavefile{index}": "",
        })
    mixes = [mix] if isinstance(mix, str) else list(mix or [])
    if len(mixes) > 2 or any(str(name).lower() not in MIXES for name in mixes):
        raise SoundError(f"`mix` is one or two of: {', '.join(MIXES)}.")
    first = MIXES[str(mixes[0]).lower()] if mixes else MIXES["mix"]
    second = MIXES[str(mixes[1]).lower()] if len(mixes) > 1 else first
    attributes.update({"modalgo1": str(first), "modalgo2": str(second), "modalgo3": "2"})
    old = holder.find("instrument")
    if old is not None:
        holder.remove(old)
    instrument = ET.Element("instrument", name="tripleoscillator")
    ET.SubElement(instrument, "tripleoscillator", attributes)
    holder.insert(0, instrument)


def _envelope(element: ET.Element, source: Mapping[str, Any], where: str) -> None:
    for key, attribute in (("attack", "att"), ("decay", "dec"), ("release", "rel")):
        if key in source:
            element.set(attribute, _text(_time(_number(source, key, 0, 0, 20, where), key)))
    if "sustain" in source:
        element.set("sustain", _text(_number(source, "sustain", 1, 0, 1, where)))


def _lfo(element: ET.Element, raw: Any, where: str) -> None:
    if not isinstance(raw, Mapping):
        raise SoundError(f"`{where}` is an object like {{\"rate\": 5, \"depth\": 0.3}}.")
    rate = _number(raw, "rate", 1, 0.05, 5000, where)
    shape_name = str(raw.get("shape", "sine")).lower()
    if shape_name not in LFO_SHAPES:
        raise SoundError(f"{where}: `shape` is one of {', '.join(LFO_SHAPES)}.")
    knob = 1 / rate / 20
    fast = knob < 0.001
    element.set("lspd", _text(max(0.001, min(1.0, knob * 100 if fast else knob))))
    element.set("x100", "1" if fast else "0")
    element.set("lamt", _text(_number(raw, "depth", 0.3, -1, 1, where)))
    element.set("lshp", str(LFO_SHAPES[shape_name]))
    element.set("lspd_syncmode", "0")


def _filter(shaping: ET.Element, cutoff: ET.Element, raw: Any) -> None:
    if not isinstance(raw, Mapping):
        raise SoundError("`filter` is an object like {\"type\": \"lowpass\", \"cutoff\": 800}.")
    kind = str(raw.get("type", "lowpass")).lower()
    if kind not in FILTERS:
        raise SoundError(f"filter: `{kind}` is not a type. Types: {', '.join(FILTERS)}.")
    shaping.set("fwet", "1")
    shaping.set("ftype", str(FILTERS[kind]))
    shaping.set("fcut", _text(_number(raw, "cutoff", 2000, 1, 14000, "filter")))
    shaping.set("fres", _text(_number(raw, "resonance", 0.5, 0.01, 10, "filter")))
    if "envelope" in raw or any(key in raw for key in _ENVELOPE_KEYS):
        cutoff.set("amt", _text(_number(raw, "envelope", 0.5, -1, 1, "filter")))
        cutoff.set("hold", "0")
        _envelope(cutoff, raw, "filter")
    if "lfo" in raw:
        _lfo(cutoff, raw["lfo"], "filter lfo")


def _set(holder: ET.Element, raw: Any) -> None:
    """Attributes by LMMS's own names: the synth's, the filter's, or an envelope's as
    `vol.NAME`, `cut.NAME`, `res.NAME`."""
    if not isinstance(raw, Mapping):
        raise SoundError("`set` is an object of attribute names and values.")
    instrument = holder.find("instrument")
    inner = instrument.find(instrument.get("name", "")) if instrument is not None else None
    shaping = holder.find("eldata")
    for name, value in raw.items():
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (int, float, str)) or not isinstance(name, str):
            raise SoundError(f"set: `{name}` takes a number or a short text.")
        text = _text(value) if isinstance(value, (int, float)) else value
        if len(text) > 200 or "<" in text:
            raise SoundError(f"set: the value of `{name}` is not one an attribute takes.")
        part, _, attribute = name.partition(".")
        if attribute and part in ("vol", "cut", "res") and shaping is not None:
            target = shaping.find(f"el{part}")
            if target is not None and attribute in target.attrib:
                target.set(attribute, text)
                continue
        elif inner is not None and name in inner.attrib:
            inner.set(name, text)
            continue
        elif shaping is not None and name in shaping.attrib:
            shaping.set(name, text)
            continue
        known = sorted(inner.attrib) if inner is not None else []
        close = difflib.get_close_matches(name, known, n=4, cutoff=0.5)
        raise SoundError(
            f"set: this track's synth has no attribute `{name}`."
            + (f" Close: {', '.join(close)}." if close else "")
            + " Call lmms with action sounds and the preset's name to see them."
        )


def _number(source: Any, key: str, default: float, low: float, high: float, where: str) -> float:
    raw = (source or {}).get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise SoundError(f"{where}: `{key}` is a number from {low:g} to {high:g}.")
    try:
        value = float(raw)
    except ValueError:
        raise SoundError(f"{where}: `{key}` is a number from {low:g} to {high:g}.") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise SoundError(f"{where}: `{key}` is a number from {low:g} to {high:g}; got {raw}.")
    return value


def _time(seconds: float, _key: str) -> float:
    """LMMS's knob value for a time in seconds."""
    return min(2.0, math.sqrt(max(0.0, seconds) / 5.0))


def _text(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
