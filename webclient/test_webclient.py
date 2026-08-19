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

import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

websockets = pytest.importorskip("websockets", reason="websockets not installed")
sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
# Already in this venv (not a new project dependency) — used only to sample real
# rendered background pixels for the WCAG contrast gate below, since a token- or
# backgroundColor-based guess can't see gradient/image backgrounds and produced
# a false pass once already (see CONTRAST_PAIRS' comment).
Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")

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
def _webclient_temp_root(tmp_path_factory):
    """A disposable, copy-on-write webclient root — this is what
    `static_server` actually serves, so the real gitignored
    `themes/local/`/`avatar/local/` are NEVER opened, moved, written to, or
    deleted by anything in this suite (the previous mechanism did all four,
    and cost this developer real, unrecoverable local-only content twice).

    Every top-level entry in `HERE` is SYMLINKED into a fresh temp dir
    (near-zero cost — no real file copying) except `themes/` and `avatar/`,
    which get REAL directories whose own children are individually
    symlinked except `local/` — left as a genuine, empty, throwaway
    directory local_theme_dir/local_avatar_dir below read/write freely.
    Absent by default, exactly like a fresh clone; each test's own
    os.makedirs(...)/_write(...) populates it, or doesn't, per scenario."""
    root = tmp_path_factory.mktemp("webclient_root")
    for name in os.listdir(HERE):
        if name in ("themes", "avatar"):
            continue
        os.symlink(os.path.join(HERE, name), os.path.join(root, name))
    for sub, local_name in (("themes", "local"), ("avatar", "local")):
        sub_root = os.path.join(root, sub)
        os.makedirs(sub_root, exist_ok=True)
        real_sub_dir = os.path.join(HERE, sub)
        for name in os.listdir(real_sub_dir):
            if name == local_name:
                continue
            os.symlink(os.path.join(real_sub_dir, name), os.path.join(sub_root, name))
        # sub_root/local intentionally NOT created here — see docstring.
    return str(root)


@pytest.fixture(scope="module")
def static_server(_webclient_temp_root):
    """Serves the disposable temp root (never the real repo directory), so
    index.html loads over http (a secure context, which getUserMedia
    requires) rather than file://."""
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=_webclient_temp_root, **kw)

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


def test_hold_fill_uses_a_compositor_transform_not_a_width_animation(client):
    """The gate-hold fill is driven from a requestAnimationFrame loop every
    frame of every press-and-hold; if it's expressed as `width`, that's a
    layout-triggering property forcing layout+paint on the subtree every
    frame. It must be a `transform: scaleX(...)` instead — same visual,
    compositor-only. Asserts the computed transform reflects hold progress
    while the computed width stays fixed."""
    page, server = client
    server.send({
        "type": "cockpit_state", "hermes_ok": True,
        "delegation": {"status": "idle", "active": False, "steps": []},
        "permissions": [{"id": 9, "summary": "Run `rm -rf /tmp/scratch`"}],
    })
    page.wait_for_function("() => !document.getElementById('gateBanner').hidden", timeout=10000)

    fill_computed = "() => { const cs = getComputedStyle(document.getElementById('gateApproveFill'));" \
        " return { width: cs.width, transform: cs.transform }; }"
    before = page.evaluate(fill_computed)

    _hold(page, "#gateApprove")
    page.wait_for_timeout(700)  # partway through the 1.5s hold — sample WHILE holding

    after = page.evaluate(fill_computed)
    page.mouse.up()  # release now that the mid-hold sample is captured

    assert after["transform"] != before["transform"], \
        f"hold fill's computed transform did not change with progress: before={before}, after={after}"
    assert after["width"] == before["width"], \
        f"hold fill's computed width changed with progress — still a layout-triggering animation: " \
        f"before={before}, after={after}"


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


def test_level_bus_skips_redundant_writes(client):
    """pushWaveform() runs every audio worklet frame; smoothedLevel.toFixed(3)
    is identical frame after frame during silence or a plateau, and each
    unconditional write to --level (set on documentElement, so it INHERITS)
    still invalidates style for every descendant that reads it. Wraps
    CSSStyleDeclaration.setProperty to count writes actually reaching
    --level, then drives pushWaveform() directly with a repeated identical
    peak followed by a genuinely different one."""
    page, server = client
    page.evaluate("""() => {
        window.__levelWriteCount = 0;
        const orig = CSSStyleDeclaration.prototype.setProperty;
        CSSStyleDeclaration.prototype.setProperty = function(name, value, ...rest) {
            if (name === '--level') window.__levelWriteCount++;
            return orig.call(this, name, value, ...rest);
        };
    }""")

    # Drive smoothedLevel to convergence on one peak first so repeated calls
    # with the SAME peak produce an identical rounded string (the smoothing
    # in pushWaveform means a single new peak value doesn't instantly land
    # on its own rounded value).
    page.evaluate("() => { for (let i = 0; i < 40; i++) pushWaveform(0.3); }")
    after_convergence = page.evaluate("() => window.__levelWriteCount")

    page.evaluate("() => { for (let i = 0; i < 10; i++) pushWaveform(0.3); }")
    after_plateau = page.evaluate("() => window.__levelWriteCount")

    page.evaluate("() => pushWaveform(1.0)")
    after_change = page.evaluate("() => window.__levelWriteCount")

    assert after_plateau == after_convergence, \
        f"--level write count grew ({after_convergence} -> {after_plateau}) across 10 frames " \
        f"of an unchanged rounded value — the redundant-write skip isn't working"
    assert after_change > after_plateau, \
        f"--level write count did not grow ({after_plateau} -> {after_change}) when the " \
        f"rounded value genuinely changed"


def test_level_survives_reset_after_an_identical_frame(client):
    """The specific bug the write-skip cache invites: push a frame that
    rounds to '0.000', then call resetWaveform(). If the cache isn't also
    updated by the reset path, the next '0' write looks like a no-op change
    and --level gets stuck at whatever pushWaveform last wrote instead of
    the true reset value."""
    page, server = client
    page.evaluate("() => { smoothedLevel = 0; pushWaveform(0); resetWaveform(); }")
    root_level = page.evaluate(
        "() => document.documentElement.style.getPropertyValue('--level')")
    assert root_level == "0", f"--level on documentElement is {root_level!r} after reset, want '0'"

    # A later genuinely-zero frame must still be correctly skipped as a
    # no-op (not re-broken by whatever made the reset case work).
    page.evaluate("""() => {
        window.__levelWriteCount = 0;
        const orig = CSSStyleDeclaration.prototype.setProperty;
        CSSStyleDeclaration.prototype.setProperty = function(name, value, ...rest) {
            if (name === '--level') window.__levelWriteCount++;
            return orig.call(this, name, value, ...rest);
        };
    }""")
    page.evaluate("() => pushWaveform(0)")
    after_same = page.evaluate("() => window.__levelWriteCount")
    assert after_same == 0, \
        f"--level write count is {after_same} for a frame identical to the post-reset value"


def test_level_bus_is_set_on_both_waveform_and_root(client):
    """THEME-SPEC.md's '--level visual bus' section documents both writes as
    a deliberate contract: the element-scoped write on #waveform is kept for
    back-compat with any theme — including a private one under
    themes/local/, invisible to this tree — already reading --level off
    #waveform/.waveform via a descendant or combinator selector. Pins that
    both targets carry the same live, non-zero value after a real push, so
    a future "this looks redundant" cleanup fails a gate instead of
    silently breaking a downstream theme author's skin."""
    # One synchronous evaluate: push and read both targets with no round-trip
    # back to Python in between, so nothing else touching the page (e.g. a
    # live mic worklet frame, on any test that has one running) can land a
    # --level write between the push and the read. Also resets lastLevelStr
    # so FIX 2's dedupe can't itself skip this push as a no-op against
    # whatever the page last happened to compute --level as.
    page, server = client
    levels = page.evaluate("""() => {
        smoothedLevel = 0;
        lastLevelStr = null;
        pushWaveform(1.0);
        return {
            waveform: document.getElementById('waveform').style.getPropertyValue('--level'),
            root: document.documentElement.style.getPropertyValue('--level'),
        };
    }""")
    assert levels["waveform"] != "" and levels["waveform"] != "0", \
        f"#waveform's own --level is {levels['waveform']!r} after a live push — the element write is missing"
    assert levels["waveform"] == levels["root"], \
        f"#waveform and documentElement disagree on --level ({levels['waveform']!r} vs {levels['root']!r})"


# ── S4a: --level bus frame-budget harness ───────────────────────────────
# Instrument: CDP's Performance domain (Performance.enable +
# Performance.getMetrics), read as before/after deltas across a driven
# burst. Verified against this exact page before being trusted: a plain
# rAF loop calling pushWaveform() 200 times moves RecalcStyleCount and
# RecalcStyleDuration by exactly the expected amounts and leaves them at 0
# when nothing runs, so it responds to real work rather than always
# reporting "fine" (this project's standing worry about perf instruments --
# see the negative-control test below for the harder proof).
LEVEL_BUS_BURST_FRAMES = 240

# A rAF-driven burst that writes a genuinely different --level almost every
# frame (varying peak via sin()), so the lastLevelStr dedupe (S3.5) does NOT
# suppress the writes -- a burst that repeats one value would measure the
# dedupe, not the bus.
#   mode='push'          -- calls the REAL pushWaveform() every frame (the
#                            production code path; used for the per-theme
#                            table).
#   mode='root_only'/'element_only'/'both_synthetic' -- a harness-only
#                            write loop that mirrors pushWaveform's own
#                            smoothing/dedupe math but writes to only the
#                            named target(s). This isolates the write-site
#                            variable for the :root-vs-#waveform control
#                            (question 1) without touching pushWaveform
#                            itself -- index.html is frozen this slice.
LEVEL_BUS_BURST_JS = """
async ({ frames, mode }) => {
  const waveform = document.getElementById('waveform');
  const root = document.documentElement;
  let synth = 0, synthLast = null;
  smoothedLevel = 0;
  lastLevelStr = null;
  return new Promise((resolve) => {
    let i = 0;
    function frame() {
      const peak = Math.abs(Math.sin(i * 0.73)) * 0.9 + 0.05;
      if (mode === 'push') {
        pushWaveform(peak);
      } else {
        synth += (Math.max(0, Math.min(1, peak * 3.2)) - synth) * 0.35;
        const str = synth.toFixed(3);
        if (str !== synthLast) {
          if (mode !== 'element_only') root.style.setProperty('--level', str);
          if (mode !== 'root_only') waveform.style.setProperty('--level', str);
          synthLast = str;
        }
      }
      i += 1;
      if (i < frames) requestAnimationFrame(frame);
      else resolve(null);
    }
    requestAnimationFrame(frame);
  });
}
"""

# Counts DOM elements actually matched by a rule whose declaration block
# references --level, under whichever theme is currently active (theme
# stylesheets stay in the DOM once loaded but are scoped by a
# `[data-theme="id"]` ancestor selector, so an inactive theme's rules
# correctly match nothing here).
#
# NOTE the `rule.cssRules && rule.cssRules.length` guard: in
# nesting-capable Chromium, a plain CSSStyleRule always carries a (possibly
# EMPTY) .cssRules -- an empty CSSRuleList is still a truthy object, so a
# bare `if (rule.cssRules)` matches *every* style rule and, followed by
# `continue`, would skip that rule's own selectorText check entirely. This
# was caught live while building this harness: an early version of this
# scanner reported 0 consumers on every theme, silently wrong.
LEVEL_BUS_CONSUMER_COUNT_JS = """
() => {
  const seen = new Set();
  const walk = (ruleList) => {
    for (const rule of ruleList) {
      if (rule.selectorText && rule.cssText && rule.cssText.includes('--level')) {
        try {
          document.querySelectorAll(rule.selectorText).forEach((el) => seen.add(el));
        } catch (e) { /* unsupported selector under this engine */ }
      }
      if (rule.cssRules && rule.cssRules.length) walk(rule.cssRules);
    }
  };
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch (e) { continue; }
    if (rules) walk(rules);
  }
  return seen.size;
}
"""


def _cdp_perf_snapshot(cdp):
    metrics = cdp.send("Performance.getMetrics")
    return {m["name"]: m["value"] for m in metrics["metrics"]}


def _drive_level_bus_burst(page, cdp, frames=LEVEL_BUS_BURST_FRAMES, mode="push"):
    """Runs LEVEL_BUS_BURST_JS for `frames` real rAF frames and returns the
    CDP Performance-domain deltas (ms/counts) it produced."""
    before = _cdp_perf_snapshot(cdp)
    page.evaluate(LEVEL_BUS_BURST_JS, {"frames": frames, "mode": mode})
    after = _cdp_perf_snapshot(cdp)
    return {
        "recalc_ms": (after["RecalcStyleDuration"] - before["RecalcStyleDuration"]) * 1000,
        "recalc_count": after["RecalcStyleCount"] - before["RecalcStyleCount"],
        "layout_ms": (after["LayoutDuration"] - before["LayoutDuration"]) * 1000,
        "layout_count": after["LayoutCount"] - before["LayoutCount"],
        "frames": frames,
    }


# Budgets, derived from measurement (see the S4a report): the real
# pushWaveform()-driven burst on this page currently costs ~1.6-1.9ms/frame
# (instrument) .. ~2.7-2.9ms/frame (chainsawman) of style recalc and
# ~0.08-0.12ms/frame of layout, stable across repeated runs. Set at roughly
# 1.4-2x the worst observed theme with headroom short of the 16.6ms/frame
# (60fps) total budget, so it fails on a real regression (e.g. a theme or a
# future consumer adding materially more cost) without flaking on ordinary
# run-to-run noise.
#
# WARNING -- this baseline is KNOWN BAD, not a target: an independent
# control (writing a registered inherits:true property NOTHING reads)
# measured ~1.8ms/frame recalc and 240/240 layout events on instrument --
# essentially IDENTICAL to the real --level burst above. The current cost
# is almost entirely the cost of writing an inherited registered custom
# property on documentElement at all, not the cost of --level's own
# consumers (see test_level_bus_root_write_costs_more_than_
# element_scoped_write's element-only numbers: 0.089ms/frame instrument,
# 0.309ms/frame chainsawman, zero/near-zero layout events). The durable
# fix is scoped work already: the S4 two-bus migration moves the expensive
# consumers (chainsawman's filter/box-shadow/animation-duration ones,
# see the per-theme investigation below) off the inherited :root --level
# onto the non-inherited, #meterChrome-scoped --level-fast -- S7's rig
# conversion is exactly the vehicle for that move. Recalc should drop
# toward the element-scoped numbers above once it lands -- a ~6-20x
# improvement -- and these budgets MUST be re-derived then, and should
# DROP substantially; if they don't, something is wrong. Do not treat
# these numbers as a quality bar -- they only exist to catch a gross
# regression on top of a currently-known-bad mechanism.
#
# PER-THEME, not one global number (V4 investigation, 2026-08-17): a
# single constant covering the worst theme slackens the two that actually
# matter. Re-derived after V4 added an extra grid-nesting level to
# #contentGrid (now a grid item inside `body > .device`'s own outer grid,
# see index.html's "V4: chassis widens" comment, where it used to be a
# direct flex child of `.device`) -- negligible against instrument's cheap
# consumers, small against hacker's, amplified against chainsawman's
# expensive ones, because #contentGrid is exactly where hacker/chainsawman
# ALSO promote their own cards via `display: contents` (their own
# mechanism, not V4's). Confirmed by testing all three themes, not just
# the one that crossed budget: instrument showed ~zero delta under the
# identical unscoped V4 changes, which rules out `#lowerBay {display:
# contents}` and the new `grid-template-areas` as the driver (both apply
# to instrument too) and narrows it to the #contentGrid nesting x
# chainsawman's own consumer cost. 5 alternating trials each, same
# `test_webclient.py`, only `index.html` swapped between HEAD (9398e8d)
# and V4, back-to-back on a quiet box (load 1.9-4.6, no other pytest
# running) so load contention isn't the variable:
#
#   theme        HEAD mean   V4 mean   delta
#   instrument   3.32        3.34      ~0.02 (noise)
#   hacker       3.63        3.68      ~0.05
#   chainsawman  4.07        4.58      ~0.51 (max observed 4.69/10 trials)
#
# The decisive fact: HEAD's own chainsawman mean (4.07) was already over
# the old flat 4.0 budget -- 4 of 5 HEAD trials landed at/under 4.03, one
# outlier at 4.51. That gate was never reliably green, with or without
# this slice; V4's ~0.5ms addition is what tipped an already-marginal
# number over the line, not a working gate breaking outright. Each budget
# below is the theme's own quiet-box V4 mean plus headroom for real load
# variance (chainsawman's own loaded runs during this same investigation
# ranged 4.17-4.97, informing its headroom specifically).
#
# BACKLOG: the structurally correct fix is a SELF-CALIBRATING gate, not
# better-guessed constants -- this repo already has the pattern:
# test_heat_bus_updates_at_roughly_12hz_not_60hz asserts --level-fast's
# cadence as a RATIO (>= 0.85x) against a raw-rAF ceiling measured in the
# SAME run, not a fixed Hz, specifically so load/hardware variance cancels
# out instead of being guessed at. The analogue here: measure a control
# cost (e.g. the unused-registered-property burst already used above as a
# comparison, or a raw layout/recalc no-op) in the same test run and
# assert THIS burst's cost as a ratio against it. Out of scope for a
# composition slice -- left as a note so the next person re-deriving these
# numbers doesn't have to rediscover the pattern from scratch.
LEVEL_BUS_RECALC_BUDGET_MS_PER_FRAME = {
    "instrument": 4.5,
    "hacker": 5.0,
}
LEVEL_BUS_LAYOUT_BUDGET_MS_PER_FRAME = 1.0


def _report_level_bus_burst(theme, consumer_count, result):
    per_frame_recalc = result["recalc_ms"] / result["frames"]
    per_frame_layout = result["layout_ms"] / result["frames"]
    print(f"\n-- {theme}: --level bus frame budget --")
    print(f"  consumers reading --level: {consumer_count}")
    print(f"  recalc: {result['recalc_ms']:.3f}ms / {result['recalc_count']} events over "
          f"{result['frames']} frames  ({per_frame_recalc:.4f}ms/frame)")
    print(f"  layout: {result['layout_ms']:.3f}ms / {result['layout_count']} events over "
          f"{result['frames']} frames  ({per_frame_layout:.4f}ms/frame)")
    return per_frame_recalc, per_frame_layout


@pytest.mark.parametrize("theme", ["instrument", "hacker"])
def test_level_bus_frame_budget_by_theme(browser, static_server, theme):
    """Drives the real pushWaveform() on a rAF loop for
    LEVEL_BUS_BURST_FRAMES frames with a varying peak (so the lastLevelStr
    dedupe can't suppress the writes) and gates the resulting CDP
    style-recalc/layout cost per theme. Prints the numbers on SUCCESS too
    -- a gate that only speaks on failure teaches nothing on the runs where
    it passes.

    The per-theme total below is dominated by the :root write itself, NOT
    by how many rules each theme has reading --level -- an independent
    control (writing a registered inherits:true property nothing reads)
    measured ~1.8ms/frame recalc and 240/240 layout events on instrument,
    essentially matching the real --level burst. The theme's OWN consumer
    style is a real but much smaller second-order effect: isolating it
    (real burst minus that same unused-property control) is roughly
    +0.05ms/frame for instrument's own 2 base consumers, ~+0.34ms/frame
    for chainsawman's filter/box-shadow/animation-duration ones -- an
    early draft of this comment compared theme totals directly and
    overstated this by ~3x."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    if theme != "instrument":
        page.evaluate(f"() => setTheme('{theme}')")
        page.wait_for_function(
            f"() => document.documentElement.getAttribute('data-theme') === '{theme}'", timeout=10000)
        # Same stylesheet-parsed race guarded elsewhere in this file -- data-theme
        # flipping alone doesn't mean the injected <link> has fetched/parsed yet.
        page.wait_for_function(
            f"() => Array.from(document.styleSheets).some(s => (s.href || '').includes('themes/{theme}.css')"
            " && s.cssRules && s.cssRules.length > 0)", timeout=10000)
    page.wait_for_timeout(60)

    consumer_count = page.evaluate(LEVEL_BUS_CONSUMER_COUNT_JS)

    cdp = ctx.new_cdp_session(page)
    cdp.send("Performance.enable")
    _drive_level_bus_burst(page, cdp, frames=30, mode="push")  # warm-up, unmeasured
    result = _drive_level_bus_burst(page, cdp, mode="push")
    per_frame_recalc, per_frame_layout = _report_level_bus_burst(theme, consumer_count, result)

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    recalc_budget = LEVEL_BUS_RECALC_BUDGET_MS_PER_FRAME[theme]
    assert per_frame_recalc < recalc_budget, (
        f"[{theme}] --level bus recalc cost {per_frame_recalc:.4f}ms/frame exceeds the "
        f"{recalc_budget}ms/frame budget")
    assert per_frame_layout < LEVEL_BUS_LAYOUT_BUDGET_MS_PER_FRAME, (
        f"[{theme}] --level bus layout cost {per_frame_layout:.4f}ms/frame exceeds the "
        f"{LEVEL_BUS_LAYOUT_BUDGET_MS_PER_FRAME}ms/frame budget")


def test_level_bus_root_write_costs_more_than_element_scoped_write(browser, static_server):
    """Question 1's isolated-variable control: same frame count, same
    varying values, the ONLY difference is whether --level is also written
    to document.documentElement (inherited, per THEME-SPEC.md) or kept to
    #waveform alone (element-scoped). Prints both so the report can state a
    real delta rather than a round-number opinion; the only hard assertion
    is that the harness can tell the two apart at all (a sanity floor, not
    a strict perf gate -- the delta itself is diagnostic, not a ceiling
    this test enforces)."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_timeout(60)

    cdp = ctx.new_cdp_session(page)
    cdp.send("Performance.enable")

    _drive_level_bus_burst(page, cdp, frames=30, mode="both_synthetic")
    both = _drive_level_bus_burst(page, cdp, mode="both_synthetic")
    _drive_level_bus_burst(page, cdp, frames=30, mode="element_only")
    element_only = _drive_level_bus_burst(page, cdp, mode="element_only")

    both_recalc = both["recalc_ms"] / both["frames"]
    element_recalc = element_only["recalc_ms"] / element_only["frames"]
    print("\n-- instrument: :root-vs-#waveform write-site control --")
    print(f"  both (root+element): {both['recalc_ms']:.3f}ms / {both['recalc_count']} recalcs, "
          f"{both['layout_ms']:.3f}ms / {both['layout_count']} layouts over {both['frames']} frames "
          f"({both_recalc:.4f}ms/frame recalc)")
    print(f"  element-only:        {element_only['recalc_ms']:.3f}ms / {element_only['recalc_count']} recalcs, "
          f"{element_only['layout_ms']:.3f}ms / {element_only['layout_count']} layouts over "
          f"{element_only['frames']} frames ({element_recalc:.4f}ms/frame recalc)")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    assert both["recalc_count"] > 0 and element_only["recalc_count"] > 0, \
        "harness produced zero recalcs in one leg -- it isn't driving the page"


# ── S4b: --heat / --level-fast visual buses ──────────────────────────────
# See THEME-SPEC.md's "Visual buses" table and index.html's @property
# comments for the design. --level stays exactly as measured/gated above
# (grandfathered); these buses are new and additive.

# Budget for --level-fast: it is written element-scoped on #meterChrome
# alone (inherits:false), the same shape S4a's element-only control
# measured at ~0.089ms/frame (instrument) / ~0.309ms/frame (chainsawman),
# not --level's ~1.8-2.7ms/frame root-write cost. 7 samples of this
# slice's own isolated-cost harness (test_level_fast_bus_frame_budget,
# same shared-box conditions the suite normally runs under):
#   recalc ms/frame: 0.0000, 0.0090, 0.0433, 0.0721, 0.1106, 0.1535, 0.3160  (mean ~0.10, max 0.316)
#   layout ms/frame: 0.0000, 0.0008, 0.0015, 0.0032, 0.0033, 0.0070, 0.0144  (mean ~0.005, max 0.0144)
# matching S4a's element-scoped table. Budget set at ~1.6x the observed
# max (recalc) and ~3.5x the observed max (layout), well short of
# --level's own 4.0/1.0 budget, so a future regression that (re)couples
# this bus to a full-page write is caught instead of quietly inheriting
# --level's much looser ceiling.
LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME = 0.5
LEVEL_FAST_LAYOUT_BUDGET_MS_PER_FRAME = 0.05


# Below what ceiling a raw-rAF control is itself no longer trustworthy --
# a box this contended can't tell a working bus from a broken one, so the
# test should say "couldn't measure" (skip) rather than report a verdict
# it isn't entitled to. See test_heat_bus_updates_at_roughly_12hz_not_60hz.
LEVEL_FAST_CADENCE_CEILING_FLOOR_HZ = 30
# --level-fast must land within this fraction of the environment's own
# rAF ceiling (not a fixed Hz) -- see the same test for why a fixed
# threshold was tried first and rejected as flake-prone.
LEVEL_FAST_CADENCE_MIN_RATIO = 0.85


def test_heat_bus_updates_at_roughly_12hz_not_60hz(browser, static_server):
    """Negative control: --heat must actually be gated to ~12Hz, not
    silently riding pushWaveform's own (much faster, ~125-375Hz) call
    rate, and --level-fast must actually reach the environment's own
    rAF ceiling rather than some fixed Hz number.

    --level-fast's check is SELF-CALIBRATING against a raw-rAF control
    (no pushWaveform work at all) measured in the same window, rather
    than a fixed floor. Tried a fixed `>= 55` first: 9/10 sample runs
    landed 56-58Hz against a measured ~60.5Hz raw-rAF ceiling on this
    box, but one landed 54.8Hz -- a ~10% flake rate on a QUIET box that
    would only get worse under load or on different hardware, which is
    the exact "a perf gate that flakes gets disabled" failure this
    project keeps re-learning. The real question was never "does it hit
    55Hz", it's "does it hit essentially every frame the environment
    actually offers" -- pushWaveform's own per-frame DOM work (wbar
    styles, the --level write, periodic text updates) costs a few
    percent of the frame budget regardless of --level-fast, so comparing
    against a raw-rAF ceiling measured in the SAME run cancels that out.
    The old (reset-to-`now`) build measured ~38/60.5 = 0.63 of ceiling;
    the fixed build measures ~57/60.5 = 0.94 -- the ratio still catches
    the regression it was written for, with more margin than the fixed
    number had, and it stays correct on a slower or more contended box
    where a fixed 55 would fail for reasons unrelated to the bus.

    The threshold (0.85) sits roughly midway between the broken build's
    0.63 and the fixed build's ~1.0 -- that gap, not a round number, is
    where it comes from.

    Ratios slightly ABOVE 1.0 (observed up to ~1.02x) are expected noise,
    not a fault: the ceiling and the real burst run in two SEPARATE
    windows back-to-back, not simultaneously, so they see slightly
    different scheduling -- within a single window pushWaveform is called
    at most once per rAF, so writes can never actually outrun frames. The
    signal that matters is the gap between ~1.0 (writing essentially every
    frame the environment offers) and the pre-fix build's ~0.63, not
    whether either individual run reads a hair over or under 1.0."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_timeout(60)

    # Raw-rAF ceiling: no pushWaveform, no property writes -- just counts
    # how many rAF callbacks this environment delivers in the window.
    ceiling = page.evaluate("""
    async ({ ms }) => {
      let frames = 0;
      const start = performance.now();
      return new Promise((resolve) => {
        function frame() {
          frames++;
          const elapsed = performance.now() - start;
          if (elapsed < ms) requestAnimationFrame(frame);
          else resolve({ frames, elapsedMs: elapsed });
        }
        requestAnimationFrame(frame);
      });
    }
    """, {"ms": 2000})
    ceiling_hz = ceiling["frames"] / (ceiling["elapsedMs"] / 1000)

    if ceiling_hz < LEVEL_FAST_CADENCE_CEILING_FLOOR_HZ:
        ctx.close()
        server.stop()
        pytest.skip(
            f"raw-rAF ceiling only ~{ceiling_hz:.1f}Hz on this box -- too contended to "
            "measure --level-fast's cadence against; not a bus failure")

    result = page.evaluate("""
    async ({ ms }) => {
      const root = document.documentElement;
      const mc = document.getElementById('meterChrome');
      let heatWrites = 0, fastWrites = 0;
      const origRoot = root.style.setProperty.bind(root.style);
      root.style.setProperty = (name, value) => {
        if (name === '--heat') heatWrites++;
        return origRoot(name, value);
      };
      const origMc = mc.style.setProperty.bind(mc.style);
      mc.style.setProperty = (name, value) => {
        if (name === '--level-fast') fastWrites++;
        return origMc(name, value);
      };
      smoothedLevel = 0;
      lastLevelStr = null;
      const start = performance.now();
      return new Promise((resolve) => {
        function frame() {
          const peak = Math.abs(Math.sin(performance.now() * 0.01)) * 0.9 + 0.05;
          pushWaveform(peak);
          const elapsed = performance.now() - start;
          if (elapsed < ms) {
            requestAnimationFrame(frame);
          } else {
            root.style.setProperty = origRoot;
            mc.style.setProperty = origMc;
            resolve({ heatWrites, fastWrites, elapsedMs: elapsed });
          }
        }
        requestAnimationFrame(frame);
      });
    }
    """, {"ms": 2000})

    heat_hz = result["heatWrites"] / (result["elapsedMs"] / 1000)
    fast_hz = result["fastWrites"] / (result["elapsedMs"] / 1000)
    fast_ratio = fast_hz / ceiling_hz
    print(f"\n-- --heat / --level-fast cadence over {result['elapsedMs']:.0f}ms "
          f"(raw-rAF ceiling {ceiling_hz:.1f}Hz over {ceiling['elapsedMs']:.0f}ms) --")
    print(f"  --heat:       {result['heatWrites']} writes  (~{heat_hz:.1f}Hz)")
    print(f"  --level-fast: {result['fastWrites']} writes  (~{fast_hz:.1f}Hz, "
          f"{fast_ratio:.2f}x the {ceiling_hz:.1f}Hz ceiling)")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    # Generous bounds around the nominal 12Hz target -- rAF jitter in a
    # headless CI browser is real; the point being proven is that --heat
    # is materially slower than --level-fast, not exact frequency lock.
    assert 6 <= heat_hz <= 20, f"--heat updated at ~{heat_hz:.1f}Hz, expected ~12Hz"
    assert fast_ratio >= LEVEL_FAST_CADENCE_MIN_RATIO, (
        f"--level-fast updated at ~{fast_hz:.1f}Hz, only {fast_ratio:.2f}x this "
        f"environment's own ~{ceiling_hz:.1f}Hz rAF ceiling (want >= "
        f"{LEVEL_FAST_CADENCE_MIN_RATIO}x)")


def test_level_fast_bus_is_not_inherited_by_descendants(browser, static_server):
    """Negative control: --level-fast is registered inherits:false and
    written only on #meterChrome. A child of #meterChrome must NOT see the
    live value -- it should read back the registered initial-value (0),
    proving the confinement that buys the fast bus its cost advantage is
    real, not just declared."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_timeout(60)

    result = page.evaluate("""
    () => {
      smoothedLevel = 0;
      lastLevelStr = null;
      pushWaveform(0.9); // forces a genuinely non-zero, non-default push
      const mc = document.getElementById('meterChrome');
      const child = document.createElement('div');
      mc.appendChild(child);
      const mcVal = getComputedStyle(mc).getPropertyValue('--level-fast').trim();
      const childVal = getComputedStyle(child).getPropertyValue('--level-fast').trim();
      child.remove();
      return { mcVal, childVal };
    }
    """)
    print(f"\n-- --level-fast inheritance control -- mount: {result['mcVal']!r}  child: {result['childVal']!r}")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    assert result["mcVal"] not in ("", "0"), \
        f"#meterChrome itself never got a live --level-fast value: {result['mcVal']!r}"
    assert result["childVal"] == "0", (
        f"a child of #meterChrome saw --level-fast={result['childVal']!r} instead of the registered "
        "initial-value 0 -- the property is inheriting when it must not")


