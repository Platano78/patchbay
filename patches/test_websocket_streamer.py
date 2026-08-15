"""Unit tests for websocket_streamer.py's voice-clone begin/chunk hardening
(fix #3: don't leave a client stuck on "receiving" if the streamer-side
structural check ever disagrees with BrainControl's) and for the echo-gate
wiring (send-path `note_playback`, receive-path `feed`, session-boundary
`reset`).

The echo-gate tests here cover *wiring*, not scoring: whether the gate
correctly tells echo from speech is pinned in `test_echo_gate.py` against
real WAVs, and duplicating it here would just couple this file to those
thresholds. So the ON-path tests drive a deterministic stand-in gate, while
the default-OFF test uses the REAL `EchoGate` -- that one is about the
production object being inert when unconfigured, so a stand-in would prove
nothing.

Run from repo root: python3 -m pytest patches/test_websocket_streamer.py -v

`websockets` is installed in this dev venv, but the `speech_to_speech`
package is not -- its handful of pipeline symbols websocket_streamer.py
imports are stubbed into `sys.modules` before the import below, same
hermetic pattern as `test_reflex_lane.py`/`test_brain_control.py`.
`speech_to_speech.voice_clone` is aliased to the REAL `patches.voice_clone`
module so `UploadManager` here is the actual deployed logic.

Only `_resolve_voice_clone_begin_result` and `_voice_clone_chunk` are
exercised -- both are plain synchronous methods, so no asyncio harness is
needed to cover this fix in isolation.
"""

from __future__ import annotations

import asyncio
import base64
import sys
import threading
import types
from queue import Queue


def _install_stubs():
    def mod(name, **attrs):
        # Merge onto an existing sys.modules entry rather than replacing it:
        # multiple test files stub the same speech_to_speech.* submodules,
        # and pytest imports every test module before any test function
        # runs -- a later file's stub would otherwise silently erase
        # attributes an earlier file's LAZY (call-time) import still needs.
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    from patches import echo_gate as real_echo_gate
    from patches import phone_context as real_phone_context
    from patches import transcript_buffer as real_transcript_buffer
    from patches import voice_clone as real_voice_clone

    pkg = mod("speech_to_speech")
    mod("speech_to_speech.pipeline")
    mod(
        "speech_to_speech.pipeline.control",
        # Real SESSION_END is a PipelineControlMessage; `_send_loop` reads
        # `.kind` off it, so a bare object() is not a faithful enough stub.
        SESSION_END=types.SimpleNamespace(kind="session_end"),
        PipelineControlMessage=type("_PipelineControlMessage", (), {}),
        is_control_message=lambda item, kind: False,
    )
    mod("speech_to_speech.pipeline.events", PipelineEvent=type("_PipelineEvent", (), {}))
    mod("speech_to_speech.pipeline.messages", AUDIO_RESPONSE_DONE=object(), PIPELINE_END=object())
    mod(
        "speech_to_speech.pipeline.queue_types",
        AudioInItem=object,
        AudioOutItem=object,
        TextEventItem=object,
    )
    mod("speech_to_speech.turn_stats", turn_stats=types.SimpleNamespace(mark=lambda *a, **kw: None))
    sys.modules["speech_to_speech.voice_clone"] = real_voice_clone
    pkg.voice_clone = real_voice_clone
    sys.modules["speech_to_speech.phone_context"] = real_phone_context
    pkg.phone_context = real_phone_context
    sys.modules["speech_to_speech.transcript_buffer"] = real_transcript_buffer
    pkg.transcript_buffer = real_transcript_buffer
    # Real module, not a stub: the default-OFF test below asserts the actual
    # shipped EchoGate is inert when VOICE_ECHO_GATE is unset.
    sys.modules["speech_to_speech.echo_gate"] = real_echo_gate
    pkg.echo_gate = real_echo_gate

    class _StubWakewordGate:
        def __init__(self):
            self.enabled = False
            self.awake = False
            self.phrase = "hey_jarvis"

        def reset(self):
            pass

    mod("speech_to_speech.wakeword_gate", WakewordGate=_StubWakewordGate)


_install_stubs()

import patches.websocket_streamer as websocket_streamer  # noqa: E402
from patches.websocket_streamer import WebSocketStreamer, _origin_allowed_same_host, _parse_ws_origins  # noqa: E402


def _make_streamer(control_callback=None):
    return WebSocketStreamer(
        stop_event=threading.Event(),
        input_queue=Queue(),
        output_queue=Queue(),
        should_listen=threading.Event(),
        control_callback=control_callback,
    )


