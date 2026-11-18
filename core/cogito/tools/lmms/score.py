"""The notes of one track, as the text the model writes and reads back.

One token per note, in order, each starting where the last one ended:

    C4:1 E4:0.5 G4:0.5 | [C4 E4 G4]:2 r:1 A#3:1/2 >0 C2:4

`PITCH:BEATS[:VOLUME]`. A pitch is a note name with its octave (C4 is middle C, `#` and
`b` for accidentals), a MIDI number, or on a drum track the name of a drum. Beats are
quarter notes, as a decimal or a fraction; left out, one beat. Volume is 0-200, 100 when
left out. `[A B C]:2` is a chord, `r:1` a rest, `|` is ignored, and `>BEAT` moves to an
absolute beat, which is how a second voice is written under the first.

Positions are LMMS ticks: 48 to a quarter note (`DefaultTicksPerTact` 192 in 4/4, LMMS
1.2.2 include/MidiTime.h). A key is LMMS's: octave * 12 + tone, so A4 is 57 and a key is
its MIDI number minus 12 (include/Note.h).

The format is ours. ABC notation is what models trained on music use (MuPT, arXiv
2404.06393; ChatMusician, arXiv 2402.16153, which counts it at 38% of MIDI's tokens, their
number), but ABC states pitch through a key signature and a default length that hold for
the whole tune, and one wrong header changes every note. Here each token carries its own
pitch and length. Which a general model writes with fewer wrong notes is not measured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

TICKS_PER_BEAT = 48
#: LMMS has ten octaves of keys (`NumKeys`), and a key is its MIDI number minus this.
KEYS = 120
MIDI_OFFSET = 12
#: The key that sounds a sample, or a synth, at its own pitch (`DefaultKey`, A4).
BASE_KEY = 57
MAX_VOLUME = 200
#: A song longer than this is a mistake in a length, not a song: 4 hours at 120 bpm.
MAX_BEATS = 28_800

_TONES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_PITCH = re.compile(r"^([a-gA-G])([#b♯♭]{0,2})(-?\d)$")
_REST = {"r", "rest", "-", "z"}
_TOKEN = re.compile(r"\[[^\]]*\]\S*|\S+")


class ScoreError(ValueError):
    """What is wrong with a track's notes, worded for the model."""


@dataclass(frozen=True, slots=True)
class Note:
    #: Ticks from the start of the track.
    pos: int
    #: Ticks.
    length: int
    #: LMMS key, or the drum's name on a drum track.
    key: int
    volume: int = 100
    drum: str = ""


def parse(text: str, *, drums: frozenset[str] = frozenset()) -> list[Note]:
    """The notes ``text`` describes. ``drums`` are the names a drum track accepts as
    pitches; empty for a pitched track."""
    notes: list[Note] = []
    at = 0
    for number, token in enumerate(_TOKEN.findall(text.replace("|", " ")), start=1):
        where = f"token {number} (`{_short(token)}`)"
        if token.startswith(">"):
            at = _ticks(token[1:], where, allow_zero=True)
            continue
        if token.startswith("["):
            close = token.find("]")
            if close < 0:
                raise ScoreError(f"{where}: a chord needs its closing `]`.")
            names = token[1:close].replace(",", " ").replace("+", " ").split()
            rest = token[close + 1 :]
            if rest and not rest.startswith(":"):
                raise ScoreError(f"{where}: after `]` comes `:BEATS`, e.g. `[C4 E4 G4]:2`.")
            fields = rest[1:].split(":") if rest else []
            if not names:
                raise ScoreError(f"{where}: the chord is empty.")
        else:
            head, *fields = token.split(":")
            names = [head]
        if len(fields) > 2:
            raise ScoreError(f"{where}: write `PITCH:BEATS` or `PITCH:BEATS:VOLUME`.")
        length = _ticks(fields[0], where) if fields and fields[0] else TICKS_PER_BEAT
        volume = _volume(fields[1], where) if len(fields) == 2 else 100
        if len(names) == 1 and names[0].lower() in _REST:
            at += length
            continue
        for name in names:
            if drums:
                drum = name.lower()
                if drum not in drums:
                    raise ScoreError(
                        f"{where}: `{name}` is not a drum of this kit. Its drums: "
                        f"{', '.join(sorted(drums))}."
                    )
                notes.append(Note(at, length, BASE_KEY, volume, drum))
            else:
                notes.append(Note(at, length, key_of(name, where), volume))
        at += length
        if at > MAX_BEATS * TICKS_PER_BEAT:
            raise ScoreError(f"{where}: the track is past {MAX_BEATS:,} beats. Check the lengths.")
    return notes


