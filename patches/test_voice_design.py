"""On-the-fly voice design, remote cloning and the three LLM tools, against a fake
speech server (the voicegate `/v1/voices/*` API). Nothing here touches a real
server; the opt-in live test is test_voice_design_live.py.

Run from repo root: python3 -m pytest patches/test_voice_design.py -v
"""

from __future__ import annotations

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue

import numpy as np
import pytest

# test_brain_control installs the speech_to_speech stubs brain_control needs and
# owns the fakes; its autouse fixtures are repeated below for this module.
from patches import test_brain_control as tbc
from patches import phone_context, tts_capabilities, voice_design
from patches import filler_clips as fc

sys.modules.setdefault("speech_to_speech.phone_context", phone_context)
sys.modules["speech_to_speech"].phone_context = phone_context
from patches import voice_tools as vt  # noqa: E402

brain_control = tbc.brain_control

SRC = 48000
PCM = (8000 * np.sin(np.arange(int(0.5 * SRC)) / 15.0)).astype("<i2").tobytes()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CHOICE_FILE", str(tmp_path / "voice_choice.json"))
    monkeypatch.setenv("VOICE_FILLER_DIR", str(tmp_path / "fillers"))
    monkeypatch.delenv("VOICE_DESIGN_TOOLS", raising=False)
    monkeypatch.setattr(voice_design, "active_phrases", lambda: ["One sec.", "Checking."])
    tts_capabilities._cache.clear()
    yield
    tts_capabilities._cache.clear()
    vt.set_voice_controller(None)


