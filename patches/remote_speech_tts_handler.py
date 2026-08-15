from __future__ import annotations

import logging
import os
import time
from math import gcd
from queue import Queue
from threading import Event
from time import perf_counter
from typing import Any, Iterator, Optional

import httpx
import numpy as np
from rich.console import Console

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.handler_types import TTSIn, TTSOut
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker

logger = logging.getLogger(__name__)
console = Console()

# The response_format="pcm" contract of OpenAI's /v1/audio/speech API: s16le, mono, 24kHz.
REMOTE_SAMPLE_RATE = 24000

# This VoiceDesign build has no default speaker: an empty/whitespace `instructions`
# field yields a verified failure mode -- HTTP 200, Content-Type audio/pcm, ZERO
# bytes, in well under a millisecond. `instructions` must never reach the wire empty.
DEFAULT_INSTRUCTIONS = "Speak in a clear, natural, neutral adult voice."

# A 200 response is not proof of a real utterance: the same empty-instructions bug
# (and a mid-stream disconnect from the remote being stopped/restarted underneath
# us) both look like "success" unless the byte count is sanity-checked. ~100ms of
# 24kHz s16le mono audio. Chosen to clear a legitimately terse reply ("Okay." is
# ~0.5-0.7s) while still catching a nothing-was-said response.
RESPONSE_FLOOR_BYTES = 4800

# Known pocket-tts preset voices (from pocket_tts_handler.py's docstring). Used only
# for a best-effort boot-time sanity check -- voice can also be a local file path or
# an hf:// path, so an unrecognized value is a warning, not a hard failure.
KNOWN_POCKET_VOICE_PRESETS = frozenset(
    {"alba", "marius", "javert", "jean", "fantine", "cosette", "eponine", "azelma"}
)


