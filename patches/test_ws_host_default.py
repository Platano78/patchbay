"""The patched pipeline binds its WebSocket to loopback unless told otherwise.

The upstream framework's ``WebSocketStreamerArguments.ws_host`` default is
"0.0.0.0" (every interface). The socket carries audio, transcripts and full
control with no authentication, so ``patches/s2s_pipeline.py`` overrides the
default to "127.0.0.1" on both parse paths: ``set_defaults`` for the CLI, and
a post-``parse_json_file`` patch for JSON configs. An explicit ``--ws_host``
or a ``"ws_host"`` key in the JSON still wins; the systemd template and the
README pass 0.0.0.0 explicitly for LAN access.

These tests drive ``parse_arguments()`` through its real entry (sys.argv),
which imports the real framework, so the whole file skips where
``speech_to_speech`` is not installed (e.g. this repo's test venv).

Run from repo root (framework venv): python3 -m pytest patches/test_ws_host_default.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

_s2s = pytest.importorskip("speech_to_speech")

# importorskip alone cannot prove the real framework is installed: other test
# modules in this directory stub `speech_to_speech` into sys.modules for
# hermeticity. Require the genuine package (a real __file__) plus the stack
# patches/s2s_pipeline.py imports at module level, else skip the module.
if not getattr(_s2s, "__file__", None) or [
    m for m in ("nltk", "torch", "transformers") if importlib.util.find_spec(m) is None
]:
    pytest.skip(
        "the real speech_to_speech framework (and its nltk/torch/transformers "
        "stack) is not installed in this venv",
        allow_module_level=True,
    )

from patches.s2s_pipeline import parse_arguments  # noqa: E402


def _ws_host(argv: list[str], monkeypatch) -> str:
    monkeypatch.setattr(sys, "argv", argv)
    return parse_arguments().websocket_streamer_kwargs.ws_host


def test_cli_without_ws_host_defaults_to_loopback(monkeypatch):
    assert _ws_host(["s2s_pipeline.py"], monkeypatch) == "127.0.0.1"


def test_cli_explicit_ws_host_wins(monkeypatch):
    argv = ["s2s_pipeline.py", "--ws_host", "0.0.0.0"]
    assert _ws_host(argv, monkeypatch) == "0.0.0.0"


def test_json_without_ws_host_defaults_to_loopback(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"llm_backend": "responses-api"}))
    argv = ["s2s_pipeline.py", str(cfg)]
    assert _ws_host(argv, monkeypatch) == "127.0.0.1"


def test_json_with_ws_host_keeps_its_value(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ws_host": "0.0.0.0"}))
    argv = ["s2s_pipeline.py", str(cfg)]
    assert _ws_host(argv, monkeypatch) == "0.0.0.0"
