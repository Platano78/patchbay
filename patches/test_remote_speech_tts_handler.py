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
def _clear_tts_capabilities_cache():
    """Module-level cache in tts_capabilities.py, keyed on base_url -- each
    test's `_StubServer` gets a fresh ephemeral port so keys don't usually
    collide across tests, but clear it anyway rather than depend on that."""
    tts_capabilities._cache.clear()
    tts_capabilities._instruct_rejected_at.clear()
    yield
    tts_capabilities._cache.clear()
    tts_capabilities._instruct_rejected_at.clear()


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

# remote_speech_tts_handler.py imports `speech_to_speech.tts_capabilities` at
# module scope -- alias it to the REAL module (same pattern as voice_clone/
# brain_discovery in test_brain_control.py) BEFORE that import below, so these
# tests exercise the actual cache/probe logic, not a missing stub.
import patches.tts_capabilities as tts_capabilities  # noqa: E402

sys.modules["speech_to_speech.tts_capabilities"] = tts_capabilities

import patches.remote_speech_tts_handler as rsh  # noqa: E402


class _TTSInput:
    def __init__(self, text: str):
        self.text = text


class _AffectTTSInput(_TTSInput):
    """Stands in for lm_output_processor.AffectTTSInput -- carries a per-turn
    affect note. A plain `_TTSInput` (no `affect` attribute at all) is what
    the voice-audition / hermes-cockpit call sites actually construct."""

    def __init__(self, text: str, affect: str | None):
        super().__init__(text)
        self.affect = affect


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

    def always_fails(text, affect=None):
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
    monkeypatch.setattr(handler, "_stream_remote", lambda text, affect=None: iter([np.array([1, 2], dtype=np.int16)]))
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


# ── instructions: omitted by default, sent only when configured AND wanted ──
# Owner ruling 2026-08-25: a fixed neutral instruct on every request IS a
# one-string instruct layer, which the owner rejected -- the voice should come
# back free-flowing unless an operator explicitly configures otherwise, and
# even then only if the active backend's tts_capabilities probe says it wants
# the field (some VoiceDesign builds fail silently on it -- see DEFAULT_INSTRUCTIONS).


