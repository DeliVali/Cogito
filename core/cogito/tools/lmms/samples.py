"""Recorded sound in a project: a file played as an instrument, cut into slices, or
placed on the timeline.

LMMS has two things for it (1.2.2 sources, read 2026-11-18):

- AudioFileProcessor, an instrument that plays one file at the pitch of each note.
  `sframe` and `eframe` are where it starts and ends, as fractions of the file;
  `reversed`, `looped` and `amp` are what they say
  (plugins/audio_file_processor/audio_file_processor.cpp). The track's `basenote` is the
  note that plays the file as recorded.
- a sample track (track type 2), which holds clips: `<sampletco pos len src>` in ticks
  (src/tracks/SampleTrack.cpp). A clip plays at the file's own speed, whatever the tempo.

A slice is the first, used many times: one LMMS track per slice, as the drum kit is one
track per drum, so a break cut in eight is eight notes `s1` to `s8` the model reorders.

A file's `src` is its path, or a path under LMMS's samples folder (`drums/kick01.ogg`),
which LMMS resolves itself (`SampleBuffer::tryToMakeAbsolute`). How long a file lasts is
read from a WAV's own header, and through the person's ffmpeg for anything else.
"""

from __future__ import annotations

import contextlib
import math
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

#: What LMMS 1.2.2 loads as a sample (libsndfile, Ogg Vorbis and its own DrumSynth).
SUFFIXES = (".wav", ".ogg", ".flac", ".aiff", ".aif", ".au", ".mp3", ".ds")
MAX_SLICES = 32
MAX_CLIPS = 256
_KEYS = ("file", "root", "start", "end", "reverse", "loop", "clips", "slices")
_ALIASES = {"file": ("path", "src", "audio")}
_SHOWN = 40

HELP = (
    "A track's `sample` uses recorded sound. A path alone plays the file as an "
    "instrument, or an object:\n"
    "  file: an audio file in the project, anywhere on the machine, or in LMMS's library "
    "(e.g. `drums/kick01.ogg`). start, end: seconds, to use a part of it. reverse, loop: "
    "true or false. root: the note that plays it as recorded (default C4); other notes "
    "play it higher or lower, and faster or slower.\n"
    "  slices: a number to cut it in that many equal parts, or a list of seconds where "
    "each part starts. The track's notes are then `s1`, `s2`, ... like a drum kit's, "
    "to reorder a break or a phrase.\n"
    "  clips: a list of beats where the file is placed on the timeline as it is, e.g. a "
    "loop or a recording. Such a track has no notes, and the file plays at its own speed: "
    "the result says how many beats it lasts at the project's tempo.\n"
    'Examples: {"file": "samples/vox.wav", "root": "A3", "start": 0.4, "end": 1.9}; '
    '{"file": "break.wav", "slices": 8} with notes `s1:0.5 s3:0.5 s2:0.5 s8:0.5`; '
    '{"file": "loop.ogg", "clips": [0, 8, 16]}.'
)


class SampleError(ValueError):
    """What is wrong with a sample, worded for the model."""


@dataclass(frozen=True, slots=True)
class Sampling:
    """What resolving a sample needs from outside: where relative paths start, LMMS's
    sample folders, and how long a file lasts (None when that cannot be read)."""

    root: Path
    library: tuple[Path, ...] = ()
    seconds: Callable[[Path], float | None] = lambda path: None


@dataclass(frozen=True, slots=True)
class Plan:
    #: "play", "slices" or "clips".
    kind: str
    #: What goes in the project: an absolute path, or one under LMMS's samples folder.
    src: str
    name: str
    seconds: float | None
    start: float = 0.0
    end: float = 1.0
    reverse: bool = False
    loop: bool = False
    #: LMMS key that plays the file as recorded.
    base: int = 48
    #: Beats.
    clips: tuple[float, ...] = ()
    #: Start and end of each slice, as fractions of the file.
    cuts: tuple[tuple[float, float], ...] = ()


def library_dirs(binary: str = "") -> tuple[Path, ...]:
    """LMMS's sample folders: the person's own first, then the ones it ships."""
    dirs = [Path.home() / "lmms" / "samples"]
    if binary:
        dirs.append(Path(binary).resolve().parent.parent / "share" / "lmms" / "samples")
    dirs += [Path("/usr/share/lmms/samples"), Path("/usr/local/share/lmms/samples")]
    seen: list[Path] = []
    for folder in dirs:
        if folder.is_dir() and folder not in seen:
            seen.append(folder)
    return tuple(seen)


def seconds_of(path: Path) -> float | None:
    """How long an audio file lasts, or None when it cannot be read here."""
    if path.suffix.lower() == ".wav":
        with contextlib.suppress(wave.Error, OSError, EOFError), wave.open(str(path)) as file:
            if file.getframerate() > 0:
                return file.getnframes() / file.getframerate()
    try:
        from cogito.tools.songstudy import audio
    except ImportError:
        return None
    ffmpeg = audio.find()
    if not ffmpeg:
        return None
    rate = 8000
    decoded = audio.decode(ffmpeg, path, rate, start=0.0, seconds=3600.0)
    if decoded.status != 0 or not decoded.samples:
        return None
    return len(decoded.samples) / 2 / rate


