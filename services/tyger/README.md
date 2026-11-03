# tyger sidecar (optional)

A small WebSocket service that decides *whose turn it is*. The caller streams the user's microphone and the agent's
own voice in 320 ms steps; the service answers each step with turn-taking events: the user has finished (`ENDPOINT`,
with the transcript), the user is cutting in over the agent (`BARGE`), or a short acknowledgement fits (`BACKCHANNEL`).
It wraps the open-source [tyger-s2s](https://github.com/nineninesix-ai/tyger-s2s) "ears" runtime (a speech encoder plus
a small turn-taking network). Nothing in patchbay needs it: the cockpit works without it, and nothing in this directory
runs unless you install and start it.

**Requirements.** A GPU with CUDA or ROCm and about 4 GB free (measured: 2.8-4.0 GiB), Linux, git, Python 3.12 (the version tested; upstream targets it)
(upstream was tested on 3.12), about 3 GB of disk for the weights and packages, plus torch itself (several GB for a GPU wheel). It is not usable on CPU (about
550 ms per 320 ms step, measured). It runs in FP32; half precision produced NaNs.

## Install

You supply the GPU build of PyTorch. `install.sh` does not guess which one you have; run it with no options and it
prints these choices and creates nothing:

```bash
# NVIDIA: the CUDA wheel index for your driver, from https://pytorch.org/get-started/locally/
TORCH_INDEX_URL=<cuda wheel index> services/tyger/install.sh

# AMD: a ROCm build of torch
TORCH_INDEX_URL=<rocm wheel index> TORCH_SPEC='torch==<version>' services/tyger/install.sh

# you already have a venv with a working GPU torch (it is reused through a .pth file; the new venv is made from
# that venv's own interpreter so the Python versions agree)
TORCH_FROM_VENV=/path/to/venv services/tyger/install.sh
```

The optional argument is the install directory (default `~/patchbay-tyger`; it must not exist). The script:

1. creates a venv and puts torch in it, then checks that torch sees a GPU;
2. installs the pinned dependencies (`transformers==5.14.1`, `aiohttp`, `soundfile`, `librosa`, ...);
3. clones tyger-s2s at one pinned commit (`97d011dc`) into `DIR/tyger-s2s`. It is imported, never copied into this
   repo; bumping it is a deliberate edit of `UPSTREAM_SHA` in `install.sh`;
4. downloads the weights (`nineninesix/tyger`, no Hugging Face token needed) into `DIR/bundle`;
5. runs upstream's own validator (batched engine against a plain single-session reference) and requires
   `events match: True` for every session. The validator downloads a small public test dataset;
6. prints `TYGER OK` as its last line. Anything else is a failure; the output of step 5 is kept in `DIR/validate.log`.

Hugging Face downloads go to `DIR/hf` unless you set `HF_HOME`. Tested here with `TORCH_FROM_VENV` on a ROCm torch;
the CUDA path installs the same pinned packages but was not run on an NVIDIA machine by the author.

## Run

```bash
TYGER_HOME=~/patchbay-tyger services/tyger/run.sh            # 127.0.0.1:8797, 4 conversations at once
services/tyger/run.sh --port 8797 --slots 2 --idle-exit-s 300
curl -s http://127.0.0.1:8797/health
```

Startup loads the weights, then runs the encoder once over every window length and batch size (so the first
conversation does not meet a cold GPU kernel; without this the first 25 steps of a conversation take 0.6-1.2 s), then
listens. Expect 20-60 s.

| Option | Default | |
|---|---|---|
| `--host` | `127.0.0.1` | bind address; see *Who can connect* |
| `--port` | `8797` | |
| `--slots` (`TYGER_SLOTS`) | `4` | simultaneous conversations; each holds one engine slot |
| `--idle-exit-s N` | off | exit cleanly after N seconds with no conversation connected |
| `--no-warm` | | skip the pre-warm (development only) |

`/health` returns `{"slots":{"total":4,"used":0,"free":4},"device":"...","warm":true,"bundle_sha":"...","upstream_sha":"..."}`.

### On demand (socket activation)

`systemd/patchbay-tyger.socket.template` and `systemd/patchbay-tyger.service.template` are user-unit templates (not
installed by anything). The socket listens all the time at no cost; the first connection starts the service; the
service exits after `--idle-exit-s 300` with no conversation, releasing the GPU. If `LISTEN_FDS`/`LISTEN_PID` are set
the server serves on that inherited socket instead of binding its own. The first connection waits in the socket's queue
while the server starts. Render the templates as described in their headers.

## Protocol

`ws://HOST:PORT/v1/floor`. One connection is one conversation and takes one slot. When all slots are taken the server
accepts the WebSocket and closes it at once with code `1013` and the reason `all N slots are busy`.

Client to server:

