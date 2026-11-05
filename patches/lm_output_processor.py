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

from speech_to_speech import filler_clips, voice_affect, voice_tools
from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.events import AssistantTextEvent, ResponseFailedEvent, TokenUsageEvent
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.handler_types import LLMOut, TTSIn
from speech_to_speech.pipeline.messages import EndOfResponse, GenerateResponseRequest, LLMResponseChunk, TokenUsage, TTSInput
from speech_to_speech.pipeline.queue_types import TextEventItem, TextPromptItem
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker
from speech_to_speech.turn_stats import turn_stats
from speech_to_speech.utils.utils import _generate_id, response_wants_audio

logger = logging.getLogger(__name__)


class AffectTTSInput(TTSInput):
    """``TTSInput`` carrying the brain's per-turn delivery note (the
    ``[affect: ...]`` marker extracted from the reply text), or ``None``
    when affect is disabled or no marker has been seen this turn yet.

    A subclass rather than a new field on ``TTSInput`` itself: that class
    lives in the base ``speech_to_speech`` package, which patchbay does not
    vendor, so extending it would put a whole upstream file under our
    maintenance forever. The TTS handler reads this via
    ``getattr(tts_input, "affect", None)`` (feature-detection, never an
    ``isinstance`` check) so a plain ``TTSInput`` from another call site
    (voice audition, hermes cockpit) keeps working unaffected.
    """

    affect: str | None = None


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


def _parse_turn_fillers() -> tuple[str, ...]:
    """Parse VOICE_TURN_FILLERS: the pool the turn-end filler (VOICE_TURN_FILLER=on)
    draws from, same format as VOICE_TOOL_FILLERS. Unset: the tool-filler pool."""
    if os.environ.get("VOICE_TURN_FILLERS") is None:
        return _TOOL_FILLERS
    raw = os.environ["VOICE_TURN_FILLERS"]
    if raw.strip().lower() == "off":
        return ()
    phrases = tuple(phrase.strip() for phrase in raw.split("|") if phrase.strip())
    return phrases if phrases else _TOOL_FILLERS


_TURN_FILLERS: tuple[str, ...] = _parse_turn_fillers()

# Spoken once when the generation that follows a tool round ends with nothing
# speakable and no further tool calls (otherwise the user hears dead air).
_EMPTY_FOLLOWUP_LINE = "Sorry, I didn't get an answer for that."

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

