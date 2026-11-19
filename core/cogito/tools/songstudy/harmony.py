"""Names for harmony, shared by what is measured from sound and what is read from notes:
keys, chords as degrees of a key, and the loop a run of bars repeats."""

from __future__ import annotations

from collections.abc import Sequence

NOTES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
#: How much each pitch class belongs to a major and a minor key on C (Krumhansl and
#: Kessler's probe-tone ratings; Krumhansl, "Cognitive Foundations of Musical Pitch", 1990).
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
MAJOR_SCALE = frozenset({0, 2, 4, 5, 7, 9, 11})
#: Natural minor with the raised sixth and seventh a minor tune borrows.
MINOR_SCALE = frozenset({0, 2, 3, 5, 7, 8, 10, 9, 11})

_MAJOR_DEGREES = {0: "I", 1: "bII", 2: "II", 3: "bIII", 4: "III", 5: "IV", 6: "bV",
                  7: "V", 8: "bVI", 9: "VI", 10: "bVII", 11: "VII"}
_MINOR_DEGREES = {0: "I", 1: "bII", 2: "II", 3: "III", 4: "#III", 5: "IV", 6: "#IV",
                  7: "V", 8: "VI", 9: "#VI", 10: "VII", 11: "#VII"}
_MAX_BARS_SHOWN = 16


def key_of(weights: Sequence[float]) -> tuple[str, str, float]:
    """The key twelve pitch-class weights fit best, the next best, and the fit (0-1)."""
    fits = []
    for tonic in range(12):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            turned = [profile[(index - tonic) % 12] for index in range(12)]
            fits.append((_correlation(weights, turned), f"{NOTES[tonic]} {mode}"))
    fits.sort(reverse=True)
    return fits[0][1], fits[1][1], max(0.0, fits[0][0])


def tonic(key: str) -> tuple[int, bool]:
    """A key's tonic pitch class and whether it is minor."""
    name, mode = key.split()
    return NOTES.index(name), mode == "minor"


def scale(key: str) -> frozenset[int]:
    """The pitch classes of a key."""
    root, minor = tonic(key)
    return frozenset((root + step) % 12 for step in (MINOR_SCALE if minor else MAJOR_SCALE))


def chord_tones(chord: str) -> frozenset[int]:
    """The pitch classes of a major or minor triad named like `Am` or `F#`."""
    small = chord.endswith("m")
    root = NOTES.index(chord[:-1] if small else chord)
    return frozenset({root, (root + (3 if small else 4)) % 12, (root + 7) % 12})


def degrees(bar: str, root: int, minor: bool) -> str:
    """A bar's chords as degrees of the key: upper case major, lower case minor."""
    out = []
    for chord in bar.split():
        if chord == "-":
            out.append("-")
            continue
        small = chord.endswith("m")
        place = NOTES.index(chord[:-1] if small else chord)
        degree = (_MINOR_DEGREES if minor else _MAJOR_DEGREES)[(place - root) % 12]
        out.append(degree.lower() if small else degree)
    return " ".join(out)


def loop(bars: Sequence[str]) -> tuple[list[str], float] | None:
    """The run of bars that ``bars`` repeats, and how many times, or None. Of the
    lengths tried, the shortest that fits about as well as the best: `Am F G F` fits a
    two-bar loop in three bars of four, and a four-bar one in all of them."""
    fits: list[tuple[float, list[str]]] = []
    for length in (1, 2, 4, 8):
        if length > 1 and len(bars) < 2 * length:
            break
        pattern = []
        for place in range(length):
            column = [bar for index, bar in enumerate(bars) if index % length == place]
            pattern.append(max(sorted(set(column)), key=column.count))
        same = sum(bar == pattern[index % length] for index, bar in enumerate(bars))
        fits.append((same / len(bars) if bars else 0.0, pattern))
    best = max((fit for fit, _pattern in fits), default=0.0)
    if best < 0.75:
        return None
    pattern = next(pattern for fit, pattern in fits if fit >= best - 0.05)
    return pattern, len(bars) / len(pattern)


def progression(bars: Sequence[str]) -> str:
    """A run of bars as text: the loop it repeats when it does, else the bars in order."""
    found = loop(bars)
    if found is not None:
        pattern, times = found
        again = f"  (a {len(pattern)}-bar loop, about {times:g} times)" if times >= 2 else ""
        return "| " + " | ".join(pattern) + " |" + again
    head = "| " + " | ".join(bars[:_MAX_BARS_SHOWN]) + " |"
    more = len(bars) - _MAX_BARS_SHOWN
    return head + (f" ... ({more} more bars)" if more > 0 else "")


def _correlation(first: Sequence[float], second: Sequence[float]) -> float:
    mean_a, mean_b = sum(first) / len(first), sum(second) / len(second)
    top = sum((a - mean_a) * (b - mean_b) for a, b in zip(first, second, strict=True))
    size = (sum((a - mean_a) ** 2 for a in first) * sum((b - mean_b) ** 2 for b in second)) ** 0.5
    return top / size if size > 0 else 0.0
