"""Round-trip tests against examples/agent-lane/reference_server.py -- the
proof that docs/agent-lane.md documents the ACTUAL contract, not just a
reading of the code. Each test drives a real HTTP round trip through
hermes_cockpit.py's public API (delegate/respond/send_message) against a
reference server bound to ephemeral ports, plus one test hitting the raw
MCP endpoint to verify the dual-call-flow tolerance (contract point D).

Run from repo root: python3 -m pytest patches/test_agent_lane_reference.py -v

Same hermetic sys.modules stub pattern as test_hermes_cockpit.py -- the real
speech_to_speech package is not installed in this repo.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path

import httpx
import pytest


def _install_stubs():
    def mod(name, **attrs):
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    import pydantic

    mod("speech_to_speech")
    mod("speech_to_speech.pipeline")
    mod("speech_to_speech.pipeline.events", PipelineEvent=pydantic.BaseModel)

    class _StubTTSInput:
        def __init__(self, text=None, turn_id=None, turn_revision=None, **kw):
            self.text = text
            self.turn_id = turn_id
            self.turn_revision = turn_revision

    mod("speech_to_speech.pipeline.messages", TTSInput=_StubTTSInput)


_install_stubs()

from patches import hermes_cockpit  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REF_SERVER_PATH = _REPO_ROOT / "examples" / "agent-lane" / "reference_server.py"

_spec = importlib.util.spec_from_file_location("agent_lane_reference_server", _REF_SERVER_PATH)
reference_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference_server)


@pytest.fixture
def ref_server(monkeypatch):
    """Start the reference server's shim + MCP endpoints on ephemeral
    loopback ports, point hermes_cockpit's module-level URLs at them, and
    tear both threads down after the test."""
    state, shim_server, mcp_server = reference_server.create_servers(
        host="127.0.0.1",
        shim_port=0,
        mcp_port=0,
        seed_approvals=["Restart the router?"],
    )
    threads = [
        threading.Thread(target=shim_server.serve_forever, daemon=True),
        threading.Thread(target=mcp_server.serve_forever, daemon=True),
    ]
    for t in threads:
        t.start()

    shim_url = f"http://127.0.0.1:{shim_server.server_address[1]}/v1/chat/completions"
    mcp_url = f"http://127.0.0.1:{mcp_server.server_address[1]}/mcp"
    monkeypatch.setattr(hermes_cockpit, "_SHIM_URL", shim_url)
    monkeypatch.setattr(hermes_cockpit, "_MCP_URL", mcp_url)
    monkeypatch.setattr(hermes_cockpit, "_resolve_shim_token", lambda: "test-token")
    monkeypatch.setattr(hermes_cockpit, "_HERMES_TARGET", "example:1234567890")

    yield types.SimpleNamespace(state=state, shim_url=shim_url, mcp_url=mcp_url)

    shim_server.shutdown()
    mcp_server.shutdown()


def _make_cockpit(monkeypatch):
    """A HermesCockpit with the background poll thread disabled -- same
    pattern as test_hermes_cockpit.py's _make_cockpit."""
    monkeypatch.setattr(hermes_cockpit.HermesCockpit, "_poll_loop", lambda self: None)
    return hermes_cockpit.HermesCockpit()


# ── delegation round trip (A) ────────────────────────────────────────────


def test_delegation_round_trip_returns_canned_result(monkeypatch, ref_server):
    cockpit = _make_cockpit(monkeypatch)

    cockpit._run_delegation("clean up the kitchen")

    assert cockpit._delegation["status"] == "done"
    assert cockpit._delegation["result"] == "I worked on: clean up the kitchen"


# ── MCP tools round trip (B) ─────────────────────────────────────────────


def test_permissions_list_open_surfaces_seeded_approval(monkeypatch, ref_server):
    cockpit = _make_cockpit(monkeypatch)

    cockpit._poll_once()

    assert cockpit.hermes_ok is True
    assert len(cockpit._permissions) == 1
    assert cockpit._permissions[0]["summary"] == "Restart the router?"


def test_permissions_respond_allow_once_clears_the_approval(monkeypatch, ref_server):
    cockpit = _make_cockpit(monkeypatch)
    cockpit._poll_once()
    approval_id = cockpit._permissions[0]["id"]

    ok, message = cockpit.respond(approval_id, approve=True)

    assert ok is True
    assert message == "approved"
    assert cockpit._permissions == []
    # Confirm it's actually gone server-side too, not just locally.
    remaining = reference_server._dispatch_tool(ref_server.state, "permissions_list_open", {})
    assert remaining["approvals"] == []


def test_permissions_respond_unknown_id_fails(monkeypatch, ref_server):
    cockpit = _make_cockpit(monkeypatch)

    ok, message = cockpit.respond("not-a-real-id", approve=True)

    assert ok is False
    assert "not-a-real-id" in message


def test_messages_send_records_to_the_log(monkeypatch, ref_server):
    cockpit = _make_cockpit(monkeypatch)

    result = cockpit.send_message("on my way")

    assert "Sent to Hermes" in result
    assert len(ref_server.state.sent_messages) == 1
    assert ref_server.state.sent_messages[0]["target"] == "example:1234567890"
    assert ref_server.state.sent_messages[0]["message"] == "on my way"


# ── dual call-flow tolerance (D) ─────────────────────────────────────────


def test_bare_tools_call_with_no_prior_initialize(ref_server):
    """The hermes_cockpit.py path: tools/call with no session, no init."""
    resp = httpx.post(
        ref_server.mcp_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "events_poll", "arguments": {}}},
        timeout=5.0,
    )
    resp.raise_for_status()
    result = resp.json()["result"]
    assert result["isError"] is False


def test_initialize_then_tools_call_with_session_id(ref_server):
    """The voice_tools.py generic-client path: initialize, then tools/call
    carrying back whatever mcp-session-id the server handed out."""
    with httpx.Client(timeout=5.0) as client:
        init = client.post(
            ref_server.mcp_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        init.raise_for_status()
        session_id = init.headers.get("mcp-session-id")
        assert session_id

        call = client.post(
            ref_server.mcp_url,
            headers={"mcp-session-id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "permissions_list_open", "arguments": {}},
            },
        )
        call.raise_for_status()
    result = call.json()["result"]
    assert result["isError"] is False
