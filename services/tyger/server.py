#!/usr/bin/env python3
"""patchbay-tyger: a WebSocket wrapper around the tyger-s2s ears runtime (pinned, imported, not vendored).

One WebSocket connection on /v1/floor is one conversation and takes one engine slot. The client sends aligned 320 ms
hops, the service answers each with the events the tyger turn rules produce. The service decides nothing about policy:
thresholds come from the client's hello frame. See README.md for the protocol.

The wire-level pieces (frames, thresholds, idle timer, socket activation) need neither torch nor a GPU, so they are
testable anywhere; the engine is only imported by build_service().
"""
import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("tyger-sidecar")

HOP_N = 5120                      # samples per hop: 320 ms at 16 kHz (checked against upstream at engine build)
FRAME_BYTES = 1 + 2 * HOP_N * 2   # mode byte + int16 user + int16 agent
MODES = {0: "listen", 1: "speaking"}
WINDOW_HOPS = 25                  # the engine's rolling window is 8 s = 25 hops

# Fallback defaults; the real ones are read from the pinned upstream's config/default.yaml (ears block).
DEFAULT_THRESHOLDS = {"ep_thr": 0.915, "ep_deb": 2, "barge_thr": 0.4, "barge_deb": 2, "bc_thr": 0.4, "bc_deb": 2}
_THR_TYPES = {"ep_thr": float, "barge_thr": float, "bc_thr": float, "ep_deb": int, "barge_deb": int, "bc_deb": int}


class FrameError(ValueError):
    """A client message the protocol cannot accept; the connection is closed with this text as the reason."""


# ---------------------------------------------------------------- protocol (no torch, no aiohttp)

def load_default_thresholds(upstream_dir):
    """The ears block of the pinned upstream's config/default.yaml over DEFAULT_THRESHOLDS."""
    thr = dict(DEFAULT_THRESHOLDS)
    cfg = Path(upstream_dir) / "config" / "default.yaml"
    if not cfg.is_file():
        return thr
    import yaml
    ears = (yaml.safe_load(cfg.read_text()) or {}).get("ears") or {}
    for k, typ in _THR_TYPES.items():
        if k in ears:
            thr[k] = typ(ears[k])
    return thr


def parse_hello(msg, defaults):
    """{"type":"hello","thresholds":{...}} -> the session's thresholds (defaults overridden by the given keys)."""
    given = msg.get("thresholds", {})
    if not isinstance(given, dict):
        raise FrameError("hello.thresholds must be an object")
    thr = dict(defaults)
    for k, v in given.items():
        if k not in _THR_TYPES:
            raise FrameError(f"unknown threshold {k!r} (known: {', '.join(sorted(_THR_TYPES))})")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FrameError(f"threshold {k} must be a number")
        typ = _THR_TYPES[k]
        lo, hi = (0.0, 1.0) if typ is float else (1, 50)
        if not lo <= v <= hi:                       # range first: also rejects NaN, inf and huge ints before any cast
            raise FrameError(f"threshold {k} must be within {lo}..{hi}")
        if typ is int and v != int(v):
            raise FrameError(f"threshold {k} must be a whole number")
        thr[k] = typ(v)
    return thr


def parse_hop(data):
    """A binary frame -> (mode, user float32, agent float32), the int16 scaled as upstream's page sends it (/32768)."""
    if len(data) != FRAME_BYTES:
        raise FrameError(f"hop frame must be {FRAME_BYTES} bytes (1 mode + 2 x {HOP_N} int16), got {len(data)}")
    if data[0] not in MODES:
        raise FrameError(f"mode byte must be 0 (listen) or 1 (speaking), got {data[0]}")
    pcm = np.frombuffer(data, dtype="<i2", offset=1)
    return MODES[data[0]], pcm[:HOP_N].astype(np.float32) / 32768.0, pcm[HOP_N:].astype(np.float32) / 32768.0


