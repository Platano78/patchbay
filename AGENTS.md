# patchbay — AGENTS.md (router / the map)

<!--
ICM per-project brain. Canonical standard: ~/project/_standards/icm/.
PUBLIC-REPO FILE: this ships to github.com/Platano78/patchbay.
Keep it free of host IPs, personal paths, and secrets — CONTEXT.md (git-excluded,
local-only) is where topology and deploy specifics live. Route to it; don't inline it.
-->

You are the generic agent. Reading this makes you the **patchbay** agent.
On entry: read this map → route to the area for the task → load ONLY that area's Inputs.

## What this is

Speech-to-speech voice agent + browser cockpit. `patches/` is the real source — a patch pack
applied *onto* an installed `speech_to_speech` package, not a standalone app; `webclient/` is
the browser UI it talks to over a WebSocket. Runs as systemd services on a LAN box.
**The patch-pack shape is the thing to internalise:** editing `patches/` changes nothing until
`patches/apply.sh` copies it into the live venv.

## Areas (route by task — load Inputs, skip the rest)

| If the task is about… | Read (Inputs) | Skip |
|---|---|---|
| Audio path — streaming, barge-in, echo, turn-taking | `patches/s2s_pipeline.py`, `patches/echo_gate.py`, `patches/websocket_streamer.py` | `webclient/`, `docs/`, `bench-wavs/` |
| Voice tools / brain + persona control | `patches/brain_control.py`, `patches/voice_tools.py`, `examples/tools/` | `webclient/`, audio-path files |
| Cockpit UI, avatar, themes | `webclient/index.html`, `webclient/serve.py`, `webclient/avatar/`, `webclient/themes/` | all of `patches/` |
| Deploy, services, rollback | `patches/apply.sh`, `systemd/*.service.template`, **`CONTEXT.md`** (local-only; holds hosts + deploy discipline) | source files — deploy is copy+restart, not a code change |
| Tests / verification | `patches/test_*.py` (10 files), `webclient/test_webclient.py` | everything else |
| Specs, contracts, prior research | `docs/plans/`, `docs/contracts/`, `docs/research/` | source; `docs/borrows/` unless the task names a borrow |

## Verbs
- `pickup`  → read `_pickup-handoff.md` §pickup, then route to the named area.
- `handoff` → read `_pickup-handoff.md` §handoff.

## Naming conventions (locate files, don't grep blindly)
- `patches/*.py` = the patch pack (the real source). Nothing here is live until `apply.sh` runs.
- `patches/test_<module>.py` = tests, paired 1:1 with the module they cover.
- `docs/plans/*_spec.md` = ratified specs · `docs/plans/backlog.md` = the queue.
- `*.service.template` = systemd units; the rendered copies live on the box, not in the repo.
- `CONTEXT.md` / `NEXT-SESSION.md` = **local-only, git-excluded.** Present in a WSL clone,
  absent from the public repo. Read CONTEXT.md first for anything host- or deploy-shaped.
- Skip by default: `bench-wavs/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`.

## Fallback law
Task not on this map → ask which area, or stay here. **Never wander the tree** or bulk-read
root docs. If this project is the wrong home → return to `../AGENTS.md` (workspace root).
