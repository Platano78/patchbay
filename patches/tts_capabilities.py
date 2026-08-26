"""Capability discovery for the active TTS backend.

`brain_control.py` used to *know* pocket-tts's preset voice list by importing
`pocket_tts.utils.utils._ORIGINS_OF_PREDEFINED_VOICES` directly -- the only
place patchbay reached into a specific TTS package. Switch the backend (e.g.
to a remote qwen CustomVoice server) and that list is simply wrong: 26 names
the backend doesn't have, an unknown-voice request silently returning zero
bytes of audio instead of an error.

This module asks the ACTIVE backend instead, for exactly three questions --
`voices`, `can_clone`, `accepts_instructions` -- and nothing else. See
`docs/plans/tts-capability-seam_spec.md` for the wire facts this is built on.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# Same probe budget as BrainControl._resolve_model -- this is a settings-panel
# / per-request feature probe, not something a user is staring at a spinner for.
_PROBE_TIMEOUT_S = 3.0

# How long a successful probe result is trusted before the next call re-hits
# the network. Keyed per-backend (see `_cache` below), so a backend swap is
# never served a stale answer even mid-TTL.
_CACHE_TTL_S = 60.0

# Sentinel cache key for the local in-process backend -- it has no base_url,
# but still needs a cache slot (see `_select_prober`).
_LOCAL_KEY = "__local__"


@dataclass(frozen=True)
class TTSCapabilities:
    voices: list[str]
    can_clone: bool
    accepts_instructions: bool


# The only capabilities value ever handed back for a backend that has never
# once probed successfully -- see `_CacheEntry` below.
_EMPTY = TTSCapabilities(voices=[], can_clone=False, accepts_instructions=True)


@dataclass
class _CacheEntry:
    last_probe_at: float
    last_good: Optional[TTSCapabilities]


# Module-level (not per-BrainControl-instance) because both BrainControl's
# background reconcile thread and RemoteSpeechTTSHandler's own pipeline
# thread ask the same question about the same backend.
_lock = threading.Lock()
_cache: dict[str, _CacheEntry] = {}


def get_capabilities(handler: Any) -> TTSCapabilities:
    """The three capabilities of the TTS handler currently in force.

    Detected by feature (`hasattr`), never `isinstance` -- same drift-resistant
    style as `BrainControl._set_voice`, so a future backend that happens to
    grow the same handle still routes correctly without this file changing.
    """
    key, prober = _select_prober(handler)
    if key is None or prober is None:
        return _EMPTY

    now = time.monotonic()
    with _lock:
        entry = _cache.get(key)
        if entry is not None and (now - entry.last_probe_at) < _CACHE_TTL_S:
            return entry.last_good if entry.last_good is not None else _EMPTY

    try:
        caps: Optional[TTSCapabilities] = prober()
    except Exception as e:
        logger.warning("tts_capabilities: probe failed for %s: %s", key, e)
        caps = None

    with _lock:
        entry = _cache.get(key)
        # A failed probe keeps whatever the last SUCCESSFUL answer was --
        # never collapses to empty just because this one attempt errored.
        # The UI treats an empty voice list as "no TTS control at all" and
        # a transient network blip must not make the whole picker vanish
        # mid-conversation.
        last_good = caps if caps is not None else (entry.last_good if entry is not None else None)
        _cache[key] = _CacheEntry(last_probe_at=now, last_good=last_good)
        return last_good if last_good is not None else _EMPTY


def _select_prober(handler: Any) -> tuple[Optional[str], Optional[Callable[[], TTSCapabilities]]]:
    model = getattr(handler, "model", None)
    if hasattr(model, "get_state_for_audio_prompt"):
        return _LOCAL_KEY, _probe_local

    base_url = getattr(handler, "base_url", None)
    if base_url:
        return base_url, lambda: _probe_remote(base_url)

    return None, None


def _probe_local() -> TTSCapabilities:
    """The pocket adapter -- the one place left in patchbay that knows
    pocket_tts by name. Empty voice list (and a warning, not a raise) on any
    import failure: pocket_tts not installed, or an upstream rename."""
    try:
        from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES

        voices = sorted(_ORIGINS_OF_PREDEFINED_VOICES.keys())
    except Exception as e:
        logger.warning("tts_capabilities: predefined voice list unavailable: %s", e)
        voices = []
    return TTSCapabilities(voices=voices, can_clone=True, accepts_instructions=False)


def _probe_remote(base_url: str) -> TTSCapabilities:
    """`GET {base_url}/v1/audio/voices`. Raises on any transport/HTTP failure
    -- the caller (`get_capabilities`) is what decides whether that means
    "serve the last-good answer" or "there is no last-good answer yet"."""
    resp = httpx.get(f"{base_url.rstrip('/')}/v1/audio/voices", timeout=_PROBE_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    body = data if isinstance(data, dict) else {}

    return TTSCapabilities(
        voices=_parse_voices(body.get("voices")),
        # Both flags are OPTIONAL on the wire -- see spec R3 for why each
        # default is the value it is, not a guess.
        can_clone=_parse_bool(body.get("can_clone"), default=False),
        accepts_instructions=_parse_bool(body.get("accepts_instructions"), default=True),
    )


def _parse_voices(raw: Any) -> list[str]:
    """Liberal in what it accepts: both `[{"name": "..."}]` (the documented
    shape, `kind` ignored -- patchbay has no use for speaker-vs-cloned here)
    and a bare `["jean", "alba"]` list parse. Entries with no usable name are
    dropped rather than raising. Sorted and de-duped."""
    if not isinstance(raw, list):
        return []
    names: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("name")
        else:
            name = None
        if isinstance(name, str) and name:
            names.add(name)
    return sorted(names)


def _parse_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is not None:
        logger.warning("tts_capabilities: non-bool capability flag %r -- using default %s", value, default)
    return default
