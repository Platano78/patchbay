"""Opt-in live check of voice design + remote clone against a real speech server.

Skipped unless VOICE_DESIGN_LIVE_URL is set (e.g. http://<speech-server>:27419).
Optional: VOICE_DESIGN_LIVE_WAV=/path/to/sample.wav also exercises the clone path.
It creates voices on the server and deletes the ones it created.

    VOICE_DESIGN_LIVE_URL=http://<speech-server>:27419 \\
      python3 -m pytest -q -s patches/test_voice_design_live.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from queue import Queue

import pytest

from patches import test_brain_control as tbc
from patches import phone_context, tts_capabilities, voice_design
from patches import filler_clips as fc

URL = os.environ.get("VOICE_DESIGN_LIVE_URL", "").strip()
pytestmark = pytest.mark.skipif(not URL, reason="VOICE_DESIGN_LIVE_URL not set")

sys.modules.setdefault("speech_to_speech.phone_context", phone_context)
sys.modules["speech_to_speech"].phone_context = phone_context
from patches import voice_tools as vt  # noqa: E402


class _In:
    def __init__(self, text):
        self.text = text


def _real_handler():
    import patches.remote_speech_tts_handler as rsh
    from threading import Event

    return rsh.RemoteSpeechTTSHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_args=(Event(),),
        setup_kwargs=dict(
            base_url=URL,
            model="voxcpm2",
            voice="ryan",
            sample_rate=16000,
            source_sample_rate=48000,
            connect_timeout_s=2.0,
            read_timeout_s=60.0,
            max_duration_s=30.0,
            pocket_kwargs={"voice": "jean"},
        ),
    )


def _speak(handler, text):
    t0 = time.monotonic()
    nbytes = 0
    for out in handler.process(_In(text)):
        audio = getattr(out, "audio", out)
        nbytes += getattr(audio, "nbytes", len(audio) if hasattr(audio, "__len__") else 0)
    return nbytes, time.monotonic() - t0


def _join_fillers():
    for t in threading.enumerate():
        if t.name == "voice-design-fillers":
            t.join(300)


@pytest.fixture
def bc(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CHOICE_FILE", str(tmp_path / "voice_choice.json"))
    monkeypatch.setenv("VOICE_FILLER_DIR", str(tmp_path / "fillers"))
    monkeypatch.setattr(voice_design, "active_phrases", lambda: ["One sec.", "Checking.", "On it.", "Let me look."])
    tts_capabilities._cache.clear()
    handler = _real_handler()
    bc = tbc._make_brain_control(tmp_path, tts_handler=handler, streamer=tbc._FakeStreamer(), tts_queue=Queue())
    vt.set_voice_controller(bc)
    created: list[str] = []
    yield bc, handler, tmp_path, created
    for v in created:
        try:
            voice_design.delete(URL, v)
        except Exception as e:  # cleanup only
            print("cleanup failed", v, e)
    vt.set_voice_controller(None)


def test_live_design_switch_speak_and_fillers(bc):
    bc, handler, tmp_path, created = bc
    t0 = time.monotonic()
    out = vt.execute("design_voice", {"description": "a cheerful elderly Irish storyteller"})
    t_design = time.monotonic() - t0
    voice = handler.voice
    print(f"\nDESIGN result={out!r} voice={voice} tool_time={t_design:.1f}s")
    assert out.startswith("Voice ready"), out
    created.append(voice)
    assert voice_design.is_designed_name(voice)

    nbytes, t_speak = _speak(handler, "Ah, sure, it is a grand soft day for a story.")
    print(f"SPEAK bytes={nbytes} time={t_speak:.1f}s")
    assert nbytes > 8000

    t1 = time.monotonic()
    _join_fillers()
    d = tmp_path / "fillers" / fc.voice_dirname(voice)
    files = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    print(f"FILLERS dir={d} files={files} wait={time.monotonic() - t1:.1f}s")
    assert files and all(f.endswith(".pcm") for f in files)

    # save -> rename, fillers follow, restore
    saved = vt.execute("save_voice", {"name": "Live Gate Test"})
    print("SAVE", saved, "->", handler.voice)
    assert saved.startswith("Saved"), saved
    created[0] = handler.voice
    assert fc.load_clip(handler.voice, "One sec.") is not None
    back = vt.execute("restore_voice", {})
    print("RESTORE", back, "->", handler.voice)
    assert handler.voice == "ryan"


@pytest.mark.skipif(not os.environ.get("VOICE_DESIGN_LIVE_WAV"), reason="VOICE_DESIGN_LIVE_WAV not set")
def test_live_clone_through_the_recording_flow(bc):
    bc, handler, tmp_path, created = bc
    path = os.environ["VOICE_DESIGN_LIVE_WAV"]
    raw = open(path, "rb").read()
    begin = bc.handle({"type": "voice_clone_begin", "name": "livegate", "ext": ".wav", "size": len(raw)})
    assert begin["type"] == "voice_clone_progress", begin
    t0 = time.monotonic()
    end = bc.handle({"type": "voice_clone_end", "name": "livegate", "ext": ".wav", "data": raw})
    print(f"\nCLONE result={end} time={time.monotonic() - t0:.1f}s")
    assert end["ok"] is True, end
    created.append(end["voice"])
    assert handler.voice == end["voice"]
    nbytes, t_speak = _speak(handler, "This is a sentence in the cloned voice.")
    print(f"SPEAK bytes={nbytes} time={t_speak:.1f}s")
    assert nbytes > 8000
    _join_fillers()
