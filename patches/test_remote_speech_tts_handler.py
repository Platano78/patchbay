"""Unit tests for remote_speech_tts_handler.py: the `remote-speech` TTS backend
(any OpenAI-compatible /v1/audio/speech server) with automatic, never-silent
fallback to pocket.

Run from repo root: python3 -m pytest patches/test_remote_speech_tts_handler.py -v

The real `speech_to_speech` package is not installed in this repo, so the
handful of symbols the module under test imports from it are stubbed into
`sys.modules` before the import below -- same hermetic pattern as
`test_stream_watchdog.py` / `test_websocket_streamer.py`. `PocketTTSHandler`
is stubbed with a spy (`_FakePocketTTSHandler`) so fallback can be observed
without depending on the real, heavy pocket-tts model. httpx and numpy are
real, installed third-party packages. Tests that exercise real network
timing (happy path, connection-refused fast-fail, trickle/duration-cap) spin
up a local stdlib HTTP server on an ephemeral port -- never the real remote
service, which another agent is concurrently starting/stopping.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Event

import numpy as np
import pytest


def _install_stubs():
    def mod(name, **attrs):
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    from typing import Generic, TypeVar

    InT = TypeVar("InT")
    OutT = TypeVar("OutT")

    class _BaseHandler(Generic[InT, OutT]):
        def __init__(self, stop_event, queue_in, queue_out, setup_args=(), setup_kwargs=None):
            self.stop_event = stop_event
            self.queue_in = queue_in
            self.queue_out = queue_out
            self.pipeline_index = None
            self.setup(*setup_args, **(setup_kwargs or {}))
            self._times = []

        def setup(self, *a, **kw):
            pass

        def process(self, item):
            raise NotImplementedError

        @property
        def min_time_to_debug(self):
            return 0.001

    class _EndOfResponse:
        def __init__(self, turn_id=None, turn_revision=None):
            self.turn_id = turn_id
            self.turn_revision = turn_revision

    mod("speech_to_speech")
    mod("speech_to_speech.baseHandler", BaseHandler=_BaseHandler)
    mod("speech_to_speech.pipeline")
    mod("speech_to_speech.pipeline.cancel_scope", CancelScope=type("_CancelScope", (), {}))
    mod("speech_to_speech.pipeline.handler_types", TTSIn=object, TTSOut=object)
    mod(
        "speech_to_speech.pipeline.messages",
        AUDIO_RESPONSE_DONE=object(),
        EndOfResponse=_EndOfResponse,
    )
    mod("speech_to_speech.pipeline.speculative_turns", SpeculativeTurnTracker=object)
    mod("speech_to_speech.TTS")

    class _FakePocketTTSHandler(_BaseHandler):
        """Spy standing in for the real (heavy, ML-backed) pocket handler.
        `construct_count` tracks how many times it was actually built (i.e.
        `setup()` ran, which is where the real one loads model weights) --
        the thing the lazy-preload tests care about."""

        calls: list = []
        construct_count = 0

        def setup(self, should_listen, cancel_scope=None, speculative_turns=None, sample_rate=16000, **kw):
            _FakePocketTTSHandler.construct_count += 1
            self.should_listen = should_listen
            self.cancel_scope = cancel_scope
            self.speculative_turns = speculative_turns
            self.sample_rate = sample_rate
            self.extra_kwargs = kw

        def process(self, tts_input):
            _FakePocketTTSHandler.calls.append(tts_input)
            yield np.array([111, 222, 333], dtype=np.int16)

    mod("speech_to_speech.TTS.pocket_tts_handler", PocketTTSHandler=_FakePocketTTSHandler)
    # The real pocket_tts package is not installed here either; the boot-time
    # validation only checks it is importable (never loads a model from it), so
    # a bare stub module satisfies that check without pulling in the real thing.
    mod("pocket_tts")
    return _FakePocketTTSHandler


FakePocketTTSHandler = _install_stubs()


@pytest.fixture(autouse=True)
def _reassert_own_pocket_stub():
    """`test_brain_control.py` stubs the SAME `speech_to_speech.TTS.pocket_tts_handler`
    dotted path with its own fake `PocketTTSHandler` at ITS import (collection) time.
    Since `RemoteSpeechTTSHandler.setup()` resolves that import lazily -- at handler
    construction, not at this file's import time -- whichever test file's stub
    happened to run last during collection silently wins for the rest of the pytest
    session, a real cross-file collision (reproduced: `pytest test_remote_speech_tts_handler.py
    test_brain_control.py` fails 10 of this file's tests; the full suite happens to pass
    only because alphabetical collection order currently favors this file -- not something
    to depend on). Re-assert our own stub before every test in this file so its
    assertions are correct regardless of collection order or which other files run."""
    sys.modules["speech_to_speech.TTS.pocket_tts_handler"].PocketTTSHandler = FakePocketTTSHandler

import patches.remote_speech_tts_handler as rsh  # noqa: E402


class _TTSInput:
    def __init__(self, text: str):
        self.text = text


def _make_handler(base_url, pocket_kwargs=None, **overrides):
    FakePocketTTSHandler.calls = []
    FakePocketTTSHandler.construct_count = 0
    kwargs = dict(
        # Unprefixed -- matches RemoteSpeechTTSHandler.setup()'s real parameter
        # names (what the pipeline hands it AFTER rename_args strips the
        # remote_speech_ prefix), not the ArgumentsClass field names.
        base_url=base_url,
        connect_timeout_s=0.3,
        read_timeout_s=2.0,
        max_duration_s=1.0,
        failure_threshold=3,
        cooldown_s=30.0,
        sample_rate=16000,
        blocksize=512,
        pocket_kwargs={"voice": "jean"} if pocket_kwargs is None else pocket_kwargs,
    )
    kwargs.update(overrides)
    return rsh.RemoteSpeechTTSHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_args=(Event(),),
        setup_kwargs=kwargs,
    )


class _StubServer:
    """Minimal local HTTP server for one test. `handler_cls` is a
    BaseHTTPRequestHandler subclass; `state` is shared mutable test state
    (request counts, response bodies) the handler class closes over."""

    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _closed_port_url() -> str:
    """A base_url nothing listens on, so connections are refused instantly."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ── 24kHz -> 16kHz resample arithmetic ──────────────────────────────────────