def test_reset_waveform_zeroes_heat_and_level_fast(browser, static_server):
    """resetWaveform() must zero --heat and --level-fast the same way it
    already zeroes --level, or a mic disconnect leaves the room/centrepiece
    lit at the last live value."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_timeout(60)

    before, after = page.evaluate("""
    () => {
      smoothedLevel = 0;
      lastLevelStr = null;
      pushWaveform(0.9);
      const root = document.documentElement;
      const mc = document.getElementById('meterChrome');
      const before = {
        heat: getComputedStyle(root).getPropertyValue('--heat').trim(),
        levelFast: getComputedStyle(mc).getPropertyValue('--level-fast').trim(),
      };
      resetWaveform();
      const after = {
        heat: getComputedStyle(root).getPropertyValue('--heat').trim(),
        levelFast: getComputedStyle(mc).getPropertyValue('--level-fast').trim(),
      };
      return [before, after];
    }
    """)
    print(f"\n-- resetWaveform zeroes new buses -- before: {before}  after: {after}")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    assert before["heat"] not in ("", "0"), f"--heat never went live before reset: {before['heat']!r}"
    assert before["levelFast"] not in ("", "0"), f"--level-fast never went live before reset: {before['levelFast']!r}"
    assert after["heat"] == "0", f"resetWaveform left --heat at {after['heat']!r}, expected 0"
    assert after["levelFast"] == "0", f"resetWaveform left --level-fast at {after['levelFast']!r}, expected 0"


def test_level_fast_bus_frame_budget(browser, static_server):
    """Extends the S4a frame-budget gate to --level-fast: drives the real
    pushWaveform() on a rAF loop with a varying peak and gates the
    resulting CDP style-recalc/layout cost against
    LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME -- the element-scoped budget,
    not --level's inherited-root one. Isolates --level-fast's own cost by
    diffing a run with the property registered+written against a run
    where pushWaveform is monkeypatched to skip only the --level-fast
    write (same burst, same --level/--heat writes on both legs) -- a raw
    total would otherwise fold in --level's own ~1.8ms/frame."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_timeout(60)

    cdp = ctx.new_cdp_session(page)
    cdp.send("Performance.enable")

    js = """
    async ({ frames, skipFast }) => {
      smoothedLevel = 0;
      lastLevelStr = null;
      heat = 0;
      nextHeatWriteMs = 0;
      nextLevelFastWriteMs = 0;
      const mc = document.getElementById('meterChrome');
      const origSet = mc.style.setProperty.bind(mc.style);
      if (skipFast) {
        mc.style.setProperty = (name, value) => {
          if (name === '--level-fast') return;
          return origSet(name, value);
        };
      }
      let i = 0;
      return new Promise((resolve) => {
        function frame() {
          const peak = Math.abs(Math.sin(i * 0.73)) * 0.9 + 0.05;
          pushWaveform(peak);
          i += 1;
          if (i < frames) requestAnimationFrame(frame);
          else { mc.style.setProperty = origSet; resolve(null); }
        }
        requestAnimationFrame(frame);
      });
    }
    """

    def drive(skip_fast):
        before = _cdp_perf_snapshot(cdp)
        page.evaluate(js, {"frames": LEVEL_BUS_BURST_FRAMES, "skipFast": skip_fast})
        after = _cdp_perf_snapshot(cdp)
        return {
            "recalc_ms": (after["RecalcStyleDuration"] - before["RecalcStyleDuration"]) * 1000,
            "layout_ms": (after["LayoutDuration"] - before["LayoutDuration"]) * 1000,
        }

    page.evaluate(js, {"frames": 30, "skipFast": False})  # warm-up, unmeasured
    with_fast = drive(skip_fast=False)
    without_fast = drive(skip_fast=True)

    per_frame_recalc = max(0.0, (with_fast["recalc_ms"] - without_fast["recalc_ms"]) / LEVEL_BUS_BURST_FRAMES)
    per_frame_layout = max(0.0, (with_fast["layout_ms"] - without_fast["layout_ms"]) / LEVEL_BUS_BURST_FRAMES)
    print("\n-- --level-fast frame budget (isolated from --level/--heat) --")
    print(f"  with --level-fast:    {with_fast['recalc_ms']:.3f}ms recalc, {with_fast['layout_ms']:.3f}ms layout "
          f"over {LEVEL_BUS_BURST_FRAMES} frames")
    print(f"  without --level-fast: {without_fast['recalc_ms']:.3f}ms recalc, {without_fast['layout_ms']:.3f}ms "
          f"layout over {LEVEL_BUS_BURST_FRAMES} frames")
    print(f"  isolated --level-fast cost: {per_frame_recalc:.4f}ms/frame recalc, "
          f"{per_frame_layout:.4f}ms/frame layout")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    assert per_frame_recalc < LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME, (
        f"--level-fast recalc cost {per_frame_recalc:.4f}ms/frame exceeds the "
        f"{LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME}ms/frame budget")
    assert per_frame_layout < LEVEL_FAST_LAYOUT_BUDGET_MS_PER_FRAME, (
        f"--level-fast layout cost {per_frame_layout:.4f}ms/frame exceeds the "
        f"{LEVEL_FAST_LAYOUT_BUDGET_MS_PER_FRAME}ms/frame budget")


def test_level_fast_budget_gate_fails_on_a_deliberately_expensive_mount(browser, static_server):
    """Negative control: the extended budget must be provably able to
    fail, not just always pass. Drives a synthetic burst that writes
    --level-fast on #meterChrome every frame AND -- standing in for a rig
    author who reads the bus then violates the transform/opacity-only rule
    by resizing document.body off it every frame, forcing a full-document
    layout instead of a confined, compositor-only reaction -- must exceed
    LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME.

    Why a full-document layout and not a filter/box-shadow rule on
    #meterChrome itself (S4a's own consumer-injection style): tried that
    first and it stayed under budget by construction -- #meterChrome is a
    single non-inherited element, so no amount of expensive *styling* on
    it alone reaches --level's page-wide cost; the property's whole job is
    to make that impossible. The only way to prove this gate can fail is
    a consumer that does something --level-fast's scoping cannot prevent:
    reads the value, then goes and touches the page outside its own
    element (the DOM equivalent of resizing document.body). That is also
    exactly the transform/opacity-only rule's target failure mode, so
    this is a faithful "deliberately expensive consumer," not a
    strawman -- do not "simplify" this back to a filter/box-shadow rule
    on the mount; that version measured well under budget and this
    control's whole point is being able to fail."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_timeout(60)

    cdp = ctx.new_cdp_session(page)
    cdp.send("Performance.enable")

    js = """
    async ({ frames }) => {
      const mc = document.getElementById('meterChrome');
      let i = 0;
      return new Promise((resolve) => {
        function frame() {
          const v = Math.abs(Math.sin(i * 0.73)) * 0.9 + 0.05;
          mc.style.setProperty('--level-fast', v.toFixed(3));
          // Deliberately expensive: unlike production (a plain custom-
          // property write, confined to #meterChrome by inherits:false),
          // this consumer reacts by resizing document.body every frame --
          // a width change forces a full-document layout, the same
          // full-page-layout cost S4a measured on an inherited :root
          // write (~1.8ms/frame), regardless of --level-fast's own
          // non-inherited scoping.
          document.body.style.width = (99.5 + v * 0.5) + '%';
          void document.body.offsetHeight;
          i += 1;
          if (i < frames) requestAnimationFrame(frame);
          else { document.body.style.width = ''; resolve(null); }
        }
        requestAnimationFrame(frame);
      });
    }
    """
    page.evaluate(js, {"frames": 30})  # warm-up, unmeasured
    before = _cdp_perf_snapshot(cdp)
    page.evaluate(js, {"frames": LEVEL_BUS_BURST_FRAMES})
    after = _cdp_perf_snapshot(cdp)
    recalc_ms = (after["RecalcStyleDuration"] - before["RecalcStyleDuration"]) * 1000
    layout_ms = (after["LayoutDuration"] - before["LayoutDuration"]) * 1000
    per_frame_recalc = recalc_ms / LEVEL_BUS_BURST_FRAMES
    per_frame_layout = layout_ms / LEVEL_BUS_BURST_FRAMES
    print(f"\n-- deliberately expensive #meterChrome consumer -- {recalc_ms:.3f}ms recalc / "
          f"{layout_ms:.3f}ms layout over {LEVEL_BUS_BURST_FRAMES} frames "
          f"({per_frame_recalc:.4f}ms/frame recalc [budget {LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME}], "
          f"{per_frame_layout:.4f}ms/frame layout [budget {LEVEL_FAST_LAYOUT_BUDGET_MS_PER_FRAME}])")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    # A body-width resize every frame stays under the (already tight)
    # recalc budget but blows through the layout one -- the full-document
    # layout it forces is exactly what --level-fast's element-scoping and
    # inherits:false are supposed to make impossible for a *conforming*
    # consumer to trigger; a non-conforming one (this test) still can, and
    # the gate must catch it on at least one axis.
    assert per_frame_recalc > LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME or \
        per_frame_layout > LEVEL_FAST_LAYOUT_BUDGET_MS_PER_FRAME, (
        f"deliberately expensive #meterChrome consumer only cost {per_frame_recalc:.4f}ms/frame recalc / "
        f"{per_frame_layout:.4f}ms/frame layout -- at or under BOTH budgets "
        f"({LEVEL_FAST_RECALC_BUDGET_MS_PER_FRAME}/{LEVEL_FAST_LAYOUT_BUDGET_MS_PER_FRAME}), so this gate "
        "cannot actually catch a regression")


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


def test_firstrun_dismissal_remeasures_the_desktop_fit_gate(browser, static_server):
    """#firstrunOverlay covers the whole page, so any measured-fit-gate
    pass that runs while it's still up (document.fonts.ready, boot's own
    setTheme('instrument') call) hit-tests #ptt's centre against the
    overlay instead of #ptt, concludes #ptt doesn't fit, and releases
    html[data-s10="cap"] -- at a viewport that otherwise fits fine.
    dismissFirstrun() hid the overlay but never re-measured, so a
    first-time user (or anyone who clears site data) was stuck on the
    pre-S10 scrolling fallback until an unrelated resize/theme-change
    trigger happened to fire. Every other test in this file pre-sets
    va-firstrun-done (see the two tests above), so the whole suite was
    structurally blind to this -- the same shape as this repo's documented
    va-avatar:'off' blindness. Deliberately does NOT set va-avatar:'off'
    for that exact reason -- "on" is the real default, the avatar pane is
    300px of content KNOWN_TO_FIT's (instrument, 1010) entry was measured
    against, and the avatar canvas is the other known #ptt-coverer this
    whole gate exists to catch; disabling it here would test a narrower
    page than the user actually gets. 1280x1010 is a KNOWN_TO_FIT case for
    the default instrument theme (see the chassis-fit test above)."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 1010})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # No va-firstrun-done -- the whole point is to exercise the overlay.
    page.add_init_script("localStorage.setItem('va-ws-url', 'ws://192.0.2.1:9999');")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_timeout(1100)  # let the avatar module's own lazy import/mount settle
    assert page.locator("#firstrunOverlay").is_visible()

    page.locator("#firstrunSkip").click()
    assert page.locator("#firstrunOverlay").is_hidden()

    metrics = page.evaluate("""() => {
        // data-s10, not `getComputedStyle(d).display === 'flex'` -- V4
        // (see index.html's "V4: chassis widens" comment) made `.device` a
        // CSS grid at 1024px+ in BOTH the capped and released states, so
        // display no longer distinguishes them at this test's viewport.
        return {
            capped: document.documentElement.getAttribute('data-s10') === 'cap',
            scrollHeight: document.documentElement.scrollHeight,
            clientHeight: document.documentElement.clientHeight,
        };
    }""")

    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()

    assert metrics["capped"], (
        "the measured-fit gate is still released after dismissing #firstrunOverlay at a "
        "viewport known to fit -- dismissFirstrun() must re-measure"
    )
    assert metrics["scrollHeight"] <= metrics["clientHeight"], (
        f"page-level vertical overflow after firstrun dismissal: "
        f"scrollHeight={metrics['scrollHeight']} > clientHeight={metrics['clientHeight']}"
    )


def test_settings_panel_close_remeasures_the_desktop_fit_gate(browser, static_server):
    """Same defect shape as test_firstrun_dismissal_remeasures_the_desktop_fit_gate
    above, second instance: #settingsOverlay is `pointer-events: none` when
    closed but `pointer-events: auto` while the service panel is open
    (index.html's `#settingsOverlay.open` rule). A measured-fit-gate pass
    taken while the panel is open -- a theme swatch click's setTheme()
    call is the most ordinary trigger, since the swatches live inside that
    same panel -- hit-tests #ptt's centre and gets the overlay back
    instead of #ptt, concluding #ptt "isn't there" and releasing
    html[data-s10="cap"]. Nothing re-measures on close, so the user is
    stuck scrolling until an unrelated trigger fires. Avatar ON -- the
    real default, and the other known #ptt-coverer this whole gate exists
    to catch. 1280x1010 is a KNOWN_TO_FIT case for the default instrument
    theme (see the chassis-fit test above)."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1280, "height": 1010})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(1100)  # let the avatar module's own lazy import/mount settle

    page.click("#gearBtn")
    page.wait_for_function(
        "() => document.getElementById('settingsPanel').classList.contains('open')", timeout=10000)

    # The panel class alone doesn't prove #settingsOverlay is actually the
    # thing that defeats the hit-test -- assert the CSS property the bug
    # is actually about (`#settingsOverlay.open { pointer-events: auto }`,
    # index.html), not just a proxy for it. A click that silently failed
    # to open the panel would otherwise let this test pass for the wrong
    # reason and see nothing.
    overlay_pe = page.evaluate(
        "() => getComputedStyle(document.getElementById('settingsOverlay')).pointerEvents")
    assert overlay_pe == "auto", (
        f"#settingsOverlay is not pointer-events:auto after opening the panel (got "
        f"{overlay_pe!r}) -- the panel did not really open, so this test can't see the bug"
    )

    # setTheme() calls s10UpdateFit() at its own end (index.html) -- this
    # is the same trigger a theme swatch click fires, just skipping the
    # swatch DOM lookup. Run it while the panel (and #settingsOverlay) is
    # still open, so the measurement it takes is the one the bug is about.
    page.evaluate("() => setTheme('instrument')")
    page.wait_for_timeout(200)  # let s10UpdateFit()'s forced layout settle

    page.click("#settingsCloseBtn")
    page.wait_for_function(
        "() => !document.getElementById('settingsPanel').classList.contains('open')", timeout=10000)

    metrics = page.evaluate("""() => {
        // data-s10, not `getComputedStyle(d).display === 'flex'` -- see
        // the matching comment in
        // test_firstrun_dismissal_remeasures_the_desktop_fit_gate above.
        return {
            capped: document.documentElement.getAttribute('data-s10') === 'cap',
            scrollHeight: document.documentElement.scrollHeight,
            clientHeight: document.documentElement.clientHeight,
        };
    }""")

    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()

    assert metrics["capped"], (
        "the measured-fit gate is still released after closing #settingsPanel at a viewport "
        "known to fit -- a hit-test taken while the panel was open must not be defeated by "
        "#settingsOverlay's own pointer-events:auto"
    )
    assert metrics["scrollHeight"] <= metrics["clientHeight"], (
        f"page-level vertical overflow after closing the settings panel: "
        f"scrollHeight={metrics['scrollHeight']} > clientHeight={metrics['clientHeight']}"
    )


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


# ── Local overlay (themes/local, avatar/local): gitignored dirs that hold
# encumbered/private skins and avatar modules, merged in at boot if present.
# This developer's own clone has real, unrecoverable content there (see
# .gitignore) — these fixtures point at the disposable directories inside
# `_webclient_temp_root` (see that fixture, above `static_server`), never
# the real ones. Zero writes/moves/deletes ever touch the real repo dirs;
# only the temp root's own throwaway `themes/local`/`avatar/local` are
# created, populated, or reset between tests.


@pytest.fixture
def local_theme_dir(_webclient_temp_root):
    path = os.path.join(_webclient_temp_root, "themes", "local")
    yield path
    shutil.rmtree(path, ignore_errors=True)  # reset for the next test — the temp root only


@pytest.fixture
def local_avatar_dir(_webclient_temp_root):
    path = os.path.join(_webclient_temp_root, "avatar", "local")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _new_page(browser, static_server, server, extra_init="", console_records=None):
    """A page pointed at the stub control server, avatar module loading
    disabled (not under test here) — same shape as the `client` fixture, but
    self-contained so callers can write themes/local or avatar/local content
    on disk BEFORE navigating, with no fixture-ordering ambiguity.

    `console_records`: optional mutable list. If given, every console ERROR
    also gets appended there as (text, location) — location carries the
    failing resource's own url, which Chromium's generic "Failed to load
    resource" text never does — so a caller can narrow-filter by URL rather
    than by the (identical for every 404) text alone. `console_errors` (the
    plain-text list, unchanged) stays the default for every existing caller."""
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    console_errors = []

    def _on_console(m):
        if m.type != "error":
            return
        console_errors.append(m.text)
        if console_records is not None:
            console_records.append((m.text, m.location))

    page.on("pageerror", lambda e: errors.append(str(e)))
    # Attached before goto — a listener added after navigation would miss any
    # console.error a fast local fetch/import fires before the caller gets
    # around to checking it.
    page.on("console", _on_console)
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}" + extra_init)
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    return ctx, page, errors, console_errors


def test_local_theme_appears_and_loads_from_local_path(browser, static_server, local_theme_dir):
    """A theme registered only in themes/local/themes.json shows up in the
    picker, and selecting it injects a stylesheet from themes/local/<id>.css
    — not themes/<id>.css, which doesn't exist for a local-only id."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "localskin", "name": "Local Skin", "swatch": ["#111111", "#eeeeee"]}]))
    _write(os.path.join(local_theme_dir, "localskin.css"), "body{}")

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'localskin')",
        timeout=10000)

    names = page.evaluate("() => Array.from(document.querySelectorAll('#themeSwatches .swatchName'))"
                           ".map(n => n.textContent)")
    assert "Local Skin" in names, "local-only theme did not appear in the picker"

    page.evaluate("() => setTheme('localskin')")
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('link[rel=\"stylesheet\"]'))"
                           ".map(l => l.getAttribute('href'))")
    assert "themes/local/localskin.css" in hrefs, f"wrong stylesheet path, got: {hrefs}"
    assert "themes/localskin.css" not in hrefs

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_local_theme_font_faces_fetch_from_tracked_fonts_dir(browser, static_server, local_theme_dir):
    """A local skin lives one directory deeper than a tracked one
    (themes/local/<id>.css vs themes/<id>.css), so a CSS-relative @font-face
    url pointing at the shared TRACKED themes/fonts/ dir needs `../` to reach
    it — `url("fonts/...")` would resolve to the nonexistent
    themes/local/fonts/ and 404 silently. Covers BOTH weights (700 Bold, 900
    Black): checking only one is exactly what let a broken second url()
    through undetected once already. Asserts on the actual network response
    (status + a nonzero body), not document.fonts.check(), which can report
    true off a fallback face."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "localskin", "name": "Local Skin", "swatch": ["#111111", "#eeeeee"]}]))
    _write(os.path.join(local_theme_dir, "localskin.css"), (
        '@font-face { font-family: "LocalSkinTestFont"; '
        'src: url("../fonts/Cinzel-Bold.woff2") format("woff2"); '
        'font-weight: 700; font-style: normal; }\n'
        '@font-face { font-family: "LocalSkinTestFont"; '
        'src: url("../fonts/Cinzel-Black.woff2") format("woff2"); '
        'font-weight: 900; font-style: normal; }\n'
    ))

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    font_responses = {}  # weight -> Response, keyed as the requests are observed

    def on_response(resp):
        if "Cinzel-Bold.woff2" in resp.url:
            font_responses["700"] = resp
        elif "Cinzel-Black.woff2" in resp.url:
            font_responses["900"] = resp

    page.on("response", on_response)

    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'localskin')",
        timeout=10000)
    page.evaluate("() => setTheme('localskin')")
    # The @font-face rules only exist in the CSSOM once the injected <link>'s
    # stylesheet has actually loaded — race document.fonts.load() against
    # that and it can resolve empty before the rules are even parsed.
    page.wait_for_function(
        "() => Array.from(document.styleSheets).some(s => (s.href || '').includes('themes/local/localskin.css')"
        " && s.cssRules && s.cssRules.length > 0)",
        timeout=10000)

    # Awaited sequentially inside one evaluate() call so both network
    # requests have settled (success or failure) by the time it returns —
    # forces the fetch regardless of whether anything in the (known-broken,
    # pre-port) skin DOM currently renders text in this font.
    page.evaluate("""async () => {
        for (const w of ['700', '900']) {
            try { await document.fonts.load(w + ' 16px LocalSkinTestFont'); } catch {}
        }
    }""")

    for weight, needle in (("700", "Cinzel-Bold.woff2"), ("900", "Cinzel-Black.woff2")):
        assert weight in font_responses, f"no network request observed for {needle} ({weight})"
        resp = font_responses[weight]
        assert resp.status == 200, f"{needle} ({weight}) did not 200: {resp.status} {resp.url}"
        body = resp.body()
        assert len(body) > 0, f"{needle} ({weight}) returned an empty body"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_local_theme_collision_replaces_tracked_entry_in_place(browser, static_server, local_theme_dir):
    """A local id colliding with a tracked one replaces it IN PLACE — the
    picker's entry count and ordering versus the tracked-only case (just
    `hacker`, since chainsawman's retirement) must not shift just because a
    local override exists. Collides with `hacker` (the only tracked theme
    left) rather than a dedicated fixture id, since that's the only
    tracked entry available to collide with now."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "hacker", "name": "Local Override", "swatch": ["#010101", "#020202"]}]))

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.length === 1", timeout=10000)

    merged = page.evaluate("() => customThemes.map(t => ({id: t.id, name: t.name}))")
    assert merged == [{"id": "hacker", "name": "Local Override"}], \
        f"collision did not replace in place / order shifted: {merged}"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_absent_local_theme_manifest_is_silent(browser, static_server, local_theme_dir):
    """The clean-clone case: no themes/local/ directory at all. Tracked
    themes load as normal, with zero console errors and no unhandled
    rejection."""
    assert not os.path.isdir(local_theme_dir)  # fixture backed up any real content; nothing recreated it

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.length === 1", timeout=10000)

    tracked = page.evaluate("() => customThemes.map(t => t.id)")
    assert tracked == ["hacker"]
    # Chromium logs its own "Failed to load resource: 404" devtools network
    # entry for the local manifest 404 regardless of app code — unrelated to
    # and unsuppressable by loadCustomThemes; only app-level console.error
    # calls indicate a regression here.
    app_errors = [e for e in console_errors if "Failed to load resource" not in e]
    assert app_errors == [], f"console.error fired for an absent local manifest: {app_errors}"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


# ── Rigs (theme-owned three.js/canvas visuals, "module": true — see
# mountRigForTheme/teardownRig in index.html). These write their own throwaway
# themes/local/<id>.mjs into the temp root via local_theme_dir, same as the
# CSS-only local theme tests above.


def test_rig_module_loads_mounts_and_tears_down_on_theme_switch(browser, static_server, local_theme_dir):
    """A theme declaring "module": true gets its .mjs dynamic-imported and
    initRig() called with a host object exposing the mounts; switching away
    tears it down synchronously — destroy() runs, the mount it used is
    emptied, and __rigDebug.controller goes back to null."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "rigtest", "name": "Rig Test", "swatch": ["#111111", "#eeeeee"], "module": True}]))
    _write(os.path.join(local_theme_dir, "rigtest.css"), "body{}")
    _write(os.path.join(local_theme_dir, "rigtest.mjs"), (
        "export async function initRig(host) {\n"
        "  const marker = document.createElement('div');\n"
        "  marker.id = 'va-rig-marker';\n"
        "  host.mounts.meterChrome.appendChild(marker);\n"
        "  return { destroy() { window.__rigDestroyed = true; } };\n"
        "}\n"
    ))

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'rigtest')",
        timeout=10000)

    page.evaluate("() => setTheme('rigtest')")
    page.wait_for_function("() => !!document.getElementById('va-rig-marker')", timeout=5000)
    assert page.evaluate("() => window.__rigDebug.controller") is not None

    page.evaluate("() => setTheme('instrument')")
    page.wait_for_function("() => window.__rigDestroyed === true", timeout=5000)
    assert page.evaluate("() => document.getElementById('va-rig-marker')") is None, \
        "teardownRig() left the marker's mount dirty"
    assert page.evaluate("() => window.__rigDebug.controller") is None

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_rig_declaring_a_newer_hostapi_is_refused_legibly(browser, static_server, local_theme_dir):
    """A manifest entry whose hostApi outruns this build's HOST_API_VERSION
    stays in the picker — disabled, with an accessible name that says why —
    rather than vanishing or landing a rig against a host shape it wasn't
    written for. Both the click path and a direct setTheme() call must
    refuse it."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "futuretheme", "name": "Future Theme", "swatch": ["#111111", "#eeeeee"], "hostApi": 99}]))
    _write(os.path.join(local_theme_dir, "futuretheme.css"), "body{}")

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'futuretheme')",
        timeout=10000)

    before = page.evaluate("() => document.documentElement.getAttribute('data-theme')")

    swatch = page.evaluate(
        "() => { const b = document.querySelector('.themeSwatch.unavailable'); "
        "return b ? { disabled: b.disabled, label: b.getAttribute('aria-label') } : null; }")
    assert swatch is not None, "no swatch rendered with the unavailable class"
    assert swatch["disabled"] is True
    assert "newer cockpit" in swatch["label"], f"aria-label doesn't explain why: {swatch['label']}"

    page.evaluate("() => document.querySelector('.themeSwatch.unavailable').click()")
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == before

    page.evaluate("() => setTheme('futuretheme')")
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == before

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_a_rig_that_throws_on_init_degrades_to_a_plain_skin(browser, static_server, local_theme_dir):
    """initRig() throwing must not abort the theme switch or surface as an
    unhandled rejection — the CSS still applies, data-theme still updates,
    and __rigDebug.controller stays null (no half-mounted rig). The fixture
    sets window.__rigThrewFrom with the real host.themeId BEFORE throwing,
    so this can only pass if initRig() actually ran with a real host object
    — otherwise (e.g. mountRigForTheme() short-circuiting before ever
    importing the module) every remaining assertion here would also be true
    with the rig feature simply absent, and the test would be proving
    nothing."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "brokenrig", "name": "Broken Rig", "swatch": ["#111111", "#eeeeee"], "module": True}]))
    _write(os.path.join(local_theme_dir, "brokenrig.css"), "body{}")
    _write(os.path.join(local_theme_dir, "brokenrig.mjs"), (
        "export async function initRig(host) {\n"
        "  window.__rigThrewFrom = host.themeId;\n"
        "  throw new Error('boom');\n"
        "}\n"
    ))

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'brokenrig')",
        timeout=10000)

    page.evaluate("() => setTheme('brokenrig')")
    page.wait_for_function(
        "() => document.documentElement.getAttribute('data-theme') === 'brokenrig'", timeout=5000)
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('link[rel=\"stylesheet\"]'))"
                           ".map(l => l.getAttribute('href'))")
    assert "themes/local/brokenrig.css" in hrefs, "CSS did not stay applied after initRig() threw"

    # Give the rejected initRig() promise a tick to settle before asserting
    # on its aftermath — setTheme() doesn't await mountRigForTheme.
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.__rigThrewFrom") == "brokenrig", \
        "initRig() never actually ran with a real host object — this test would pass even with rigs deleted"
    assert page.evaluate("() => window.__rigDebug.controller") is None
    assert page.evaluate("() => document.querySelector('#ptt') !== null"), "page not still functional"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_base_instrument_path_unchanged_with_no_rig_present(browser, static_server):
    """No theme in this suite's default manifest ships a module — the
    default boot path must show zero rig activity, proving the only new
    work on every existing path is one absent-`module` check."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function("() => window.__rigDebug !== undefined", timeout=10000)

    assert page.evaluate("() => window.__rigDebug.controller") is None
    assert page.evaluate("() => window.__rigDebug.canvasCount") == 0

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


