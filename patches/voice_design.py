"""On-the-fly voice design and remote voice cloning, over the speech server's
`/v1/voices/*` API (the voicegate proxy Patchbay already uses for speech).

    POST   /v1/voices/design                  {"description", "name"?}  -> {"voice": "designed:<id>", "id", "name", "seconds"}
    POST   /v1/voices/clone                   multipart file (+ name, reference_text) -> same shape
    POST   /v1/voices/designed/<id>/rename    {"name"}                  -> {"voice", "id", "name"}
    DELETE /v1/voices/designed/<id>                                     -> 204

A designed or cloned voice is addressed like a preset (`"voice": "designed:<id>"`
on /v1/audio/speech). This module is the HTTP + filler-rendering half; the
voice switch itself stays in `BrainControl` (it owns `_set_voice`).

`VOICE_DESIGN_TOOLS=on|off` (default on) gates the LLM tools and the remote
clone/delete UI paths together.

Stdlib + httpx at import time (numpy/scipy only when a filler is rendered), and
no `speech_to_speech` imports at module level, so `scripts/render_fillers.py`
can load it by path.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from math import gcd
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

DESIGNED_PREFIX = "designed:"
# The only place a colon is accepted in a voice name: this exact prefix, then a
# plain id. Anything else goes through voice_clone's strict name gate as before.
_DESIGNED_RE = re.compile(r"designed:[A-Za-z0-9_-]{1,64}")

DESIGN_TIMEOUT_S = 45.0
# The LLM tool reports a timeout at this point (voice_tools._VOICE_DESIGN_TIMEOUT_S);
# a design finishing after it must not switch the voice.
TOOL_DEADLINE_S = 50.0
CLONE_TIMEOUT_S = 60.0
RENAME_TIMEOUT_S = 10.0
DESIGN_DESCRIPTION_MIN, DESIGN_DESCRIPTION_MAX = 3, 300
SAVE_NAME_MAX = 40

MIN_CLIP_S, MAX_CLIP_S = 0.15, 3.0

_OFF = {"off", "0", "false", "no"}


class VoiceDesignError(Exception):
    """A design/clone/rename/delete call failed; the message is plain text
    safe to hand to the LLM or the UI."""


def enabled() -> bool:
    return os.environ.get("VOICE_DESIGN_TOOLS", "on").strip().lower() not in _OFF


def is_designed_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_DESIGNED_RE.fullmatch(name))


def designed_id(name: str) -> str:
    return name[len(DESIGNED_PREFIX):]


def is_remote_handler(handler: Any) -> bool:
    """The remote-speech backend, detected by feature like BrainControl._set_voice
    does: a settable `voice`, a `base_url`, and no pocket-style voice-state API."""
    if handler is None:
        return False
    if hasattr(getattr(handler, "model", None), "get_state_for_audio_prompt"):
        return False
    return bool(getattr(handler, "base_url", None)) and hasattr(handler, "voice")


# ── HTTP ─────────────────────────────────────────────────────────────────────


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else err
        if isinstance(msg, str) and msg:
            return msg[:200]
    except Exception:
        pass
    return f"HTTP {resp.status_code}"


def _checked_voice(resp: httpx.Response, what: str) -> dict[str, Any]:
    if resp.status_code != 200:
        raise VoiceDesignError(f"the voice server refused the {what} ({_error_text(resp)})")
    try:
        body = resp.json()
    except ValueError:
        body = None
    voice = body.get("voice") if isinstance(body, dict) else None
    if not is_designed_name(voice):
        raise VoiceDesignError(f"the voice server's {what} answer was not usable")
    return body


def _post(base_url: str, path: str, timeout: float, what: str, **kw: Any) -> httpx.Response:
    try:
        return httpx.post(f"{base_url.rstrip('/')}{path}", timeout=timeout, **kw)
    except httpx.TimeoutException as e:
        raise VoiceDesignError(f"the {what} timed out") from e
    except httpx.HTTPError as e:
        raise VoiceDesignError(f"the voice server is unreachable ({type(e).__name__})") from e


def clean_description(description: Any) -> str:
    text = " ".join(str(description or "").split())
    if len(text) < DESIGN_DESCRIPTION_MIN:
        raise VoiceDesignError("the voice description is too short")
    if len(text) > DESIGN_DESCRIPTION_MAX:
        text = text[:DESIGN_DESCRIPTION_MAX].rsplit(" ", 1)[0] or text[:DESIGN_DESCRIPTION_MAX]
    return text


def design(base_url: str, description: str, name: Optional[str] = None, timeout: float = DESIGN_TIMEOUT_S) -> dict[str, Any]:
    payload: dict[str, Any] = {"description": clean_description(description)}
    if name:
        payload["name"] = name
    return _checked_voice(_post(base_url, "/v1/voices/design", timeout, "voice design", json=payload), "design")


def clone(base_url: str, wav: bytes, name: str, reference_text: Optional[str] = None, timeout: float = CLONE_TIMEOUT_S) -> dict[str, Any]:
    data = {"name": name}
    if reference_text:
        data["reference_text"] = reference_text
    resp = _post(
        base_url, "/v1/voices/clone", timeout, "voice clone",
        files={"file": ("sample.wav", wav, "audio/wav")}, data=data,
    )
    return _checked_voice(resp, "clone")


def rename(base_url: str, voice: str, name: str, timeout: float = RENAME_TIMEOUT_S) -> dict[str, Any]:
    if not is_designed_name(voice):
        raise VoiceDesignError("that voice cannot be renamed")
    resp = _post(base_url, f"/v1/voices/designed/{designed_id(voice)}/rename", timeout, "rename", json={"name": name})
    return _checked_voice(resp, "rename")


def delete(base_url: str, voice: str, timeout: float = RENAME_TIMEOUT_S) -> None:
    if not is_designed_name(voice):
        raise VoiceDesignError("that voice cannot be deleted")
    try:
        resp = httpx.delete(f"{base_url.rstrip('/')}/v1/voices/designed/{designed_id(voice)}", timeout=timeout)
    except httpx.HTTPError as e:
        raise VoiceDesignError(f"the voice server is unreachable ({type(e).__name__})") from e
    if resp.status_code not in (200, 204):
        raise VoiceDesignError(f"the voice server refused the delete ({_error_text(resp)})")


def clean_save_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text or len(text) > SAVE_NAME_MAX or not text.isprintable():
        raise VoiceDesignError(f"a voice name must be 1 to {SAVE_NAME_MAX} plain characters")
    return text


# ── filler rendering (shared with scripts/render_fillers.py) ─────────────────


def _filler_clips():
    try:
        from speech_to_speech import filler_clips

        return filler_clips
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location("filler_clips_for_design", Path(__file__).with_name("filler_clips.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def trim_silence(x, rate: int):
    """Cut leading/trailing samples below ~3% of the clip's peak, keeping 20 ms of margin."""
    import numpy as np

    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak <= 0:
        return x[:0]
    loud = np.nonzero(np.abs(x) > max(0.03 * peak, 100))[0]
    if loud.size == 0:
        return x[:0]
    pad = int(0.02 * rate)
    return x[max(0, loud[0] - pad) : loud[-1] + pad + 1]


