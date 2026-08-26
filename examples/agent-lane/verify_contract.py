#!/usr/bin/env python3
"""agent-lane contract checker -- verify a shim server against the exact
requests the shipped cockpit code makes, WITHOUT installing the pipeline.

Python 3 standard library only (urllib, json, argparse) -- no httpx, no
pytest, no `speech_to_speech` import, no venv. Run this against your own
server before wiring it into the cockpit at all; it is the executable spec
that `docs/agent-lane.md` describes in prose.

Each check replays one real request shape from the shipped client code, at
the SAME timeout the real client uses for that call -- these are design
constraints on your server, not just testing artifacts:

  - ARMING PROBE       patches/voice_tools.py `_probe()` -- 1.5s timeout
  - the four MCP tools patches/hermes_cockpit.py `_mcp_call()` -- 5.0s timeout
                       (`_MCP_TIMEOUT_S`, patches/hermes_cockpit.py:30)
  - DELEGATION         patches/hermes_cockpit.py `_run_delegation()` -- the
                       cockpit itself allows up to 900s (`_SHIM_TIMEOUT_S`,
                       patches/hermes_cockpit.py:40) for a delegated task,
                       but this checker only waits `--delegation-timeout`
                       seconds (default 30s) before giving up with a WARN,
                       not a FAIL -- a slow-but-working delegation is not a
                       contract violation, this script just can't sit for
                       fifteen minutes to prove it.

Usage:
    python3 verify_contract.py --shim-url http://localhost:8087/v1/chat/completions \\
        --mcp-url http://localhost:8088/mcp [--token TOKEN] [--target platform:chat_id] \\
        [--delegation-timeout SECONDS]

Exit code 0 iff every check PASSed (a WARN does not fail the run).
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

_DEFAULT_SHIM_URL = "http://localhost:8087/v1/chat/completions"
_DEFAULT_MCP_URL = "http://localhost:8088/mcp"

# Real production budgets, named here so a PASS/WARN in the output can be
# read against what the cockpit actually enforces:
_PROBE_TIMEOUT_S = 1.5  # patches/voice_tools.py _probe()
_MCP_TIMEOUT_S = 5.0  # patches/hermes_cockpit.py:30 _MCP_TIMEOUT_S
_PRODUCTION_DELEGATION_TIMEOUT_S = 900.0  # patches/hermes_cockpit.py:40 _SHIM_TIMEOUT_S
_DEFAULT_DELEGATION_CHECK_TIMEOUT_S = 30.0  # this checker's own bounded wait, NOT a cockpit value

_PASS = "PASS"
_FAIL = "FAIL"
_WARN = "WARN"

_results: list[tuple[str, str, str]] = []  # (status, check name, reason)


def _record(status: str, name: str, reason: str) -> None:
    _results.append((status, name, reason))
    print(f"[{status}] {name}: {reason}")


class _HTTPResult:
    """Minimal stand-in for what we need out of an HTTP response: status
    code, headers, and the raw body -- mirrors what httpx.Response gives the
    shipped client code."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _post(url: str, headers: dict[str, str], payload: dict, timeout_s: float) -> _HTTPResult:
    """POST JSON and return whatever came back, 2xx or not -- callers decide
    what a non-2xx means. Raises only on a transport-level failure (refused
    connection, DNS, timeout), never on a non-2xx HTTP status."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read()
            return _HTTPResult(resp.status, dict(resp.headers.items()), body)
    except urllib.error.HTTPError as e:
        body = e.read()
        return _HTTPResult(e.code, dict(e.headers.items()) if e.headers else {}, body)


def _is_timeout(e: Exception) -> bool:
    """True iff ``e`` represents a request that exceeded its timeout, as
    opposed to a refused connection, DNS failure, or other transport error.
    urllib raises socket.timeout directly in some cases and wraps it in
    urllib.error.URLError(reason=socket.timeout(...)) in others."""
    if isinstance(e, socket.timeout):
        return True
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, socket.timeout):
        return True
    return False


# -- 1. ARMING PROBE -------------------------------------------------------
# Replays patches/voice_tools.py:_probe() exactly: same headers, same 1.5s
# timeout, same accept/reject rule (2xx AND (non-JSON body OR no top-level
# "error" key) = armed).


def check_arming_probe(mcp_url: str) -> None:
    name = "ARMING PROBE (tools/list)"
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        result = _post(mcp_url, headers, payload, _PROBE_TIMEOUT_S)
    except Exception as e:
        _record(
            _FAIL,
            name,
            f"request failed ({type(e).__name__}: {e}). The cockpit's startup probe (patches/voice_tools.py "
            "_probe(), 1.5s timeout) will also fail this way, so delegate_to_hermes / hermes_status / "
            "send_to_hermes will NEVER ARM -- silently, with no error shown to the user. Grep the pipeline "
            "log for 'voice_tools: armed' to see the probe's own verdict (it reports 'hermes=down').",
        )
        return

    if not (200 <= result.status < 300):
        _record(
            _FAIL,
            name,
            f"HTTP {result.status} (need 2xx). The cockpit's startup probe treats any non-2xx as 'down' -- "
            "delegate_to_hermes / hermes_status / send_to_hermes will NEVER ARM, silently. Grep the "
            "pipeline log for 'voice_tools: armed' (it will report 'hermes=down').",
        )
        return

    try:
        body = result.json()
    except ValueError:
        # A non-JSON 2xx body is treated as success by _probe() -- deliberately.
        _record(
            _PASS,
            name,
            f"HTTP {result.status}, non-JSON body -- _probe()'s rule counts this as armed, but consider "
            "returning a real tools/list JSON-RPC result instead.",
        )
        return

    if isinstance(body, dict) and "error" in body:
        _record(
            _FAIL,
            name,
            f"response body has a top-level \"error\" key ({body.get('error')!r}). The cockpit's startup "
            "probe (patches/voice_tools.py _probe()) treats this as down -- delegate_to_hermes / "
            "hermes_status / send_to_hermes will NEVER ARM, silently, with no error shown to the user. "
            "Fix: answer tools/list with {\"jsonrpc\": \"2.0\", \"id\": ..., \"result\": {\"tools\": [...]}}. "
            "Grep the pipeline log for 'voice_tools: armed' (it will report 'hermes=down').",
        )
        return

    _record(_PASS, name, f"HTTP {result.status}, no top-level error -- the three agent voice tools will arm")


# -- 2. DELEGATION ----------------------------------------------------------
# Replays patches/hermes_cockpit.py:_run_delegation()'s POST shape. The real
# cockpit allows up to 900s (_SHIM_TIMEOUT_S) for this call; this checker
# only waits `check_timeout_s` (default 30s, --delegation-timeout to raise)
# before giving up -- on ITS OWN timeout that's a WARN, not a FAIL, because
# a slow-but-working shim is not a contract violation.


def check_delegation(shim_url: str, token: Optional[str], check_timeout_s: float) -> None:
    name = "DELEGATION (chat.completions)"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "model": "hermes-codex",
        "messages": [{"role": "user", "content": "verify_contract.py smoke task"}],
    }
    try:
        result = _post(shim_url, headers, payload, check_timeout_s)
    except Exception as e:
        if _is_timeout(e):
            _record(
                _WARN,
                name,
                f"no response within {check_timeout_s:.0f}s. The cockpit itself allows up to "
                f"{_PRODUCTION_DELEGATION_TIMEOUT_S:.0f}s (_SHIM_TIMEOUT_S, patches/hermes_cockpit.py:40) for "
                "a delegated task, so this is NOT necessarily a contract violation -- this checker just can't "
                "wait that long. Re-run with --delegation-timeout to wait longer if you want a real PASS/FAIL "
                "here.",
            )
            return
        _record(_FAIL, name, f"request failed ({type(e).__name__}: {e})")
        return

    if not (200 <= result.status < 300):
        _record(
            _FAIL,
            name,
            f"HTTP {result.status} (need 2xx). hermes_cockpit.py calls resp.raise_for_status() BEFORE "
            "reading the body -- a non-2xx means the body is never read, no matter what's in it.",
        )
        return

    try:
        data = result.json()
    except ValueError:
        _record(_FAIL, name, "response body is not valid JSON")
        return

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        _record(_FAIL, name, f"missing choices[0].message.content in response: {data!r}")
        return

    if not isinstance(content, str):
        _record(_FAIL, name, f"choices[0].message.content is not a string (got {type(content).__name__})")
        return

    _record(_PASS, name, f"got string content ({len(content)} chars)")


# -- 3. MCP tools/call -- bare, no initialize, no session id ---------------
# Replays patches/hermes_cockpit.py:_mcp_call() + _tool_call() +
# _unwrap_tool_result() exactly, INCLUDING its 5.0s timeout (_MCP_TIMEOUT_S,
# patches/hermes_cockpit.py:30) -- a server that only answers after 5s+ is
# already broken in production, even if it answers correctly.

_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _mcp_call(mcp_url: str, method: str, params: dict) -> tuple[Optional[dict], Optional[str]]:
    """Returns (rpc_result, error_reason). error_reason is None on success."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        result = _post(mcp_url, _MCP_HEADERS, payload, _MCP_TIMEOUT_S)
    except Exception as e:
        if _is_timeout(e):
            return None, (
                f"no response within {_MCP_TIMEOUT_S:.1f}s. hermes_cockpit.py's _mcp_call() uses a "
                f"{_MCP_TIMEOUT_S:.1f}s timeout (_MCP_TIMEOUT_S, patches/hermes_cockpit.py:30) for every MCP "
                "call -- this exceeded it, and the real cockpit would treat this exact call as failed too."
            )
        return None, f"request failed ({type(e).__name__}: {e})"

    if not (200 <= result.status < 300):
        return None, (
            f"HTTP {result.status} (need 2xx). hermes_cockpit.py's _mcp_call() calls "
            "resp.raise_for_status() BEFORE reading the body -- a non-2xx means any JSON-RPC error you "
            "put in the body is never read; the cockpit just sees the call as a total failure."
        )

    try:
        body = result.json()
    except ValueError:
        return None, "response body is not valid JSON"

    if "error" in body:
        return None, f"top-level JSON-RPC error: {body['error']!r}"

    return body.get("result"), None


