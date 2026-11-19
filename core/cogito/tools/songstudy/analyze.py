"""What a recording's style is made of, measured from its samples.

Tempo and beats, bar lines, key, a chord for every bar, the sections and which of them
repeat, the drum pattern of each section on a sixteenth-note grid, and a few words about
the sound. It does not transcribe a melody or words: the point is how a song is built,
not its notes.

The methods are the plain signal ones, written here on numpy alone:

- onsets are the positive change of a log-magnitude spectrogram, in three bands;
- tempo is the lag that best explains the onset curve's autocorrelation, under a prior
  centred on 120 bpm, and beats follow by dynamic programming (Ellis, "Beat Tracking by
  Dynamic Programming", 2007);
- key is the Krumhansl-Kessler profile that correlates best with the pitch-class energy
  (Krumhansl, "Cognitive Foundations of Musical Pitch", 1990);
- a chord is the major or minor triad whose template fits a beat's pitch classes best,
  smoothed over beats. Template matching on chroma is what Chordino does, measured at
  about 80% chord symbol recall with a far better chroma than this one (the figure is
  from an ISMIR 2015 paper, read 2026-11-18; theirs, not reproduced here);
- sections come from a checkerboard kernel over the bars' self-similarity (Foote,
  "Automatic Audio Segmentation Using a Measure of Audio Novelty", 2000).

librosa 1.0 (2026-08-11) implements all of these and more. It is not a dependency: it
brings numba and scipy for what is a few hundred lines here. How often this code is right
on real songs is not measured; the lab checks it on songs rendered from known notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import harmony

SR = 22_050
N_FFT = 2048
HOP = 512
FPS = SR / HOP
#: The bass is read from the signal at an eighth of the rate, where one bin is 1.3 Hz:
#: at the full rate a bin is 10.8 Hz and two bass notes share it.
_BASS_FACTOR = 8
#: Shorter than this there are too few bars to say anything about.
MIN_SECONDS = 8.0

NOTES = harmony.NOTES
_MAJOR = np.array(harmony.MAJOR_PROFILE)
_MINOR = np.array(harmony.MINOR_PROFILE)
_BANDS = ("low", "mid", "high")
#: Hz. Low holds the kick and the bass, mid the snare and clap among other attacks, high
#: the hats and cymbals.
_BAND_EDGES = {"low": (30.0, 150.0), "mid": (200.0, 2500.0), "high": (6000.0, 11000.0)}
_TEMPO_RANGE = (50.0, 210.0)
#: What a chord change costs against the fit of a triad, so one odd beat does not make one.
_CHANGE_COST = 0.12
_BASS_WEIGHT = 0.25
#: How far two stretches of bars must differ to be two sections (see `_boundaries`).
_CHANGE_FLOOR = 0.6
#: Within eight bars of either end only the four-bar comparison exists, and half of an
#: eight-bar chord loop looks like a change to it: there a change must be this much plainer.
_EDGE_TRUST = 0.4


class StudyError(ValueError):
    """Why a recording could not be studied, worded for the model."""


@dataclass(frozen=True, slots=True)
class Section:
    letter: str
    #: 1-based.
    first_bar: int
    bars: int
    #: "full", "medium" or "quiet", against the loudest section.
    level: str
    #: One entry per bar: its chords in order, e.g. "Am" or "Am F".
    chords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Study:
    seconds: float
    tempo: float
    #: The same pulse at half or double speed, when that reading is nearly as good.
    tempo_other: float | None
    beats_per_bar: int
    key: str
    key_other: str
    #: Correlation with the key's profile, 0-1.
    key_fit: float
    #: Where the off-beat eighth falls in the beat (0.5 is straight), or None.
    swing: float | None
    brightness_hz: float
    onsets_per_second: float
    sections: tuple[Section, ...]
    #: Per section letter, per band, one character a step: X strong, x present, . silent.
    grooves: dict[str, dict[str, str]] = field(default_factory=dict)


def study(samples: np.ndarray) -> Study:
    """Study mono samples at `SR`, as floats in -1..1."""
    y = np.asarray(samples, dtype=np.float32)
    if y.size < MIN_SECONDS * SR:
        raise StudyError(
            f"The recording is under {int(MIN_SECONDS)} seconds, too short to find bars in."
        )
    if float(np.abs(y).max()) < 1e-4:
        raise StudyError("The recording is silent.")
    spec = _spectrogram(y, N_FFT, HOP)
    freqs = np.arange(spec.shape[1]) * SR / N_FFT
    bands = {
        name: _flux(spec[:, (freqs >= low) & (freqs < high)])
        for name, (low, high) in _BAND_EDGES.items()
    }
    total = _flux(spec)
    bpm, other = _tempo(total)
    beats = _on_the_kick(_beats(total, 60.0 * FPS / bpm), bands["low"])
    if len(beats) < 8:
        raise StudyError("No steady beat was found in the recording.")

    chroma = _beat_mean(_chroma(spec, freqs, 200.0, 2000.0), beats)
    bass_spec, bass_freqs = _bass(y)
    bass = _beat_mean(_chroma(bass_spec, bass_freqs, 35.0, 130.0), beats)
    loud = _beat_mean(np.sqrt((spec ** 2).mean(axis=1, keepdims=True)), beats)[:, 0]
    chords = _chords(chroma, bass, loud)
    key, key_other, key_fit = _key(chroma, loud)

    per_bar, phase = _meter(bands["low"], chords, beats)
    starts = _sounding(list(range(phase, len(beats) - per_bar, per_bar)), loud, per_bar)
    if len(starts) < 2:
        raise StudyError("The recording is too short to find bars in.")
    features = _bar_features(spec, freqs, chroma, total, beats, starts, per_bar)
    bounds = _boundaries(features)
    bounds, letters = _merged(bounds, _letters(features, bounds))
    levels = _levels(loud, starts, per_bar, bounds)
    bar_chords = [_bar_chords(chords[start : start + per_bar]) for start in starts]
    sections = tuple(
        Section(letters[index], first + 1, last - first, levels[index],
                tuple(bar_chords[first:last]))
        for index, (first, last) in enumerate(zip(bounds[:-1], bounds[1:], strict=True))
    )
    grooves = _grooves(bands, beats, starts, per_bar, sections)
    energy = spec.sum(axis=1)
    centroid = float(((spec * freqs).sum(axis=1) / np.maximum(energy, 1e-9) * energy).sum()
                     / max(float(energy.sum()), 1e-9))
    return Study(
        seconds=y.size / SR,
        tempo=bpm,
        tempo_other=other,
        beats_per_bar=per_bar,
        key=key,
        key_other=key_other,
        key_fit=key_fit,
        swing=_swing(bands["high"], total, beats),
        brightness_hz=centroid,
        onsets_per_second=_onset_rate(total),
        sections=sections,
        grooves=grooves,
    )


# -- spectra --------------------------------------------------------------------------


def _spectrogram(
    y: np.ndarray, n_fft: int, hop: int, bins: slice | None = None
) -> np.ndarray:
    """Magnitudes, one row a frame, frames centred on multiples of ``hop``."""
    padded = np.pad(y, n_fft // 2, mode="reflect")
    window = np.hanning(n_fft).astype(np.float32)
    count = 1 + (padded.size - n_fft) // hop
    offsets = np.arange(n_fft)
    rows = []
    for first in range(0, count, 512):
        starts = np.arange(first, min(first + 512, count)) * hop
        frames = padded[starts[:, None] + offsets] * window
        magnitudes = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
        rows.append(magnitudes if bins is None else magnitudes[:, bins])
    return np.concatenate(rows)


def _bass(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The spectrogram below 300 Hz at fine resolution, on the main one's frames."""
    taps = 127
    n = np.arange(taps) - (taps - 1) / 2
    cutoff = 0.8 / _BASS_FACTOR
    kernel = (cutoff * np.sinc(cutoff * n) * np.hamming(taps)).astype(np.float32)
    low = np.convolve(y, kernel, mode="same")[::_BASS_FACTOR]
    freqs = np.arange(N_FFT // 2 + 1) * (SR / _BASS_FACTOR) / N_FFT
    keep = slice(0, int(np.searchsorted(freqs, 300.0)))
    return _spectrogram(low, N_FFT, HOP // _BASS_FACTOR, keep), freqs[keep]


def _flux(spec: np.ndarray) -> np.ndarray:
    """How much new energy each frame brings: the onset curve."""
    if spec.shape[1] == 0:
        return np.zeros(spec.shape[0], dtype=np.float32)
    rise = np.maximum(np.diff(np.log1p(spec), axis=0), 0.0).sum(axis=1)
    curve = np.concatenate([[0.0], rise])
    width = 21
    local = np.convolve(curve, np.ones(width) / width, mode="same")
    return np.maximum(curve - local, 0.0).astype(np.float32)


def _chroma(spec: np.ndarray, freqs: np.ndarray, low: float, high: float) -> np.ndarray:
    """Energy per pitch class a frame, from the bins between ``low`` and ``high`` Hz,
    with A at 440 Hz."""
    keep = np.flatnonzero((freqs >= low) & (freqs <= high))
    classes = (np.round(12 * np.log2(freqs[keep] / 440.0)).astype(int) + 9) % 12
    fold = np.zeros((keep.size, 12), dtype=np.float32)
    fold[np.arange(keep.size), classes] = 1.0
    # Only the peaks along frequency count: a loud partial leaks into the bins beside it,
    # and those belong to other pitch classes.
    part = spec[:, keep[0] - 1 : keep[-1] + 2]
    middle = part[:, 1:-1]
    peaks = np.where((middle >= part[:, :-2]) & (middle > part[:, 2:]), middle, 0.0)
    return peaks @ fold


def _beat_mean(frames: np.ndarray, beats: np.ndarray) -> np.ndarray:
    """``frames`` averaged over each beat, one row a beat."""
    edges = np.append(beats, min(frames.shape[0], 2 * beats[-1] - beats[-2]))
    rows = [
        frames[first : max(last, first + 1)].mean(axis=0)
        for first, last in zip(edges[:-1], edges[1:], strict=True)
    ]
    return np.asarray(rows)


# -- pulse ----------------------------------------------------------------------------


def _tempo(curve: np.ndarray) -> tuple[float, float | None]:
    centred = curve - curve.mean()
    size = 1 << int(np.ceil(np.log2(2 * centred.size)))
    spectrum = np.fft.rfft(centred, size)
    auto = np.fft.irfft(spectrum * np.conj(spectrum))[: centred.size]
    auto = auto / max(float(auto[0]), 1e-9)
    lags = np.arange(auto.size)
    candidates = np.arange(_TEMPO_RANGE[0], _TEMPO_RANGE[1] + 0.25, 0.25)

    def fit(bpm: np.ndarray) -> np.ndarray:
        period = 60.0 * FPS / bpm
        return sum(np.interp(multiple * period, lags, auto) for multiple in (1, 2, 3, 4))

    raw = fit(candidates)
    prior = np.exp(-0.5 * np.log2(candidates / 120.0) ** 2)
    scores = raw * prior
    best = int(np.argmax(scores))
    coarse = float(candidates[best])
    # The same pulse over many beats: a long lag tells 123 from 124 where one beat cannot.
    fine = np.arange(coarse - 2.0, coarse + 2.0, 0.05)
    period = 60.0 * FPS / fine
    long = sum(
        np.interp(multiple * period, lags, auto)
        for multiple in (4, 8, 16, 32)
        if multiple * period.max() < auto.size / 2
    )
    bpm = float(fine[int(np.argmax(long))]) if isinstance(long, np.ndarray) else coarse
    # A pulse fits at half and double speed too, and which one a listener taps is not in
    # the signal. Where the prior chose a slow or a fast reading, the other is named.
    other = None
    factor = 2.0 if coarse <= 100.0 else 0.5 if coarse >= 160.0 else None
    if factor is not None:
        near = np.flatnonzero(np.abs(candidates - coarse * factor) <= 2.0)
        if near.size and raw[near].max() >= 0.5 * raw[best]:
            other = bpm * factor
    return bpm, other


def _beats(curve: np.ndarray, period: float) -> np.ndarray:
    """Frame of every beat: the onsets that are strong and one period apart."""
    local = curve / max(float(curve.std()), 1e-9)
    offsets = np.arange(max(1, round(period / 2)), round(period * 2) + 1)
    penalty = -100.0 * np.log(offsets / period) ** 2
    score = local.astype(np.float64).copy()
    back = np.full(local.size, -1, dtype=np.int64)
    for frame in range(int(offsets[0]), local.size):
        usable = offsets[offsets <= frame]
        candidates = score[frame - usable] + penalty[: usable.size]
        pick = int(np.argmax(candidates))
        score[frame] = local[frame] + candidates[pick]
        back[frame] = frame - usable[pick]
    tail = max(0, local.size - round(period))
    frame = tail + int(np.argmax(score[tail:]))
    beats = []
    while frame >= 0:
        beats.append(frame)
        frame = int(back[frame])
    return np.asarray(beats[::-1], dtype=np.int64)


def _on_the_kick(beats: np.ndarray, low: np.ndarray) -> np.ndarray:
    """The beats, moved half a beat when the low band hits between them: a loud off-beat
    hat can take the pulse, and the kick is the better witness of where the beat is."""
    if beats.size < 4:
        return beats
    between = (beats[:-1] + beats[1:]) // 2
    on, off = float(_at(low, beats).mean()), float(_at(low, between).mean())
    return between if off > 1.5 * on and off > 0 else beats


def _at(curve: np.ndarray, frames: np.ndarray) -> np.ndarray:
    """The curve's peak within a frame of each of ``frames``."""
    frames = np.clip(np.round(frames).astype(int), 1, curve.size - 2)
    return np.maximum(np.maximum(curve[frames - 1], curve[frames]), curve[frames + 1])


def _meter(low: np.ndarray, chords: list[str], beats: np.ndarray) -> tuple[int, int]:
    """Beats in a bar (4 unless three plainly fits better) and which beat starts one. A
    bar starts where the low band hits hardest and the chords change."""
    kick = _at(low, beats)
    kick = kick / max(float(np.percentile(kick, 95)), 1e-9)
    change = np.array(
        [0.0] + [float(a != b) for a, b in zip(chords[:-1], chords[1:], strict=True)]
    )
    accent = np.minimum(kick, 1.5) + change

    def repeat(lag: int) -> float:
        if accent.size <= 2 * lag + 2 or float(accent.std()) < 1e-6:
            return 0.0
        return float(np.corrcoef(accent[:-lag], accent[lag:])[0, 1])

    per_bar = 3 if repeat(3) + repeat(6) > repeat(4) + repeat(8) + 0.3 else 4
    phase = max(range(per_bar), key=lambda start: float(accent[start::per_bar].mean()))
    return per_bar, phase


def _sounding(starts: list[int], loud: np.ndarray, per_bar: int) -> list[int]:
    """``starts`` without the bars of silence or fading tail at either end."""
    levels = [float(loud[start : start + per_bar].mean()) for start in starts]
    floor = 0.25 * float(np.median(levels)) if levels else 0.0
    while levels and levels[-1] < floor:
        levels.pop()
        starts = starts[:-1]
    while levels and levels[0] < floor:
        levels.pop(0)
        starts = starts[1:]
    return starts


def _swing(high: np.ndarray, total: np.ndarray, beats: np.ndarray) -> float | None:
    curve = high if float(high.max()) > 0 else total
    floor = 0.3 * float(np.percentile(_at(curve, beats), 95))
    places = []
    for first, last in zip(beats[:-1], beats[1:], strict=True):
        span = last - first
        lo, hi = first + round(0.3 * span), first + round(0.85 * span)
        if hi <= lo:
            continue
        peak = lo + int(np.argmax(curve[lo:hi]))
        if curve[peak] > floor:
            places.append((peak - first) / span)
    if len(places) < max(8, len(beats) // 4):
        return None
    middle = float(np.median(places))
    agree = float(np.mean(np.abs(np.asarray(places) - middle) < 0.07))
    return middle if agree >= 0.6 else None


def _onset_rate(curve: np.ndarray) -> float:
    floor = float(curve.mean() + curve.std())
    peaks = (curve[1:-1] > floor) & (curve[1:-1] >= curve[:-2]) & (curve[1:-1] > curve[2:])
    return float(peaks.sum()) / (curve.size / FPS)


# -- harmony --------------------------------------------------------------------------


def _key(chroma: np.ndarray, loud: np.ndarray) -> tuple[str, str, float]:
    profile = (chroma * loud[:, None]).sum(axis=0)
    fits = []
    for tonic in range(12):
        for name, shape in (("major", _MAJOR), ("minor", _MINOR)):
            fit = float(np.corrcoef(profile, np.roll(shape, tonic))[0, 1])
            fits.append((fit, f"{NOTES[tonic]} {name}"))
    fits.sort(reverse=True)
    return fits[0][1], fits[1][1], max(0.0, fits[0][0])


def _chords(chroma: np.ndarray, bass: np.ndarray, loud: np.ndarray) -> list[str]:
    """One chord a beat: the triad that fits, with the bass note counting for its root."""
    names, templates = [], []
    for root in range(12):
        for suffix, third in (("", 4), ("m", 3)):
            template = np.zeros(12)
            template[[root, (root + third) % 12, (root + 7) % 12]] = 1.0
            names.append(NOTES[root] + suffix)
            templates.append(template / np.linalg.norm(template))
    shapes = np.asarray(templates)
    centred = chroma - chroma.min(axis=1, keepdims=True)
    centred = centred / np.maximum(np.linalg.norm(centred, axis=1, keepdims=True), 1e-9)
    low = bass - bass.min(axis=1, keepdims=True)
    low = low / np.maximum(low.max(axis=1, keepdims=True), 1e-9)
    fit = centred @ shapes.T + _BASS_WEIGHT * np.repeat(low, 2, axis=1)

    best = fit[0].copy()
    back = np.zeros(fit.shape, dtype=np.int64)
    for beat in range(1, fit.shape[0]):
        stay = best
        move = float(best.max()) - _CHANGE_COST
        back[beat] = np.where(stay >= move, np.arange(fit.shape[1]), int(np.argmax(best)))
        best = np.maximum(stay, move) + fit[beat]
    path = [int(np.argmax(best))]
    for beat in range(fit.shape[0] - 1, 0, -1):
        path.append(int(back[beat][path[-1]]))
    quiet = loud < 0.03 * float(loud.max())
    return ["-" if quiet[beat] else names[index] for beat, index in enumerate(path[::-1])]


def _bar_chords(beats: list[str]) -> str:
    runs: list[str] = []
    for chord in beats:
        if not runs or runs[-1] != chord:
            runs.append(chord)
    return " ".join(runs)


# -- form -----------------------------------------------------------------------------


def _bar_features(
    spec: np.ndarray, freqs: np.ndarray, chroma: np.ndarray, total: np.ndarray,
    beats: np.ndarray, starts: list[int], per_bar: int,
) -> np.ndarray:
    """What a bar sounds like, for comparing bars: its pitch classes, how much energy
    each band holds, and how busy it is."""
    rows = []
    for start in starts:
        first, last = int(beats[start]), int(beats[start + per_bar])
        frames = spec[first:last]
        pitch = chroma[start : start + per_bar].mean(axis=0)
        pitch = pitch - pitch.min()
        pitch = pitch / max(float(np.linalg.norm(pitch)), 1e-9)
        levels = [
            np.log10(max(float((frames[:, (freqs >= low) & (freqs < high)] ** 2).mean()), 1e-9))
            for low, high in _BAND_EDGES.values()
        ]
        rows.append(np.concatenate([pitch, levels, [float(total[first:last].mean())]]))
    table = np.asarray(rows)
    extra = table[:, 12:]
    # In units of the song's own spread, but never under 3 dB of level or a fifth of the
    # busyness: a song that does not change must not have its small wavering blown up.
    least = np.array([0.3, 0.3, 0.3, 0.2 * max(float(extra[:, 3].mean()), 1e-9)])
    table[:, 12:] = (extra - extra.mean(axis=0)) / np.maximum(extra.std(axis=0), least) * 0.5
    table[:, :12] *= 1.5
    return table


def _boundaries(features: np.ndarray) -> list[int]:
    """Bar indexes where a section starts, the first bar and one past the last included.

    A section starts where the bars before and the bars after differ on average. Averages
    over four bars, and over eight where the song is long enough, so a chord loop of
    either length is one section and not a change at every bar."""
    count = features.shape[0]
    novelty = _novelty(features, 4 if count >= 12 else 2)
    if count >= 24:
        wide = _novelty(features, 8)
        inside = slice(8, count - 7)
        edges = novelty * _EDGE_TRUST
        edges[inside] = np.minimum(novelty[inside], wide[inside])
        novelty = edges
    bounds = [0]
    for bar in np.argsort(novelty)[::-1]:
        if novelty[bar] < _CHANGE_FLOOR:
            break
        window = novelty[max(0, bar - 1) : bar + 2]
        if novelty[bar] >= window.max() and all(abs(int(bar) - other) >= 4 for other in bounds):
            bounds.append(int(bar))
    return sorted({*bounds, count})


def _novelty(features: np.ndarray, half: int) -> np.ndarray:
    """Per bar, how far the mean of the ``half`` bars before is from the mean after."""
    count = features.shape[0]
    novelty = np.zeros(count)
    for bar in range(half, count - half + 1):
        before = features[bar - half : bar].mean(axis=0)
        after = features[bar : bar + half].mean(axis=0)
        novelty[bar] = float(np.linalg.norm(before - after))
    return novelty


def _letters(features: np.ndarray, bounds: list[int]) -> list[str]:
    """A letter per section; sections that sound alike share one."""
    means: list[np.ndarray] = []
    letters: list[str] = []
    known: list[tuple[str, np.ndarray]] = []
    for first, last in zip(bounds[:-1], bounds[1:], strict=True):
        mean = features[first:last].mean(axis=0)
        means.append(mean)
        match = None
        for letter, seen in known:
            sizes = float(np.linalg.norm(mean) * np.linalg.norm(seen))
            alike = float(mean @ seen) / max(sizes, 1e-9)
            if alike > 0.9 and float(np.abs(mean[12:] - seen[12:]).max()) < 0.5:
                match = letter
                break
        if match is None:
            match = chr(ord("A") + min(len(known), 25))
            known.append((match, mean))
        letters.append(match)
    return letters


def _merged(bounds: list[int], letters: list[str]) -> tuple[list[int], list[str]]:
    """Neighbouring sections with one letter are one section."""
    kept_bounds, kept_letters = [bounds[0]], []
    for index, letter in enumerate(letters):
        if kept_letters and kept_letters[-1] == letter:
            kept_bounds[-1] = bounds[index + 1]
        else:
            kept_letters.append(letter)
            kept_bounds.append(bounds[index + 1])
    return kept_bounds, kept_letters


def _levels(loud: np.ndarray, starts: list[int], per_bar: int, bounds: list[int]) -> list[str]:
    levels = []
    for first, last in zip(bounds[:-1], bounds[1:], strict=True):
        beats = loud[starts[first] : starts[last - 1] + per_bar]
        levels.append(20 * np.log10(max(float(np.sqrt((beats ** 2).mean())), 1e-9)))
    top = max(levels)
    return ["full" if top - level < 3 else "medium" if top - level < 9 else "quiet"
            for level in levels]


def _grooves(
    bands: dict[str, np.ndarray], beats: np.ndarray, starts: list[int], per_bar: int,
    sections: tuple[Section, ...],
) -> dict[str, dict[str, str]]:
    """Each section letter's bar, averaged: per band, four steps a beat."""
    steps = per_bar * 4
    grids = {}
    for name in _BANDS:
        curve = bands[name]
        rows = []
        for start in starts:
            edges = beats[start : start + per_bar + 1].astype(float)
            frames = np.concatenate([
                first + (last - first) * np.arange(4) / 4
                for first, last in zip(edges[:-1], edges[1:], strict=True)
            ])
            rows.append(_at(curve, frames))
        table = np.asarray(rows)
        grids[name] = table / max(float(np.percentile(table, 95)), 1e-9)
    grooves: dict[str, dict[str, str]] = {}
    for letter in dict.fromkeys(section.letter for section in sections):
        bars = [
            bar
            for section in sections if section.letter == letter
            for bar in range(section.first_bar - 1, section.first_bar - 1 + section.bars)
        ]
        grooves[letter] = {}
        for name in _BANDS:
            mean = grids[name][bars].mean(axis=0)
            grooves[letter][name] = "".join(
                "X" if value >= 0.6 else "x" if value >= 0.3 else "." for value in mean[:steps]
            )
    return grooves
