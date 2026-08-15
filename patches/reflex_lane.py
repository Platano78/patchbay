"""Pre-LLM reflex lane (fulloch-borrow, Slice 2).

A conservative regex fast-path that short-circuits obvious smart-home
utterances straight to the Home Assistant voice tool, skipping the LLM
entirely. Deploy-safe: the pipeline only inserts :class:`ReflexGate` when
``VOICE_REFLEX=1`` (see ``s2s_pipeline._build_pipeline_handlers``), so a plain
restart leaves behaviour unchanged.

Design (docs/plans/fulloch-borrow-voice-agent-2026-07-10.md, Slice 2):

* **Bias hard toward false negatives.** A false positive costs an LLM-quality
  answer; a false negative costs only the normal ~1s LLM path. Triggers mirror
  the HA tool's parse heads but add a device-noun gate on the actuation verbs,
  so "turn on the kitchen lights" fast-paths while "turn on the charm" falls
  through to the LLM.
* **Fail open.** Dispatch goes through ``voice_tools.execute("home_assistant",
  ...)``. If that returns a "not configured / not armed / didn't parse"
  sentinel, the original request is forwarded downstream unchanged, exactly as
  if this gate were absent.
* **Reuse the LM output path.** On a real hit the reply is injected onto the
  same ``lm_response_queue`` the LM handler feeds, so ``LMOutputProcessor`` and
  everything downstream (client text event, TTS, mic re-open) behave exactly as
  for a normal short LLM turn -- no bespoke turn plumbing.

Known accepted limitation: reflex turns never enter the LM handler's chat
history, so a follow-up like "and the bedroom?" lacks the prior exchange
(fulloch's ``_run_without_llm`` has the same trade). The upstream LM handler is
left untouched for this.
"""

from __future__ import annotations

import logging
import re
import time
from queue import Queue
from typing import Any, Iterator, Optional

from speech_to_speech import voice_tools
from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.handler_types import LLMIn, LLMOut
from speech_to_speech.pipeline.messages import EndOfResponse, GenerateResponseRequest, LLMResponseChunk
from speech_to_speech.pipeline.queue_types import LMOutItem
from speech_to_speech.turn_stats import turn_stats

logger = logging.getLogger(__name__)

# ── Fail-open sentinels ───────────────────────────────────────────────
# Strings the HA tool returns when it did NOT actually act: unconfigured,
# unarmed, or the parse-miss capabilities line. On any of these the gate
# forwards the turn to the LLM instead of speaking the sentinel back.
_FAIL_OPEN_EXACT = frozenset(
    {
        "Home Assistant isn't set up yet.",
        "That tool isn't available.",
    }
)
_CAPABILITIES_PREFIX = "I can turn things on or off"


def _is_fail_open(result: str) -> bool:
    r = result.strip()
    return r in _FAIL_OPEN_EXACT or r.startswith(_CAPABILITIES_PREFIX)


# ── Trigger detection ─────────────────────────────────────────────────
# Anchored, full-utterance regexes mirroring home_assistant.parse_command's
# heads. Kept independent of that module (a dynamically loaded VOICE_TOOLS_DIR
# drop-in with no stable import path) so this gate is self-contained; the real
# parse + dispatch still happens inside voice_tools.execute, the backstop for
# anything these looser gates over-trigger on.

_COMPOUND_RE = re.compile(r"\b(?:and|then)\b", re.IGNORECASE)

# Entity references too vague to fast-path -- defer to the LLM.
_VAGUE_ENTITIES = frozenset({"it", "that", "this", "them", "those", "these", "everything", "all"})
_ANAPHORA_DETERMINERS = frozenset({"it", "that", "this", "them", "those", "these"})

# Actuation heads only fire on a concrete device noun. This is the extra gate
# (beyond parse_command) that keeps "turn on the charm" on the LLM path while
# letting "turn on the kitchen lights" through.
_DEVICE_NOUN_RE = re.compile(r"\b(?:lights?|lamps?|fans?|switch(?:es)?|plugs?|bulbs?|outlets?)\b", re.IGNORECASE)

_COLORS = (
    "warm white|cool white|red|green|blue|yellow|orange|purple|pink|white|"
    "cyan|magenta|teal|indigo|violet|gold"
)

_BRIGHTNESS_RE = re.compile(
    r"^\s*(?:please\s+)?(?:set|change|put|turn|adjust)\s+(?:the\s+)?(.+?)\s+brightness\s+to\s+.+?\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_COLOR_RE = re.compile(
    r"^\s*(?:please\s+)?(?:set|change|make|turn)\s+(?:the\s+)?(.+?)\s+(?:to\s+|colou?r\s+to\s+)?(?:" + _COLORS + r")\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_SCENE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:activate|run|start)\s+(?:the\s+)?(.+?)\s+scene\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_TURN_RE = re.compile(
    r"^\s*(?:please\s+)?turn\s+(?:on|off)\s+(?:the\s+)?(.+?)\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_TURN_REV_RE = re.compile(
    r"^\s*(?:please\s+)?turn\s+(?:the\s+)?(.+?)\s+(?:on|off)\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_TOGGLE_RE = re.compile(
    r"^\s*(?:please\s+)?toggle\s+(?:the\s+)?(.+?)\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_QUERY_POWER_RE = re.compile(
    r"^\s*(?:please\s+)?is\s+(?:the\s+)?(.+?)\s+(?:on|off)\s*\??\s*$",
    re.IGNORECASE,
)
_QUERY_TEMP_RE = re.compile(
    r"^\s*(?:please\s+)?what(?:'s|\s+is)\s+the\s+temperature\s+(?:in|at)\s+(?:the\s+)?(.+?)\s*\??\s*$",
    re.IGNORECASE,
)