def _unwrap_tool_result(result: dict) -> tuple[Optional[dict], Optional[str]]:
    """Mirrors patches/hermes_cockpit.py:_unwrap_tool_result()'s three-shape
    ladder exactly, plus the isError check _tool_call() does first."""
    if result.get("isError"):
        return None, "MCP result has isError=true"

    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        try:
            return json.loads(structured["result"]), None
        except (TypeError, ValueError):
            return None, "structuredContent.result is not valid JSON"

    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            try:
                return json.loads(first["text"]), None
            except (TypeError, ValueError):
                return None, "content[0].text is not valid JSON"

    return result, None


def _call_tool(mcp_url: str, name: str, arguments: dict) -> tuple[Optional[dict], Optional[str]]:
    result, err = _mcp_call(mcp_url, "tools/call", {"name": name, "arguments": arguments})
    if err is not None:
        return None, err
    if result is None:
        return None, "tools/call returned no result"
    return _unwrap_tool_result(result)


def check_events_poll(mcp_url: str) -> None:
    name = "MCP tools/call: events_poll"
    payload, err = _call_tool(mcp_url, "events_poll", {"after_cursor": 0, "limit": 20})
    if err is not None:
        _record(_FAIL, name, err)
        return
    if not isinstance(payload, dict) or "next_cursor" not in payload:
        _record(_FAIL, name, f"unwrapped payload has no 'next_cursor' field: {payload!r}")
        return
    _record(_PASS, name, f"next_cursor={payload['next_cursor']!r}")


