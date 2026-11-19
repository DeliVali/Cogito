"""What a project's notes add up to, and what looks wrong in them.

The model writes notes as text and cannot hear the result. This reads the notes back as
music: the key they fit, a chord for every bar, the drum pattern, each track's range,
and the mistakes that can be found without hearing anything: a track that drifts off
the chords because a length was miscounted, a bass that starts a bar outside its chord,
two tracks a semitone apart, an instrument written outside what it can play, notes
outside the key, bars where nothing plays. It is counted from the notes, so it is exact
about what was written and silent about how it sounds.

The key, chords and groove come out in the words `study_song` uses for a recording, so
a piece can be set beside its reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from cogito.tools.songstudy import harmony

from . import mmp, score

_STEP = score.TICKS_PER_BEAT // 4
#: A track whose bars mostly miss their chord is looked at when this many do.
_MISSES = 3
#: The least share of a bar's sound that must be chord notes for the bar to fit.
_FIT = 0.25
#: How much better a moved track must fit, as a share of its sound, to call it moved.
_GAIN = 0.2
#: A moved track must then fit this well, over at least this many bars: a sparse line
#: of a few notes fits better at some shift by chance (seen on a bell line of eight notes).
_MOVED_FIT = 0.7
_MOVED_BARS = 4
#: Tried on a track that misses its chords: a miscounted length moves everything after
#: it by this much.
_SHIFTS = (-96, -48, -24, 24, 48, 96)
_SHOWN = 4

#: Lowest and highest written note of the instruments a call may name (standard
#: orchestration ranges, professional players; Adler, "The Study of Orchestration").
RANGES = {
    "violin": ("G3", "A7"), "viola": ("C3", "E6"), "cello": ("C2", "C6"),
    "contrabass": ("E1", "G4"), "harp": ("C1", "G7"), "timpani": ("D2", "C4"),
    "piccolo": ("D5", "C8"), "flute": ("C4", "D7"), "oboe": ("A#3", "A6"),
    "clarinet": ("E3", "C7"), "bassoon": ("A#1", "E5"), "soprano_sax": ("G#3", "E6"),
    "alto_sax": ("C#3", "A5"), "tenor_sax": ("G#2", "E5"), "trumpet": ("F#3", "D6"),
    "french_horn": ("B1", "F5"), "trombone": ("E2", "F5"), "tuba": ("D1", "F4"),
    "piano": ("A0", "C8"), "bright_piano": ("A0", "C8"), "guitar": ("E2", "E6"),
    "nylon_guitar": ("E2", "B5"), "electric_guitar": ("E2", "E6"),
    "bass_guitar": ("E1", "G4"), "acoustic_bass": ("E1", "G4"),
    "glockenspiel": ("G5", "C8"), "vibraphone": ("F3", "F6"), "marimba": ("C2", "C7"),
    "celesta": ("C4", "C8"), "choir": ("E2", "C6"),
}
#: The names a kit's notes may carry: drums, and slices of a file.
_PIECES = frozenset(mmp.DRUMS) | frozenset(f"s{index}" for index in range(1, 100))
_BANDS = {
    "low": ("kick", "kick_hard", "kick_soft"),
    "mid": ("snare", "snare_acoustic", "snare_electro", "rim", "sidestick", "clap", "tom"),
    "high": ("hat", "openhat", "pedalhat", "ride", "crash", "shaker", "clave"),
}


@dataclass(frozen=True, slots=True)
class Voice:
    name: str
    instrument: str
    notes: tuple[score.Note, ...]
    kit: bool
    #: Whether its notes count as harmony: not for noise or a played recording.
    pitched: bool = True


@dataclass(frozen=True, slots=True)
class Review:
    bars: int
    key: str = ""
    key_other: str = ""
    key_fit: float = 0.0
    #: One chord a bar, "-" where no pitched note sounds.
    chords: tuple[str, ...] = ()
    #: Per band, one character a step of the bar.
    groove: dict[str, str] = field(default_factory=dict)
    tracks: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def review(view: mmp.ProjectView) -> Review:
    numerator, _, denominator = view.signature.partition("/")
    bar = score.TICKS_PER_BEAT * 4 * int(numerator or 4) // max(1, int(denominator or 4))
    voices = [
        Voice(
            track.name, track.instrument,
            tuple(score.parse(
                track.notes,
                drums=_PIECES if track.instrument in (mmp.DRUM_KIT, mmp.SLICES) else frozenset(),
            )),
            track.instrument in (mmp.DRUM_KIT, mmp.SLICES),
            track.pitched,
        )
        for track in view.tracks if not track.muted
    ]
    pitched = [voice for voice in voices if not voice.kit and voice.pitched and voice.notes]
    kits = [voice for voice in voices if voice.kit and voice.notes]
    count = max(view.bars, 1)
    problems: list[str] = []
    for voice in voices:
        if not voice.notes:
            problems.append(f"{voice.name} has no notes.")
    key = other = ""
    fit = 0.0
    chords: tuple[str, ...] = ()
    if pitched:
        weights = [0.0] * 12
        for voice in pitched:
            for note in voice.notes:
                weights[note.key % 12] += note.length
        key, other, fit = harmony.key_of(weights)
        chords = _chords(pitched, bar, count, key)
        problems += _drift(pitched, bar, count, key)
        problems += _bass(pitched, bar, count, key)
        problems += _clashes(pitched, bar)
        problems += _ranges(pitched, bar)
        problems += _outside(pitched, bar, key, fit)
    problems += _overrun([voice for voice in voices if voice.notes], bar)
    problems += _short(voices, bar, count)
    problems += _doubled(voices, bar)
    problems += _silent(voices, bar, count)
    return Review(
        bars=count, key=key, key_other=other, key_fit=fit, chords=chords,
        groove=_groove(kits, bar, count) if kits else {},
        tracks=tuple(_line(voice, bar) for voice in voices),
        problems=tuple(problems),
    )


def text(found: Review) -> list[str]:
    """The review as lines for the model."""
    lines = []
    if found.key:
        unsure = "unsure; " if found.key_fit < 0.6 else ""
        lines.append(f"Key: {found.key} ({unsure}next best {found.key_other}).")
        root, minor = harmony.tonic(found.key)
        lines.append("Chords, a bar each: " + harmony.progression(found.chords))
        lines.append("In the key: " + harmony.progression(
            [harmony.degrees(chord, root, minor) for chord in found.chords]
        ))
    if found.groove:
        lines.append("Drums, one bar, 4 steps a beat (X in most bars, x in some):")
        lines.append(f"  low  {_spaced(found.groove['low'])}   kicks")
        lines.append(f"  mid  {_spaced(found.groove['mid'])}   snares, claps, toms")
        lines.append(f"  high {_spaced(found.groove['high'])}   hats and cymbals")
    lines.append("Tracks:")
    lines += [f"- {line}" for line in found.tracks]
    lines.append("")
    if found.problems:
        lines.append(f"Looks wrong ({len(found.problems)}); fix what was not meant:")
        lines += [f"{index}. {problem}" for index, problem in enumerate(found.problems, 1)]
    else:
        lines.append(
            "Nothing looks wrong to these checks (key, chords, bass, clashes, ranges, "
            "gaps). They read the notes and cannot hear: ask the person how it sounds."
        )
    return lines


def against(found: Review, tempo: int, study: Any, reference: Path) -> list[str]:
    """Where a piece is like a studied recording, and whether it is too like it.

    A tempo, a key and a chord loop are each shared by many songs. All three the same at
    once is the recording's own backing, so that is what is called too close. A melody
    cannot be compared, since the study holds none, and a drum pattern read from sound
    is too rough to decide by."""
    lines = [f"Against {reference} (its values are estimates from the sound):"]
    theirs = [study.tempo, *([study.tempo_other] if study.tempo_other else [])]
    near = min(theirs, key=lambda value: abs(value - tempo))
    same_tempo = abs(near - tempo) <= 0.04 * near
    lines.append(
        f"- Tempo: {tempo} here, {round(near, 1):g} there: "
        f"{'the same' if same_tempo else 'different'}."
    )
    if found.key:
        lines.append(
            f"- Key: {found.key} here, {study.key} there: "
            f"{'the same' if found.key == study.key else 'different'}."
        )
    longest = max(study.sections, key=lambda section: section.bars, default=None)
    same_loop = False
    if found.key and longest is not None:
        mine, their = harmony.loop(found.chords), harmony.loop(longest.chords)
        if mine and their:
            here = [harmony.degrees(bar, *harmony.tonic(found.key)) for bar in mine[0]]
            there = [harmony.degrees(bar, *harmony.tonic(study.key)) for bar in their[0]]
            same_loop = len(here) > 1 and _turned(here, there)
            lines.append(
                f"- Chord loop: | {' | '.join(here)} | here, | {' | '.join(there)} | there: "
                f"{'the same' if same_loop else 'different'}."
            )
        else:
            lines.append("- Chord loop: one of the two repeats no loop, so they differ.")
    theirs_groove = study.grooves.get(longest.letter, {}) if longest is not None else {}
    if found.groove and theirs_groove:
        lines.append(
            "- Drums there (low band holds the bass too, so compare by eye): low "
            f"{_spaced(theirs_groove.get('low', ''))}, mid {_spaced(theirs_groove.get('mid', ''))}."
        )
    same_key = bool(found.key) and found.key == study.key
    if same_tempo and same_loop and same_key:
        lines.append(
            "Too close: the same tempo, key and chord loop together are that recording's "
            "own backing. Keep the style (the kind of loop, the feel, the sounds) and "
            "change at least the key or the loop."
        )
    elif same_loop:
        lines.append(
            "The chord loop is the reference's, in another key or tempo. A loop is shared "
            "by many songs, but a piece of its own is better served by one of yours: "
            "consider changing a chord. The tune must be yours either way."
        )
    else:
        lines.append(
            "Its own: the chord loop is not the reference's. This compares the backing "
            "only; the tune must be yours too."
        )
    return lines


def _turned(here: list[str], there: list[str]) -> bool:
    """Whether two loops are one loop, started at any of its bars."""
    if len(here) != len(there):
        return False
    return any(here == there[turn:] + there[:turn] for turn in range(len(there)))


# -- harmony --------------------------------------------------------------------------


def _weights(voices: list[Voice], first: int, last: int) -> list[float]:
    """How long each pitch class sounds between two ticks."""
    weights = [0.0] * 12
    for voice in voices:
        for note in voice.notes:
            overlap = min(note.pos + note.length, last) - max(note.pos, first)
            if overlap > 0:
                weights[note.key % 12] += overlap
    return weights


def _chord(weights: list[float], lowest: int | None, key: str) -> str:
    """The triad a bar's pitch classes spell: the one that holds most of the sound, the
    lowest note counting for its root and the key breaking ties."""
    total = sum(weights)
    if total <= 0:
        return "-"
    inside = harmony.scale(key)
    best, best_fit = "-", float("-inf")
    for root in range(12):
        for suffix, third in (("", 4), ("m", 3)):
            tones = (root, (root + third) % 12, (root + 7) % 12)
            held = sum(weights[tone] for tone in tones)
            fit = held - 0.5 * (total - held)
            fit += 0.3 * total if lowest == root else 0.0
            fit += 0.05 * total if all(tone in inside for tone in tones) else 0.0
            if fit > best_fit:
                best, best_fit = harmony.NOTES[root] + suffix, fit
    return best


def _chords(voices: list[Voice], bar: int, count: int, key: str) -> tuple[str, ...]:
    chords = []
    for index in range(count):
        first, last = index * bar, (index + 1) * bar
        sounding = [
            note for voice in voices for note in voice.notes
            if note.pos < first + bar // 2 and note.pos + note.length > first
        ]
        lowest = min(sounding, key=lambda note: note.key).key % 12 if sounding else None
        chords.append(_chord(_weights(voices, first, last), lowest, key))
    return tuple(chords)


def _fits(voice: Voice, chords: tuple[str, ...], bar: int, shift: int = 0) -> list[float | None]:
    """Per bar, the share of the voice's sound that is notes of the bar's chord; None
    where it plays under a beat or the bar has no chord."""
    held = [0.0] * len(chords)
    total = [0.0] * len(chords)
    for note in voice.notes:
        start, end = note.pos + shift, note.pos + shift + note.length
        for index in range(max(0, start // bar), min(len(chords), -(-end // bar))):
            overlap = min(end, (index + 1) * bar) - max(start, index * bar)
            if overlap <= 0 or chords[index] == "-":
                continue
            total[index] += overlap
            if note.key % 12 in harmony.chord_tones(chords[index]):
                held[index] += overlap
    return [
        held[index] / total[index] if total[index] >= score.TICKS_PER_BEAT else None
        for index in range(len(chords))
    ]


def _mean(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) / len(known) if known else None


def _drift(voices: list[Voice], bar: int, count: int, key: str) -> list[str]:
    """Tracks that stop fitting the chords the other tracks spell. When the track fits
    from some bar on only if moved by a beat or two, a length was miscounted there:
    the mistake a long run of notes written as text most often holds. A track that just
    plays outside the chords is named too."""
    problems = []
    for voice in voices:
        others = [other for other in voices if other is not voice]
        if not others:
            continue
        chords = _chords(others, bar, count, key)
        plain = _fits(voice, chords, bar)
        best: tuple[float, int, int] | None = None
        for shift in _SHIFTS:
            moved = _fits(voice, chords, bar, shift)
            for start in range(count):
                after, after_moved = _mean(plain[start:]), _mean(moved[start:])
                before, before_moved = _mean(plain[:start]), _mean(moved[:start])
                if after is None or after_moved is None or after_moved - after < _GAIN:
                    continue
                if after_moved < _MOVED_FIT:
                    continue
                if before is not None and before_moved is not None and before < before_moved:
                    continue
                span = sum(value is not None for value in plain[start:])
                if span >= _MOVED_BARS and (best is None or (after_moved - after) * span > best[0]):
                    best = ((after_moved - after) * span, shift, start)
        if best is not None:
            beats = best[1] / score.TICKS_PER_BEAT
            problems.append(
                f"{voice.name} fits the chords from bar {best[2] + 1} on only when moved "
                f"{abs(beats):g} beat{'s' if abs(beats) != 1 else ''} "
                f"{'later' if best[1] > 0 else 'earlier'}: a length is probably miscounted "
                f"in bar {best[2] + 1} or the one before. Recount the beats there."
            )
            continue
        missed = [index for index, value in enumerate(plain) if value is not None and value < _FIT]
        if len(missed) >= _MISSES:
            problems.append(
                f"{voice.name} mostly plays notes outside the bar's chord in {_bars(missed)} "
                f"({_named(chords, missed)}). Check those bars against the chords."
            )
    return problems


def _overrun(voices: list[Voice], bar: int) -> list[str]:
    """A track that ends a little past a bar line the others end on: its notes add up to
    more beats than the bars hold."""
    ends = {voice.name: max(note.pos + note.length for note in voice.notes)
            for voice in voices if voice.notes}
    on_line = [end for end in ends.values() if end % bar == 0]
    if not on_line:
        return []
    line = max(set(on_line), key=on_line.count)
    problems = []
    for name, end in ends.items():
        over = end - line
        if 0 < over < bar:
            beats = over / score.TICKS_PER_BEAT
            problems.append(
                f"{name} ends {beats:g} beat{'s' if beats != 1 else ''} after bar "
                f"{line // bar}, where the other tracks end: its lengths add up to too many "
                "beats somewhere. Recount its bars."
            )
    return problems


def _bass(voices: list[Voice], bar: int, count: int, key: str) -> list[str]:
    """The lowest track starting a bar on a note that is not in the bar's chord."""
    if len(voices) < 2:
        return []
    low = min(voices, key=lambda voice: median(note.key for note in voice.notes))
    chords = _chords([voice for voice in voices if voice is not low], bar, count, key)
    wrong = []
    for note in low.notes:
        index, offset = divmod(note.pos, bar)
        if offset or index >= len(chords) or chords[index] == "-":
            continue
        if note.key % 12 not in harmony.chord_tones(chords[index]):
            wrong.append(f"{score.name_of(note.key)} under {chords[index]} in bar {index + 1}")
    if not wrong:
        return []
    return [
        f"{low.name} (the lowest track) starts {len(wrong)} bar"
        f"{'s' if len(wrong) != 1 else ''} on a note outside the chord: {_some(wrong)}."
    ]


