"""Home Assistant voice control drop-in.

Point VOICE_TOOLS_DIR at this directory (or copy this file into yours), set
HA_URL / HA_TOKEN, and restart the voice agent. See the bottom of this file
for the one exposed tool, `home_assistant`.

Command parsing (`parse_command`) is a pure function, independent of the HTTP
I/O below it, so a later pre-LLM reflex lane can reuse it without going
through this module's network calls.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

_HTTP_TIMEOUT_S = 5.0
TIMEOUT_S = 10.0

_CAPABILITIES_MSG = (
    "I can turn things on or off, set brightness or color, or run scenes."
)

# ---------------------------------------------------------------------------
# Command parsing — pure, no I/O. Policies (compound/vague-entity bail-outs,
# entity-name suffix handling) are adapted from the Fulloch project (MIT).
# ---------------------------------------------------------------------------

# A real device alias is never "and"/"then" joined mid-command — bail so the
# LLM can decompose a compound utterance into separate tool calls instead of
# us guessing which half applies to which entity.
_COMPOUND_RE = re.compile(r"\b(?:and|then)\b", re.IGNORECASE)

# Entity references too vague to resolve to a single entity_id — let the LLM
# decide (it may re-call this tool once per resolved entity, or ask back).
_VAGUE_ENTITIES = frozenset(
    {"it", "that", "this", "them", "those", "these", "everything", "all"}
)
# Anaphoric determiners that, when they *lead* a multi-word entity ("those
# lights"), mark a back-reference rather than a real device name.
_ANAPHORA_DETERMINERS = frozenset({"it", "that", "this", "them", "those", "these"})

_NAME_SUFFIXES = ("lights", "light", "lamp", "fan", "fans", "switch")
_LEADING_FILLERS = ("the ", "a ", "an ", "my ")

_COLOR_MAP: dict[str, list[int]] = {
    "red": [255, 0, 0],
    "green": [0, 255, 0],
    "blue": [0, 0, 255],
    "yellow": [255, 255, 0],
    "orange": [255, 165, 0],
    "purple": [128, 0, 128],
    "pink": [255, 192, 203],
    "white": [255, 255, 255],
    "warm white": [255, 244, 229],
    "cool white": [255, 255, 255],
    "cyan": [0, 255, 255],
    "magenta": [255, 0, 255],
    "teal": [0, 128, 128],
    "indigo": [75, 0, 130],
    "violet": [238, 130, 238],
    "gold": [255, 215, 0],
}
# Multi-word names first so the alternation prefers "warm white" over "white".
_COLOR_ALT = "|".join(sorted(_COLOR_MAP, key=len, reverse=True))

_BRIGHTNESS_RE = re.compile(
    r"^\s*(?:please\s+)?(?:set|change|put|turn|adjust)\s+(?:the\s+)?(.+?)\s+"
    r"brightness\s+to\s+(.+?)\s*(?:percent|%)?\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_COLOR_RE = re.compile(
    r"^\s*(?:please\s+)?(?:set|change|make|turn)\s+(?:the\s+)?(.+?)\s+"
    r"(?:to\s+|colou?r\s+to\s+)?(" + _COLOR_ALT + r")\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_SCENE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:activate|run|start)\s+(?:the\s+)?(.+?)\s+scene"
    r"\s*[.?!]*\s*$",
    re.IGNORECASE,
)
# "turn on the kitchen lights" and the reversed "turn the kitchen lights on".
_TURN_ONOFF_RE = re.compile(
    r"^\s*(?:please\s+)?turn\s+(on|off)\s+(?:the\s+)?(.+?)\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_TURN_ONOFF_REV_RE = re.compile(
    r"^\s*(?:please\s+)?turn\s+(?:the\s+)?(.+?)\s+(on|off)\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_TOGGLE_RE = re.compile(
    r"^\s*(?:please\s+)?toggle\s+(?:the\s+)?(.+?)\s*[.?!]*\s*$",
    re.IGNORECASE,
)
_QUERY_POWER_RE = re.compile(
    r"^\s*(?:please\s+)?is\s+(?:the\s+)?(.+?)\s+(?:on|off)\s*\??\s*$",
    re.IGNORECASE,
)
_QUERY_TEMP_RE = re.compile(
    r"^\s*(?:please\s+)?what(?:'s|\s+is)\s+the\s+temperature\s+(?:in|at)\s+"
    r"(?:the\s+)?(.+?)\s*\??\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Intent:
    """A parsed voice command.

    `action` is one of: turn_on, turn_off, toggle, set_brightness, set_color,
    activate_scene, query_power, query_temperature. `value` holds the
    brightness percent (int) for set_brightness, or the color name (str) for
    set_color; otherwise None.
    """

    action: str
    entity: Optional[str] = None
    value: Optional[Any] = None


def _has_compound(command: str) -> bool:
    return bool(_COMPOUND_RE.search(command))


def _is_vague_entity(entity: str) -> bool:
    e = entity.lower().strip()
    if e in _VAGUE_ENTITIES:
        return True
    first = e.split(None, 1)[0] if e.split() else ""
    return first in _ANAPHORA_DETERMINERS


def _parse_percent(text: str) -> Optional[int]:
    text = text.strip().lower()
    m = re.search(r"\d{1,3}", text)
    if m:
        return max(0, min(100, int(m.group())))
    if text == "half":
        return 50
    if text == "full":
        return 100
    return None


def parse_command(text: str) -> Optional[Intent]:
    """Parse a natural-language Home Assistant command into an Intent.

    Pure function — no network I/O. Returns None for compound utterances,
    vague entities, or anything unrecognized.
    """
    if not text or not text.strip():
        return None
    command = text.strip()
    if _has_compound(command):
        return None

    m = _BRIGHTNESS_RE.match(command)
    if m:
        entity = m.group(1).strip()
        pct = _parse_percent(m.group(2).strip())
        if entity and pct is not None and not _is_vague_entity(entity):
            return Intent("set_brightness", entity, pct)
        return None

    m = _COLOR_RE.match(command)
    if m:
        entity = m.group(1).strip()
        color = m.group(2).lower()
        if entity and not _is_vague_entity(entity):
            return Intent("set_color", entity, color)
        return None

    m = _SCENE_RE.match(command)
    if m:
        entity = m.group(1).strip()
        if entity and not _is_vague_entity(entity):
            return Intent("activate_scene", entity)
        return None

    m = _TURN_ONOFF_RE.match(command)
    if m:
        state, entity = m.group(1).lower(), m.group(2).strip()
    else:
        m = _TURN_ONOFF_REV_RE.match(command)
        state, entity = (m.group(2).lower(), m.group(1).strip()) if m else (None, None)
    if state is not None:
        if entity and not _is_vague_entity(entity):
            return Intent("turn_on" if state == "on" else "turn_off", entity)
        return None

    m = _TOGGLE_RE.match(command)
    if m:
        entity = m.group(1).strip()
        if entity and not _is_vague_entity(entity):
            return Intent("toggle", entity)
        return None

    m = _QUERY_POWER_RE.match(command)
    if m:
        entity = m.group(1).strip()
        if entity and not _is_vague_entity(entity):
            return Intent("query_power", entity)
        return None

    m = _QUERY_TEMP_RE.match(command)
    if m:
        entity = m.group(1).strip()
        if entity and not _is_vague_entity(entity):
            return Intent("query_temperature", entity)
        return None

    return None


# ---------------------------------------------------------------------------
# Home Assistant REST transport — stdlib only (no `requests` dependency).
# ---------------------------------------------------------------------------


class _HAError(Exception):
    """Internal transport error. `kind` is one of: notfound, timeout, other."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