def check_permissions_list_open(mcp_url: str) -> None:
    name = "MCP tools/call: permissions_list_open"
    payload, err = _call_tool(mcp_url, "permissions_list_open", {})
    if err is not None:
        _record(_FAIL, name, err)
        return
    approvals = payload.get("approvals") if isinstance(payload, dict) else None
    if not isinstance(approvals, list):
        _record(_FAIL, name, f"unwrapped payload has no 'approvals' list: {payload!r}")
        return
    for entry in approvals:
        if not isinstance(entry, dict) or "id" not in entry:
            _record(_FAIL, name, f"an approval entry is missing 'id': {entry!r}")
            return
    label_keys = ("summary", "title", "description", "tool", "action")
    if approvals and not any(any(k in a for k in label_keys) for a in approvals):
        _record(
            _WARN,
            name,
            f"{len(approvals)} approval(s), none carry any of {label_keys} -- the cockpit's "
            "permissionSummary() will fall back to JSON-stringifying the whole object as the label",
        )
        return
    _record(_PASS, name, f"{len(approvals)} approval(s), each with an id")


def check_permissions_respond(mcp_url: str) -> None:
    """Uses a deliberately unknown id -- this call is inherently mutating in
    the real-id case, so we never guess a real one; an unknown-id response
    is enough to prove the error-in-payload contract (F: unknown id ->
    {"error": ...} inside the unwrapped payload, isError still false)."""
    name = "MCP tools/call: permissions_respond (unknown id)"
    payload, err = _call_tool(
        mcp_url, "permissions_respond", {"id": "__verify_contract_nonexistent__", "decision": "deny"}
    )
    if err is not None:
        _record(_FAIL, name, err)
        return
    if not isinstance(payload, dict):
        _record(_FAIL, name, f"unwrapped payload is not an object: {payload!r}")
        return
    if "error" not in payload:
        _record(
            _WARN,
            name,
            f"an unknown id did not produce {{'error': ...}} in the unwrapped payload: {payload!r} -- "
            "hermes_cockpit.py's respond() checks result.get('error') to detect this failure mode",
        )
        return
    _record(_PASS, name, f"unknown id correctly reported as {payload['error']!r}")


