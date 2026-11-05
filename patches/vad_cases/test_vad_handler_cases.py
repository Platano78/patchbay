"""vad_handler.py (patched): Smart Turn act/shadow, PTT skip, and the turn-end filler hook.

Drives the real patched VADHandler with a scripted VADIterator and a fake
smart-turn server call. Needs the real framework (torch, speech_to_speech);
skipped without it.

Lives in its own directory because other test files install a fake, non-package
`speech_to_speech` into sys.modules at collection time; this file needs the real
one, so `patches/test_vad_smart_turn.py` runs it in a fresh interpreter. Inside
a polluted session it skips itself.

Run from repo root: python3 -m pytest patches/vad_cases/test_vad_handler_cases.py -v
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path
from queue import Queue

import numpy as np
import pytest

_pkg = sys.modules.get("speech_to_speech")
if _pkg is not None and not hasattr(_pkg, "__path__"):
    pytest.skip("speech_to_speech is stubbed in this session; see patches/test_vad_smart_turn.py", allow_module_level=True)
torch = pytest.importorskip("torch")
pytest.importorskip("speech_to_speech.baseHandler")
pytest.importorskip("speech_to_speech.pipeline.speculative_turns")

from patches import filler_clips, turn_classifier as tc  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "patched_vad_handler", Path(__file__).resolve().parent.parent / "vad_handler.py"
)
vh = importlib.util.module_from_spec(_spec)
sys.modules["patched_vad_handler"] = vh
_spec.loader.exec_module(vh)

CHUNK = 512
MS = CHUNK / 16000 * 1000  # 32 ms


class FakeIterator:
    """Scripted stand-in for VADIterator: one step per chunk."""

    def __init__(self, script):
        self.script = list(script)
        self.triggered = False
        self.buffer: list = []
        self.active_speech_samples = 0
        self.last_utterance_active_speech_samples = 0
        self.threshold = 0.6
        self.min_silence_samples = 0

    def speech_buffer(self):
        return list(self.buffer)

    def reset_states(self):
        pass

    def __call__(self, x):
        step = self.script.pop(0) if self.script else {}
        out = None
        if step.get("start"):
            self.triggered = True
            self.buffer = [x]
            self.active_speech_samples = step.get("active", 16000)
        elif step.get("end") is not None:
            self.triggered = False
            samples = step["end"]
            self.last_utterance_active_speech_samples = samples
            self.buffer = []
            out = [torch.full((samples,), 0.1)]
        return out


def _ok(complete, p):
    return json.dumps({"custom_schema_output": {"data": {"complete": complete, "probability": p}}}).encode()


class Rig:
    def __init__(self, monkeypatch, script, verdicts, mode="act", hold_ms=320):
        monkeypatch.setattr(vh.torch.hub, "load", lambda *a, **k: (None, None))
        monkeypatch.setattr(vh, "VADIterator", lambda *a, **k: FakeIterator(script))
        monkeypatch.setenv("VOICE_SMART_TURN", mode)
        monkeypatch.setenv("VOICE_SMART_TURN_HOLD_MS", str(hold_ms))
        self.calls = 0
        self.verdicts = list(verdicts)

        def post(url, body, timeout):
            self.calls += 1
            v = self.verdicts.pop(0) if self.verdicts else ("raise",)
            if v[0] == "raise":
                raise TimeoutError("fake")
            if v[0] == "sleep":
                time.sleep(v[1])
                return _ok(True, 0.9)
            return _ok(*v)

        monkeypatch.setattr(tc, "_http_post", post)
        # SmartTurn binds _http_post as a default arg at def time: rebuild with the fake.
        self.listen = threading.Event()
        self.listen.set()
        from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker

        self.text = Queue()
        self.h = vh.VADHandler(
            threading.Event(),
            queue_in=Queue(),
            queue_out=Queue(),
            setup_args=(self.listen,),
            setup_kwargs=dict(
                sample_rate=16000,
                min_silence_ms=300,
                min_speech_ms=64,
                min_speech_continuation_ms=0,
                text_output_queue=self.text,
                speculative_turns=SpeculativeTurnTracker(),
                speculative_reopen_ms=-1,
                unanswered_reopen_ms=-1,
                smart_turn_base_url="http://fake",
            ),
        )
        if self.h._smart_turn is not None:
            self.h._smart_turn._post = post
        self.finals: list = []
        self.n = 0

    def feed(self, k=1):
        for _ in range(k):
            self.n += 1
            for out in self.h.process(bytes(CHUNK * 2)):
                self.finals.append((self.n, out))


def _turn_script(speech=16000, gap=0, second=None):
    """chunk1 speech starts, chunk2 VAD ends the turn; optional resumed fragment."""
    s = [{"start": True}, {"end": speech}]
    if second:
        s += [{}] * gap + [{"start": True}, {"end": second}]
    return s


@pytest.fixture(autouse=True)
def _clean():
    tc.reset_ptt()
    yield
    tc.reset_ptt()
    filler_clips.cancel()


def test_off_emits_at_once_and_never_calls_the_server(monkeypatch):
    r = Rig(monkeypatch, _turn_script(), [], mode="off")
    assert r.h._smart_turn is None
    r.feed(2)
    assert [n for n, _ in r.finals] == [2] and r.calls == 0


def test_act_complete_ends_immediately(monkeypatch):
    r = Rig(monkeypatch, _turn_script(), [(True, 0.98)])
    r.feed(2)
    assert [n for n, _ in r.finals] == [2] and r.calls == 1


def test_act_unknown_ends_immediately(monkeypatch):
    r = Rig(monkeypatch, _turn_script(), [("raise",)])
    r.feed(2)
    assert [n for n, _ in r.finals] == [2]


def test_act_incomplete_holds_then_expires_in_silence(monkeypatch):
    r = Rig(monkeypatch, _turn_script(), [(False, 0.01)], hold_ms=320)
    r.feed(2)
    assert r.finals == []  # held, not emitted
    r.feed(8)
    assert r.finals == []  # 8 * 32 ms = 256 ms < 320
    r.feed(3)
    assert len(r.finals) == 1
    assert r.finals[0][1].mode == "final" and len(r.finals[0][1].audio) == 16000
    assert r.calls == 1  # the expired hold is not classified again
    assert r.h._held_turn is None


def test_act_hold_resume_continues_the_same_turn(monkeypatch):
    r = Rig(monkeypatch, _turn_script(gap=4, second=8000), [(False, 0.02), (True, 0.95)])
    r.feed(2)
    assert r.finals == []
    r.feed(2 + 4)  # gap, then the resumed speech starts (chunk 7) and ends (chunk 8)
    assert len(r.finals) == 1
    audio = r.finals[0][1].audio
    assert len(audio) >= 16000 + 8000  # held turn + silence + new fragment, one turn
    assert r.calls == 2
    assert r.finals[0][0] == 8  # emitted when the second fragment ended, not on expiry


def test_act_resumed_turn_that_is_still_incomplete_holds_again(monkeypatch):
    r = Rig(monkeypatch, _turn_script(gap=2, second=4000), [(False, 0.02), (False, 0.03)], hold_ms=320)
    r.feed(2 + 2 + 2)
    assert r.finals == [] and r.calls == 2
    r.feed(12)
    assert len(r.finals) == 1 and len(r.finals[0][1].audio) >= 16000 + 4000


def test_ptt_turns_are_never_classified(monkeypatch):
    r = Rig(monkeypatch, _turn_script(), [(False, 0.0)])
    tc.note_ptt("down")
    tc.note_ptt("up")
    r.feed(2)
    assert [n for n, _ in r.finals] == [2] and r.calls == 0


def test_shadow_never_delays_emission(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=tc.__name__)
    r = Rig(monkeypatch, _turn_script(), [("sleep", 0.15)], mode="shadow")
    t0 = time.monotonic()
    r.feed(2)
    assert [n for n, _ in r.finals] == [2]
    assert time.monotonic() - t0 < 0.1
    end = time.monotonic() + 2
    while time.monotonic() < end and not any("SMART_TURN_SHADOW" in x.message for x in caplog.records):
        time.sleep(0.01)
    assert any("SMART_TURN_SHADOW complete=true" in x.message for x in caplog.records)


def test_session_end_drops_a_held_turn(monkeypatch):
    r = Rig(monkeypatch, _turn_script(), [(False, 0.0)])
    r.feed(2)
    assert r.h._held_turn is not None
    r.h.on_session_end()
    assert r.h._held_turn is None


def test_turn_end_starts_the_filler_timer_and_speech_cancels_it(monkeypatch):
    monkeypatch.setenv("VOICE_TURN_FILLER", "on")
    monkeypatch.setenv("VOICE_TURN_FILLER_DELAY_MS", "5000")
    r = Rig(monkeypatch, _turn_script(), [(True, 0.9)], mode="off")
    r.feed(2)
    assert filler_clips.FILLERS._turn is not None
    r.h.iterator.script = [{"start": True}]
    r.feed(1)
    assert filler_clips.FILLERS._turn is None
