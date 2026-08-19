from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Optional

from pathlib import Path

import httpx
from openai import OpenAI

from speech_to_speech.LLM.base_openai_compatible_language_model import BaseOpenAICompatibleHandler
from speech_to_speech import voice_clone
from speech_to_speech import voice_tools
# brain_discovery is dependency-light by design (stdlib + httpx only, no
# speech_to_speech.* imports of its own -- see its docstring) so it can run
# standalone as a CLI before a pipeline exists to configure. That means the
# import direction is one-way: brain_discovery must never import FROM this
# module. `_extract_model_ids`/`MAX_PROBED_MODEL_IDS` used to live here and
# moved there so both this module's `/models` probe and discovery's own
# probing share one parser instead of two that could drift.
from speech_to_speech import brain_discovery
from speech_to_speech.brain_discovery import _extract_model_ids, MAX_PROBED_MODEL_IDS  # noqa: F401 -- re-exported for existing brain_control._extract_model_ids call sites/tests

logger = logging.getLogger(__name__)

# Same ruling-1 placement as voice_clone's `voices/` sidecar: outside the
# package, next to brains.json, so a `pip install --upgrade` can't erase it.
_DEFAULT_PERSONA_FILE = "~/speech-to-speech/persona.json"

# The persona goes into the system message of every LLM request. The cap is a
# sanity bound, not a policy -- it's free text the user is deliberately writing.
PERSONA_MAX_CHARS = 8000

# Newline/tab/CR are legitimate in a multi-line persona; every other C0 control
# character (and DEL) is a paste accident or a smuggled control sequence.
_ALLOWED_CONTROL_CHARS = "\n\r\t"

# Per-brain model override sidecar (ruling 1, 2026-07-19): a brains.json
# entry's `model` is fixed at edit time, but an endpoint may serve many
# models (NVIDIA NIM serves hundreds; a llama.cpp router a handful) -- this
# lets the panel pick one per brain, persisted, without editing brains.json.
# A model id is a single line of free text, unlike the multi-line persona, so
# no control character is allowed through (not even newline/tab).
MODEL_OVERRIDE_MAX_CHARS = 200

_MODEL_OVERRIDES_FILENAME = "model_overrides.json"

# Accepted values for a brain's optional `reasoning_effort` (brains.json).
_VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})

# Shipped per-brain starting points. These are OFFERED, never auto-applied: a
# preset only takes effect for a brain the user explicitly put in preset mode,
# because silently preferring one over a global persona the user typed would
# override their own words.
#
# Each preset states the lane's PURPOSE -- what this lane is for in the
# cockpit -- never a claim about what any particular model cannot do. Lanes
# are roles; the model behind a lane varies per deployment. (User ruling
# 2026-07-19.)
BRAIN_PRESETS: dict[str, str] = {
    # The always-on lane: whatever fast local model answers by default.
    "coder": (
        "You are the always-on lane of this cockpit: the brain that answers "
        "instantly and drives tools. Answer in one or two spoken sentences, "
        "then stop. When a request needs a tool, call it straight away instead "
        "of talking through what you might do. "
        "Everything you say is spoken aloud, so use no markdown, lists, or code blocks."
    ),
    # The desktop lane: whatever model the user has loaded locally. The "stay
    # full late in conversation" line is a mitigation phrased as instruction --
    # loaded desktop models have been observed getting terser as history grows.
    "local": (
        "You are the desktop lane: whatever model the user has loaded on their "
        "own machine. Keep your answers as full late in a long conversation as "
        "at the start -- do not get terser as the history grows. "
        "Think it through, but say only the conclusion out loud. "
        "Everything you say is spoken aloud, so use no markdown, lists, or code blocks."
    ),
    # The escalation lane: a hosted model behind any OpenAI-compatible endpoint,
    # reached for questions worth the trip.
    "frontier": (
        "You are the frontier lane: a hosted model reached for the questions "
        "worth escalating. Give the considered answer rather than the fast "
        "one, in a few spoken sentences. "
        "Everything you say is spoken aloud, so use no markdown, lists, or code blocks."
    ),
    # Not a raw model: an agent behind a shim, with its own planning, loops and
    # memory, which may work for minutes and report back over its own channel.
    # Talking to it is delegation, so a persona tuned for snappy voice replies is
    # actively wrong here (council 3-0, 2026-07-03: the cockpit is not an agent).
    "hermes": (
        "You are the voice front end to Hermes, an autonomous agent with its own "
        "planning, tools and memory. Work sent here is delegated, not answered on "
        "the spot. Say out loud what you are handing off and that it is running, "
        "then stop -- a task may take minutes and report back separately. "
        "Do not attempt the work yourself in this reply, and never invent a result "
        "for something still in progress. "
        "Everything you say is spoken aloud, so use no markdown, lists, or code blocks."
    ),
}

# Resolution tiers, most specific first. Reported to the UI as `resolved_from`.
TIER_BRAIN_CUSTOM = "brain_custom"
TIER_BRAIN_PRESET = "brain_preset"
TIER_GLOBAL = "global"
TIER_DEFAULT = "default"

# How often the background reachability sweep (kicked from config_get) may
# start, at most. Not a request rate limit -- opening the settings panel
# repeatedly must not turn into a serial probe storm against every brain.
_DEFAULT_PROBE_DEBOUNCE_S = 20.0