def test_resample_produces_expected_sample_count():
    # blocksize=512, ratio=24000/16000=1.5 -> target_chunk_size=768 raw 24kHz
    # samples resamples (up=2, down=3) to exactly 768*2/3=512 samples: an exact
    # match to blocksize with no pad/trim needed in the steady state.
    handler = _make_handler(base_url=None, blocksize=512)
    assert handler._target_chunk_size == 768
    raw = np.zeros(768, dtype=np.float32)

    resampled = handler._resample(raw)

    assert len(resampled) == 512


def test_resample_is_noop_when_sample_rate_already_matches_remote():
    handler = _make_handler(base_url=None, sample_rate=24000)
    raw = np.zeros(512, dtype=np.float32)

    resampled = handler._resample(raw)

    assert len(resampled) == 512
    assert np.array_equal(resampled, raw)


# ── base_url unset -> backend unavailable ───────────────────────────────────


def test_base_url_unset_falls_back_to_pocket_without_any_network_call():
    handler = _make_handler(base_url=None)
    assert handler.base_url is None

    chunks = list(handler.process(_TTSInput("hello")))

    assert len(FakePocketTTSHandler.calls) == 1
    assert len(chunks) == 1
    assert np.array_equal(chunks[0], np.array([111, 222, 333], dtype=np.int16))


# ── happy path: incremental streaming at the correct sample rate ───────────


def _make_happy_handler_cls(pcm_bytes: bytes, writes: int):
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            # Drain the request body before responding. Skipping this leaves
            # unread bytes in the server's receive buffer when the HTTP/1.0
            # connection closes after this handler returns -- the OS then sends
            # a TCP RST instead of a clean FIN, which the client occasionally
            # observes as httpx.ReadError: [Errno 104] Connection reset by peer
            # (root-caused: reproduced a ~5% flake rate without this, 0/100 with it).
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            step = max(1, len(pcm_bytes) // writes)
            for i in range(0, len(pcm_bytes), step):
                self.wfile.write(pcm_bytes[i : i + step])
                self.wfile.flush()

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *a):
            pass

    return _Handler