def _make_capturing_handler_cls(captured: list, pcm_bytes: bytes, voices_response: dict | None = None):
    """`voices_response`, when given, is served as the JSON body of
    `GET /v1/audio/voices` -- lets a test control what `tts_capabilities`'s
    remote probe observes. `None` means no route at all (404), which is what
    a probe failure (and thus the empty-capabilities default) looks like."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(pcm_bytes)

        def do_GET(self):
            if self.path != "/v1/audio/voices" or voices_response is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(voices_response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return _Handler


def test_instructions_omitted_when_none_configured():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()  # 6000 bytes, above the response floor
    server = _StubServer(_make_capturing_handler_cls(captured, pcm_bytes))
    try:
        handler = _make_handler(base_url=server.base_url)  # remote_speech_instructions not overridden

        list(handler.process(_TTSInput("hello")))

        assert len(captured) == 1
        assert "instructions" not in captured[0]
    finally:
        server.close()


def test_instructions_whitespace_configured_is_also_omitted():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(_make_capturing_handler_cls(captured, pcm_bytes))
    try:
        handler = _make_handler(base_url=server.base_url, instructions="   ")

        list(handler.process(_TTSInput("hello")))

        assert "instructions" not in captured[0]
    finally:
        server.close()


def test_instructions_sent_when_configured_and_backend_accepts_them():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(
        _make_capturing_handler_cls(captured, pcm_bytes, voices_response={"voices": [], "accepts_instructions": True})
    )
    try:
        handler = _make_handler(base_url=server.base_url, instructions="be dramatic")

        list(handler.process(_TTSInput("hello")))

        assert captured[0]["instructions"] == "be dramatic"
    finally:
        server.close()


def test_instructions_omitted_when_backend_declares_it_does_not_accept_them():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(
        _make_capturing_handler_cls(
            captured, pcm_bytes, voices_response={"voices": [], "accepts_instructions": False}
        )
    )
    try:
        handler = _make_handler(base_url=server.base_url, instructions="be dramatic")

        list(handler.process(_TTSInput("hello")))

        assert "instructions" not in captured[0]
    finally:
        server.close()


# ── per-turn affect: precedence against operator instructions ──────────────
# Owner ruling: operator-set `--remote_speech_instructions` (or live
# `speech_style`) always wins over the brain's per-turn affect -- silently
# overriding an explicit live user action would make the cockpit panel lie.


def test_affect_used_when_no_operator_instructions_configured():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(
        _make_capturing_handler_cls(captured, pcm_bytes, voices_response={"voices": [], "accepts_instructions": True})
    )
    try:
        handler = _make_handler(base_url=server.base_url)  # no operator instructions configured

        list(handler.process(_AffectTTSInput("hello", "be amused")))

        assert captured[0]["instructions"] == "be amused"
    finally:
        server.close()


def test_operator_instructions_win_over_affect():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(
        _make_capturing_handler_cls(captured, pcm_bytes, voices_response={"voices": [], "accepts_instructions": True})
    )
    try:
        handler = _make_handler(base_url=server.base_url, instructions="be dramatic")

        list(handler.process(_AffectTTSInput("hello", "be amused")))

        assert captured[0]["instructions"] == "be dramatic"
    finally:
        server.close()


def test_no_operator_instructions_and_no_affect_sends_no_instructions_key():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(
        _make_capturing_handler_cls(captured, pcm_bytes, voices_response={"voices": [], "accepts_instructions": True})
    )
    try:
        handler = _make_handler(base_url=server.base_url)

        list(handler.process(_AffectTTSInput("hello", None)))

        assert "instructions" not in captured[0]
    finally:
        server.close()


def test_affect_omitted_when_backend_does_not_accept_instructions():
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(
        _make_capturing_handler_cls(
            captured, pcm_bytes, voices_response={"voices": [], "accepts_instructions": False}
        )
    )
    try:
        handler = _make_handler(base_url=server.base_url)

        list(handler.process(_AffectTTSInput("hello", "be amused")))

        assert "instructions" not in captured[0]
    finally:
        server.close()


def test_plain_tts_input_with_no_affect_attribute_reaches_stream_remote_unaffected():
    # The voice-audition / hermes-cockpit call sites construct a bare
    # TTSInput with no `affect` attribute at all -- getattr(..., None) must
    # not raise, and no instruct is sent.
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _StubServer(
        _make_capturing_handler_cls(captured, pcm_bytes, voices_response={"voices": [], "accepts_instructions": True})
    )
    try:
        handler = _make_handler(base_url=server.base_url)

        chunks = list(handler.process(_TTSInput("hello")))  # plain, no .affect

        assert len(chunks) > 0
        assert "instructions" not in captured[0]
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

    def always_fails(text, affect=None):
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
    monkeypatch.setattr(handler, "_stream_remote", lambda text, affect=None: iter([np.array([9], dtype=np.int16)]))
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
    mod(
        "speech_to_speech.arguments_classes.remote_transcription_stt_arguments",
        RemoteTranscriptionSTTHandlerArguments=object,
    )
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


def test_rename_args_instructions_default_is_empty_on_the_real_pipeline_path():
    """The seam that actually shipped wrong: `setup()`'s own default (asserted
    on directly by every other test in this file, including the omission tests
    above) is NEVER what the real pipeline hands the handler -- `rename_args()`
    always produces an explicit `instructions` key, taken from
    RemoteSpeechTTSHandlerArguments' field default, which overrides `setup()`'s
    default entirely. `setup(instructions="")`'s default going empty was not
    enough on its own; this ArgumentsClass field had to change too, or the live
    pipeline kept sending the old neutral instruct on every request regardless.
    Crosses the REAL rename_args boundary (same stub infra as
    test_setup_signature_matches_what_rename_args_produces above), not a
    reimplementation of it."""
    _install_s2s_pipeline_stubs()
    from patches.remote_speech_tts_arguments import RemoteSpeechTTSHandlerArguments
    from patches.s2s_pipeline import rename_args

    args = RemoteSpeechTTSHandlerArguments()
    rename_args(args, "remote_speech")

    assert args.__dict__["instructions"] == ""


# ── barge-in cancel: a cancelled generation is never spoken ─────────────────


class _Scope:
    """CancelScope's contract here: `generation`, `is_stale`, and `cancel` bumping it."""

    def __init__(self):
        self.generation = 1

    def is_stale(self, gen):
        return gen != self.generation

    def cancel(self):
        self.generation += 1


class _GenInput(_TTSInput):
    def __init__(self, text, cancel_generation):
        super().__init__(text)
        self.cancel_generation = cancel_generation