# ── _resolve_voice_clone_begin_result (fix #3) ──────────────────────────


def test_begin_result_passthrough_on_brain_control_error():
    streamer = _make_streamer()
    msg = {"type": "voice_clone_begin", "name": "my_voice", "ext": ".wav", "size": 100}
    brain_control_result = {"type": "voice_clone_result", "ok": False, "name": "my_voice", "error": "name taken"}

    result = streamer._resolve_voice_clone_begin_result("client-1", msg, brain_control_result)

    assert result is brain_control_result
    # No session should have been opened.
    ok, error, name = streamer._voice_uploads.chunk("client-1", b"x")
    assert not ok
    assert error == "no upload in progress"


def test_begin_result_opens_session_on_success():
    streamer = _make_streamer()
    msg = {"type": "voice_clone_begin", "name": "my_voice", "ext": ".wav", "size": 100}
    brain_control_result = {"type": "voice_clone_progress", "stage": "receiving"}

    result = streamer._resolve_voice_clone_begin_result("client-1", msg, brain_control_result)

    assert result == brain_control_result
    ok, error, name = streamer._voice_uploads.chunk("client-1", b"hello")
    assert (ok, error, name) == (True, "", "my_voice")


def test_begin_result_downgrades_when_local_check_disagrees():
    streamer = _make_streamer()
    # Simulate the local structural check disagreeing with BrainControl's
    # (the exact scenario fix #3 guards against): size is invalid locally.
    msg = {"type": "voice_clone_begin", "name": "my_voice", "ext": ".wav", "size": "not-a-number"}
    brain_control_result = {"type": "voice_clone_progress", "stage": "receiving"}

    result = streamer._resolve_voice_clone_begin_result("client-1", msg, brain_control_result)

    assert result["type"] == "voice_clone_result"
    assert result["ok"] is False
    assert result["name"] == "my_voice"
    assert "invalid upload size" in result["error"]

    # No stuck session left behind -- a follow-up chunk cleanly reports
    # "no upload in progress" instead of silently vanishing.
    ok, error, name = streamer._voice_uploads.chunk("client-1", b"x")
    assert not ok
    assert error == "no upload in progress"


def test_begin_result_non_progress_types_pass_through_unchanged():
    streamer = _make_streamer()
    msg = {"type": "config_set", "voice": "alba"}
    ack = {"type": "config_ack", "ok": True}

    result = streamer._resolve_voice_clone_begin_result("client-1", msg, ack)

    assert result is ack


# ── _voice_clone_chunk (malformed payload / no session) ─────────────────


def test_chunk_rejects_malformed_base64():
    streamer = _make_streamer()
    result = streamer._voice_clone_chunk("client-1", "not valid base64!!!")
    assert result["ok"] is False
    assert result["error"] == "malformed chunk payload"


def test_chunk_without_session_reports_out_of_order():
    streamer = _make_streamer()
    result = streamer._voice_clone_chunk("client-1", "aGVsbG8=")  # "hello"
    assert result == {"type": "voice_clone_result", "ok": False, "name": None, "error": "no upload in progress"}


# ── echo gate wiring ────────────────────────────────────────────────────

CHUNK_BYTES = 512 * 2  # one VAD chunk, matching _handle_client's split


class _RecordingGate:
    """Deterministic stand-in: records every call, and drops the mic chunks
    whose index appears in `drop_indices`. Lets the wiring tests assert
    exactly which bytes reached which method without depending on how the
    real correlator scores anything."""

    def __init__(self, drop_indices=()):
        self.enabled = True
        self.played = []
        self.fed = []
        self.resets = 0
        self._drop = set(drop_indices)

    def note_playback(self, pcm):
        self.played.append(pcm)

    def feed(self, pcm):
        idx = len(self.fed)
        self.fed.append(pcm)
        return idx not in self._drop

    def reset(self):
        self.resets += 1

    def state(self):
        return "gating"


class _FakeClient:
    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self.sent = []

    def __aiter__(self):
        async def gen():
            for message in self._incoming:
                yield message

        return gen()

    async def send(self, data):
        self.sent.append(data)


