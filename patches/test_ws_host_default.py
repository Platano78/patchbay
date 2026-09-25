"""The patched pipeline binds its WebSocket to loopback unless told otherwise.

The upstream framework's ``WebSocketStreamerArguments.ws_host`` default is
"0.0.0.0" (every interface). The socket carries audio, transcripts and full
control with no authentication, so ``patches/s2s_pipeline.py`` overrides the
default to "127.0.0.1" on both parse paths: ``set_defaults`` for the CLI, and
a post-``parse_json_file`` patch for JSON configs. An explicit ``--ws_host``
or a ``"ws_host"`` key in the JSON still wins; the systemd template and the
README pass 0.0.0.0 explicitly for LAN access.

``parse_arguments()`` is driven in a SUBPROCESS with this suite's own
interpreter (``sys.executable``), so no in-process ``speech_to_speech`` stub
installed by a sibling test module can shadow the real framework. The whole
file skips only when the real framework cannot be imported in a subprocess
(e.g. this repo's test venv, which has no torch/transformers/nltk stack).

Run from repo root (framework venv): python3 -m pytest patches/test_ws_host_default.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TIMEOUT = 120


def _run_python(code: str, args: tuple[str, ...] = ()):
    """Run ``code`` with the suite's own interpreter from the repo root."""
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


def _stderr_tail(proc, lines: int = 15) -> str:
    tail = proc.stderr.strip().splitlines()[-lines:]
    return "\n".join(tail) if tail else f"(no stderr; exit code {proc.returncode})"


# Import probe, once, in a subprocess: the only trustworthy test for "is the
# real framework here", because other modules in this directory stub
# `speech_to_speech` into *this* process's sys.modules for hermeticity.
_probe = _run_python("import speech_to_speech, torch, transformers, nltk")
if _probe.returncode != 0:
    pytest.skip(
        "the real speech_to_speech framework (and its nltk/torch/transformers "
        "stack) is not installed in this venv "
        f"(subprocess import failed: {_stderr_tail(_probe, 1)})",
        allow_module_level=True,
    )

# sys.argv[0] under `-c` is "-c"; rebuild the argv parse_arguments() expects.
# _parse_ws_host passes only the arguments after the program name, so
# sys.argv[1:] below is exactly the CLI args / JSON config path.
_PARSE_CODE = (
    "import sys\n"
    'sys.argv = ["s2s_pipeline.py", *sys.argv[1:]]\n'
    "from patches.s2s_pipeline import parse_arguments\n"
    "print(parse_arguments().websocket_streamer_kwargs.ws_host)\n"
)


def _parse_ws_host(argv: list[str], cwd: Path = _REPO_ROOT) -> str:
    """Run parse_arguments() out of process; return the resolved ws_host.

    ``argv`` is the full argv a launch would see (``argv[0]`` is the program
    name). A non-zero exit is a real break in the patch, not a skip-worthy
    absence: fail with the subprocess's stderr tail.
    """
    proc = _run_python(_PARSE_CODE, tuple(argv[1:]))
    if proc.returncode != 0:
        pytest.fail(
            f"parse_arguments subprocess failed (exit {proc.returncode}):\n"
            f"{_stderr_tail(proc)}"
        )
    return proc.stdout.strip()


def test_cli_without_ws_host_defaults_to_loopback():
    assert _parse_ws_host(["s2s_pipeline.py"]) == "127.0.0.1"


def test_cli_explicit_ws_host_wins():
    argv = ["s2s_pipeline.py", "--ws_host", "0.0.0.0"]
    assert _parse_ws_host(argv) == "0.0.0.0"


def test_json_without_ws_host_defaults_to_loopback(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"llm_backend": "responses-api"}))
    argv = ["s2s_pipeline.py", str(cfg)]
    assert _parse_ws_host(argv) == "127.0.0.1"


def test_json_with_ws_host_keeps_its_value(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ws_host": "0.0.0.0"}))
    argv = ["s2s_pipeline.py", str(cfg)]
    assert _parse_ws_host(argv) == "0.0.0.0"
