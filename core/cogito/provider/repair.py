"""Turns what a model emitted into a call that can be made.

Calls come from the endpoint's native `tool_calls`, and from one text form: a
`<tool_call>` block, the Qwen/Hermes chat-template format, in case the server leaves it
in the text instead of converting it (`xml_tool_call`): a JSON envelope or Qwen's
`<function=...>` body. A server leaves one there beside native calls when it names a tool
the request did not list, so it is read beside them too (`normalize.extract`). Since 2026-10-30
no other text form is read: the supported models send native calls. Anything else that
looks like a call is recognised (`text_call`) and never run; the loop answers it with its
repair prompt so the turn goes on.

A call found either way can still be wrong: a model trained on another harness sends
`file_path` to a tool whose parameter is `path`, or arguments that are near-miss JSON.
Rejecting that costs a retry; fixing it is a dict lookup or a capped parse.

Every function here is pure. They are tested against a corpus of real malformed output.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from cogito.ids import new_id
from cogito.provider.base import ToolSpec, ToolUseBlock

_MAX_JSON_MUTATIONS = 3


# --------------------------------------------------------------------------------------
# Extraction: the one text form that is read
# --------------------------------------------------------------------------------------


def _block(name: str, arguments: Any, raw: str, loose: bool = False) -> ToolUseBlock | None:
    if not isinstance(arguments, Mapping):
        return None
    return ToolUseBlock(
        id=new_id(),
        name=str(name),
        input=dict(arguments),
        raw=raw,
        repaired_by=(LOOSE_JSON,) if loose else (),
    )


def _from_envelope(payload: Any, raw: str, loose: bool = False) -> ToolUseBlock | None:
    """Accept the several shapes that all mean "call this tool with these arguments".

    ``loose`` says `loose_json` had to change the text the payload came from.
    """
    if not isinstance(payload, Mapping):
        return None
    name = payload.get("name") or payload.get("tool") or payload.get("function")
    if isinstance(name, Mapping):  # {"function": {"name": ..., "arguments": ...}}
        return _from_envelope(name, raw, loose)
    if not isinstance(name, str):
        return None
    for key in ("arguments", "parameters", "args", "input", "tool_input"):
        if key in payload:
            arguments = payload[key]
            if isinstance(arguments, str):
                arguments, changed = loose_json(arguments)
                loose = loose or changed
            return _block(name, arguments, raw, loose)
    return _block(name, {}, raw, loose)


_FUNCTION = re.compile(r"<function=([^>\n]+)>\s*(.*?)\s*</function>\Z", re.DOTALL)
_PARAMETER = re.compile(r"<parameter=([^>\n]+)>(.*?)</parameter>", re.DOTALL)


def _function_call(body: str, tools: Mapping[str, ToolSpec]) -> ToolUseBlock | None:
    """`<function=NAME><parameter=KEY>value</parameter>...</function>`: the body Qwen3-Coder
    and the Qwen3.5+ templates write inside `<tool_call>`, qwen3.8-flash-motoris's included.

    Read as vLLM's Qwen Coder parser reads it (rust/src/parser/src/tool/qwen_coder.rs, read
    2026-10-31): one wrapping newline is taken off each value, and a value is converted only
    by the type its parameter declares. Numbers and booleans are left to `coerce`; an object
    or array is parsed here, since `coerce` does not parse JSON.
    """
    match = _FUNCTION.fullmatch(body.strip())
    if match is None:
        return None
    name = match.group(1).strip()
    spec = tools.get(resolve_name(name, tools)[0])
    properties = spec.parameters.get("properties", {}) if spec is not None else {}
    arguments: dict[str, Any] = {}
    for key, value in _PARAMETER.findall(match.group(2)):
        key = key.strip()
        value = value.removeprefix("\n").removesuffix("\n")
        if properties.get(key, {}).get("type") in ("object", "array"):
            parsed, _ = loose_json(value)
            if isinstance(parsed, dict | list):
                value = parsed
        arguments[key] = value
    return ToolUseBlock(id=new_id(), name=name, input=arguments, raw=body)


def xml_tool_call(text: str, tools: Mapping[str, ToolSpec]) -> list[ToolUseBlock] | None:
    """`<tool_call>...</tool_call>`, the Qwen and Hermes chat-template format: a JSON
    envelope inside, or Qwen's `<function=...>` body (`_function_call`)."""
    found: list[ToolUseBlock] = []
    for match in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL):
        body = match.group(1)
        if body.startswith("<function="):
            block = _function_call(body, tools)
        else:
            payload, loose = loose_json(body)
            block = _from_envelope(payload, body, loose)
        if block is not None:
            found.append(block)
    return found or None


