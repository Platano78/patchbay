# WebSocket client protocol

What a non-browser client must speak to act as a voice client for this agent. Everything —
VAD, STT, LLM, tools, persona, wake word, TTS — runs server-side; a client is a microphone,
a speaker, and a socket.

Authority for this document is `patches/websocket_streamer.py`. `webclient/index.html` is the
reference implementation, but most of it is cockpit UI; the audio path is a small subset.

## Transport

One WebSocket carries **both** binary and text frames. Default `ws://HOST:8765`.

- **Binary frames** are audio, in both directions. Nothing else is ever binary.
- **Text frames** are JSON objects with a `type` discriminator. The field is `type`, not
  `event_type`.

There is **no authentication and no handshake**. The socket is live the moment it opens; the
network is the entire perimeter. Do not expose this port outside a trusted network.

Origin-based restriction (`VOICE_WS_ORIGINS`) exists but is a browser defence — it keys off the
`Origin` header. A native client sends none, and is covered by the `-` allowlist entry. If an
operator has set `VOICE_WS_ORIGINS` without including `-`, native clients are rejected — this is
the first thing to check when a non-browser client cannot connect but the browser can.

## Turn-taking belongs to the server

The server ends a turn when **its own VAD** sees `min_silence_ms` of quiet. It has no concept of a
client-side control — a push-to-talk button, a tap-to-stop, anything. It will therefore end a turn
mid-hold if the speaker pauses, and it ships with `min_silence_ms=64`, which is shorter than an
ordinary pause between words.

A client whose UI implies the user owns the turn boundary has two jobs. Raise `min_silence_ms`
server-side so natural pauses do not end a turn — note this is global and shared with every other
client. And end the turn deliberately by **sending silence**: the VAD counts samples, not wall-clock
time, so writing `min_silence_ms` worth of zero bytes closes the turn immediately, at the moment
the user chose, rather than at whatever pause the VAD noticed first.

## Audio

Both directions use the same format. There is no header, no container, no negotiation:

| | |
|---|---|
| Encoding | signed 16-bit PCM, little-endian |
| Channels | 1 (mono) |
| Sample rate | 16000 Hz |

**Uplink** (client → server): write raw PCM into binary frames at whatever cadence suits the
capture buffer. The server keeps a per-client remainder buffer and re-chunks the stream into
512-sample (1024-byte) VAD frames, so **frame boundaries need not align to anything** and no
samples are dropped across frames.

**Downlink** (server → client): TTS audio, same format. The server buffers to at least 3200
bytes (100 ms) before sending, then flushes whatever remains when its queue drains — so chunk
sizes are uneven and a client must treat the stream as continuous, not as one-message-one-
utterance. Play gaplessly by scheduling each chunk after the previous one ends.

### Half-duplex is required

A client that keeps its microphone open while playing assistant audio will feed that audio back
and the agent will answer itself. The reference client is half-duplex: capture is suppressed
while playback is in flight. Implement the same discipline.

The server has an optional acoustic echo gate (`patches/echo_gate.py`, `VOICE_ECHO_GATE`), off and
inert when unset — but a deployment may set it, so its state is not something a client can assume
either way. Do not rely on it. A native client with a speaker and mic in the same handset should
attach the platform's own echo canceller (on Android, `AcousticEchoCanceler` with a
`VOICE_COMMUNICATION` capture source) rather than suppressing playback while capturing: muting the
output starves any canceller of the reference signal it subtracts, which makes echo harder to
cancel, not easier.

### Session is shared, not per-connection

Every connected client feeds the same input queue, and TTS audio is broadcast to *all* of them.
Connecting does not open a private session — a phone joining while a browser tab is open shares
one conversation with it. This is deliberate (the cockpit is a second screen onto one agent),
but it means a client must not assume the audio it receives answers the audio it sent.

## Wake word

When the wake-word gate is enabled, incoming audio is scored for the wake phrase and **never
reaches the pipeline** until it fires. A client streams continuously either way; the gate is
transparent.

Detection typically fires mid-utterance, and the remainder of that same breath flows through
normally on the next binary frame — so "hey jarvis, what's the weather" works as one breath.

State arrives as `wakeword_state` frames (below). A client that ignores them still works; it
just won't show the user whether the agent is listening.

## Client → server text frames

Only `config_get` is needed for a minimal client. The rest are optional capabilities.

| `type` | Payload | Purpose |
|---|---|---|
| `config_get` | — | Request a `config_state` snapshot. Send once on connect. |
| `config_set` | one of the keys below | Change agent configuration. |
| `phone_context` | `lat`, `lon`, `accuracy`, `tz`, `battery_pct`, `charging` | Ambient device context, so tools can answer "what's the weather" without asking where you are. Opt-in. Server-side kill switch: `VOICE_PHONE_CONTEXT=off`. |
| `camera_frame` | `data`: base64 JPEG | Latest camera frame for the `look` tool. Rate-limited server-side to 1/s, capped at 2 MB. |
| `screen_frame` | `data`: base64 JPEG | Same, for screen share. |
| `voice_clone_begin` / `voice_clone_chunk` / `voice_clone_end` | see `webclient/index.html` | Custom voice upload. Skip unless you need it. |

`config_set` keys in use: `brain`, `brain_model`, `persona`, `persona_scope`, `persona_mode`,
`voice`, `wake_word`, `wake_word_model`, `reset_chat`, `reload_tools`, `voice_delete`,
`permission_respond`. Each is answered with a `config_ack`.

## Server → client text frames

**A client must ignore unknown `type` values.** New event types are added over time and older
clients are expected to keep working; the reference client logs and drops them.

| `type` | Fields | Meaning |
|---|---|---|
| `transcription_completed` | `transcript` | The user's turn, as recognised. Marks turn commit. |
| `partial_transcription` | `delta` | Incremental transcript text. Append, don't replace. |
| `assistant_text` | `text`, `tools` | The reply. **A tool-calling turn emits an empty-`text` frame carrying `tools` first**, then a second frame with the real answer — don't render the first as an empty reply. |
| `wakeword_state` | `state` (`awake`/`asleep`/`off`), `phrase`, `score` | Wake-gate state. |
| `config_state` | full config snapshot | Answer to `config_get`; also broadcast on change. |
| `config_ack` | `ok`, `error`, plus a full snapshot on success | Answer to `config_set`. On `ok`, treat as `config_state`. May carry `chat_reset: true` — the server dropped its history, so mirror that. |
| `history_replay` | `entries[]` with `ts`, `user`, `assistant` | Prior turns, sent on connect. Apply only to empty local state; a reconnect without a client restart already has them. |
| `token_usage` | `input_tokens`, `output_tokens` | Diagnostics. |
| `cockpit_state`, `search_links` | — | Cockpit UI panels. Ignorable. |
| `voice_clone_progress` / `voice_clone_result` | — | Voice upload progress. Ignorable. |

Other types (`speech_started`, `speech_stopped`, …) exist and are safely ignorable.

## Minimal voice client

1. Open the socket; set binary mode to raw bytes.
2. Send `{"type":"config_get"}`.
3. Stream 16 kHz mono int16 PCM up while capturing.
4. Play received binary frames gaplessly as 16 kHz mono int16 PCM.
5. Suppress capture while playing.
6. Optionally render `transcription_completed` and `assistant_text`; ignore everything else.

Steps 1–5 are a working voice agent. Everything beyond is cockpit surface.
