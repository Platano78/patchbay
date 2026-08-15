"""Unit tests for voice_clone.py (custom voice cloning: name rules, chunked-
upload state machine, sidecar dir, audition text).

Run from repo root: python3 -m pytest patches/test_voice_clone.py -v

voice_clone.py has no `speech_to_speech` import and only lazily imports
`soundfile`/`pocket_tts` inside the functions that need them, so these tests
need no stubs and no installed package -- except the normalization tests,
which are skipped when `soundfile` isn't installed (it is NOT installed on
this dev box).
"""

from __future__ import annotations

import importlib.util

import pytest

from patches.voice_clone import (
    MAX_CHUNK_BYTES,
    MAX_DECODED_SECONDS,
    MAX_UPLOAD_BYTES,
    UploadManager,
    atomic_export_state,
    check_delete_allowed,
    normalize_to_wav,
    resolve_audition_text,
    validate_extension,
    validate_name,
    validate_size,
)

PREDEFINED = ("alba", "jean", "anna")

HAS_SOUNDFILE = importlib.util.find_spec("soundfile") is not None


# ── name rules ───────────────────────────────────────────────────────────


def test_valid_name_accepted():
    assert validate_name("my_voice-1", PREDEFINED) == (True, "")


def test_name_uppercase_rejected():
    ok, error = validate_name("MyVoice", PREDEFINED)
    assert not ok
    assert "invalid name" in error


def test_name_symbol_rejected():
    ok, error = validate_name("my voice!", PREDEFINED)
    assert not ok
    assert "invalid name" in error


def test_name_too_long_rejected():
    ok, error = validate_name("a" * 33, PREDEFINED)
    assert not ok
    assert "invalid name" in error


def test_name_empty_rejected():
    ok, error = validate_name("", PREDEFINED)
    assert not ok


def test_name_non_str_rejected():
    ok, error = validate_name(None, PREDEFINED)
    assert not ok


def test_name_predefined_collision_rejected():
    ok, error = validate_name("alba", PREDEFINED)
    assert not ok
    assert "built-in" in error


def test_name_max_length_boundary_accepted():
    assert validate_name("a" * 32, PREDEFINED)[0] is True


# ── extension rules ──────────────────────────────────────────────────────


@pytest.mark.parametrize("ext", [".wav", "wav", ".aiff", ".aif", ".flac", ".ogg", ".mp3", "MP3"])
def test_accepted_extensions(ext):
    assert validate_extension(ext) == (True, "")


@pytest.mark.parametrize("ext", [".webm", ".m4a", ".mp4"])
def test_rejected_extensions_have_convert_guidance(ext):
    ok, error = validate_extension(ext)
    assert not ok
    assert "convert" in error.lower()


def test_unknown_extension_rejected():
    ok, error = validate_extension(".xyz")
    assert not ok
    assert "unsupported" in error


def test_missing_extension_rejected():
    ok, error = validate_extension(None)
    assert not ok
    assert "missing" in error


# ── size rules ───────────────────────────────────────────────────────────


def test_size_within_cap_accepted():
    assert validate_size(1024) == (True, "")


def test_size_at_cap_accepted():
    assert validate_size(MAX_UPLOAD_BYTES)[0] is True


def test_size_over_cap_rejected():
    ok, error = validate_size(MAX_UPLOAD_BYTES + 1)
    assert not ok
    assert "MB cap" in error


def test_size_zero_rejected():
    assert validate_size(0)[0] is False


def test_size_non_numeric_rejected():
    assert validate_size("big")[0] is False


def test_size_bool_rejected():
    # bool is a subclass of int -- must not be accepted as a byte count.
    assert validate_size(True)[0] is False


# ── delete rules (ruling 10) ─────────────────────────────────────────────


def test_delete_active_voice_rejected():
    ok, error = check_delete_allowed("my_voice", "my_voice", PREDEFINED)
    assert not ok
    assert "switch to another voice" in error


def test_delete_predefined_rejected():
    ok, error = check_delete_allowed("alba", None, PREDEFINED)
    assert not ok
    assert "built-in" in error


def test_delete_inactive_custom_voice_allowed():
    assert check_delete_allowed("my_voice", "other_voice", PREDEFINED) == (True, "")


def test_delete_allowed_when_no_active_voice():
    assert check_delete_allowed("my_voice", None, PREDEFINED) == (True, "")


def test_delete_rejects_path_traversal_name():
    """`voice_delete: "../../x"` must never reach `voice_path()`/`unlink()` --
    format-checked here, the one gate both the delete path and `_set_voice`
    funnel a wire-supplied name through."""
    ok, error = check_delete_allowed("../../x", None, PREDEFINED)
    assert not ok
    assert error == "invalid name"


def test_delete_rejects_non_string_name():
    ok, error = check_delete_allowed(123, None, PREDEFINED)
    assert not ok
    assert error == "invalid name"


# ── upload state machine (UploadManager) ────────────────────────────────