def test_input_of_a_cancelled_generation_is_dropped_without_network_or_fallback():
    handler = _make_handler(base_url=_closed_port_url(), cancel_scope=_Scope())

    chunks = list(handler.process(_GenInput("old reply", cancel_generation=0)))

    assert chunks == []
    assert len(FakePocketTTSHandler.calls) == 0  # not even the pocket fallback speaks it


def test_end_of_response_of_a_cancelled_generation_does_not_signal_done():
    handler = _make_handler(base_url=_closed_port_url(), cancel_scope=_Scope())
    stale = rsh.EndOfResponse()
    stale.cancel_generation = 0
    current = rsh.EndOfResponse()
    current.cancel_generation = 1

    assert list(handler.process(stale)) == []
    assert list(handler.process(current)) == [rsh.AUDIO_RESPONSE_DONE]


def test_cancel_during_synthesis_stops_the_stream_without_failing_over():
    pcm_bytes = np.linspace(-16000, 16000, 48000, dtype=np.int16).astype("<i2").tobytes()
    server = _StubServer(_make_happy_handler_cls(pcm_bytes, writes=8))
    try:
        scope = _Scope()
        handler = _make_handler(base_url=server.base_url, cancel_scope=scope)
        control = _make_handler(base_url=server.base_url)
        # Skip the scipy resample (not in every dev venv); the block cadence is what's under test.
        handler._needs_resampling = control._needs_resampling = False
        full = list(control.process(_TTSInput("hello world")))
        FakePocketTTSHandler.calls = []
        assert len(full) > 4, "premise: an uncancelled stream is many blocks"

        got = []
        for chunk in handler.process(_GenInput("hello world", cancel_generation=1)):
            got.append(chunk)
            if len(got) == 2:
                scope.cancel()  # the user barges in after two blocks

        assert len(got) < len(full)
        assert len(got) <= 3  # at most the block already in hand
        assert len(FakePocketTTSHandler.calls) == 0  # a cancel is not a failure to fail over from
    finally:
        server.close()


def test_without_a_cancel_scope_an_old_generation_is_still_spoken():
    """VOICE_BARGE_CANCEL=off: no scope is wired and nothing is judged stale."""
    pcm_bytes = np.zeros(6000, dtype="<i2").tobytes()
    server = _StubServer(_make_happy_handler_cls(pcm_bytes, writes=2))
    try:
        handler = _make_handler(base_url=server.base_url)

        assert len(list(handler.process(_GenInput("hello", cancel_generation=0)))) > 0
    finally:
        server.close()


def test_cancel_at_any_block_leaves_nothing_more_to_yield_including_the_final_flush():
    """Whichever block the cancel lands after -- in-loop or the end-of-stream flush -- no further block comes out."""
    # 1000 samples = 2000 bytes: not a multiple of the target chunk, so the stream ends with a flush tail.
    pcm_bytes = np.linspace(-16000, 16000, 2500, dtype=np.int16).astype("<i2").tobytes()
    server = _StubServer(_make_happy_handler_cls(pcm_bytes, writes=1))
    try:
        control = _make_handler(base_url=server.base_url)
        control._needs_resampling = False
        full = list(control.process(_TTSInput("hello")))
        assert len(full) >= 3, "premise: several blocks, ending in the flush"
        for k in range(1, len(full)):
            scope = _Scope()
            handler = _make_handler(base_url=server.base_url, cancel_scope=scope)
            handler._needs_resampling = False
            gen = handler.process(_GenInput("hello", cancel_generation=1))
            for _ in range(k):
                next(gen)
            scope.cancel()
            assert list(gen) == [], f"cancel after block {k} of {len(full)} still yielded audio"
    finally:
        server.close()


# ── base-mode backend: `instructions` rejected with HTTP 400 ───────────────
# Verified live 2026-10-23: a Qwen3-TTS BASE-mode server answers any request
# carrying `instructions` with this exact 400 and never says so on /voices.

_BASE_MODE_400 = json.dumps({"error": {"message": "--instruct is not supported for base models", "type": "x"}})


