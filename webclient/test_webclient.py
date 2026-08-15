"""Browser tests for index.html — the client behaviours no unit test can reach.

The rest of this repo's suite is Python. index.html is ~4500 lines of inline
JavaScript with no harness at all, which is exactly how a fix once shipped
looking verified while being unreachable dead code: `release()` returned early
on `openMic`, so the wake-word re-arm inside `stopCapture()` never ran (fixed
in 02eb2a0, pinned here by `test_wakeword_arms_during_ptt_hold`).

These drive the real page in a real Chromium with a fake microphone, against a
stub control websocket that speaks the handful of frames each path needs. No
part of the voice pipeline is involved and nothing on the network is touched —
both servers bind ephemeral ports on 127.0.0.1, so this is safe to run on a box
where the live agent already owns :8765.

Run from repo root: python3 -m pytest webclient/test_webclient.py -v

Needs playwright and websockets, plus a browser:
    pip install playwright websockets && python3 -m playwright install chromium

All of it is skipped when any of that is missing, so the suite still passes on
a machine that only ever runs the pipeline.
"""

import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

websockets = pytest.importorskip("websockets", reason="websockets not installed")
sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

import asyncio  # noqa: E402  — only reachable once the two imports above succeed

HERE = os.path.dirname(os.path.abspath(__file__))

# Minimal config_state: renderConfigState() indexes ev.brains unguarded, so the
# shape matters more than the content.
CONFIG_STATE = {
    "type": "config_state",
    "active_brain": "local",
    "model": "stub-model",
    "brains": [{"name": "local", "label": "Local", "available": True,
                "reachable": True, "model": "stub-model", "models": []}],
    # `wake_word` is filled in per-request by the stub, since it has to agree
    # with whatever wakeword_state the test has broadcast — a real server
    # reports the gate's live state here, and a snapshot that contradicts it
    # would disarm the client's microphone.
    "voices": [],
    "custom_voices": [],
    "tools_armed": 0,
}

# Read straight out of the page's own script scope. These are top-level `let`s in
# a classic inline script, so they resolve as bare identifiers here but are NOT
# on `window` — the typeof guards keep a renamed variable an honest failure
# rather than a ReferenceError that reads like a browser problem.
CLIENT_STATE = """() => ({
    openMic: typeof openMic !== 'undefined' ? openMic : null,
    openMicStreaming: typeof openMicStreaming !== 'undefined' ? openMicStreaming : null,
    capturing: typeof capturing !== 'undefined' ? capturing : null,
    isHeld: typeof isHeld !== 'undefined' ? isHeld : null,
    micGraph: typeof micNode !== 'undefined' ? micNode !== null : null,
    pttCaption: document.getElementById('pttCaption').textContent,
    pttDisabled: document.getElementById('ptt').disabled,
})"""

GATE_STATE = """() => ({
    fill: document.getElementById('gateApproveFill').style.getPropertyValue('--hold-progress'),
    holding: document.getElementById('gateApprove').classList.contains('holding'),
    disabled: document.getElementById('gateApprove').getAttribute('aria-disabled'),
})"""


class StubControlServer:
    """The parts of the control protocol these paths exercise, and nothing else.

    Runs its own event loop on a background thread so the tests can stay
    synchronous (Playwright's sync API); `send` hops threads to reach it.
    """

    def __init__(self):
        self.port = None
        self.text_msgs = []
        self.binary_frames = 0
        self.reject_permissions = False  # answer permission_respond with config_ack{ok:false}
        self.ack_payload = None  # body of the config_ack{ok:true} a config_set gets back
        self.wake_enabled = False  # mirrors the last wakeword_state this stub sent
        self.answer_config_get = True  # off = leave a resync in flight, unanswered
        self._client = None
        self._loop = None
        self._stop = None
        self._ready = threading.Event()
        self._thread = None

    # `path` is a positional arg on websockets <14 and gone on >=14 — accept both.
    async def _handler(self, ws, path=None):
        self._client = ws
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    self.binary_frames += 1
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                self.text_msgs.append(data)
                if data.get("type") == "config_get":
                    if self.answer_config_get:
                        await ws.send(json.dumps(self._state_frame()))
                elif data.get("type") == "config_set" and "permission_respond" in data:
                    if self.reject_permissions:
                        pr = data["permission_respond"]
                        await ws.send(json.dumps({
                            "type": "config_ack", "ok": False,
                            "permission_respond": {
                                "id": pr.get("id"), "approve": pr.get("approve"),
                                "message": "another screen already answered",
                            },
                        }))
                elif data.get("type") == "config_set" and self.ack_payload is not None:
                    await ws.send(json.dumps(
                        {"type": "config_ack", "ok": True, **self.ack_payload}))
        except Exception:  # the page went away mid-test; nothing to clean up
            pass
        finally:
            self._client = None

    async def _serve(self):
        self._stop = asyncio.Event()
        async with websockets.serve(self._handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    def start(self):
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._serve())

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "stub control server never came up"

    def stop(self):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    def _state_frame(self):
        """A config_state that agrees with the wake-word state this stub has
        already broadcast — the same consistency a real server has for free."""
        return dict(CONFIG_STATE, wake_word={
            "enabled": self.wake_enabled,
            "state": "asleep" if self.wake_enabled else "off",
            "phrase": "hey jarvis",
            "model": "hey_jarvis",
            "models": ["hey_jarvis"],
        })

    def send(self, payload):
        """Push a server->client frame, as the pipeline or a second screen would."""
        assert self._client is not None, "no browser client connected"
        if payload.get("type") == "wakeword_state":
            self.wake_enabled = payload.get("state") in ("asleep", "awake")
        asyncio.run_coroutine_threadsafe(
            self._client.send(json.dumps(payload)), self._loop).result(timeout=5)


