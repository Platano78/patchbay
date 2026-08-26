# agent-lane examples

## `verify_contract.py` — run this first

A standalone, stdlib-only contract checker for your own agent-lane server —
see [`docs/agent-lane.md`](../../docs/agent-lane.md) for the full spec.
Needs nothing installed: no pipeline, no venv, no third-party packages.

```bash
python3 verify_contract.py --shim-url http://localhost:8087/v1/chat/completions \
    --mcp-url http://localhost:8088/mcp
```

It replays the exact requests the shipped cockpit code makes (the
`tools/list` arming probe, delegation, and all four MCP tools) and reports
PASS/FAIL/WARN per check with exit code 0 only when every check passes.
Point it at your own server before wiring it into the cockpit at all — it
catches the same failures the pipeline would, without needing a pipeline.

## `reference_server.py`

A runnable, dependency-light (stdlib-only) implementation of the cockpit's
delegate/status/approve contract. This is a **reference/test server, not a
real agent**: no auth hardening beyond an optional bearer-token check, no
persistence, no rate limiting. Loopback only — do not expose it beyond your
own machine.

```bash
python3 reference_server.py --seed-approval "Restart the router?"
```

Binds `HERMES_SHIM_URL`'s and `HERMES_MCP_URL`'s default ports (8087, 8088)
so it works with the cockpit's env defaults unchanged. See *Verify your
wiring* in `docs/agent-lane.md` for the full round-trip walkthrough
(`verify_contract.py` is stage 1 of that; the sections below cover stage 2).

Run `python3 reference_server.py --help` for all flags.

## `../../patches/test_agent_lane_reference.py`

The pytest suite covering `reference_server.py` itself. It needs `pytest`,
`httpx`, and `pydantic` installed, and it must be run **from the repo
root**, not from inside `examples/agent-lane/` or `patches/` — it does
`from patches import hermes_cockpit`, which fails with `ModuleNotFoundError:
No module named 'patches'` unless the repo root is on `sys.path` (which
only happens when pytest is invoked from there):

```bash
cd <repo root>
python3 -m pytest patches/test_agent_lane_reference.py -q
```