| Frame | Meaning |
|---|---|
| text `{"type":"hello","thresholds":{...}}` | optional, any time; sets this conversation's thresholds. Keys: `ep_thr`, `barge_thr`, `bc_thr` (0..1) and `ep_deb`, `barge_deb`, `bc_deb` (debounce, in 80 ms frames, 1..50). Keys not given keep the defaults, which are the `ears:` block of the pinned upstream's `config/default.yaml`. |
| binary hop, 20481 bytes | `mode` byte (`0` listen, `1` speaking) + user `int16[5120]` + agent `int16[5120]`, little-endian, 16 kHz mono, 320 ms. `mode` is yours to set: `1` while the agent's voice is playing, never a guess by the model. The agent half is the audio the agent is playing, aligned to the user's; zeros while it is silent. |
| text `{"type":"reset"}` | start a new turn (applied before the next hop) |

Server to client: one text frame per hop, in order,

```json
{"hop": 12, "events": [{"kind": "FRAME", "state": "LISTEN", "yield_prob": 0.1, "hazard": 0.0, "bc_prob": 0.2, "p_end": 0.0}], "hop_ms": 61.3}
```

`hop` counts hops since the connection opened. `events` are exactly what the upstream engine produces for that hop:
four `FRAME` events (one per 80 ms) plus, when they fire, `ENDPOINT` (`transcript`, `turn_s`, `p_end`), `BARGE`
(`yield_prob`, `onset_s`), `BACKCHANNEL` (`partial`, `bc_prob`, `turn_s`), `PARTIAL` (`text`, the user's words so far).
An `ENDPOINT` hop is answered once its transcript is ready, so that reply takes longer. If the engine fails on a hop
the reply has `"events":[]` and an `"error"` name, and the conversation continues. A malformed frame closes the
connection with code `1003` and the reason in the close frame.

Hops that arrive faster than they are processed are queued and answered in order. A client that sends faster than real
time simply gets its answers faster than real time; one that is slower than 320 ms per hop starves the turn rules.

The server never writes audio to disk, and its log holds counts and timings only (no audio, no transcripts): a line per
conversation and one `hop_ms p50/p90` line per minute while hops flow.

## ROCm idle spin (AMD only, optional)

On the author's machine (an integrated Radeon, gfx1151; a nightly ROCm 7.14 torch wheel with the system ROCm runtime
at 1.18) the idle process held 100% of one CPU core: a spin in the ROCm runtime (TheRock issue 7051), not in this
service. Preloading the *system* runtime together with a small stub library made it 0.0% when idle, with unchanged hop
latency. The stubs are 15 functions the wheel's newer runtime knows and the system's older one lacks
(`rocm/hsa_shim.c`; each returns an error status; nothing was observed calling them, but that was not checked
exhaustively). Under sustained 4-conversation load the process still used about 15% of a core.

```bash
gcc -shared -fPIC -o libhsa_shim.so services/tyger/rocm/hsa_shim.c
TYGER_ROCR_PRELOAD="$PWD/libhsa_shim.so /usr/lib/x86_64-linux-gnu/libhsa-runtime64.so.1.18.0" services/tyger/run.sh
```

`run.sh` applies the preload only when `TYGER_ROCR_PRELOAD` is set and every file it names exists. Otherwise it starts
anyway, and on a ROCm torch it prints one `WARNING`. This is specific to one machine and one wheel; do not expect the
library name to match yours. NVIDIA users need none of it. With socket activation the spin only exists while a
conversation (or the idle timeout) is running.

## Who can connect

There is no authentication. Anyone who can reach the port can stream audio in and read the decisions and transcripts
out, and can occupy all the slots. The default bind is `127.0.0.1`. Use `--host` with a LAN address only when the
caller is on another machine, only on a network you trust, and prefer a firewall rule or a VPN over `0.0.0.0`. (The
socket template binds `127.0.0.1` too.)

## Tests

```bash
python3 -m pytest services/tyger/ -q -rs                         # no torch or GPU needed; some tests skip without aiohttp
TYGER_HOME=~/patchbay-tyger ~/patchbay-tyger/venv/bin/python -m pytest services/tyger/ -q -rs   # all of it
```

The protocol, thresholds, slot refusal, idle exit, socket activation and `run.sh` preload logic are tested without a
GPU. With a GPU and the install, a replay test sends recorded hops through the server and requires the same events as
a direct `EarsEngine.process` run, and a companion test proves that perturbing one hop breaks that equality. The replay
audio is not in the repo: point `TYGER_CLIPS` at a directory of 16 kHz float32 `.npy` clips (names in
`test_server.py`), or the test skips.

## Licences

The TYGER weights are under OpenMDW-1.1; they are downloaded by `install.sh`, not redistributed here. The tyger-s2s
runtime is Apache-2.0 and is cloned, not copied. This directory's code is the repository's licence.
