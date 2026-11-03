#!/usr/bin/env bash
# Install the tyger sidecar: venv + pinned upstream runtime + the public weights bundle, then upstream's own
# validator as the gate. Usage: install.sh [DIR]    (default ~/patchbay-tyger; DIR must not exist)
#
# You must provide a GPU build of torch (>= 4 GB GPU, CUDA or ROCm). It is not auto-detected. One of:
#   TORCH_INDEX_URL=<wheel index>   e.g. the CUDA index from https://pytorch.org/get-started/locally/
#                                   (upstream ran torch 2.13.0+cu130 on an RTX 5090)
#   TORCH_SPEC='torch==X.Y.Z'       a pin or a wheel URL (with TORCH_INDEX_URL for a ROCm index); default 'torch'
#   TORCH_FROM_VENV=<venv dir>      reuse the torch of an existing venv (a .pth onto its site-packages); the new
#                                   venv is made from that venv's own interpreter so the versions agree
# Other knobs: PYTHON (interpreter for the venv), HF_HOME (default DIR/hf).
set -euo pipefail

UPSTREAM_URL="https://github.com/nineninesix-ai/tyger-s2s"
UPSTREAM_SHA="97d011dcd4c8d2a117e54f00bf5b2d457cefa160"
BUNDLE_REPO="nineninesix/tyger"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:-$HOME/patchbay-tyger}"

if [ -e "$DIR" ]; then
  echo "install dir already exists: $DIR" >&2
  echo "Remove it or pass a fresh path, then re-run." >&2
  exit 1
fi
if [ -z "${TORCH_FROM_VENV:-}" ] && [ -z "${TORCH_SPEC:-}" ] && [ -z "${TORCH_INDEX_URL:-}" ]; then
  cat >&2 <<'MSG'
The tyger sidecar needs a GPU build of torch, and this script does not guess which one you have. Choose one:
  NVIDIA (CUDA):  TORCH_INDEX_URL=<the CUDA index for your driver, pytorch.org/get-started/locally> ./install.sh
  AMD (ROCm):     TORCH_INDEX_URL=<the ROCm index> TORCH_SPEC='torch==<version>' ./install.sh
  an existing venv that already has a working GPU torch:  TORCH_FROM_VENV=/path/to/venv ./install.sh
Nothing was created.
MSG
  exit 2
fi
if [ -n "${TORCH_FROM_VENV:-}" ]; then PYTHON="${PYTHON:-$TORCH_FROM_VENV/bin/python}"; else PYTHON="${PYTHON:-python3}"; fi
for t in git "$PYTHON"; do command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }; done

mkdir -p "$DIR"
DIR="$(cd "$DIR" && pwd)"
trap '[ $? -eq 0 ] || echo "install failed; remove $DIR before re-running" >&2' EXIT
export HF_HOME="${HF_HOME:-$DIR/hf}"
PIP=("$DIR/venv/bin/python" -m pip install --no-cache-dir --quiet)

echo "== venv ($DIR/venv, $("$PYTHON" --version 2>&1))"
"$PYTHON" -m venv "$DIR/venv"
if [ -n "${TORCH_FROM_VENV:-}" ]; then
  site="$("$TORCH_FROM_VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  echo "$site" > "$("$DIR/venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/torch_from_venv.pth"
else
  echo "== torch (${TORCH_SPEC:-torch}${TORCH_INDEX_URL:+ from $TORCH_INDEX_URL})"
  idx=(); if [ -n "${TORCH_INDEX_URL:-}" ]; then idx=(--index-url "$TORCH_INDEX_URL"); fi
  "${PIP[@]}" "${TORCH_SPEC:-torch}" "${idx[@]}"
fi
"$DIR/venv/bin/python" - <<'PY' || { echo "torch is installed but sees no GPU (CUDA or ROCm). The sidecar cannot run without one." >&2; exit 1; }
import torch, sys
if not torch.cuda.is_available():
    sys.exit(1)
print("torch", torch.__version__, "|", torch.cuda.get_device_name(0), "|", "ROCm" if torch.version.hip else "CUDA")
PY

echo "== dependencies"
"${PIP[@]}" transformers==5.14.1 safetensors==0.8.0 soundfile==0.14.0 aiohttp==3.14.5 PyYAML==6.0.3 \
  datasets==5.1.0 librosa==1.0.0 huggingface_hub pytest

echo "== upstream runtime @ ${UPSTREAM_SHA:0:8}"
git init -q "$DIR/tyger-s2s"
git -C "$DIR/tyger-s2s" fetch -q --depth 1 "$UPSTREAM_URL" "$UPSTREAM_SHA"
git -C "$DIR/tyger-s2s" checkout -q FETCH_HEAD
[ "$(git -C "$DIR/tyger-s2s" rev-parse HEAD)" = "$UPSTREAM_SHA" ] || { echo "upstream checkout is not the pinned SHA" >&2; exit 1; }

echo "== weights bundle ($BUNDLE_REPO, no token needed, license OpenMDW-1.1)"
"$DIR/venv/bin/python" - "$DIR/bundle" "$BUNDLE_REPO" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(sys.argv[2], local_dir=sys.argv[1], allow_patterns=["config.json", "model.safetensors", "nemotron/*.json"])
PY

echo "== upstream validator (batched engine vs plain reference; needs the GPU free)"
log="$DIR/validate.log"
PYTHONDONTWRITEBYTECODE=1 "$DIR/venv/bin/python" "$HERE/validate.py" "$DIR" 2>&1 | tee "$log" | grep -E "events match|tick ms" || true
good="$(grep -c 'events match: True' "$log" || true)"
if [ "$good" -lt 5 ] || grep -q 'events match: False' "$log"; then
  echo "VALIDATION FAILED: want 'events match: True' for all 5 sessions, got $good. Full output: $log" >&2
  exit 1
fi

echo "Run it:  TYGER_HOME=$DIR $HERE/run.sh"
echo "TYGER OK"
