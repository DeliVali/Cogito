"""The terminal's output as plain lines, for the model to read.

Not a terminal emulator: a line model over the output. Printable text overwrites at the
cursor, CR goes back to the start of the line, BS one column, erase-in-line clears, and
every other CSI and OSC sequence is dropped, and so are DCS, SOS, PM and APC strings, up to
their ST. A full-screen program (vim, top) reads poorly; that is accepted (design, decision
4).

The shell integration's private OSC (`ESC ] 6973 ; cogito ; <nonce> ; prompt ; <status>
[; ready] BEL`) is read here too: each one with this host's nonce is a prompt, reported to
the caller with its status. ``; ready`` promises the second mark (``<nonce> ; ready``), sent
once the prompt is drawn and the line editor reads (`integration`). One without the nonce (a
program's output faking a prompt) is dropped like any other OSC. Whether the shell turned
bracketed paste on (``CSI ? 2004 h``) is kept too, and whether it did so since the last
prompt mark.
"""

from __future__ import annotations

#: How many lines are kept. The model reads at most 1,000 of them.
MAX_LINES = 2000
#: What every shell integration mark starts with, inside an OSC; then the host's nonce.
PROMPT_MARK = "6973;cogito;"
#: An OSC longer than this is not ours and is dropped as it streams.
_MAX_OSC = 512
_TAB = 8