#: The text extractors, in the order they are tried. One since 2026-10-30.
EXTRACTORS = (("xml_tool_call", xml_tool_call),)

#: What is left of a `<tool_call>` once the call inside it is taken out.
_EMPTY_WRAPPERS = re.compile(r"<tool_call>\s*</tool_call>")


def strip_calls(text: str, calls: Sequence[ToolUseBlock]) -> str:
    """A reply's text without the `<tool_call>` calls read from it.

    Otherwise each call would be shown twice in the app -- once as text in the answer, once
    as the tool's own row (QA 2026-10-13) -- and kept twice in the transcript. Each call's
    `raw` is taken out once, then any wrapper it leaves empty. The prose around the calls
    stays as written.
    """
    for call in calls:
        if call.raw and call.raw in text:
            text = text.replace(call.raw, "", 1)
    text = _EMPTY_WRAPPERS.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_JSON_SPACE = " \t\r\n"
#: Strict about structure, lenient about control characters. Small models put raw newlines
#: and tabs inside strings, and that does not change where a value ends.
_DECODER = json.JSONDecoder(strict=False)


# --------------------------------------------------------------------------------------
# A call written in the text: recognised, never run
# --------------------------------------------------------------------------------------

#: The tool a JSON envelope names (`{"name": "read", ...}`).
_CALL_NAME = re.compile(r'"(?:name|tool)"\s*:\s*"([\w.-]+)"')
#: A call carries its arguments; a JSON block that only has a "name" is data (a
#: package.json for a package called "check" is not a call to `check`).
_CALL_ARGUMENTS = re.compile(r'"(?:arguments|parameters|args|input)"\s*:')
_FENCE_BODY = re.compile(r"```([\w-]*)[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL)
_TAGGED = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.DOTALL)
_TAG_NAME = re.compile(r'<function\s*=\s*([\w.-]+)\s*>|<invoke\s+name="([\w.-]+)"')


def text_call(text: str, tools: Mapping[str, ToolSpec]) -> str | None:
    """The registered tool a reply clearly tried to call in a form `xml_tool_call` does not
    read, or None. Such a call is never run (since 2026-10-30); the loop answers it with its
    repair prompt so the turn goes on instead of ending on the call shown as text.

    Clearly tried, and never an answer that shows one: an unclosed `<tool_call>`, a
    `<function=...>` outside one, an `<invoke name=...>` tag, a ```json or unlabelled block, a reply that is nothing but one
    fenced block whatever its label (dolphin3-qwen25-3b fenced a call as ```xml), or a reply
    that is itself an object. A block in another language with prose around it -- an example
    being explained, a diff, code shown to the person -- is left alone (bug 47).
    """
    for match in _TAG_NAME.finditer(text):
        name = match.group(1) or match.group(2)
        if name in tools:
            return name
    blocks = [body for body in _TAGGED.findall(text)]
    fences = _FENCE_BODY.findall(text)
    blocks.extend(body for label, body in fences if label.lower() in ("", "json", "tool_code"))
    stripped = text.strip()
    if len(fences) == 1 and re.fullmatch(r"```[\w-]*[ \t]*\n.*?(?:```)?", stripped, re.DOTALL):
        blocks.append(fences[0][1])
    if stripped.startswith("{"):
        blocks.append(stripped)
    for block in blocks:
        if not _CALL_ARGUMENTS.search(block):
            continue
        for name in _CALL_NAME.findall(block):
            if name in tools:
                return name
    return None


# --------------------------------------------------------------------------------------
# Loose JSON
# --------------------------------------------------------------------------------------


#: Names a call whose JSON parsed only when read leniently (`loose_json`, or
#: `native_arguments` for a native call). When such a call fails, the loop says so
#: (`loop/engine.py`, `_LOSSY_REPAIRS`).
LOOSE_JSON = "loose_json"
#: Marks a native call whose arguments are not JSON even read loosely. Never run:
#: `normalize.extract` turns it into a failure and counts it under this name.
INVALID_JSON = "invalid_json"


def parse_loose_json(text: str) -> Any:
    """Parse JSON, repairing a near miss. Returns None when it is not close.

    Capped at a few mutations on purpose. Anything needing more is not a typo, and guessing
    harder gives a wrong call instead of a clear failure.
    """
    return loose_json(text)[0]


