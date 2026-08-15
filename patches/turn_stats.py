"""Per-turn latency instrumentation (fulloch-borrow, Slice 4).

Observe-only: no data flowing through the pipeline is touched. Stamps a
handful of events already visible from the patch surface -- first LLM chunk,
first TTS input, first audio out, end of turn -- against the upstream
``speech_stopped_at_s`` timestamp (set when the user's speech ends; see
``speech_to_speech.pipeline.messages.VADAudio.created_at_s``) and logs one
``TURN_STATS`` line per completed turn.

Stdlib-only. Thread-safe: VAD/STT, the LM output processor, and the
WebSocket sender each run on their own thread, and all call into the single
module-level ``turn_stats`` singleton below.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock

logger = logging.getLogger(__name__)

# Same clock domain as the upstream `speech_stopped_at_s` timestamp
# (VADAudio.created_at_s uses `time.perf_counter` as its pydantic
# default_factory) -- deltas are only meaningful if both sides come from
# the same clock, so this module uses perf_counter throughout rather than
# time.monotonic.
_clock = time.perf_counter


@dataclass
class _Turn:
    speech_stopped_at_s: float | None
    route: str = "llm"
    marks: dict[str, float] = field(default_factory=dict)
    llm_chunks: int = 0
    pending_followup: bool = False


class TurnStats:
    """Tracks the single in-flight turn for this pipeline instance."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._turn: _Turn | None = None

    def on_llm_chunk(self, speech_stopped_at_s: float | None) -> None:
        """Call once per LLMResponseChunk reaching LMOutputProcessor.

        ``speech_stopped_at_s`` is set only on a turn's first generation
        cycle; a tool-call follow-up cycle passes None (see
        LMOutputProcessor._run_tool_calls) and simply continues accumulating
        into the already-active turn. A non-None value while a turn is
        already active means the previous turn's EndOfResponse was dropped
        (e.g. cancelled/stale) -- flush what it has and start fresh so state
        never silently accumulates across turns.
        """
        with self._lock:
            if speech_stopped_at_s is not None:
                if self._turn is not None:
                    self._flush_locked()
                self._turn = _Turn(speech_stopped_at_s=speech_stopped_at_s)
            if self._turn is not None:
                self._turn.marks.setdefault("ttft_llm", _clock())
                self._turn.llm_chunks += 1

    def on_tts_input(self) -> None:
        """Call right before each TTSInput is yielded to the TTS queue."""
        with self._lock:
            if self._turn is not None:
                self._turn.marks.setdefault("first_tts_in", _clock())

    def mark(self, event: str) -> None:
        """Record an arbitrary named event (first call per turn wins).

        Used from outside LMOutputProcessor (e.g. the WebSocket sender's
        first real audio flush) where there is no turn_id to correlate
        against -- callers just mark "now" against whatever turn is
        currently active. Best-effort: a call with no active turn is
        dropped silently, never raises.
        """
        with self._lock:
            if self._turn is not None:
                self._turn.marks.setdefault(event, _clock())

    def note_followup_pending(self) -> None:
        """Call when a tool call just enqueued a follow-up generation.

        The next end_of_response() belongs to this turn's next generation
        cycle, not the turn's true end -- consume the flag there instead of
        flushing.
        """
        with self._lock:
            if self._turn is not None:
                self._turn.pending_followup = True

    def set_route(self, route: str) -> None:
        """Reserved for the reflex lane (Slice 2) to mark route="reflex"."""
        with self._lock:
            if self._turn is not None:
                self._turn.route = route

    def end_of_response(self) -> None:
        """Call on every EndOfResponse reaching LMOutputProcessor.

        Flushes and logs the turn, unless a tool-call follow-up was just
        queued for it, in which case the pending flag is consumed and
        tracking continues into the next generation cycle.
        """
        with self._lock:
            if self._turn is None:
                return
            if self._turn.pending_followup:
                self._turn.pending_followup = False
                return
            self._turn.marks.setdefault("end_of_response", _clock())
            self._flush_locked()

    def flush(self) -> None:
        """Force-flush whatever the active turn has, if any."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        turn = self._turn
        self._turn = None
        if turn is None:
            return

        start = turn.speech_stopped_at_s

        def _delta(event: str) -> float | None:
            ts = turn.marks.get(event)
            if ts is None or start is None:
                return None
            return ts - start

        turn_total_s = _delta("end_of_response")

        logger.info(
            "TURN_STATS route=%s ttft_llm_s=%s first_tts_in_s=%s first_audio_out_s=%s "
            "turn_total_s=%s llm_chunks=%d",
            turn.route,
            _fmt(_delta("ttft_llm")),
            _fmt(_delta("first_tts_in")),
            _fmt(_delta("first_audio_out")),
            _fmt(turn_total_s),
            turn.llm_chunks,
        )


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "NA"


turn_stats = TurnStats()
