"""
LLM Output Processor

Intercepts LLM output to:
1. Extract tool calls and send them via text_output_queue
2. Forward clean text to TTS pipeline
"""

from __future__ import annotations

import json
import logging
import os
import random
from collections.abc import Iterator
from queue import Queue

from openai.types.realtime.conversation_item import RealtimeConversationItemFunctionCallOutput

from speech_to_speech import voice_tools
from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.events import AssistantTextEvent, ResponseFailedEvent, TokenUsageEvent
from speech_to_speech.pipeline.handler_types import LLMOut, TTSIn
from speech_to_speech.pipeline.messages import EndOfResponse, GenerateResponseRequest, LLMResponseChunk, TokenUsage, TTSInput
from speech_to_speech.pipeline.queue_types import TextEventItem, TextPromptItem
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker
from speech_to_speech.turn_stats import turn_stats
from speech_to_speech.utils.utils import _generate_id, response_wants_audio

logger = logging.getLogger(__name__)

# Tool-call filler phrases: a "let me check" alone gets stale on repeat tool
# calls, so we rotate over a pool instead of a single hardcoded string.
_DEFAULT_TOOL_FILLERS: tuple[str, ...] = (
    "Let me check.",
    "One sec.",
    "Checking.",
    "On it.",
    "Let me look.",
    "Give me a moment.",
    "Looking that up.",
)


def _parse_tool_fillers() -> tuple[str, ...]:
    """Parse VOICE_TOOL_FILLERS (pipe-separated, since phrases may contain
    commas). Unset or blank-after-parsing falls back to the default pool.
    The special value "off" (case-insensitive, exact) disables the filler
    entirely by returning an empty pool.
    """
    raw = os.environ.get("VOICE_TOOL_FILLERS")
    if raw is None:
        return _DEFAULT_TOOL_FILLERS
    if raw.strip().lower() == "off":
        return ()
    phrases = tuple(phrase.strip() for phrase in raw.split("|") if phrase.strip())
    return phrases if phrases else _DEFAULT_TOOL_FILLERS


_TOOL_FILLERS: tuple[str, ...] = _parse_tool_fillers()
_last_tool_filler: str | None = None

# Runaway tool-call chain guard: a chain that keeps calling tools round
# after round burns latency and, unmitigated, keeps the filler firing every
# single round. The cap bounds the chain length; the cadence knob throttles
# how often the filler plays within it.
_DEFAULT_TOOL_CALL_CAP = 8
_DEFAULT_TOOL_FILLER_EVERY = 3

_SYNTHETIC_CAP_OUTPUT = (
    "Tool budget for this turn is exhausted. Do not call any more tools. "
    "Answer the user now with what you already have, and say plainly what "
    "you could not finish."
)


def _parse_tool_call_cap() -> int | None:
    """Parse VOICE_TOOL_CALL_CAP: the max number of tool-call rounds
    executed per turn. Unset or blank falls back to the default cap (8).
    The special values "0" and "off" (case-insensitive) disable capping
    entirely (returns None), as does any non-positive integer. Malformed
    values fall back to the default. Never raises.
    """
    raw = os.environ.get("VOICE_TOOL_CALL_CAP")
    if raw is None:
        return _DEFAULT_TOOL_CALL_CAP
    stripped = raw.strip()
    if not stripped:
        return _DEFAULT_TOOL_CALL_CAP
    if stripped.lower() == "off":
        return None
    try:
        value = int(stripped)
    except ValueError:
        return _DEFAULT_TOOL_CALL_CAP
    return value if value > 0 else None


def _parse_tool_filler_every() -> int:
    """Parse VOICE_TOOL_FILLER_EVERY: play the tool-call filler every Nth
    tool-call round (1-based, so a value of 3 plays on rounds 1, 4, 7, ...).
    Unset, blank, malformed, or less than 1 falls back to the default (3).
    A value of 1 plays the filler on every round (today's behaviour).
    Never raises.
    """
    raw = os.environ.get("VOICE_TOOL_FILLER_EVERY")
    if raw is None:
        return _DEFAULT_TOOL_FILLER_EVERY
    stripped = raw.strip()
    if not stripped:
        return _DEFAULT_TOOL_FILLER_EVERY
    try:
        value = int(stripped)
    except ValueError:
        return _DEFAULT_TOOL_FILLER_EVERY
    return value if value >= 1 else _DEFAULT_TOOL_FILLER_EVERY


def _pick_filler() -> str | None:
    """Pick a tool-call filler phrase, or None if fillers are disabled.

    Avoids repeating the last-returned phrase back-to-back when the pool
    has more than one entry.
    """
    global _last_tool_filler
    if not _TOOL_FILLERS:
        return None
    phrase = random.choice(_TOOL_FILLERS)
    if len(_TOOL_FILLERS) > 1:
        while phrase == _last_tool_filler:
            phrase = random.choice(_TOOL_FILLERS)
    _last_tool_filler = phrase
    return phrase


