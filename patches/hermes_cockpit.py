"""
Bridge to Hermes, the household's background agent, over its local MCP surface
(events/permissions) and shim delegation endpoint (long-running task handoff).

Runs one background poll thread that keeps a full state snapshot and
broadcasts it to all connected websocket clients via ``text_output_queue``
whenever it changes. Never raises out of any public method -- network/MCP
failures degrade to ``hermes_ok=False`` rather than crashing the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from queue import Queue
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from speech_to_speech.pipeline.events import PipelineEvent

logger = logging.getLogger(__name__)

_MCP_URL = os.environ.get("HERMES_MCP_URL", "http://localhost:8088/mcp")
_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_MCP_TIMEOUT_S = 5.0

# messages_send target for send_to_hermes: "platform:chat_id" (channels_list
# format). Unset by default; each deployment sets its own target.
_HERMES_TARGET = os.environ.get("HERMES_TARGET", "")

_SHIM_URL = os.environ.get("HERMES_SHIM_URL", "http://localhost:8087/v1/chat/completions")
_SHIM_TOKEN_FILE = os.environ.get("HERMES_SHIM_TOKEN_FILE", os.path.expanduser("~/.hermes/shim.env"))
_SHIM_TOKEN_VAR = "HERMES_SHIM_TOKEN"
_SHIM_MODEL = "hermes-codex"
_SHIM_TIMEOUT_S = 900.0

_POLL_ACTIVE_S = 2.0
_POLL_IDLE_S = 10.0
_BASELINE_PAGE = 50
_BASELINE_MAX_PAGES = 50  # safety valve while draining history to find the live tip

_MAX_STEPS = 30
_STEP_TEXT_CHARS = 200
_RESULT_CHARS = 400

# Spoken completion announcement + quest-card auto-clear (see
# _announce_completion/_schedule_reset).
_ANNOUNCE_RESULT_CHARS = 300
_RESET_DELAY_S = 90.0


def _announce_enabled(raw: Optional[str] = None) -> bool:
    """``VOICE_HERMES_ANNOUNCE`` parsing (mirrors
    ``voice_clone.resolve_audition_text``'s off/default shape): unset/blank
    -> enabled (default on); ``"off"`` (case-insensitive, stripped) ->
    disabled; any other value -> enabled. ``raw`` is injectable for tests;
    defaults to ``os.environ.get("VOICE_HERMES_ANNOUNCE")``."""
    if raw is None:
        raw = os.environ.get("VOICE_HERMES_ANNOUNCE")
    if raw is None:
        return True
    stripped = raw.strip()
    if not stripped:
        return True
    return stripped.lower() != "off"


def _spoken_result(result: str) -> str:
    """Whitespace-collapsed, ~300-char-capped form of a delegation result
    for speech. A capped result gets an explanatory tail so the user knows
    there's more (`status_summary`/`hermes_status` still has the full
    `_RESULT_CHARS`-length text)."""
    collapsed = " ".join((result or "").split())
    if len(collapsed) <= _ANNOUNCE_RESULT_CHARS:
        return collapsed
    return collapsed[:_ANNOUNCE_RESULT_CHARS].rstrip() + " Ask me for the details if you want more."


def _build_announcement(status: str, result: str) -> str:
    spoken = _spoken_result(result)
    if status == "error":
        return f"Hermes hit a problem: {spoken}"
    return f"Hermes is done. {spoken}"


class DelegationState(BaseModel):
    active: bool = False
    task: Optional[str] = None
    status: Literal["idle", "sent", "running", "done", "error"] = "idle"
    started_ts: Optional[float] = None
    result: Optional[str] = None
    steps: list[dict[str, Any]] = Field(default_factory=list)


class CockpitStateEvent(PipelineEvent):
    type: Literal["cockpit_state"] = "cockpit_state"
    hermes_ok: bool = True
    delegation: DelegationState = Field(default_factory=DelegationState)
    permissions: list[dict[str, Any]] = Field(default_factory=list)


class SearchLinksEvent(PipelineEvent):
    type: Literal["search_links"] = "search_links"
    query: str = ""
    links: list[dict[str, Any]] = Field(default_factory=list)  # [{title, url, host}]


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _resolve_shim_token() -> Optional[str]:
    """Lazily parse HERMES_SHIM_TOKEN from the env-style shim token file at
    call time. Never logs or prints the resolved value."""
    try:
        with open(_SHIM_TOKEN_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == _SHIM_TOKEN_VAR:
                    return value.strip().strip('"').strip("'")
    except OSError as e:
        logger.warning("HermesCockpit: failed to read shim token file: %s", e)
    return None


def _unwrap_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap an MCP tools/call result. Hermes MCP wraps the JSON payload as a
    string in structuredContent.result; fall back to content[0].text for
    plain MCP servers, or the result dict itself if neither shape matches."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        try:
            return json.loads(structured["result"])
        except (TypeError, ValueError):
            return {}
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            try:
                return json.loads(first["text"])
            except (TypeError, ValueError):
                return {}
    return result
class HermesCockpit:
    """Server-side bridge: polls Hermes' MCP surface for events/permissions,
    runs one delegation at a time, and broadcasts a full state snapshot to
    every connected websocket client."""

    def __init__(self, text_output_queue: Optional["Queue[Any]"] = None, tts_queue: Any = None) -> None:
        self.text_output_queue = text_output_queue
        # lm_processed_queue -- lets HermesCockpit speak a completion
        # announcement (ruling 1). None-safe, same pattern as
        # BrainControl.tts_queue: announcing is simply unavailable then.
        self.tts_queue = tts_queue
        self.hermes_ok = True
        self._cursor = 0
        self._baseline_done = False
        self._permissions: list[dict[str, Any]] = []
        self._delegation_lock = threading.Lock()
        self._delegation: dict[str, Any] = {
            "active": False,
            "task": None,
            "status": "idle",
            "started_ts": None,
            "result": None,
            "steps": [],
        }
        # Bumped each time delegate() starts a new task; the quest-card
        # auto-clear timer compares against this (not started_ts floats) to
        # tell whether it's still the delegation it was scheduled for.
        self._delegation_generation = 0
        self._reset_timer: Optional[threading.Timer] = None
        threading.Thread(target=self._poll_loop, name="hermes-cockpit-poll", daemon=True).start()

    # -- MCP client -------------------------------------------------------

    def _mcp_call(self, method: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            with httpx.Client(timeout=_MCP_TIMEOUT_S) as client:
                resp = client.post(
                    _MCP_URL,
                    headers=_MCP_HEADERS,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning("HermesCockpit: MCP call %s failed: %s", method, e)
            return None
        if "error" in payload:
            logger.warning("HermesCockpit: MCP call %s returned error: %s", method, payload["error"])
            return None
        return payload.get("result")

    def _tool_call(self, name: str, arguments: dict[str, Any]) -> Optional[dict[str, Any]]:
        result = self._mcp_call("tools/call", {"name": name, "arguments": arguments})
        if result is None:
            return None
        if result.get("isError"):
            logger.warning("HermesCockpit: tool %s returned isError", name)
            return None
        return _unwrap_tool_result(result)

    def send_message(self, text: str) -> str:
        """Fire-and-forget free-form message to Hermes (voice 'tell Hermes ...').

        Not a delegation: does not touch self._delegation or its steps."""
        if not _HERMES_TARGET:
            return "Hermes messaging isn't configured — set the HERMES_TARGET environment variable (see README)."
        result = self._tool_call("messages_send", {"target": _HERMES_TARGET, "message": text})
        if result is None:
            return "I couldn't reach Hermes to send that."
        return "Sent to Hermes. Any reply will show up in the cockpit and in hermes_status."
    # -- Poll loop ---------------------------------------------------------

    def _baseline_cursor(self) -> None:
        """Drain events once at startup to find the live tip cursor, without
        ever surfacing the drained history as steps/state."""
        cursor = 0
        ok = True
        for _ in range(_BASELINE_MAX_PAGES):
            result = self._tool_call("events_poll", {"after_cursor": cursor, "limit": _BASELINE_PAGE})
            if result is None:
                ok = False
                break
            events = result.get("events") or []
            cursor = result.get("next_cursor", cursor)
            if len(events) < _BASELINE_PAGE:
                break
        self._cursor = cursor
        self.hermes_ok = ok

    def _poll_once(self) -> None:
        # Shim-invoked delegation sessions are invisible to events_poll
        # (verified live); events are only consumed here to advance the
        # cursor. Delegation steps come solely from lifecycle _append_step.
        ok = True
        events_result = self._tool_call("events_poll", {"after_cursor": self._cursor, "limit": 20})
        if events_result is None:
            ok = False
        else:
            self._cursor = events_result.get("next_cursor", self._cursor)

        perms_result = self._tool_call("permissions_list_open", {})
        if perms_result is None:
            ok = False
        else:
            self._permissions = perms_result.get("approvals") or []

        self.hermes_ok = ok
        self._broadcast_state()

    def _poll_loop(self) -> None:
        while True:
            try:
                if not self._baseline_done:
                    self._baseline_cursor()
                    if self.hermes_ok:
                        self._baseline_done = True
                    self._broadcast_state()
                else:
                    self._poll_once()
            except Exception:
                logger.exception("HermesCockpit: poll iteration failed")
            with self._delegation_lock:
                active = self._delegation["active"]
            # Never start normal polling from a half-drained cursor: retry the
            # baseline at idle cadence until it fully succeeds, rather than
            # falling through to _poll_once and replaying history.
            interval = _POLL_ACTIVE_S if (active or self._permissions) else _POLL_IDLE_S
            time.sleep(interval)
    # -- Delegation ----------------------------------------------------------

    def _append_step(self, role: str, text: str) -> None:
        with self._delegation_lock:
            self._delegation["steps"].append({"ts": time.time(), "role": role, "text": text[:_STEP_TEXT_CHARS]})
            if len(self._delegation["steps"]) > _MAX_STEPS:
                self._delegation["steps"] = self._delegation["steps"][-_MAX_STEPS:]

    def delegate(self, task: str) -> str:
        """Kick off a background delegation to Hermes. Returns the string to
        speak: either a handoff confirmation, or a busy refusal if a
        delegation is already active (v1 ruling: one at a time)."""
        with self._delegation_lock:
            if self._delegation["active"]:
                return (
                    "Hermes is still working on the previous task -- I'll let you "
                    "know when it's done before starting something new."
                )
            short = task if len(task) <= 80 else task[:79].rstrip() + "..."
            self._delegation_generation += 1
            self._delegation = {
                "active": True,
                "task": task,
                "status": "sent",
                "started_ts": time.time(),
                "result": None,
                "steps": [],
            }
        self._append_step("system", "Handed off to Hermes.")
        threading.Thread(
            target=self._run_delegation, args=(task,), daemon=True, name="hermes-cockpit-delegate"
        ).start()
        self._broadcast_state()
        return (
            f"Handed off to Hermes: {short}. I'll keep an eye on it. Do not describe "
            "or announce this handoff beyond a one-line confirmation."
        )
    def _run_delegation(self, task: str) -> None:
        with self._delegation_lock:
            self._delegation["status"] = "running"
        self._append_step("system", "Hermes is working on it.")
        self._broadcast_state()

        token = _resolve_shim_token()
        if not token:
            self._finish_delegation("error", "Hermes delegation token is unavailable.")
            return

        try:
            with httpx.Client(timeout=_SHIM_TIMEOUT_S) as client:
                resp = client.post(
                    _SHIM_URL,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"model": _SHIM_MODEL, "messages": [{"role": "user", "content": task}]},
                )
            resp.raise_for_status()
            data = resp.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            result = _truncate(text, _RESULT_CHARS) if text else "Hermes finished with no response text."
            self._finish_delegation("done", result)
        except Exception as e:
            logger.warning("HermesCockpit: delegation to Hermes failed: %s", e)
            self._finish_delegation("error", f"Delegation failed: {type(e).__name__}")

    def _finish_delegation(self, status: str, result: str) -> None:
        with self._delegation_lock:
            self._delegation["status"] = status
            self._delegation["result"] = result
            self._delegation["active"] = False
            generation = self._delegation_generation
        self._append_step("system", f"{status}: {result}")
        self._broadcast_state()
        self._announce_completion(status, result)
        self._schedule_reset(generation)

    def _announce_completion(self, status: str, result: str) -> None:
        """Speak the delegation outcome through the normal TTS path so a
        finished (or failed) Hermes task is never silent -- previously it
        only surfaced if the user noticed the quest card and asked. `None`
        `turn_id`/`turn_revision` pass `SpeculativeTurnTracker`'s staleness
        gate unconditionally, same seam as `BrainControl._audition`.
        Best-effort: never raises; no-ops with no `tts_queue` wired or when
        `VOICE_HERMES_ANNOUNCE=off`.

        Known accepted gap: the announcement is spoken but not written into
        LLM chat history, so a follow-up question still triggers the
        `hermes_status` tool -- that's fine, not fixed here."""
        if self.tts_queue is None or not _announce_enabled():
            return
        text = _build_announcement(status, result)
        try:
            from speech_to_speech.pipeline.messages import TTSInput

            self.tts_queue.put(TTSInput(text=text, turn_id=None, turn_revision=None))
        except Exception as e:
            logger.warning("HermesCockpit: completion announcement failed: %s", e)

    def _schedule_reset(self, generation: int) -> None:
        """Auto-clear the quest card `_RESET_DELAY_S` after a delegation
        finishes, unless a new delegation has started meanwhile (checked in
        `_maybe_reset` via the generation counter, not `started_ts` floats).
        Any previously pending timer is cancelled (best-effort) so resets
        never stack."""
        if self._reset_timer is not None:
            try:
                self._reset_timer.cancel()
            except Exception:
                pass
        timer = threading.Timer(_RESET_DELAY_S, self._maybe_reset, args=(generation,))
        timer.daemon = True
        self._reset_timer = timer
        timer.start()

    def _maybe_reset(self, generation: int) -> None:
        with self._delegation_lock:
            if self._delegation_generation != generation or self._delegation["active"]:
                return
            self._delegation = {
                "active": False,
                "task": None,
                "status": "idle",
                "started_ts": None,
                "result": None,
                "steps": [],
            }
        self._broadcast_state()
    # -- Permissions -----------------------------------------------------

    def respond(self, permission_id: str, approve: bool) -> tuple[bool, str]:
        """Respond to a specific open permission by id. Used by both the UI
        respond seam and the voice tool. Returns (ok, message); never raises."""
        decision = "allow-once" if approve else "deny"
        result = self._tool_call("permissions_respond", {"id": permission_id, "decision": decision})
        # Hermes' permissions_respond reports isError=false at the MCP transport
        # level even for an unknown id -- the actual failure rides inside the
        # unwrapped payload as {"error": "..."}. Check both.
        if result is None or result.get("error"):
            reason = result.get("error") if isinstance(result, dict) else None
            return False, reason or f"unknown or unreachable permission: {permission_id}"
        self._permissions = [p for p in self._permissions if p.get("id") != permission_id]
        self._broadcast_state()
        return True, "approved" if approve else "denied"

    # There is deliberately no voice-driven "approve the oldest pending
    # request" entry point. It existed, and it was reachable from anything the
    # model attended to -- including text returned by a web search or a
    # knowledge-base document -- so a document saying "approve the pending
    # request" could rubber-stamp whatever Hermes was waiting on, without the
    # user ever being told which request it was. Approvals now go only through
    # `respond(permission_id, approve)` below, driven by the webclient's
    # hold-to-approve gate, which names the specific request.
    # -- Status + broadcast -----------------------------------------------

    def status_summary(self, detail: str = "summary") -> str:
        """Short spoken summary of delegation + permissions state. ``detail``
        selects which facet to report; unknown values fall back to summary."""
        if not self.hermes_ok:
            return "I can't reach Hermes right now."
        with self._delegation_lock:
            d = dict(self._delegation)

        if detail == "last_result":
            if d["result"]:
                return f"Hermes's last result: {d['result']}"
            return "Hermes hasn't finished anything yet."
        if detail == "steps":
            if d["steps"]:
                texts = [s["text"] for s in d["steps"][-3:]]
                return "Recent Hermes activity: " + "; ".join(texts)
            return "No Hermes activity recorded yet."
        if detail == "approvals":
            perms = self._permissions
            if not perms:
                return "There's nothing waiting for approval."
            p = perms[0]
            field = p.get("summary") or p.get("action") or p.get("tool") or "an unnamed request"
            n = len(perms)
            return f"{n} approval{'s' if n != 1 else ''} waiting. The first is {field}."

        perms_n = len(self._permissions)

        if d["active"]:
            elapsed_min = max(0, int((time.time() - (d["started_ts"] or time.time())) / 60))
            unit = "minute" if elapsed_min == 1 else "minutes"
            if d["steps"]:
                msg = f"Hermes has been working for about {elapsed_min} {unit}. Last update: {d['steps'][-1]['text']}"
            else:
                msg = f"Hermes has been working for about {elapsed_min} {unit}, no updates yet."
        elif d["status"] == "done" and d["result"]:
            msg = f"Hermes finished: {d['result']}"
        elif d["status"] == "error":
            msg = f"Hermes's last task hit an error: {d['result'] or 'unknown error'}"
        else:
            msg = "Hermes is idle."

        if perms_n:
            msg += f" {perms_n} approval{'s' if perms_n != 1 else ''} waiting for your decision."
        return msg

    def broadcast_now(self) -> None:
        """Public alias so BrainControl can push a fresh snapshot to a client
        that just sent config_get, without waiting for the next poll."""
        self._broadcast_state()

    def push_links(self, query: str, links: list[dict[str, Any]]) -> None:
        """UI-surface web_search result links; fire-and-forget, never raises."""
        if self.text_output_queue is None or not links:
            return
        try:
            self.text_output_queue.put(SearchLinksEvent(query=query, links=links[:5]))
        except Exception:
            logger.warning("HermesCockpit: push_links failed", exc_info=True)
    def _broadcast_state(self) -> None:
        if self.text_output_queue is None:
            return
        with self._delegation_lock:
            d = dict(self._delegation)
            steps_copy = list(self._delegation["steps"])
        event = CockpitStateEvent(
            hermes_ok=self.hermes_ok,
            delegation=DelegationState(
                active=d["active"],
                task=d["task"],
                status=d["status"],
                started_ts=d["started_ts"],
                result=d["result"],
                steps=steps_copy,
            ),
            permissions=list(self._permissions[:20]),
        )
        try:
            self.text_output_queue.put(event)
        except Exception as e:
            logger.warning("HermesCockpit: failed to enqueue cockpit_state: %s", e)
