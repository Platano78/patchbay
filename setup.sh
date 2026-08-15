#!/usr/bin/env bash
# Installs the huggingface/speech-to-speech framework pinned at the commit
# Patchbay's patch pack (patches/) was built against, creates its venv,
# and applies the patch pack on top.
#
# NOTE: patches/apply.sh locates the target package by importing
# speech_to_speech from "$INSTALL_DIR/.venv" — it reads the same INSTALL_DIR
# env var this script does, so overriding INSTALL_DIR here needs no edit to
# apply.sh. (To write the pack somewhere other than the resolved install
# entirely, pass `apply.sh --dest DIR`; `--dry-run` previews without writing.)
#
# Because the install below is editable, that import resolves to the live
# src/speech_to_speech tree — so apply.sh deploys straight into the running
# install. That is intended here, but see patches/README.md before running it
# by hand against a production box.
set -euo pipefail

REPO_URL="https://github.com/huggingface/speech-to-speech.git"
REPO_SHA="1e63f7e9343e491809d0d60e64f7ea551dbe845a"
INSTALL_DIR="${INSTALL_DIR:-$HOME/speech-to-speech-main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -d "$INSTALL_DIR" ]; then
  echo "INSTALL_DIR already exists: $INSTALL_DIR" >&2
  echo "Remove it or set INSTALL_DIR to a fresh path, then re-run." >&2
  exit 1
fi

echo "Cloning $REPO_URL @ $REPO_SHA into $INSTALL_DIR"
git clone "$REPO_URL" "$INSTALL_DIR"
git -C "$INSTALL_DIR" checkout "$REPO_SHA"

echo "Creating venv at $INSTALL_DIR/.venv"
python3 -m venv "$INSTALL_DIR/.venv"

echo "Installing speech-to-speech (editable) + extras"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[kokoro]"
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[pocket,websocket]"

echo "Applying cockpit patch pack"
bash "$SCRIPT_DIR/patches/apply.sh"

if [ ! -f "$SCRIPT_DIR/brains.json" ]; then
  echo "No brains.json found — copying brains.json.example (edit it before starting the service)"
  cp "$SCRIPT_DIR/brains.json.example" "$SCRIPT_DIR/brains.json"
fi

cat <<EOF

Setup complete.

Next steps:
  1. Edit $SCRIPT_DIR/brains.json with your LLM endpoint(s).
  2. Start the pipeline (see README.md for the full flag list), e.g.:
       $INSTALL_DIR/.venv/bin/speech-to-speech \\
         --mode websocket --ws_host 0.0.0.0 --ws_port 8765 \\
         --stt parakeet-tdt --parakeet_tdt_device cpu \\
         --tts pocket --pocket_tts_voice jean --pocket_tts_device cpu \\
         --llm_backend chat-completions \\
         --responses_api_base_url http://localhost:8084/v1 \\
         --responses_api_api_key dummy \\
         --model_name <your-model-name> \\
         --responses_api_stream
  3. Serve the cockpit:
       python3 $SCRIPT_DIR/webclient/serve.py --port 8770
EOF
