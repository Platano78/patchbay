from __future__ import annotations

import io
import logging
import re
import wave
from threading import Lock
from time import perf_counter
from typing import Any, Iterator, Optional

import httpx
import numpy as np

from speech_to_speech.pipeline.handler_types import STTIn, STTOut
from speech_to_speech.pipeline.messages import PartialTranscription, Transcription
from speech_to_speech.STT.base_stt_handler import BaseSTTHandler

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# One cheap probe at startup; a failure only warns (see `_probe_health`).
HEALTH_TIMEOUT_S = 2.0

_LANG_RE = re.compile(r"^[a-z]{2}$")


def _normalize_language(value: Any) -> Optional[str]:
    """ISO 639-1 code from a server-reported language ("en", "en-US" -> "en"), else None.

    Whisper servers sometimes report a full name ("english"); those are ignored rather
    than guessed at, and the caller falls back to the configured / last known language.
    """
    if not isinstance(value, str):
        return None
    primary = value.strip().lower().replace("_", "-").split("-")[0]
    return primary if _LANG_RE.match(primary) else None


def encode_wav(audio: np.ndarray) -> bytes:
    """16 kHz mono PCM16 WAV, in memory."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url}"
    return f"{type(exc).__name__}: {exc}"


class RemoteTranscriptionSTTHandler(BaseSTTHandler):
    """STT handler for any server implementing OpenAI's ``/v1/audio/transcriptions`` protocol.

    Named and documented by PROTOCOL, not by any specific server or model. Mirrors the
    message contract of ``ParakeetTDTSTTHandler``:

    * ``progressive`` input (only with ``enable_live_transcription``): skipped (never queued)
      while a final is being processed or another progressive request is in flight;
      otherwise the last ``progressive_window_s`` of audio-so-far is re-transcribed and
      yielded as a ``PartialTranscription``. Failures are logged at DEBUG and yield nothing.
      The request runs synchronously inside ``process()`` and the base handler processes items
      serially, so a progressive already in flight can delay a following final by up to
      ``progressive_timeout_s`` (Parakeet behaves the same way).
    * final input: all the audio is transcribed and yielded as a ``Transcription``. Any
      failure is logged at ERROR and yields a ``Transcription`` with empty text.

    ``progressive_timeout_s`` / ``final_timeout_s`` are httpx per-operation (connect/read/write)
    timeouts, NOT wall-clock deadlines: a server that trickles a valid body can hold a call
    longer than the timeout.

    There is no local fallback and no local model: when the remote is down the turn simply
    gets an empty transcript. A failed startup health probe only warns.
    """

    def setup(
        self,
        base_url: Optional[str] = None,
        model: str = "whisper-1",
        language: Optional[str] = None,
        progressive_timeout_s: float = 1.0,
        final_timeout_s: float = 15.0,
        progressive_window_s: float = 15.0,
        enable_live_transcription: bool = False,
        live_transcription_update_interval: float = 0.5,  # accepted for parity with Parakeet; unused
        gen_kwargs: Optional[dict[str, Any]] = None,  # for compatibility with the pipeline, not used
    ) -> None:
        # NOTE: parameter names are unprefixed (`base_url`, not `remote_transcription_base_url`):
        # `rename_args()` in s2s_pipeline.py strips the `remote_transcription_` prefix before
        # setup_kwargs reach here (same seam as remote_speech_tts_handler.py).
        self.base_url = (base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError(
                "--stt remote-transcription requires --remote_transcription_base_url "
                "(the base URL of a server implementing OpenAI's /v1/audio/transcriptions)"
            )
        self.model = model
        self.start_language = (language or "").strip() or None
        self._configured_language = _normalize_language(self.start_language)
        self.last_language = self._configured_language or "en"
        self.progressive_timeout_s = progressive_timeout_s
        self.final_timeout_s = final_timeout_s
        self.progressive_window_s = progressive_window_s
        self.enable_live_transcription = enable_live_transcription
        self.live_transcription_update_interval = live_transcription_update_interval
        self.gen_kwargs = gen_kwargs or {}
        self.sample_rate = SAMPLE_RATE

        self._state_lock = Lock()  # guards processing_final / last_language
        self._progressive_lock = Lock()  # held while a progressive request is in flight
        self.processing_final = False

        self._client = httpx.Client()
        self._probe_health()

    def _probe_health(self) -> None:
        """GET /health once. A failure is a WARNING, never a boot failure: the remote may
        simply not be up yet, and requests fail cleanly later."""
        try:
            resp = self._client.get(f"{self.base_url}/health", timeout=HEALTH_TIMEOUT_S)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            logger.info("remote-transcription: %s is healthy (model=%s)", self.base_url, self.model)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "remote-transcription: health check against %s failed (%s); continuing, "
                "transcription requests will fail until it is reachable",
                self.base_url,
                _describe(e),
            )

    def _transcribe(self, audio: np.ndarray, timeout_s: float) -> tuple[str, Optional[str]]:
        """POST the audio; return (stripped text, server-reported language or None). Raises on failure."""
        data = {"model": self.model, "response_format": "json"}
        if self.start_language:
            data["language"] = self.start_language
        resp = self._client.post(
            f"{self.base_url}/v1/audio/transcriptions",
            data=data,
            files={"file": ("audio.wav", encode_wav(audio), "audio/wav")},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise ValueError("response is not a JSON object with a string `text`")
        return body["text"].strip(), _normalize_language(body.get("language"))

    def process(self, vad_audio: STTIn) -> Iterator[STTOut]:
        """Yield :class:`PartialTranscription` (progressive) or :class:`Transcription` (final)."""
        process_start_s = perf_counter()
        is_progressive = vad_audio.mode == "progressive"
        audio_input = np.asarray(vad_audio.audio, dtype=np.float32).reshape(-1)
        audio_duration_s = len(audio_input) / self.sample_rate

        if is_progressive:
            if not self.enable_live_transcription:
                return
            if self.processing_final:
                logger.debug("Skipping stale progressive update (final audio already received)")
                return
            if not self._progressive_lock.acquire(blocking=False):
                logger.debug("Skipping progressive update (previous progressive request still in flight)")
                return
            try:
                window = int(self.progressive_window_s * self.sample_rate)
                if window > 0 and len(audio_input) > window:
                    audio_input = audio_input[-window:]
                if len(audio_input) == 0:
                    return
                text, _ = self._transcribe(audio_input, self.progressive_timeout_s)
            except Exception as e:  # noqa: BLE001
                logger.debug("Progressive transcription failed: %s", _describe(e))
                return
            finally:
                self._progressive_lock.release()
            if text:
                yield PartialTranscription(
                    text=text,
                    turn_id=vad_audio.turn_id,
                    turn_revision=vad_audio.turn_revision,
                )
            return

        logger.info(
            "Remote final STT start turn=%s rev=%s audio=%.3fs age=%.3fs",
            vad_audio.turn_id,
            vad_audio.turn_revision,
            audio_duration_s,
            self._item_age_s(vad_audio),
        )
        with self._state_lock:
            self.processing_final = True
            # server-reported (below) > configured > last known > "en"
            language_code = self._configured_language or self.last_language
        text = ""
        try:
            if len(audio_input) > 0:
                text, server_language = self._transcribe(audio_input, self.final_timeout_s)
                with self._state_lock:
                    if server_language:
                        self.last_language = server_language
                    language_code = server_language or language_code
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Remote transcription failed for final turn=%s rev=%s: %s",
                vad_audio.turn_id,
                vad_audio.turn_revision,
                _describe(e),
            )
            text = ""
        finally:
            with self._state_lock:
                self.processing_final = False

        logger.info(
            "Remote final STT done turn=%s rev=%s total=%.3fs chars=%d",
            vad_audio.turn_id,
            vad_audio.turn_revision,
            perf_counter() - process_start_s,
            len(text),
        )
        yield Transcription(
            text=text,
            language_code=language_code,
            turn_id=vad_audio.turn_id,
            turn_revision=vad_audio.turn_revision,
            speech_stopped_at_s=vad_audio.created_at_s,
        )

    def cleanup(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