class FakeServer:
    def __init__(self):
        self.voices = ["jean", "alba"]
        self.calls: list[tuple] = []
        self.design_status = 200
        self.clone_status = 200
        self.design_delay = 0.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, status, body=None, ctype="application/json"):
                data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def do_GET(self):
                if self.path.startswith("/v1/audio/voices"):
                    self._send(200, {"voices": [{"name": v} for v in outer.voices]})
                else:
                    self._send(404, {"error": {"message": "nope"}})

            def do_POST(self):
                raw = self._body()
                p = self.path
                if p == "/v1/voices/design":
                    body = json.loads(raw)
                    outer.calls.append(("design", body))
                    if outer.design_delay:
                        import time as _t

                        _t.sleep(outer.design_delay)
                    if outer.design_status != 200:
                        return self._send(outer.design_status, {"error": {"message": "render failed"}})
                    outer.voices.append("designed:d1")
                    return self._send(200, {"voice": "designed:d1", "id": "d1", "name": "d1", "seconds": 4.2})
                if p == "/v1/voices/clone":
                    outer.calls.append(("clone", self.headers.get("Content-Type"), raw))
                    if outer.clone_status != 200:
                        return self._send(outer.clone_status, {"error": {"message": "sample too short"}})
                    outer.voices.append("designed:c1")
                    return self._send(200, {"voice": "designed:c1", "id": "c1", "name": "mine", "seconds": 2.0})
                if p.startswith("/v1/voices/designed/") and p.endswith("/rename"):
                    body = json.loads(raw)
                    outer.calls.append(("rename", p, body))
                    old = "designed:" + p.split("/")[4]
                    new = "designed:" + body["name"].lower()
                    outer.voices[outer.voices.index(old)] = new
                    return self._send(200, {"voice": new, "id": new[9:], "name": body["name"]})
                if p == "/v1/audio/speech":
                    body = json.loads(raw)
                    outer.calls.append(("speech", body))
                    if body.get("voice", "").startswith("designed:") and body["voice"] not in outer.voices:
                        return self._send(404, {"error": {"message": "unknown voice"}})
                    return self._send(200, PCM, "application/octet-stream")
                self._send(404, {"error": {"message": "nope"}})

            def do_DELETE(self):
                outer.calls.append(("delete", self.path))
                voice = "designed:" + self.path.rsplit("/", 1)[1]
                if voice in outer.voices:
                    outer.voices.remove(voice)
                self._send(204)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def kinds(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


def _handler(url, voice="jean"):
    return types.SimpleNamespace(base_url=url, voice=voice, model="voxcpm2", sample_rate=16000, source_sample_rate=SRC)


def _bc(tmp_path, server, voice="jean"):
    handler = _handler(server.url, voice)
    streamer = tbc._FakeStreamer()
    bc = tbc._make_brain_control(tmp_path, tts_handler=handler, streamer=streamer, tts_queue=Queue())
    return bc, handler, streamer


def _join_fillers():
    for t in threading.enumerate():
        if t.name == "voice-design-fillers":
            t.join(10)


def _saved_voice():
    return json.loads(brain_control.voice_choice_path().read_text())["voice"]


# ── design_voice ─────────────────────────────────────────────────────────────


def test_design_voice_switches_persists_and_renders_fillers(tmp_path, server):
    bc, handler, streamer = _bc(tmp_path, server)

    out = bc.design_voice("a cheerful elderly Irish storyteller")

    assert out.startswith("Voice ready")
    assert handler.voice == "designed:d1"
    assert _saved_voice() == "designed:d1"
    assert server.calls[0] == ("design", {"description": "a cheerful elderly Irish storyteller"})
    # the capability cache was refreshed, so the new voice is a listed voice
    assert "designed:d1" in tts_capabilities.get_capabilities(handler).voices
    # no audition: the LLM's own reply is the audition
    assert bc.tts_queue.empty()
    _join_fillers()
    d = tmp_path / "fillers" / "designed_d1"
    assert sorted(p.name for p in d.iterdir()) == ["checking.pcm", "one_sec.pcm"]
    assert fc.load_clip("designed:d1", "One sec.") is not None  # filler lookup uses the same mapping
    assert streamer.broadcasts and streamer.broadcasts[-1]["voice"] == "designed:d1"


def test_design_failure_leaves_voice_unchanged(tmp_path, server):
    server.design_status = 502
    bc, handler, _ = _bc(tmp_path, server)

    out = bc.design_voice("a pirate")

    assert "couldn't design" in out and "unchanged" in out
    assert handler.voice == "jean"
    assert not brain_control.voice_choice_path().exists()
    assert bc._voice_before_design is None


def test_design_unreachable_server_is_a_plain_error(tmp_path):
    handler = _handler(tbc._closed_port_url())
    bc = tbc._make_brain_control(tmp_path, tts_handler=handler)
    out = bc.design_voice("a pirate")
    assert "unreachable" in out and handler.voice == "jean"


def test_design_short_description_refused_before_any_call(tmp_path, server):
    bc, handler, _ = _bc(tmp_path, server)
    assert "too short" in bc.design_voice("  hi ")[:60] or "too short" in bc.design_voice("hi")
    assert server.calls == []


def test_long_description_is_trimmed_to_300(tmp_path, server):
    bc, _, _ = _bc(tmp_path, server)
    bc.design_voice("very " * 100)
    assert len(server.calls[0][1]["description"]) <= 300


def test_design_trusts_the_new_voice_even_if_the_probe_is_stale(tmp_path, server, monkeypatch):
    bc, handler, _ = _bc(tmp_path, server)
    monkeypatch.setattr(tts_capabilities, "invalidate", lambda h: None)  # cache stays stale
    tts_capabilities.get_capabilities(handler)  # prime: lacks designed:d1
    assert bc.design_voice("a pirate").startswith("Voice ready")
    assert handler.voice == "designed:d1"


def test_flag_off_disables_everything(tmp_path, server, monkeypatch):
    monkeypatch.setenv("VOICE_DESIGN_TOOLS", "off")
    bc, handler, _ = _bc(tmp_path, server)
    assert "isn't available" in bc.design_voice("a pirate")
    assert server.calls == []
    assert bc.voice_design_available() is False


def test_local_pocket_backend_is_untouched(tmp_path):
    bc = tbc._make_brain_control(tmp_path, tts_handler=tbc._FakeTTSHandler("jean"))
    assert bc.voice_design_available() is False
    assert "isn't available" in bc.design_voice("a pirate")


# ── restore / save ───────────────────────────────────────────────────────────


def test_restore_voice_goes_back_to_the_voice_before_the_design(tmp_path, server):
    bc, handler, _ = _bc(tmp_path, server, voice="alba")
    bc.design_voice("a pirate")
    assert handler.voice == "designed:d1"

    assert bc.restore_voice().startswith("Back to")
    assert handler.voice == "alba"
    assert _saved_voice() == "alba"


def test_restore_without_a_design_uses_the_boot_voice(tmp_path, server):
    bc, handler, _ = _bc(tmp_path, server, voice="jean")
    handler.voice = "alba"
    assert bc.restore_voice().startswith("Back to")
    assert handler.voice == "jean"


def test_save_voice_renames_current_designed_voice_and_carries_fillers(tmp_path, server):
    bc, handler, _ = _bc(tmp_path, server)
    bc.design_voice("a lighthouse keeper")
    _join_fillers()

    out = bc.save_voice("Keeper")

    assert out.startswith("Saved") and "Keeper" in out
    assert server.calls[-1] == ("rename", "/v1/voices/designed/d1/rename", {"name": "Keeper"})
    assert handler.voice == "designed:keeper"
    assert _saved_voice() == "designed:keeper"
    _join_fillers()
    assert fc.load_clip("designed:keeper", "One sec.") is not None
    assert not (tmp_path / "fillers" / "designed_d1").exists()
    # "go back" still works after a save
    assert bc.restore_voice().startswith("Back to") and handler.voice == "jean"


def test_save_voice_refuses_a_preset(tmp_path, server):
    bc, handler, _ = _bc(tmp_path, server)
    out = bc.save_voice("Keeper")
    assert "nothing to save" in out
    assert server.calls == []


# ── name safety ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["designed:../x", "designed:", "designed:a b", "designed:a/b", "x:designed:a", "designed:" + "a" * 65])
def test_malformed_designed_names_are_not_designed_names(bad):
    assert voice_design.is_designed_name(bad) is False


def test_unlisted_designed_name_is_unknown_and_colon_is_not_a_clone_name(tmp_path, server):
    bc, handler, _ = _bc(tmp_path, server)
    ok, err = bc._set_voice("designed:nope")
    assert (ok, err[:13]) == (False, "unknown voice")
    assert handler.voice == "jean"
    from patches import voice_clone

    assert voice_clone.is_name_format_valid("designed:d1") is False  # strict local gate unchanged


def test_ui_can_select_a_server_listed_designed_voice(tmp_path, server):
    server.voices.append("designed:old1")
    bc, handler, _ = _bc(tmp_path, server)
    ok, err = bc._set_voice("designed:old1", audition=False)
    assert ok, err
    assert handler.voice == "designed:old1"


def test_filler_dirname_has_no_colon():
    assert ":" not in fc.voice_dirname("designed:abc_1")


# ── state payload + delete ───────────────────────────────────────────────────


def test_state_lists_designed_voices_as_custom_and_offers_clone(tmp_path, server):
    server.voices.append("designed:old1")
    bc, _, _ = _bc(tmp_path, server)
    st = bc._state_payload()
    assert st["voices"] == ["alba", "jean"]
    assert "designed:old1" in st["custom_voices"]
    assert st["can_clone"] is True


def test_state_unchanged_when_flag_off(tmp_path, server, monkeypatch):
    monkeypatch.setenv("VOICE_DESIGN_TOOLS", "off")
    server.voices.append("designed:old1")
    bc, _, _ = _bc(tmp_path, server)
    st = bc._state_payload()
    assert "designed:old1" in st["voices"] and st["can_clone"] is False


def test_delete_designed_voice_calls_the_server_and_refreshes(tmp_path, server):
    server.voices.append("designed:old1")
    bc, handler, streamer = _bc(tmp_path, server)
    ack = bc._config_set({"voice_delete": "designed:old1"})
    assert ack["ok"] is True
    assert ("delete", "/v1/voices/designed/old1") in server.calls
    assert "designed:old1" not in tts_capabilities.get_capabilities(handler).voices


def test_delete_active_designed_voice_is_refused(tmp_path, server):
    bc, handler, _ = _bc(tmp_path, server)
    bc.design_voice("a pirate")
    ack = bc._config_set({"voice_delete": "designed:d1"})
    assert ack["ok"] is False and "switch to another voice" in ack["error"]
    assert "delete" not in server.kinds()


# ── remote clone through the existing recording flow ─────────────────────────


def _wav(seconds=3.0, rate=16000):
    import io
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, (0.3 * np.sin(np.arange(int(seconds * rate)) / 20.0)).astype("float32"), rate, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def test_remote_clone_flow_uploads_selects_and_renders_fillers(tmp_path, server, monkeypatch):
    monkeypatch.setattr(
        sys.modules["speech_to_speech.pipeline.messages"], "TTSInput", lambda **kw: types.SimpleNamespace(**kw), raising=False
    )
    bc, handler, streamer = _bc(tmp_path, server)
    wav = _wav()

    begin = bc.handle({"type": "voice_clone_begin", "name": "mine", "ext": ".wav", "size": len(wav)})
    assert begin == {"type": "voice_clone_progress", "stage": "receiving"}
    end = bc.handle({"type": "voice_clone_end", "name": "mine", "ext": ".wav", "data": wav})

    assert end["ok"] is True and end["voice"] == "designed:c1"
    kind, ctype, raw = [c for c in server.calls if c[0] == "clone"][0]
    assert ctype.startswith("multipart/form-data")
    assert b'name="file"' in raw and b'name="name"' in raw and b"mine" in raw and b"RIFF" in raw
    assert handler.voice == "designed:c1" and _saved_voice() == "designed:c1"
    # existing behaviour kept: the user hears the new voice (audition by the name they gave)
    assert bc.tts_queue.get_nowait().text == "Hi, I'm mine. This is how I sound."
    _join_fillers()
    assert fc.load_clip("designed:c1", "One sec.") is not None


def test_remote_clone_server_rejection_is_reported_and_voice_unchanged(tmp_path, server):
    server.clone_status = 400
    bc, handler, _ = _bc(tmp_path, server)
    end = bc.handle({"type": "voice_clone_end", "name": "mine", "ext": ".wav", "data": _wav()})
    assert end["ok"] is False and "sample too short" in end["error"]
    assert handler.voice == "jean"


def test_remote_clone_begin_still_validates_name_ext_size(tmp_path, server):
    bc, _, _ = _bc(tmp_path, server)
    assert bc.handle({"type": "voice_clone_begin", "name": "Bad Name", "ext": ".wav", "size": 10})["ok"] is False
    assert bc.handle({"type": "voice_clone_begin", "name": "ok", "ext": ".m4a", "size": 10})["ok"] is False
    assert bc.handle({"type": "voice_clone_begin", "name": "jean", "ext": ".wav", "size": 10})["ok"] is False  # taken by a preset


def test_clone_with_flag_off_keeps_the_old_refusal(tmp_path, server, monkeypatch):
    monkeypatch.setenv("VOICE_DESIGN_TOOLS", "off")
    bc, _, _ = _bc(tmp_path, server)
    r = bc.handle({"type": "voice_clone_begin", "name": "mine", "ext": ".wav", "size": 10})
    assert r["ok"] is False and "does not support voice cloning" in r["error"]


# ── the tools ────────────────────────────────────────────────────────────────


def _arm(monkeypatch):
    monkeypatch.setattr(vt, "_probe", lambda *a, **k: False)
    monkeypatch.setattr(vt, "_probe_health", lambda *a, **k: False)
    monkeypatch.delenv("VOICE_TOOLS", raising=False)
    monkeypatch.setattr(vt, "_TOOLS_DIR", "")
    return [t["name"] for t in vt.get_tool_defs()]


def test_tools_armed_only_for_remote_backend_with_flag_on(tmp_path, server, monkeypatch):
    _bc(tmp_path, server)  # registers itself as the controller
    # the stub voice_tools used by brain_control tests is not the real module: register on the real one
    vt.set_voice_controller(_bc(tmp_path, server)[0])
    names = _arm(monkeypatch)
    assert {"design_voice", "save_voice", "restore_voice"} <= set(names)

    monkeypatch.setenv("VOICE_DESIGN_TOOLS", "off")
    assert "design_voice" not in _arm(monkeypatch)

    monkeypatch.delenv("VOICE_DESIGN_TOOLS")
    vt.set_voice_controller(tbc._make_brain_control(tmp_path, tts_handler=tbc._FakeTTSHandler("jean")))
    assert "design_voice" not in _arm(monkeypatch)


def test_tool_descriptions_forbid_unprompted_use():
    by_name = {t["name"]: t for t in vt.TOOL_DEFS}
    d = by_name["design_voice"]["description"]
    assert "ONLY when the user explicitly asks" in d and "Never call it on your own" in d
    assert "ONLY when the user asks" in by_name["save_voice"]["description"]
    assert "ONLY when the user asks" in by_name["restore_voice"]["description"]
    assert by_name["design_voice"]["parameters"]["required"] == ["description"]


def test_execute_runs_the_three_tools_end_to_end(tmp_path, server, monkeypatch):
    bc, handler, _ = _bc(tmp_path, server, voice="alba")
    vt.set_voice_controller(bc)
    _arm(monkeypatch)

    assert vt.execute("design_voice", {"description": "a grumpy old lighthouse keeper"}).startswith("Voice ready")
    assert handler.voice == "designed:d1"
    assert vt.execute("save_voice", {"name": "Keeper"}).startswith("Saved")
    assert handler.voice == "designed:keeper"
    assert vt.execute("restore_voice", {}).startswith("Back to")
    assert handler.voice == "alba"
    _join_fillers()


def test_execute_without_a_controller_is_a_plain_message():
    vt.set_voice_controller(None)
    assert "isn't available" in vt.execute("design_voice", {"description": "a pirate"})
    assert "isn't available" in vt.execute("restore_voice", {})


# ── filler core shared with scripts/render_fillers.py ────────────────────────


def test_render_voice_fillers_skips_existing(tmp_path, server):
    root = tmp_path / "r"
    server.voices.append("designed:d1")
    first = voice_design.render_voice_fillers(server.url, "designed:d1", phrases=["One sec."], root=root)
    assert [p.name for p in first] == ["one_sec.pcm"]
    again = voice_design.render_voice_fillers(server.url, "designed:d1", phrases=["One sec."], root=root)
    assert again == []


# ── review fixes ─────────────────────────────────────────────────────────────


def test_trailing_newline_is_not_a_valid_name(tmp_path, server):
    from patches import voice_clone

    assert voice_design.is_designed_name("designed:abc\n") is False
    assert voice_clone.is_name_format_valid("abc\n") is False
    bc, handler, _ = _bc(tmp_path, server)
    ack = bc._config_set({"voice_delete": "designed:abc\n"})
    assert ack["ok"] is False
    assert server.calls == []


def test_late_design_does_not_switch_the_voice(tmp_path, server, monkeypatch):
    # The tool already told the LLM it timed out; a design that lands after that
    # must not change the voice behind the user's back.
    monkeypatch.setattr(voice_design, "TOOL_DEADLINE_S", 0.2, raising=False)
    server.design_delay = 0.6
    bc, handler, _ = _bc(tmp_path, server)

    out = bc.design_voice("a pirate")

    assert handler.voice == "jean"
    assert not brain_control.voice_choice_path().exists()
    assert bc._voice_before_design is None
    assert "unchanged" in out
    assert ("delete", "/v1/voices/designed/d1") in server.calls  # orphan cleaned up
