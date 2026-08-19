"""Home Assistant control-deck data layer for the web client.

This process holds Home Assistant's websocket connection; the browser never
talks to HA directly. It gets Server-Sent Events instead, so a browser tab
reload or a flaky phone connection never has to redo an HA handshake.

Reuses the *patterns* of examples/tools/home_assistant.py (stdlib-only REST
transport, lazy env-var config) rather than importing it: that file is a
drop-in voice tool meant to be copied into a user's VOICE_TOOLS_DIR, not a
package dependency of the web client, and it has no websocket support.

Endpoints, wired up in serve.py:
  GET  /ha/stream  - Server-Sent Events, one 'state' event per entity change
  GET  /ha/states  - snapshot of every known entity (also the poll backstop)
  POST /ha/intent  - a batch of write intents, one pending unit

Absent unless HA_URL and HA_TOKEN are both set — see `enabled()`. This repo
is public and most users have no Home Assistant instance; the feature must
not exist for them.

Write status is a closed, published enum consumers switch on exhaustively:
`unknown | pending | confirmed | failed`. `stale` is not a fifth state — a
stale write folds into `unknown` with a `reason` string.

`state` is a DIFFERENT field, and it is an OPEN set, not this enum: it is
whatever string Home Assistant reports (`on`, `off`, `unavailable`,
domain-specific values like `heating`/`idle`/`playing`, and this list is not
closed). `unavailable` is a real, live value — a Hue lamp genuinely goes
unreachable, and that string is passed through verbatim rather than
collapsed, because it carries more information than a binary would. This
means any consumer MUST default a `state` value it does not explicitly
handle to unknown, never to off — a renderer that treats an unhandled state
as off would show OFF for a light that is physically unreachable, which is
the exact lie this whole feature exists to prevent (rule 7).

There is deliberately no auth on these routes (owner's LAN, his call), which
makes three things the actual guardrails: the token never leaves this
process, only known entity-id shapes and known services are forwarded, and
every request/response is bounded and timed out.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

# `websockets` is only needed for HA's live push -- everything else in this
# module is urllib/stdlib. It is not a hard dependency of the web client
# (serve.py must boot for every downstream user, HA or not), so a missing
# install degrades to poll-only mode rather than failing import.
try:
    import websockets
except ImportError:
    websockets = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT_S = 5.0
_MAX_BODY_BYTES = 8192
_MAX_BATCH_SIZE = 20
_RECONCILE_INTERVAL_S = 45.0
# No live push in poll-only mode, so poll more often to bound staleness.
_RECONCILE_INTERVAL_DEGRADED_S = 10.0
# Capped exponential-ish ladder; the last value repeats forever rather than
# growing unbounded.
_RECONNECT_BACKOFF_S = (1, 2, 5, 10, 20, 30)
_degraded_logged = False


def _log_degraded_once() -> None:
    global _degraded_logged
    if _degraded_logged:
        return
    _degraded_logged = True
    print(
        "ha_deck: 'websockets' not installed -- running in poll-only mode "
        f"(state updates every {_RECONCILE_INTERVAL_DEGRADED_S:.0f}s instead "
        "of instantly). pip install websockets to enable live push.",
        flush=True,
    )

_ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

WRITE_UNKNOWN = "unknown"
WRITE_PENDING = "pending"
WRITE_CONFIRMED = "confirmed"
WRITE_FAILED = "failed"

# domain -> {service -> allowed data keys (empty set = no extra data allowed)}
# Closed allowlist: a service/domain pair not listed here is rejected before
# it reaches Home Assistant, and any data key not listed for that pair is
# stripped from the forwarded payload rather than passed through.
_ALLOWED_SERVICES: dict[str, dict[str, frozenset[str]]] = {
    "light": {
        "turn_on": frozenset({"brightness_pct", "rgb_color", "color_temp_kelvin"}),
        "turn_off": frozenset(),
        "toggle": frozenset(),
    },
    "switch": {"turn_on": frozenset(), "turn_off": frozenset(), "toggle": frozenset()},
    "fan": {
        "turn_on": frozenset(),
        "turn_off": frozenset(),
        "toggle": frozenset(),
        "set_percentage": frozenset({"percentage"}),
    },
    "cover": {"open_cover": frozenset(), "close_cover": frozenset(), "stop_cover": frozenset()},
    "climate": {
        "set_temperature": frozenset({"temperature"}),
        "set_hvac_mode": frozenset({"hvac_mode"}),
    },
    "scene": {"turn_on": frozenset()},
    "lock": {"lock": frozenset(), "unlock": frozenset()},
    "homeassistant": {"turn_on": frozenset(), "turn_off": frozenset(), "toggle": frozenset()},
}


def _get_token() -> str:
    return os.environ.get("HA_TOKEN", "").strip()


def _get_ha_url() -> str:
    return os.environ.get("HA_URL", "").rstrip("/")


def enabled() -> bool:
    """Whether the deck is configured at all. False => routes stay absent."""
    return bool(_get_token() and _get_ha_url())


def _ha_ws_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):] + "/api/websocket"
    return "ws://" + http_url[len("http://"):] + "/api/websocket"


# ---------------------------------------------------------------------------
# REST transport — same shape as examples/tools/home_assistant.py's
# _http_request, kept local so this module has no import-time dependency on
# a file users are meant to copy/replace.
# ---------------------------------------------------------------------------


class _HAError(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


def _http_request(method: str, url: str, token: str, body: Optional[dict] = None) -> Any:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise _HAError("http_error") from e
    except socket.timeout as e:
        raise _HAError("timeout") from e
    except urllib.error.URLError as e:
        raise _HAError("timeout" if isinstance(e.reason, socket.timeout) else "unreachable") from e
    except ValueError as e:
        raise _HAError("bad_response") from e


# ---------------------------------------------------------------------------
# Entity state
# ---------------------------------------------------------------------------


@dataclass
class EntityState:
    entity_id: str
    domain: str
    state: str = "unknown"
    attributes: dict = field(default_factory=dict)
    last_changed: Optional[str] = None
    write_status: str = WRITE_UNKNOWN
    write_reason: Optional[str] = None
    # Internal only, never published: the monotonic instant the most recent
    # in-flight write for this entity was issued. Used to reject a poll
    # snapshot that started before the write did (rule 9's write-stamp guard).
    write_issued_at: Optional[float] = None

    def to_public(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "domain": self.domain,
            "state": self.state,
            "attributes": self.attributes,
            "last_changed": self.last_changed,
            "write": {"status": self.write_status, "reason": self.write_reason},
        }


def _validate_entity_id(entity_id: Any) -> bool:
    return isinstance(entity_id, str) and bool(_ENTITY_ID_RE.match(entity_id))


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


# ---------------------------------------------------------------------------
# Deck — owns HA connectivity, entity state, and SSE fan-out.
# ---------------------------------------------------------------------------


class Deck:
    def __init__(self, ha_url: str, token: str):
        self._ha_url = ha_url
        self._token = token
        self._state: dict[str, EntityState] = {}
        self._lock = threading.Lock()
        self._subscribers: set = set()
        self._sub_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None

    # -- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        if websockets is None:
            _log_degraded_once()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()

    async def _run(self) -> None:
        self._stop_event = asyncio.Event()
        if websockets is None:
            # No live push available -- poll is the only source of truth, so
            # it runs on its own instead of as a backstop behind a socket.
            await self._poll_only_loop()
            return
        poll_task = asyncio.ensure_future(self._poll_loop())
        try:
            await self._connect_loop()
        finally:
            poll_task.cancel()
            try:
                await poll_task
            except (asyncio.CancelledError, Exception):
                pass

    # -- HA websocket ---------------------------------------------------

    async def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                attempt = 0  # a clean session resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if self._stop_event.is_set():
                return
            delay = _RECONNECT_BACKOFF_S[min(attempt, len(_RECONNECT_BACKOFF_S) - 1)]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _connect_once(self) -> None:
        url = _ha_ws_url(self._ha_url)
        async with websockets.connect(url, open_timeout=_HTTP_TIMEOUT_S) as ws:
            greeting = json.loads(await ws.recv())
            if greeting.get("type") != "auth_required":
                return
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth_result = json.loads(await ws.recv())
            if auth_result.get("type") != "auth_ok":
                return
            await ws.send(json.dumps(
                {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
            ))
            # A fresh REST snapshot on every (re)connect closes any gap that
            # opened while the socket was down, same as a reconcile poll.
            await self._reconcile(started_at=time.monotonic())
            while not self._stop_event.is_set():
                recv = asyncio.ensure_future(ws.recv())
                stop = asyncio.ensure_future(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    {recv, stop}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                if stop in done:
                    return
                msg = json.loads(recv.result())
                event = msg.get("event") or {}
                if msg.get("type") == "event" and event.get("event_type") == "state_changed":
                    self._apply_event(event["data"])

    def _apply_event(self, data: dict) -> None:
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not _validate_entity_id(entity_id) or new_state is None:
            return
        # A live push is authoritative the instant it arrives -- it isn't a
        # snapshot that could have started before an in-flight write, so it
        # always applies (and clears any pending write for this entity).
        self._apply_state(entity_id, new_state, started_at=None)

    # -- poll-only mode (no `websockets` installed) ----------------------

    async def _poll_only_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._reconcile(started_at=time.monotonic())
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=_RECONCILE_INTERVAL_DEGRADED_S
                )
                return  # stop was set
            except asyncio.TimeoutError:
                pass

    # -- reconciliation poll (backstop, live-push mode) ------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=_RECONCILE_INTERVAL_S)
                return  # stop was set
            except asyncio.TimeoutError:
                pass
            await self._reconcile(started_at=time.monotonic())

    async def _reconcile(self, started_at: float) -> None:
        loop = asyncio.get_running_loop()
        try:
            states = await loop.run_in_executor(
                None, _http_request, "GET", f"{self._ha_url}/api/states", self._token, None
            )
        except _HAError:
            return  # transient blip -- next poll or the next reconnect tries again
        for raw in states or []:
            entity_id = raw.get("entity_id")
            if not _validate_entity_id(entity_id):
                continue
            self._apply_state(entity_id, raw, started_at=started_at)

    # -- applying observed HA state, guarded against clobbering a pending write

    def _apply_state(self, entity_id: str, raw: dict, started_at: Optional[float]) -> None:
        domain = _domain_of(entity_id)
        with self._lock:
            ent = self._state.setdefault(entity_id, EntityState(entity_id, domain))
            if ent.write_status == WRITE_PENDING and ent.write_issued_at is not None:
                if started_at is not None and started_at < ent.write_issued_at:
                    # This snapshot began before the in-flight write was
                    # issued -- it cannot know about it, so applying it would
                    # bounce the UI back to pre-write state. Drop it; the
                    # write-stamp guard (rule 9).
                    return
                ent.write_status = WRITE_CONFIRMED
                ent.write_reason = None
                ent.write_issued_at = None
            ent.state = raw.get("state") or "unknown"
            ent.attributes = raw.get("attributes") or {}
            ent.last_changed = raw.get("last_changed") or raw.get("last_updated")
        self._broadcast(entity_id)

    def _mark_unreachable(self, entity_id: str, domain: str, reason: str) -> None:
        with self._lock:
            ent = self._state.setdefault(entity_id, EntityState(entity_id, domain))
            ent.write_status = WRITE_FAILED
            ent.write_reason = reason
            ent.write_issued_at = None
        self._broadcast(entity_id)

    # -- SSE fan-out ------------------------------------------------------

    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue()
        with self._sub_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._sub_lock:
            self._subscribers.discard(q)

    def _broadcast(self, entity_id: str) -> None:
        with self._lock:
            ent = self._state.get(entity_id)
            pub = ent.to_public() if ent else None
        if pub is None:
            return
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(pub)

    def snapshot(self) -> list:
        with self._lock:
            return [ent.to_public() for ent in self._state.values()]

    # -- writes -------------------------------------------------------

    def submit_batch(self, intents: list) -> dict:
        """Issue a batch of write intents as one unit. Returns a JSON-able
        dict with a per-entity outcome; never raises on an individual HA
        failure (that is reported per-entity, not as a batch failure)."""
        batch_id = f"b{time.monotonic_ns()}"
        results = [self._submit_one(intent) for intent in intents]
        return {"batch_id": batch_id, "results": results}

    def _submit_one(self, intent: dict) -> dict:
        entity_id = intent.get("entity_id") if isinstance(intent, dict) else None
        err = _validate_intent(intent)
        if err:
            return {
                "entity_id": entity_id if isinstance(entity_id, str) else None,
                "accepted": False,
                "error": err,
            }

        service = intent["service"]
        data = intent.get("data")
        domain = _domain_of(entity_id)
        allowed_keys = _ALLOWED_SERVICES[domain][service]
        payload = {k: v for k, v in (data or {}).items() if k in allowed_keys}
        payload["entity_id"] = entity_id

        issued_at = time.monotonic()
        with self._lock:
            ent = self._state.setdefault(entity_id, EntityState(entity_id, domain))
            ent.write_status = WRITE_PENDING
            ent.write_reason = None
            ent.write_issued_at = issued_at
        self._broadcast(entity_id)

        try:
            _http_request(
                "POST", f"{self._ha_url}/api/services/{domain}/{service}", self._token, payload
            )
        except _HAError as e:
            self._mark_unreachable(entity_id, domain, e.kind)
            return {"entity_id": entity_id, "accepted": False, "error": e.kind}

        # Best-effort immediate re-fetch so the UI doesn't sit in "pending"
        # for a full poll cycle -- failure here is swallowed, the write
        # already succeeded and the reconcile poll / next push will confirm.
        try:
            fetch_started = time.monotonic()
            state = _http_request(
                "GET", f"{self._ha_url}/api/states/{entity_id}", self._token, None
            )
            if state is not None:
                self._apply_state(entity_id, state, started_at=fetch_started)
        except _HAError:
            pass

        return {"entity_id": entity_id, "accepted": True, "error": None}


def _validate_intent(intent: Any) -> Optional[str]:
    if not isinstance(intent, dict):
        return "malformed intent"
    entity_id = intent.get("entity_id")
    if not _validate_entity_id(entity_id):
        return "invalid entity_id"
    domain = _domain_of(entity_id)
    service = intent.get("service")
    valid_service = (
        isinstance(service, str)
        and domain in _ALLOWED_SERVICES
        and service in _ALLOWED_SERVICES[domain]
    )
    if not valid_service:
        return "unknown service"
    data = intent.get("data")
    if data is not None and not isinstance(data, dict):
        return "invalid data"
    return None


# ---------------------------------------------------------------------------
# Module singleton + HTTP glue. serve.py calls start() once at startup and
# dispatches the three routes to the handle_* functions below, but only when
# enabled() is true -- when it's false these paths are simply never routed
# here and fall through to the normal static-file 404.
# ---------------------------------------------------------------------------

_deck: Optional[Deck] = None


def start() -> bool:
    global _deck
    if not enabled():
        return False
    if _deck is None:
        _deck = Deck(_get_ha_url(), _get_token())
        _deck.start()
    return True


def _respond_json(handler, status: int, obj: dict) -> None:
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def handle_states(handler) -> None:
    if _deck is None:
        _respond_json(handler, 404, {"error": "not found"})
        return
    _respond_json(handler, 200, {"entities": _deck.snapshot()})


def handle_stream(handler) -> None:
    if _deck is None:
        _respond_json(handler, 404, {"error": "not found"})
        return
    q = _deck.subscribe()
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for pub in _deck.snapshot():
            _write_sse(handler, pub)
        while True:
            try:
                pub = q.get(timeout=15.0)
            except queue.Empty:
                handler.wfile.write(b": keep-alive\n\n")
                handler.wfile.flush()
                continue
            _write_sse(handler, pub)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        _deck.unsubscribe(q)


def _write_sse(handler, pub: dict) -> None:
    handler.wfile.write(b"event: state\ndata: " + json.dumps(pub).encode("utf-8") + b"\n\n")
    handler.wfile.flush()


def handle_intent(handler) -> None:
    if _deck is None:
        _respond_json(handler, 404, {"error": "not found"})
        return
    length_hdr = handler.headers.get("Content-Length")
    try:
        length = int(length_hdr)
    except (TypeError, ValueError):
        handler.close_connection = True
        _respond_json(handler, 400, {"error": "missing content-length"})
        return
    if length < 0 or length > _MAX_BODY_BYTES:
        # Refuse to read an oversized body at all -- close rather than risk
        # desyncing the connection on unread bytes.
        handler.close_connection = True
        _respond_json(handler, 413, {"error": "body too large"})
        return
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw)
    except ValueError:
        _respond_json(handler, 400, {"error": "invalid json"})
        return
    intents = payload.get("intents") if isinstance(payload, dict) else None
    if not isinstance(intents, list) or not intents or len(intents) > _MAX_BATCH_SIZE:
        _respond_json(handler, 400, {"error": "invalid batch"})
        return
    result = _deck.submit_batch(intents)
    _respond_json(handler, 200, result)
