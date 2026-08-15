from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RemoteSpeechTTSHandlerArguments:
    """CLI/JSON args for the `remote-speech` TTS backend.

    Named by protocol, not by any specific server: `remote-speech` talks to any
    server implementing OpenAI's `/v1/audio/speech` API. Strictly opt-in --
    `remote_speech_base_url` unset means the backend is unavailable and `pocket`
    (the shipped default) is unaffected. When primary, pocket is the automatic,
    never-silent fallback: by default it is NOT constructed until the first
    failover (`remote_speech_fallback_preload=False`), accepting a cold start on
    that one utterance so its weights are never loaded on the pipeline host in
    the common case; set `remote_speech_fallback_preload` to construct it warm
    at startup instead (see `TTS/remote_speech_tts_handler.py`).
    """

    remote_speech_base_url: Optional[str] = field(
        default=None,
        metadata={
            "help": "Base URL of an OpenAI-compatible /v1/audio/speech server, e.g. "
            "'http://host:port'. Unset (the default) disables this backend entirely."
        },
    )
    remote_speech_fallback_preload: bool = field(
        default=False,
        metadata={
            "help": "If true, construct and setup() the pocket fallback at startup so it is warm "
            "before the first utterance (higher RAM, no cold-start on first failover). Default "
            "false: pocket is not constructed at all until the first failover, accepting a cold "
            "start on that one utterance in exchange for not loading pocket's weights on the "
            "pipeline host in the common case where the remote never fails. A cheap boot-time "
            "check (no weights loaded) still validates the fallback is constructible either way."
        },
    )
    remote_speech_model: str = field(
        default="tts-1",
        metadata={"help": "The `model` field sent in the /v1/audio/speech request body."},
    )
    remote_speech_voice: Optional[str] = field(
        default=None,
        metadata={"help": "Optional `voice` field, passed through to the server verbatim."},
    )
    remote_speech_instructions: str = field(
        default="Speak in a clear, natural, neutral adult voice.",
        metadata={
            "help": "The `instructions` field, passed through to the server verbatim -- the "
            "expression/prosody handle. Sets the startup value; live-settable afterwards via "
            "config_set {speech_style: ...} (see brain_control.py). "
            "Must never reach the server empty: this VoiceDesign build has no default speaker, "
            "so an empty/whitespace instructions field yields a silent HTTP 200 with zero bytes "
            "of audio (a verified failure mode, not hypothetical) -- an empty or whitespace-only "
            "value here is replaced with this field's default at request time, never sent as-is."
        },
    )
    remote_speech_sample_rate: int = field(
        default=16000,
        metadata={
            "help": "Output sample rate in Hz to match the pipeline's audio output. The remote "
            "server's 24kHz PCM response is resampled to this rate."
        },
    )
    remote_speech_blocksize: int = field(
        default=512,
        metadata={"help": "Size of audio blocks to yield for streaming. Default is 512."},
    )
    remote_speech_connect_timeout_s: float = field(
        default=1.0,
        metadata={
            "help": "Connect timeout in seconds, kept short and separate from the read timeout so "
            "a stopped remote (ECONNREFUSED) fails fast instead of hanging."
        },
    )
    remote_speech_read_timeout_s: float = field(
        default=10.0,
        metadata={"help": "Per-read (inter-chunk) timeout in seconds."},
    )
    remote_speech_max_duration_s: float = field(
        default=15.0,
        metadata={
            "help": "Hard wall-clock cap on total synthesis duration for one utterance. An HTTP "
            "read timeout resets on every byte and never fires against a slow trickle, so this "
            "bounds total duration explicitly instead."
        },
    )
    remote_speech_failure_threshold: int = field(
        default=3,
        metadata={
            "help": "Consecutive remote failures before the circuit breaker opens: further "
            "utterances are served by pocket directly (no remote attempt) until the cooldown ends."
        },
    )
    remote_speech_cooldown_s: float = field(
        default=30.0,
        metadata={"help": "Seconds the circuit breaker stays open before re-probing the remote via GET /health."},
    )
