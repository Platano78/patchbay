"""Tests for the tyger sidecar.

Run from the repo root:
  python3 -m pytest services/tyger/ -q -rs                      # CPU-free tests (no torch, no GPU)
  <TYGER_HOME>/venv/bin/python -m pytest services/tyger/ -q -rs  # plus the aiohttp tests and, with a GPU and the
                                                                 # bundle, the GPU tests including replay equivalence
The replay test also needs local speech clips (TYGER_CLIPS, a directory of float32 .npy at 16 kHz) and is skipped
without them; no audio is committed.
"""
import asyncio
import glob
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server  # noqa: E402

HOP = server.HOP_N


def hop_frame(mode=0, user=None, agent=None):
    u = np.zeros(HOP, "<i2") if user is None else user.astype("<i2")
    a = np.zeros(HOP, "<i2") if agent is None else agent.astype("<i2")
    return bytes([mode]) + u.tobytes() + a.tobytes()


class FakeSession:
    def __init__(self, **thr):
        self.__dict__.update(thr)
        self.resets = 0

    def reset(self):
        self.resets += 1


class FakeSvc:
    """Stands in for upstream's EarsService: slots, open/close/feed/active."""
    def __init__(self, slots=2, fail=False):
        self.slots, self.fail, self.sessions, self.fed = slots, fail, [], []

    def open(self, **thr):
        if len(self.sessions) >= self.slots:
            return None
        s = FakeSession(**thr)
        self.sessions.append(s)
        return s

    def close(self, s):
        self.sessions.remove(s)

    def active(self):
        return len(self.sessions)

    async def feed(self, sess, user, agent, mode, more=False):
        if self.fail:
            raise RuntimeError("tick failed")
        self.fed.append((sess, user, agent, mode))
        return [{"kind": "FRAME", "state": "idle", "n": len(self.fed), "mode": mode}]


INFO = {"device": "fake", "warm": True, "bundle_sha": "b", "upstream_sha": "u"}


# ------------------------------------------------------------------ protocol framing

def test_hop_frame_parses_modes_and_scales_int16():
    user = np.arange(HOP, dtype="<i2") - 2560
    agent = np.full(HOP, -32768, "<i2")
    mode, u, a = server.parse_hop(hop_frame(1, user, agent))
    assert mode == "speaking" and u.dtype == np.float32 and a.dtype == np.float32
    np.testing.assert_array_equal(u, user.astype(np.float32) / 32768.0)
    assert a[0] == -1.0
    assert server.parse_hop(hop_frame(0))[0] == "listen"
    assert server.FRAME_BYTES == 20481


@pytest.mark.parametrize("data", [b"", hop_frame()[:-1], hop_frame() + b"\0", hop_frame(2), hop_frame(255)])
def test_bad_hop_frames_are_refused(data):
    with pytest.raises(server.FrameError):
        server.parse_hop(data)


# ------------------------------------------------------------------ hello / thresholds

def test_hello_overrides_only_what_it_names():
    d = server.DEFAULT_THRESHOLDS
    got = server.parse_hello({"type": "hello", "thresholds": {"barge_thr": 0.35, "ep_deb": 3}}, d)
    assert got["barge_thr"] == 0.35 and got["ep_deb"] == 3
    assert got["ep_thr"] == d["ep_thr"] and got["bc_deb"] == d["bc_deb"]
    assert isinstance(got["ep_deb"], int)
    assert server.parse_hello({"type": "hello"}, d) == d


@pytest.mark.parametrize("thr", [{"nope": 1}, {"ep_thr": 1.5}, {"ep_thr": "0.9"}, {"ep_deb": 0}, {"ep_deb": 2.5},
                                 {"bc_thr": True}, "x",
                                 {"ep_deb": float("nan")}, {"ep_deb": float("inf")}, {"ep_thr": float("-inf")},
                                 {"ep_thr": float("nan")}, {"ep_deb": 51}, {"ep_deb": 10 ** 400},
                                 {"ep_thr": 10 ** 400}, {"barge_thr": -(10 ** 400)},
                                 json.loads('{"ep_deb": NaN}'), json.loads('{"ep_deb": 1e400}'),
                                 json.loads('{"bc_thr": -1e400}')])
