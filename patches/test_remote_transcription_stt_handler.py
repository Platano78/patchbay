"""Unit tests for remote_transcription_stt_handler.py: the `remote-transcription` STT backend
(any server implementing OpenAI's /v1/audio/transcriptions protocol).

Run from repo root (framework venv): python3 -m pytest patches/test_remote_transcription_stt_handler.py -v

Three layers:

* Hermetic handler tests -- the handler module is loaded under private stand-ins for the handful of
  `speech_to_speech` symbols it imports (see `_load_handler_module`); sys.modules is restored right
  after, so nothing leaks into the other test files. Every request goes to a real stdlib HTTP server
  on an ephemeral port, never to a real transcription service.
* Source-level guards (arguments <-> setup() seam, apply.sh mapping) -- stdlib only.
* Real-framework probes in a SUBPROCESS (same pattern as test_vocab_snap.TestRealHookPoint): the
  handler against the real BaseSTTHandler / VADAudio / Transcription classes, and the real
  s2s_pipeline wiring (`--stt remote-transcription`, the default `parakeet-tdt` path, and the
  vocab-snapping TranscriptionNotifier downstream). Skip when the framework is not importable.
"""

from __future__ import annotations

import dataclasses
import email
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Event
from unittest import mock

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HANDLER_PATH = _REPO_ROOT / "patches" / "remote_transcription_stt_handler.py"
_ARGS_PATH = _REPO_ROOT / "patches" / "remote_transcription_stt_arguments.py"

SR = 16000


# ── hermetic loading of the module under test ───────────────────────────────


