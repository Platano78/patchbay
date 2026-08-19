"""Unit tests for brain_discovery.py's local-model-server probing.

Run from repo root: python3 -m pytest patches/test_brain_discovery.py -v

No stubbing needed here, unlike test_brain_control.py: brain_discovery.py is
dependency-light by design (stdlib + httpx only, no `speech_to_speech.*`
imports of its own -- see its module docstring, and its `main()` for the CLI
this also exercises), so it imports cleanly as plain `patches.brain_discovery`.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from patches import brain_discovery


# ── test HTTP servers standing in for local model servers ──────────────


class _JSONHandler(BaseHTTPRequestHandler):
    """Serves one fixed JSON body (or a fixed status) at any path."""

    body: bytes = b"{}"
    status: int = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def _serve(body=None, status=200):
    """Start a ThreadingHTTPServer on an ephemeral loopback port. Returns
    (base_url, httpd) -- caller must httpd.shutdown()."""
    handler = type("_Handler", (_JSONHandler,), {
        "body": json.dumps(body if body is not None else {}).encode(),
        "status": status,
    })
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    return f"http://127.0.0.1:{port}/v1", httpd


def _dead_port_url():
    """A base_url nothing is listening on -- bind-then-close guarantees the
    port is free at the moment of the test, connection refused fast."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def _hanging_server():
    """Accepts the TCP connection but never writes a response -- the client's
    httpx read timeout is what ends the probe, not a connection error."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_and_stall():
        try:
            srv.accept()
        except OSError:
            pass  # closed out from under us at teardown

    threading.Thread(target=_accept_and_stall, daemon=True).start()
    return f"http://127.0.0.1:{port}/v1", srv


# ── discover() ───────────────────────────────────────────────────────


def test_discover_parses_plain_openai_shape(monkeypatch):
    url, httpd = _serve({"data": [{"id": "llama3"}, {"id": "phi3"}]})
    try:
        monkeypatch.setenv("BRAIN_DISCOVERY_URLS", url)
        results = brain_discovery.discover(timeout_s=1.0)
        assert results == [{"base_url": url, "models": ["llama3", "phi3"], "hint": "custom"}]
    finally:
        httpd.shutdown()


def test_discover_parses_llama_cpp_router_shape_with_loaded_entry(monkeypatch):
    url, httpd = _serve({
        "data": [
            {"id": "modelA", "status": {"value": "not-loaded"}},
            {"id": "modelB", "status": {"value": "loaded"}},
        ]
    })
    try:
        monkeypatch.setenv("BRAIN_DISCOVERY_URLS", url)
        results = brain_discovery.discover(timeout_s=1.0)
        assert len(results) == 1
        assert results[0]["base_url"] == url
        assert results[0]["models"] == ["modelA", "modelB"]
    finally:
        httpd.shutdown()


def test_discover_ignores_404(monkeypatch):
    url, httpd = _serve(status=404)
    try:
        monkeypatch.setenv("BRAIN_DISCOVERY_URLS", url)
        assert brain_discovery.discover(timeout_s=1.0) == []
    finally:
        httpd.shutdown()


def test_discover_timeout_does_not_raise(monkeypatch):
    url, srv = _hanging_server()
    try:
        monkeypatch.setenv("BRAIN_DISCOVERY_URLS", url)
        assert brain_discovery.discover(timeout_s=0.3) == []
    finally:
        srv.close()


def test_discover_mixed_result_set(monkeypatch):
    """One good responder, one 404, one dead port, one hang -- only the good
    one should come back, and discover() must not raise for any of it."""
    good_url, good_httpd = _serve({"data": [{"id": "m1"}]})
    bad_url, bad_httpd = _serve(status=404)
    dead_url = _dead_port_url()
    hang_url, hang_srv = _hanging_server()
    try:
        monkeypatch.setenv(
            "BRAIN_DISCOVERY_URLS", ",".join([good_url, bad_url, dead_url, hang_url])
        )
        results = brain_discovery.discover(timeout_s=0.5)
        assert results == [{"base_url": good_url, "models": ["m1"], "hint": "custom"}]
    finally:
        good_httpd.shutdown()
        bad_httpd.shutdown()
        hang_srv.close()


def test_discover_env_override_replaces_not_extends(monkeypatch):
    """BRAIN_DISCOVERY_URLS must fully replace the default candidate list,
    not add to it -- otherwise an operator who opts into one extra host would
    unknowingly also scan every default port too."""
    monkeypatch.setenv("BRAIN_DISCOVERY_URLS", "http://127.0.0.1:1/v1")
    assert brain_discovery._candidate_urls() == ["http://127.0.0.1:1/v1"]


def test_default_candidates_are_loopback_only():
    for url in brain_discovery._default_candidates():
        assert url.startswith("http://localhost:")


# ── brains_json_snippet() / main() (the standalone CLI) ─────────────────


def test_brains_json_snippet_is_valid_json_with_expected_fields():
    hit = {"base_url": "http://localhost:11434/v1", "models": ["llama3", "phi3"], "hint": "Ollama"}
    parsed = json.loads(brain_discovery.brains_json_snippet(hit))
    assert set(parsed.keys()) == {"ollama"}
    assert parsed["ollama"] == {
        "label": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3",
        "available": True,
    }


def test_brains_json_snippet_falls_back_to_auto_with_no_models():
    hit = {"base_url": "http://localhost:8080/v1", "models": [], "hint": "llama.cpp llama-server"}
    parsed = json.loads(brain_discovery.brains_json_snippet(hit))
    entry = next(iter(parsed.values()))
    assert entry["model"] == "auto"


def test_main_prints_found_entry_and_snippet(monkeypatch, capsys):
    monkeypatch.setattr(
        brain_discovery,
        "discover",
        lambda timeout_s=1.0: [{"base_url": "http://localhost:11434/v1", "models": ["llama3"], "hint": "Ollama"}],
    )
    brain_discovery.main()
    out = capsys.readouterr().out
    assert "Ollama" in out
    assert "http://localhost:11434/v1" in out
    assert "llama3" in out
    assert '"base_url": "http://localhost:11434/v1"' in out


def test_main_prints_empty_result_guidance(monkeypatch, capsys):
    monkeypatch.setattr(brain_discovery, "discover", lambda timeout_s=1.0: [])
    brain_discovery.main()
    out = capsys.readouterr().out
    assert "No local model server answered" in out
    assert "BRAIN_DISCOVERY_URLS" in out
    for url in brain_discovery._default_candidates():
        assert url in out


def test_main_never_raises_even_if_discover_raises(monkeypatch, capsys):
    def _raise(timeout_s=1.0):
        raise RuntimeError("boom")

    monkeypatch.setattr(brain_discovery, "discover", _raise)
    brain_discovery.main()  # must not raise
    assert "brain discovery failed unexpectedly" in capsys.readouterr().out
