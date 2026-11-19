"""`study_song`: how a recording is built, as text the model writes a new piece from.

The model cannot hear. Given an audio file, the tool measures its tempo, key, the chords
of each section, which sections repeat, the drum pattern on a sixteenth-note grid and a
few words about the sound (`analyze.py`), and returns them as text. It does not return
the melody or the words: it is for learning a style, not for reproducing a song.

The numbers are estimates and the result says so. numpy is imported when the tool runs,
never when Cogito starts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters
from cogito.tools.project_files import resolve_path

from . import audio, harmony

_DESCRIPTION = (
    "Study a recording (mp3, wav, ogg, flac, m4a...) to learn its style: tempo, key, the "
    "chords of each section, which sections repeat, the drum pattern on a 16-step grid, "
    "and how bright and busy it sounds. You cannot hear: this is how you find out how a "
    "song the person gives you is built, so you can write a new one in that style. It "
    "returns no melody and no words, and the new piece must be its own: use the tempo, "
    "the feel, the kind of chord loop and the groove, not the song's own tune. The values "
    "are estimates from the sound; say so when you report them. Up to 8 minutes a call; "
    "`start` and `seconds` choose a part."
)


class StudySongTool:
    def __init__(
        self,
        *,
        find: Callable[[], str] = audio.find,
        decode: Callable[..., audio.Decoded] = audio.decode,
    ) -> None:
        #: Injected so a test needs no ffmpeg.
        self._find = find
        self._decode = decode
        self.spec = ToolSpec(
            name="study_song",
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=_DESCRIPTION,
            parameters=parameters(
                {
                    "path": {"type": "string", "description": "The audio file."},
                    "start": {
                        "type": "number",
                        "description": "Seconds into the file to start at. Default 0.",
                    },
                    "seconds": {
                        "type": "number",
                        "description": "How many seconds to study. Default: to the end.",
                    },
                },
                ["path"],
            ),
            aliases={
                "path": ("file", "file_path", "filename", "song", "audio"),
                "start": ("from", "offset"),
                "seconds": ("duration", "length"),
            },
            budget_tokens=1800,
            summary="study a recording's tempo, key, chords, sections and groove",
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult.error("`path` is the audio file to study.", "invalid_arguments")
        path = resolve_path(context.project_root, raw)
        if path.suffix.lower() not in audio.SUFFIXES:
            return ToolResult.error(
                f"`{raw}` is not an audio file this tool reads. It reads: "
                f"{', '.join(suffix[1:] for suffix in audio.SUFFIXES)}. For an LMMS project "
                "use lmms with action read.",
                "invalid_arguments",
            )
        if not path.is_file():
            return ToolResult.error(
                f"{path} does not exist. Ask the person where the song is, or look with glob.",
                "not_found",
            )
        start = _number(arguments.get("start"), 0.0)
        seconds = _number(arguments.get("seconds"), audio.MAX_SECONDS)
        if start is None or seconds is None or start < 0 or seconds <= 0:
            return ToolResult.error(
                "`start` and `seconds` are numbers of seconds, e.g. start 60, seconds 90.",
                "invalid_arguments",
            )
        seconds = min(seconds, audio.MAX_SECONDS)
        measured = measure(
            path, start=start, seconds=seconds, cancel=context.cancel,
            find=self._find, decode=self._decode,
        )
        if isinstance(measured, ToolResult):
            return measured
        found, more = measured
        return ToolResult(content=_report(path, found, start, more))


def measure(
    path: Path, *, start: float = 0.0, seconds: float = audio.MAX_SECONDS,
    cancel: Any = None, find: Callable[[], str] = audio.find,
    decode: Callable[..., audio.Decoded] = audio.decode,
) -> tuple[Any, bool] | ToolResult:
    """The study of ``seconds`` of an audio file and whether the file goes on after it,
    or the error to return."""
    ffmpeg = find()
    if not ffmpeg:
        return ToolResult.error(
            "ffmpeg is not installed, so the recording cannot be read here. It was not "
            "installed for the person: tell them to install it (Arch: `sudo pacman -S "
            "ffmpeg`; Debian/Ubuntu: `sudo apt install ffmpeg`; Fedora: `sudo dnf "
            "install ffmpeg`), then call again.",
            "no_ffmpeg",
        )
    try:
        import numpy as np

        from . import analyze
    except ImportError:
        return ToolResult.error(
            "This Cogito was installed without numpy, which the study needs. Tell the "
            "person; nothing was read.",
            "no_numpy",
        )
    # One second more than asked for, to know whether the file goes on past it.
    decoded = decode(ffmpeg, path, analyze.SR, start=start, seconds=seconds + 1.0, cancel=cancel)
    if decoded.status == -1 or (cancel is not None and cancel.cancelled):
        return ToolResult.error("Stopped by the person before the study ended.", "cancelled")
    if decoded.status == -2:
        return ToolResult.error(
            f"ffmpeg did not finish reading {path} in {int(audio.DECODE_SECONDS)} s and "
            "was stopped. Study a shorter part with `start` and `seconds`.",
            "decode_timeout",
        )
    if decoded.status != 0 or not decoded.samples:
        return ToolResult.error(
            f"ffmpeg could not read {path} as audio"
            + (f" (exit status {decoded.status})" if decoded.status else "")
            + (f". Its last output:\n{decoded.output}" if decoded.output else ".")
            + "\nCheck that it is an audio file and that `start` is inside it.",
            "unreadable_audio",
        )
    samples = np.frombuffer(decoded.samples, dtype="<i2").astype(np.float32) / 32768.0
    limit = int(seconds * analyze.SR)
    try:
        return analyze.study(samples[:limit]), samples.size > limit
    except analyze.StudyError as exc:
        return ToolResult.error(f"{path}: {exc}", "no_study")


def _number(raw: Any, default: float) -> float | None:
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _report(path: Path, found: Any, start: float, more: bool) -> str:
    span = f"{_clock(start)}-{_clock(start + found.seconds)}" if start else _clock(found.seconds)
    lines = [
        f"{path}: studied {span}"
        + (", and the file goes on after it (call again with `start`)." if more else "."),
        "Estimates from the sound, not a score. No melody or words are given: write your own.",
        "",
        f"Tempo: {_bpm(found.tempo)} bpm"
        + (f" (could also be felt at {_bpm(found.tempo_other)})" if found.tempo_other else "")
        + f", {found.beats_per_bar}/4. Feel: {_feel(found.swing)}.",
        f"Key: {found.key}"
        + (" (unsure" if found.key_fit < 0.6 else " (")
        + f"{'; ' if found.key_fit < 0.6 else ''}next best {found.key_other}).",
        f"Sound: {_brightness(found.brightness_hz)}, {_busy(found.onsets_per_second)} "
        f"({found.onsets_per_second:.1f} attacks a second).",
        "",
        "Form: " + " ".join(
            f"{section.letter}({section.bars})" for section in found.sections
        ) + "  (letter = section, sections with the same letter sound alike; number = bars)",
    ]
    tonic, minor = harmony.tonic(found.key)
    shown: set[str] = set()
    for section in found.sections:
        if section.letter in shown:
            continue
        shown.add(section.letter)
        lines.append("")
        lines.append(
            f"## Section {section.letter}: bars {section.first_bar}-"
            f"{section.first_bar + section.bars - 1}, {section.level}"
        )
        lines.append("Chords: " + harmony.progression(section.chords))
        degrees = harmony.progression(
            [harmony.degrees(bar, tonic, minor) for bar in section.chords]
        )
        lines.append("In the key: " + degrees)
        groove = found.grooves.get(section.letter, {})
        if groove:
            lines.append("Groove, one bar, 4 steps a beat (X strong, x light, . nothing):")
            lines.append(f"  low  {_spaced(groove['low'])}   kick and bass")
            lines.append(f"  mid  {_spaced(groove['mid'])}   snare, clap, other attacks")
            lines.append(f"  high {_spaced(groove['high'])}   hats and cymbals")
    return "\n".join(lines)


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _bpm(tempo: float) -> str:
    return str(round(tempo)) if abs(tempo - round(tempo)) < 0.3 else f"{tempo:.1f}"


def _feel(swing: float | None) -> str:
    if swing is None:
        return "no steady off-beat to tell straight from swung"
    if swing >= 0.58:
        return f"swung (the off-beat eighth falls late, at {swing:.2f} of the beat)"
    return "straight"


def _brightness(hz: float) -> str:
    if hz < 1200:
        return "dark"
    if hz < 2200:
        return "warm"
    return "bright" if hz < 3200 else "very bright"


def _busy(rate: float) -> str:
    return "sparse" if rate < 2 else "moderate" if rate < 5 else "busy"


def _spaced(row: str) -> str:
    return " ".join(row[index : index + 4] for index in range(0, len(row), 4))
