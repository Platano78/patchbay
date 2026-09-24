# Security

## Supported versions

Only the latest release gets fixes.

## Reporting a vulnerability

Report privately through GitHub: this repository's **Security** tab →
**Report a vulnerability**. Please don't open a public issue for it.

## What you are running

Patchbay has **no authentication anywhere**. It is built for one person's own
machine or trusted network, and the network is the whole perimeter. This is
what that means in practice.

### What listens

| Port | Process | What it is | Bound to |
|---|---|---|---|
| 8765 | pipeline | the WebSocket: audio in and out, transcripts, and every control message | `--ws_host`. SETUP.md's first run passes `127.0.0.1`; the systemd template passes `0.0.0.0`; if you omit the flag, the upstream framework's default is `0.0.0.0` |
| 8443 | pipeline | optional TLS copy of the same WebSocket, only when `VOICE_WS_CERTFILE` and `VOICE_WS_KEYFILE` are both set (`VOICE_WSS_PORT` moves it) | same host as 8765 |
| 8770 | `webclient/serve.py` | HTTP (or HTTPS with `--certfile`/`--keyfile`): the cockpit's files, `/models` + `/v1/models` (reports whether 8765 is listening), and `/ha/states`, `/ha/stream`, `/ha/intent` only when `HA_URL` and `HA_TOKEN` are set | `--host`, default `127.0.0.1`; the systemd template passes `0.0.0.0` |

Anything that can reach 8765 is a full participant in the one shared session:
it hears the live audio and transcripts, and it can rewrite the persona,
switch the brain and model, reset the chat, upload or delete cloned voices,
toggle the wake word, re-import drop-in tools, and answer an agent-lane
approval request (`permission_respond`).

`serve.py` serves every file under `webclient/`. Don't keep private keys or
anything else private in that directory (including `webclient/certs/`, which
is gitignored but not hidden from the server).

### Origin checks are not authentication

`VOICE_WS_ORIGINS` (and the same-host rule used when it is unset) filters
which **browser pages** may open the WebSocket. It stops a web page on
another site from connecting through your browser. A non-browser client
sends no `Origin` at all; the default admits it, as does any allowlist that
includes `-`. It is not a login.

The same-host rule compares the `Origin`'s hostname with the `Host` header's
hostname. It does **not** stop DNS rebinding: a hostile hostname that
resolves to your machine produces a page whose `Origin` and `Host` match.

### The Home Assistant deck

With `HA_URL` and `HA_TOKEN` set, `serve.py` forwards write requests from
`/ha/intent` to Home Assistant, for a fixed list of domains and services
that includes `lock.unlock`. The token stays inside `serve.py`. A write must
be a `Content-Type: application/json` POST (else `415`), and if it carries an
`Origin`, that Origin's hostname must match the `Host` header (else `403`).
That blocks a cross-site form or `text/plain` POST from a page you visit. It
does not stop DNS rebinding (above), and it does not stop anything that can
reach port 8770 directly — `curl` sends no `Origin`. With Home Assistant
configured, keep `serve.py` on `127.0.0.1`, or put real authentication in
front of it.

### Other things that follow from the design

- **Drop-in tools are code.** `VOICE_TOOLS_DIR` names a directory whose
  `.py` files the pipeline imports and runs. Treat that directory like your
  shell profile.
- **Camera and screen frames are written to disk.** When a client shares its
  camera or screen, the latest frame is written to
  `/dev/shm/voice_camera_frame.jpg` / `/dev/shm/voice_screen_frame.jpg`
  (`VOICE_CAMERA_FRAME`, `VOICE_SCREEN_FRAME`) with the process's default
  file permissions — world-readable under the usual `022` umask — and stays
  there until it is overwritten or the machine reboots.
- **Phone context reaches the active brain.** If a browser turns on *Phone
  context*, its approximate location, timezone and battery state go into
  the prompt of whichever brain is active — including a hosted one.
  `VOICE_PHONE_CONTEXT=off` disables it server-side.
- **Voice cloning needs consent.** pocket-tts's licence prohibits voice
  impersonation or cloning without explicit and lawful consent. Clone only
  voices you have the right to.
- **API keys** belong in the mode-`0600` files `brains.json` points at
  (`api_key_file`), and secret env vars in a systemd `EnvironmentFile=`, not
  in `Environment=` lines (unit files are readable by any local user via
  `systemctl cat`). `brains.json` is gitignored; keep it that way.

## Exposing it beyond this machine

Bind `0.0.0.0` (or a LAN/VPN address) only when you mean every device that
can reach that address to have everything above. README's *Who can connect*
covers the WebSocket side; `patches/README.md` *Remote access / HTTPS*
covers TLS. If you need real access control, put a reverse proxy with
authentication in front of both ports and keep the processes themselves on
`127.0.0.1`.