def _drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_gate_off_is_pure_passthrough(monkeypatch):
    """Default-OFF equivalence, proven against the REAL EchoGate: with
    VOICE_ECHO_GATE unset every chunk reaches input_queue and no playback
    reference is retained, so a restart with no env changes behaves exactly
    as it did before the gate was wired in."""
    monkeypatch.delenv("VOICE_ECHO_GATE", raising=False)
    streamer = _make_streamer()
    assert streamer.echo_gate.enabled is False
    assert streamer.echo_gate.state() == "off"

    # Send path: note_playback is called but must retain nothing.
    client = _FakeClient()
    streamer.clients.add(client)
    asyncio.run(streamer._broadcast_audio(b"\x11\x22" * 800))
    assert streamer.echo_gate._ref_buffer == bytearray()
    assert streamer.echo_gate._ref_total == 0

    # Receive path: every chunk passes, unconditionally.
    for pattern in (b"\x00\x00", b"\x7f\x7f", b"\x01\x02"):
        assert streamer._echo_pass(pattern * 512) is True


def test_gate_off_receive_loop_queues_every_chunk(monkeypatch):
    """The same equivalence at the loop level: three whole chunks in one
    websocket message produce three input_queue puts, byte-identical to the
    unwired behaviour."""
    monkeypatch.delenv("VOICE_ECHO_GATE", raising=False)
    streamer = _make_streamer()
    streamer.should_listen.set()
    payload = b"".join(bytes([i]) * CHUNK_BYTES for i in (1, 2, 3))

    asyncio.run(streamer._handle_client(_FakeClient([payload])))

    queued = _drain(streamer.input_queue)
    assert queued[:3] == [bytes([i]) * CHUNK_BYTES for i in (1, 2, 3)]


def test_gate_on_drops_scored_echo_and_passes_the_rest():
    streamer = _make_streamer()
    streamer.echo_gate = _RecordingGate(drop_indices=(0, 2))
    streamer.should_listen.set()
    chunks = [bytes([i]) * CHUNK_BYTES for i in (10, 11, 12, 13)]

    asyncio.run(streamer._handle_client(_FakeClient([b"".join(chunks)])))

    # The gate saw all four; only the two it passed were enqueued.
    assert streamer.echo_gate.fed == chunks
    queued = _drain(streamer.input_queue)
    assert queued[:2] == [chunks[1], chunks[3]]


def test_note_playback_receives_exactly_the_bytes_sent():
    streamer = _make_streamer()
    streamer.echo_gate = _RecordingGate()
    client = _FakeClient()
    streamer.clients.add(client)
    data = bytes(range(256)) * 8

    asyncio.run(streamer._broadcast_audio(data))

    assert streamer.echo_gate.played == [data]
    assert client.sent == [data]


def test_send_loop_audio_paths_all_record_playback():
    """Drives the real `_send_loop`: the threshold flush (>= MIN_AUDIO_BYTES)
    and the queue-empty flush must each hand their exact payload to
    note_playback."""
    streamer = _make_streamer()
    streamer.echo_gate = _RecordingGate()
    client = _FakeClient()
    streamer.clients.add(client)
    # 4000 bytes: crosses MIN_AUDIO_BYTES (3200) on the threshold path, then
    # the 800-byte remainder leaves via the queue-empty flush.
    streamer.output_queue.put(b"\xab\xcd" * 2000)

    async def drive():
        task = asyncio.create_task(streamer._send_loop())
        await asyncio.sleep(0.1)
        streamer.stop_event.set()
        await task

    asyncio.run(drive())

    assert streamer.echo_gate.played == client.sent
    assert b"".join(streamer.echo_gate.played) == b"\xab\xcd" * 2000


def test_no_audio_send_site_bypasses_the_playback_funnel():
    """Structural guard: `_send_loop` must not send audio directly. Every
    outbound-audio path goes through `_broadcast_audio` so none can be added
    later that forgets `note_playback` -- a hole in the reference signal
    means real echo correlates against nothing and is passed through as user
    speech, which is the failure this whole slice exists to prevent."""
    import inspect

    source = inspect.getsource(WebSocketStreamer._send_loop)
    assert "client.send(data)" not in source
    assert source.count("_broadcast_audio") == 4


def test_reset_fires_when_the_last_client_disconnects():
    streamer = _make_streamer()
    streamer.echo_gate = _RecordingGate()
    streamer.should_listen.set()

    asyncio.run(streamer._handle_client(_FakeClient([])))

    assert streamer.echo_gate.resets == 1


