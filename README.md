# Patchbay

Patchbay — a local voice instrument. Talk to your own models on your own
hardware.

A self-hostable local voice-agent cockpit: the Hugging Face
[`speech-to-speech`](https://github.com/huggingface/speech-to-speech) framework
paired with a static web cockpit (`webclient/`) that includes an avatar pane
with an **avatar selector dropdown** to pick among bundled
[TalkingHead](https://github.com/met4citizen/TalkingHead) 3D heads (GLB) or a
2D still-image avatar (mouth lip-synced from the same audio) — the head choice
is user-owned and persisted, independent of the active theme — a
brain/persona selector panel, and a settings UI for switching LLM backends
live over the WebSocket control channel. The persona panel ships with a
library of ten built-in personas (offered starting points, never
auto-applied) alongside your own saved ones — pick one, tweak the text if you
like, then hit Apply.

This repo is a **skeleton**: the custom cockpit UI and the patch pack that
wires persona/brain switching into `speech-to-speech` are here, but the
framework itself, your model endpoints, and any avatar assets are supplied by
you at install time.

## Architecture

```
 browser (webclient/index.html)
        │  WebSocket  ws://<host>:8765
        ▼
 speech-to-speech pipeline (patched)
   ├─ STT   — parakeet-tdt
   ├─ LLM   — OpenAI-compatible chat-completions endpoint ("brain")
   └─ TTS   — pocket (default; remote-speech opt-in, see *TTS backends*)
        ▲
        │  HTTP :8770 (static files)
 webclient/serve.py
```

- The browser cockpit connects to the pipeline over a single WebSocket
  (`ws://<hostname>:8765`) for audio in/out plus JSON control messages
  (`config_get`/`config_set` — brain selection, persona text, chat reset).
- `webclient/serve.py` serves the cockpit's own static files on `:8770`
  (nothing else — no other client-side endpoints beyond the WS and a relative
  `fetch("themes/themes.json")`).
- The LLM ("brain") is any OpenAI-compatible `chat-completions` endpoint —
  local (llama.cpp, vLLM, Ollama, etc.) or hosted. `patches/brain_control.py`
  lets you register several brains in `brains.json` and hot-swap between them
  from the cockpit UI without restarting the service.
- A brain's endpoint can serve more than one model — an NVIDIA NIM endpoint
  serves hundreds, a llama.cpp router a handful — so `brains.json`'s `model`
  field is only the *configured default*. Add a `"models": ["id", ...]` array
  to a brain entry to curate the list the settings panel offers for it; if you
  don't, the panel falls back to whatever the last live `/v1/models` probe of
  that endpoint reported. Either way, picking a model from the panel writes a
  per-brain override (persisted to a sidecar file, see `VOICE_MODEL_OVERRIDES_FILE`
  below) that beats the configured default until you clear it — it never edits
  `brains.json` itself.

### One conversation, many screens

Every connected browser is a window onto the **same** session: one chat
history, one brain, one voice. Start a conversation at the desk, continue it
from the phone — that continuity is deliberate. Events are broadcast live to
whoever is connected at that moment; a device that reconnects (screen lock,
backgrounded tab, reload) also gets the last N completed turns replayed into
its history rail, so it doesn't come back to an empty one. That replay buffer
lives in server memory only (cleared on restart) — see `patches/README.md`
for the `VOICE_HISTORY_REPLAY`/`VOICE_HISTORY_REPLAY_TURNS` env vars.

### Who can connect

Worth knowing before you expose the socket: **"every connected browser is a
window onto the same session" has no membership check.** Anything that can
reach the WebSocket port becomes a full screen — it receives the live audio and
transcript of every conversation, and it can rewrite the persona, switch the
active brain, change the model, and reset the chat.

The quick-start above binds `--ws_host 0.0.0.0`, which is every interface,
including your LAN. Two things follow:

- **A VPN is not a boundary while you bind `0.0.0.0`.** Tailscale (or similar)
  adds a path; it does not remove the LAN one. Bind `127.0.0.1` or your
  tailnet address if you want the VPN to actually be the perimeter.
- **A VPN does not stop a hostile web page either.** WebSockets are exempt from
  the browser's same-origin policy, so any page you happen to visit — on a
  device already inside the perimeter — can open a socket to your assistant.
  That is cross-site WebSocket hijacking, and it originates *inside* the
  network, from your own browser. Device-level VPN auth cannot see it.

The control for that second one is `VOICE_WS_ORIGINS`, an allowlist of page
origins permitted to open the socket. It costs nothing at runtime and there is
no token to distribute:

```bash
# only pages served from your own cockpit may open the socket
export VOICE_WS_ORIGINS="https://box.tail1234.ts.net,-"
```

The `-` entry means "also allow clients that send no `Origin` header at all",
which is every non-browser client — scripts, health probes, `websocat`. Leave
it out and you will lock those out.

**Unset (the default) is no longer "no restriction" as of this release.** It
now accepts (a) clients that send no `Origin` header at all — every
non-browser client, same as the `-` entry above — and (b) pages whose Origin
hostname matches the Host header the connection came in on, i.e. the same box
the socket itself is reachable on (any port, a LAN IP, `localhost`, or a
tailnet name). Anything else is rejected, and the rejection is logged once per
connection naming `VOICE_WS_ORIGINS`. This is a behavior change from earlier
versions, where unset meant no restriction at all — if your setup relies on a
page served from a genuinely different host opening this socket, set
`VOICE_WS_ORIGINS` explicitly (above) to keep that working. Setting the env
var at all — to any value, including just `-` — fully overrides this default
and keeps the exact allowlist semantics described above.

If you want genuinely separate conversations per device, don't look for a
toggle — run a **second pipeline instance** on another port (`--ws_port 8766`
plus a second systemd unit) and point the other device at it. Both instances
can share the same LLM endpoint, which is stateless per request; the cost is a
second copy of STT+TTS on CPU. Per-client sessions inside one instance would
require restructuring the upstream framework's single-conversation design and
is not planned.

## Prerequisites

- An **OpenAI-compatible chat-completions LLM endpoint** (local or remote).
- Enough CPU/GPU for **STT** (`parakeet-tdt`) and **TTS** (`pocket`, the
  default) — both run fine CPU-only; a GPU only helps the LLM. The optional
  `remote-speech` TTS backend (see *TTS backends*) offloads synthesis to
  another server entirely, at the cost of `pocket` still needing to be
  installed as its fallback.
- Python 3.10+, `git`.

## Self-host setup

```bash
# 1. Clone and pin the framework at the commit this pack was built against
git clone https://github.com/huggingface/speech-to-speech.git speech-to-speech-main
cd speech-to-speech-main
git checkout 1e63f7e9343e491809d0d60e64f7ea551dbe845a

# 2. Create a venv and install (CPU-only torch/torchaudio is fine)
python3 -m venv .venv
.venv/bin/pip install -e ".[kokoro]"
.venv/bin/pip install -e ".[pocket,websocket]"

# 3. Apply this cockpit's patch pack on top of the editable install
bash /path/to/patchbay/patches/apply.sh

# 4. Configure your brain(s)
cp /path/to/patchbay/brains.json.example /path/to/patchbay/brains.json
# edit brains.json: base_url / model / api_key(_file) for each backend you want.
# The pipeline locates this file via the BRAINS_JSON env var (default
# ~/speech-to-speech/brains.json) — export it to point at the file you just
# created (done in step 5 below), or move the file to that default path.
#
# NOTE the two directories are different on purpose:
#   ~/speech-to-speech-main/   the framework INSTALL tree (step 1, replaceable)
#   ~/speech-to-speech/        the CONFIG sidecar — brains.json, persona.json,
#                              model_overrides.json, voices/
# Config lives outside the install tree so reinstalling or upgrading the
# framework never touches your brains, personas, or cloned voices.

# 5. Run the pipeline, pointing --responses_api_base_url / --model_name at
#    your own endpoint (these are the flags patches/apply.sh's target reads
#    at startup; brains.json lets you add more brains to hot-swap between
#    afterwards):
BRAINS_JSON=/path/to/patchbay/brains.json \
.venv/bin/speech-to-speech \
  --mode websocket --ws_host 0.0.0.0 --ws_port 8765 \
  --stt parakeet-tdt --parakeet_tdt_device cpu \
  --tts pocket --pocket_tts_voice jean --pocket_tts_device cpu \
  --llm_backend chat-completions \
  --responses_api_base_url http://localhost:8084/v1 \
  --responses_api_api_key dummy \
  --model_name <your-model-name> \
  --responses_api_stream

# 6. Serve the cockpit
python3 /path/to/patchbay/webclient/serve.py --port 8770
```

Then open `http://<host>:8770` in a browser.

Key flags to point at your own infrastructure:

| Flag | Purpose |
|---|---|
| `--responses_api_base_url` | your OpenAI-compatible LLM endpoint |
| `--model_name` | model id served at that endpoint |
| `--stt` | STT backend (`parakeet-tdt` by default) |
| `--tts` | TTS backend (`pocket` by default; `remote-speech` is opt-in, see *TTS backends*) |
| `--remote_speech_base_url` | `remote-speech` server URL (any OpenAI-compatible `/v1/audio/speech` endpoint); unset disables the backend |
| `--remote_speech_fallback_preload` | preload `remote-speech`'s `pocket` fallback at startup instead of on first failover (default `False`) |
| `--ws_port` | WebSocket port the cockpit connects to |

See `patches/README.md` for what the patch pack changes and why, and
`systemd/*.template` for a reference of running both processes as systemd
services.

## Bundled assets & what you supply

**Bundled:** six ready avatar heads in `webclient/avatar/model/` (mirrored from
the [TalkingHead](https://github.com/met4citizen/TalkingHead) repo's own public
distribution) — per-file licensing in `webclient/avatar/model/LICENSE-NOTE.txt`
(`mpfb.glb` is CC0; the rest are non-commercial/personal-use). The HeadAudio
viseme model (`model-en-mixed.bin`) and vendored three.js/TalkingHead/HeadAudio
libraries are included (MIT).

**You supply:** a still image at `webclient/avatar/refs/<name>.png` (gitignored)
if you want the 2D still-image avatar — it's lip-synced by
`webclient/avatar/avatar2d.mjs`; and your own theme reference images if you use
the theming tools under `webclient/themes/`. You can also drop in any extra
TalkingHead-compatible GLB and add one line to `AVATAR_REGISTRY` in
`webclient/index.html`.

## Optional integrations

The patch pack reads a few env vars for optional local-service integrations.
All are optional with localhost defaults — ignore them if you don't run those
services; the core voice agent (LLM brain + STT + TTS) works without any of
them.

| Env var | Default | Purpose |
|---|---|---|
| `BRAINS_JSON` | `~/speech-to-speech/brains.json` | path to your brain registry |
| `HERMES_SHIM_URL` | `http://localhost:8087/v1/chat/completions` | optional Hermes "shim" brain endpoint |
| `HERMES_SHIM_TOKEN_FILE` | `~/.hermes/shim.env` | optional Hermes shim token file |
| `HERMES_MCP_URL` | `http://localhost:8088/mcp` | optional Hermes MCP endpoint (cockpit brain) |
| `HERMES_TARGET` | *(unset — required for `send_to_hermes`)* | Hermes message target, `platform:chat_id` per Hermes `channels_list` |
| `QMD_MCP_URL` | `http://localhost:8070/mcp` | optional QMD knowledge endpoint (voice tools) |
| `VOICE_TOOLS` | *(unset)* | pin the armed voice-tool set to a comma-separated list |
| `VOICE_TOOLS_DIR` | *(unset)* | directory of drop-in local voice tools (one `.py` per tool) |
| `VOICE_MODEL_OVERRIDES_FILE` | next to your persona file, `model_overrides.json` | where per-brain model overrides set from the panel are stored |
| `VOICE_CHOICE_FILE` | next to your persona file, `voice_choice.json` | where the voice picked from the panel is stored, so it survives a restart |
| `VOICE_CLONE_DIR` | `~/speech-to-speech/voices` | where custom (cloned) voice states are stored |
| `VOICE_AUDITION_TEXT` | `Hi, I'm {name}. This is how I sound.` | sample spoken after a voice switch; `off` disables |
| `VOICE_PHONE_CONTEXT` | *(unset)* | set to `off` to disable phone context server-side, even if a client has it toggled on |
| `GENESIS_API_URL` | `http://localhost:8080` | optional Agent-Genesis endpoint; adds a conversation-history lane to `knowledge_lookup`. Probed at startup — if nothing answers, the lane is silently dropped |
| `FAULKNER_API_URL` | `http://localhost:8086` | optional Faulkner-DB endpoint backing `decision_lookup`. Same probe-or-drop behaviour |

### Behaviour & safety knobs

These are not integrations — they bound or tune what the pipeline already
does. All have working defaults; you only need them if you want to change
the behaviour described.

| Env var | Default | Purpose |
|---|---|---|
| `VOICE_STREAM_MAX_S` | `120.0` | hard ceiling on a single LLM streaming response, in seconds. Exists because an HTTP read timeout does not bound *total* duration — a model trickling one token at a time can hold the thread open indefinitely without ever tripping a read timeout. `off` disables |
| `VOICE_MAX_TOKENS` | `1024` | `max_tokens` sent with every LLM request. A spoken answer never needs more, and it bounds a runaway generation. `off` disables |
| `VOICE_TOOL_CALL_CAP` | `8` | maximum tool calls the model may make in one turn before it is cut off, so a tool loop cannot run forever |
| `VOICE_TOOL_FILLER_EVERY` | `3` | how often a spoken filler ("Let me check.") plays during a tool chain — every Nth round, so rounds 1, 4, 7… A long chain doesn't chatter |
| `VOICE_ECHO_GATE` | *(unset — off)* | server-side echo/speech discrimination, so the assistant does not hear itself. `on` gates, `observe` scores and logs without ever dropping audio, `off`/unset disables. Start with `observe` and read the logs before enabling |
| `VOICE_HERMES_ANNOUNCE` | *(on)* | speak a short notice when a Hermes delegation completes. `off` disables |
| `VOICE_REFLEX` | *(unset)* | set to `1` to enable the reflex lane — a fast path that answers a few intents without a full LLM round trip |
| `VOICE_CAMERA_FRAME` | `/dev/shm/voice_camera_frame.jpg` | where a client-pushed camera frame is written for the vision tool to read |
| `VOICE_SCREEN_FRAME` | `/dev/shm/voice_screen_frame.jpg` | same, for a shared screen frame |
| `VOICE_WS_ORIGINS` | *(unset — no-Origin clients + same-Host origins only)* | comma-separated allowlist of page origins permitted to open the WebSocket, fully overriding the default. The entry `-` also admits clients that send no `Origin` header (scripts, health probes). See *Who can connect* above — this is the control for cross-site WebSocket hijacking, which a VPN cannot stop |
| `VOICE_SYSTEM_RULES` | *(built-in text)* | overrides the pipeline-level system instruction appended to every request for every brain. It is a pipeline invariant, not a persona — it exists because some models collapse answer length as history grows. `off` disables |
| `VOICE_PERSONA_FILE` | `~/speech-to-speech/persona.json` | where personas set from the panel are stored (config sidecar, survives a framework reinstall) |
| `VOICE_TOOL_FILLERS` | *(seven built-ins)* | pipe-separated (`\|`) list of spoken fillers, since a phrase may contain a comma. `off` disables |
| `VOICE_WAKE_WORD` | *(unset — off)* | boot default for the join-deaf wake-word gate. The settings panel can toggle it live without a restart |
| `VOICE_WAKE_WORD_MODEL` | `hey_jarvis` | openWakeWord model name, or a path to a custom `.onnx`. Drop a trained model into openWakeWord's models directory and it appears in the panel's dropdown |
| `VOICE_WAKE_WORD_THRESHOLD` | `0.5` | detection score gate. Near-misses at or above `0.25` log at INFO so a real attempt that fell short still leaves evidence to calibrate against |
| `VOICE_WS_CERTFILE` / `VOICE_WS_KEYFILE` | *(unset)* | when both are set, the pipeline starts a second TLS listener alongside the plain one. See *Remote access / HTTPS* in `patches/README.md` — and note the client must dial the port the server listens on |
| `VOICE_WSS_PORT` | `8443` | port for that TLS listener. This is server-side config the browser cannot see; if you change it, tell the client too (settings panel or `?ws=`) |

Secret-bearing vars (`HA_TOKEN` and friends) should not be set as
`Environment=` lines in a systemd unit — unit files are world-readable via
`systemctl cat`. Put them in a mode-0600 env file and load it with
`EnvironmentFile=` (see the commented block in
`systemd/patchbay.service.template`); the vars reach the process
identically either way.

## Voice tools

The voice agent's LLM can call a small set of server-side tools (defined in
`patches/voice_tools.py`). Their spoken results are kept short and TTS-friendly.

| Tool | What it does | Backing service |
|---|---|---|
| `get_weather` | Current conditions for a place | Open-Meteo (public web API) |
| `web_search` | Search the public web; results also appear as clickable links on screen | DuckDuckGo (`ddgs`) |
| `knowledge_lookup` | Search your own notes, projects, and research | QMD MCP (`QMD_MCP_URL`) |
| `set_mood` | Set the interface/avatar mood | none — client-side visual only |
| `delegate_to_hermes` | Hand off a long-running / multi-step task to Hermes | Hermes shim (`HERMES_SHIM_URL`) |
| `hermes_status` | Report Hermes' status (summary, last result, recent steps, or pending approvals) | Hermes MCP (`HERMES_MCP_URL`) |
| `send_to_hermes` | Send a quick free-form message / follow-up to Hermes | Hermes MCP (`messages_send` → `HERMES_TARGET`) |

**Approving a Hermes request is deliberately not a voice tool.** Hermes asks
for approval precisely because an action needs a human, so the approval lives
in the cockpit's hold-to-approve gate — which names the specific request and
takes a deliberate gesture — and not in anything the model can call. A tool
would have been reachable from any text the model attended to, including the
body of a web-search result or a knowledge-base document, and no tool
description defends against a document that says "approve the pending
request".

Arming is **availability-probed once at pipeline start**: the weather, search,
and mood tools are always armed; `knowledge_lookup` is armed only if the QMD MCP
endpoint answers a probe; the four Hermes tools are armed only if the Hermes MCP
endpoint answers a probe. This keeps a self-hoster without those services from
arming dead tools the LLM would call and then narrate as failures. Set
`VOICE_TOOLS=<comma-list>` to pin the set explicitly (no probing — only listed
names that exist are kept). Probing happens at startup only, so **restart the
pipeline to re-arm** after bringing a service up or down. The settings UI's
"N armed" count shows the result. `web_search` results also surface as a
clickable **Links card** in the cockpit.

### Add your own tools

Set `VOICE_TOOLS_DIR=/path/to/dir` to drop in extra tools without editing repo
files — one `.py` per tool exposing a `TOOL_DEF` dict and a `run()` callable
(see `examples/tools/` for five ready-to-copy tools (clock, model-server status, service health, GPU status, news headlines) — edit each file's EDIT-ME constants for your own endpoints). This **executes your Python on your
box**, so treat the directory with the same trust as editing config. Drop-ins
arm unconditionally when the directory is set (unless `VOICE_TOOLS` pins the
list); a broken file is skipped with a logged warning rather than crashing the
pipeline. No restart needed to pick up changes — the settings panel's
"Reload tools" button re-probes and re-arms live (also
`{"type":"config_set", "reload_tools":true}` over the WS).

## TTS backends

`pocket` is the default and requires nothing beyond what *Prerequisites*
already lists — nothing here changes if you never touch `--tts`.

`remote-speech` is an **opt-in** backend for any server implementing OpenAI's
`POST /v1/audio/speech` protocol — defined by that protocol, not by any
particular piece of hardware; OpenAI itself qualifies too. Point
`--remote_speech_base_url` at such a server and pass `--tts remote-speech`;
`pocket` stays installed underneath it as an **automatic, never-silent
fallback** — an unreachable, erroring, empty, truncated, or too-slow remote
response falls through to `pocket` transparently rather than going mute. By
default `pocket`'s weights aren't loaded until the first such failover, at a
measured **~2.1s** cost on that one utterance; pass
`--remote_speech_fallback_preload` to preload them at startup instead, if
that first-fallback latency matters more to you than the extra idle memory.

```bash
.venv/bin/pip install -e ".[pocket,websocket]"   # remote-speech's fallback still needs pocket

.venv/bin/speech-to-speech \
  ... \
  --tts remote-speech \
  --remote_speech_base_url http://<your-tts-server>:<port> \
  --pocket_tts_voice jean --pocket_tts_device cpu \
  ...
```

**The expression/prosody handle.** `remote-speech`'s `instructions` field is
the reason this backend exists: unlike `pocket` (good default prosody, no
expression control) or `kokoro` (flat), a server implementing this protocol
can be told *how* to say something, not just *what*. Set the startup value
with `--remote_speech_instructions "..."`. While the pipeline is running, the
same handle is live-settable — no restart — as `speech_style` over the
WebSocket control channel:

```json
{"type": "config_set", "speech_style": "cheerful and upbeat, like a game show host"}
```

`config_get` reports the current value back. Clearing it (empty or
whitespace) reverts to the default rather than ever sending an empty value —
an empty `instructions` field is a verified silent failure mode (HTTP 200,
zero bytes of audio) on at least one such server. `config_set {speech_style:
...}` against `pocket` or `kokoro` returns a clear error naming the active
backend instead of silently doing nothing, since neither has this handle.

## Custom voices (voice cloning)

The settings panel's **Advanced — custom voice** section lets you add your own
voices to the dropdown: record 10–30 seconds of speech in the browser, or
upload a clip (`.wav`, `.aiff`, `.flac`, `.ogg`, `.mp3` — not `.webm`/`.m4a`;
convert those first). The server builds a pocket-tts voice state from it once
(a few seconds of CPU), stores it under `VOICE_CLONE_DIR`
(default `~/speech-to-speech/voices/`, safe across framework reinstalls), and
the voice appears in the dropdown on every connected client. Switching voices
speaks a short audition sample so you hear the result immediately
(`VOICE_AUDITION_TEXT`, set to `off` to disable). Clips longer than 30 seconds
are truncated to the first 30. Clean audio matters: background noise, echo,
and compression artifacts become part of the cloned voice, so record somewhere
quiet (Kyutai recommends cleaning the sample first — e.g. Adobe's free
[Enhance Speech](https://podcast.adobe.com/en/enhance)).

**One-time setup** — pocket-tts ships its clone-capable weights behind a
Hugging Face terms gate. Until you complete this, the cockpit shows a
friendly error instead of building voices (already-built custom voices keep
working regardless):

1. Accept the terms at <https://huggingface.co/kyutai/pocket-tts>
   (any HF account; approval is automatic).
2. On the box that runs the pipeline, log that account in as the service
   user: `hf auth login` (or place a token at `~/.cache/huggingface/token`).
3. Restart the pipeline service — it re-downloads the model weights with
   cloning enabled (~220 MB, one time).

`soundfile` must be installed in the pipeline's venv for non-WAV uploads and
recording normalization: `pip install soundfile`.

**Consent notice**: pocket-tts's license prohibits "voice impersonation or
cloning without explicit and lawful consent." Clone only voices you have the
right to clone — your own, or a consenting speaker's.

**Persistence.** Whichever voice you pick in the panel — built-in or
cloned — is remembered across restarts, so it survives a service restart
without editing the unit file's `--pocket_tts_voice`/`--remote_speech_base_url`
argv. It's stored next to your persona file as `voice_choice.json`
(override the location with `VOICE_CHOICE_FILE`). To reset, delete that file
or just pick another voice from the panel.

## Phone context (optional)

The settings panel has a **Phone context** toggle, **off by default**: "Share
location & phone state". When you turn it on, the browser streams your
approximate location (via the W3C Geolocation API), timezone, and battery
level/charging state to your own voice server over the same WebSocket the
rest of the cockpit already uses — no new endpoint, no third-party service.
This lets tools like `get_weather` and the LLM's sense of "now"/"here" work
without you naming a place every time.

It's sent only to your voice server, but the ambient line it produces reaches
whichever brain is currently active — including a remote/cloud brain, if
that's what you've selected in the Brain panel. If that matters to you,
switch to a local brain before enabling it, or leave it off.

Location updates are throttled client-side (moved >100m or 5+ minutes since
the last send) and expire server-side after 30 minutes of staleness. Denying
the browser's location permission prompt turns the toggle back off. Set
`VOICE_PHONE_CONTEXT=off` on the server to disable the feature entirely,
regardless of what any client has toggled.

Any browser works — a phone gives GPS-grade accuracy, a desktop typically
falls back to coarser IP/network-based geolocation, which is still useful for
timezone/weather purposes.

## Tests

```bash
python3 -m pytest patches/ examples/ webclient/ -q
```

`patches/` and `examples/` are plain unit tests and need nothing beyond
`pytest`. `webclient/test_webclient.py` is different: it drives the real
`index.html` in a headless Chromium with a fake microphone, against a stub
control websocket, to cover client behaviour no unit test can reach — the
wake-word mic handoff, the permission gate, the settings dialog's focus trap.
It needs a browser:

```bash
pip install playwright websockets && python3 -m playwright install chromium
```

Without those it skips rather than fails, so the suite still passes on a
machine that only runs the pipeline — but then the client is untested, so
check for `s` in the pytest output before trusting a green run. Both of its
servers bind ephemeral ports on localhost, so it is safe to run on the same
box as a live agent.

## Attribution & acknowledgments

This project borrows ideas as well as code, and gladly says so:

- [`huggingface/speech-to-speech`](https://github.com/huggingface/speech-to-speech) (Apache-2.0) — the STT/LLM/TTS pipeline this cockpit drives.
- [`met4citizen/TalkingHead`](https://github.com/met4citizen/TalkingHead) + HeadAudio by Mika Suominen (MIT) — the 3D avatar + audio-driven lip-sync approach, the ready-head roster, and the Blender avatar pipelines that shaped this project's avatar architecture.
- Ready Player Me, AvatarSDK, Avaturn, VRoid Studio, and the MakeHuman/MPFB community — creators of the bundled example heads.
- Classic visual-novel / Live2D-style talking portraits — the inspiration for the 2D still-image avatar path.

See `NOTICE` for full attribution and per-asset licensing.
