"""Who holds the floor right now: per-client playback state plus the user's
speech state, as one small thread-safe object (floor-management slice 2).

Why it exists: the server knows when it SENT audio, but only the client knows
when the speaker is actually playing it. Clients report ``playback``
start/end/cut frames; the streamer feeds those, plus the VAD's
``speech_started`` / ``speech_stopped`` events and "audio sent" stamps, in
here. Later slices (session tape, announcement gate, duck/yield) read the same
object instead of each keeping their own idea of "is anyone talking".

Observe-only: nothing here changes what the pipeline does. Stdlib only. The
clock is injectable (``clock=``) so tests need no sleeping; the default is
``time.monotonic``.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Hashable


class FloorState:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # client id -> {"playing": bool, "start": float | None, "end": float | None}
        self._clients: dict[Hashable, dict] = {}
        self._user_speaking = False
        self._last_user_speech_end: float | None = None
        self._last_audio_sent: float | None = None

    # -- client playback (from the `playback` frames) ----------------------

    def playback_start(self, client: Hashable) -> None:
        with self._lock:
            rec = self._clients.setdefault(client, {"playing": False, "start": None, "end": None})
            rec["playing"] = True
            rec["start"] = self._clock()

    def playback_stop(self, client: Hashable) -> None:
        """An `end` or a `cut`: the client's speaker is no longer playing a reply."""
        with self._lock:
            rec = self._clients.setdefault(client, {"playing": False, "start": None, "end": None})
            rec["playing"] = False
            rec["end"] = self._clock()

    def is_playing(self, client: Hashable) -> bool:
        with self._lock:
            rec = self._clients.get(client)
            return bool(rec and rec["playing"])

    def playback_times(self, client: Hashable) -> tuple[float | None, float | None]:
        """(last start, last end) for one client; (None, None) if unknown."""
        with self._lock:
            rec = self._clients.get(client)
            return (rec["start"], rec["end"]) if rec else (None, None)

    def any_client_playing(self) -> bool:
        with self._lock:
            return any(rec["playing"] for rec in self._clients.values())

    def drop_client(self, client: Hashable) -> None:
        """Disconnect: forget that client, so a dead tab can never read as "still playing"."""
        with self._lock:
            self._clients.pop(client, None)

    # -- user speech and audio sent (from the send loop's events) ----------

    def user_speech_started(self) -> None:
        with self._lock:
            self._user_speaking = True

    def user_speech_stopped(self) -> None:
        with self._lock:
            self._user_speaking = False
            self._last_user_speech_end = self._clock()

    def audio_sent(self) -> None:
        with self._lock:
            self._last_audio_sent = self._clock()

    @property
    def user_speaking(self) -> bool:
        with self._lock:
            return self._user_speaking

    # -- queries ------------------------------------------------------------

    def mutual_quiet_s(self, now: float | None = None) -> float:
        """Seconds since neither side was talking. 0.0 while the user is
        speaking or any client is playing; otherwise the time since the latest
        of the user's last speech end, any client's last playback end, and the
        last audio the server sent. ``inf`` when none of those ever happened."""
        with self._lock:
            if now is None:
                now = self._clock()
            if self._user_speaking or any(rec["playing"] for rec in self._clients.values()):
                return 0.0
            stamps = [self._last_user_speech_end, self._last_audio_sent]
            stamps.extend(rec["end"] for rec in self._clients.values())
            known = [s for s in stamps if s is not None]
            if not known:
                return math.inf
            return max(0.0, now - max(known))