# ── S2: the teardown gate ──────────────────────────────────────────────
#
# Why this exists: S1 lets a theme's rig own a WebGL context. The failure
# mode is invisible — no error, no console warning, no failing test —
# canvases just go black after a user has switched themes a while, because
# the browser caps live WebGL contexts and silently evicts the oldest. This
# instruments the page (BEFORE any page script runs, via add_init_script)
# to count what a rig can leak: live WebGL contexts, still-advancing rAF
# loops, and net window/document listener registrations. Exposed as
# window.__leakProbe.snapshot().
#
# rAF detail: an outstanding-handle count alone reads a self-rescheduling
# loop as "1 outstanding" forever, which looks identical whether the loop is
# live or has quietly stopped rescheduling — it would never catch a leak.
# Instead this counts total rAF *firings* page-wide (fireCount) — the base
# page's only other rAF user is the PTT-hold meter animation
# (index.html:3213-3257), which these tests never trigger, so any advance
# in fireCount after a rig's destroy() is the rig's own loop still running.
LEAK_PROBE_INIT = """
(function () {
  const probe = { webgl: { created: 0, live: new Set() }, raf: { fireCount: 0 }, listeners: {} };

  // WebGL contexts: created on getContext(), released on WEBGL_lose_context's
  // loseContext() (three.js's own forceContextLoss() path — released
  // synchronously here rather than waiting on the async webglcontextlost
  // event) or on the event itself as a backup for a rig that lost its
  // context some other way.
  const origGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, ...args) {
    const ctx = origGetContext.call(this, type, ...args);
    if (ctx && /^(webgl2?|experimental-webgl)$/.test(type) && !probe.webgl.live.has(ctx)) {
      probe.webgl.created += 1;
      probe.webgl.live.add(ctx);
      this.addEventListener('webglcontextlost', () => { probe.webgl.live.delete(ctx); });
      const origGetExtension = ctx.getExtension.bind(ctx);
      ctx.getExtension = function (name) {
        const ext = origGetExtension(name);
        if (name === 'WEBGL_lose_context' && ext && !ext.__leakProbeWrapped) {
          ext.__leakProbeWrapped = true;
          const origLoseContext = ext.loseContext.bind(ext);
          ext.loseContext = function (...a) {
            probe.webgl.live.delete(ctx);
            return origLoseContext(...a);
          };
        }
        return ext;
      };
    }
    return ctx;
  };

  // rAF: wrap the pair so both fireCount (has anything fired since a given
  // snapshot) and outstanding (handles requested but neither fired nor
  // cancelled) are visible.
  const origRAF = window.requestAnimationFrame.bind(window);
  const origCAF = window.cancelAnimationFrame.bind(window);
  const pendingRafHandles = new Set(); // guards against double-decrement when
                                        // cancelAnimationFrame is called on a
                                        // handle that already fired
  window.requestAnimationFrame = function (fn) {
    const handle = origRAF(function (ts) {
      pendingRafHandles.delete(handle);
      probe.raf.fireCount += 1;
      return fn(ts);
    });
    pendingRafHandles.add(handle);
    return handle;
  };
  window.cancelAnimationFrame = function (handle) {
    pendingRafHandles.delete(handle);
    return origCAF(handle);
  };

  // Listeners: window and document, net registrations keyed by type, so a
  // removeEventListener that doesn't match anything added can't go negative.
  // Two standard patterns remove a listener WITHOUT ever calling
  // removeEventListener — { once: true } (the browser removes it after it
  // fires) and { signal } (removed when the AbortSignal aborts) — both must
  // also decrement, or a correct rig using either reads as a false-positive
  // leak.
  function instrument(target, label) {
    const byType = new Map(); // type -> Map(fn -> count)
    // type -> Map(fn -> onceWrapper) — the wrapper is what's actually
    // registered with the target for a `once` listener, so an explicit
    // removeEventListener(type, fn) before it ever fires must be forwarded
    // to the WRAPPER, or the real listener stays live while the probe
    // thinks it's gone (a false negative the probe itself would have
    // caused, by substituting a function the caller never asked to remove).
    const onceWrappers = new Map();
    const origAdd = target.addEventListener.bind(target);
    const origRemove = target.removeEventListener.bind(target);
    function decrement(type, fn) {
      const m = byType.get(type);
      if (m && m.has(fn)) {
        const next = m.get(fn) - 1;
        if (next <= 0) m.delete(fn); else m.set(fn, next);
      }
    }
    target.addEventListener = function (type, fn, opts) {
      if (!byType.has(type)) byType.set(type, new Map());
      const m = byType.get(type);
      m.set(fn, (m.get(fn) || 0) + 1);
      const normalized = (typeof opts === 'object' && opts !== null) ? opts : {};
      if (normalized.once) {
        const onceWrapper = function (...args) {
          onceWrappers.get(type)?.delete(fn); // fired — its own removal, not a caller's
          decrement(type, fn); // decrement the key WE incremented (the original fn)
          return fn.apply(this, args);
        };
        if (!onceWrappers.has(type)) onceWrappers.set(type, new Map());
        onceWrappers.get(type).set(fn, onceWrapper);
        return origAdd(type, onceWrapper, opts);
      }
      if (normalized.signal) {
        normalized.signal.addEventListener('abort', () => decrement(type, fn), { once: true });
      }
      return origAdd(type, fn, opts);
    };
    target.removeEventListener = function (type, fn, opts) {
      const wrappers = onceWrappers.get(type);
      if (wrappers && wrappers.has(fn)) {
        const wrapper = wrappers.get(fn);
        wrappers.delete(fn);
        decrement(type, fn);
        return origRemove(type, wrapper, opts); // remove what's ACTUALLY registered
      }
      decrement(type, fn);
      return origRemove(type, fn, opts);
    };
    probe.listeners[label] = byType;
  }
  instrument(window, 'window');
  instrument(document, 'document');

  function listenerNetCount() {
    let total = 0;
    for (const byType of Object.values(probe.listeners)) {
      for (const m of byType.values()) for (const c of m.values()) total += c;
    }
    return total;
  }

  window.__leakProbe = {
    snapshot() {
      return {
        webglLive: probe.webgl.live.size,
        webglCreated: probe.webgl.created,
        rafFireCount: probe.raf.fireCount,
        rafOutstanding: pendingRafHandles.size,
        listenerNet: listenerNetCount(),
      };
    },
  };
})();
"""


# The REALISTIC mistake: a real three.js renderer, a hand-rolled
# requestAnimationFrame render loop (not renderer.setAnimationLoop() — many
# rigs write their own, and it matters: setAnimationLoop's internal loop is
# stopped BY renderer.dispose() itself, via animation.stop(), which would
# mask this exact leak class if used here), and a resize listener. destroy()
# calls only renderer.dispose() — tears down internal subsystems but does
# NOT release the WebGL context, does NOT touch this rig's own rAF handle,
# and does NOT remove the listener (see plan §3.3). All three leak classes
# must trip.
LEAKY_RIG_MJS = (
    "import * as THREE from 'three';\n"
    "export async function initRig(host) {\n"
    "  const canvas = host.createCanvas('meterChrome');\n"
    "  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true });\n"
    "  renderer.setSize(64, 64, false);\n"
    "  const scene = new THREE.Scene();\n"
    "  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 10);\n"
    "  camera.position.z = 2;\n"
    "  const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial({ color: 0x00ff00 }));\n"
    "  scene.add(mesh);\n"
    "  function onResize() { renderer.setSize(64, 64, false); }\n"
    "  window.addEventListener('resize', onResize);\n"
    "  function loop() { mesh.rotation.y += 0.01; renderer.render(scene, camera); requestAnimationFrame(loop); }\n"
    "  requestAnimationFrame(loop);\n"
    "  return {\n"
    "    destroy() { renderer.dispose(); },\n"
    "  };\n"
    "}\n"
)

# The full discipline, same manual-rAF construction as LEAKY_RIG_MJS so the
# comparison is apples-to-apples: cancel the rig's own rAF handle, remove
# the listener by the same function reference, dispose scene resources,
# dispose() then forceContextLoss(), drop the canvas. Zero leaks.
CLEAN_RIG_MJS = (
    "import * as THREE from 'three';\n"
    "export async function initRig(host) {\n"
    "  const canvas = host.createCanvas('meterChrome');\n"
    "  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true });\n"
    "  renderer.setSize(64, 64, false);\n"
    "  const scene = new THREE.Scene();\n"
    "  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 10);\n"
    "  camera.position.z = 2;\n"
    "  const geometry = new THREE.BoxGeometry(1, 1, 1);\n"
    "  const material = new THREE.MeshBasicMaterial({ color: 0x00ff00 });\n"
    "  const mesh = new THREE.Mesh(geometry, material);\n"
    "  scene.add(mesh);\n"
    "  function onResize() { renderer.setSize(64, 64, false); }\n"
    "  window.addEventListener('resize', onResize);\n"
    "  let rafHandle = null;\n"
    "  function loop() { mesh.rotation.y += 0.01; renderer.render(scene, camera); rafHandle = requestAnimationFrame(loop); }\n"
    "  rafHandle = requestAnimationFrame(loop);\n"
    "  return {\n"
    "    destroy() {\n"
    "      cancelAnimationFrame(rafHandle);\n"
    "      window.removeEventListener('resize', onResize);\n"
    "      scene.traverse((obj) => {\n"
    "        if (obj.geometry) obj.geometry.dispose();\n"
    "        if (obj.material) obj.material.dispose();\n"
    "      });\n"
    "      renderer.dispose();\n"
    "      renderer.forceContextLoss();\n"
    "      canvas.remove();\n"
    "    },\n"
    "  };\n"
    "}\n"
)


def _write_rig_theme(local_theme_dir, theme_id, name, mjs_source):
    """Shared setup for the teardown-gate tests: a themes.json entry with
    "module": true, a no-op CSS file, and the given rig module source."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": theme_id, "name": name, "swatch": ["#111111", "#eeeeee"], "module": True}]))
    _write(os.path.join(local_theme_dir, f"{theme_id}.css"), "body{}")
    _write(os.path.join(local_theme_dir, f"{theme_id}.mjs"), mjs_source)


# ── S3: the host-side reaper ────────────────────────────────────────────
# Same rig, same real three.js renderer as LEAKY_RIG_MJS above, but destroy()
# is a true no-op — not even renderer.dispose(). This is the case teardownRig()
# itself must defend against: a rig that skipped or threw partway through its
# own cleanup and left its canvas still attached to its mount.
SKIP_DESTROY_RIG_MJS = (
    "import * as THREE from 'three';\n"
    "export async function initRig(host) {\n"
    "  const canvas = host.createCanvas('meterChrome');\n"
    "  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true });\n"
    "  renderer.setSize(64, 64, false);\n"
    "  const scene = new THREE.Scene();\n"
    "  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 10);\n"
    "  camera.position.z = 2;\n"
    "  const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshBasicMaterial({ color: 0x00ff00 }));\n"
    "  scene.add(mesh);\n"
    "  function loop() { mesh.rotation.y += 0.01; renderer.render(scene, camera); requestAnimationFrame(loop); }\n"
    "  requestAnimationFrame(loop);\n"
    "  return { destroy() {} };\n"
    "}\n"
)

# Orphans its own canvas by detaching it directly, WITHOUT a theme switch and
# WITHOUT losing its context — teardownRig() never runs here at all, so only
# the 100ms interval reaper can catch this. Plain WebGL (no three.js) is
# enough: createRigCanvas()'s per-canvas getContext wrap only needs a real
# call to record something into the registry.
ORPHAN_RIG_MJS = (
    "export async function initRig(host) {\n"
    "  const canvas = host.createCanvas('meterChrome');\n"
    "  canvas.getContext('webgl');\n"
    "  window.__orphanCanvas = () => canvas.remove();\n"
    "  return { destroy() {} };\n"
    "}\n"
)

# Wraps the host-created canvas in a positioning <div> — completely ordinary
# rig code, and it makes the canvas a GRANDCHILD of its mount rather than a
# direct child. destroy() is a no-op, same shape as SKIP_DESTROY_RIG_MJS.
NESTED_CANVAS_RIG_MJS = (
    "export async function initRig(host) {\n"
    "  const canvas = host.createCanvas('meterChrome');\n"
    "  const wrapper = document.createElement('div');\n"
    "  wrapper.appendChild(canvas);\n"
    "  host.mounts.meterChrome.appendChild(wrapper);\n"
    "  canvas.getContext('webgl');\n"
    "  return { destroy() {} };\n"
    "}\n"
)


def test_the_host_heals_a_context_leak_a_rig_left_behind(browser, static_server, local_theme_dir):
    """LEAKY_RIG_MJS's destroy() calls only renderer.dispose() (the
    realistic mistake, not a strawman — see plan §3.3), which per upstream
    docs tears down internal subsystems but does NOT release the WebGL
    context — before S3 existed this was a genuine leak. teardownRig()'s
    registry-wide release now catches it: every entry left in rigCanvases at
    teardown time is dead by definition, so it gets released regardless of
    what destroy() did or didn't do. New behaviour, its own name, split out
    of what used to be test_teardown_gate_catches_a_leaky_rig."""
    _write_rig_theme(local_theme_dir, "leakyrig", "Leaky Rig", LEAKY_RIG_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server, extra_init=LEAK_PROBE_INIT)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'leakyrig')",
        timeout=10000)

    before = page.evaluate("() => window.__leakProbe.snapshot()")

    page.evaluate("() => setTheme('leakyrig')")
    page.wait_for_function("() => window.__rigDebug.controller !== null", timeout=5000)
    page.evaluate("() => setTheme('instrument')")
    page.wait_for_function("() => window.__rigDebug.controller === null", timeout=5000)

    after = page.evaluate("() => window.__leakProbe.snapshot()")
    print(f"leaky rig context-healing probe: before={before} after={after}")

    assert after["webglLive"] == before["webglLive"], \
        f"the host did not heal the leaky rig's WebGL context (before={before}, after={after})"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_teardown_gate_still_catches_the_undefended_leak_classes(browser, static_server, local_theme_dir):
    """The proof-it-fails half, permanently encoded — WebGL contexts are
    deliberately OUT of scope for this test now that the host heals them
    itself (see test_the_host_heals_a_context_leak_a_rig_left_behind); S3 is
    scoped to WebGL contexts only (plan §3.4) and does not touch a rig's
    rAF loop or its own event listeners, so those two classes remain real
    and undefended by design — the reason a rig author must still implement
    destroy() properly. If either of these ever stops failing to hold, the
    probe or this fixture regressed."""
    _write_rig_theme(local_theme_dir, "leakyrig", "Leaky Rig", LEAKY_RIG_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server, extra_init=LEAK_PROBE_INIT)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'leakyrig')",
        timeout=10000)

    before = page.evaluate("() => window.__leakProbe.snapshot()")

    page.evaluate("() => setTheme('leakyrig')")
    page.wait_for_function("() => window.__rigDebug.controller !== null", timeout=5000)
    page.evaluate("() => setTheme('instrument')")
    page.wait_for_function("() => window.__rigDebug.controller === null", timeout=5000)

    mid = page.evaluate("() => window.__leakProbe.snapshot()")
    page.wait_for_timeout(200)  # let a still-running rAF loop fire a few more frames
    after = page.evaluate("() => window.__leakProbe.snapshot()")

    print(f"leaky rig probe: before={before} mid={mid} after={after}")

    assert after["rafFireCount"] > mid["rafFireCount"], \
        f"leaky rig's rAF loop stopped advancing after teardown (mid={mid}, after={after}) " \
        f"— the probe or the fixture regressed"
    assert after["listenerNet"] > before["listenerNet"], \
        f"leaky rig did not leave a net-new listener (before={before}, after={after})"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_a_clean_rig_leaks_nothing_across_a_b_a(browser, static_server, local_theme_dir):
    """The full-discipline rig (plan §3.3's destroy() order: stop the loop,
    remove the listener by reference, dispose scene resources, dispose()
    then forceContextLoss(), drop the canvas) switching instrument ->
    cleanrig -> instrument must leave zero live contexts, no still-advancing
    rAF loop, and net-zero listeners."""
    _write_rig_theme(local_theme_dir, "cleanrig", "Clean Rig", CLEAN_RIG_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server, extra_init=LEAK_PROBE_INIT)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'cleanrig')",
        timeout=10000)

    before = page.evaluate("() => window.__leakProbe.snapshot()")

    page.evaluate("() => setTheme('cleanrig')")
    page.wait_for_function("() => window.__rigDebug.controller !== null", timeout=5000)

    # Positive control, while the rig is still mounted and running: prove
    # the probe can actually SEE rAF advancing in this environment before
    # trusting a later "stopped advancing" reading as meaningful. A gate
    # that can only report "clean" — never "dirty" — is measuring nothing;
    # this is what would have caught rAF throttling/backgrounding silently
    # neutering the leak assertion below.
    running_start = page.evaluate("() => window.__leakProbe.snapshot()")
    page.wait_for_timeout(200)
    running_end = page.evaluate("() => window.__leakProbe.snapshot()")
    rig_advance = running_end["rafFireCount"] - running_start["rafFireCount"]
    print(f"clean rig probe, positive control (rig mounted+running): "
          f"start={running_start} end={running_end} advance={rig_advance}")
    assert rig_advance >= 3, (
        f"the probe observed only {rig_advance} rAF firings over 200ms with a live rig "
        f"running (start={running_start}, end={running_end}) — rAF is throttled or "
        f"backgrounded in this environment, so the probe cannot see a leak here and the "
        f"assertions below would prove nothing"
    )

    page.evaluate("() => setTheme('instrument')")
    page.wait_for_function("() => window.__rigDebug.controller === null", timeout=5000)

    mid = page.evaluate("() => window.__leakProbe.snapshot()")
    page.wait_for_timeout(200)
    after = page.evaluate("() => window.__leakProbe.snapshot()")

    print(f"clean rig probe: before={before} mid={mid} after={after}")

    assert after["webglLive"] == before["webglLive"], \
        f"clean rig left a live WebGL context (before={before}, after={after})"
    assert after["rafFireCount"] == mid["rafFireCount"], \
        f"clean rig's rAF loop is still advancing after teardown (mid={mid}, after={after})"
    assert after["listenerNet"] == before["listenerNet"], \
        f"clean rig left a net-new listener (before={before}, after={after})"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_mount_returns_to_baseline_after_a_rig_is_torn_down(browser, static_server, local_theme_dir):
    """teardownRig() restores each mount's captured boot baseline (S1) — pin
    that #meterChrome's DOM is byte-identical before any rig mounts and
    after a full instrument -> cleanrig -> instrument cycle."""
    _write_rig_theme(local_theme_dir, "cleanrig", "Clean Rig", CLEAN_RIG_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'cleanrig')",
        timeout=10000)

    baseline = page.evaluate("() => document.getElementById('meterChrome').innerHTML")

    page.evaluate("() => setTheme('cleanrig')")
    page.wait_for_function("() => window.__rigDebug.controller !== null", timeout=5000)
    page.evaluate("() => setTheme('instrument')")
    page.wait_for_function("() => window.__rigDebug.controller === null", timeout=5000)

    after = page.evaluate("() => document.getElementById('meterChrome').innerHTML")

    print(f"#meterChrome baseline length={len(baseline)} after-cycle length={len(after)} match={after == baseline}")

    assert after == baseline, "#meterChrome did not return to its boot baseline after a rig cycle"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_a_rig_that_skips_destroy_still_does_not_leak(browser, static_server, local_theme_dir):
    """A rig whose destroy() is a true no-op (SKIP_DESTROY_RIG_MJS) still
    leaves zero live WebGL contexts and a mount back at baseline after
    switching away — teardownRig()'s own registry-wide release catches it,
    not the interval reaper, since the entry is still live in rigCanvases
    the instant teardownRig() runs. This is the synchronous half of S3."""
    _write_rig_theme(local_theme_dir, "skipdestroyrig", "Skip Destroy Rig", SKIP_DESTROY_RIG_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server, extra_init=LEAK_PROBE_INIT)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'skipdestroyrig')",
        timeout=10000)

    before = page.evaluate("() => window.__leakProbe.snapshot()")
    baseline = page.evaluate("() => document.getElementById('meterChrome').innerHTML")

    page.evaluate("() => setTheme('skipdestroyrig')")
    page.wait_for_function("() => window.__rigDebug.controller !== null", timeout=5000)
    page.evaluate("() => setTheme('instrument')")
    page.wait_for_function("() => window.__rigDebug.controller === null", timeout=5000)

    after = page.evaluate("() => window.__leakProbe.snapshot()")
    after_html = page.evaluate("() => document.getElementById('meterChrome').innerHTML")

    print(f"skip-destroy rig probe: before={before} after={after}")

    assert after["webglLive"] == before["webglLive"], \
        f"teardownRig()'s registry release did not reap the skipped-destroy rig's context (before={before}, after={after})"
    assert after_html == baseline, "#meterChrome did not return to baseline after the skipped-destroy rig cycle"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_a_nested_canvas_still_gets_its_context_released(browser, static_server, local_theme_dir):
    """A rig that wraps its host.createCanvas() output in a positioning
    <div> (NESTED_CANVAS_RIG_MJS) — completely ordinary rig code — leaves
    the canvas as a GRANDCHILD of its mount, not a direct child. A
    teardown mechanism that walks only direct mount children misses it
    silently; releasing every entry in the rigCanvases registry itself,
    regardless of DOM position, is what makes this safe."""
    _write_rig_theme(local_theme_dir, "nestedrig", "Nested Rig", NESTED_CANVAS_RIG_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server, extra_init=LEAK_PROBE_INIT)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'nestedrig')",
        timeout=10000)

    before = page.evaluate("() => window.__leakProbe.snapshot()")

    page.evaluate("() => setTheme('nestedrig')")
    page.wait_for_function("() => window.__rigDebug.controller !== null", timeout=5000)
    page.evaluate("() => setTheme('instrument')")
    page.wait_for_function("() => window.__rigDebug.controller === null", timeout=5000)

    after = page.evaluate("() => window.__leakProbe.snapshot()")
    print(f"nested canvas rig probe: before={before} after={after}")

    assert after["webglLive"] == before["webglLive"], \
        f"nested canvas's WebGL context was not released on teardown (before={before}, after={after})"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_the_reaper_collects_a_canvas_orphaned_without_teardown(browser, static_server, local_theme_dir):
    """A rig (ORPHAN_RIG_MJS) that detaches its own canvas WITHOUT a theme
    switch and WITHOUT losing its context — teardownRig() never runs here at
    all, so this can only pass if the 100ms interval reaper independently
    notices the canvas is no longer connected and releases it."""
    _write_rig_theme(local_theme_dir, "orphanrig", "Orphan Rig", ORPHAN_RIG_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server, extra_init=LEAK_PROBE_INIT)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'orphanrig')",
        timeout=10000)

    page.evaluate("() => setTheme('orphanrig')")
    page.wait_for_function("() => window.__rigDebug.controller !== null", timeout=5000)
    page.wait_for_function("() => typeof window.__orphanCanvas === 'function'", timeout=5000)

    before = page.evaluate("() => window.__leakProbe.snapshot()")
    assert before["webglLive"] >= 1, "fixture did not actually obtain a WebGL context to orphan"
    assert page.evaluate("() => window.__rigDebug.reaperActive") is True, \
        "reaper did not start on the first createCanvas() call"

    page.evaluate("() => window.__orphanCanvas()")
    # Wait past a couple of 100ms sweep intervals rather than asserting
    # immediately — this is what proves the INTERVAL path, not a synchronous
    # one, did the work.
    page.wait_for_function("() => window.__rigDebug.reaped > 0", timeout=3000)

    after = page.evaluate("() => window.__leakProbe.snapshot()")
    reaped = page.evaluate("() => window.__rigDebug.reaped")

    print(f"orphan rig probe: before={before} after={after} reaped={reaped}")

    assert after["webglLive"] < before["webglLive"], \
        f"the interval reaper did not release the orphaned canvas's context (before={before}, after={after})"
    assert reaped >= 1

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_no_reaper_timer_runs_when_no_rig_is_active(browser, static_server):
    """The default theme ships no rig — the reaper must be lazily started
    (never running by default), the same R10 discipline S1 pinned for
    teardown."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function("() => window.__rigDebug !== undefined", timeout=10000)

    assert page.evaluate("() => window.__rigDebug.reaperActive") is False

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_ptt_size_token_actually_sizes_the_button(browser, static_server, local_theme_dir):
    """--ptt-size/--ptt-radius were dead: #ptt was declared twice at equal
    specificity and the later, unscoped, hardcoded rule always won, so no
    theme could actually resize the button by setting the token. A theme
    declaring a different --ptt-size must now actually resize #ptt; the base
    instrument's PTT must stay exactly 78px so the "zero visual change for
    every existing theme" claim behind the fix is pinned, not just asserted."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "ptttest", "name": "PTT Test", "swatch": ["#111111", "#eeeeee"]}]))
    _write(os.path.join(local_theme_dir, "ptttest.css"),
           '[data-theme="ptttest"] { --ptt-size: 120px; --ptt-radius: 10px; }\n')

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'ptttest')",
        timeout=10000)

    base_width = page.evaluate("() => document.getElementById('ptt').getBoundingClientRect().width")
    assert base_width == 78, f"base instrument PTT is not 78px wide: {base_width}"

    page.evaluate("() => setTheme('ptttest')")
    page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'ptttest'")
    themed_width = page.evaluate("() => document.getElementById('ptt').getBoundingClientRect().width")
    assert themed_width == 120, f"--ptt-size did not resize #ptt, got {themed_width}px — the token is still dead"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


LOCAL_AVATAR_REGISTRY_MJS = (
    "export default [{ id: 'localhead', name: 'Local Head', kind: '2d' }];\n"
)


def test_local_avatar_registry_entry_appears_in_picker(browser, static_server, local_avatar_dir):
    """An avatar contributed only by avatar/local/registry.mjs shows up in
    #avatarSelect."""
    os.makedirs(local_avatar_dir, exist_ok=True)
    _write(os.path.join(local_avatar_dir, "registry.mjs"), LOCAL_AVATAR_REGISTRY_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => Array.from(document.getElementById('avatarSelect').options).some(o => o.value === 'localhead')",
        timeout=10000)

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_absent_local_avatar_registry_is_silent(browser, static_server, local_avatar_dir):
    """No avatar/local/registry.mjs (the normal case) — the built-in
    registry populates the picker and logs no error."""
    assert not os.path.isdir(local_avatar_dir)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => document.getElementById('avatarSelect').options.length > 0", timeout=10000)

    values = page.evaluate("() => Array.from(document.getElementById('avatarSelect').options).map(o => o.value)")
    assert "localhead" not in values
    assert "brunette" in values
    # Same native 404 devtools noise as the theme manifest case (see there);
    # filtered out, only app-level console.error calls should remain.
    app_errors = [e for e in console_errors if "Failed to load resource" not in e]
    assert app_errors == [], f"console.error fired for an absent local registry: {app_errors}"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_saved_local_avatar_id_resolves_instead_of_falling_back(browser, static_server, local_avatar_dir):
    """The async-import ordering seam: a saved va-avatar-model pointing at a
    LOCAL avatar id must resolve to that entry once the local registry has
    merged, not silently fall back to DEFAULT_AVATAR_ID because the picker
    (and selectedAvatarId's validity check) ran before the merge landed."""
    os.makedirs(local_avatar_dir, exist_ok=True)
    _write(os.path.join(local_avatar_dir, "registry.mjs"), LOCAL_AVATAR_REGISTRY_MJS)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(
        browser, static_server, server,
        extra_init="localStorage.setItem('va-avatar-model', 'localhead');")
    page.wait_for_function(
        "() => document.getElementById('avatarSelect').options.length > 0", timeout=10000)

    assert page.evaluate("() => selectedAvatarId()") == "localhead", \
        "saved local avatar id fell back to the default instead of resolving"
    assert page.evaluate("() => document.getElementById('avatarSelect').value") == "localhead", \
        "the picker's selected option did not reflect the saved local avatar id"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def _parse_avatar_registry(index_html_src):
    """Text-scrape the exported index.html's `const AVATAR_REGISTRY = [...]`
    literal — deliberately not a JS eval, so this stays honest about the
    exported tree's actual bytes rather than trusting some other module to
    interpret them. Each entry is one line; id/kind/model are pulled by
    field, not by a fixed set of known ids, so a future entry is covered
    automatically."""
    m = re.search(r"const AVATAR_REGISTRY = \[(.*?)\n\];", index_html_src, re.S)
    assert m, "AVATAR_REGISTRY literal not found — index.html format changed, update this test's scrape"
    entries = []
    for line in m.group(1).splitlines():
        idm = re.search(r'id:\s*"([^"]+)"', line)
        if not idm:
            continue
        kindm = re.search(r'kind:\s*"([^"]+)"', line)
        modelm = re.search(r'model:\s*"([^"]+)"', line)
        entries.append({
            "id": idm.group(1),
            "kind": kindm.group(1) if kindm else None,
            "model": modelm.group(1) if modelm else None,
        })
    return entries


def test_public_avatar_registry_assets_all_ship(tmp_path):
    """Every entry in the PUBLIC AVATAR_REGISTRY must resolve to an asset
    that actually ships in the export — not just checking what's IN the
    export, but whether what ships can run. The 2D Monarch entry shipped
    with no image (webclient/avatar/refs/ is gitignored, never tracked),
    leaving a dead, unloadable row in a downstream stranger's avatar picker.

    Runs against the real `scripts/export-public.sh` output (git-ls-files
    based), not the browser harness's `_webclient_temp_root` — that fixture
    only excludes themes/local/ and avatar/local/ by name, so a gitignored
    asset elsewhere (like avatar/refs/) that still happens to exist on this
    dev box would pass there without proving anything. The export is the
    actual clean-clone condition.

    Mechanical, not a hardcoded id list: kind "3d" entries are checked via
    their own `model` path; kind "2d" entries via avatar2d.mjs's own
    MONARCH_URL constant, scraped from its source rather than restated here.
    A future avatar with a missing asset, or an unrecognized kind, fails
    this automatically."""
    repo_root = os.path.dirname(HERE)
    if not _release_tooling_available(repo_root):
        pytest.skip("release tooling is not part of the public export "
                    "(scripts/ and git history are private-tree-only)")
    dest = os.path.join(str(tmp_path), "pbexp")
    export = subprocess.run(
        ["bash", "scripts/export-public.sh", dest],
        cwd=repo_root, capture_output=True, text=True, timeout=60)
    assert export.returncode == 0, f"export failed:\nstdout: {export.stdout}\nstderr: {export.stderr}"

    web_dir = os.path.join(dest, "webclient")
    with open(os.path.join(web_dir, "index.html")) as f:
        registry = _parse_avatar_registry(f.read())
    assert registry, "empty AVATAR_REGISTRY in the export — parsing regression, not the bug under test"

    with open(os.path.join(web_dir, "avatar", "avatar2d.mjs")) as f:
        avatar2d_src = f.read()
    kind_2d_match = re.search(r'MONARCH_URL\s*=\s*"([^"]+)"', avatar2d_src)
    assert kind_2d_match, "avatar2d.mjs no longer defines MONARCH_URL the same way — update this test's scrape"
    kind_2d_path = kind_2d_match.group(1)

    failures = []
    for entry in registry:
        if entry["kind"] == "3d":
            rel = entry["model"]
            if not rel:
                failures.append(f"{entry['id']}: kind 3d entry with no model path")
                continue
        elif entry["kind"] == "2d":
            rel = kind_2d_path
        else:
            failures.append(f"{entry['id']}: unrecognized kind {entry['kind']!r} — teach this test its asset path")
            continue
        asset_path = os.path.normpath(os.path.join(web_dir, rel))
        if not os.path.isfile(asset_path) or os.path.getsize(asset_path) == 0:
            failures.append(f"{entry['id']} ({rel}): missing or empty asset at {asset_path}")

    assert not failures, "public avatar registry entries with missing/empty assets:\n" + "\n".join(failures)


def _release_tooling_available(repo_root):
    """True only in the real dev repo: scripts/ ships there but is
    deliberately excluded from every export (export-public.sh's own
    EXCLUDES — "release/QA tooling stays private"), and an exported tree
    has no `.git` at all, so `git show`/`git ls-files` history is
    unavailable too. Tests that need either are private-tree-only and
    should skip cleanly wherever this is false, not fail — a downstream
    stranger running the full suite off their own clone ships this exact
    file verbatim and would otherwise hit a nuisance failure for a dev-only
    reason unrelated to anything they broke."""
    if not os.path.isfile(os.path.join(repo_root, "scripts", "export-public.sh")):
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == "true"


