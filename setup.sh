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

CHECK_MODE=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_MODE=1 ;;
    *) echo "unknown argument: $arg" >&2; echo "Usage: setup.sh [--check]" >&2; exit 2 ;;
  esac
done

# --check verifies an existing install/apply without installing or writing
# anything (safe to run any number of times, including in CI or by an agent
# looping on its output). Prints one "CHECK <name>: PASS|FAIL <reason>" line
# per check; required checks (venv, package-import, patches-applied) must all
# PASS for exit 0, optional checks (playwright) never affect the exit code.
run_checks() {
  local overall=0

  if [ -x "$INSTALL_DIR/.venv/bin/python3" ]; then
    echo "CHECK venv: PASS venv found at $INSTALL_DIR/.venv"
  else
    echo "CHECK venv: FAIL no venv at $INSTALL_DIR/.venv (run ./setup.sh first, or set INSTALL_DIR to where it lives)"
    overall=1
  fi

  if [ -x "$INSTALL_DIR/.venv/bin/python3" ] \
     && "$INSTALL_DIR/.venv/bin/python3" -c "import speech_to_speech" >/dev/null 2>&1; then
    echo "CHECK package-import: PASS speech_to_speech imports cleanly from the venv"
  else
    echo "CHECK package-import: FAIL speech_to_speech did not import (venv missing, or the editable install is incomplete)"
    overall=1
  fi

  local dry_out
  if dry_out="$(bash "$SCRIPT_DIR/patches/apply.sh" --dry-run 2>&1)"; then
    if echo "$dry_out" | grep -qE '^[[:space:]]+(new|OVERWRITE)[[:space:]]'; then
      echo "CHECK patches-applied: FAIL patch pack is not fully/cleanly applied (run: bash patches/apply.sh)"
      overall=1
    else
      echo "CHECK patches-applied: PASS all patch files present and unchanged (idempotent re-run confirmed)"
    fi
  else
    echo "CHECK patches-applied: FAIL patches/apply.sh --dry-run errored: $dry_out"
    overall=1
  fi

  # webclient/test_webclient.py runs under the system python3, not the
  # framework venv (README.md: `python3 -m pytest webclient/`) — probe the
  # same interpreter the tests actually use.
  if command -v python3 >/dev/null 2>&1 && python3 -c "import playwright.sync_api" >/dev/null 2>&1; then
    echo "CHECK playwright (optional): PASS installed in system python3 — webclient browser tests will run"
  else
    echo "CHECK playwright (optional): FAIL not installed in system python3 (webclient tests run there, not the venv) — webclient browser tests will skip, rest of the suite still runs"
  fi

  return "$overall"
}

if [ "$CHECK_MODE" -eq 1 ]; then
  run_checks
  exit $?
fi

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

# Install CPU-only torch/torchaudio first so the framework's dependency
# resolve below (which only pins torch>=2.4.0 on Linux) sees them already
# satisfied, instead of pulling the default CUDA build (~5GB of nvidia-*-cu13
# + triton). GPU users: swap in a CUDA build post-install if you want one.
echo "Installing CPU-only torch/torchaudio (default install has no GPU requirement)"
"$INSTALL_DIR/.venv/bin/pip" install torch torchaudio --index-url https://download.pytorch.org/whl/cpu

"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[kokoro]"
"$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR[pocket,websocket]"

echo "Applying cockpit patch pack"
bash "$SCRIPT_DIR/patches/apply.sh"

if [ ! -f "$SCRIPT_DIR/brains.json" ]; then
  echo "No brains.json found — copying brains.json.example (edit it before starting the service)"
  cp "$SCRIPT_DIR/brains.json.example" "$SCRIPT_DIR/brains.json"
fi

# Advisory only, never fatal: brain_discovery.py is standalone (stdlib +
# httpx, no speech_to_speech import of its own — see its module docstring),
# so it can run even though the pipeline hasn't started yet. Tells a
# first-time installer what's actually on their box instead of leaving them
# to guess from the generic example. Loopback-only probe, see README's
# "Finding your local model server".
echo
echo "Checking for a local model server already running (loopback only)..."
"$INSTALL_DIR/.venv/bin/python3" "$SCRIPT_DIR/patches/brain_discovery.py" || true

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

Verify the install any time with: $SCRIPT_DIR/setup.sh --check
EOF

echo "SETUP OK"
