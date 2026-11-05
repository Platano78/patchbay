"""filler_clips.py: the clip cache and the turn-end filler timer.

Run from repo root: python3 -m pytest patches/test_filler_clips.py -v
"""

from __future__ import annotations

import time
from queue import Queue

import numpy as np
import pytest

from patches import filler_clips as fc

DELAY_MS = 40


def _clip(n=4800):
    return (np.arange(n, dtype=np.int16) % 200).tobytes()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_FILLER_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TURN_FILLER", "on")
    monkeypatch.setenv("VOICE_TURN_FILLER_DELAY_MS", str(DELAY_MS))
    return tmp_path


def _write(root, voice, phrase, data=None):
    p = fc.clip_path(voice, phrase, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if data is not None else _clip())


def _make(voice="ryan", picks=("One sec.",)):
    f = fc.FillerClips()
    q: Queue = Queue()
    f.attach_sink(q)
    f.set_voice_getter(lambda: voice)
    it = iter(picks)
    f.set_turn_picker(lambda: next(it, None))
    return f, q


def _wait_for(q, timeout=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not q.empty():
            return True
        time.sleep(0.005)
    return False


def _settle():
    time.sleep(DELAY_MS / 1000 * 3 + 0.1)


# -- cache -----------------------------------------------------------------
def test_play_cached_enqueues_the_voices_clip(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    assert f.play_cached("One sec.") is True
    item = q.get_nowait()
    assert isinstance(item, fc.FillerClip) and bytes(item) == _clip()


def test_play_cached_is_per_voice(env):
    _write(env, "ryan", "One sec.")
    f, q = _make(voice="the-butler")
    assert f.play_cached("One sec.") is False
    assert q.empty()


@pytest.mark.parametrize("voice", [None, ""])
def test_play_cached_without_a_voice_is_a_noop(env, voice):
    _write(env, "ryan", "One sec.")
    f, q = _make(voice=voice)
    assert f.play_cached("One sec.") is False and q.empty()


def test_play_cached_without_a_sink_is_a_noop(env):
    _write(env, "ryan", "One sec.")
    f = fc.FillerClips()
    f.set_voice_getter(lambda: "ryan")
    assert f.play_cached("One sec.") is False


def test_unusable_clips_are_ignored(env):
    _write(env, "ryan", "Odd.", b"\x01\x02\x03")  # odd byte count
    _write(env, "ryan", "Huge.", bytes(2 * 48000 * 10))
    _write(env, "ryan", "Empty.", b"")
    f, q = _make()
    assert not any(f.play_cached(p) for p in ("Odd.", "Huge.", "Empty."))


def test_voice_names_cannot_escape_the_cache_dir(env):
    assert fc.clip_path("../../etc", "x", env).parent.parent == env


# -- turn filler -------------------------------------------------------------
def test_turn_filler_fires_when_answered_and_no_reply_audio(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_transcript("what time is it")
    assert _wait_for(q)
    assert isinstance(q.get_nowait(), fc.TurnFillerClip)
    assert f.turn_filler_just_played()


def test_turn_filler_waits_for_a_late_transcript(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    _settle()
    assert q.empty()  # delay passed, turn not known to be answered
    f.note_transcript("hello")
    assert _wait_for(q)


def test_turn_filler_never_fires_for_an_unanswered_turn(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_transcript("   ")  # blank transcript = dropped turn
    _settle()
    assert q.empty()
    f.cancel()


def test_no_filler_once_reply_audio_started(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_transcript("hi")
    f.note_reply_audio()
    _settle()
    assert q.empty()


def test_no_filler_when_reply_audio_is_already_queued(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    q.put(np.zeros(1600, dtype=np.int16))
    f.on_turn_end()
    f.note_transcript("hi")
    _settle()
    assert q.qsize() == 1 and not isinstance(q.queue[0], fc.FillerClip)


def test_new_speech_cancels_the_pending_filler(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_transcript("hi")
    f.cancel()  # VAD: the user is speaking again
    _settle()
    assert q.empty()


def test_new_speech_pulls_an_already_queued_clip(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_transcript("hi")
    assert _wait_for(q)
    f.cancel()
    assert q.empty()


def test_a_newer_turn_supersedes_the_older_timer(env):
    _write(env, "ryan", "One sec.")
    f, q = _make(picks=("One sec.", "One sec."))
    f.on_turn_end()
    f.on_turn_end()
    f.note_transcript("hi")
    _settle()
    assert q.qsize() == 1


def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv("VOICE_TURN_FILLER")
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_transcript("hi")
    _settle()
    assert q.empty()


def test_no_clip_for_the_voice_is_a_noop(env):
    f, q = _make(voice="nobody")
    f.on_turn_end()
    f.note_transcript("hi")
    _settle()
    assert q.empty() and not f.turn_filler_just_played()


def test_a_tool_filler_this_turn_stands_the_turn_filler_down(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_filler()  # tool path spoke/queued a filler
    f.note_transcript("hi")
    _settle()
    assert q.empty()


def test_a_tool_filler_from_the_previous_turn_does_not_block(env):
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.note_filler()
    time.sleep(0.01)
    f.on_turn_end()
    f.note_transcript("hi")
    assert _wait_for(q)


def test_turn_filler_only_ever_writes_audio_to_the_sink(env):
    """Fillers are audio-queue items; there is no path from here to chat history
    or the text event queue."""
    _write(env, "ryan", "One sec.")
    f, q = _make()
    f.on_turn_end()
    f.note_transcript("hi")
    assert _wait_for(q)
    assert all(isinstance(i, fc.FillerClip) for i in list(q.queue))