@pytest.fixture(scope="module")
def static_server():
    """Serves this directory, so index.html loads over http (a secure context,
    which getUserMedia requires) rather than file://."""
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=HERE, **kw)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, args=[
                "--use-fake-device-for-media-stream",   # a mic that always exists
                "--use-fake-ui-for-media-stream",       # and never prompts
                "--autoplay-policy=no-user-gesture-required",
            ])
        except Exception as e:
            pytest.skip(f"no chromium (run `python3 -m playwright install chromium`): {e}")
        yield b
        b.close()


@pytest.fixture
def client(browser, static_server):
    """A freshly loaded page plus the stub server it's talking to.

    Function-scoped on purpose: these tests leave the client in different
    wake-word/permission states, and a new page is cheaper than unwinding them.
    """
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"],
                              viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Point the client at the stub instead of the real :8765, and keep the heavy
    # avatar module from loading — neither is under test here.
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)

    yield page, server

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def _hold(page, selector):
    """Press and hold the pointer over an element (mouse.down alone doesn't move)."""
    box = page.locator(selector).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()


def test_wakeword_arms_during_ptt_hold(client):
    """Wake word armed from a second screen while this one holds PTT.

    The README sells every browser as a window onto one shared session, so
    arming from the phone while the desk holds the button is ordinary use.
    Before 02eb2a0 the release was swallowed: `isHeld` stuck true, the mic
    streaming forever, and the open-mic graph never built.
    """
    page, server = client
    _hold(page, "#ptt")
    page.wait_for_function(
        "() => typeof capturing !== 'undefined' && capturing && micNode !== null",
        timeout=10000)

    server.send({"type": "wakeword_state", "state": "asleep", "phrase": "hey jarvis"})
    page.wait_for_timeout(400)
    mid = page.evaluate(CLIENT_STATE)
    assert mid["openMic"] is True, "the arm broadcast never reached this client"
    assert mid["isHeld"] is True, "the hold ended on its own"

    page.mouse.up()
    page.wait_for_function(
        "() => typeof openMicStreaming !== 'undefined' && openMicStreaming",
        timeout=10000)

    after = page.evaluate(CLIENT_STATE)
    assert after["isHeld"] is False, "PTT hold never ended — release() swallowed it"
    assert after["openMicStreaming"] is True
    assert after["micGraph"] is True, "armed with no mic graph — the dead-mic state"
    assert "Wake word armed" in after["pttCaption"]
    assert after["pttDisabled"] is True


def test_plain_wakeword_arm_and_disarm(client):
    """The ordinary arm/disarm path, no PTT involved — guards the fix above."""
    page, server = client

    server.send({"type": "wakeword_state", "state": "asleep", "phrase": "hey jarvis"})
    page.wait_for_function(
        "() => typeof openMicStreaming !== 'undefined' && openMicStreaming", timeout=10000)
    armed = page.evaluate(CLIENT_STATE)
    assert armed["micGraph"] is True and armed["pttDisabled"] is True
    assert armed["isHeld"] is False

    before = server.binary_frames
    page.wait_for_timeout(800)
    assert server.binary_frames > before, "armed but no audio reaching the server"

    server.send({"type": "wakeword_state", "state": "off"})
    page.wait_for_function(
        "() => typeof openMic !== 'undefined' && !openMic && !capturing", timeout=10000)
    off = page.evaluate(CLIENT_STATE)
    assert off["micGraph"] is False, "mic device never released on disarm"
    assert off["pttCaption"] == "Hold to talk"