def plan(raw: Any, sampling: Sampling, key_of: Callable[[str], int]) -> Plan:
    """What a track's `sample` asks for, checked."""
    if isinstance(raw, str):
        raw = {"file": raw}
    if not isinstance(raw, Mapping):
        raise SampleError("`sample` is a path or an object like {\"file\": \"loop.wav\"}.")
    fields = dict(raw)
    for name, others in _ALIASES.items():
        for other in others:
            if name not in fields and other in fields:
                fields[name] = fields.pop(other)
    unknown = [key for key in fields if key not in _KEYS]
    if unknown:
        raise SampleError(
            f"`sample` has no field `{unknown[0]}`. Its fields: {', '.join(_KEYS)}. Call "
            "lmms with action sounds for what each takes."
        )
    file = fields.get("file")
    if not isinstance(file, str) or not file.strip():
        raise SampleError("`sample` needs `file`, the audio file to use.")
    src, path = _resolve(file.strip(), sampling)
    seconds = sampling.seconds(path)
    start, end = _part(fields, seconds, path.name)
    if "clips" in fields and "slices" in fields:
        raise SampleError("`sample` takes `clips` or `slices`, not both: make two tracks.")
    common = {
        "src": src, "name": path.name, "seconds": seconds, "start": start, "end": end,
        "reverse": fields.get("reverse") is True,
    }
    if "clips" in fields:
        for key in ("start", "end", "reverse", "loop", "root"):
            if key in fields:
                raise SampleError(
                    f"A clip is the whole file as recorded, so `{key}` does not apply. To "
                    "use a part of it, play it as an instrument (leave `clips` out)."
                )
        return Plan(kind="clips", clips=_clips(fields["clips"]), **common)
    if "slices" in fields:
        return Plan(kind="slices", cuts=_cuts(fields["slices"], start, end, seconds, path.name),
                    **common)
    root = fields.get("root", "C4")
    try:
        base = key_of(str(root))
    except ValueError:
        raise SampleError(
            f"`root` is the note that plays the file as recorded, e.g. `C4`; got `{root}`."
        ) from None
    return Plan(kind="play", loop=fields.get("loop") is True, base=base, **common)


def player(src: str, *, start: float = 0.0, end: float = 1.0, reverse: bool = False,
           loop: bool = False, amp: str = "100") -> ET.Element:
    """An AudioFileProcessor on ``src``, playing the file from ``start`` to ``end``."""
    if reverse:
        # LMMS turns the whole file around first and then takes the part, so the part
        # is counted from the other end (found by rendering: a reversed half second came
        # out as the file's silent tail).
        start, end = 1.0 - end, 1.0 - start
    instrument = ET.Element("instrument", name="audiofileprocessor")
    ET.SubElement(instrument, "audiofileprocessor", {
        "src": src, "reversed": "1" if reverse else "0", "looped": "1" if loop else "0",
        "amp": amp, "sframe": _text(start), "eframe": _text(end), "lframe": _text(start),
        "stutter": "0", "interp": "1",
    })
    return instrument


def describe(found: Plan, tempo: int) -> str:
    """One line for the write result: what the file is and how it was used."""
    if found.seconds is None:
        long = "its length could not be read here"
    else:
        beats = found.seconds * tempo / 60
        long = f"{found.seconds:.2f} s, {beats:.2f} beats at {tempo} bpm"
    if found.kind == "clips":
        line = f"{found.name} ({long}) placed at beat{'s' if len(found.clips) != 1 else ''} "
        line += ", ".join(f"{beat:g}" for beat in found.clips) + "."
        if found.seconds:
            whole = max(1, round(found.seconds * tempo / 60))
            fits = whole * 60 / found.seconds
            if abs(fits - tempo) > 0.5:
                line += (
                    f" It plays at its own speed: at {fits:.1f} bpm it would last exactly "
                    f"{whole} beat{'s' if whole != 1 else ''}."
                )
        return line
    if found.kind == "slices":
        each = ""
        if found.seconds is not None:
            spans = [(b - a) * found.seconds for a, b in found.cuts]
            each = f", {min(spans):.2f} to {max(spans):.2f} s each"
        return (
            f"{found.name} ({long}) cut into {len(found.cuts)} slices{each}: notes "
            f"`s1` to `s{len(found.cuts)}`."
        )
    part = "" if (found.start, found.end) == (0.0, 1.0) else " (a part of it)"
    return f"{found.name} ({long}) played as an instrument{part}."


