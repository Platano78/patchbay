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

There is **no application-level login and no post-open handshake**. The socket is live the
moment it opens; the network is the entire perimeter. Do not expose this port outside a trusted
network.

The WebSocket **upgrade itself can be refused with HTTP 403** before the socket opens: a `Host`
header that is not allowed gets body `host not allowed`, an `Origin` header that is not allowed
gets body `origin not allowed`. Check both when a client cannot connect.

Origin-based restriction (`VOICE_WS_ORIGINS`) exists but is a browser defence — it keys off the
`Origin` header. A native client sends none, and is covered by the `-` allowlist entry. If an
operator has set `VOICE_WS_ORIGINS` without including `-`, native clients are rejected — this is
the first thing to check when a non-browser client cannot connect but the browser can. The Host
allowlist (the DNS-rebinding guard; see README *Who can connect*) is separate and applies to
every request.

## Turn-taking belongs to the server

The server ends a turn when **its own VAD** sees `min_silence_ms` of quiet. The VAD has no concept
of a client-side control — a push-to-talk button, a tap-to-stop, anything — so a client that holds
the floor must say so with the `ptt` message (below). Without it, the server ends a turn mid-hold
whenever the speaker pauses for `min_silence_ms` (300 ms on the reference deployment; the upstream
default of 64 ms is shorter than an ordinary pause between words).

**`ptt` hold.** Send `{"type":"ptt","state":"down"}` when the user presses and
`{"type":"ptt","state":"up"}` when they release. While a connection's hold is `down`, silence does
not end the turn: the server withholds the pauses from the VAD, so speech before and after a pause
is one turn. On `up` the server ends the turn at once by feeding the VAD silence, the same path
silence takes. A hold the client never releases is dropped when the socket closes, and in any case
after 120 s, after which the VAD behaves normally. A hold with no speech in it produces no turn,
as before. Servers with `VOICE_PTT_HOLD=0`, and older servers, ignore the frames, so sending them is
always safe. A client that never sends `ptt` (wake word, open mic, native clients) gets exactly the
old behaviour: the VAD alone ends turns.

Still true: a client may end a turn deliberately by **sending silence** — the VAD counts samples,
not wall-clock time, so writing `min_silence_ms` worth of zero bytes closes the turn at the moment
the user chose. The webclient sends `up` first and then about one second of zeros, so it works
against servers without the hold.

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
| `playback` | `state`: `start`, `end` or `cut`; `delay_ms` (`start` only) | Optional, metadata only, no audio. Tells the server when this client's speaker is actually playing a reply: `start` when the first chunk of a reply is scheduled (`delay_ms` = how far in the future it will sound), `end` once the last chunk has played out, `cut` when playback was stopped early (barge-in). Gives the server a heard-latency figure (`TURN_HEARD` in its log) and, with `VOICE_OVERLAP_OBSERVE`, brackets the window in which this client's mic frames are measured instead of queued. Frames are ordered with the audio on the same socket, so a mic frame sent after `end`/`cut` is never read as playing-phase. Servers that do not know it ignore it. The stall-fill clips and the wake chime never send it. |
| `ptt` | `state`: `down` or `up` | Push-to-talk hold. `down` on press, `up` on release (before any tail silence). While `down`, the server's silence rule cannot end the turn; `up` ends it. See "Turn-taking belongs to the server". Per connection; dropped on socket close or after 120 s. Ignored by servers without the feature. |
| `barge_in` | — | Send when the user takes the floor while a reply is playing or still being produced (a push-to-talk press does), **before** you flush your own playback and before the first mic frame. The server cancels the reply in flight — the LLM stream, the speech synthesis and the queued audio — re-opens the microphone, and answers with `audio_stop`. Nothing in flight: the server changes nothing and still answers `audio_stop`. From the moment you send it, **drop every binary frame you receive until `audio_stop` arrives**: frames are ordered on the socket, so everything before it is the cancelled reply and everything after it is new audio. Send it, and start dropping, only when the last `config_state` carried `barge_cancel: true`; otherwise behave as if the frame did not exist. Also release the drop after about 1.5 s in case an `audio_stop` is lost. |
| `voice_clone_begin` / `voice_clone_chunk` / `voice_clone_end` | see `webclient/index.html` | Custom voice upload. Skip unless you need it. |

`config_set` keys, as handled by `_config_set` in `patches/brain_control.py`:

