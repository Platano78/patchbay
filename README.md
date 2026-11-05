# Patchbay

Patchbay — a local voice instrument. Talk to your own models on your own
hardware.

![The Patchbay cockpit: an analog-instrument faceplate with a level gauge,
talk key, control deck, transcript tape and a silkscreened command legend](docs/screenshot-instrument.png)

*A freshly installed instance — no brain configured, no deck provider, nothing
connected yet. The deck fills in from whatever state provider you point it at
(Home Assistant is the one implementation shipped today), and the legend plate
lists whichever tools your pipeline actually armed at startup.*

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
framework itself is fetched at install time and your model endpoints are
yours. Example 3D avatar heads ship with it (see *Bundled assets*); the
optional 2D still-image avatar is an image you supply.

## Who this is for

People who self-host: you already run (or can reach) an OpenAI-compatible
chat-completions model server — llama.cpp, Ollama, vLLM, LM Studio, or a
hosted API — and want to talk to it by voice from a browser. It runs on
Linux; speech-to-text and text-to-speech run on the CPU, so no GPU is
needed on this side. It is not a hosted product: there are no accounts and
no authentication, and it is meant to run on your own machine or network.
Start with [SETUP.md](SETUP.md); agents acting for you start with
[AGENTS.md](AGENTS.md).

## Architecture

```
 browser (webclient/index.html)
        │  WebSocket  ws://<host>:8765
        ▼
 speech-to-speech pipeline (patched)
   ├─ STT   — parakeet-tdt (default; remote-transcription opt-in, see *STT backends*)
   ├─ LLM   — OpenAI-compatible chat-completions endpoint ("brain")
   └─ TTS   — pocket (default; remote-speech opt-in, see *TTS backends*)
        ▲
        │  HTTP :8770 (static files, /models, /ha/* when configured)
 webclient/serve.py
```

- The browser cockpit connects to the pipeline over a single WebSocket
  (`ws://<hostname>:8765`) for audio in/out plus JSON control messages
  (`config_get`/`config_set` — brain selection, persona text, chat reset).
- `webclient/serve.py` (stdlib only) serves, on `:8770`: the cockpit's own
  static files; `/models` and `/v1/models`, a small JSON status reporting
  whether the pipeline's WebSocket port is listening; and — only when both
  `HA_URL` and `HA_TOKEN` are set in its environment — the Home Assistant
  deck routes `/ha/states`, `/ha/stream` and `/ha/intent` (see *Optional
  integrations*). It binds `127.0.0.1` unless you pass `--host`.
- The LLM ("brain") is any OpenAI-compatible `chat-completions` endpoint —
  local (llama.cpp, vLLM, Ollama, etc.) or hosted. `patches/brain_control.py`
  lets you register several brains in `brains.json` and hot-swap between them
  from the cockpit UI without restarting the service. A brain entry can carry
  a `"type"` (`openai-compatible`, `openrouter`, `nvidia-nim`, `anthropic`,
  `agent`) that supplies its `base_url` and `api_key_var` for you — see
  *Hosted brains* below.
- A brain's endpoint can serve more than one model — an NVIDIA NIM endpoint
  serves hundreds, a llama.cpp router a handful — so `brains.json`'s `model`
  field is only the *configured default*. Add a `"models": ["id", ...]` array
  to a brain entry to curate the list the settings panel offers for it; if you
  don't, the panel falls back to whatever the last live `/v1/models` probe of
  that endpoint reported. Either way, picking a model from the panel writes a
  per-brain override (persisted to a sidecar file, see `VOICE_MODEL_OVERRIDES_FILE`
  below) that beats the configured default until you clear it — it never edits
  `brains.json` itself.
- A brain entry may also carry `"chat_template_kwargs": {"flag": value, ...}`
  — extra chat-template flags your model understands (for example a
  conversational-mode switch). They are merged over the defaults Patchbay
  sends (`enable_thinking: false`, plus `reasoning_effort` if the entry sets
  one), your keys winning. On a hosted lane, or the official OpenAI
  endpoint, nothing is sent by default, so only your keys go out (plus
  `enable_thinking: false` and the effort if the entry also sets a valid
  `reasoning_effort`).
  Anything that is not an object with string keys is logged and ignored. A
  template flag that adds its own brevity instruction overlaps
  `VOICE_SYSTEM_RULES`; set `VOICE_SYSTEM_RULES=off` if you want only the
  model's.

### Finding your local model server

Not sure what to put in `brains.json`? `patches/brain_discovery.py` checks
whether something is already listening on the usual local model-server ports
(Ollama, LM Studio, llama.cpp `llama-server`, vLLM/LocalAI, KoboldCpp, Jan)
and prints what it finds, including a ready-to-paste `brains.json` entry.
`setup.sh` runs it automatically right after copying `brains.json.example`;
run it again any time with:

```bash
~/speech-to-speech-main/.venv/bin/python3 patches/brain_discovery.py
```

It's **loopback-only by default** — probing your LAN unprompted isn't this
tool's job. Every run says which target set it scanned on its first line, so
a result can't be mistaken for more coverage than it had.

If your model server runs on another box, ask for it explicitly:

```bash
# one or more named hosts (repeat --host)
python3 patches/brain_discovery.py --host 10.0.0.20 --host 10.0.0.30

# or a whole private block, same known port list
python3 patches/brain_discovery.py --cidr 10.0.0.0/24
```

`--cidr` takes **private address space only** and at most a `/24` (256
addresses); anything wider or public is refused outright rather than
half-scanned. `--host` is unrestricted — you named it. Both are bounded: at
most 64 probes in flight, `--timeout` seconds each (default 1s), so a full
`/24` is a few tens of seconds, and the tool prints its own worst-case
estimate before it starts.

