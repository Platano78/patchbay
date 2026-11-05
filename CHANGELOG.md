# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Removed

- The pre-LLM reflex lane (`patches/reflex_lane.py`, `VOICE_REFLEX`). `VOICE_REFLEX` is now ignored: every turn goes to the LLM, which already handles smart-home commands through the `home_assistant` tool. Reason: measured over 60 days enabled, it answered zero turns (its `^turn ...` patterns missed real phrasings such as "can you turn off the living room lights") and it duplicated the tool's command parsing. `emit_synthetic_reply` and the chat-buffer reader that parrot mode shared with it now live in `patches/parrot_lane.py`.

### Added

- Smart Turn end-of-turn (`VOICE_SMART_TURN=act|shadow|off`, off by default): when the silence rule would end an open-mic turn, the pipeline first asks a `smart-turn` classifier on the speech server whether the turn sounds finished; if not, it keeps listening up to `VOICE_SMART_TURN_HOLD_MS` (1200) and treats resumed speech as the same turn. `shadow` only logs what it would decide. Needs the `remote-transcription` backend on a server that serves `smart-turn`. `patches/turn_classifier.py`, `patches/vad_handler.py`, README "Smart Turn and fillers".
- Turn fillers (`VOICE_TURN_FILLER=on`, off by default): one short pre-rendered clip ("Mm.", "One sec.") plays `VOICE_TURN_FILLER_DELAY_MS` (600) after a transcribed turn ends if no reply audio has started yet. Tool fillers now also play from the same clip cache when a clip exists. Clips are rendered once per voice with `examples/render_fillers.py`. `patches/filler_clips.py`.
- Voice design tools (`VOICE_DESIGN_TOOLS`, on with the remote speech backend and a server exposing `/v1/voices/design` and `/v1/voices/clone`): the LLM tools `design_voice` (switch to a voice designed from a description), `save_voice` (keep it under a name) and `restore_voice` (back to the normal voice). `patches/voice_design.py`.
- Push-to-talk hold: the webclient sends `{"type":"ptt","state":"down"}` on press (button, touch or Space) and `"up"` on release, before its tail silence. While a connection holds `down`, the server withholds the pauses of the turn from the VAD, so a pause mid-thought (measured 2026-11-02: a turn finalised 3 s into a 6 s hold, and another 3 s into an 8 s hold) no longer ends the turn; speech before and after it is one turn. `up` ends the turn at once by feeding the VAD silence, the same path silence takes today. A hold ends on `up`, on socket close, or after 120 s. Clients that never send `ptt` behave exactly as before. On by default; `VOICE_PTT_HOLD=0` ignores the frames. `patches/websocket_streamer.py` (`PttHold`), `docs/contracts/ws-client-protocol.md`.
- `remote-transcription` STT backend (`--stt remote-transcription`): speech-to-text on any server implementing OpenAI's `/v1/audio/transcriptions` protocol, so the model can live on another machine. Opt-in (`--remote_transcription_base_url` required); `parakeet-tdt` stays the default. Live partials re-upload the last `--remote_transcription_progressive_window_s` seconds on the same endpoint; no local fallback (a failed request gives an empty transcript). `patches/remote_transcription_stt_handler.py`, `patches/remote_transcription_stt_arguments.py`, README "STT backends".
- Server-side cancel on barge-in (websocket mode). A push-to-talk press that lands on a reply sends `{"type":"barge_in"}`; the server cancels the LLM stream, the speech synthesis and the queued audio, re-opens the microphone and broadcasts `{"type":"audio_stop"}`, and the client drops audio until it arrives. Before this, only the browser's playback stopped: the pipeline kept producing the old reply, which could resume after the new question and take the held utterance with it. The cancelled reply's text so far stays in chat history. On by default; `VOICE_BARGE_CANCEL=off` restores the old behaviour. Both frames are in `docs/contracts/ws-client-protocol.md`.
- `patches/backchannel.py`: lexical acknowledgement classifier (reimplemented from the idea in `speakrail/speakrail`, stdlib-only); the phrase list is changed materially and the module carries no third-party code.
- `examples/echo_gate_report.py`: stdlib-only fixed-format summary of `echo_gate.calib` log lines (fills the documented gap at README.md:381), with verdict line (`ECHO EVIDENCE` / `NO ECHO EVIDENCE`).
- `playback` client frames (`start`/`end`/`cut`, always sent, metadata only) and `patches/floor_state.py`, the server's shared "who holds the floor" state. Every playback start logs `TURN_HEARD speech_stopped_to_heard_s=… first_audio_out_to_heard_s=…` (the time until the reply is actually audible; includes one-way network latency once, and is anchored at the VAD decision).
- `VOICE_OVERLAP_OBSERVE` (off by default; needs `VOICE_ECHO_GATE=observe|on`): an open-mic client also streams mic frames while a reply plays; the server scores them under `phase=playback` and never queues them. `echo_gate_calib` lines gain ` phase=playback` only for those frames (unchanged otherwise), `config_state` gains `overlap_observe`, and `examples/echo_gate_report.py` prints a per-window table.
- `services/tyger/`: optional turn-taker sidecar (GPU, own venv; the pipeline does not use it yet): `install.sh`, `run.sh`, a WebSocket server at `/v1/floor`, socket-activated user-unit templates in `systemd/patchbay-tyger.*.template`, and tests. See `services/tyger/README.md`.

