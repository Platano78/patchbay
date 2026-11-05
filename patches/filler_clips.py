"""Pre-rendered filler clips and the turn-end filler timer.

Extends the existing filler system in ``LLM/lm_output_processor.py``
(``_pick_filler`` / ``VOICE_TOOL_FILLERS``); it adds no phrase list or picker of
its own.

Two jobs:

1. CACHE. ``scripts/render_fillers.py`` renders each filler phrase once, per
   persona voice, to ``<VOICE_FILLER_DIR>/<voice>/<slug>.pcm`` (raw s16le mono at
   the pipeline output rate). When a phrase is picked and a clip exists for the
   TTS handler's current voice, the clip goes straight onto the audio output
   queue (instant). No clip, no sink, no voice: the caller falls back to today's
   path (a ``TTSInput`` through the normal TTS). With no cache directory the
   behaviour is exactly what it was before this module existed.

2. TURN FILLER (``VOICE_TURN_FILLER=on``, default off). When a user turn ends
   and no reply audio has been enqueued ``VOICE_TURN_FILLER_DELAY_MS`` later
   (default 600), play one filler from the pool. Only for turns that are
   actually being answered (a non-blank transcript arrived), only if a cached
   clip exists for the voice, and never after reply audio started, after a tool
   filler already played this turn, or after the user spoke again / barged in.
   Clips go to the audio queue only: nothing here touches chat history.

Stdlib only, and no ``speech_to_speech`` imports, so ``scripts/render_fillers.py``
can load it by path.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_DELAY_MS = 600
# How long after a turn ends we keep waiting for its transcript before giving up.
TRANSCRIPT_WAIT_CAP_S = 3.0
# A tool-call filler is skipped if a turn filler played this recently.
TOOL_AFTER_TURN_FILLER_S = 2.0
_MAX_CLIP_BYTES = 2 * 48000 * 3  # 3 s at 48 kHz: a filler is never longer than this
_POLL_S = 0.1


class FillerClip(bytes):
    """Marker type: raw s16le audio of a filler. The websocket send loop plays it
    directly and does not count it as reply audio."""


class TurnFillerClip(FillerClip):
    """A FillerClip played because a turn ended with no reply yet (not because of a
    tool call): clients that opted out of the turn-end filler do not get it."""


def turn_filler_enabled() -> bool:
    return os.environ.get("VOICE_TURN_FILLER", "").strip().lower() in _TRUTHY


def turn_filler_delay_s() -> float:
    raw = os.environ.get("VOICE_TURN_FILLER_DELAY_MS", "").strip()
    try:
        ms = int(raw) if raw else DEFAULT_DELAY_MS
    except ValueError:
        ms = DEFAULT_DELAY_MS
    return max(0, ms) / 1000.0


def cache_root() -> Path:
    raw = os.environ.get("VOICE_FILLER_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "patchbay" / "fillers"


def voice_dirname(voice: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", voice.strip()) or "_"


def clip_name(phrase: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")[:48] or "clip"
    return f"{slug}.pcm"


def clip_path(voice: str, phrase: str, root: Path | None = None) -> Path:
    return (root or cache_root()) / voice_dirname(voice) / clip_name(phrase)


def load_clip(voice: str | None, phrase: str | None) -> FillerClip | None:
    """The cached clip for (voice, phrase), or None when absent or unusable."""
    if not voice or not phrase:
        return None
    try:
        data = clip_path(voice, phrase).read_bytes()
    except OSError:
        return None
    if not data or len(data) % 2 or len(data) > _MAX_CLIP_BYTES:
        logger.warning("filler clip %s ignored: %d bytes is not a usable clip", clip_path(voice, phrase), len(data))
        return None
    return FillerClip(data)


def _is_reply_audio(item: Any) -> bool:
    if isinstance(item, FillerClip):
        return False
    if hasattr(item, "audio"):  # AudioOutput
        return True
    if hasattr(item, "tobytes"):  # ndarray
        return True
    return isinstance(item, bytes) and len(item) > 1024  # control sentinels are short


class _Turn:
    __slots__ = ("ended_at", "audio_seen", "transcript_seen", "timer")

    def __init__(self, ended_at: float) -> None:
        self.ended_at = ended_at
        self.audio_seen = False
        self.transcript_seen = False
        self.timer: threading.Timer | None = None


class FillerClips:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._sink: Any = None
        self._voice_getter: Callable[[], str | None] | None = None
        self._picker: Callable[[], str | None] | None = None
        self._turn: _Turn | None = None
        self._last_filler_at = float("-inf")
        self._last_turn_filler_at = float("-inf")

    # -- wiring ---------------------------------------------------------
    def attach_sink(self, output_queue: Any) -> None:
        self._sink = output_queue

    def set_voice_getter(self, getter: Callable[[], str | None]) -> None:
        self._voice_getter = getter

    def set_turn_picker(self, picker: Callable[[], str | None]) -> None:
        self._picker = picker

    # -- cache path (tool fillers and turn fillers) ---------------------
    def _current_voice(self) -> str | None:
        try:
            return self._voice_getter() if self._voice_getter else None
        except Exception:
            logger.debug("filler voice getter failed", exc_info=True)
            return None

    def play_cached(self, phrase: str, kind: type[FillerClip] = FillerClip) -> bool:
        """Enqueue the cached clip for ``phrase`` in the current voice. True if it
        was enqueued (the caller then skips its TTS fallback)."""
        sink = self._sink
        if sink is None:
            return False
        clip = load_clip(self._current_voice(), phrase)
        if clip is None:
            return False
        sink.put(kind(clip))
        self.note_filler()
        return True

    def note_filler(self) -> None:
        """A filler was played or queued (clip or TTS): the turn filler stands down."""
        self._last_filler_at = self._clock()

    def turn_filler_just_played(self) -> bool:
        return self._clock() - self._last_turn_filler_at < TOOL_AFTER_TURN_FILLER_S

    def should_drop_filler(self, clip: Any) -> bool:
        """Send-loop check at dequeue: a turn filler is dropped if reply audio for its turn
        was already sent (it lost the race onto the queue) or its turn is gone."""
        if not isinstance(clip, TurnFillerClip):
            return False
        turn = self._turn
        return turn is None or turn.audio_seen

    # -- turn filler ----------------------------------------------------
    def on_turn_end(self) -> None:
        """The user's turn ended (VAD final; PTT release arrives the same way)."""
        self.cancel()
        if not turn_filler_enabled():
            return
        turn = _Turn(self._clock())
        with self._lock:
            self._turn = turn
        self._schedule(turn, turn_filler_delay_s())

    def cancel(self) -> None:
        """New speech or a barge-in: forget the pending turn and pull any queued clip."""
        with self._lock:
            turn, self._turn = self._turn, None
        if turn is not None and turn.timer is not None:
            turn.timer.cancel()
        self._drop_queued_clips()

    def note_transcript(self, text: Any) -> None:
        turn = self._turn
        if turn is not None and isinstance(text, str) and text.strip():
            turn.transcript_seen = True

    def note_reply_audio(self) -> None:
        turn = self._turn
        if turn is not None:
            turn.audio_seen = True

    def _schedule(self, turn: _Turn, delay_s: float) -> None:
        timer = threading.Timer(delay_s, self._fire, args=(turn,))
        timer.daemon = True
        turn.timer = timer
        timer.start()

    def _drop_queued_clips(self) -> None:
        q = self._sink
        if q is None or not hasattr(q, "mutex") or not hasattr(q, "queue"):
            return
        with q.mutex:
            kept = [i for i in q.queue if not isinstance(i, FillerClip)]
            if len(kept) != len(q.queue):
                q.queue.clear()
                q.queue.extend(kept)
                q.not_full.notify_all()

    def _reply_audio_queued(self) -> bool:
        q = self._sink
        if q is None or not hasattr(q, "mutex") or not hasattr(q, "queue"):
            return False
        with q.mutex:
            return any(_is_reply_audio(i) for i in q.queue)

    def _fire(self, turn: _Turn) -> None:
        try:
            self._fire_inner(turn)
        except Exception:
            logger.exception("turn filler failed")

    def _fire_inner(self, turn: _Turn) -> None:
        if self._turn is not turn:
            return  # superseded: new speech, barge-in, or a newer turn
        if turn.audio_seen or self._reply_audio_queued():
            return
        if not turn.transcript_seen:
            # Not known to be answered (yet). Wait for the transcript, but not forever:
            # an empty/dropped turn never gets a reply and must not get a filler.
            if self._clock() - turn.ended_at < TRANSCRIPT_WAIT_CAP_S:
                self._schedule(turn, _POLL_S)
            return
        if self._last_filler_at >= turn.ended_at:
            return  # a tool filler already played or is queued this turn
        picker = self._picker
        phrase = picker() if picker else None
        if not phrase:
            return
        with self._lock:
            if self._turn is not turn:
                return
        if self.play_cached(phrase, TurnFillerClip):
            self._last_turn_filler_at = self._clock()
            logger.info("TURN_FILLER phrase=%r voice=%r", phrase, self._current_voice())


FILLERS = FillerClips()

attach_sink = FILLERS.attach_sink
set_voice_getter = FILLERS.set_voice_getter
set_turn_picker = FILLERS.set_turn_picker
play_cached = FILLERS.play_cached
note_filler = FILLERS.note_filler
turn_filler_just_played = FILLERS.turn_filler_just_played
on_turn_end = FILLERS.on_turn_end
cancel = FILLERS.cancel
note_transcript = FILLERS.note_transcript
note_reply_audio = FILLERS.note_reply_audio
should_drop_filler = FILLERS.should_drop_filler