def loose_json(text: str) -> tuple[Any, bool]:
    """`parse_loose_json`, and whether the text had to be changed to parse."""
    text = text.strip()
    if not text:
        return None, False
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        pass

    candidate = text
    for _ in range(_MAX_JSON_MUTATIONS):
        candidate = _mutate_json(candidate)
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            continue
    return None, False


def unterminated(text: str) -> bool:
    """True when ``text`` stops inside a string or with a bracket still open.

    `parse_loose_json` closes and parses such JSON anyway. A forgotten brace and a cut at
    the output limit look the same here. Only the caller knows whether the limit was hit,
    and then it matters: closing a cut string runs a call with content the model never
    finished.
    """
    return _ends_inside_string(text) or _unbalanced(text)[0]


def _mutate_json(text: str) -> str:
    # A Python triple-quoted string, `"content": """import ..."""` (QA 2026-10-13,
    # Qwen2.5-Coder-7B: a whole `write` dropped twice). `"""` never occurs in JSON outside a
    # string, so the text between such quotes becomes one JSON string, newlines escaped.
    if '"""' in text:
        text = re.sub(r'"""(.*?)"""', lambda m: json.dumps(m.group(1)), text, flags=re.DOTALL)
    # Braces doubled the way a format-string template escapes them, `{{"name": ...}}`
    # (QA 2026-10-13, Qwen2.5-Coder-7B: two `read` calls dropped and the turn ended on them as
    # text). JSON never opens with `{{`, so only then is each `{{`/`}}` outside strings read
    # as one brace.
    if text.lstrip().startswith("{{"):
        text = _undouble_braces(text)
    # Trailing commas before a closer.
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Unescaped newlines inside string literals.
    text = re.sub(
        r'"((?:[^"\\]|\\.)*)"',
        lambda m: '"' + m.group(1).replace("\n", "\\n").replace("\r", "") + '"',
        text,
        flags=re.DOTALL,
    )
    # A comma missing between two values on separate lines, as in `"a"\n "b"` inside an
    # array (QA 2026-10-13, Qwen2.5-Coder-7B: a whole `edit` call dropped for one missing
    # comma, the turn ending with the JSON shown as text). json_repair (mangiucugna) mends the
    # same slip. Only between lines, so a value and the next key on one line stay as written.
    text = _insert_missing_commas(text)
    # An unterminated string. Closers already at the end belong to the structure, so the
    # quote goes before them. Appending it at the very end would pull them into the value
    # and give valid JSON that says something the model never wrote.
    if _ends_inside_string(text):
        body = text.rstrip()
        trailing = 0
        while trailing < len(body) and body[len(body) - 1 - trailing] in "]}":
            trailing += 1
        cut = len(body) - trailing
        text = body[:cut] + '"' + body[cut:]

    opens, closes = _unbalanced(text)
    return text + "".join(reversed(closes)) if opens else text


def _undouble_braces(text: str) -> str:
    """`{{` and `}}` outside strings become `{` and `}`; a lone brace stays as it is."""
    out: list[str] = []
    in_string = escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_string:
            out.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character in "{}" and text[index + 1 : index + 2] == character:
            out.append(character)
            index += 1
        else:
            out.append(character)
            in_string = character == '"'
        index += 1
    return "".join(out)


_VALUE_END = frozenset('"}]0123456789el')  # a string, object, array, number, true/false/null
_VALUE_START = frozenset('"{[')


def _insert_missing_commas(text: str) -> str:
    """Put a comma between a value and the next one when only whitespace holding a newline
    separates them, outside strings. Anything else is left as it is."""
    out: list[str] = []
    in_string = escaped = False
    previous = ""  # the last character outside strings that is not whitespace
    end_at = -1  # where in `out` that character ended
    newline_since = False
    for character in text:
        if in_string:
            out.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
                previous, end_at, newline_since = '"', len(out), False
            continue
        if character in " \t\r\n":
            newline_since = newline_since or character == "\n"
            out.append(character)
            continue
        if newline_since and character in _VALUE_START and previous in _VALUE_END:
            out.insert(end_at, ",")
        out.append(character)
        if character == '"':
            in_string = True
        else:
            previous, end_at, newline_since = character, len(out), False
    return "".join(out)


def _ends_inside_string(text: str) -> bool:
    quote: str | None = None
    escaped = False
    for character in text:
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character == '"':
            quote = character
    return quote is not None


