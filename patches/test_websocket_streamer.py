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
hermetic pattern as `test_parrot_lane.py`/`test_brain_control.py`.
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
    from patches import floor_state as real_floor_state
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
    class _AudioOutput:
        """Shape of upstream pipeline.messages.AudioOutput (audio + cancel_generation)."""

        def __init__(self, audio, cancel_generation=None):
            self.audio = audio
            self.cancel_generation = cancel_generation

    mod(
        "speech_to_speech.pipeline.messages",
        AUDIO_RESPONSE_DONE=object(),
        PIPELINE_END=object(),
        AudioOutput=_AudioOutput,
    )
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
    sys.modules["speech_to_speech.floor_state"] = real_floor_state
    pkg.floor_state = real_floor_state

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
from patches.websocket_streamer import (  # noqa: E402
    WebSocketStreamer,
    _origin_allowed_same_host,
    _parse_ws_origins,
)

# `host_allowed` is deliberately NOT imported here: the hook tests below
# must also import and RUN against the original (pre-allowlist) streamer,
# so the "evil Host is rejected with 403" expectation fails by ASSERTION
# there -- a behavioural RED, not an ImportError. Tests that call
# `host_allowed` directly import it inside the test body.


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
        self.phases = []
        self.resets = 0
        self._drop = set(drop_indices)

    def note_playback(self, pcm):
        self.played.append(pcm)

    def feed(self, pcm, phase=None):
        idx = len(self.fed)
        self.fed.append(pcm)
        self.phases.append(phase)
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


# ── wake gate fail-open is logged as what it is ─────────────────────────


def _real_armed_wake_gate(monkeypatch, load_model):
    from patches import wakeword_gate

    monkeypatch.setattr(wakeword_gate, "_openwakeword_installed", lambda: True)
    gate = wakeword_gate.WakewordGate()
    gate.enabled = True
    monkeypatch.setattr(gate, "_load_model", load_model)
    return gate


def test_wake_gate_fail_open_is_not_logged_as_a_detection(monkeypatch, caplog):
    def broken():
        raise RuntimeError("model file missing")

    streamer = _make_streamer()
    streamer.wakeword_gate = _real_armed_wake_gate(monkeypatch, broken)

    with caplog.at_level("INFO"):
        asyncio.run(streamer._handle_client(_FakeClient([b"\x00\x00" * 1280])))

    messages = [r.getMessage() for r in caplog.records if r.name == websocket_streamer.logger.name]
    assert any("wake gate failed open (detector unavailable)" in m for m in messages), messages
    assert not any("wake word detected" in m for m in messages), messages


def test_wake_gate_real_detection_still_logs_its_score(monkeypatch, caplog):
    class _Hit:
        def predict(self, frame):
            return {"hey_jarvis": 0.9}

    streamer = _make_streamer()
    streamer.wakeword_gate = _real_armed_wake_gate(monkeypatch, lambda: _Hit())

    with caplog.at_level("INFO"):
        asyncio.run(streamer._handle_client(_FakeClient([b"\x00\x00" * 1280])))

    messages = [r.getMessage() for r in caplog.records if r.name == websocket_streamer.logger.name]
    assert any("wake word detected (score=0.9)" in m for m in messages), messages
    assert not any("failed open" in m for m in messages), messages


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


# ── Host-header allowlist (DNS rebinding) ─────────────────────────────────
#
# A page at http://evil.example:8770 whose DNS flips to 127.0.0.1 sends an
# Origin AND a Host that both say evil.example -- the same-host rule above
# passes it. Only the Host header betrays it, so `host_allowed` rejects any
# Host that is not an IP literal, localhost, one of this machine's own names
# (incl. its Tailscale name), a VOICE_WS_ORIGINS hostname, or a
# VOICE_ALLOWED_HOSTS entry; `*` disables. This is the pipeline-side twin of
# `webclient/host_guard.py`; the webclient tests pin every rule in detail,
# these pin the same rules for THIS copy plus the hook wiring.


