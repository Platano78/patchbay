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
* **A model that cannot load switches the gate off, visibly.** The model is
  loaded when the gate is enabled (at boot with ``VOICE_WAKE_WORD=1``, when the
  panel toggles it on, when a model is picked), not on the first audio frame. If
  that fails, one warning names the model and the fix, ``available`` goes False
  and ``UNAVAILABLE_REASON`` (read by ``BrainControl`` for the panel) says why;
  the gate is off, not failing open behind a panel that shows it armed. A model
  pick (``set_model``) is loaded before anything changes; a pick that cannot
  load is rejected and leaves the gate exactly as it was. Only a
  failure *after* a successful load (a scoring error mid-stream) still fails
  open, and that is logged but not shown in the panel.
* **But never arm without a detector.** Failing open is for a detector that
  breaks; ``openwakeword`` simply not being installed is knowable up front
  (:attr:`WakewordGate.available`), and arming then would fail open on the
  first frame while the user believes they're wake-gated. So an unavailable
  detector keeps the gate disabled instead: ``VOICE_WAKE_WORD=1`` logs one
  warning at boot, and ``BrainControl`` refuses to enable it.
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

import functools
import importlib.util
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


@functools.lru_cache(maxsize=1)
def _openwakeword_installed() -> bool:
    """find_spec, not an import: the answer is all that's needed here, and
    importing openwakeword pulls in onnxruntime. Cached -- installing it
    takes a pipeline restart anyway (see WakewordGate.UNAVAILABLE_REASON)."""
    try:
        return importlib.util.find_spec("openwakeword") is not None
    except (ImportError, ValueError):
        return False


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

    UNAVAILABLE_REASON = (
        "openwakeword is not installed in the pipeline's venv "
        "(pip install openwakeword onnxruntime, then restart the pipeline)"
    )

    def __init__(self) -> None:
        self.enabled = os.environ.get("VOICE_WAKE_WORD", "").strip().lower() in _TRUTHY
        self._model_arg = os.environ.get("VOICE_WAKE_WORD_MODEL", "hey_jarvis")
        self.threshold = _env_number("VOICE_WAKE_WORD_THRESHOLD", 0.5)
        self._awake = False
        self._model: Any = None
        self._load_failed = False
        # Set when the configured model could not be loaded at probe time (see
        # _probe_locked). Makes `available` False and swaps in a reason that
        # names the model: BrainControl already reads `available` and
        # `UNAVAILABLE_REASON` off the gate for the panel and for refusing to arm.
        self._load_error: str | None = None
        self._buffer = bytearray()
        # Guards _buffer/_model/_load_failed/_awake mutation across feed()
        # (audio-loop thread) vs rearm()/set_model()/reset() (control-message
        # thread). Plain Lock, not RLock -- set_model() calls the unlocked
        # _rearm_locked() helper instead of re-entering rearm() to avoid needing one.
        self._lock = threading.Lock()
        if self.enabled and not self.available:
            logger.warning("VOICE_WAKE_WORD=1 ignored, wake word stays off: %s", self.UNAVAILABLE_REASON)
            self.enabled = False
        elif self.enabled:
            # Load the model now, not on the first audio frame: a model that cannot
            # load must switch the gate off visibly, never fail open behind a panel
            # that says wake-gating is on.
            with self._lock:
                self._probe_locked()

    @property
    def awake(self) -> bool:
        return self._awake

    @property
    def available(self) -> bool:
        """Whether a detector can run at all: ``openwakeword`` installed and the
        configured model loaded when last probed. False means enabling the gate
        would only ever fail open."""
        return _openwakeword_installed() and self._load_error is None

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
        self._load_error = None
        self._awake = False
        if self.enabled:
            self._probe_locked()

    def _probe_locked(self) -> bool:
        """Load the configured model now (caller holds ``self._lock``). On
        failure: log one sentence naming the model and the fix, record the
        reason (``available`` goes False, ``UNAVAILABLE_REASON`` names the
        model) and switch the gate OFF -- visible in the panel -- instead of
        letting it fail open while it still reads as armed. Returns success."""
        if self._model is not None:
            return True
        try:
            self._model = self._load_model()
        except Exception as exc:
            self._model = None
            self._record_load_failure(exc)
            return False
        return True

    def _load_failure_reason(self, exc: Exception) -> str:
        detail = str(exc) or type(exc).__name__
        return (
            f"wake word model {self._model_arg!r} could not be loaded ({detail}); "
            "pick another wake word model in the panel, or set VOICE_WAKE_WORD_MODEL to a bundled "
            "model name or an existing .onnx path and restart the pipeline"
        )

    def _record_load_failure(self, exc: Exception) -> None:
        """Caller holds ``self._lock``. The configured model could not load: say
        so once, make ``available`` False with the reason, and switch the gate
        OFF -- never leave it armed to fail open."""
        self._load_error = str(exc) or type(exc).__name__
        self.UNAVAILABLE_REASON = self._load_failure_reason(exc)
        self.enabled = False
        logger.warning("wake word stays off: %s", self.UNAVAILABLE_REASON)
        logger.debug("wake word model load traceback", exc_info=exc)

    def set_model(self, name_or_path: str) -> tuple[bool, str]:
        """Swap the configured wake-phrase model. ``name_or_path`` must be
        either one of :meth:`available_models` or an existing ``.onnx``/
        ``.tflite`` path on disk (the custom-model seam). The picked model is
        loaded first, whether or not the gate is enabled; only if that works
        does it replace the current model and re-arm the gate. A pick that
        cannot load changes nothing (model, enabled, awake) and returns the
        reason. Returns ``(ok, error)`` like ``BrainControl._set_brain``."""
        if not isinstance(name_or_path, str) or not name_or_path.strip():
            return False, "wake word model name required"
        name_or_path = name_or_path.strip()

        # Re-selecting the ACTIVE model by its display name -- which is exactly what
        # the settings panel sends back, since `models` and `model` are both the
        # stripped form -- keeps `_model_arg` verbatim: for a custom-path model the
        # bare basename is NOT loadable from disk, so adopting it would turn a
        # working custom wake word into a broken one on a pure round trip. It
        # still reloads and rearms, like any explicit pick.
        same = name_or_path == self._strip_model_suffix(self._model_arg)
        if not same:
            is_known = name_or_path in self.available_models()
            is_custom_path = name_or_path.endswith((".onnx", ".tflite")) and os.path.isfile(name_or_path)
            if not (is_known or is_custom_path):
                return False, f"unknown wake word model: {name_or_path}"

        with self._lock:
            # A pick is an explicit choice, so load the picked model now, enabled
            # or not. Nothing about the gate changes until that succeeds: a
            # rejected pick leaves it exactly as it was (model, awake, enabled).
            old_arg = self._model_arg
            if not same:
                self._model_arg = name_or_path
            try:
                candidate = self._load_model()
            except Exception as exc:
                reason = self._load_failure_reason(exc)
                self._model_arg = old_arg
                logger.warning("wake word model pick rejected: %s", reason)
                logger.debug("wake word model load traceback", exc_info=exc)
                return False, reason
            self._model = candidate
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
        """Lazily load the openWakeWord model. Returns False if loading fails:
        an enabled gate switches itself off with the reason (see
        :meth:`_record_load_failure`); a gate that is not enabled fails open."""
        if self._load_failed:
            return False
        if self._model is not None:
            return True
        try:
            self._model = self._load_model()
        except Exception as exc:
            if self.enabled:
                # An armed gate whose model will not load switches OFF with the
                # reason shown, rather than failing open behind a panel that says
                # it is waiting for the wake phrase.
                self._record_load_failure(exc)
                return False
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
            # openwakeword <0.5: `wakeword_model_paths`, always onnx internally,
            # and it only takes file paths -- a bare name like "hey_jarvis" has to
            # be resolved to the wheel's bundled file first.
            return Model(wakeword_model_paths=[self._resolve_bundled_path(self._model_arg)])

    @classmethod
    def _resolve_bundled_path(cls, name_or_path: str) -> str:
        """openwakeword <0.5 only loads files. A path (or anything that exists on
        disk) passes through unchanged; a bare name is matched against the
        pretrained models the installed package ships (``hey_jarvis`` ->
        ``.../resources/models/hey_jarvis_v0.1.onnx``). Raises ``FileNotFoundError``
        naming the models that do exist when nothing matches."""
        if os.path.isfile(name_or_path) or name_or_path.endswith((".onnx", ".tflite")) or os.sep in name_or_path:
            return name_or_path
        import openwakeword

        models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
        candidates = [os.path.join(models_dir, f) for f in sorted(os.listdir(models_dir)) if f.endswith(".onnx")]
        for path in candidates:
            stem = cls._strip_model_suffix(path)
            if stem == name_or_path and stem not in _NON_WAKE_MODEL_NAMES:
                return path
        known = sorted(
            {cls._strip_model_suffix(p) for p in candidates} - _NON_WAKE_MODEL_NAMES
        )
        raise FileNotFoundError(f"no bundled wake word model named {name_or_path!r} (installed: {', '.join(known)})")
