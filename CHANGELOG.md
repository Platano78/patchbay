# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Removed

- The pre-LLM reflex lane (`patches/reflex_lane.py`, `VOICE_REFLEX`). `VOICE_REFLEX` is now ignored: every turn goes to the LLM, which already handles smart-home commands through the `home_assistant` tool. Reason: measured over 60 days enabled, it answered zero turns (its `^turn ...` patterns missed real phrasings such as "can you turn off the living room lights") and it duplicated the tool's command parsing. `emit_synthetic_reply` and the chat-buffer reader that parrot mode shared with it now live in `patches/parrot_lane.py`.

### Fixed

- Wake word on Python 3.12 and newer: `pip install openwakeword onnxruntime` resolves to openwakeword 0.4.0 there (0.5 and later need `tflite-runtime`, which has no wheel), and the gate asked 0.4.0 for the bare name `hey_jarvis`, which it cannot resolve, so it failed open: the pipeline answered everything while the panel showed wake-gating on. The gate now resolves a bare model name to the wheel's bundled file on 0.4.x (paths pass through unchanged), and loads the model when the gate is switched on instead of on the first audio. A model that cannot load now switches the gate off with one log warning naming the model and the fix, and the panel's toggle shows that reason. SETUP's wake-word section says which Python versions need the one-time model download (3.10 and 3.11, openwakeword 0.6) and which have the models in the wheel (3.12 and newer, 0.4.0). `patches/wakeword_gate.py`, SETUP.md.
- Printed hints and docs: `brain_discovery.py`'s own hints and the README name the framework venv's python (the script imports `httpx`, so a bare `python3` died with a traceback); SETUP shows `brain_lanes.py check --brains-json brains.json` (it otherwise reads `~/speech-to-speech/brains.json`); `examples/render_fillers.py` with no speech server prints one sentence naming the URL and exits 1 instead of a urllib traceback; the README's expected-skip list matches what clean hosts measured, and notes a custom `INSTALL_DIR` for the tests; the browser-test skip sentence no longer claims `websockets` is skipped on; AGENTS.md points the smoke check at "section 5" (SETUP has it inside step 4); SETUP's optional loopback-only `sed` lines touch only the `ExecStart=` line, no longer the template comment above it.
- Cockpit page: the armed-tool list on the command legend no longer takes height from the meter and deck (it is capped to four rows and scrolls on its own, keyboard-focusable), so the PEAK/READY/RMS readout stays whole however many tools are armed; the legend's ARM line no longer prints the placeholder "the wake word" before connect (it stays hidden until the real phrase is known); the connection-failure hint now names both causes, nothing listening or the pipeline refusing the page's origin (`VOICE_WS_ORIGINS` / `VOICE_ALLOWED_HOSTS`), since the browser cannot tell them apart.
- Documentation corrections: the model-endpoint check now shows the keyed `curl` form for hosted endpoints and `brain_lanes.py check`; the rollback in `patches/README.md` warns that it discards every uncommitted change under `src/speech_to_speech` and says to check `git status` first; the README test section says what the browser tests need on a minimal image (`libatomic1`, `playwright install-deps chromium`) and which failures skip (Chromium cannot launch) and which error (Playwright's driver cannot start); the systemd section says units are installed only if you want auto-start and that `enable --now` also starts them at every boot; the tyger README states the weights' licence before the install commands; `/models` is documented as a check of fixed port 8765.
- Hermes delegations now reach you. With `HERMES_TARGET` set, a finished or failed voice delegation is posted to that chat with Hermes's full answer (capped at 3500 characters; `VOICE_HERMES_NOTIFY=off` to stop), and it is written into the assistant's conversation so it can tell you the result instead of guessing (independent of `VOICE_HERMES_ANNOUNCE`). `patches/hermes_cockpit.py`, `patches/s2s_pipeline.py`.
- The consequential-action gate can no longer be confirmed by the model alone: `confirm_action` is refused ("The user hasn't answered yet") until a final user transcript has arrived since the refusal, so refuse, confirm and retry inside one turn no longer works. Existing single-use and exact-argument rules are unchanged. `patches/voice_tools.py`, `patches/s2s_pipeline.py`.
- `send_to_hermes` says what it does: it posts a note into your own chat with Hermes, which does not act on it. Its description points at `delegate_to_hermes` for anything Hermes should do, and its result reads "Posted to your chat with Hermes. Hermes doesn't act on these; to have it do something, delegate it." The README's `HERMES_TARGET` row says the same. `patches/voice_tools.py`, `patches/hermes_cockpit.py`.
- `hermes_status` no longer forgets a finished Hermes task when the quest card clears 90 seconds later: the last finished delegation (task, result, steps) is kept and reported ("Hermes is idle. Its last task finished 5 minutes ago: ...") until the next delegation finishes; a fresh install's idle wording is unchanged. The spoken completion announcement now says "a file" instead of reading out an absolute path and is cut at a sentence end at 160 characters (was 300), so it fits the remote TTS's 15 s cap instead of failing over to the fallback voice mid-sentence. `patches/hermes_cockpit.py`.
- `get_weather` resolves a bare place name the way a person would: it asks the geocoder for ten candidates and prefers countries, regions and capitals, then larger towns, then the API's own order (before, the first hit won, so "New Jersey" gave a village in Trinidad). A name shared by several places is spoken with its region ("Springfield, Illinois, United States"). `patches/voice_tools.py`.
- A model that repeated the same tool call within one turn (seen: five identical `web_search` queries in a row) no longer waits for each repeat: an identical `web_search` or `get_weather` call (same arguments ignoring case and spacing) is answered with a short "already made this turn" result instead of running again. The per-turn cap (`VOICE_TOOL_CALL_CAP`, still 8) is unchanged. Only the read-only lookups `web_search` and `get_weather` are guarded; any other tool may repeat an identical call. `patches/lm_output_processor.py`.
- A failed Hermes delegation is spoken as a plain phrase with the status code or the kind of failure ("the Hermes gateway timed out (504)", "Hermes timed out after 15 minutes", "Hermes could not be reached") instead of the exception class name. No URL or host is spoken; the log line keeps the detail. `patches/hermes_cockpit.py`.

### Added

- `get_weather` takes `when` (`now`, `today`, `tonight`, `tomorrow`) and `units` (`fahrenheit`, `celsius`). A forecast gives the day's high, low, conditions and chance of precipitation, or the overnight low for tonight; the current reading now also says today's high and low. Without `units`, places in the US and the other countries that use Fahrenheit get Fahrenheit and mph (whole degrees), everywhere else Celsius and km/h; `VOICE_WEATHER_UNITS` overrides that for every place. Before, it could only report the current reading in Celsius. `patches/voice_tools.py`, README env table.
- `sports_scores` tool: final and live scores and start times (server local time) from ESPN's public scoreboard JSON for `nba`, `nfl`, `mlb`, `nhl`, `wnba`, `mls`, `ncaaf`, `ncaam` and `epl`, for today, yesterday, tomorrow or a date, optionally one team (a team with no game that day is looked up a day either side). At most eight games. `web_search` now points the model here for scores, since a web search returns none. Repeat calls within a turn are guarded like `web_search`. `patches/voice_tools.py`, README tool table.
- `VOICE_DEFAULT_PLACE` (unset by default): a place name or `lat,lon` that `get_weather` uses when no place is named and the client sent no location. A client's location still wins. `patches/voice_tools.py`, README env table.
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
