#!/usr/bin/env python3
"""agent-lane reference server -- REFERENCE/TEST implementation, not a real
agent. It exists to prove the delegate/status/approve lane's wiring end to
end and to give a self-hoster a runnable starting point for their own agent.
No auth hardening beyond an optional bearer-token check on the shim
endpoint, no persistence, no rate limiting. Loopback only by default --
do not expose this beyond your own machine.

Serves BOTH halves of the contract documented in docs/agent-lane.md, on the
lane's own default ports so it works with zero env vars set:

  - an OpenAI-compatible /v1/chat/completions  (matches HERMES_SHIM_URL's
    default, http://localhost:8087/v1/chat/completions) that echoes back a
    canned "I worked on: <task>" completion;
  - an MCP JSON-RPC endpoint at /mcp  (matches HERMES_MCP_URL's default,
    http://localhost:8088/mcp) implementing the four tools the cockpit
    calls: events_poll, permissions_list_open, permissions_respond,
    messages_send.

Two ports because the contract's own defaults are two ports; running them
in one process keeps "clone, run, it just works" true without asking a
user to juggle two terminals.

Stdlib only (http.server) -- no FastAPI, no MCP SDK -- so this runs on a
bare Python 3.10 with nothing installed.

Usage:
    python3 reference_server.py --seed-approval "Restart the router?"

Then, with the cockpit's pipeline pointed at the defaults (or your own
HERMES_SHIM_URL/HERMES_MCP_URL), say "delegate <task>" and watch the seeded
approval show up in the cockpit's hold-to-approve gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

logger = logging.getLogger("agent_lane_reference")

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "agent-lane-reference", "version": "1"}
_SESSION_ID = "ref-session-1"

TOOLS = [
    {
        "name": "events_poll",
        "description": "Poll for new events after a cursor. Event content is unused by the "
        "cockpit -- only next_cursor matters, so this reference server always returns an "
        "empty events list and a monotonically advancing cursor.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_cursor": {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "permissions_list_open",
        "description": "List currently pending approval requests.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "permissions_respond",
        "description": "Resolve a pending approval by id with decision 'allow-once' or 'deny'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "decision": {"type": "string", "enum": ["allow-once", "deny"]},
            },
            "required": ["id", "decision"],
        },
    },
    {
        "name": "messages_send",
        "description": "Send a free-form message to a target 'platform:chat_id'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["target", "message"],
        },
    },
]


class State:
    """In-memory state shared by both handler classes: a monotonic events
    cursor, pending approvals, and a log of sent messages. One instance per
    server run."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cursor = 0
        self.approvals: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self._next_approval_id = 1

    def seed_approval(self, summary: str) -> str:
        with self.lock:
            approval_id = f"appr-{self._next_approval_id}"
            self._next_approval_id += 1
            self.approvals.append({"id": approval_id, "summary": summary})
        return approval_id


def _dispatch_tool(state: State, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one MCP tool by name. Returns the *unwrapped* JSON payload (the
    caller wraps it in the content[0].text shape)."""
    if name == "events_poll":
        with state.lock:
            state.cursor += 1
            cursor = state.cursor
        return {"events": [], "next_cursor": cursor}

    if name == "permissions_list_open":
        with state.lock:
            approvals = list(state.approvals)
        return {"approvals": approvals}

    if name == "permissions_respond":
        approval_id = arguments.get("id")
        decision = arguments.get("decision")
        if decision not in ("allow-once", "deny"):
            return {"error": f"unknown decision: {decision!r}"}
        with state.lock:
            match = next((a for a in state.approvals if a.get("id") == approval_id), None)
            if match is None:
                return {"error": f"unknown permission id: {approval_id}"}
            state.approvals.remove(match)
        logger.info("permissions_respond: %s -> %s", approval_id, decision)
        return {"ok": True, "id": approval_id, "decision": decision}

    if name == "messages_send":
        target = arguments.get("target")
        message = arguments.get("message")
        entry = {"target": target, "message": message, "ts": time.time()}
        with state.lock:
            state.sent_messages.append(entry)
        logger.info("messages_send -> %s: %s", target, message)
        return {"ok": True}

    return {"error": f"unknown tool: {name}"}


def _wrap_tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """The tools/call result shape a plain MCP server (not Hermes' own
    structuredContent.result-as-string variant) uses: content[0].text is the
    JSON payload, stringified."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


def _make_handler(base_cls: type, **class_attrs: Any) -> type:
    """Bind per-run state onto a fresh subclass, since http.server
    instantiates the handler class fresh per request."""
    return type(base_cls.__name__, (base_cls,), class_attrs)


class _JSONHandlerMixin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - " + fmt, self.client_address[0], *args)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def _send_json(self, status: int, obj: Any, extra_headers: Optional[dict[str, str]] = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class MCPHandler(_JSONHandlerMixin):
    """POST /mcp -- JSON-RPC 2.0. Tolerates BOTH call flows the cockpit's own
    code uses: a bare tools/call with no prior initialize (hermes_cockpit.py),
    and an initialize-then-call flow forwarding mcp-session-id (the pattern
    voice_tools.py's generic MCP client uses against other servers). A
    session id is handed back from initialize but never required back."""

    state: State

    def do_POST(self) -> None:  # noqa: N802
        try:
            req = self._read_json()
        except (ValueError, TypeError):
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return

        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "initialize":
            result = {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": _SERVER_INFO,
            }
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": rpc_id, "result": result},
                extra_headers={"mcp-session-id": _SESSION_ID},
            )
            return

        if method == "tools/list":
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": TOOLS}})
            return

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            payload = _dispatch_tool(self.state, name, arguments)
            self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "result": _wrap_tool_result(payload)})
            return

        self._send_json(
            200,
            {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"method not found: {method}"}},
        )