def test_happy_path_assembles_chunks_in_order():
    mgr = UploadManager()
    ok, error = mgr.begin("client-1", "my_voice", ".wav", 10)
    assert (ok, error) == (True, "")

    ok, error, name = mgr.chunk("client-1", b"hello ")
    assert (ok, error, name) == (True, "", "my_voice")
    ok, error, name = mgr.chunk("client-1", b"world")
    assert (ok, error, name) == (True, "", "my_voice")

    session, error = mgr.end("client-1")
    assert error == ""
    assert session.name == "my_voice"
    assert session.ext == ".wav"
    assert session.data == b"hello world"
    assert session.finished is True


def test_begin_rejects_invalid_name_without_creating_session():
    mgr = UploadManager()
    ok, error = mgr.begin("client-1", "Bad Name", ".wav", 10)
    assert not ok
    session, error = mgr.end("client-1")
    assert session is None
    assert error == "no upload in progress"


def test_chunk_oversize_clears_session():
    mgr = UploadManager()
    mgr.begin("client-1", "my_voice", ".wav", MAX_UPLOAD_BYTES)
    ok, error, name = mgr.chunk("client-1", b"x" * (MAX_CHUNK_BYTES + 1))
    assert not ok
    assert "byte cap" in error
    assert name == "my_voice"

    # Session was cleared on the failed chunk -- a follow-up chunk is now
    # out-of-order.
    ok, error, name = mgr.chunk("client-1", b"y")
    assert not ok
    assert error == "no upload in progress"
    assert name is None


def test_chunk_without_begin_is_out_of_order():
    mgr = UploadManager()
    ok, error, name = mgr.chunk("client-1", b"stray")
    assert not ok
    assert error == "no upload in progress"
    assert name is None


def test_end_without_begin_is_out_of_order():
    mgr = UploadManager()
    session, error = mgr.end("client-1")
    assert session is None
    assert error == "no upload in progress"


def test_new_begin_aborts_previous_upload():
    mgr = UploadManager()
    mgr.begin("client-1", "voice_one", ".wav", 100)
    mgr.chunk("client-1", b"stale bytes from the aborted upload")

    ok, error = mgr.begin("client-1", "voice_two", ".mp3", 5)
    assert (ok, error) == (True, "")
    mgr.chunk("client-1", b"fresh")

    session, error = mgr.end("client-1")
    assert session.name == "voice_two"
    assert session.ext == ".mp3"
    assert session.data == b"fresh"


def test_abort_clears_in_flight_session():
    mgr = UploadManager()
    mgr.begin("client-1", "my_voice", ".wav", 10)
    mgr.abort("client-1")
    session, error = mgr.end("client-1")
    assert session is None


def test_sessions_are_independent_per_client():
    mgr = UploadManager()
    mgr.begin("client-1", "voice_a", ".wav", 10)
    mgr.begin("client-2", "voice_b", ".mp3", 10)
    mgr.chunk("client-1", b"aaa")
    mgr.chunk("client-2", b"bbb")

    session1, _ = mgr.end("client-1")
    session2, _ = mgr.end("client-2")
    assert session1.name == "voice_a"
    assert session1.data == b"aaa"
    assert session2.name == "voice_b"
    assert session2.data == b"bbb"


def test_chunk_rejects_non_bytes_payload():
    mgr = UploadManager()
    mgr.begin("client-1", "my_voice", ".wav", 10)
    ok, error, name = mgr.chunk("client-1", "not-bytes")
    assert not ok
    assert "invalid chunk payload" in error


# ── normalization (soundfile) ────────────────────────────────────────────


@pytest.mark.skipif(not HAS_SOUNDFILE, reason="soundfile not installed on this box")
def test_normalize_float32_wav_to_16bit_mono():
    import io

    import numpy as np
    import soundfile as sf

    stereo = np.stack(
        [np.linspace(-0.5, 0.5, 4800, dtype="float32"), np.linspace(0.5, -0.5, 4800, dtype="float32")], axis=1
    )
    buf = io.BytesIO()
    sf.write(buf, stereo, 48000, subtype="FLOAT", format="WAV")

    wav_bytes = normalize_to_wav(buf.getvalue(), ".wav")

    data, samplerate = sf.read(io.BytesIO(wav_bytes), dtype="int16", always_2d=False)
    assert samplerate == 48000
    assert data.ndim == 1  # mono
    assert data.dtype.name == "int16"


@pytest.mark.skipif(not HAS_SOUNDFILE, reason="soundfile not installed on this box")
def test_normalize_garbage_raises_voice_clone_error():
    from patches.voice_clone import VoiceCloneError

    with pytest.raises(VoiceCloneError):
        normalize_to_wav(b"not audio data at all", ".wav")