def _clashes(voices: list[Voice], bar: int) -> list[str]:
    """Notes of two tracks a semitone (or an octave and a semitone) apart, held together
    for at least half a beat."""
    notes = sorted(
        ((note, voice.name) for voice in voices for note in voice.notes),
        key=lambda pair: pair[0].pos,
    )
    found = []
    for index, (note, name) in enumerate(notes):
        end = note.pos + note.length
        for other, other_name in notes[index + 1 :]:
            if other.pos >= end:
                break
            together = min(end, other.pos + other.length) - other.pos
            if other_name != name and abs(other.key - note.key) in (1, 13) and (
                together >= score.TICKS_PER_BEAT // 2
            ):
                found.append(
                    f"{score.name_of(note.key)} ({name}) against "
                    f"{score.name_of(other.key)} ({other_name}) at {_place(other.pos, bar)}"
                )
    if not found:
        return []
    return [f"{len(found)} semitone clash{'es' if len(found) != 1 else ''} between tracks: "
            f"{_some(found)}."]


def _ranges(voices: list[Voice], bar: int) -> list[str]:
    problems = []
    for voice in voices:
        limits = RANGES.get(voice.instrument)
        if limits is None:
            continue
        low, high = score.key_of(limits[0]), score.key_of(limits[1])
        out = [note for note in voice.notes if not low <= note.key <= high]
        if out:
            shown = _some([f"{score.name_of(note.key)} at {_place(note.pos, bar)}" for note in out])
            problems.append(
                f"{voice.name}: {len(out)} note{'s' if len(out) != 1 else ''} outside what a "
                f"{voice.instrument.replace('_', ' ')} plays ({limits[0]} to {limits[1]}): {shown}."
            )
    return problems


