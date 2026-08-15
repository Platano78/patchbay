import asyncio
import base64
import json
import logging
import os
import ssl
import time
from queue import Empty, Queue
from threading import Event
from typing import Any, Callable

import numpy as np
from websockets.asyncio.server import ServerConnection

from speech_to_speech import phone_context
from speech_to_speech.pipeline.control import SESSION_END, PipelineControlMessage, is_control_message
from speech_to_speech.pipeline.events import PipelineEvent
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, PIPELINE_END
from speech_to_speech.pipeline.queue_types import AudioInItem, AudioOutItem, TextEventItem
from speech_to_speech.transcript_buffer import TranscriptBuffer
from speech_to_speech.turn_stats import turn_stats
from speech_to_speech.voice_clone import UploadManager
from speech_to_speech.wakeword_gate import WakewordGate

logger = logging.getLogger(__name__)

# Echo gate (default OFF, see echo_gate.py). Imported defensively: unlike the
# other patch modules this one is new, so a box whose patch pack predates it
# would otherwise fail to import the streamer entirely. Degrade to an inert
# gate instead -- the same fail-open rule the gate itself follows internally.
try:
    from speech_to_speech.echo_gate import EchoGate
except Exception:  # pragma: no cover - only on a box missing the module
    logger.warning("echo_gate module unavailable; running with echo gating permanently disabled")

    class EchoGate:  # type: ignore[no-redef]
        enabled = False

        def note_playback(self, pcm: bytes) -> None:
            pass

        def feed(self, pcm: bytes) -> bool:
            return True

        def reset(self) -> None:
            pass

        def state(self) -> str:
            return "off"


# Camera-vision: the webclient streams frames as {"type":"camera_frame","data":<b64 jpeg>};
# we write the latest to this tmpfs path for the `look` voice tool to read. Rate-limited +
# size-capped, atomic replace, best-effort (never fatal to the audio loop).
_CAMERA_FRAME_PATH = os.environ.get("VOICE_CAMERA_FRAME", "/dev/shm/voice_camera_frame.jpg")
_CAMERA_MIN_INTERVAL_S = 1.0
_CAMERA_MAX_BYTES = 2_000_000

# Screen-share vision: mirror of the camera path above, driven by {"type":"screen_frame",...}.
_SCREEN_FRAME_PATH = os.environ.get("VOICE_SCREEN_FRAME", "/dev/shm/voice_screen_frame.jpg")
_SCREEN_MIN_INTERVAL_S = 1.0
_SCREEN_MAX_BYTES = 2_000_000

# HTTPS/wss support: when the webclient is served over TLS (required for getUserMedia
# on non-localhost hosts), the browser dials wss:// for the audio socket too. Set both
# cert env vars to also start a TLS listener alongside the plain one; see
# patches/README.md "Remote access / HTTPS".
_WSS_CERTFILE = os.environ.get("VOICE_WS_CERTFILE")
_WSS_KEYFILE = os.environ.get("VOICE_WS_KEYFILE")
# Kept as a raw string at module level and parsed inside _run_server's TLS try-block
# so a typo'd port doesn't raise at import time -- same fail-soft rule as a bad cert.
_WSS_PORT_RAW = os.environ.get("VOICE_WSS_PORT", "8443")


def _origin_allowed_same_host(origin: str | None, host_header: str | None) -> bool:
    """Same-host default policy, used only when `VOICE_WS_ORIGINS` is unset.

    Accepts (a) requests with no Origin header at all (native clients: a
    script, a health probe, websocat -- none of them send one), and (b)
    Origins whose hostname matches the Host header's hostname, which covers
    the cockpit served from any port on this box, a LAN IP, `localhost`, or a
    tailnet name -- all of it is "this same box", just addressed differently.
    Everything else (a different host) is rejected: a page loaded from
    somewhere else must not be able to open this socket. Pure/testable;
    `websockets.serve(origins=...)` can't express this comparison itself
    because it only ever sees the Origin header, not Host.
    """
    if origin is None:
        return True
    from urllib.parse import urlsplit

    try:
        origin_host = urlsplit(origin).hostname
    except ValueError:
        return False
    if not origin_host or not host_header:
        return False
    try:
        request_host = urlsplit(f"//{host_header}").hostname
    except ValueError:
        return False
    return origin_host == request_host


