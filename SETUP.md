# Patchbay setup runbook

Executable steps for a human, an agentic model, or an agent (e.g. Hermes).
Each step is one exact command, its expected output, and what to do if the
output doesn't match. Run the steps in order; do not skip ahead.

## 0. What you need

- `python3` 3.10 or newer, and `git`.
- ~2 GB free disk (framework checkout + venv + downloaded model weights).
- A microphone and a browser (Chrome/Edge/Firefox — any recent build).
- An **OpenAI-compatible chat-completions LLM endpoint**, reachable from this
  box — local (llama.cpp, vLLM, Ollama, an LLM router) or remote. It does not
  matter which; you supply the URL in step 3.
- No GPU is required. STT (`parakeet-tdt`) and TTS (`pocket`, the default)
  both run CPU-only; a GPU only helps the LLM, which is a separate process
  you are pointing at, not something this repo runs for you. `setup.sh`
  installs the CPU build of torch; if you want a CUDA build anyway, swap it
  in after setup finishes (`pip install torch torchaudio --index-url
  https://download.pytorch.org/whl/cuXXX` in the venv).

Verify the prerequisites:

```bash
python3 --version && git --version
```

Expected output: two lines, `Python 3.10.x` (or higher) and `git version
x.y.z`. If `python3` reports below 3.10, install a newer Python before
continuing — the framework's install step will fail obscurely otherwise.

## 1. Clone and install

```bash
git clone <this-repo-url> patchbay
cd patchbay
./setup.sh
# optional: INSTALL_DIR=/custom/path ./setup.sh
```

This clones `huggingface/speech-to-speech` pinned at the commit the patch
pack was built against, creates a venv, installs the framework plus the
`pocket`/`kokoro`/`websocket` extras, and applies the cockpit's patch pack
on top.

Expected output: the last line printed is exactly `SETUP OK`.

What if it didn't say that: the script uses `set -euo pipefail`, so it
stopped at the first failing command and printed that command's own error
above. Common causes and fixes:

- `pip install` failure (network, or a missing build toolchain for a native
  extension) — read the pip error above `SETUP OK`'s absence, fix it
  (e.g. `apt install build-essential python3-dev` on Debian/Ubuntu), then
  re-run `./setup.sh`. It is not idempotent on a partial clone: if
  `INSTALL_DIR` (default `~/speech-to-speech-main`) already exists from a
  failed attempt, either `rm -rf ~/speech-to-speech-main` first or set
  `INSTALL_DIR=/fresh/path ./setup.sh`.
- `INSTALL_DIR already exists` — same fix: remove it or point `INSTALL_DIR`
  elsewhere.

## 2. Verify the install

```bash
./setup.sh --check
```

If you set `INSTALL_DIR` in step 1, pass the same value here:
`INSTALL_DIR=/custom/path ./setup.sh --check`.

Expected output: four lines, each starting `CHECK <name>: PASS ...`, and the
command exits `0`. `CHECK playwright` is optional and does not affect the
exit code either way.

What if a required check FAILs (exit code non-zero, one or more of `venv`,
`package-import`, `patches-applied` say `FAIL`): the FAIL line names the
reason and the fix inline. Re-run `./setup.sh` (safe to re-run once the
underlying issue — usually a partial `INSTALL_DIR` from step 1 — is
resolved), then `./setup.sh --check` again. This command is read-only: it
never installs, clones, or writes anything, so loop on it freely.

## 3. Configure the brain

```bash
cp brains.json.example brains.json
```

`setup.sh` already ran this copy for you in step 1 if `brains.json` didn't
exist yet, so this command is a no-op unless you deleted it — either way,
edit `brains.json` (setup.sh created it from the example) next.

**Not sure what to put in it?** `setup.sh` also runs a loopback-only scan for
a local model server already running on your box and prints what it finds,
right after it copies the example. Missed it, or want to check again later:

```bash
~/speech-to-speech-main/.venv/bin/python3 patches/brain_discovery.py
```