def to_clip(pcm: bytes, source_rate: int, rate: int) -> bytes:
    import numpy as np

    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    if source_rate != rate:
        from scipy.signal import resample_poly

        g = gcd(source_rate, rate)
        x = resample_poly(x, rate // g, source_rate // g)
    x = trim_silence(x, rate)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def active_phrases() -> list[str]:
    """The tool + turn filler pools the running service uses, de-duplicated."""
    from speech_to_speech.LLM import lm_output_processor as lmp

    seen: dict[str, None] = {}
    for p in (*lmp._TOOL_FILLERS, *lmp._TURN_FILLERS):
        seen.setdefault(p)
    return list(seen)


def render_voice_fillers(
    base_url: str,
    voice: str,
    *,
    model: str = "voxcpm2",
    rate: int = 16000,
    source_rate: int = 48000,
    phrases: Optional[Iterable[str]] = None,
    root: Optional[Path] = None,
    timeout: float = 60.0,
) -> list[Path]:
    """Render every filler phrase for `voice` into the filler cache, skipping
    clips that exist. Returns the files written. Per-phrase failures are logged
    and skipped (a voice with fewer fillers just plays fewer)."""
    fc = _filler_clips()
    pool = list(phrases) if phrases is not None else active_phrases()
    written: list[Path] = []
    for phrase in pool:
        path = fc.clip_path(voice, phrase, root)
        if path.exists():
            continue
        try:
            resp = httpx.post(
                f"{base_url.rstrip('/')}/v1/audio/speech",
                json={"model": model, "input": phrase, "voice": voice, "response_format": "pcm"},
                headers={"X-Voicebox-Priority": "interactive"},
                timeout=timeout,
            )
            resp.raise_for_status()
            clip = to_clip(resp.content, source_rate, rate)
            secs = len(clip) / 2 / rate
            if not MIN_CLIP_S <= secs <= MAX_CLIP_S:
                raise ValueError(f"{secs:.2f}s is outside {MIN_CLIP_S}-{MAX_CLIP_S}s")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(clip)
            tmp.replace(path)
            written.append(path)
        except Exception as e:
            logger.warning("voice_design: filler %r for %s not rendered: %s", phrase, voice, e)
    logger.info("voice_design: %d filler clip(s) rendered for %s", len(written), voice)
    return written


_RENDERING: set[str] = set()
_RENDERING_LOCK = threading.Lock()


def render_fillers_async(handler: Any, voice: str) -> Optional[threading.Thread]:
    """Render `voice`'s fillers on a daemon thread so the switch is not blocked.
    One render per voice at a time; never raises."""
    base_url = getattr(handler, "base_url", None)
    if not base_url:
        return None
    with _RENDERING_LOCK:
        if voice in _RENDERING:
            return None
        _RENDERING.add(voice)

    def work() -> None:
        try:
            render_voice_fillers(
                base_url,
                voice,
                model=getattr(handler, "model", None) or "voxcpm2",
                rate=int(getattr(handler, "sample_rate", 16000)),
                source_rate=int(getattr(handler, "source_sample_rate", 48000)),
            )
        except Exception:
            logger.warning("voice_design: filler rendering for %s failed", voice, exc_info=True)
        finally:
            with _RENDERING_LOCK:
                _RENDERING.discard(voice)

    t = threading.Thread(target=work, name="voice-design-fillers", daemon=True)
    t.start()
    return t


def move_fillers(old_voice: str, new_voice: str, root: Optional[Path] = None) -> bool:
    """After a rename the voice id changes: carry already-rendered fillers over."""
    fc = _filler_clips()
    base = root or fc.cache_root()
    src, dst = base / fc.voice_dirname(old_voice), base / fc.voice_dirname(new_voice)
    if not src.is_dir() or dst.exists():
        return False
    try:
        src.rename(dst)
        return True
    except OSError:
        return False
