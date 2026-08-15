"""Wakeword join-deaf gate (ShayneP-borrow, see
knowledge-base/research-notes/shaynep-local-voice-ai-2026-07-12.md).

While armed, the pipeline stays deaf: incoming mic audio is scored against a
cheap openWakeWord ONNX model instead of being pushed onto ``input_queue``.
Only once the wake phrase is detected does audio start flowing to VAD/ASR.
Deploy-safe: disabled unless ``VOICE_WAKE_WORD=1`` (see
``websocket_streamer.WebSocketStreamer``), so a plain restart leaves
behaviour unchanged.

Design:

* **Lazy, dependency-free import.** ``openwakeword`` is only imported inside
  :meth:`WakewordGate._load_model`, on first use -- this module (and its
  tests) can be imported with neither ``speech_to_speech`` nor
  ``openwakeword`` installed.
* **Fail open.** Any exception while loading the model or scoring a frame is
  logged once and treated as an immediate, permanent wake for the rest of
  the session -- a broken detector must never brick the assistant.
* **Calibration affordance.** Any score above 0.05 is logged at DEBUG so the
  threshold can be tuned against a real microphone. A score at or above 0.25
  that doesn't cross the (higher) detection threshold is also logged at INFO
  as a near miss -- visible at the live log level, unlike the DEBUG line, so
  a real attempt that fell short leaves journal evidence.
* **Arbitrary frame boundaries.** ``feed()`` accumulates raw bytes into an
  internal buffer and only scores complete 1280-sample (80ms) frames --
  WebSocket frames arrive as multiples of 512 samples, not 1280, so a
  remainder must survive across calls.
* **Cross-thread safe.** ``feed()`` runs on the audio-loop thread while
  ``rearm()``/``set_model()``/``reset()`` can run on a control-message
  thread (``BrainControl.handle`` via ``asyncio.to_thread``) or on
  streamer-disconnect. All four share ``_lock`` around their mutable-state
  bodies; ``state()``, ``phrase``, ``available_models()``, and the
  ``awake``/``enabled`` reads stay lock-free (plain attribute reads, GIL-atomic).
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}

_FRAME_SAMPLES = 1280  # 80ms @ 16kHz, openWakeWord's native hop size
_FRAME_BYTES = _FRAME_SAMPLES * 2  # int16 mono
_SCORE_LOG_FLOOR = 0.05  # calibration affordance: log anything above the noise floor
_NEAR_MISS_FLOOR = 0.25  # visibility affordance: log a near-miss at INFO (not just DEBUG)

# openWakeWord ships non-wake-phrase files (feature extractors, VAD, timer/weather
# demo models) alongside the pretrained wake-phrase models in the same directory --
# these are never valid `VOICE_WAKE_WORD_MODEL` choices, so available_models() drops
# them from the settings-panel dropdown.
_NON_WAKE_MODEL_NAMES = {"melspectrogram", "embedding_model", "silero_vad", "timer", "weather"}


def _env_number(name: str, default: float) -> float:
    """Read a numeric env var, falling back to `default` on anything
    unparseable rather than raising -- same fail-open contract as
    echo_gate._env_number / brain_control._env_seconds: ``WakewordGate()`` is
    constructed unguarded in ``WebSocketStreamer.__init__``, so a typo in a
    hand-edited systemd drop-in must never crash the assistant at startup."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using the default %r", name, raw, default)
        return default


