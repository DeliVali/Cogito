"""The person's own message, when it is too large to send whole.

Tool output is clipped and offloaded (`context/budgets.py`); this does the same for the
person's message. A pasted page of ~106k tokens sent whole to a model serving 43,008
tokens a request was refused three times (the desktop app's log, 2026-09-30). Compaction
cannot fix that: it folds what was already said, it measures the last request rather than
the next one, and its seed pins every instruction verbatim (`compact.render_tasks` never
clips one). A message larger than the window stays larger after any number of folds.

Past a fixed share of the model's context window (a fact the harness holds; the model is
never asked), the message is written whole to the session's artifacts, the same store tool
output overflows to. The model gets its beginning and end, where it was saved, which lines
are not shown, and how to read them with the tools it has. Below that share the message
goes unchanged, byte for byte. The size is the loop's own count, but never below what the
text's scripts and long unspaced runs cost in the tokenizers cogito-lab measured (`size`).
When a server still refuses a request as too large, the loop offloads the message against
the size the server named (`loop.engine._offload_opening`).

Other harnesses, read 2026-09-30, and where this departs:

- Codex refuses input over ``MAX_USER_INPUT_TEXT_CHARS = 1 << 20`` characters so one
  message cannot monopolize a large window (codex-rs/protocol/src/user_input.rs), and
  treats a request the model refuses as too large as terminal
  (codex-rs/protocol/src/error.rs).
- Claude Code sends the prompt and recovers from "prompt is too long" by compacting. Its
  changelog shows that path fixed repeatedly for this case: sessions stuck when
  auto-compaction had no complete earlier exchange to summarize (2.1.269), and a session
  dominated by one very large first prompt, now summarized on its own (2.1.281). It
  offloads to a file with a preview only harness-produced text: hook output over 10,000
  characters, tool results over 50,000 characters (CHANGELOG).
- OpenCode detects the overflow from the provider's error (including llama.cpp's "exceeds
  the available context size"), never retries it, compacts the earlier history and replays
  the message after the summary (packages/opencode/src/session/compaction.ts).
- Aider estimates before sending, warns past the model's ``max_input_tokens`` and asks
  whether to send anyway (aider/coders/base_coder.py, ``check_tokens``).

None of them offloads the person's message. Refusing (Codex) or asking (Aider) sends the
person back to cut a page by hand. Compacting (Claude Code, OpenCode) cannot shrink one
message larger than the window, and on a local model with a 32-64k window a paste is the
usual way to get there. Offloading is this harness's rule for anything too large for the
window (CONTRIBUTING.md section 6.3): stored whole, never silently cut, the pointer travels
with it.

Why a quarter of the window: a fold pins the instruction verbatim, so whatever share it
takes is never freed by folding. At the default 32,768-token window, compaction fires at
75% (`BudgetSettings.compact_at`) = 24,576 tokens. A fold always keeps the fixed part of a
request, 1,797 tokens (measured here 2026-09-30 with the loop's counter: a 253-token
system prompt with no project documents and 1,544 tokens of native tool definitions),
plus the handover (`compact.MAX_SUMMARY_TOKENS` 1,400 and `literal_budget_tokens` 320).
With the message at half the window (16,384), 4,675 tokens remain after a fold: not even
one `read` result (budget 6,000), so the conversation would fold on every step. At a
quarter (8,192), 12,867 remain: two. It is a constant, not derived from ``compact_at``, so
a benchmark task that lowers its own compaction line never moves it. No benchmark
instruction comes near it (the largest is 1,647 tokens, cogito-lab
tests/context/test_inbound_never_in_bench.py).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from cogito.config import paths
from cogito.context.offload import ArtifactRef, ArtifactStore
from cogito.provider import tokens

log = logging.getLogger(__name__)

#: Share of the model's context window one message may take before it is offloaded. The
#: module docstring explains the quarter.
SHARE = 0.25

#: Tokens of the message shown to the model, beginning and end together. Same ceiling as
#: the `shell` tool's output (`config.settings.DEFAULT_TOOL_BUDGETS`), so a paste is never
#: shown longer than a command's output.
EXCERPT_TOKENS = 2000

#: Bytes per slice of an over-long line in the shell hint: about 1,000 tokens, well within
#: what `shell` returns whole.
_SLICE_BYTES = 4000
#: Lines per slice the shell hint suggests where `read` is not available.
_SLICE_LINES = 200

# Minimum counts for `size`. The loop's counter (`provider.tokens.Heuristic`, 1.3 tokens
# per word-ish run) counts a run of Han characters, or of hex, as a handful of tokens:
# 32,000 of either came out under 100. Each floor below is the most any of five tokenizers
# spent on that kind of text: Qwen3 (the Qwen3-Coder family), Qwen2.5-Coder, DeepSeek-R1,
# Granite 4.1 and StarCoder2. Measured 2026-09-30 in cogito-lab
# experiments/2026-09-30-inbound-token-floor (scripts from this machine's gettext
# catalogs; random bytes and two minified files shipped on it), rounded toward the higher
# count.

#: Han, Kana and Hangul: at most 1.483 tokens per code point (Granite 4.1, Chinese).
_WIDE = re.compile(
    "[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff"
    "\uac00-\ud7af\uf900-\ufaff]"
)
_WIDE_TOKENS = 1.5
#: Thai: at most 1.079 tokens per code point (StarCoder2).
_THAI = re.compile("[\u0e00-\u0e7f]")
_THAI_TOKENS = 1.1
#: A run of at least this many non-whitespace characters is counted by its content, not by
#: the word counter, which takes it for one or two words.
_RUN = re.compile(r"\S{64,}")
#: Hex: at least a token per 1.135 characters (Qwen3); base64: per 1.351 (Qwen3); anything
#: else without spaces, such as minified CSS or JS: per 2.598 (StarCoder2).
_HEX = re.compile(r"[0-9A-Fa-f]+")
_HEX_CHARS_PER_TOKEN = 1.1
_BASE64 = re.compile(r"[A-Za-z0-9+/=_-]+")
_BASE64_CHARS_PER_TOKEN = 1.35
_RUN_CHARS_PER_TOKEN = 2.5
#: Quotes, brackets and separators that wrap a value in a printed structure.
_WRAPPING = "'\"`,;:()[]{}<>"
#: Everything else: "1 token ≈ 4 characters" of English text (OpenAI Help Center, "What are
#: tokens and how to count them", read 2026-09-30).
_CHARS_PER_TOKEN = 4

_BODY = (
    "about {tokens:,} tokens, more than a "
    "quarter of this model's {window:,}-token context window. It is saved unchanged in "
    "{path} ({lines:,} lines, {bytes:,} bytes, sha256 {sha256}). Its beginning and its end "
    "follow; the middle is only in the file.{gap}{how}]\n\n"
    "{head}\n\n[... {hidden:,} characters not shown ...]\n\n{tail}"
)
#: How each kind of offloaded message opens. ``person``: the message that opens the
#: running turn. ``earlier``: a message of the person's already in the conversation (a
#: reopened session, a fold's copy, a steer), which must not read as a new request: a model
#: told "the user's message" redoes work it finished. ``other``: text the harness wrote in
#: the user's role. Each has its own opening, so `original` reads only the person's back.
OPENINGS = {
    "person": "[The user's message was too long to send whole: ",
    "earlier": "[An earlier message from the user was too long to keep in the context window: ",
    "other": "[This message was too long to keep in the context window: ",
}
_NOTICE = OPENINGS["person"] + _BODY
_PREFIX = OPENINGS["person"]
_PERSON_PREFIXES = (OPENINGS["person"], OPENINGS["earlier"])
#: How a capped instruction in a fold's seed says what is not shown (`cap`).
_CAPPED = "\n\n[... {hidden:,} characters of this request are not shown here.{where}]\n\n"
_WHERE = re.compile(
    r"It is saved unchanged in (?P<path>.+?) \((?P<lines>[\d,]+) lines, (?P<bytes>[\d,]+) "
    r"bytes, sha256 (?P<sha256>[0-9a-f]{64})\)\."
)


@dataclass(frozen=True, slots=True)
class Inbound:
    """What the model is sent for one message from the person."""

    text: str
    #: Where the message was saved whole; None when it was sent as it is.
    artifact: ArtifactRef | None
    #: Its size, as `size` counts it.
    original_tokens: int

    @property
    def offloaded(self) -> bool:
        return self.artifact is not None


def _floor(text: str) -> int:
    """The least ``text`` can cost, by what it is made of (see the floors above)."""
    wide = len(_WIDE.findall(text))
    thai = len(_THAI.findall(text))
    in_runs = 0
    runs = 0.0
    for match in _RUN.finditer(text):
        plain = _THAI.sub("", _WIDE.sub("", match.group()))
        in_runs += len(plain)
        # Classified without the quotes and punctuation around it: `'0102...',` in a dumped
        # dict is hex. Each character taken off counts as a token of its own.
        core = plain.strip(_WRAPPING)
        runs += len(plain) - len(core)
        if _HEX.fullmatch(core):
            runs += len(core) / _HEX_CHARS_PER_TOKEN
        elif _BASE64.fullmatch(core):
            runs += len(core) / _BASE64_CHARS_PER_TOKEN
        else:
            runs += len(core) / _RUN_CHARS_PER_TOKEN
    rest = len(text) - wide - thai - in_runs
    return int(wide * _WIDE_TOKENS + thai * _THAI_TOKENS + runs + rest / _CHARS_PER_TOKEN)


def size(text: str) -> int:
    """Tokens ``text`` is taken to cost: the loop's count (`provider.tokens.count`), but
    never less than its scripts and long unspaced runs cost in the measured tokenizers
    (`_floor`), so neither slips under the line. Ordinary prose is counted exactly as the
    loop counts it."""
    return max(tokens.count(text), _floor(text))


def limit_tokens(window_tokens: int | None) -> int | None:
    """The most one message may take of a ``window_tokens`` window before it is offloaded.
    None when the window is unknown, and then nothing is offloaded."""
    if not window_tokens or window_tokens <= 0:
        return None
    return int(window_tokens * SHARE)


def exceeds(text: str, window_tokens: int | None) -> bool:
    """Whether ``text`` is over the line for a ``window_tokens`` window."""
    limit = limit_tokens(window_tokens)
    return limit is not None and size(text) > limit


def _head(text: str, budget: int) -> str:
    """The start of ``text``, at most ``budget`` tokens, ending on a line break when one
    is close to where the budget ends."""
    chars = min(len(text), max(1, budget) * _CHARS_PER_TOKEN)
    piece = text[:chars]
    while chars > 1 and size(piece) > budget:
        chars = chars * 3 // 4
        piece = text[:chars]
    cut = piece.rfind("\n")
    return piece[:cut] if cut >= len(piece) * 4 // 5 else piece


def _tail(text: str, budget: int) -> str:
    """The end of ``text``, at most ``budget`` tokens, starting on a line break when one
    is close to where the budget starts."""
    chars = min(len(text), max(1, budget) * _CHARS_PER_TOKEN)
    piece = text[-chars:]
    while chars > 1 and size(piece) > budget:
        chars = chars * 3 // 4
        piece = text[-chars:]
    cut = piece.find("\n")
    return piece[cut + 1 :] if 0 <= cut <= len(piece) // 5 else piece


def _gap(text: str, head: str, tail: str) -> tuple[int, int]:
    """The first and last line not shown whole between ``head`` and ``tail``, 1-based and
    counted the way `read` counts them; first > last when every line is shown whole."""
    shown_head = head.count("\n") + (1 if text[len(head) : len(head) + 1] == "\n" else 0)
    start = len(text) - len(tail)
    first_tail_line = text.count("\n", 0, start) + 1
    starts_a_line = start == 0 or text[start - 1] == "\n"
    shown_from = first_tail_line if starts_a_line else first_tail_line + 1
    return shown_head + 1, shown_from - 1


def _gap_said(first: int, last: int) -> str:
    if first > last:
        return ""
    if first == last:
        return f" Line {first:,} is not shown whole here."
    return f" Lines {first:,}–{last:,} are not shown whole here."


def _how(
    path: Path,
    tools: Collection[str],
    text: str,
    first: int,
    line_budget_tokens: int | None,
) -> str:
    """How to read it back, naming only tools the model has. Sizes are in bytes: GNU
    `cut -c` counts characters or bytes depending on the locale (coreutils 9.11 here),
    while `cut -b` and `head -c` always count bytes."""
    quoted = shlex.quote(str(path))
    lines = text.split("\n")
    if "\x00" in text:
        # `read` refuses a file with a NUL in its first 8 KiB as binary
        # (`tools.files._looks_binary`), and ripgrep answers "binary file matches" without
        # the lines (ripgrep 15.2.0 here).
        if "shell" in tools:
            return (
                " It contains NUL bytes, so read and grep take the file for binary: read it "
                f"with shell instead, e.g. `tr -d '\\000' < {quoted} | sed -n "
                f"'{first},{first + _SLICE_LINES - 1}p'`."
            )
        return (
            " It contains NUL bytes, so read and grep take the file for binary, and no tool "
            "here can read it back."
        )
    said: list[str] = []
    if "read" in tools:
        said.append(
            "Read it with read(path, offset, limit) -- offset is a 1-based line number; "
            f"what is not shown here starts at offset={first}."
        )
    elif "shell" in tools:
        said.append(
            f"Read it with shell, e.g. `sed -n '{first},{first + _SLICE_LINES - 1}p' {quoted}`."
        )
    if "grep" in tools:
        said.append("Find text in it with grep(pattern, path).")
    number = max(range(len(lines)), key=lambda index: len(lines[index])) + 1
    longest = lines[number - 1]
    if line_budget_tokens is not None and size(longest) > line_budget_tokens:
        size_bytes = len(longest.encode("utf-8"))
        if "shell" in tools:
            said.append(
                f"Line {number:,} alone is {size_bytes:,} bytes, more than read shows at once: "
                f"take it in slices with shell, e.g. `sed -n '{number}p' {quoted} | cut -b "
                f"1-{_SLICE_BYTES}`, then `cut -b {_SLICE_BYTES + 1}-{2 * _SLICE_BYTES}`."
            )
        else:
            said.append(f"Line {number:,} alone is {size_bytes:,} bytes.")
    if not said:
        said.append("No tool here can read it back.")
    return " " + " ".join(said)


def fit(
    text: str,
    *,
    window_tokens: int | None,
    store: ArtifactStore | None,
    tools: Collection[str] = (),
    line_budget_tokens: int | None = None,
    notice: str = "person",
) -> Inbound:
    """``text`` as the model gets it. Unchanged (the same string) unless it takes more than
    `SHARE` of ``window_tokens``. Then it is saved whole in ``store`` and replaced by a
    notice with its beginning, its end, the file and how to read it back with ``tools``
    (the names the model can call). ``line_budget_tokens`` is the most `read` returns
    whole; a longer line gets its own advice. ``notice`` picks the opening (`OPENINGS`).
    With no ``store``, or one that cannot be written, nothing could be read back, so nothing
    is offloaded. The file is named by its content, so the same text is saved once."""
    limit = limit_tokens(window_tokens)
    if limit is None or store is None:
        return Inbound(text, None, 0)
    counted = size(text)
    if counted <= limit:
        return Inbound(text, None, counted)

    try:
        reference = store.stash(text, kind="user_message")
    except OSError as exc:
        # Nowhere to put it (full disk, read-only project): send it as is and let the
        # provider decide. A turn is never lost to this.
        log.warning("a %d-token message could not be offloaded, so it goes whole: %s", counted, exc)
        return Inbound(text, None, counted)
    side = max(1, min(EXCERPT_TOKENS, limit // 2) // 2)
    head, tail = _head(text, side), _tail(text, side)
    if len(head) + len(tail) >= len(text):
        tail = text[len(head) :]
    first, last = _gap(text, head, tail)
    written = (OPENINGS[notice] + _BODY).format(
        tokens=counted,
        window=window_tokens,
        path=reference.path,
        lines=reference.lines,
        bytes=reference.bytes,
        sha256=reference.sha256,
        gap=_gap_said(first, last),
        how=_how(reference.path, tools, text, first, line_budget_tokens),
        head=head,
        hidden=len(text) - len(head) - len(tail),
        tail=tail,
    )
    return Inbound(written, reference, counted)


def excerpt(text: str, budget_tokens: int) -> str:
    """``text`` cut to its beginning and end, about ``budget_tokens`` together, with how
    many characters are left out between them. Nothing is saved: for a copy of a message
    whose original is kept elsewhere (a fold's summary request reads the parent's
    messages, which stay in the store)."""
    if size(text) <= budget_tokens:
        return text
    side = max(1, budget_tokens // 2)
    head, tail = _head(text, side), _tail(text, side)
    hidden = len(text) - len(head) - len(tail)
    return f"{head}\n\n[... {hidden:,} characters not shown here ...]\n\n{tail}"


def _where(path: Path, tools: Collection[str]) -> str:
    """Where a capped request is kept, and how to open it with the tools there are."""
    said = [f" The whole request is saved in {path}."]
    if "read" in tools:
        said.append("Read it with read(path, offset, limit).")
    elif "shell" in tools:
        quoted = shlex.quote(str(path))
        said.append(f"Read it with shell, e.g. `sed -n '1,{_SLICE_LINES}p' {quoted}`.")
    if "grep" in tools:
        said.append("Find text in it with grep(pattern, path).")
    return " ".join(said)


def cap(
    text: str, budget_tokens: int, *, store: ArtifactStore | None, tools: Collection[str] = ()
) -> str:
    """``text`` in at most about ``budget_tokens``: unchanged when it fits, else its
    beginning and end with a line saying how much is not shown and where the whole of it is
    saved (in ``store``, once per content) and how to read it with ``tools``. For an
    instruction in a fold's seed (`compact.render_tasks`): a page pasted as an instruction
    would otherwise ride whole in every handover."""
    if size(text) <= budget_tokens:
        return text
    where = ""
    if store is not None:
        try:
            where = _where(store.stash(text, kind="user_message").path, tools)
        except OSError as exc:
            log.warning("a capped request could not be saved: %s", exc)
    marker = _CAPPED.format(hidden=len(text), where=where)
    side = max(1, (budget_tokens - size(marker)) // 2)
    while True:
        head, tail = _head(text, side), _tail(text, side)
        if len(head) + len(tail) >= len(text):
            return text
        capped = head + _CAPPED.format(hidden=len(text) - len(head) - len(tail), where=where) + tail
        if size(capped) <= budget_tokens or side <= 1:
            return capped
        side = max(1, side * 3 // 4)


def original(text: str, project_root: Path) -> str | None:
    """The person's message as they wrote it, when ``text`` is a notice `fit` wrote for one
    of theirs (the ``person`` or ``earlier`` opening) and the
    file it names still holds exactly what was saved: under this project's artifacts, with
    the digest the notice gives. None for anything else: a message sent whole, a file since
    changed or removed, or a typed notice naming some other file."""
    if not text.startswith(_PERSON_PREFIXES):
        return None
    return saved(text, project_root)


def saved(text: str, project_root: Path) -> str | None:
    """What any notice `fit` wrote (every opening in `OPENINGS`) replaced, read back from
    the file it names under the same checks as `original`. None for anything else."""
    if not text.startswith(tuple(OPENINGS.values())):
        return None
    where = _WHERE.search(text)
    if where is None:
        return None
    saved = Path(where.group("path"))
    root = (paths.project_dir(project_root) / "artifacts").resolve()
    try:
        resolved = saved.resolve()
        resolved.relative_to(root)
        content = resolved.read_bytes()
    except (OSError, ValueError):
        return None
    if hashlib.sha256(content).hexdigest() != where.group("sha256"):
        return None
    return content.decode("utf-8", errors="replace")
