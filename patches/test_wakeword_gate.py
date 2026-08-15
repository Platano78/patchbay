"""Unit tests for wakeword_gate.py (ShayneP-borrow join-deaf gate).

Run from repo root: python3 -m pytest patches/test_wakeword_gate.py -v

No ``openwakeword`` install required for the fake-scorer tests below: the
module under test only imports ``openwakeword`` lazily inside
``WakewordGate._load_model``, which every test here monkeypatches. The one
real-model smoke test is gated behind ``pytest.importorskip("openwakeword")``
and skips cleanly when the dependency isn't installed.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types

import pytest

from patches import wakeword_gate as wg


class FakeModel:
    """Records every frame it's asked to score."""

    def __init__(self, score=0.0):
        self.score = score
        self.frames = []
        self.reset_calls = 0

    def predict(self, frame):
        self.frames.append(frame)
        return {"fake_model": self.score}

    def reset(self):
        self.reset_calls += 1


def _silence(num_samples: int) -> bytes:
    return b"\x00\x00" * num_samples


# ── (a) disabled by default ────────────────────────────────────────────


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VOICE_WAKE_WORD", raising=False)
    gate = wg.WakewordGate()
    assert gate.enabled is False


# ── (b) frame accumulation across misaligned boundaries ────────────────


def test_frames_accumulate_across_boundaries(monkeypatch):
    gate = wg.WakewordGate()
    fake = FakeModel(score=0.0)
    monkeypatch.setattr(gate, "_load_model", lambda: fake)

    # 4 pushes of 512 samples (1024 bytes) each = 2048 samples total.
    # That's exactly 1 complete 1280-sample frame plus a 768-sample remainder
    # that must survive across feed() calls.
    for _ in range(4):
        gate.feed(_silence(512))

    assert len(fake.frames) == 1
    assert len(fake.frames[0]) == wg._FRAME_SAMPLES
    assert fake.frames[0].dtype == wg.np.int16
    assert len(gate._buffer) == (2048 - wg._FRAME_SAMPLES) * 2


# ── (c) threshold crossing wakes + reports score ────────────────────────


