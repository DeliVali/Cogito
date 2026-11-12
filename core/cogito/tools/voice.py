"""`voice_listen` and `voice_speak`: the user talks, the agent talks back.

Both are thin adapters over cogito-vox's `VoiceIO`, running in this process
(no MCP hop, no second server). cogito-vox owns the audio: voice activity
detection, endpointing, echo cancellation, barge-in, and the STT/TTS engines
in its `engines.toml`. These classes only map arguments in and results and
errors out, so they behave the same as the MCP server the package serves to
other harnesses.

Off by default (`[voice] enabled = true` in config.toml turns them on) and
never `core`: a definition sits in the window on every step and no suite
task speaks, so carrying them by default could only cost tokens
(CONTRIBUTING section 7).

Barge-in stops the agent's audio, not its turn. When the user talks over
`voice_speak`, playback stops, the result says what they said, and the loop
handles it like any other tool result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from cogito_vox import VoxDeviceBusyError, VoxEngineError, VoxError, VoxInputError

from cogito.provider.base import PayloadShape, Provenance, Reproducibility, Retention, ToolSpec
from cogito.tools.base import ToolContext, ToolResult, parameters

if TYPE_CHECKING:
    from cogito_vox import VoiceIO


def _error(exc: VoxError) -> ToolResult:
    if isinstance(exc, VoxDeviceBusyError):
        kind = "busy"
    elif isinstance(exc, VoxEngineError):
        kind = "engine_unavailable"
    elif isinstance(exc, VoxInputError):
        kind = "bad_arguments"
    else:
        kind = "voice_error"
    return ToolResult.error(str(exc), kind)


class VoiceListenTool:
    def __init__(self, voice: VoiceIO) -> None:
        self._voice = voice
        self.spec = ToolSpec(
            name="voice_listen",
            #: The person hears or is heard: a read-only run leaves it out.
            operates=True,
            #: What was heard happened once; listening again records something new.
            provenance=Provenance(
                reproducibility=Reproducibility.EPHEMERAL,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=(
                "Listen to the user through the microphone and return what they said, "
                "once they stop talking. Use it when the conversation is by voice and you "
                "need the user's reply."
            ),
            parameters=parameters(
                {
                    "max_seconds": {
                        "type": "number",
                        "description": "Give up after this many seconds. Default 30.",
                    },
                }
            ),
            aliases={"max_seconds": ("timeout", "seconds", "duration")},
            budget_tokens=400,
            summary="Hear the user's spoken reply as text.",
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        try:
            max_seconds = float(arguments.get("max_seconds") or 30.0)
        except (TypeError, ValueError):
            return ToolResult.error("max_seconds must be a number of seconds.", "bad_arguments")
        try:
            heard = self._voice.listen(max_seconds=max_seconds)
        except VoxError as exc:
            return _error(exc)
        if not heard.transcript:
            content = f"Heard nothing in {max_seconds:g} s."
        else:
            content = f"The user said: {heard.transcript}"
        return ToolResult(
            content=content,
            metadata={
                "endpoint_reason": heard.endpoint_reason,
                "duration_ms": heard.duration_ms,
                "engine": heard.engine_used,
            },
        )


class VoiceSpeakTool:
    def __init__(self, voice: VoiceIO) -> None:
        self._voice = voice
        self.spec = ToolSpec(
            name="voice_speak",
            #: The person hears or is heard: a read-only run leaves it out.
            operates=True,
            #: The result can hold what the user said while interrupting: evidence that
            #: cannot be obtained again.
            provenance=Provenance(
                reproducibility=Reproducibility.EPHEMERAL,
                retention=Retention.EVIDENCE,
                shape=PayloadShape.COMPACT,
            ),
            description=(
                "Say text out loud to the user. Keep it short and conversational. If the "
                "user talks over you, you stop, and the result tells you what they said."
            ),
            parameters=parameters(
                {"text": {"type": "string", "description": "What to say."}},
                required=("text",),
            ),
            aliases={"text": ("message", "content", "say", "utterance")},
            budget_tokens=300,
            summary="Say something out loud to the user.",
        )

    def run(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        text = str(arguments.get("text", "")).strip()
        if not text:
            return ToolResult.error("voice_speak needs `text`: what to say.", "bad_arguments")
        try:
            said = self._voice.speak(text)
        except VoxError as exc:
            return _error(exc)
        if not said.interrupted:
            content = "Said it."
        else:
            heard = said.user_utterance or "(nothing intelligible)"
            spoken = said.spoken_text or "(nothing)"
            content = (
                f"The user interrupted you. They heard: {spoken!r}. They said: {heard}"
            )
        return ToolResult(
            content=content,
            metadata={
                "interrupted": said.interrupted,
                "ttfb_ms": said.ttfb_ms,
                "duration_ms": said.duration_ms,
                "engine": said.engine_used,
            },
        )
