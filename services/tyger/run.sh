#!/usr/bin/env bash
# Launch the tyger sidecar from an install made by install.sh. Extra arguments go to server.py.
#   TYGER_HOME          install dir (default ~/patchbay-tyger)
#   TYGER_ROCR_PRELOAD  ROCm only, optional: space-separated libraries to LD_PRELOAD (README, "ROCm idle spin")
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TYGER_HOME="${TYGER_HOME:-$HOME/patchbay-tyger}"
PY="$TYGER_HOME/venv/bin/python"
[ -x "$PY" ] || { echo "no tyger install at $TYGER_HOME (run services/tyger/install.sh, or set TYGER_HOME)" >&2; exit 1; }
export HF_HOME="${HF_HOME:-$TYGER_HOME/hf}" HF_HUB_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1

# The preload applies only when it is set AND every named file exists; it is never a reason to refuse to start.
preload=""
if [ -n "${TYGER_ROCR_PRELOAD:-}" ]; then
  ok=1
  for lib in $TYGER_ROCR_PRELOAD; do
    if [ ! -f "$lib" ]; then
      echo "WARNING: TYGER_ROCR_PRELOAD names $lib, which does not exist; starting without the preload (a ROCm runtime may spin one core while idle)" >&2
      ok=0
    fi
  done
  if [ "$ok" = 1 ]; then preload="$TYGER_ROCR_PRELOAD"; fi
elif [ "$("$PY" -c 'import torch; print(1 if torch.version.hip else 0)' 2>/dev/null || echo 0)" = 1 ]; then
  echo "WARNING: this is a ROCm torch and TYGER_ROCR_PRELOAD is not set; the ROCm runtime may spin one core while idle (README, 'ROCm idle spin')" >&2
fi
if [ -n "$preload" ]; then export LD_PRELOAD="$preload${LD_PRELOAD:+ $LD_PRELOAD}"; fi
exec "$PY" "$HERE/server.py" --home "$TYGER_HOME" "$@"