class ShimHandler(_JSONHandlerMixin):
    """POST /v1/chat/completions -- OpenAI-compatible chat completions,
    canned response. If ``token`` is set, requires ``Authorization: Bearer
    <token>``; this is a courtesy for exercising the auth path, not real
    security."""

    state: State
    token: Optional[str] = None

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        if self.token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.token}":
                self._send_json(401, {"error": "unauthorized"})
                return
        try:
            body = self._read_json()
        except (ValueError, TypeError):
            self._send_json(400, {"error": "bad json"})
            return

        task = ""
        for message in body.get("messages") or []:
            if message.get("role") == "user":
                task = message.get("content") or ""
        text = f"I worked on: {task}" if task else "I worked on: (no task given)"
        logger.info("chat.completions -> %r", text)
        self._send_json(
            200,
            {
                "id": "chatcmpl-ref-1",
                "object": "chat.completion",
                "model": body.get("model") or "reference",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            },
        )


def create_servers(
    host: str = "127.0.0.1",
    shim_port: int = 8087,
    mcp_port: int = 8088,
    token: Optional[str] = None,
    seed_approvals: Optional[list[str]] = None,
) -> tuple[State, ThreadingHTTPServer, ThreadingHTTPServer]:
    """Build (but do not start) both servers, sharing one State. Port 0
    binds an OS-assigned ephemeral port -- read it back from
    ``server.server_address[1]`` (used by the test suite)."""
    state = State()
    for text in seed_approvals or []:
        state.seed_approval(text)
    shim_cls = _make_handler(ShimHandler, state=state, token=token)
    mcp_cls = _make_handler(MCPHandler, state=state)
    shim_server = ThreadingHTTPServer((host, shim_port), shim_cls)
    mcp_server = ThreadingHTTPServer((host, mcp_port), mcp_cls)
    return state, shim_server, mcp_server


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="loopback by default; do not bind 0.0.0.0")
    parser.add_argument("--shim-port", type=int, default=8087)
    parser.add_argument("--mcp-port", type=int, default=8088)
    parser.add_argument("--token", default=None, help="require this bearer token on /v1/chat/completions")
    parser.add_argument(
        "--seed-approval",
        action="append",
        default=[],
        metavar="TEXT",
        help="seed a pending approval with this label (repeatable)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    state, shim_server, mcp_server = create_servers(
        args.host, args.shim_port, args.mcp_port, args.token, args.seed_approval
    )

    threads = [
        threading.Thread(target=shim_server.serve_forever, name="agent-lane-shim", daemon=True),
        threading.Thread(target=mcp_server.serve_forever, name="agent-lane-mcp", daemon=True),
    ]
    for t in threads:
        t.start()

    print("agent-lane reference server up:")
    print(f"  shim (HERMES_SHIM_URL): http://{args.host}:{args.shim_port}/v1/chat/completions")
    print(f"  mcp  (HERMES_MCP_URL):  http://{args.host}:{args.mcp_port}/mcp")
    if args.token:
        print(f"  shim requires bearer token: {args.token}")
    for text, approval in zip(args.seed_approval, state.approvals):
        print(f"  seeded approval {approval['id']}: {text}")
    print("Ctrl-C to stop.")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        shim_server.shutdown()
        mcp_server.shutdown()


if __name__ == "__main__":
    main()