## [2.8.0] - 2026-09-24

### Added

- `examples/smoke_turn.py`: verifies a real spoken turn (heard → answer →
  reply audio) end to end without a microphone; on failure it prints one
  sentence naming what didn't arrive and exits `1`.
- Host-header allowlist against DNS rebinding (see Security) and tests for it:
  `webclient/test_host_guard.py`, plus `patches/test_ws_host_default.py` for
  the loopback default.
- This changelog.

### Changed

- setup.sh: skips the optional kokoro TTS extra on Python 3.13+ (it cannot
  resolve there and failed the whole install on a stock Python 3.14); checks
  for `git` and the `venv` module before cloning anything; `--check` prints
  one line instead of a traceback and probes playwright inside the venv,
  where the tests run.
- User-facing install hints name the framework venv's own binaries — the
  `hf auth login` and `pip install soundfile` hints in the pipeline and in
  `patches/voice_clone.py`, and the remote-speech example — because a bare
  `pip` hits the system Python, which the pipeline does not use.
- Documentation rewritten for a stranger, or the agent installing for one:
  AGENTS.md is an install guide with exact commands, expected output and a
  troubleshooting table in the software's own words (CLAUDE.md imports it);
  SETUP.md gains prerequisites incl. `python3-venv` and a download table with
  sizes and licences; README/NOTICE list the twelve bundled avatar heads with their
  real licences and the HA routes `serve.py` actually exposes; the WS protocol
  contract, THEME-SPEC and patches/README were corrected against the code.
- Phone location: the settings panel, README, SETUP and SECURITY.md now state
  before the opt-in that the server sends the coordinates to OpenStreetMap's
  Nominatim for the place lookup, and what reaches the brain.
- Docs state where untracked theme files under `webclient/themes/local/`
  actually go: they are served to anyone who can reach the cockpit port.
- Tests run outside the framework venv now stop with one sentence naming the
  venv command (root `conftest.py`) instead of crashing with import
  tracebacks; inside the venv nothing changes.
- The level-bus and level-fast perf gates compare against a control measured
  in the same run instead of fixed millisecond thresholds tuned on one
  machine.
- AGENTS.md rule: an installing agent must ask before installing the systemd
  service; a foreground run plus the smoke check is a finished setup.

### Fixed

- Wake word selected without `openwakeword` installed used to be accepted,
  then fail open and log `wake word detected (score=None)` while every
  utterance reached the brain. It is now refused with the reason, and the
  toggle and ARM are greyed out with that reason; a fail-open is logged as
  one.
- The cockpit status no longer stays "processing…" (THINK lamp, THINKING
  needle) after push-to-talk turns — including silent holds: saying nothing
  produces no server frames at all, so a 10 s "nothing heard" watchdog now
  ends the state; any real sign of life (partial, transcript, answer,
  assistant audio, failure, a new hold, socket close) cancels it.
- SETUP's phone-context check relied on a `config_get` field that does not
  exist; replaced with checks a user can see.
- `examples/smoke_turn.py` reports a connection dropped mid-turn in one
  sentence instead of a traceback.

### Security

- Safe-by-default bindings: `serve.py` binds `127.0.0.1`, and the patched
  pipeline overrides the framework's `ws_host` default of `0.0.0.0` to
  `127.0.0.1` on both the CLI and the JSON-config parse paths. An explicit
  `--ws_host` / `"ws_host"` still wins; the systemd template and README pass
  `0.0.0.0` explicitly for LAN access.
- DNS-rebinding guard: the cockpit HTTP server (`webclient/host_guard.py`)
  and the pipeline WebSocket now reject requests whose `Host` header is not
  an IP literal, `localhost`, one of this machine's own names (hostname,
  FQDN, `.local`, Tailscale MagicDNS), a hostname named in `VOICE_WS_ORIGINS`,
  or a name the operator allowed via `VOICE_ALLOWED_HOSTS` (`*` disables it). A rebinding page carries the attacker's hostname,
  which no same-origin check can see.
- `POST /ha/intent` requires a JSON body and a same-host Origin, so a web
  page on another origin can no longer drive Home Assistant through the
  cockpit.

## [2.7.0] - 2026-09-13

Earlier releases are described on the GitHub releases page:
<https://github.com/Platano78/patchbay/releases>.