class _Msg:
    """Stand-in for the pydantic pipeline messages: keyword fields, equality by value."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__

    def __repr__(self):
        return f"{type(self).__name__}({self.__dict__})"


class _PartialTranscription(_Msg):
    tag = "partial_transcription"


class _Transcription(_Msg):
    tag = "transcription"


class _BaseSTTHandler:
    def __init__(self, stop_event, queue_in, queue_out, setup_args=(), setup_kwargs=None):
        self.stop_event = stop_event
        self.queue_in = queue_in
        self.queue_out = queue_out
        self.setup(*setup_args, **(setup_kwargs or {}))

    def setup(self, *a, **kw):
        pass

    def _item_age_s(self, item):
        return 0.0


def _load_handler_module():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        return m

    stubs = {
        "speech_to_speech": mod("speech_to_speech"),
        "speech_to_speech.pipeline": mod("speech_to_speech.pipeline"),
        "speech_to_speech.pipeline.handler_types": mod(
            "speech_to_speech.pipeline.handler_types", STTIn=object, STTOut=object
        ),
        "speech_to_speech.pipeline.messages": mod(
            "speech_to_speech.pipeline.messages",
            PartialTranscription=_PartialTranscription,
            Transcription=_Transcription,
        ),
        "speech_to_speech.STT": mod("speech_to_speech.STT"),
        "speech_to_speech.STT.base_stt_handler": mod(
            "speech_to_speech.STT.base_stt_handler", BaseSTTHandler=_BaseSTTHandler
        ),
    }
    spec = importlib.util.spec_from_file_location("_rt_stt_handler_under_test", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


rth = _load_handler_module()


class _VADAudio:
    def __init__(self, audio, mode="final", turn_id="t1", turn_revision=1, created_at_s=123.5):
        self.audio = audio
        self.mode = mode
        self.turn_id = turn_id
        self.turn_revision = turn_revision
        self.created_at_s = created_at_s


def _speech(seconds: float, freq=440.0, amp=0.5) -> np.ndarray:
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ── a real local HTTP server ────────────────────────────────────────────────


class _Server:
    """`state` knobs: status, body (dict/str/bytes), delay_s, health_status, gate (Event: when set to an
    Event the POST handler blocks until it is set). Records every POST in `state["posts"]` and sets
    `state["arrived"]` when a POST has been received."""

    def __init__(self, **state):
        self.state = {
            "status": 200,
            "body": {"text": " hello world "},
            "delay_s": 0.0,
            "health_status": 200,
            "gate": None,
            "posts": [],
            "gets": [],
            "arrived": threading.Event(),
        }
        self.state.update(state)
        st = self.state

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                st["gets"].append(self.path)
                self.send_response(st["health_status"])
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                st["posts"].append({"path": self.path, "content_type": self.headers["Content-Type"], "body": body})
                st["arrived"].set()
                if st["gate"] is not None:
                    st["gate"].wait(10)
                if st["delay_s"]:
                    time.sleep(st["delay_s"])
                payload = st["body"]
                raw = payload if isinstance(payload, bytes) else (
                    payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                )
                try:
                    self.send_response(st["status"])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up (timeout test)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server():
    servers = []

    def make(**state):
        s = _Server(**state)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def _make(base_url, **overrides):
    kwargs = dict(base_url=base_url, enable_live_transcription=True)
    kwargs.update(overrides)
    return rth.RemoteTranscriptionSTTHandler(Event(), queue_in=Queue(), queue_out=Queue(), setup_kwargs=kwargs)


def _parts(post) -> dict:
    """Parse a recorded multipart POST into {field: (filename, bytes)}."""
    msg = email.message_from_bytes(b"Content-Type: " + post["content_type"].encode() + b"\r\n\r\n" + post["body"])
    out = {}
    for part in msg.get_payload():
        out[part.get_param("name", header="content-disposition")] = (
            part.get_filename(),
            part.get_payload(decode=True),
        )
    return out


def _wav_info(raw: bytes):
    with wave.open(io.BytesIO(raw), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes(), w.readframes(w.getnframes())


# ── final ───────────────────────────────────────────────────────────────────


def test_final_yields_transcription_with_ids_and_speech_stopped_at(server):
    srv = server(body={"text": "  turn off the lights  "})
    h = _make(srv.url)
    out = list(h.process(_VADAudio(_speech(1.0), mode="final", turn_id="turn-9", turn_revision=3, created_at_s=42.25)))
    assert out == [
        _Transcription(
            text="turn off the lights",
            language_code="en",
            turn_id="turn-9",
            turn_revision=3,
            speech_stopped_at_s=42.25,
        )
    ]
    assert srv.state["posts"][0]["path"] == "/v1/audio/transcriptions"


def test_mode_none_is_treated_as_final(server):
    srv = server()
    out = list(_make(srv.url).process(_VADAudio(_speech(0.5), mode=None)))
    assert [type(m) for m in out] == [_Transcription]


def test_final_uploads_all_audio_even_past_the_progressive_window(server):
    srv = server()
    list(_make(srv.url).process(_VADAudio(_speech(20.0), mode="final")))
    _, _, rate, frames, _ = _wav_info(_parts(srv.state["posts"][0])["file"][1])
    assert (rate, frames) == (SR, 20 * SR)


def test_language_code_server_then_configured_then_last_known_then_en(server):
    srv = server(body={"text": "hallo", "language": "de"})
    h = _make(srv.url)
    assert list(h.process(_VADAudio(_speech(0.3))))[0].language_code == "de"
    srv.state["body"] = {"text": "hello"}  # server stops reporting a language: last known wins
    assert list(h.process(_VADAudio(_speech(0.3))))[0].language_code == "de"

    configured = _make(srv.url, language="fr")  # no server language: configured wins
    assert list(configured.process(_VADAudio(_speech(0.3))))[0].language_code == "fr"
    srv.state["body"] = {"text": "hallo", "language": "de-DE"}  # server value still wins; region dropped
    assert list(configured.process(_VADAudio(_speech(0.3))))[0].language_code == "de"

    fresh = _make(srv.url)
    srv.state["body"] = {"text": "x", "language": "english"}  # a language NAME is not a code: ignored
    assert list(fresh.process(_VADAudio(_speech(0.3))))[0].language_code == "en"


# ── progressive ─────────────────────────────────────────────────────────────


def test_progressive_yields_partial_transcription(server):
    srv = server(body={"text": "turn off the"})
    h = _make(srv.url)
    out = list(h.process(_VADAudio(_speech(1.0), mode="progressive", turn_id="t7", turn_revision=2)))
    assert out == [_PartialTranscription(text="turn off the", turn_id="t7", turn_revision=2)]


def test_progressive_with_empty_text_yields_nothing(server):
    srv = server(body={"text": "   "})
    assert list(_make(srv.url).process(_VADAudio(_speech(1.0), mode="progressive"))) == []


def test_progressive_is_ignored_when_live_transcription_is_off(server):
    srv = server()
    h = _make(srv.url, enable_live_transcription=False)
    assert list(h.process(_VADAudio(_speech(1.0), mode="progressive"))) == []
    assert srv.state["posts"] == []
    assert [type(m) for m in h.process(_VADAudio(_speech(1.0), mode="final"))] == [_Transcription]


def test_progressive_skipped_while_a_final_is_processing(server):
    gate = Event()
    srv = server(gate=gate)
    h = _make(srv.url)
    finals = []
    t = threading.Thread(target=lambda: finals.extend(h.process(_VADAudio(_speech(1.0), mode="final"))))
    t.start()
    try:
        assert srv.state["arrived"].wait(5), "final request never reached the server"
        assert h.processing_final is True
        assert list(h.process(_VADAudio(_speech(1.0), mode="progressive"))) == []
        assert len(srv.state["posts"]) == 1  # the skipped progressive sent nothing
    finally:
        gate.set()
        t.join(10)
    assert [type(m) for m in finals] == [_Transcription]
    assert h.processing_final is False
    # and once the final is done, progressive works again
    assert [type(m) for m in h.process(_VADAudio(_speech(1.0), mode="progressive"))] == [_PartialTranscription]


def test_progressive_skipped_while_another_progressive_is_in_flight(server):
    gate = Event()
    srv = server(gate=gate)
    h = _make(srv.url)
    first = []
    t = threading.Thread(target=lambda: first.extend(h.process(_VADAudio(_speech(1.0), mode="progressive"))))
    t.start()
    try:
        assert srv.state["arrived"].wait(5)
        assert list(h.process(_VADAudio(_speech(2.0), mode="progressive"))) == []
        assert len(srv.state["posts"]) == 1
    finally:
        gate.set()
        t.join(10)
    assert [type(m) for m in first] == [_PartialTranscription]
    assert [type(m) for m in h.process(_VADAudio(_speech(1.0), mode="progressive"))] == [_PartialTranscription]


def test_a_final_is_not_blocked_by_a_progressive_in_flight(server):
    gate = Event()
    srv = server(gate=gate)
    h = _make(srv.url)
    t = threading.Thread(target=lambda: list(h.process(_VADAudio(_speech(1.0), mode="progressive"))))
    t.start()
    try:
        assert srv.state["arrived"].wait(5)
        gate.set()  # let both complete; the point is that the final does not wait on the progressive's lock
        assert [type(m) for m in h.process(_VADAudio(_speech(1.0), mode="final"))] == [_Transcription]
    finally:
        gate.set()
        t.join(10)


@pytest.mark.parametrize(
    "seconds, window_s, expected_frames",
    [(20.0, 15.0, 15 * SR), (5.0, 15.0, 5 * SR), (4.0, 2.0, 2 * SR), (15.0, 15.0, 15 * SR)],
)
def test_progressive_window_truncation(server, seconds, window_s, expected_frames):
    srv = server()
    h = _make(srv.url, progressive_window_s=window_s)
    list(h.process(_VADAudio(_speech(seconds), mode="progressive")))
    _, _, _, frames, _ = _wav_info(_parts(srv.state["posts"][0])["file"][1])
    assert frames == expected_frames


def test_progressive_window_keeps_the_tail_not_the_head(server):
    srv = server()
    h = _make(srv.url, progressive_window_s=1.0)
    audio = np.concatenate([np.full(SR, 0.9, dtype=np.float32), np.full(SR, -0.9, dtype=np.float32)])
    list(h.process(_VADAudio(audio, mode="progressive")))
    pcm = np.frombuffer(_wav_info(_parts(srv.state["posts"][0])["file"][1])[4], dtype="<i2")
    assert pcm.size == SR and (pcm < 0).all()  # only the last (negative) second


# ── wire format ─────────────────────────────────────────────────────────────


def test_multipart_body_has_a_valid_16k_mono_pcm16_wav_and_the_model_field(server):
    srv = server()
    h = _make(srv.url, model="parakeet")
    audio = _speech(0.5, amp=0.5)
    list(h.process(_VADAudio(audio, mode="final")))
    post = srv.state["posts"][0]
    assert post["content_type"].startswith("multipart/form-data; boundary=")
    parts = _parts(post)
    assert parts["model"][1] == b"parakeet"
    assert parts["response_format"][1] == b"json"
    assert "language" not in parts  # unset: the server decides
    filename, raw = parts["file"]
    assert filename and filename.endswith(".wav")
    channels, width, rate, frames, data = _wav_info(raw)
    assert (channels, width, rate, frames) == (1, 2, SR, audio.size)
    pcm = np.frombuffer(data, dtype="<i2")
    assert np.max(np.abs(pcm.astype(np.float32) / 32767.0 - audio)) < 1e-3  # a real round-trip, not silence


def test_wav_encoding_clips_out_of_range_samples(server):
    raw = rth.encode_wav(np.array([2.0, -2.0, 0.0], dtype=np.float32))
    pcm = np.frombuffer(_wav_info(raw)[4], dtype="<i2")
    assert pcm.tolist() == [32767, -32767, 0]


def test_default_model_is_whisper_1_and_language_is_sent_when_configured(server):
    srv = server()
    h = _make(srv.url, language="en")
    list(h.process(_VADAudio(_speech(0.3))))
    parts = _parts(srv.state["posts"][0])
    assert parts["model"][1] == b"whisper-1"
    assert parts["language"][1] == b"en"


def test_one_client_is_reused_across_requests(server):
    srv = server()
    h = _make(srv.url)
    client = h._client
    list(h.process(_VADAudio(_speech(0.3), mode="progressive")))
    list(h.process(_VADAudio(_speech(0.3), mode="final")))
    assert h._client is client and not client.is_closed
    h.cleanup()
    assert client.is_closed


# ── failures: no exception escapes, no local fallback ───────────────────────


def test_final_http_500_yields_empty_transcription_and_logs_one_error(server, caplog):
    srv = server(status=500, body={"error": "boom"})
    h = _make(srv.url)
    with caplog.at_level(logging.DEBUG, logger=rth.logger.name):
        out = list(h.process(_VADAudio(_speech(0.5), turn_id="t5", turn_revision=2, created_at_s=7.0)))
    assert out == [
        _Transcription(text="", language_code="en", turn_id="t5", turn_revision=2, speech_stopped_at_s=7.0)
    ]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "500" in errors[0].getMessage()


def test_final_timeout_yields_empty_transcription(server, caplog):
    srv = server(delay_s=1.0)
    h = _make(srv.url, final_timeout_s=0.2)
    t0 = time.monotonic()
    with caplog.at_level(logging.ERROR, logger=rth.logger.name):
        out = list(h.process(_VADAudio(_speech(0.5))))
    assert time.monotonic() - t0 < 0.9
    assert [(type(m), m.text) for m in out] == [(_Transcription, "")]
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    assert h.processing_final is False


def test_final_unreachable_server_yields_empty_transcription():
    h = _make(_dead_url())
    out = list(h.process(_VADAudio(_speech(0.5))))
    assert [(type(m), m.text) for m in out] == [(_Transcription, "")]


@pytest.mark.parametrize("body", ["not json", {"no_text": 1}, {"text": 5}, ["list"]])
def test_final_malformed_response_yields_empty_transcription(server, body):
    srv = server(body=body)
    out = list(_make(srv.url).process(_VADAudio(_speech(0.5))))
    assert [(type(m), m.text) for m in out] == [(_Transcription, "")]


def test_progressive_failures_yield_nothing_and_log_at_debug_only(server, caplog):
    srv = server(status=500)
    h = _make(srv.url)
    with caplog.at_level(logging.DEBUG, logger=rth.logger.name):
        assert list(h.process(_VADAudio(_speech(0.5), mode="progressive"))) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r for r in caplog.records if r.levelno == logging.DEBUG and "Progressive" in r.getMessage()]

    srv.state.update(status=200, delay_s=1.0)
    h2 = _make(srv.url, progressive_timeout_s=0.15)
    assert list(h2.process(_VADAudio(_speech(0.5), mode="progressive"))) == []
    assert h2._progressive_lock.acquire(blocking=False)  # a failed progressive releases its slot
    h2._progressive_lock.release()


def test_empty_audio_final_does_not_call_the_server(server):
    srv = server()
    out = list(_make(srv.url).process(_VADAudio(np.zeros(0, dtype=np.float32))))
    assert [(type(m), m.text) for m in out] == [(_Transcription, "")]
    assert srv.state["posts"] == []


# ── startup: health probe, opt-in ───────────────────────────────────────────


def test_healthy_server_is_probed_once_at_setup_without_a_warning(server, caplog):
    srv = server()
    with caplog.at_level(logging.DEBUG, logger=rth.logger.name):
        _make(srv.url + "/")  # trailing slash is normalised away
    assert srv.state["gets"] == ["/health"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("how", ["refused", "503"])
def test_failed_health_check_warns_and_the_handler_still_works(server, caplog, how):
    if how == "refused":
        url = _dead_url()
    else:
        srv = server(health_status=503)
        url = srv.url
    with caplog.at_level(logging.DEBUG, logger=rth.logger.name):
        h = _make(url)  # must not raise
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "health" in warnings[0].getMessage()
    if how == "503":  # the server is up after all: requests work
        assert [(type(m), m.text) for m in h.process(_VADAudio(_speech(0.3)))] == [(_Transcription, "hello world")]


@pytest.mark.parametrize("base_url", [None, "", "   "])
def test_base_url_unset_refuses_the_backend(base_url):
    with pytest.raises(ValueError, match="remote_transcription_base_url"):
        _make(base_url)


# ── the arguments class <-> setup() seam ────────────────────────────────────


def _args_class():
    spec = importlib.util.spec_from_file_location("_rt_stt_args_under_test", _ARGS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RemoteTranscriptionSTTHandlerArguments


def test_arguments_defaults_are_the_documented_ones():
    a = _args_class()()
    assert a.remote_transcription_base_url is None
    assert a.remote_transcription_model == "whisper-1"
    assert a.remote_transcription_language is None
    assert a.remote_transcription_progressive_timeout_s == 1.0
    assert a.remote_transcription_final_timeout_s == 15.0
    assert a.remote_transcription_progressive_window_s == 15.0


def test_every_argument_reaches_setup_and_nothing_else_is_required():
    """rename_args() strips the `remote_transcription` prefix; the result (plus the two live-transcription
    flags and gen_kwargs the pipeline adds) must be exactly what setup() accepts -- a mismatch is a
    TypeError at startup, not a test failure elsewhere."""
    fields = {f.name for f in dataclasses.fields(_args_class())}
    assert all(f.startswith("remote_transcription_") for f in fields)
    produced = {f[len("remote_transcription_"):] for f in fields}
    accepted = set(inspect.signature(rth.RemoteTranscriptionSTTHandler.setup).parameters) - {"self"}
    injected = {"enable_live_transcription", "live_transcription_update_interval", "gen_kwargs"}
    assert produced <= accepted
    assert accepted - produced == injected


def test_no_local_asr_model_is_imported_by_the_handler():
    src = _HANDLER_PATH.read_text()
    for forbidden in ("parakeet", "torch", "nano_parakeet", "mlx", "whisper_stt", "faster_whisper"):
        assert not re.search(rf"^\s*(from|import)\s+\S*{forbidden}", src, re.M | re.I), forbidden


def test_apply_sh_ships_both_new_files():
    text = (_REPO_ROOT / "patches" / "apply.sh").read_text()
    assert '"remote_transcription_stt_handler.py|STT/remote_transcription_stt_handler.py"' in text
    assert '"remote_transcription_stt_arguments.py|arguments_classes/remote_transcription_stt_arguments.py"' in text
    assert (_REPO_ROOT / "patches" / "remote_transcription_stt_handler.py").is_file()
    assert (_REPO_ROOT / "patches" / "remote_transcription_stt_arguments.py").is_file()


# ── real framework, in a subprocess ─────────────────────────────────────────


def _framework_importable():
    p = subprocess.run(
        [sys.executable, "-c", "import speech_to_speech, torch, transformers, nltk"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return p.returncode == 0


_PROBE = textwrap.dedent(
    """
    import json, sys, threading, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from queue import Queue
    from threading import Event
    from types import SimpleNamespace
    from unittest import mock

    # A stale framework install does not have the new pack modules yet: serve them from patches/.
    import conftest; conftest.alias_unapplied_pack_modules()
    # Test the pack's files, not whatever the install holds. A stale install lacks the new modules
    # (aliased above) or has an old module_arguments.py without the new --stt choice; an applied
    # install has its own COPIES of the new modules, whose classes are not the ones imported from
    # patches/ below, so isinstance() against them would fail. Serve all of them from patches/.
    import patches.module_arguments as _ma
    import patches.remote_transcription_stt_arguments as _ra
    import patches.remote_transcription_stt_handler as _rh
    sys.modules["speech_to_speech.arguments_classes.module_arguments"] = _ma
    sys.modules["speech_to_speech.arguments_classes.remote_transcription_stt_arguments"] = _ra
    sys.modules["speech_to_speech.STT.remote_transcription_stt_handler"] = _rh
    import numpy as np

    posts = []
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
            posts.append(self.path)
            raw = json.dumps({"text": "what is Vox EPM doing", "language": "en"}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            self.wfile.write(raw)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]

    from speech_to_speech.pipeline.messages import VADAudio, Transcription, PartialTranscription
    from speech_to_speech.STT.base_stt_handler import BaseSTTHandler
    from patches import s2s_pipeline as sp
    from patches.remote_transcription_stt_handler import RemoteTranscriptionSTTHandler
    res = {}

    # 1. the handler against the REAL message classes and base class
    h = RemoteTranscriptionSTTHandler(Event(), queue_in=Queue(), queue_out=Queue(),
                                      setup_kwargs={"base_url": url, "model": "parakeet", "enable_live_transcription": True})
    res["is_base_stt"] = isinstance(h, BaseSTTHandler)
    audio = (0.3 * np.sin(np.arange(16000) / 20)).astype(np.float32)
    part = list(h.process(VADAudio(audio=audio, mode="progressive", turn_id="t1", turn_revision=2)))
    fin = list(h.process(VADAudio(audio=audio, mode="final", turn_id="t1", turn_revision=2, created_at_s=5.5)))
    res["partial"] = [[type(m) is PartialTranscription, m.text, m.turn_id, m.turn_revision] for m in part]
    res["final"] = [[type(m) is Transcription, m.text, m.language_code, m.turn_id, m.turn_revision, m.speech_stopped_at_s] for m in fin]

    # 2. wiring: get_stt_handler
    def mk(stt, remote_kwargs):
        mk_kwargs = SimpleNamespace(stt=stt, enable_live_transcription=True, live_transcription_update_interval=0.5)
        return sp.get_stt_handler(mk_kwargs, Event(), Queue(), Queue(), None,
                                  SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                                  SimpleNamespace(), remote_kwargs)
    remote_kw = SimpleNamespace(base_url=url, model="parakeet", language=None, progressive_timeout_s=1.0,
                                final_timeout_s=15.0, progressive_window_s=15.0, gen_kwargs={})
    built = mk("remote-transcription", remote_kw)
    res["remote_built"] = type(built).__name__
    try:
        mk("remote-transcription", SimpleNamespace(base_url=None))
        res["unset_refused"] = False
    except ValueError as e:
        res["unset_refused"] = "remote_transcription_base_url" in str(e)

    class SpyParakeet:
        instances = []
        def __init__(self, stop_event, queue_in, queue_out, setup_kwargs):
            self.setup_kwargs = setup_kwargs; SpyParakeet.instances.append(self)
    fake = SimpleNamespace(ParakeetTDTSTTHandler=SpyParakeet)
    with mock.patch.dict(sys.modules, {"speech_to_speech.STT.parakeet_tdt_handler": fake}):
        default = mk("parakeet-tdt", None)  # the shipped default path, no remote kwargs at all
    res["default_is_parakeet_spy"] = type(default) is SpyParakeet
    res["default_got_live_flags"] = sorted(default.setup_kwargs)

    # 3. argument parsing + prepare_all_args (the real CLI path)
    sys.argv = ["s2s_pipeline.py", "--stt", "remote-transcription",
                "--remote_transcription_base_url", url, "--remote_transcription_model", "parakeet",
                "--remote_transcription_final_timeout_s", "9"]
    parsed = sp.parse_arguments()
    sp.prepare_all_args(parsed.module_kwargs, parsed.whisper_stt_handler_kwargs, parsed.paraformer_stt_handler_kwargs,
        parsed.faster_whisper_stt_handler_kwargs, parsed.mlx_audio_whisper_stt_handler_kwargs,
        parsed.parakeet_tdt_stt_handler_kwargs, parsed.language_model_handler_kwargs,
        parsed.responses_api_language_model_handler_kwargs, parsed.chat_tts_handler_kwargs,
        parsed.facebook_mms_tts_handler_kwargs, parsed.pocket_tts_handler_kwargs, parsed.kokoro_tts_handler_kwargs,
        parsed.qwen3_tts_handler_kwargs, parsed.remote_speech_tts_handler_kwargs,
        parsed.remote_transcription_stt_handler_kwargs)
    res["cli_stt"] = parsed.module_kwargs.stt
    res["cli_renamed"] = {k: v for k, v in vars(parsed.remote_transcription_stt_handler_kwargs).items() if k != "base_url"}
    sys.argv = ["s2s_pipeline.py"]
    default_parsed = sp.parse_arguments()
    res["default_stt"] = default_parsed.module_kwargs.stt
    res["default_remote_base_url"] = default_parsed.remote_transcription_stt_handler_kwargs.remote_transcription_base_url

    # 4. the real chain: _build_pipeline_handlers -> remote STT -> VocabSnappingNotifier -> brain + cockpit
    from speech_to_speech.STT.transcription_notifier import TranscriptionNotifier
    from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig
    from speech_to_speech.LLM.chat import Chat
    from openai.types.realtime import RealtimeSessionCreateRequest
    class Dummy:
        pipeline_index = 0
        def __init__(self, *a, **k): pass
    text_output_queue = Queue()
    runtime_config = RuntimeConfig(chat=Chat(10), session=RealtimeSessionCreateRequest(type="realtime", instructions="x"))
    kwargs = {k: SimpleNamespace() for k in (
        "vad_handler_kwargs", "whisper_stt_handler_kwargs", "faster_whisper_stt_handler_kwargs",
        "paraformer_stt_handler_kwargs", "mlx_audio_whisper_stt_handler_kwargs",
        "parakeet_tdt_stt_handler_kwargs", "language_model_handler_kwargs",
        "responses_api_language_model_handler_kwargs", "chat_tts_handler_kwargs",
        "facebook_mms_tts_handler_kwargs", "pocket_tts_handler_kwargs", "kokoro_tts_handler_kwargs",
        "qwen3_tts_handler_kwargs", "remote_speech_tts_handler_kwargs")}
    import tempfile, os
    vf = os.path.join(tempfile.mkdtemp(), "vocab.txt"); open(vf, "w").write("VoxCPM | vox epm\\n")
    os.environ["VOICE_VOCAB_FILE"] = vf
    module_kwargs = SimpleNamespace(stt="remote-transcription", enable_live_transcription=True,
                                    live_transcription_update_interval=0.5)
    with mock.patch.object(sp, "VADHandler", Dummy), \\
         mock.patch.object(sp, "get_llm_handler", lambda *a, **k: Dummy()), \\
         mock.patch.object(sp, "get_tts_handler", lambda *a, **k: Dummy()), \\
         mock.patch("speech_to_speech.LLM.lm_output_processor.LMOutputProcessor", Dummy):
        handlers = sp._build_pipeline_handlers(
            stop_event=Event(), should_listen=Event(),
            recv_audio_chunks_queue=Queue(), spoken_prompt_queue=Queue(), stt_output_queue=Queue(),
            text_prompt_queue=Queue(), lm_response_queue=Queue(), lm_processed_queue=Queue(),
            send_audio_chunks_queue=Queue(), text_output_queue=text_output_queue,
            transcription_notifier_setup={"text_output_queue": text_output_queue,
                                          "should_listen": Event(), "runtime_config": runtime_config},
            module_kwargs=module_kwargs, remote_transcription_stt_handler_kwargs=remote_kw, **kwargs)
    stt = next(x for x in handlers if isinstance(x, RemoteTranscriptionSTTHandler))
    notifier = next(x for x in handlers if isinstance(x, TranscriptionNotifier))
    res["chain"] = [type(x).__name__ for x in handlers]
    res["notifier_is_vocab_snapping"] = type(notifier) is sp.VocabSnappingNotifier
    spoken = list(stt.process(VADAudio(audio=audio, mode="final", turn_id="t9", turn_revision=1)))
    reqs = [r for m in spoken for r in notifier.process(m)]
    res["brain_heard"] = runtime_config.chat.buffer[-1].content[0].text
    events = []
    while not text_output_queue.empty(): events.append(text_output_queue.get())
    res["cockpit_saw"] = [e.transcript for e in events if hasattr(e, "transcript")]
    res["requests_to_llm"] = len(reqs)
    print("RESULT " + json.dumps(res))
    """
)


@pytest.fixture(scope="module")
def res():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(_REPO_ROOT),
        env=dict(os.environ, VOICE_VOCAB_FILE=""),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2500:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("RESULT "))
    return json.loads(line[len("RESULT "):])


@pytest.mark.skipif(
    not _framework_importable(), reason="the real speech_to_speech framework is not installed in this venv"
)
class TestRealFramework:
    def test_handler_speaks_the_real_message_contract(self, res):
        assert res["is_base_stt"] is True
        assert res["partial"] == [[True, "what is Vox EPM doing", "t1", 2]]
        assert res["final"] == [[True, "what is Vox EPM doing", "en", "t1", 2, 5.5]]

    def test_stt_remote_transcription_builds_the_remote_handler(self, res):
        assert res["remote_built"] == "RemoteTranscriptionSTTHandler"
        assert res["unset_refused"] is True

    def test_default_parakeet_path_is_unchanged(self, res):
        assert res["default_is_parakeet_spy"] is True
        assert "enable_live_transcription" in res["default_got_live_flags"]
        assert res["default_stt"] == "parakeet-tdt"
        assert res["default_remote_base_url"] is None

    def test_cli_flags_reach_the_handler_kwargs_prefix_stripped(self, res):
        assert res["cli_stt"] == "remote-transcription"
        assert res["cli_renamed"] == {
            "model": "parakeet",
            "language": None,
            "progressive_timeout_s": 1.0,
            "final_timeout_s": 9.0,
            "progressive_window_s": 15.0,
            "gen_kwargs": {},
        }

    def test_vocab_snapping_notifier_path_is_unchanged(self, res):
        assert res["chain"][1] == "RemoteTranscriptionSTTHandler"
        assert res["notifier_is_vocab_snapping"] is True
        assert res["brain_heard"] == "what is VoxCPM doing"
        assert res["cockpit_saw"] == ["what is VoxCPM doing"]
        assert res["requests_to_llm"] == 1