class LMOutputProcessor(BaseHandler[LLMOut, TTSIn]):
    """
    Processes LLM output to extract tool calls and forward clean text to TTS.

    Input: :class:`LLMResponseChunk`, :class:`TokenUsage`, or :class:`EndOfResponse` from LLM
    Output: :class:`TTSInput` or :class:`EndOfResponse` to TTS
    Side effect: Sends :class:`AssistantTextEvent` / :class:`TokenUsageEvent` to text_output_queue
    """

    def setup(
        self,
        text_output_queue: Queue[TextEventItem] | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        text_prompt_queue: Queue[TextPromptItem] | None = None,
    ) -> None:
        """
        Initialize the processor.

        Args:
            text_output_queue: Queue to send text messages and tool calls
            text_prompt_queue: Queue feeding the LLM handler; used to push a
                follow-up generation request after a tool call resolves
        """
        self.text_output_queue = text_output_queue
        self.speculative_turns = speculative_turns
        self.text_prompt_queue = text_prompt_queue
        self._tool_call_cap = _parse_tool_call_cap()
        self._tool_filler_every = _parse_tool_filler_every()
        # Per-turn round counter (a "round" = one LLMResponseChunk carrying
        # tools). turn_id=None chunks share a single counter key -- that
        # doesn't happen for real tool turns today, this just avoids
        # crashing if it ever does. Comparing against this initial None
        # naturally does the right thing on the very first chunk either way.
        self._tool_turn_id: str | None = None
        self._tool_rounds_this_turn = 0

    def _turn_output_allowed(self, turn_id: str | None, turn_revision: int | None) -> bool:
        if self.speculative_turns is None:
            return True
        return self.speculative_turns.is_latest_after_reopen_grace(turn_id, turn_revision)

    def _next_tool_round(self, turn_id: str | None) -> int:
        """Return the 1-based round number for a tool-carrying chunk within
        the current turn, resetting the counter when ``turn_id`` differs
        from the previously tracked one."""
        if turn_id != self._tool_turn_id:
            self._tool_turn_id = turn_id
            self._tool_rounds_this_turn = 0
        self._tool_rounds_this_turn += 1
        return self._tool_rounds_this_turn

    def _run_tool_calls(self, lm_output: LLMResponseChunk, round_num: int) -> None:
        """Execute each tool call, record its output in chat history, and queue
        a follow-up generation so the model can speak the result.

        Runs on the same thread as ``process()`` (blocking on network calls is
        fine here — nothing downstream is waiting on this handler for audio).
        Never raises: an executor or queue failure must not kill the handler
        thread, it just means the tool call goes unanswered this turn.

        Once ``round_num`` exceeds the configured cap, no further tool calls
        are executed -- OpenAI-style chat history still requires an output
        per call_id though, so every call in the chunk gets a synthetic
        refusal instead. The model is given two more rounds (cap+1, cap+2)
        to read that refusal and wrap up via a follow-up generation; from
        cap+3 on, the chain is cut off outright (no follow-up pushed) and
        the model's last spoken text stands.
        """
        chat = lm_output.runtime_config.chat
        cap = self._tool_call_cap
        over_cap = cap is not None and round_num > cap

        if over_cap:
            logger.warning(
                "LMOutputProcessor: tool call cap (%s) exceeded for turn=%s round=%d -- refusing execution",
                cap,
                lm_output.turn_id,
                round_num,
            )
            for tool_call in lm_output.tools:
                try:
                    chat.append_tool_output(
                        tool_call.call_id,
                        RealtimeConversationItemFunctionCallOutput(
                            type="function_call_output",
                            id=_generate_id("fco"),
                            call_id=tool_call.call_id,
                            output=_SYNTHETIC_CAP_OUTPUT,
                        ),
                    )
                except Exception:
                    logger.exception(
                        "LMOutputProcessor: failed to record synthetic cap output for %s", tool_call.name
                    )
            if round_num > cap + 2:
                return
            resolved_any = True
        else:
            resolved_any = False
            for tool_call in lm_output.tools:
                try:
                    args = json.loads(tool_call.arguments) if tool_call.arguments else {}
                except (TypeError, ValueError):
                    args = {}
                try:
                    result = voice_tools.execute(tool_call.name, args)
                    chat.append_tool_output(
                        tool_call.call_id,
                        RealtimeConversationItemFunctionCallOutput(
                            type="function_call_output",
                            id=_generate_id("fco"),
                            call_id=tool_call.call_id,
                            output=result,
                        ),
                    )
                    resolved_any = True
                except Exception:
                    logger.exception("LMOutputProcessor: tool call failed for %s", tool_call.name)

        if resolved_any and self.text_prompt_queue is not None:
            try:
                self.text_prompt_queue.put(
                    GenerateResponseRequest(
                        runtime_config=lm_output.runtime_config,
                        turn_id=lm_output.turn_id,
                        turn_revision=lm_output.turn_revision,
                        speech_stopped_at_s=None,
                    )
                )
                turn_stats.note_followup_pending()
            except Exception:
                logger.exception("LMOutputProcessor: failed to enqueue follow-up generation")

    def process(self, lm_output: LLMOut) -> Iterator[TTSIn]:
        """
        Process LLM output: send text/tools to WebSocket, forward clean text to TTS.

        Yields:
            :class:`TTSInput` or :class:`EndOfResponse` for TTS
        """
        if isinstance(lm_output, TokenUsage):
            if not self._turn_output_allowed(
                lm_output.turn_id,
                lm_output.turn_revision,
            ):
                logger.debug(
                    "Dropping stale token usage for turn=%s rev=%s", lm_output.turn_id, lm_output.turn_revision
                )
                return
            if self.text_output_queue is not None:
                self.text_output_queue.put(
                    TokenUsageEvent(
                        input_tokens=lm_output.input_tokens or 0,
                        output_tokens=lm_output.output_tokens or 0,
                        turn_id=lm_output.turn_id,
                        turn_revision=lm_output.turn_revision,
                    )
                )
            return

        if isinstance(lm_output, EndOfResponse):
            if not self._turn_output_allowed(
                lm_output.turn_id,
                lm_output.turn_revision,
            ):
                logger.debug(
                    "Dropping stale end-of-response for turn=%s rev=%s",
                    lm_output.turn_id,
                    lm_output.turn_revision,
                )
                return
            turn_stats.end_of_response()
            # A failed generation (e.g. invalid out-of-band input) closes the response as
            # "failed" via the text side-channel, then falls through to emit the normal
            # EndOfResponse so the audio path still re-enables listening / releases the slot.
            if lm_output.error and self.text_output_queue is not None:
                self.text_output_queue.put(
                    ResponseFailedEvent(
                        message=lm_output.error,
                        turn_id=lm_output.turn_id,
                        turn_revision=lm_output.turn_revision,
                    )
                )
            yield EndOfResponse(
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                cancel_generation=lm_output.cancel_generation,
            )
            return

        if not isinstance(lm_output, LLMResponseChunk):
            logger.warning("LMOutputProcessor received unexpected type: %s", type(lm_output))
            return

        if not self._turn_output_allowed(
            lm_output.turn_id,
            lm_output.turn_revision,
        ):
            logger.debug("Dropping stale LLM chunk for turn=%s rev=%s", lm_output.turn_id, lm_output.turn_revision)
            return

        turn_stats.on_llm_chunk(lm_output.speech_stopped_at_s)

        logger.debug(f"LM processor: text='{lm_output.text}', tools={lm_output.tools}")

        if self.text_output_queue is not None:
            event = AssistantTextEvent(
                text=lm_output.text,
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                cancel_generation=lm_output.cancel_generation,
            )
            if lm_output.tools:
                event.tools = lm_output.tools
                logger.info(f"Sending to clients: text='{lm_output.text}', tools={[t.name for t in lm_output.tools]}")
            else:
                logger.debug(f"Sending to clients: text='{lm_output.text}' (no tools)")
            self.text_output_queue.put(event)

        if lm_output.tools and lm_output.runtime_config is not None:
            round_num = self._next_tool_round(lm_output.turn_id)
            # A mood change alone doesn't warrant a "let me check" — that filler
            # is for tools with real work/latency to cover. Mixed visual+data
            # tool calls in the same chunk still get it. On top of that, only
            # every Nth round (VOICE_TOOL_FILLER_EVERY) is even considered --
            # a long tool chain otherwise plays a filler every single round.
            all_visual = all(t.name in voice_tools.VISUAL_TOOLS for t in lm_output.tools)
            plays_filler_round = (round_num - 1) % self._tool_filler_every == 0
            if plays_filler_round and not all_visual and response_wants_audio(lm_output.response):
                filler = _pick_filler()
                if filler is not None:
                    turn_stats.on_tts_input()
                    yield TTSInput(
                        text=filler,
                        language_code=lm_output.language_code,
                        runtime_config=lm_output.runtime_config,
                        response=lm_output.response,
                        turn_id=lm_output.turn_id,
                        turn_revision=lm_output.turn_revision,
                        speech_stopped_at_s=lm_output.speech_stopped_at_s,
                        cancel_generation=lm_output.cancel_generation,
                    )
            self._run_tool_calls(lm_output, round_num)

        if lm_output.text and response_wants_audio(lm_output.response):
            logger.debug(f"Forwarding to TTS: '{lm_output.text}'")
            turn_stats.on_tts_input()
            yield TTSInput(
                text=lm_output.text,
                language_code=lm_output.language_code,
                runtime_config=lm_output.runtime_config,
                response=lm_output.response,
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                speech_stopped_at_s=lm_output.speech_stopped_at_s,
                cancel_generation=lm_output.cancel_generation,
            )