def test_export_script_excludes_local_overlay_dirs(tmp_path):
    """scripts/export-public.sh must exit 0 and produce a tree with neither
    webclient/themes/local/ nor webclient/avatar/local/ anywhere — they're
    untracked (git ls-files already skips them), so the export needs no
    special-case transform for them at all."""
    repo_root = os.path.dirname(HERE)
    if not _release_tooling_available(repo_root):
        pytest.skip("release tooling is not part of the public export "
                    "(scripts/ and git history are private-tree-only)")
    dest = os.path.join(str(tmp_path), "pbexp")
    result = subprocess.run(
        ["bash", "scripts/export-public.sh", dest],
        cwd=repo_root, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"export failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert os.path.isfile(dest + ".report.txt")

    local_hits = [os.path.join(root, d) for root, dirs, _ in os.walk(dest) for d in dirs if d == "local"]
    assert local_hits == [], f"local overlay dir(s) leaked into the export: {local_hits}"


def test_exported_tree_test_suite_still_collects(tmp_path):
    """The export gate above only sweeps for banned strings — it says
    nothing about whether the exported test_webclient.py can even be
    IMPORTED by pytest on a clean clone, where themes/local/ never exists.
    A module-level file read at collection time (this exact defect shipped
    once already) makes pytest abort with "N errors during collection" and
    ZERO tests run — worse than a failing test, since a stranger reasonably
    reads that as the whole project being broken. --collect-only keeps this
    fast (no browser launch) while still exercising the failure mode that
    matters: import-time explosion. Collecting is not passing, though — this
    is also the release gate, so a second, real (non-collect-only) run
    follows: zero failures allowed, skips fine (the genuinely
    private-tree-only tests each skip with their own clear reason)."""
    repo_root = os.path.dirname(HERE)
    if not _release_tooling_available(repo_root):
        pytest.skip("release tooling is not part of the public export "
                    "(scripts/ and git history are private-tree-only)")
    dest = os.path.join(str(tmp_path), "pbexp")
    export = subprocess.run(
        ["bash", "scripts/export-public.sh", dest],
        cwd=repo_root, capture_output=True, text=True, timeout=60)
    assert export.returncode == 0, f"export failed:\nstdout: {export.stdout}\nstderr: {export.stderr}"

    collect = subprocess.run(
        ["python3", "-m", "pytest", "test_webclient.py", "--collect-only", "-q"],
        cwd=os.path.join(dest, "webclient"), capture_output=True, text=True, timeout=60)
    assert collect.returncode == 0, (
        f"exported test_webclient.py failed to COLLECT (not just run) on a clean clone:\n"
        f"stdout: {collect.stdout}\nstderr: {collect.stderr}")

    # "N/... tests collected" (or "no tests ran" if deps are missing here, which
    # would itself be a false pass) - assert a real nonzero count was found.
    match = re.search(r"(\d+) tests? collected", collect.stdout)
    assert match and int(match.group(1)) > 0, (
        f"expected a nonzero collected-test count, got:\n{collect.stdout}")

    # This is a full second execution of the exported suite (browser launches
    # and all), not a collect -- it WILL grow with the suite. Measured at
    # 123.24s for 95 passed/7 skipped (S4b, quiet box, load 0.63) against
    # the previous 120s ceiling -- already over it, which is why S4a's
    # ~112-test suite passed here and S4b's ~117-test one didn't: the box
    # was never the variable, the suite's own growth was. 600s gives real
    # headroom rather than another few-second buffer that the next slice's
    # tests eat again. Do not "fix" a future timeout here by deselecting
    # perf tests from this run -- the point of this gate is that the
    # exported tree passes exactly as a stranger would run it.
    run = subprocess.run(
        ["python3", "-m", "pytest", "test_webclient.py", "-q"],
        cwd=os.path.join(dest, "webclient"), capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, (
        f"exported test_webclient.py collected but did not pass cleanly on a clean clone "
        f"(skips are fine, failures are not):\nstdout: {run.stdout}\nstderr: {run.stderr}")


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


# ── check-hooks.py gate (Terminal skin rebuild) ─────────────────────────
# check-theme.py only verifies scoping; it says nothing about whether a
# selector's id/class still exists post-port. check-hooks.py closes that
# gap — these three prove it does, on the actual page and the actual themes.

THEMES_DIR = os.path.join(HERE, "themes")
CHECK_HOOKS = os.path.join(THEMES_DIR, "check-hooks.py")
INDEX_HTML = os.path.join(HERE, "index.html")


# The absent local overlay (themes/local/, avatar/local/ — untracked-by-design,
# normal on every clone but this developer's own) makes Chromium emit its own
# unsuppressable native "Failed to load resource: 404" console line for these
# two urls specifically — no application code can silence a devtools network
# log, and the app itself already handles both absences silently and
# correctly (see the local-overlay tests elsewhere in this file). Filtering
# is by URL (from the console message's .location, which Chromium's generic
# 404 text never carries), not by text alone — a genuinely missing
# stylesheet/font must still fail whatever asserts against this. B2's
# bootDeck() adds a third normal-404 case: /ha/states is absent by design
# for every clone without HA_URL/HA_TOKEN set (see ha_deck.py), and the same
# native-log ceiling applies — no JS can silence Chromium's own report of a
# fetch() response status.
KNOWN_OPTIONAL_LOCAL_OVERLAY_404_URLS = ("avatar/local/registry.mjs", "themes/local/themes.json", "/ha/states")


def _filter_known_optional_404s(console_records):
    out = []
    for text, location in console_records:
        url = (location or {}).get("url", "")
        if "Failed to load resource" in text and any(
                url.endswith(u) for u in KNOWN_OPTIONAL_LOCAL_OVERLAY_404_URLS):
            continue
        out.append(text)
    return out


def test_hacker_theme_applies_and_repaints_with_no_console_errors(browser, static_server):
    """Selecting the Terminal (hacker) theme sets data-theme on <html>,
    injects themes/hacker.css, and leaves the page with zero console errors —
    and it must have actually re-skinned, not merely loaded (see the
    computed-style assertion below)."""
    server = StubControlServer()
    server.start()
    console_records = []
    ctx, page, errors, console_errors = _new_page(
        browser, static_server, server, console_records=console_records)

    default_device_bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('.device')).backgroundImage")

    page.evaluate("() => setTheme('hacker')")
    page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'hacker'")
    # data-theme flipping is not enough — the injected <link> still needs its
    # stylesheet to actually fetch/parse before computed values reflect it.
    # Under load this raced and intermittently read hacker.css hrefs, into
    # a page still painted with the previous theme's rules (observed twice:
    # once as a reported flake, once as a false screenshot of `instrument`
    # captioned `chainsawman`) — see THEME-SPEC.md and this file's history.
    page.wait_for_function(
        "() => Array.from(document.styleSheets).some(s => (s.href || '').includes('themes/hacker.css')"
        " && s.cssRules && s.cssRules.length > 0)",
        timeout=10000)
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('link[rel=\"stylesheet\"]'))"
                           ".map(l => l.getAttribute('href'))")
    assert "themes/hacker.css" in hrefs, f"hacker.css was not injected, got: {hrefs}"

    themed_accent = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
    assert themed_accent.lower() == "#33ff66"

    # .device's chassis background is component-fidelity work, not a token
    # swap (the old hacker.css never touched it — see the pre-rewrite proof
    # below) — this is what proves the skin actually re-manufactured the
    # faceplate rather than just recoloring CSS variables.
    themed_device_bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('.device')).backgroundImage")
    assert themed_device_bg != default_device_bg, \
        "theme selection loaded but .device's chassis background did not actually change"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    app_errors = _filter_known_optional_404s(console_records)
    assert not app_errors, f"console errors after selecting hacker theme: {app_errors}"


# chainsawman.css previously carried real, pre-existing dead selectors
# (.sessionRow / .sessionKey, plus the same "~ can never match" combinator
# bug hacker.css had — #ptt.recording ~ #meterChrome / ~ #waveform aren't
# siblings either). The T3 Devil Hunter rebuild fixed both — this set is now
# empty, kept (rather than deleted) as the documented home for a future
# theme file that's known to fail check-hooks.py for reasons out of scope
# for its own slice.
KNOWN_DEAD_HOOK_THEMES = set()


def test_check_hooks_passes_on_every_tracked_theme_css():
    """Regression guard for the gate itself: every theme CSS file tracked in
    this repo must pass check-hooks.py against the real index.html, so a
    future skin can never reintroduce a dead selector (a class renamed out
    from under it, or a `~`/`+` combinator that can no longer match) without
    the test suite catching it."""
    theme_files = sorted(
        f for f in os.listdir(THEMES_DIR)
        if f.endswith(".css") and os.path.isfile(os.path.join(THEMES_DIR, f))
    )
    assert theme_files, "no tracked theme CSS files found — fixture path is wrong"
    for css_name in theme_files:
        css_path = os.path.join(THEMES_DIR, css_name)
        result = subprocess.run(
            ["python3", CHECK_HOOKS, css_path, INDEX_HTML],
            capture_output=True, text=True)
        if css_name in KNOWN_DEAD_HOOK_THEMES:
            assert result.returncode == 1, (
                f"{css_name} was expected to still have known dead hooks (returncode 1) "
                f"but check-hooks.py now reports it clean — update KNOWN_DEAD_HOOK_THEMES:\n"
                f"{result.stdout}{result.stderr}")
            continue
        assert result.returncode == 0, (
            f"check-hooks.py failed on {css_name}:\n{result.stdout}{result.stderr}")


def test_check_hooks_catches_the_known_dead_hacker_selectors(tmp_path):
    """Proves the gate itself actually works, not just that today's hacker.css
    happens to be clean: feed it the pre-rewrite hacker.css (git history,
    commit a028973) and confirm it fails and names the id/class removed by
    the DOM port, plus the sibling combinator that can never match."""
    if not _release_tooling_available(os.path.dirname(HERE)):
        pytest.skip("release tooling is not part of the public export "
                    "(scripts/ and git history are private-tree-only)")
    old_hacker = subprocess.run(
        ["git", "show", "a028973:webclient/themes/hacker.css"],
        cwd=HERE, capture_output=True, text=True, check=True).stdout
    old_path = tmp_path / "hacker.css"
    old_path.write_text(old_hacker)

    result = subprocess.run(
        ["python3", CHECK_HOOKS, str(old_path), INDEX_HTML],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "sessionKey" in result.stdout
    assert "sessionRow" in result.stdout
    assert "'#ptt.recording' and '#waveform' are not siblings" in result.stdout



# ── WCAG contrast gate ───────────────────────────────────────────────────
# check-hooks.py proves a selector still matches something; check-theme.py
# proves it's scoped. Neither can see that the result is unreadable — a
# selector matching a real element with a real background says nothing about
# whether the text on it is legible. This closes that third gap.
#
# The background is sampled from REAL RENDERED PIXELS (a Playwright
# screenshot decoded with Pillow), not assumed from a CSS custom-property
# name or walked from ancestor `backgroundColor`. Both of those alternatives
# were tried and both lie: `.stat-strip` and `.device` both paint with
# `background-image` gradients, so every ancestor's `backgroundColor` reads
# as transparent, and a hand-picked "expected" token (`--metal-mid`, guessed
# for `.stat .lcd-label` on the assumption it sat on bare plastic) scored a
# confident 7.66:1 for text that was actually invisible — `.stat-strip` paints
# its own hardcoded near-black panel. A gate that reports a passing ratio for
# unreadable text is worse than no gate; pixel sampling can't make that
# mistake because it measures what a human eye actually receives, immune to
# gradients, images, blend modes, and ancestor opacity.

def _linearize(channel_0_255):
    c = channel_0_255 / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb):
    r, g, b = rgb
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def _wcag_ratio(rgb_a, rgb_b):
    la, lb = _relative_luminance(rgb_a), _relative_luminance(rgb_b)
    la, lb = max(la, lb), min(la, lb)
    return (la + 0.05) / (lb + 0.05)


def _parse_css_color(css_color):
    """'rgb(r, g, b)' / 'rgba(r, g, b, a)' -> (r, g, b, a) floats."""
    nums = [float(n) for n in re.findall(r"[\d.]+", css_color)]
    if len(nums) == 3:
        nums.append(1.0)
    return tuple(nums)