def _outside(voices: list[Voice], bar: int, key: str, fit: float) -> list[str]:
    if fit < 0.6:
        return []
    inside = harmony.scale(key)
    problems = []
    for voice in voices:
        out = [note for note in voice.notes if note.key % 12 not in inside]
        if not out:
            continue
        shown = _some(list(dict.fromkeys(
            f"{score.name_of(note.key)} in bar {note.pos // bar + 1}" for note in out
        )))
        share = len(out) / len(voice.notes)
        meaning = (
            "this track may be written in another key"
            if share > 0.3 else "fine as colour, a mistake if not meant"
        )
        problems.append(
            f"{voice.name}: {len(out)} of {len(voice.notes)} notes are outside {key} "
            f"({shown}); {meaning}."
        )
    return problems


# -- the rest -------------------------------------------------------------------------


def _doubled(voices: list[Voice], bar: int) -> list[str]:
    """A track holding one pitch twice at once: the second note cuts the first."""
    problems = []
    for voice in voices:
        ends: dict[tuple[int, str], int] = {}
        places = []
        for note in sorted(voice.notes, key=lambda note: note.pos):
            pitch = (note.key, note.drum)
            if ends.get(pitch, 0) > note.pos:
                places.append(_place(note.pos, bar))
            ends[pitch] = max(ends.get(pitch, 0), note.pos + note.length)
        if places:
            problems.append(
                f"{voice.name} plays a note again while it still sounds, {len(places)} "
                f"time{'s' if len(places) != 1 else ''}: {_some(places)}. Shorten the first "
                "or move the second."
            )
    return problems