| Key | Value | Effect |
|---|---|---|
| `brain` | brain name from `brains.json` | switch the active brain (probed first); resets chat if it changed |
| `brain_model` | `{"brain": name, "model": id}` | per-brain model override; empty `model` clears it |
| `voice` | voice name | switch TTS voice |
| `voice_audition` | bool, with `voice` | `false` suppresses the spoken sample after a switch |
| `speech_style` | string | delivery instructions for a `remote-speech` TTS; an error on backends without that handle |
| `voice_delete` | custom voice name | delete a cloned voice |
| `persona` | text | persona text for the scope below |
| `persona_scope` | `"global"` (default) or `"brain"` | which tier `persona` writes |
| `persona_mode` | `"custom"`, `"preset"` or `"inherit"` | with brain scope: which tier the active brain uses |
| `wake_word` | bool | enable/disable the wake-word gate; `true` is refused when no detector is installed |
| `wake_word_model` | model name or `.onnx`/`.tflite` path | pick the wake phrase |
| `parrot` | bool | arm/disarm parrot mode (every turn spoken back verbatim, no LLM) |
| `discover_brains` | `true` | loopback scan for model servers; the ack carries `discovered_brains` |
| `reset_chat` | `true` | drop the chat history |
| `reload_tools` | `true` | re-probe and re-arm voice tools, including `VOICE_TOOLS_DIR` drop-ins |
| `permission_respond` | `{"id": ..., "approve": bool}` | answer an agent-lane approval request; handled on its own, other keys in the same frame are ignored |

Several keys may share one frame. `permission_respond` is checked first and, if present, is the
only key handled. Otherwise keys are applied in the order above; the first one that fails ends
processing with `ok: false`, and keys applied before it stay applied.

**Who hears about a change.** The requester always gets the `config_ack` (a full snapshot on
success). Other connected clients only learn of a change the server *broadcasts*:

- `brain_model`, `parrot`, `voice_delete`, and a finished voice clone broadcast a fresh
  `config_state` to every client.
- `wake_word` and `wake_word_model` broadcast a `wakeword_state` frame to every client.
- `brain`, `voice`, `speech_style`, `persona*`, `reset_chat`, `reload_tools`,
  `discover_brains` and `permission_respond` answer the requester only. Another screen sees
  those on its next `config_get`.
- A `config_get` also starts a background reachability probe of the configured brains, which
  broadcasts a `config_state` if a brain's reachability changed, and (when the agent lane is
  wired) pushes a fresh `cockpit_state` to every client.

## Server → client text frames

**A client must ignore unknown `type` values.** New event types are added over time and older
clients are expected to keep working; the reference client logs and drops them.

| `type` | Fields | Meaning |
|---|---|---|
| `transcription_completed` | `transcript` | The user's turn, as recognised. Marks turn commit. |
| `partial_transcription` | `delta`, optional `turn_id`, `turn_revision` | The whole transcript so far for the turn, despite the name. **Replace** the live text, don't append. Drop a partial whose turn is older than the newest seen, whose `turn_revision` is older than the newest seen for the same turn, or whose turn is already final. |
| `assistant_text` | `text`, `tools` | The reply. **A tool-calling turn emits an empty-`text` frame carrying `tools` first**, then a second frame with the real answer — don't render the first as an empty reply. |
| `wakeword_state` | `state` (`awake`/`asleep`/`off`), `phrase`, `score` | Wake-gate state. |
| `config_state` | full config snapshot | Answer to `config_get`; also broadcast on some changes (above). Its `barge_cancel` boolean is `true` only when the server cancels a reply on `barge_in` (`VOICE_BARGE_CANCEL` not `off`); send `barge_in` only when the last snapshot said so, and treat an absent key as `false`. Its `overlap_observe` boolean is `true` only when the server runs the overlap-observe instrument (`VOICE_OVERLAP_OBSERVE` with the echo gate scoring): an open-mic client may then also stream mic frames while a reply plays (a client that does not stays correct, it simply feeds the server less). Its `wake_word` block (or `null` when the server has no gate) carries `enabled`, `state`, `phrase`, `model`, `models`, and `available`; when `available` is `false` — no detector installed — it also carries `unavailable_reason`, a sentence to show the user, and `config_set {wake_word: true}` will be refused. |
| `config_ack` | `ok`, `error`, plus a full snapshot on success | Answer to `config_set`. On `ok`, treat as `config_state`. May carry `chat_reset: true` — the server dropped its history, so mirror that. |
| `history_replay` | `entries[]` with `ts`, `user`, `assistant` | Prior turns, sent on connect. Apply only to empty local state; a reconnect without a client restart already has them. |
| `audio_stop` | — | Stop playing and discard anything scheduled or buffered: the reply in flight was cancelled. Broadcast to every client when any client (or a server-side detector) interrupts a reply, so a second screen goes quiet too; also the answer to your own `barge_in`, in which case it is the marker after which binary frames are new audio again. |
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
