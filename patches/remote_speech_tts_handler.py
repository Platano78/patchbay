from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from math import gcd
from queue import Queue
from threading import Event
from time import perf_counter
from typing import Any, Iterator, Optional

import httpx
import numpy as np
from rich.console import Console

from speech_to_speech import tts_capabilities
from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.cancel_scope import CancelScope
from speech_to_speech.pipeline.handler_types import TTSIn, TTSOut
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse
from speech_to_speech.pipeline.speculative_turns import SpeculativeTurnTracker

logger = logging.getLogger(__name__)
console = Console()

# The response_format="pcm" contract of OpenAI's /v1/audio/speech API: s16le, mono, 24kHz.
REMOTE_SAMPLE_RATE = 24000

# Zero-bytes-on-empty-`instructions` is a property of ONE VoiceDesign build we
# probed, not every OpenAI-`/v1/audio/speech` server (qwen CustomVoice is happy
# with the field absent -- see docs/plans/tts-capability-seam_spec.md, maintainer notes, not in the public export). So this
# is no longer a universal default: by default no instructions are sent at all
# (owner ruling 2026-08-25 -- a fixed neutral instruct on every request IS a
# one-string instruct layer, and the voice is meant to come back free-flowing).
# This constant is what an operator passes explicitly via
# `--remote_speech_instructions` when running a backend that DOES need one.
DEFAULT_INSTRUCTIONS = "Speak in a clear, natural, neutral adult voice."