def test_rejected_approval_clears_fill(client):
    """Hold approve the full 1.5s, then have the server reject it.

    The bar staying full reads as a successful approval — the UI stating the
    opposite of what happened, on a permission gate.
    """
    page, server = client
    server.reject_permissions = True
    server.send({
        "type": "cockpit_state", "hermes_ok": True,
        "delegation": {"status": "idle", "active": False, "steps": []},
        "permissions": [{"id": 7, "summary": "Run `rm -rf /tmp/scratch`"}],
    })
    page.wait_for_function(
        "() => !document.getElementById('gateBanner').hidden", timeout=10000)

    _hold(page, "#gateApprove")
    page.wait_for_timeout(1900)   # GATE_HOLD_MS is 1500
    page.mouse.up()

    responded = [m for m in server.text_msgs if "permission_respond" in m]
    assert responded, "a completed hold sent no permission_respond"
    assert responded[0]["permission_respond"]["approve"] is True

    page.wait_for_function(
        "() => document.getElementById('gateApprove').getAttribute('aria-disabled') === 'false'",
        timeout=10000)
    after = page.evaluate(GATE_STATE)
    assert after["fill"] in ("0", "0.000", ""), \
        f"rejected approval left the bar at {after['fill']!r} — looks approved"
    assert after["holding"] is False


def _config_get_count(server):
    return len([m for m in server.text_msgs if m.get("type") == "config_get"])


def test_successful_ack_repaints_the_panel_on_its_own(client):
    """Since slice 12 a successful config_ack is a complete snapshot, so the
    client renders straight from it instead of chasing it with a config_get."""
    page, server = client
    assert _config_get_count(server) == 1, "the connect-time config_get"

    server.ack_payload = {
        "chat_reset": False,
        "active_brain": "cloud",
        "model": "cloud-model",
        "persona": "", "default_persona": "", "persona_persisted": False,
        "persona_tiers": None, "voice": "", "voices": [], "custom_voices": [],
        "tools_armed": 3,
        "wake_word": {"enabled": False, "phrase": "hey jarvis", "state": "off",
                      "model": "hey_jarvis", "models": ["hey_jarvis"]},
        "brains": [
            {"name": "local", "label": "Local", "available": True, "models": []},
            {"name": "cloud", "label": "Cloud", "available": True, "models": []},
        ],
    }
    page.evaluate("() => ws.send(JSON.stringify({type: 'config_set', brain: 'cloud'}))")

    page.wait_for_function(
        "() => document.getElementById('brainStatus').textContent.includes('Cloud')",
        timeout=10000)
    assert page.evaluate("() => document.getElementById('sessionTools').textContent") == "3 armed"
    assert _config_get_count(server) == 1, \
        "the ack was a full snapshot — the follow-up config_get is a wasted round trip"


def test_partial_ack_from_an_older_server_keeps_the_panel_and_the_mic(client):
    """An ack without `brains`/`voices`/`wake_word` means "no news", not "empty".

    A cached page can meet a server that predates the unification while a
    deploy is in flight. Such an ack does trigger a compat `config_get`, but
    that answer is a round trip away — so this holds the resync open
    (`answer_config_get = False`) and checks the client is intact meanwhile.
    Without that, the resync heals the damage before anything can observe it
    and this test passes against a client with no tolerance at all (verified:
    it did).

    Blanking the brain list for a round trip is a flash. Reading an absent
    `wake_word` as "off" is worse: setOpenMicMode tears the mic graph down and
    stops the device, so a live wake-word session drops audio and re-prompts.
    """
    page, server = client
    server.answer_config_get = False
    server.send({"type": "wakeword_state", "state": "asleep", "phrase": "hey jarvis"})
    page.wait_for_function(
        "() => typeof openMicStreaming !== 'undefined' && openMicStreaming", timeout=10000)
    brains_before = page.evaluate("() => document.getElementById('brainList').children.length")
    assert brains_before > 0

    # Exactly what the pre-slice-12 server sent: no brains, no voices, no wake_word.
    server.ack_payload = {
        "chat_reset": False, "active_brain": "local", "model": "stub-model",
        "persona": "", "default_persona": "", "persona_persisted": False,
        "persona_tiers": None, "voice": "", "custom_voices": [], "tools_armed": 0,
    }
    page.evaluate("() => ws.send(JSON.stringify({type: 'config_set', reset_chat: true}))")
    for _ in range(50):  # the compat resync is server-side state, so poll from here
        if _config_get_count(server) >= 2:
            break
        page.wait_for_timeout(100)

    after = page.evaluate(CLIENT_STATE)
    assert after["openMic"] is True, "a silent frame disarmed wake word"
    assert after["micGraph"] is True, "a silent frame tore down the live mic graph"
    assert page.evaluate("() => document.getElementById('brainList').children.length") == brains_before, \
        "a silent frame blanked the brain list"
    assert _config_get_count(server) == 2, \
        "an old-server ack can't carry the panel state — the client must still resync"