def _make_base_mode_handler_cls(posts: list, pcm_bytes: bytes, reject_body: str = _BASE_MODE_400):
    """400s (with `reject_body`) any POST carrying `instructions`; 200 PCM otherwise.
    /v1/audio/voices says nothing about accepts_instructions (like the live server)."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            posts.append(body)
            if "instructions" in body:
                data = reject_body.encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(pcm_bytes)

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"voices": [{"name": "aiden"}]}')

        def log_message(self, *a):
            pass

    return _Handler


def test_instruct_400_retries_once_without_instructions_and_flips_capability_cache():
    posts: list = []
    server = _StubServer(_make_base_mode_handler_cls(posts, np.zeros(3000, dtype="<i2").tobytes()))
    try:
        handler = _make_handler(base_url=server.base_url)

        chunks = list(handler.process(_AffectTTSInput("hello", "be amused")))

        assert len(chunks) > 0
        assert len(FakePocketTTSHandler.calls) == 0, "remote audio must be served, not pocket"
        assert [("instructions" in p) for p in posts] == [True, False], "exactly one retry, without instructions"
        assert handler._consecutive_failures == 0
        assert handler._circuit_open_until == 0.0
        assert tts_capabilities.get_capabilities(handler).accepts_instructions is False

        posts.clear()
        list(handler.process(_AffectTTSInput("again", "be amused")))
        assert len(posts) == 1 and "instructions" not in posts[0], "next turn must not re-trigger the 400"
    finally:
        server.close()


def test_instruct_rejection_is_not_permanent_after_override_expires(monkeypatch):
    posts: list = []
    server = _StubServer(_make_base_mode_handler_cls(posts, np.zeros(3000, dtype="<i2").tobytes()))
    try:
        handler = _make_handler(base_url=server.base_url)
        list(handler.process(_AffectTTSInput("hello", "be amused")))
        assert tts_capabilities.get_capabilities(handler).accepts_instructions is False

        # Past the override window AND the probe-cache TTL: instructions are tried again.
        real = time.monotonic
        monkeypatch.setattr(tts_capabilities.time, "monotonic", lambda: real() + tts_capabilities._INSTRUCT_REJECT_TTL_S + 1)
        assert tts_capabilities.get_capabilities(handler).accepts_instructions is True
    finally:
        server.close()


def test_other_400_is_not_retried_and_counts_as_a_failure():
    posts: list = []
    body = json.dumps({"error": {"message": "unknown voice: nope"}})
    server = _StubServer(_make_base_mode_handler_cls(posts, b"\x00" * 3000, reject_body=body))
    try:
        handler = _make_handler(base_url=server.base_url)

        chunks = list(handler.process(_AffectTTSInput("hello", "be amused")))

        assert len(posts) == 1, "an unrelated 400 must not be retried"
        assert handler._consecutive_failures == 1
        assert len(FakePocketTTSHandler.calls) == 1, "falls back to pocket as before"
        assert np.array_equal(chunks[-1], np.array([111, 222, 333], dtype=np.int16))
        assert tts_capabilities.get_capabilities(handler).accepts_instructions is True
    finally:
        server.close()


# ── pocket fallback must never be handed a voice pocket cannot load ────────
# Verified live: the selected remote voice 'nature-documentary' reached
# PocketTTSHandler and raised LibsndfileError -> silence.


def _fallback_voice(handler) -> str:
    list(handler.process(_TTSInput("hello")))
    return handler._pocket.extra_kwargs["voice"]


def test_fallback_uses_pocket_default_voice_when_selected_voice_is_remote_only(caplog):
    handler = _make_handler(base_url=None, pocket_kwargs={"voice": "jean"})
    # what brain_control._set_voice_remote does on a live voice change:
    handler._pocket_kwargs["voice"] = "nature-documentary"
    handler._pocket = None

    with caplog.at_level("INFO", logger=rsh.logger.name):
        assert _fallback_voice(handler) == "jean"

    msgs = [r.getMessage() for r in caplog.records if "pocket fallback voice" in r.getMessage()]
    assert len(msgs) == 1 and "nature-documentary" in msgs[0] and "jean" in msgs[0]


def test_fallback_keeps_a_pocket_preset_selection():
    handler = _make_handler(base_url=None, pocket_kwargs={"voice": "jean"})
    handler._pocket_kwargs["voice"] = "alba"
    handler._pocket = None

    assert _fallback_voice(handler) == "alba"


def test_fallback_keeps_a_registered_cloned_voice(tmp_path, monkeypatch):
    import patches.voice_clone as vc

    monkeypatch.setitem(sys.modules, "speech_to_speech.voice_clone", vc)
    monkeypatch.setenv("VOICE_CLONE_DIR", str(tmp_path))
    (tmp_path / "myclone.safetensors").write_bytes(b"x")
    handler = _make_handler(base_url=None, pocket_kwargs={"voice": "jean"})
    handler._pocket_kwargs["voice"] = "myclone"
    handler._pocket = None

    assert _fallback_voice(handler) == str(tmp_path / "myclone.safetensors")


def test_fallback_keeps_an_existing_file_path_voice(tmp_path):
    wav = tmp_path / "me.wav"
    wav.write_bytes(b"x")
    handler = _make_handler(base_url=None, pocket_kwargs={"voice": "jean"})
    handler._pocket_kwargs["voice"] = str(wav)
    handler._pocket = None

    assert _fallback_voice(handler) == str(wav)


# ── non-24kHz source, stream_format, run-on guard ───────────────────────────


def _sine_pcm(seconds: float, rate: int) -> bytes:
    t = np.arange(int(seconds * rate))
    return (np.sin(t * 0.05) * 8000).astype("<i2").tobytes()


def test_48k_source_is_resampled_to_16k_and_duration_preserved():
    pcm = _sine_pcm(1.0, 48000)  # 48000 samples
    server = _StubServer(_make_happy_handler_cls(pcm, writes=8))
    try:
        handler = _make_handler(base_url=server.base_url, source_sample_rate=48000)
        assert handler._target_chunk_size == 1536  # 512 * 48000/16000

        chunks = list(handler.process(_TTSInput("hello world")))

        assert len(FakePocketTTSHandler.calls) == 0
        assert all(len(c) == handler.blocksize for c in chunks)
        total = sum(len(c) for c in chunks)
        assert abs(total - 48000 // 3) <= handler.blocksize
    finally:
        server.close()


def test_response_floor_scales_with_source_rate():
    # 100 ms of 48kHz s16le mono = 9600 bytes; 5000 bytes is ~52 ms there (but
    # would clear the 24kHz floor of 4800, so this only passes if the floor scales).
    short = _StubServer(_make_fixed_body_handler_cls(_sine_pcm(0.1, 48000)[:5000]))
    ok = _StubServer(_make_fixed_body_handler_cls(_sine_pcm(0.1, 48000)[:9600]))
    try:
        h = _make_handler(base_url=short.base_url, source_sample_rate=48000)
        list(h.process(_TTSInput("hello")))
        assert len(FakePocketTTSHandler.calls) == 1
        assert h._consecutive_failures == 1

        h = _make_handler(base_url=ok.base_url, source_sample_rate=48000)
        list(h.process(_TTSInput("hello")))
        assert len(FakePocketTTSHandler.calls) == 0  # _make_handler resets the list
        assert h._consecutive_failures == 0
    finally:
        short.close()
        ok.close()


def test_stream_format_sent_when_set_and_omitted_when_empty():
    captured: list = []
    server = _StubServer(_make_capturing_handler_cls(captured, np.zeros(3000, dtype="<i2").tobytes()))
    try:
        list(_make_handler(base_url=server.base_url, stream_format="audio").process(_TTSInput("hi")))
        list(_make_handler(base_url=server.base_url).process(_TTSInput("hi")))
        assert captured[0]["stream_format"] == "audio"
        assert "stream_format" not in captured[1]
    finally:
        server.close()


def test_stream_format_survives_the_instructions_400_retry():
    posts: list = []
    server = _StubServer(_make_base_mode_handler_cls(posts, np.zeros(3000, dtype="<i2").tobytes()))
    try:
        handler = _make_handler(base_url=server.base_url, stream_format="audio")
        list(handler.process(_AffectTTSInput("hello", "be amused")))
        assert [("instructions" in p) for p in posts] == [True, False]
        assert [p.get("stream_format") for p in posts] == ["audio", "audio"]
    finally:
        server.close()


def _played_seconds(chunks, rate=16000) -> float:
    return sum(len(c) for c in chunks) / rate


def test_runon_guard_cuts_a_long_stream_without_fallback_or_failure(caplog):
    pcm = _sine_pcm(13.0, 24000)
    server = _StubServer(_make_happy_handler_cls(pcm, writes=650))
    try:
        handler = _make_handler(base_url=server.base_url, runon_chars_per_s=7.0, max_duration_s=30.0)
        text = "x" * 35  # cap = 2 + 35/7 = 7 s
        with caplog.at_level("WARNING"):
            chunks = list(handler.process(_TTSInput(text)))

        # lower bound is loose: TCP may coalesce several small writes into one read
        assert 5.0 <= _played_seconds(chunks) <= 7.2
        assert len(FakePocketTTSHandler.calls) == 0
        assert handler._consecutive_failures == 0
        assert handler._circuit_open_until == 0.0
        assert len([r for r in caplog.records if "run-on" in r.getMessage()]) == 1
    finally:
        server.close()


def test_runon_guard_disabled_by_default_plays_everything():
    pcm = _sine_pcm(13.0, 24000)
    server = _StubServer(_make_happy_handler_cls(pcm, writes=130))
    try:
        handler = _make_handler(base_url=server.base_url, max_duration_s=30.0)
        chunks = list(handler.process(_TTSInput("x" * 35)))
        assert _played_seconds(chunks) >= 12.9
    finally:
        server.close()


def test_new_args_reach_setup_through_the_real_rename_args_path():
    """Ruling-5 boot trap: ArgumentsClass fields are remote_speech_*; setup()
    gets them prefix-stripped. Builds the real args class with non-default
    values, runs the REAL rename_args, and constructs the handler from exactly
    what it produces -- a misnamed setup() parameter is a TypeError here."""
    _install_s2s_pipeline_stubs()
    from patches.remote_speech_tts_arguments import RemoteSpeechTTSHandlerArguments
    from patches.s2s_pipeline import rename_args

    args = RemoteSpeechTTSHandlerArguments(
        remote_speech_base_url="http://10.0.0.5:9000",
        remote_speech_source_sample_rate=48000,
        remote_speech_stream_format="audio",
        remote_speech_runon_chars_per_s=7.0,
    )
    rename_args(args, "remote_speech")
    produced = dict(args.__dict__)
    produced["pocket_kwargs"] = {"voice": "jean"}

    handler = rsh.RemoteSpeechTTSHandler(
        Event(), queue_in=Queue(), queue_out=Queue(), setup_args=(Event(),), setup_kwargs=produced
    )

    assert handler.source_sample_rate == 48000
    assert handler.stream_format == "audio"
    assert handler.runon_chars_per_s == 7.0
    assert handler._target_chunk_size == 1536


def _make_scripted_handler_cls(script):
    """`script`: list of (sleep_before_s, bytes) writes, in order."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "audio/pcm")
            self.end_headers()
            for delay, data in script:
                time.sleep(delay)
                self.wfile.write(data)
                self.wfile.flush()

        def log_message(self, *a):
            pass

    return _Handler


