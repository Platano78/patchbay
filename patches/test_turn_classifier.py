"""turn_classifier.py: the Smart Turn client against a fake /v1/tasks/run server.

Run from repo root: python3 -m pytest patches/test_turn_classifier.py -v
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from patches import turn_classifier as tc


class _Fake:
    def __init__(self):
        self.requests: list[dict] = []
        self.reply = {"custom_schema_output": {"schema": "smart_turn.v1", "data": {"complete": True, "probability": 0.57}}}
        self.status = 200
        self.delay = 0.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body})
                time.sleep(outer.delay)
                data = json.dumps(outer.reply).encode()
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


@pytest.fixture
def fake():
    f = _Fake()
    yield f
    f.srv.shutdown()


def _audio(seconds, rate=16000):
    return (0.1 * np.sin(np.arange(int(seconds * rate)) / 20.0)).astype(np.float32)


def _st(fake, **kw):
    return tc.SmartTurn(fake.url, "act", **kw)


def test_complete_verdict_and_wire_shape(fake):
    v = _st(fake, threshold=0.6).classify(_audio(1.0), 16000)
    assert v.complete is True and v.probability == pytest.approx(0.57) and v.latency_ms >= 0
    req = fake.requests[0]
    assert req["path"] == "/v1/tasks/run"
    assert req["body"]["model"] == "smart-turn"
    assert req["body"]["request"]["options"] == {"threshold": "0.6"}
    wav = wave.open(io.BytesIO(base64.b64decode(req["body"]["request"]["audio_base64"])))
    assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)


def test_incomplete_verdict(fake):
    fake.reply["custom_schema_output"]["data"] = {"complete": False, "probability": 0.007}
    v = _st(fake).classify(_audio(1.0), 16000)
    assert v.complete is False and v.probability == pytest.approx(0.007)


def test_sends_only_the_last_eight_seconds(fake):
    _st(fake).classify(_audio(11.0), 16000)
    wav = wave.open(io.BytesIO(base64.b64decode(fake.requests[0]["body"]["request"]["audio_base64"])))
    assert wav.getnframes() == 8 * 16000


def test_resamples_to_16k(fake):
    _st(fake).classify(_audio(2.0, 48000), 48000)
    wav = wave.open(io.BytesIO(base64.b64decode(fake.requests[0]["body"]["request"]["audio_base64"])))
    assert wav.getframerate() == 16000 and wav.getnframes() == pytest.approx(2 * 16000, abs=2)


def test_timeout_is_unknown_and_bounded(fake):
    fake.delay = 1.0
    t0 = time.monotonic()
    v = _st(fake, timeout_s=0.25).classify(_audio(1.0), 16000)
    assert v.complete is None
    assert time.monotonic() - t0 < 0.6


@pytest.mark.parametrize(
    "reply",
    [{}, {"custom_schema_output": {}}, {"custom_schema_output": {"data": {"complete": "yes", "probability": 1}}}],
)
def test_malformed_answer_is_unknown(fake, reply):
    fake.reply = reply
    assert _st(fake).classify(_audio(1.0), 16000).complete is None


def test_http_error_is_unknown(fake):
    fake.status = 500
    assert _st(fake).classify(_audio(1.0), 16000).complete is None


def test_connection_refused_is_unknown():
    assert tc.SmartTurn("http://127.0.0.1:1", "act").classify(_audio(1.0), 16000).complete is None


def test_shadow_does_not_block_and_logs_one_line(fake, caplog):
    fake.delay = 0.15
    caplog.set_level(logging.INFO, logger=tc.__name__)
    st = tc.SmartTurn(fake.url, "shadow", timeout_s=1.0)
    t0 = time.monotonic()
    st.shadow(_audio(1.5), 16000)
    assert time.monotonic() - t0 < 0.05
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any("SMART_TURN_SHADOW" in r.message for r in caplog.records):
        time.sleep(0.01)
    lines = [r.message for r in caplog.records if "SMART_TURN_SHADOW" in r.message]
    assert len(lines) == 1
    assert lines[0].startswith("SMART_TURN_SHADOW complete=true p=0.57 latency_ms=")
    assert lines[0].endswith("turn_s=1.5")


@pytest.mark.parametrize("raw,expected", [(None, "off"), ("", "off"), ("off", "off"), ("shadow", "shadow"), ("ACT", "act"), ("bogus", "off")])
def test_mode_from_env(monkeypatch, raw, expected):
    monkeypatch.delenv("VOICE_SMART_TURN", raising=False)
    if raw is not None:
        monkeypatch.setenv("VOICE_SMART_TURN", raw)
    assert tc.mode_from_env() == expected


def test_env_knobs(monkeypatch):
    monkeypatch.delenv("VOICE_SMART_TURN_THRESHOLD", raising=False)
    monkeypatch.delenv("VOICE_SMART_TURN_HOLD_MS", raising=False)
    assert (tc.threshold_from_env(), tc.hold_ms_from_env()) == (0.5, 1200)
    monkeypatch.setenv("VOICE_SMART_TURN_THRESHOLD", "0.7")
    monkeypatch.setenv("VOICE_SMART_TURN_HOLD_MS", "800")
    assert (tc.threshold_from_env(), tc.hold_ms_from_env()) == (0.7, 800)
    monkeypatch.setenv("VOICE_SMART_TURN_THRESHOLD", "junk")
    assert tc.threshold_from_env() == 0.5


def test_build_off_and_no_server(monkeypatch):
    monkeypatch.setenv("VOICE_SMART_TURN", "off")
    assert tc.build("http://x") is None
    monkeypatch.setenv("VOICE_SMART_TURN", "act")
    assert tc.build(None) is None
    assert tc.build("http://x").mode == "act"


def test_ptt_marker():
    tc.reset_ptt()
    assert not tc.ptt_owns_turn(100.0)
    tc.note_ptt("down", 100.0)
    assert tc.ptt_owns_turn(150.0)  # held: it is a PTT turn
    tc.note_ptt("up", 151.0)
    assert tc.ptt_owns_turn(153.0)  # the release's flush ends the turn here
    assert not tc.ptt_owns_turn(151.0 + tc.PTT_GRACE_S + 0.1)
    tc.reset_ptt()