def test_settings_panel_focus_trap(client):
    """The panel claims aria-modal, so Tab must not escape it and Escape must close."""
    page, _ = client
    page.evaluate("() => document.getElementById('gearBtn').focus()")
    page.click("#gearBtn")
    page.wait_for_function(
        "() => document.getElementById('settingsPanel').classList.contains('open')",
        timeout=10000)

    opened = page.evaluate("""() => ({
        role: document.getElementById('settingsPanel').getAttribute('role'),
        modal: document.getElementById('settingsPanel').getAttribute('aria-modal'),
        hidden: document.getElementById('settingsPanel').getAttribute('aria-hidden'),
        focusInPanel: document.getElementById('settingsPanel').contains(document.activeElement),
        count: settingsFocusables().length,
    })""")
    assert opened["role"] == "dialog" and opened["modal"] == "true"
    assert opened["hidden"] == "false"
    assert opened["focusInPanel"], "opening moved focus nowhere"
    assert opened["count"] > 1

    # Forward wrap: Tab off the last control lands on the first.
    page.evaluate("() => settingsFocusables().slice(-1)[0].focus()")
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement === settingsFocusables()[0]")

    # Backward wrap: Shift+Tab off the first lands on the last.
    page.evaluate("() => settingsFocusables()[0].focus()")
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("() => document.activeElement === settingsFocusables().slice(-1)[0]")

    for _ in range(10):
        page.keyboard.press("Tab")
        assert page.evaluate(
            "() => document.getElementById('settingsPanel').contains(document.activeElement)"), \
            "focus escaped the dialog"

    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => !document.getElementById('settingsPanel').classList.contains('open')",
        timeout=10000)
    closed = page.evaluate("""() => ({
        hidden: document.getElementById('settingsPanel').getAttribute('aria-hidden'),
        onGear: document.activeElement === document.getElementById('gearBtn'),
        stuckInside: document.getElementById('settingsPanel').contains(document.activeElement),
    })""")
    assert closed["onGear"], "focus did not return to the control that opened the panel"
    assert not closed["stuckInside"], "focus left inside an aria-hidden subtree"
    assert closed["hidden"] == "true"


# ── ?ws= scheme validation ────────────────────────────────────────────
#
# Only ws:/wss: may reach the WebSocket constructor. A malformed value (or a
# disallowed scheme) in the ?ws= param or a stale localStorage entry must be
# ignored -- and the bad stored value cleared -- rather than throw and break
# reconnect.


