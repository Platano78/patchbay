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

## D) Both call flows — the thing most likely to break your server

Two different pieces of cockpit code call your MCP endpoint, and they do
**not** behave the same way:

- `patches/hermes_cockpit.py::_mcp_call` posts `tools/call` **directly**,
  with **no `initialize` handshake and no session id**. This is the path
  every real delegate/status/approve round trip takes today.
- `patches/voice_tools.py`'s generic MCP client (used for other MCP
  integrations, and for the startup arming probe) **does** send
  `initialize` first and forwards back whatever `mcp-session-id` header
  your `initialize` response returned, before calling `tools/call`.

Your server must tolerate **both**: answer a bare `tools/call` with no
prior request on the connection, and also answer `initialize` (handing back
a session id if you want one) followed by `tools/call` carrying that id
back. Never *require* the session id — nothing that talks to you today
sends one on the path that actually matters.

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

1. `python3 examples/agent-lane/reference_server.py --seed-approval "Restart the router?"`
   — starts both endpoints on their defaults, with one pending approval.
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