def test_reset_failure_cannot_break_disconnect():
    """Fail-open at the wiring layer: a gate that raises on reset must not
    stop the session from ending cleanly."""

    class _ExplodingGate(_RecordingGate):
        def reset(self):
            raise RuntimeError("boom")

    streamer = _make_streamer()
    streamer.echo_gate = _ExplodingGate()
    streamer.should_listen.set()

    asyncio.run(streamer._handle_client(_FakeClient([])))

    assert _drain(streamer.input_queue)  # SESSION_END still enqueued


def test_feed_failure_passes_audio_through():
    """Fail-open at the wiring layer: a gate whose feed raises must not
    silence the user."""

    class _ExplodingGate(_RecordingGate):
        def feed(self, pcm):
            raise RuntimeError("boom")

    streamer = _make_streamer()
    streamer.echo_gate = _ExplodingGate()

    assert streamer._echo_pass(b"\x01\x02" * 512) is True


def test_note_playback_failure_still_sends_audio():
    class _ExplodingGate(_RecordingGate):
        def note_playback(self, pcm):
            raise RuntimeError("boom")

    streamer = _make_streamer()
    streamer.echo_gate = _ExplodingGate()
    client = _FakeClient()
    streamer.clients.add(client)

    asyncio.run(streamer._broadcast_audio(b"\x05\x06" * 100))

    assert client.sent == [b"\x05\x06" * 100]


# ── Origin allowlist (VOICE_WS_ORIGINS) ──────────────────────────────────
#
# A WebSocket is exempt from the same-origin policy, so any page in any browser
# that can reach the port may open this socket and become a full screen. A VPN
# does not help: the request originates inside the perimeter, from the user's
# own browser. The allowlist is the control that does help.
#
# Unset must stay byte-for-byte the old behaviour -- an upgrade cannot break a
# working install by silently locking every client out.


def test_unset_applies_no_restriction():
    assert _parse_ws_origins(None) is None


def test_blank_is_treated_as_unset():
    assert _parse_ws_origins("") is None
    assert _parse_ws_origins("   ") is None
    assert _parse_ws_origins(" , , ") is None


def test_single_origin():
    assert _parse_ws_origins("https://box.tail1234.ts.net") == ["https://box.tail1234.ts.net"]


def test_multiple_origins_and_whitespace_tolerated():
    assert _parse_ws_origins(" https://a.example , https://b.example ") == [
        "https://a.example",
        "https://b.example",
    ]


def test_dash_entry_admits_clients_that_send_no_origin_header():
    """`websockets` spells "request carried no Origin header" as None. Every
    non-browser client is in that bucket, so an allowlist without `-` locks out
    scripts, health probes and websocat -- the trap this entry exists to avoid."""
    assert _parse_ws_origins("-") == [None]
    assert _parse_ws_origins("https://a.example,-") == ["https://a.example", None]


def test_parsed_origins_are_accepted_by_the_installed_websockets():
    """Guards the assumption the whole slice rests on: that this websockets
    version takes `origins` and accepts None inside the sequence."""
    import inspect

    import websockets

    sig = inspect.signature(websockets.serve)
    assert "origins" in sig.parameters or "kwargs" in sig.parameters


# ── Origin fail-safe default (VOICE_WS_ORIGINS unset) ────────────────────
#
# Unset used to mean "no restriction at all". The new default: accept
# no-Origin clients (native) and Origins whose hostname matches the
# connected Host header (any port on this box, a LAN IP, localhost, or a
# tailnet name); reject everything else. `websockets.serve(origins=...)`
# can't express this Host-relative comparison, so it's a standalone pure
# function reached from `_run_server` via `process_request`.


def test_same_host_no_origin_header_is_allowed():
    """Native clients (scripts, health probes, websocat) send no Origin
    header at all."""
    assert _origin_allowed_same_host(None, "box:8765") is True


def test_same_host_matching_hostname_is_allowed():
    assert _origin_allowed_same_host("http://box:8770", "box:8765") is True


def test_same_host_matching_tailnet_hostname_is_allowed():
    assert _origin_allowed_same_host("https://box.tail1234.ts.net:8770", "box.tail1234.ts.net:8765") is True


def test_same_host_different_hostname_is_rejected():
    assert _origin_allowed_same_host("http://evil.example", "box:8765") is False


def test_same_host_missing_host_header_is_rejected():
    """An Origin was sent but there's no Host to compare it to -- fail closed."""
    assert _origin_allowed_same_host("http://box", None) is False


def test_same_host_malformed_origin_is_rejected():
    assert _origin_allowed_same_host("not a url", "box:8765") is False
    assert _origin_allowed_same_host("http://[::1", "box:8765") is False


