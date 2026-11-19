"""`lmms`: write music as an LMMS project the person opens, hears and edits.

The model cannot hear. What it can do is write notes, and LMMS, an open-source
workstation with a piano roll, a mixer and synths, reads them from a plain XML file. So the
tool keeps the model on the part it can do and leaves the listening to the person:

    write   make or change a project's tracks (`mmp.apply`): notes as text (`score`)
    read    the project back as that text, with what the person changed in LMMS
    render  LMMS renders it to an audio file, without a window
    open    LMMS opens it, for the person to look at and play

A project the person may have changed is read before its notes are replaced: the tool
remembers what each file looked like when it last read or wrote it, and a call that would
replace notes in a file that has changed since is refused once, with what to do. The file
before each change is kept beside it as `NAME.mmp.bak`, the name LMMS uses for its own
backup.

It writes inside the project folder only, and never under `.cogito`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cogito.config.paths import PROJECT_DIRNAME
from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters
from cogito.tools.project_files import resolve_path, unwritable_message

from . import ladspa, mix, mmp, program, review, samples, score, sounds

_ACTIONS = ("write", "read", "check", "sounds", "render", "open")
_SUFFIXES = (".mmp", ".mmpz")
#: LMMS's own tempo range (include/Song.h: MinTempo 10, MaxTempo 999).
_TEMPO = (10, 999)
_MAX_TRACKS = 64

_DESCRIPTION = (
    "Write music as an LMMS project (.mmp) the person opens in LMMS to hear and edit. You "
    "cannot hear it: write the notes, open or render it, and ask the person how it sounds.\n"
    "- write: create or change the project at `path`. Each item of `tracks` has a `name`, "
    "an `instrument` and `notes`. An existing track is found by name: its notes are "
    "replaced, and everything you do not name (other tracks, effects, what the person "
    "changed) stays. `remove` takes tracks out.\n"
    "- read: the tempo and every track's notes. Do it before changing a project the person "
    "may have edited.\n"
    "- check: reads your notes back as music (key, a chord a bar, drum pattern, each "
    "track's range) and lists what looks wrong: a track off the chords because a length "
    "was miscounted, bass or clashing notes, an instrument out of its range, empty bars. "
    "Run it after writing and fix what it finds before asking the person to listen. With "
    "`reference` (an audio file) it also says where your piece is like that recording "
    "and where it is too close: the piece must be its own.\n"
    "- render: an audio file beside the project. - open: opens it in LMMS; after a later "
    "write the person must reopen it.\n"
    "Notes are tokens in order, each starting where the last ended: `PITCH:BEATS` (a beat "
    "is a quarter note; `C4` is middle C). `[C4 E4 G4]:2` is a chord, `r:1` a rest, `>8` "
    "jumps to beat 8 for a second voice. Example: `E4:1 G4:0.5 A4:0.5 [C4 E4 G4]:2 r:1`.\n"
    "Instruments: `drums` (pitches are drum names: kick, snare, hat, openhat, clap, ride, "
    "crash, tom), e.g. `[kick hat]:0.5 hat:0.5 [snare hat]:0.5 hat:0.5`; synths: "
    + ", ".join(mmp.SYNTHS)
    + "; General MIDI names (piano, organ, guitar, bass_guitar, strings, choir, trumpet, "
    "flute, ...) or `gm:N`; or any preset of LMMS's synths by name.\n"
    "- sounds: lists those presets and LMMS's samples (with `query`, the ones matching, "
    "or one preset's attributes) and says how a track's `sound` designs a synth "
    "(oscillators, envelope, filter, LFO) and how its `sample` plays an audio file, cuts "
    "it into slices or places it as clips, and how `effects` (reverb, delay, eq, "
    "compressor...) and `automation` (a value moving over the song, like a filter "
    "opening) are written. Call it before choosing sounds for a style; it needs no `path`."
)


class LmmsTool:
    def __init__(
        self,
        *,
        runner: program.Runner = program.run,
        find: Any = program.find,
        soundfont: Any = mmp.soundfont,
        measure: Any = None,
        levels: Any = None,
        seconds: Any = samples.seconds_of,
        plugins: Any = None,
    ) -> None:
        #: Injected so a test never starts LMMS or depends on what is installed.
        self._runner = runner
        self._find = find
        self._soundfont = soundfont
        #: Studies a recording for `check`'s `reference`; `study_song`'s own by default.
        self._measure = measure
        #: Says how loud a rendered file is, since the model cannot hear that it clips.
        self._levels = levels or _levels
        #: How long an audio file lasts, for a track's `sample`.
        self._seconds = seconds
        #: The LADSPA plugins installed, read once a session, when first asked for.
        self._scan = plugins or (lambda: ladspa.scan(ladspa.folders(self._find())))
        self._found: dict[str, ladspa.Plugin] | None = None
        #: What each project looked like when this session last read or wrote it.
        self._seen: dict[Path, tuple[int, int]] = {}
        self.spec = ToolSpec(
            name="lmms",
            #: A file the person edits in another program between calls.
            provenance=Provenance(
                reproducibility=Reproducibility.MUTABLE,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=_DESCRIPTION,
            parameters=parameters(
                {
                    "action": {"type": "string", "enum": list(_ACTIONS)},
                    "path": {
                        "type": "string",
                        "description": "The project file, inside the project folder, "
                        "ending in .mmp (e.g. `music/song.mmp`).",
                    },
                    "tempo": {"type": "integer", "description": "Beats per minute (write)."},
                    "time_signature": {
                        "type": "string",
                        "description": "Like `4/4` or `3/4` (write). 4/4 for a new project.",
                    },
                    "tracks": {
                        "type": "array",
                        "description": "The tracks to create or change (write).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "instrument": {"type": "string"},
                                "notes": {"type": "string"},
                                "effects": {
                                    "type": "array",
                                    "description": "The track's effects; see action sounds.",
                                },
                                "sample": {
                                    "description": "An audio file to play, slice or place "
                                    "as clips; see action sounds.",
                                },
                                "sound": {
                                    "type": "object",
                                    "description": "A designed synth; see action sounds.",
                                },
                                "volume": {"type": "integer", "description": "0-200."},
                                "pan": {"type": "integer", "description": "-100 to 100."},
                            },
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    },
                    "remove": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of tracks to take out (write).",
                    },
                    "audio_format": {
                        "type": "string",
                        "enum": list(program.FORMATS),
                        "description": "render: the audio file's format. ogg by default.",
                    },
                    "master_effects": {
                        "type": "array",
                        "description": "write: effects on the whole song.",
                    },
                    "automation": {
                        "type": "array",
                        "description": "write: values that move over the song, each "
                        "{target, points, curve}; see action sounds.",
                    },
                    "query": {
                        "type": "string",
                        "description": "sounds: words of a preset's name, or its name.",
                    },
                    "reference": {
                        "type": "string",
                        "description": "check: an audio file to compare the piece with.",
                    },
                    "open": {
                        "type": "boolean",
                        "description": "write or render: also open the project in LMMS.",
                    },
                },
                required=("action",),
            ),
            aliases={
                "path": ("file", "file_path", "filename", "project"),
                "tempo": ("bpm",),
                "time_signature": ("signature", "meter"),
                "audio_format": ("format",),
                "remove": ("delete", "remove_tracks"),
                "reference": ("reference_song", "compare_with"),
            },
            budget_tokens=2500,
            mutates=True,
            summary="write music as an LMMS project; render or open it",
        )

    def _plugins(self) -> dict[str, ladspa.Plugin]:
        if self._found is None:
            self._found = dict(self._scan())
        return self._found

    # -- the call -------------------------------------------------------------------

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        action = str(arguments.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            return ToolResult.error(
                f"`action` must be one of: {', '.join(_ACTIONS)}.", "invalid_arguments"
            )
        if action == "sounds":
            query = arguments.get("query")
            try:
                asked = query if isinstance(query, str) else ""
                if "ladspa" in asked.lower().split():
                    return ToolResult(content=ladspa.listing(asked, self._plugins()) or (
                        "No LADSPA plugins were found on this machine."
                    ))
                text = sounds.catalog(asked, sounds.preset_dirs(self._find()))
                library = samples.listing(asked, samples.library_dirs(self._find()))
                if asked and library and text.startswith("No preset matches"):
                    text = ""
                extra = (
                    [samples.HELP, mix.HELP, *library, ladspa.listing("", self._plugins())]
                    if not asked else library
                )
                return ToolResult(content="\n\n".join(part for part in (text, *extra) if part))
            except sounds.SoundError as exc:
                return ToolResult.error(str(exc), "bad_preset")
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult.error(
                "`path` is the project file, e.g. `music/song.mmp`.", "invalid_arguments"
            )
        path = resolve_path(context.project_root, raw)
        if path.suffix.lower() not in _SUFFIXES:
            return ToolResult.error(
                f"`path` must end in .mmp (an LMMS project); got `{raw}`. For the audio "
                "file, call render with the project's path.",
                "invalid_arguments",
            )
        if context.cancel.cancelled:
            return ToolResult.error("Stopped before anything was done.", "cancelled")
        if action == "read":
            return self._read(path)
        if action == "check":
            return self._check(path, arguments, context)
        if action == "write":
            return self._write(path, arguments, context)
        if action == "render":
            return self._render(path, arguments, context)
        return self._open(path)

    def _read(self, path: Path) -> ToolResult:
        if not path.is_file():
            return ToolResult.error(
                f"{path} does not exist. Create it with action write.", "not_found"
            )
        try:
            root = mmp.load(path)
        except mmp.ProjectError as exc:
            return ToolResult.error(str(exc), "bad_project")
        self._note_seen(path)
        return ToolResult(content=_described(path, mmp.describe(root)))

    def _check(self, path: Path, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not path.is_file():
            return ToolResult.error(
                f"{path} does not exist. Create it with action write.", "not_found"
            )
        try:
            view = mmp.describe(mmp.load(path))
            found = review.review(view)
        except (mmp.ProjectError, score.ScoreError) as exc:
            return ToolResult.error(str(exc), "bad_project")
        self._note_seen(path)
        lines = [
            f"{path}: {view.tempo} bpm, {view.signature}, {view.bars} bars, "
            f"{_clock(view.seconds)}. Read from the notes, not heard.",
            *review.text(found),
        ]
        raw = arguments.get("reference")
        if isinstance(raw, str) and raw.strip():
            reference = resolve_path(context.project_root, raw)
            if not reference.is_file():
                return ToolResult.error(
                    f"The reference {reference} does not exist. Ask the person where the "
                    "song is, or call check without `reference`.",
                    "not_found",
                )
            measure = self._measure
            if measure is None:
                from cogito.tools.songstudy.tool import measure
            measured = measure(reference, cancel=context.cancel)
            if isinstance(measured, ToolResult):
                return measured
            tempo = int(float(view.tempo or 0))
            lines += ["", *review.against(found, tempo, measured[0], reference)]
        return ToolResult(content="\n".join(lines))

    def _write(self, path: Path, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if (refused := _outside(path, context.project_root)) is not None:
            return refused
        try:
            changes = _track_changes(arguments.get("tracks"))
            remove = _names(arguments.get("remove"))
            tempo, numerator, denominator = _head(arguments)
        except ValueError as exc:
            return ToolResult.error(str(exc), "invalid_arguments")
        existed = path.is_file()
        if not existed and not any(change.notes or _places(change) for change in changes):
            return ToolResult.error(
                f"{path} does not exist yet, so the call must give at least one track with "
                "`notes` (or a `sample` with `clips`).",
                "invalid_arguments",
            )
        master, moves = arguments.get("master_effects"), arguments.get("automation")
        for name in ("master_effects", "automation"):
            given = arguments.get(name)
            if isinstance(given, str) and given.strip().startswith(("[", "{")):
                try:
                    given = json.loads(given)
                except ValueError:
                    return ToolResult.error(f"`{name}` is a list of objects.", "invalid_arguments")
                if name == "master_effects":
                    master = given
                else:
                    moves = given
        nothing = not changes and not remove and tempo is None and numerator is None
        if existed and nothing and master is None and moves is None:
            return ToolResult.error(
                "Nothing to change: give `tracks`, `remove`, `tempo` or `time_signature`.",
                "invalid_arguments",
            )
        if existed and self._seen.get(path) != _stamp(path):
            touches = [c.name for c in changes if c.notes is not None or _places(c)] + remove
            if touches:
                return ToolResult.error(
                    f"{path} exists and was not read in this conversation, or it changed "
                    "since (the person may have edited it in LMMS). Call lmms with action "
                    "read first, then send the change again with what you want kept.",
                    "unread_project",
                )
        try:
            root = mmp.load(path) if existed else mmp.new_project()
            mmp.set_head(root, tempo=tempo, numerator=numerator, denominator=denominator)
            done = mmp.apply(
                root, changes, remove=remove, font=self._soundfont(),
                presets=sounds.preset_dirs(self._find()),
                sampling=samples.Sampling(
                    Path(context.project_root), samples.library_dirs(self._find()),
                    self._seconds,
                ),
                master_effects=master, automation=moves, plugins=self._plugins,
            )
            data = mmp.dump(root, path)
        except (
            mmp.ProjectError, score.ScoreError, sounds.SoundError, samples.SampleError,
            mix.MixError,
        ) as exc:
            return ToolResult.error(str(exc), "invalid_arguments")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if existed:
                backup = path.with_name(path.name + ".bak")
                backup.write_bytes(path.read_bytes())
            scratch = path.with_name(f".{path.name}.cogito-tmp")
            scratch.write_bytes(data)
            os.replace(scratch, path)
        except OSError as exc:
            return ToolResult.error(unwritable_message(path, exc), "unwritable")
        self._note_seen(path)
        view = mmp.describe(root)
        lines = [
            f"{'Changed' if existed else 'Wrote'} {path}: {view.tempo} bpm, "
            f"{view.signature}, {view.bars} bars, {_clock(view.seconds)}, "
            f"{len(view.tracks)} tracks."
        ]
        for label, names in (
            ("New", done.created), ("Changed", done.changed), ("Removed", done.removed)
        ):
            if names:
                lines.append(f"{label}: {', '.join(names)}.")
        lines += done.samples
        if existed:
            lines.append(f"The file as it was is {path.name}.bak.")
        if done.fallbacks:
            lines.append(
                "No General MIDI SoundFont is installed, so these play on a synth, not the "
                f"instrument named: {'; '.join(done.fallbacks)}. Tell the person; installing "
                "one (the `soundfont-fluid` or `fluid-soundfont-gm` package) and writing the "
                "track's instrument again fixes it."
            )
        result = ToolResult(
            content="\n".join(lines),
            metadata={"created": not existed, "tracks": len(view.tracks), "notes": done.notes},
            files_touched=(str(path),),
        )
        if arguments.get("open") is True:
            return _joined(result, self._open(path))
        lines.append("Render it (action render) or open it in LMMS (action open).")
        return ToolResult(
            content="\n".join(lines), metadata=result.metadata, files_touched=result.files_touched
        )

    def _render(self, path: Path, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if not path.is_file():
            return ToolResult.error(
                f"{path} does not exist. Create it with action write.", "not_found"
            )
        if (refused := _outside(path, context.project_root)) is not None:
            return refused
        fmt = str(arguments.get("audio_format") or "ogg").strip().lower().lstrip(".")
        if fmt not in program.FORMATS:
            return ToolResult.error(
                f"`audio_format` must be one of: {', '.join(program.FORMATS)}.", "invalid_arguments"
            )
        binary = self._find()
        if not binary:
            return ToolResult.error(program.MISSING, "no_lmms")
        output = path.with_suffix(f".{fmt}")
        before = _stamp(output)
        ran = self._runner(
            program.render_command(binary, path, output, fmt),
            timeout=program.RENDER_SECONDS, cancel=context.cancel,
        )
        if ran.status == -1:
            return ToolResult.error("The render was stopped.", "cancelled")
        if ran.status == -2:
            return ToolResult.error(
                f"LMMS did not finish rendering in {int(program.RENDER_SECONDS)} s and was "
                f"stopped. Its last output:\n{ran.output or '(none)'}",
                "render_timeout",
            )
        after = _stamp(output)
        if ran.status != 0 or after is None or after == before or after[1] == 0:
            return ToolResult.error(
                f"LMMS did not write {output} (exit status {ran.status}). Its last output:"
                f"\n{ran.output or '(none)'}",
                "render_failed",
            )
        try:
            seconds = mmp.describe(mmp.load(path)).seconds
        except mmp.ProjectError:
            seconds = 0.0
        loud = self._levels(output, context.cancel)
        result = ToolResult(
            content=(
                f"Rendered {output} ({after[1]:,} bytes, about {_clock(seconds)})."
                + (f" {loud}" if loud else "")
                + " You cannot hear it: ask the person to play it and say what to change."
            ),
            metadata={"output": str(output), "bytes": after[1]},
            files_touched=(str(output),),
        )
        if arguments.get("open") is True:
            return _joined(result, self._open(path))
        return result

    def _open(self, path: Path) -> ToolResult:
        if not path.is_file():
            return ToolResult.error(
                f"{path} does not exist. Create it with action write.", "not_found"
            )
        binary = self._find()
        if not binary:
            return ToolResult.error(program.MISSING, "no_lmms")
        if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
            return ToolResult.error(
                "There is no desktop session to open LMMS in. The project is at "
                f"{path}; the person can open it themselves.",
                "no_desktop",
            )
        ran = self._runner(program.open_command(binary, path), timeout=3.0, detach=True)
        if ran.status not in (None, 0):
            return ToolResult.error(
                f"LMMS closed at once (exit status {ran.status}). Its output:\n"
                f"{ran.output or '(none)'}",
                "open_failed",
            )
        return ToolResult(
            content=(
                f"LMMS is opening {path} for the person. What they change and save there is "
                "in the file: read it (action read) before you change it again."
            )
        )

    def _note_seen(self, path: Path) -> None:
        stamp = _stamp(path)
        if stamp is not None:
            self._seen[path] = stamp


# -- arguments ------------------------------------------------------------------------

_TRACK_ALIASES = {
    "instrument": ("preset", "voice", "program"),
    "notes": ("score", "pattern", "sequence", "melody"),
    "name": ("track", "title"),
    "volume": ("vol",),
    "pan": ("panning",),
}


def _track_changes(raw: Any) -> list[mmp.TrackChange]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("`tracks` is a list of objects, each with `name`, `instrument` "
                             "and `notes`.") from None
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list) or len(raw) > _MAX_TRACKS:
        raise ValueError(f"`tracks` is a list of at most {_MAX_TRACKS} objects.")
    changes: list[mmp.TrackChange] = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"Track {number} must be an object with `name` and `notes`.")
        fields = dict(item)
        for name, others in _TRACK_ALIASES.items():
            for other in others:
                if name not in fields and other in fields:
                    fields[name] = fields.pop(other)
        name = fields.get("name")
        if not isinstance(name, str) or not name.strip() or " · " in name or len(name) > 80:
            raise ValueError(f"Track {number} needs a short `name` (and no ` · ` in it).")
        notes = fields.get("notes")
        if isinstance(notes, list):
            notes = " ".join(str(part) for part in notes)
        if notes is not None and not isinstance(notes, str):
            raise ValueError(f"Track `{name}`: `notes` is text, e.g. `C4:1 E4:1 G4:2`.")
        sound = fields.get("sound")
        if isinstance(sound, str) and sound.strip().startswith("{"):
            try:
                sound = json.loads(sound)
            except ValueError:
                raise ValueError(
                    f"Track `{name}`: `sound` is an object; see action sounds."
                ) from None
        if isinstance(sound, str) and "instrument" not in fields:
            # A name where an object goes: the model meant the instrument.
            fields["instrument"], sound = sound, None
        if sound is not None and not isinstance(sound, Mapping):
            raise ValueError(f"Track `{name}`: `sound` is an object; see action sounds.")
        instrument = fields.get("instrument") or ""
        if not isinstance(instrument, str):
            raise ValueError(f"Track `{name}`: `instrument` is a name. Known: {mmp.instruments()}.")
        changes.append(mmp.TrackChange(
            name=name.strip(), instrument=instrument.strip().lower(), notes=notes,
            volume=_ranged(fields.get("volume"), 0, 200, f"Track `{name}`: `volume`"),
            pan=_ranged(fields.get("pan"), -100, 100, f"Track `{name}`: `pan`"),
            sound=sound,
            sample=fields.get("sample"),
            effects=_listed(fields.get("effects"), name),
        ))
    return changes


def _listed(raw: Any, name: str) -> Any:
    """A track's `effects`, read from JSON text where a model sent it so."""
    if isinstance(raw, str) and raw.strip().startswith(("[", "{")):
        try:
            return json.loads(raw)
        except ValueError:
            raise ValueError(f"Track `{name}`: `effects` is a list of objects.") from None
    return raw