def _short(voices: list[Voice], bar: int, count: int) -> list[str]:
    """A track that starts with the song and stops before its half: often a pattern
    written once and never repeated."""
    problems = []
    for voice in voices:
        if not voice.notes or count < 4:
            continue
        first = min(note.pos for note in voice.notes) // bar
        last = -(-max(note.pos + note.length for note in voice.notes) // bar)
        if first == 0 and last * 2 < count:
            problems.append(
                f"{voice.name} stops after bar {last} of {count}. If it should go on, write "
                "its notes for the whole song: nothing repeats by itself."
            )
    return problems


def _silent(voices: list[Voice], bar: int, count: int) -> list[str]:
    filled = [False] * count
    for voice in voices:
        for note in voice.notes:
            for index in range(note.pos // bar, min(count, -(-(note.pos + note.length) // bar))):
                filled[index] = True
    if True not in filled:
        return []
    first, last = filled.index(True), count - 1 - filled[::-1].index(True)
    empty = [index for index in range(first, last + 1) if not filled[index]]
    return [f"Nothing plays in {_bars(empty)}."] if empty else []


def _groove(kits: list[Voice], bar: int, count: int) -> dict[str, str]:
    steps = max(1, bar // _STEP)
    rows = {}
    for band, drums in _BANDS.items():
        hits = [0] * steps
        for voice in kits:
            for note in voice.notes:
                if note.drum in drums and note.pos % _STEP == 0:
                    hits[(note.pos % bar) // _STEP] += 1
        rows[band] = "".join(
            "X" if hit >= 0.6 * count else "x" if hit >= 0.2 * count else "." for hit in hits
        )
    return rows


def _line(voice: Voice, bar: int) -> str:
    if not voice.notes:
        return f"{voice.name} ({voice.instrument}): no notes"
    first = min(note.pos for note in voice.notes) // bar + 1
    last = -(-max(note.pos + note.length for note in voice.notes) // bar)
    span = f"bars {first}-{last}"
    if voice.kit:
        drums = ", ".join(sorted({note.drum for note in voice.notes}))
        return f"{voice.name} (drums: {drums}): {len(voice.notes)} hits, {span}"
    low = score.name_of(min(note.key for note in voice.notes))
    high = score.name_of(max(note.key for note in voice.notes))
    return f"{voice.name} ({voice.instrument}): {len(voice.notes)} notes, {low} to {high}, {span}"


def _place(pos: int, bar: int) -> str:
    index, offset = divmod(pos, bar)
    return f"bar {index + 1} beat {offset / score.TICKS_PER_BEAT + 1:g}"


def _bars(indexes: list[int]) -> str:
    """Bar numbers as runs: `bars 5-8, 12`."""
    runs: list[list[int]] = []
    for index in indexes:
        if runs and index == runs[-1][1] + 1:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    parts = [str(a + 1) if a == b else f"{a + 1}-{b + 1}" for a, b in runs]
    return ("bar " if len(indexes) == 1 else "bars ") + ", ".join(parts)


def _named(chords: tuple[str, ...], indexes: list[int]) -> str:
    return _some([f"bar {index + 1} is {chords[index]}" for index in indexes])


def _some(items: list[str]) -> str:
    more = len(items) - _SHOWN
    return "; ".join(items[:_SHOWN]) + (f"; and {more} more" if more > 0 else "")


def _spaced(row: str) -> str:
    return " ".join(row[index : index + 4] for index in range(0, len(row), 4))
