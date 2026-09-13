"""Parrot mode: an ear-test instrument, not a product feature.

When armed, :class:`ParrotGate` sits between transcription and the LM
handler and answers *every* fresh user turn with its own transcript,
verbatim, via the same synthetic-reply path
:func:`reflex_lane.emit_synthetic_reply` uses. The LM is never consulted.
Everything downstream of the LM (``LMOutputProcessor``, TTS, the client audio
path) runs exactly as for a normal short LLM turn, so a broken link anywhere
in that chain becomes audible in seconds -- the reflex/LLM lanes only prove
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

Ordering with the reflex lane: ParrotGate sits BEFORE ReflexGate in the
chain, so when armed it short-circuits a turn before reflex ever sees it --
a reflex hit during a parrot run would answer from the Home Assistant tool
instead of echoing the user's own words, which would make the instrument
lie about the path under test. When disarmed, turns flow through to reflex
normally. Both gates may be present in the chain at once; ``reflex_present``
only controls a log annotation below.

Because this is a diagnostic instrument, its cardinal sin is answering from
anywhere other than "the user's own transcript, through the normal TTS
path". It must never fall back to the LM's chat history, never rewrite the
text, and never swallow a turn it cannot echo.
"""

from __future__ import annotations

import logging
from queue import Queue
from typing import Iterator, Optional

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.handler_types import LLMIn, LLMOut
from speech_to_speech.pipeline.messages import GenerateResponseRequest
from speech_to_speech.pipeline.queue_types import LMOutItem

from speech_to_speech.reflex_lane import _last_user_text, emit_synthetic_reply

logger = logging.getLogger(__name__)


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
        reflex_present: bool = False,
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
        self.reflex_present = reflex_present

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
        if self.reflex_present:
            logger.info("PARROT route=parrot text=%r (reflex lane present, bypassed)", text[:80])
        else:
            logger.info("PARROT route=parrot text=%r", text[:80])
