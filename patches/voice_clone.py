"""Custom voice cloning: name/format validation, the chunked-upload state
machine, soundfile normalization, and sidecar (``~/speech-to-speech/voices/``)
persistence for the `voice_clone_*` control-frame protocol.

Dependency-light like ``think_filter.py``/``voice_rules.py``: no
``speech_to_speech`` import at module scope, and the two genuinely heavy
dependencies -- ``soundfile`` (WAV normalization) and ``pocket_tts`` (state
build/export, performed by the caller, not here) -- are only ever imported
lazily inside the one function that needs them, so this module (and its
tests) stay importable with neither installed.

See ``docs/plans/custom-voice-cloning-v1.3.md`` (rulings 1, 3, 5, 9, 10) and
``docs/research/pocket-tts-voice-cloning-2026-07-17.md`` for the underlying
pocket_tts API this builds on.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# Ruling 1: sidecar dir next to brains.json, survives reinstall.
_DEFAULT_VOICES_DIR = "~/speech-to-speech/voices/"

# Protocol section: 25 MB raw upload cap, <=512 KiB raw per chunk (b64 ~683
# KiB, under the 1 MiB websockets default frame cap -- see research §7).
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_CHUNK_BYTES = 512 * 1024

# Ruling 5: soundfile-decodable containers vs the two the box cannot read.
ACCEPTED_EXTENSIONS = (".wav", ".aiff", ".aif", ".flac", ".ogg", ".mp3")
REJECTED_EXTENSIONS = (".webm", ".m4a", ".mp4")

# Ruling 8.
DEFAULT_AUDITION_TEXT = "Hi, I'm {name}. This is how I sound."

# Decode-bomb guard: MAX_UPLOAD_BYTES already caps the compressed upload, but
# a small file can still decode to a very long/high-rate PCM buffer (e.g. a
# long silent or low-entropy recording compresses hard). Bound the DECODED
# duration too, checked via `soundfile.info` before the full decode.
MAX_DECODED_SECONDS = 120

# Ruling 7, verbatim.
CLONING_UNAVAILABLE_MSG = (
    "This install can't build new voices yet: accept the terms at "
    "https://huggingface.co/kyutai/pocket-tts, run `hf auth login` as the "
    "service user, then restart the service."
)

# Ruling 9: lowercase, digits, underscore, hyphen, 1-32 chars.
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class VoiceCloneError(Exception):
    """Raised for voice-cloning-specific failures (missing ``soundfile``,
    undecodable audio) that callers should surface as a friendly WS error
    rather than a stack trace."""


# ── name / format / size validation (pure) ─────────────────────────────────


def is_name_format_valid(name: Any) -> bool:
    """Format-only check (ruling 9's regex), with no collision/deletion-state
    opinion -- the one gate every name arriving off the wire must pass before
    it's ever joined into a filesystem path via `voice_path()`."""
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def validate_name(name: Any, predefined: Iterable[str]) -> tuple[bool, str]:
    """Full name check: format (ruling 9) plus collision against the 26
    built-in predefined voice names."""
    if not is_name_format_valid(name):
        return False, "invalid name: use lowercase letters, digits, underscore, hyphen (1-32 chars)"
    if name in set(predefined):
        return False, "name taken by a built-in voice"
    return True, ""


def validate_extension(ext: Any) -> tuple[bool, str]:
    if not isinstance(ext, str) or not ext:
        return False, "missing file extension"
    normalized = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
    if normalized in ACCEPTED_EXTENSIONS:
        return True, ""
    if normalized in REJECTED_EXTENSIONS:
        return False, f"{normalized} isn't supported -- convert to WAV/MP3 first"
    return False, f"unsupported file extension: {normalized}"


def validate_size(size: Any) -> tuple[bool, str]:
    if isinstance(size, bool) or not isinstance(size, (int, float)) or size <= 0:
        return False, "invalid upload size"
    if size > MAX_UPLOAD_BYTES:
        return False, f"upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap"
    return True, ""


def check_delete_allowed(name: str, active_voice: Optional[str], predefined: Iterable[str]) -> tuple[bool, str]:
    """Pure form of ruling 10's delete rules -- testable without touching the
    sidecar dir or the model. ``active_voice`` is the TTS handler's current
    ``.voice`` (falsy when no handler is wired).

    Also the traversal gate: format-checks ``name`` the same way
    ``validate_name`` does, so a wire value like ``"../../x"`` is rejected
    here -- the one place both the delete path and ``_set_voice`` funnel
    through -- rather than reaching ``voice_path()`` unvalidated.
    """
    if not is_name_format_valid(name):
        return False, "invalid name"
    if name in set(predefined):
        return False, "can't delete a built-in voice"
    if active_voice and name == active_voice:
        return False, "switch to another voice first"
    return True, ""


# ── chunked-upload state machine (pure, no I/O) ─────────────────────────────


class UploadSession:
    """One client's in-flight upload buffer."""

    def __init__(self, name: str, ext: str, declared_size: Any) -> None:
        self.name = name
        self.ext = ext
        self.declared_size = declared_size
        self.finished = False
        self.data: bytes = b""
        self._buffer = bytearray()

    def add_chunk(self, raw: Any) -> tuple[bool, str]:
        if self.finished:
            return False, "upload already finished"
        if not isinstance(raw, (bytes, bytearray)):
            return False, "invalid chunk payload"
        if len(raw) > MAX_CHUNK_BYTES:
            return False, f"chunk exceeds the {MAX_CHUNK_BYTES} byte cap"
        if len(self._buffer) + len(raw) > MAX_UPLOAD_BYTES:
            return False, f"upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap"
        self._buffer.extend(raw)
        return True, ""

    def finish(self) -> bytes:
        self.finished = True
        self.data = bytes(self._buffer)
        return self.data


class UploadManager:
    """Per-client single-in-flight upload bookkeeping for the
    `voice_clone_begin`/`voice_clone_chunk`/`voice_clone_end` frames.

    Pure state machine -- no I/O, no model calls, no `speech_to_speech`/
    `pocket_tts`/`soundfile` imports. `websocket_streamer.py` owns one
    instance and keys sessions by client identity (`id(websocket)`); only
    structural checks (name format, extension, size) happen here -- the
    predefined-name collision and cloning-gate checks need the model and
    live in `BrainControl`, reached separately via `control_callback`.
    """

    def __init__(self) -> None:
        self._sessions: dict[Any, UploadSession] = {}

    def begin(self, client_key: Any, name: Any, ext: Any, size: Any) -> tuple[bool, str]:
        """Validate + start a new session. A new `begin` for the same
        `client_key` silently replaces (aborts) any prior in-flight upload,
        per protocol."""
        if not isinstance(name, str) or not _NAME_RE.match(name):
            return False, "invalid name: use lowercase letters, digits, underscore, hyphen (1-32 chars)"
        ok, error = validate_extension(ext)
        if not ok:
            return False, error
        ok, error = validate_size(size)
        if not ok:
            return False, error
        self._sessions[client_key] = UploadSession(name, ext, size)
        return True, ""

    def chunk(self, client_key: Any, raw: Any) -> tuple[bool, str, Optional[str]]:
        session = self._sessions.get(client_key)
        if session is None:
            return False, "no upload in progress", None
        ok, error = session.add_chunk(raw)
        if not ok:
            self._sessions.pop(client_key, None)
        return ok, error, session.name

    def end(self, client_key: Any) -> tuple[Optional[UploadSession], str]:
        session = self._sessions.pop(client_key, None)
        if session is None:
            return None, "no upload in progress"
        session.finish()
        return session, ""

    def abort(self, client_key: Any) -> None:
        """Drop any in-flight session for a client (e.g. begin rejected by
        BrainControl's semantic checks, or client disconnect)."""
        self._sessions.pop(client_key, None)


# ── audio normalization (lazy soundfile) ────────────────────────────────────


def normalize_to_wav(raw: bytes, ext: str) -> bytes:
    """Decode `raw` (any accepted format) and re-encode as 16-bit mono WAV
    bytes via `soundfile` (ruling 5 -- pocket's own WAV reader hard-codes
    int16 and would misdecode float/24-bit WAVs). Raises `VoiceCloneError`
    on a missing `soundfile` install or an undecodable file."""
    try:
        import soundfile as sf
    except ImportError as e:
        raise VoiceCloneError("voice cloning uploads require soundfile: pip install soundfile") from e

    import io
    import tempfile

    suffix = ext if ext.startswith(".") else f".{ext}"
    tmp_path = None
    try:
        # Extension-driven format detection, mirroring upstream's own upload
        # handling (`NamedTemporaryFile(suffix=<original extension>)`, §6).
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(raw)
            tmp_path = fh.name

        info = sf.info(tmp_path)
        if info.samplerate > 0 and info.frames / info.samplerate > MAX_DECODED_SECONDS:
            raise VoiceCloneError(f"recording exceeds the {MAX_DECODED_SECONDS}s cap ({ext})")

        data, samplerate = sf.read(tmp_path, dtype="float32", always_2d=True)
    except VoiceCloneError:
        raise
    except Exception as e:
        raise VoiceCloneError(f"couldn't decode uploaded audio ({ext}): {e}") from e
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]

    out = io.BytesIO()
    sf.write(out, mono, samplerate, subtype="PCM_16", format="WAV")
    return out.getvalue()