def test_bad_hello_is_refused(thr):
    with pytest.raises(server.FrameError):
        server.parse_hello({"type": "hello", "thresholds": thr}, server.DEFAULT_THRESHOLDS)


def test_defaults_come_from_the_upstream_ears_block(tmp_path):
    assert server.load_default_thresholds(tmp_path) == server.DEFAULT_THRESHOLDS      # no config: the fallback
    pytest.importorskip("yaml")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yaml").write_text("ears:\n  ep_thr: 0.8\n  barge_deb: 3\nllm: {}\n")
    got = server.load_default_thresholds(tmp_path)
    assert got["ep_thr"] == 0.8 and got["barge_deb"] == 3 and got["bc_thr"] == server.DEFAULT_THRESHOLDS["bc_thr"]


# ------------------------------------------------------------------ one conversation, no sockets

def test_conversation_replies_in_order_and_applies_hello_and_reset():
    async def go():
        svc = FakeSvc()
        conv = server.Conversation(svc, svc.open(**server.DEFAULT_THRESHOLDS), server.Stats())
        r0 = json.loads(await conv.on_hop(hop_frame(0)))
        r1 = json.loads(await conv.on_hop(hop_frame(1)))
        assert [r0["hop"], r1["hop"]] == [0, 1]
        assert r0["events"][0]["n"] == 1 and r1["events"][0]["n"] == 2          # processed in arrival order
        assert r1["events"][0]["mode"] == "speaking" and r0["hop_ms"] >= 0 and "error" not in r0
        conv.on_text(json.dumps({"type": "hello", "thresholds": {"barge_thr": 0.3}}), server.DEFAULT_THRESHOLDS)
        assert conv.session.barge_thr == 0.3 and conv.session.ep_thr == server.DEFAULT_THRESHOLDS["ep_thr"]
        conv.on_text('{"type":"reset"}', server.DEFAULT_THRESHOLDS)
        assert conv.session.resets == 1
        for bad in ("not json", "[]", '{"type":"nope"}'):
            with pytest.raises(server.FrameError):
                conv.on_text(bad, server.DEFAULT_THRESHOLDS)
    asyncio.run(go())


def test_an_engine_error_keeps_the_conversation_going():
    async def go():
        svc = FakeSvc(fail=True)
        conv = server.Conversation(svc, svc.open(**server.DEFAULT_THRESHOLDS), server.Stats())
        r = json.loads(await conv.on_hop(hop_frame()))
        assert r["events"] == [] and r["error"] == "RuntimeError" and r["hop"] == 0
    asyncio.run(go())


def test_stats_flush_reports_count_and_percentiles_once():
    s = server.Stats()
    assert s.flush() is None
    for ms in range(1, 101):
        s.add(float(ms))
    n, p50, p90 = s.flush()
    assert n == 100 and 50 <= p50 <= 51 and 90 <= p90 <= 91 and s.flush() is None


# ------------------------------------------------------------------ idle exit

def test_idle_clock_counts_from_the_last_conversation():
    async def go():
        t = [100.0]
        idle = server.Idle(10, clock=lambda: t[0])
        assert idle.remaining() == 10
        t[0] = 104
        idle.enter()
        assert idle.remaining() is None                    # connected: cannot fire
        t[0] = 500
        idle.leave()
        assert idle.remaining() == 10                      # counted from the disconnect
        t[0] = 507
        assert idle.remaining() == 3
        t[0] = 600
        assert idle.remaining() == 0
        assert server.Idle(None).remaining() is None
    asyncio.run(go())


def test_idle_watch_fires_only_after_the_limit_with_no_conversation():
    async def go():
        idle, stop = server.Idle(0.4), asyncio.Event()
        watcher = asyncio.ensure_future(server.idle_watch(idle, stop))
        await asyncio.sleep(0.2)
        idle.enter()                                        # a conversation holds the timer off
        await asyncio.sleep(0.6)
        assert not stop.is_set()
        t0 = asyncio.get_running_loop().time()
        idle.leave()
        await asyncio.wait_for(stop.wait(), 2)
        assert 0.3 < asyncio.get_running_loop().time() - t0 < 1.0
        await watcher
    asyncio.run(go())