def test_malformed_ws_param_is_ignored_and_page_still_loads(browser, static_server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('va-avatar', 'off'); localStorage.removeItem('va-ws-url');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html?ws=http%3A%2F%2Fevil.example")
    page.wait_for_timeout(300)

    ws_url = page.evaluate("() => typeof WS_URL !== 'undefined' ? WS_URL : null")
    stored = page.evaluate("() => localStorage.getItem('va-ws-url')")

    assert ws_url != "http://evil.example"
    assert stored != "http://evil.example"
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_valid_ws_param_override_is_accepted_and_persisted(browser, static_server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('va-avatar', 'off'); localStorage.removeItem('va-ws-url');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html?ws=ws%3A%2F%2F127.0.0.1%3A9999")
    page.wait_for_timeout(300)

    assert page.evaluate("() => WS_URL") == "ws://127.0.0.1:9999"
    assert page.evaluate("() => localStorage.getItem('va-ws-url')") == "ws://127.0.0.1:9999"
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_stale_malformed_stored_ws_url_is_cleared_on_load(browser, static_server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        "localStorage.setItem('va-avatar', 'off'); localStorage.setItem('va-ws-url', 'not a url at all');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_timeout(300)

    assert page.evaluate("() => localStorage.getItem('va-ws-url')") is None
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


# ── connect timeout ──────────────────────────────────────────────────
#
# A refused port fires onclose fast on its own, so the existing failure UX
# already runs. A firewalled/black-holed host never fires onclose at all --
# the socket just sits in CONNECTING forever, with the status stuck on
# "connecting..." and nothing in the log. 192.0.2.1 (RFC 5737 TEST-NET-1) is
# guaranteed unroutable, so a SYN to it black-holes rather than getting
# refused -- the one case a refused-port test can't reach.

def test_hung_connect_times_out_and_reports_failure(browser, static_server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        "window.__vaConnectTimeoutMs = 500;"
        "localStorage.setItem('va-avatar', 'off');"
        "localStorage.setItem('va-ws-url', 'ws://192.0.2.1:9999');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")

    # Still hung well before the deadline -- proves the failure isn't just
    # the ordinary "nobody's listening" refusal path.
    page.wait_for_timeout(200)
    assert page.evaluate("() => document.getElementById('statusText').textContent") == "connecting…"

    page.wait_for_function(
        "() => document.getElementById('statusText').textContent.startsWith(\"can't reach\")",
        timeout=5000)

    assert page.evaluate("() => document.getElementById('dot').className") == "error"
    assert page.evaluate("() => document.getElementById('log').textContent") != ""
    assert page.evaluate("() => document.getElementById('wsUrlAdvanced').open") is True
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def _frontier_brains(model_override=None):
    """A `local` brain plus a `frontier` brain with a 120-id catalog — well
    past the 15-model threshold where the searchable combobox replaces the
    plain <select>, standing in for the ~102-model NVIDIA NIM catalog."""
    models = [f"vendor/model-{i}" for i in range(120)]
    return [
        {"name": "local", "label": "Local", "available": True, "reachable": True,
         "model": "stub-model", "models": []},
        {"name": "frontier", "label": "Frontier", "available": True, "reachable": True,
         "model": "deepseek-ai/deepseek-v3.2", "model_override": model_override,
         "models": models},
    ]


def _open_settings(page):
    page.click("#gearBtn")
    page.wait_for_function(
        "() => document.getElementById('settingsPanel').classList.contains('open')",
        timeout=10000)


def test_model_combo_renders_for_large_catalog_and_filters(client):
    """A 100+-model brain gets the searchable combobox, not a bare <select>,
    and typing narrows the visible options — the core fix for the NIM
    catalog being unusable as a plain dropdown."""
    page, server = client
    _open_settings(page)
    server.send({"type": "config_state", "active_brain": "frontier",
                 "model": "deepseek-ai/deepseek-v3.2", "brains": _frontier_brains()})
    page.wait_for_selector(".modelComboInput", timeout=10000)

    assert page.locator(".brainModelSelect").count() == 0, \
        "a 120-model brain still rendered the plain <select>"
    combo = page.locator(".modelComboInput")
    assert "120 models" in (combo.get_attribute("placeholder") or "")

    combo.click()
    page.wait_for_selector(".modelComboOption", timeout=10000)
    assert page.locator(".modelComboOption").count() == 121  # 120 + the Default row

    combo.fill("model-11")
    page.wait_for_function(
        "() => document.querySelectorAll('.modelComboOption').length === 11",
        timeout=10000)
    texts = page.locator(".modelComboOption").all_inner_texts()
    assert all("model-11" in t for t in texts)


def test_model_combo_selection_sends_brain_model_frame(client):
    """Picking a row sends the same `brain_model` config_set frame the plain
    <select> always has — the combobox is a UI swap, not a protocol change."""
    page, server = client
    _open_settings(page)
    server.send({"type": "config_state", "active_brain": "frontier",
                 "model": "deepseek-ai/deepseek-v3.2", "brains": _frontier_brains()})
    page.wait_for_selector(".modelComboInput", timeout=10000)

    before = len(server.text_msgs)
    combo = page.locator(".modelComboInput")
    combo.click()
    combo.fill("vendor/model-42")
    page.wait_for_function(
        "() => document.querySelectorAll('.modelComboOption').length === 1",
        timeout=10000)
    page.click(".modelComboOption")

    sent = [m for m in server.text_msgs[before:] if m.get("type") == "config_set"
            and "brain_model" in m]
    assert sent, "no config_set brain_model frame followed the pick"
    assert sent[-1]["brain_model"] == {"brain": "frontier", "model": "vendor/model-42"}
    assert combo.input_value() == "vendor/model-42"


def test_brain_row_shows_effective_model(client):
    """Each brain's row states what model is actually in force — an override
    (prefixed) if set, else the plain `model` — without opening any picker."""
    page, server = client
    _open_settings(page)
    server.send({"type": "config_state", "active_brain": "local",
                 "model": "stub-model", "brains": _frontier_brains(model_override="vendor/model-7")})
    page.wait_for_function(
        "() => document.querySelectorAll('.brainMeta').length >= 2", timeout=10000)

    metas = page.evaluate("""() => Array.from(document.querySelectorAll('#brainList .brainOption'))
        .map(row => ({
            name: row.querySelector('.brainName').textContent,
            meta: row.querySelector('.brainMeta') ? row.querySelector('.brainMeta').textContent : null,
        }))""")
    by_name = {m["name"]: m["meta"] for m in metas}
    assert by_name["Local"] and "stub-model" in by_name["Local"]
    assert by_name["Frontier"] and "▸ vendor/model-7" in by_name["Frontier"]


def test_details_state_persists_across_reload(client, static_server):
    """A group's open/closed state survives a reload — the panel reopens the
    way the user left it instead of collapsing everything again."""
    page, server = client
    _open_settings(page)
    assert page.evaluate("() => document.getElementById('appearanceGroup').open") is False

    page.click("#appearanceGroup summary")
    # <details>'s `toggle` event is spec'd to fire as a queued task, not
    # synchronously with the attribute flip, so give it a tick before
    # checking the listener's side effect.
    page.wait_for_function(
        "() => localStorage.getItem('va-details-appearanceGroup') === '1'", timeout=10000)

    page.reload()
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    _open_settings(page)
    assert page.evaluate("() => document.getElementById('appearanceGroup').open") is True, \
        "the group's open state did not survive the reload"


# ── Instrument faceplate (P1 port) ──────────────────────────────────────

def test_instrument_default_theme(client):
    """A fresh load with no localStorage theme lands on the instrument
    faceplate, not a stale built-in id the boot script no longer knows."""
    page, server = client
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "instrument"


def test_meter_needle_tracks_level_bus(client):
    """#needleWrap exists and its rotation actually changes with --level —
    the needle is pure CSS off the same --level custom property the
    waveform bars already use, so this is the only way to prove it's wired
    rather than a static div sitting in the meter face."""
    page, server = client
    assert page.locator("#needleWrap").count() == 1

    page.evaluate("() => document.documentElement.style.setProperty('--level', '0')")
    page.wait_for_timeout(200)  # let the needle's CSS transition (0.12s) settle
    at_zero = page.evaluate(
        "() => getComputedStyle(document.getElementById('needleWrap')).transform")

    page.evaluate("() => document.documentElement.style.setProperty('--level', '0.8')")
    page.wait_for_timeout(200)
    at_high = page.evaluate(
        "() => getComputedStyle(document.getElementById('needleWrap')).transform")

    assert at_high != at_zero, "needle rotation did not move with --level"


def test_arm_switch_mirrors_wake_toggle(client):
    """Clicking the ARM throw switch is the same action as the settings
    panel's wake-word checkbox — one control, two physical read-outs."""
    page, server = client
    server.send({"type": "wakeword_state", "state": "off"})
    page.wait_for_timeout(200)

    before = page.evaluate("() => document.getElementById('wakeToggle').checked")
    assert before is False

    page.evaluate("() => document.getElementById('armSwitch').click()")
    page.wait_for_function(
        "() => document.getElementById('wakeToggle').checked === true", timeout=10000)

    assert page.evaluate(
        "() => document.getElementById('armSwitch').querySelector('.toggle-track')"
        ".getAttribute('data-armed')") == "true"


# ── OUTPUT-zone faceplate selectors (P2 port) ────────────────────────────

def test_voice_display_mirrors_selection(client):
    """#voiceDisplay's centered active label mirrors the current voice, and
    changing #voiceSelect keeps it in sync — a read-only mirror, not a
    second source of truth."""
    page, server = client
    server.send(dict(CONFIG_STATE, voices=["alba", "jean", "fantine"], voice="jean"))
    page.wait_for_function(
        "() => document.querySelector('#voiceDisplay .rail-labels .active')?.textContent === 'jean'",
        timeout=10000)

    page.select_option("#voiceSelect", "alba")
    page.wait_for_function(
        "() => document.querySelector('#voiceDisplay .rail-labels .active')?.textContent === 'alba'",
        timeout=10000)


def test_persona_display_opens_service_panel(client):
    """Clicking the PERSONA faceplate display opens the settings panel with
    Persona expanded — a shortcut into the existing panel, not a second
    control surface."""
    page, server = client
    page.click("#personaDisplay")
    page.wait_for_function(
        "() => document.getElementById('settingsPanel').classList.contains('open')",
        timeout=10000)
    assert page.evaluate("() => document.getElementById('personaGroup').open") is True


# ── Service panel (P3 instrument port) ───────────────────────────────────

def test_service_panel_details_have_disclosure_lamps(client):
    """Every collapsible group's summary carries a .disc-lamp element — the
    lit/unlit dot that reads the group's open state, per the disclosure-row
    treatment ported from the settings mockup."""
    page, server = client
    _open_settings(page)
    group_ids = ["personaGroup", "appearanceGroup", "inputGroup", "wsUrlAdvanced", "voiceCloneAdvanced"]
    for gid in group_ids:
        count = page.locator(f"#{gid} > summary > .disc-lamp").count()
        assert count == 1, f"#{gid}'s summary has no .disc-lamp"


def test_wake_toggle_still_clickable_after_reskin(client):
    """The real #wakeToggle checkbox sits opacity-0 on top of the decorative
    .switch-track-h so it stays a live hit target under the reskin — clicks
    at the DECORATIVE track's coordinates (not the input's own selector)
    must still land on and flip the input, proving the overlay didn't steal
    the click for itself."""
    page, server = client
    server.send(dict(CONFIG_STATE, wake_word={
        "enabled": False, "state": "off", "phrase": "hey jarvis",
        "model": "hey_jarvis", "models": ["hey_jarvis"]}))
    page.wait_for_function(
        "() => !document.getElementById('wakeWordBlock').hidden", timeout=10000)
    _open_settings(page)
    page.click("#inputGroup summary")
    page.wait_for_function(
        "() => document.getElementById('inputGroup').open", timeout=10000)

    before = page.evaluate("() => document.getElementById('wakeToggle').checked")
    # A real mouse click at the track's own coordinates -- Playwright's
    # click() actionability check refuses this (it insists the resolved
    # locator itself receives the event, and the input intercepts), which is
    # exactly the overlay working; a raw coordinate click is what a real
    # cursor does and is what proves the hit-test lands on the input.
    box = page.locator("#wakeWordBlock .switch-track-h").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    after = page.evaluate("() => document.getElementById('wakeToggle').checked")
    assert after is True and after != before


# ── Field-unit reflow (P4 mobile port) ───────────────────────────────────

def test_mobile_reflow_puts_meter_and_reply_before_the_talk_button(browser, static_server):
    """At field-unit width the level meter (the state carrier) and the
    assistant's reply must both render ABOVE the ARM/TALK input row, not
    below it — the input row led on desktop-derived layouts before this
    slice's grid-template-areas reorder, which would bury the one control
    that must never sit below the fold on a phone."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('va-avatar', 'off');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_timeout(500)

    meter_y = page.locator("#needleWrap").bounding_box()["y"]
    reply_y = page.locator("#assistantText").bounding_box()["y"]
    input_y = page.locator(".zone-input").bounding_box()["y"]

    assert meter_y < input_y, "the level meter renders below the ARM/TALK row on a phone-width viewport"
    assert reply_y < input_y, "the assistant's reply renders below the ARM/TALK row on a phone-width viewport"
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_mobile_long_status_does_not_cause_horizontal_overflow(browser, static_server):
    """A long fault string ("can't reach ws://...") has no natural break
    point, and #status inherits `white-space: nowrap` from the base
    (non-instrument) rule -- at field-unit width that pushed the whole
    document wider than the viewport instead of wrapping to a second
    line."""
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        "window.__vaConnectTimeoutMs = 500;"
        "localStorage.setItem('va-avatar', 'off');"
        "localStorage.setItem('va-ws-url', 'ws://192.0.2.1:9999');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function(
        "() => document.getElementById('statusText').textContent.startsWith(\"can't reach\")",
        timeout=5000)

    assert page.evaluate("() => document.documentElement.scrollWidth") <= 390, \
        "the long status string overflows the viewport width"
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


# ── firstrun calibration overlay ────────────────────────────────────────

def test_firstrun_shows_on_fresh_profile(browser, static_server):
    """A browser that has never completed setup sees the calibration
    overlay -- the [hidden] default alone must not be what's hiding it."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        "localStorage.setItem('va-avatar', 'off');"
        "localStorage.setItem('va-ws-url', 'ws://192.0.2.1:9999');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_timeout(300)

    assert page.locator("#firstrunOverlay").is_visible()
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_firstrun_enter_dismisses_and_persists(browser, static_server):
    """Pressing ENTER hides the overlay and remembers that choice -- a
    reload of the same browser must not show it again."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # No va-firstrun-done key in add_init_script (unlike the test above) --
    # this runs on every navigation including the reload below, and the
    # whole point of that reload is to prove the flag SET during the test
    # persists rather than getting re-cleared out from under it.
    page.add_init_script(
        "localStorage.setItem('va-avatar', 'off');"
        "localStorage.setItem('va-ws-url', 'ws://192.0.2.1:9999');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_timeout(300)
    assert page.locator("#firstrunOverlay").is_visible()

    page.locator("#firstrunEnter").click()

    assert page.locator("#firstrunOverlay").is_hidden()
    assert page.evaluate("() => localStorage.getItem('va-firstrun-done')") == "1"

    page.reload()
    page.wait_for_timeout(300)
    assert page.locator("#firstrunOverlay").is_hidden()
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_firstrun_absent_for_returning_user(client):
    """The default fixture profile has already completed calibration --
    this pins the no-regression case alongside the two tests above that
    actually discriminate fixed vs. unfixed behaviour."""
    page, server = client
    assert page.locator("#firstrunOverlay").is_hidden()


def test_firstrun_service_button_opens_panel_above_overlay(browser, static_server):
    """The SERVICE PANEL button must actually be reachable once clicked --
    if the overlay's z-index sits above the settings panel's, the settings
    panel opens underneath it and every control in it is unclickable.
    Playwright's actionability check refuses to click a covered element,
    so the close-button click below succeeding at all IS the z-order
    assertion -- no explicit z-index read needed."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        "localStorage.setItem('va-avatar', 'off');"
        "localStorage.setItem('va-ws-url', 'ws://192.0.2.1:9999');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_timeout(300)
    assert page.locator("#firstrunOverlay").is_visible()

    page.locator("#firstrunServiceBtn1").click()
    page.click("#settingsCloseBtn", timeout=3000)

    assert page.locator("#firstrunOverlay").is_visible()
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


# ── Camera device picker ─────────────────────────────────────────────────

def test_cam_select_persists_choice(browser, static_server):
    """Picking a camera in #camSelect writes it to localStorage, and the
    next time the select is (re)built from scratch it restores that choice
    -- exactly what boot does on a reload. A real page.reload() can't pin
    this: Chromium's fake video device (--use-fake-device-for-media-stream)
    rotates its own deviceId on every navigation (verified directly against
    this harness -- three reloads, three different ids), a fingerprinting
    defense outside this client's control, not something any fix here could
    satisfy. Re-running populateCamSelect() in place is what a reload's boot
    call does and pins the actual restore-from-localStorage behaviour."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["camera"], viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)

    _open_settings(page)
    page.click("#inputGroup summary")
    # options[0] is the "System default" placeholder this client always adds.
    page.wait_for_function(
        "() => document.getElementById('camSelect').options.length > 1", timeout=10000)
    device_id = page.eval_on_selector(
        "#camSelect", "el => el.options[el.options.length - 1].value")
    assert device_id, "no fake videoinput device exposed by the browser"

    page.select_option("#camSelect", device_id)
    page.wait_for_function(
        f"() => localStorage.getItem('va-cam-device') === {device_id!r}", timeout=5000)

    page.evaluate("() => { document.getElementById('camSelect').innerHTML = ''; populateCamSelect(); }")
    page.wait_for_function(
        "() => document.getElementById('camSelect').options.length > 1", timeout=10000)
    restored = page.eval_on_selector("#camSelect", "el => el.value")
    assert restored == device_id, "the saved camera choice was not restored on rebuild"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_camera_uses_saved_device(browser, static_server):
    """A saved camera choice must actually reach getUserMedia as an exact
    deviceId constraint, and a failed start (device gone, denied, whatever)
    must leave #camBtn clickable again rather than stuck disabled."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    saved_id = "test-saved-camera-id"
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}"
        f"localStorage.setItem('va-cam-device', {saved_id!r});"
        "window.__camCalls = [];"
        "navigator.mediaDevices.getUserMedia = (c) => {"
        "  window.__camCalls.push(c);"
        "  return Promise.reject(new DOMException('rejected for test', 'NotAllowedError'));"
        "};")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)

    page.click("#camBtn")
    page.wait_for_function("() => window.__camCalls && window.__camCalls.length > 0", timeout=5000)

    calls = page.evaluate("() => window.__camCalls")
    assert calls[0]["video"]["deviceId"]["exact"] == saved_id, \
        f"getUserMedia was not called with the saved deviceId: {calls[0]}"

    page.wait_for_function(
        "() => document.getElementById('camBtn').getAttribute('aria-pressed') === 'false'"
        " && !document.getElementById('camBtn').disabled", timeout=5000)
    assert page.evaluate("() => document.getElementById('camBtn').textContent") == "📷 Camera off", \
        "camBtn did not re-enable/reset after the failed start"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