def atomic_export_state(state: Any, dest: Path, export_fn: Optional[Callable[[Any, Path], None]] = None) -> None:
    """Export `state` to `dest` atomically: write to a same-directory temp
    path that can never match the `*.safetensors` glob `list_custom_voices`
    scans, then `os.replace` it into place in one step.

    A failure at any point (`export_fn` raising, `os.replace` failing) always
    leaves `dest` untouched -- if `dest` already held a previous, successful
    build (ruling 9: re-uploading an existing name overwrites), that GOOD
    file survives a failed re-upload instead of being left half-written or
    deleted. The temp path is always cleaned up, success or failure.

    `export_fn` defaults to `pocket_tts.models.tts_model.export_model_state`
    (lazy import); injectable so this is testable without pocket_tts
    installed.
    """
    if export_fn is None:
        from pocket_tts.models.tts_model import export_model_state as export_fn

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_name(dest.name + ".tmp")
    try:
        export_fn(state, tmp_dest)
        os.replace(tmp_dest, dest)
    finally:
        if tmp_dest.exists():
            try:
                tmp_dest.unlink()
            except OSError:
                pass


# ── sidecar dir (list/delete) ────────────────────────────────────────────────


def voices_dir() -> Path:
    return Path(os.environ.get("VOICE_CLONE_DIR", _DEFAULT_VOICES_DIR)).expanduser()


