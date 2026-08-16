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
import re
import shutil
import subprocess
import threading
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
    picker's entry count and ordering versus the tracked-only case
    (chainsawman, hacker) must not shift just because a local override
    exists."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "chainsawman", "name": "Local Override", "swatch": ["#010101", "#020202"]}]))

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.length === 2", timeout=10000)

    merged = page.evaluate("() => customThemes.map(t => ({id: t.id, name: t.name}))")
    assert merged == [{"id": "chainsawman", "name": "Local Override"}, {"id": "hacker", "name": "Terminal"}], \
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
        "() => typeof customThemes !== 'undefined' && customThemes.length === 2", timeout=10000)

    tracked = page.evaluate("() => customThemes.map(t => t.id)")
    assert tracked == ["chainsawman", "hacker"]
    # Chromium logs its own "Failed to load resource: 404" devtools network
    # entry for the local manifest 404 regardless of app code — unrelated to
    # and unsuppressable by loadCustomThemes; only app-level console.error
    # calls indicate a regression here.
    app_errors = [e for e in console_errors if "Failed to load resource" not in e]
    assert app_errors == [], f"console.error fired for an absent local manifest: {app_errors}"

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

    run = subprocess.run(
        ["python3", "-m", "pytest", "test_webclient.py", "-q"],
        cwd=os.path.join(dest, "webclient"), capture_output=True, text=True, timeout=120)
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
# stylesheet/font must still fail whatever asserts against this.
KNOWN_OPTIONAL_LOCAL_OVERLAY_404_URLS = ("avatar/local/registry.mjs", "themes/local/themes.json")


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


# ── chainsawman (Devil Hunter): tracked motif skin + local art overlay ──────
# The tracked file ships publicly and must carry zero references to
# `assets/` (that's the whole point of the split — real character art never
# leaves this developer's own clone). The local file at themes/local/
# REPLACES the tracked one in place when both share an id (see THEME-SPEC.md
# "Local overlay") rather than cascading on top of it.
#
# themes/local/ is gitignored and therefore absent on every clone but this
# developer's own — that absence is the NORMAL case, not an error, so its
# content must never be read at MODULE level (an exported/clean-clone import
# would raise FileNotFoundError and abort collection for the WHOLE file,
# taking every other test down with it — this exact defect shipped once).
# `chainsawman_local_css` below is a module-scoped fixture instead: pytest
# instantiates module-scoped fixtures before function-scoped ones for the
# same test, so it still reads the file before `local_theme_dir` moves the
# real directory aside (preserving the "exercise the ACTUAL shipping file"
# property), and tests that depend on it skip cleanly when it's absent
# rather than exploding collection.
CHAINSAWMAN_TRACKED_CSS_PATH = os.path.join(THEMES_DIR, "chainsawman.css")
CHAINSAWMAN_LOCAL_CSS_PATH = os.path.join(THEMES_DIR, "local", "chainsawman.css")


@pytest.fixture(scope="module")
def chainsawman_local_css():
    if not os.path.isfile(CHAINSAWMAN_LOCAL_CSS_PATH):
        pytest.skip("themes/local/chainsawman.css absent (normal on a clean clone — "
                    "the local art overlay is untracked-by-design and only exists on "
                    "this developer's own machine)")
    with open(CHAINSAWMAN_LOCAL_CSS_PATH, encoding="utf-8") as f:
        return f.read()


def test_chainsawman_tracked_css_contains_no_assets_reference():
    """The public/tracked skin must be pure CSS motif work — no `assets/`
    url anywhere — so the export can never leak character art. This is the
    guard that would catch the split being done backwards (art rules landing
    in the tracked file instead of the local overlay)."""
    with open(CHAINSAWMAN_TRACKED_CSS_PATH, encoding="utf-8") as f:
        css_text = f.read()
    assert "assets/" not in css_text, \
        "tracked chainsawman.css references assets/ — character art must only live in themes/local/chainsawman.css"