def _places(change: mmp.TrackChange) -> bool:
    """Whether a track's `sample` places clips, which is content without notes."""
    return isinstance(change.sample, Mapping) and "clips" in change.sample


def _names(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ValueError("`remove` is a list of track names.")
    return [name.strip() for name in raw if name.strip()]


def _head(arguments: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    tempo = _ranged(arguments.get("tempo"), *_TEMPO, "`tempo`")
    signature = arguments.get("time_signature")
    if signature in (None, ""):
        return tempo, None, None
    top, _, bottom = str(signature).partition("/")
    if not (top.strip().isdigit() and bottom.strip().isdigit()):
        raise ValueError("`time_signature` is two numbers, like `4/4` or `6/8`.")
    numerator, denominator = int(top), int(bottom)
    if not 1 <= numerator <= 32 or denominator not in (1, 2, 4, 8, 16, 32):
        raise ValueError("`time_signature`: 1-32 over 1, 2, 4, 8, 16 or 32.")
    return tempo, numerator, denominator


def _ranged(raw: Any, low: int, high: int, what: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise ValueError(f"{what} is a whole number from {low} to {high}.") from None
    if isinstance(raw, bool) or not low <= value <= high:
        raise ValueError(f"{what} is a whole number from {low} to {high}.")
    return value


# -- results --------------------------------------------------------------------------


def _described(path: Path, view: mmp.ProjectView) -> str:
    lines = [
        f"{path}: {view.tempo} bpm, {view.signature}, {view.bars} bars, "
        f"{_clock(view.seconds)}, {len(view.tracks)} tracks."
    ]
    for track in view.tracks:
        level = "".join(
            f", {label} {value}"
            for label, value, default in (("volume", track.volume, "100"), ("pan", track.pan, "0"))
            if value != default
        )
        lines.append(
            f"\n## {track.name} ({track.instrument}{level}{', muted' if track.muted else ''}), "
            f"{track.count} notes\n{track.notes or '(no notes)'}"
        )
        if track.effects:
            lines.append(f"Effects: {track.effects}")
    if view.master_effects:
        lines.append(f"\nEffects on the whole song: {view.master_effects}")
    if view.automation:
        lines.append("\nAutomation:\n" + "\n".join(f"- {line}" for line in view.automation))
    if view.other:
        lines.append(
            "\nTracks this tool leaves as they are (samples, beats, automation): "
            + ", ".join(name or "(unnamed)" for name in view.other)
        )
    return "\n".join(lines)


def _outside(path: Path, project_root: Path) -> ToolResult | None:
    """A refusal when ``path`` is not a file this tool may write, else None."""
    try:
        resolved = path.resolve()
        project = Path(project_root).resolve()
    except OSError as exc:
        return ToolResult.error(unwritable_message(path, exc), "unwritable")
    if project not in resolved.parents:
        return ToolResult.error(
            f"{path} is outside the project folder ({project}). The lmms tool writes "
            "inside it only, e.g. `music/song.mmp`.",
            "outside_project",
        )
    if PROJECT_DIRNAME in resolved.relative_to(project).parts:
        return ToolResult.error(
            f"{path} is inside {PROJECT_DIRNAME}, which holds the session's own record. "
            "Choose another folder, e.g. `music/`.",
            "outside_project",
        )
    return None


def _levels(path: Path, cancel: Any = None) -> str:
    """How loud a rendered file is, in a sentence, or "" where it cannot be measured
    (no ffmpeg, no numpy). A mix that clips or is silent is a mistake the model cannot
    hear and the notes do not show."""
    try:
        import numpy as np

        from cogito.tools.songstudy import audio
    except ImportError:
        return ""
    ffmpeg = audio.find()
    if not ffmpeg:
        return ""
    decoded = audio.decode(ffmpeg, path, 22_050, start=0.0, seconds=audio.MAX_SECONDS,
                           cancel=cancel)
    if decoded.status != 0 or not decoded.samples:
        return ""
    samples = np.abs(np.frombuffer(decoded.samples, dtype="<i2").astype(np.float32)) / 32768.0
    peak = float(samples.max())
    clipped = float((samples >= 0.97).mean())
    if peak < 0.01:
        return "It is silent: check that the tracks have notes and instruments that sound."
    if clipped >= 0.001:
        return (
            f"It is too loud: {clipped:.1%} of it is at full scale and distorts. Lower the "
            "loudest tracks' `volume` and render again."
        )
    if peak >= 0.99:
        return (
            "Its level touches full scale without distorting much: lower the loudest "
            "track's `volume` a little to leave room."
        )
    if peak < 0.2:
        return f"It is quiet (its peak is {peak:.0%} of full scale): raise the tracks' `volume`."
    return f"Its level is fine (peak {peak:.0%} of full scale, nothing clips)."


def _joined(first: ToolResult, second: ToolResult) -> ToolResult:
    """``first`` with what opening the project said after it."""
    return ToolResult(
        content=f"{first.content}\n{second.content}",
        metadata=first.metadata,
        files_touched=first.files_touched,
    )


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def _clock(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"