def _fake_serve_capturing(calls):
    async def fake_serve(handler, host, port, **kwargs):
        calls.append(kwargs)

        class _FakeServer:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        return _FakeServer()

    return fake_serve


def test_run_server_installs_same_host_process_request_when_unset(monkeypatch):
    """When `VOICE_WS_ORIGINS` is unset, `_run_server` must wire the
    same-host check into `websockets.serve` -- the env var's own explicit
    override semantics (an allowlist, or `-`) must stay untouched when it IS
    set, so this only applies to the unset default."""
    monkeypatch.delenv("VOICE_WS_ORIGINS", raising=False)
    monkeypatch.delenv("VOICE_WS_CERTFILE", raising=False)
    monkeypatch.delenv("VOICE_WS_KEYFILE", raising=False)
    calls = []
    import websockets

    monkeypatch.setattr(websockets, "serve", _fake_serve_capturing(calls))

    streamer = _make_streamer()
    streamer.stop_event.set()  # exit the run loop immediately after startup
    asyncio.run(streamer._run_server())

    assert len(calls) == 1
    assert "origins" not in calls[0]  # env unset -> no static allowlist
    assert calls[0]["process_request"].__func__ is WebSocketStreamer._check_same_host_origin


def test_run_server_leaves_process_request_unset_when_origins_configured(monkeypatch):
    monkeypatch.setenv("VOICE_WS_ORIGINS", "https://box.example")
    monkeypatch.delenv("VOICE_WS_CERTFILE", raising=False)
    monkeypatch.delenv("VOICE_WS_KEYFILE", raising=False)
    calls = []
    import websockets

    monkeypatch.setattr(websockets, "serve", _fake_serve_capturing(calls))

    streamer = _make_streamer()
    streamer.stop_event.set()
    asyncio.run(streamer._run_server())

    assert calls[0]["origins"] == ["https://box.example"]
    assert "process_request" not in calls[0]


# ── max_size (ruling #17) ──────────────────────────────────────────────


def test_run_server_passes_explicit_max_size(monkeypatch):
    """The library default (1 MiB) kills a connection before the app-level
    2 MiB camera-frame check ever runs -- both listeners need an explicit,
    larger cap."""
    monkeypatch.delenv("VOICE_WS_ORIGINS", raising=False)
    monkeypatch.delenv("VOICE_WS_CERTFILE", raising=False)
    monkeypatch.delenv("VOICE_WS_KEYFILE", raising=False)
    calls = []
    import websockets

    monkeypatch.setattr(websockets, "serve", _fake_serve_capturing(calls))

    streamer = _make_streamer()
    streamer.stop_event.set()
    asyncio.run(streamer._run_server())

    assert calls[0]["max_size"] == 4 * 1024 * 1024


# ── slow-receiver broadcast timeout (ruling #7) ───────────────────────────


class _HangingClient:
    """A client whose `send()` never resolves on its own -- stands in for a
    dead peer or a wedged NAT that would otherwise hold a broadcast's
    `gather` open forever, backing up delivery to every OTHER client behind
    it in the fan-out."""

    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, data):
        await asyncio.sleep(10)
        self.sent.append(data)  # pragma: no cover - never reached within the test timeout

    async def close(self):
        self.closed = True


def test_slow_client_is_dropped_and_others_still_receive():
    streamer = _make_streamer()
    streamer._BROADCAST_SEND_TIMEOUT_S = 0.05
    slow = _HangingClient()
    fast = _FakeClient()
    streamer.clients.add(slow)
    streamer.clients.add(fast)

    asyncio.run(streamer._broadcast_audio(b"\x01\x02" * 100))

    assert slow not in streamer.clients
    assert slow.closed is True
    assert fast in streamer.clients
    assert fast.sent == [b"\x01\x02" * 100]


def test_fast_clients_unaffected_when_no_one_is_slow():
    streamer = _make_streamer()
    streamer._BROADCAST_SEND_TIMEOUT_S = 0.05
    a, b = _FakeClient(), _FakeClient()
    streamer.clients.add(a)
    streamer.clients.add(b)

    asyncio.run(streamer._broadcast_audio(b"\xaa\xbb"))

    assert a.sent == [b"\xaa\xbb"]
    assert b.sent == [b"\xaa\xbb"]
    assert streamer.clients == {a, b}


