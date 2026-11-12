"""How each shell tells the core that a command ended, and with what status.

When the shell is about to read a line it prints a private OSC that xterm.js ignores:
``ESC ] 6973 ; cogito ; <nonce> ; prompt ; <status> [; ready] BEL`` (`screen.PROMPT_MARK`).
With ``; ready``, a second mark follows once the prompt is drawn and the line editor reads:
``ESC ] 6973 ; cogito ; <nonce> ; ready BEL``. A line is typed into a new tab only after it:
a prompt that takes seconds to draw (a git prompt) is drawn after the first mark, and what is
typed before the editor reads it goes in as plain keys. The nonce
is random for each host and is written only into our startup files, in the host's 0700
folder, never into the environment, so a program's output cannot fake a prompt by printing
the mark (`screen.Screen` ignores one without it). The person's own startup files run
first, then ours:

- bash: ``--init-file`` of ours that sources ``~/.bashrc``, then puts a function that keeps
  the status first in ``PROMPT_COMMAND`` (a string, or an array since bash 5.1) and the one
  that prints the mark last, so a prompt hook of theirs has run before a line is typed. That
  last one also puts ``\\[${__cogito_ready}\\]`` at the end of PS1 when it is missing (the
  person's prompt hook may set PS1 anew each time); readline draws it last, after it turned
  bracketed paste on. With ``promptvars`` off, or PS1 readonly, it cannot, and the mark
  promises no ready.
- zsh: a ``ZDOTDIR`` of ours. Its ``.zshenv`` and ``.zshrc`` run the person's from their
  own ``ZDOTDIR`` (or ``$HOME``) and restore it. A function first in ``precmd_functions``
  keeps the status; the mark is printed from ``zle-line-init``, once every precmd function
  has run: Powerlevel10k's setup wizard runs in its own precmd function, after ours, and
  reads (and first throws away) whatever was typed. The prompt is drawn by then, so the
  ready mark follows at once (zsh turns bracketed paste on just after that hook).
- fish: ``--init-command`` sourcing a file of ours (run after config.fish), with a
  ``fish_postexec`` handler that keeps the status and ``fish_prompt`` handler that prints
  it. The ready mark is a ``fish_right_prompt`` of ours, defined only when the person has
  none: fish draws it with the left prompt, after turning bracketed paste on. Their
  ``fish_prompt`` is never touched, so ``funcsave fish_prompt`` saves their own; ours prints
  a variable only these tabs set, so a saved copy prints nothing elsewhere. With a right
  prompt of their own the mark promises no ready mark. fish draws no right prompt when the
  left one leaves it no room; the host then takes bracketed paste, on for 1 s with no ready
  mark, as the editor being up (`host._READY_LATE`). A line fish rejects
  (``fish_posterror``) or one the person clears with Ctrl-C (``fish_cancel``) draws no new
  prompt, so those print the mark too, promising no ready mark. ``no-query-term``,
  since fish 4.1+ otherwise waits up to 10 s for a terminal to answer its queries (4.9.3
  here).

History expansion is turned off in bash and zsh (``set +H``, ``setopt no_bang_hist``): an
approved ``echo "x!e"`` must not become the person's last command starting with ``e``.

Any other shell runs without it; then a command's end is unknown (`Launch.integrated`).
"""

from __future__ import annotations

import os
import pwd
from dataclasses import dataclass, field
from pathlib import Path


def _mark(nonce: str) -> str:
    """The prompt mark, for printf with the status and ``;ready`` (or nothing) after it."""
    return rf"\033]6973;cogito;{nonce};prompt;%s%s\007"


def _ready(nonce: str) -> str:
    return rf"\033]6973;cogito;{nonce};ready\007"


def _bash(nonce: str) -> str:
    return rf"""# Cogito's terminal: the person's ~/.bashrc, then the prompt hook.
if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi
set +H
__cogito_ready=$'{_ready(nonce)}'
__cogito_keep() {{ __cogito_last=$?; set +H; return $__cogito_last; }}
# True when PS1 is readonly: assigning it then aborts the whole prompt hook.
if (( BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 404 )); then
  eval '__cogito_fixed() {{ [[ ${{PS1@a}} == *r* ]]; }}'
else
  __cogito_fixed() {{
    local d
    d=$(declare -p PS1 2>/dev/null) || return 1
    d=${{d#declare -}}
    [[ ${{d%% *}} == *r* ]]
  }}
fi
__cogito_prompt() {{
  local more=
  # The ready mark goes at the end of PS1, so it comes once the prompt is drawn; put back
  # whenever the person's prompt was replaced. The variable keeps the nonce out of PS1,
  # which some exported. A readonly PS1 is left alone and the mark promises no ready.
  if shopt -q promptvars; then
    case $PS1 in
      *'\[${{__cogito_ready}}\]'*) more=';ready' ;;
      *) if ! __cogito_fixed; then PS1+='\[${{__cogito_ready}}\]'; more=';ready'; fi ;;
    esac
  fi
  printf '{_mark(nonce)}' "$__cogito_last" "$more"
  return $__cogito_last
}}
if [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
  PROMPT_COMMAND=(__cogito_keep "${{PROMPT_COMMAND[@]}}" __cogito_prompt)
else
  PROMPT_COMMAND="__cogito_keep"$'\n'"${{PROMPT_COMMAND}}"$'\n'"__cogito_prompt"
fi
"""


_ZSHENV = r"""# Cogito's terminal: the person's .zshenv, from their own ZDOTDIR.
__cogito_dir=$ZDOTDIR
ZDOTDIR=${COGITO_USER_ZDOTDIR:-$HOME}
if [[ -f "$ZDOTDIR/.zshenv" ]]; then source "$ZDOTDIR/.zshenv"; fi
COGITO_USER_ZDOTDIR=$ZDOTDIR
ZDOTDIR=$__cogito_dir
unset __cogito_dir
"""


