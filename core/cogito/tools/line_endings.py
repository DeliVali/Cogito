"""A file's own line endings: read as they are, written back the same way.

`read`, `edit` and `write` work on text with "\\n" alone between lines. That is what `read`
shows the model, what every hashline hash covers and what `str_replace` matches. Python's
universal-newline reading gives that view, and it stays. It must not decide the bytes a
file is written with. Left to `Path.write_text`'s newline translation, a CRLF or CR file
edited on Linux comes back LF, and an LF file edited on Windows comes back CRLF. That is a
whole-file diff in the user's repository, and a shell script that no longer runs.

So an existing file's ending is read from its own bytes, the work is done in LF, and the
result is written in that ending with no translation. Text from the model is normalized the
same way, so a CRLF `new` cannot leave a stray CR in an LF file.

Surveyed tools (local clones, read 2026-10-07):

- oh-my-pi d19afc2 (`crates/pi-edit/src/text.rs:27-64`, `files.rs:48-66,199-208`):
  normalizes CRLF and CR to LF and re-encodes on write with the file's first ending. A
  CR-only file comes back LF (its enum has no CR). A new file is LF.
- Codex c6049eb (`codex-rs/apply-patch/src/text_file.rs`, `lib.rs:59-95`): writes LF by
  default. The opt-in `CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS=1` keeps each untouched
  line's own ending and gives inserted lines the file's first ending. No ending means LF.
- Cline 39ff235 (`sdk/packages/core/src/extensions/tools/executors/line-endings.ts`) and
  Gemini CLI fb972b2 (`packages/core/src/utils/textUtils.ts:93`, `tools/edit.ts:684-691,
  988-995`): CRLF if "\\r\\n" appears anywhere, else LF, and the model's text is
  normalized to it. A new file gets `os.EOL` (in Cline, unless the content already holds a
  CR). opencode 108b988 (`tool/edit.ts:22-33`) and crush bdcf796
  (`fsext/fileutil.go:242-256`) detect the same way.
- OpenHands-ACI a8d8d23 (`editor/editor.py:166,410`) and the OpenHands SDK 35410b3 file
  editor (`file_editor/editor.py:516`) open in text mode with no `newline`, so they
  translate on write.

Where this module departs from them:

- The ending is the file's dominant one: the most frequent of CRLF, LF and CR. "CRLF
  anywhere" turns an LF file with one pasted CRLF line entirely CRLF, and "the first one"
  lets a single odd first line decide. A tie goes to whichever comes first, as in Codex
  and oh-my-pi.
- CR stays CR, as in Codex's preserve mode (oh-my-pi turns it into LF).
- A mixed file is written entirely in its dominant ending. One ending per file is also all
  that `write` and `whole_file` can honour, since they have no untouched lines.
- A file with no ending to go by (new, empty, one line, or binary) gets LF on every
  platform, as in oh-my-pi and Codex rather than Cline's or Gemini CLI's `os.EOL`, because
  a CRLF file in an LF project is the breakage this module prevents. The one exception: a
  directory where every neighbour with the same suffix is CRLF. No surveyed tool looks at
  neighbours. That rule is this project's own and a hypothesis until measured.
"""

from __future__ import annotations

import re
from pathlib import Path

LF = "\n"
CRLF = "\r\n"
CR = "\r"

_ENDING = re.compile(r"\r\n|\r|\n")

#: How much of a neighbour is read to tell its ending, and how many are consulted.
_NEIGHBOUR_SAMPLE = 64 * 1024
_MAX_NEIGHBOURS = 16

#: The same binary test `read` applies (`files._looks_binary`): a NUL in the first 8 KiB.
_NULL_SAMPLE = 8192


def read_exact(path: Path) -> str:
    """The file's text as on disk: decoded as `read` decodes it (UTF-8, an invalid byte
    becoming U+FFFD), with every line ending left as it is."""
    return path.read_bytes().decode("utf-8", errors="replace")


def to_lf(text: str) -> str:
    """CRLF and lone CR as LF, which is what universal-newline reading makes of a file."""
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str) -> list[str]:
    """The lines `read` numbers: `text` split on "\\n" alone, one final newline dropped."""
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n") if text else []


def dominant(text: str) -> str | None:
    """The most frequent line ending in `text`, a tie going to whichever comes first.
    `None` when it has no line ending at all."""
    crlf = text.count("\r\n")
    counts = {CRLF: crlf, LF: text.count("\n") - crlf, CR: text.count("\r") - crlf}
    best = max(counts.values())
    if best == 0:
        return None
    tied = {ending for ending, count in counts.items() if count == best}
    if len(tied) == 1:
        return tied.pop()
    for match in _ENDING.finditer(text):
        if match.group() in tied:
            return match.group()
    return LF  # unreachable: every tied ending occurs at least once


def for_new_file(path: Path) -> str:
    """The ending for a file with none of its own to go by: LF, unless every neighbour in
    the same directory with the same suffix is CRLF.

    Only the same suffix counts. A CRLF `.bat` next to a new `.sh` says nothing about the
    `.sh`, and suffix-less files (a LICENSE, a Makefile, a script) vary too much to follow.
    """
    suffix = path.suffix.lower()
    if not suffix:
        return LF
    try:
        neighbours = sorted(
            entry
            for entry in path.parent.iterdir()
            if entry.suffix.lower() == suffix and entry.name != path.name and entry.is_file()
        )
    except OSError:
        return LF
    crlf = 0
    for neighbour in neighbours[:_MAX_NEIGHBOURS]:
        try:
            with neighbour.open("rb") as handle:
                sample = handle.read(_NEIGHBOUR_SAMPLE)
        except OSError:
            continue
        if b"\0" in sample[:_NULL_SAMPLE]:
            continue
        ending = dominant(sample.decode("utf-8", errors="replace"))
        if ending is None:
            continue
        if ending != CRLF:
            return LF
        crlf += 1
    return CRLF if crlf else LF


def ending_for(path: Path, text: str | None) -> str:
    """The ending to write `path` with, given its current text (`None` if it has none).

    A binary file's CR and LF bytes are not line endings, so it counts as having none.
    """
    own = dominant(text) if text and "\0" not in text[:_NULL_SAMPLE] else None
    return own or for_new_file(path)


def restore(text: str, ending: str) -> str:
    """LF text written in `ending`. Normalizes first, so a CR still in the model's text
    cannot be left behind."""
    text = to_lf(text)
    return text if ending == LF else text.replace("\n", ending)


def write_exact(path: Path, text: str) -> None:
    """Write `text` as UTF-8 with no newline translation, whatever the platform.

    Encoded before the file is opened, so text UTF-8 cannot hold (a lone surrogate from a
    model's JSON) raises here and leaves the file untouched, not after `open` has
    truncated it.
    """
    path.write_bytes(text.encode("utf-8"))