# A 200 response is not proof of a real utterance: the same empty-instructions bug
# (and a mid-stream disconnect from the remote being stopped/restarted underneath
# us) both look like "success" unless the byte count is sanity-checked. The floor
# is 100ms of SOURCE audio (source_sample_rate * 2 // 10 bytes of s16le mono; 4800
# at the 24kHz default). Chosen to clear a legitimately terse reply ("Okay." is
# ~0.5-0.7s) while still catching a nothing-was-said response.

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
        instructions: str = "",
        sample_rate: int = 16000,
        blocksize: int = 512,
        source_sample_rate: int = REMOTE_SAMPLE_RATE,
        stream_format: str = "",
        runon_chars_per_s: float = 0.0,
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
        # Empty/whitespace stays empty -- an operator-supplied instruct (or none
        # at all, the default) is respected as-is. See DEFAULT_INSTRUCTIONS.
        self.instructions = (instructions or "").strip()
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        # Rate of the PCM the SERVER returns (24000 per the OpenAI pcm contract;
        # some servers differ). Empty stream_format = field omitted from the request.
        self.source_sample_rate = source_sample_rate
        self.stream_format = (stream_format or "").strip()
        self.runon_chars_per_s = runon_chars_per_s
        self.connect_timeout_s = connect_timeout_s
        self.read_timeout_s = read_timeout_s
        self.max_duration_s = max_duration_s
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s

        self._needs_resampling = self.sample_rate != self.source_sample_rate
        if self._needs_resampling:
            g = gcd(self.sample_rate, self.source_sample_rate)
            self._resample_up = self.sample_rate // g
            self._resample_down = self.source_sample_rate // g
            resample_ratio = self.source_sample_rate / self.sample_rate
            self._target_chunk_size = max(1, int(self.blocksize * resample_ratio))
        else:
            self._resample_up = self._resample_down = 1
            self._target_chunk_size = self.blocksize

        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        # Logged once (not per utterance) the first time an operator-set
        # instruct suppresses a per-turn affect -- see _stream_remote.
        self._affect_suppression_logged = False

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
        # The operator's own --pocket_tts_voice: what the fallback speaks when the
        # live-selected (remote) voice is one pocket cannot load. See _pocket_voice.
        self._pocket_default_voice = self._pocket_kwargs.get("voice")

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

    def _pocket_voice(self) -> tuple[Any, str]:
        """(voice pocket should be built with, reason). A live voice change
        (brain_control._set_voice_remote) writes the REMOTE voice name into
        _pocket_kwargs; pocket can only load its presets, hf:// / file paths and
        registered cloned voices, and raises on anything else -- which is silence.
        Decided here, once, at the only place pocket is constructed."""
        voice = self._pocket_kwargs.get("voice")
        default = self._pocket_default_voice
        if voice == default:
            return voice, "configured pocket voice"
        if isinstance(voice, str):
            if voice in KNOWN_POCKET_VOICE_PRESETS:
                return voice, "selected voice is a pocket preset"
            if voice.startswith("hf://") or os.path.exists(voice):
                return voice, "selected voice is a loadable path"
            try:
                from speech_to_speech import voice_clone

                if voice_clone.is_name_format_valid(voice) and voice_clone.voice_path(voice).is_file():
                    return str(voice_clone.voice_path(voice)), "selected voice is a registered cloned voice"
            except ImportError:
                pass
        return default, f"selected voice {voice!r} is not loadable by pocket"

    def _construct_pocket(self) -> Any:
        voice, why = self._pocket_voice()
        logger.info(
            "remote-speech: constructing pocket fallback (%s); pocket fallback voice %r (%s)",
            "preloaded at startup" if self.fallback_preload else "first failover, cold start accepted",
            voice,
            why,
        )
        return self._pocket_handler_cls(
            Event(),
            queue_in=Queue(),
            queue_out=Queue(),
            setup_args=(self.should_listen,),
            setup_kwargs={**self._pocket_kwargs, "voice": voice},
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

    @contextmanager
    def _open_speech_stream(self, payload: dict[str, Any], timeout: httpx.Timeout) -> Iterator[httpx.Response]:
        """POST the synthesis request. A base-mode server (e.g. Qwen3-TTS) answers
        a request carrying `instructions` with HTTP 400 "--instruct is not
        supported" and does not advertise that on /v1/audio/voices: remember it
        for this backend and replay the SAME request once without `instructions`,
        inside this synthesis attempt (no failure recorded, no pocket). Any other
        400 -- or any other status -- is returned untouched for the caller's
        raise_for_status()."""
        url = f"{self.base_url}/v1/audio/speech"
        with httpx.stream("POST", url, json=payload, timeout=timeout) as response:
            if response.status_code == 400 and "instructions" in payload:
                body = response.read().decode("utf-8", errors="replace").lower()
                if "instruct" in body and "not supported" in body:
                    logger.warning(
                        "remote-speech: backend rejected `instructions` (%s) -- retrying once without them "
                        "and not sending them again for a while",
                        body[:200],
                    )
                    tts_capabilities.mark_instructions_unsupported(self)
                else:
                    yield response
                    return
            else:
                yield response
                return
        retry_payload = {k: v for k, v in payload.items() if k != "instructions"}
        with httpx.stream("POST", url, json=retry_payload, timeout=timeout) as response:
            yield response

    def _stream_remote(self, text: str, affect: str | None = None) -> Iterator[TTSOut]:
        """Stream PCM audio from the remote server, resampled and blocked to match
        the pipeline's output contract. Raises on any failure (connect error, HTTP
        error, the total-duration cap, a mid-stream disconnect, or an implausibly
        short/empty response) so the caller can fall back to pocket.

        ``affect`` is the brain's per-turn delivery note (see
        ``voice_affect.py``). An operator-configured ``self.instructions``
        always wins over it -- ``speech_style`` is an explicit live user
        action surfaced in the cockpit panel, and silently overriding it
        every turn would make the panel lie about what the voice is doing.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "response_format": "pcm",
        }
        if self.voice:
            payload["voice"] = self.voice
        if self.stream_format:
            payload["stream_format"] = self.stream_format
        # Omitted (never sent as an empty string) unless there's something to
        # say AND the active backend actually wants it -- a backend that
        # declares accepts_instructions: false (e.g. a CustomVoice model) is
        # honored even if an operator configured one. See DEFAULT_INSTRUCTIONS
        # and docs/plans/tts-capability-seam_spec.md (maintainer notes, not in the public export).
        if self.instructions:
            resolved_instructions = self.instructions
            if affect and not self._affect_suppression_logged:
                self._affect_suppression_logged = True
                logger.info(
                    "remote-speech: per-turn affect suppressed by operator instructions=%r", self.instructions
                )
        else:
            resolved_instructions = affect
        if resolved_instructions and tts_capabilities.get_capabilities(self).accepts_instructions:
            payload["instructions"] = resolved_instructions

        gen = self.cancel_scope.generation if self.cancel_scope else None
        start = perf_counter()
        deadline = start + self.max_duration_s
        first_chunk = True
        total_bytes_received = 0
        runon_cap_s = 2.0 + len(text) / self.runon_chars_per_s if self.runon_chars_per_s > 0 else None
        floor_bytes = self.source_sample_rate * 2 // 10  # 100 ms of source audio

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
        yielded_any = False

        with self._open_speech_stream(payload, timeout) as response:
            response.raise_for_status()
            for raw_chunk in response.iter_bytes():
                # A chunk that crosses the run-on cap is cut (below), not timed
                # out, even if it arrived after the deadline: raising here would
                # replay the already-spoken start via pocket.
                runon_crossed = (
                    runon_cap_s is not None
                    and (total_bytes_received + len(raw_chunk)) / 2 / self.source_sample_rate > runon_cap_s
                )
                if time.monotonic() > deadline and not runon_crossed:
                    raise TimeoutError(
                        f"remote-speech synthesis exceeded {self.max_duration_s:.0f}s total-duration cap"
                    )
                if gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen):
                    logger.info("TTS generation cancelled (interruption)")
                    return
                if not raw_chunk:
                    continue
                total_bytes_received += len(raw_chunk)
                if runon_crossed:
                    # Run-on guard: the start of this utterance has already been
                    # played, so end it here -- no raise, no pocket (it would
                    # re-speak the beginning), no failure recorded. If nothing
                    # was played yet there is nothing to re-speak: fail over.
                    if not yielded_any:
                        raise RuntimeError(
                            f"remote-speech run-on: first audio already exceeds the {runon_cap_s:.1f}s cap "
                            "-- nothing was spoken, treating as a failed synthesis"
                        )
                    logger.warning(
                        "remote-speech: run-on cut -- %d-char text capped at %.1fs, received %.1fs of audio",
                        len(text),
                        runon_cap_s,
                        total_bytes_received / 2 / self.source_sample_rate,
                    )
                    return
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
                        # A barge-in cancel must also stop the blocks of the chunk
                        # already in hand, or they land in the output queue just
                        # after the server drained it.
                        if gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen):
                            logger.info("TTS generation cancelled (interruption)")
                            return
                        yielded_any = True
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
        if total_bytes_received < floor_bytes:
            raise RuntimeError(
                f"remote-speech returned only {total_bytes_received} bytes of audio "
                f"(floor is {floor_bytes} bytes, ~100ms) -- treating as a failed synthesis"
            )

        if audio_buffer and buffer_size > 0:
            concatenated = np.concatenate(audio_buffer)
            resampled = self._resample(concatenated)
            audio_int16 = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
            audio_int16 = np.concatenate([leftover, audio_int16])
        else:
            audio_int16 = leftover

        for i in range(0, len(audio_int16), self.blocksize):
            # Same barge-in rule as the in-loop blocks: a cancel that lands after the
            # last in-loop check must not leak the tail of the cancelled reply.
            if gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen):
                logger.info("TTS generation cancelled (interruption)")
                return
            chunk = audio_int16[i : i + self.blocksize]
            if len(chunk) < self.blocksize:
                chunk = np.pad(chunk, (0, self.blocksize - len(chunk)))
            yield chunk

    def _input_cancelled(self, tts_input: TTSIn) -> bool:
        """True when this input belongs to a generation a barge-in cancelled.

        `_stream_remote` captures the CURRENT generation when it starts, so an input
        queued before the cancel would otherwise be spoken under the new one."""
        gen = getattr(tts_input, "cancel_generation", None)
        return gen is not None and self.cancel_scope is not None and self.cancel_scope.is_stale(gen)

    def process(self, tts_input: TTSIn) -> Iterator[TTSOut]:
        if self._input_cancelled(tts_input):
            logger.info("TTS input of a cancelled generation dropped")
            return
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
        # Feature-detection, never isinstance(): a plain TTSInput from the
        # voice-audition or hermes-cockpit call sites has no `affect`
        # attribute at all, and must send no instruct either.
        affect = getattr(tts_input, "affect", None)

        if self.base_url is not None and self._circuit_allows_remote():
            console.print(f"[green]ASSISTANT: {text}")
            try:
                for chunk in self._stream_remote(text, affect):
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