def test_happy_path_streams_incrementally_at_correct_rate():
    one_second_24k = np.linspace(-16000, 16000, 24000, dtype=np.int16)
    pcm_bytes = one_second_24k.astype("<i2").tobytes()
    server = _StubServer(_make_happy_handler_cls(pcm_bytes, writes=8))
    try:
        handler = _make_handler(base_url=server.base_url)

        chunks = list(handler.process(_TTSInput("hello world")))

        assert len(FakePocketTTSHandler.calls) == 0  # remote succeeded, no fallback
        assert len(chunks) > 1  # incremental, not one giant buffered blob
        for c in chunks[:-1]:
            assert len(c) == handler.blocksize
        total_samples = sum(len(c) for c in chunks)
        expected = round(24000 * (16000 / 24000))  # 1s of 24kHz -> 1s of 16kHz
        # allow one block of pad/leftover slack at the tail
        assert abs(total_samples - expected) <= handler.blocksize
    finally:
        server.close()


# ── connection refused: fast-fail, not the full read timeout ───────────────


def test_connection_refused_falls_back_fast_not_full_timeout():
    handler = _make_handler(
        base_url=_closed_port_url(),
        connect_timeout_s=0.3,
        read_timeout_s=5.0,
    )

    start = time.monotonic()
    chunks = list(handler.process(_TTSInput("hello")))
    elapsed = time.monotonic() - start

    assert len(FakePocketTTSHandler.calls) == 1  # fell back to pocket
    assert len(chunks) == 1
    # Must not have sat anywhere near the (much larger) read timeout.
    assert elapsed < 2.0


# ── trickle/slow server: total-duration cap trips, not the read timeout ────


def _make_trickle_handler_cls(chunk_interval_s: float, num_chunks: int):
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))  # see the happy-path handler's comment
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            # A steady trickle of tiny chunks, well inside the read timeout on
            # every single one -- exactly the pathology a per-read timeout
            # cannot catch (it resets on every byte). Only a total-duration
            # watchdog catches this; the client is expected to abort mid-loop.
            for _ in range(num_chunks):
                try:
                    self.wfile.write(b"\x00\x00")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(chunk_interval_s)

        def log_message(self, *a):
            pass

    return _Handler


def test_trickle_trips_duration_cap_and_falls_back():
    # 0.1s between chunks, well under the 5s read timeout on every single
    # read -- a read timeout alone would never fire here.
    server = _StubServer(_make_trickle_handler_cls(chunk_interval_s=0.1, num_chunks=30))
    try:
        handler = _make_handler(
            base_url=server.base_url,
            max_duration_s=0.3,
            read_timeout_s=5.0,
        )

        start = time.monotonic()
        chunks = list(handler.process(_TTSInput("hello")))
        elapsed = time.monotonic() - start

        assert len(FakePocketTTSHandler.calls) == 1  # duration cap tripped, fell back
        assert len(chunks) == 1
        # Aborted around the 0.3s cap, nowhere near the full 3s trickle or the 5s read timeout.
        assert elapsed < 1.5
    finally:
        server.close()


# ── circuit breaker: opens after N failures, re-closes after a successful probe ──