# ------------------------------------------------------------------ socket activation

def test_activated_socket_needs_the_matching_pid_and_a_fd():
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen()
    try:
        assert server.activated_socket({}, ls.fileno()) is None
        assert server.activated_socket({"LISTEN_PID": "1", "LISTEN_FDS": "1"}, ls.fileno()) is None
        assert server.activated_socket({"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "0"}, ls.fileno()) is None
        env = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1", "LISTEN_FDNAMES": "x"}
        got = server.activated_socket(env, os.dup(ls.fileno()))
        assert got is not None and got.getsockname() == ls.getsockname() and env == {}   # env consumed, as sd does
        got.close()
    finally:
        ls.close()


# ------------------------------------------------------------------ over a real socket (needs aiohttp)

def run_server(svc, **kw):
    """serve() in a task; yields (task, port)."""
    pytest.importorskip("aiohttp")
    return asyncio.ensure_future(server.serve(svc, INFO, server.DEFAULT_THRESHOLDS, "127.0.0.1", kw.pop("port", 0), **kw))


def listening_socket():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s, s.getsockname()[1]


def test_slot_refusal_and_health_and_roundtrip():
    aiohttp = pytest.importorskip("aiohttp")

    async def go():
        svc = FakeSvc(slots=1)
        sock, port = listening_socket()
        task = run_server(svc, sock=sock)
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as http:
            async with http.get(base + "/health") as r:
                h = await r.json()
            assert h == {"slots": {"total": 1, "used": 0, "free": 1}, "device": "fake", "warm": True,
                         "bundle_sha": "b", "upstream_sha": "u"}
            async with http.ws_connect(base + "/v1/floor") as ws1:
                await ws1.send_str(json.dumps({"type": "hello", "thresholds": {"ep_thr": 0.5}}))
                await ws1.send_bytes(hop_frame(0))
                reply = json.loads((await ws1.receive()).data)
                assert reply["hop"] == 0 and reply["events"][0]["kind"] == "FRAME"
                async with http.get(base + "/health") as r:
                    assert (await r.json())["slots"] == {"total": 1, "used": 1, "free": 0}
                async with http.ws_connect(base + "/v1/floor") as ws2:       # the house is full
                    msg = await ws2.receive()
                    assert msg.type == aiohttp.WSMsgType.CLOSE and msg.data == 1013
                    assert "slots are busy" in msg.extra
                await ws1.send_bytes(hop_frame(0)[:100])                      # a malformed hop ends that conversation
                msg = await ws1.receive()
                assert msg.type == aiohttp.WSMsgType.CLOSE and msg.data == 1003 and "bytes" in msg.extra
            await asyncio.sleep(0.1)
            assert svc.active() == 0                                          # the slot came back
        task.cancel()
    asyncio.run(go())


def test_serves_on_the_systemd_socket_and_exits_after_idle():
    aiohttp = pytest.importorskip("aiohttp")

    async def go():
        sock, port = listening_socket()
        env = {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "1"}
        act = server.activated_socket(env, os.dup(sock.fileno()))              # the fd 3 path, on a different fd
        sock.close()
        loop = asyncio.get_running_loop()
        task = run_server(FakeSvc(), sock=act, idle_exit_s=1.0)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://127.0.0.1:{port}/v1/floor") as ws:
                await asyncio.sleep(1.6)                                       # connected: must not exit
                assert not task.done()
                t0 = loop.time()
            await asyncio.wait_for(task, 5)                                    # disconnected: exits ~1 s later
        assert 0.7 < loop.time() - t0 < 2.5
    asyncio.run(go())


# ------------------------------------------------------------------ run.sh preload logic (fake venv python)

@pytest.fixture
def fake_home(tmp_path):
    """A TYGER_HOME whose venv python reports what run.sh gave it instead of starting a server."""
    py = tmp_path / "home" / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/usr/bin/env bash\nif [ "$1" = -c ]; then echo "${FAKE_ROCM:-0}"; exit 0; fi\n'
                  'echo "LD_PRELOAD=[${LD_PRELOAD:-}]"; echo "ARGS=$*"\n')
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return tmp_path / "home"


def run_sh(home, **env):
    e = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "TYGER_ROCR_PRELOAD")}
    e.update(TYGER_HOME=str(home), **env)
    return subprocess.run(["bash", str(HERE / "run.sh"), "--port", "1"], env=e, capture_output=True, text=True)