def _unbalanced(text: str) -> tuple[bool, list[str]]:
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for character in text:
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character == '"':
            quote = character
        elif character in "{[":
            stack.append("}" if character == "{" else "]")
        elif character in "}]" and stack:
            stack.pop()
    return bool(stack), stack


# --------------------------------------------------------------------------------------
# Normalisation: repair a candidate that was found
# --------------------------------------------------------------------------------------


def _distance(left: str, right: str, ceiling: int = 2) -> int:
    """Levenshtein distance, abandoned once it exceeds the ceiling."""
    if abs(len(left) - len(right)) > ceiling:
        return ceiling + 1
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b))
            )
        if min(current) > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


def resolve_name(name: str, tools: Mapping[str, ToolSpec]) -> tuple[str, str | None]:
    """Map what the model called it to what it is called here.

    Only an unambiguous match is accepted. Guessing between two plausible tools leads the
    agent to do the wrong thing.
    """
    if name in tools:
        return name, None
    lowered = name.lower()
    if matches := [known for known in tools if known.lower() == lowered]:
        return matches[0], "name_case"

    # An MCP tool named without its server (`fetch_results` for
    # `Server__fetch_results`): qwen3.8-flash-motoris did, in the app, 2026-10-31.
    suffix = "__" + lowered
    if len(matches := [k for k in tools if k.lower().endswith(suffix)]) == 1:
        return matches[0], "name_server_prefix"

    normalised = re.sub(r"[^a-z0-9]", "", lowered)
    if matches := [k for k in tools if re.sub(r"[^a-z0-9]", "", k.lower()) == normalised]:
        return matches[0], "name_punctuation"

    if matches := [k for k in tools if k.lower().startswith(lowered) or lowered.startswith(k.lower())]:
        if len(matches) == 1:
            return matches[0], "name_prefix"

    close = [k for k in tools if _distance(lowered, k.lower()) <= 2]
    if len(close) == 1:
        return close[0], "name_fuzzy"
    return name, None


def apply_aliases(arguments: Mapping[str, Any], spec: ToolSpec) -> tuple[dict[str, Any], bool]:
    """Rename parameters a model trained on another harness would have used."""
    lookup = {
        alias.lower(): canonical
        for canonical, aliases in spec.aliases.items()
        for alias in aliases
    }
    known = set(spec.parameters.get("properties", {}))
    out: dict[str, Any] = {}
    fired = False
    for key, value in arguments.items():
        if key in known:
            out[key] = value
            continue
        canonical = lookup.get(key.lower())
        if canonical and canonical not in out:
            out[canonical] = value
            fired = True
        else:
            out[key] = value
    return out, fired


def unwrap_double_encoded(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], bool, bool]:
    """`{"arguments": "{\\"path\\": \\"a\\"}"}`: an object that is really a JSON string.

    Very common from OpenAI-compatible local servers, which pass the model's raw argument
    string through unparsed. Returns the arguments, whether they were unwrapped, and
    whether `loose_json` had to change the string.
    """
    if len(arguments) == 1:
        (key, value), = arguments.items()
        if key in {"arguments", "args", "input", "parameters"} and isinstance(value, str):
            parsed, loose = loose_json(value)
            if isinstance(parsed, Mapping):
                return dict(parsed), True, loose
    return dict(arguments), False, False


def native_arguments(call: ToolUseBlock) -> ToolUseBlock:
    """Read a native call's arguments when its dialect could not.

    `openai_api` and `anthropic_api` leave `input` empty and keep the text in `raw` when it
    is not JSON. A near miss (a trailing comma, a raw newline or tab in a string) is read
    without changing any value and labelled `LOOSE_JSON`. `loose_json` is not used here:
    it also drops a comma before a closer inside a string, and a raw CR. Anything else is
    labelled `INVALID_JSON` and is never run. That includes text that stops inside a
    string or with a bracket open: closing it would run content the model never finished.
    """
    raw = call.raw.strip()
    if call.input or not raw:
        return call
    try:
        json.loads(raw)
        return call
    except json.JSONDecodeError:
        pass
    arguments = None
    if not unterminated(raw):
        with contextlib.suppress(json.JSONDecodeError):
            arguments = _DECODER.decode(_without_trailing_commas(raw))
    if isinstance(arguments, Mapping):
        return replace(call, input=dict(arguments), repaired_by=(*call.repaired_by, LOOSE_JSON))
    return replace(call, repaired_by=(*call.repaired_by, INVALID_JSON))


