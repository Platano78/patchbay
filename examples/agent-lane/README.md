# agent-lane reference server

A runnable, dependency-light (stdlib-only) implementation of the cockpit's
delegate/status/approve contract — see [`docs/agent-lane.md`](../../docs/agent-lane.md)
for the full spec. This is a **reference/test server, not a real agent**: no
auth hardening beyond an optional bearer-token check, no persistence, no
rate limiting. Loopback only — do not expose it beyond your own machine.

```bash
python3 reference_server.py --seed-approval "Restart the router?"
```

Binds `HERMES_SHIM_URL`'s and `HERMES_MCP_URL`'s default ports (8087, 8088)
so it works with the cockpit's env defaults unchanged. See *Verify your
wiring* in `docs/agent-lane.md` for the full round-trip walkthrough.

Run `python3 reference_server.py --help` for all flags.