def test_chainsawman_theme_applies_and_repaints_with_no_console_errors(browser, static_server, local_theme_dir):
    """Selecting Devil Hunter (chainsawman) sets data-theme on <html>,
    injects the TRACKED themes/chainsawman.css (this developer's own real
    themes/local/ overlay is swapped aside by the fixture so this proves the
    public file specifically, not whatever local override happens to be on
    disk), and leaves the page with zero console errors — and it must have
    actually re-skinned, not merely loaded."""
    assert not os.path.isdir(local_theme_dir)  # fixture backed up any real content; nothing recreated it

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    default_device_bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('.device')).backgroundImage")

    page.evaluate("() => setTheme('chainsawman')")
    page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'chainsawman'")
    # See test_hacker_theme_applies_and_repaints_with_no_console_errors above
    # for why this wait is required, not optional: data-theme flipping alone
    # does not guarantee the injected stylesheet has parsed yet.
    page.wait_for_function(
        "() => Array.from(document.styleSheets).some(s => (s.href || '').includes('themes/chainsawman.css')"
        " && s.cssRules && s.cssRules.length > 0)",
        timeout=10000)
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('link[rel=\"stylesheet\"]'))"
                           ".map(l => l.getAttribute('href'))")
    assert "themes/chainsawman.css" in hrefs, f"chainsawman.css was not injected, got: {hrefs}"
    assert "themes/local/chainsawman.css" not in hrefs

    themed_accent = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
    assert themed_accent.lower() == "#ff5a1f"

    themed_device_bg = page.evaluate(
        "() => getComputedStyle(document.querySelector('.device')).backgroundImage")
    assert themed_device_bg != default_device_bg, \
        "theme selection loaded but .device's chassis background did not actually change"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"
    # local_theme_dir is absent for this test (proving the TRACKED file) —
    # Chromium logs its own devtools 404 for that manifest fetch regardless
    # of app code, same noise test_absent_local_theme_manifest_is_silent
    # already filters; only app-level console.error calls matter here.
    app_errors = [e for e in console_errors if "Failed to load resource" not in e]
    assert app_errors == [], f"console errors after selecting chainsawman theme: {app_errors}"


def test_local_chainsawman_overlay_replaces_tracked_entry_in_place(
        browser, static_server, local_theme_dir, chainsawman_local_css):
    """A local chainsawman entry REPLACES the tracked one in place — the
    picker's entry count and ordering versus the tracked-only case must not
    shift, and selecting it must inject themes/local/chainsawman.css, not
    themes/chainsawman.css. Skips on a clean clone (see chainsawman_local_css)."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "chainsawman", "name": "Devil Hunter", "swatch": ["#121110", "#ff5a1f"]}]))
    _write(os.path.join(local_theme_dir, "chainsawman.css"), chainsawman_local_css)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)
    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.length === 2", timeout=10000)

    merged = page.evaluate("() => customThemes.map(t => ({id: t.id, name: t.name}))")
    assert merged == [{"id": "chainsawman", "name": "Devil Hunter"}, {"id": "hacker", "name": "Terminal"}], \
        f"collision did not replace in place / order shifted: {merged}"

    page.evaluate("() => setTheme('chainsawman')")
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('link[rel=\"stylesheet\"]'))"
                           ".map(l => l.getAttribute('href'))")
    assert "themes/local/chainsawman.css" in hrefs, f"local override path was not injected, got: {hrefs}"
    assert "themes/chainsawman.css" not in hrefs

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_local_chainsawman_art_overlay_images_resolve(
        browser, static_server, local_theme_dir, chainsawman_local_css):
    """The 404 trap: themes/local/chainsawman.css's background-image url is
    `../assets/chainsawman/...`, one directory deeper than a tracked skin —
    checked by proving the real committed local file's one remaining art
    reference (gen-halftone-texture.png) 200s with a nonzero body, against
    the REAL tracked assets directory (untouched by the local_theme_dir
    fixture — only themes/local/ moves). The earlier Denji character
    portrait is gone from this file — the huge tachometer now fills the
    space it used to occupy in .zone-input, and the instrument, not a
    second competing visual, is meant to own that column. Skips on a clean
    clone (see chainsawman_local_css)."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "chainsawman", "name": "Devil Hunter", "swatch": ["#121110", "#ff5a1f"]}]))
    _write(os.path.join(local_theme_dir, "chainsawman.css"), chainsawman_local_css)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    image_responses = {}

    def on_response(resp):
        if "gen-halftone-texture.png" in resp.url:
            image_responses["halftone"] = resp

    page.on("response", on_response)

    page.wait_for_function(
        "() => typeof customThemes !== 'undefined' && customThemes.some(t => t.id === 'chainsawman')",
        timeout=10000)
    page.evaluate("() => setTheme('chainsawman')")
    page.wait_for_function(
        "() => document.documentElement.getAttribute('data-theme') === 'chainsawman'", timeout=10000)
    page.wait_for_timeout(500)  # let the background-image fetch settle

    assert "halftone" in image_responses, "no network request observed for gen-halftone-texture.png"
    resp = image_responses["halftone"]
    assert resp.status == 200, f"gen-halftone-texture.png did not 200: {resp.status} {resp.url}"
    body = resp.body()
    assert len(body) > 0, "gen-halftone-texture.png returned an empty body"

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


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
    color_css = page.evaluate(
        f"() => getComputedStyle(document.querySelector({selector!r})).color")
    box = locator.bounding_box()
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    r, g, b, a = _parse_css_color(color_css)
    bg_rgb = _dominant_background_pixel(img, box, (r, g, b))
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