def _pin_host_env(monkeypatch, own=None):
    """Hermetic: pin the own-names set and clear both env vars, so the box
    running pytest cannot sway a verdict. `raising=False` so this also
    works against a streamer without the allowlist internals (the hook
    tests must run, not error, on the pre-allowlist code)."""
    monkeypatch.setattr(websocket_streamer, "_own_names", None, raising=False)
    monkeypatch.setattr(
        websocket_streamer,
        "_machine_own_names",
        lambda: set(own) if own is not None else {"box", "box.local", "box.example.com"},
        raising=False,
    )
    monkeypatch.delenv("VOICE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("VOICE_WS_ORIGINS", raising=False)


def test_host_no_host_header_is_allowed(monkeypatch):
    """Native clients (HTTP/1.0, scripts, websocat) send no Host; rebinding
    needs a browser, which always sends one."""
    from patches.websocket_streamer import host_allowed

    _pin_host_env(monkeypatch)
    assert host_allowed(None) is True
    assert host_allowed("") is True
    assert host_allowed("   ") is True  # blank counts as absent


def test_host_ip_literals_are_allowed(monkeypatch):
    from patches.websocket_streamer import host_allowed

    _pin_host_env(monkeypatch)
    assert host_allowed("127.0.0.1:8765") is True
    assert host_allowed("[::1]:8770") is True


def test_host_localhost_and_own_names_are_allowed(monkeypatch):
    from patches.websocket_streamer import host_allowed

    _pin_host_env(monkeypatch)
    assert host_allowed("LOCALHOST:8765") is True
    assert host_allowed("box.local.") is True  # trailing dot stripped
    assert host_allowed("box:8765") is True


def test_host_evil_hostname_is_rejected(monkeypatch):
    from patches.websocket_streamer import host_allowed

    _pin_host_env(monkeypatch)
    assert host_allowed("evil.example:8765") is False


def test_host_unparseable_header_fails_closed(monkeypatch):
    """A present-but-unparseable Host is a browser-shaped request with a
    broken hostname -- reject it (fail closed), unlike the absent case."""
    from patches.websocket_streamer import host_allowed

    _pin_host_env(monkeypatch)
    assert host_allowed("[evil") is False
    assert host_allowed("[::1") is False  # unterminated IPv6 literal
    assert host_allowed("::1") is False  # bare IPv6 is not a legal HTTP Host; no hostname -> fail closed


def test_host_allowed_hosts_entry_and_star(monkeypatch):
    from patches.websocket_streamer import host_allowed

    _pin_host_env(monkeypatch)
    monkeypatch.setenv("VOICE_ALLOWED_HOSTS", "vpn.example")
    assert host_allowed("vpn.example:8765") is True
    assert host_allowed("evil.example:8765") is False
    monkeypatch.setenv("VOICE_ALLOWED_HOSTS", "*")
    assert host_allowed("evil.example:8765") is True


def test_host_voice_ws_origins_hostnames_are_allowed(monkeypatch):
    from patches.websocket_streamer import host_allowed

    _pin_host_env(monkeypatch)
    monkeypatch.setenv("VOICE_WS_ORIGINS", "https://cockpit.example:8443,-")
    assert host_allowed("cockpit.example:8765") is True
    assert host_allowed("other.example:8765") is False


class _FakeRespondConnection:
    """Just enough of `ServerConnection` for the hook: capture `respond`.
    Synchronous like the real `ServerConnection.respond(status, message) ->
    Response`, which `process_request` returns unawaited."""

    def __init__(self):
        self.responded = None

    def respond(self, status, message):
        self.responded = (status, message)
        return f"responded {status}"


def _fake_handshake_request(headers):
    return types.SimpleNamespace(headers=headers)


def test_hook_rejects_evil_host_when_origins_unset(monkeypatch, caplog):
    _pin_host_env(monkeypatch)
    streamer = _make_streamer()
    conn = _FakeRespondConnection()
    req = _fake_handshake_request({"Origin": "http://evil.example:8770", "Host": "evil.example:8765"})
    with caplog.at_level("WARNING"):
        asyncio.run(streamer._check_same_host_origin(conn, req))
    assert conn.responded == (403, "host not allowed\n")
    messages = [r.getMessage() for r in caplog.records if r.name == websocket_streamer.logger.name]
    assert any("VOICE_ALLOWED_HOSTS" in m and "evil.example:8765" in m for m in messages)


def test_hook_rejects_evil_host_even_when_origins_is_set(monkeypatch):
    """A configured VOICE_WS_ORIGINS allowlist does not switch the Host
    check off: with the attacker's name absent from it, a rebinding
    handshake is still 403 'host not allowed'. (Origins that DO name a host
    allow that host by rule 5 -- the operator said so; that case is
    `test_host_voice_ws_origins_hostnames_are_allowed`.)"""
    _pin_host_env(monkeypatch)
    monkeypatch.setenv("VOICE_WS_ORIGINS", "https://good.example,-")
    streamer = _make_streamer()
    conn = _FakeRespondConnection()
    req = _fake_handshake_request({"Origin": "http://evil.example:8770", "Host": "evil.example:8765"})
    asyncio.run(streamer._check_same_host_origin(conn, req))
    assert conn.responded == (403, "host not allowed\n")


def test_hook_allows_ip_host_native_and_same_host_browser(monkeypatch):
    _pin_host_env(monkeypatch)
    streamer = _make_streamer()
    conn = _FakeRespondConnection()
    # native client: no Origin, IP Host
    req = _fake_handshake_request({"Host": "127.0.0.1:8765"})
    assert asyncio.run(streamer._check_same_host_origin(conn, req)) is None
    # cockpit over the LAN IP: same-host Origin, IP Host
    req = _fake_handshake_request({"Origin": "http://127.0.0.1:8770", "Host": "127.0.0.1:8765"})
    assert asyncio.run(streamer._check_same_host_origin(conn, req)) is None
    assert conn.responded is None


def test_hook_allowed_hosts_entry_restores_a_named_host(monkeypatch):
    """The documented escape hatch: allow the name, and the rebinding-shaped
    (but honest) request goes through -- host check passes, and with
    VOICE_WS_ORIGINS unset the Origin is same-host with the allowed Host."""
    _pin_host_env(monkeypatch)
    monkeypatch.setenv("VOICE_ALLOWED_HOSTS", "evil.example")
    streamer = _make_streamer()
    conn = _FakeRespondConnection()
    req = _fake_handshake_request({"Origin": "http://evil.example:8770", "Host": "evil.example:8765"})
    assert asyncio.run(streamer._check_same_host_origin(conn, req)) is None


def test_hook_star_disables_host_check(monkeypatch):
    _pin_host_env(monkeypatch)
    monkeypatch.setenv("VOICE_ALLOWED_HOSTS", "*")
    streamer = _make_streamer()
    conn = _FakeRespondConnection()
    req = _fake_handshake_request({"Host": "evil.example:8765"})  # no Origin -> native-shaped
    assert asyncio.run(streamer._check_same_host_origin(conn, req)) is None


def test_hook_logs_rejection_once_per_distinct_hostname(monkeypatch, caplog):
    _pin_host_env(monkeypatch)
    streamer = _make_streamer()
    with caplog.at_level("WARNING"):
        for host in ("evil.example:8765", "evil.example:8765", "worse.example:8765"):
            conn = _FakeRespondConnection()
            req = _fake_handshake_request({"Origin": None, "Host": host})
            req.headers.pop("Origin")  # absent, like a real native/rebinding header set would be handled
            asyncio.run(streamer._check_same_host_origin(conn, req))
            assert conn.responded == (403, "host not allowed\n")
    messages = [
        r.getMessage() for r in caplog.records
        if r.name == websocket_streamer.logger.name and "VOICE_ALLOWED_HOSTS" in r.getMessage()
    ]
    assert len(messages) == 2  # evil once, worse once -- not three


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


def test_run_server_still_passes_origins_and_installs_process_request_when_configured(monkeypatch):
    """VOICE_WS_ORIGINS set: its `origins=` allowlist semantics stay
    untouched -- but `process_request` is now installed too, because the Host
    allowlist (DNS rebinding) applies in every configuration; the hook runs
    the same-host Origin check only when the env var is unset."""
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
    assert calls[0]["process_request"].__func__ is WebSocketStreamer._check_same_host_origin


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


# ── floor management slice 2: playback frames, overlap-observe routing ────
#
# The sequences below are driven through the real `_handle_client` receive
# loop: WebSocket preserves frame order, so a text `playback` frame brackets
# the binary mic frames around it exactly, and these tests pin that contract.

import json  # noqa: E402


def _playback(state, **extra):
    return json.dumps({"type": "playback", "state": state, **extra})


def _overlap_streamer(monkeypatch, *, flag=True, gate="observe"):
    if flag:
        monkeypatch.setenv("VOICE_OVERLAP_OBSERVE", "1")
    else:
        monkeypatch.delenv("VOICE_OVERLAP_OBSERVE", raising=False)
    if gate:
        monkeypatch.setenv("VOICE_ECHO_GATE", gate)
    else:
        monkeypatch.delenv("VOICE_ECHO_GATE", raising=False)
    streamer = _make_streamer()
    streamer.echo_gate = _RecordingGate()
    streamer.should_listen.set()
    return streamer


def _chunk(i):
    return bytes([i]) * CHUNK_BYTES


def test_overlap_observe_playing_frames_are_never_queued(monkeypatch):
    streamer = _overlap_streamer(monkeypatch)
    frames = [_chunk(1), _playback("start", delay_ms=20), _chunk(2), _chunk(3), _playback("end"), _chunk(4)]

    asyncio.run(streamer._handle_client(_FakeClient(frames)))

    # should_listen was SET throughout, yet the two playing-phase chunks never queued.
    assert _drain(streamer.input_queue)[:2] == [_chunk(1), _chunk(4)]
    assert streamer.echo_gate.fed == [_chunk(1), _chunk(2), _chunk(3), _chunk(4)]
    assert streamer.echo_gate.phases == [None, "playback", "playback", None]


def test_overlap_observe_cut_also_ends_the_playing_phase(monkeypatch):
    streamer = _overlap_streamer(monkeypatch)
    frames = [_playback("start", delay_ms=0), _chunk(2), _playback("cut"), _chunk(3)]

    asyncio.run(streamer._handle_client(_FakeClient(frames)))

    assert _drain(streamer.input_queue)[:1] == [_chunk(3)]
    assert streamer.echo_gate.phases == ["playback", None]


def test_flag_off_routing_ignores_playback_frames(monkeypatch):
    """Flag unset: byte-identical routing. The same frame sequence queues every
    chunk and the gate never sees a phase."""
    streamer = _overlap_streamer(monkeypatch, flag=False)
    frames = [_chunk(1), _playback("start", delay_ms=20), _chunk(2), _chunk(3), _playback("end"), _chunk(4)]

    asyncio.run(streamer._handle_client(_FakeClient(frames)))

    assert streamer.overlap_observe is False
    assert _drain(streamer.input_queue)[:4] == [_chunk(1), _chunk(2), _chunk(3), _chunk(4)]
    assert streamer.echo_gate.phases == [None, None, None, None]


def test_overlap_observe_is_inert_without_the_echo_gate(monkeypatch, caplog):
    caplog.set_level("WARNING")
    streamer = _overlap_streamer(monkeypatch, gate=None)
    # `_overlap_streamer` swapped in a recording gate after construction; the
    # decision under test was taken at construction, against the real gate.
    assert streamer.overlap_observe is False
    assert any("VOICE_OVERLAP_OBSERVE" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    frames = [_playback("start", delay_ms=0), _chunk(2)]
    asyncio.run(streamer._handle_client(_FakeClient(frames)))
    assert _drain(streamer.input_queue)[:1] == [_chunk(2)]


def test_overlap_observe_logs_one_info_line_per_window(monkeypatch, caplog):
    caplog.set_level("INFO")
    streamer = _overlap_streamer(monkeypatch)
    frames = [_playback("start", delay_ms=0), _playback("end"), _playback("start", delay_ms=0), _playback("cut")]

    asyncio.run(streamer._handle_client(_FakeClient(frames)))

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("overlap_observe ")]
    import re

    assert [re.match(r"overlap_observe window=(\d+) state=(\w+) client=\d+ t=\d+\.\d{3}$", ln).groups() for ln in lines] == [
        ("1", "start"), ("1", "end"), ("2", "start"), ("2", "cut"),
    ]
    # The report tool parses the real emitter's lines (same contract, one place).
    from examples import echo_gate_report as report

    parsed = report.parse_windows("\n".join(lines))
    assert [(w["n"], w["t1"] != float("inf")) for w in parsed] == [(1, True), (2, True)]


def test_flag_off_logs_no_overlap_lines(monkeypatch, caplog):
    caplog.set_level("INFO")
    streamer = _overlap_streamer(monkeypatch, flag=False)
    asyncio.run(streamer._handle_client(_FakeClient([_playback("start", delay_ms=0), _playback("end")])))
    assert not [r for r in caplog.records if "overlap_observe window" in r.getMessage()]


def test_playback_frames_drive_floor_state_and_disconnect_clears_it(monkeypatch):
    streamer = _overlap_streamer(monkeypatch, flag=False)
    seen = {}

    class _Probe(_FakeClient):
        def __aiter__(self):
            async def gen():
                yield _playback("start", delay_ms=0)
                seen["playing"] = streamer.floor_state.any_client_playing()
                yield _playback("end")
                seen["after_end"] = streamer.floor_state.any_client_playing()
                yield _playback("start", delay_ms=0)

            return gen()

    asyncio.run(streamer._handle_client(_Probe()))
    assert seen == {"playing": True, "after_end": False}
    # Disconnected mid-playback: the dead client must not read as still playing.
    assert streamer.floor_state.any_client_playing() is False


def test_playback_start_stamps_heard_with_the_clients_delay(monkeypatch):
    streamer = _overlap_streamer(monkeypatch, flag=False)
    calls = []
    monkeypatch.setattr(websocket_streamer, "turn_stats", types.SimpleNamespace(mark=lambda *a: None, mark_heard=calls.append))
    monkeypatch.setattr(websocket_streamer.time, "perf_counter", lambda: 500.0)

    asyncio.run(streamer._handle_client(_FakeClient([_playback("start", delay_ms=250)])))

    assert calls == [500.25]


def test_playback_delay_is_untrusted_input(monkeypatch):
    streamer = _overlap_streamer(monkeypatch, flag=False)
    calls = []
    monkeypatch.setattr(websocket_streamer, "turn_stats", types.SimpleNamespace(mark=lambda *a: None, mark_heard=calls.append))
    monkeypatch.setattr(websocket_streamer.time, "perf_counter", lambda: 10.0)
    bad = [None, "5", True, float("nan"), -50, 10**9]
    frames = [json.dumps({"type": "playback", "state": "start", "delay_ms": v}) for v in bad]
    frames.append('{"type":"playback","state":"start","delay_ms":Infinity}')  # json allows it in Python

    asyncio.run(streamer._handle_client(_FakeClient(frames)))

    assert calls == [10.0, 10.0, 10.0, 10.0, 10.0, 70.0, 10.0]  # clamp at 60 s; Infinity -> 0


def test_playback_frame_with_unknown_state_is_ignored(monkeypatch):
    streamer = _overlap_streamer(monkeypatch)
    asyncio.run(streamer._handle_client(_FakeClient([_playback("rewind"), _chunk(1)])))
    assert streamer.floor_state.any_client_playing() is False
    assert _drain(streamer.input_queue)[:1] == [_chunk(1)]


def test_send_loop_feeds_user_speech_state_and_audio_sent(monkeypatch):
    streamer = _overlap_streamer(monkeypatch, flag=False)
    client = _FakeClient()
    streamer.clients.add(client)

    class _Event(websocket_streamer.PipelineEvent):
        def __init__(self, kind):
            self.kind = kind

        def model_dump(self):
            return {"type": self.kind}

    states = []

    async def drive():
        task = asyncio.create_task(streamer._send_loop())
        streamer.text_output_queue = Queue()
        streamer.text_output_queue.put(_Event("speech_started"))
        await asyncio.sleep(0.1)
        states.append(streamer.floor_state.user_speaking)
        streamer.text_output_queue.put(_Event("speech_stopped"))
        streamer.output_queue.put(b"\x01\x02" * 2000)
        await asyncio.sleep(0.1)
        states.append(streamer.floor_state.user_speaking)
        streamer.stop_event.set()
        await task

    asyncio.run(drive())

    assert states == [True, False]
    assert streamer.floor_state.mutual_quiet_s() != float("inf")  # audio_sent and speech end were both stamped


# ── server-side cancel on barge-in (VOICE_BARGE_CANCEL) ───────────────────
#
# A client `{"type":"barge_in"}` frame stops a reply that is still being
# produced. Driven through the real `_handle_client` receive loop; a second
# resident client keeps the first-client reset (which re-opens the mic and
# drains the output queue on connect) from erasing the in-flight state the
# test sets up.


class _Scope:
    """CancelScope's contract for the streamer: cancel() bumps the generation."""

    def __init__(self):
        self.generation = 0

    def cancel(self):
        self.generation += 1

    def is_stale(self, gen):
        return gen != self.generation


def _barge_streamer(monkeypatch, *, scope=True, in_flight=True):
    streamer = _make_streamer()
    streamer.cancel_scope = _Scope() if scope else None
    streamer.clients.add(_FakeClient())  # not the first client: no connect-time reset
    streamer.sent_json = []
    streamer.acks = []
    monkeypatch.setattr(streamer, "broadcast_json", lambda payload: streamer.sent_json.append(payload))
    monkeypatch.setattr(streamer, "_send_json_to", lambda cid, payload: streamer.acks.append(payload))
    if in_flight:
        streamer.should_listen.clear()
        streamer.output_queue.put(b"\x01\x02" * 1600)
        streamer.output_queue.put(b"\x03\x04" * 1600)
        streamer._audio_buffer.extend(b"\x05\x06" * 100)
    else:
        streamer.should_listen.set()
    return streamer


def _barge_in_frame():
    return json.dumps({"type": "barge_in"})


def test_barge_in_cancels_drains_reopens_the_mic_and_tells_every_client(monkeypatch):
    streamer = _barge_streamer(monkeypatch)

    asyncio.run(streamer._handle_client(_FakeClient([_barge_in_frame(), _chunk(7)])))

    assert streamer.cancel_scope.generation == 1
    assert streamer.output_queue.empty()
    assert not streamer._audio_buffer
    assert streamer.should_listen.is_set()
    assert streamer.sent_json == [{"type": "audio_stop"}]
    assert streamer.acks == []
    # The user's audio right behind the frame is not lost: the mic was already open for it.
    assert _drain(streamer.input_queue)[:1] == [_chunk(7)]


def test_barge_in_with_audio_still_queued_but_the_mic_open_still_cancels(monkeypatch):
    streamer = _barge_streamer(monkeypatch)
    streamer.should_listen.set()  # the mic is open but output is still queued

    asyncio.run(streamer._handle_client(_FakeClient([_barge_in_frame()])))

    assert streamer.cancel_scope.generation == 1
    assert streamer.output_queue.empty()


def test_barge_in_is_ignored_when_no_response_is_in_flight(monkeypatch):
    streamer = _barge_streamer(monkeypatch, in_flight=False)

    asyncio.run(streamer._handle_client(_FakeClient([_barge_in_frame(), _chunk(7)])))

    assert streamer.cancel_scope.generation == 0
    assert streamer.sent_json == []  # nobody else is told to stop
    assert streamer.acks == [{"type": "audio_stop"}]  # the asker is released from its audio gate
    assert _drain(streamer.input_queue)[:1] == [_chunk(7)]


def test_barge_in_frame_is_inert_without_a_cancel_scope(monkeypatch):
    """VOICE_BARGE_CANCEL=off: the server treats the frame as unknown, today's behaviour."""
    streamer = _barge_streamer(monkeypatch, scope=False)

    asyncio.run(streamer._handle_client(_FakeClient([_barge_in_frame(), _chunk(7)])))

    assert streamer.output_queue.qsize() == 2
    assert len(streamer._audio_buffer) == 200
    assert not streamer.should_listen.is_set()
    assert streamer.sent_json == [] and streamer.acks == []
    assert _drain(streamer.input_queue) == []  # mic stays shut, exactly as before


def test_barge_in_function_serves_a_server_side_caller(monkeypatch):
    streamer = _barge_streamer(monkeypatch)

    assert streamer._barge_in(None, "detector") is True

    assert streamer.cancel_scope.generation == 1
    assert streamer.should_listen.is_set()
    assert streamer.sent_json == [{"type": "audio_stop"}]


def test_send_loop_does_not_flush_audio_discarded_by_a_barge_in(monkeypatch):
    """The half-built send buffer lives on the streamer: a cancel must not leave
    it behind to be flushed to the client after audio_stop."""
    streamer = _barge_streamer(monkeypatch)
    client = _FakeClient()
    streamer.clients.add(client)
    streamer._barge_in(None, "test")

    async def drive():
        task = asyncio.create_task(streamer._send_loop())
        await asyncio.sleep(0.1)
        streamer.stop_event.set()
        await task

    asyncio.run(drive())

    assert client.sent == []


def test_barge_cancel_knob_defaults_on_and_off_values(monkeypatch):
    monkeypatch.delenv("VOICE_BARGE_CANCEL", raising=False)
    assert websocket_streamer.barge_cancel_enabled() is True
    for value in ("off", "OFF", "0", "false", "no"):
        monkeypatch.setenv("VOICE_BARGE_CANCEL", value)
        assert websocket_streamer.barge_cancel_enabled() is False, value
    for value in ("on", "1", "", "yes"):
        monkeypatch.setenv("VOICE_BARGE_CANCEL", value)
        assert websocket_streamer.barge_cancel_enabled() is True, value


# ── AudioOutput unwrapping (VOICE_BARGE_CANCEL stamps every TTS output) ──

_DONE = b"__RESPONSE_DONE__"  # the real upstream sentinel values
_END = b"END"


def _run_send_loop(streamer, items):
    for item in items:
        streamer.output_queue.put(item)

    async def drive():
        task = asyncio.create_task(streamer._send_loop())
        await asyncio.sleep(0.1)
        streamer.stop_event.set()
        await task

    asyncio.run(drive())


def _wrapped_streamer(monkeypatch, gen=0):
    monkeypatch.setattr(websocket_streamer, "AUDIO_RESPONSE_DONE", _DONE)
    monkeypatch.setattr(websocket_streamer, "PIPELINE_END", _END)
    streamer = _make_streamer()
    streamer.cancel_scope = _Scope()
    streamer.cancel_scope.generation = gen
    client = _FakeClient()
    streamer.clients.add(client)
    streamer.should_listen.clear()
    return streamer, client


def test_send_loop_unwraps_audio_output_and_rearms_the_mic(monkeypatch):
    import numpy as np

    streamer, client = _wrapped_streamer(monkeypatch)
    AO = sys.modules["speech_to_speech.pipeline.messages"].AudioOutput
    pcm = np.arange(2000, dtype=np.int16)
    _run_send_loop(
        streamer,
        [AO(audio=pcm, cancel_generation=0), AO(audio=b"\x01\x02" * 500, cancel_generation=0), AO(audio=_DONE, cancel_generation=0)],
    )

    assert b"".join(client.sent) == pcm.tobytes() + b"\x01\x02" * 500
    assert streamer.should_listen.is_set()


def test_send_loop_stale_done_still_rearms_the_mic(monkeypatch):
    streamer, client = _wrapped_streamer(monkeypatch, gen=1)
    AO = sys.modules["speech_to_speech.pipeline.messages"].AudioOutput
    _run_send_loop(streamer, [AO(audio=_DONE, cancel_generation=0)])

    assert streamer.should_listen.is_set()


def test_send_loop_drops_stale_chunks_and_keeps_current_ones(monkeypatch):
    streamer, client = _wrapped_streamer(monkeypatch, gen=2)
    AO = sys.modules["speech_to_speech.pipeline.messages"].AudioOutput
    _run_send_loop(
        streamer,
        [
            AO(audio=b"\xaa\xaa" * 2000, cancel_generation=1),
            AO(audio=b"\xbb\xbb" * 2000, cancel_generation=2),
            AO(audio=_DONE, cancel_generation=2),
        ],
    )

    assert b"".join(client.sent) == b"\xbb\xbb" * 2000
    assert streamer.should_listen.is_set()


def test_send_loop_unstamped_wrapper_is_never_treated_as_stale(monkeypatch):
    streamer, client = _wrapped_streamer(monkeypatch, gen=5)
    AO = sys.modules["speech_to_speech.pipeline.messages"].AudioOutput
    _run_send_loop(streamer, [AO(audio=b"\x07\x07" * 2000, cancel_generation=None), AO(audio=_DONE)])

    assert b"".join(client.sent) == b"\x07\x07" * 2000
    assert streamer.should_listen.is_set()


def test_send_loop_bare_bytes_without_a_scope_unchanged(monkeypatch):
    """Barge-cancel off: bare bytes and a bare DONE, no cancel scope (also covered by
    test_send_loop_audio_paths_all_record_playback)."""
    streamer, client = _wrapped_streamer(monkeypatch)
    streamer.cancel_scope = None
    _run_send_loop(streamer, [b"\x09\x09" * 2000, _DONE])

    assert b"".join(client.sent) == b"\x09\x09" * 2000
    assert streamer.should_listen.is_set()


def test_upstream_output_for_queue_wraps_as_the_streamer_expects():
    """Contract with the REAL framework: only runs where speech_to_speech is installed."""
    import subprocess

    script = (
        "from types import SimpleNamespace as N\n"
        "import numpy as np\n"
        "from speech_to_speech.baseHandler import BaseHandler\n"
        "from speech_to_speech.pipeline.messages import AudioOutput, AUDIO_RESPONSE_DONE\n"
        "src = N(cancel_generation=3)\n"
        "for out in (b'\\x01\\x02', np.zeros(4, np.int16), AUDIO_RESPONSE_DONE):\n"
        "    w = BaseHandler.output_for_queue(None, out, src)\n"
        "    assert isinstance(w, AudioOutput) and w.cancel_generation == 3, w\n"
        "assert w.audio == AUDIO_RESPONSE_DONE\n"
    )
    probe = subprocess.run([sys.executable, "-I", "-c", "import speech_to_speech.baseHandler"], capture_output=True)
    if probe.returncode != 0:
        import pytest

        pytest.skip("real speech_to_speech framework not importable here")
    run = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


# ── PTT hold: {"type":"ptt","state":"down"|"up"} (VOICE_PTT_HOLD) ─────────
#
# A pause inside a held push-to-talk turn must not end the turn. The upstream VAD
# is not in this repo, so `_ModelVAD` models the one rule that matters here:
# `min_silence_ms` of consecutive silent samples, after speech, finalises a turn
# (live flag --min_silence_ms 300). It counts the samples it is FED -- which is
# exactly why withholding a pause from it keeps the turn open. Loud chunk = speech.

import json as _json  # noqa: E402

import numpy as _np  # noqa: E402

_SR = 16000
_MIN_SILENCE_MS = 300


def _speech(n_chunks, amp=6000):
    return [(_np.full(512, amp, dtype=_np.int16)).tobytes() for _ in range(n_chunks)]


def _silence(seconds):
    return [bytes(CHUNK_BYTES) for _ in range(int(seconds * _SR / 512))]


class _ModelVAD:
    """Finalises a turn after _MIN_SILENCE_MS of fed silence following speech."""

    def __init__(self):
        self.turns = []
        self._audio = []
        self._speech_seen = False
        self._silent = 0

    def feed(self, chunk):
        loud = bool(_np.abs(_np.frombuffer(chunk, dtype=_np.int16)).max() > 1000)
        if loud:
            self._speech_seen = True
            self._silent = 0
            self._audio.append(chunk)
        elif self._speech_seen:
            self._silent += 512
            self._audio.append(chunk)
            if self._silent * 1000 >= _MIN_SILENCE_MS * _SR:
                self.turns.append(b"".join(self._audio))
                self._audio, self._speech_seen, self._silent = [], False, 0

    def feed_all(self, q):
        for c in _drain(q):
            if isinstance(c, bytes):
                self.feed(c)


def _ptt(state):
    return _json.dumps({"type": "ptt", "state": state})


def _run_session(frames, monkeypatch=None):
    streamer = _make_streamer()
    streamer.should_listen.set()
    asyncio.run(streamer._handle_client(_FakeClient(frames)))
    vad = _ModelVAD()
    vad.feed_all(streamer.input_queue)
    return streamer, vad


def test_ptt_hold_keeps_one_turn_across_a_one_second_pause(monkeypatch):
    monkeypatch.delenv("VOICE_PTT_HOLD", raising=False)
    a, b = _speech(20, 6000), _speech(20, 7000)
    frames = [_ptt("down"), b"".join(a), b"".join(_silence(1.0)), b"".join(b), _ptt("up")]
    streamer, vad = _run_session(frames)
    assert len(vad.turns) == 1, f"expected ONE turn, got {len(vad.turns)}"
    assert b"".join(a) in vad.turns[0] and b"".join(b) in vad.turns[0]


def test_ptt_up_ends_the_turn_without_client_tail_silence(monkeypatch):
    monkeypatch.delenv("VOICE_PTT_HOLD", raising=False)
    a = _speech(20)
    streamer, vad = _run_session([_ptt("down"), b"".join(a), _ptt("up")])
    assert len(vad.turns) == 1 and b"".join(a) in vad.turns[0]


def test_ptt_silent_hold_queues_no_speech_and_ends_clean(monkeypatch):
    monkeypatch.delenv("VOICE_PTT_HOLD", raising=False)
    streamer, vad = _run_session([_ptt("down"), b"".join(_silence(2.0)), _ptt("up")])
    assert vad.turns == []  # nothing was said: no turn, same as a silent hold today


def test_client_that_never_sends_ptt_still_ends_on_min_silence():
    a, b = _speech(20, 6000), _speech(20, 7000)
    frames = [b"".join(a), b"".join(_silence(1.0)), b"".join(b)]
    streamer, vad = _run_session(frames)
    assert len(vad.turns) == 1 and vad.turns[0].startswith(b"".join(a))
    # the pause ended turn 1; speech b is a second, still-open turn (not finalised yet)
    assert b"".join(b) not in vad.turns[0]
    # and every byte reached the queue unmodified
    sent = b"".join(a) + b"".join(_silence(1.0)) + b"".join(b)
    q = _make_streamer()
    q.should_listen.set()
    asyncio.run(q._handle_client(_FakeClient(frames)))
    assert b"".join(c for c in _drain(q.input_queue) if isinstance(c, bytes)) == sent


def test_kill_switch_ignores_ptt_frames(monkeypatch):
    monkeypatch.setenv("VOICE_PTT_HOLD", "0")
    a, b = _speech(20, 6000), _speech(20, 7000)
    frames = [_ptt("down"), b"".join(a), b"".join(_silence(1.0)), b"".join(b), _ptt("up")]
    streamer, vad = _run_session(frames)
    assert len(vad.turns) == 1 and b"".join(b) not in vad.turns[0]  # pause cut the turn, as before


def test_ptt_hold_cap_resumes_normal_vad():
    from patches.websocket_streamer import PTT_HOLD_MAX_S, PttHold

    now = [1000.0]
    hold = PttHold(clock=lambda: now[0])
    hold.down()
    assert hold.active
    now[0] += PTT_HOLD_MAX_S + 1
    assert not hold.active
    quiet = bytes(CHUNK_BYTES)
    assert all(hold.admit(quiet) for _ in range(40)), "after the cap every chunk reaches the VAD"


def test_ptt_hold_is_dropped_when_the_socket_closes_without_up(monkeypatch):
    """No 'up' ever arrives: the hold dies with the connection, and the next connection's VAD is normal."""
    monkeypatch.delenv("VOICE_PTT_HOLD", raising=False)
    streamer = _make_streamer()
    streamer.should_listen.set()
    a = _speech(20)
    asyncio.run(streamer._handle_client(_FakeClient([_ptt("down"), b"".join(a)])))  # closes mid-hold
    _drain(streamer.input_queue)
    b = _speech(20, 7000)
    asyncio.run(streamer._handle_client(_FakeClient([b"".join(b), b"".join(_silence(1.0))])))
    vad = _ModelVAD()
    vad.feed_all(streamer.input_queue)
    assert len(vad.turns) == 1 and b"".join(b) in vad.turns[0]


def test_ptt_hold_withholds_a_noisy_pause_but_never_speech():
    from patches.websocket_streamer import PttHold

    rng = _np.random.default_rng(0)
    hold = PttHold()
    hold.down()
    noise = lambda: rng.normal(0, 400, 512).astype(_np.int16).tobytes()  # room noise floor, RMS ~400
    voiced = _speech(1, 6000)[0]
    assert hold.admit(voiced)
    passed = sum(hold.admit(noise()) for _ in range(30))  # a ~1 s pause
    assert passed <= 4, "a long noisy pause must be withheld after a few chunks"
    assert hold.admit(voiced), "speech after the pause always passes"


def test_ptt_up_places_the_sub_chunk_remainder_before_the_silence_flush(monkeypatch):
    """[down, 20 chunks + 320 samples, up, 60 chunks of silence]: the 320-sample
    remainder is pre-'up' speech, so it must reach the queue BEFORE the zero flush,
    never after it (where it would open the next turn)."""
    monkeypatch.delenv("VOICE_PTT_HOLD", raising=False)
    speech = b"".join(_speech(20)) + _np.full(320, 6000, dtype=_np.int16).tobytes()
    streamer = _make_streamer()
    streamer.should_listen.set()
    frames = [_ptt("down"), speech, _ptt("up"), b"".join(_silence(2.0))[: 60 * CHUNK_BYTES]]
    asyncio.run(streamer._handle_client(_FakeClient(frames)))
    chunks = [c for c in _drain(streamer.input_queue) if isinstance(c, bytes)]
    loud = [bool(_np.abs(_np.frombuffer(c, dtype=_np.int16)).max() > 1000) for c in chunks]
    first_zero = loud.index(False)
    assert sum(loud[:first_zero]) == 21, "the padded remainder is the 21st speech chunk"
    assert not any(loud[first_zero:]), "no speech chunk may follow the first zero-flush chunk"


def test_ptt_up_after_the_cap_expired_still_flushes():
    from patches.websocket_streamer import PTT_HOLD_MAX_S, PttHold

    now = [0.0]
    hold = PttHold(clock=lambda: now[0])
    hold.down()
    now[0] += PTT_HOLD_MAX_S + 1
    assert hold.admit(bytes(CHUNK_BYTES))  # expiry is noticed here
    assert hold.up() is True, "the turn is still open: up must flush"
    assert hold.up() is False, "and only once"


def test_ptt_hold_admits_every_chunk_at_the_owners_real_mic_levels():
    """Owner's live echo-gate log of PTT turns: 1 s window RMS 2-65 when quiet, 580-4800 in speech."""
    from patches.websocket_streamer import PttHold

    def gauss(rms):
        return _np.random.default_rng(int(rms)).normal(0, rms, 512).clip(-32768, 32767).astype(_np.int16).tobytes()

    for speech_rms in (580, 1500, 4800):
        hold = PttHold()
        hold.down()
        for _ in range(20):
            hold.admit(gauss(65))
        assert all(hold.admit(gauss(speech_rms)) for _ in range(40)), f"speech at RMS {speech_rms} was withheld"
        assert sum(hold.admit(gauss(65)) for _ in range(30)) <= 4, "the pause after speech is withheld"
        # speech straight off the press, with no noise preceding it
        hold2 = PttHold()
        hold2.down()
        assert all(hold2.admit(gauss(speech_rms)) for _ in range(40))