def test_runon_cut_wins_over_the_deadline_when_the_cap_crossing_chunk_is_late():
    # 1 s of audio arrives at once, then 2.5 s more arrive after max_duration_s:
    # that chunk crosses the 3 s cap AND the deadline. Cut, never TimeoutError->pocket.
    # 8 kHz source keeps the late chunk under one 64 KiB read, so the cap crossing
    # lands on the first read after the deadline.
    script = [(0, _sine_pcm(1.0, 8000)), (0.6, _sine_pcm(2.5, 8000))]
    server = _StubServer(_make_scripted_handler_cls(script))
    try:
        handler = _make_handler(
            base_url=server.base_url,
            runon_chars_per_s=7.0,
            source_sample_rate=8000,
            max_duration_s=0.3,
            read_timeout_s=5.0,
        )
        chunks = list(handler.process(_TTSInput("x" * 7)))  # cap = 2 + 7/7 = 3 s

        assert len(chunks) > 0
        assert len(FakePocketTTSHandler.calls) == 0
        assert handler._consecutive_failures == 0
    finally:
        server.close()


def test_runon_cut_before_any_audio_was_spoken_fails_over_to_pocket():
    # source 1 kHz so one read (8000 bytes = 4 s) crosses the 3 s cap deterministically,
    # before a single block is yielded: nothing was spoken, so pocket is right.
    server = _StubServer(_make_scripted_handler_cls([(0, _sine_pcm(4.0, 1000))]))
    try:
        handler = _make_handler(
            base_url=server.base_url, runon_chars_per_s=7.0, source_sample_rate=1000, max_duration_s=5.0
        )
        chunks = list(handler.process(_TTSInput("x" * 7)))

        assert len(FakePocketTTSHandler.calls) == 1
        assert handler._consecutive_failures == 1
        assert chunks[-1].tolist() == [111, 222, 333]
    finally:
        server.close()