def test_circuit_breaker_opens_then_recloses_after_successful_probe(monkeypatch):
    handler = _make_handler(base_url="http://example.invalid", failure_threshold=3)

    def always_fails(text):
        raise ConnectionError("simulated remote failure")
        yield  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(handler, "_stream_remote", always_fails)

    # 3 consecutive failures should open the circuit.
    for _ in range(3):
        list(handler.process(_TTSInput("hi")))
    assert handler._circuit_open_until > 0.0
    assert handler._consecutive_failures == 3

    # While still within the cooldown, no remote attempt (health or synthesis)
    # should be made at all -- the whole point of the breaker.
    probe_calls = []
    monkeypatch.setattr(handler, "_probe_health", lambda: probe_calls.append(1) or False)
    list(handler.process(_TTSInput("hi")))
    assert probe_calls == []  # cooldown not elapsed yet -> no probe attempted
    assert len(FakePocketTTSHandler.calls) == 4  # 3 failures + this one, all served by pocket

    # Jump time past the cooldown: a failed probe should keep it open and
    # restart the cooldown, without touching the real synthesis path.
    # (`rsh.time` and this file's `time` are the *same* module object -- so
    # the replacement must not call `time.monotonic()` itself, or it would
    # recurse into its own patched version.)
    real_monotonic = time.monotonic
    monkeypatch.setattr(rsh.time, "monotonic", lambda: real_monotonic() + 9999)
    list(handler.process(_TTSInput("hi")))
    assert probe_calls == [1]
    assert handler._circuit_open_until > 0.0  # still open

    # The failed probe restarted the cooldown from t=+9999, so jump further
    # still before the next attempt.
    monkeypatch.setattr(rsh.time, "monotonic", lambda: real_monotonic() + 99999)

    # A successful probe closes the circuit and this utterance is attempted
    # (and succeeds) on the remote again.
    monkeypatch.setattr(handler, "_probe_health", lambda: True)
    monkeypatch.setattr(handler, "_stream_remote", lambda text: iter([np.array([1, 2], dtype=np.int16)]))
    chunks = list(handler.process(_TTSInput("hi")))

    assert handler._circuit_open_until == 0.0
    assert handler._consecutive_failures == 0
    assert len(chunks) == 1
    assert len(FakePocketTTSHandler.calls) == 5  # unchanged: this one used the remote, not pocket


# ── lazy pocket preload (default) vs eager preload ──────────────────────────


def test_lazy_preload_default_does_not_construct_pocket_until_first_failover():
    handler = _make_handler(base_url=_closed_port_url())  # remote_speech_fallback_preload defaults False
    assert FakePocketTTSHandler.construct_count == 0  # no weights loaded at startup

    list(handler.process(_TTSInput("hi")))
    assert FakePocketTTSHandler.construct_count == 1
    first_instance = handler._pocket

    list(handler.process(_TTSInput("hi again")))
    assert FakePocketTTSHandler.construct_count == 1  # reused, not rebuilt
    assert handler._pocket is first_instance


def test_fallback_preload_true_constructs_pocket_at_startup_and_reuses_it():
    handler = _make_handler(base_url=_closed_port_url(), fallback_preload=True)
    assert FakePocketTTSHandler.construct_count == 1  # warm immediately
    first_instance = handler._pocket

    list(handler.process(_TTSInput("hi")))
    assert FakePocketTTSHandler.construct_count == 1  # not reconstructed on failover
    assert handler._pocket is first_instance


# ── boot-time validation of the (possibly not-yet-constructed) fallback ────


def test_boot_validation_fails_loudly_when_pocket_tts_package_missing(monkeypatch):
    # sys.modules[name] = None is Python's own sentinel for "import already
    # failed" -- `import pocket_tts` raises ImportError against it, simulating
    # the package not being installed without needing the real thing absent.
    monkeypatch.setitem(sys.modules, "pocket_tts", None)

    with pytest.raises(ImportError):
        _make_handler(base_url=None)


def test_boot_validation_fails_when_no_voice_configured():
    with pytest.raises(ValueError):
        _make_handler(base_url=None, pocket_kwargs={})


# ── instructions must never reach the wire empty ────────────────────────────


def _make_capturing_handler_cls(captured: list, pcm_bytes: bytes):
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(pcm_bytes)

        def log_message(self, *a):
            pass

    return _Handler


def test_instructions_omitted_by_caller_uses_nonempty_default_on_wire():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()  # 6000 bytes, above the response floor
    server = _StubServer(_make_capturing_handler_cls(captured, pcm_bytes))
    try:
        handler = _make_handler(base_url=server.base_url)  # remote_speech_instructions not overridden

        list(handler.process(_TTSInput("hello")))

        assert len(captured) == 1
        assert captured[0]["instructions"] == rsh.DEFAULT_INSTRUCTIONS
        assert captured[0]["instructions"]  # non-empty on the wire
    finally:
        server.close()


