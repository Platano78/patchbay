"""Unit tests for turn_stats.py (Slice 4 instrumentation).

Run from repo root: python3 -m unittest patches.test_turn_stats -v
"""

from __future__ import annotations

import logging
import unittest

from patches import turn_stats as turn_stats_module


class FakeClock:
    """Deterministic stand-in for time.perf_counter."""

    def __init__(self, start: float = 100.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TurnStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self._orig_clock = turn_stats_module._clock
        turn_stats_module._clock = self.clock
        self.stats = turn_stats_module.TurnStats()

    def tearDown(self) -> None:
        turn_stats_module._clock = self._orig_clock

    def _last_record(self, cm: "unittest._AssertLogsContext") -> logging.LogRecord:
        return cm.records[-1]

    def test_full_turn_logs_expected_deltas(self) -> None:
        speech_stopped_at_s = 100.0
        with self.assertLogs("patches.turn_stats", level="INFO") as cm:
            self.clock.advance(0.20)
            self.stats.on_llm_chunk(speech_stopped_at_s)  # ttft_llm @ 0.20s
            self.clock.advance(0.05)
            self.stats.on_llm_chunk(None)  # 2nd chunk, no reset, no new mark
            self.clock.advance(0.05)
            self.stats.on_tts_input()  # first_tts_in @ 0.30s
            self.clock.advance(0.40)
            self.stats.mark("first_audio_out")  # @ 0.70s
            self.clock.advance(0.30)
            self.stats.end_of_response()  # turn_total @ 1.00s, flush

        line = self._last_record(cm).getMessage()
        self.assertIn("route=llm", line)
        self.assertIn("ttft_llm_s=0.200", line)
        self.assertIn("first_tts_in_s=0.300", line)
        self.assertIn("first_audio_out_s=0.700", line)
        self.assertIn("turn_total_s=1.000", line)
        self.assertIn("llm_chunks=2", line)
        # Turn state cleared after flush.
        self.assertIsNone(self.stats._turn)

    def test_double_turn_reset_flushes_stale_turn_first(self) -> None:
        with self.assertLogs("patches.turn_stats", level="INFO") as cm:
            self.stats.on_llm_chunk(100.0)  # turn A starts
            self.clock.advance(0.15)
            self.stats.on_tts_input()
            # Turn A's end_of_response never arrives (dropped/cancelled) --
            # turn B's first chunk should flush A (partial) then start fresh.
            self.clock.advance(1.0)
            self.stats.on_llm_chunk(101.15)  # turn B starts, new speech_stopped_at_s
            self.clock.advance(0.10)
            self.stats.end_of_response()  # flush B

        self.assertEqual(len(cm.records), 2)
        turn_a_line = cm.records[0].getMessage()
        turn_b_line = cm.records[1].getMessage()

        # Turn A flushed with only ttft_llm/first_tts_in populated, no
        # turn_total (its end_of_response never happened) -- doesn't crash.
        self.assertIn("ttft_llm_s=0.000", turn_a_line)
        self.assertIn("first_tts_in_s=0.150", turn_a_line)
        self.assertIn("turn_total_s=NA", turn_a_line)
        self.assertIn("llm_chunks=1", turn_a_line)

        self.assertIn("ttft_llm_s=0.000", turn_b_line)
        self.assertIn("turn_total_s=0.100", turn_b_line)
        self.assertIn("llm_chunks=1", turn_b_line)

    def test_partial_turn_missing_stamps_does_not_crash(self) -> None:
        with self.assertLogs("patches.turn_stats", level="INFO") as cm:
            self.stats.on_llm_chunk(100.0)
            # No TTS input, no audio out -- e.g. a tool-only response with no
            # audio (response_wants_audio False) -- go straight to end.
            self.clock.advance(0.05)
            self.stats.end_of_response()

        line = self._last_record(cm).getMessage()
        self.assertIn("ttft_llm_s=0.000", line)
        self.assertIn("first_tts_in_s=NA", line)
        self.assertIn("first_audio_out_s=NA", line)
        self.assertIn("turn_total_s=0.050", line)
        self.assertIn("llm_chunks=1", line)

    def test_mark_with_no_active_turn_is_a_silent_noop(self) -> None:
        # No turn started -- e.g. the WebSocket sender's audio flush races
        # ahead of the first LLM chunk. Must not raise.
        self.stats.mark("first_audio_out")
        self.stats.end_of_response()
        self.assertIsNone(self.stats._turn)

    def test_tool_call_followup_defers_flush_to_final_end_of_response(self) -> None:
        with self.assertLogs("patches.turn_stats", level="INFO") as cm:
            self.stats.on_llm_chunk(100.0)  # cycle 1: tool-bearing chunk
            self.clock.advance(0.10)
            self.stats.note_followup_pending()
            self.clock.advance(0.05)
            self.stats.end_of_response()  # cycle 1 end -- must NOT flush
            self.assertEqual(len(cm.records), 0)

            self.stats.on_llm_chunk(None)  # cycle 2: follow-up chunk
            self.clock.advance(0.20)
            self.stats.on_tts_input()
            self.clock.advance(0.10)
            self.stats.end_of_response()  # cycle 2 end -- true turn end, flush

        line = self._last_record(cm).getMessage()
        self.assertIn("llm_chunks=2", line)
        self.assertIn("first_tts_in_s=0.350", line)
        self.assertIn("turn_total_s=0.450", line)

    def test_flush_with_no_active_turn_does_not_raise(self) -> None:
        self.stats.flush()

    def test_set_route_reserved_for_reflex_lane(self) -> None:
        with self.assertLogs("patches.turn_stats", level="INFO") as cm:
            self.stats.on_llm_chunk(100.0)
            self.stats.set_route("reflex")
            self.stats.end_of_response()

        self.assertIn("route=reflex", self._last_record(cm).getMessage())


if __name__ == "__main__":
    unittest.main()
