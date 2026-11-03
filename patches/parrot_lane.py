"""Parrot mode: an ear-test instrument, not a product feature.

When armed, :class:`ParrotGate` sits between transcription and the LM
handler and answers *every* fresh user turn with its own transcript,
verbatim, via a synthetic LM reply (:func:`emit_synthetic_reply`). The LM is
never consulted.
Everything downstream of the LM (``LMOutputProcessor``, TTS, the client audio
path) runs exactly as for a normal short LLM turn, so a broken link anywhere
in that chain becomes audible in seconds -- a normal LLM turn only proves
that synthesis works, not that the client ever hears or renders it.

Always inserted into the handler chain (see
``s2s_pipeline._build_pipeline_handlers``) so it can be armed and disarmed
live from the cockpit panel without a service restart -- restarting would
destroy the very conversation the instrument exists to diagnose.
``VOICE_PARROT=1`` only sets the STARTUP state (start armed instead of
disarmed); toggling afterwards is a ``config_set {"parrot": true|false}``
that flips :attr:`ParrotGate.armed` directly (see ``brain_control.py``).
When disarmed, ``process()`` yields every request through untouched, same as
for a non-turn message.

Because this is a diagnostic instrument, its cardinal sin is answering from
anywhere other than "the user's own transcript, through the normal TTS
path". It must never fall back to the LM's chat history, never rewrite the
text, and never swallow a turn it cannot echo.
"""

from __future__ import annotations

import logging
from queue import Queue
from typing import Any, Iterator, Optional

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.handler_types import LLMIn, LLMOut
from speech_to_speech.pipeline.messages import EndOfResponse, GenerateResponseRequest, LLMResponseChunk
from speech_to_speech.pipeline.queue_types import LMOutItem
from speech_to_speech.turn_stats import turn_stats

logger = logging.getLogger(__name__)


def _last_user_text(runtime_config: Any) -> Optional[str]:
    """Best-effort read of the latest user message text from the chat buffer.

    The transcript the LLM would see lives only in ``runtime_config.chat`` (the
    realtime service appends it before enqueuing the generate request; the
    ``GenerateResponseRequest`` itself carries no text). Duck-typed and
    exception-safe: any read failure returns ``None`` -> the turn goes to the
    LLM.
    """
    chat = getattr(runtime_config, "chat", None)
    if chat is None:
        return None
    try:
        for item in reversed(list(getattr(chat, "buffer", []))):
            if getattr(item, "role", None) != "user":
                continue
            parts = [
                p.text
                for p in getattr(item, "content", [])
                if getattr(p, "type", None) == "input_text" and getattr(p, "text", None)
            ]
            return " ".join(parts).strip() if parts else None
    except Exception:
        logger.debug("ParrotGate: failed reading chat buffer; deferring to LLM", exc_info=True)
        return None
    return None


def emit_synthetic_reply(
    lm_response_queue: Optional[Queue[LMOutItem]],
    request: GenerateResponseRequest,
    reply_text: str,
    route: str,
) -> None:
    """Inject a synthetic LM response for *reply_text* onto lm_response_queue.

    *route* is the label ``turn_stats`` logs for the turn.

    Turn bookkeeping: start and stamp the turn on *this* thread before the
    synthetic chunk is dequeued downstream. The chunk carries
    ``speech_stopped_at_s=None`` so ``LMOutputProcessor.on_llm_chunk`` does
    not flush-and-restart the turn (which would reset the route) -- it only
    observes the already-open synthetic turn. The trailing EndOfResponse then
    flushes it, logging TURN_STATS with *route*. Ordering is guaranteed:
    set_route happens-before the put, which happens-before the downstream
    dequeue.
    """
    turn_stats.on_llm_chunk(request.speech_stopped_at_s)
    turn_stats.set_route(route)

    if lm_response_queue is None:
        return

    lm_response_queue.put(
        LLMResponseChunk(
            text=reply_text,
            language_code=request.language_code,
            tools=[],
            runtime_config=request.runtime_config,
            response=request.response,
            turn_id=request.turn_id,
            turn_revision=request.turn_revision,
            speech_stopped_at_s=None,
            cancel_generation=None,
        )
    )
    lm_response_queue.put(
        EndOfResponse(
            turn_id=request.turn_id,
            turn_revision=request.turn_revision,
            cancel_generation=None,
        )
    )


class ParrotGate(BaseHandler[LLMIn, LLMIn]):
    """Echoes every fresh user turn back verbatim; the LM never sees it.

    ``queue_out`` feeds the LM handler (only non-turn / follow-up messages and
    empty transcripts ever reach it while parrot is armed). Echoed replies are
    injected onto ``lm_response_queue`` -- the LM handler's *output* queue --
    so ``LMOutputProcessor`` and everything downstream behave identically to a
    normal short LLM turn.
    """

    def setup(
        self,
        lm_response_queue: Optional[Queue[LMOutItem]] = None,
        armed: bool = False,
    ) -> None:
        if lm_response_queue is None:
            logger.warning(
                "ParrotGate configured without lm_response_queue: every turn will "
                "fail open to the LLM instead of being echoed"
            )
        self.lm_response_queue = lm_response_queue
        # Live-toggleable: brain_control.py's config_set flips this attribute
        # directly (same idiom as the wake-word gate's `.enabled`), no restart.
        self.armed = armed

    def process(self, request: LLMIn) -> Iterator[LLMOut]:
        if not self.armed:
            yield request
            return

        # Only a fresh user turn is echoable. A tool-call follow-up generation
        # carries speech_stopped_at_s=None and must always reach the LM; so
        # must anything that is not a GenerateResponseRequest.
        if not isinstance(request, GenerateResponseRequest) or request.speech_stopped_at_s is None:
            yield request
            return

        text = _last_user_text(request.runtime_config)
        if not text or not text.strip():
            # An empty/whitespace transcript is its own bug; echoing it would
            # muddy the instrument. Forward to the LM as usual.
            yield request
            return

        if self.lm_response_queue is None:
            logger.error("ParrotGate: lm_response_queue not configured; failing open")
            yield request
            return

        emit_synthetic_reply(self.lm_response_queue, request, text, "parrot")
        logger.info("PARROT route=parrot text=%r", text[:80])