def test_instructions_empty_or_whitespace_substitutes_default_on_wire():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(_make_capturing_handler_cls(captured, pcm_bytes))
    try:
        handler = _make_handler(base_url=server.base_url, instructions="   ")

        list(handler.process(_TTSInput("hello")))

        assert captured[0]["instructions"] == rsh.DEFAULT_INSTRUCTIONS
    finally:
        server.close()


# ── "success-shaped but empty" responses must fail over, not play as silence ──


def _make_fixed_body_handler_cls(body: bytes):
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))  # see the happy-path handler's comment
            self.send_response(200)
            self.send_header("Content-Type", "audio/pcm")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *a):
            pass

    return _Handler


def test_zero_byte_200_response_is_always_a_failure_and_falls_back():
    # The verified real-world bug: HTTP 200, Content-Type audio/pcm, zero bytes,
    # in well under a millisecond (this build has no default speaker and an
    # empty instructions field triggers it -- guarded elsewhere, this is the
    # floor-check's defense in depth).
    server = _StubServer(_make_fixed_body_handler_cls(b""))
    try:
        handler = _make_handler(base_url=server.base_url)

        chunks = list(handler.process(_TTSInput("hello")))

        assert len(FakePocketTTSHandler.calls) == 1  # fell back, not silence
        assert handler._consecutive_failures == 1  # counted as a failure, not success
        assert chunks[-1].tolist() == [111, 222, 333]  # pocket's fallback audio
    finally:
        server.close()


def test_short_response_under_floor_is_treated_as_failure_and_falls_back():
    short_body = np.zeros(1000, dtype="<i2").tobytes()  # 2000 bytes, under the 4800-byte floor
    server = _StubServer(_make_fixed_body_handler_cls(short_body))
    try:
        handler = _make_handler(base_url=server.base_url)

        chunks = list(handler.process(_TTSInput("hello")))

        assert len(FakePocketTTSHandler.calls) == 1
        assert handler._consecutive_failures == 1
        assert chunks[-1].tolist() == [111, 222, 333]
    finally:
        server.close()


# ── mid-stream disconnect (remote stopped underneath us) ────────────────────


def _make_midstream_drop_handler_cls(good_chunk: bytes):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))  # see the happy-path handler's comment
            self.send_response(200)
            self.send_header("Content-Type", "audio/pcm")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(good_chunk):x}\r\n".encode() + good_chunk + b"\r\n")
            self.wfile.flush()
            # Sever the connection instead of sending the terminating zero-length
            # chunk -- simulates the remote being stopped mid-utterance (the
            # profile-manager restart case), not a clean end of stream.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, *a):
            pass

    return _Handler


def test_midstream_disconnect_falls_back_and_is_not_treated_as_done():
    # Above the response floor, so a naive "enough bytes = success" check would
    # wrongly treat this as a complete utterance -- only the missing chunked
    # terminator exposes the truncation.
    good_chunk = np.zeros(3000, dtype="<i2").tobytes()  # 6000 bytes > 4800-byte floor
    server = _StubServer(_make_midstream_drop_handler_cls(good_chunk))
    try:
        handler = _make_handler(base_url=server.base_url)

        chunks = list(handler.process(_TTSInput("hello")))

        assert len(FakePocketTTSHandler.calls) == 1  # fell back, not treated as complete
        assert handler._consecutive_failures == 1  # counted as a failure, not a success
        assert chunks[-1].tolist() == [111, 222, 333]  # ends with pocket's fallback audio
    finally:
        server.close()


# ── circuit breaker tolerates the ~4s remote-restart warmup, no retry storm ──


