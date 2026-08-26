# Agent lane — delegate / status / approve

The cockpit's delegate/status/approve lane (voice tools `delegate_to_hermes`,
`hermes_status`, `send_to_hermes` + the hold-to-approve gate in the webclient)
talks to a background agent over two plain HTTP surfaces. Nothing in the
cockpit code is specific to any one agent — the `HERMES_*` env var names are
shipped API and stay as-is, but they name a **contract**, not a product.
Implement this contract and any agent (yours, not the maintainer's) plugs in.

Authority for this document is `patches/hermes_cockpit.py` and
`patches/voice_tools.py`; a runnable reference implementation of both
surfaces is in `examples/agent-lane/reference_server.py`.

## The two endpoints

| | Env var | Default | Shape |
|---|---|---|---|
| Delegation | `HERMES_SHIM_URL` | `http://localhost:8087/v1/chat/completions` | OpenAI-compatible chat completions |
| Cockpit brain | `HERMES_MCP_URL` | `http://localhost:8088/mcp` | MCP over JSON-RPC 2.0, four tools |

If neither answers, the three agent voice tools simply stay unarmed at
pipeline startup — that's the normal, expected state for most self-hosters,
not an error condition. Nothing else in the pipeline depends on them.

**Before either endpoint is called for real, `HERMES_MCP_URL` must also
answer a `tools/list` probe — see the next section. This is a separate,
required surface, not a suggestion.**

## A0) The arming probe — `tools/list`, required

At pipeline startup, `patches/voice_tools.py`'s `get_tool_defs()` sends this
bare request to `HERMES_MCP_URL` once, with **no `initialize` first**:

```json
POST <HERMES_MCP_URL>
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
```

with a **1.5 second timeout**. The acceptance rule (`_probe()`,
`patches/voice_tools.py:361-377`) is exact:

- HTTP status must be 2xx, **and**
- if the body parses as JSON, it must have **no top-level `"error"` key**.
- A 2xx response with a **non-JSON body counts as success** — deliberately
  lenient, but don't rely on it; answer with a real `tools/list` result.
- Any exception (refused connection, timeout, DNS failure, etc.) counts as
  failure.

If this probe fails, `delegate_to_hermes`, `hermes_status`, and
`send_to_hermes` are **silently never armed** for the lifetime of that
pipeline run — no error is shown to the user, nothing retries, and the
tools simply aren't in the model's tool list. The only visible evidence is
one INFO log line at pipeline startup:

```
voice_tools: armed N/M (probe: qmd=... hermes=down genesis=... faulkner=...)
```

If your agent isn't arming, grep the pipeline log for `voice_tools: armed`
and check whether it says `hermes=up` or `hermes=down` — that is the
authoritative answer to "did my server pass the probe." Run
`examples/agent-lane/verify_contract.py` against your server directly
(see *Verify your wiring* below) to check this without needing a running
pipeline at all.

### The other two timeouts — design constraints on your server

The arming probe's 1.5s isn't the only budget your server has to meet:

- **Every MCP `tools/call`** (`events_poll`, `permissions_list_open`,
  `permissions_respond`, `messages_send`) has a **5.0 second timeout**
  (`_MCP_TIMEOUT_S`, `patches/hermes_cockpit.py:30`). A server that only
  answers after 5s+ is broken in production even if the answer is
  otherwise correct — the cockpit sees the call as a total failure.
- **Delegation** (`HERMES_SHIM_URL`) gets a much longer leash: **up to 900
  seconds (15 minutes)** (`_SHIM_TIMEOUT_S`, `patches/hermes_cockpit.py:40`),
  since a delegated task is expected to be long-running.

`examples/agent-lane/verify_contract.py` checks MCP calls against the real
5.0s budget. It does **not** wait the full 900s for delegation by default
(that would make an interactive check unusable) — see *Verify your
wiring* below for how it handles that.

## A) Delegation — `HERMES_SHIM_URL`

Plain OpenAI chat-completions. The cockpit sends:

```json
POST /v1/chat/completions
Authorization: Bearer <token>
{"model": "<HERMES_SHIM_MODEL, unused by you>", "messages": [{"role": "user", "content": "<the task text>"}]}
```

and reads the response as `choices[0].message.content`. That's the entire
contract — no streaming, no function calling, no system message. The
request has no fixed timeout under 15 minutes on the cockpit side, since a
delegated task is expected to be long-running.

The bearer token is read **lazily, at call time**, from
`HERMES_SHIM_TOKEN_FILE` (default `~/.hermes/shim.env`), an env-style file
containing a line `HERMES_SHIM_TOKEN=<value>`. If the file is missing or
unreadable, delegation fails closed with "Hermes delegation token is
unavailable" — the cockpit never delegates without a token.

## B) Cockpit brain — `HERMES_MCP_URL`, four tools

Called as standard MCP `tools/call`. Exact names and argument shapes:

### `events_poll({after_cursor, limit})`

```json
{"events": [...], "next_cursor": <opaque cursor>}
```

**Event *content* is deliberately never read.** The cockpit only advances
its cursor from `next_cursor`; delegation progress comes from the shim
response and lifecycle, not from events. A reference server can (and does)
always return `events: []` and just increment the cursor.

### `permissions_list_open({})`

```json
{"approvals": [ {"id": "...", ...} ]}
```

Each approval needs an `id`. Its human-readable label is the first
present of, in order: `summary`, `title`, `description`, `tool`, `action`
— else the whole object is JSON-stringified (`webclient/index.html`'s
`permissionSummary()`). If your agent's approval objects don't already have
one of those keys, add a `summary` field.

### `permissions_respond({id, decision})`

`decision` is exactly `"allow-once"` or `"deny"` — no other values. On an
unknown `id`, report the failure by returning `{"error": "..."}` inside the
unwrapped result payload (not an MCP-transport-level `isError`) — the
cockpit checks both, but Hermes' own server reports `isError: false` even
for an unknown id and puts the real failure inside the payload.

### `messages_send({target, message})`

`target` is `"platform:chat_id"` — whatever your agent's own
`channels_list`-style addressing uses. `HERMES_TARGET` supplies this string;
the cockpit never constructs or validates it.

## C) Result unwrapping

The cockpit accepts any of these three shapes for a `tools/call` result and
tries them in order:

1. `structuredContent.result` as a **JSON string** (Hermes' own shape — it
   wraps the payload as a string, not a nested object);
2. `content[0]` where `content[0].type == "text"`, and `content[0].text` is
   a **JSON string** of the payload;
3. the raw result object itself, unmodified.

**A reference/new server should use shape 2** — it's the plain MCP
convention and what `examples/agent-lane/reference_server.py` does. Shape 1
exists only because Hermes' own server happens to do it that way.

## D) The two real call paths against `HERMES_MCP_URL`

Two different pieces of cockpit code call your MCP endpoint, and neither of
them sends an `initialize` handshake **today**:

- The **startup arming probe** (`patches/voice_tools.py`'s
  `get_tool_defs()`, described in A0 above) sends a bare `tools/list`, no
  handshake, no session id.
- **Every real delegate/status/approve round trip** goes through
  `patches/hermes_cockpit.py::_mcp_call`, which posts `tools/call`
  **directly** — also no `initialize` handshake and no session id.

**No shipped code path sends `initialize` to `HERMES_MCP_URL` today.** Your
server should still *tolerate* an `initialize` call for forward
compatibility — a future client, or someone else's MCP-conformant tooling
pointed at the same URL, may send one — and it's fine to hand back a
session id in response. But it must never be *required*: nothing that
talks to you today sends one on either path that actually matters, so a
server that insists on `initialize` before answering `tools/list` or
`tools/call` will simply never work with this cockpit.

## E) The HTTP-status contract

Both call sites — `hermes_cockpit.py::delegate()`'s POST to
`HERMES_SHIM_URL`, and `hermes_cockpit.py::_mcp_call`'s POST to
`HERMES_MCP_URL` — call `resp.raise_for_status()` **before touching the
response body at all**. A non-2xx status is treated as a total failure and
the body is **never read**, no matter what's in it — including a
well-formed JSON-RPC error object.

**Always answer both endpoints with HTTP 200.** Put JSON-RPC errors and
tool-call errors in the response *body* (a top-level `"error"` key for a
JSON-RPC-level failure, or `{"error": "..."}` inside the unwrapped
`tools/call` payload for a tool-level failure — see section C and
`permissions_respond` above). Returning a REST-style error status (4xx/5xx)
for anything other than a genuine transport failure will make the cockpit
treat a well-formed error response as if your server were unreachable.

## When the agent is absent

If `HERMES_SHIM_URL` / `HERMES_MCP_URL` don't answer at pipeline startup,
`delegate_to_hermes`, `hermes_status`, and `send_to_hermes` are simply left
unarmed (probed once at startup — see the README's *Voice tools* section).
This is the default, normal state for most installs, not a degraded one.

## Env vars

```bash
export HERMES_SHIM_URL="http://localhost:8087/v1/chat/completions"
export HERMES_SHIM_TOKEN_FILE="$HOME/.hermes/shim.env"   # contains: HERMES_SHIM_TOKEN=<value>
export HERMES_MCP_URL="http://localhost:8088/mcp"
export HERMES_TARGET="example:1234567890"                # your agent's own platform:chat_id
```

## Verify your wiring

### Stage 1 — check your server against the contract directly, no pipeline needed

`examples/agent-lane/verify_contract.py` is a standalone, stdlib-only
Python 3 script that replays every request shape above (the `tools/list`
arming probe, delegation, and all four MCP tools, unwrapped exactly the way
`hermes_cockpit.py` unwraps them) against your server and reports
PASS/FAIL/WARN per check. It needs nothing installed — no pipeline, no
venv, no dependencies beyond the Python 3 standard library.

```bash
python3 examples/agent-lane/verify_contract.py \
    --shim-url http://localhost:8087/v1/chat/completions \
    --mcp-url http://localhost:8088/mcp
```

Run this first, against your own server, before wiring it into the cockpit
at all. It is the executable form of this whole document — if it doesn't
pass, the pipeline's arming probe and delegate/status/approve round trip
won't work either, and this script tells you exactly which check failed
and why, instead of a silent "the tools never armed."

Each check uses the **real production timeout** for that call (1.5s
arming probe, 5.0s MCP tools) — a PASS means your server meets the actual
budget, not just "eventually answers correctly." Delegation is the one
exception: the cockpit allows up to 900s for it, but this script only
waits 30s by default (`--delegation-timeout` to raise it) and reports a
**WARN, not a FAIL** if it times out at that shorter wait — a slow
delegation isn't necessarily broken, the checker just can't sit for
fifteen minutes to find out.

**If you aren't implementing this in Python:** the contract is plain HTTP
and JSON — nothing in it is Python-specific. Read `verify_contract.py`
as the executable spec and re-implement the same requests/assertions
against your server in whatever language you're using; a server that
passes it in spirit will work with the cockpit regardless of what wrote it.

### Stage 2 — the full pipeline round trip (optional second stage)

Once stage 1 passes, confirm the same server works from inside a real
cockpit pipeline:

1. `python3 examples/agent-lane/reference_server.py --seed-approval "Restart the router?"`
   — starts both endpoints on their defaults, with one pending approval.
   (Substitute your own server if you're checking that instead.)
2. Create `~/.hermes/shim.env` containing a line `HERMES_SHIM_TOKEN=anything`
   (the reference server doesn't check it unless started with `--token`).
3. Restart the pipeline (arming is probed once at startup).
4. Say "delegate clean up the kitchen" — the cockpit should hand off, and
   within a few seconds speak/show "I worked on: clean up the kitchen".
5. Within ~10s (idle poll interval) the seeded approval should appear in
   the cockpit's hold-to-approve gate. Hold to approve it — the reference
   server's log line (`permissions_respond: appr-1 -> allow-once`) and the
   gate clearing are the round trip working end to end.

## Not part of this contract

Anything about how your agent decides what to do with a delegated task,
how it authenticates its own users, or how it stores approvals is entirely
yours. The cockpit only ever speaks the shapes above.