It checks the usual local ports (Ollama, LM Studio, llama.cpp, vLLM/LocalAI,
KoboldCpp, Jan) and prints a ready-to-paste `brains.json` entry for anything
that answers — it never writes `brains.json` for you. Every run names the
target set it scanned on its first line.

Model server on a different box? The scan won't see it by default — say so
explicitly:

```bash
~/speech-to-speech-main/.venv/bin/python3 patches/brain_discovery.py \
  --host 10.0.0.20
# or sweep a private block (a /24 at most):
~/speech-to-speech-main/.venv/bin/python3 patches/brain_discovery.py \
  --cidr 10.0.0.0/24
```

See README's "Finding your local model server" for what it probes, the
guards on `--cidr`, and `BRAIN_DISCOVERY_URLS` for a port it doesn't know.

Edit `brains.json`: for at least one entry, set `base_url` to your LLM
endpoint's `/v1` base, `model` to the model id it serves, and `available` to
`true`. Only `local` ships `available: true` by default (Ollama's default
address); `local-alt` and `frontier` ship `available: false` — leave them
that way unless you're using them. `local-alt` needs a real port for whatever
you run there (see its `note`); `frontier` needs an `api_key_file` you likely
don't have yet — safe to ignore both for a first run.

Verify the endpoint answers before starting the pipeline:

```bash
curl -sf <your-base-url>/models
```

Expected output: JSON with a top-level `"data"` array listing at least one
model id — e.g. `{"object":"list","data":[{"id":"your-model-name",...}]}`.

What if it didn't: `curl` prints nothing and exits non-zero, or the JSON has
no `data` array. The endpoint is unreachable, wrong URL/port, or not
OpenAI-compatible at `/models`. Fix the URL/port or start the LLM server
before continuing — everything past this point depends on it answering.

## 4. First run (foreground)

Start the pipeline, pointing it at the endpoint you just verified
(`<your-base-url>` and `<your-model-name>` from step 3):

```bash
BRAINS_JSON=$PWD/brains.json \
~/speech-to-speech-main/.venv/bin/speech-to-speech \
  --mode websocket --ws_host 0.0.0.0 --ws_port 8765 \
  --stt parakeet-tdt --parakeet_tdt_device cpu \
  --tts pocket --pocket_tts_voice jean --pocket_tts_device cpu \
  --llm_backend chat-completions \
  --responses_api_base_url <your-base-url> \
  --responses_api_api_key dummy \
  --model_name <your-model-name> \
  --responses_api_stream
```

Watch the log for the port-bind line (first-run model downloads can take a
minute or two before this appears):

```
WebSocket server ready, waiting for connections...
```