class WakewordGate:
    """Scores raw int16 PCM against an openWakeWord model until the
    configured wake phrase is heard, then stays awake for the rest of the
    session."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("VOICE_WAKE_WORD", "").strip().lower() in _TRUTHY
        self._model_arg = os.environ.get("VOICE_WAKE_WORD_MODEL", "hey_jarvis")
        self.threshold = _env_number("VOICE_WAKE_WORD_THRESHOLD", 0.5)
        self._awake = False
        self._model: Any = None
        self._load_failed = False
        self._buffer = bytearray()
        # Guards _buffer/_model/_load_failed/_awake mutation across feed()
        # (audio-loop thread) vs rearm()/set_model()/reset() (control-message
        # thread). Plain Lock, not RLock -- set_model() calls the unlocked
        # _rearm_locked() helper instead of re-entering rearm() to avoid needing one.
        self._lock = threading.Lock()

    @property
    def awake(self) -> bool:
        return self._awake

    @property
    def phrase(self) -> str:
        """Human-readable wake phrase derived from the model name/path, e.g.
        ``"hey_jarvis"`` -> ``"hey jarvis"``."""
        return self._strip_model_suffix(self._model_arg).replace("_", " ")

    @property
    def model_name(self) -> str:
        """Display/selection key for the configured model: the same stripped
        form :meth:`available_models` returns, so the settings-panel dropdown
        can match the active entry by plain equality. The raw
        ``VOICE_WAKE_WORD_MODEL`` value (which may be a full ``.onnx`` path)
        stays internal -- selecting this name back via :meth:`set_model` is
        recognised as the current model and preserves it verbatim."""
        return self._strip_model_suffix(self._model_arg)

    @staticmethod
    def _strip_model_suffix(name_or_path: str) -> str:
        """``"/opt/models/hey_jarvis_v0.1.onnx"`` -> ``"hey_jarvis"``: drop the
        directory, the ``.onnx``/``.tflite`` extension, and a pretrained
        model's ``_vX.Y`` version suffix. Shared by :attr:`phrase` and
        :meth:`available_models`."""
        base = os.path.basename(name_or_path)
        base = re.sub(r"\.(onnx|tflite)$", "", base)
        base = re.sub(r"_v[\d.]+$", "", base)
        return base

    def state(self) -> str:
        """Coarse status for the settings panel: ``"off"`` (not enabled),
        ``"awake"``, or ``"asleep"``."""
        if not self.enabled:
            return "off"
        return "awake" if self._awake else "asleep"

    def rearm(self) -> None:
        """Deliberate user re-arm -- toggling wake word on, or picking a new
        model -- as opposed to :meth:`reset`'s automatic per-session re-arm.

        Clears the buffer, goes back to sleep, AND gives a previously-broken
        scorer one fresh load attempt: unlike :meth:`reset`, which
        deliberately preserves ``_load_failed`` so a detector that already
        proved unusable doesn't go deaf again on the next session,
        ``rearm()`` resets that latch because the user just took an explicit
        action implying they want detection to actually run again. If the
        retry fails again, the gate fails open again on the very next
        :meth:`feed`."""
        with self._lock:
            self._rearm_locked()

    def _rearm_locked(self) -> None:
        """Body of :meth:`rearm`, callable while ``self._lock`` is already
        held (by :meth:`set_model`) so callers never re-enter the lock."""
        self._buffer.clear()
        self._load_failed = False
        self._awake = False

    def set_model(self, name_or_path: str) -> tuple[bool, str]:
        """Swap the configured wake-phrase model. ``name_or_path`` must be
        either one of :meth:`available_models` or an existing ``.onnx``/
        ``.tflite`` path on disk (the custom-model seam). On success, drops
        the loaded model (so the new one lazy-loads on the next
        :meth:`feed`) and calls :meth:`rearm`. Returns ``(ok, error)`` like
        ``BrainControl._set_brain``."""
        if not isinstance(name_or_path, str) or not name_or_path.strip():
            return False, "wake word model name required"
        name_or_path = name_or_path.strip()

        # Re-selecting the ACTIVE model by its display name -- which is exactly what
        # the settings panel sends back, since `models` and `model` are both the
        # stripped form. Handled before the is_known check below because
        # available_models() always contains the current model's stripped name, so
        # that check would accept it and clobber _model_arg: for a custom-path model
        # the bare basename is NOT loadable from disk, turning a working custom wake
        # word into a broken one on a pure round trip. Still reloads + rearms, so the
        # explicit-user-action semantics of a normal set_model are unchanged.
        if name_or_path == self._strip_model_suffix(self._model_arg):
            with self._lock:
                self._model = None
                self._rearm_locked()
            return True, ""

        is_known = name_or_path in self.available_models()
        is_custom_path = name_or_path.endswith((".onnx", ".tflite")) and os.path.isfile(name_or_path)
        if not (is_known or is_custom_path):
            return False, f"unknown wake word model: {name_or_path}"

        with self._lock:
            self._model_arg = name_or_path
            self._model = None
            self._rearm_locked()
        return True, ""

    def available_models(self) -> list[str]:
        """Enumerate wake-phrase model files in openWakeWord's models
        directory -- the seam a custom-trained ``.onnx`` dropped in there
        uses to auto-appear in the settings-panel dropdown. Excludes
        non-wake-phrase files (feature extractors, VAD, timer/weather demo
        models). Always includes the currently configured model, even if it
        isn't found on disk (e.g. a custom path). Sorted; on any failure
        (``openwakeword`` not installed, directory missing, ...) falls back
        to just the current model."""
        current = self._strip_model_suffix(self._model_arg)
        try:
            import openwakeword

            models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
            names = {current}
            for fname in os.listdir(models_dir):
                if not fname.endswith((".onnx", ".tflite")):
                    continue
                name = self._strip_model_suffix(fname)
                if name in _NON_WAKE_MODEL_NAMES:
                    continue
                names.add(name)
            return sorted(names)
        except Exception:
            return [current]

    def reset(self) -> None:
        """Re-arm for a new session: clear the buffer, go back to sleep, but
        keep the loaded model (and its own streaming state, if any)."""
        with self._lock:
            self._buffer.clear()
            # A broken detector must stay failed-open across sessions too -- don't
            # let a new session go deaf again on a model that already proved unusable.
            self._awake = self._load_failed
            if self._model is not None:
                reset_fn = getattr(self._model, "reset", None)
                if callable(reset_fn):
                    try:
                        reset_fn()
                    except Exception:
                        logger.debug("wakeword model reset() failed", exc_info=True)

    def feed(self, data: bytes) -> float | None:
        """Feed raw int16 PCM bytes; score every complete 1280-sample frame
        accumulated across this and prior calls. Returns the max score seen
        in this call, or ``None`` if no complete frame was consumed."""
        with self._lock:
            if not self._ensure_model():
                return None

            self._buffer.extend(data)
            max_score: float | None = None
            while len(self._buffer) >= _FRAME_BYTES:
                frame_bytes = bytes(self._buffer[:_FRAME_BYTES])
                del self._buffer[:_FRAME_BYTES]
                frame = np.frombuffer(frame_bytes, dtype=np.int16)

                try:
                    predictions = self._model.predict(frame)
                except Exception:
                    logger.exception("wakeword predict failed; failing open for the rest of the session")
                    self._model = None
                    self._load_failed = True
                    self._awake = True
                    return max_score

                score = float(max(predictions.values())) if predictions else 0.0
                if max_score is None or score > max_score:
                    max_score = score
                if score > _SCORE_LOG_FLOOR:
                    logger.debug("wake word score: %.3f", score)
                if not self._awake and score >= self.threshold:
                    self._awake = True
                    logger.info("wake word %r detected (score=%.3f)", self.phrase, score)
                elif not self._awake and score >= _NEAR_MISS_FLOOR:
                    logger.info("wake word near miss (score=%.3f)", score)

            return max_score

    def _ensure_model(self) -> bool:
        """Lazily load the openWakeWord model. Returns False (and fails
        open, permanently, for the session) if loading ever fails."""
        if self._load_failed:
            return False
        if self._model is not None:
            return True
        try:
            self._model = self._load_model()
        except Exception:
            logger.exception("wakeword model load failed; failing open for the rest of the session")
            self._load_failed = True
            self._awake = True
            return False
        return True

    def _load_model(self) -> Any:
        """Construct the openWakeWord ``Model``. Not wrapped in try/except
        itself -- :meth:`_ensure_model` is the single fail-open seam so it
        can be monkeypatched directly in tests."""
        from openwakeword import Model

        try:
            # openwakeword >=0.5: `wakeword_models` + explicit inference framework
            # (defaults to tflite, which we don't ship a runtime for).
            return Model(wakeword_models=[self._model_arg], inference_framework="onnx")
        except TypeError:
            # openwakeword <0.5: `wakeword_model_paths`, always onnx internally.
            return Model(wakeword_model_paths=[self._model_arg])