def _zshrc(nonce: str) -> str:
    return rf"""# Cogito's terminal: the person's .zshrc, then the prompt hook.
ZDOTDIR=${{COGITO_USER_ZDOTDIR:-$HOME}}
unset COGITO_USER_ZDOTDIR
if [[ -f "$ZDOTDIR/.zshrc" ]]; then source "$ZDOTDIR/.zshrc"; fi
setopt no_bang_hist
typeset -gi __cogito_last=0 __cogito_due=0 __cogito_zle=0
__cogito_mark() {{ printf '{_mark(nonce)}' "$__cogito_last" "$1"; }}
__cogito_precmd() {{
  local s=$?
  __cogito_last=$s
  setopt no_bang_hist
  if (( __cogito_zle )); then __cogito_due=1; else __cogito_mark; fi
  return $s
}}
__cogito_line_init() {{
  # The line editor is up and the prompt drawn: both marks.
  if (( __cogito_due )); then
    __cogito_due=0
    __cogito_mark ';ready'
    printf '{_ready(nonce)}'
  fi
}}
precmd_functions=(__cogito_precmd $precmd_functions)
if zmodload zsh/zle 2>/dev/null && autoload -Uz add-zle-hook-widget 2>/dev/null \
    && add-zle-hook-widget line-init __cogito_line_init 2>/dev/null; then
  __cogito_zle=1
fi
"""


def _fish(nonce: str) -> str:
    mark = rf"printf '\e]6973;cogito;{nonce};prompt;%s%s\a'"
    ready = rf"printf '\e]6973;cogito;{nonce};ready\a'"
    return f"""# Cogito's terminal: the prompt hook, after config.fish.
set -g __cogito_status 0
set -g __cogito_ready_mark ({ready})
function __cogito_postexec --on-event fish_postexec
    set -g __cogito_status $status
end
# The ready mark is the right prompt, which fish draws with the left one, when the person
# has none of their own. Their fish_prompt is never touched. It prints a variable only these
# tabs set, so a copy saved with funcsave prints nothing in any other fish and is still
# taken for ours here. Returns 1 when the right prompt is the person's.
function __cogito_right
    if not functions -q fish_right_prompt
        # fish draws no right prompt without a visible width: the mark, then one space.
        function fish_right_prompt
            set -q __cogito_ready_mark[1]; and printf '%s ' $__cogito_ready_mark
        end
    end
    functions fish_right_prompt | string match -q '*__cogito_ready_mark*'
end
function __cogito_prompt --on-event fish_prompt
    if __cogito_right
        {mark} $__cogito_status ';ready'
    else
        {mark} $__cogito_status ''
    end
end
function __cogito_posterror --on-event fish_posterror
    set -g __cogito_status 123
    {mark} $__cogito_status ''
end
function __cogito_cancel --on-event fish_cancel
    {mark} $__cogito_status ''
end
"""


def _write_private(path: Path, text: str) -> None:
    """Our startup file, readable by the person alone (it holds the nonce)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _fish_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


@dataclass(frozen=True, slots=True)
class Launch:
    """The argv and environment changes that start one shell."""

    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    #: The shell's name: "bash", "zsh", "fish" or another.
    kind: str = ""
    #: True when the shell reports each prompt with its status.
    integrated: bool = False


def person_shell(env: dict[str, str]) -> str:
    """The person's shell: ``$SHELL``, else their passwd entry, else ``/bin/bash``."""
    shell = env.get("SHELL", "").strip()
    if shell and os.access(shell, os.X_OK):
        return shell
    try:
        entry = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        entry = ""
    if entry and os.access(entry, os.X_OK):
        return entry
    return "/bin/bash"


def kind_of(shell: str) -> str:
    name = Path(shell).name
    return name[1:] if name.startswith("-") else name


def launch(shell: str, state_dir: Path, env: dict[str, str], nonce: str = "") -> Launch:
    """How to start ``shell`` interactive with the prompt hook, writing what it needs into
    ``state_dir`` (one per host, 0700) with the host's ``nonce`` in its mark."""
    kind = kind_of(shell)
    if kind == "bash":
        rc = state_dir / "bashrc"
        if not rc.exists():
            _write_private(rc, _bash(nonce))
        return Launch((shell, "--init-file", str(rc), "-i"), {}, kind, True)
    if kind == "zsh":
        zdot = state_dir / "zsh"
        zdot.mkdir(mode=0o700, exist_ok=True)
        for name, text in ((".zshenv", _ZSHENV), (".zshrc", _zshrc(nonce))):
            if not (zdot / name).exists():
                _write_private(zdot / name, text)
        changes = {"ZDOTDIR": str(zdot)}
        if env.get("ZDOTDIR"):
            changes["COGITO_USER_ZDOTDIR"] = env["ZDOTDIR"]
        return Launch((shell, "-i"), changes, kind, True)
    if kind == "fish":
        rc = state_dir / "cogito.fish"
        if not rc.exists():
            _write_private(rc, _fish(nonce))
        # fish 4.1+ asks the terminal what it supports and waits up to 10 s for the answer;
        # a tab nobody is looking at yet (one the model opened) has no xterm to answer.
        argv = (
            shell, "--interactive", "--features", "no-query-term",
            "--init-command", f"source {_fish_quote(str(rc))}",
        )
        return Launch(argv, {}, kind, True)
    return Launch((shell, "-i"), {}, kind, False)
