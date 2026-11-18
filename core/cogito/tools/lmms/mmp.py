"""An LMMS project file: reading one, changing its tracks, writing it back.

LMMS (GPL-2.0-or-later) is the open-source workstation the `lmms` tool writes for: its
project is XML, `.mmp` plain and `.mmpz` the same text through Qt's `qCompress` (a 4-byte
big-endian length, then zlib). The element and attribute names here were read from LMMS
1.2.2's sources on 2026-11-18: data/projects/templates/default.mpt, src/core/DataFile.cpp,
src/core/Note.cpp, src/tracks/Pattern.cpp, plugins/sf2_player and
plugins/audio_file_processor. Nothing of LMMS is copied or linked; Cogito writes the file
and runs the program the person installed.

A change keeps what it does not name. A track is found by its name: its notes are
replaced, its instrument only when the call gives one, and every other track, the mixer,
the effects and whatever the person set in LMMS stay as they were.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from . import score

#: LMMS track types (include/Track.h).
INSTRUMENT_TRACK = "0"
#: A melody pattern, as against a beat pattern's steps (include/Pattern.h).
MELODY_PATTERN = "1"
#: A project larger than this is not one a tool should parse: LMMS's own demos are
#: under 1 MB uncompressed, and a project that embeds samples is the person's to edit.
MAX_BYTES = 32 * 1024 * 1024

#: Where distributions put a General MIDI SoundFont, in the order tried.
SOUNDFONTS = (
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/soundfonts/default.sf2",
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/sounds/sf2/default-GM.sf2",
    "/usr/share/soundfonts/GeneralUser-GS.sf2",
)

#: General MIDI programs by the name a call may give (0-based, as LMMS's `patch`).
GM = {
    "piano": 0, "bright_piano": 1, "electric_piano": 4, "harpsichord": 6, "celesta": 8,
    "glockenspiel": 9, "music_box": 10, "vibraphone": 11, "marimba": 12, "organ": 16,
    "church_organ": 19, "accordion": 21, "harmonica": 22, "nylon_guitar": 24,
    "guitar": 25, "electric_guitar": 27, "overdrive_guitar": 29, "distortion_guitar": 30,
    "acoustic_bass": 32, "bass_guitar": 33, "slap_bass": 36, "synth_bass": 38,
    "violin": 40, "viola": 41, "cello": 42, "contrabass": 43, "pizzicato": 45, "harp": 46,
    "timpani": 47, "strings": 48, "slow_strings": 49, "synth_strings": 50, "choir": 52,
    "voice": 53, "trumpet": 56, "trombone": 57, "tuba": 58, "french_horn": 60,
    "brass": 61, "soprano_sax": 64, "alto_sax": 65, "tenor_sax": 66, "oboe": 68,
    "bassoon": 70, "clarinet": 71, "piccolo": 72, "flute": 73, "pan_flute": 75,
    "square_lead": 80, "saw_lead": 81, "warm_pad": 89, "sitar": 104, "banjo": 105,
    "kalimba": 108, "steel_drums": 114,
}

#: Synths built from LMMS's TripleOscillator, which needs no SoundFont: the wave of each
#: oscillator (0 sine, 1 triangle, 2 saw, 3 square; include/Oscillator.h), its octave
#: shift and volume, and the volume envelope's attack, decay, sustain and release as
#: LMMS's knob values. The numbers are choices made by ear's stand-in, a waveform; they
#: are a starting point the person changes in LMMS.
@dataclass(frozen=True, slots=True)
class Synth:
    waves: tuple[int, int, int]
    octaves: tuple[int, int, int]
    volumes: tuple[int, int, int]
    attack: float = 0.0
    decay: float = 0.5
    sustain: float = 0.5
    release: float = 0.1


SYNTHS = {
    "sine": Synth((0, 0, 0), (0, 12, 0), (60, 10, 0)),
    "triangle": Synth((1, 1, 0), (0, 0, 0), (55, 0, 0)),
    "saw": Synth((2, 2, 0), (0, 0, 0), (35, 25, 0)),
    "square": Synth((3, 3, 0), (0, 0, 0), (30, 0, 0)),
    "pluck": Synth((2, 3, 0), (0, 0, 0), (35, 15, 0), decay=0.25, sustain=0.0, release=0.2),
    "pad": Synth((2, 2, 0), (0, 0, -12), (25, 25, 20), attack=0.5, sustain=0.8, release=0.6),
    "bass": Synth((2, 3, 0), (-12, -12, -24), (35, 15, 35), decay=0.4, sustain=0.6),
    "sub_bass": Synth((0, 0, 0), (-12, -24, 0), (60, 30, 0)),
    "lead": Synth((2, 3, 2), (0, 0, 12), (30, 20, 10), sustain=0.7, release=0.15),
    "bell": Synth((0, 0, 0), (0, 12, 24), (45, 20, 12), decay=0.9, sustain=0.0, release=0.6),
}

#: A drum kit's drums, each a sample that ships with LMMS (data/samples/drums). One drum
#: is one LMMS track, since its sample player holds one sample.
DRUMS = {
    "kick": "drums/kick01.ogg",
    "kick_hard": "drums/kick_hard01.ogg",
    "kick_soft": "drums/kick_soft01.ogg",
    "snare": "drums/snare01.ogg",
    "snare_acoustic": "drums/snare_acoustic01.ogg",
    "snare_electro": "drums/snare_electro01.ogg",
    "rim": "drums/rim01.ogg",
    "sidestick": "drums/sidestick01.ogg",
    "clap": "drums/clap01.ogg",
    "hat": "drums/hihat_closed01.ogg",
    "openhat": "drums/hihat_opened01.ogg",
    "pedalhat": "drums/hihat_foot_pedal01.ogg",
    "ride": "drums/ride01.ogg",
    "crash": "drums/crash01.ogg",
    "tom": "drums/tom01.ogg",
    "shaker": "drums/shaker01.ogg",
    "clave": "drums/clav01.ogg",
}
DRUM_KIT = "drums"


class ProjectError(ValueError):
    """What is wrong with a project file or a change to it, worded for the model."""


def instruments() -> str:
    """Every instrument name a call may give, for an error or the tool's description."""
    return (
        f"`{DRUM_KIT}` (a kit: {', '.join(DRUMS)}); synths: {', '.join(SYNTHS)}; "
        f"General MIDI: {', '.join(GM)}, or `gm:N` for program N (0-127)"
    )