_SYNTHETIC_UNPARSEABLE_ARGS_OUTPUT = (
    "This tool call was not executed because its arguments were incomplete "
    "or malformed. Retry with a shorter request, or answer without the tool."
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


def _pick_from(pool: tuple[str, ...]) -> str | None:
    global _last_tool_filler
    if not pool:
        return None
    phrase = random.choice(pool)
    if len(pool) > 1:
        while phrase == _last_tool_filler:
            phrase = random.choice(pool)
    _last_tool_filler = phrase
    return phrase


def _pick_filler() -> str | None:
    """Pick a tool-call filler phrase, or None if fillers are disabled.

    Avoids repeating the last-returned phrase back-to-back when the pool
    has more than one entry.
    """
    return _pick_from(_TOOL_FILLERS)


def _pick_turn_filler() -> str | None:
    """Same picker (and the same no-back-to-back rule) over the turn-filler pool."""
    return _pick_from(_TURN_FILLERS)


filler_clips.set_turn_picker(_pick_turn_filler)


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
        cancel_scope: CancelScope | None = None,
    ) -> None:
        """
        Initialize the processor.

        Args:
            text_output_queue: Queue to send text messages and tool calls
            text_prompt_queue: Queue feeding the LLM handler; used to push a
                follow-up generation request after a tool call resolves
            cancel_scope: websocket-mode barge-in cancel (VOICE_BARGE_CANCEL).
                When set, chunks and end-of-response markers of a cancelled
                generation are dropped here instead of reaching the TTS.
        """
        self.cancel_scope = cancel_scope
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
        # Whether the model has already spoken (text forwarded to TTS) in the
        # current tool round of this turn. In the upstream base class, a
        # ToolCall event first flushes the buffered sentences as a text-only
        # chunk (_consume_streaming: ToolCall branch -> _flush -> _chunk(text=)),
        # then _record_tool_call yields a separate chunk with tools=[item] and
        # no text (non-streaming likewise emits text chunks before the tool
        # chunk). Pre-tool text therefore arrives in EARLIER chunks, so this
        # must span chunks. Keyed by turn_id like the round
        # counter above (one slot: a new turn_id replaces it, nothing grows).
        self._spoke_turn_id: str | None = None
        self._spoke_this_round = False
        # Per-turn affect carry-over: a chunk with no [affect: ...] marker of
        # its own inherits the turn's last-seen affect (a tool-call follow-up
        # generation reuses the same turn_id and may or may not repeat one).
        self._affect_tracker = voice_affect.TurnAffectTracker()
        # Empty-follow-up guard. Per-generation: did it carry tool calls /
        # speakable text. _awaiting_followup is set at the end of a generation
        # that ran tools and queued a follow-up (the tool chunk itself, for its
        # runtime_config / response / language, plus the tool names); the next
        # EndOfResponse for that turn is the follow-up's.
        self._gen_had_tools = False
        self._gen_spoke = False
        self._pending_followup: tuple[LLMResponseChunk, list[str]] | None = None
        self._awaiting_followup: tuple[LLMResponseChunk, list[str]] | None = None

    def _reset_followup_guard(self) -> None:
        """Forget all empty-follow-up state (a discarded stale/cancelled
        generation must not leak its flags into the next one)."""
        self._gen_had_tools = False
        self._gen_spoke = False
        self._pending_followup = None
        self._awaiting_followup = None

    def _turn_output_allowed(self, turn_id: str | None, turn_revision: int | None) -> bool:
        if self.speculative_turns is None:
            return True
        return self.speculative_turns.is_latest_after_reopen_grace(turn_id, turn_revision)

    def _generation_is_stale(self, gen: int | None) -> bool:
        return gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen)

    def _next_tool_round(self, turn_id: str | None) -> int:
        """Return the 1-based round number for a tool-carrying chunk within
        the current turn, resetting the counter when ``turn_id`` differs
        from the previously tracked one."""
        if turn_id != self._tool_turn_id:
            self._tool_turn_id = turn_id
            self._tool_rounds_this_turn = 0
        self._tool_rounds_this_turn += 1
        return self._tool_rounds_this_turn

    def _model_spoke_this_round(self, turn_id: str | None) -> bool:
        return self._spoke_turn_id == turn_id and self._spoke_this_round

    def _set_model_spoke(self, turn_id: str | None, spoke: bool) -> None:
        if turn_id != self._spoke_turn_id:
            self._spoke_turn_id = turn_id
        self._spoke_this_round = spoke

    def _run_tool_calls(self, lm_output: LLMResponseChunk, round_num: int) -> bool:
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

        Returns True iff a follow-up generation was queued.
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
                return False
            resolved_any = True
        else:
            resolved_any = False
            for tool_call in lm_output.tools:
                if tool_call.arguments:
                    try:
                        args = json.loads(tool_call.arguments)
                    except (TypeError, ValueError):
                        logger.warning(
                            "LMOutputProcessor: unparseable arguments for tool %s, refusing execution: %.200s",
                            tool_call.name,
                            tool_call.arguments,
                        )
                        try:
                            chat.append_tool_output(
                                tool_call.call_id,
                                RealtimeConversationItemFunctionCallOutput(
                                    type="function_call_output",
                                    id=_generate_id("fco"),
                                    call_id=tool_call.call_id,
                                    output=_SYNTHETIC_UNPARSEABLE_ARGS_OUTPUT,
                                ),
                            )
                            resolved_any = True
                        except Exception:
                            logger.exception(
                                "LMOutputProcessor: failed to record synthetic unparseable-args output for %s",
                                tool_call.name,
                            )
                        continue
                else:
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
                return True
            except Exception:
                logger.exception("LMOutputProcessor: failed to enqueue follow-up generation")
        return False

    def _check_empty_followup(self, eor: EndOfResponse) -> TTSInput | None:
        """Called at the end of every non-stale generation. Returns the fallback
        TTSInput iff this generation was the follow-up to a tool round and
        produced no speakable text and no further tool calls."""
        had_tools, spoke = self._gen_had_tools, self._gen_spoke
        pending = self._pending_followup
        self._gen_had_tools = False
        self._gen_spoke = False
        self._pending_followup = None
        if had_tools:
            # This generation ran tools: its follow-up (if one was queued) is next.
            self._awaiting_followup = pending
            return None
        awaiting, self._awaiting_followup = self._awaiting_followup, None
        if awaiting is None or spoke or eor.error:
            return None
        tool_chunk, tool_names = awaiting
        if (tool_chunk.turn_id, tool_chunk.turn_revision) != (eor.turn_id, eor.turn_revision):
            return None
        if not response_wants_audio(tool_chunk.response):
            return None
        logger.warning(
            "LMOutputProcessor: empty follow-up after tool round, turn=%s tools=%s -- speaking fallback",
            eor.turn_id,
            tool_names,
        )
        if self.text_output_queue is not None:
            # Shown in the cockpit transcript like any assistant line. Not added
            # to the chat history: it is not model output.
            self.text_output_queue.put(
                AssistantTextEvent(
                    text=_EMPTY_FOLLOWUP_LINE,
                    turn_id=eor.turn_id,
                    turn_revision=eor.turn_revision,
                    cancel_generation=eor.cancel_generation,
                )
            )
        turn_stats.on_tts_input()
        return TTSInput(
            text=_EMPTY_FOLLOWUP_LINE,
            language_code=tool_chunk.language_code,
            runtime_config=tool_chunk.runtime_config,
            response=tool_chunk.response,
            turn_id=eor.turn_id,
            turn_revision=eor.turn_revision,
            speech_stopped_at_s=None,
            cancel_generation=eor.cancel_generation,
        )

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
            if self._generation_is_stale(lm_output.cancel_generation):
                # A cancelled reply must not emit AUDIO_RESPONSE_DONE: it would
                # re-open the mic in the middle of the turn that replaced it.
                logger.debug("Dropping end-of-response of a cancelled generation")
                self._reset_followup_guard()
                return
            if not self._turn_output_allowed(
                lm_output.turn_id,
                lm_output.turn_revision,
            ):
                logger.debug(
                    "Dropping stale end-of-response for turn=%s rev=%s",
                    lm_output.turn_id,
                    lm_output.turn_revision,
                )
                self._reset_followup_guard()
                return
            turn_stats.end_of_response()
            fallback = self._check_empty_followup(lm_output)
            if fallback is not None:
                yield fallback
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

        if self._generation_is_stale(lm_output.cancel_generation):
            logger.debug("Dropping chunk of a cancelled generation (turn=%s)", lm_output.turn_id)
            return

        if not self._turn_output_allowed(
            lm_output.turn_id,
            lm_output.turn_revision,
        ):
            logger.debug("Dropping stale LLM chunk for turn=%s rev=%s", lm_output.turn_id, lm_output.turn_revision)
            return

        turn_stats.on_llm_chunk(lm_output.speech_stopped_at_s)
        if lm_output.tools:
            self._gen_had_tools = True

        logger.debug(f"LM processor: text='{lm_output.text}', tools={lm_output.tools}")

        # Affect extraction/stripping happens here, before the text reaches
        # AssistantTextEvent (the webclient transcript + server-side transcript
        # buffer) or TTS -- a marker that survives either would be shown to the
        # user or, worse, spoken aloud. Disabled is a no-op: text_for_speech
        # stays exactly lm_output.text and affect_for_turn stays None.
        text_for_speech = lm_output.text
        affect_for_turn: str | None = None
        if voice_affect.ENABLED:
            # feed() (not the pure extract_affect()) because the LM layer
            # sentence-splits before this method ever sees the text, and a
            # marker containing a full stop can straddle two chunks --
            # feed() holds back a dangling opener rather than ever letting
            # half a marker reach speech. See TurnAffectTracker's docstring.
            text_for_speech, affect_for_turn = self._affect_tracker.feed(lm_output.turn_id, lm_output.text)
        if text_for_speech and text_for_speech.strip():
            self._gen_spoke = True

        if self.text_output_queue is not None:
            event = AssistantTextEvent(
                text=text_for_speech,
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                cancel_generation=lm_output.cancel_generation,
            )
            if lm_output.tools:
                event.tools = lm_output.tools
                logger.info(f"Sending to clients: text='{text_for_speech}', tools={[t.name for t in lm_output.tools]}")
            else:
                logger.debug(f"Sending to clients: text='{text_for_speech}' (no tools)")
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
            # The filler only covers silence while tools run: if the model has
            # already spoken this round (earlier chunk, or this chunk's own
            # text), another acknowledgement would double it.
            model_spoke = bool(text_for_speech and text_for_speech.strip()) or self._model_spoke_this_round(
                lm_output.turn_id
            )
            # This tool round is consumed: the next one starts silent.
            self._set_model_spoke(lm_output.turn_id, False)
            if (
                plays_filler_round
                and not all_visual
                and not model_spoke
                and response_wants_audio(lm_output.response)
                and not filler_clips.turn_filler_just_played()  # a turn-end filler just covered this silence
            ):
                filler = _pick_filler()
                if filler is not None and filler_clips.play_cached(filler):
                    pass  # pre-rendered clip in the persona voice went straight to the audio queue
                elif filler is not None:
                    filler_clips.note_filler()
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
            if self._run_tool_calls(lm_output, round_num):
                self._pending_followup = (lm_output, [t.name for t in lm_output.tools])

        if text_for_speech and response_wants_audio(lm_output.response):
            logger.debug(f"Forwarding to TTS: '{text_for_speech}'")
            turn_stats.on_tts_input()
            if text_for_speech.strip() and not lm_output.tools:
                self._set_model_spoke(lm_output.turn_id, True)
            yield AffectTTSInput(
                text=text_for_speech,
                language_code=lm_output.language_code,
                runtime_config=lm_output.runtime_config,
                response=lm_output.response,
                turn_id=lm_output.turn_id,
                turn_revision=lm_output.turn_revision,
                speech_stopped_at_s=lm_output.speech_stopped_at_s,
                cancel_generation=lm_output.cancel_generation,
                affect=affect_for_turn,
            )