def test_preload_applies_only_when_every_named_file_exists(fake_home, tmp_path):
    a, b = tmp_path / "a.so", tmp_path / "b.so"
    a.write_text(""), b.write_text("")
    r = run_sh(fake_home, TYGER_ROCR_PRELOAD=f"{a} {b}")
    assert r.returncode == 0 and f"LD_PRELOAD=[{a} {b}]" in r.stdout and "WARNING" not in r.stderr
    assert "--home" in r.stdout and "--port 1" in r.stdout
    r = run_sh(fake_home, TYGER_ROCR_PRELOAD=f"{a} {tmp_path}/missing.so")
    assert r.returncode == 0, "a bad preload must not stop the launch"
    assert "LD_PRELOAD=[]" in r.stdout and "WARNING" in r.stderr and "missing.so" in r.stderr


def test_rocm_without_a_preload_warns_once_and_cuda_does_not(fake_home):
    r = run_sh(fake_home, FAKE_ROCM="1")
    assert r.returncode == 0 and "LD_PRELOAD=[]" in r.stdout and r.stderr.count("WARNING") == 1
    r = run_sh(fake_home, FAKE_ROCM="0")
    assert r.returncode == 0 and r.stderr == ""


def test_run_sh_without_an_install_says_so(tmp_path):
    r = run_sh(tmp_path / "nothing")
    assert r.returncode == 1 and "install.sh" in r.stderr


# ------------------------------------------------------------------ GPU tests

def tyger_home():
    if os.environ.get("TYGER_HOME"):
        return Path(os.environ["TYGER_HOME"]).expanduser()
    prefix = Path(sys.prefix)
    if (prefix.parent / "tyger-s2s").is_dir():
        return prefix.parent
    return Path.home() / "patchbay-tyger"


@pytest.fixture(scope="module")
def engine_service():
    """The real engine behind upstream's EarsService, 2 slots, in this process."""
    home = tyger_home()
    if not (home / "bundle" / "config.json").is_file() or not (home / "tyger-s2s").is_dir():
        pytest.skip(f"no tyger install at {home} (install.sh)")
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("no GPU")
    svc, info, defaults = server.build_service(home, 2, warm=False)
    return svc, info, defaults


def clip(pattern):
    d = os.environ.get("TYGER_CLIPS", "/models/scratch/tyger/clips")
    hits = sorted(glob.glob(os.path.join(d, pattern)))
    if not hits:
        pytest.skip(f"local clips absent ({d}/{pattern}); set TYGER_CLIPS")
    return np.load(hits[0])


def to_i16(x):
    return np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")


def scenario_hops():
    """Listen: speech, a pause (endpoint), speech, a pause. Speaking: an agent clip with the user breaking in. Listen
    again. Returns [(mode byte, user int16, agent int16)] on the 320 ms grid."""
    sil = lambda s: np.zeros(int(16000 * s), np.float32)
    ryan, aiden, vivian = clip("seg_ryan_14f8*"), clip("seg_aiden_8b55*")[:48000], clip("seg_vivian_b9d9*")
    listen1 = np.concatenate([sil(0.6), ryan, sil(2.5)])
    agent = clip("seg_serena_67cc*")[:96000]
    speak_user = np.concatenate([sil(2.0), aiden, sil(1.0)])[:len(agent)]
    speak_user = np.pad(speak_user, (0, len(agent) - len(speak_user)))
    listen2 = np.concatenate([sil(0.4), vivian, sil(2.5)])
    hops = []

    def grid(u, a, mode):
        n = len(u) // HOP
        return [(mode, to_i16(u[i * HOP:(i + 1) * HOP]), to_i16(a[i * HOP:(i + 1) * HOP])) for i in range(n)]
    hops += grid(listen1, np.zeros_like(listen1), 0)
    hops += grid(speak_user, agent, 1)
    hops += grid(listen2, np.zeros_like(listen2), 0)
    return hops