_CLOSER_AHEAD = re.compile(r"[ \t\r\n]*[}\]]")
#: What can come right before a comma that does not follow a value, as in `[,]`.
_NO_VALUE_BEFORE = ("", "[", "{", ",", ":")


def _without_trailing_commas(text: str) -> str:
    """``text`` without each comma that follows a value and comes right before a `}` or
    `]`. Commas inside strings are kept."""
    out: list[str] = []
    in_string = False
    escaped = False
    last = ""
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif (
            character == ","
            and last not in _NO_VALUE_BEFORE
            and _CLOSER_AHEAD.match(text, index + 1)
        ):
            continue
        out.append(character)
        if character not in _JSON_SPACE:
            last = character
    return "".join(out)


_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def _unquoted(value: str) -> str:
    """A scalar the model wrapped in quotes or backticks, as in `"src/a.py"`, without them.

    Only one line in one pair. A line break, or the wrapping character inside, means text
    (`"a" --flag "b"`, a docstring), and it is returned as written.
    """
    stripped = value.strip()
    if len(stripped) < 2 or stripped[0] != stripped[-1] or stripped[0] not in "`'\"":
        return value
    inner = stripped[1:-1]
    if stripped[0] in inner or "\n" in inner or "\r" in inner:
        return value
    return inner


def coerce(arguments: Mapping[str, Any], spec: ToolSpec) -> tuple[dict[str, Any], bool]:
    """Bend values into the types the schema declares, where the intent is unambiguous."""
    properties = spec.parameters.get("properties", {})
    out: dict[str, Any] = {}
    fired = False
    for key, value in arguments.items():
        declared = properties.get(key, {}).get("type")
        new = value
        if declared == "string" and isinstance(value, str):
            if key not in spec.verbatim:
                new = _unquoted(value)
        elif declared == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            new = int(value.strip())
        elif declared == "number" and isinstance(value, str):
            try:
                new = float(value.strip())
            except ValueError:
                new = value
        elif declared == "boolean" and isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _TRUE:
                new = True
            elif lowered in _FALSE:
                new = False
        elif declared == "array" and not isinstance(value, list | tuple):
            new = [value]
        if new != value:
            fired = True
        out[key] = new
    return out, fired


def normalise(call: ToolUseBlock, tools: Mapping[str, ToolSpec]) -> tuple[ToolUseBlock, tuple[str, ...]]:
    """Apply every repair that does not require guessing. Returns the call and what fired."""
    fired: list[str] = []

    name, name_fix = resolve_name(call.name, tools)
    if name_fix:
        fired.append(name_fix)

    arguments, unwrapped, loose = unwrap_double_encoded(call.input)
    if unwrapped:
        fired.append("double_encoded_args")
    if loose and LOOSE_JSON not in call.repaired_by:
        fired.append(LOOSE_JSON)

    spec = tools.get(name)
    if spec is not None:
        arguments, aliased = apply_aliases(arguments, spec)
        if aliased:
            fired.append("arg_alias")
        arguments, coerced = coerce(arguments, spec)
        if coerced:
            fired.append("coerce")

    if not fired and name == call.name and arguments == dict(call.input):
        return call, ()
    return (
        ToolUseBlock(
            id=call.id,
            name=name,
            input=arguments,
            raw=call.raw,
            repaired_by=(*call.repaired_by, *fired),
        ),
        tuple(fired),
    )


def validate(call: ToolUseBlock, spec: ToolSpec) -> str:
    """Return an empty string when valid, or a message that says what to change."""
    properties: Mapping[str, Any] = spec.parameters.get("properties", {})
    required: Sequence[str] = spec.parameters.get("required", ())

    if missing := [name for name in required if name not in call.input]:
        expected = ", ".join(
            f"{n} ({properties.get(n, {}).get('type', 'any')})" for n in properties
        )
        return f"missing required argument(s): {', '.join(missing)}. {spec.name} takes: {expected}"

    for key, value in call.input.items():
        declared = properties.get(key, {}).get("type")
        if declared is None:
            continue
        expected_python = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": (list, tuple),
            "object": dict,
        }.get(declared)
        if expected_python and not isinstance(value, expected_python):
            return (
                f"argument {key!r} should be {declared}, got "
                f"{type(value).__name__}: {value!r:.60}"
            )
    return ""