def _get_token() -> str:
    return os.environ.get("HA_TOKEN", "").strip()


def _get_ha_url() -> str:
    return os.environ.get("HA_URL", "http://127.0.0.1:8123").rstrip("/")


def _http_request(
    method: str, url: str, token: str, body: Optional[dict] = None
) -> Any:
    """Perform one HA REST call. Returns parsed JSON (or None). Raises _HAError."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise _HAError("notfound" if e.code in (400, 404) else "other") from e
    except socket.timeout as e:
        raise _HAError("timeout") from e
    except urllib.error.URLError as e:
        raise _HAError("timeout" if isinstance(e.reason, socket.timeout) else "connection") from e
    except ValueError as e:
        # Covers json.JSONDecodeError and UnicodeDecodeError (e.g. HA behind
        # a proxy returning an HTML error page instead of JSON) — both are
        # ValueError subclasses.
        raise _HAError("other") from e


def _ha_error_message(e: _HAError, entity_id: str) -> str:
    if e.kind == "notfound":
        return f"I couldn't find anything called {_friendly_for(entity_id)}."
    if e.kind == "timeout":
        return "Home Assistant didn't respond in time."
    return "I couldn't reach Home Assistant."


# ---------------------------------------------------------------------------
# Entity alias cache — lazy, loaded on first use, never poisoned by failure,
# and refreshed on a lookup miss so renaming a device doesn't break voice
# control until the process restarts.
# ---------------------------------------------------------------------------

_ALIASES: dict[str, str] = {}
_ALIASES_LOADED = False
# When the last FORCED refresh ran -- deliberately not stamped by the initial
# load, or the cooldown would swallow the very first miss after startup and the
# refresh path would never fire at all.
_ALIASES_REFRESHED_AT = 0.0
# A miss is the only signal that the snapshot might be stale, so a refresh is
# driven by misses rather than a timer. The cooldown stops a user saying
# something HA genuinely doesn't have from re-fetching every entity each time.
_ALIAS_REFRESH_COOLDOWN_S = 30.0
_ALIAS_LOCK = threading.Lock()


def _ensure_aliases(force: bool = False) -> Optional[str]:
    """Populate `_ALIASES` on first use, or refresh it when `force` is set and
    the cooldown has elapsed. Returns an error message, else None.

    A forced refresh that fails returns None, not an error: we already have a
    working snapshot, so a transient HA blip should degrade to "possibly
    stale", never to a spoken error.
    """
    global _ALIASES, _ALIASES_LOADED, _ALIASES_REFRESHED_AT
    with _ALIAS_LOCK:
        if _ALIASES_LOADED and not force:
            return None
        if force and (time.monotonic() - _ALIASES_REFRESHED_AT) < _ALIAS_REFRESH_COOLDOWN_S:
            return None
        try:
            states = _http_request("GET", f"{_get_ha_url()}/api/states", _get_token())
        except _HAError:
            # Leave the cache as-is (not marked loaded on a cold start) so the
            # next call retries.
            return None if _ALIASES_LOADED else "I couldn't reach Home Assistant."
        aliases: dict[str, str] = {}
        for state in states or []:
            entity_id = state.get("entity_id")
            if not entity_id:
                continue
            friendly = (state.get("attributes") or {}).get("friendly_name") or entity_id
            aliases.setdefault(friendly.lower(), entity_id)
        # Single rebind, not clear()+update(): `_resolve_entity` reads this
        # dict without the lock, and a two-step swap would let it observe an
        # empty or half-filled mapping mid-refresh and report "couldn't find"
        # for a device that exists.
        _ALIASES = aliases
        _ALIASES_LOADED = True
        if force:
            _ALIASES_REFRESHED_AT = time.monotonic()
        return None


def _strip_leading_filler(name: str) -> str:
    for filler in _LEADING_FILLERS:
        if name.startswith(filler):
            return name[len(filler):]
    return name


def _resolve_entity(name: str) -> Optional[str]:
    """Resolve a spoken name to an entity_id, or None if nothing matches.

    Order: exact alias match, suffix strip/add variants, token-superset
    fuzzy match (shortest alias wins), raw entity_id passthrough.
    """
    key = _strip_leading_filler(name.lower().strip())
    if key in _ALIASES:
        return _ALIASES[key]

    head, _, tail = key.rpartition(" ")
    if head and tail in _NAME_SUFFIXES and head in _ALIASES:
        return _ALIASES[head]
    for suffix in _NAME_SUFFIXES:
        candidate = f"{key} {suffix}"
        if candidate in _ALIASES:
            return _ALIASES[candidate]

    input_tokens = set(key.split())
    if input_tokens:
        candidates = [
            (alias, eid)
            for alias, eid in _ALIASES.items()
            if input_tokens.issubset(set(alias.split()))
        ]
        if candidates:
            candidates.sort(key=lambda kv: len(kv[0]))
            return candidates[0][1]

    if "." in name:
        return name
    return None


def _friendly_for(entity_id: str) -> str:
    for alias, eid in _ALIASES.items():
        if eid == entity_id:
            return alias
    slug = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
    return slug.replace("_", " ")


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else "homeassistant"


# ---------------------------------------------------------------------------
# Deny-list — optional, enforced inside the service-call function so nothing
# that reaches _call_service can bypass it.
# ---------------------------------------------------------------------------


def _load_denylist() -> set[str]:
    tools_dir = os.environ.get("VOICE_TOOLS_DIR", "")
    if not tools_dir:
        return set()
    path = os.path.join(tools_dir, "ha_denylist.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(e) for e in data}


def _call_service(
    domain: str,
    service: str,
    entity_id: str,
    data: Optional[dict] = None,
    success_message: str = "Okay.",
) -> str:
    if entity_id in _load_denylist():
        return f"Sorry, {_friendly_for(entity_id)} isn't available for voice control."
    payload: dict = {"entity_id": entity_id}
    if data:
        payload.update(data)
    try:
        _http_request(
            "POST", f"{_get_ha_url()}/api/services/{domain}/{service}", _get_token(), payload
        )
    except _HAError as e:
        return _ha_error_message(e, entity_id)
    return success_message


def _run_query_power(entity_id: str) -> str:
    friendly = _friendly_for(entity_id)
    if entity_id in _load_denylist():
        return f"Sorry, {friendly} isn't available for voice control."
    try:
        state = _http_request(
            "GET", f"{_get_ha_url()}/api/states/{entity_id}", _get_token()
        )
    except _HAError as e:
        return _ha_error_message(e, entity_id)
    current = (state or {}).get("state", "")
    if current == "on":
        return f"Yes, {friendly} is on."
    if current == "off":
        return f"No, {friendly} is off."
    if current:
        return f"{friendly}'s state is {current}."
    return f"I couldn't get {friendly}'s status."


def _run_query_temperature(entity_id: str) -> str:
    friendly = _friendly_for(entity_id)
    if entity_id in _load_denylist():
        return f"Sorry, {friendly} isn't available for voice control."
    try:
        state = _http_request(
            "GET", f"{_get_ha_url()}/api/states/{entity_id}", _get_token()
        )
    except _HAError as e:
        return _ha_error_message(e, entity_id)
    attrs = (state or {}).get("attributes") or {}
    value = attrs.get("current_temperature")
    if value is None:
        try:
            value = float((state or {}).get("state", ""))
        except (TypeError, ValueError):
            value = None
    if value is None:
        return f"I couldn't get the temperature for {friendly}."
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"It's {value} degrees in {friendly}."


def _dispatch(intent: Intent, entity_id: Optional[str]) -> str:
    friendly = _friendly_for(entity_id) if entity_id else ""
    if intent.action == "turn_on":
        return _call_service(
            _domain_of(entity_id), "turn_on", entity_id,
            success_message=f"Okay, turning on {friendly}.",
        )
    if intent.action == "turn_off":
        return _call_service(
            _domain_of(entity_id), "turn_off", entity_id,
            success_message=f"Okay, turning off {friendly}.",
        )
    if intent.action == "toggle":
        return _call_service(
            _domain_of(entity_id), "toggle", entity_id,
            success_message=f"Okay, toggling {friendly}.",
        )
    if intent.action == "set_brightness":
        return _call_service(
            _domain_of(entity_id), "turn_on", entity_id,
            data={"brightness_pct": intent.value},
            success_message=f"Setting {friendly} brightness to {intent.value} percent.",
        )
    if intent.action == "set_color":
        rgb = _COLOR_MAP.get(intent.value, [255, 255, 255])
        return _call_service(
            _domain_of(entity_id), "turn_on", entity_id,
            data={"rgb_color": rgb},
            success_message=f"Setting {friendly} to {intent.value}.",
        )
    if intent.action == "activate_scene":
        return _call_service(
            _domain_of(entity_id), "turn_on", entity_id,
            success_message=f"Activating {friendly}.",
        )
    if intent.action == "query_power":
        return _run_query_power(entity_id)
    if intent.action == "query_temperature":
        return _run_query_temperature(entity_id)
    return _CAPABILITIES_MSG


TOOL_DEF = {
    "type": "function",
    "name": "home_assistant",
    "description": (
        "Control Home Assistant smart-home devices: turn lights, switches, "
        "or fans on or off, toggle them, set a light's brightness or color, "
        "activate a scene, or check whether something is on or a room's "
        "temperature. Give the whole request as natural language in "
        "`command`, e.g. 'turn on the kitchen lights' or 'set the office "
        "lamp to blue'. Handles one device or action per call — for "
        "multiple actions, call this tool once per action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "The home automation request in natural language, e.g. "
                    "'turn off the living room lights' or 'activate the "
                    "movie night scene'."
                ),
            }
        },
        "required": ["command"],
    },
}


def run(arg: Optional[str] = None) -> str:
    command = (arg or "").strip()
    if not command:
        return "I need a command."
    if not _get_token():
        return "Home Assistant isn't set up yet."

    intent = parse_command(command)
    if intent is None:
        return _CAPABILITIES_MSG

    err = _ensure_aliases()
    if err:
        return err

    entity_id = None
    if intent.entity is not None:
        entity_id = _resolve_entity(intent.entity)
        if entity_id is None:
            # The alias map is a snapshot taken on first use. A device renamed
            # or added in Home Assistant since then would miss here forever,
            # so treat a miss as the cue to re-read the entity list and try
            # once more before telling the user it doesn't exist.
            _ensure_aliases(force=True)
            entity_id = _resolve_entity(intent.entity)
        if entity_id is None:
            return f"I couldn't find anything called {intent.entity}."

    return _dispatch(intent, entity_id)