def direct_events(svc, hops, thr):
    """The reference: EarsEngine.process one hop at a time, finals transcribed as service._tick does."""
    import time
    from app.plugins.ears.tyger.session import EarsSession
    eng = svc.engine
    sess, out = EarsSession(0, **thr), []
    for mode, u, a in hops:
        events, finals = eng.process([(sess, u.astype(np.float32) / 32768.0, a.astype(np.float32) / 32768.0,
                                       "speaking" if mode else "listen")])[sess]
        for ev, wav in finals:
            txt = eng.asr.transcribe([wav])[0]
            deadline = time.time() + 10
            while any(t is None for t in ev.get("seg_texts", [])) and time.time() < deadline:
                time.sleep(0.01)
            ev["transcript"] = " ".join(t for t in [*ev.pop("seg_texts", []), txt] if t)
        out.append(events)
    eng.head.reset_slot(0)
    return out


def served_events(engine_service, hops, perturb=None):
    """The same hops through the WebSocket service."""
    aiohttp = pytest.importorskip("aiohttp")
    svc, info, defaults = engine_service

    async def go():
        sock, port = listening_socket()
        task = asyncio.ensure_future(server.serve(svc, info, defaults, "127.0.0.1", 0, sock=sock))
        out = []
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(f"http://127.0.0.1:{port}/v1/floor") as ws:
                await ws.send_str(json.dumps({"type": "hello", "thresholds": {}}))
                for i, (mode, u, a) in enumerate(hops):
                    if i == perturb:
                        u = np.roll(u, 777) // 3 + 11                      # a different hop of the same size
                    await ws.send_bytes(hop_frame(mode, u, a))
                    out.append(json.loads((await ws.receive()).data))
        task.cancel()
        return out
    return asyncio.run(go())


def comparable(per_hop):
    """The deterministic part of each hop's events. PARTIAL events and BACKCHANNEL's `partial` text come from the ASR
    thread asynchronously (engine._finish_partials), so what they hold at a given hop depends on timing, not on the
    audio; everything else (including ENDPOINT, whose hop waits for its transcript) is a function of the hops."""
    return [[{k: v for k, v in e.items() if not (e["kind"] == "BACKCHANNEL" and k == "partial")}
             for e in events if e["kind"] != "PARTIAL"] for events in per_hop]


def endpoint_transcripts(per_hop):
    return [e["transcript"] for events in per_hop for e in events if e["kind"] == "ENDPOINT"]


def replay(engine_service, perturb=None):
    svc, info, defaults = engine_service
    hops = scenario_hops()
    ref = comparable(direct_events(svc, hops, defaults))
    got = served_events(engine_service, hops, perturb)
    assert [g["hop"] for g in got] == list(range(len(hops)))
    return hops, ref, comparable([g["events"] for g in got])


@pytest.mark.gpu
def test_replay_equivalence_server_matches_a_direct_engine_run(engine_service):
    perturb = os.environ.get("TYGER_TEST_PERTURB_HOP")            # for the negative control only
    hops, ref, got = replay(engine_service, int(perturb) if perturb else None)
    kinds = {e["kind"] for events in ref for e in events}
    assert "ENDPOINT" in kinds, "the fixture must produce an endpoint, or this test proves little"
    assert any(e.get("transcript") for events in ref for e in events if e["kind"] == "ENDPOINT")
    assert len(got) == len(ref) == len(hops)
    assert endpoint_transcripts(got) == endpoint_transcripts(ref), "ENDPOINT transcripts differ (finals are awaited)"
    for i, (r, g) in enumerate(zip(ref, got)):
        assert g == r, f"hop {i}: server events differ from the direct engine run"


@pytest.mark.gpu
def test_replay_detects_a_perturbed_hop(engine_service):
    hops, ref, got = replay(engine_service, perturb=10)
    assert got != ref, "a perturbed hop produced identical events: the replay comparison cannot fail"