def check_messages_send(mcp_url: str, target: str) -> None:
    name = "MCP tools/call: messages_send"
    payload, err = _call_tool(mcp_url, "messages_send", {"target": target, "message": "verify_contract.py ping"})
    if err is not None:
        _record(_FAIL, name, err)
        return
    _record(_PASS, name, f"call succeeded, unwrapped payload: {payload!r}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shim-url", default=_DEFAULT_SHIM_URL, help=f"default: {_DEFAULT_SHIM_URL}")
    parser.add_argument("--mcp-url", default=_DEFAULT_MCP_URL, help=f"default: {_DEFAULT_MCP_URL}")
    parser.add_argument("--token", default=None, help="bearer token for the shim endpoint, if it requires one")
    parser.add_argument(
        "--target", default="example:1234567890", help="platform:chat_id passed to messages_send (default: example:1234567890)"
    )
    parser.add_argument(
        "--delegation-timeout",
        type=float,
        default=_DEFAULT_DELEGATION_CHECK_TIMEOUT_S,
        help=(
            "how long THIS CHECKER waits for the delegation call before giving up with a WARN "
            f"(default: {_DEFAULT_DELEGATION_CHECK_TIMEOUT_S:.0f}s). The cockpit itself allows up to "
            f"{_PRODUCTION_DELEGATION_TIMEOUT_S:.0f}s ({_PRODUCTION_DELEGATION_TIMEOUT_S / 60:.0f} min) in "
            "production -- raise this flag if your shim genuinely needs longer than the default to answer "
            "a smoke task."
        ),
    )
    args = parser.parse_args(argv)

    print(f"agent-lane contract check against shim={args.shim_url} mcp={args.mcp_url}")
    print(
        f"production budgets: arming probe {_PROBE_TIMEOUT_S:.1f}s, MCP tools {_MCP_TIMEOUT_S:.1f}s, "
        f"delegation {_PRODUCTION_DELEGATION_TIMEOUT_S:.0f}s "
        f"(this checker only waits {args.delegation_timeout:.0f}s for delegation before WARNing)\n"
    )

    check_arming_probe(args.mcp_url)
    check_delegation(args.shim_url, args.token, args.delegation_timeout)
    check_events_poll(args.mcp_url)
    check_permissions_list_open(args.mcp_url)
    check_permissions_respond(args.mcp_url)
    check_messages_send(args.mcp_url, args.target)

    passed = sum(1 for s, _, _ in _results if s == _PASS)
    warned = sum(1 for s, _, _ in _results if s == _WARN)
    failed = sum(1 for s, _, _ in _results if s == _FAIL)
    print(f"\n{passed} passed, {warned} warned, {failed} failed (of {len(_results)} checks)")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
