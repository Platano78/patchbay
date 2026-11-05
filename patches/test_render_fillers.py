"""examples/render_fillers.py against a fake speech server, plus pinning its pool
parsing to lm_output_processor's.

Run from repo root: python3 -m pytest patches/test_render_fillers.py -v
"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from patches import filler_clips as fc

_p = Path(__file__).resolve().parent.parent / "examples" / "render_fillers.py"
_spec = importlib.util.spec_from_file_location("render_fillers", _p)
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)

SRC = 48000


def _speech_pcm(seconds=0.5, lead=0.3, tail=0.4):
    t = np.arange(int(seconds * SRC))
    voice = (8000 * np.sin(t / 15.0)).astype(np.int16)
    return (np.zeros(int(lead * SRC), np.int16).tobytes() + voice.tobytes() + np.zeros(int(tail * SRC), np.int16).tobytes())


@pytest.fixture
def server():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("GET", self.path, dict(self.headers)))
            data = json.dumps({"voices": ["ryan", "the-butler"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("POST", self.path, dict(self.headers), body))
            data = _speech_pcm()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("VOICE_TOOL_FILLERS", "VOICE_TURN_FILLERS"):
        monkeypatch.delenv(k, raising=False)


def test_dry_run_lists_voices_times_phrases_and_writes_nothing(server, tmp_path, capsys):
    url, seen = server
    assert rf.main(["--base-url", url, "--dir", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2 voice(s) x 7 phrase(s)" in out
    assert out.count("render") >= 14 and "ryan" in out and "Let me check." in out
    assert list(tmp_path.iterdir()) == []
    assert [s[0] for s in seen] == ["GET"] and "model=voxcpm2" in seen[0][1]


def test_render_writes_trimmed_clips_at_the_pipeline_rate(server, tmp_path, monkeypatch):
    url, seen = server
    monkeypatch.setenv("VOICE_TOOL_FILLERS", "One sec.|Mm.")
    assert rf.main(["--base-url", url, "--dir", str(tmp_path), "--voice", "ryan"]) == 0
    clips = sorted((tmp_path / "ryan").glob("*.pcm"))
    assert [c.name for c in clips] == ["mm.pcm", "one_sec.pcm"]
    for c in clips:
        secs = c.stat().st_size / 2 / 16000
        assert 0.4 < secs < 0.7  # 0.5 s of speech, 0.7 s of padding silence trimmed away
    post = [s for s in seen if s[0] == "POST"][0]
    assert post[3] == {"model": "voxcpm2", "input": "One sec.", "voice": "ryan", "response_format": "pcm"} or post[3]["voice"] == "ryan"
    assert post[2].get("X-Voicebox-Priority") == "interactive"
    # and the runtime reads exactly these files
    monkeypatch.setenv("VOICE_FILLER_DIR", str(tmp_path))
    assert fc.load_clip("ryan", "One sec.") is not None


def test_render_is_idempotent(server, tmp_path, monkeypatch):
    url, seen = server
    monkeypatch.setenv("VOICE_TOOL_FILLERS", "One sec.")
    args = ["--base-url", url, "--dir", str(tmp_path), "--voice", "ryan"]
    rf.main(args)
    n = len([s for s in seen if s[0] == "POST"])
    rf.main(args)
    assert len([s for s in seen if s[0] == "POST"]) == n
    rf.main(args + ["--force"])
    assert len([s for s in seen if s[0] == "POST"]) == n + 1


def test_turn_pool_is_added_to_the_tool_pool(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_FILLERS", "A.|B.")
    monkeypatch.setenv("VOICE_TURN_FILLERS", "Mm.|A.")
    assert rf.active_phrases() == ["A.", "B.", "Mm."]


@pytest.mark.parametrize("tool,turn", [(None, None), ("off", None), ("X.|Y.", None), (" | ", None), ("X.", "Mm.|Okay."), (None, "off"), ("X.", " ")])
def test_pool_parsing_matches_lm_output_processor(monkeypatch, tool, turn):
    import patches.test_lm_output_processor  # noqa: F401  (installs the stubs lm_output_processor needs)
    from patches import lm_output_processor as lop

    for k, v in (("VOICE_TOOL_FILLERS", tool), ("VOICE_TURN_FILLERS", turn)):
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    want_tool = lop._parse_tool_fillers()
    monkeypatch.setattr(lop, "_TOOL_FILLERS", want_tool)
    want_turn = lop._parse_turn_fillers()
    got_tool = rf.parse_pool("VOICE_TOOL_FILLERS", rf._default_pool())
    assert got_tool == want_tool
    assert rf.parse_pool("VOICE_TURN_FILLERS", got_tool) == want_turn
    assert rf._default_pool() == lop._DEFAULT_TOOL_FILLERS


def test_clip_outside_length_bounds_is_rejected(server, tmp_path, monkeypatch):
    url, _ = server
    monkeypatch.setenv("VOICE_TOOL_FILLERS", "Short.")
    monkeypatch.setattr(rf, "render", lambda *a, **k: np.zeros(100, np.int16).tobytes() + (np.ones(50, np.int16) * 9000).tobytes())
    assert rf.main(["--base-url", url, "--dir", str(tmp_path), "--voice", "ryan"]) == 1
    assert not list(tmp_path.rglob("*.pcm"))
