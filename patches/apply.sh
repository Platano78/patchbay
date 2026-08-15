#!/usr/bin/env bash
# Re-apply the persona/brain-selector patch pack after a package reinstall.
# Copies the modified $PKG files + brain_control.py into whatever venv the
# speech_to_speech package currently lives in. brains.json and webclient/
# live outside the package and are untouched by reinstalls, so they are not
# copied here. Idempotent: safe to re-run over an already-patched tree.
#
# Since #8 (2026-07-03), the live install is an editable install from
# github.com/huggingface/speech-to-speech main
# ($INSTALL_DIR (default $HOME/speech-to-speech-main), venv at .venv there); $PKG resolves
# straight to the repo's src/speech_to_speech, so a pip reinstall of deps
# does not wipe these edits — only `git checkout` of the repo files would.
#
# Because that resolution lands on the LIVE source tree, a plain run deploys —
# there is no separate staging step. Use --dry-run to see what would be
# written first, and --dest to write somewhere other than the live install.
set -euo pipefail

USAGE="Usage: apply.sh [-n|--dry-run] [--dest DIR]   (DIR also settable as \$DEST)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/speech-to-speech-main}"
DEST="${DEST:-}"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1; shift ;;
    --dest)
      [ $# -ge 2 ] || { echo "--dest requires a directory argument" >&2; exit 2; }
      DEST="$2"; shift 2 ;;
    --dest=*) DEST="${1#*=}"; shift ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; echo "$USAGE" >&2; exit 2 ;;
  esac
done

# src (relative to SCRIPT_DIR) | dst (relative to PKG)
FILES=(
  "brain_control.py|brain_control.py"
  "voice_clone.py|voice_clone.py"
  "websocket_streamer.py|connections/websocket_streamer.py"
  "s2s_pipeline.py|s2s_pipeline.py"
  "lm_output_processor.py|LLM/lm_output_processor.py"
  "voice_tools.py|voice_tools.py"
  "turn_stats.py|turn_stats.py"
  "reflex_lane.py|reflex_lane.py"
  "hermes_cockpit.py|hermes_cockpit.py"
  "wakeword_gate.py|wakeword_gate.py"
  "echo_gate.py|echo_gate.py"
  "think_filter.py|think_filter.py"
  "voice_rules.py|voice_rules.py"
  "phone_context.py|phone_context.py"
  "transcript_buffer.py|transcript_buffer.py"
  "chat_completions_language_model.py|LLM/chat_completions_language_model.py"
  "remote_speech_tts_handler.py|TTS/remote_speech_tts_handler.py"
  "remote_speech_tts_arguments.py|arguments_classes/remote_speech_tts_arguments.py"
  "module_arguments.py|arguments_classes/module_arguments.py"
)

if [ -n "$DEST" ]; then
  PKG="$DEST"
  ORIGIN="--dest override"
else
  PKG="$("$INSTALL_DIR/.venv/bin/python3" -c 'import speech_to_speech, os; print(os.path.dirname(speech_to_speech.__file__))')"
  ORIGIN="live install, resolved via $INSTALL_DIR/.venv"
fi

if [ -z "$PKG" ] || [ ! -d "$PKG" ]; then
  echo "Could not locate speech_to_speech package directory: ${PKG:-<empty>}" >&2
  exit 1
fi

# Pre-flight every source file and destination directory BEFORE copying
# anything. Without this a missing file aborts mid-list under `set -e`, leaving
# the tree half-applied with no error until the service next restarts. It also
# keeps --dry-run honest: a preview promising 16 files must not be followed by
# a run that delivers 9.
problems=0
for entry in "${FILES[@]}"; do
  [ -f "$SCRIPT_DIR/${entry%%|*}" ] || {
    echo "missing source file: $SCRIPT_DIR/${entry%%|*}" >&2; problems=$((problems + 1)); }
  dst_dir="$(dirname "$PKG/${entry#*|}")"
  [ -d "$dst_dir" ] || {
    echo "missing destination directory: $dst_dir" >&2; problems=$((problems + 1)); }
done
[ "$problems" -eq 0 ] || {
  echo "Aborting: $problems problem(s) found, nothing was copied." >&2; exit 1; }

[ "$DRY_RUN" -eq 1 ] && echo "DRY RUN — nothing will be written."
echo "Destination: $PKG"
echo "  ($ORIGIN)"
for entry in "${FILES[@]}"; do
  src="$SCRIPT_DIR/${entry%%|*}"
  dst="$PKG/${entry#*|}"
  if [ ! -f "$dst" ]; then
    status="new"
  elif cmp -s "$src" "$dst"; then
    status="unchanged"
  else
    status="OVERWRITE"
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %-10s %s\n' "$status" "${entry#*|}"
  else
    cp "$src" "$dst"
  fi
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Re-run without --dry-run to apply."
else
  echo "Patches applied."
fi
