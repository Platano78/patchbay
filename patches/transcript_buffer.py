"""Server-side transcript replay buffer: keeps the last N completed turns so
a client that (re)joins mid-session (phone screen lock, tab backgrounded,
reload) can seed its history rail instead of showing an empty one, even
though a client that stayed connected already has the whole conversation.

Mirrors the client-side pairing logic in webclient/index.html's
`handleEvent` switch exactly, one event behind: `transcription_completed`
opens a pending user turn (closing any prior unanswered one as
"-- (interrupted)", i.e. barge-in); the next non-tool-only `assistant_text`
completes it. `feed()` is fed the same broadcast payloads clients receive,
so the two stay in lockstep by construction.

Dependency-light like `voice_rules.py`/`phone_context.py`: no
`speech_to_speech` import, operates on plain dicts, importable/testable
standalone with nothing installed.

Single-writer, single-reader within one asyncio event loop:
`websocket_streamer.py`'s `_send_loop` (feed) and `_handle_client` (replay
on join) both run in the streamer's one event loop thread, so a
`collections.deque(maxlen=N)` needs no lock -- do not add a
`threading.Lock` here.
"""

from __future__ import annotations

import os
from collections import deque
from typing import Any, Optional

DEFAULT_CAP = 50


def _parse_enabled(raw: Optional[str]) -> bool:
    """`VOICE_HISTORY_REPLAY` parsing: unset/blank -> enabled; `"off"`
    (case-insensitive, stripped) -> disabled; any other value -> enabled.
    Never raises."""
    if raw is None:
        return True
    stripped = raw.strip()
    if not stripped:
        return True
    return stripped.lower() != "off"


def _parse_cap(raw: Optional[str]) -> int:
    """`VOICE_HISTORY_REPLAY_TURNS` parsing: unset/blank/malformed ->
    `DEFAULT_CAP`; any parseable int (including <= 0) is returned as-is --
    the caller treats <= 0 as disabled. Never raises."""
    if raw is None:
        return DEFAULT_CAP
    stripped = raw.strip()
    if not stripped:
        return DEFAULT_CAP
    try:
        return int(stripped)
    except ValueError:
        return DEFAULT_CAP


class TranscriptBuffer:
    """Holds the last N completed turns (`{"user", "assistant", "ts"}`) and
    replays them to a newly-joined client. Reads its env config once, at
    construction time."""

    def __init__(self) -> None:
        cap = _parse_cap(os.environ.get("VOICE_HISTORY_REPLAY_TURNS"))
        self._enabled = cap > 0 and _parse_enabled(os.environ.get("VOICE_HISTORY_REPLAY"))
        self._turns: deque[dict[str, Any]] = deque(maxlen=cap if cap > 0 else 1)
        self._pending: Optional[dict[str, Any]] = None

    def feed(self, payload: dict[str, Any], now: float) -> None:
        """Mirror the client's `transcription_completed` / `assistant_text`
        pairing. `now` is the caller's `time.time()`, injected so this needs
        no clock patching in tests."""
        if not self._enabled:
            return

        ev_type = payload.get("type")

        if ev_type == "transcription_completed":
            transcript = payload.get("transcript")
            if not isinstance(transcript, str) or not transcript.strip():
                return  # ignore empty/blank transcript entirely
            if self._pending is not None:
                # Barge-in: a new turn started before the previous one got its reply.
                self._turns.append(
                    {"user": self._pending["user"], "assistant": "— (interrupted)", "ts": self._pending["ts"]}
                )
            self._pending = {"user": transcript, "ts": now}
            return

        if ev_type == "assistant_text":
            text = payload.get("text") or ""
            tools = payload.get("tools")
            is_tool_only = not text and isinstance(tools, list) and len(tools) > 0
            if is_tool_only:
                return
            if self._pending is not None:
                self._turns.append({"user": self._pending["user"], "assistant": text or "—", "ts": self._pending["ts"]})
                self._pending = None
            return

        # any other event type: ignore

    def replay_payload(self) -> Optional[dict[str, Any]]:
        """Returns the `history_replay` frame to send a joining client, or
        `None` when disabled or there is nothing completed yet. Excludes the
        still-pending (unanswered) turn. Entries are chronological
        (oldest-first) -- the deque is already in that order since turns are
        only ever appended, never inserted."""
        if not self._enabled or not self._turns:
            return None
        return {"type": "history_replay", "entries": list(self._turns)}