def _parse_ws_origins(raw: str | None) -> list | None:
    """Parse VOICE_WS_ORIGINS into the list `websockets.serve(origins=...)` wants,
    or None to apply no Origin restriction at all.

    Why this exists: a WebSocket is exempt from the browser's same-origin policy,
    so ANY page loaded in ANY browser that can reach this port may open a socket
    to it and become a full screen -- live audio, transcripts, persona edits,
    brain switching. That is cross-site WebSocket hijacking, and no network
    perimeter stops it: the request originates inside the perimeter, from the
    user's own browser. A VPN authenticates devices, not the pages running on
    them.

    Unset (the default) passes no `origins` and behaves exactly as before, so
    upgrading cannot break a working install. The entry ``-`` maps to None,
    which is how `websockets` spells "a request that sent no Origin header at
    all" -- every non-browser client (a script, a health probe, websocat) is in
    that bucket, so an allowlist without ``-`` silently locks tooling out.
    """
    if raw is None:
        return None
    entries = [item.strip() for item in raw.split(",")]
    origins = [None if item == "-" else item for item in entries if item]
    return origins or None


class WebSocketStreamer:
    """
    Handles bidirectional audio streaming over WebSocket.

    Receives audio from clients and puts it in the input_queue.
    Sends audio from the output_queue to clients.
    Sends text messages (transcripts/tools) from text_output_queue to clients.
    """

    def __init__(
        self,
        stop_event: Event,
        input_queue: Queue[AudioInItem],
        output_queue: Queue[AudioOutItem],
        should_listen: Event,
        text_output_queue: Queue[TextEventItem] | None = None,
        host: str = "0.0.0.0",
        port: int = 8765,
        control_callback: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.stop_event = stop_event
        self.input_queue = input_queue  # clients -> VAD
        self.output_queue = output_queue  # TTS -> clients
        self.text_output_queue = text_output_queue  # Text messages -> clients
        self.should_listen = should_listen
        self.host = host
        self.port = port
        self.control_callback = control_callback  # config_get/config_set handler (brain + persona control)
        self.clients: set[ServerConnection] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: Any = None
        self.tls_server: Any = None
        self._last_cam_write = 0.0
        self._last_screen_write = 0.0
        self.wakeword_gate = WakewordGate()
        # Echo gate: scores incoming mic audio against the TTS we just sent so
        # barge-in can work. Default OFF -- with VOICE_ECHO_GATE unset this is
        # an inert pass-through and costs nothing on either path.
        self.echo_gate = EchoGate()
        # Custom voice cloning: chunk frames are buffered here (bytes only,
        # no model work) keyed by client identity; only `voice_clone_begin`
        # (validation) and `voice_clone_end` (assembled bytes + the actual
        # build) reach `control_callback`. See patches/README.md.
        self._voice_uploads = UploadManager()
        # Transcript replay: last N completed turns, replayed to a joining
        # client so a reconnect (screen lock, backgrounded tab, reload)
        # doesn't show an empty history rail. See patches/README.md.
        self._transcript_buffer = TranscriptBuffer()

    def run(self) -> None:
        """Run the WebSocket server (called from a thread)."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            self.loop.run_until_complete(self._run_server())
        except Exception as e:
            logger.error(f"WebSocket server error: {e}")
        finally:
            self.loop.close()

    async def _run_server(self) -> None:
        """Main async server loop."""
        import websockets

        logger.info(f"WebSocket server starting on ws://{self.host}:{self.port}")

        origins_raw = os.environ.get("VOICE_WS_ORIGINS")
        origins = _parse_ws_origins(origins_raw)
        if origins_raw is None:
            logger.warning(
                "WebSocket Origin default: no-Origin clients (native) and Origins matching "
                "the connected Host are accepted; anything else is rejected. Set "
                "VOICE_WS_ORIGINS to override (see README).",
            )
        else:
            logger.info("WebSocket Origin allowlist: %s", origins)

        # kwargs, not positional: `origins=None` and omitting the kwarg both
        # mean "no `websockets`-side restriction" (the same-host check below
        # runs instead via `process_request` when the env var is unset), but
        # once the env var IS set its allowlist semantics must be untouched.
        serve_kwargs: dict[str, Any] = {} if origins is None else {"origins": origins}
        if origins_raw is None:
            serve_kwargs["process_request"] = self._check_same_host_origin

        self.server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            max_size=4 * 1024 * 1024,
            **serve_kwargs,
        )

        if _WSS_CERTFILE and _WSS_KEYFILE:
            try:
                wss_port = int(_WSS_PORT_RAW)
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(_WSS_CERTFILE, _WSS_KEYFILE)
                self.tls_server = await websockets.serve(
                    self._handle_client,
                    self.host,
                    wss_port,
                    ssl=ctx,
                    max_size=4 * 1024 * 1024,
                    **serve_kwargs,  # same policy on both listeners
                )
                logger.info(f"WebSocket TLS listener on wss://{self.host}:{wss_port}")
            except Exception as e:
                logger.error(f"WebSocket TLS listener failed to start ({e}); continuing plain-only")
                self.tls_server = None

        logger.info("WebSocket server ready, waiting for connections...")

        # Start the sender task
        sender_task = asyncio.create_task(self._send_loop())

        # Wait until stop_event is set
        while not self.stop_event.is_set():
            await asyncio.sleep(0.1)

        # Cleanup
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass

        # Close all clients
        for client in list(self.clients):
            try:
                await client.close()
            except Exception:
                pass

        self.server.close()
        await self.server.wait_closed()
        if self.tls_server is not None:
            self.tls_server.close()
            await self.tls_server.wait_closed()
        logger.info("WebSocket server closed")

    async def _check_same_host_origin(self, connection: ServerConnection, request: Any) -> Any:
        """`process_request` hook: applied only when `VOICE_WS_ORIGINS` is
        unset (the `websockets`-native `origins=` allowlist handles the
        explicit-override case and can't express a Host-relative check
        itself). Rejects the handshake with 403 unless
        `_origin_allowed_same_host` accepts it; logs the rejection once, here,
        rather than per message."""
        origin = request.headers.get("Origin")
        host_header = request.headers.get("Host")
        if _origin_allowed_same_host(origin, host_header):
            return None
        logger.warning(
            "WebSocket connection rejected: Origin %r doesn't match Host %r. "
            "Set VOICE_WS_ORIGINS to allow it explicitly (see README).",
            origin,
            host_header,
        )
        return connection.respond(403, "origin not allowed\n")

    async def _handle_client(self, websocket: ServerConnection) -> None:
        """Handle a single WebSocket client connection."""
        client_id = id(websocket)
        logger.info(f"Client {client_id} connected")
        self.clients.add(websocket)
        recv_buffer = bytearray()

        # Enable listening when first client connects
        if len(self.clients) == 1:
            # Drain edge queues so stale data from a previous session doesn't
            # leak into the new one (SESSION_END may not have flushed everything).
            for q in (self.output_queue, self.text_output_queue):
                if q is not None:
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except Empty:
                            break
            self.should_listen.set()
            logger.debug("Listening enabled, edge queues drained (should_listen.set())")

            if self.wakeword_gate.enabled and not self.wakeword_gate.awake:
                try:
                    await websocket.send(
                        json.dumps({"type": "wakeword_state", "state": "asleep", "phrase": self.wakeword_gate.phrase})
                    )
                except Exception:
                    logger.debug("Client %s: wakeword asleep notice failed", client_id, exc_info=True)

        # Seed this joining client's history rail with the last N completed
        # turns (best-effort -- a send failure here can't break the join).
        replay = self._transcript_buffer.replay_payload()
        if replay is not None:
            try:
                await websocket.send(json.dumps(replay))
            except Exception:
                logger.debug("Client %s: history replay send failed", client_id, exc_info=True)

        try:
            logger.debug(f"Client {client_id}: Starting message receive loop")
            async for message in websocket:
                if isinstance(message, bytes):
                    logger.debug(f"Client {client_id}: Received {len(message)} bytes of audio")

                    # Wakeword join-deaf gate (ShayneP-borrow): while enabled and not yet
                    # awake, audio is scored for the wake phrase and never reaches
                    # input_queue/VAD. Detection typically fires mid-utterance (e.g. "hey
                    # jarvis, what's the weather") -- the remainder of that same breath
                    # flows through the normal should_listen path below on the very next
                    # binary frame, so one-breath wake+command works.
                    if self.wakeword_gate.enabled and not self.wakeword_gate.awake:
                        was_awake = self.wakeword_gate.awake
                        score = await asyncio.to_thread(self.wakeword_gate.feed, message)
                        if self.wakeword_gate.awake and not was_awake:
                            logger.info(f"Client {client_id}: wake word detected (score={score})")
                            await asyncio.gather(
                                *[
                                    self._send_or_drop(
                                        client, json.dumps({"type": "wakeword_state", "state": "awake", "score": score})
                                    )
                                    for client in self.clients
                                ],
                                return_exceptions=True,
                            )
                        continue

                    if self.should_listen.is_set():
                        # Split into 512-sample (1024 bytes) chunks for VAD.
                        # Keep a per-client remainder buffer so no samples are dropped
                        # when WebSocket frame boundaries are not aligned.
                        chunk_size_bytes = 512 * 2  # 512 samples * 2 bytes per int16
                        recv_buffer.extend(message)
                        num_chunks = 0
                        while len(recv_buffer) >= chunk_size_bytes:
                            chunk = bytes(recv_buffer[:chunk_size_bytes])
                            del recv_buffer[:chunk_size_bytes]
                            # Echo gate: drop chunks that are just our own TTS
                            # bleeding back through the mic. Inert (returns
                            # True immediately) unless VOICE_ECHO_GATE is set.
                            if not self._echo_pass(chunk):
                                continue
                            self.input_queue.put(chunk)
                            num_chunks += 1
                        logger.debug(f"Client {client_id}: Queued {num_chunks} chunks for processing")
                    else:
                        logger.debug(f"Client {client_id}: Skipping audio (should_listen not set)")

                elif isinstance(message, str):
                    try:
                        msg = json.loads(message)
                    except (ValueError, TypeError):
                        logger.debug(f"Client {client_id}: ignoring malformed text frame")
                        continue
                    if not isinstance(msg, dict):
                        logger.debug(f"Client {client_id}: ignoring non-object text frame")
                        continue
                    if msg.get("type") == "camera_frame":
                        await asyncio.to_thread(self._write_camera_frame, msg.get("data"))
                    elif msg.get("type") == "screen_frame":
                        await asyncio.to_thread(self._write_screen_frame, msg.get("data"))
                    elif msg.get("type") == "phone_context":
                        ok = await asyncio.to_thread(phone_context.update, msg)
                        if not ok:
                            logger.debug(f"Client {client_id}: phone_context update rejected")
                    elif msg.get("type") == "voice_clone_chunk":
                        result = await asyncio.to_thread(self._voice_clone_chunk, client_id, msg.get("data"))
                        if result is not None:
                            await websocket.send(json.dumps(result))
                    elif msg.get("type") == "voice_clone_end":
                        await self._handle_voice_clone_end(client_id, websocket)
                    elif msg.get("type") in ("config_get", "config_set", "voice_clone_begin") and self.control_callback:
                        result = await asyncio.to_thread(self.control_callback, msg)
                        if msg.get("type") == "voice_clone_begin":
                            result = self._resolve_voice_clone_begin_result(client_id, msg, result)
                        await websocket.send(json.dumps(result))
                    else:
                        logger.debug(f"Client {client_id}: ignoring unknown text frame type {msg.get('type')!r}")

        except Exception as e:
            logger.error(f"Client {client_id} error: {type(e).__name__}: {e}", exc_info=True)
        finally:
            self.clients.discard(websocket)
            self._voice_uploads.abort(client_id)
            logger.info(f"Client {client_id} disconnected (finally block)")

            if len(self.clients) == 0:
                logger.debug("Last WebSocket client disconnected, ending session")
                self.input_queue.put(SESSION_END)
                self.wakeword_gate.reset()
                # The playback reference and the locked echo delay describe
                # THIS session's audio path (this client's speaker, mic and
                # buffering). Carrying either into the next session would
                # score fresh mic audio against a dead reference at a lag
                # that no longer holds, so re-acquire from scratch.
                try:
                    self.echo_gate.reset()
                except Exception:
                    logger.debug("echo gate reset failed", exc_info=True)

    _BROADCAST_SEND_TIMEOUT_S = 5.0

    async def _send_or_drop(self, client: ServerConnection, data: Any) -> None:
        """One client's leg of a broadcast fan-out. A client that never drains
        (dead peer, wedged NAT, stalled tab) would otherwise hold `send()`
        open indefinitely and, because every broadcast `gather`s all clients
        together, back up audio/text delivery to every OTHER client behind
        it. Bound each leg individually and drop only the offender on
        timeout -- the rest of the fan-out is unaffected."""
        try:
            await asyncio.wait_for(client.send(data), timeout=self._BROADCAST_SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Client %s: broadcast send timed out after %ss; disconnecting",
                id(client),
                self._BROADCAST_SEND_TIMEOUT_S,
            )
            self.clients.discard(client)
            try:
                await client.close()
            except Exception:
                pass
        except Exception:
            pass  # same fail-soft contract gather(return_exceptions=True) gave callers before

    def broadcast_wakeword_state(self) -> None:
        """Push the current wake-word gate status to every connected client.
        Thread-safe: safe to call from BrainControl's `asyncio.to_thread`
        context (control messages are handled off the event loop). Best-effort
        -- never raises, only debug-logs failures."""
        try:
            if self.loop is None:
                return
            payload = json.dumps(
                {"type": "wakeword_state", "state": self.wakeword_gate.state(), "phrase": self.wakeword_gate.phrase}
            )

            async def _send_all() -> None:
                await asyncio.gather(
                    *[self._send_or_drop(client, payload) for client in self.clients],
                    return_exceptions=True,
                )

            asyncio.run_coroutine_threadsafe(_send_all(), self.loop)
        except Exception:
            logger.debug("broadcast_wakeword_state failed", exc_info=True)

    def broadcast_json(self, payload: dict[str, Any]) -> None:
        """Push an arbitrary JSON payload to every connected client.
        Thread-safe: safe to call from BrainControl's `asyncio.to_thread`
        context (control messages are handled off the event loop), same
        pattern as `broadcast_wakeword_state`. Best-effort -- never raises."""
        try:
            if self.loop is None:
                return
            data = json.dumps(payload)

            async def _send_all() -> None:
                await asyncio.gather(
                    *[self._send_or_drop(client, data) for client in self.clients],
                    return_exceptions=True,
                )

            asyncio.run_coroutine_threadsafe(_send_all(), self.loop)
        except Exception:
            logger.debug("broadcast_json failed", exc_info=True)

    def _resolve_voice_clone_begin_result(self, client_id: int, msg: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Given BrainControl's response to a `voice_clone_begin`, open the
        local chunk-buffering session on success. `UploadManager.begin`
        re-checks the same structural rules (name format/ext/size) BrainControl
        already validated -- if the two ever disagreed, a client that got a
        "receiving" progress frame would otherwise hit "no upload in
        progress" on every following chunk with no explanation. Downgrade to
        an explicit `voice_clone_result` error instead, and never leave a
        session behind on that path."""
        if result.get("type") != "voice_clone_progress":
            return result
        ok, error = self._voice_uploads.begin(client_id, msg.get("name"), msg.get("ext"), msg.get("size"))
        if ok:
            return result
        self._voice_uploads.abort(client_id)
        return {"type": "voice_clone_result", "ok": False, "name": msg.get("name"), "error": error}

    def _voice_clone_chunk(self, client_id: int, data: Any) -> dict[str, Any] | None:
        """Decode + buffer one `voice_clone_chunk` frame -- bytes only, no
        model work; BrainControl is reached only at begin/end. Returns an
        error `voice_clone_result` to send back, or `None` on a silent
        successful buffer (no per-chunk ack in the protocol)."""
        raw = None
        if isinstance(data, str):
            try:
                raw = base64.b64decode(data, validate=True)
            except Exception:
                raw = None
        if raw is None:
            self._voice_uploads.abort(client_id)
            return {"type": "voice_clone_result", "ok": False, "name": None, "error": "malformed chunk payload"}
        ok, error, name = self._voice_uploads.chunk(client_id, raw)
        if ok:
            return None
        return {"type": "voice_clone_result", "ok": False, "name": name, "error": error}

    async def _handle_voice_clone_end(self, client_id: int, websocket: ServerConnection) -> None:
        """Finalize the buffered upload and hand the assembled bytes to
        BrainControl -- this is "the END frame triggers the BrainControl
        call" from the protocol section."""
        session, error = self._voice_uploads.end(client_id)
        if session is None:
            await websocket.send(json.dumps({"type": "voice_clone_result", "ok": False, "name": None, "error": error}))
            return
        if not self.control_callback:
            await websocket.send(
                json.dumps(
                    {"type": "voice_clone_result", "ok": False, "name": session.name, "error": "voice cloning unavailable"}
                )
            )
            return
        try:
            await websocket.send(json.dumps({"type": "voice_clone_progress", "stage": "building"}))
        except Exception:
            logger.debug("Client %s: voice_clone building notice failed", client_id, exc_info=True)
        end_msg = {"type": "voice_clone_end", "name": session.name, "ext": session.ext, "data": session.data}
        result = await asyncio.to_thread(self.control_callback, end_msg)
        await websocket.send(json.dumps(result))

    def _write_camera_frame(self, data: Any) -> None:
        """Write the latest client camera frame to the tmpfs path the `look` vision
        tool reads. Rate-limited + size-capped, atomic replace, never raises."""
        try:
            if not isinstance(data, str):
                return
            now = time.monotonic()
            if now - self._last_cam_write < _CAMERA_MIN_INTERVAL_S:
                return
            raw = base64.b64decode(data, validate=True)
            if not raw or len(raw) > _CAMERA_MAX_BYTES:
                return
            tmp = f"{_CAMERA_FRAME_PATH}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, _CAMERA_FRAME_PATH)
            self._last_cam_write = now
        except Exception as e:
            logger.debug("camera_frame write failed: %r", e)

    def _write_screen_frame(self, data: Any) -> None:
        """Write the latest client screen-share frame to the tmpfs path the `look` vision
        tool reads. Rate-limited + size-capped, atomic replace, never raises."""
        try:
            if not isinstance(data, str):
                return
            now = time.monotonic()
            if now - self._last_screen_write < _SCREEN_MIN_INTERVAL_S:
                return
            raw = base64.b64decode(data, validate=True)
            if not raw or len(raw) > _SCREEN_MAX_BYTES:
                return
            tmp = f"{_SCREEN_FRAME_PATH}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, _SCREEN_FRAME_PATH)
            self._last_screen_write = now
        except Exception as e:
            logger.debug("screen_frame write failed: %r", e)

    def _echo_pass(self, chunk: bytes) -> bool:
        """True if `chunk` should reach VAD/ASR. Wraps `EchoGate.feed` so the
        receive loop can never be broken by the gate: `feed` already fails
        open internally, and this catches anything it somehow doesn't (e.g. a
        no-op stand-in whose contract drifts). Deafness to the user is the one
        failure this pipeline must not have."""
        try:
            return self.echo_gate.feed(chunk)
        except Exception:
            logger.debug("echo gate feed failed; passing audio", exc_info=True)
            return True

    async def _broadcast_audio(self, data: bytes) -> None:
        """Send one chunk of TTS audio to every client, and record it as the
        echo gate's playback reference.

        Every outbound-audio path in `_send_loop` goes through here on
        purpose: the gate decides "is this mic audio explained by what we just
        played?", so a send site that skipped `note_playback` would leave a
        hole in the reference, and the echo from that hole would correlate
        against nothing and be passed through as if it were user speech.
        Funnelling the sends makes that omission impossible to reintroduce."""
        try:
            self.echo_gate.note_playback(data)
        except Exception:
            logger.debug("echo gate note_playback failed", exc_info=True)
        await asyncio.gather(*[self._send_or_drop(client, data) for client in self.clients], return_exceptions=True)

    async def _send_loop(self) -> None:
        """Send audio and text from queues to all connected clients."""
        # Buffer audio until we have at least 100ms worth (3200 bytes = 1600 samples at 16kHz int16)
        MIN_AUDIO_BYTES = 3200
        audio_buffer = bytearray()

        while not self.stop_event.is_set():
            try:
                # Check for audio
                try:
                    audio_chunk = self.output_queue.get_nowait()
                    if isinstance(audio_chunk, bytes) and audio_chunk == PIPELINE_END:
                        if audio_buffer and self.clients:
                            data = bytes(audio_buffer)
                            audio_buffer.clear()
                            await self._broadcast_audio(data)
                        break
                    if isinstance(audio_chunk, bytes) and audio_chunk == AUDIO_RESPONSE_DONE:
                        if audio_buffer and self.clients:
                            data = bytes(audio_buffer)
                            audio_buffer.clear()
                            turn_stats.mark("first_audio_out")
                            await self._broadcast_audio(data)
                        self.should_listen.set()
                        logger.debug("Response complete, listening re-enabled")
                        continue
                    if is_control_message(audio_chunk, SESSION_END.kind):
                        audio_buffer.clear()
                        continue

                    if isinstance(audio_chunk, PipelineControlMessage):
                        continue

                    if self.clients:
                        chunk_bytes: bytes
                        if isinstance(audio_chunk, bytes):
                            chunk_bytes = audio_chunk
                        elif isinstance(audio_chunk, np.ndarray):
                            chunk_bytes = audio_chunk.tobytes()
                        elif hasattr(audio_chunk, "tobytes"):
                            chunk_bytes = audio_chunk.tobytes()
                        else:
                            continue
                        audio_buffer.extend(chunk_bytes)

                        if len(audio_buffer) >= MIN_AUDIO_BYTES:
                            data = bytes(audio_buffer)
                            audio_buffer.clear()
                            logger.debug(f"Sending {len(data)} bytes of audio to {len(self.clients)} client(s)")
                            turn_stats.mark("first_audio_out")
                            await self._broadcast_audio(data)
                except Empty:
                    # Flush any buffered audio when queue is empty
                    if audio_buffer and self.clients:
                        data = bytes(audio_buffer)
                        audio_buffer.clear()
                        logger.debug(f"Flushing {len(data)} bytes of audio to {len(self.clients)} client(s)")
                        turn_stats.mark("first_audio_out")
                        await self._broadcast_audio(data)

                # Check for text/tool messages
                if self.text_output_queue:
                    try:
                        text_message = self.text_output_queue.get_nowait()
                        if self.clients:
                            if isinstance(text_message, PipelineEvent):
                                payload = text_message.model_dump()
                                self._transcript_buffer.feed(payload, time.time())
                                await asyncio.gather(
                                    *[self._send_or_drop(client, json.dumps(payload)) for client in self.clients],
                                    return_exceptions=True,
                                )
                            elif isinstance(text_message, (PipelineControlMessage, bytes)):
                                continue
                    except Empty:
                        pass

                await asyncio.sleep(0.01)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Send loop error: {e}")
