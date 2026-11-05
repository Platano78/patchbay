"""Smart Turn end-of-turn classifier client.

Asks the speech server's ``smart-turn`` model whether the user's turn so far
sounds finished (``POST {base}/v1/tasks/run``). Used by ``VAD/vad_handler.py``
when the VAD thinks an open-mic turn just ended.

``VOICE_SMART_TURN`` = ``off`` (default) | ``shadow`` | ``act``
  shadow: classify in a background thread after the turn has already been
          emitted and log ``SMART_TURN_SHADOW``; never changes behaviour.
  act:    classify before emitting; if the turn sounds unfinished, keep
          listening up to ``VOICE_SMART_TURN_HOLD_MS`` (default 1200) for the
          user to continue.
``VOICE_SMART_TURN_THRESHOLD`` (default 0.5) is sent to the server as the
``threshold`` option.

Fail-open: any error, timeout or odd answer is "unknown", which behaves exactly
like having no classifier. Push-to-talk turns are never classified (the release
is the user's explicit end): ``websocket_streamer`` reports PTT down/up through
``note_ptt`` and the VAD asks ``ptt_owns_turn``.

Stdlib + numpy (+ scipy for a non-16 kHz pipeline); no ``speech_to_speech`` imports.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
import urllib.request
import wave
from math import gcd
from typing import Callable, NamedTuple

import numpy as np

try:  # imported up front: a first-use import would eat the 250 ms budget
    from scipy.signal import resample_poly
except ImportError:  # pragma: no cover - only a non-16 kHz pipeline needs it
    resample_poly = None

logger = logging.getLogger(__name__)

MODEL_RATE = 16000
MAX_TURN_S = 8.0
TIMEOUT_S = 0.25
DEFAULT_THRESHOLD = 0.5
DEFAULT_HOLD_MS = 1200
PTT_MAX_AGE_S = 30.0  # a 'down' older than this is treated as released (websocket_streamer sets it to PTT_HOLD_MAX_S)
PTT_GRACE_S = 5.0  # a turn that ends within this long after a PTT release is the PTT turn


class Verdict(NamedTuple):
    complete: bool | None  # None = unknown (error, timeout, bad answer)
    probability: float | None
    latency_ms: int


UNKNOWN = Verdict(None, None, 0)


def mode_from_env() -> str:
    raw = os.environ.get("VOICE_SMART_TURN", "").strip().lower()
    if raw in ("", "off", "0", "false", "no"):
        return "off"
    if raw in ("shadow", "act"):
        return raw
    logger.warning("VOICE_SMART_TURN=%r is not off|shadow|act; treating as off", raw)
    return "off"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def threshold_from_env() -> float:
    return _float_env("VOICE_SMART_TURN_THRESHOLD", DEFAULT_THRESHOLD)


def hold_ms_from_env() -> int:
    return max(0, int(_float_env("VOICE_SMART_TURN_HOLD_MS", DEFAULT_HOLD_MS)))


# -- PTT marker -----------------------------------------------------------
class _PttMarker:
    def __init__(self) -> None:
        self.down_at: float | None = None
        self.up_at: float = float("-inf")

    def note(self, state: str, now: float) -> None:
        if state == "down":
            self.down_at = now
        elif state == "up":
            self.down_at = None
            self.up_at = now

    def owns_turn(self, now: float) -> bool:
        if self.down_at is not None and now - self.down_at < PTT_MAX_AGE_S:
            return True
        return now - self.up_at < PTT_GRACE_S


_PTT = _PttMarker()


def note_ptt(state: str, now: float | None = None) -> None:
    _PTT.note(state, time.monotonic() if now is None else now)


def ptt_owns_turn(now: float | None = None) -> bool:
    return _PTT.owns_turn(time.monotonic() if now is None else now)


def reset_ptt() -> None:
    _PTT.down_at = None
    _PTT.up_at = float("-inf")


# -- client ---------------------------------------------------------------
def _to_wav_16k(audio: np.ndarray, sample_rate: int) -> bytes:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)[-int(MAX_TURN_S * sample_rate) :]
    if sample_rate != MODEL_RATE:
        g = gcd(sample_rate, MODEL_RATE)
        x = resample_poly(x, MODEL_RATE // g, sample_rate // g).astype(np.float32)
    x = x[-int(MAX_TURN_S * MODEL_RATE) :]
    pcm = np.clip(x * 32768.0, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MODEL_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _http_post(url: str, body: bytes, timeout_s: float) -> bytes:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (operator-configured URL)
        return resp.read()


class SmartTurn:
    def __init__(
        self,
        base_url: str,
        mode: str,
        threshold: float = DEFAULT_THRESHOLD,
        hold_ms: int = DEFAULT_HOLD_MS,
        timeout_s: float = TIMEOUT_S,
        post: Callable[[str, bytes, float], bytes] = _http_post,
    ) -> None:
        self.url = base_url.rstrip("/") + "/v1/tasks/run"
        self.mode = mode
        self.threshold = threshold
        self.hold_ms = hold_ms
        self.timeout_s = timeout_s
        self._post = post

    def _request(self, audio: np.ndarray, sample_rate: int) -> Verdict:
        started = time.monotonic()
        body = json.dumps(
            {
                "model": "smart-turn",
                "request": {
                    "audio_base64": base64.b64encode(_to_wav_16k(audio, sample_rate)).decode("ascii"),
                    "options": {"threshold": str(self.threshold)},
                },
            }
        ).encode()
        raw = self._post(self.url, body, self.timeout_s)
        data = json.loads(raw)["custom_schema_output"]["data"]
        complete = data["complete"]
        if not isinstance(complete, bool):
            raise ValueError(f"complete is {complete!r}")
        return Verdict(complete, float(data["probability"]), int((time.monotonic() - started) * 1000))

    def classify(self, audio: np.ndarray, sample_rate: int) -> Verdict:
        """Blocking, hard-bounded by ``timeout_s``. Never raises: failure is UNKNOWN."""
        result: list[Verdict] = []

        def work() -> None:
            try:
                result.append(self._request(audio, sample_rate))
            except Exception as exc:
                logger.debug("smart-turn request failed: %s: %s", type(exc).__name__, exc)

        t = threading.Thread(target=work, name="smart-turn", daemon=True)
        started = time.monotonic()
        t.start()
        t.join(self.timeout_s)
        if result:
            return result[0]
        return Verdict(None, None, int((time.monotonic() - started) * 1000))

    def shadow(self, audio: np.ndarray, sample_rate: int) -> None:
        """Classify in the background and log one line; the caller never waits."""
        turn_s = len(audio) / sample_rate
        snapshot = np.array(audio, copy=True)

        def work() -> None:
            v = self.classify(snapshot, sample_rate)
            if v.complete is None:
                logger.info("SMART_TURN_SHADOW complete=unknown latency_ms=%d turn_s=%.1f", v.latency_ms, turn_s)
            else:
                logger.info(
                    "SMART_TURN_SHADOW complete=%s p=%.2f latency_ms=%d turn_s=%.1f",
                    str(v.complete).lower(),
                    v.probability,
                    v.latency_ms,
                    turn_s,
                )

        threading.Thread(target=work, name="smart-turn-shadow", daemon=True).start()


def build(base_url: str | None, sample_rate: int | None = None) -> SmartTurn | None:
    """A configured SmartTurn, or None when the feature is off or has no server."""
    mode = mode_from_env()
    if mode == "off":
        return None
    if not base_url:
        logger.warning("VOICE_SMART_TURN=%s but no remote transcription base url; Smart Turn stays off", mode)
        return None
    logger.info("Smart Turn %s via %s (threshold=%.2f hold=%dms)", mode, base_url, threshold_from_env(), hold_ms_from_env())
    return SmartTurn(base_url, mode, threshold_from_env(), hold_ms_from_env())
