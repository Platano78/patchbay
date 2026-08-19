"""Pure-Python, headless tests for the HA deck data layer.

No Playwright, no browser -- these exercise webclient/ha_deck.py directly
plus a stub Home Assistant (REST over http.server, websocket API over
`websockets.serve`), imitating the StubControlServer pattern in
test_webclient.py rather than importing from it.

Run: python3 -m pytest webclient/test_ha_deck.py -q
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets

import ha_deck

TOKEN = "s3cr3t-test-token"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class FakeHandler:
    """Just enough of BaseHTTPRequestHandler's surface for ha_deck's
    handle_* functions to write a response into, without a real socket."""

    def __init__(self, headers=None, body: bytes = b""):
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.resp_headers: dict = {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.resp_headers[k] = v

    def end_headers(self):
        pass

    def body_text(self) -> str:
        return self.wfile.getvalue().decode("utf-8", "replace")


class StubHA:
    """A minimal Home Assistant: REST over http.server, websocket API over
    `websockets.serve`, on two separate ports (real HA shares one port, but
    ha_deck's websocket URL derivation is a one-line pure function tested
    separately, so tests can point the two halves at each other directly)."""

    def __init__(self, token: str = TOKEN):
        self.token = token
        self.states: dict = {}
        self.calls: list = []  # (domain, service, payload)
        self.service_status: dict = {}  # (domain, service) -> non-200 to force a failure
        self.rest_delay = 0.0
        self.rest_port = None
        self.ws_port = None
        self._httpd = None
        self._http_thread = None
        self._ws_clients: set = set()
        self._loop = None
        self._ws_thread = None
        self._ws_ready = threading.Event()
        self._stop_ws = None

    @property
    def ha_url(self) -> str:
        return f"http://127.0.0.1:{self.rest_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/api/websocket"

    def start(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._make_rest_handler())
        self.rest_port = self._httpd.server_address[1]
        self._http_thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._http_thread.start()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._serve_ws())

        self._ws_thread = threading.Thread(target=run, daemon=True)
        self._ws_thread.start()
        assert self._ws_ready.wait(10), "stub HA websocket never came up"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
        if self._loop and self._stop_ws:
            self._loop.call_soon_threadsafe(self._stop_ws.set)
        if self._ws_thread:
            self._ws_thread.join(timeout=5)

    # -- websocket half -------------------------------------------------

    async def _serve_ws(self):
        self._stop_ws = asyncio.Event()
        async with websockets.serve(self._ws_handler, "127.0.0.1", 0) as server:
            self.ws_port = server.sockets[0].getsockname()[1]
            self._ws_ready.set()
            await self._stop_ws.wait()

    async def _ws_handler(self, ws):
        self._ws_clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "auth_required"}))
            auth = json.loads(await ws.recv())
            if auth.get("access_token") != self.token:
                await ws.send(json.dumps({"type": "auth_invalid"}))
                return
            await ws.send(json.dumps({"type": "auth_ok"}))
            async for _raw in ws:
                pass  # subscribe_events etc: tests drive pushes via push_event()
        except Exception:
            pass
        finally:
            self._ws_clients.discard(ws)

    def push_event(self, entity_id, state, attributes=None):
        payload = {
            "type": "event",
            "event": {
                "event_type": "state_changed",
                "data": {
                    "entity_id": entity_id,
                    "new_state": {
                        "entity_id": entity_id,
                        "state": state,
                        "attributes": attributes or {},
                        "last_changed": _now_iso(),
                    },
                },
            },
        }
        for ws in list(self._ws_clients):
            asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps(payload)), self._loop
            ).result(timeout=5)

    def drop_connections(self):
        for ws in list(self._ws_clients):
            asyncio.run_coroutine_threadsafe(ws.close(), self._loop).result(timeout=5)

    # -- REST half --------------------------------------------------------

    def _make_rest_handler(self):
        stub = self

        class RestHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {stub.token}"

            def do_GET(self):
                if stub.rest_delay:
                    time.sleep(stub.rest_delay)
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.path == "/api/states":
                    self._json(200, list(stub.states.values()))
                    return
                if self.path.startswith("/api/states/"):
                    eid = self.path[len("/api/states/"):]
                    state = stub.states.get(eid)
                    if state is None:
                        self._json(404, {"error": "not found"})
                        return
                    self._json(200, state)
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                m = re.match(r"^/api/services/([a-z_]+)/([a-z_]+)$", self.path)
                if not m:
                    self._json(404, {"error": "not found"})
                    return
                domain, service = m.group(1), m.group(2)
                stub.calls.append((domain, service, payload))
                status = stub.service_status.get((domain, service), 200)
                if status != 200:
                    self._json(status, {"error": "boom"})
                    return
                eid = payload.get("entity_id")
                if eid and eid in stub.states:
                    if service == "turn_on":
                        stub.states[eid]["state"] = "on"
                    elif service == "turn_off":
                        stub.states[eid]["state"] = "off"
                    stub.states[eid]["last_changed"] = _now_iso()
                self._json(200, [])

        return RestHandler


@pytest.fixture()
def stub_ha():
    stub = StubHA()
    stub.start()
    stub.states["light.kitchen"] = {
        "entity_id": "light.kitchen", "state": "off",
        "attributes": {"friendly_name": "Kitchen"}, "last_changed": _now_iso(),
    }
    yield stub
    stub.stop()


@pytest.fixture()
def deck(stub_ha):
    d = ha_deck.Deck(stub_ha.ha_url, stub_ha.token)
    yield d
    d.stop()


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """Every test gets a clean module singleton and no ambient HA env vars,
    whether or not it uses the `deck`/`stub_ha` fixtures."""
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setattr(ha_deck, "_deck", None)
    yield
    monkeypatch.setattr(ha_deck, "_deck", None)


# ---------------------------------------------------------------------------
# Absent unless configured
# ---------------------------------------------------------------------------


def test_disabled_without_env():
    assert ha_deck.enabled() is False
    assert ha_deck.start() is False


def test_routes_404_when_unset():
    for fn in (ha_deck.handle_states, ha_deck.handle_stream):
        h = FakeHandler()
        fn(h)
        assert h.status == 404

    h = FakeHandler(headers={"Content-Length": "2"}, body=b"{}")
    ha_deck.handle_intent(h)
    assert h.status == 404


# ---------------------------------------------------------------------------
# Input validation -- reject rather than forward
# ---------------------------------------------------------------------------


def test_malformed_entity_id_rejected():
    err = ha_deck._validate_intent({"entity_id": "not-an-entity", "service": "turn_on"})
    assert err == "invalid entity_id"


def test_unknown_service_rejected():
    err = ha_deck._validate_intent({"entity_id": "light.kitchen", "service": "reboot"})
    assert err == "unknown service"


def test_unknown_domain_rejected():
    err = ha_deck._validate_intent({"entity_id": "camera.front", "service": "turn_on"})
    assert err == "unknown service"


def test_oversized_body_rejected():
    h = FakeHandler(headers={"Content-Length": str(ha_deck._MAX_BODY_BYTES + 1)})
    ha_deck._deck = object()  # any non-None sentinel -- handle_intent must not touch it
    ha_deck.handle_intent(h)
    assert h.status == 413
    assert h.close_connection is True
    # And it must never have read that (fake, absent) body at all.
    assert h.rfile.tell() == 0


# ---------------------------------------------------------------------------
# The safety property: HA down never reads as "off"
# ---------------------------------------------------------------------------


def test_unreachable_reports_unknown_not_off():
    d = ha_deck.Deck("http://127.0.0.1:1", TOKEN)  # port 1: connection refused
    # An entity already known stays "unknown" -- never fabricated as "off".
    d._state["light.kitchen"] = ha_deck.EntityState("light.kitchen", "light")
    asyncio.run(d._reconcile(started_at=time.monotonic()))
    assert d._state["light.kitchen"].state == "unknown"
    # And an unreachable poll must not invent entities either.
    assert list(d._state.keys()) == ["light.kitchen"]


def test_ha_slow_times_out_without_wedging(stub_ha, monkeypatch):
    stub_ha.rest_delay = 2.0
    monkeypatch.setattr(ha_deck, "_HTTP_TIMEOUT_S", 0.2)
    started = time.monotonic()
    with pytest.raises(ha_deck._HAError) as exc:
        ha_deck._http_request("GET", f"{stub_ha.ha_url}/api/states", stub_ha.token)
    elapsed = time.monotonic() - started
    assert exc.value.kind == "timeout"
    assert elapsed < 1.0, "the call waited for the full 2s delay -- timeout wasn't honored"


# ---------------------------------------------------------------------------
# Write-stamp guard (rule 9): a stale reconcile must not clobber pending
# ---------------------------------------------------------------------------


def test_stale_reconcile_does_not_clobber_pending(deck):
    issued_at = time.monotonic()
    deck._state["light.kitchen"] = ha_deck.EntityState(
        "light.kitchen", "light", state="off",
        write_status=ha_deck.WRITE_PENDING, write_issued_at=issued_at,
    )

    # A snapshot that started BEFORE the write was issued: must be dropped.
    deck._apply_state(
        "light.kitchen", {"state": "off", "attributes": {}}, started_at=issued_at - 1.0
    )
    ent = deck._state["light.kitchen"]
    assert ent.write_status == ha_deck.WRITE_PENDING
    assert ent.state == "off"  # untouched, not re-applied from the stale snapshot

    # A snapshot that started AFTER the write: legitimately clears pending.
    deck._apply_state(
        "light.kitchen", {"state": "on", "attributes": {}}, started_at=issued_at + 1.0
    )
    ent = deck._state["light.kitchen"]
    assert ent.write_status == ha_deck.WRITE_CONFIRMED
    assert ent.state == "on"


def test_ws_push_always_applies_even_mid_write(deck):
    """A live push isn't a snapshot that could predate the write -- it
    always applies, unlike the poll backstop above."""
    issued_at = time.monotonic()
    deck._state["light.kitchen"] = ha_deck.EntityState(
        "light.kitchen", "light", write_status=ha_deck.WRITE_PENDING, write_issued_at=issued_at
    )
    deck._apply_event({
        "entity_id": "light.kitchen",
        "new_state": {"state": "on", "attributes": {}},
    })
    ent = deck._state["light.kitchen"]
    assert ent.write_status == ha_deck.WRITE_CONFIRMED
    assert ent.state == "on"


# ---------------------------------------------------------------------------
# Batch writes: one pending unit, partial failure reported per entity
# ---------------------------------------------------------------------------


def test_batch_partial_failure(stub_ha, deck):
    stub_ha.states["switch.pump"] = {
        "entity_id": "switch.pump", "state": "off", "attributes": {}, "last_changed": _now_iso(),
    }
    stub_ha.service_status[("switch", "turn_on")] = 500  # this one fails at HA

    result = deck.submit_batch([
        {"entity_id": "light.kitchen", "service": "turn_on"},   # succeeds
        {"entity_id": "switch.pump", "service": "turn_on"},     # HA rejects it
        {"entity_id": "not-an-entity", "service": "turn_on"},   # rejected before it reaches HA
    ])

    by_entity = {r["entity_id"]: r for r in result["results"]}
    assert by_entity["light.kitchen"]["accepted"] is True
    assert by_entity["switch.pump"]["accepted"] is False
    assert by_entity["switch.pump"]["error"] == "http_error"
    assert by_entity["not-an-entity"]["accepted"] is False
    assert by_entity["not-an-entity"]["error"] == "invalid entity_id"

    # Each entity's write outcome landed independently -- one failure in the
    # batch didn't drag the others down with it.
    assert deck._state["light.kitchen"].write_status in (ha_deck.WRITE_PENDING, ha_deck.WRITE_CONFIRMED)
    assert deck._state["switch.pump"].write_status == ha_deck.WRITE_FAILED


# ---------------------------------------------------------------------------
# The token never leaves this process
# ---------------------------------------------------------------------------


def test_token_never_in_responses(stub_ha, deck):
    ha_deck._deck = deck
    deck._state["light.kitchen"] = ha_deck.EntityState("light.kitchen", "light")

    h = FakeHandler()
    ha_deck.handle_states(h)
    assert TOKEN not in h.body_text()

    stub_ha.service_status[("light", "turn_on")] = 500
    body = json.dumps({"intents": [{"entity_id": "light.kitchen", "service": "turn_on"}]}).encode()
    h2 = FakeHandler(headers={"Content-Length": str(len(body))}, body=body)
    ha_deck.handle_intent(h2)
    assert TOKEN not in h2.body_text()

    with pytest.raises(ha_deck._HAError):
        ha_deck._http_request("GET", f"{stub_ha.ha_url}/api/nonexistent", "bad-token")


def test_batch_size_capped():
    ha_deck._deck = object()
    intents = [{"entity_id": "light.x", "service": "turn_on"}] * (ha_deck._MAX_BATCH_SIZE + 1)
    body = json.dumps({"intents": intents}).encode()
    h = FakeHandler(headers={"Content-Length": str(len(body))}, body=body)
    ha_deck.handle_intent(h)
    assert h.status == 400


def test_ha_ws_url_transform():
    assert ha_deck._ha_ws_url("http://127.0.0.1:8123") == "ws://127.0.0.1:8123/api/websocket"
    assert ha_deck._ha_ws_url("https://ha.example.com") == "wss://ha.example.com/api/websocket"


# ---------------------------------------------------------------------------
# Socket drop -> reconnect, poll backstop fills the gap meanwhile
# ---------------------------------------------------------------------------


def test_reconnect_and_backstop_fill_gap(stub_ha, monkeypatch):
    monkeypatch.setattr(ha_deck, "_RECONNECT_BACKOFF_S", (0.05,) * 6)
    monkeypatch.setattr(ha_deck, "_ha_ws_url", lambda _url: stub_ha.ws_url)

    d = ha_deck.Deck(stub_ha.ha_url, stub_ha.token)
    d.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and "light.kitchen" not in d._state:
            time.sleep(0.05)
        assert "light.kitchen" in d._state, "initial connect-time reconcile never populated state"

        # Change state on HA *while the socket is down* -- only the poll
        # backstop (run again on the next reconnect, here) can surface it.
        stub_ha.drop_connections()
        stub_ha.states["light.kitchen"]["state"] = "on"
        stub_ha.states["light.kitchen"]["last_changed"] = _now_iso()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and d._state["light.kitchen"].state != "on":
            time.sleep(0.05)
        assert d._state["light.kitchen"].state == "on", "reconnect never reconciled the gap"
    finally:
        d.stop()


def test_ws_push_reaches_subscribers(stub_ha, monkeypatch):
    monkeypatch.setattr(ha_deck, "_ha_ws_url", lambda _url: stub_ha.ws_url)
    d = ha_deck.Deck(stub_ha.ha_url, stub_ha.token)
    d.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and "light.kitchen" not in d._state:
            time.sleep(0.05)
        q = d.subscribe()
        try:
            stub_ha.push_event("light.kitchen", "on")
            pub = q.get(timeout=5.0)
        finally:
            d.unsubscribe(q)
        assert pub["entity_id"] == "light.kitchen"
        assert pub["state"] == "on"
    finally:
        d.stop()


# ---------------------------------------------------------------------------
# state is an open set (HA's own value), distinct from the closed write enum
# ---------------------------------------------------------------------------


def test_unavailable_state_survives_distinct_from_off_and_unknown(stub_ha, deck):
    """A real, live case: a Hue lamp that has genuinely dropped off the mesh
    reports "unavailable", not "off". That string must reach the consumer
    verbatim -- neither coerced to "off" (the lie this feature exists to
    prevent) nor collapsed into our own "unknown" sentinel (which would hide
    that HA itself has a specific, more informative answer)."""
    entity_id = "light.living_room_hue_white_lamp_1"
    stub_ha.states[entity_id] = {
        "entity_id": entity_id, "state": "unavailable",
        "attributes": {"friendly_name": "Hue White Lamp 1"}, "last_changed": _now_iso(),
    }

    asyncio.run(deck._reconcile(started_at=time.monotonic()))

    ent = deck._state[entity_id]
    assert ent.state == "unavailable"
    assert ent.state != "off"
    assert ent.state != ha_deck.WRITE_UNKNOWN  # distinct field, distinct value space

    pub = ent.to_public()
    assert pub["state"] == "unavailable"
    # And the closed write enum is untouched by this -- no write was issued.
    assert pub["write"]["status"] == ha_deck.WRITE_UNKNOWN


# ---------------------------------------------------------------------------
# Degraded mode: `websockets` not installed -- poll-only, never a hard fail
# ---------------------------------------------------------------------------


def test_degraded_mode_without_websockets_still_serves_state(stub_ha, monkeypatch):
    monkeypatch.setattr(ha_deck, "websockets", None)
    monkeypatch.setattr(ha_deck, "_RECONCILE_INTERVAL_DEGRADED_S", 0.05)

    d = ha_deck.Deck(stub_ha.ha_url, stub_ha.token)
    d.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and "light.kitchen" not in d._state:
            time.sleep(0.02)
        assert "light.kitchen" in d._state, "poll-only mode never populated state"
        assert d._state["light.kitchen"].state == "off"

        # A change on HA must still surface -- via polling, not push.
        stub_ha.states["light.kitchen"]["state"] = "on"
        stub_ha.states["light.kitchen"]["last_changed"] = _now_iso()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and d._state["light.kitchen"].state != "on":
            time.sleep(0.02)
        assert d._state["light.kitchen"].state == "on", "poll-only mode never picked up the change"
    finally:
        d.stop()


def test_serve_boots_when_ha_deck_import_fails(monkeypatch):
    """serve.py must never fail to start over the HA feature -- not for a
    missing `websockets`, and not for anything else ha_deck.py might raise
    at import time."""
    import builtins
    import sys
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "ha_deck":
            raise ImportError("simulated: ha_deck unimportable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)
    sys.modules.pop("serve", None)
    try:
        import serve as poisoned_serve
        assert poisoned_serve.ha_deck is None

        srv = ThreadingHTTPServer(("127.0.0.1", 0), poisoned_serve.Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=3)
            assert r.status == 200
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/ha/states", timeout=3)
            assert exc.value.code == 404
        finally:
            srv.shutdown()
    finally:
        sys.modules.pop("serve", None)