def voice_path(name: str) -> Path:
    return voices_dir() / f"{name}.safetensors"


def list_custom_voices() -> list[str]:
    d = voices_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.safetensors"))


def delete_voice(name: str) -> tuple[bool, str]:
    path = voice_path(name)
    if not path.is_file():
        return False, f"no such custom voice: {name}"
    try:
        path.unlink()
    except OSError as e:
        return False, f"failed to delete {name}: {e}"
    return True, ""


# ── audition text (ruling 8) ─────────────────────────────────────────────────


def resolve_audition_text(name: str, raw: Optional[str] = None) -> Optional[str]:
    """`VOICE_AUDITION_TEXT` parsing (mirrors `voice_rules._parse`): unset/
    blank -> `DEFAULT_AUDITION_TEXT`; `"off"` (case-insensitive, stripped) ->
    `None` (disabled); any other non-blank string -> used verbatim. `{name}`
    is substituted in the resolved template. `raw` is injectable for tests;
    defaults to `os.environ.get("VOICE_AUDITION_TEXT")`."""
    if raw is None:
        raw = os.environ.get("VOICE_AUDITION_TEXT")
    if raw is None:
        template = DEFAULT_AUDITION_TEXT
    else:
        stripped = raw.strip()
        if not stripped:
            template = DEFAULT_AUDITION_TEXT
        elif stripped.lower() == "off":
            return None
        else:
            template = stripped
    try:
        return template.format(name=name)
    except (KeyError, IndexError, ValueError):
        return template