def test_circuit_breaker_tolerates_warmup_without_retry_storm(monkeypatch):
    handler = _make_handler(
        base_url="http://example.invalid",
        failure_threshold=2,
        cooldown_s=5.0,
    )

    def always_fails(text):
        raise ConnectionError("simulated remote down")
        yield  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(handler, "_stream_remote", always_fails)
    for _ in range(2):
        list(handler.process(_TTSInput("hi")))
    assert handler._circuit_open_until > 0.0

    probe_calls: list = []
    fake_now = [time.monotonic()]
    monkeypatch.setattr(rsh.time, "monotonic", lambda: fake_now[0])

    def probe_still_warming_up():
        probe_calls.append(1)
        return False  # not ready yet -- simulates the multi-second warmup window

    monkeypatch.setattr(handler, "_probe_health", probe_still_warming_up)

    # Utterances arriving WITHIN the cooldown (clock not advanced) must not
    # probe at all -- this is what actually bounds the storm, since a busy
    # conversation can produce many utterances per second.
    for _ in range(10):
        list(handler.process(_TTSInput("hi")))
    assert probe_calls == []

    # Utterances keep arriving throughout the warmup; each one that lands after
    # a cooldown expiry should trigger AT MOST one probe -- never a storm of
    # remote attempts while it is still coming up.
    for _ in range(5):
        fake_now[0] += handler.cooldown_s + 0.1
        list(handler.process(_TTSInput("hi")))

    assert len(probe_calls) == 5  # exactly one probe per cooldown-expiry check, never more
    assert handler._circuit_open_until > 0.0  # still open, warmup not over yet
    assert len(FakePocketTTSHandler.calls) == 17  # 2 initial + 10 within-cooldown + 5 warmup utterances

    # The remote is now actually healthy -- the next probe should close it cleanly.
    monkeypatch.setattr(handler, "_probe_health", lambda: True)
    monkeypatch.setattr(handler, "_stream_remote", lambda text: iter([np.array([9], dtype=np.int16)]))
    fake_now[0] += handler.cooldown_s + 0.1

    chunks = list(handler.process(_TTSInput("hi")))

    assert handler._circuit_open_until == 0.0
    assert len(chunks) == 1
    assert len(FakePocketTTSHandler.calls) == 17  # unchanged: this utterance used the remote


# ── the seam that actually broke in production: rename_args() vs setup() ────


def _install_s2s_pipeline_stubs():
    """Stub the arguments_classes/api/pipeline surface s2s_pipeline.py imports
    at module scope, so the REAL `rename_args` function can be imported and
    run -- this is the actual function the pipeline calls before building any
    TTS handler, not a reimplementation of it. Only RemoteSpeechTTSHandlerArguments
    is aliased to the real class (its real field names matter here); every other
    arguments class is a bare placeholder, since rename_args only touches
    `.__dict__` and never inspects the real types."""

    def mod(name, **attrs):
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    from patches.remote_speech_tts_arguments import RemoteSpeechTTSHandlerArguments as RealArgs

    def _raise_lookup(*a, **kw):
        raise LookupError("stubbed: no real nltk data, s2s_pipeline.py's except LookupError handles this")

    mod(
        "nltk",
        data=types.SimpleNamespace(find=_raise_lookup),
        download=lambda *a, **kw: None,  # no-op: don't hit the network for a stubbed dependency
    )
    mod("transformers", HfArgumentParser=object)
    mod("speech_to_speech.api")
    mod("speech_to_speech.api.openai_realtime")
    mod("speech_to_speech.api.openai_realtime.pipeline_unit", PipelineUnit=object)
    mod("speech_to_speech.api.openai_realtime.runtime_config", RuntimeConfig=object)
    mod("speech_to_speech.arguments_classes")
    for modname, clsname in [
        ("chat_completions_language_model_arguments", "ChatCompletionsLanguageModelHandlerArguments"),
        ("chat_tts_arguments", "ChatTTSHandlerArguments"),
        ("facebookmms_tts_arguments", "FacebookMMSTTSHandlerArguments"),
        ("faster_whisper_stt_arguments", "FasterWhisperSTTHandlerArguments"),
        ("kokoro_tts_arguments", "KokoroTTSHandlerArguments"),
        ("language_model_arguments", "LanguageModelHandlerArguments"),
        ("mlx_audio_whisper_arguments", "MLXAudioWhisperSTTHandlerArguments"),
        ("module_arguments", "ModuleArguments"),
        ("paraformer_stt_arguments", "ParaformerSTTHandlerArguments"),
        ("parakeet_tdt_arguments", "ParakeetTDTSTTHandlerArguments"),
        ("pocket_tts_arguments", "PocketTTSHandlerArguments"),
        ("qwen3_tts_arguments", "Qwen3TTSHandlerArguments"),
        ("responses_api_language_model_arguments", "ResponsesApiLanguageModelHandlerArguments"),
        ("socket_receiver_arguments", "SocketReceiverArguments"),
        ("socket_sender_arguments", "SocketSenderArguments"),
        ("vad_arguments", "VADHandlerArguments"),
        ("websocket_streamer_arguments", "WebSocketStreamerArguments"),
        ("whisper_stt_arguments", "WhisperSTTHandlerArguments"),
    ]:
        mod(f"speech_to_speech.arguments_classes.{modname}", **{clsname: object})
    mod("speech_to_speech.arguments_classes.remote_speech_tts_arguments", RemoteSpeechTTSHandlerArguments=RealArgs)
    mod("speech_to_speech.LLM.chat", Chat=object)
    mod("speech_to_speech.pipeline.handler_types", LLMIn=object, LLMOut=object, STTIn=object, STTOut=object)
    mod(
        "speech_to_speech.pipeline.queue_types",
        AudioInItem=object,
        AudioOutItem=object,
        LMOutItem=object,
        STTOutItem=object,
        TextEventItem=object,
        TextPromptItem=object,
        TTSInItem=object,
        VADOutItem=object,
    )
    mod("speech_to_speech.STT.transcription_notifier", TranscriptionNotifier=object)
    mod("speech_to_speech.utils.thread_manager", ThreadManager=object)
    mod("speech_to_speech.VAD.vad_handler", VADHandler=object)


