"""Pipeline logging setup: httpx request lines are quieted, and the deployed
patch-pack revision (written by apply.sh) is logged once at startup.

Driven in a SUBPROCESS against the real framework, like test_ws_host_default.py;
skips when the framework is not importable.

Run from repo root (framework venv): python3 -m pytest patches/test_pipeline_logging.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(code: str):
    return subprocess.run([sys.executable, "-c", code], cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=120)


_probe = _run("import speech_to_speech, torch, transformers, nltk")
if _probe.returncode != 0:
    pytest.skip("the real speech_to_speech framework is not installed in this venv", allow_module_level=True)


def test_setup_logger_quiets_httpx_and_logs_the_patch_version():
    proc = _run(
        "import logging\n"
        "import conftest; conftest.alias_unapplied_pack_modules()\n"
        "from patches import s2s_pipeline\n"
        "s2s_pipeline.setup_logger('info')\n"
        "print('HTTPX', logging.getLogger('httpx').getEffectiveLevel() == logging.WARNING)\n"
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "HTTPX True" in proc.stdout
    assert "Patchbay patch pack unknown" in proc.stderr  # no PATCH_VERSION beside patches/s2s_pipeline.py


def test_patch_version_reads_the_file_apply_sh_wrote():
    proc = _run(
        "import tempfile, pathlib\n"
        "import conftest; conftest.alias_unapplied_pack_modules()\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "(d / 'PATCH_VERSION').write_text('abc1234-dirty\\n')\n"
        "from patches import s2s_pipeline\n"
        "s2s_pipeline.__file__ = str(d / 's2s_pipeline.py')\n"
        "print('VER', s2s_pipeline._patch_version())\n"
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "VER abc1234-dirty" in proc.stdout