def _env_seconds(name: str, default: float) -> float:
    """Read a numeric env var, falling back to `default` on anything
    unparseable rather than raising -- same fail-open contract as
    echo_gate._env_number (90992c7): a hand-edited systemd drop-in with a
    typo in this value must never crash the assistant at startup."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using the default %r", name, raw, default)
        return default


def persona_path() -> Path:
    return Path(os.environ.get("VOICE_PERSONA_FILE", _DEFAULT_PERSONA_FILE)).expanduser()


def _has_control_chars(text: str, allowed: str = "") -> bool:
    """True if `text` holds a C0 control char or DEL that isn't in `allowed`.

    A persona allows newline/tab/CR (it's prose the user typed into a
    textarea); a model id allows none. That difference is the only reason the
    two validators below aren't identical.
    """
    return any((ord(ch) < 32 and ch not in allowed) or ord(ch) == 127 for ch in text)


def validate_persona(text: Any) -> tuple[bool, str]:
    """Sanity-bound a user-submitted persona. Empty is valid -- it means
    "restore the default" upstream, not "no system prompt"."""
    if not isinstance(text, str):
        return False, "persona must be text"
    if len(text) > PERSONA_MAX_CHARS:
        return False, f"persona exceeds the {PERSONA_MAX_CHARS} character cap"
    if _has_control_chars(text, _ALLOWED_CONTROL_CHARS):
        return False, "persona contains control characters"
    return True, ""


def empty_persona_store() -> dict[str, Any]:
    return {"version": 2, "global": "", "brains": {}}


def _sanitize_brain_entry(raw: Any) -> Optional[dict[str, Any]]:
    """Normalize one stored per-brain entry, or None if it's unusable.

    Tolerates a bare string (an override written by hand) as custom text.
    Anything that fails validation is dropped rather than raising -- one bad
    brain entry must not cost the user their other brains' personas.
    """
    if isinstance(raw, str):
        raw = {"mode": "custom", "text": raw}
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    if mode == "preset":
        return {"mode": "preset"}
    if mode != "custom":
        return None
    text = raw.get("text")
    ok, _ = validate_persona(text)
    if not ok or not text:
        return None
    return {"mode": "custom", "text": text}


def load_persona_store() -> dict[str, Any]:
    """Read the persisted persona store, always returning a usable dict.

    Fail-safe by construction: this runs at pipeline startup in a live voice
    assistant, so a missing/empty/unreadable/corrupt file logs and yields an
    empty store (everything falls back to the CLI default) rather than raising.
    Individual malformed fields are dropped, not fatal.

    Reads the v1 global-only shape (`{"version": 1, "persona": "..."}`) as a
    global persona, so a file written before per-brain personas existed keeps
    working.
    """
    path = persona_path()
    store = empty_persona_store()
    try:
        with open(path, "r") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return store
    except (OSError, ValueError) as e:
        logger.warning("BrainControl: ignoring unreadable persona file %s: %s", path, e)
        return store

    if not isinstance(payload, dict):
        logger.warning("BrainControl: ignoring malformed persona file %s: not an object", path)
        return store

    # v1: a single global persona under `persona`. v2 keeps it under `global`.
    raw_global = payload.get("global", payload.get("persona"))
    if raw_global:
        ok, error = validate_persona(raw_global)
        if ok:
            store["global"] = raw_global
        else:
            logger.warning("BrainControl: dropping global persona from %s: %s", path, error)

    raw_brains = payload.get("brains")
    if isinstance(raw_brains, dict):
        for name, raw_entry in raw_brains.items():
            entry = _sanitize_brain_entry(raw_entry) if isinstance(name, str) else None
            if entry is None:
                logger.warning("BrainControl: dropping unusable persona entry for brain %r in %s", name, path)
                continue
            store["brains"][name] = entry
    elif raw_brains is not None:
        logger.warning("BrainControl: ignoring malformed `brains` in %s: not an object", path)

    return store


def _save_sidecar(path: Path, payload: dict[str, Any], empty: bool, label: str) -> None:
    """Write `payload` to `path` as JSON, or delete the file when `empty`.

    Atomic (same-directory temp file + `os.replace`), so a crash mid-write can
    never leave a truncated file for the loader to choke on. Best-effort: a
    write failure logs and returns -- the in-memory value is already live, and
    losing persistence must not fail the user's config_set.

    Shared by every sidecar store here; `label` only names the store in the
    warning, so a failure says which file it was.
    """
    try:
        if empty:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(payload, f)
            os.replace(tmp_path, path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as e:
        logger.warning("BrainControl: failed to persist %s to %s: %s", label, path, e)


def save_persona_store(store: dict[str, Any]) -> None:
    """Persist `store`, or delete the file when nothing is configured."""
    payload = {
        "version": 2,
        "global": store.get("global") or "",
        "brains": {k: v for k, v in (store.get("brains") or {}).items() if v},
    }
    _save_sidecar(
        persona_path(),
        payload,
        empty=not payload["global"] and not payload["brains"],
        label="persona",
    )


# ── Per-brain model override sidecar ────────────────────────────────────
#
# Same shape/atomicity/fail-safe discipline as the persona store above, just
# for one string per brain instead of a tiered persona. Default location is
# next to the persona file (not a second hardcoded package-adjacent path) so
# both sidecars land together without a second env var to remember by default.


def model_overrides_path() -> Path:
    raw = os.environ.get("VOICE_MODEL_OVERRIDES_FILE")
    if raw:
        return Path(raw).expanduser()
    return persona_path().parent / _MODEL_OVERRIDES_FILENAME


def validate_model_override(text: Any) -> tuple[bool, str]:
    """Sanity-bound a user-submitted model id. Unlike a persona, empty is NOT
    valid here -- an empty override is handled by the caller as "clear the
    override", never persisted as text. `"auto"` is rejected: that's how a
    brains.json entry says "resolve whatever the endpoint reports as loaded",
    and an override that reads "auto" would be indistinguishable from having
    no override at all -- clearing the override is the way back to it."""
    if not isinstance(text, str):
        return False, "model override must be text"
    if not text:
        return False, "model override must not be empty"
    if len(text) > MODEL_OVERRIDE_MAX_CHARS:
        return False, f"model override exceeds the {MODEL_OVERRIDE_MAX_CHARS} character cap"
    if text == "auto":
        return False, 'model override cannot be "auto" -- clear the override to restore the configured default'
    if _has_control_chars(text):
        return False, "model override contains control characters"
    return True, ""


def empty_model_override_store() -> dict[str, Any]:
    return {"version": 1, "brains": {}}


def _sanitize_model_override_entry(raw: Any) -> Optional[str]:
    """Normalize one stored override, or None if it's unusable -- dropped
    rather than raising, same one-bad-entry-must-not-cost-the-rest contract
    as `_sanitize_brain_entry`."""
    if not isinstance(raw, str):
        return None
    ok, _ = validate_model_override(raw)
    if not ok:
        return None
    return raw


def load_model_override_store() -> dict[str, Any]:
    """Read the persisted model-override store, always returning a usable
    dict. Fail-safe by construction (runs at pipeline startup): a missing,
    unreadable, or corrupt file logs and yields an empty store rather than
    raising; a single malformed brain entry is dropped, not fatal."""
    path = model_overrides_path()
    store = empty_model_override_store()
    try:
        with open(path, "r") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return store
    except (OSError, ValueError) as e:
        logger.warning("BrainControl: ignoring unreadable model override file %s: %s", path, e)
        return store

    if not isinstance(payload, dict):
        logger.warning("BrainControl: ignoring malformed model override file %s: not an object", path)
        return store

    raw_brains = payload.get("brains")
    if isinstance(raw_brains, dict):
        for name, raw_entry in raw_brains.items():
            entry = _sanitize_model_override_entry(raw_entry) if isinstance(name, str) else None
            if entry is None:
                logger.warning("BrainControl: dropping unusable model override for brain %r in %s", name, path)
                continue
            store["brains"][name] = entry
    elif raw_brains is not None:
        logger.warning("BrainControl: ignoring malformed `brains` in %s: not an object", path)

    return store


def save_model_override_store(store: dict[str, Any]) -> None:
    """Persist `store`, or delete the file when nothing is configured."""
    payload = {
        "version": 1,
        "brains": {k: v for k, v in (store.get("brains") or {}).items() if v},
    }
    _save_sidecar(
        model_overrides_path(),
        payload,
        empty=not payload["brains"],
        label="model overrides",
    )


# ── Chosen-voice sidecar ────────────────────────────────────────────────
#
# Same fail-safe/atomicity discipline as the persona and model-override
# sidecars above, for the single string `config_set {voice}` already
# validates and applies live -- the boot argv voice otherwise silently wins
# back on every restart. Default location is next to the persona file, same
# reasoning as `model_overrides_path()`.

_VOICE_CHOICE_FILENAME = "voice_choice.json"

# A stored name already passed voice_clone.is_name_format_valid's 1-32 char
# regex at write time, and the boot-apply path re-checks it through the same
# gates a wire value goes through -- this is only a defensive ceiling against
# a hand-edited file, so it can be generous without weakening either gate.
VOICE_CHOICE_MAX_CHARS = 200


def voice_choice_path() -> Path:
    raw = os.environ.get("VOICE_CHOICE_FILE")
    if raw:
        return Path(raw).expanduser()
    return persona_path().parent / _VOICE_CHOICE_FILENAME


def load_voice_choice() -> str:
    """Read the persisted voice choice, or "" on anything unusable (missing,
    unreadable, corrupt, not an object, not a string, oversize). Fail-safe by
    construction (runs at pipeline startup): never raises. The returned name
    is NOT re-validated against predefined/custom voices here -- the caller
    applies it through the same `_set_voice` gates a wire value goes through.
    """
    path = voice_choice_path()
    try:
        with open(path, "r") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as e:
        logger.warning("BrainControl: ignoring unreadable voice choice file %s: %s", path, e)
        return ""

    if not isinstance(payload, dict):
        logger.warning("BrainControl: ignoring malformed voice choice file %s: not an object", path)
        return ""

    voice = payload.get("voice")
    if not isinstance(voice, str) or not voice:
        return ""
    if len(voice) > VOICE_CHOICE_MAX_CHARS:
        logger.warning("BrainControl: ignoring oversized voice choice in %s", path)
        return ""
    return voice


def save_voice_choice(name: str) -> None:
    """Persist `name` as the chosen voice, or delete the file when empty."""
    _save_sidecar(
        voice_choice_path(),
        {"voice": name},
        empty=not name,
        label="voice choice",
    )


def _curated_models(entry: dict[str, Any]) -> list[str]:
    """A brains.json entry MAY carry a curated `models` list (ruling 3) --
    filtered defensively since brains.json is hand-edited."""
    curated = entry.get("models")
    if not isinstance(curated, list):
        return []
    return [m for m in curated if isinstance(m, str) and m]


def resolve_persona(store: dict[str, Any], brain: str, default: str) -> tuple[str, str]:
    """Resolve the effective persona for `brain`, most specific tier first.

    Returns `(text, tier)`. A brain in preset mode reads `BRAIN_PRESETS` live,
    so a preset improved in a later release reaches the users who selected it
    instead of being frozen at the moment they clicked.
    """
    entry = (store.get("brains") or {}).get(brain) or {}
    if entry.get("mode") == "custom" and entry.get("text"):
        return entry["text"], TIER_BRAIN_CUSTOM
    if entry.get("mode") == "preset" and BRAIN_PRESETS.get(brain):
        return BRAIN_PRESETS[brain], TIER_BRAIN_PRESET
    if store.get("global"):
        return store["global"], TIER_GLOBAL
    return default, TIER_DEFAULT


class BrainControl:
    """Live brain (LLM backend) + persona switch driven by WebSocket control messages.

    Instantiated once per websocket pipeline and wired as
    `websocket_streamer.control_callback`. `handle()` is invoked via
    `asyncio.to_thread` by the streamer, so blocking network calls here are safe
    and do not stall the audio loop. Never raises out of `handle()`.
    """

    def __init__(
        self,
        llm_handler: Any,
        runtime_config: Any,
        brains_path: str,
        tts_handler: Any = None,
        cockpit: Any = None,
        streamer: Any = None,
        tts_queue: Any = None,
    ) -> None:
        self.llm_handler = llm_handler
        self.runtime_config = runtime_config
        self.brains_path = brains_path
        self.brains: dict[str, dict[str, Any]] = self._load_brains(brains_path)
        self.active_brain = "coder"
        self.tts_handler = tts_handler
        self.cockpit = cockpit
        # lm_processed_queue -- lets BrainControl inject an audition TTSInput
        # after a voice change (ruling 8). None keeps every pre-existing
        # call site/test valid -- auditioning is simply unavailable then,
        # same pattern as `streamer`.
        self.tts_queue = tts_queue
        # WebSocketStreamer, for wake-word control (gate lives on it) and
        # broadcasting wakeword_state after a config_set flips it. None keeps
        # every pre-existing call site/test valid -- wake word control is
        # simply unavailable then.
        self.streamer = streamer
        # Captured at construction time, before any config_set — this IS the
        # args-class init_chat_prompt default. Empty persona restores this,
        # it never means "no system prompt".
        self.default_persona = runtime_config.session.instructions or ""
        # Startup precedence: a persona persisted from the panel outranks the
        # CLI default, so an edit survives a restart without touching the unit
        # file. Clearing everything deletes the file and this falls back to the
        # default. Resolution is per-brain, so it's re-run on every brain switch.
        self.persona_store = load_persona_store()
        self.persona_tier = TIER_DEFAULT
        self._apply_persona()
        # Same startup precedence as the persona store, one tier only: an
        # override persisted from the panel wins over brains.json's `model`
        # field until cleared. Read by `_effective_model`.
        self.model_overrides = load_model_override_store()
        # RLock, not Lock: `_set_brain` records its probe result (below) while
        # already holding this lock via `_config_set`'s outer `with`, and the
        # background reconcile thread also needs to take it around its own
        # probes -- a plain Lock would deadlock on the first path.
        self._config_lock = threading.RLock()
        # Observed reachability per brain, keyed by name: {"ok": bool, "at":
        # monotonic seconds, "error": str}. Absent = never probed since
        # startup, reported to the panel as `reachable: null`. Orthogonal to
        # `available` (configured intent, from brains.json) -- this is what
        # the last actual probe (a switch attempt or a background sweep)
        # observed. Guarded by `_config_lock`.
        self._brain_probes: dict[str, dict[str, Any]] = {}
        # Background-reconcile bookkeeping, also under `_config_lock`: at most
        # one sweep in flight, and a debounce so opening the panel repeatedly
        # doesn't restart it before the last one even finished.
        self._reconcile_in_progress = False
        self._last_reconcile_start: Optional[float] = None
        self._reconcile_thread: Optional[threading.Thread] = None
        # Thread-local scratch space for the model ids a `_resolve_model` GET
        # just saw, read by the caller on the SAME thread immediately after
        # the call returns (see `_resolve_model`'s docstring). Thread-local,
        # not a plain instance attribute: `_set_brain` (a config_set thread)
        # and the background reconcile thread can each be mid-probe of a
        # DIFFERENT brain at once, and a shared attribute would let one
        # overwrite the other's in-flight result.
        self._probed_model_ids = threading.local()
        self.tools_armed = 0
        self.tools_armed_names: list[str] = []
        try:
            armed = voice_tools.get_tool_defs()
            runtime_config.session.tools = armed
            self.tools_armed = len(armed)
            self.tools_armed_names = [t["name"] for t in armed]
        except Exception as e:
            logger.warning("BrainControl: failed to arm voice tools: %s", e)
        # Startup precedence, same spirit as the persona store above: a voice
        # chosen from the panel outranks the boot argv voice, so a switch
        # survives a restart without touching the unit file. Applied through
        # the SAME `_set_voice` gates a wire value goes through -- a
        # hand-edited voice_choice.json gets no more trust than the network.
        # `audition=False`: boot must never speak. A stored voice that fails
        # to apply (unknown, backend without switching support, remote
        # validation fails) is one WARNING that keeps the argv default --
        # never a crash, and the file is left alone either way (`_set_voice`
        # only rewrites it on success; a failed apply never touches it).
        stored_voice = load_voice_choice()
        if stored_voice and stored_voice != (getattr(self.tts_handler, "voice", None) or ""):
            ok, error = self._set_voice(stored_voice, audition=False)
            if not ok:
                logger.warning("BrainControl: stored voice %r failed to apply at boot: %s", stored_voice, error)

    def _apply_persona(self) -> bool:
        """Re-resolve the persona for the active brain and push it into the
        runtime config. Returns True when the effective persona changed --
        the caller resets chat history on that, since the system prompt the
        earlier turns were produced under is no longer the one in force."""
        text, tier = resolve_persona(self.persona_store, self.active_brain, self.default_persona)
        self.persona_tier = tier
        new_instructions = text or None
        if new_instructions == self.runtime_config.session.instructions:
            return False
        self.runtime_config.session.instructions = new_instructions
        logger.info("BrainControl: persona for brain %s resolved from tier %s", self.active_brain, tier)
        return True

    def _persona_tier_state(self) -> dict[str, Any]:
        """Everything the panel needs to show WHICH tier is in force and what
        the other tiers hold, so switching brains can update the display
        without another round trip."""
        entry = (self.persona_store.get("brains") or {}).get(self.active_brain) or {}
        return {
            "resolved_from": self.persona_tier,
            "global": self.persona_store.get("global") or "",
            "brain": self.active_brain,
            "brain_mode": entry.get("mode") or "inherit",
            "brain_text": entry.get("text") or "",
            "preset": BRAIN_PRESETS.get(self.active_brain, ""),
            "presets": dict(BRAIN_PRESETS),
        }

    @property
    def persona_persisted(self) -> bool:
        """Whether a persona is currently persisted to disk. Read from the
        filesystem rather than cached so it stays honest if the file is
        removed out from under a running pipeline."""
        try:
            return persona_path().is_file()
        except OSError:
            return False

    def _load_brains(self, path: str) -> dict[str, dict[str, Any]]:
        with open(path, "r") as f:
            return json.load(f)

    def _resolve_api_key(self, entry: dict[str, Any]) -> Optional[str]:
        """Resolve a brain's API key: literal `api_key`, or lazily parsed from
        `api_key_file` (env-style `VAR=value` lines) looking up `api_key_var`.
        Never logs the resolved value."""
        if entry.get("api_key"):
            return entry["api_key"]
        api_key_file = entry.get("api_key_file")
        api_key_var = entry.get("api_key_var")
        if not api_key_file or not api_key_var:
            return None
        try:
            with open(api_key_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    if key.strip() == api_key_var:
                        return value.strip().strip('"').strip("'")
        except OSError as e:
            logger.warning("BrainControl: failed to read api_key_file %s: %s", api_key_file, e)
        return None

    def _resolve_model(self, base_url: str, model: str, api_key: Optional[str]) -> Optional[str]:
        """GET {base_url}/models to resolve the model id and probe reachability.

        For `model == "auto"`, picks the entry whose `status.value == "loaded"`
        (llama-cpp router schema), falling back to the first listed model id for
        plain OpenAI-style lists. For a fixed model name, the GET is purely a
        reachability probe (fail closed on error). Returns None on any failure.

        As a side effect (ruling 4), also parses every served model id out of
        this SAME response into `self._probed_model_ids.ids` -- read by the
        caller right after this call returns -- so a caller that wants the
        live list doesn't have to issue a second `/models` GET.
        """
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        # Reset up front: a failed GET below must not leave a PRIOR call's
        # ids attributed to this one.
        self._probed_model_ids.ids = []
        try:
            resp = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0, headers=headers)
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            logger.warning("BrainControl: model probe failed for %s: %s", base_url, e)
            return None

        self._probed_model_ids.ids = _extract_model_ids(data)

        if not data:
            return None

        if model != "auto":
            return model

        for entry in data:
            status = entry.get("status")
            if isinstance(status, dict) and status.get("value") == "loaded":
                return entry.get("id")

        return data[0].get("id")

    def _predefined_voices(self) -> list[str]:
        """Names of pocket_tts's built-in preset voices, sorted. Empty on any
        import failure (pocket_tts not installed, upstream rename, etc)."""
        try:
            from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES

            return sorted(_ORIGINS_OF_PREDEFINED_VOICES.keys())
        except Exception as e:
            logger.warning("BrainControl: predefined voice list unavailable: %s", e)
            return []

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        try:
            msg_type = msg.get("type")
            if msg_type == "config_get":
                self._maybe_start_reconcile()
                state = self._config_state()
                if self.cockpit is not None:
                    # Push a fresh cockpit_state to all clients so a page that
                    # just loaded renders the card without waiting for the
                    # next poll tick.
                    self.cockpit.broadcast_now()
                return state
            if msg_type == "config_set":
                return self._config_set(msg)
            if msg_type == "voice_clone_begin":
                return self._voice_clone_begin(msg)
            if msg_type == "voice_clone_end":
                return self._voice_clone_end(msg)
            return {"type": "config_ack", "ok": False, "error": f"unknown type: {msg_type}"}
        except Exception as e:
            logger.exception("BrainControl.handle failed")
            return {"type": "config_ack", "ok": False, "error": str(e)}

    def _wake_word_state(self) -> Optional[dict[str, Any]]:
        """Wake-word block for `config_state`/`config_ack` -- None when there's
        no streamer to control (wake word control unavailable)."""
        if self.streamer is None:
            return None
        gate = self.streamer.wakeword_gate
        return {
            "enabled": gate.enabled,
            "state": gate.state(),
            "phrase": gate.phrase,
            # Stripped display form, NOT the raw VOICE_WAKE_WORD_MODEL arg: it has to
            # compare equal to an entry in `models` below or the panel's dropdown can't
            # mark the active model. See WakewordGate.model_name.
            "model": gate.model_name,
            "models": gate.available_models(),
        }

    def _record_probe(self, name: str, ok: bool, error: str, models: Optional[list[str]] = None) -> bool:
        """Atomically record the outcome of an actual reachability probe for
        `name` and report whether the OBSERVABLE state changed.

        The compare (against the prior entry) and the write both happen
        under `_config_lock` in one critical section -- reading the prior
        value outside the lock and comparing after the fact is a race: a
        concurrent `_set_brain` probe for the same brain could land in
        between, making the comparison stale and the caller's `changed`
        computation wrong (missed or spurious broadcast). A first-ever
        record (no prior entry) counts as changed -- that's the
        null-to-observed transition the panel needs to redraw for.

        Called both from `_set_brain` (already holding `_config_lock` via
        `_config_set`) and from the background reconcile thread (which takes
        the lock fresh) -- reentrant-safe because `_config_lock` is an RLock.
        `_set_brain`'s callers ignore the return value; only the reconcile
        loop needs it.

        `models` (ruling 4) is the list of ids this SAME probe's `/models` GET
        saw, or `None`/empty when the caller has none (an unreachable brain,
        or an older call site). It is NOT part of the changed-detection
        compare -- only `ok`/`error` gate a broadcast, same as before.
        """
        with self._config_lock:
            before = self._brain_probes.get(name)
            changed = before is None or before.get("ok") != ok or before.get("error") != error
            self._brain_probes[name] = {"ok": ok, "at": time.monotonic(), "error": error, "models": models or []}
            return changed

    def _maybe_start_reconcile(self) -> None:
        """Kick a daemon thread that serially re-probes every `available:
        true` brain, unless one is already running or the last sweep started
        too recently (ruling 3). Never blocks the caller -- `config_get` must
        return with cached/stale reachability, not wait on the network."""
        debounce = _env_seconds("VOICE_BRAIN_PROBE_DEBOUNCE_S", _DEFAULT_PROBE_DEBOUNCE_S)
        now = time.monotonic()
        with self._config_lock:
            if self._reconcile_in_progress:
                return
            if self._last_reconcile_start is not None and (now - self._last_reconcile_start) < debounce:
                return
            self._reconcile_in_progress = True
            self._last_reconcile_start = now
        thread = threading.Thread(
            target=self._reconcile_brain_reachability, name="brain-reachability-probe", daemon=True
        )
        self._reconcile_thread = thread
        thread.start()

    def _reconcile_brain_reachability(self) -> None:
        """Body of the background reconcile thread: probe every configured
        (`available: true`) brain, then broadcast a fresh `config_state` if
        anything observable changed. Never raises out -- this runs
        unsupervised on its own thread, nowhere to report an exception to
        except the log."""
        try:
            changed = False
            for name, entry in list(self.brains.items()):
                if not entry.get("available", False):
                    continue
                base_url = entry.get("base_url")
                model = self._effective_model(name, entry)
                api_key = self._resolve_api_key(entry)
                # Network probe stays OUTSIDE the lock -- only the
                # compare-and-record in `_record_probe` needs it.
                resolved = self._resolve_model(base_url, model, api_key)
                probed_models = getattr(self._probed_model_ids, "ids", [])
                ok = resolved is not None
                error = "" if ok else f"model probe failed for {name}"
                changed |= self._record_probe(name, ok, error, probed_models)
            if changed and self.streamer is not None:
                self.streamer.broadcast_json(self._config_state())
        except Exception:
            logger.exception("BrainControl: background reachability reconcile failed")
        finally:
            with self._config_lock:
                self._reconcile_in_progress = False

    def _state_payload(self) -> dict[str, Any]:
        """Everything the panel renders, serialized in ONE place.

        `config_state` and a successful `config_ack` are the same snapshot in a
        different envelope. They used to be written out separately, and they
        drifted exactly as you would expect: the ack carried `model` the state
        didn't, the state carried `voices` and `brains` the ack didn't. The
        client paid for that gap three times over -- a hand-merge of
        `custom_voices` against a cached predefined list, a
        hidden-when-unavailable caveat that only held for one of the two
        frames, and a follow-up `config_get` round trip after every successful
        `config_set` just to fetch what the ack couldn't carry.

        So: one builder, one shape. The envelope adds `type`, and an ack adds
        `ok` plus `chat_reset` -- which stays ack-only on purpose, because it
        describes what the operation just did, not what the state is.
        """
        with self._config_lock:
            probes = dict(self._brain_probes)
        return {
            "active_brain": self.active_brain,
            "model": self.llm_handler.model_name,
            "persona": self.runtime_config.session.instructions or "",
            "default_persona": self.default_persona or "",
            "persona_persisted": self.persona_persisted,
            "persona_tiers": self._persona_tier_state(),
            "voice": (self.tts_handler.voice or "") if self.tts_handler else "",
            "voices": self._predefined_voices() if self.tts_handler else [],
            "custom_voices": voice_clone.list_custom_voices() if self.tts_handler else [],
            "speech_style": (getattr(self.tts_handler, "instructions", "") or "") if self.tts_handler else "",
            "tools_armed": self.tools_armed,
            "tools": list(self.tools_armed_names),
            "wake_word": self._wake_word_state(),
            "brains": [
                {
                    "name": name,
                    "label": entry.get("label", name),
                    "model": entry.get("model", ""),
                    # The persisted override, "" when none (ruling 5) -- the
                    # panel marks this option selected in its per-brain picker.
                    "model_override": (self.model_overrides.get("brains") or {}).get(name, ""),
                    # A curated list (brains.json's `models`, ruling 3) always
                    # wins over the live-probed list -- the operator's own
                    # curation is more trustworthy than whatever an endpoint
                    # happened to report last. Empty when neither exists.
                    "models": _curated_models(entry) or probes.get(name, {}).get("models", []),
                    "available": entry.get("available", False),
                    "note": entry.get("note", ""),
                    # Observed, not configured -- null until a switch attempt
                    # or a background sweep has actually probed this brain.
                    "reachable": probes.get(name, {}).get("ok"),
                    "probe_error": probes.get(name, {}).get("error", ""),
                }
                for name, entry in self.brains.items()
            ],
        }

    def _config_state(self) -> dict[str, Any]:
        return {"type": "config_state", **self._state_payload()}

    def _config_set(self, msg: dict[str, Any]) -> dict[str, Any]:
        if "permission_respond" in msg:
            return self._handle_permission_respond(msg["permission_respond"])

        with self._config_lock:
            chat_reset = False

            if "brain" in msg:
                prev_brain = self.active_brain
                ok, error = self._set_brain(msg["brain"])
                if not ok:
                    return {"type": "config_ack", "ok": False, "error": error}
                if self.active_brain != prev_brain:
                    chat_reset = True
                    # The new brain may have its own override or preset -- the
                    # persona is a property of the brain in force, not of the
                    # session, so re-resolve before anything else reads it.
                    self._apply_persona()
            if "brain_model" in msg:
                ok, error, needs_reset = self._set_brain_model(msg["brain_model"])
                if not ok:
                    return {"type": "config_ack", "ok": False, "error": error}
                if needs_reset:
                    chat_reset = True
                # Broadcast explicitly (ruling 6): the requester resyncs via
                # its own follow-up config_get after this ack, same as every
                # other config_set branch, but OTHER connected screens only
                # see a model-override change through this -- same reasoning
                # as the voice_delete/voice_clone broadcasts elsewhere here.
                if self.streamer is not None:
                    self.streamer.broadcast_json(self._config_state())
            if "voice" in msg:
                ok, error = self._set_voice(msg["voice"])
                if not ok:
                    return {"type": "config_ack", "ok": False, "error": error}
            if "speech_style" in msg:
                ok, error = self._set_speech_style(msg["speech_style"])
                if not ok:
                    return {"type": "config_ack", "ok": False, "error": error}
            if "voice_delete" in msg:
                ok, error = self._voice_delete(msg["voice_delete"])
                if not ok:
                    return {"type": "config_ack", "ok": False, "error": error}
            if "persona" in msg or "persona_mode" in msg:
                ok, error = self._set_persona(msg)
                if not ok:
                    return {"type": "config_ack", "ok": False, "error": error}
                if self._apply_persona():
                    chat_reset = True
            if "wake_word" in msg:
                if self.streamer is None:
                    return {"type": "config_ack", "ok": False, "error": "wake word control unavailable"}
                gate = self.streamer.wakeword_gate
                if msg["wake_word"]:
                    gate.enabled = True
                    gate.rearm()
                else:
                    gate.enabled = False
                self.streamer.broadcast_wakeword_state()
            if "wake_word_model" in msg:
                if self.streamer is None:
                    return {"type": "config_ack", "ok": False, "error": "wake word control unavailable"}
                ok, error = self.streamer.wakeword_gate.set_model(msg["wake_word_model"])
                if not ok:
                    return {"type": "config_ack", "ok": False, "error": error}
                self.streamer.broadcast_wakeword_state()
            discovered_brains: Optional[list[dict[str, Any]]] = None
            if msg.get("discover_brains"):
                try:
                    discovered_brains = brain_discovery.discover()
                except Exception as e:
                    # discover() itself never raises, but guard the call site
                    # too -- a scan must never fail the whole config_set.
                    logger.warning("BrainControl: brain discovery failed: %s", e)
                    discovered_brains = []
            if msg.get("reset_chat"):
                chat_reset = True
            if chat_reset:
                self.runtime_config.chat.reset()
            if msg.get("reload_tools"):
                try:
                    armed = voice_tools.get_tool_defs()
                    self.runtime_config.session.tools = armed
                    self.tools_armed = len(armed)
                    self.tools_armed_names = [t["name"] for t in armed]
                    logger.info("BrainControl: tools reloaded (%d armed)", self.tools_armed)
                except Exception as e:
                    logger.warning("BrainControl: tool reload failed: %s", e)
                    return {"type": "config_ack", "ok": False, "error": f"tool reload failed: {e}"}

            # A complete snapshot, not a summary: the client can now paint the
            # whole panel from this ack alone, so it no longer chases every
            # successful config_set with a `config_get`. `chat_reset` is the
            # one field that is about the operation rather than the state.
            ack: dict[str, Any] = {
                "type": "config_ack",
                "ok": True,
                "chat_reset": chat_reset,
                **self._state_payload(),
            }
            if discovered_brains is not None:
                ack["discovered_brains"] = discovered_brains
            return ack

    def _set_persona(self, msg: dict[str, Any]) -> tuple[bool, str]:
        """Write one tier of the persona store.

        `persona_scope` picks the tier: "global" (the default, and what an
        older client that only sends `persona` gets) or "brain" (the active
        brain). For brain scope, `persona_mode` selects "custom" (use the
        `persona` text), "preset" (track the shipped preset for this brain),
        or "inherit" (drop the override and fall through to global/default).
        Clearing custom text is the same as inherit -- an override you emptied
        falls back down the chain, it never means "no persona".
        """
        scope = msg.get("persona_scope", "global")
        if scope not in ("global", "brain"):
            return False, f"unknown persona scope: {scope}"

        persona = msg.get("persona", "")
        ok, error = validate_persona(persona)
        if not ok:
            return False, error

        if scope == "global":
            self.persona_store["global"] = persona
        else:
            mode = msg.get("persona_mode", "custom")
            if mode not in ("custom", "preset", "inherit"):
                return False, f"unknown persona mode: {mode}"
            brains = self.persona_store.setdefault("brains", {})
            if mode == "preset":
                if not BRAIN_PRESETS.get(self.active_brain):
                    return False, f"no preset available for brain: {self.active_brain}"
                brains[self.active_brain] = {"mode": "preset"}
            elif mode == "custom" and persona:
                brains[self.active_brain] = {"mode": "custom", "text": persona}
            else:
                brains.pop(self.active_brain, None)

        # Persisted unconditionally, not only on a change: an unchanged persona
        # may still not be on disk yet, and a cleared one must remove its entry
        # even when the resolved text happens to be identical.
        save_persona_store(self.persona_store)
        return True, ""

    def _handle_permission_respond(self, payload: Any) -> dict[str, Any]:
        if self.cockpit is None:
            return {"type": "config_ack", "ok": False, "error": "hermes cockpit unavailable"}
        if not isinstance(payload, dict):
            return {"type": "config_ack", "ok": False, "error": "invalid permission_respond payload"}
        perm_id = payload.get("id")
        approve = bool(payload.get("approve"))
        if not perm_id:
            return {"type": "config_ack", "ok": False, "error": "permission_respond requires an id"}
        ok, message = self.cockpit.respond(perm_id, approve)
        return {
            "type": "config_ack",
            "ok": ok,
            "permission_respond": {"id": perm_id, "approve": approve, "message": message},
        }

    def _effective_model(self, name: str, entry: dict[str, Any]) -> str:
        """Resolve the model actually requested for `name`: a persisted
        override wins (ruling 2), else `entry`'s configured `model` field
        (defaulting to `"auto"`, same default `_set_brain` used before
        overrides existed)."""
        override = (self.model_overrides.get("brains") or {}).get(name)
        return override or entry.get("model", "auto")

    def _set_brain(self, name: str) -> tuple[bool, str]:
        entry = self.brains.get(name)
        if entry is None:
            return False, f"unknown brain: {name}"
        if not entry.get("available", False):
            return False, entry.get("note") or f"brain not available: {name}"

        base_url = entry["base_url"]
        model = self._effective_model(name, entry)
        api_key = self._resolve_api_key(entry)
        resolved = self._resolve_model(base_url, model, api_key)
        probed_models = getattr(self._probed_model_ids, "ids", [])
        if resolved is None:
            error = f"model probe failed for {name}"
            # Recorded on failure too (not just success): a failed switch must
            # immediately mark the brain unreachable in the next config_state,
            # not wait for the next background sweep.
            self._record_probe(name, False, error, probed_models)
            return False, error

        # Same swap + _extra_body rule as BaseOpenAICompatibleHandler.setup()
        # (disable_thinking=True, no reasoning_effort -- matches our CLI default),
        # UNLESS this brain declares its own `reasoning_effort` (brains.json).
        # That escape hatch exists because a client-sent `chat_template_kwargs`
        # OVERRIDES a server-side pinned reasoning_effort on at least one
        # llama-server build -- so our own disable_thinking kwargs silently
        # clobber the server's pin and fall back to the template's own default
        # (verified: gpt-oss defaults to xhigh, ~4x more reasoning than the
        # server's pinned "medium", all of it swallowed as dead air by the
        # think-filter). A brain that needs a specific effort must ask for it
        # in the same kwargs blob rather than rely on the server-side pin
        # surviving a switch.
        self.llm_handler.client = OpenAI(api_key=api_key or "dummy", base_url=base_url)
        self.llm_handler.model_name = resolved
        effort = entry.get("reasoning_effort")
        extra_body = None
        if isinstance(effort, str) and effort:
            if effort in _VALID_REASONING_EFFORTS:
                extra_body = {"chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": effort}}
            else:
                # A typo in brains.json must not kill the whole switch -- fall
                # through to current behavior, same fail-open spirit as
                # `_env_seconds`.
                logger.warning(
                    "BrainControl: brain %s has invalid reasoning_effort %r (expected one of %s); ignoring",
                    name,
                    effort,
                    sorted(_VALID_REASONING_EFFORTS),
                )
        if extra_body is None:
            extra_body = BaseOpenAICompatibleHandler._build_extra_body(base_url, True, None)
        self.llm_handler._extra_body = extra_body
        self.active_brain = name
        self._record_probe(name, True, "", probed_models)
        return True, ""

    def _set_brain_model(self, payload: Any) -> tuple[bool, str, bool]:
        """Handle a `brain_model` config_set payload: set or clear a
        per-brain model override. Returns `(ok, error, chat_reset)`.

        Probe-before-persist (ruling 6): when `name` is the ACTIVE brain and
        the effective model actually changes, the candidate override is
        staged in memory and `_set_brain` is re-run on the same brain -- it
        reads the staged value via `_effective_model` and probes it for
        real. A failed probe rolls the staged value back and nothing is
        written to disk (the failed probe already recorded the brain
        unreachable via `_set_brain`'s own `_record_probe` call). Only a
        successful probe persists, and only then is chat reset -- the turns
        so far were produced by a different model. When `name` is NOT the
        active brain, the override is persisted unprobed; it will probe
        honestly the next time that brain is switched to.
        """
        if not isinstance(payload, dict):
            return False, "invalid brain_model payload", False
        name = payload.get("brain")
        if not isinstance(name, str) or name not in self.brains:
            return False, f"unknown brain: {name}", False
        model = payload.get("model") or ""
        if not isinstance(model, str):
            return False, "model must be text", False
        if model:
            ok, error = validate_model_override(model)
            if not ok:
                return False, error, False

        entry = self.brains[name]
        brains_overrides = self.model_overrides.setdefault("brains", {})
        prev_override = brains_overrides.get(name)
        new_override = model or None

        def _stage(value: Optional[str]) -> None:
            if value:
                brains_overrides[name] = value
            else:
                brains_overrides.pop(name, None)

        current_effective = prev_override or entry.get("model", "auto")
        new_effective = new_override or entry.get("model", "auto")
        needs_probe = name == self.active_brain and new_effective != current_effective

        if not needs_probe:
            _stage(new_override)
            save_model_override_store(self.model_overrides)
            return True, "", False

        _stage(new_override)
        try:
            ok, error = self._set_brain(name)
        except Exception:
            # `_set_brain` isn't expected to raise (its own probe failures
            # return False), but anything outside `_resolve_model`'s catches
            # (e.g. the OpenAI client constructor) would otherwise leave the
            # staged override in `self.model_overrides` -- reported by
            # config_state as an override that never probed and isn't on
            # disk, then silently gone on restart. `handle()`'s top-level
            # except still turns this into an error ack; our only job here
            # is to not leave staged state behind.
            _stage(prev_override)
            raise
        if not ok:
            _stage(prev_override)
            return False, error, False

        save_model_override_store(self.model_overrides)
        return True, "", True

    def _set_voice(self, name: str, audition: bool = True) -> tuple[bool, str]:
        """Dispatch to the active backend's voice-switch path, then persist
        the choice on success -- the single choke point both the local and
        remote branches pass through, so a sidecar write here covers a wire
        `config_set {voice}` and the boot-apply path in `__init__` alike.
        `audition=False` (boot only) suppresses the spoken sample; boot must
        never speak."""
        if self.tts_handler is None:
            return False, "voice switching unavailable"
        # Feature-detected the same way `_set_speech_style` detects `instructions`:
        # the honest signal is whether this handler exposes the pocket-style
        # voice-state API, not an isinstance check (drift-resistant if a future
        # backend grows the same handle). Checked BEFORE name validation so an
        # unsupported backend (kokoro, qwen3) never produces a misleading
        # "unknown voice" for a name that was never the problem.
        if hasattr(getattr(self.tts_handler, "model", None), "get_state_for_audio_prompt"):
            ok, error = self._set_voice_local(name, audition=audition)
        # `remote-speech` (RemoteSpeechTTSHandler) has no pocket-style voice-state
        # API -- its `.model` is a plain string like "tts-1" -- but it DOES accept
        # `voice` per-request against an OpenAI-compatible server (see
        # `_stream_remote`). Detected by its own capability surface (`base_url` +
        # a settable `voice` attribute) rather than isinstance, same drift-resistant
        # spirit as the check above; a backend with neither surface still falls
        # through to the "not supported" error below.
        elif hasattr(self.tts_handler, "base_url") and hasattr(self.tts_handler, "voice"):
            ok, error = self._set_voice_remote(name, audition=audition)
        else:
            backend = type(self.tts_handler).__name__
            return False, f"voice switching is not supported by the active TTS backend ({backend})"
        if ok:
            save_voice_choice(name)
        return ok, error

    def _set_voice_local(self, name: str, audition: bool = True) -> tuple[bool, str]:
        if name in self._predefined_voices():
            source: Any = name
        else:
            # Custom (cloned) voice -- resolve to its sidecar state file.
            # `name` reaches here straight off the wire (config_set voice),
            # so it needs the same format gate as the delete path before
            # `voice_path()` ever joins it into a filesystem path (ruling 4:
            # a value like "../../x" must not resolve outside the voices dir).
            if not voice_clone.is_name_format_valid(name):
                return False, f"unknown voice: {name}"
            path = voice_clone.voice_path(name)
            if not path.is_file():
                return False, f"unknown voice: {name}"
            source = str(path)

        try:
            # Build the new state first so a failed load never leaves the
            # handler with a half-swapped voice.
            new_state = self.tts_handler.model.get_state_for_audio_prompt(source)
        except Exception as e:
            logger.warning("BrainControl: voice load failed for %s: %s", name, e)
            return False, f"voice load failed: {name}"

        self.tts_handler.voice_state = new_state
        self.tts_handler.voice = name
        if audition:
            self._audition(name)
        return True, ""

    def _set_voice_remote(self, name: str, audition: bool = True) -> tuple[bool, str]:
        """Voice switching for the remote-speech backend (any OpenAI-compatible
        /v1/audio/speech server -- in practice your own TTS server).

        Never probes the network: unlike the local path there is no state to
        build up front, `voice` is just sent per-request (`_stream_remote`), so
        this applies even while the remote is unreachable and simply takes
        effect on the next utterance.
        """
        if name not in self._predefined_voices():
            # Preset names validate against the same list as the local backend --
            # the remote server IS pocket-tts, so preset parity holds. Format-
            # validated before the "custom voices unavailable" error so a
            # traversal-shaped name still gets the same "unknown voice" rejection
            # as the local path, even though nothing here builds a filesystem path.
            if not voice_clone.is_name_format_valid(name):
                return False, f"unknown voice: {name}"
            # The sidecar .safetensors state files voice cloning writes are LOCAL
            # to the pipeline host -- the remote server has no way to load them.
            return False, "custom voices are not available while the remote TTS backend is active"

        self.tts_handler.voice = name
        # Keep the pocket fallback in lockstep so a mid-conversation failover
        # speaks the newly chosen voice, not whatever it last had:
        # `_pocket_kwargs` feeds the NEXT lazy construction, but a fallback
        # already built (e.g. from an earlier failover) captured the old voice
        # at construction time and won't re-read the dict -- so also drop the
        # already-built instance and let `_pocket_instance()` rebuild it (cold
        # start on its next use) with the new voice.
        if hasattr(self.tts_handler, "_pocket_kwargs"):
            self.tts_handler._pocket_kwargs["voice"] = name
        if hasattr(self.tts_handler, "_pocket"):
            self.tts_handler._pocket = None
        if audition:
            self._audition(name)
        return True, ""

    def _set_speech_style(self, value: Any) -> tuple[bool, str]:
        """Live-set the expression/prosody handle on the active TTS handler.

        Feature-detected via an `instructions` attribute rather than an
        isinstance check -- today only `RemoteSpeechTTSHandler` has one, but
        this stays correct if a future backend grows the same handle. A
        backend without it (pocket, kokoro, qwen3) must fail loudly and name
        itself, not silently accept and ignore the value.
        """
        if self.tts_handler is None:
            return False, "speech style control unavailable"
        if not isinstance(value, str):
            return False, "speech_style must be text"
        if getattr(self.tts_handler, "instructions", None) is None:
            backend = type(self.tts_handler).__name__
            return False, f"speech_style is not supported by the active TTS backend ({backend})"

        # Same never-empty-on-the-wire rule as the handler's own setup():
        # an empty/whitespace value reverts to its default rather than
        # ever reaching the wire empty (a verified 200-with-zero-bytes bug).
        from speech_to_speech.TTS.remote_speech_tts_handler import DEFAULT_INSTRUCTIONS

        self.tts_handler.instructions = value.strip() or DEFAULT_INSTRUCTIONS
        return True, ""

    def _audition(self, name: str) -> None:
        """After ANY successful voice change, speak a short sample through
        the normal TTS path so the change is audible without a manual test
        (ruling 8). `None` `turn_id`/`turn_revision` pass
        `SpeculativeTurnTracker`'s staleness gate unconditionally (an
        untracked turn id is always treated as latest). Best-effort: never
        raises; silently no-ops with no `tts_queue` wired or when
        `VOICE_AUDITION_TEXT=off`."""
        if self.tts_queue is None:
            return
        text = voice_clone.resolve_audition_text(name)
        if not text:
            return
        try:
            from speech_to_speech.pipeline.messages import TTSInput

            self.tts_queue.put(TTSInput(text=text, turn_id=None, turn_revision=None))
        except Exception as e:
            logger.warning("BrainControl: audition failed for %s: %s", name, e)

    def _voice_delete(self, name: str) -> tuple[bool, str]:
        # Logged on every path: a silent handler made it impossible to tell from the
        # logs whether a user's delete had ever reached the server at all.
        logger.info("BrainControl: voice delete requested for %s", name)
        if self.tts_handler is None:
            logger.warning("BrainControl: voice delete refused for %s: voice switching unavailable", name)
            return False, "voice switching unavailable"
        ok, error = voice_clone.check_delete_allowed(name, self.tts_handler.voice, self._predefined_voices())
        if not ok:
            logger.warning("BrainControl: voice delete refused for %s: %s", name, error)
            return False, error
        ok, error = voice_clone.delete_voice(name)
        if ok:
            logger.info("BrainControl: voice delete succeeded for %s", name)
        else:
            logger.warning("BrainControl: voice delete failed for %s: %s", name, error)
        if ok and self.streamer is not None:
            # Same broadcast as _voice_clone_end -- every client's
            # custom-voices dropdown needs to drop the deleted entry, not
            # just the requesting one.
            self.streamer.broadcast_json(self._config_state())
        return ok, error

    def _voice_clone_begin(self, msg: dict[str, Any]) -> dict[str, Any]:
        name = msg.get("name")
        ext = msg.get("ext")
        size = msg.get("size")

        if self.tts_handler is None:
            return {"type": "voice_clone_result", "ok": False, "name": name, "error": "voice cloning unavailable"}

        ok, error = voice_clone.validate_name(name, self._predefined_voices())
        if not ok:
            return {"type": "voice_clone_result", "ok": False, "name": name, "error": error}
        ok, error = voice_clone.validate_extension(ext)
        if not ok:
            return {"type": "voice_clone_result", "ok": False, "name": name, "error": error}
        ok, error = voice_clone.validate_size(size)
        if not ok:
            return {"type": "voice_clone_result", "ok": False, "name": name, "error": error}

        if not getattr(self.tts_handler.model, "has_voice_cloning", False):
            return {
                "type": "voice_clone_result",
                "ok": False,
                "name": name,
                "error": voice_clone.CLONING_UNAVAILABLE_MSG,
            }

        return {"type": "voice_clone_progress", "stage": "receiving"}

    def _voice_clone_end(self, msg: dict[str, Any]) -> dict[str, Any]:
        name = msg.get("name")
        ext = msg.get("ext") or ""
        raw = msg.get("data")

        if self.tts_handler is None:
            return {"type": "voice_clone_result", "ok": False, "name": name, "error": "voice cloning unavailable"}
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return {"type": "voice_clone_result", "ok": False, "name": name, "error": "empty upload"}

        # Re-validate at build time -- defense in depth against a client that
        # skipped or raced the begin-time check (chunk bytes are buffered by
        # websocket_streamer independently of that check).
        ok, error = voice_clone.validate_name(name, self._predefined_voices())
        if not ok:
            return {"type": "voice_clone_result", "ok": False, "name": name, "error": error}
        if not getattr(self.tts_handler.model, "has_voice_cloning", False):
            return {
                "type": "voice_clone_result",
                "ok": False,
                "name": name,
                "error": voice_clone.CLONING_UNAVAILABLE_MSG,
            }

        with self._config_lock:
            try:
                wav_bytes = voice_clone.normalize_to_wav(bytes(raw), ext)
            except voice_clone.VoiceCloneError as e:
                return {"type": "voice_clone_result", "ok": False, "name": name, "error": str(e)}

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
                    fh.write(wav_bytes)
                    tmp_path = fh.name
                # Build the new state before touching the sidecar dir or the
                # live handler -- same fail-safe build-before-swap order as
                # _set_voice (ruling 2/7). Export is atomic (temp file +
                # os.replace) so a failed export never leaves a partial
                # .safetensors for list_custom_voices to serve, and never
                # clobbers a pre-existing GOOD voice on a failed overwrite.
                new_state = self.tts_handler.model.get_state_for_audio_prompt(tmp_path, truncate=True)
                voice_clone.atomic_export_state(new_state, voice_clone.voice_path(name))
            except Exception as e:
                logger.warning("BrainControl: voice_clone build failed for %s: %s", name, e)
                return {"type": "voice_clone_result", "ok": False, "name": name, "error": f"voice build failed: {e}"}
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            self.tts_handler.voice_state = new_state
            self.tts_handler.voice = name

        self._audition(name)
        if self.streamer is not None:
            self.streamer.broadcast_json(self._config_state())
        return {"type": "voice_clone_result", "ok": True, "name": name, "error": None}