def _select_and_measure(page, theme_id, pairs):
    page.evaluate(f"() => setTheme({theme_id!r})")
    page.wait_for_function(
        f"() => document.documentElement.getAttribute('data-theme') === {theme_id!r}")
    page.wait_for_timeout(150)  # theme <link> fetch + first paint
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


def test_chainsawman_theme_faceplate_text_meets_wcag_contrast(browser, static_server, local_theme_dir):
    """Devil Hunter's scuffed gunmetal chassis is only legible with engraved
    ink on the bare metal and cream on the powered-dark panels, same law as
    every other skin. Measures the TRACKED motif-only file (local overlay
    swapped aside) — always runs, clean clone included. The local-art
    variant is measured separately (see the test right below) since it must
    skip, not run, when themes/local/chainsawman.css is absent."""
    assert not os.path.isdir(local_theme_dir)  # fixture backed up any real content; nothing recreated it

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    tracked_results = _select_and_measure(page, "chainsawman", CONTRAST_PAIRS)
    print("\n-- chainsawman (tracked) --")
    for selector, bucket, min_ratio, ratio, fg_rgb, bg_rgb, desc in tracked_results:
        print(f"  {ratio:5.2f}:1 (need >={min_ratio}, {bucket})  fg={fg_rgb} bg={bg_rgb}  "
              f"{selector}  — {desc}")
    for selector, bucket, min_ratio, ratio, fg_rgb, bg_rgb, desc in tracked_results:
        assert ratio >= min_ratio, (
            f"chainsawman(tracked)/{selector} ({desc}) is {ratio:.2f}:1 (fg={fg_rgb} bg={bg_rgb}), "
            f"below the {min_ratio}:1 WCAG floor for {bucket} text")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


def test_local_chainsawman_art_overlay_faceplate_text_meets_wcag_contrast(
        browser, static_server, local_theme_dir, chainsawman_local_css):
    """Same law, with the real local art layer active (halftone + character
    portrait) — the art must never sit under live text badly enough to drop
    a pair below the floor. Skips on a clean clone (see chainsawman_local_css)."""
    os.makedirs(local_theme_dir, exist_ok=True)
    _write(os.path.join(local_theme_dir, "themes.json"),
           json.dumps([{"id": "chainsawman", "name": "Devil Hunter", "swatch": ["#121110", "#ff5a1f"]}]))
    _write(os.path.join(local_theme_dir, "chainsawman.css"), chainsawman_local_css)

    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    local_results = _select_and_measure(page, "chainsawman", CONTRAST_PAIRS)
    print("\n-- chainsawman (local, art overlay active) --")
    for selector, bucket, min_ratio, ratio, fg_rgb, bg_rgb, desc in local_results:
        print(f"  {ratio:5.2f}:1 (need >={min_ratio}, {bucket})  fg={fg_rgb} bg={bg_rgb}  "
              f"{selector}  — {desc}")
    for selector, bucket, min_ratio, ratio, fg_rgb, bg_rgb, desc in local_results:
        assert ratio >= min_ratio, (
            f"chainsawman(local-art)/{selector} ({desc}) is {ratio:.2f}:1 (fg={fg_rgb} bg={bg_rgb}), "
            f"below the {min_ratio}:1 WCAG floor for {bucket} text")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"


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
    page.wait_for_function(
        f"() => document.documentElement.getAttribute('data-theme') === {theme_id!r}")
    page.wait_for_timeout(150)  # theme <link> fetch + first paint — see _select_and_measure above
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