For a port this tool doesn't know about, `BRAIN_DISCOVERY_URLS`
(comma-separated full base URLs) still names endpoints directly; it replaces
the default port list rather than adding to it, and `--host`/`--cidr` win
over it when both are set.

It only finds servers that are already running — it starts nothing, and it
never writes `brains.json` for you (the printed snippet is yours to paste
in). Also reachable live, once the pipeline is up: the settings panel's
"Scan for local models" button under **Brain**, for adding a second brain
without a restart — that button is loopback-only, the LAN modes are the
CLI's.

### Hosted brains

Prefer to rent a model instead of running one? A `brains.json` entry can
carry a `"type"` field — `openai-compatible` (the default, for your own
server), `openrouter`, `nvidia-nim`, or `anthropic` — and the type supplies
that provider's `base_url`, `api_key_var`, and (where it has one) a default
model, so you don't have to look them up yourself. `python3
patches/brain_lanes.py show <type>` prints the required fields, where to get
a key, and a paste-ready entry; for example:

```
$ python3 patches/brain_lanes.py show openrouter
OpenRouter
  type        openrouter   (kind: hosted)
  base_url    https://openrouter.ai/api/v1   -- supplied by the type; you may omit it
  API key     REQUIRED, as OPENROUTER_API_KEY
              get one at https://openrouter.ai/keys
              put it in a mode-0600 file as `VAR=value`, then point
              api_key_file at that file -- an absolute path or
              `~/...`, both work
  model       no default: this provider's ids drift, so pick a live one
              "auto" is NOT safe on this lane: it exposes no loaded-model
              status, so "auto" means "whatever is first in the catalogue"
  ...
```

Three hosted providers ship types today:

| Provider | `type` | Key console | Free tier |
|---|---|---|---|
| OpenRouter | `openrouter` | <https://openrouter.ai/keys> | `:free`-suffixed model ids |
| NVIDIA NIM | `nvidia-nim` | <https://build.nvidia.com/settings/api-keys> | free credits on signup, no card |
| Anthropic (OpenAI-compat) | `anthropic` | <https://platform.claude.com/settings/keys> | none |

The key itself goes in a file named by `api_key_file` (absolute path or
`~/...`), one `VAR=value` line per key (`#` comments and blanks ignored,
mode `0600`), with `api_key_var` naming which line to read.

`"model": "auto"` is **not safe** on any hosted lane — unlike a self-hosted
server, none of these three expose a loaded-model status, so `"auto"` just
means "whatever is first in the provider's catalogue". Pick a real model id
(`brain_lanes.py show <type>` prints a live spread from the provider's own
`/v1/models`).

Anthropic's own docs call its OpenAI-compatible layer "not considered a
long-term or production-ready solution … primarily intended to test and
compare model capabilities" — it works, but treat it as a way in, not a
destination.

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

The first run in [SETUP.md](SETUP.md) binds both processes to `127.0.0.1`
(the pipeline's `--ws_host` default is patched to `127.0.0.1`, and
`serve.py` already defaults to it), so only this machine can connect. The
systemd templates bind `0.0.0.0` — every interface, including your LAN — and
so does any command you change to `--ws_host 0.0.0.0` / `--host 0.0.0.0` for
LAN or phone access. Once you do, two things follow:

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

**Unset (the default) is not "no restriction", since v2.0.0.** It
accepts (a) clients that send no `Origin` header at all — every
non-browser client, same as the `-` entry above — and (b) pages whose Origin
hostname matches the Host header the connection came in on, i.e. the same box
the socket itself is reachable on (any port, a LAN IP, `localhost`, or a
tailnet name). Anything else is rejected, and the rejection is logged once per
connection naming `VOICE_WS_ORIGINS`. This is a behavior change from versions
before v2.0.0, where unset meant no restriction at all — if your setup relies on a
page served from a genuinely different host opening this socket, set
`VOICE_WS_ORIGINS` explicitly (above) to keep that working. Setting the env
var at all — to any value, including just `-` — fully overrides this default
and keeps the exact allowlist semantics described above.