def test_setup_signature_matches_what_rename_args_produces():
    """The boundary that actually broke in production: RemoteSpeechTTSHandler
    was deployed with setup() accepting prefixed names like `remote_speech_base_url`,
    but `rename_args()` in s2s_pipeline.py strips that prefix before setup_kwargs
    ever reaches the handler -- so the live pipeline crashed with `TypeError:
    unexpected keyword argument 'base_url'`. Every other test in this file
    constructs the handler directly with already-correct kwarg names, so none of
    them exercised this seam. This test builds the real ArgumentsClass, runs the
    REAL rename_args on it, and checks both directions against setup()'s real
    signature: every produced key must be accepted (the bug that shipped), and
    every accepted key must be produced (a dead, unreachable CLI flag)."""
    import inspect

    _install_s2s_pipeline_stubs()
    from patches.remote_speech_tts_arguments import RemoteSpeechTTSHandlerArguments
    from patches.s2s_pipeline import rename_args

    args = RemoteSpeechTTSHandlerArguments()
    rename_args(args, "remote_speech")
    produced = dict(args.__dict__)
    # get_tts_handler()'s remote-speech branch also injects pocket_kwargs
    # alongside the renamed dict -- part of the real setup_kwargs the pipeline
    # builds, even though it isn't itself a rename_args output.
    produced["pocket_kwargs"] = {}

    accepted = set(inspect.signature(rsh.RemoteSpeechTTSHandler.setup).parameters) - {"self"}
    # Injected by the pipeline separately (generic cancel_scope/speculative_turns
    # loop, and the should_listen positional setup_arg), never via rename_args.
    injected_separately = {"should_listen", "cancel_scope", "speculative_turns"}

    unaccepted = set(produced) - accepted
    assert not unaccepted, (
        f"rename_args() produces {sorted(unaccepted)}, which setup() does not accept -- "
        f"this is the exact TypeError class that crashed the live pipeline."
    )

    unconsumed = accepted - set(produced) - injected_separately
    assert not unconsumed, (
        f"setup() accepts {sorted(unconsumed)}, but rename_args() (plus pocket_kwargs) "
        f"never produces them -- a dead parameter nobody can configure via CLI/JSON."
    )
