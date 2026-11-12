"""Run state and the stop conditions.

Every budget is checked in one place (`check`), so there is one notion of "too long" and
it is enforced.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from cogito.cancel import CancelToken
from cogito.config.settings import BudgetSettings

#: The source listing lives in `tools.project_files` now, where `read` also uses it to name
#: close files. Re-exported so the engine's import stays as it was.
from cogito.tools.project_files import known_source_paths as known_source_paths


def tree_digest(root: Path, paths: Iterable[str]) -> str:
    """A cheap fingerprint of exactly the given files, to notice a shell call changed one.

    Content is never read: (path, size, mtime) catches a write and is cheap enough to run
    after every shell call. `paths` is `Engine._known_paths`, the session's pre-shell
    baseline (`known_source_paths`) plus `ToolResult.files_touched`. A file outside it that
    was never read, edited or written (a build's `.class`, a compiler's `.js`, a linker's
    binary) is ignored on purpose: counting it as a change is the false positive that let a
    `javac`/`go build`/`tsc` re-run loop with no real edit look like progress. A path that
    no longer exists is hashed as absent, which still counts as a change.

    The measured cost is in the commit that introduced this function.
    """
    hasher = hashlib.sha1()
    for rel in sorted(paths):
        try:
            stat = (root / rel).stat()
        except OSError:
            hasher.update(f"{rel}:GONE\n".encode())
            continue
        hasher.update(f"{rel}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return hasher.hexdigest()


class Phase(StrEnum):
    """What the run is doing. The desktop orb renders these directly."""

    SAMPLING = "sampling"
    PARSING = "parsing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPACTING = "compacting"
    DONE = "done"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_STEPS = "max_steps"
    WALL_CLOCK = "wall_clock"
    TOKEN_BUDGET = "token_budget"
    USER_INTERRUPT = "user_interrupt"
    CONSECUTIVE_TOOL_ERRORS = "consecutive_tool_errors"
    NO_PROGRESS = "no_progress"
    PROVIDER_FAILURE = "provider_failure"
    #: A reply cut off at `BudgetSettings.max_output_tokens_per_step`, with its truncation
    #: notices exhausted (`Engine._TRUNCATED_NOTICE`, `max_truncation_notices`), so the turn
    #: ends on the cut text rather than sampling again. Distinct from `END_TURN`: the model
    #: never decided it was done, the limit cut it off mid-reply.
    OUTPUT_LIMIT = "output_limit"


def fingerprint(name: str, arguments: dict[str, object]) -> str:
    """A stable identity for one tool call, so repetition can be detected."""
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(f"{name}:{canonical}".encode()).hexdigest()[:16]


@dataclass
class RunState:
    cancel: CancelToken
    started_at: float = field(default_factory=time.monotonic)
    step: int = 0
    phase: Phase = Phase.SAMPLING
    input_tokens: int = 0
    output_tokens: int = 0
    consecutive_tool_errors: int = 0
    #: How many error streaks this turn became a notice instead of a stop
    #: (`BudgetSettings.max_tool_error_notices`).
    tool_error_notices: int = 0
    #: How many times the conversation was folded during this run. Reported because a run
    #: that compacted three times and one that never did are not comparable measurements.
    compactions: int = 0
    #: Repeat counts since something last changed: a workspace change, a steer or the build
    #: gate's message (`reset_repeats` clears it). Not over the whole run, so a call repeated
    #: across a real edit-test-fix cycle never builds toward NO_PROGRESS.
    #:
    #: The source baseline and last shell digest are not here: they are per session
    #: (`Engine._known_paths`, `Engine._last_tree_digest`), because a fresh RunState is
    #: built for every turn (`cli.py`, `server/app.py`) and a build byproduct left by an
    #: earlier turn must not be taken for source when a later turn's first `shell` call sees
    #: it.
    calls: Counter[str] = field(default_factory=Counter)
    #: `web_search` calls since the last `web_fetch` or workspace change (`reset_repeats`).
    #: Near-duplicate queries are different fingerprints, so `calls` never sees a model that
    #: only searches. Moves only for a `web_search` the registry holds, so a session without
    #: the web tools never changes it.
    searches_without_fetch: int = 0
    #: The cleaned prose of the previous step's reply, and how many steps in a row have
    #: repeated it. A model that keeps narrating the same thing while nudging one tool
    #: argument (`find -name "*package*"` then `*update*` then `ls | grep`) makes a fresh
    #: `fingerprint` every step, so `calls` never sees a loop; the identical prose does.
    #: Empty prose (a bare call with no narration) never counts. Cleared by `reset_repeats`
    #: on real progress, like the call streak.
    last_reply_prose: str = ""
    identical_replies: int = 0
    stop_reason: StopReason | None = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def charge(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def note_call(self, name: str, arguments: dict[str, object]) -> int:
        """Record a call and return how many times it has been repeated since progress,
        meaning since `reset_repeats` last cleared the counts, not over the whole run.
        """
        key = fingerprint(name, arguments)
        self.calls[key] += 1
        return self.calls[key]

    def note_reply(self, prose: str) -> int:
        """Record this step's reply prose and return how many steps in a row have
        repeated it, counting this one. Non-empty prose identical to the previous step's
        extends the streak; anything else restarts it. Empty prose resets it to zero, so a
        bare tool call with no narration never builds toward NO_PROGRESS.
        """
        if prose and prose == self.last_reply_prose:
            self.identical_replies += 1
        else:
            self.identical_replies = 1 if prose else 0
        self.last_reply_prose = prose
        return self.identical_replies

    def reset_repeats(self) -> None:
        """Forget every identical-call streak counted so far.

        Called when the workspace changed: after a successful edit/write and after a shell
        call whose tree digest moved. A call made before that point is not the same
        observation any more, even with identical name and arguments. Without it, a real
        edit -> re-run-tests -> fix loop looks like a model stuck resending one dead call
        (NO_PROGRESS).

        Called too when a steer joins the transcript: the person just changed the
        conversation, so repeats of a call before the correction and after it are two
        situations, not one streak toward `NO_PROGRESS`. `consecutive_tool_errors` is left
        alone: a steer says nothing about whether a tool works, only that the task changed.

        Clears the web-search count too: it has the same meaning, searches since something
        changed.
        """
        self.calls.clear()
        self.searches_without_fetch = 0
        self.last_reply_prose = ""
        self.identical_replies = 0

    def note_tool_outcome(self, *, ok: bool) -> None:
        """``ok`` is False only for a failure of the tool itself; see `is_tool_failure`."""
        self.consecutive_tool_errors = 0 if ok else self.consecutive_tool_errors + 1

    def take_tool_error_notice(self, budget: BudgetSettings) -> bool:
        """Whether this streak becomes a notice instead of a stop: it reached
        `max_consecutive_tool_errors` and this turn has notices left. Clears the streak when
        it does, so `check` does not stop on it."""
        if (
            self.consecutive_tool_errors < budget.max_consecutive_tool_errors
            or self.tool_error_notices >= budget.max_tool_error_notices
        ):
            return False
        self.tool_error_notices += 1
        self.consecutive_tool_errors = 0
        return True


def is_tool_failure(name: str, *, is_error: bool, error_kind: str, exit_code: object) -> bool:
    """Whether a result counts toward `max_consecutive_tool_errors`.

    Only a failure of the tool itself does: arguments it could not use, a call the guard
    refused, a tool that does not exist, an edit that did not resolve, a tool that raised.
    A `shell` command that ran and exited non-zero is not one of those. bash reported a
    status -- a compile that failed, tests that failed (`cargo test` exits 101), a binary
    that is not installed (127), or the harness's own timeout (124) -- and that status is
    what the model needs to read next, not evidence that the tool is broken. The result
    stays an error for the model and for the graph; only this counter stops reading it as
    one. `search.py`'s `_nothing_found` made the same call for a search with no matches.

    Evidence (quick controls at e12e48e): replayed under this rule, 21 of the 40 failed
    runs this counter stopped would not have been stopped at that point -- qwen3-coder-30b
    8 of 8, qwen3.5-9b 11 of 14, gemma-4-12b 2 of 18 -- e.g. qwen3-coder-30b's
    impl-cpp-knapsack-1, three failing compiles in a row while fixing them. Repetition
    stays bounded without it: the identical-call streak (`max_identical_calls`) still
    stops a model re-running one failing command -- qwen3.5-9b's java-bug-01-role-check-1
    is stopped at the same step that way -- and `max_steps` bounds everything else.

    A negative exit status is not a status bash reported: the shell died before the
    harness could read one, which still counts, as it always did.
    """
    if not is_error:
        return False
    ran = name == "shell" and error_kind == "nonzero_exit"
    return not (ran and isinstance(exit_code, int) and exit_code > 0)


def check(state: RunState, budget: BudgetSettings) -> StopReason | None:
    """The single place a run is allowed to decide it should stop."""
    if state.cancel.cancelled:
        return StopReason.USER_INTERRUPT
    if budget.max_steps > 0 and state.step >= budget.max_steps:
        return StopReason.MAX_STEPS
    if budget.wall_clock_seconds > 0 and state.elapsed >= budget.wall_clock_seconds:
        return StopReason.WALL_CLOCK
    # Token use is counted and shown (the context ring, an API's cost), never a reason to
    # stop: the person decided on 2026-10-25 that Cogito does not cap a session by tokens.
    # `max_steps` and the wall clock bound the work; folding keeps the window in range.
    if state.consecutive_tool_errors >= budget.max_consecutive_tool_errors:
        return StopReason.CONSECUTIVE_TOOL_ERRORS
    if state.calls and max(state.calls.values()) >= budget.max_identical_calls:
        return StopReason.NO_PROGRESS
    limit = budget.max_identical_replies
    if limit > 0 and state.identical_replies >= limit:
        return StopReason.NO_PROGRESS
    limit = budget.max_searches_without_fetch
    if limit > 0 and state.searches_without_fetch >= limit:
        return StopReason.NO_PROGRESS
    return None
