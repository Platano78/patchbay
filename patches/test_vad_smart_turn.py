"""Runs patches/vad_cases/test_vad_handler_cases.py (the patched VADHandler's Smart Turn
act/shadow, PTT-skip and turn-end-filler tests) in a fresh interpreter.

Those tests need the real framework package; many other test files replace
`speech_to_speech` with a stub at collection time, which a shared session cannot
undo. A subprocess sees the real one (or skips if torch/the package are absent).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_patched_vad_handler_smart_turn_cases():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "patches/vad_cases/test_vad_handler_cases.py", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
