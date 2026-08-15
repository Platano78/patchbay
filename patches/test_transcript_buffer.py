"""Unit tests for transcript_buffer.py (server-side history replay buffer).

Run from repo root: python3 -m pytest patches/test_transcript_buffer.py -v

transcript_buffer.py is dependency-free (no speech_to_speech, no non-stdlib
imports), so these tests need no stubs and no installed package.
"""

from __future__ import annotations

import pytest

from patches.transcript_buffer import DEFAULT_CAP, TranscriptBuffer, _parse_cap, _parse_enabled


def _completed(transcript: str) -> dict:
    return {"type": "transcription_completed", "transcript": transcript}


def _assistant(text: str = "", tools: list | None = None) -> dict:
    payload: dict = {"type": "assistant_text", "text": text}
    if tools is not None:
        payload["tools"] = tools
    return payload


# ── pairing / chronological order ──────────────────────────────────────────


def test_pairing_produces_one_completed_turn():
    buf = TranscriptBuffer()
    buf.feed(_completed("hello"), now=1.0)
    buf.feed(_assistant("hi there"), now=2.0)

    replay = buf.replay_payload()

    assert replay == {"type": "history_replay", "entries": [{"user": "hello", "assistant": "hi there", "ts": 1.0}]}


def test_multiple_turns_are_oldest_first():
    buf = TranscriptBuffer()
    buf.feed(_completed("first"), now=1.0)
    buf.feed(_assistant("reply one"), now=1.5)
    buf.feed(_completed("second"), now=2.0)
    buf.feed(_assistant("reply two"), now=2.5)

    replay = buf.replay_payload()

    assert [e["user"] for e in replay["entries"]] == ["first", "second"]
    assert [e["ts"] for e in replay["entries"]] == [1.0, 2.0]


# ── tool-only assistant frame ignored ──────────────────────────────────────


def test_tool_only_assistant_frame_ignored():
    buf = TranscriptBuffer()
    buf.feed(_completed("what's the weather"), now=1.0)
    buf.feed(_assistant("", tools=[{"name": "get_weather"}]), now=1.2)

    # Pending turn is still open -- not yet in the replay.
    assert buf.replay_payload() is None

    buf.feed(_assistant("it's sunny"), now=1.4)

    replay = buf.replay_payload()
    assert replay["entries"] == [{"user": "what's the weather", "assistant": "it's sunny", "ts": 1.0}]


# ── barge-in: unanswered turn closed as interrupted ────────────────────────


def test_barge_in_closes_prior_turn_as_interrupted():
    buf = TranscriptBuffer()
    buf.feed(_completed("first question"), now=1.0)
    buf.feed(_completed("second question"), now=2.0)  # no assistant reply between
    buf.feed(_assistant("answer to second"), now=2.5)

    replay = buf.replay_payload()

    assert replay["entries"] == [
        {"user": "first question", "assistant": "— (interrupted)", "ts": 1.0},
        {"user": "second question", "assistant": "answer to second", "ts": 2.0},
    ]


# ── empty/blank transcript ignored ─────────────────────────────────────────


def test_empty_transcript_ignored():
    buf = TranscriptBuffer()
    buf.feed(_completed(""), now=1.0)
    buf.feed(_completed("   "), now=1.1)

    assert buf.replay_payload() is None

    buf.feed(_completed("real question"), now=2.0)
    buf.feed(_assistant("real answer"), now=2.1)

    replay = buf.replay_payload()
    assert replay["entries"] == [{"user": "real question", "assistant": "real answer", "ts": 2.0}]


# ── pending (unanswered) turn not in replay ────────────────────────────────


def test_pending_turn_not_in_replay():
    buf = TranscriptBuffer()
    buf.feed(_completed("still waiting"), now=1.0)

    assert buf.replay_payload() is None


# ── replay_payload is None with zero completed turns ───────────────────────


def test_replay_none_when_nothing_completed():
    buf = TranscriptBuffer()
    assert buf.replay_payload() is None


# ── ts carried through from the transcription time, not the assistant time ─


def test_ts_is_transcription_time_not_assistant_time():
    buf = TranscriptBuffer()
    buf.feed(_completed("hello"), now=100.0)
    buf.feed(_assistant("hi"), now=9999.0)

    replay = buf.replay_payload()
    assert replay["entries"][0]["ts"] == 100.0


# ── cap eviction ────────────────────────────────────────────────────────────


def test_cap_eviction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_HISTORY_REPLAY_TURNS", "2")
    buf = TranscriptBuffer()
    for i in range(3):
        buf.feed(_completed(f"q{i}"), now=float(i))
        buf.feed(_assistant(f"a{i}"), now=float(i) + 0.5)

    replay = buf.replay_payload()
    assert [e["user"] for e in replay["entries"]] == ["q1", "q2"]


# ── VOICE_HISTORY_REPLAY=off disables entirely ──────────────────────────────


def test_replay_off_disables_feed_and_replay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_HISTORY_REPLAY", "off")
    buf = TranscriptBuffer()
    buf.feed(_completed("hello"), now=1.0)
    buf.feed(_assistant("hi"), now=1.5)

    assert buf.replay_payload() is None


def test_replay_off_case_insensitive(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_HISTORY_REPLAY", "  Off  ")
    buf = TranscriptBuffer()
    buf.feed(_completed("hello"), now=1.0)
    buf.feed(_assistant("hi"), now=1.5)

    assert buf.replay_payload() is None


# ── VOICE_HISTORY_REPLAY_TURNS <= 0 disables via feed/replay no-op ─────────


def test_cap_zero_disables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_HISTORY_REPLAY_TURNS", "0")
    buf = TranscriptBuffer()
    buf.feed(_completed("hello"), now=1.0)
    buf.feed(_assistant("hi"), now=1.5)

    assert buf.replay_payload() is None


def test_cap_negative_disables(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_HISTORY_REPLAY_TURNS", "-5")
    buf = TranscriptBuffer()
    buf.feed(_completed("hello"), now=1.0)
    buf.feed(_assistant("hi"), now=1.5)

    assert buf.replay_payload() is None


# ── VOICE_HISTORY_REPLAY_TURNS parsing (_parse_cap unit tests) ─────────────


def test_parse_cap_unset_is_default():
    assert _parse_cap(None) == DEFAULT_CAP


def test_parse_cap_blank_is_default():
    assert _parse_cap("   ") == DEFAULT_CAP


def test_parse_cap_malformed_is_default():
    assert _parse_cap("not-a-number") == DEFAULT_CAP


def test_parse_cap_custom_value():
    assert _parse_cap("7") == 7


def test_custom_cap_value_respected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOICE_HISTORY_REPLAY_TURNS", "1")
    buf = TranscriptBuffer()
    buf.feed(_completed("q0"), now=0.0)
    buf.feed(_assistant("a0"), now=0.5)
    buf.feed(_completed("q1"), now=1.0)
    buf.feed(_assistant("a1"), now=1.5)

    replay = buf.replay_payload()
    assert [e["user"] for e in replay["entries"]] == ["q1"]


# ── VOICE_HISTORY_REPLAY parsing (_parse_enabled unit tests) ───────────────


def test_parse_enabled_unset_is_true():
    assert _parse_enabled(None) is True


def test_parse_enabled_blank_is_true():
    assert _parse_enabled("   ") is True


def test_parse_enabled_off_is_false():
    assert _parse_enabled("off") is False
    assert _parse_enabled("  OFF  ") is False


def test_parse_enabled_other_value_is_true():
    assert _parse_enabled("on") is True