class RemoteSpeechTTSHandler(BaseHandler[TTSIn, TTSOut]):
    """TTS handler for any server implementing OpenAI's ``/v1/audio/speech`` protocol.

    Named and documented by PROTOCOL, not by any specific server, host, or model --
    any OpenAI-compatible ``/v1/audio/speech`` endpoint (including OpenAI itself)
    satisfies this backend. Streams the server's ``response_format: "pcm"`` reply
    (s16le, 24kHz, mono) incrementally and resamples to the pipeline's 16kHz using
    the same buffered polyphase approach as ``pocket_tts_handler.py``.

    ``pocket`` is the automatic, never-silent fallback: an unreachable, erroring,
    empty/truncated, or too-slow remote transparently falls through to pocket
    instead of going mute. By default pocket is NOT constructed until the first
    failover (accepting a cold start on that one utterance so its weights are
    never loaded on the pipeline host in the common case); set
    ``remote_speech_fallback_preload`` to construct it warm at startup instead.
    A cheap, weight-free boot-time check validates the fallback is constructible
    either way. A circuit breaker avoids paying a failed round-trip on every
    utterance once the remote is confirmed down, and re-probes it after a
    cooldown long enough to ride out a routine ~seconds-long remote restart.
    """

    def setup(
        self,
        should_listen: Event,
        base_url: Optional[str] = None,
        fallback_preload: bool = False,
        model: str = "tts-1",
        voice: Optional[str] = None,
        instructions: str = DEFAULT_INSTRUCTIONS,
        sample_rate: int = 16000,
        blocksize: int = 512,
        connect_timeout_s: float = 1.0,
        read_timeout_s: float = 10.0,
        max_duration_s: float = 15.0,
        failure_threshold: int = 3,
        cooldown_s: float = 30.0,
        pocket_kwargs: Optional[dict[str, Any]] = None,
        cancel_scope: CancelScope | None = None,
        speculative_turns: SpeculativeTurnTracker | None = None,
        gen_kwargs: Optional[dict[str, Any]] = None,  # For compatibility with pipeline, not used
    ) -> None:
        # NOTE: these parameter names are unprefixed (`base_url`, not
        # `remote_speech_base_url`) to match every other TTS handler's setup()
        # signature (pocket_tts_handler.py, kokoro_handler.py, ...). The
        # ArgumentsClass fields ARE `remote_speech_*` (that's what makes the CLI
        # flags right); `rename_args()` in s2s_pipeline.py strips that prefix
        # before setup_kwargs reaches here. A mismatch here is a TypeError at
        # startup, not a test failure -- see test_setup_signature_matches_
        # what_rename_args_produces, which exists specifically to catch that
        # class of bug at the seam instead of relying on every unit test
        # happening to exercise the real prefix-stripping path.
        self.should_listen = should_listen
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns

        self.base_url = (base_url or "").rstrip("/") or None
        self.fallback_preload = fallback_preload
        self.model = model
        self.voice = voice
        # Never send an empty/whitespace instructions field -- see DEFAULT_INSTRUCTIONS.
        self.instructions = (instructions or "").strip() or DEFAULT_INSTRUCTIONS
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.max_duration_s = max_duration_s
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s

        self._needs_resampling = self.sample_rate != REMOTE_SAMPLE_RATE
        if self._needs_resampling:
            g = gcd(self.sample_rate, REMOTE_SAMPLE_RATE)
            self._resample_up = self.sample_rate // g
            self._resample_down = REMOTE_SAMPLE_RATE // g
            resample_ratio = REMOTE_SAMPLE_RATE / self.sample_rate
            self._target_chunk_size = max(1, int(self.blocksize * resample_ratio))
        else:
            self._resample_up = self._resample_down = 1
            self._target_chunk_size = self.blocksize

        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        # Pocket is the automatic fallback. It runs as a plain synthesis engine
        # (setup() + direct process() calls below), never as its own pipeline
        # thread: the dedicated stop_event/queues in `_construct_pocket` only
        # satisfy BaseHandler.__init__ and are never touched by a `run()` loop.
        # speculative_turns is left None because staleness is already checked
        # once, upstream, in this handler's own process().
        try:
            from speech_to_speech.TTS.pocket_tts_handler import PocketTTSHandler
        except ImportError as e:
            raise ImportError(
                "remote-speech's automatic fallback requires Pocket TTS. "
                'Install it with `pip install "speech-to-speech[pocket]"`.'
            ) from e
        self._pocket_handler_cls = PocketTTSHandler

        self._pocket_kwargs: dict[str, Any] = dict(pocket_kwargs or {})
        self._pocket_kwargs.setdefault("sample_rate", self.sample_rate)
        self._pocket_kwargs["cancel_scope"] = cancel_scope
        self._pocket_kwargs["speculative_turns"] = None

        # Cheap, weight-free check that the fallback is constructible, whether or
        # not it is preloaded now: catches a broken fallback config at boot,
        # before the pipeline accepts a single utterance, rather than discovering
        # it mid-conversation on the first failover.
        self._validate_pocket_fallback_boot()

        self._pocket: Any = None
        if self.fallback_preload:
            self._pocket = self._construct_pocket()

        if self.base_url is None:
            logger.warning(
                "remote-speech: remote_speech_base_url is unset -- this backend is "
                "unavailable and every utterance will be served by pocket."
            )

    def _validate_pocket_fallback_boot(self) -> None:
        """Weight-free sanity check that the pocket fallback is constructible.

        Does NOT call `TTSModel.load_model()` (that only happens inside
        `PocketTTSHandler.setup()`, invoked lazily by `_construct_pocket`).
        Checks: the `pocket_tts` package itself is importable (its class import
        above only proves `speech_to_speech`'s wrapper is importable, not that
        the underlying library is installed -- pocket_tts_handler.py imports it
        lazily inside setup()), and that a voice is configured. Voice PRESET
        validity is a best-effort warning, not a hard failure, since a valid
        voice can also be a local file path or an hf:// URL this cheap check
        cannot resolve without loading the model.
        """
        try:
            import pocket_tts  # noqa: F401 -- package presence only, no model load
        except ImportError as e:
            raise ImportError(
                "remote-speech's automatic fallback requires the pocket_tts package. "
                'Install it with `pip install "speech-to-speech[pocket]"`.'
            ) from e

        voice = self._pocket_kwargs.get("voice")
        if not voice or not isinstance(voice, str):
            raise ValueError("remote-speech's pocket fallback requires a non-empty pocket_tts_voice.")
        if voice not in KNOWN_POCKET_VOICE_PRESETS and not voice.startswith("hf://") and not os.path.exists(voice):
            logger.warning(
                "remote-speech: pocket_tts_voice=%r is not a known preset, an hf:// path, or an "
                "existing file -- best-effort check only (resolving it for real would require "
                "loading the model), so this is a warning, not a boot failure.",
                voice,
            )

    def _construct_pocket(self) -> Any:
        logger.info(
            "remote-speech: constructing pocket fallback (%s)",
            "preloaded at startup" if self.fallback_preload else "first failover, cold start accepted",
        )
        return self._pocket_handler_cls(
            Event(),
            queue_in=Queue(),
            queue_out=Queue(),
            setup_args=(self.should_listen,),
            setup_kwargs=dict(self._pocket_kwargs),
        )

    def _pocket_instance(self) -> Any:
        """Return the warm pocket instance, constructing it on first use if
        `fallback_preload` is False. Retained for the rest of the process
        lifetime once built -- never torn down and reconstructed per-utterance."""
        if self._pocket is None:
            self._pocket = self._construct_pocket()
        return self._pocket

    @property
    def min_time_to_debug(self) -> float:
        return 0.1

    def _circuit_allows_remote(self) -> bool:
        """True if a remote attempt should be made this turn. False means the
        circuit is open and this utterance should be served by pocket directly.

        Once the cooldown has elapsed, this performs one cheap ``GET /health``
        probe rather than paying for a full synthesis round-trip on every
        utterance while the remote is down: a successful probe closes the
        circuit (this utterance IS attempted on the remote); a failed probe
        keeps it open and restarts the cooldown.
        """
        if self._circuit_open_until == 0.0:
            return True
        if time.monotonic() < self._circuit_open_until:
            return False
        if self._probe_health():
            logger.info("remote-speech: health probe succeeded, circuit closed")
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0
            return True
        logger.info(
            "remote-speech: health probe failed, circuit stays open for another %.0fs",
            self.cooldown_s,
        )
        self._circuit_open_until = time.monotonic() + self.cooldown_s
        return False

    def _record_success(self) -> None:
        if self._consecutive_failures or self._circuit_open_until:
            logger.info("remote-speech: recovered, circuit closed")
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        logger.info(
            "remote-speech: synthesis failed (%s), consecutive_failures=%d/%d -- falling back to pocket",
            reason,
            self._consecutive_failures,
            self.failure_threshold,
        )
        if self._consecutive_failures >= self.failure_threshold and self._circuit_open_until == 0.0:
            self._circuit_open_until = time.monotonic() + self.cooldown_s
            logger.info(
                "remote-speech: circuit breaker OPEN for %.0fs (next utterance re-probes via GET /health)",
                self.cooldown_s,
            )

    def _probe_health(self) -> bool:
        """GET /health once. Called by `_circuit_allows_remote` after the
        cooldown elapses, to decide whether to close the circuit."""
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=self.connect_timeout_s)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 -- a failed probe just keeps the circuit open
            return False

    def _resample(self, audio: np.ndarray) -> np.ndarray:
        if not self._needs_resampling:
            return audio
        from scipy.signal import resample_poly

        return resample_poly(audio, up=self._resample_up, down=self._resample_down)

    def _stream_remote(self, text: str) -> Iterator[TTSOut]:
        """Stream PCM audio from the remote server, resampled and blocked to match
        the pipeline's output contract. Raises on any failure (connect error, HTTP
        error, the total-duration cap, a mid-stream disconnect, or an implausibly
        short/empty response) so the caller can fall back to pocket."""
        payload: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "response_format": "pcm",
            # Always sent, never omitted or empty: this build has no default
            # speaker, and an empty instructions field is a verified silent
            # (200 + zero bytes) failure mode -- see DEFAULT_INSTRUCTIONS.
            "instructions": self.instructions,
        }
        if self.voice:
            payload["voice"] = self.voice

        gen = self.cancel_scope.generation if self.cancel_scope else None
        start = perf_counter()
        deadline = start + self.max_duration_s
        first_chunk = True
        total_bytes_received = 0

        # Connect timeout kept short and separate from the read (inter-chunk) timeout
        # so a stopped remote (ECONNREFUSED) fails instantly instead of hanging.
        # Neither timeout alone bounds a slow trickle -- an HTTP read timeout resets
        # on every byte -- so `deadline` below is the real duration cap (mirrors
        # chat_completions_language_model.py's total-duration watchdog).
        timeout = httpx.Timeout(
            connect=self.connect_timeout_s,
            read=self.read_timeout_s,
            write=self.connect_timeout_s,
            pool=self.connect_timeout_s,
        )

        byte_remainder = b""
        audio_buffer: list[np.ndarray] = []
        buffer_size = 0
        leftover = np.array([], dtype=np.int16)

        with httpx.stream("POST", f"{self.base_url}/v1/audio/speech", json=payload, timeout=timeout) as response:
            response.raise_for_status()
            for raw_chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"remote-speech synthesis exceeded {self.max_duration_s:.0f}s total-duration cap"
                    )
                if gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen):
                    logger.info("TTS generation cancelled (interruption)")
                    return
                if not raw_chunk:
                    continue
                total_bytes_received += len(raw_chunk)
                if first_chunk:
                    logger.info("remote-speech TTFA: %.3fs", perf_counter() - start)
                    first_chunk = False

                data = byte_remainder + raw_chunk
                usable = len(data) - (len(data) % 2)  # keep an even byte count (2 bytes/int16 sample)
                byte_remainder = data[usable:]
                if usable == 0:
                    continue

                samples_i16 = np.frombuffer(data[:usable], dtype="<i2")
                audio_buffer.append(samples_i16.astype(np.float32) / 32768.0)
                buffer_size += len(samples_i16)

                while buffer_size >= self._target_chunk_size:
                    concatenated = np.concatenate(audio_buffer)
                    chunk_to_process = concatenated[: self._target_chunk_size]
                    remainder = concatenated[self._target_chunk_size :]

                    resampled = self._resample(chunk_to_process)
                    audio_int16 = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
                    audio_int16 = np.concatenate([leftover, audio_int16])

                    n = (len(audio_int16) // self.blocksize) * self.blocksize
                    for i in range(0, n, self.blocksize):
                        yield audio_int16[i : i + self.blocksize]
                    leftover = audio_int16[n:]

                    audio_buffer = [remainder] if len(remainder) > 0 else []
                    buffer_size = len(remainder)

        # A 200 with a clean end-of-stream is NOT proof of a real utterance: the
        # empty-instructions bug (now guarded above, but defense in depth) and any
        # other "success-shaped but empty" response both land here with a stream
        # that ended normally. Zero bytes is unconditionally a failure; anything
        # under the floor is treated the same way rather than played as a
        # legitimately-short reply.
        if total_bytes_received < RESPONSE_FLOOR_BYTES:
            raise RuntimeError(
                f"remote-speech returned only {total_bytes_received} bytes of audio "
                f"(floor is {RESPONSE_FLOOR_BYTES} bytes, ~100ms) -- treating as a failed synthesis"
            )

        if audio_buffer and buffer_size > 0:
            concatenated = np.concatenate(audio_buffer)
            resampled = self._resample(concatenated)
            audio_int16 = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
            audio_int16 = np.concatenate([leftover, audio_int16])
        else:
            audio_int16 = leftover

        for i in range(0, len(audio_int16), self.blocksize):
            chunk = audio_int16[i : i + self.blocksize]
            if len(chunk) < self.blocksize:
                chunk = np.pad(chunk, (0, self.blocksize - len(chunk)))
            yield chunk

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        speculative_turns = getattr(self, "speculative_turns", None)
        if isinstance(tts_input, EndOfResponse):
            if speculative_turns and not speculative_turns.is_latest_after_reopen_grace(
                tts_input.turn_id,
                tts_input.turn_revision,
            ):
                return
            yield AUDIO_RESPONSE_DONE
            return

        if speculative_turns and not speculative_turns.is_latest_after_reopen_grace(
            tts_input.turn_id,
            tts_input.turn_revision,
        ):
            logger.debug("Dropping stale TTS input for turn=%s rev=%s", tts_input.turn_id, tts_input.turn_revision)
            return
        if speculative_turns:
            speculative_turns.commit(tts_input.turn_id, tts_input.turn_revision)

        text = tts_input.text

        if self.base_url is not None and self._circuit_allows_remote():
            console.print(f"[green]ASSISTANT: {text}")
            try:
                for chunk in self._stream_remote(text):
                    yield chunk
                self._record_success()
                return
            except Exception as e:  # noqa: BLE001 -- any remote failure falls back to pocket, never goes silent
                # Falls through to the pocket fallback below unconditionally, even
                # if some audio for this utterance was already streamed out (a
                # mid-stream disconnect, or the duration cap tripping after a
                # partial buffer) -- a truncated utterance must not be treated as
                # a completed one. See message-thread ruling: never silently end
                # a cut-off utterance and call it done.
                self._record_failure(f"{type(e).__name__}: {e}")
        elif self.base_url is not None:
            logger.info("remote-speech: circuit open, serving pocket directly")

        yield from self._pocket_instance().process(tts_input)
