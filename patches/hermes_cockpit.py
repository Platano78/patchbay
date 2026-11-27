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
import re
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



def _describe_duration(seconds: float) -> str:
    if seconds >= 120:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds)} seconds"


def _describe_delegation_failure(exc: BaseException) -> str:
    """A plain phrase for a failed delegation, spoken to the user: the status
    code or the kind of failure, never a class name, URL or host (the full
    exception stays in the log line)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 504:
            return f"the Hermes gateway timed out ({code})"
        return f"Hermes answered with an error ({code})"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "Hermes could not be reached"
    if isinstance(exc, httpx.TimeoutException):
        return f"Hermes timed out after {_describe_duration(_SHIM_TIMEOUT_S)}"
    if isinstance(exc, httpx.TransportError):
        return "Hermes could not be reached"
    return "something went wrong on the way to Hermes"


_POLL_ACTIVE_S = 2.0
_POLL_IDLE_S = 10.0
# Backoff cap while the MCP endpoint stays unreachable and idle: doubling from
# _POLL_IDLE_S would otherwise grow unbounded, and 60s is still frequent
# enough to notice an agent coming back without hammering a dead port.
_POLL_BACKOFF_MAX_S = 60.0
_BASELINE_PAGE = 50
_BASELINE_MAX_PAGES = 50  # safety valve while draining history to find the live tip

_MAX_STEPS = 30
_STEP_TEXT_CHARS = 200
_RESULT_CHARS = 400
# Telegram caps a message at 4096 chars; leave room for the lead-in line.
_NOTIFY_CHARS = 3500

# Spoken completion announcement + quest-card auto-clear (see
# _announce_completion/_schedule_reset).
# 160 chars + the 41-char tail + "Hermes is done. " is ~217 chars: ~13.6 s at
# the ~16 chars/s the remote TTS speaks, under its 15 s total-duration cap
# (a longer announcement fails over to the fallback voice mid-sentence).
_ANNOUNCE_RESULT_CHARS = 160
_ANNOUNCE_TAIL = " Ask me for the details if you want more."
_ANNOUNCE_MIN_SENTENCE_CHARS = 60
# An absolute path token ("/a/b", "~/a/b"): read aloud it is seconds of noise.
# It may be wrapped in one quote, bracket or backtick (Hermes writes markdown);
# the wrapper goes with it. A bare one-segment "/etc" is left alone: too close
# to ordinary words like "and/or".
_PATH_TOKEN_RE = re.compile(r"(?<!\S)([\"'(\[`])?(~/\S*|/\S*)")
_PATH_TRAILING = ".,;:!?)]\"'`"
_PATH_KEEP_AFTER_WRAPPER = ".,;:!?"
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


def _speak_paths(text: str) -> str:
    def _sub(m: "re.Match[str]") -> str:
        opener, token = m.group(1), m.group(2)
        body = token.rstrip(_PATH_TRAILING)
        trailing = token[len(body):]
        is_path = len(body) > 2 if body.startswith("~/") else "/" in body[1:]
        if not is_path:
            return m.group(0)
        if opener:  # the wrapper's closer goes with the path; keep sentence punctuation
            trailing = "".join(c for c in trailing if c in _PATH_KEEP_AFTER_WRAPPER)
        return "a file" + trailing

    return _PATH_TOKEN_RE.sub(_sub, text)


def _spoken_result(result: str) -> str:
    """Whitespace-collapsed form of a delegation result for speech: absolute
    file paths become "a file" (the full text stays in the delegation
    record, `hermes_status` and the card), and anything over
    `_ANNOUNCE_RESULT_CHARS` is cut at the last sentence end (past
    `_ANNOUNCE_MIN_SENTENCE_CHARS`) or, failing that, the last space, and
    gets a tail so the user knows there's more."""
    collapsed = _speak_paths(" ".join((result or "").split()))
    if len(collapsed) <= _ANNOUNCE_RESULT_CHARS:
        return collapsed
    head = collapsed[: _ANNOUNCE_RESULT_CHARS + 1]
    end = max(head.rfind(p) for p in (". ", "! ", "? ")) + 1  # keep the punctuation
    if end > _ANNOUNCE_MIN_SENTENCE_CHARS:
        cut = collapsed[:end]
    else:
        space = head.rfind(" ")
        cut = collapsed[:space] if space > 0 else collapsed[:_ANNOUNCE_RESULT_CHARS]
    return cut.rstrip() + _ANNOUNCE_TAIL


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

    def __init__(
        self,
        text_output_queue: Optional["Queue[Any]"] = None,
        tts_queue: Any = None,
        runtime_config: Any = None,
    ) -> None:
        self.text_output_queue = text_output_queue
        # The pipeline's RuntimeConfig: a finished delegation is written into
        # `runtime_config.chat` so the brain knows the result. None-safe.
        self.runtime_config = runtime_config
        # lm_processed_queue -- lets HermesCockpit speak a completion
        # announcement (ruling 1). None-safe, same pattern as
        # BrainControl.tts_queue: announcing is simply unavailable then.
        self.tts_queue = tts_queue
        self.hermes_ok = True
        # Consecutive transport-failure counter (reset to 0 on any successful
        # _mcp_call): drives both the "warn once, then go quiet" log
        # suppression and the idle-poll backoff below. Deliberately NOT
        # incremented for a payload-level {"error": ...} response -- that
        # path means something answered and is misbehaving, which stays at
        # WARNING every time (see _mcp_call).
        self._consecutive_transport_failures = 0
        # Consecutive failed *poll iterations* while idle (distinct from the
        # per-call counter above, which drives log suppression): drives the
        # idle-poll backoff in _poll_loop. Reset to 0 by any successful
        # iteration or whenever a delegation/approval makes polling active.
        self._idle_failure_streak = 0
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
        # The last *finished* delegation. `_maybe_reset` clears the quest card
        # (`_delegation`) but never this: status answers read it, so Hermes
        # does not report "idle, nothing recorded" about a task it finished.
        # Overwritten only when the next delegation finishes.
        self._last_delegation: Optional[dict[str, Any]] = None
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
            # No agent configured is the normal state for most self-hosters
            # (SETUP.md step 6, docs/agent-lane.md), and this fires every
            # idle poll -- so only the FIRST consecutive transport failure
            # gets a WARNING; the rest are DEBUG. Any later success resets
            # the counter, so a real outage still produces a fresh WARNING.
            self._consecutive_transport_failures += 1
            if self._consecutive_transport_failures == 1:
                logger.warning(
                    "HermesCockpit: MCP call %s to %s timed out or failed: %s -- this is expected if "
                    "you haven't configured an agent; see docs/agent-lane.md and "
                    "examples/agent-lane/verify_contract.py if you have. Further failures logged at DEBUG.",
                    method,
                    _MCP_URL,
                    e,
                )
            else:
                logger.debug("HermesCockpit: MCP call %s failed: %s", method, e)
            return None
        self._consecutive_transport_failures = 0
        if "error" in payload:
            # The endpoint answered -- something is present and misbehaving.
            # Always WARNING, unlike the transport-failure path above.
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
        return "Posted to your chat with Hermes. Hermes doesn't act on these; to have it do something, delegate it."
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
            time.sleep(self._next_poll_interval())

    def _next_poll_interval(self) -> float:
        """Sleep interval for the next poll loop iteration. Pulled out of
        `_poll_loop` so the backoff math is unit-testable without a real
        `time.sleep`. Never start normal polling from a half-drained cursor:
        the baseline retries at idle cadence (subject to the same backoff)
        until it fully succeeds, rather than falling through to `_poll_once`
        and replaying history."""
        with self._delegation_lock:
            active = self._delegation["active"]
        if active or self._permissions:
            # An active delegation or pending approvals always polls at the
            # fast cadence -- backoff applies only to the failing-and-idle
            # case below.
            self._idle_failure_streak = 0
            return _POLL_ACTIVE_S
        if self.hermes_ok:
            self._idle_failure_streak = 0
            return _POLL_IDLE_S
        # Bounded exponential backoff: 10s, 20s, 40s, 60s, 60s, ... A dead
        # endpoint polled every 10s forever is wasted work (and a full
        # _MCP_TIMEOUT_S wait each time); snaps straight back to
        # _POLL_IDLE_S the moment a poll succeeds.
        self._idle_failure_streak += 1
        return min(_POLL_IDLE_S * (2 ** (self._idle_failure_streak - 1)), _POLL_BACKOFF_MAX_S)
    # -- Delegation ----------------------------------------------------------

    def _append_step_locked(self, role: str, text: str) -> None:
        """Caller holds `_delegation_lock`."""
        self._delegation["steps"].append({"ts": time.time(), "role": role, "text": text[:_STEP_TEXT_CHARS]})
        if len(self._delegation["steps"]) > _MAX_STEPS:
            self._delegation["steps"] = self._delegation["steps"][-_MAX_STEPS:]

    def _append_step(self, role: str, text: str) -> None:
        with self._delegation_lock:
            self._append_step_locked(role, text)

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
            self._finish_delegation("done", result, full_text=text or result)
        except Exception as e:
            logger.warning("HermesCockpit: delegation to Hermes failed: %s", e)
            self._finish_delegation("error", f"Delegation failed: {_describe_delegation_failure(e)}.")

    def _finish_delegation(self, status: str, result: str, full_text: Optional[str] = None) -> None:
        with self._delegation_lock:
            self._delegation["status"] = status
            self._delegation["result"] = result
            self._delegation["active"] = False
            generation = self._delegation_generation
            # Final step and the last-delegation record are built in this one
            # lock hold, so a delegate() landing afterwards cannot be recorded
            # as the task that just finished.
            self._append_step_locked("system", f"{status}: {result}")
            task = self._delegation["task"]
            self._last_delegation = {
                "task": task,
                "status": status,
                "result": result,
                "finished_ts": time.time(),
                "steps": list(self._delegation["steps"]),
            }
        self._broadcast_state()
        self._remember_in_chat(status, task, result)
        self._notify_target(status, task, full_text if full_text else result)
        self._announce_completion(status, result)
        self._schedule_reset(generation)

    def _remember_in_chat(self, status: str, task: Optional[str], result: str) -> None:
        """Append the outcome to the LLM's chat history as one assistant message
        (same shape as chat_completions_language_model's write-back) so the
        brain can tell the user the result instead of guessing. Runs on the
        delegation thread: upstream `Chat.add_item` takes the chat's own lock
        (LLM/chat.py `with self._lock`), so it is safe against the LLM thread.
        `trim_if_needed` is deliberately not called here; the next turn's
        normal write-back trims. Best-effort: never raises."""
        chat = getattr(self.runtime_config, "chat", None)
        if chat is None:
            return
        if status == "done":
            text = f'Hermes finished the task "{task}": {result}'
        else:
            text = f'Hermes could not finish the task "{task}": {result}'
        try:
            from openai.types.realtime.realtime_conversation_item_assistant_message import (
                Content as AssistantContent,
                RealtimeConversationItemAssistantMessage,
            )

            chat.add_item(
                RealtimeConversationItemAssistantMessage(
                    type="message", role="assistant", content=[AssistantContent(type="output_text", text=text)]
                )
            )
        except Exception as e:
            logger.warning("HermesCockpit: could not record the delegation outcome in chat: %s", e)

    def _notify_target(self, status: str, task: Optional[str], full_text: str) -> None:
        """Post the finished delegation to HERMES_TARGET (the owner's own chat)
        so the deliverable is readable later, e.g. when away from the cockpit.
        Off with VOICE_HERMES_NOTIFY=off or no HERMES_TARGET. Runs on a daemon
        thread; never blocks or raises into the finish path."""
        if not _HERMES_TARGET or not _announce_enabled(os.environ.get("VOICE_HERMES_NOTIFY")):
            return
        lead = "Hermes finished your voice request" if status == "done" else "Hermes couldn't finish your voice request"
        body = full_text.strip()
        if len(body) > _NOTIFY_CHARS:
            body = body[: _NOTIFY_CHARS - 1].rstrip() + "\u2026"  # keep line breaks: this is read as text
        task_line = _truncate(task or "", 300)  # the task is uncapped; keep the message under Telegram's 4096
        message = f"{lead}: {task_line}\n\n{body}"

        def _post() -> None:
            try:
                if self._tool_call("messages_send", {"target": _HERMES_TARGET, "message": message}) is None:
                    logger.warning("HermesCockpit: could not post the delegation outcome to HERMES_TARGET")
            except Exception as e:
                logger.warning("HermesCockpit: posting the delegation outcome failed: %s", e)

        try:
            threading.Thread(target=_post, daemon=True, name="hermes-cockpit-notify").start()
        except Exception as e:  # a failed start must not turn a finished task into an error
            logger.warning("HermesCockpit: could not start the delegation-outcome post: %s", e)

    def _announce_completion(self, status: str, result: str) -> None:
        """Speak the delegation outcome through the normal TTS path so a
        finished (or failed) Hermes task is never silent -- previously it
        only surfaced if the user noticed the quest card and asked. `None`
        `turn_id`/`turn_revision` pass `SpeculativeTurnTracker`'s staleness
        gate unconditionally, same seam as `BrainControl._audition`.
        Best-effort: never raises; no-ops with no `tts_queue` wired or when
        `VOICE_HERMES_ANNOUNCE=off`.

        The announcement is speech only; the brain learns the result from
        `_remember_in_chat`, which writes it into the chat history
        independently of this method (and of VOICE_HERMES_ANNOUNCE).
        `hermes_status` also stays truthful because `status_summary` reads
        `_last_delegation`, which the quest-card reset leaves alone."""
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
            last = dict(self._last_delegation) if self._last_delegation else None

        if detail == "last_result":
            result = d["result"] or (last["result"] if last else None)
            if result:
                return f"Hermes's last result: {result}"
            return "Hermes hasn't finished anything yet."
        if detail == "steps":
            steps = d["steps"] or (last["steps"] if last and not d["active"] else [])
            if steps:
                texts = [s["text"] for s in steps[-3:]]
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
        elif last and last["result"]:
            ago = f"{_describe_duration(max(0.0, time.time() - last['finished_ts']))} ago"
            if last["status"] == "error":
                msg = f"Hermes is idle. Its last task hit an error {ago}: {last['result']}"
            else:
                msg = f"Hermes is idle. Its last task finished {ago}: {last['result']}"
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