What if it didn't appear: an exception was printed instead — read it. The
two most common: a missing STT/TTS dependency (re-run `./setup.sh --check`
in another terminal — `package-import` or `patches-applied` FAILing means
the venv is incomplete) or a bad `--responses_api_base_url` value (recheck
step 3's `curl`).

In a second terminal, serve the cockpit:

```bash
python3 webclient/serve.py --port 8770
```

Expected output: `Serving <path> on http://0.0.0.0:8770`.

Open `http://localhost:8770` in a browser. A one-time **FIRST CALIBRATION**
overlay appears with a status lamp under "CONNECT TO INSTRUMENT SERVER":

- `NOT TESTED` (lamp off) — before the page has tried to connect.
- `TESTING LINK` (amber, pulsing) — connection attempt in progress.
- `LINK OK` (green) — the WebSocket handshake with `:8765` succeeded. This is
  the pass condition for this step.
- `FAULT` (red) — connection failed; see step 5's LLM-endpoint-unreachable
  branch below.

## 5. Talk to it

Hold the push-to-talk button, speak, release. Expected: a live transcript of
your speech appears, then a spoken reply plays back within a couple of
seconds.

Three likely first-run failures, each with its own check:

**No mic permission prompt, or a blocked-mic error.** Browsers only allow
`getUserMedia` on a secure context (`https://` or `http://localhost`).
Loading the cockpit by LAN IP or hostname over plain `http://` will silently
fail to get mic permission.

```bash
curl -s http://localhost:8770 -o /dev/null -w '%{http_code}\n'
```

Expected: `200`, and you are viewing the page via `localhost` (not a LAN
IP/hostname) unless you've set up HTTPS — see step 6.

**Calibration lamp shows FAULT, or `config_get`/no transcript ever arrives.**
The LLM endpoint from step 3 is unreachable or stopped answering.

```bash
curl -sf <your-base-url>/models
```

Expected: same JSON shape as step 3. If it fails now but passed in step 3,
the LLM server stopped — restart it, then reopen the settings panel (gear
icon) to re-probe.

**Transcript appears but no audio plays back.** TTS failed to load or
produce audio.

```bash
~/speech-to-speech-main/.venv/bin/python3 -c "import pocket_tts"
```

Expected: no output, exit code `0`. A traceback means the `pocket` extra
didn't install — re-run `./setup.sh --check`; `package-import` should have
caught this, so also check the pipeline's own terminal log for a TTS
exception near startup.

## 6. Optional

Each of these is independent; skip any you don't need.

**Run as systemd services** (keeps the pipeline and webclient running across
reboots/crashes):

```bash
sed \
  -e "s|\${RUN_USER}|$(whoami)|g" \
  -e "s|\${HOME_DIR}|$HOME|g" \
  -e "s|\${VENV_DIR}|$HOME/speech-to-speech-main/.venv|g" \
  -e "s|\${WORKING_DIR}|$PWD|g" \
  -e "s|\${WS_PORT}|8765|g" \
  -e "s|\${STT_BACKEND}|parakeet-tdt|g" \
  -e "s|\${TTS_BACKEND}|pocket|g" \
  -e "s|\${TTS_VOICE}|jean|g" \
  -e "s|\${LLM_BASE_URL}|<your-base-url>|g" \
  -e "s|\${LLM_MODEL_NAME}|<your-model-name>|g" \
  systemd/patchbay.service.template | sudo tee /etc/systemd/system/patchbay.service

sed \
  -e "s|\${RUN_USER}|$(whoami)|g" \
  -e "s|\${HOME_DIR}|$HOME|g" \
  -e "s|\${WEBCLIENT_DIR}|$PWD/webclient|g" \
  -e "s|\${HTTP_PORT}|8770|g" \
  systemd/voice-webclient.service.template | sudo tee /etc/systemd/system/voice-webclient.service

sudo systemctl daemon-reload
sudo systemctl enable --now patchbay voice-webclient
```

Verify: `systemctl is-active patchbay voice-webclient` both print `active`.

**Wake word** (hands-free "hey jarvis" activation instead of push-to-talk):
set `VOICE_WAKE_WORD=1` in the unit's `Environment=` (or export it before a
foreground run) after `pip install openwakeword onnxruntime` into the venv.
Verify: the settings panel's "Wake word" checkbox is enabled (not greyed
out) after a restart.

**Phone context** (location/timezone/battery ambient info): opt-in per
browser via the settings panel's "Phone context" toggle — no server config
needed. Verify: after enabling it, `config_get` over the WebSocket control
channel reports a non-null `phone_context` block.

**Custom voice cloning**: accept the terms at
<https://huggingface.co/kyutai/pocket-tts>, then `hf auth login` as the
service user, then restart the pipeline. Verify: the settings panel's
"Advanced - custom voice" section stops showing a terms-gate error and lets
you record/upload a sample.

**Remote access over HTTPS** (mic access from a non-localhost device): see
`patches/README.md` "Remote access / HTTPS" — it is a full runbook of its
own (cert generation, `VOICE_WS_CERTFILE`/`VOICE_WS_KEYFILE`, matching client
port). Verify per that doc's own steps.

**Optional integrations** (`HERMES_*`, `QMD_*`, `GENESIS_*`, `FAULKNER_*`
env vars): every one of these is probed once at pipeline startup and
silently dropped if nothing answers — the core voice agent (LLM brain + STT
+ TTS) works with none of them set. Only set these if you're running the
corresponding service; see `README.md`'s *Optional integrations* table for
what each does.