def soundfont(candidates: Sequence[str] = SOUNDFONTS) -> str:
    """The General MIDI SoundFont on this machine, or "" when there is none."""
    return next((path for path in candidates if Path(path).is_file()), "")


@dataclass(frozen=True, slots=True)
class TrackChange:
    """One track of a call: its name, and what the call gives for it."""

    name: str
    #: "" keeps the track's instrument (or, for a new track, means `piano`).
    instrument: str = ""
    #: None keeps the track's notes.
    notes: str | None = None
    volume: int | None = None
    pan: int | None = None


@dataclass(slots=True)
class Applied:
    """What a change did, for the tool's result."""

    created: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: General MIDI names played by a synth because no SoundFont is installed.
    fallbacks: list[str] = field(default_factory=list)
    notes: int = 0
    #: The last tick any note of the whole project ends on.
    ticks: int = 0


# -- reading and writing the file ---------------------------------------------------


def load(path: Path) -> ET.Element:
    """The project at ``path`` (`.mmp` or `.mmpz`) as its root element."""
    try:
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise ProjectError(
                f"{path} is {size:,} bytes, more than this tool reads. Open it in LMMS."
            )
        data = path.read_bytes()
    except OSError as exc:
        raise ProjectError(f"{path} could not be read: {exc.strerror or exc}.") from None
    if path.suffix.lower() == ".mmpz":
        try:
            data = zlib.decompressobj().decompress(data[4:], MAX_BYTES + 1)
        except zlib.error:
            raise ProjectError(f"{path} is not a compressed LMMS project.") from None
        if len(data) > MAX_BYTES:
            raise ProjectError(f"{path} unpacks to more than this tool reads. Open it in LMMS.")
    # The only declaration an LMMS project carries is `<!DOCTYPE lmms-project>`. An entity
    # is how an XML file expands without end, and LMMS writes none.
    if b"<!ENTITY" in data:
        raise ProjectError(f"{path} declares XML entities, which an LMMS project never does.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ProjectError(f"{path} is not valid XML: {exc}.") from None
    if root.tag != "lmms-project" or root.find("song/trackcontainer") is None:
        raise ProjectError(f"{path} is not an LMMS song project.")
    return root


def dump(root: ET.Element, path: Path) -> bytes:
    """``root`` as the bytes of the file at ``path``, compressed when it is `.mmpz`."""
    ET.indent(root, space="  ")
    text = (
        '<?xml version="1.0"?>\n<!DOCTYPE lmms-project>\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    ).encode("utf-8")
    if path.suffix.lower() == ".mmpz":
        return struct.pack(">I", len(text)) + zlib.compress(text)
    return text


def new_project(*, tempo: int = 120, numerator: int = 4, denominator: int = 4) -> ET.Element:
    """An empty song: no tracks, the master channel, LMMS's own window layout."""
    return ET.fromstring(
        f'<lmms-project version="1.0" creator="LMMS" creatorversion="1.2.2" type="song">'
        f'<head timesig_numerator="{numerator}" mastervol="100" '
        f'timesig_denominator="{denominator}" bpm="{tempo}" masterpitch="0"/>'
        "<song>"
        '<trackcontainer width="900" x="5" y="5" maximized="0" height="420" visible="1" '
        'type="song" minimized="0"/>'
        '<fxmixer width="543" x="5" y="430" maximized="0" height="335" visible="1" '
        'minimized="0"><fxchannel num="0" muted="0" volume="1" name="Master" soloed="0">'
        '<fxchain numofeffects="0" enabled="0"/></fxchannel></fxmixer>'
        '<ControllerRackView width="350" x="680" y="430" maximized="0" height="200" '
        'visible="0" minimized="0"/>'
        '<pianoroll width="900" x="5" y="5" maximized="0" height="480" visible="0" '
        'minimized="0"/>'
        '<automationeditor width="640" x="0" y="0" maximized="0" height="400" visible="0" '
        'minimized="0"/>'
        '<projectnotes width="640" x="700" y="10" maximized="0" height="400" visible="0" '
        'minimized="0"></projectnotes>'
        '<timeline lp1pos="192" lp0pos="0" lpstate="0"/>'
        "<controllers/>"
        "</song></lmms-project>"
    )


# -- changing it --------------------------------------------------------------------


def set_head(
    root: ET.Element,
    *,
    tempo: int | None = None,
    numerator: int | None = None,
    denominator: int | None = None,
) -> None:
    head = root.find("head")
    if head is None:
        head = ET.SubElement(root, "head", mastervol="100", masterpitch="0")
        root.remove(head)
        root.insert(0, head)
    # LMMS writes the tempo as an attribute, or as a child element once it is automated;
    # the attribute is the one a plain project reads.
    for name, value in (
        ("bpm", tempo), ("timesig_numerator", numerator), ("timesig_denominator", denominator)
    ):
        if value is not None:
            head.set(name, str(value))
        elif head.get(name) is None and head.find(name) is None:
            head.set(name, {"bpm": "120"}.get(name, "4"))


def apply(
    root: ET.Element,
    tracks: Sequence[TrackChange],
    *,
    remove: Sequence[str] = (),
    font: str = "",
) -> Applied:
    """Make ``tracks`` so in ``root``, and take ``remove`` out. Everything a call does
    not name is left as it is."""
    container = root.find("song/trackcontainer")
    assert container is not None  # `load` and `new_project` both guarantee it
    done = Applied()
    seen: set[str] = set()
    for change in tracks:
        folded = change.name.casefold()
        if folded in seen:
            raise ProjectError(f"Two tracks are named `{change.name}`. Give each its own name.")
        seen.add(folded)
        existing = _tracks_named(container, change.name)
        kit = change.instrument == DRUM_KIT or (
            not change.instrument and _is_kit(container, change.name)
        )
        if kit:
            _apply_kit(container, change, done)
        else:
            _apply_pitched(container, change, existing, font, done)
    for name in remove:
        gone = _tracks_named(container, name) + _kit_tracks(container, name)
        if not gone:
            raise ProjectError(
                f"There is no track named `{name}` to remove. The tracks: "
                f"{', '.join(_names(container)) or 'none'}."
            )
        for track in gone:
            container.remove(track)
        done.removed.append(name)
    done.ticks = length(root)
    return done


def _apply_pitched(
    container: ET.Element,
    change: TrackChange,
    existing: list[ET.Element],
    font: str,
    done: Applied,
) -> None:
    # A track that was a kit and is now given a pitched instrument: the kit goes.
    for track in _kit_tracks(container, change.name):
        container.remove(track)
    if existing:
        track = existing[0]
        if track.get("type") != INSTRUMENT_TRACK:
            raise ProjectError(
                f"`{change.name}` is not an instrument track in this project (it holds "
                "samples, automation or a beat). Give the new track another name."
            )
        if change.instrument:
            _set_instrument(track, change.instrument, font, done)
        done.changed.append(change.name)
    else:
        track = ET.SubElement(container, "track", muted="0", type=INSTRUMENT_TRACK,
                              name=change.name, solo="0")
        track.append(_instrument_track())
        _set_instrument(track, change.instrument or "piano", font, done)
        done.created.append(change.name)
    _set_mix(track, change)
    if change.notes is not None:
        notes = score.parse(change.notes)
        _set_notes(track, change.name, notes)
        done.notes += len(notes)


def _apply_kit(container: ET.Element, change: TrackChange, done: Applied) -> None:
    for track in _tracks_named(container, change.name):
        container.remove(track)
    had = {_drum_of(track, change.name): track for track in _kit_tracks(container, change.name)}
    if change.notes is None:
        if not had:
            raise ProjectError(f"`{change.name}` is a new drum track, so it needs `notes`.")
        for track in had.values():
            _set_mix(track, change)
        done.changed.append(change.name)
        return
    notes = score.parse(change.notes, drums=frozenset(DRUMS))
    by_drum: dict[str, list[score.Note]] = {}
    for note in notes:
        by_drum.setdefault(note.drum, []).append(note)
    for drum, track in had.items():
        if drum not in by_drum:
            container.remove(track)
    for drum, drum_notes in by_drum.items():
        track = had.get(drum)
        if track is None:
            track = ET.SubElement(container, "track", muted="0", type=INSTRUMENT_TRACK,
                                  name=f"{change.name} · {drum}", solo="0")
            track.append(_instrument_track())
            _replace_instrument(track, _sample_player(DRUMS[drum]))
        _set_mix(track, change)
        _set_notes(track, drum, drum_notes)
    done.notes += len(notes)
    (done.changed if had else done.created).append(change.name)


def _set_instrument(track: ET.Element, name: str, font: str, done: Applied) -> None:
    wanted = name.strip().lower().replace(" ", "_").replace("-", "_")
    if wanted in SYNTHS:
        _replace_instrument(track, _synth(SYNTHS[wanted]), SYNTHS[wanted])
        return
    program = GM.get(wanted)
    if program is None and wanted.startswith("gm:") and wanted[3:].isdigit():
        program = int(wanted[3:])
    if program is None or not 0 <= program <= 127:
        raise ProjectError(f"`{name}` is not an instrument. Known: {instruments()}.")
    if font:
        _replace_instrument(track, _soundfont_player(font, program))
        return
    # No SoundFont on this machine: a synth of about the same register stands in, and the
    # result says so, so nobody takes a saw wave for a violin.
    if 32 <= program <= 39:
        stand_in = "bass"
    elif 48 <= program <= 55 or 88 <= program <= 95:
        stand_in = "pad"
    elif program <= 31 or 104 <= program <= 115:
        stand_in = "pluck"
    else:
        stand_in = "lead"
    _replace_instrument(track, _synth(SYNTHS[stand_in]), SYNTHS[stand_in])
    done.fallbacks.append(f"{name} -> {stand_in}")


def _replace_instrument(
    track: ET.Element, instrument: ET.Element, synth: Synth | None = None
) -> None:
    holder = track.find("instrumenttrack")
    if holder is None:
        holder = _instrument_track()
        track.insert(0, holder)
    old = holder.find("instrument")
    if old is not None:
        holder.remove(old)
    holder.insert(0, instrument)
    envelope = holder.find("eldata/elvol")
    if envelope is not None:
        shape = synth or Synth((0, 0, 0), (0, 0, 0), (0, 0, 0), sustain=1.0, decay=0.0)
        # `amt` 1 turns the volume envelope on; 0 leaves the instrument's own.
        envelope.set("amt", "1" if synth else "0")
        for attribute, value in (
            ("att", shape.attack), ("dec", shape.decay),
            ("sustain", shape.sustain), ("rel", shape.release), ("hold", 0.0 if synth else 0.5),
        ):
            envelope.set(attribute, _number(value))


def _set_mix(track: ET.Element, change: TrackChange) -> None:
    holder = track.find("instrumenttrack")
    if holder is None:
        return
    if change.volume is not None:
        holder.set("vol", str(change.volume))
    if change.pan is not None:
        holder.set("pan", str(change.pan))


def _set_notes(track: ET.Element, name: str, notes: Sequence[score.Note]) -> None:
    for pattern in track.findall("pattern"):
        track.remove(pattern)
    if not notes:
        return
    pattern = ET.SubElement(track, "pattern", steps="16", muted="0", type=MELODY_PATTERN,
                            name=name, pos="0")
    for note in sorted(notes, key=lambda n: (n.pos, n.key)):
        ET.SubElement(pattern, "note", key=str(note.key), vol=str(note.volume), pan="0",
                      len=str(note.length), pos=str(note.pos))


# -- describing it ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackView:
    name: str
    instrument: str
    volume: str
    pan: str
    muted: bool
    notes: str
    count: int


@dataclass(frozen=True, slots=True)
class ProjectView:
    tempo: str
    signature: str
    #: Bars the song lasts, rounded up.
    bars: int
    seconds: float
    tracks: tuple[TrackView, ...]
    #: Tracks this tool does not write: samples, beats, automation.
    other: tuple[str, ...]


def describe(root: ET.Element) -> ProjectView:
    """The project as the model reads it: each track's notes in `score`'s text."""
    container = root.find("song/trackcontainer")
    assert container is not None
    tracks: list[TrackView] = []
    other: list[str] = []
    kits: dict[str, list[tuple[str, ET.Element]]] = {}
    for track in container.findall("track"):
        name = track.get("name", "")
        if track.get("type") != INSTRUMENT_TRACK:
            other.append(name)
            continue
        kit, _, drum = name.partition(" · ")
        if drum in DRUMS and _source(track) == DRUMS[drum]:
            kits.setdefault(kit, []).append((drum, track))
            continue
        notes = _notes(track)
        holder = track.find("instrumenttrack")
        tracks.append(TrackView(
            name, _instrument_name(track),
            holder.get("vol", "100") if holder is not None else "100",
            holder.get("pan", "0") if holder is not None else "0",
            track.get("muted") == "1", score.write(notes), len(notes),
        ))
    for kit, members in kits.items():
        notes = [
            score.Note(note.pos, note.length, score.BASE_KEY, note.volume, drum)
            for drum, track in members
            for note in _notes(track)
        ]
        holder = members[0][1].find("instrumenttrack")
        tracks.append(TrackView(
            kit, DRUM_KIT,
            holder.get("vol", "100") if holder is not None else "100",
            holder.get("pan", "0") if holder is not None else "0",
            all(track.get("muted") == "1" for _drum, track in members),
            score.write(notes), len(notes),
        ))
    tempo, numerator, denominator = _head(root)
    ticks = length(root)
    bar = score.TICKS_PER_BEAT * 4 * numerator // max(1, denominator)
    return ProjectView(
        tempo=str(tempo), signature=f"{numerator}/{denominator}",
        bars=-(-ticks // bar) if bar else 0,
        seconds=ticks / score.TICKS_PER_BEAT * 60 / tempo if tempo else 0.0,
        tracks=tuple(tracks), other=tuple(other),
    )


def length(root: ET.Element) -> int:
    """The tick the last note of any track ends on."""
    end = 0
    for track in root.iterfind("song/trackcontainer/track"):
        for pattern in track.findall("pattern"):
            start = max(0, _int(pattern.get("pos")))
            for note in pattern.findall("note"):
                end = max(end, start + _int(note.get("pos")) + max(0, _int(note.get("len"))))
    return end


def _notes(track: ET.Element) -> list[score.Note]:
    notes: list[score.Note] = []
    for pattern in track.findall("pattern"):
        start = max(0, _int(pattern.get("pos")))
        for note in pattern.findall("note"):
            span = _int(note.get("len"))
            key = _int(note.get("key"))
            if span <= 0:
                # A beat pattern's step: LMMS stores it with a negative length and plays
                # it for a sixteenth.
                span = score.TICKS_PER_BEAT // 4
            if 0 <= key < score.KEYS:
                notes.append(score.Note(
                    start + _int(note.get("pos")), span, key,
                    min(score.MAX_VOLUME, max(0, _int(note.get("vol"), 100))),
                ))
    return notes


def _head(root: ET.Element) -> tuple[int, int, int]:
    head = root.find("head")

    def read(name: str, default: int) -> int:
        if head is None:
            return default
        child = head.find(name)
        raw = head.get(name) or (child.get("value") if child is not None else None)
        return _int(raw, default) or default

    return read("bpm", 120), read("timesig_numerator", 4), read("timesig_denominator", 4)


def _instrument_name(track: ET.Element) -> str:
    instrument = track.find("instrumenttrack/instrument")
    if instrument is None:
        return "?"
    kind = instrument.get("name", "?")
    inner = instrument.find(kind)
    if kind == "sf2player" and inner is not None:
        program = _int(inner.get("patch"))
        known = next((name for name, number in GM.items() if number == program), "")
        return known or f"gm:{program}"
    if kind == "audiofileprocessor" and inner is not None:
        return f"sample {inner.get('src', '')}"
    return kind


def _source(track: ET.Element) -> str:
    player = track.find("instrumenttrack/instrument/audiofileprocessor")
    return player.get("src", "") if player is not None else ""


def _tracks_named(container: ET.Element, name: str) -> list[ET.Element]:
    folded = name.casefold()
    return [t for t in container.findall("track") if t.get("name", "").casefold() == folded]


def _kit_tracks(container: ET.Element, name: str) -> list[ET.Element]:
    prefix = f"{name} · ".casefold()
    return [
        track for track in container.findall("track")
        if track.get("name", "").casefold().startswith(prefix)
        and track.get("name", "")[len(prefix):] in DRUMS
    ]


def _is_kit(container: ET.Element, name: str) -> bool:
    return bool(_kit_tracks(container, name)) and not _tracks_named(container, name)


def _drum_of(track: ET.Element, kit: str) -> str:
    return track.get("name", "")[len(kit) + 3 :]


def _names(container: ET.Element) -> list[str]:
    names: list[str] = []
    for track in container.findall("track"):
        kit, _, drum = track.get("name", "").partition(" · ")
        name = kit if drum in DRUMS else track.get("name", "")
        if name not in names:
            names.append(name)
    return names


# -- LMMS's own elements --------------------------------------------------------------

_ENVELOPE = (
    'lspd_denominator="4" sustain="0.5" pdel="0" userwavefile="" dec="0.5" lamt="0" '
    'latt="0" rel="0.1" amt="0" x100="0" att="0" lpdel="0" hold="0.5" lspd_syncmode="0" '
    'lshp="0" lspd="0.1" ctlenvamt="0" lspd_numerator="4"'
)


#: Levels that put the three kinds of instrument near one another in a render. Measured
#: with LMMS 1.2.2 and FluidR3_GM on 2026-11-18: at LMMS's own defaults a SoundFont piano
#: came out about a tenth as loud as a saw chord or the kick, and five tracks clipped.
_SYNTH_LEVEL = 0.5
_FONT_GAIN = "3"
_SAMPLE_AMP = "60"


def _instrument_track() -> ET.Element:
    """An instrument track's holder as LMMS 1.2.2's default template writes it, without
    its instrument."""
    return ET.fromstring(
        '<instrumenttrack pan="0" fxch="0" usemasterpitch="1" pitchrange="1" pitch="0" '
        f'basenote="{score.BASE_KEY}" vol="100">'
        '<eldata fres="0.5" ftype="0" fcut="14000" fwet="0">'
        f"<elvol {_ENVELOPE}/><elcut {_ENVELOPE}/><elres {_ENVELOPE}/></eldata>"
        '<chordcreator chord="0" chordrange="1" chord-enabled="0"/>'
        '<arpeggiator arptime="100" arprange="1" arpskip="0" arptime_denominator="4" '
        'arptime_syncmode="0" arpmode="0" arpcycle="0" arp-enabled="0" arp="0" '
        'arptime_numerator="4" arpdir="0" arpmiss="0" arpgate="100"/>'
        '<midiport inputcontroller="0" fixedoutputvelocity="-1" inputchannel="0" '
        'outputcontroller="0" writable="0" outputchannel="1" fixedinputvelocity="-1" '
        'fixedoutputnote="-1" outputprogram="1" basevelocity="63" readable="0"/>'
        '<fxchain numofeffects="0" enabled="0"/>'
        "</instrumenttrack>"
    )


def _synth(synth: Synth) -> ET.Element:
    instrument = ET.Element("instrument", name="tripleoscillator")
    attributes: dict[str, str] = {}
    for index in range(3):
        attributes.update({
            f"wavetype{index}": str(synth.waves[index]),
            f"coarse{index}": str(synth.octaves[index]),
            f"vol{index}": str(round(synth.volumes[index] * _SYNTH_LEVEL)),
            f"pan{index}": "0",
            # The second oscillator sits a few cents off the first, so two saws do not
            # cancel into one.
            f"finel{index}": "4" if index == 1 else "0",
            f"finer{index}": "-4" if index == 1 else "0",
            f"phoffset{index}": "0",
            f"stphdetun{index}": "0",
            f"userwavefile{index}": "",
        })
    # Modulation 2 is "mix": each oscillator is added to the next (TripleOscillator.h).
    attributes.update({"modalgo1": "2", "modalgo2": "2", "modalgo3": "2"})
    ET.SubElement(instrument, "tripleoscillator", attributes)
    return instrument


def _soundfont_player(font: str, program: int) -> ET.Element:
    instrument = ET.Element("instrument", name="sf2player")
    ET.SubElement(instrument, "sf2player", {
        "src": font, "patch": str(program), "bank": "0", "gain": _FONT_GAIN,
        "reverbOn": "0", "reverbRoomSize": "0.2", "reverbDamping": "0",
        "reverbWidth": "0.5", "reverbLevel": "0.9",
        "chorusOn": "0", "chorusNum": "3", "chorusLevel": "2", "chorusSpeed": "0.3",
        "chorusDepth": "8",
    })
    return instrument


def _sample_player(source: str) -> ET.Element:
    instrument = ET.Element("instrument", name="audiofileprocessor")
    ET.SubElement(instrument, "audiofileprocessor", {
        "src": source, "reversed": "0", "looped": "0", "amp": _SAMPLE_AMP, "sframe": "0",
        "eframe": "1", "lframe": "0", "stutter": "0", "interp": "1",
    })
    return instrument


def _int(raw: str | None, default: int = 0) -> int:
    try:
        return int(float(raw)) if raw not in (None, "") else default
    except ValueError:
        return default


def _number(value: float) -> str:
    return f"{value:g}"