def test_threshold_crossing_wakes(monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.5")
    gate = wg.WakewordGate()
    fake = FakeModel(score=0.9)
    monkeypatch.setattr(gate, "_load_model", lambda: fake)

    score = gate.feed(_silence(wg._FRAME_SAMPLES))

    assert gate.awake is True
    assert score == pytest.approx(0.9)


# ── (d) sub-threshold scores never wake ──────────────────────────────────


def test_subthreshold_never_wakes(monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.5")
    gate = wg.WakewordGate()
    fake = FakeModel(score=0.3)
    monkeypatch.setattr(gate, "_load_model", lambda: fake)

    score = gate.feed(_silence(wg._FRAME_SAMPLES))

    assert gate.awake is False
    assert score == pytest.approx(0.3)


# ── VOICE_WAKE_WORD_THRESHOLD parsing -> fail open, never crash ──────────


def test_unparseable_threshold_falls_back_to_default(monkeypatch):
    """A hand-edited systemd drop-in typo (e.g. a stray trailing dot) must
    never raise out of WakewordGate.__init__ -- it's constructed unguarded
    in WebSocketStreamer.__init__, so a ValueError here takes the whole
    voice agent down at startup instead of leaving it deaf-until-armed."""
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.5.")
    gate = wg.WakewordGate()
    assert gate.threshold == 0.5


def test_valid_threshold_is_honoured_exactly(monkeypatch):
    """Proves the fail-open fallback doesn't swallow real, well-formed
    config -- a valid threshold must reach `.threshold` unchanged."""
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.72")
    gate = wg.WakewordGate()
    assert gate.threshold == pytest.approx(0.72)


def test_unset_threshold_defaults_to_point_five(monkeypatch):
    monkeypatch.delenv("VOICE_WAKE_WORD_THRESHOLD", raising=False)
    gate = wg.WakewordGate()
    assert gate.threshold == 0.5


# ── (e) loader exception -> fail open ────────────────────────────────────


def test_loader_exception_fails_open(monkeypatch):
    gate = wg.WakewordGate()

    def boom():
        raise RuntimeError("no model file")

    monkeypatch.setattr(gate, "_load_model", boom)

    score = gate.feed(_silence(wg._FRAME_SAMPLES))  # must not raise

    assert gate.awake is True
    assert score is None


# ── (f) predict exception mid-stream -> fail open ────────────────────────


def test_predict_exception_fails_open(monkeypatch):
    gate = wg.WakewordGate()

    class ExplodingModel:
        def predict(self, frame):
            raise RuntimeError("onnxruntime blew up")

    monkeypatch.setattr(gate, "_load_model", lambda: ExplodingModel())

    gate.feed(_silence(wg._FRAME_SAMPLES))  # must not raise

    assert gate.awake is True


# ── near-miss visibility: scores >= 0.25 log at INFO unless they cross ──
# ── the detection threshold (which already logs at INFO on its own) ─────


@pytest.mark.parametrize(
    ("score", "expect_near_miss", "expect_detected"),
    [
        (0.3, True, False),
        (0.1, False, False),
        (0.9, False, True),
    ],
)
def test_near_miss_logging(monkeypatch, caplog, score, expect_near_miss, expect_detected):
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.5")
    gate = wg.WakewordGate()
    fake = FakeModel(score=score)
    monkeypatch.setattr(gate, "_load_model", lambda: fake)

    with caplog.at_level("INFO", logger=wg.logger.name):
        gate.feed(_silence(wg._FRAME_SAMPLES))

    near_miss_logged = any("near miss" in r.message for r in caplog.records)
    detected_logged = any("detected" in r.message for r in caplog.records)
    assert near_miss_logged is expect_near_miss
    assert detected_logged is expect_detected


# ── (g) reset() re-arms and clears the buffer ────────────────────────────


def test_reset_rearms_and_clears_buffer(monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.5")
    gate = wg.WakewordGate()
    fake = FakeModel(score=0.9)
    monkeypatch.setattr(gate, "_load_model", lambda: fake)

    gate.feed(_silence(wg._FRAME_SAMPLES))
    assert gate.awake is True
    gate.feed(_silence(256))  # leaves a remainder in the buffer

    gate.reset()

    assert gate.awake is False
    assert len(gate._buffer) == 0
    assert fake.reset_calls == 1
    # model stays loaded -- no reload on the next feed()
    assert gate._model is fake


# ── reset() must not undo fail-open across sessions ──────────────────────


def test_reset_after_loader_failure_stays_awake(monkeypatch):
    gate = wg.WakewordGate()

    def boom():
        raise RuntimeError("no model file")

    monkeypatch.setattr(gate, "_load_model", boom)

    gate.feed(_silence(wg._FRAME_SAMPLES))  # fails open
    assert gate.awake is True

    gate.reset()

    assert gate.awake is True  # a broken detector stays failed-open, session or not


def test_reset_after_predict_failure_stays_awake(monkeypatch):
    gate = wg.WakewordGate()

    class ExplodingModel:
        def predict(self, frame):
            raise RuntimeError("onnxruntime blew up")

    monkeypatch.setattr(gate, "_load_model", lambda: ExplodingModel())

    gate.feed(_silence(wg._FRAME_SAMPLES))  # fails open mid-stream
    assert gate.awake is True

    gate.reset()

    assert gate.awake is True
    score = gate.feed(_silence(wg._FRAME_SAMPLES))  # must not raise, and not re-arm deafness
    assert gate.awake is True
    assert score is None


# ── phrase derivation ────────────────────────────────────────────────────


def test_phrase_default():
    gate = wg.WakewordGate()
    assert gate.phrase == "hey jarvis"


def test_phrase_from_custom_path(monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL", "/opt/models/my_word_v1.2.onnx")
    gate = wg.WakewordGate()
    assert gate.phrase == "my word"


# ── rearm() -- deliberate user re-arm (vs reset()'s auto re-arm) ─────────


def test_rearm_after_loader_failure_retries_and_can_refail(monkeypatch):
    gate = wg.WakewordGate()

    def boom():
        raise RuntimeError("no model file")

    monkeypatch.setattr(gate, "_load_model", boom)
    gate.feed(_silence(wg._FRAME_SAMPLES))  # fails open
    assert gate.awake is True
    assert gate._load_failed is True

    gate.rearm()

    assert gate._load_failed is False  # unlike reset(), rearm() clears the latch
    assert gate.awake is False
    assert len(gate._buffer) == 0

    # A second consecutive failure fails open again -- one retry, not infinite.
    gate.feed(_silence(wg._FRAME_SAMPLES))
    assert gate.awake is True
    assert gate._load_failed is True


def test_rearm_after_loader_failure_retries_and_succeeds(monkeypatch):
    gate = wg.WakewordGate()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("no model file")
        return FakeModel(score=0.0)

    monkeypatch.setattr(gate, "_load_model", flaky)
    gate.feed(_silence(wg._FRAME_SAMPLES))  # fails open on the first attempt
    assert gate.awake is True

    gate.rearm()
    gate.feed(_silence(wg._FRAME_SAMPLES))  # retries the loader, succeeds this time

    assert gate.awake is False
    assert calls["n"] == 2


# ── set_model() ────────────────────────────────────────────────────────


def test_set_model_valid_name_swaps_and_rearms(monkeypatch):
    gate = wg.WakewordGate()
    monkeypatch.setattr(gate, "available_models", lambda: ["hey_jarvis", "alexa"])
    fake = FakeModel(score=0.9)
    monkeypatch.setattr(gate, "_load_model", lambda: fake)
    gate.feed(_silence(wg._FRAME_SAMPLES))
    assert gate.awake is True

    ok, err = gate.set_model("alexa")

    assert ok is True
    assert err == ""
    assert gate._model_arg == "alexa"
    assert gate._model is None  # dropped -- lazy-reloads on the next feed()
    assert gate.awake is False  # rearm() fired


def test_set_model_garbage_name_rejected_and_unchanged(monkeypatch):
    gate = wg.WakewordGate()
    monkeypatch.setattr(gate, "available_models", lambda: ["hey_jarvis"])
    original_arg = gate._model_arg

    ok, err = gate.set_model("totally_not_a_model")

    assert ok is False
    assert err
    assert gate._model_arg == original_arg


# ── model_name / set_model round trip (raw vs stripped reconciliation) ──


def test_model_name_is_the_stripped_form_for_a_custom_path(monkeypatch):
    """`model_name` is what the settings panel compares against `models`, so it
    must be stripped even when VOICE_WAKE_WORD_MODEL is a full .onnx path."""
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL", "/opt/models/my_wake_v1.0.onnx")
    gate = wg.WakewordGate()

    assert gate.model_name == "my_wake"
    assert gate._model_arg == "/opt/models/my_wake_v1.0.onnx"


def test_model_name_appears_in_available_models(monkeypatch, tmp_path):
    """The dropdown can only mark an active entry if the reported model is one of
    the offered ones. available_models() always includes the current model."""
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL", str(tmp_path / "my_wake_v1.0.onnx"))
    gate = wg.WakewordGate()

    assert gate.model_name in gate.available_models()


def test_set_model_with_own_display_name_preserves_custom_path(monkeypatch, tmp_path):
    """The round trip that used to break a custom model: the panel reports the
    stripped name, the user re-selects it, and the bare basename gets stored --
    leaving a name openWakeWord cannot resolve from disk. It must stay a path."""
    custom = tmp_path / "my_wake_v1.0.onnx"
    custom.write_bytes(b"")
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL", str(custom))
    gate = wg.WakewordGate()
    monkeypatch.setattr(gate, "available_models", lambda: ["hey_jarvis", "my_wake"])

    ok, err = gate.set_model(gate.model_name)

    assert ok is True
    assert err == ""
    assert gate._model_arg == str(custom)  # NOT "my_wake"
    assert gate.model_name == "my_wake"


def test_set_model_with_own_display_name_still_reloads_and_rearms(monkeypatch, tmp_path):
    """Preserving the path must not cost the explicit-user-action semantics: the
    model is still dropped for a lazy reload and the gate still goes back to sleep."""
    custom = tmp_path / "my_wake_v1.0.onnx"
    custom.write_bytes(b"")
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL", str(custom))
    gate = wg.WakewordGate()
    monkeypatch.setattr(gate, "available_models", lambda: ["my_wake"])
    monkeypatch.setattr(gate, "_load_model", lambda: FakeModel(score=0.9))
    gate.feed(_silence(wg._FRAME_SAMPLES))
    assert gate.awake is True

    ok, _ = gate.set_model("my_wake")

    assert ok is True
    assert gate._model is None
    assert gate.awake is False


def test_set_model_to_a_different_known_name_still_swaps(monkeypatch, tmp_path):
    """The identity shortcut must not swallow a real change away from a custom path."""
    custom = tmp_path / "my_wake_v1.0.onnx"
    custom.write_bytes(b"")
    monkeypatch.setenv("VOICE_WAKE_WORD_MODEL", str(custom))
    gate = wg.WakewordGate()
    monkeypatch.setattr(gate, "available_models", lambda: ["hey_jarvis", "my_wake"])

    ok, err = gate.set_model("hey_jarvis")

    assert ok is True
    assert err == ""
    assert gate._model_arg == "hey_jarvis"


# ── available_models() ────────────────────────────────────────────────


def _fake_openwakeword_module(tmp_path):
    """Minimal fake `openwakeword` module (just a `__file__`) so
    available_models() resolves a models dir under `tmp_path` without
    requiring the real dependency to be installed."""
    pkg_dir = tmp_path / "openwakeword"
    pkg_dir.mkdir()
    fake_mod = types.ModuleType("openwakeword")
    fake_mod.__file__ = str(pkg_dir / "__init__.py")
    return fake_mod


def test_available_models_excludes_non_wake_files_and_includes_current(monkeypatch, tmp_path):
    gate = wg.WakewordGate()
    monkeypatch.setitem(sys.modules, "openwakeword", _fake_openwakeword_module(tmp_path))
    monkeypatch.setattr(
        wg.os,
        "listdir",
        lambda d: [
            "hey_jarvis_v0.1.onnx",
            "alexa_v0.1.onnx",
            "melspectrogram.onnx",
            "embedding_model.onnx",
            "silero_vad.onnx",
            "timer_v0.1.onnx",
            "weather_v0.1.onnx",
        ],
    )

    models = gate.available_models()

    assert models == ["alexa", "hey_jarvis"]  # sorted; non-wake-phrase files excluded


def test_available_models_always_includes_current_even_if_absent(monkeypatch, tmp_path):
    gate = wg.WakewordGate()
    monkeypatch.setitem(sys.modules, "openwakeword", _fake_openwakeword_module(tmp_path))
    monkeypatch.setattr(wg.os, "listdir", lambda d: ["alexa_v0.1.onnx"])

    models = gate.available_models()

    assert "hey_jarvis" in models  # default VOICE_WAKE_WORD_MODEL, not on disk here
    assert "alexa" in models


def test_available_models_falls_back_to_current_on_any_exception(monkeypatch, tmp_path):
    gate = wg.WakewordGate()
    monkeypatch.setitem(sys.modules, "openwakeword", _fake_openwakeword_module(tmp_path))

    def boom(_d):
        raise OSError("models dir vanished")

    monkeypatch.setattr(wg.os, "listdir", boom)

    models = gate.available_models()

    assert models == [gate._strip_model_suffix(gate._model_arg)]


# ── state() ────────────────────────────────────────────────────────────


def test_state_off_when_disabled(monkeypatch):
    monkeypatch.delenv("VOICE_WAKE_WORD", raising=False)
    gate = wg.WakewordGate()
    assert gate.state() == "off"


def test_state_asleep_when_enabled_not_awake(monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD", "1")
    gate = wg.WakewordGate()
    assert gate.state() == "asleep"


def test_state_awake_when_enabled_and_awake(monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD", "1")
    gate = wg.WakewordGate()
    gate._awake = True
    assert gate.state() == "awake"


# ── _lock: feed() vs rearm()/set_model() from another thread ─────────────


class SlowFakeModel(FakeModel):
    """Same as FakeModel, but predict() sleeps briefly so the feeder thread
    holds `_lock` long enough for the control thread to contend on it."""

    def predict(self, frame):
        time.sleep(0.001)
        return super().predict(frame)


def test_feed_concurrent_with_rearm_never_raises(monkeypatch):
    monkeypatch.setenv("VOICE_WAKE_WORD_THRESHOLD", "0.9")  # noise never crosses it
    gate = wg.WakewordGate()
    monkeypatch.setattr(gate, "available_models", lambda: ["hey_jarvis"])
    monkeypatch.setattr(gate, "_load_model", lambda: SlowFakeModel(score=0.1))

    errors: list[BaseException] = []

    def feeder():
        try:
            for _ in range(200):
                gate.feed(_silence(wg._FRAME_SAMPLES))
        except BaseException as exc:  # noqa: BLE001 - must observe any exception from the thread
            errors.append(exc)

    feeder_thread = threading.Thread(target=feeder)
    feeder_thread.start()
    for i in range(50):
        if i % 2 == 0:
            gate.rearm()
        else:
            gate.set_model("hey_jarvis")
    feeder_thread.join(timeout=10)

    assert not feeder_thread.is_alive()
    assert errors == []
    assert gate.awake in (True, False)
    gate.rearm()  # deterministic final state regardless of interleaving
    assert len(gate._buffer) < wg._FRAME_BYTES


# ── real openwakeword smoke test (skips without the dependency) ─────────


def test_real_openwakeword_smoke():
    pytest.importorskip("openwakeword")
    os.environ["VOICE_WAKE_WORD"] = "1"
    try:
        gate = wg.WakewordGate()
        assert gate.enabled is True

        rng_bytes = os.urandom(16000 * 2 * 2)  # 2s of noise, int16 mono 16kHz
        for i in range(0, len(rng_bytes), 1024):
            gate.feed(rng_bytes[i : i + 1024])

        assert gate.awake is False
    finally:
        os.environ.pop("VOICE_WAKE_WORD", None)