def test_chainsawman_chain_revs_with_mic_level(browser, static_server):
    """The Devil Hunter centrepiece is a chainsaw rev, not a passive meter —
    a chainsaw that doesn't rev when spoken to is the defect this test
    exists to prevent. This asserts that intent against the CURRENT
    mechanism: the centrepiece is no longer a needle pivoting through a
    dial (the owner rejected that build outright — "the blades move and it
    shows action", not a repainted tachometer) but an actual chain, its two
    rails (`.meterTicks::before`/`::after`) travelling around a guide bar
    via an animated `background-position`. Speed, not needle angle, is the
    readout now, so the correct signal is the animation's DURATION, not a
    transform snapshot: unlike the old needle (whose rotation ANGLE was
    itself a function of --level baked into the keyframe, requiring
    phase-pinning via the Web Animations API to compare two samples on the
    same point in the cycle), the chain's keyframes are fixed regardless of
    --level — only `animation-duration` changes — so duration is
    phase-independent and directly comparable with a plain
    `getComputedStyle(el, '::before').animationDuration` read, no
    currentTime pinning needed. Drives the live `--level` bus (the same
    custom property pushWaveform sets every audio frame) directly on
    documentElement, from 0 to a high value, and asserts the chain's
    duration shortens (rides faster) — and, separately, that the
    assistant-speaking fixed mid-speed state is NOT the same duration as
    idle, since a chain that reads "revving" identically whether idle or
    replying is exactly the silently-dead defect this test guards against.
    Also keeps the housing shake duration assertion from the dial-era test
    unchanged — that mechanism didn't change in this pass."""
    server = StubControlServer()
    server.start()
    ctx, page, errors, console_errors = _new_page(browser, static_server, server)

    page.evaluate("() => setTheme('chainsawman')")
    page.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'chainsawman'")
    page.wait_for_timeout(150)

    def sample(level):
        return page.evaluate("""(level) => {
            document.documentElement.style.setProperty('--level', String(level));
            const ticks = document.querySelector('.meterTicks');
            const chrome = document.getElementById('meterChrome');
            return {
                topRailDuration: getComputedStyle(ticks, '::before').animationDuration,
                bottomRailDuration: getComputedStyle(ticks, '::after').animationDuration,
                housingDuration: getComputedStyle(chrome).animationDuration,
            };
        }""", level)

    low = sample(0)
    high = sample(1)

    # Assistant-speaking: fixed mid-speed, independent of --level (reset to
    # 0 first so a stale high level can't accidentally produce the same
    # duration by coincidence).
    assistant = page.evaluate("""() => {
        document.documentElement.style.setProperty('--level', '0');
        document.body.classList.remove('recording');
        const a = document.getElementById('assistantText');
        a.classList.add('speaking');
        const ticks = document.querySelector('.meterTicks');
        const d = getComputedStyle(ticks, '::before').animationDuration;
        a.classList.remove('speaking');
        return d;
    }""")

    ctx.close()
    server.stop()
    assert not errors, f"uncaught page errors: {errors}"

    assert low["topRailDuration"] not in ("", "0s"), \
        f"chain's top rail has no computed animation-duration at level=0: {low}"
    assert low["topRailDuration"] != high["topRailDuration"], (
        f"chain's top-rail animation-duration is identical at --level=0 and --level=1 "
        f"(both {low['topRailDuration']!r}) — the chain doesn't speed up with the mic"
    )
    assert low["bottomRailDuration"] != high["bottomRailDuration"], (
        f"chain's bottom-rail animation-duration is identical at --level=0 and --level=1 "
        f"(both {low['bottomRailDuration']!r}) — the return rail doesn't speed up with the mic"
    )
    assert low["housingDuration"] != high["housingDuration"], (
        f"#meterChrome's animation-duration is identical at --level=0 and --level=1 "
        f"({low['housingDuration']!r}) — the shake's SPEED doesn't scale with the mic"
    )
    assert assistant not in (low["topRailDuration"], high["topRailDuration"]), (
        f"assistant-speaking chain duration ({assistant!r}) matches an idle/recording "
        f"duration (idle={low['topRailDuration']!r}, level=1={high['topRailDuration']!r}) "
        f"— the assistant reply doesn't read as a distinct sustained rev"
    )