@pytest.mark.skipif(not HAS_SOUNDFILE, reason="soundfile not installed on this box")
def test_normalize_rejects_decoded_duration_over_cap():
    """Decode-bomb guard: `MAX_UPLOAD_BYTES` bounds the compressed upload, but
    a low-entropy/silent recording can still compress a long decoded PCM
    buffer into a small file. `sf.info` bounds decoded duration before the
    full decode. A low sample rate keeps the on-disk file tiny (a few hundred
    KB) despite exceeding the cap in seconds."""
    import io

    import numpy as np
    import soundfile as sf

    from patches.voice_clone import VoiceCloneError

    sr = 200
    samples = np.zeros(sr * (MAX_DECODED_SECONDS + 1), dtype="float32")
    buf = io.BytesIO()
    sf.write(buf, samples, sr, subtype="FLOAT", format="WAV")

    with pytest.raises(VoiceCloneError, match=f"{MAX_DECODED_SECONDS}s cap"):
        normalize_to_wav(buf.getvalue(), ".wav")


@pytest.mark.skipif(not HAS_SOUNDFILE, reason="soundfile not installed on this box")
def test_normalize_allows_decoded_duration_under_cap():
    import io

    import numpy as np
    import soundfile as sf

    sr = 200
    samples = np.zeros(sr * 5, dtype="float32")
    buf = io.BytesIO()
    sf.write(buf, samples, sr, subtype="FLOAT", format="WAV")

    normalize_to_wav(buf.getvalue(), ".wav")  # must not raise


def test_normalize_without_soundfile_raises_friendly_error(monkeypatch):
    import builtins

    from patches.voice_clone import VoiceCloneError

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "soundfile":
            raise ImportError("no module named soundfile")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(VoiceCloneError, match="pip install soundfile"):
        normalize_to_wav(b"irrelevant", ".wav")


# ── audition text (ruling 8) ─────────────────────────────────────────────


def test_audition_text_default_substitutes_name():
    assert resolve_audition_text("alba", raw=None) == "Hi, I'm alba. This is how I sound."


def test_audition_text_env_unset_uses_default(monkeypatch):
    monkeypatch.delenv("VOICE_AUDITION_TEXT", raising=False)
    assert resolve_audition_text("alba") == "Hi, I'm alba. This is how I sound."


def test_audition_text_off_disables():
    assert resolve_audition_text("alba", raw="off") is None
    assert resolve_audition_text("alba", raw="  Off  ") is None
    assert resolve_audition_text("alba", raw="OFF") is None


def test_audition_text_custom_template_substitutes_name():
    assert resolve_audition_text("alba", raw="Hey there, {name} here.") == "Hey there, alba here."


def test_audition_text_blank_falls_back_to_default():
    assert resolve_audition_text("alba", raw="   ") == "Hi, I'm alba. This is how I sound."


def test_audition_text_malformed_template_returned_verbatim():
    # A stray brace in a custom template must not crash the audition path.
    assert resolve_audition_text("alba", raw="broken {") == "broken {"


# ── atomic export (MAJOR fix: no partial .safetensors leak) ─────────────


def test_atomic_export_new_voice_success(tmp_path):
    dest = tmp_path / "my_voice.safetensors"

    def fake_export(state, path):
        path.write_bytes(b"good state bytes")

    atomic_export_state({"k": "v"}, dest, export_fn=fake_export)

    assert dest.read_bytes() == b"good state bytes"
    assert list(tmp_path.glob("*.safetensors")) == [dest]
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_export_failure_on_new_voice_leaves_no_file(tmp_path):
    dest = tmp_path / "my_voice.safetensors"

    def failing_export(state, path):
        path.write_bytes(b"half-written garbage")
        raise RuntimeError("disk full mid-write")

    with pytest.raises(RuntimeError):
        atomic_export_state({"k": "v"}, dest, export_fn=failing_export)

    assert not dest.exists()
    assert list(tmp_path.glob("*.safetensors")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_export_failure_preserves_existing_voice(tmp_path):
    dest = tmp_path / "my_voice.safetensors"
    dest.write_bytes(b"GOOD ORIGINAL STATE")

    def failing_export(state, path):
        path.write_bytes(b"half-written garbage from a failed re-upload")
        raise RuntimeError("build failed mid-write")

    with pytest.raises(RuntimeError):
        atomic_export_state({"k": "v"}, dest, export_fn=failing_export)

    # The pre-existing GOOD voice must survive a failed overwrite untouched.
    assert dest.read_bytes() == b"GOOD ORIGINAL STATE"
    assert list(tmp_path.glob("*.safetensors")) == [dest]
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_export_success_overwrites_existing_voice(tmp_path):
    dest = tmp_path / "my_voice.safetensors"
    dest.write_bytes(b"OLD STATE")

    def fake_export(state, path):
        path.write_bytes(b"NEW STATE")

    atomic_export_state({"k": "v"}, dest, export_fn=fake_export)

    assert dest.read_bytes() == b"NEW STATE"
    assert list(tmp_path.glob("*.safetensors")) == [dest]
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_export_creates_parent_dir(tmp_path):
    dest = tmp_path / "nested" / "voices" / "my_voice.safetensors"

    def fake_export(state, path):
        path.write_bytes(b"state")

    atomic_export_state({"k": "v"}, dest, export_fn=fake_export)

    assert dest.read_bytes() == b"state"