# ── serve.py: clickjacking headers + /models query-string & HEAD handling ──
#
# Plain http.client requests against the REAL serve.py Handler (not the
# SimpleHTTPRequestHandler stand-in `static_server` above uses for the
# browser fixture) — no browser needed for either check.

import http.client  # noqa: E402
import importlib.util  # noqa: E402

_serve_spec = importlib.util.spec_from_file_location("webclient_serve", os.path.join(HERE, "serve.py"))
_serve_mod = importlib.util.module_from_spec(_serve_spec)
_serve_spec.loader.exec_module(_serve_mod)


@pytest.fixture(scope="module")
def serve_py_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _serve_mod.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def test_response_headers_deny_framing(serve_py_server):
    """The cockpit is a full voice/persona/brain control surface -- an
    invisible framing iframe is a live clickjacking vector, not just
    defense-in-depth."""
    conn = http.client.HTTPConnection("127.0.0.1", serve_py_server)
    conn.request("GET", "/index.html")
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert resp.getheader("Content-Security-Policy") == "frame-ancestors 'none'"
    conn.close()


def test_models_endpoint_strips_query_string(serve_py_server):
    conn = http.client.HTTPConnection("127.0.0.1", serve_py_server)
    conn.request("GET", "/models?foo=bar")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status in (200, 503)  # not a 404 -- the query string didn't confuse routing
    assert "object" in body or "error" in body


def test_models_endpoint_supports_head(serve_py_server):
    conn = http.client.HTTPConnection("127.0.0.1", serve_py_server)
    conn.request("HEAD", "/models")
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    assert resp.status in (200, 503)
    assert body == b""
    assert resp.getheader("Content-Length") not in (None, "0")