def key_of(name: str, where: str = "the pitch") -> int:
    """The LMMS key of a note name (`C4`, `F#3`, `Bb2`) or a MIDI number."""
    if name.isdigit():
        key = int(name) - MIDI_OFFSET
    else:
        match = _PITCH.match(name)
        if match is None:
            raise ScoreError(
                f"{where}: `{_short(name)}` is not a pitch. Write a note name with its octave "
                "(`C4` is middle C, `F#3`, `Bb2`), a MIDI number, or `r` for a rest."
            )
        letter, accidentals, octave = match.groups()
        shift = sum(1 if mark in "#♯" else -1 for mark in accidentals)
        key = int(octave) * 12 + _TONES[letter.lower()] + shift
    if not 0 <= key < KEYS:
        raise ScoreError(f"{where}: `{name}` is outside the keyboard (C0 to B9).")
    return key


def name_of(key: int) -> str:
    return f"{_NAMES[key % 12]}{key // 12}"


def write(notes: list[Note]) -> str:
    """``notes`` as text `parse` reads back to the same notes."""
    groups: dict[tuple[int, int, int], list[Note]] = {}
    for note in sorted(notes, key=lambda n: (n.pos, n.length, n.key, n.drum)):
        groups.setdefault((note.pos, note.length, note.volume), []).append(note)
    # Notes that sound over one another go to separate voices, each written whole and
    # after the one before it, so a line reads as a line.
    voices: list[tuple[list[str], int]] = []
    for (pos, length, volume), group in groups.items():
        names = [note.drum or name_of(note.key) for note in group]
        head = names[0] if len(names) == 1 else "[" + " ".join(names) + "]"
        token = f"{head}:{_beats(length)}" + (f":{volume}" if volume != 100 else "")
        for index, (tokens, end) in enumerate(voices):
            if end <= pos:
                if pos > end:
                    tokens.append(f"r:{_beats(pos - end)}")
                tokens.append(token)
                voices[index] = (tokens, pos + length)
                break
        else:
            lead = [f"r:{_beats(pos)}"] if pos else []
            voices.append(([*lead, token], pos + length))
    return " >0 ".join(" ".join(tokens) for tokens, _end in voices)


def _ticks(text: str, where: str, *, allow_zero: bool = False) -> int:
    try:
        beats = Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise ScoreError(
            f"{where}: `{_short(text)}` is not a number of beats. Write a decimal or a "
            "fraction: `1`, `0.5`, `3/4`."
        ) from None
    ticks = round(beats * TICKS_PER_BEAT)
    if beats < 0 or beats > MAX_BEATS or (ticks < 1 and not (allow_zero and beats == 0)):
        raise ScoreError(
            f"{where}: {text} beats is out of range. The shortest note is 1/48 of a beat."
        )
    return ticks


def _volume(text: str, where: str) -> int:
    if not text.isdigit() or int(text) > MAX_VOLUME:
        raise ScoreError(
            f"{where}: volume is a whole number from 0 to {MAX_VOLUME}; 100 is normal."
        )
    return int(text)


def _beats(ticks: int) -> str:
    beats = Fraction(ticks, TICKS_PER_BEAT)
    if beats.denominator == 1:
        return str(beats.numerator)
    if beats.denominator in (2, 4, 8):
        return str(float(beats))
    return f"{beats.numerator}/{beats.denominator}"


def _short(text: str) -> str:
    return text if len(text) <= 40 else text[:37] + "..."