def _jsonable(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


class Stats:
    """Per-hop latency, logged as one line a minute (counts and timings only, never audio or transcripts)."""
    def __init__(self):
        self.ms = []

    def add(self, ms):
        self.ms.append(ms)

    def flush(self):
        if not self.ms:
            return None
        a = np.asarray(self.ms)
        self.ms = []
        return len(a), float(np.percentile(a, 50)), float(np.percentile(a, 90))


class Idle:
    """Seconds since the last conversation ended (or since start), for --idle-exit-s."""
    def __init__(self, limit_s, clock=time.monotonic):
        self.limit, self.clock, self.n, self.since = limit_s, clock, 0, clock()
        self.changed = asyncio.Event()

    def enter(self):
        self.n += 1
        self.changed.set()

    def leave(self):
        self.n -= 1
        if self.n == 0:
            self.since = self.clock()
        self.changed.set()

    def restart(self):
        self.since = self.clock()
        self.changed.set()

    def remaining(self):
        """Seconds until the idle limit, or None if it cannot fire (no limit, or a conversation is connected)."""
        if self.limit is None or self.n:
            return None
        return max(0.0, self.since + self.limit - self.clock())


async def idle_watch(idle, stop):
    """Set `stop` once idle.remaining() reaches 0. Sleeps; no polling."""
    while True:
        rem = idle.remaining()
        if rem is not None and rem <= 0:
            stop.set()
            return
        idle.changed.clear()
        try:
            await asyncio.wait_for(idle.changed.wait(), rem)
        except asyncio.TimeoutError:
            pass


def activated_socket(environ=os.environ, first_fd=3):
    """The listening socket systemd passed (LISTEN_FDS/LISTEN_PID, sd_listen_fds protocol), or None."""
    if environ.get("LISTEN_PID") != str(os.getpid()) or int(environ.get("LISTEN_FDS", "0") or 0) < 1:
        return None
    for k in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        environ.pop(k, None)
    return socket.socket(fileno=first_fd)


class Conversation:
    """One WebSocket connection's state: the engine session, the hop counter."""
    def __init__(self, svc, session, stats):
        self.svc, self.session, self.stats, self.hop = svc, session, stats, 0

    def on_text(self, text, defaults):
        """-> reply dict or None. Raises FrameError."""
        try:
            msg = json.loads(text)
        except ValueError:
            raise FrameError("text frames must be JSON") from None
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            for k, v in parse_hello(msg, defaults).items():
                setattr(self.session, k, v)
            return None
        if kind == "reset":
            self.session.reset()
            return None
        raise FrameError(f"unknown message type {kind!r}")

    async def on_hop(self, data):
        """-> the JSON reply text for one binary hop. Raises FrameError."""
        mode, user, agent = parse_hop(data)
        t0 = time.perf_counter()
        try:
            events = await self.svc.feed(self.session, user, agent, mode)
            err = None
        except Exception as e:                                   # the engine logged it; the conversation goes on
            events, err = [], type(e).__name__
        ms = (time.perf_counter() - t0) * 1e3
        self.stats.add(ms)
        out = {"hop": self.hop, "events": events, "hop_ms": round(ms, 1)}
        if err:
            out["error"] = err
        self.hop += 1
        return json.dumps(out, default=_jsonable)


# ---------------------------------------------------------------- the aiohttp app

def make_app(svc, info, defaults, idle, stats):
    from aiohttp import WSMsgType, web

    async def health(request):
        used = svc.active()
        return web.json_response({"slots": {"total": svc.slots, "used": used, "free": svc.slots - used},
                                  "device": info["device"], "warm": info["warm"],
                                  "bundle_sha": info["bundle_sha"], "upstream_sha": info["upstream_sha"]})

    async def floor(request):
        ws = web.WebSocketResponse(max_msg_size=FRAME_BYTES + 1024, heartbeat=20)
        await ws.prepare(request)
        session = svc.open(**defaults)
        if session is None:
            await ws.close(code=1013, message=f"all {svc.slots} slots are busy".encode())
            return ws
        conv = Conversation(svc, session, stats)
        idle.enter()
        log.info("conversation open (%d/%d slots)", svc.active(), svc.slots)
        try:
            async for msg in ws:
                try:
                    if msg.type == WSMsgType.BINARY:
                        await ws.send_str(await conv.on_hop(msg.data))
                    elif msg.type == WSMsgType.TEXT:
                        conv.on_text(msg.data, defaults)
                    else:
                        break
                except FrameError as e:
                    await ws.close(code=1003, message=str(e).encode()[:120])
                    break
        finally:
            svc.close(session)
            idle.leave()
            log.info("conversation closed after %d hops", conv.hop)
        return ws

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/v1/floor", floor)
    return app


async def serve(svc, info, defaults, host, port, idle_exit_s=None, sock=None, stats_every_s=60.0):
    """Run until idle_exit_s elapses with no conversation connected (forever if None)."""
    from aiohttp import web
    idle, stats, stop = Idle(idle_exit_s), Stats(), asyncio.Event()
    runner = web.AppRunner(make_app(svc, info, defaults, idle, stats))
    await runner.setup()
    site = web.SockSite(runner, sock) if sock is not None else web.TCPSite(runner, host, port)
    await site.start()
    log.info("listening on %s (%d slots, idle exit %s)", "systemd socket" if sock else f"{host}:{port}",
             svc.slots, f"{idle_exit_s:g}s" if idle_exit_s else "off")

    async def report():
        while True:
            await asyncio.sleep(stats_every_s)
            s = stats.flush()
            if s:
                log.info("hops: n=%d hop_ms p50 %.0f p90 %.0f", *s)

    tasks = [asyncio.ensure_future(report())]
    if idle_exit_s is not None:
        idle.restart()
        tasks.append(asyncio.ensure_future(idle_watch(idle, stop)))
        await stop.wait()
        log.info("no conversation for %gs: exiting", idle_exit_s)
    else:
        await asyncio.Event().wait()
    for t in tasks:
        t.cancel()
    await runner.cleanup()


# ---------------------------------------------------------------- the real engine (imports torch)

def _git_sha(path):
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def warm_engine(engine, slots):
    """Run the encoder over every window length (1..25 hops) and batch size once, so the first conversation does not
    meet a cold shape (upstream I-27: cold hops reach 0.6-1.25 s). Leaves no state behind."""
    import torch
    from app.plugins.ears.tyger.session import EarsSession
    rng = np.random.default_rng(0)
    t0 = time.perf_counter()
    for w in range(1, WINDOW_HOPS + 1):
        for n in range(1, 2 * slots + 1):
            engine.asr.encode([(rng.standard_normal(w * HOP_N) * 0.01).astype(np.float32) for _ in range(n)])
    for b in range(1, slots + 1):                                 # the heads and the turn rules at each batch size
        sess = [EarsSession(k) for k in range(b)]
        for _ in range(3):
            engine.process([(s, (rng.standard_normal(HOP_N) * 0.01).astype(np.float32),
                             np.zeros(HOP_N, np.float32), "listen") for s in sess])
    engine.asr.transcribe([(rng.standard_normal(16000) * 0.01).astype(np.float32)])
    torch.cuda.synchronize()
    for k in range(slots):
        engine.head.reset_slot(k)
    log.info("encoder warm over %d shapes in %.1fs", WINDOW_HOPS * 2 * slots, time.perf_counter() - t0)


def build_service(home, slots, warm=True):
    """-> (EarsService, info dict, default thresholds). Needs the install made by install.sh."""
    home = Path(home).expanduser()
    upstream, bundle = home / "tyger-s2s", home / "bundle"
    if not (upstream / "app" / "plugins" / "ears" / "tyger").is_dir() or not (bundle / "config.json").is_file():
        sys.exit(f"{home}: no tyger install here (run services/tyger/install.sh first, or set TYGER_HOME)")
    sys.path.insert(0, str(upstream))
    import torch
    if not torch.cuda.is_available():
        sys.exit("torch sees no GPU (CUDA or ROCm): the tyger sidecar needs one with >= 4 GB free")
    torch.set_num_threads(4)                                      # upstream: the default (all cores) mostly spin-waits
    from app.plugins.ears.tyger.constants import HOP_N as UP_HOP_N
    from app.plugins.ears.tyger.engine import EarsEngine
    from app.plugins.ears.tyger.service import EarsService
    if UP_HOP_N != HOP_N:
        sys.exit(f"upstream hop is {UP_HOP_N} samples, this server speaks {HOP_N}: bump deliberately")
    t0 = time.perf_counter()
    engine = EarsEngine(bundle, slots=slots)
    log.info("engine loaded in %.1fs", time.perf_counter() - t0)
    if warm:
        warm_engine(engine, slots)
    svc = EarsService(engine, stats_every_s=60.0)
    svc.slots = slots
    backend = "ROCm" if torch.version.hip else "CUDA"
    info = {"device": f"{torch.cuda.get_device_name(0)} ({backend})", "warm": bool(warm),
            "bundle_sha": json.loads((bundle / "config.json").read_text()).get("weights_sha256", "unknown"),
            "upstream_sha": _git_sha(upstream)}
    return svc, info, load_default_thresholds(upstream)


def main(argv=None):
    ap = argparse.ArgumentParser(description="patchbay tyger sidecar: turn-taking decisions over WebSocket")
    ap.add_argument("--home", default=os.environ.get("TYGER_HOME", "~/patchbay-tyger"), help="install dir (install.sh)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address; the service has no authentication")
    ap.add_argument("--port", type=int, default=8797)
    ap.add_argument("--slots", type=int, default=int(os.environ.get("TYGER_SLOTS", "4")), help="concurrent conversations")
    ap.add_argument("--idle-exit-s", type=float, default=None, help="exit after this long with no conversation")
    ap.add_argument("--no-warm", action="store_true", help="skip the encoder pre-warm (development)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sock = activated_socket()
    svc, info, defaults = build_service(a.home, a.slots, warm=not a.no_warm)
    asyncio.run(serve(svc, info, defaults, a.host, a.port, a.idle_exit_s, sock))
    logging.shutdown()
    os._exit(0)          # CUDA/ROCm teardown with the engine's daemon threads can abort at normal exit (upstream I-15)


if __name__ == "__main__":
    main()