# Actuation heads: matched entity must be concrete (device noun, not vague).
_ACTUATION_HEADS = (_BRIGHTNESS_RE, _COLOR_RE, _TURN_RE, _TURN_REV_RE, _TOGGLE_RE)
# Query/scene heads: structure is specific enough; only reject vague entities
# (a power query targets any real entity name, e.g. "is the sun on?").
_QUERY_HEADS = (_SCENE_RE, _QUERY_POWER_RE, _QUERY_TEMP_RE)


def _is_vague_entity(entity: str) -> bool:
    e = entity.lower().strip()
    if e in _VAGUE_ENTITIES:
        return True
    first = e.split(None, 1)[0] if e.split() else ""
    return first in _ANAPHORA_DETERMINERS


def is_reflex_candidate(text: str) -> bool:
    """Whether *text* is an obvious smart-home command worth the fast path.

    Conservative by construction: compound utterances, vague/anaphoric
    entities, and actuation verbs without a concrete device noun all return
    ``False`` so the turn goes to the LLM.
    """
    if not text or not text.strip():
        return False
    command = text.strip()
    if _COMPOUND_RE.search(command):
        return False

    for rx in _ACTUATION_HEADS:
        m = rx.match(command)
        if m:
            entity = m.group(1).strip()
            return bool(entity) and not _is_vague_entity(entity) and bool(_DEVICE_NOUN_RE.search(entity))

    for rx in _QUERY_HEADS:
        m = rx.match(command)
        if m:
            entity = m.group(1).strip()
            return bool(entity) and not _is_vague_entity(entity)

    return False


def _last_user_text(runtime_config: Any) -> Optional[str]:
    """Best-effort read of the latest user message text from the chat buffer.

    The transcript the LLM would see lives only in ``runtime_config.chat`` (the
    realtime service appends it before enqueuing the generate request; the
    ``GenerateResponseRequest`` itself carries no text). Duck-typed and
    exception-safe: any read failure returns ``None`` -> fall through to the
    LLM, matching the bias toward false negatives.
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
        logger.debug("ReflexGate: failed reading chat buffer; deferring to LLM", exc_info=True)
        return None
    return None


class ReflexGate(BaseHandler[LLMIn, LLMIn]):
    """Sits between the transcription/service stage and the LM handler.

    Forwards every request straight through *except* a fresh user turn whose
    transcript is an obvious smart-home command that the HA tool actually
    resolves -- those are answered here and never reach the LM.

    ``queue_out`` feeds the LM handler (forwarded requests). Short-circuit
    replies are injected onto ``lm_response_queue`` -- the LM handler's *output*
    queue -- so ``LMOutputProcessor`` handles them identically to a real LLM
    response.
    """

    def setup(self, lm_response_queue: Optional[Queue[LMOutItem]] = None) -> None:
        if lm_response_queue is None:
            logger.warning(
                "ReflexGate configured without lm_response_queue: every reflex hit will "
                "fail open to the LLM instead of short-circuiting"
            )
        self.lm_response_queue = lm_response_queue

    def process(self, request: LLMIn) -> Iterator[LLMOut]:
        # Only a fresh user turn is a reflex candidate. A tool-call follow-up
        # generation carries speech_stopped_at_s=None (see turn_stats docstring)
        # and must always reach the LM; so must anything that is not a
        # GenerateResponseRequest.
        if not isinstance(request, GenerateResponseRequest) or request.speech_stopped_at_s is None:
            yield request
            return

        text = _last_user_text(request.runtime_config)
        if not text or not is_reflex_candidate(text):
            yield request
            return

        try:
            result = voice_tools.execute("home_assistant", {"command": text})
        except Exception:
            # execute() is documented never to raise; guard anyway and fail open.
            logger.exception("ReflexGate: home_assistant execute raised; deferring to LLM")
            yield request
            return

        if not result or _is_fail_open(result):
            logger.debug("ReflexGate: fail-open for %r (result=%r); deferring to LLM", text, result)
            yield request
            return

        if self.lm_response_queue is None:
            # No queue to answer on; short-circuiting here would swallow the turn
            # (never yielded, never answered -> mic never re-opens). Fail open.
            logger.error("ReflexGate: lm_response_queue not configured; failing open")
            yield request
            return

        # Real hit: answer here, do not forward to the LM.
        self._emit_reply(request, result)
        latency_s = time.perf_counter() - request.speech_stopped_at_s
        logger.info("REFLEX route=reflex latency_s=%.3f text=%r", latency_s, text[:80])

    def _emit_reply(self, request: GenerateResponseRequest, reply_text: str) -> None:
        """Inject a synthetic LM response for *reply_text* onto lm_response_queue.

        Turn bookkeeping: start and stamp the turn on *this* thread before the
        synthetic chunk is dequeued downstream. The chunk carries
        ``speech_stopped_at_s=None`` so ``LMOutputProcessor.on_llm_chunk`` does
        not flush-and-restart the turn (which would reset route to "llm") -- it
        only observes the already-open reflex turn. The trailing EndOfResponse
        then flushes it, logging TURN_STATS route=reflex. Ordering is
        guaranteed: set_route happens-before the put, which happens-before the
        downstream dequeue.
        """
        turn_stats.on_llm_chunk(request.speech_stopped_at_s)
        turn_stats.set_route("reflex")

        if self.lm_response_queue is None:
            return

        self.lm_response_queue.put(
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
        self.lm_response_queue.put(
            EndOfResponse(
                turn_id=request.turn_id,
                turn_revision=request.turn_revision,
                cancel_generation=None,
            )
        )