def listing(query: str, library: Sequence[Path]) -> list[str]:
    """Lines for `sounds`: the sample library's folders, or the files matching words."""
    files: dict[str, Path] = {}
    for folder in library:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUFFIXES:
                files.setdefault(path.relative_to(folder).as_posix(), path)
    if not files:
        return []
    words = [word.lower() for word in query.split()]
    if not words:
        counts: dict[str, int] = {}
        for name in files:
            counts[name.split("/")[0]] = counts.get(name.split("/")[0], 0) + 1
        return [
            "Samples in LMMS's library, by folder (use one as a track's `sample`, e.g. "
            "`drums/kick01.ogg`; call sounds with words to see files): "
            + ", ".join(f"{folder} ({count})" for folder, count in counts.items())
        ]
    matching = [name for name in files if all(word in name.lower() for word in words)]
    if not matching:
        return []
    more = len(matching) - _SHOWN
    return [
        f"Samples matching `{query}` (use one as a track's `sample`): "
        + ", ".join(matching[:_SHOWN]) + (f", and {more} more" if more > 0 else "")
    ]


# -- reading the fields ---------------------------------------------------------------


def _resolve(file: str, sampling: Sampling) -> tuple[str, Path]:
    """The project's `src` for a file and where it is on disk."""
    given = Path(file).expanduser()
    if given.suffix.lower() not in SUFFIXES:
        raise SampleError(
            f"`{file}` is not an audio file LMMS plays as a sample. It plays: "
            f"{', '.join(suffix[1:] for suffix in SUFFIXES)}."
        )
    path = given if given.is_absolute() else sampling.root / given
    if path.is_file():
        return str(path.resolve()), path.resolve()
    if not given.is_absolute():
        for folder in sampling.library:
            if (folder / given).is_file():
                # LMMS finds a path under its samples folder by itself.
                return given.as_posix(), folder / given
    raise SampleError(
        f"The sample `{file}` does not exist, in the project or in LMMS's library. Look "
        "with glob, or call lmms with action sounds and words to find one in the library."
    )


def _part(fields: Mapping[str, Any], seconds: float | None, name: str) -> tuple[float, float]:
    """`start` and `end` in seconds as fractions of the file."""
    if "start" not in fields and "end" not in fields:
        return 0.0, 1.0
    if not seconds:
        raise SampleError(
            f"`start` and `end` are in seconds, and how long {name} lasts could not be read "
            "here (it is not a WAV and ffmpeg is not installed). Leave them out or use a WAV."
        )
    start = _number(fields.get("start", 0.0), "start")
    end = _number(fields.get("end", seconds), "end")
    if not 0 <= start < end <= seconds + 0.01:
        raise SampleError(
            f"`start` and `end` must be inside the file, start before end: {name} lasts "
            f"{seconds:.2f} s."
        )
    return start / seconds, min(1.0, end / seconds)


def _clips(raw: Any) -> tuple[float, ...]:
    values = [raw] if isinstance(raw, (int, float, str)) and not isinstance(raw, bool) else raw
    if not isinstance(values, list) or not values or len(values) > MAX_CLIPS:
        raise SampleError("`clips` is a list of beats where the file starts, e.g. [0, 8, 16].")
    beats = sorted({_number(value, "clips") for value in values})
    if beats[0] < 0:
        raise SampleError("`clips` are beats from the start of the song: none is negative.")
    return tuple(beats)


def _cuts(
    raw: Any, start: float, end: float, seconds: float | None, name: str
) -> tuple[tuple[float, float], ...]:
    if isinstance(raw, bool):
        raise SampleError("`slices` is a number of equal parts, or a list of seconds.")
    if isinstance(raw, (int, float, str)):
        count = _number(raw, "slices")
        if count != int(count) or not 2 <= count <= MAX_SLICES:
            raise SampleError(f"`slices` as a number is 2 to {MAX_SLICES} equal parts.")
        step = (end - start) / int(count)
        return tuple((start + index * step, start + (index + 1) * step)
                     for index in range(int(count)))
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SLICES:
        raise SampleError(
            f"`slices` is a number of equal parts, or a list of up to {MAX_SLICES} seconds "
            "where each slice starts."
        )
    if not seconds:
        raise SampleError(
            f"`slices` as seconds needs the length of {name}, which could not be read here. "
            "Give a number of equal parts instead."
        )
    places = [_number(value, "slices") / seconds for value in raw]
    if places != sorted(set(places)) or places[0] < start - 1e-9 or places[-1] >= end:
        raise SampleError(
            f"`slices` are seconds in rising order inside the part used: {name} lasts "
            f"{seconds:.2f} s."
        )
    return tuple(zip(places, [*places[1:], end], strict=True))


def _number(raw: Any, what: str) -> float:
    if isinstance(raw, bool):
        raise SampleError(f"`{what}` takes numbers.")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SampleError(f"`{what}` takes numbers; got `{raw}`.") from None
    if not math.isfinite(value):
        raise SampleError(f"`{what}` takes numbers; got `{raw}`.")
    return value


def _text(value: float) -> str:
    return f"{value:.7f}".rstrip("0").rstrip(".") or "0"
