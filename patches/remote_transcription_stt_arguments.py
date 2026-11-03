from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RemoteTranscriptionSTTHandlerArguments:
    """CLI/JSON args for the `remote-transcription` STT backend.

    Named by protocol, not by any specific server: `remote-transcription` talks to
    any server implementing OpenAI's `/v1/audio/transcriptions` API (a Whisper-API
    server, OpenAI itself, ...). Strictly opt-in -- `--stt remote-transcription`
    without `remote_transcription_base_url` is refused at startup, and the shipped
    default (`parakeet-tdt`) is unaffected. There is no local fallback: if the
    remote is down, the turn's final transcript is empty (see
    `STT/remote_transcription_stt_handler.py`).
    """

    remote_transcription_base_url: Optional[str] = field(
        default=None,
        metadata={
            "help": "Base URL of an OpenAI-compatible /v1/audio/transcriptions server, e.g. "
            "'http://host:port'. Required when --stt remote-transcription is used."
        },
    )
    remote_transcription_model: str = field(
        default="whisper-1",
        metadata={"help": "The `model` form field sent in the /v1/audio/transcriptions request."},
    )
    remote_transcription_language: Optional[str] = field(
        default=None,
        metadata={
            "help": "Optional language code (e.g. 'en') sent as the `language` form field and used as "
            "the reported language when the server does not return one. Unset: the server auto-detects."
        },
    )
    remote_transcription_progressive_timeout_s: float = field(
        default=1.0,
        metadata={
            "help": "Per-operation (connect/read) timeout in seconds for a live (progressive) "
            "partial-transcript request, NOT a total deadline: a server that keeps trickling bytes "
            "can hold the call longer. A failed or timed-out partial is dropped silently. Partials "
            "are skipped (never queued) while a final or another partial is in progress, but a "
            "partial already in flight can delay a following final by up to this long."
        },
    )
    remote_transcription_final_timeout_s: float = field(
        default=15.0,
        metadata={
            "help": "Per-operation (connect/read) timeout in seconds for the final transcript "
            "request, NOT a total deadline: a server that keeps trickling bytes can hold the call "
            "longer. On timeout or error the turn gets an empty transcript (there is no local "
            "fallback)."
        },
    )
    remote_transcription_progressive_window_s: float = field(
        default=15.0,
        metadata={
            "help": "Progressive requests upload only the last N seconds of the utterance so far "
            "(the final always uploads all of it)."
        },
    )