**DNS rebinding is stopped by a separate Host-header check** — in both
processes (the pipeline's socket and `serve.py`), including with
`VOICE_WS_ORIGINS` set. The check blocks rebinding through hostnames you
have *not* allowed: any name you name in `VOICE_WS_ORIGINS` or
`VOICE_ALLOWED_HOSTS` is trusted, and `VOICE_ALLOWED_HOSTS=*` turns the
check off.
Origin checks cannot see that attack (a rebinding page sends a matching Origin *and* Host, both
naming the attacker), so the one reliable signal is checked: a request whose
Host is not on the allowlist gets `403 host not allowed`, logged once per
distinct `Host` header value. Without any configuration the allowlist
accepts: IP
addresses, `localhost` (and `*.localhost`), this machine's own names — its
hostname, FQDN, `<hostname>.local`, and its Tailscale MagicDNS name when
Tailscale is installed — and every hostname named in `VOICE_WS_ORIGINS`. A
different name you legitimately reach the box under goes in
`VOICE_ALLOWED_HOSTS` (below). See [SECURITY.md](SECURITY.md).

If you want genuinely separate conversations per device, don't look for a
toggle — run a **second pipeline instance** on another port (`--ws_port 8766`
plus a second systemd unit) and point the other device at it. Both instances
can share the same LLM endpoint, which is stateless per request; the cost is a
second copy of STT+TTS on CPU. Per-client sessions inside one instance would
require restructuring the upstream framework's single-conversation design and
is not planned.

## Setup

Self-hosting this requires Linux, an OpenAI-compatible chat-completions LLM
endpoint (local or remote), Python with its `venv` module (tested on 3.10,
3.12 and 3.14), `git`, `curl`, and about 6 GB of disk for the framework,
its venv and the downloaded model weights. STT (`parakeet-tdt`) and TTS
(`pocket`, the default) both run CPU-only — a GPU only helps the LLM you
point it at. SETUP.md step 0 lists every download with its size and
licence.

**Full step-by-step runbook: [SETUP.md](SETUP.md).** It's written to be
followed exactly — one command per step, an expected output to check
against, and a fallback for when it doesn't match — including `./setup.sh
--check` to verify an existing install, configuring `brains.json`, a
no-microphone check that a whole turn works (`examples/smoke_turn.py`), the
`patches/README.md` reference for what the patch pack changes, and
`systemd/*.template` for running both processes as services (system units
with sudo, or user units without).

## Bundled assets & what you supply

**Bundled:** twelve example 3D avatar heads (GLB) in `webclient/avatar/model/`.
They are **not uniformly licensed** — per-file terms are in
`webclient/avatar/model/LICENSE-NOTE.txt`:

- Six are mirrored from the [TalkingHead](https://github.com/met4citizen/TalkingHead)
  repo's own public distribution. `mpfb.glb` is CC0. `brunette.glb` and
  `brunette-t.glb` (Ready Player Me) are CC BY-NC 4.0. `avatarsdk.glb`
  (AvatarSDK), `avaturn.glb` (Avaturn) and `vroid.glb` (VRoid Studio) are for
  non-commercial use.
- Six (`old-man`, `young-woman`, `heavyset-neutral`, `professional-woman`,
  `young-man`, `elder-woman`) were generated for this project from MakeHuman
  Community CC0 asset packs; the mesh, morph and texture content is CC0, and
  the embedded rig derives from TalkingHead's MIT-licensed MPFB rig.

The MIT `LICENSE` covers Patchbay's own code; it does not relicense these
assets. For any non-personal use, check each head's terms or replace it with
one you have rights to. The HeadAudio viseme model (`model-en-mixed.bin`)
and vendored three.js/TalkingHead/HeadAudio libraries are included (MIT).

**You supply:** a still image at `webclient/avatar/refs/<name>.png` (gitignored)
if you want the 2D still-image avatar — it's lip-synced by
`webclient/avatar/avatar2d.mjs`; and your own theme reference images if you use
the theming tools under `webclient/themes/`. You can also drop in any extra
TalkingHead-compatible GLB and add one line to `AVATAR_REGISTRY` in
`webclient/index.html`.

## Optional integrations

A separate, optional GPU service for turn-taking (a sidecar with its own venv; needs a CUDA or ROCm GPU with 4 GB free) lives in [`services/tyger/`](services/tyger/README.md).

The patch pack reads a few env vars for optional local-service integrations.
All are optional with localhost defaults — ignore them if you don't run those
services; the core voice agent (LLM brain + STT + TTS) works without any of
them.

| Env var | Default | Purpose |
|---|---|---|
| `BRAINS_JSON` | `~/speech-to-speech/brains.json` | path to your brain registry |
| `HERMES_SHIM_URL` | `http://localhost:8087/v1/chat/completions` | optional Hermes "shim" brain endpoint — see *Agent lane (optional)* below |
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
| `HA_URL` / `HA_TOKEN` | *(unset — deck off)* | read by **`webclient/serve.py`**, not the pipeline: when both are set (HA's base URL, e.g. `http://homeassistant.local:8123`, and a long-lived access token), `serve.py` holds a connection to Home Assistant and serves the cockpit's control deck at `/ha/states`, `/ha/stream` and `/ha/intent`; with either unset those routes don't exist. The token never leaves `serve.py`. Only a fixed list of domains and services is forwarded (lights, switches, fans, covers, climate, scenes, **locks — including unlock**, and `homeassistant` on/off/toggle). `/ha/intent` only accepts a `Content-Type: application/json` POST (else `415`) whose `Origin`, if the browser sent one, has the same hostname as the `Host` it was sent to (else `403`). There is still no authentication: anyone who can reach `serve.py` can operate those devices, so with HA configured keep `serve.py` on `127.0.0.1` or put your own authentication in front of it (see [SECURITY.md](SECURITY.md)). The `home_assistant` example tool reads the same two variables in the *pipeline's* environment |

### Agent lane (optional)

**Bring your own agent.** This is a plain, documented contract — an
OpenAI-compatible chat-completions endpoint plus four MCP tools
(`events_poll`, `permissions_list_open`, `permissions_respond`,
`messages_send`) — that any background agent can implement, not a
maintainer-only service. Wire up your own agent to the same shapes and it
plugs into the cockpit's delegate/status/approve lane. See
[`docs/agent-lane.md`](docs/agent-lane.md) for the full spec,
[`examples/agent-lane/`](examples/agent-lane/) for a runnable reference
server to develop and test against, and
[`examples/agent-lane/verify_contract.py`](examples/agent-lane/verify_contract.py)
to check your own shim against the contract — it's stdlib-only, so you can
run it before you have a pipeline installed at all.

Hermes is the maintainer's own implementation of this contract — **a
separate, self-hosted agent service**, not part of Patchbay, not bundled,
and not required. It's the backing service for the three `*_hermes` voice
tools below. Most self-hosters will never set this up, and that's the
normal case, not a missing feature: when nothing answers at
`HERMES_MCP_URL`, the Hermes tools simply stay unarmed and the rest of the
voice agent works exactly the same. Arming is decided by that MCP probe
alone — a missing or unreachable shim (`HERMES_SHIM_URL`) or token file
does not disarm anything: `delegate_to_hermes` still hands the task off,
and the delegation then ends in an error on the cockpit's delegation card
(spoken too, unless `VOICE_HERMES_ANNOUNCE=off`). If you do run something
that speaks the same shim/MCP surface — Hermes or your own — point the
`HERMES_*` env vars above at it; those names stay as shipped API regardless
of what implements the other end.

### Behaviour & safety knobs

These are not integrations — they bound or tune what the pipeline already
does. All have working defaults; you only need them if you want to change
the behaviour described.

| Env var | Default | Purpose |
|---|---|---|
| `VOICE_STREAM_MAX_S` | `120.0` | hard ceiling on a single LLM streaming response, in seconds. Exists because an HTTP read timeout does not bound *total* duration — a model trickling one token at a time can hold the thread open indefinitely without ever tripping a read timeout. `off` disables |
| `VOICE_MAX_TOKENS` | `1024` | `max_tokens` sent with every LLM request. A spoken answer never needs more, and it bounds a runaway generation. `off` disables |
| `VOICE_TOOL_CALL_CAP` | `8` | maximum tool-call **rounds** in one turn (one model response may hold several calls; each response counts once) before it is cut off, so a tool loop cannot run forever; `0`/`off` disables |
| `VOICE_TOOL_FILLER_EVERY` | `3` | how often a spoken filler ("Let me check.") plays during a tool chain — every Nth round, so rounds 1, 4, 7… A long chain doesn't chatter |
| `VOICE_ECHO_GATE` | *(unset — off)* | server-side echo/speech discrimination, so the assistant does not hear itself. `on` gates, `observe` scores and logs without ever dropping audio, `off`/unset disables. Start with `observe` and read the logs before enabling; `journalctl -u patchbay -o cat | python3 examples/echo_gate_report.py` summarises those logs and says whether any echo was ever measured |
| `VOICE_BARGE_CANCEL` | *(on)* | when a push-to-talk press lands on a reply, the browser tells the server (`barge_in`) and the server stops producing it: the LLM stream, the speech synthesis and the queued audio are cancelled, so the old reply never resumes after the new question. What the model had already said is kept in its chat history. `off` restores the earlier behaviour, where only the browser's own playback stopped |
| `VOICE_PTT_HOLD` | *(on)* | the browser tells the server when push-to-talk is held (`ptt` `down`/`up`); while held, the server withholds pauses from its silence rule so a pause mid-thought does not end the turn, and `up` ends it. Dropped after 120 s or when the socket closes. Clients that never send `ptt` are unaffected. `0` ignores the frames, restoring silence-only turn ends |
| `VOICE_OVERLAP_OBSERVE` | *(unset — off)* | a measurement instrument, not a feature: set to `1` (needs `VOICE_ECHO_GATE=observe` or `on`, otherwise it logs a warning and stays inert) and an open-mic client **streams the microphone to the server during replies** (the same data path the wake-word gate already receives while asleep). The server scores those frames with the echo gate, labelled `phase=playback`, and never sends them to the pipeline, so nothing changes in how the assistant listens or answers. Read the result with `examples/echo_gate_report.py`, which then also prints a per-playback-window table. Remove it after the collection session. Push-to-talk is unaffected |
| `VOICE_HERMES_ANNOUNCE` | *(on)* | speak a short notice when a Hermes delegation completes. `off` disables |
| `VOICE_PARROT` | *(unset)* | sets the **startup** state of **parrot mode**, a diagnostic: every turn is spoken back to you verbatim with no LLM involved, so you can hear whether the whole audio path — synthesis, streaming, playback, avatar — actually works. Not a conversational mode; the assistant will only ever repeat you. Set to `1` to start armed; leave unset to start disarmed. Toggle it live from the settings panel's Connection section afterwards — no restart needed, so you never have to kill the conversation you're diagnosing. Short-circuits a turn before the LLM sees it; turn it off again to restore normal conversation |
| `VOICE_CAMERA_FRAME` | `/dev/shm/voice_camera_frame.jpg` | where a client-pushed camera frame is written for the vision tool to read |
| `VOICE_SCREEN_FRAME` | `/dev/shm/voice_screen_frame.jpg` | same, for a shared screen frame |
| `VOICE_WS_ORIGINS` | *(unset — no-Origin clients + same-Host origins only)* | comma-separated allowlist of page origins permitted to open the WebSocket, fully overriding the default. The entry `-` also admits clients that send no `Origin` header (scripts, health probes). See *Who can connect* above — this is the control for cross-site WebSocket hijacking, which a VPN cannot stop |
| `VOICE_ALLOWED_HOSTS` | *(unset — IP addresses, `localhost`, this machine's own names incl. its Tailscale name, and `VOICE_WS_ORIGINS` hostnames are allowed)* | comma-separated list of extra hostnames accepted in the `Host` header of both the pipeline's WebSocket and the cockpit's HTTP server — the DNS-rebinding guard. Hostnames named in `VOICE_WS_ORIGINS` or listed here are trusted; the single value `*` turns the check off entirely (any Host accepted). See *Who can connect* above |
| `VOICE_SYSTEM_RULES` | *(built-in text)* | overrides the pipeline-level system instruction appended to every request for every brain. It is a pipeline invariant, not a persona — it exists because some models collapse answer length as history grows. `off` disables |
| `VOICE_DATE_ANCHOR` | *(on)* | appends `Today's date is <Weekday>, <Month> <D>, <YYYY>.` (the pipeline host's local date, no clock time, recomputed every request) to the system message, so the model does not guess its training-era year. `off` disables |
| `VOICE_AFFECT` | *(unset — off)* | per-turn delivery conditioning. The brain prefixes each reply with a short note — `[affect: what this moment is - how it should sound]` — which is stripped before the text is spoken or shown, and sent to the TTS server as that one request's `instructions` field. `on` enables the note; `words` additionally asks the brain to write the reply in the register its own note names. Needs a TTS backend that accepts `instructions` (a `remote-speech` server that does not declare `accepts_instructions: false`); an operator-set speech style always takes precedence over it. Off by default and byte-identical to absent when off. Measured effect is delivery — pacing and register — not laughter or other paralinguistic events |
| `VOICE_VOCAB_FILE` | `~/speech-to-speech/vocab.txt` | vocabulary for the post-transcription **vocab snap** (next section). Missing file = feature off; `off` disables even if the file exists |
| `VOICE_PERSONA_FILE` | `~/speech-to-speech/persona.json` | where personas set from the panel are stored (config sidecar, survives a framework reinstall) |
| `VOICE_TOOL_FILLERS` | *(seven built-ins)* | pipe-separated (`\|`) list of spoken fillers, since a phrase may contain a comma. `off` disables. If `scripts/render_fillers.py` has rendered a phrase in the active voice, the clip plays instantly; otherwise the phrase is spoken through the TTS as before |
| `VOICE_TURN_FILLER` | `off` | `on` plays one short filler from the same pool when a turn has ended and no reply audio has started `VOICE_TURN_FILLER_DELAY_MS` later. Only for turns that were transcribed (an unanswered or blank turn gets none), only from a pre-rendered clip in the active voice (no clip, no filler), never after reply audio started, never on top of a tool filler (and a tool filler skips if a turn filler just played), cancelled by new speech or a barge-in. Not recorded anywhere: audio only. A client that cannot tolerate a clip before the reply connects with `?turn_filler=0` on its websocket URL and is skipped |
| `VOICE_TURN_FILLER_DELAY_MS` | `600` | the wait above |
| `VOICE_TURN_FILLERS` | *(the tool pool)* | pool for the turn filler, same format as `VOICE_TOOL_FILLERS` (`Mm.\|One sec.\|Okay.\|Let me see.` suits a turn that is not a tool call) |
| `VOICE_DESIGN_TOOLS` | `on` | With the remote speech backend and a voicegate-style server (`/v1/voices/design`, `/v1/voices/clone`): arms the LLM tools `design_voice` ("talk like a grumpy lighthouse keeper" switches to a freshly designed voice for the rest of the conversation), `save_voice` ("keep this voice, call it Keeper") and `restore_voice` ("go back to your normal voice"), renders the new voice's fillers in the background, and routes the panel's voice-clone recording to the server (the result is a `designed:<id>` voice, listed and deletable under custom voices). `off` removes all of it; the local pocket clone path is never affected. Implementation: `patches/voice_design.py` |
| `VOICE_FILLER_DIR` | `~/.cache/patchbay/fillers` | clip cache, `<dir>/<voice>/<phrase-slug>.pcm` (raw s16le mono at the pipeline output rate). Filled by `scripts/render_fillers.py` |
| `VOICE_SMART_TURN` | `off` | end-of-turn classifier on the speech server (`smart-turn`, same host as `--remote_transcription_base_url`). `act`: when the silence rule ends an open-mic turn, ask the classifier first; if the turn sounds unfinished, keep listening up to `VOICE_SMART_TURN_HOLD_MS` and, if speech resumes, treat it as the same turn. `shadow`: only log `SMART_TURN_SHADOW complete=… p=… latency_ms=… turn_s=…`, never change anything. Costs up to 250 ms per open-mic turn end in `act`; any error or timeout behaves as if off. Push-to-talk turns are never classified |
| `VOICE_SMART_TURN_THRESHOLD` | `0.5` | probability above which the server calls a turn complete |
| `VOICE_SMART_TURN_HOLD_MS` | `1200` | how long `act` keeps listening after an unfinished-sounding turn before ending it anyway |
| `VOICE_WAKE_WORD` | *(unset — off)* | boot default for the join-deaf wake-word gate. The settings panel can toggle it live without a restart. Needs `openwakeword` + `onnxruntime` in the pipeline's venv (SETUP.md step 6 — its pre-trained models, `hey_jarvis` included, are CC BY-NC-SA 4.0, non-commercial); without them the setting is ignored with a warning and the panel's toggle is greyed out with the reason |
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

### Smart Turn and fillers (speech-server features)

Both need the `remote-transcription` / `remote-speech` backends on a speech server that serves `smart-turn` and the persona voices, and both are inert without their env var. Pre-render the clips once per voice, with the same environment as the service (so it renders the phrases the service would pick):

```sh
python3 scripts/render_fillers.py --base-url http://HOST:PORT --dry-run   # voices x phrases, writes nothing
python3 scripts/render_fillers.py --base-url http://HOST:PORT             # all voices; --voice ryan to limit
```

`--rate` must equal the pipeline output rate (the TTS handler's `remote_speech_sample_rate`, 16000 unless you set it) and `--source-rate` the rate the server returns (48000 for voxcpm2). Existing clips are skipped; `--force` re-renders. Turn fillers also need `VOICE_TURN_FILLER=on`, and Smart Turn `VOICE_SMART_TURN=act` (or `shadow` to only log what it would decide).

## Vocabulary snap (optional)

Speech recognisers mangle names they have never seen: "VoxCPM" comes back as "Vox EPM". If you
give the pipeline a list of the terms you use, it repairs those in the final transcript before the
assistant reads it, and the cockpit transcript shows the corrected text.

Create `~/speech-to-speech/vocab.txt` (or point `VOICE_VOCAB_FILE` at another path): one entry per
line, in the spelling you want, `#` starts a comment line. A line may also list **aliases**, the
known ways the recogniser mishears the term, separated by `|`:

```
# project names
Patchbay
Parakeet-TDT
VoxCPM | vox epm
fork-pi | fork pie | fork pi
```

It is **off until that file exists**, and edits are picked up without a restart. Aliases are matched
exactly (ignoring case, spaces and punctuation) and always rewritten to the first spelling (an alias may be at most three spoken words). Without
an alias, a span of one to three spoken words is rewritten only when it is a close match for a term,
or equals it once case, spaces and punctuation are ignored (so "patch bay" becomes "Patchbay"). It is
deliberately conservative:

- terms shorter than four letters are ignored;
- spans containing ordinary common English words are not fuzzy-matched;
- a fuzzy match is refused when one string contains the other, so "Qwen 3" or "Heralds" never become
  "Qwen" or "Herald" (list a real mishearing of that shape as an alias);
- a lowercase single word that equals a term apart from case ("crate", "herald") is left as spoken;
- anything ambiguous is left as spoken.

Each rewrite is logged at INFO as `vocab snap: 'Vox EPM' -> 'VoxCPM'`.

## Voice tools

The voice agent's LLM can call a small set of server-side tools (defined in
`patches/voice_tools.py`). Their spoken results are kept short and TTS-friendly.

| Tool | What it does | Backing service |
|---|---|---|
| `get_weather` | Current conditions for a place | Open-Meteo (public web API) |
| `web_search` | Search the public web; results also appear as clickable links on screen | DuckDuckGo (`ddgs`) |
| `knowledge_lookup` | Search your own notes, projects, and research | QMD MCP (`QMD_MCP_URL`) |
| `decision_lookup` *(optional)* | Look up a recorded design decision and its rationale ("why did we…") | Faulkner-DB (`FAULKNER_API_URL`) |
| `set_mood` | Set the interface/avatar mood — only when the user asks for an expression or mood change | none — client-side visual only |
| `delegate_to_hermes` *(optional)* | Hand off a long-running / multi-step task to Hermes | Hermes shim (`HERMES_SHIM_URL`) |
| `hermes_status` *(optional)* | Report Hermes' status (summary, last result, recent steps, or pending approvals) | Hermes MCP (`HERMES_MCP_URL`) |
| `send_to_hermes` *(optional)* | Send a quick free-form message / follow-up to Hermes | Hermes MCP (`messages_send` → `HERMES_TARGET`) |
| `confirm_action` *(only with `VOICE_CONFIRM_TOOLS`)* | Lets the model retry a consequential tool once the user has said yes — see *Confirming consequential actions* below | none |

Hermes is a separate, self-hosted service (see *Agent lane (optional)*
above) — most installs never run it, and these three tools simply stay
unarmed until something answers at `HERMES_MCP_URL`.

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
endpoint answers a probe, and `decision_lookup` only if Faulkner-DB does; the three Hermes tools are armed only if the Hermes MCP
endpoint answers a probe. This keeps a self-hoster without those services from
arming dead tools the LLM would call and then narrate as failures. Set
`VOICE_TOOLS=<comma-list>` to pin the set explicitly (no probing — only listed
names that exist are kept). Probing happens at startup only, so **restart the
pipeline to re-arm** after bringing a service up or down. The settings UI's
"N armed" count shows the result. `web_search` results also surface as a
clickable **Links card** in the cockpit.

When a tool times out or fails, the model is told so and instructed not to
guess or retry; the web-search and weather results carry a "Checked at" time.
If the reply after a tool call comes back empty, the agent says a short
"Sorry, I didn't get an answer for that." instead of going silent. Each executed
tool logs one INFO line (name, milliseconds, first 200 characters of the result).

### Add your own tools

Set `VOICE_TOOLS_DIR=/path/to/dir` to drop in extra tools without editing repo
files — one `.py` per tool exposing a `TOOL_DEF` dict and a `run()` callable
(see `examples/tools/` for eight ready-to-copy tools: clock, model-server status,
service health, GPU status, news headlines, camera vision (needs a multimodal
endpoint), Home Assistant control, and a read-only web page reader that refuses
non-public addresses — edit each file's EDIT-ME constants for your own
endpoints). This **executes your Python on your
box**, so treat the directory with the same trust as editing config. Drop-ins
arm unconditionally when the directory is set (unless `VOICE_TOOLS` pins the
list); a broken file is skipped with a logged warning rather than crashing the
pipeline.

A drop-in may also set module-level constants to override the defaults:
`TIMEOUT_S` (seconds before the call is abandoned), `ARG_KEY` / `ARG_LABEL`
(which parameter carries the argument, and how to name it when asking for it),
`TOOL_LABEL` (how the tool is named aloud), `REQUIRED` (whether a missing
argument is refused), `RESULT_CHARS` (how much of the result reaches the model —
the default suits a one-line spoken answer, raise it for a tool returning a
document excerpt), `TIME_SENSITIVE = True` (default `False`: a successful result
gets ` (Checked at 3:42 PM today.)` appended, so a stale answer left in the
conversation is recognisable later — set it for tools about "now", like scores
or traffic), and `CONSEQUENTIAL = True` (the tool is refused until the
user confirms it; see below). No restart needed to pick up changes — the settings panel's
"Reload tools" button re-probes and re-arms live (also
`{"type":"config_set", "reload_tools":true}` over the WS).

### Confirming consequential actions

Some tools do something you would not want done on a misheard word — spend
money, send a message, delete something. Set `VOICE_CONFIRM_TOOLS` to a
pipe-separated list of tool names (or mark a drop-in `CONSEQUENTIAL = True`) and
those tools are **refused on the first call**: the agent is told to explain what
will happen and ask you outright. Only after you agree, and the agent calls
`confirm_action` naming that tool, does a retry go through.

The authorisation is deliberately narrow. It is tied to the exact argument that
was refused, so approving one message does not approve a different one, and it
is **used once** — an immediate repeat is refused again. Nothing is confirmed
that was not refused first, and `confirm_action` itself is never gated.

**Nothing is gated by default.** With `VOICE_CONFIRM_TOOLS` unset, no tool
requires confirmation and `confirm_action` is not even offered to the model, so
this costs nothing until you ask for it. `VOICE_CONFIRM_TOOLS=off` disables the
mechanism outright, including for drop-ins that declare themselves consequential.

## STT backends

`parakeet-tdt` is the default and runs on the pipeline host — nothing here
changes if you never touch `--stt`.

`remote-transcription` is an **opt-in** backend for any server implementing
OpenAI's `POST /v1/audio/transcriptions` protocol (a Whisper-API server,
OpenAI itself, ...), so the speech-to-text model can live on another machine
and the pipeline host only makes HTTP calls. Point
`--remote_transcription_base_url` at such a server and pass
`--stt remote-transcription`:

```bash
~/speech-to-speech-main/.venv/bin/speech-to-speech \
  --mode websocket --ws_host 127.0.0.1 --ws_port 8765 \
  --stt remote-transcription \
  --remote_transcription_base_url http://<your-stt-server>:<port> \
  --remote_transcription_model <model-id-your-server-expects> \
  --tts pocket --pocket_tts_device cpu \
  --llm_backend chat-completions \
  --responses_api_base_url <your-base-url> \
  --responses_api_api_key dummy \
  --model_name <your-model-name> \
  --responses_api_stream
```

| Flag | Default | Meaning |
|---|---|---|
| `--remote_transcription_base_url` | *(unset)* | required with this backend; startup is refused without it |
| `--remote_transcription_model` | `whisper-1` | the `model` form field |
| `--remote_transcription_language` | *(unset)* | the `language` form field; unset lets the server decide |
| `--remote_transcription_progressive_timeout_s` | `1.0` | per-operation (connect/read) timeout for a live partial request, not a total deadline; a partial already in flight can delay a following final by up to this long |
| `--remote_transcription_final_timeout_s` | `15.0` | per-operation (connect/read) timeout for the final request, not a total deadline (a server that trickles bytes can hold the call longer) |
| `--remote_transcription_progressive_window_s` | `15.0` | a live partial uploads only the last N seconds of the utterance |

Audio goes up as a 16 kHz mono PCM16 WAV in a multipart upload; the reply is
read as JSON (`text`, and `language` if the server returns one). Live partials
re-send the audio so far on the same endpoint (at most one in flight; a
partial that arrives while one is running, or after the final started, is
skipped) and follow `--enable_live_transcription` (on by default).

**There is no local fallback.** If the server is down, slow or returns an
error, that turn's transcript is empty and an error is logged — the pipeline
does not load a local model behind your back. A failed health check at startup
(`GET <base_url>/health`) only logs a warning, so the pipeline still boots when
the STT server comes up later.

## TTS backends

`pocket` is the default and requires nothing beyond what SETUP.md step 0
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

SETUP.md step 4's command with only the TTS flags changed (`setup.sh`
already installed the `pocket` extra the fallback needs):

```bash
BRAINS_JSON=$PWD/brains.json \
~/speech-to-speech-main/.venv/bin/speech-to-speech \
  --mode websocket --ws_host 127.0.0.1 --ws_port 8765 \
  --stt parakeet-tdt --parakeet_tdt_device cpu \
  --tts remote-speech \
  --remote_speech_base_url http://<your-tts-server>:<port> \
  --pocket_tts_voice jean --pocket_tts_device cpu \
  --llm_backend chat-completions \
  --responses_api_base_url <your-base-url> \
  --responses_api_api_key dummy \
  --model_name <your-model-name> \
  --responses_api_stream
```

**Servers that don't match the OpenAI defaults.** Three optional flags, all
inert unless set: `--remote_speech_source_sample_rate <hz>` (the rate of the PCM
the server returns; default 24000), `--remote_speech_stream_format <value>`
(sent as `stream_format`, whitespace trimmed; some servers only stream incrementally when
it is set), and `--remote_speech_runon_chars_per_s <n>` (run-on guard, off at 0:
audio longer than `2 + len(text)/n` seconds is cut where it stands once audio has
started playing, with no `pocket` replay and no circuit-breaker failure; if
nothing has played yet, it falls back to `pocket` like any failed synthesis).

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

`config_get` reports the current value back. It is **empty by default**, and
an empty value means the field is left out of the request entirely rather than
sent blank — an empty `instructions` string is a verified silent failure mode
(HTTP 200, zero bytes of audio) on at least one such server. Nothing is sent
unless you ask for it, so the delivery you get is whatever the model does with
the text itself. `config_set {speech_style: ...}` against `pocket` or `kokoro`
returns a clear error naming the active backend instead of silently doing
nothing, since neither has this handle.

### What your TTS server can tell the cockpit

The cockpit does not assume anything about which TTS backend you run — it
*asks*. On `GET /v1/audio/voices` it reads three things, all optional:

```json
{
  "voices": [{"name": "jean", "kind": "speaker"}],
  "can_clone": false,
  "accepts_instructions": false
}
```

| field | default if absent | what it controls |
|---|---|---|
| `voices` | *(empty — the voice picker hides)* | the names in the cockpit's voice dropdown |
| `can_clone` | `false` | whether the **Advanced — custom voice** panel is shown |
| `accepts_instructions` | `true` | whether `instructions` is transmitted at all |

A bare `["jean", "alba"]` array works too; `voices` is parsed liberally.

Serving none of this is fine — the endpoint is optional and a server that
doesn't answer simply gets the defaults above. But a server that *does* answer
gets a cockpit whose dropdown matches reality, which matters more than it
sounds: some servers answer an unknown voice with `200 OK` and zero bytes
rather than an error, so a stale dropdown produces silence instead of a
message you can act on.

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
   user: `~/speech-to-speech-main/.venv/bin/hf auth login` (or place a token at `~/.cache/huggingface/token`).
3. Restart the pipeline service — it re-downloads the model weights with
   cloning enabled (~220 MB, one time).

`soundfile` must be installed in the pipeline's venv for non-WAV uploads and
recording normalization: `~/speech-to-speech-main/.venv/bin/pip install soundfile`.

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
rest of the cockpit already uses — no new endpoint. This lets tools like
`get_weather` and the LLM's sense of "now"/"here" work without you naming a
place every time.

**Where your location goes from there — read this before turning it on:**

- **OpenStreetMap's Nominatim service.** To turn coordinates into a place
  name, the voice server sends them to
  `https://nominatim.openstreetmap.org/reverse` (`patches/phone_context.py`).
- **Open-Meteo**, when `get_weather` runs without a place named: the stored
  coordinates go to `https://api.open-meteo.com/v1/forecast`.
- **The active brain.** Every request carries an ambient line with the
  resolved place name and local time, to whichever brain is currently
  active — including a remote/cloud brain, if that's what you've selected
  in the Brain panel.

If that matters to you, leave it off, or set `VOICE_PHONE_CONTEXT=off` on
the server.

Location updates are throttled client-side (moved >100m or 5+ minutes since
the last send) and expire server-side after 30 minutes of staleness. Denying
the browser's location permission prompt turns the toggle back off. Set
`VOICE_PHONE_CONTEXT=off` on the server to disable the feature entirely,
regardless of what any client has toggled.

Any browser works — a phone gives GPS-grade accuracy, a desktop typically
falls back to coarser IP/network-based geolocation, which is still useful for
timezone/weather purposes.

## Install as an app

The web client (`webclient/`) is an installable Progressive Web App
(`manifest.json` + `sw.js`) — installing it gives you an icon that launches
straight to the cockpit, no browser chrome, on whatever device you install it
on. This is the same install flow every PWA uses; nothing here is specific to
this project.

**A service worker only registers in a secure context**, so this needs one of:
- `https://` — your own reverse proxy in front of `serve.py`, or `serve.py
  --certfile ... --keyfile ...` directly (see `serve.py --help`).
- `http://localhost` / `http://127.0.0.1` — works with no certificate at all,
  which is why this is the easiest way to try it on the same machine the
  server runs on.

A plain `http://` LAN address (e.g. `http://192.0.2.50:8770`) is **not** a
secure context, so install won't be offered there — this is a browser
restriction, not something this app can opt out of. If you want to install
on a phone or tablet over your LAN, put a reverse proxy with a trusted
certificate in front of it, or use a self-signed cert and install its CA on
the device first (browsers won't offer install behind an untrusted cert
warning either).

**Per platform:**
- **Chrome/Edge (desktop):** an install icon appears in the address bar, or
  use the menu → "Install Patchbay…" / "Cast, save, and share" → "Install page
  as app".
- **Chrome (Android):** menu → "Add to Home screen" / "Install app".
- **Safari (iOS/iPadOS):** Share sheet → "Add to Home Screen". iOS doesn't
  fire the same install-prompt event Chrome does, so there's no in-app
  install button here — this is the only path on iOS, and it's what the
  `apple-mobile-web-app-*` meta tags and touch icon in `index.html` are for.
- **Fire OS / Silk (e.g. Echo Show):** **cannot install a PWA.** Fire OS's
  Silk browser has no PWA install support at all — the install control in
  the settings panel simply never appears there, same as on any browser that
  doesn't support it. Getting an installable icon on an Echo Show needs a
  native APK wrapping a WebView (or a TWA-style shell) pointed at your
  cockpit's URL; that's outside what a PWA manifest can do.

Once installed, an update banner appears in the **service panel** (the gear
icon) whenever a newer `index.html` is deployed and you're still on the old
one — click it to reload onto the new version. The service worker itself
(`webclient/sw.js`) never caches dynamic state (brain/config/HA endpoints) and
serves the shell network-first, so an offline load only ever shows the last
version you actually had open, never something stale served in place of a
live connection.

## Tests

Run the suite in the framework's venv — the patch modules import its
packages (`httpx`, `numpy`, `openai`, `pydantic`), so the system `python3`
cannot collect them. From the repo root:

```bash
~/speech-to-speech-main/.venv/bin/pip install pytest
~/speech-to-speech-main/.venv/bin/python3 -m pytest patches/ examples/ webclient/ -q
```

`webclient/test_webclient.py` drives the real `index.html` in a headless
Chromium with a fake microphone, against a stub control websocket, to cover
client behaviour no unit test can reach — the wake-word mic handoff, the
permission gate, the settings dialog's focus trap. It needs a browser, also
in the venv:

```bash
~/speech-to-speech-main/.venv/bin/pip install playwright websockets
~/speech-to-speech-main/.venv/bin/python3 -m playwright install chromium
```

Without those it skips rather than fails, so the suite still passes on a
machine that only runs the pipeline — but then the client is untested, so
check the skip count in the pytest summary before trusting a green run.
Some skips are expected even with everything installed — measured with the
browser suite installed: 22, which are the echo-gate tests that need benchmark
recordings (not shipped), one wake-word test that needs `openwakeword`,
four that need the maintainer's release tooling (not shipped), and one that
needs `node`. Both of the browser suite's servers bind ephemeral ports on
localhost, so it is safe to run on the same box as a live agent.

## Attribution & acknowledgments

This project borrows ideas as well as code, and gladly says so:

- [`speakrail/speakrail`](https://github.com/speakrail/speakrail) (Apache-2.0) — the lexical backchannel test (a short reply over the assistant tiled entirely from acknowledgement phrases), reimplemented in `patches/backchannel.py`; the duck-then-decide barge-in design it pairs with.
- [`huggingface/speech-to-speech`](https://github.com/huggingface/speech-to-speech) (Apache-2.0) — the STT/LLM/TTS pipeline this cockpit drives.
- [`met4citizen/TalkingHead`](https://github.com/met4citizen/TalkingHead) + HeadAudio by Mika Suominen (MIT) — the 3D avatar + audio-driven lip-sync approach, the ready-head roster, and the Blender avatar pipelines that shaped this project's avatar architecture.
- Ready Player Me, AvatarSDK, Avaturn, VRoid Studio, and the MakeHuman/MPFB community — creators of the bundled example heads.
- Classic visual-novel / Live2D-style talking portraits — the inspiration for the 2D still-image avatar path.

See `NOTICE` for full attribution and per-asset licensing.