def _dominant_background_pixel(img, box, glyph_rgb, inset=2, bucket=16):
    """The true painted background behind `box` (a Playwright bounding box,
    viewport pixels), read from a real screenshot rather than assumed —
    crop the element's own rendered pixels (inset a couple px on every side,
    so a neighbouring element's edge doesn't bleed into the sample), bucket
    them into coarse RGB cubes, and return the most common exact shade
    inside the heaviest bucket. Direction-agnostic on purpose: guessing
    "sample N px above/left" breaks the moment a theme's layout differs
    even slightly.

    Two earlier, both-broken approaches, and why bucketing beats both:

    1. Tolerance-exclude anything near the known glyph color, then take the
       plain majority. Backfired exactly when text and background are
       painted close in RGB space (hacker.css's `.rotary-current`
       dark-ink-on-dark-chip defect, caught 2026-08-16): excluding
       near-glyph colors threw out the TRUE majority background pixels
       along with the glyph pixels, leaving a handful of anti-aliased
       edge-bleed outliers as the fallback "background" — a reading that
       silently depended on whatever else was painting nearby, and flipped
       a real ~1:1 pairing to a false-passing ~6.6:1 depending on
       incidental page state (any CSS animation running elsewhere was
       enough to tip it).
    2. Plain majority-by-exact-pixel with no exclusion at all (tried while
       fixing #1, also wrong): breaks the moment the background is a
       gradient rather than a flat fill, which most of this faceplate's
       "lit readout" chips are — a gradient paints a continuum of shades
       that never exactly repeat, so even though background pixels vastly
       outnumber a bold glyph's flat anti-aliased fill core in TOTAL area,
       no single background shade individually beats the glyph's one
       repeated exact color in a pixel-exact count (measured: 24 identical
       glyph-color pixels vs. the gradient's shades landing 5-7 times each).

    Bucketing sidesteps both: it sums the gradient's many close shades back
    into one cluster (fixing #2) using pure mutual-proximity clustering with
    no reference to glyph_rgb at all (fixing #1, since a near-glyph
    background cluster's total weight still outnumbers the thinner glyph
    strokes on its own merits, no exclusion needed). `glyph_rgb` is kept in
    the signature for callers/future use but no longer needed to compute
    this."""
    left = max(0, int(box["x"]) + inset)
    top = max(0, int(box["y"]) + inset)
    right = min(img.width, int(box["x"] + box["width"]) - inset)
    bottom = min(img.height, int(box["y"] + box["height"]) - inset)
    if right <= left or bottom <= top:  # box too small to inset — use it uncropped
        left, top = max(0, int(box["x"])), max(0, int(box["y"]))
        right = min(img.width, int(box["x"] + box["width"]))
        bottom = min(img.height, int(box["y"] + box["height"]))
    crop = img.crop((left, top, right, bottom))
    colors = crop.getcolors(maxcolors=crop.width * crop.height + 16) or []
    if not colors:
        return (0, 0, 0)
    buckets = {}
    for count, rgb in colors:
        key = tuple(c // bucket for c in rgb)
        entry = buckets.setdefault(key, {"total": 0, "shades": []})
        entry["total"] += count
        entry["shades"].append((count, rgb))
    winner = max(buckets.values(), key=lambda e: e["total"])
    return max(winner["shades"])[1]  # most common exact shade inside the heaviest bucket


def _measure_pair(page, selector):
    """(ratio, foreground rgb, background rgb) for one selector, against a
    freshly captured screenshot so scroll position matches bounding_box()."""
    # Several of these classes repeat inside #firstrunOverlay (hidden, and
    # earlier in DOM order than the main content) — ":visible" plus .first
    # picks the one instance a user can actually see, regardless of DOM order.
    locator = page.locator(f"{selector}:visible").first
    locator.scroll_into_view_if_needed()
    page.wait_for_timeout(30)
    # Read color/opacity off THIS locator's own element, not a fresh
    # `document.querySelector(selector)` -- a bare re-query ignores the
    # ":visible" filter above and grabs whichever DOM match comes first,
    # which for `.baseplate .nom` is the hidden firstrun overlay's dimmer
    # `<a>` (opacity 0.75) rather than the footer text actually on screen.
    # That mismatch composited the wrong opacity against the right (real,
    # screenshot-sampled) background and manufactured a false failure.
    style = locator.evaluate(
        "el => { const cs = getComputedStyle(el); "
        "return { color: cs.color, opacity: cs.opacity }; }")
    box = locator.bounding_box()
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    r, g, b, a = _parse_css_color(style["color"])
    bg_rgb = _dominant_background_pixel(img, box, (r, g, b))
    # `opacity` is a separate compositing step from the `color` value's own
    # alpha channel -- a rule that dims text via `opacity: 0.42` (rather
    # than an rgba() alpha) renders exactly as faded against the real
    # background, but `getComputedStyle(...).color` reports the UNDIMMED
    # rgb() unconditionally. Folding it into the same fg/bg blend as `a`
    # is the correct model: both dim the glyph toward whatever's behind it.
    a *= float(style["opacity"])
    fg_composited = tuple(a * fg + (1 - a) * bgc for fg, bgc in zip((r, g, b), bg_rgb))
    return _wcag_ratio(fg_composited, bg_rgb), (r, g, b), bg_rgb


# (selector, size bucket, min ratio, what/where). Every selector is
# disambiguated to the specific instance the bug report was about — several
# of these classes repeat inside #firstrunOverlay and #settingsPanel too, and
# a bare querySelector() silently grabs whichever comes first in DOM order
# (usually one of those, not the visible one). No assumed background here —
# see the module comment above for why that's the whole point.
CONTRAST_PAIRS = [
    ("#pttCaption", "normal", 4.5, "PTT caption"),
    (".zone-input .hint", "normal", 4.5, "'Hold to talk...' hint under the TALK key"),
    ("#sessionRail .selector-label", "normal", 4.5, "PERSONA/VOICE selector label"),
    ("#sessionRail .rotary-current", "normal", 4.5, "persona rotary readout ('DEFAULT')"),
    (".baseplate .nom", "normal", 4.5, "footer nomenclature"),
    ("#armSwitch .nom", "normal", 4.5, "ARM/OFF toggle label"),
    (".stat .lcd-label", "normal", 4.5, "session stat-strip label (BRAIN/VOICE/...)"),
    (".legendPlate .nom", "normal", 4.5, "command legend plate"),
]

# `instrument`'s cream-on-chassis leak (as low as 1.77:1 on the footer) was
# fixed directly in the base stylesheet (T2b, 2026-08-15) — see
# themes/THEME-SPEC.md's "light on dark, ink on metal, never light-on-light"
# note. Kept as a named, currently-empty set (rather than deleted) so a
# future theme that's known to fail for reasons out of scope for its own
# slice — the way `instrument` itself was, and the way a custom theme's own
# CSS defect might be — has somewhere to go without disabling the assertion
# for everyone; still measured and printed every run either way.
CONTRAST_KNOWN_FAILING_THEMES = set()


def _wait_for_theme_applied(page, theme_id):
    """`setTheme()` flips `data-theme` synchronously, but for a CUSTOM theme
    it only APPENDS a `<link rel=stylesheet>` (index.html's `setTheme`) --
    fetching and parsing that link is async, and a snapshot/measurement
    taken before it resolves reads the OUTGOING theme's pixels while
    believing they're the new theme's. A fixed `wait_for_timeout` guessed at
    "long enough" and was wrong under load (measured: a real run lost this
    race with hacker.css, producing a 0/9-hooks-moved divergence failure and
    -- worse -- would have been a silent false PASS in the contrast gate,
    which only checks ratios, not which theme's pixels it actually read).

    Waits for `data-theme` to flip, then for the theme's own stylesheet to
    be genuinely PARSED (`cssRules` populated, not merely requested/pending)
    before returning. A BUILTIN theme (only `instrument`, currently) never
    appends a `<link>` at all, so the stylesheet wait is skipped for those --
    checked from `customThemes` live on the page, not assumed from the id.
    A theme already in `loadedThemeLinks` (re-selected, not its first load)
    already has a parsed stylesheet by the time `data-theme` flips, so this
    resolves immediately rather than re-waiting on a fetch that isn't
    happening. Finishes with a double `requestAnimationFrame` so the first
    paint under the new CSS has actually happened before the caller reads
    layout.

    Real timeout (5s) with a message naming the theme, not a bare
    `wait_for_function` timeout -- a genuinely missing/broken stylesheet
    must fail loudly, not silently sleep past it."""
    page.wait_for_function(
        f"() => document.documentElement.getAttribute('data-theme') === {theme_id!r}", timeout=5000)
    try:
        page.wait_for_function(
            """(id) => {
                const isCustom = typeof customThemes !== 'undefined'
                    && customThemes.some((t) => t.id === id);
                if (!isCustom) return true;  // builtin theme -- setTheme never appends a <link> for it
                const suffix = `themes/${id}.css`;
                const suffixLocal = `themes/local/${id}.css`;
                for (const ss of document.styleSheets) {
                    if (!ss.href) continue;
                    if (ss.href.endsWith(suffix) || ss.href.endsWith(suffixLocal)) {
                        try { return ss.cssRules && ss.cssRules.length > 0; } catch (e) { return false; }
                    }
                }
                return false;
            }""",
            arg=theme_id, timeout=5000)
    except sync_api.TimeoutError as e:
        raise AssertionError(
            f"theme {theme_id!r}'s stylesheet never applied within 5000ms -- no <link> matching "
            f"themes/{theme_id}.css or themes/local/{theme_id}.css with non-empty cssRules") from e
    page.evaluate(
        "() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def _select_and_measure(page, theme_id, pairs):
    page.evaluate(f"() => setTheme({theme_id!r})")
    _wait_for_theme_applied(page, theme_id)
    results = []
    for selector, bucket, min_ratio, desc in pairs:
        ratio, fg_rgb, bg_rgb = _measure_pair(page, selector)
        results.append((selector, bucket, min_ratio, ratio, fg_rgb, bg_rgb, desc))
    return results


def test_hacker_theme_faceplate_text_meets_wcag_contrast(client):
    """Terminal's beige plastic faceplate is only legible if the printed
    nomenclature is dark ink on it, never light/phosphor-green text — and a
    dark panel (.stat-strip, an .lcd-window) needs the opposite. This gate
    doesn't enforce a color, only legibility, measured from real pixels.
    Also measures `instrument` for comparison and prints both, so a
    regression review always has the real numbers, not just a pass/fail bit."""
    page, server = client

    for theme_id in ("instrument", "hacker"):
        results = _select_and_measure(page, theme_id, CONTRAST_PAIRS)
        print(f"\n-- {theme_id} --")
        for selector, bucket, min_ratio, ratio, fg_rgb, bg_rgb, desc in results:
            print(f"  {ratio:5.2f}:1 (need >={min_ratio}, {bucket})  fg={fg_rgb} bg={bg_rgb}  "
                  f"{selector}  — {desc}")
        if theme_id in CONTRAST_KNOWN_FAILING_THEMES:
            continue
        for selector, bucket, min_ratio, ratio, fg_rgb, bg_rgb, desc in results:
            assert ratio >= min_ratio, (
                f"{theme_id}/{selector} ({desc}) is {ratio:.2f}:1 (fg={fg_rgb} bg={bg_rgb}), "
                f"below the {min_ratio}:1 WCAG floor for {bucket} text")



# ── Settings drawer off-canvas regression gate ───────────────────────────
# A theme overriding #settingsPanel's `position` (base: `fixed`, parked
# off-canvas via `transform: translateX(...)` when closed) strands the
# drawer on-screen permanently — this exact defect shipped in
# chainsawman.css twice (once pre-port, once in this rebuild's first pass).
# Loops over every theme actually registered on the page (BUILTIN_THEMES +
# the tracked customThemes — real local overlay swapped aside for a
# deterministic fresh-clone list) so a future skin is covered automatically,
# the same shape as check-hooks' every-tracked-theme regression test above.
def _rect_intersects_viewport(rect, viewport):
    return not (rect["right"] <= 0 or rect["left"] >= viewport["w"]
                or rect["bottom"] <= 0 or rect["top"] >= viewport["h"])


def test_settings_drawer_stays_off_canvas_when_closed_across_every_theme(browser, static_server, local_theme_dir):
    """CLOSED: #settingsPanel's rect must not intersect the viewport, and
    #settingsOverlay's opacity must be 0. OPEN (`.open` added directly,
    bypassing the gear button): the opposite of both. Checked for every
    theme id the page itself reports registered."""
    assert not os.path.isdir(local_theme_dir)  # fixture backed up any real content; nothing recreated it

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    theme_ids = page.evaluate(
        "() => BUILTIN_THEMES.map(t => t.id).concat(customThemes.map(t => t.id))")
    assert theme_ids, "no themes found on the page - fixture/selector is wrong"

    viewport = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
    failures = []

    for theme_id in theme_ids:
        page.evaluate(f"() => setTheme({theme_id!r})")
        page.wait_for_function(f"() => document.documentElement.getAttribute('data-theme') === {theme_id!r}")
        page.wait_for_timeout(60)

        closed_rect = page.evaluate(
            "() => document.getElementById('settingsPanel').getBoundingClientRect().toJSON()")
        if _rect_intersects_viewport(closed_rect, viewport):
            failures.append(f"{theme_id}: #settingsPanel intersects the viewport while CLOSED: {closed_rect}")
        overlay_closed = page.evaluate(
            "() => getComputedStyle(document.getElementById('settingsOverlay')).opacity")
        if overlay_closed != "0":
            failures.append(f"{theme_id}: #settingsOverlay opacity is {overlay_closed} while CLOSED (want 0)")

        page.evaluate(
            "() => { document.getElementById('settingsPanel').classList.add('open');"
            " document.getElementById('settingsOverlay').classList.add('open'); }")
        page.wait_for_timeout(400)  # base sheet's 0.2s transform/opacity transition

        open_rect = page.evaluate(
            "() => document.getElementById('settingsPanel').getBoundingClientRect().toJSON()")
        if not _rect_intersects_viewport(open_rect, viewport):
            failures.append(f"{theme_id}: #settingsPanel does NOT intersect the viewport while OPEN: {open_rect}")
        overlay_open = page.evaluate(
            "() => getComputedStyle(document.getElementById('settingsOverlay')).opacity")
        if overlay_open != "1":
            failures.append(f"{theme_id}: #settingsOverlay opacity is {overlay_open} while OPEN (want 1)")

        page.evaluate(
            "() => { document.getElementById('settingsPanel').classList.remove('open');"
            " document.getElementById('settingsOverlay').classList.remove('open'); }")
        page.wait_for_timeout(250)

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    assert not failures, "settings drawer off-canvas/overlay regression:\n" + "\n".join(failures)


# ── Idle life: a connected, idle faceplate is never perfectly still ─────
# Before the L0 base life pass, document.getAnimations() was empty on a
# CONNECTED, idle page — the only animations in the sheet were transient
# (shimmer/pulse/connecting-only). This gate pins the resting vocabulary:
# named, running, and silenced under prefers-reduced-motion.
INSTRUMENT_IDLE_ANIMATIONS = {
    "instrumentDotBreathe", "instrumentLampBreathe",
    "instrumentGlassSweep", "instrumentNeedleDrift",
}


def test_instrument_faceplate_has_continuous_idle_life(client):
    page, server = client
    page.wait_for_function(
        "() => document.documentElement.getAttribute('data-va-state') === 'connected'")
    page.wait_for_timeout(200)

    running_names = page.evaluate(
        "() => document.getAnimations().filter(a => a.playState === 'running')"
        ".map(a => a.animationName)")
    assert running_names, "no animations running on a connected, idle page — the faceplate is a still image"
    assert INSTRUMENT_IDLE_ANIMATIONS.issubset(set(running_names)), (
        f"expected {INSTRUMENT_IDLE_ANIMATIONS} among the running animations, got {running_names}")

    page.emulate_media(reduced_motion="reduce")
    page.wait_for_timeout(150)
    still_running = page.evaluate(
        "() => document.getAnimations().filter(a => a.playState === 'running' "
        "&& a.animationName).length")
    assert still_running == 0, (
        f"{still_running} continuous animation(s) still running under prefers-reduced-motion: reduce")
    page.emulate_media(reduced_motion="no-preference")


# ── Dead space: .zone-input must not leave a void under its own content ──
# Before this pass, the 3-column grid stretched every zone to the tallest
# (the output rail), leaving 351px of nothing below the TALK key on a
# 1280-wide desktop viewport. 60px is generous breathing room for a machined
# faceplate's own bottom padding + the baseplate seam below it, while still
# catching a regression back to a half-empty column.
ZONE_INPUT_DEAD_SPACE_THRESHOLD_PX = 60


def test_zone_input_has_no_dead_space_below_its_content(client):
    page, server = client
    page.wait_for_function(
        "() => document.documentElement.getAttribute('data-va-state') === 'connected'")
    page.wait_for_timeout(200)

    metrics = page.evaluate("""() => {
        const zone = document.querySelector('.zone-input');
        const zr = zone.getBoundingClientRect();
        let maxBottom = zr.top;
        zone.querySelectorAll('*').forEach(el => {
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') return;
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return;
            if (r.bottom > maxBottom) maxBottom = r.bottom;
        });
        return { zoneBottom: zr.bottom, contentBottom: maxBottom };
    }""")
    tail = metrics["zoneBottom"] - metrics["contentBottom"]
    assert tail < ZONE_INPUT_DEAD_SPACE_THRESHOLD_PX, (
        f".zone-input has {tail:.0f}px of empty space below its last visible content "
        f"(threshold {ZONE_INPUT_DEAD_SPACE_THRESHOLD_PX}px)")


# ── Theme divergence gate ─────────────────────────────────────────────────
# The owner's bug report was literal: "it looks exactly the same, there is
# no distinct feel between the 3 themes. it's just color changes." A theme
# that only redefines the 23-token contract will always pass check-theme.py
# and check-hooks.py (both are selector-hygiene gates, blind to layout) and
# can still be a recolor. This gate makes a recolor mechanically unpassable
# by asserting real structural facts: the instrument's centrepiece (the
# analog needle gauge) must be replaced, not just retextured, and
# #contentGrid's own layout recipe and a majority of its fixed hooks' screen
# positions must differ between the two themes at desktop width.
DIVERGENCE_HOOKS = [
    "#armSwitch", "#ptt", "#transcript", "#assistantText",
    "#personaDisplay", "#voiceDisplay", ".stat-strip",
    "#sessionPersonaWindow", ".zone-meter",
]
DIVERGENCE_MOVE_THRESHOLD_PX = 80
DIVERGENCE_MIN_MOVED_HOOKS = 5


def _theme_layout_snapshot(page, theme_id):
    page.evaluate(f"() => setTheme({theme_id!r})")
    _wait_for_theme_applied(page, theme_id)
    return page.evaluate("""(hooks) => {
        const cg = document.getElementById('contentGrid');
        const cs = getComputedStyle(cg);
        const rectOf = sel => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return { x: r.left, y: r.top };
        };
        const meterHousing = document.querySelector('.meter-housing');
        const meterChrome = document.getElementById('meterChrome');
        return {
            gridTemplateAreas: cs.gridTemplateAreas,
            meterHousingDisplay: meterHousing ? getComputedStyle(meterHousing).display : null,
            meterChromeDisplay: meterChrome ? getComputedStyle(meterChrome).display : null,
            hookRects: hooks.map(rectOf),
        };
    }""", DIVERGENCE_HOOKS)


# A theme id -> set of clause names ("centrepiece", "meterchrome", "areas",
# "moved") it is exempted from, each entry requiring an inline comment
# explaining why that specific theme legitimately cannot satisfy the clause.
# Empty by default — a theme that needs an entry here is the exception, not
# the norm, and weakening a threshold for everyone to accommodate one theme
# is exactly what this dict exists to avoid.
DIVERGENCE_KNOWN_EXCEPTIONS = {}


def test_every_custom_theme_is_structurally_divergent_from_instrument(browser, static_server):
    """Every registered non-base theme must be a genuinely different
    interface from the instrument faceplate, not the same panel in a
    different palette — see the module comment above for why this needs to
    be a mechanical gate, not a design review. Runs over BUILTIN_THEMES +
    customThemes as reported live by the page (the same list the drawer
    itself renders), minus the base `instrument` theme, so a future skin is
    covered automatically with no test changes required. Desktop width only
    (1280px, >=1024px breakpoint where relayouts live) — the mobile stack is
    intentionally untouched by every theme's own convention."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    theme_ids = page.evaluate(
        "() => BUILTIN_THEMES.map(t => t.id).concat(customThemes.map(t => t.id))")
    assert theme_ids, "no themes found on the page - fixture/selector is wrong"
    assert "instrument" in theme_ids, "base theme id changed - update this gate"
    custom_ids = [t for t in theme_ids if t != "instrument"]
    assert custom_ids, "no custom themes registered - nothing for this gate to check"

    instrument = _theme_layout_snapshot(page, "instrument")
    assert instrument["meterHousingDisplay"] != "none", \
        f"instrument's .meter-housing should be visible, got {instrument['meterHousingDisplay']!r}"
    assert instrument["meterChromeDisplay"] == "none", \
        f"instrument doesn't use #meterChrome, got {instrument['meterChromeDisplay']!r}"

    failures = []
    for theme_id in custom_ids:
        exceptions = DIVERGENCE_KNOWN_EXCEPTIONS.get(theme_id, set())
        snap = _theme_layout_snapshot(page, theme_id)

        # (a) the analog needle gauge is the instrument's centrepiece —
        # hidden outright, not merely recolored.
        if "centrepiece" not in exceptions and snap["meterHousingDisplay"] != "none":
            failures.append(
                f"{theme_id}: must hide .meter-housing (replace the centrepiece), "
                f"got {snap['meterHousingDisplay']!r}")

        # (b) #meterChrome is the shared inert mount every theme gets for
        # free, display:none by default — the theme must be the one to claim it.
        if "meterchrome" not in exceptions and snap["meterChromeDisplay"] in (None, "none"):
            failures.append(
                f"{theme_id}: must build its centrepiece in #meterChrome, "
                f"got {snap['meterChromeDisplay']!r}")

        # (c) #contentGrid's own computed layout recipe must differ, not just its skin
        if "areas" not in exceptions and snap["gridTemplateAreas"] == instrument["gridTemplateAreas"]:
            failures.append(
                f"{theme_id}: #contentGrid's grid-template-areas is identical to instrument "
                f"({instrument['gridTemplateAreas']!r}) — recolored, not relaid out")

        # (d) a real relayout moves things — count fixed hooks whose
        # top-left corner shifted by more than DIVERGENCE_MOVE_THRESHOLD_PX.
        if "moved" not in exceptions:
            moved = 0
            detail = []
            for hook, r1, r2 in zip(DIVERGENCE_HOOKS, instrument["hookRects"], snap["hookRects"]):
                if r1 is None or r2 is None:
                    detail.append(f"{hook}: missing in one theme (r1={r1}, r2={r2})")
                    continue
                dist = ((r1["x"] - r2["x"]) ** 2 + (r1["y"] - r2["y"]) ** 2) ** 0.5
                detail.append(f"{hook}: moved {dist:.0f}px  ({r1['x']:.0f},{r1['y']:.0f}) -> ({r2['x']:.0f},{r2['y']:.0f})")
                if dist > DIVERGENCE_MOVE_THRESHOLD_PX:
                    moved += 1
            if moved < DIVERGENCE_MIN_MOVED_HOOKS:
                failures.append(
                    f"{theme_id}: only {moved}/{len(DIVERGENCE_HOOKS)} hooks moved more than "
                    f"{DIVERGENCE_MOVE_THRESHOLD_PX}px vs instrument — reads as a recolor, "
                    f"not a different interface:\n" + "\n".join(detail))

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    assert not failures, "\n\n".join(failures)


def test_hacker_scope_needle_still_sees_the_level_bus_typed(browser, static_server):
    """Guards the `@property --level` registration specifically: it MUST
    declare `inherits: true`, or a shipped skin's own centrepiece consumer
    (which reads --level from a descendant of documentElement, not from an
    element it's set on directly) goes silently dead. Picks Terminal's own
    `.meterNeedle` scope trace -- `transform: ... scaleY(calc(1 + var(--level,
    0) * 9))` (themes/hacker.css) -- as the one real consumer under test
    (retargeted from chainsawman's own box-shadow glow, same invariant,
    after chainsawman's retirement)."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    page.evaluate("() => setTheme('hacker')")
    _wait_for_theme_applied(page, "hacker")

    def needle_transform(level):
        page.evaluate("""(level) => {
            document.documentElement.style.setProperty('--level', String(level));
        }""", level)
        # .meterNeedle transitions `transform` over 0.1s (themes/hacker.css)
        # -- unlike chainsawman's old box-shadow consumer (no transition,
        # read synchronously), a read taken in the same tick as the
        # property write can still reflect the PRE-write animated value;
        # wait past the transition duration before reading.
        page.wait_for_timeout(200)
        return page.evaluate(
            "() => getComputedStyle(document.querySelector('.meterNeedle')).transform")

    low = needle_transform(0)
    high = needle_transform(1)

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    assert low != high, (
        f".meterNeedle's transform did not respond to --level (low={low!r}, high={high!r}) — "
        f"the typed @property registration broke inheritance into the shipped skin"
    )


# ── S10: desktop chassis fits the viewport, content bands scroll ────────

# (theme, height) pairs measured (not estimated) to fit within the cap at
# width 1280 -- the anti-cheat check below requires the cap to actually
# engage for these, so a fix that always releases it can't pass by
# reachability alone. hacker isn't included: its own floor wasn't measured.
# 1271 is the owner's real window.innerHeight -- above both 990 and 1010,
# so it's a regression guard here, not a new RED case: the old fixed 1000px
# threshold already covered it too.
#
# ("chainsawman", 990) and ("chainsawman", 1010) were both in this set
# before V4's tape/deck floors (index.html's `body > .device` grid-
# template-rows: `minmax(190px, 1fr)` / `minmax(150px, 1fr)`) existed --
# re-measured (not assumed) after adding them, both now genuinely release
# the cap instead of fitting: that theme's own ~975px "tach" centrepiece
# plus tape+deck's new combined 340px floor no longer fit in the ~800px
# available at those two heights. Removed rather than left to fail --
# self-consistency (page genuinely scrolls, #ptt still reachable) still
# holds for both, verified by this same test's other assertions, which
# don't skip when a case leaves this set. The tradeoff was deliberate: an
# empty deck/tape region that collapses under any pressure at all isn't a
# reserved region (owner ruling) -- worth losing the cap at two edge-case
# heights for a skin whose own centrepiece was already this tall.
# chainsawman itself (theme retired, owner ruling) is gone from the
# `theme` parametrize below entirely, so its own ("chainsawman", 1271)
# entry that used to live here is gone too -- the note above stays as the
# historical reason `KNOWN_TO_FIT` is height-based rather than a flat
# per-theme floor, which still holds for whatever themes remain.
KNOWN_TO_FIT = {
    ("instrument", 990), ("instrument", 1010), ("instrument", 1271),
}


@pytest.mark.parametrize("width,height", [
    (1280, 900), (1280, 990), (1280, 1010), (1280, 1271), (1920, 1080), (3440, 1440),
])
@pytest.mark.parametrize("theme", ["instrument", "hacker"])
def test_desktop_chassis_fits_the_viewport_with_zero_page_scroll(browser, static_server, theme, width, height):
    """`.device` is height-capped above the mobile breakpoint, but no
    longer by a fixed viewport-height constant (R9's 1000px left the
    tallest theme, chainsawman, only ~9px of headroom and gave instrument
    and chainsawman floors ~90px apart -- no single number threads both).
    The cap is now opt-in via `html[data-s10="cap"]`, set at runtime by
    `s10UpdateFit()` (index.html) only after it hit-tests `#ptt` against
    `#contentGrid`'s own visible band in the capped state. So this test no
    longer branches on a height constant either: in EITHER mode #ptt must
    be reachable, and whichever mode the page actually landed in must be
    internally consistent (capped -> zero page overflow; released ->
    `.device` falls back to its pre-S10 rules and the page genuinely
    scrolls, proving the fallback isn't just "happened to still look
    fine"). Separately, for the (theme, height) combinations already known
    to fit (see KNOWN_TO_FIT below), the cap must actually be engaged and
    scroll zero -- a fix that always releases the cap would otherwise
    satisfy the reachability check alone without ever exercising the cap
    path. 1271 in the height matrix is the owner's own real
    window.innerHeight -- above both 990 and 1010, so it's a regression
    guard here (the old fixed 1000px threshold already covered it too),
    not a new case this gate was written to catch. The local-only
    encumbered skin gets the same gate separately in test_local_themes.py,
    not here — this file ships in the public export and its banned-string
    gate bans that id.

    Deliberately does NOT set 'va-avatar':'off' the way most of this
    file's other tests do -- "on" is the real default (avatarWanted()
    treats anything but the literal string "off" as wanted), and the
    avatar module's own canvas is exactly what painted over the PTT key
    in the build this gate exists to catch (a hit-test run against the
    real default-on state found 6 of 12 theme x viewport cases clipped or
    covered; the same run with the avatar disabled found none of them,
    because disabling it never mounts the canvas that was the problem).
    A gate that can't see the failure it was written for isn't a gate."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": width, "height": height})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(1100)  # let the avatar module's own lazy import/mount settle

    if theme != "instrument":
        page.evaluate(f"() => setTheme('{theme}')")
        page.wait_for_function(
            f"() => document.documentElement.getAttribute('data-theme') === '{theme}'", timeout=10000)
        # data-theme flipping alone is not enough -- the injected <link>
        # still needs its stylesheet to actually fetch/parse before layout
        # reflects it (see test_hacker_theme_applies_and_repaints_with_no_console_errors,
        # which documents the exact race this guards against).
        page.wait_for_function(
            f"() => Array.from(document.styleSheets).some(s => (s.href || '').includes('themes/{theme}.css')"
            " && s.cssRules && s.cssRules.length > 0)", timeout=10000)
    page.wait_for_timeout(1300)  # theme-driven avatar model swap, same settle budget

    # Positive control: a theme measurement that never verifies data-theme
    # actually flipped is worthless -- this exact mistake produced four
    # identical fake rows during S10 recon (see themes/THEME-SPEC.md history).
    got_theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert got_theme == theme, f"setTheme('{theme}') did not take -- data-theme is {got_theme!r}"

    metrics = page.evaluate("""() => {
        // 'body > .device' (not '.device') -- that also matches the HIDDEN
        // #firstrunOverlay .device, earlier in DOM order (see
        // test_local_themes.py's note on the same trap).
        const d = document.querySelector('body > .device');
        const cs = getComputedStyle(d);
        const r = d.getBoundingClientRect();

        // #ptt "fully visible" -- a HIT-TEST, not a geometry comparison.
        // getBoundingClientRect() reports an element's true layout
        // position regardless of what's actually painted on top of it or
        // clipping it -- an earlier version of this gate compared #ptt's
        // rect against .device's rect and it read OK in every one of 12
        // theme x viewport cases, including builds a screenshot proved
        // had no visible TALK button at all: .device is tall enough to
        // contain #ptt even when #contentGrid has clipped it out from
        // under an ancestor's overflow, or the avatar module's own canvas
        // is painted on top of it. elementFromPoint() at #ptt's own centre
        // returns whatever the user's cursor would actually land on --
        // the only thing that answers "can they click TALK".
        //
        // SINGULAR, deliberately, and NOT the same call as s10PttFits()'s
        // own hit-test in index.html (elementsFromPoint(), plural). They
        // answer different questions: s10PttFits() asks "should the CAP
        // engage" -- something merely painted on top (an open settings
        // panel's overlay, say) is irrelevant to that. This assertion
        // asks "can the user actually click TALK RIGHT NOW" -- something
        // painted on top means they can't, full stop, and that must fail.
        // Unifying the two would make this pass on exactly the avatar-
        // canvas-covers-#ptt build this gate was written to catch.
        const pttEl = document.getElementById('ptt');
        const pttRect = pttEl.getBoundingClientRect();
        const cx = pttRect.left + pttRect.width / 2, cy = pttRect.top + pttRect.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        const pttVisible = !!(hit && (hit === pttEl || pttEl.contains(hit) || hit.contains(pttEl)));

        // Diagnostic only, not a gate condition -- if the hit-test fails,
        // this says WHICH ancestor's clip is responsible (or 'none' if the
        // culprit is an overlapping sibling instead, e.g. the avatar
        // canvas), so a failure message doesn't just say "not clickable"
        // with no lead on why.
        let clippedBy = 'none';
        let node = pttEl.parentElement;
        while (node) {
            const ncs = getComputedStyle(node);
            if (ncs.overflowY === 'auto' || ncs.overflowY === 'scroll' || ncs.overflowY === 'hidden') {
                const nr = node.getBoundingClientRect();
                if (!(pttRect.top >= nr.top - 0.5 && pttRect.bottom <= nr.bottom + 0.5)) {
                    clippedBy = node.id || node.tagName;
                    break;
                }
            }
            node = node.parentElement;
        }

        return {
            scrollHeight: document.documentElement.scrollHeight,
            clientHeight: document.documentElement.clientHeight,
            deviceVisible: cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0,
            deviceWidth: r.width,
            // Positive control: confirms which branch the runtime
            // measured-fit gate actually landed in. Was `cs.display ===
            // 'flex'` -- valid while `.device` only ever became a flex
            // column when capped, but V4 (see index.html's "V4: chassis
            // widens" comment) made `.device` a CSS grid at 1024px+ in
            // BOTH the capped and released states, so display no longer
            // distinguishes them at any width this test exercises (all
            // >=1280). The attribute `s10UpdateFit()` actually sets is the
            // direct, display-independent signal.
            deviceCapped: document.documentElement.getAttribute('data-s10') === 'cap',
            pttVisible,
            pttHitWas: hit ? (hit.id || hit.tagName + '.' + String(hit.className).slice(0, 24)) : 'null',
            clippedBy,
        };
    }""")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"

    assert metrics["deviceVisible"], f"[{theme} @ {width}x{height}] .device is not visible"
    # V4 (see index.html's "V4: chassis widens" comment) deliberately
    # reverses the earlier "chassis stays 1180" ruling this assertion used
    # to encode -- it's now `clamp(1180px, 92vw, 1760px)`, a range, not a
    # constant. Recomputed per viewport width rather than re-asserting the
    # old literal 1180 so this still catches an implementation that shrinks
    # or hides the chassis (the original intent), without also failing the
    # new, intended widening this same slice ships.
    expected_width = max(1180, min(1760, round(0.92 * width)))
    assert abs(metrics["deviceWidth"] - expected_width) <= 2, (
        f"[{theme} @ {width}x{height}] .device width is {metrics['deviceWidth']} px, want "
        f"clamp(1180, 92vw, 1760) = {expected_width}px at this viewport width "
        f"(a fix must not shrink or hide the chassis to make it fit)"
    )
    assert metrics["pttVisible"], (
        f"[{theme} @ {width}x{height}] #ptt (the push-to-talk key) is not clickable at rest "
        f"-- scrolled out of view or painted over (hit-test landed on {metrics['pttHitWas']!r}, "
        f"clip suspect: {metrics['clippedBy']!r})"
    )
    # Self-consistency: whichever mode the measured-fit gate actually
    # landed in, the layout must match it -- capped means zero page
    # overflow, released means the page genuinely scrolls (proving the
    # fallback engaged rather than "happened to still look fine").
    if metrics["deviceCapped"]:
        assert metrics["scrollHeight"] <= metrics["clientHeight"], (
            f"[{theme} @ {width}x{height}] page-level vertical overflow: "
            f"scrollHeight={metrics['scrollHeight']} > clientHeight={metrics['clientHeight']}"
        )
    else:
        assert metrics["scrollHeight"] > metrics["clientHeight"], (
            f"[{theme} @ {width}x{height}] .device released the cap (fallback layout) but the "
            f"page doesn't actually scroll -- every shipped theme's natural content is taller "
            f"than this viewport, so scrollHeight={metrics['scrollHeight']} <= "
            f"clientHeight={metrics['clientHeight']} means the fallback isn't really engaging"
        )

    # Anti-cheat: an implementation that always releases the cap would
    # satisfy every assertion above (reachable #ptt, "released" mode
    # self-consistent with a scrolling page). These two (theme, height)
    # combinations are known -- measured, not estimated -- to actually
    # fit, so the cap must be the one that engages here.
    if (theme, height) in KNOWN_TO_FIT:
        assert metrics["deviceCapped"], (
            f"[{theme} @ {width}x{height}] content is known to fit but the measured-fit gate "
            f"released the cap instead of engaging it"
        )


# ── V4: the chassis widens with the viewport, avatar owns a right column ──

# 485x206 (was 575x245 -- HEAD's own size, held as the regression floor
# through most of this slice). Lowered deliberately, not a concession: the
# owner's own stated complaint about this composition was "the hero is too
# big", and the original 575x245 guard was forcing #contentGrid's session
# rail to truncate LIVE state ("neutral"/"connecting…" collapsing to
# fragments like "con…") to protect a gauge he called oversized -- a guard
# that makes a glanced-at live value that much harder to read isn't
# protecting the thing that matters.
#
# 498x212 (briefly 485x206, then briefly, wrongly, 540x230 -- see below) is
# the size #contentGrid's own rebalanced columns (index.html's "V4: chassis
# widens" comment, near #contentGrid) actually produce now, not a new
# independent floor. It is NOT simply "as low as the owner is willing to
# go" -- it is the actual ceiling once #avatarMount's own 300px floor at
# 1440 is taken into account correctly: an earlier pass in this same
# investigation pushed the meter's raw floor up to where #meterFace
# measured 540x230 with #avatarPane (not #avatarMount) still reading
# "300px", but #avatarPane's own padding costs ~24px, so #avatarMount --
# the dimension actually gated -- had really dropped to 276px, invisible
# because nothing was asserting on avatarPane. Worse: that same pass also
# raised METER's column floor without raising `body > .device`'s own
# outer column-1 floor to match, which reproduced the exact clamp()-
# overflow bug this file already fixed once (index.html's #contentGrid
# comment): #contentGrid asked its container for more than it was given
# and overflowed sideways, painting "TOOLS" under the avatar pane --
# invisible to a per-element width check for the same reason avatarMount
# was: every individual measurement still read "correct" in isolation.
# 498x212 is what's left once BOTH are solved for correctly at once (see
# index.html's #contentGrid comment for the arithmetic) -- still a floor,
# not a target: this asserts the meter doesn't shrink BELOW this measured
# value at 1440x1271, same shape as before, just a lower number for a
# stated, now doubly-verified reason.
BASELINE_METER_W = 498
BASELINE_METER_H = 212


@pytest.mark.parametrize("width", [1280, 1440, 1600, 1920, 2200])
def test_v4_chassis_widens_avatar_column_meter_undiminished(browser, static_server, width):
    """V4 reverses the earlier "chassis stays 1180" ruling (see index.html's
    "V4: chassis widens" comment): the chassis is a `clamp(1180px, 92vw,
    1760px)` range now, centred at every width, with the avatar promoted
    into a real right-hand column instead of a strip at the bottom of the
    page. This is the negative-control-bearing test for that reversal --
    every assertion here was proven to fail against the pre-V4 build
    (chassis pinned to 1180, avatar 664x250 wedged under #contentGrid) --
    see the handoff report for the stash/unstash RED/GREEN transcript.

    Height is fixed at 1271 (the owner's real window.innerHeight, same
    constant test_desktop_chassis_fits_the_viewport_with_zero_page_scroll
    uses) -- this test is about WIDTH behaviour, not an independent height
    sweep of its own.

    The #meterFace assertion below is scoped to `instrument` (the default
    theme this test loads, asserted explicitly rather than assumed) on
    purpose, and checks the element EXISTS before checking its size: a rig
    legitimately replaces the centrepiece entirely (that's the point of
    the rig tier -- "a different interface, not a different palette"), so
    a bare `#meterFace >= 575x245` gate would either fail every rig or, if
    written carelessly, pass vacuously on `null.width` when the element is
    simply gone. 575x245 is a regression guard for the one theme that
    still ships this exact centrepiece, not a contract every theme must
    honour -- the real contract (440px minimum for a theme that KEEPS a
    centrepiece) is documented in prose in themes/THEME-SPEC.md's "Chassis
    regions" section, not enforced here.
    """
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": width, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(1100)  # avatar module's own lazy import/mount, same settle budget S10 uses

    # Positive control for the theme-scoped assertion below -- this test
    # never calls setTheme(), so it's relying on the page's own default
    # being instrument; confirm that rather than assume it, the same
    # discipline every theme-identity assertion in this file already uses.
    got_theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert got_theme == "instrument", (
        f"this test assumes the default theme is instrument, got {got_theme!r} -- "
        f"the #meterFace assertion below is meaningless against any other theme"
    )

    metrics = page.evaluate("""() => {
        // 'body > .device', not '.device' -- the hidden #firstrunOverlay
        // dialog earlier in DOM order carries the same class (see S10's
        // own test above, and test_local_themes.py, for the same trap).
        const d = document.querySelector('body > .device');
        const dr = d.getBoundingClientRect();
        const grid = document.getElementById('contentGrid').getBoundingClientRect();
        const avatar = document.getElementById('avatarPane').getBoundingClientRect();
        // Existence checked explicitly, not via a bare .getBoundingClientRect()
        // that would throw on null -- a rig replacing #meterFace entirely
        // must fail this test with a clear message, not a bare TypeError.
        const meterEl = document.getElementById('meterFace');
        const meter = meterEl ? meterEl.getBoundingClientRect() : null;

        const pttEl = document.getElementById('ptt');
        const pttRect = pttEl.getBoundingClientRect();
        const cx = pttRect.left + pttRect.width / 2, cy = pttRect.top + pttRect.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        const pttVisible = !!(hit && (hit === pttEl || pttEl.contains(hit) || hit.contains(pttEl)));

        return {
            deviceRect: { left: dr.left, right: dr.right, width: dr.width, height: dr.height },
            centred: Math.abs(dr.left - (innerWidth - dr.right)),
            scrollX: document.documentElement.scrollWidth - innerWidth,
            scrollY: document.documentElement.scrollHeight - innerHeight,
            meterFound: !!meterEl,
            meter: meter ? { w: meter.width, h: meter.height } : null,
            avatarLeft: avatar.left, avatarWidth: avatar.width, avatarHeight: avatar.height,
            gridRight: grid.right,
            pttVisible,
        };
    }""")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"

    expected_width = max(1180, min(1760, round(0.92 * width)))
    assert abs(metrics["deviceRect"]["width"] - expected_width) <= 2, (
        f"[{width}] chassis width is {metrics['deviceRect']['width']}px, want "
        f"clamp(1180, 92vw, 1760) = {expected_width}px"
    )
    assert metrics["centred"] <= 2, (
        f"[{width}] chassis is not centred: left/right margins differ by "
        f"{metrics['centred']}px (rect={metrics['deviceRect']})"
    )
    assert metrics["scrollX"] <= 0, f"[{width}] horizontal page scroll: {metrics['scrollX']}px"
    assert metrics["scrollY"] <= 0, f"[{width}] vertical page scroll: {metrics['scrollY']}px"
    assert metrics["pttVisible"], f"[{width}] #ptt is not clickable at rest"

    # instrument-only regression guard -- the meter must be at least as big
    # as it was before this slice at every tested width. Existence checked
    # first and loudly: a theme that removed #meterFace entirely (a rig
    # replacing the centrepiece, in particular) must fail THIS assertion
    # with a clear message, not pass vacuously because `null.width` never
    # got compared, and not the default theme having been confirmed
    # instrument above -- if it's ever missing there, that's a real
    # instrument regression, not an expected rig substitution.
    assert metrics["meterFound"], f"[{width}] #meterFace not found in the instrument theme"
    assert metrics["meter"]["w"] >= BASELINE_METER_W - 1, (
        f"[{width}] #meterFace width {metrics['meter']['w']}px < baseline {BASELINE_METER_W}px"
    )
    assert metrics["meter"]["h"] >= BASELINE_METER_H - 1, (
        f"[{width}] #meterFace height {metrics['meter']['h']}px < baseline {BASELINE_METER_H}px"
    )

    # Avatar owns a real right-hand column: to the right of #contentGrid,
    # with actual width/height (not a collapsed 0x0 box).
    assert metrics["avatarLeft"] >= metrics["gridRight"] - 1, (
        f"[{width}] avatar (left={metrics['avatarLeft']}) is not right of "
        f"#contentGrid (right={metrics['gridRight']})"
    )
    assert metrics["avatarWidth"] > 0 and metrics["avatarHeight"] > 0, (
        f"[{width}] avatar column collapsed to {metrics['avatarWidth']}x{metrics['avatarHeight']}"
    )


def test_rig_can_reclaim_chassis_regions_by_area_name(browser, static_server):
    """The chassis grid contract (themes/THEME-SPEC.md's "Chassis regions")
    exists to let a rig relayout `body > .device` by redefining
    `grid-template-areas` alone -- if any region were placed by
    `grid-column`/`grid-row` line numbers instead of `grid-area: <name>`,
    that placement could NOT be overridden this way (a numbered placement
    doesn't defer to an area map), and the contract would be decorative.
    Simulates a rig with a small injected stylesheet -- no `.mjs`, no
    fixture theme needed, since the mechanism under test is pure CSS.

    Swaps `console` and `monitor` left/right (the two required-adjacent
    regions) and asserts they actually moved, `#ptt` is still reachable,
    and the page still doesn't scroll -- a rig that reclaims the chassis
    must not silently break S10's own contract in the process.

    This test was proven to fail against a hardcoded implementation, not
    just written to pass against this one: temporarily restoring the
    pre-named-areas placement (`#avatarPane { grid-column: 2; grid-row: 2
    / span 2; }`, this file's own git history) makes this test fail with
    the avatar still on the right after the override -- see the handoff
    report for the transcript."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(1100)

    before = page.evaluate("""() => ({
        consoleLeft: document.getElementById('contentGrid').getBoundingClientRect().left,
        monitorLeft: document.getElementById('avatarPane').getBoundingClientRect().left,
    })""")

    # A rig's own stylesheet, scoped the same way a real one would be
    # (`[data-theme="…"]`) -- swaps console/monitor into a 2-column
    # layout with monitor on the LEFT instead of the right, the simplest
    # unambiguous "did this actually move" probe. Deliberately reuses the
    # SAME column-count contract (2 columns) the base map uses; a rig may
    # use a different one, but this test only needs one that proves the
    # mechanism.
    page.add_style_tag(content="""
        html[data-theme="instrument"] body > .device {
            grid-template-areas:
                "nameplate nameplate"
                "monitor   console"
                "monitor   tape"
                "controls  deck"
                "task      task"
                "links     links"
                "log       log"
                "baseplate baseplate" !important;
        }
    """)
    page.wait_for_timeout(200)

    after = page.evaluate("""() => {
        const ptt = document.getElementById('ptt');
        const pr = ptt.getBoundingClientRect();
        const hit = document.elementFromPoint(pr.left + pr.width / 2, pr.top + pr.height / 2);
        return {
            consoleLeft: document.getElementById('contentGrid').getBoundingClientRect().left,
            monitorLeft: document.getElementById('avatarPane').getBoundingClientRect().left,
            pttVisible: !!(hit && (hit === ptt || ptt.contains(hit) || hit.contains(ptt))),
            scrollX: document.documentElement.scrollWidth - innerWidth,
            scrollY: document.documentElement.scrollHeight - innerHeight,
        };
    }""")

    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()

    assert after["monitorLeft"] < before["monitorLeft"], (
        f"monitor region did not move left after the rig's grid-template-areas override "
        f"(before={before['monitorLeft']}, after={after['monitorLeft']}) -- placement is not "
        f"tracking grid-area by name"
    )
    assert after["consoleLeft"] > before["consoleLeft"], (
        f"console region did not move right after the rig's grid-template-areas override "
        f"(before={before['consoleLeft']}, after={after['consoleLeft']})"
    )
    assert after["monitorLeft"] < after["consoleLeft"], (
        "monitor is not actually left of console after the swap"
    )
    assert after["pttVisible"], "#ptt is not clickable after a rig reclaimed the chassis regions"
    assert after["scrollX"] <= 0, f"horizontal page scroll after rig relayout: {after['scrollX']}px"
    assert after["scrollY"] <= 0, f"vertical page scroll after rig relayout: {after['scrollY']}px"


# ── Command legend plate ─────────────────────────────────────────────────

def test_legend_plate_does_not_overlap_tape_or_controls(browser, static_server):
    """The command legend plate lives in the new `legend` chassis region --
    row 4's left cell, below `#historyRail` (TAPE) and left of
    `.lowerBayControls`'s diagnostics buttons (row 4 was "controls controls"
    full width, now "legend controls" -- see THEME-SPEC.md's "Chassis
    regions"). A rect-only comparison can pass on a build where something
    is actually painted on top (this file's own note on the S10/deck
    overlap gates), so this also hit-tests the plate's own centre with
    elementFromPoint()."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(300)

    result = page.evaluate("""() => {
        const legend = document.getElementById('legendPlate');
        const tape = document.getElementById('historyRail');
        const controls = document.querySelector('.lowerBayControls');
        const lr = legend.getBoundingClientRect();
        const tr = tape.getBoundingClientRect();
        const cr = controls.getBoundingClientRect();
        const intersects = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
        const cx = lr.left + lr.width / 2, cy = lr.top + lr.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        return {
            visible: lr.width > 0 && lr.height > 0,
            overlapsTape: intersects(lr, tr),
            overlapsControls: intersects(lr, cr),
            hitIsLegend: !!(hit && (hit === legend || legend.contains(hit))),
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()
    assert result["visible"], "#legendPlate has no visible box at 1440x1271"
    assert not result["overlapsTape"], "legend plate overlaps #historyRail (TAPE)"
    assert not result["overlapsControls"], "legend plate overlaps .lowerBayControls"
    assert result["hitIsLegend"], "legend plate's own centre is painted over by something else"


@pytest.mark.parametrize("width,height", [(1280, 1271), (1440, 1271), (1920, 1080)])
def test_legend_plate_does_not_break_ptt_reachability_or_page_scroll(browser, static_server, width, height):
    """The row-4 split that gave the legend plate its own cell ("legend
    controls" replacing "controls controls") must not reopen S10's own
    contract: #ptt reachable via elementFromPoint, page scroll zero."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": width, "height": height})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(300)

    result = page.evaluate("""() => {
        const ptt = document.getElementById('ptt');
        const pr = ptt.getBoundingClientRect();
        const hit = document.elementFromPoint(pr.left + pr.width / 2, pr.top + pr.height / 2);
        return {
            pttVisible: !!(hit && (hit === ptt || ptt.contains(hit) || hit.contains(ptt))),
            scrollX: document.documentElement.scrollWidth - innerWidth,
            scrollY: document.documentElement.scrollHeight - innerHeight,
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()
    assert result["pttVisible"], f"#ptt not reachable at {width}x{height}"
    assert result["scrollX"] <= 0, f"horizontal page scroll at {width}x{height}: {result['scrollX']}px"
    assert result["scrollY"] <= 0, f"vertical page scroll at {width}x{height}: {result['scrollY']}px"


def test_legend_plate_does_not_overlap_links_card_when_populated(browser, static_server):
    """The idle-state overlap gate above only ever saw the 2-line plate.
    Fully populated (15 armed tools -> 8 tool rows) at the row-4 floor's
    150px was measured well short of the plate's actual height -- it
    painted over #linksCard instead of shrinking, because the floor never
    had to account for content-dependent height. Reproduces both through
    the real message paths (config_state for tools, search_links for the
    links list) and checks: a rect intersection against EVERY OTHER
    visible `body > .device` grid child (not just #linksCard -- the links
    card also moved beside tape in this same change, so a narrower check
    would miss a collision with whatever else is now its neighbor), a
    real hit-test at the first link's own top-left corner, and a real
    hit-test at the legend plate's OWN last tool cell (proves the plate's
    tail isn't under anything either) -- rects alone have lied before
    when something else was actually painted on top (see the sibling
    overlap gate's own note)."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(300)

    tools = ["get_weather", "web_search", "knowledge_lookup", "decision_lookup", "set_mood",
              "delegate_to_hermes", "hermes_status", "send_to_hermes", "current_time",
              "fleet_status", "gpu_status", "home_assistant", "infra_health", "look",
              "news_headlines"]
    server.send(dict(CONFIG_STATE, tools=tools))
    page.wait_for_function(
        "() => document.getElementById('legendTools').children.length === 15")

    server.send({"type": "search_links", "links": [
        {"url": "https://example.com/a", "title": "Example A", "host": "example.com"},
        {"url": "https://example.com/b", "title": "Example B", "host": "example.com"},
        {"url": "https://example.com/c", "title": "Example C", "host": "example.com"},
    ]})
    page.wait_for_function(
        "() => document.getElementById('linksCard').hidden === false")

    result = page.evaluate("""() => {
        const legend = document.getElementById('legendPlate');
        const firstLink = document.querySelector('#linksList a');
        const toolCells = document.querySelectorAll('#legendTools .nom');
        const lastToolCell = toolCells[toolCells.length - 1];
        const lr = legend.getBoundingClientRect();
        const intersects = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

        // Every OTHER visible `body > .device` grid child -- not just
        // #linksCard -- so a collision with whatever the links move makes
        // its new neighbor is caught too.
        const siblingSelectors = ['#linksCard', '#questCard', '#log', '.lowerBayControls',
            '#historyRail', 'footer.baseplate'];
        const offenders = [];
        siblingSelectors.forEach((sel) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const r = el.getBoundingClientRect();
            const visible = r.width > 0 && r.height > 0 && getComputedStyle(el).display !== 'none'
                && !el.hidden;
            if (!visible) return;
            if (intersects(lr, r)) {
                const overlapX = Math.max(0, Math.min(lr.right, r.right) - Math.max(lr.left, r.left));
                const overlapY = Math.max(0, Math.min(lr.bottom, r.bottom) - Math.max(lr.top, r.top));
                offenders.push({sel, rect: {left: r.left, top: r.top, right: r.right, bottom: r.bottom},
                    overlapPx: {x: overlapX, y: overlapY}});
            }
        });

        const alr = firstLink.getBoundingClientRect();
        const linkHit = document.elementFromPoint(alr.left + 5, alr.top + 5);
        const tcr = lastToolCell.getBoundingClientRect();
        const toolHit = document.elementFromPoint(tcr.left + tcr.width / 2, tcr.top + tcr.height / 2);

        return {
            legendRect: {left: lr.left, top: lr.top, right: lr.right, bottom: lr.bottom, height: lr.height},
            offenders,
            hitIsFirstLink: !!(linkHit && (linkHit === firstLink || firstLink.contains(linkHit))),
            hitIsLastToolCell: !!(toolHit && (toolHit === lastToolCell || lastToolCell.contains(toolHit))),
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()
    assert not result["offenders"], (
        f"legend plate overlaps {result['offenders']} legend={result['legendRect']}")
    assert result["hitIsFirstLink"], (
        f"first link not reachable via elementFromPoint at its own top-left corner "
        f"legend={result['legendRect']}")
    assert result["hitIsLastToolCell"], (
        f"legend plate's last tool cell not reachable via elementFromPoint at its own centre "
        f"legend={result['legendRect']}")


def test_links_card_scrolls_internally_instead_of_overflowing_its_row(browser, static_server):
    """#linksCard moved from a free-growing full-width row into the tape
    row's height-capped cell (see the `grid-template-areas` change), and
    nothing gave it #historyRail's own internal-scroll behaviour that cap
    demands. The real producer (`patches/hermes_cockpit.py:493`) caps at
    FIVE links, not three -- reproduced with five realistic long titles
    through the real `search_links` message path, not hand-injected
    markup (a raw DOM fill exaggerates height and doesn't match what the
    client actually renders)."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(300)

    server.send({"type": "search_links", "links": [
        {"url": "https://example.com/a-fairly-long-article-slug-one",
         "title": "A Fairly Long Example Article Title About Something Specific",
         "host": "example.com"},
        {"url": "https://en.wikipedia.org/wiki/Example_Topic",
         "title": "Example Topic - Wikipedia, the Free Encyclopedia",
         "host": "en.wikipedia.org"},
        {"url": "https://news.example.com/2026/08/19/story-headline",
         "title": "A Fairly Long News Headline About An Ongoing Situation",
         "host": "news.example.com"},
        {"url": "https://docs.example.org/guide/getting-started",
         "title": "Getting Started With The Thing - Official Documentation",
         "host": "docs.example.org"},
        {"url": "https://blog.example.net/2026/why-this-matters",
         "title": "Why This Matters: A Fairly Long Blog Post Title",
         "host": "blog.example.net"},
    ]})
    page.wait_for_function(
        "() => document.getElementById('linksCard').hidden === false"
        " && document.querySelectorAll('#linksList a').length === 5")

    result = page.evaluate("""() => {
        const links = document.getElementById('linksCard');
        const list = document.getElementById('linksList');
        const controls = document.querySelector('.lowerBayControls');
        const tape = document.getElementById('historyRail');
        const anchors = document.querySelectorAll('#linksList a');
        const fifth = anchors[anchors.length - 1];

        // `getBoundingClientRect()` on `list`/`fifth` reports their FULL
        // layout box regardless of an ancestor's `overflow: auto` -- an
        // overflowing child's rect legitimately extends past a correctly
        // clipping container, so a naive rect-intersection check here
        // would fail even against a correct fix. What actually proves
        // "not painted where it shouldn't be" is a real hit-test: pick a
        // point that's inside the OFFENDING rect and ask the compositor
        // what's actually there.
        const cardRect = links.getBoundingClientRect();
        const tapeRect = tape.getBoundingClientRect();
        const controlsRect = controls.getBoundingClientRect();
        const listRect = list.getBoundingClientRect();

        // Point at controls' own centre -- if the overflowing list is
        // visually painting over it (unclipped), the hit would land on a
        // link/list element instead of controls itself.
        const controlsHit = document.elementFromPoint(
            controlsRect.left + controlsRect.width / 2, controlsRect.top + controlsRect.height / 2);
        const controlsHitIsControls = !!(controlsHit && (controlsHit === controls || controls.contains(controlsHit)));

        // Point just below the CARD's own bottom edge, inside the list's
        // (unclipped) layout rect -- if overflow is correctly clipping,
        // nothing from #linksList paints there.
        const belowCardPoint = {x: cardRect.left + 10, y: cardRect.bottom + 5};
        const belowCardInsideListRect = belowCardPoint.y < listRect.bottom;
        const belowCardHit = belowCardInsideListRect
            ? document.elementFromPoint(belowCardPoint.x, belowCardPoint.y) : null;
        const listPaintsBelowCard = !!(belowCardHit && (list.contains(belowCardHit) || belowCardHit === list));

        const scroller = links; // mirrors #historyRail's own idiom: the grid item itself scrolls
        const scrollHeight = scroller.scrollHeight;
        const clientHeight = scroller.clientHeight;

        // Reachability BEFORE scrolling to the bottom.
        const fr5Before = fifth.getBoundingClientRect();
        const hitBefore = document.elementFromPoint(fr5Before.left + 5, fr5Before.top + 5);
        const reachableBefore = !!(hitBefore && (hitBefore === fifth || fifth.contains(hitBefore)));

        scroller.scrollTop = scroller.scrollHeight;
        const fr5After = fifth.getBoundingClientRect();
        const hitAfter = document.elementFromPoint(fr5After.left + 5, fr5After.top + 5);
        const reachableAfter = !!(hitAfter && (hitAfter === fifth || fifth.contains(hitAfter)));

        return {
            cardRect: {top: cardRect.top, bottom: cardRect.bottom, left: cardRect.left, right: cardRect.right},
            tapeRect: {top: tapeRect.top, bottom: tapeRect.bottom},
            listRect: {top: listRect.top, bottom: listRect.bottom},
            controlsHitIsControls,
            listPaintsBelowCard,
            belowCardInsideListRect,
            scrollHeight, clientHeight,
            reachableBefore, reachableAfter,
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()

    assert result["controlsHitIsControls"], (
        f"a point at .lowerBayControls' own centre hits something else -- the overflowing "
        f"#linksList is painting over it -- card={result['cardRect']} list={result['listRect']}")
    if result["belowCardInsideListRect"]:
        assert not result["listPaintsBelowCard"], (
            f"#linksList paints past #linksCard's own bottom edge instead of being clipped -- "
            f"card={result['cardRect']} list={result['listRect']}")
    assert result["cardRect"]["bottom"] <= result["tapeRect"]["bottom"] + 1, (
        f"#linksCard's bottom ({result['cardRect']['bottom']}) exceeds #historyRail's "
        f"({result['tapeRect']['bottom']}) -- same grid row, must not paint past its track")
    assert result["scrollHeight"] > result["clientHeight"], (
        f"5 long-title links didn't exceed the card's clientHeight -- "
        f"scrollHeight={result['scrollHeight']} clientHeight={result['clientHeight']} "
        f"(gate assumption invalid, needs more/longer content)")
    assert result["reachableAfter"], (
        f"5th link still not reachable via elementFromPoint after scrolling the card to its bottom")


def test_tape_links_split_is_60_40(browser, static_server):
    """Owner ruling: on the tape row, tape gets ~60% of the row's width and
    #linksCard the remaining ~40% (was ~75/25 under the old 2-column chassis
    map, where column 1's 989px console floor also governed the tape/links
    split). Reproduces via the real message paths (config_state for the
    legend plate's 15 armed tools, search_links for 5 links) rather than
    hand-injected markup, same idiom as the sibling gates above."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(300)

    tools = ["get_weather", "web_search", "knowledge_lookup", "decision_lookup", "set_mood",
              "delegate_to_hermes", "hermes_status", "send_to_hermes", "current_time",
              "fleet_status", "gpu_status", "home_assistant", "infra_health", "look",
              "news_headlines"]
    server.send(dict(CONFIG_STATE, tools=tools))
    page.wait_for_function(
        "() => document.getElementById('legendTools').children.length === 15")

    server.send({"type": "search_links", "links": [
        {"url": "https://example.com/a-fairly-long-article-slug-one",
         "title": "A Fairly Long Example Article Title About Something Specific",
         "host": "example.com"},
        {"url": "https://en.wikipedia.org/wiki/Example_Topic",
         "title": "Example Topic - Wikipedia, the Free Encyclopedia",
         "host": "en.wikipedia.org"},
        {"url": "https://news.example.com/2026/08/19/story-headline",
         "title": "A Fairly Long News Headline About An Ongoing Situation",
         "host": "news.example.com"},
        {"url": "https://docs.example.org/guide/getting-started",
         "title": "Getting Started With The Thing - Official Documentation",
         "host": "docs.example.org"},
        {"url": "https://blog.example.net/2026/why-this-matters",
         "title": "Why This Matters: A Fairly Long Blog Post Title",
         "host": "blog.example.net"},
    ]})
    page.wait_for_function(
        "() => document.getElementById('linksCard').hidden === false"
        " && document.querySelectorAll('#linksList a').length === 5")

    result = page.evaluate("""() => {
        const tape = document.getElementById('historyRail');
        const links = document.getElementById('linksCard');
        const contentGrid = document.getElementById('contentGrid');
        const firstLink = document.querySelector('#linksList a');
        const toolCells = document.querySelectorAll('#legendTools .nom');
        const lastToolCell = toolCells[toolCells.length - 1];

        const tapeRect = tape.getBoundingClientRect();
        const linksRect = links.getBoundingClientRect();
        const gridRect = contentGrid.getBoundingClientRect();

        const alr = firstLink.getBoundingClientRect();
        const linkHit = document.elementFromPoint(alr.left + 5, alr.top + 5);
        const tcr = lastToolCell.getBoundingClientRect();
        const toolHit = document.elementFromPoint(tcr.left + tcr.width / 2, tcr.top + tcr.height / 2);

        return {
            tapeWidth: tapeRect.width,
            linksWidth: linksRect.width,
            gridWidth: gridRect.width,
            scrollWidth: document.documentElement.scrollWidth,
            clientWidth: document.documentElement.clientWidth,
            hitIsFirstLink: !!(linkHit && (linkHit === firstLink || firstLink.contains(linkHit))),
            hitIsLastToolCell: !!(toolHit && (toolHit === lastToolCell || lastToolCell.contains(toolHit))),
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()

    tape_share = result["tapeWidth"] / (result["tapeWidth"] + result["linksWidth"])
    assert 0.57 <= tape_share <= 0.63, (
        f"tape share {tape_share:.3f} not within 0.57-0.63 -- tapeWidth={result['tapeWidth']} "
        f"linksWidth={result['linksWidth']}")
    assert result["gridWidth"] >= 989, (
        f"#contentGrid width {result['gridWidth']} fell below its own 989px floor")
    assert result["scrollWidth"] <= result["clientWidth"], (
        f"page scrolls horizontally: scrollWidth={result['scrollWidth']} clientWidth={result['clientWidth']}")
    assert result["hitIsFirstLink"], (
        f"first link not reachable via elementFromPoint at its own top-left corner")
    assert result["hitIsLastToolCell"], (
        f"legend plate's last tool cell not reachable via elementFromPoint at its own centre")


def test_legend_plate_wake_phrase_tracks_the_live_selection(client):
    """The printed ARM line must name whatever wake word is actually
    selected right now, not a hardcoded guess (index.html's
    syncLegendWakePhrase reads #wakeModelSelect's own selected option).
    Sends a config_state with two models, then changes the selection the
    way the settings panel would, and asserts the plate follows both
    times, the second without any server round trip."""
    page, server = client
    server.send(dict(CONFIG_STATE, wake_word={
        "enabled": False, "state": "off", "phrase": "hey jarvis",
        "model": "hey_jarvis", "models": ["hey_jarvis", "alexa"],
    }))
    page.wait_for_function(
        "() => document.getElementById('wakeModelSelect').options.length === 2")

    before = page.evaluate("() => document.getElementById('legendWakePhrase').textContent")
    assert before == "hey jarvis", f"legend did not pick up the initial selection: {before!r}"

    # #wakeModelSelect lives inside the (closed-by-default) settings drawer --
    # dispatch the same "change" a real selection in the open panel would fire,
    # rather than fighting Playwright's visibility requirement for a real click.
    page.evaluate("""() => {
        const sel = document.getElementById('wakeModelSelect');
        sel.value = 'alexa';
        sel.dispatchEvent(new Event('change', { bubbles: true }));
    }""")
    after = page.evaluate("() => document.getElementById('legendWakePhrase').textContent")

    assert after == "alexa", f"legend did not follow the changed wake-word selection: {after!r}"
    assert after != before, "seed did not move the value"


def test_legend_arm_line_hides_when_wake_word_is_unsupported(client):
    """No `wake_word` (null) means no wake-word streamer is configured at
    all -- the ARM line must not keep claiming a phrase the client cannot
    read, the same class of defect as a false readout on an instrument.

    Reads computed style, not the `hidden` property -- multiple gates in
    this file were green today while asserting the attribute meant to
    cause an effect rather than the pixels it actually produces (see the
    deck-toggle tests' own notes on `.deckTile[hidden]`); `.nom`'s own
    `display: block` is an author rule that could just as easily have
    outranked `[hidden]`'s UA `display: none` the same way."""
    page, server = client
    server.send({"type": "config_state", "active_brain": "local", "model": "stub-model",
                 "brains": CONFIG_STATE["brains"], "voices": [], "custom_voices": [],
                 "tools_armed": 0, "wake_word": None})
    page.wait_for_function(
        "() => getComputedStyle(document.getElementById('legendArmLine')).display === 'none'")
    assert page.eval_on_selector(
        "#legendArmLine", "el => el.offsetParent === null"
    ), "ARM line is still laid out on the plate after wake_word went unsupported"


def test_legend_plate_lists_armed_tools_from_config_state(client):
    """The plate prints the full armed-tool list at rest — driven by the real
    `config_state` message path, not a direct render call, and by the
    server's names, never a hand-mapped label (a drop-in tool must render
    with no client change)."""
    page, server = client
    server.send(dict(CONFIG_STATE, tools=["get_weather", "home_assistant"]))
    page.wait_for_function(
        "() => document.getElementById('legendTools').children.length === 2")

    assert page.locator("#legendTools").is_visible()
    text = page.evaluate("() => document.getElementById('legendTools').textContent")
    assert "GET WEATHER" in text.upper()
    assert "HOME ASSISTANT" in text.upper()


def test_legend_plate_tools_hidden_without_tools_field(client):
    """An older server (or one that never sent `tools`) must not leave a
    stale or hardcoded list showing — the block stays hidden."""
    page, server = client
    server.send(dict(CONFIG_STATE))  # CONFIG_STATE carries no `tools` key
    page.wait_for_timeout(200)

    assert not page.locator("#legendTools").is_visible()


# ── R9 addendum: nothing inside the chassis silently clips ──────────────

# Deliberate internal scroll/clamp containers -- everything else inside
# body > .device is expected to render at its full natural height. Each
# entry here is verified against its own CSS rule, not guessed:
#   #contentGrid / #lowerBay -- the two S10 scroll bands themselves.
#   #historyRail  -- `#lowerBay #historyRail { max-height: 240px; }`
#                     (index.html), a pre-S10 internal-scroll precedent.
#   #log          -- `#log { max-height: 96px; overflow-y: auto; }`
#                     (index.html), the diagnostics escape hatch.
#   .meter-glass  -- aria-hidden decorative glass/gleam overlay on the VU
#                     gauge (index.html:1530), `overflow: hidden` by design
#                     to mask its own `::before` sweep animation inside the
#                     glass window's shape. Verified via `git stash` that
#                     this predates the whole slice: identical h=245/sh=294
#                     at HEAD, every viewport, base instrument theme only
#                     -- not a regression, not information, not this
#                     gate's job.
#   #vaDeck       -- V4's reserved deck region (index.html): row 4 of
#                     `body > .device`'s outer grid is one of the three `fr`
#                     tracks the S10 cap can genuinely squeeze under real
#                     height pressure (see the grid-template-rows comment
#                     there), and `html[data-s10="cap"] ... #vaDeck` sets
#                     `overflow: hidden` deliberately for exactly that case
#                     -- same shape as #historyRail's own pre-existing
#                     entry, just new in this slice.
CLIP_SCAN_EXEMPT = ["#contentGrid", "#lowerBay", "#historyRail", "#log", ".meter-glass", "#vaDeck"]

CLIP_SCAN_JS = """(exempt) => {
    const dev = document.querySelector('body > .device');
    const out = [];
    const walk = (el) => {
        if (el.nodeType !== 1) return;
        const isExempt = exempt.some((sel) => el.matches && el.matches(sel));
        const cs = getComputedStyle(el);
        // A scrollHeight/clientHeight mismatch on an `overflow: visible`
        // element clips NOTHING -- the content just paints outside the
        // box, unclipped, same as it always has (this is exactly why
        // decorative elements using CSS transforms or an inline SVG gauge
        // -- .meterFace, #pttDeco, the .toggle-track knob -- reported a
        // mismatch during recon with no actual visual defect: none of
        // them set a non-visible overflow). Only overflow:hidden/auto/
        // scroll/clip is capable of hiding anything.
        const clips = cs.overflowY !== 'visible' || cs.overflowX !== 'visible';
        // clientHeight === 0 excludes display:none/collapsed subtrees
        // (e.g. hidden question/link cards) -- nothing is visibly clipped
        // if nothing is visible at all.
        if (!isExempt && clips && el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 1) {
            out.push({
                tag: el.tagName, id: el.id,
                cls: String(el.className || '').slice(0, 40),
                h: Math.round(el.clientHeight), sh: el.scrollHeight,
            });
        }
        for (const c of el.children) walk(c);
    };
    walk(dev);
    return out;
}"""


@pytest.mark.parametrize("width,height", [
    (1280, 900), (1280, 990), (1280, 1010), (1280, 1271), (1920, 1080), (3440, 1440),
])
@pytest.mark.parametrize("theme", ["instrument", "hacker"])
def test_nothing_inside_the_chassis_silently_clips(browser, static_server, theme, width, height):
    """The PTT hit-test only ever proves ONE control is reachable -- it says
    nothing about anything else inside `body > .device`. This is the
    broader property: walk every descendant and flag any element whose
    scrollHeight exceeds its own clientHeight without being one of the
    verified, deliberate internal-scroll containers (CLIP_SCAN_EXEMPT).

    Written after a visual pass caught `.stat-strip` (MOOD/CONN readouts)
    silently missing on Terminal (hacker) @1920x1080/@3440x1440 -- a
    #contentGrid GRID ITEM promoted there by the theme's own
    `#sessionRail { display: contents }` relayout, compressed below its
    own content by the same "automatic minimum size goes to 0 once the
    container's overflow isn't visible" mechanism that #contentGrid /
    #lowerBay themselves needed guarding against, recurring one level
    deeper for grid items sized by an implicit `auto` row. Zero page
    overflow and a reachable PTT key both held throughout -- this class of
    bug has no symptom either of those checks can see.

    Same avatar-on rationale as the PTT gate: disabling the avatar module
    would leave this test structurally blind to any canvas-driven clipping
    the same way the PTT gate was before that was caught."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": width, "height": height})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    page.goto(f"http://127.0.0.1:{static_server}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_timeout(1100)  # let the avatar module's own lazy import/mount settle

    if theme != "instrument":
        page.evaluate(f"() => setTheme('{theme}')")
        page.wait_for_function(
            f"() => document.documentElement.getAttribute('data-theme') === '{theme}'", timeout=10000)
        page.wait_for_function(
            f"() => Array.from(document.styleSheets).some(s => (s.href || '').includes('themes/{theme}.css')"
            " && s.cssRules && s.cssRules.length > 0)", timeout=10000)
    page.wait_for_timeout(1300)  # theme-driven avatar model swap, same settle budget

    got_theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert got_theme == theme, f"setTheme('{theme}') did not take -- data-theme is {got_theme!r}"

    findings = page.evaluate(CLIP_SCAN_JS, CLIP_SCAN_EXEMPT)

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"

    assert not findings, (
        f"[{theme} @ {width}x{height}] element(s) silently clipped inside body > .device "
        f"(not in CLIP_SCAN_EXEMPT): " +
        "; ".join(f"{f['tag']}#{f['id']}.{f['cls']} h={f['h']} sh={f['sh']}" for f in findings)
    )


# ── HA control deck (B2) ──────────────────────────────────────────────
# The frozen `static_server`/`client` fixtures have no /ha routes (see
# static_server's own docstring), so the deck needs its own scriptable
# server layered on the same disposable temp root (_webclient_temp_root)
# rather than touching either fixture.

class DeckServer:
    """A ThreadingHTTPServer serving the disposable temp root plus
    scriptable /ha/states, /ha/stream, /ha/intent handlers a test drives
    directly — the browser-side equivalent of `ha_deck.py`'s real routes,
    just with test-controlled state instead of a real Home Assistant."""

    def __init__(self, root):
        self._root = root
        self.states_status = 200  # non-200 simulates "HA not configured"
        self.entities = []        # /ha/states body + /ha/stream replay snapshot
        self.intents = []         # captured POST /ha/intent bodies
        self.port = None
        self._httpd = None
        self._subscribers = []
        self._sub_lock = threading.Lock()

    def start(self):
        srv = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=srv._root, **kw)

            def log_message(self, *a):
                pass

            def _json(self, status, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/ha/states":
                    if srv.states_status != 200:
                        self._json(srv.states_status, {"error": "not found"})
                    else:
                        self._json(200, {"entities": srv.entities})
                    return
                if self.path == "/ha/stream":
                    if srv.states_status != 200:
                        self._json(404, {"error": "not found"})
                        return
                    q = queue.Queue()
                    with srv._sub_lock:
                        srv._subscribers.append(q)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        for ent in srv.entities:
                            self.wfile.write(f"event: state\ndata: {json.dumps(ent)}\n\n".encode())
                        self.wfile.flush()
                        while True:
                            try:
                                ev = q.get(timeout=1.0)
                            except queue.Empty:
                                continue
                            if ev is None:  # shutdown sentinel
                                return
                            self.wfile.write(f"event: state\ndata: {json.dumps(ev)}\n\n".encode())
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    finally:
                        with srv._sub_lock:
                            if q in srv._subscribers:
                                srv._subscribers.remove(q)
                    return
                super().do_GET()

            def do_POST(self):
                if self.path == "/ha/intent":
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw)
                    srv.intents.append(payload)
                    results = [{"entity_id": i.get("entity_id"), "accepted": True, "error": None}
                               for i in payload.get("intents", [])]
                    self._json(200, {"batch_id": "test-batch", "results": results})
                    return
                self.send_response(404)
                self.end_headers()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(None)
        if self._httpd:
            self._httpd.shutdown()

    def push(self, entity):
        """Broadcast one SSE state event to every open /ha/stream connection."""
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(entity)


def _deck_entity(entity_id, state, write_status="unknown", write_reason=None, friendly=None):
    return {
        "entity_id": entity_id,
        "domain": entity_id.split(".", 1)[0],
        "state": state,
        "attributes": {"friendly_name": friendly or entity_id},
        "last_changed": "2026-08-17T00:00:00+00:00",
        "write": {"status": write_status, "reason": write_reason},
    }


@pytest.fixture
def deck_server(_webclient_temp_root):
    srv = DeckServer(_webclient_temp_root)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def deck_client(browser, deck_server):
    """Like `client`, but pointed at `deck_server` (so /ha/* exists) and at
    a viewport wide enough to clear the deck's 1024px desktop threshold.
    Each test sets deck_server.entities/states_status *before* calling
    page.goto, since bootDeck() fetches /ha/states on load."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    yield page, server, deck_server
    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def _goto_deck(page, deck, query=""):
    page.goto(f"http://127.0.0.1:{deck.port}/index.html{query}")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)


def test_deck_unhandled_state_renders_unknown_not_off(deck_client):
    """A state string the client has never seen must default to `unknown`,
    never `off` — an unhandled state reading as OFF for a physically
    unreachable device is the exact lie this feature exists to prevent."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.study_lamp", "heating")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.study_lamp"]')
    state = page.eval_on_selector('[data-entity="light.study_lamp"]', "el => el.dataset.state")
    assert state == "unknown"
    assert state != "off"


def test_deck_unavailable_state_is_distinct(deck_client):
    page, server, deck = deck_client
    deck.entities = [_deck_entity("switch.hall_switch", "unavailable")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.hall_switch"]')
    state = page.eval_on_selector('[data-entity="switch.hall_switch"]', "el => el.dataset.state")
    assert state == "unavailable"
    assert state not in ("off", "unknown")


def test_deck_cover_open_closed_map_to_on_off(deck_client):
    """HA's own cover vocabulary (open/closed) is an unambiguous on/off
    synonym and must classify accordingly, not fall to `unknown`."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("cover.garage", "open"),
        _deck_entity("cover.blinds", "closed"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="cover.garage"]')
    open_state = page.eval_on_selector('[data-entity="cover.garage"]', "el => el.dataset.state")
    closed_state = page.eval_on_selector('[data-entity="cover.blinds"]', "el => el.dataset.state")
    assert open_state == "on"
    assert closed_state == "off"


def test_deck_lock_locked_unlocked_map_to_off_on(deck_client):
    """HA's lock vocabulary (locked/unlocked) is an unambiguous off/on
    synonym and must classify accordingly, not fall to `unknown`."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("lock.front_door", "locked"),
        _deck_entity("lock.back_door", "unlocked"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="lock.front_door"]')
    locked_state = page.eval_on_selector('[data-entity="lock.front_door"]', "el => el.dataset.state")
    unlocked_state = page.eval_on_selector('[data-entity="lock.back_door"]', "el => el.dataset.state")
    assert locked_state == "off"
    assert unlocked_state == "on"


def test_deck_climate_mode_stays_unknown_but_preserves_raw_state(deck_client):
    """Climate modes (heat/cool/idle/auto) are deliberately NOT mapped onto
    on/off — a thermostat isn't a two-state device. The tile must still
    classify as `unknown` (never on, never off) while the raw HA value is
    preserved in the tile's aria-label rather than discarded."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("climate.home", "cool")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="climate.home"]')
    state = page.eval_on_selector('[data-entity="climate.home"]', "el => el.dataset.state")
    assert state == "unknown"
    assert state not in ("on", "off")
    label = page.eval_on_selector('[data-entity="climate.home"]', "el => el.getAttribute('aria-label')")
    assert "cool" in label


def test_deck_tile_node_identity_survives_sse_updates(deck_client):
    """Persistent-stage gate: the tile DOM node must be created once and
    mutated in place, never rebuilt, across repeated SSE updates."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.kitchen", "off")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.kitchen"]')
    page.evaluate("document.querySelector('[data-entity=\"light.kitchen\"]').__deckTestMark = 'stamp1'")
    for st in ["on", "off", "on"]:
        deck.push(_deck_entity("light.kitchen", st))
        page.wait_for_function(
            f"() => document.querySelector('[data-entity=\"light.kitchen\"]')?.dataset.state === '{st}'",
            timeout=5000)
    mark = page.evaluate("document.querySelector('[data-entity=\"light.kitchen\"]').__deckTestMark")
    assert mark == "stamp1", "tile DOM node was replaced, not mutated in place"


def test_deck_absent_when_ha_not_configured(deck_client):
    page, server, deck = deck_client
    deck.states_status = 404
    _goto_deck(page, deck)
    page.wait_for_timeout(500)
    tile_count = page.eval_on_selector_all("#deckTiles > *", "els => els.length")
    assert tile_count == 0
    body_text = page.eval_on_selector("#vaDeck", "el => el.textContent")
    assert "error" not in body_text.lower()
    assert "not found" not in body_text.lower()


def test_deck_click_posts_intent_and_reflects_pending_then_confirmed(deck_client):
    page, server, deck = deck_client
    deck.entities = [_deck_entity("switch.porch", "off")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.porch"]')
    page.click('[data-entity="switch.porch"]')
    page.wait_for_timeout(300)
    assert deck.intents == [{"intents": [{"entity_id": "switch.porch", "service": "toggle"}]}]
    deck.push(_deck_entity("switch.porch", "off", write_status="pending"))
    page.wait_for_function(
        "() => document.querySelector('[data-entity=\"switch.porch\"]')?.dataset.write === 'pending'",
        timeout=5000)
    deck.push(_deck_entity("switch.porch", "on", write_status="confirmed"))
    page.wait_for_function(
        "() => document.querySelector('[data-entity=\"switch.porch\"]')?.dataset.write === 'confirmed'",
        timeout=5000)


def test_deck_decksim_forces_every_tile_and_shows_badge(deck_client):
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.a", "on"), _deck_entity("switch.b", "off")]
    _goto_deck(page, deck, "?decksim=unavailable")
    page.wait_for_selector('[data-entity="light.a"]')
    states = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.state)")
    assert states == ["unavailable", "unavailable"]
    badge = page.eval_on_selector("#deckSimBadge", "el => ({hidden: el.hidden, text: el.textContent})")
    assert badge["hidden"] is False
    assert "unavailable" in badge["text"].lower()


def test_deck_no_decksim_means_no_badge_and_no_forcing(deck_client):
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.a", "on")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.a"]')
    hidden = page.eval_on_selector("#deckSimBadge", "el => el.hidden")
    assert hidden is True
    state = page.eval_on_selector('[data-entity="light.a"]', "el => el.dataset.state")
    assert state == "on"


def test_deck_tiles_render_as_multi_column_grid(deck_client):
    """A single-column stack of tiles shows far too few of a real 29-tile
    deck in the available box. `#deckTiles` must resolve to a grid with
    more than one column at a >=1280px viewport."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity(f"light.tile{i}", "on") for i in range(6)]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.tile0"]')
    tracks = page.eval_on_selector(
        "#deckTiles",
        "el => getComputedStyle(el).gridTemplateColumns.trim().split(/\\s+/).length")
    assert tracks > 1, f"expected 2+ grid columns, got {tracks}"


def test_deck_sim_badge_shares_label_row_not_a_tile_row(deck_client):
    """The always-unmissable sim badge must live in the label row, not cost
    one of the ~5 visible tile rows -- the first tile's top position must
    be identical whether or not the badge is showing."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.a", "on")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.a"]')
    top_no_sim = page.eval_on_selector(
        '[data-entity="light.a"]', "el => el.getBoundingClientRect().top")

    _goto_deck(page, deck, "?decksim=unavailable")
    page.wait_for_selector('[data-entity="light.a"]')
    badge_hidden = page.eval_on_selector("#deckSimBadge", "el => el.hidden")
    assert badge_hidden is False
    top_with_sim = page.eval_on_selector(
        '[data-entity="light.a"]', "el => el.getBoundingClientRect().top")
    assert top_with_sim == top_no_sim, "sim badge pushed the tile grid down"


def test_deck_fills_meter_zone_void_at_owner_geometry(browser, deck_server):
    """Owner ruling: the deck nests inside `.zone-meter`'s dead space below
    the gauge instead of a chassis-level region, at his real geometry
    (1440x1271, avatar on, s10 cap). Checks the four things that ruling
    actually promises: the deck lives inside the meter zone, it has real
    height (not a sliver), its tiles still grid up, and the page still
    never scrolls -- a relayout that reclaims dead space by introducing a
    new one would be a wash, not a fix. Also reconfirms `#ptt` stays
    reachable via a single `elementFromPoint()` hit-test at its own centre
    (S10's own contract), since #vaDeck's relocation is a sibling of the
    meter housing #ptt sits near."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    deck_server.entities = [_deck_entity(f"light.tile{i}", "on") for i in range(6)]
    page.goto(f"http://127.0.0.1:{deck_server.port}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_selector('[data-entity="light.tile0"]')
    page.wait_for_timeout(1100)

    result = page.evaluate("""() => {
        const deck = document.getElementById('vaDeck');
        const ptt = document.getElementById('ptt');
        const pr = ptt.getBoundingClientRect();
        const hit = document.elementFromPoint(pr.left + pr.width / 2, pr.top + pr.height / 2);
        return {
            insideMeterZone: !!deck.closest('.zone-meter'),
            deckHeight: deck.getBoundingClientRect().height,
            tracks: getComputedStyle(document.getElementById('deckTiles'))
                .gridTemplateColumns.trim().split(/\\s+/).length,
            pttVisible: !!(hit && (hit === ptt || ptt.contains(hit) || hit.contains(ptt))),
            scrollY: document.documentElement.scrollHeight - innerHeight,
        };
    }""")

    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
    server.stop()

    assert result["insideMeterZone"], "#vaDeck is no longer inside .zone-meter"
    assert result["deckHeight"] > 250, f"deck height {result['deckHeight']} did not fill the meter void"
    assert result["tracks"] > 1, f"expected 2+ grid columns, got {result['tracks']}"
    assert result["pttVisible"], "#ptt is not clickable after the deck relocation"
    assert result["scrollY"] == 0, f"page scrolls after the deck relocation: {result['scrollY']}px"


def test_deck_link_down_does_not_flip_tiles_off(deck_client):
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.hall", "on")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.hall"]')
    # Tear the deck server all the way down (the control websocket is a
    # separate server/port, unaffected). shutdown() alone stops accepting
    # new connections but leaves the already-open /ha/stream handler thread
    # running forever on its own queue -- stop() also pushes the sentinel
    # that ends that loop, actually closing the connection so the browser's
    # EventSource sees it drop and fires onerror.
    deck.stop()
    page.wait_for_function(
        "() => document.getElementById('vaDeck').dataset.link === 'down'", timeout=10000)
    state = page.eval_on_selector('[data-entity="light.hall"]', "el => el.dataset.state")
    assert state == "on", "link loss must never flip a tile's last-known state"


def test_deck_excludes_scene_and_unlisted_domains(deck_client):
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("scene.movie_night", "2026-08-17T00:00:00+00:00"),
        _deck_entity("person.owner", "home"),
        _deck_entity("light.kept", "on"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.kept"]')
    ids = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    assert ids == ["light.kept"]


def _goto_owner_deck(browser, deck_server, entity_count=40, query=""):
    """Shared setup for the S10/patchbay gates below: the owner's real
    geometry (1440x1271, avatar off so boot is fast, s10 cap engaged) with
    a deck full enough (29 tiles, long friendly names) to reproduce the
    overflow this slice fixes -- the existing
    test_deck_fills_meter_zone_void_at_owner_geometry fixture used only 6
    tiles, which never grew `.zone-meter` past its container at all."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    deck_server.entities = [
        _deck_entity(f"light.tile{i}", "on", friendly=f"Long Friendly Name For Entity Number {i} Do Not Disturb")
        for i in range(entity_count)
    ]
    page.goto(f"http://127.0.0.1:{deck_server.port}/index.html{query}")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    # Generic `.deckTile`, not `[data-entity="light.tile0"]` -- the filter
    # test below deliberately excludes tile0 via `?deckfilter=`, so a
    # selector tied to one specific entity would hang on that call.
    page.wait_for_selector('.deckTile')
    page.wait_for_timeout(1100)
    return page, server, errors


def test_deck_does_not_burst_zone_meter_past_content_grid(browser, deck_server):
    """S10 fix, part 1: `.zone-meter` is a grid item in #contentGrid
    (align-items: stretch) but defaults to `min-height: auto`, the same
    automatic-minimum-size trap already fixed elsewhere in this file for
    #contentGrid itself -- a full 29-tile deck is taller than the meter
    void, so without an explicit override `.zone-meter` floors at that
    content height and bursts past #contentGrid's own bottom edge instead
    of clipping to it. Checked at the owner's real geometry, where the
    burst was ~248px (measured 1077 vs 829)."""
    page, server, errors = _goto_owner_deck(browser, deck_server)
    result = page.evaluate("""() => {
        const cg = document.getElementById('contentGrid').getBoundingClientRect();
        const zm = document.querySelector('.zone-meter').getBoundingClientRect();
        return {
            cgBottom: cg.bottom,
            zmBottom: zm.bottom,
            scrollY: document.documentElement.scrollHeight - innerHeight,
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert result["zmBottom"] <= result["cgBottom"] + 1, (
        f".zone-meter bottom {result['zmBottom']} exceeds #contentGrid bottom {result['cgBottom']}")
    assert result["scrollY"] == 0, f"page scrolls after the deck relocation: {result['scrollY']}px"


def test_deck_does_not_overlap_tape(browser, deck_server):
    """The defect the owner actually saw: `#historyRail` (TAPE) painting
    straight over the bottom of an overflowing deck. Asserted directly,
    not just inferred from the container-overflow gate above."""
    page, server, errors = _goto_owner_deck(browser, deck_server)
    result = page.evaluate("""() => {
        const deck = document.getElementById('vaDeck').getBoundingClientRect();
        const tape = document.getElementById('historyRail').getBoundingClientRect();
        return { deckBottom: deck.bottom, tapeTop: tape.top };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert result["tapeTop"] >= result["deckBottom"] - 1, (
        f"TAPE top {result['tapeTop']} is above deck bottom {result['deckBottom']} -- TAPE overlaps the deck")


def test_ptt_still_reachable_with_full_deck(browser, deck_server):
    """S10's whole point, reconfirmed against a full 29-tile deck: `#ptt`
    must still hit-test as itself at its own centre. Deliberately SINGULAR
    `elementFromPoint()` (not the runtime gate's plural `elementsFromPoint`)
    -- see this file's own note on `test_deck_fills_meter_zone_void_at_owner_geometry`
    for why that distinction matters."""
    page, server, errors = _goto_owner_deck(browser, deck_server)
    result = page.evaluate("""() => {
        const ptt = document.getElementById('ptt');
        const pr = ptt.getBoundingClientRect();
        const hit = document.elementFromPoint(pr.left + pr.width / 2, pr.top + pr.height / 2);
        return !!(hit && (hit === ptt || ptt.contains(hit) || hit.contains(ptt)));
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert result, "#ptt is not reachable via elementFromPoint at its own centre"


def test_deck_grid_has_at_least_four_columns_at_real_width(browser, deck_server):
    """Patchbay density target: ~72-88px jack points should fit 4+ columns
    at the deck's real ~500px inner width (six was the design target)."""
    page, server, errors = _goto_owner_deck(browser, deck_server)
    tracks = page.eval_on_selector(
        "#deckTiles",
        "el => getComputedStyle(el).gridTemplateColumns.trim().split(/\\s+/).length")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert tracks >= 4, f"expected 4+ grid columns at the deck's real width, got {tracks}"


def test_deck_filter_default_shows_all_and_active_filter_shows_hidden_count(browser, deck_server):
    """Part 3: no `?deckfilter=` means every allowlisted-domain entity
    renders and the status caption stays hidden (no silent cap by default).
    An active filter must both actually exclude the matched entities AND
    show the hidden count somewhere visible -- a filter that drops
    entities silently is the failure this slice exists to avoid."""
    page, server, errors = _goto_owner_deck(browser, deck_server, entity_count=10)
    unfiltered = page.evaluate("""() => ({
        tiles: document.querySelectorAll('.deckTile').length,
        statusHidden: document.getElementById('deckFilterStatus').hidden,
    })""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert unfiltered["tiles"] == 10, f"default (unfiltered) deck dropped entities: {unfiltered['tiles']} of 10"
    assert unfiltered["statusHidden"], "filter status caption shows with no filter active"

    page2, server2, errors2 = _goto_owner_deck(
        browser, deck_server, entity_count=10, query="?deckfilter=Number%200,Number%201,Number%202")
    filtered = page2.evaluate("""() => ({
        tiles: document.querySelectorAll('.deckTile').length,
        statusHidden: document.getElementById('deckFilterStatus').hidden,
        statusText: document.getElementById('deckFilterStatus').textContent,
    })""")
    assert not errors2, f"uncaught page errors: {errors2}"
    page2.context.close()
    server2.stop()
    assert filtered["tiles"] == 7, f"filter did not exclude the matched entities: {filtered['tiles']} tiles"
    assert not filtered["statusHidden"], "active filter did not surface a hidden-count caption"
    assert "7" in filtered["statusText"] and "10" in filtered["statusText"], (
        f"hidden-count caption doesn't show shown/total: {filtered['statusText']!r}")


def test_deck_tile_never_straddles_the_scroll_boundary(browser, deck_server):
    """Eye-check follow-up: a row that's only partially inside `#deckTiles`'
    own visible (unscrolled) box used to show its sockets with no legend
    beneath them -- the socket sits above the clip boundary, the legend
    strip below it. A jack point and its legend must be shown or hidden
    as ONE unit, never split by the container's own bottom edge.

    Deviates from the literal "legend bottom is within the scrollable
    content" wording: every legend is ALWAYS within scrollHeight with a
    non-zero computed height regardless of this bug (the DOM node and its
    text are intact either way) -- that phrasing can't distinguish
    "clipped by the visible viewport" from "fine", so this asserts the
    real geometry: nothing may start above the container's visible bottom
    edge and end below it. Needs raw/write captions (climate/lock/cover
    domains) to push a row's height enough to actually overflow into the
    next one -- the plain on/off tiles this deck's other tests use are
    short enough that 29 of them alone don't reproduce it."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    domains_cycle = ["light", "switch", "fan", "cover", "climate", "lock"]
    entities = []
    for i in range(29):
        dom = domains_cycle[i % len(domains_cycle)]
        st = {"climate": "cool", "lock": "locked", "cover": "open"}.get(dom, "on")
        entities.append(_deck_entity(
            f"{dom}.e{i}", st, friendly=f"Long Friendly Name For Entity Number {i} Do Not Disturb"))
    deck_server.entities = entities
    page.goto(f"http://127.0.0.1:{deck_server.port}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_selector('.deckTile')
    page.wait_for_timeout(1300)
    straddling = page.evaluate("""() => {
        const dt = document.getElementById('deckTiles');
        const dtBottom = dt.getBoundingClientRect().bottom;
        return Array.from(document.querySelectorAll('.deckTile')).filter(t => {
            const r = t.getBoundingClientRect();
            return r.top < dtBottom - 0.5 && r.bottom > dtBottom + 0.5;
        }).map(t => t.dataset.entity);
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert straddling == [], f"tile(s) straddle #deckTiles' visible bottom edge: {straddling}"


def test_deck_legends_stay_unique_for_shared_prefix_names(browser, deck_server):
    """Long HA friendly names that share a possessive owner prefix and a
    common device-family lead-in must not render as identical, useless
    legend stubs -- the whole point of a legend strip on a patchbay is
    telling two adjacent jacks apart."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(viewport={"width": 1440, "height": 1271})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    names = [
        "Jordan's 4th Fire TV Do not disturb",
        "Jordan's Fire TV Do not disturb",
        "Jordan's Echo Dot Announcements",
        "Jordan's Echo Auto Announcements",
        "Jordan's Echo Show Announcements",
        # No recognized abbreviation phrase in either of these -- the ONLY
        # thing that can disambiguate this pair is growing the
        # front-anchored window until it diverges (deckRelabelTiles'
        # uniqueness loop), not the marker-window shortcut the names above
        # already get for free.
        "Jordan's Garage Door Sensor Left",
        "Jordan's Garage Door Sensor Right",
    ]
    deck_server.entities = [_deck_entity(f"switch.e{i}", "off", friendly=n) for i, n in enumerate(names)]
    page.goto(f"http://127.0.0.1:{deck_server.port}/index.html")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_selector('.deckTile')
    page.wait_for_timeout(1200)
    info = page.eval_on_selector_all(
        ".deckTileName",
        "els => els.map(e => ({text: e.textContent, truncated: e.scrollWidth > e.clientWidth + 1}))")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    legends = [i["text"] for i in info]
    assert all(legends), f"an empty legend rendered: {legends}"
    assert len(set(legends)) == len(legends), f"duplicate legends rendered: {legends}"
    # `textContent` equality alone can't see CSS `text-overflow: ellipsis`
    # doing the actual damage -- two DIFFERENT full names can still render
    # IDENTICALLY once clipped to the column's width (the owner's actual
    # "ALDWIN'S ECHO..." complaint). A short-enough legend should never
    # need the ellipsis at all.
    truncated = [i["text"] for i in info if i["truncated"]]
    assert not truncated, f"legend(s) still wide enough to be CSS-ellipsis-truncated: {truncated}"


def test_deck_legend_keeps_which_automation(deck_client):
    """Two automations sharing a device-family lead-in ("Zuul Automation:")
    must not both collapse to that shared prefix alone -- the trigger name
    is the ONE thing that tells these two switches apart."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("switch.zuul_automation_coming_home", "off", friendly="Zuul Automation: Coming home"),
        _deck_entity("switch.zuul_automation_leaving_home", "off", friendly="Zuul Automation: Leaving home"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.zuul_automation_coming_home"]')
    page.wait_for_timeout(300)
    legends = page.evaluate("""() => ({
        coming: document.querySelector(
            '[data-entity="switch.zuul_automation_coming_home"] .deckTileName').textContent,
        leaving: document.querySelector(
            '[data-entity="switch.zuul_automation_leaving_home"] .deckTileName').textContent,
    })""")
    assert legends["coming"] != legends["leaving"], f"identical legends: {legends}"
    assert "COMING" in legends["coming"].upper(), f"lost 'Coming': {legends}"
    assert "LEAVING" in legends["leaving"].upper(), f"lost 'Leaving': {legends}"


def test_deck_legend_keeps_device_name_when_marker_abbreviates(deck_client):
    """Several different devices sharing one function ("Do not disturb")
    must each keep their OWN device name -- collapsing every one of them
    down to a bare "DND" (plus a manufactured `#2`/`#3` counter to break
    the resulting tie) is unique but tells you nothing about which
    physical device you're muting. A recognized class marker firing on
    the whole name must protect the rest of that name from the
    shared-prefix strip, not trigger it."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("switch.e0", "off", friendly="Jordan's Echo Dot Do not disturb"),
        _deck_entity("switch.e1", "off", friendly="Jordan's 4th Fire TV Do not disturb"),
        _deck_entity("switch.e2", "off", friendly="Jordan's Echo Auto Do not disturb"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.e0"]')
    page.wait_for_timeout(300)
    legends = page.eval_on_selector_all(".deckTileName", "els => els.map(e => e.textContent)")
    upper = [l.upper() for l in legends]
    assert "ECHO DOT" in upper[0], f"lost the device name: {legends}"
    assert "FIRE TV" in upper[1], f"lost the device name: {legends}"
    assert "ECHO AUTO" in upper[2], f"lost the device name: {legends}"
    assert not any(re.search(r"#\d+$", l) for l in legends), f"manufactured counter used: {legends}"
    assert not any("DND" in u and "DISTURB" in u for u in upper), (
        f"abbreviation left its own source word behind: {legends}")


def test_deck_legend_keeps_which_lamp(deck_client):
    """Two Hue lamps differing only by a trailing digit must keep that
    digit -- dropping it is dropping the entire point of the legend."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("light.living_room_hue_white_lamp_1", "on", friendly="Hue white lamp 1"),
        _deck_entity("light.living_room_hue_white_lamp_2", "on", friendly="Hue white lamp 2"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.living_room_hue_white_lamp_1"]')
    page.wait_for_timeout(300)
    legends = page.evaluate("""() => ({
        one: document.querySelector(
            '[data-entity="light.living_room_hue_white_lamp_1"] .deckTileName').textContent,
        two: document.querySelector(
            '[data-entity="light.living_room_hue_white_lamp_2"] .deckTileName').textContent,
    })""")
    assert legends["one"] != legends["two"], f"identical legends: {legends}"
    assert "1" in legends["one"], f"lost the '1': {legends}"
    assert "2" in legends["two"], f"lost the '2': {legends}"


def test_deck_legend_normalizes_whitespace_without_losing_words(deck_client):
    """A `friendly_name` with a doubled space and a trailing newline
    (real HA data, `switch.front_light`) must render a clean legend that
    still carries every word -- normalization must not become a second,
    accidental way to lose the distinguishing tail."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("switch.front_light", "on", friendly="Front  light\n")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.front_light"]')
    page.wait_for_timeout(300)
    legend = page.eval_on_selector('[data-entity="switch.front_light"] .deckTileName', "el => el.textContent")
    assert legend == legend.strip(), f"stray whitespace in legend: {legend!r}"
    assert "  " not in legend, f"doubled space survived normalization: {legend!r}"
    assert "front" in legend.lower() and "light" in legend.lower(), f"lost a word: {legend!r}"


def test_deck_legends_never_empty_or_equal_under_long_shared_prefix(deck_client):
    """A whole set that shares one long device-family prefix must still
    yield distinct, non-empty legends for every member -- the shared part
    is exactly what's supposed to be removed, never the differing tail."""
    page, server, deck = deck_client
    names = ["Alpha", "Bravo", "Charlie"]
    deck.entities = [
        _deck_entity(f"switch.dept_e{i}", "off", friendly=f"Building Twelve East Wing Corridor Sensor {n}")
        for i, n in enumerate(names)
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.dept_e0"]')
    page.wait_for_timeout(300)
    legends = page.eval_on_selector_all(".deckTileName", "els => els.map(e => e.textContent)")
    assert all(legends), f"an empty legend rendered: {legends}"
    assert len(set(legends)) == len(legends), f"duplicate legends rendered: {legends}"


def test_deck_unavailable_points_sort_after_reachable_ones(deck_client):
    """Owner ruling: reachable points first. `switch.aaa_unavail` sorts
    alphabetically BEFORE `switch.bbb_on` under the old plain
    (domain, entity_id) order, but a covered/dead jack must never bury a
    live one -- it belongs at the end regardless of where its entity_id
    falls."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("switch.aaa_unavail", "unavailable", friendly="AAA Unavailable"),
        _deck_entity("switch.bbb_on", "on", friendly="BBB On"),
        _deck_entity("switch.ccc_off", "off", friendly="CCC Off"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.bbb_on"]')
    order = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    assert order == ["switch.bbb_on", "switch.ccc_off", "switch.aaa_unavail"], (
        f"unavailable point did not sort last: {order}")


def test_deck_order_frozen_when_a_point_goes_unavailable_later(deck_client):
    """Owner ruling: the order is frozen at load. A point going
    `unavailable` AFTER the initial snapshot must mutate in place, not
    jump to the back of the deck under the user's finger."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("switch.aaa_on", "on", friendly="AAA On"),
        _deck_entity("switch.bbb_on", "on", friendly="BBB On"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.bbb_on"]')
    before = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    assert before == ["switch.aaa_on", "switch.bbb_on"]

    deck.push(_deck_entity("switch.aaa_on", "unavailable", friendly="AAA On"))
    page.wait_for_function(
        "() => document.querySelector('[data-entity=\"switch.aaa_on\"]')?.dataset.state === 'unavailable'",
        timeout=5000)
    after = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    assert after == before, f"tile order changed after an SSE state flip: {before} -> {after}"


# ── Deck section, Service Panel (V4 follow-up) ───────────────────────────

def test_deck_settings_section_absent_when_ha_not_configured(deck_client):
    """The Service Panel's DECK section must not render at all -- no empty
    section, no disabled controls -- when /ha/states is non-200, same
    principle as #vaDeck itself never rendering tiles for that user."""
    page, server, deck = deck_client
    deck.states_status = 404
    _goto_deck(page, deck)
    _open_settings(page)
    page.wait_for_timeout(500)
    assert page.evaluate("() => document.getElementById('deckGroup').hidden") is True


def test_deck_off_hides_deck_and_persists_across_reload(deck_client):
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.a", "on")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.a"]')
    # Unset default must equal today's shipped behavior: deck ON.
    assert page.evaluate("() => document.getElementById('vaDeck').hidden") is False

    _open_settings(page)
    page.click("#deckGroup summary")
    page.wait_for_function("() => document.getElementById('deckGroup').open", timeout=10000)
    page.click("#deckOnToggle")
    page.wait_for_function(
        "() => document.getElementById('vaDeck').hidden === true", timeout=5000)

    page.reload()
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    assert page.evaluate("() => document.getElementById('vaDeck').hidden") is True, \
        "deck-off setting did not survive the reload"


def test_deck_unchecking_a_point_hides_it_and_preserves_sibling_order(deck_client):
    """Per-point visibility (part 2) must toggle the existing tile node, not
    destroy/recreate anything -- the other tiles keep their DOM order and
    node identity."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("switch.aaa", "on", friendly="AAA"),
        _deck_entity("switch.bbb", "on", friendly="BBB"),
        _deck_entity("switch.ccc", "on", friendly="CCC"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.ccc"]')
    page.evaluate(
        "document.querySelector('[data-entity=\"switch.bbb\"]').__deckTestMark = 'stamp-bbb'")
    before = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    assert before == ["switch.aaa", "switch.bbb", "switch.ccc"]

    _open_settings(page)
    page.click("#deckGroup summary")
    page.wait_for_function("() => document.getElementById('deckGroup').open", timeout=10000)
    page.uncheck("#deckPointList input[id='deckPoint_switch_bbb']")
    # Assert the jack is actually GONE FROM THE SCREEN, not merely that the
    # attribute meant to hide it got set. This originally waited on
    # `.hidden === true` and passed for a build where unchecking a point did
    # nothing visible: `.deckTile { display: flex }` is an author rule and
    # `[hidden] { display: none }` is a UA rule, so the flag flipped and the
    # tile stayed put. A gate for "is it gone" reads computed style, never the
    # attribute that is supposed to cause it.
    page.wait_for_function(
        "() => getComputedStyle(document.querySelector('[data-entity=\"switch.bbb\"]'))"
        ".display === 'none'", timeout=5000)
    assert page.eval_on_selector(
        '[data-entity="switch.bbb"]', "el => el.offsetParent === null"
    ), "point unchecked in the panel is still laid out on the deck"

    after = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    assert after == before, f"unchecking a point reordered/recreated tiles: {before} -> {after}"
    mark = page.evaluate(
        "document.querySelector('[data-entity=\"switch.bbb\"]').__deckTestMark")
    assert mark == "stamp-bbb", "hidden tile's DOM node was replaced, not reused"
    assert page.eval_on_selector('[data-entity="switch.aaa"]', "el => el.hidden") is False
    assert page.eval_on_selector('[data-entity="switch.ccc"]', "el => el.hidden") is False
    assert page.evaluate("() => localStorage.getItem('va-deck-hidden')") == '["switch.bbb"]'


def test_deck_hide_unavailable_is_exact_and_frozen_against_live_sse(deck_client):
    """Hide-unavailable (part 3) hides exactly the unavailable points, and
    must NOT re-evaluate live: an SSE update flipping a visible point to
    unavailable while the switch is ON must not hide it or move anything."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("switch.reachable", "on", friendly="Reachable"),
        _deck_entity("switch.gone", "unavailable", friendly="Gone"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.gone"]')

    _open_settings(page)
    page.click("#deckGroup summary")
    page.wait_for_function("() => document.getElementById('deckGroup').open", timeout=10000)
    page.click("#deckHideUnavailableToggle")
    # Computed style, not the `hidden` property -- same reason as the per-point
    # test above: `.deckTile`'s author `display: flex` beat the UA `[hidden]`
    # rule, so this assertion was green on a build where the switch visibly did
    # nothing. Read the pixels, not the flag meant to change them.
    page.wait_for_function(
        "() => getComputedStyle(document.querySelector('[data-entity=\"switch.gone\"]'))"
        ".display === 'none'",
        timeout=5000)
    assert page.eval_on_selector(
        '[data-entity="switch.reachable"]', "el => getComputedStyle(el).display !== 'none'"
    ), "hide-unavailable also hid a reachable point"

    # Now flip a VISIBLE point to unavailable via SSE, switch still on --
    # frozen-order guarantee: this must stay in place and stay visible.
    before_order = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    deck.push(_deck_entity("switch.reachable", "unavailable", friendly="Reachable"))
    page.wait_for_function(
        "() => document.querySelector('[data-entity=\"switch.reachable\"]')?.dataset.state === 'unavailable'",
        timeout=5000)
    assert page.eval_on_selector('[data-entity="switch.reachable"]', "el => el.hidden") is False, \
        "an entity that went unavailable via SSE was hidden without a switch flip -- live re-evaluation regressed"
    after_order = page.eval_on_selector_all(".deckTile", "els => els.map(e => e.dataset.entity)")
    assert after_order == before_order


def test_deck_sim_picker_forces_state_and_does_not_survive_reload(deck_client):
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.a", "on")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.a"]')

    _open_settings(page)
    page.click("#deckGroup summary")
    page.wait_for_function("() => document.getElementById('deckGroup').open", timeout=10000)
    page.select_option("#deckSimSelect", "unavailable")
    page.wait_for_function(
        "() => document.querySelector('[data-entity=\"light.a\"]')?.dataset.state === 'unavailable'",
        timeout=5000)
    badge = page.eval_on_selector("#deckSimBadge", "el => ({hidden: el.hidden, text: el.textContent})")
    assert badge["hidden"] is False
    assert "unavailable" in badge["text"].lower()

    page.reload()
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.wait_for_selector('[data-entity="light.a"]')
    assert page.evaluate(
        "() => document.querySelector('[data-entity=\"light.a\"]').dataset.state") == "on", \
        "forced state survived a reload -- it must be in-memory only"
    assert page.eval_on_selector("#deckSimBadge", "el => el.hidden") is True


def test_deck_point_list_scrolls_instead_of_growing_unbounded(deck_client):
    """A real 29-point deck must not dwarf every other Service Panel
    section -- #deckPointList gets a bounded, internally-scrolling box,
    same as any other real-instrument readout in this chassis."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity(f"light.tile{i}", "on", friendly=f"Tile {i}") for i in range(29)]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.tile28"]')

    _open_settings(page)
    page.click("#deckGroup summary")
    page.wait_for_function("() => document.getElementById('deckGroup').open", timeout=10000)
    page.wait_for_selector("#deckPointList .switch-h-row")

    box = page.eval_on_selector(
        "#deckPointList",
        "el => ({scrollHeight: el.scrollHeight, clientHeight: el.clientHeight})")
    assert box["clientHeight"] <= 360, f"point list is not bounded: {box['clientHeight']}px tall"
    assert box["scrollHeight"] > box["clientHeight"],         "29 rows don't overflow the box -- the scroll gate can't actually catch an unbounded list"


def test_deck_point_list_uses_switch_primitive_not_a_native_checkbox_look(deck_client):
    """The per-point control must read as the panel's own throw switch, not
    a bare browser checkbox -- and the visible uppercasing on its legend
    must be CSS, never textContent (the accessible name stays mixed-case)."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("light.kitchen", "on", friendly="Kitchen Light")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="light.kitchen"]')

    _open_settings(page)
    page.click("#deckGroup summary")
    page.wait_for_function("() => document.getElementById('deckGroup').open", timeout=10000)
    page.wait_for_selector("#deckPointList .switch-h-row")

    row = page.eval_on_selector(
        "#deckPointList .switch-h-row",
        "el => ({ hasNativeInput: !!el.querySelector('input:not(.switch-h-input)'),"
        " hasTrack: !!el.querySelector('.switch-track-h') })")
    assert row["hasNativeInput"] is False, "a bare native checkbox is still present"
    assert row["hasTrack"] is True, "the decorative throw-switch track is missing"

    label = page.eval_on_selector(
        "#deckPointList label",
        "el => ({ text: el.textContent, transform: getComputedStyle(el).textTransform,"
        " family: getComputedStyle(el).fontFamily })")
    assert label["text"] == "Kitchen Light",         "accessible name (textContent) must stay mixed-case -- uppercasing belongs in CSS only"
    assert label["transform"] == "uppercase"
    assert "mono" in label["family"].lower() or "menlo" in label["family"].lower()         or "courier" in label["family"].lower(), f"legend isn't using the mono faceplate font: {label['family']}"


# ── S12/R10: skin-specific deck placement (fix for the 3b7aab1 regression) ──

def _goto_owner_deck_themed(browser, deck_server, theme, entity_count=29, width=1440, height=1271, query=""):
    """Like `_goto_owner_deck`, but selects a themed skin (hacker) before
    measuring -- `#vaDeck`'s placement is now genuinely theme-specific
    (a skin either gives it its own `grid-area: deck` or hides it
    outright), so the owner-geometry deck fixture needs a themed sibling
    rather than only ever exercising the default `instrument` faceplate."""
    server = StubControlServer()
    server.start()
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        f"localStorage.setItem('va-ws-url', 'ws://127.0.0.1:{server.port}');"
        "localStorage.setItem('va-avatar', 'off');"
        "try{localStorage.setItem('va-firstrun-done','1')}catch(e){}")
    deck_server.entities = [
        _deck_entity(f"light.tile{i}", "on", friendly=f"Long Friendly Name For Entity Number {i} Do Not Disturb")
        for i in range(entity_count)
    ]
    page.goto(f"http://127.0.0.1:{deck_server.port}/index.html{query}")
    page.wait_for_function("() => ws && ws.readyState === 1", timeout=15000)
    page.evaluate(f"() => setTheme('{theme}')")
    page.wait_for_function(f"() => document.documentElement.getAttribute('data-theme') === '{theme}'")
    page.wait_for_function(
        f"() => Array.from(document.styleSheets).some(s => (s.href || '').includes('themes/{theme}.css')"
        " && s.cssRules && s.cssRules.length > 0)",
        timeout=10000)
    # state="attached", not the default "visible" -- a themed skin may
    # legitimately hide #vaDeck (`display: none`), which hides every
    # descendant .deckTile with it; the deck still needs to finish
    # rendering before this helper hands back control either way.
    page.wait_for_selector('.deckTile', state="attached")
    page.wait_for_timeout(1100)
    return page, server, errors


@pytest.mark.parametrize("theme", ["hacker"])
def test_themed_deck_does_not_overlap_status_strip(browser, deck_server, theme):
    """The 3b7aab1 regression, measured directly (not the naive "does deck
    start below the status strip" check, which passes even on the broken
    code -- `.zone-meter`'s own box collapses to ~20px under both skins'
    `display: block` override once combined with the base sheet's `.zone-
    meter { min-height: 0 }` (1024px+) and the S10 cap's definite grid
    height: the row shrinks, but `#vaDeck` (still in normal flow,
    `overflow: visible`) keeps rendering at its full natural size regardless
    -- and paints straight over `#meterChrome`, the centrepiece in the
    ADJACENT column, not merely "the row below". Verified: on the true
    pre-fix HEAD, deck spans y135-377.5 while #meterChrome spans y130.5-
    508.4 -- a dead-centre overlap this file's own git history confirms
    was invisible to a naive top/bottom-only check.)

    A second, independent way to be "not really there" surfaced later: a
    real rectangle with zero pixel overlap against every #contentGrid
    sibling can still be entirely CLIPPED by `#contentGrid`'s own
    `overflow-y: auto` under the S10 cap (its box ends, but the item's
    LAYOUT rect keeps going past it, unclipped from `getBoundingClientRect`'s
    point of view) -- landing, in absolute page coordinates, on top of
    whatever chassis region sits BELOW `#contentGrid` entirely
    (`#historyRail`/`.lowerBayControls`, in `#lowerBay`, a different
    chassis region). A rect comparison only against `#contentGrid`'s own
    children is blind to this; both a real intersection check against
    those `#lowerBay` regions AND an `elementFromPoint()` hit-test at the
    deck's own centre are required to catch it -- the hit-test is the one
    that actually proves something is genuinely PAINTED there, not merely
    positioned there. `#vaDeck` now either gets its own `grid-area: deck`
    row with both properties verified, or hides outright (a legitimate
    answer this test also accepts, for a skin with no sensible home for a
    29-point patchbay)."""
    page, server, errors = _goto_owner_deck_themed(browser, deck_server, theme)
    result = page.evaluate("""() => {
        const deck = document.getElementById('vaDeck');
        const dr = deck.getBoundingClientRect();
        const siblingSelectors = [
            '#meterChrome', '#transcript', '#assistantText',
            '.selector:has(#personaDisplay)', '.selector:has(#voiceDisplay)',
            '.stat-strip', '#sessionPersonaWindow', '#pttWrap', '.zone-head',
            '#historyRail', '.lowerBayControls', '#avatarPane',
        ];
        const overlaps = [];
        for (const sel of siblingSelectors) {
            const el = document.querySelector(sel);
            if (!el || getComputedStyle(el).display === 'none') continue;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const intersects = dr.left < r.right && dr.right > r.left
                && dr.top < r.bottom && dr.bottom > r.top;
            if (intersects) overlaps.push(sel);
        }
        const cx = dr.left + dr.width / 2, cy = dr.top + dr.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        return {
            deckDisplay: getComputedStyle(deck).display,
            deckHeight: dr.height,
            overlaps,
            centreHitsDeck: !!(hit && (hit === deck || deck.contains(hit))),
            hitId: hit ? (hit.id || hit.className || hit.tagName) : null,
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    if result["deckDisplay"] == "none":
        return
    assert result["deckHeight"] >= 200, (
        f"{theme}: deck height {result['deckHeight']}px is below the ~200px usability floor")
    assert not result["overlaps"], (
        f"{theme}: #vaDeck overlaps other chassis region(s): {result['overlaps']}")
    assert result["centreHitsDeck"], (
        f"{theme}: #vaDeck's own centre hit-tests to {result['hitId']!r}, not the deck itself -- "
        f"it has a rect but is not actually painted/reachable there")


@pytest.mark.parametrize("theme", ["hacker"])
def test_themed_deck_ptt_still_reachable(browser, deck_server, theme):
    """S10's whole point, reconfirmed on the shipped themed skin against a
    full 29-tile deck: `#ptt` must still hit-test as itself at its own
    centre.
    Deliberately SINGULAR `elementFromPoint()` (not the runtime gate's
    plural `elementsFromPoint`) -- see `test_ptt_still_reachable_with_full_deck`
    for why that distinction matters."""
    page, server, errors = _goto_owner_deck_themed(browser, deck_server, theme)
    result = page.evaluate("""() => {
        const ptt = document.getElementById('ptt');
        const pr = ptt.getBoundingClientRect();
        const hit = document.elementFromPoint(pr.left + pr.width / 2, pr.top + pr.height / 2);
        return !!(hit && (hit === ptt || ptt.contains(hit) || hit.contains(ptt)));
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert result, f"{theme}: #ptt is not reachable via elementFromPoint at its own centre with a full deck"


def test_instrument_deck_placement_unchanged_by_themed_deck_fix(browser, deck_server):
    """Regression guard for the theme the owner actually uses: giving a
    themed skin its own `#vaDeck` placement (R10) must not touch the base
    `instrument` faceplate's -- `#vaDeck` should still
    render inside `.zone-meter` at ~530x370, ~433px from the viewport top
    (measured at 1440x1271 with a real 29-tile deck). Must fail if a later
    change "fixes" the skins by touching the base theme instead of its own
    stylesheet."""
    page, server, errors = _goto_owner_deck(browser, deck_server, entity_count=29)
    result = page.evaluate("""() => {
        const deck = document.getElementById('vaDeck');
        const dr = deck.getBoundingClientRect();
        return {
            insideMeterZone: !!deck.closest('.zone-meter'),
            width: dr.width, height: dr.height, top: dr.top,
        };
    }""")
    assert not errors, f"uncaught page errors: {errors}"
    page.context.close()
    server.stop()
    assert result["insideMeterZone"], "#vaDeck moved out of .zone-meter on the instrument theme"
    assert abs(result["width"] - 530) <= 15, f"instrument #vaDeck width drifted: {result['width']}"
    assert abs(result["height"] - 370) <= 15, f"instrument #vaDeck height drifted: {result['height']}"
    assert abs(result["top"] - 433) <= 15, f"instrument #vaDeck top drifted: {result['top']}"



# ── Deck accessibility: roving tabindex, arrow keys, dimmed-label contrast ──
# Live audit findings (2026-08-17): the 28 clickable tiles were each their
# own Tab stop (tabindex=0), sitting between #armSwitch and #personaDisplay
# and costing a keyboard user ~30% of the page's controls just to cross the
# deck. Fixed with a roving-tabindex toolbar (index.html's deckRovingSync/
# deckMoveHorizontal/deckMoveVertical) -- one Tab stop in, arrow keys move
# between jacks, one Tab stop out.

def test_deck_keyboard_bypass_armswitch_to_personadisplay_is_bounded(deck_client):
    """WCAG 2.4.1: with a real, multi-tile deck in between, Tab from
    #armSwitch must reach #personaDisplay in a SMALL bounded number of
    stops -- the deck itself is one stop each way, not 29. A seed that
    doesn't change this number (e.g. asserting <=30) would prove nothing;
    this was run RED against the un-roved `tabIndex=0`-per-tile build first."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity(f"switch.p{i}", "on", friendly=f"Point {i}") for i in range(12)]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.p11"]')
    page.evaluate("() => document.getElementById('armSwitch').focus()")
    stops = []
    for _ in range(8):
        page.keyboard.press("Tab")
        stops.append(page.evaluate(
            "() => document.activeElement.id || document.activeElement.className"))
        if page.evaluate(
                "() => document.activeElement === document.getElementById('personaDisplay')"):
            break
    assert page.evaluate(
        "() => document.activeElement === document.getElementById('personaDisplay')"), \
        f"never reached #personaDisplay within 8 Tab presses: {stops}"
    assert len(stops) <= 4, f"took {len(stops)} real Tab presses to cross the deck: {stops}"


def test_deck_arrow_keys_move_focus_and_enter_space_still_toggle(deck_client):
    """Arrow keys move the roving 0-stop between jacks (not the page's own
    Tab order); Enter and Space on the newly-focused jack must still fire
    exactly one /ha/intent batch each -- the existing per-tile keydown
    handler, untouched by the roving-tabindex change."""
    page, server, deck = deck_client
    deck.entities = [
        _deck_entity("switch.a", "on", friendly="A"),
        _deck_entity("switch.b", "on", friendly="B"),
    ]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.b"]')
    page.evaluate("() => document.querySelector('[data-entity=\"switch.a\"]').focus()")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("() => document.activeElement.dataset.entity") == "switch.b"
    assert page.evaluate(
        "() => document.querySelector('[data-entity=\"switch.a\"]').tabIndex") == -1, \
        "moving focus off a jack must drop it back to a -1 stop"

    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    assert deck.intents == [{"intents": [{"entity_id": "switch.b", "service": "toggle"}]}]

    page.keyboard.press("Space")
    page.wait_for_timeout(200)
    assert deck.intents == [
        {"intents": [{"entity_id": "switch.b", "service": "toggle"}]},
        {"intents": [{"entity_id": "switch.b", "service": "toggle"}]},
    ]


def test_deck_point_list_dimmed_label_meets_wcag_contrast(deck_client):
    """WCAG 1.4.3: a hidden point's dimmed legend in the Service Panel's
    "Visible points" list must still clear 4.5:1 -- measured from real
    rendered pixels (the same harness as the faceplate contrast gate
    above), never assumed from the CSS token. This was the label the audit
    measured at 2.1-2.7:1 under the old `opacity: 0.42` treatment."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("switch.front_light", "on", friendly="Front light")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.front_light"]')
    _open_settings(page)
    page.click("#deckGroup summary")
    page.wait_for_function("() => document.getElementById('deckGroup').open", timeout=10000)
    page.uncheck("#deckPointList input[id='deckPoint_switch_front_light']")
    selector = ("#settingsPanel #deckPointList "
                ".switch-h-row:has(.switch-h-input:not(:checked)) .deckPointLabel")
    page.wait_for_selector(selector)
    ratio, fg_rgb, bg_rgb = _measure_pair(page, selector)
    print(f"dimmed deckPointLabel: {ratio:.2f}:1 (fg={fg_rgb} bg={bg_rgb})")
    assert ratio >= 4.5, (
        f"dimmed deck point label is {ratio:.2f}:1 (fg={fg_rgb} bg={bg_rgb}), "
        f"below the 4.5:1 WCAG AA floor")


def test_deck_link_and_write_failure_are_announced_once_each(deck_client):
    """The shared #deckLiveStatus live region (WCAG 4.1.3) announces real
    transitions in words -- link loss and a write going to "failed" -- and
    must not re-announce an already-announced, unchanged state on every
    following SSE frame (29 entities updating would otherwise be noise)."""
    page, server, deck = deck_client
    deck.entities = [_deck_entity("switch.porch", "off")]
    _goto_deck(page, deck)
    page.wait_for_selector('[data-entity="switch.porch"]')

    deck.push(_deck_entity("switch.porch", "off", write_status="failed"))
    page.wait_for_function(
        "() => document.querySelector('[data-entity=\"switch.porch\"]')?.dataset.write === 'failed'",
        timeout=5000)
    page.wait_for_timeout(500)  # past the 400ms coalesce window
    text1 = page.eval_on_selector("#deckLiveStatus", "el => el.textContent")
    assert "write failed" in text1.lower(), f"no write-failed announcement: {text1!r}"

    # A repeat frame that's STILL "failed" must not add a second announcement.
    deck.push(_deck_entity("switch.porch", "off", write_status="failed"))
    page.wait_for_timeout(500)
    text2 = page.eval_on_selector("#deckLiveStatus", "el => el.textContent")
    assert text2 == text1, f"an unchanged write status re-announced itself: {text1!r} -> {text2!r}"

    deck.stop()
    page.wait_for_function(
        "() => document.getElementById('vaDeck').dataset.link === 'down'", timeout=10000)
    page.wait_for_timeout(500)
    text3 = page.eval_on_selector("#deckLiveStatus", "el => el.textContent")
    assert "link lost" in text3.lower(), f"no link-lost announcement: {text3!r}"


# ── PWA install (manifest.json + sw.js) ──────────────────────────────────
#
# A REAL, non-symlinked copy of the PWA files -- unlike `_webclient_temp_root`
# above (module-scoped, symlinks index.html straight back to the real repo
# file), the stale-shell gate below needs to mutate the served bytes, and a
# symlink write would land on the actual repo.
#
# Served on http://localhost specifically, per spec, on a fresh ephemeral
# port per test -- service worker storage is partitioned per origin
# (scheme+host+port), so each test starts with no registration or cache left
# over from a previous one.

_SW_READY_JS = """async () => {
  const withTimeout = (p, ms, label) => Promise.race([
    p,
    new Promise((_, rej) => setTimeout(() => rej(new Error(label)), ms)),
  ]);
  const reg = await withTimeout(navigator.serviceWorker.ready, 8000, 'sw ready timeout');
  const worker = reg.active;
  if (!worker) return null;
  if (worker.state === 'activated') return worker.state;
  // `navigator.serviceWorker.ready` resolves as soon as the registration HAS
  // an active worker -- per spec that happens when the worker enters
  // `activating`, NOT when activation finishes. sw.js's activate handler does
  // real async work (enumerating and evicting old caches), so reading
  // `.state` straight after `ready` legitimately returns 'activating' whenever
  // the box is busy -- which is why this showed up only in a full-suite run
  // and never in isolation. Wait for the transition instead of asserting the
  // race; a genuine failure to activate still fails, on the timeout.
  await withTimeout(new Promise((resolve) => {
    worker.addEventListener('statechange', function onChange() {
      if (worker.state === 'activated') {
        worker.removeEventListener('statechange', onChange);
        resolve();
      }
    });
  }), 8000, 'sw activation timeout');
  return worker.state;
}"""


@pytest.fixture
def pwa_root(tmp_path):
    root = tmp_path / "pwa_root"
    root.mkdir()
    for name in ("index.html", "sw.js", "manifest.json"):
        shutil.copy(os.path.join(HERE, name), root / name)
    shutil.copytree(os.path.join(HERE, "icons"), root / "icons")
    return root


@pytest.fixture
def pwa_server(pwa_root):
    """Serves `pwa_root` and answers GET /models with a 200 JSON body,
    standing in for serve.py's real dynamic /models endpoint -- just enough
    that a fetch handler which forgot to exclude it WOULD cache something,
    so the exclusion gate below can actually go red if that regresses."""
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(pwa_root), **kw)

        def do_GET(self):
            if self.path == "/models":
                body = b'{"object": "list", "data": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _pwa_page(browser, port, path="/index.html"):
    """A fresh context/page pointed at a refused WS port (fast failure, no
    dangling CONNECTING socket) so the boot sequence's connect() doesn't
    interfere -- these tests are about the service worker, not the control
    channel."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(
        "localStorage.setItem('va-avatar', 'off');"
        "localStorage.setItem('va-firstrun-done', '1');"
        "localStorage.setItem('va-ws-url', 'ws://127.0.0.1:1');")
    page.goto(f"http://localhost:{port}{path}")
    return ctx, page, errors


def _await_controlled(page):
    """Reload until the page is under the active SW's control. Immediately
    after `serviceWorker.ready` resolves, the browser doesn't always have
    the just-activated worker wired as the controller for the very next
    navigation yet (observed racing in practice, ~1 in 3 runs) -- one
    settling reload plus an explicit wait removes it, matching the ordinary
    "reload once after install" behavior every real PWA install has."""
    page.reload()
    page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=5000)


def test_manifest_served_parses_and_names_real_correctly_sized_icons(pwa_server):
    """Gate (a): manifest.json is served (not just present on disk), parses,
    carries the fields a browser needs to offer install, and every icon it
    names actually exists at the size it claims -- opened with PIL, not
    trusted from the JSON."""
    with urllib.request.urlopen(f"http://localhost:{pwa_server}/manifest.json") as resp:
        assert resp.status == 200
        manifest = json.loads(resp.read())

    for key in ("name", "short_name", "start_url", "scope", "display", "icons", "theme_color"):
        assert key in manifest, f"manifest.json missing required field {key!r}"

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert "192x192" in sizes and "512x512" in sizes, f"missing a 192 and/or 512 icon: {sizes}"

    for icon in manifest["icons"]:
        path = os.path.join(HERE, icon["src"])
        assert os.path.isfile(path), f"manifest names {icon['src']!r} but it does not exist"
        with Image.open(path) as im:
            actual = f"{im.width}x{im.height}"
        assert actual == icon["sizes"], f"{icon['src']} is {actual} but manifest claims {icon['sizes']}"


def test_service_worker_registers_and_activates(browser, pwa_server):
    """Gate (b): a real registration reaching `activated`, not a check that
    sw.js exists on disk."""
    ctx, page, errors = _pwa_page(browser, pwa_server)
    state = page.evaluate(_SW_READY_JS)
    assert state == "activated"
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_offline_shell_still_renders_after_activation(browser, pwa_server):
    """Gate (c): once the SW has activated and cached the shell, going
    offline still renders the chassis. Proven to actually depend on the SW,
    not the browser's own disk cache or something else: a context that
    never goes online at all, with sw.js itself blocked outright, is
    asserted to fail offline first."""
    ctrl_ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctrl_ctx.route("**/sw.js", lambda route: route.abort())
    ctrl_page = ctrl_ctx.new_page()
    ctrl_page.add_init_script(
        "localStorage.setItem('va-avatar', 'off');"
        "localStorage.setItem('va-firstrun-done', '1');"
        "localStorage.setItem('va-ws-url', 'ws://127.0.0.1:1');")
    ctrl_ctx.set_offline(True)
    control_failed = False
    try:
        ctrl_page.goto(f"http://localhost:{pwa_server}/index.html", timeout=5000)
    except Exception:
        control_failed = True
    ctrl_ctx.close()
    assert control_failed, (
        "the offline load succeeded with no service worker involved at all -- "
        "this control is supposed to fail, or the real assertion below proves nothing")

    ctx, page, errors = _pwa_page(browser, pwa_server)
    page.evaluate(_SW_READY_JS)
    page.wait_for_selector("#ptt")
    _await_controlled(page)

    ctx.set_offline(True)
    page.reload()
    page.wait_for_selector("#ptt", timeout=5000)
    title = page.evaluate("() => document.querySelector('.nameplate h1')?.textContent || ''")
    assert "PATCHBAY" in title
    ctx.set_offline(False)
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_dynamic_endpoints_are_never_cached_by_the_service_worker(browser, pwa_server):
    """Gate (e): /models (see webclient/serve.py) is exactly the shape of
    endpoint the shell/asset caching strategies must never touch -- fetch it
    through an active SW, then check every cache for an entry that would
    prove it got cached (the fixture answers it with a 200 JSON body, so a
    handler that forgot to exclude it would have cached one)."""
    ctx, page, errors = _pwa_page(browser, pwa_server)
    page.evaluate(_SW_READY_JS)
    page.wait_for_selector("#ptt")
    _await_controlled(page)

    matches = page.evaluate("""async () => {
        await fetch('/models');
        await fetch('/models');
        const names = await caches.keys();
        const hits = [];
        for (const name of names) {
            const cache = await caches.open(name);
            for (const req of await cache.keys()) {
                if (new URL(req.url).pathname === '/models') hits.push(name);
            }
        }
        return hits;
    }""")
    assert matches == [], f"/models was cached in: {matches}"
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_install_and_update_controls_live_in_settings_panel_and_are_hidden_at_rest(browser, pwa_server):
    """Gate (f): both controls are in the DOM inside #settingsPanel (so the
    JS only ever has to flip `hidden`, never build the element), and
    neither is a real hit target on a normal load -- elementFromPoint, not
    a rect check alone (a rect check alone has lied in this repo before)."""
    ctx, page, errors = _pwa_page(browser, pwa_server)
    page.wait_for_selector("#ptt")

    in_panel = page.evaluate("""() => {
        const panel = document.getElementById('settingsPanel');
        return {
            install: panel.contains(document.getElementById('pwaInstallBtn')),
            update: panel.contains(document.getElementById('pwaUpdateBtn')),
        };
    }""")
    assert in_panel == {"install": True, "update": True}

    _open_settings(page)
    for el_id in ("pwaInstallBtn", "pwaUpdateBtn"):
        hit = page.evaluate(f"""() => {{
            const el = document.getElementById('{el_id}');
            const r = el.getBoundingClientRect();
            const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
            const stack = document.elementsFromPoint(cx, cy);
            return stack.some(e => e === el || el.contains(e));
        }}""")
        assert hit is False, f"#{el_id} is a live hit target at rest (should be hidden)"
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


def test_stale_shell_gate_serves_new_bytes_after_a_deploy(browser, pwa_server, pwa_root):
    """Gate (d), the important one: network-first for the shell means a
    reload after the SW is active sees a new deploy immediately, not the
    version cached at install. This is exactly the regression a cache-first
    shell would introduce (index.html has no build step or version stamp
    and deploys with a plain `git pull`) -- verified by hand against a
    scratch cache-first fetch handler while writing this test, which left
    the pre-deploy title in place and failed the assertion below."""
    ctx, page, errors = _pwa_page(browser, pwa_server)
    page.evaluate(_SW_READY_JS)
    page.wait_for_selector("#ptt")
    _await_controlled(page)

    marker = "STALE-SHELL-GATE-MARKER"
    index_path = pwa_root / "index.html"
    original = index_path.read_text()
    assert marker not in original
    index_path.write_text(original.replace("<title>Patchbay", f"<title>{marker} Patchbay", 1))
    # Force Last-Modified into a new whole second: SimpleHTTPRequestHandler's
    # own conditional-GET support (independent of anything the SW does)
    # answers a same-second mutation with a 304 against the browser's HTTP
    # cache entry from the first load, which would fail this gate for a
    # reason that has nothing to do with the SW's caching strategy.
    bumped = os.path.getmtime(index_path) + 2
    os.utime(index_path, (bumped, bumped))

    page.reload()
    page.wait_for_selector("#ptt")
    assert marker in page.title(), (
        "reload after a deploy still shows the pre-deploy title -- the "
        "shell is being served from cache instead of the network")

    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()


# ── sw.js adversarial-review fixes ───────────────────────────────────────

def test_offline_shell_is_not_poisoned_by_a_404_navigation(browser, pwa_server):
    """A navigation to a bad path -- SimpleHTTPRequestHandler (and the real
    serve.py, same base class) answers a 404 with Content-Type: text/html,
    which satisfies isNavigationRequest() -- must never overwrite the cached
    offline shell. Otherwise a stray navigation (typo, stale bookmark, a
    probe) leaves every later offline load rendering that 404 instead of
    the cockpit."""
    ctx, page, errors = _pwa_page(browser, pwa_server)
    page.evaluate(_SW_READY_JS)
    page.wait_for_selector("#ptt")
    _await_controlled(page)

    # Confirm the premise for real, not assumed.
    probe = page.evaluate("""async () => {
        const res = await fetch('/this-path-does-not-exist');
        return {status: res.status, contentType: res.headers.get('content-type') || ''};
    }""")
    assert probe["status"] == 404
    assert "text/html" in probe["contentType"], f"404 wasn't served as text/html: {probe}"

    # A real navigation (not just fetch()) so the SW's navigation branch runs.
    page.goto(f"http://localhost:{pwa_server}/this-path-does-not-exist")
    page.wait_for_timeout(200)

    ctx.set_offline(True)
    page.goto(f"http://localhost:{pwa_server}/index.html")
    has_chassis = page.evaluate("() => !!document.getElementById('ptt')")
    body_snippet = page.evaluate("() => (document.body.innerText || '').slice(0, 120)")
    ctx.set_offline(False)
    ctx.close()
    assert has_chassis, (
        f"offline root did not render the cockpit chassis -- the 404 poisoned "
        f"the shell cache; body starts: {body_snippet!r}")


# Loads sw.js in a minimal sandboxed self/caches/fetch and invokes its real
# "fetch" listener directly for a same-origin static-asset GET, outside the
# browser network stack entirely. Chromium collapses both a normal offline
# failure and a broken respondWith() into the same page-visible
# "net::ERR_FAILED"/"TypeError: Failed to fetch" (confirmed empirically
# while writing this), so the only way to actually see what respondWith()
# was called with is to call the handler ourselves and inspect it.
_SW_FETCH_PROBE_JS = r"""'use strict';
const listeners = {};
global.self = {
  addEventListener: (name, fn) => { listeners[name] = fn; },
  location: { origin: 'http://localhost' },
};
global.URL = URL;

const CACHED = process.env.SIM_CACHED === '1' ? new Response('cached body') : undefined;
const NETWORK_OK = process.env.SIM_NETWORK_OK === '1';

const fakeCache = { match: async () => CACHED, put: async () => {} };
global.caches = {
  open: async () => fakeCache,
  match: async () => CACHED,
  keys: async () => [],
  delete: async () => true,
};
global.fetch = async () => {
  if (NETWORK_OK) return new Response('net body', { status: 200 });
  throw new TypeError('Failed to fetch');
};

const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));

const fetchHandler = listeners['fetch'];
if (!fetchHandler) {
  console.log(JSON.stringify({ error: 'no fetch handler registered' }));
  process.exit(1);
}

const event = {
  request: {
    method: 'GET',
    url: 'http://localhost/theme.css',
    mode: 'cors',
    headers: { get: (name) => (name === 'accept' ? '*/*' : null) },
  },
  respondWith(promiseOrValue) {
    Promise.resolve(promiseOrValue).then(
      (value) => {
        console.log(JSON.stringify({
          resolvedToResponse: value instanceof Response,
          value: value === undefined ? 'undefined' : String(value),
        }));
      },
      (err) => {
        console.log(JSON.stringify({ rejected: true, message: String((err && err.message) || err) }));
      }
    );
  },
};

fetchHandler(event);
"""


def _probe_sw_asset_fetch(sw_path, tmp_dir, cached, network_ok):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    probe = os.path.join(str(tmp_dir), "sw_fetch_probe.js")
    with open(probe, "w") as f:
        f.write(_SW_FETCH_PROBE_JS)
    env = dict(os.environ, SIM_CACHED="1" if cached else "0",
               SIM_NETWORK_OK="1" if network_ok else "0")
    result = subprocess.run([node, probe, str(sw_path)],
                             capture_output=True, text=True, timeout=10, env=env)
    assert result.returncode == 0, f"probe crashed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_offline_uncached_asset_fetch_resolves_respondwith_to_a_response(pwa_root):
    """A same-origin static asset that was never cached, fetched while
    offline: `cached` is undefined and the network fetch rejects, so the
    stale-while-revalidate branch's `cached || network` must not resolve to
    `undefined` -- respondWith() requires a real Response (or a rejection),
    never a promise that resolves to nothing."""
    outcome = _probe_sw_asset_fetch(os.path.join(str(pwa_root), "sw.js"), pwa_root,
                                     cached=False, network_ok=False)
    assert outcome.get("resolvedToResponse") is True, (
        f"respondWith() received a non-Response for an offline, never-cached asset: {outcome}")


def test_a_deploy_does_not_auto_reload_the_open_cockpit_only_the_user_can(browser, pwa_server, pwa_root):
    """Bug 3, the important one: this is a live voice instrument, and an
    unannounced reload drops the WebSocket and kills an in-flight
    conversation, possibly mid-sentence. A worker discovering an update MUST
    NOT self-activate/self-reload the page -- only clicking "Update ready"
    in the service panel may. Proven with a real update: sw.js's bytes are
    mutated on disk and discovered via registration.update(), the same
    mechanism a real browser uses periodically/on navigation, with no
    reload of this test's own page involved in triggering the check."""
    ctx, page, errors = _pwa_page(browser, pwa_server)
    page.evaluate(_SW_READY_JS)
    page.wait_for_selector("#ptt")
    _await_controlled(page)

    navigations = []
    page.on("framenavigated", lambda f: navigations.append(f.url) if f.parent_frame is None else None)

    sw_path = pwa_root / "sw.js"
    original = sw_path.read_text()
    marker = "// UPDATE-GATE-MARKER"
    assert marker not in original
    sw_path.write_text(original + f"\n{marker}\n")
    # Same 1-second Last-Modified granularity artifact as the stale-shell
    # gate above: SimpleHTTPRequestHandler's own conditional-GET support
    # answers a same-second mutation with a 304, which here means
    # registration.update() never even sees the new bytes -- nothing to do
    # with the bug under test.
    bumped = os.path.getmtime(sw_path) + 2
    os.utime(sw_path, (bumped, bumped))

    page.evaluate("""async () => {
        const reg = await navigator.serviceWorker.getRegistration();
        await reg.update();
    }""")
    page.wait_for_function(
        "() => document.getElementById('pwaUpdateBtn') && !document.getElementById('pwaUpdateBtn').hidden",
        timeout=10000)

    # (a) the page must not have navigated itself while discovering/
    # installing the update -- a real wait window past where an
    # install-time skipWaiting() would already have fired controllerchange.
    page.wait_for_timeout(1500)
    assert navigations == [], (
        f"the page navigated on its own after an update was merely discovered "
        f"(no user click yet): {navigations}")

    # The user-initiated path: click it, and the reload DOES happen, with
    # the new (mutated) worker script now in control.
    page.click("#gearBtn")
    page.wait_for_function("() => document.getElementById('settingsPanel').classList.contains('open')")
    page.click("#pwaUpdateBtn")
    page.wait_for_event("framenavigated", timeout=5000)
    page.wait_for_selector("#ptt")
    active_script = page.evaluate("""async () => {
        const reg = await navigator.serviceWorker.getRegistration();
        return reg && reg.active ? reg.active.scriptURL : null;
    }""")
    assert active_script and active_script.endswith("/sw.js")
    assert not errors, f"uncaught page errors: {errors}"
    ctx.close()