class Screen:
    """Lines of text, fed in pieces. Not thread-safe: one reader thread feeds it, and readers
    take `lines()` under the tab's lock."""

    def __init__(self, max_lines: int = MAX_LINES, nonce: str = "") -> None:
        self._max = max_lines
        self._mark = f"{PROMPT_MARK}{nonce};"
        #: The shell asked for bracketed paste (``CSI ? 2004 h``) and has not turned it off.
        self.bracketed = False
        #: The shell turned bracketed paste on after the last prompt mark (and has not
        #: turned it off since).
        self.editor_up = False
        #: The last prompt mark promised a ready mark.
        self.promised = False
        #: The ready mark came after the last prompt mark: the prompt is drawn and the line
        #: editor reads.
        self.drawn = False
        self._lines: list[str] = []
        self._current: list[str] = []
        self._col = 0
        #: How many lines were dropped from the front, so a mark stays valid.
        self.dropped = 0
        # Parser state: "text", "esc", "csi", "osc", "osc_esc", "string", "string_esc",
        # "charset".
        self._state = "text"
        self._params = ""
        self._osc: list[str] = []

    # -- reading ---------------------------------------------------------------------------

    def mark(self) -> int:
        """The index of the line the cursor is on now, counted from the first line ever."""
        return self.dropped + len(self._lines)

    def lines(self, since: int | None = None, last: int | None = None) -> list[str]:
        """The lines from ``since`` (a `mark`) on, or the ``last`` few, the cursor's line
        included. Trailing spaces are dropped."""
        every = [*self._lines, "".join(self._current)]
        if since is not None:
            every = every[max(0, since - self.dropped):]
        if last is not None:
            every = every[-last:] if last > 0 else []
        return [line.rstrip() for line in every]

    # -- feeding ---------------------------------------------------------------------------

    def feed(self, text: str) -> list[int | None]:
        """Take decoded output. Returns the status of each prompt mark in it, in order
        (None when a mark's status is not a number)."""
        prompts: list[int | None] = []
        for char in text:
            state = self._state
            if state == "text":
                self._text(char)
            elif state == "esc":
                self._escape(char)
            elif state == "csi":
                if "\x40" <= char <= "\x7e":
                    self._csi(self._params, char)
                    self._state = "text"
                else:
                    self._params += char
                    if len(self._params) > 64:
                        self._state = "text"
            elif state == "osc":
                if char == "\x07":
                    self._end_osc(prompts)
                elif char == "\x1b":
                    self._state = "osc_esc"
                elif len(self._osc) < _MAX_OSC:
                    self._osc.append(char)
            elif state == "osc_esc":
                # ESC \ ends it; anything else after ESC ends it too.
                self._end_osc(prompts)
                if char != "\\":
                    self._state = "text"
                    self._text(char)
            elif state == "string":
                # DCS, SOS, PM or APC: nothing in it is text, up to ST (ESC \); CAN and
                # SUB cancel it.
                if char == "\x1b":
                    self._state = "string_esc"
                elif char in "\x18\x1a":
                    self._state = "text"
            elif state == "string_esc":
                # ST ends it; any other ESC ends it and starts its own sequence.
                self._state = "text"
                if char != "\\":
                    self._escape(char)
            elif state == "charset":
                self._state = "text"
        return prompts

    def _text(self, char: str) -> None:
        if char == "\x1b":
            self._state = "esc"
        elif char == "\n":
            self._newline()
        elif char == "\r":
            self._col = 0
        elif char == "\b":
            self._col = max(0, self._col - 1)
        elif char == "\t":
            self._put_spaces(_TAB - self._col % _TAB)
        elif char < " " or "\x7f" <= char <= "\x9f":
            return
        else:
            self._put(char)

    def _escape(self, char: str) -> None:
        if char == "[":
            self._state = "csi"
            self._params = ""
        elif char == "]":
            self._state = "osc"
            self._osc = []
        elif char in "PX^_":
            self._state = "string"
        elif char in "()*+":
            self._state = "charset"
        else:
            # A two-character sequence (ESC 7, ESC =, ESC M...): nothing to keep.
            self._state = "text"

    def _end_osc(self, prompts: list[int | None]) -> None:
        body = "".join(self._osc)
        self._osc = []
        self._state = "text"
        if not body.startswith(self._mark):
            return
        fields = body[len(self._mark):].split(";")
        if fields[0] == "prompt":
            self.editor_up = self.drawn = False
            self.promised = "ready" in fields[2:]
            status = fields[1].strip() if len(fields) > 1 else ""
            prompts.append(int(status) if status.lstrip("-").isdigit() else None)
        elif fields == ["ready"]:
            self.drawn = True

    @property
    def editor_ready(self) -> bool:
        """The line editor reads, by the ready mark where the prompt mark promised one, else
        by bracketed paste turned on since it."""
        return self.drawn if self.promised else self.editor_up

    def _csi(self, params: str, final: str) -> None:
        if params.startswith("?") and final in "hl" and "2004" in params[1:].split(";"):
            self.bracketed = self.editor_up = final == "h"
            return
        if params.startswith(("?", ">", "<", "=")):
            return
        first = params.split(";", 1)[0]
        number = int(first) if first.isdigit() else 0
        if final == "K":
            if number == 0:
                del self._current[self._col:]
            elif number == 1:
                # From the start to the cursor, the cursor's cell included.
                end = min(self._col + 1, len(self._current))
                self._current[:end] = [" "] * end
            else:
                self._current = []
        elif final == "C":
            self._col += max(1, number)
        elif final == "D":
            self._col = max(0, self._col - max(1, number))
        elif final == "G":
            self._col = max(0, max(1, number) - 1)
        elif final == "P":
            del self._current[self._col : self._col + max(1, number)]
        elif final == "X":
            end = min(len(self._current), self._col + max(1, number))
            self._current[self._col : end] = [" "] * (end - self._col)
        elif final == "J" and number in (2, 3) and self._current:
            # The screen was cleared: what is above is gone from it.
            self._newline()

    def _put(self, char: str) -> None:
        if self._col > len(self._current):
            self._current.extend(" " * (self._col - len(self._current)))
        if self._col == len(self._current):
            self._current.append(char)
        else:
            self._current[self._col] = char
        self._col += 1

    def _put_spaces(self, count: int) -> None:
        for _ in range(count):
            self._put(" ")

    def _newline(self) -> None:
        self._lines.append("".join(self._current))
        self._current = []
        self._col = 0
        overflow = len(self._lines) + 1 - self._max
        if overflow > 0:
            del self._lines[:overflow]
            self.dropped += overflow