# ── _write_camera_frame / _write_screen_frame ───────────────────────────
#
# Both write to real tmpfs paths by default (_CAMERA_FRAME_PATH,
# _SCREEN_FRAME_PATH), so every test here redirects those module-level
# constants to a pytest tmp_path before calling the method.


class _FakeClock:
    """Mutable stand-in for time.monotonic so the rate-limit is driven by
    value, not real sleeping."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_camera_frame_valid_write_lands(monkeypatch, tmp_path):
    streamer = _make_streamer()
    cam_path = tmp_path / "cam.jpg"
    monkeypatch.setattr(websocket_streamer, "_CAMERA_FRAME_PATH", str(cam_path))

    raw = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    streamer._write_camera_frame(base64.b64encode(raw).decode())

    assert cam_path.read_bytes() == raw


def test_camera_frame_rejects_non_string_data(monkeypatch, tmp_path):
    streamer = _make_streamer()
    cam_path = tmp_path / "cam.jpg"
    monkeypatch.setattr(websocket_streamer, "_CAMERA_FRAME_PATH", str(cam_path))

    for bad in (None, 123, {}, [b"x"]):
        streamer._write_camera_frame(bad)

    assert not cam_path.exists()


def test_camera_frame_rejects_invalid_base64(monkeypatch, tmp_path):
    streamer = _make_streamer()
    cam_path = tmp_path / "cam.jpg"
    monkeypatch.setattr(websocket_streamer, "_CAMERA_FRAME_PATH", str(cam_path))

    streamer._write_camera_frame("!!!not base64!!!")

    assert not cam_path.exists()


def test_camera_frame_rejects_oversize(monkeypatch, tmp_path):
    streamer = _make_streamer()
    cam_path = tmp_path / "cam.jpg"
    monkeypatch.setattr(websocket_streamer, "_CAMERA_FRAME_PATH", str(cam_path))

    oversize = base64.b64encode(b"\x00" * (websocket_streamer._CAMERA_MAX_BYTES + 1)).decode()
    streamer._write_camera_frame(oversize)

    assert not cam_path.exists()


def test_camera_frame_rejects_empty_decoded(monkeypatch, tmp_path):
    streamer = _make_streamer()
    cam_path = tmp_path / "cam.jpg"
    monkeypatch.setattr(websocket_streamer, "_CAMERA_FRAME_PATH", str(cam_path))

    streamer._write_camera_frame(base64.b64encode(b"").decode())

    assert not cam_path.exists()


def test_camera_frame_rate_limit_skips_then_allows_after_interval(monkeypatch, tmp_path):
    streamer = _make_streamer()
    cam_path = tmp_path / "cam.jpg"
    monkeypatch.setattr(websocket_streamer, "_CAMERA_FRAME_PATH", str(cam_path))
    clock = _FakeClock(100.0)
    monkeypatch.setattr(websocket_streamer.time, "monotonic", clock)

    streamer._write_camera_frame(base64.b64encode(b"frame-one").decode())
    assert cam_path.read_bytes() == b"frame-one"

    # Immediate second call, clock unchanged: within the 1.0s window, skipped.
    streamer._write_camera_frame(base64.b64encode(b"frame-two").decode())
    assert cam_path.read_bytes() == b"frame-one"

    # Advance the clock past the interval: the next write goes through.
    clock.t += websocket_streamer._CAMERA_MIN_INTERVAL_S + 0.1
    streamer._write_camera_frame(base64.b64encode(b"frame-two").decode())
    assert cam_path.read_bytes() == b"frame-two"


def test_screen_frame_writes_independently_of_camera(monkeypatch, tmp_path):
    streamer = _make_streamer()
    cam_path = tmp_path / "cam.jpg"
    screen_path = tmp_path / "screen.jpg"
    monkeypatch.setattr(websocket_streamer, "_CAMERA_FRAME_PATH", str(cam_path))
    monkeypatch.setattr(websocket_streamer, "_SCREEN_FRAME_PATH", str(screen_path))
    clock = _FakeClock(200.0)
    monkeypatch.setattr(websocket_streamer.time, "monotonic", clock)

    # Camera write first, to arm the camera rate limit at clock=200.
    streamer._write_camera_frame(base64.b64encode(b"cam-frame").decode())
    assert cam_path.read_bytes() == b"cam-frame"

    # Same clock tick: screen write is unaffected by the camera's rate
    # limit -- it tracks _last_screen_write, not _last_cam_write.
    streamer._write_screen_frame(base64.b64encode(b"screen-frame").decode())
    assert screen_path.read_bytes() == b"screen-frame"
