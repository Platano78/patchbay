# Patchbay — for agents installing it for someone

## 1. What this is

Patchbay is a patch pack applied onto the Hugging Face
[`speech-to-speech`](https://github.com/huggingface/speech-to-speech) framework, plus a
browser cockpit: the user speaks into a web page, the pipeline transcribes it, asks an LLM,
and speaks the answer back. It does not include a model — it needs an OpenAI-compatible
chat-completions endpoint the user already runs or can reach.

[SETUP.md](SETUP.md) is the step-by-step runbook this file summarises. If the two ever
disagree, SETUP.md wins; tell the user about the mismatch.

## 2. Decide with the user first

- **Where is their model server?** Get a base URL (`http://host:port/v1`) and check it:
  `curl -sf <base>/models` must print JSON with a `"data"` array. If they don't know,
  `setup.sh` runs a loopback scan (SETUP step 3 shows how to rerun it, or scan another host).
- **Where will they open the cockpit?** Default: a browser on this same machine, at
  `http://localhost:8770`. Another device (phone, tablet, second computer) is a separate,
  later step that needs LAN binding and HTTPS — only do it if they ask.

## 3. Rules

- **Bind `127.0.0.1`** (`--ws_host 127.0.0.1`; `serve.py`'s default) unless the user asks for
  access from other devices. If they do, first read them README's *Who can connect*: there is
  no authentication, and anyone who can reach the ports gets the live conversation and the
  controls.
- **Never commit `brains.json`.** It is gitignored because it can name API key files.
- **Before `./setup.sh`, tell the user what it downloads** — the size and licence table in
  SETUP step 0 (about 6 GB of disk in total; the speech models are CC-BY-4.0) — and get a yes.
- **Ask before installing it as a service.** Running it in the foreground (step 4) and passing
  the smoke check (step 5) finishes the setup. Installing systemd units makes it start at every
  boot — offer that, say what it changes, and only do it if the user says yes.
- Don't run the test suite unless the user asks, or you changed code.
- Don't delete anything you didn't create. `setup.sh` refuses to reuse an existing
  `INSTALL_DIR`; if one is left over from a failed attempt *you* started, removing it is fine —
  otherwise ask.
- If a file or command named in these docs is missing, report that to the user. Don't go
  searching outside this repo for a substitute.

## 4. Install and verify

Run from the repo root. Paths assume the default `INSTALL_DIR` (`~/speech-to-speech-main`).

**Prerequisites** (SETUP step 0):

```bash
python3 --version && python3 -c 'import venv, ensurepip' && git --version && curl --version | head -1
```

Expect three lines (`Python 3.x.y`, `git version ...`, `curl ...`) and no traceback. On
Debian/Ubuntu a failure is fixed by `sudo apt install python3-venv git curl` — that needs the
user's sudo, so ask them to run it. Tested on Python 3.10, 3.12 and 3.14.

**Install** (step 1), after the user agreed to the downloads:

```bash
./setup.sh
```

The last line must be exactly `SETUP OK`. On Python 3.13 or newer it also prints
`Skipping the optional kokoro TTS extra: it does not support Python 3.14 yet (pocket, the default TTS, is unaffected).`
(with the actual version) — that is expected, not an error.

**Verify** (step 2):

```bash
./setup.sh --check
```

Exit code `0`, and these three lines say `PASS`:

```
CHECK venv: PASS ...
CHECK package-import: PASS ...
CHECK patches-applied: PASS ...
```

A fourth line, `CHECK playwright (optional): ...`, may say `FAIL`; it never affects the exit
code. The check is read-only — loop on it freely.

**Configure** (step 3):

```bash
test -e brains.json || cp brains.json.example brains.json
```

Then edit one entry: `base_url` = the user's `/v1` base, `model` = a model id from their
`/models`, `available` = `true`.

**Run** (step 4), in the foreground first:

```bash
BRAINS_JSON=$PWD/brains.json \
~/speech-to-speech-main/.venv/bin/speech-to-speech \
  --mode websocket --ws_host 127.0.0.1 --ws_port 8765 \
  --stt parakeet-tdt --parakeet_tdt_device cpu \
  --tts pocket --pocket_tts_voice jean --pocket_tts_device cpu \
  --llm_backend chat-completions \
  --responses_api_base_url <base> \
  --responses_api_api_key dummy \
  --model_name <model> \
  --responses_api_stream
```

Ready when the log shows `WebSocket server ready, waiting for connections...` (the first start
downloads ~2.7 GB of models first). Then, in another terminal:

```bash
python3 webclient/serve.py --port 8770
```

prints `Serving <path> on http://127.0.0.1:8770`. The user opens `http://localhost:8770`.

## 5. Verify it answers, without a microphone

With the pipeline running:

```bash
~/speech-to-speech-main/.venv/bin/python3 examples/smoke_turn.py
```

Success is exit code `0` and this shape (answer wording and byte count vary by model):

```
heard: What is the capital of France?
answer: The capital of France is Paris.
reply audio: 74752 bytes
SMOKE OK
```

On failure it prints one sentence naming what didn't arrive and exits `1`. It changes no
configuration. The pipeline has one shared session, so an open cockpit hears this turn too.

## 6. Run it as a service

**Optional, and only on the user's request.** Section 3 has the rule — *Ask before installing
it as a service*: a foreground run (step 4) plus the smoke check (step 5) is a finished setup.
Do this section only after the user has said yes to auto-start. SETUP step 6,
*Run as systemd services*, has both variants with the exact commands:

- **System units** (`/etc/systemd/system/`, needs sudo — ask the user).
- **User units** when there is no sudo: rendered into `~/.config/systemd/user/`, managed with
  `systemctl --user`, and `loginctl enable-linger "$USER"` so they survive logout and start at
  boot.

Both templates bind `0.0.0.0`. For a this-machine-only install, apply the two extra `sed`
lines SETUP gives to bind `127.0.0.1` instead.

## 7. Where things live

| Thing | Location |
|---|---|
| Brain registry | `brains.json` in the repo root; the pipeline reads the path in `BRAINS_JSON` (unset: `~/speech-to-speech/brains.json`) |
| Personas, per-brain model overrides, chosen voice | `~/speech-to-speech/persona.json`, `model_overrides.json`, `voice_choice.json` (override with `VOICE_PERSONA_FILE`, `VOICE_MODEL_OVERRIDES_FILE`, `VOICE_CHOICE_FILE`) |
| Cloned voices | `~/speech-to-speech/voices/` (`VOICE_CLONE_DIR`) |
| Framework install + venv | `~/speech-to-speech-main`, venv at `~/speech-to-speech-main/.venv` (`INSTALL_DIR`) |
| Downloaded models | `~/.cache/huggingface/hub` (speech models), `~/.cache/torch/hub` (VAD), `~/nltk_data` |
| Patch pack source | `patches/` — copied into the install by `patches/apply.sh`; editing it changes nothing until that runs |

## 8. Troubleshooting

Each row starts with the exact text the software prints.

| You see | Meaning | Do |
|---|---|---|
| `setup.sh` output does not end with `SETUP OK` | it stopped at the first failing command; that command's error is just above | read that error; the rows below cover setup's own sentences |
| `python3 cannot create virtual environments on this system (the venv module is missing). On Debian/Ubuntu: sudo apt install python3-venv   then re-run ./setup.sh` | prerequisite missing; nothing was created | user installs `python3-venv`, then rerun |
| `git is not installed. On Debian/Ubuntu: sudo apt install git   then re-run ./setup.sh` | prerequisite missing | same |
| `INSTALL_DIR already exists: <path>` | an earlier attempt left the install directory | remove it if you created it (else ask), or set `INSTALL_DIR` to a fresh path |
| `Skipping the optional kokoro TTS extra: it does not support Python <version> yet (pocket, the default TTS, is unaffected).` | expected on Python 3.13+ | nothing |
| `CHECK venv: FAIL no venv at <dir>/.venv (run ./setup.sh first, or set INSTALL_DIR to where it lives)` | no install at that path | run `./setup.sh`, or pass the `INSTALL_DIR` you installed with |
| `CHECK package-import: FAIL speech_to_speech.brain_control / connections.websocket_streamer did not import (...)` | venv incomplete or patch pack broken | `bash patches/apply.sh`, then `./setup.sh --check` |
| `CHECK patches-applied: FAIL patch pack is not fully/cleanly applied (run: bash patches/apply.sh)` | `patches/` differs from what is installed | `bash patches/apply.sh`, restart the pipeline |
| `CHECK patches-applied: FAIL patches/apply.sh --dry-run errored: ...` | `apply.sh` could not find the install | the line ends with the fix: `./setup.sh`, or the right `INSTALL_DIR` |
| `CHECK playwright (optional): FAIL ...` | browser test deps not installed | nothing, unless running the tests (README *Tests*) |
| `openai.APIConnectionError: Connection error.` at the end of a traceback when the pipeline starts | the LLM endpoint is unreachable; the pipeline warms the model up at boot and exits | fix `--responses_api_base_url`; check with `curl -sf <base>/models` |
| `BrainControl: no brains.json at <path> -- brain switching is off.` | a warning; the command-line brain still works | set `BRAINS_JSON` to the file's path |
| `WebSocket connection rejected: Origin '<origin>' doesn't match Host '<host>'. Set VOICE_WS_ORIGINS to allow it explicitly (see README).` | a browser page from another host tried to open the socket | serve the page from the same host, or set `VOICE_WS_ORIGINS` (README *Who can connect*) |
| `WebSocket connection rejected: Host '<host>' is not this machine. Set VOICE_ALLOWED_HOSTS to allow it (see README).` (the HTTP server logs the same sentence starting `HTTP request rejected:`) | the page addressed the box by a hostname that is not this machine's — the DNS-rebinding guard; expected and correct for a hostile name | only if the name is legitimately yours (private DNS, custom alias): add it to `VOICE_ALLOWED_HOSTS` (README *Who can connect*) |
| Wake word toggle greyed out with `openwakeword is not installed in the pipeline's venv (pip install openwakeword onnxruntime, then restart the pipeline)` | no detector installed | install it only if the user wants wake word (the pre-trained models are non-commercial, SETUP step 6) |
| Cockpit status `mic denied` | the browser refused the microphone: not a secure context (`http://` to a LAN address), or the permission was denied | use `http://localhost:8770` on the same machine, or set up HTTPS |
| Calibration lamp `FAULT` / status `can't reach ws://...` | the browser could not open the WebSocket (wrong URL or port, pipeline not running, `https` page without `wss`, or Origin rejected) — **not** the LLM | check the pipeline is up and the settings panel's "Server socket URL" |
| `LINK OK`, transcript appears, no answer | the LLM isn't answering | read the pipeline log; `curl -sf <base>/models` |
