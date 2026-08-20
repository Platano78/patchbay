"""Unit tests for brain_discovery.py's local-model-server probing.

Run from repo root: python3 -m pytest patches/test_brain_discovery.py -v

No stubbing needed here, unlike test_brain_control.py: brain_discovery.py is
dependency-light by design (stdlib + httpx only, no `speech_to_speech.*`
imports of its own -- see its module docstring, and its `main()` for the CLI
this also exercises), so it imports cleanly as plain `patches.brain_discovery`.

`main()` is always called with an explicit argv list here -- it parses
`sys.argv` when given None, which under pytest would be pytest's own
arguments.
"""

from __future__ import annotations

import json
import socket
import threading
import time
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
        lambda timeout_s=1.0, urls=None: [
            {"base_url": "http://localhost:11434/v1", "models": ["llama3"], "hint": "Ollama"}
        ],
    )
    brain_discovery.main([])
    out = capsys.readouterr().out
    assert "Ollama" in out
    assert "http://localhost:11434/v1" in out
    assert "llama3" in out
    assert '"base_url": "http://localhost:11434/v1"' in out


def test_main_prints_empty_result_guidance(monkeypatch, capsys):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    monkeypatch.setattr(brain_discovery, "discover", lambda timeout_s=1.0, urls=None: [])
    brain_discovery.main([])
    out = capsys.readouterr().out
    assert "No local model server answered" in out
    assert "BRAIN_DISCOVERY_URLS" in out
    for url in brain_discovery._default_candidates():
        assert url in out


def test_main_never_raises_even_if_discover_raises(monkeypatch, capsys):
    def _raise(timeout_s=1.0, urls=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(brain_discovery, "discover", _raise)
    brain_discovery.main([])  # must not raise
    assert "brain discovery failed unexpectedly" in capsys.readouterr().out


# ── opt-in LAN scan: --host / --cidr (issue #1) ─────────────────────────


def test_discover_no_args_stays_on_loopback(monkeypatch):
    """The contract brain_control.py (and so the cockpit's scan button) relies
    on: discover() with no arguments never leaves the loopback interface, no
    matter what the CLI grew."""
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    probed = []
    monkeypatch.setattr(
        brain_discovery, "_probe", lambda url, t, client=None: probed.append(url) or None
    )
    brain_discovery.discover(timeout_s=0.1)
    assert probed
    for url in probed:
        assert url.startswith("http://localhost:")


def test_discover_explicit_urls_beat_the_env_override(monkeypatch):
    """`urls=` is how the CLI hands a LAN target set in; it must win over
    BRAIN_DISCOVERY_URLS rather than being quietly ignored."""
    monkeypatch.setenv("BRAIN_DISCOVERY_URLS", "http://127.0.0.1:1/v1")
    probed = []
    monkeypatch.setattr(
        brain_discovery, "_probe", lambda url, t, client=None: probed.append(url) or None
    )
    brain_discovery.discover(timeout_s=0.1, urls=["http://10.0.0.5:11434/v1"])
    assert probed == ["http://10.0.0.5:11434/v1"]


def test_discover_empty_url_list_is_a_no_op():
    assert brain_discovery.discover(timeout_s=0.1, urls=[]) == []


def test_candidates_for_hosts_covers_every_known_port_per_host():
    urls = brain_discovery.candidates_for_hosts(["10.0.0.20", "10.0.0.30"])
    assert len(urls) == 2 * len(brain_discovery._PORT_HINTS)
    for port in brain_discovery._PORT_HINTS:
        assert f"http://10.0.0.20:{port}/v1" in urls
        assert f"http://10.0.0.30:{port}/v1" in urls


def test_candidates_for_hosts_dedupes_repeated_hosts():
    urls = brain_discovery.candidates_for_hosts(["10.0.0.1", "10.0.0.1"])
    assert len(urls) == len(brain_discovery._PORT_HINTS)


def test_candidates_for_hosts_brackets_ipv6_and_still_hints_the_port():
    urls = brain_discovery.candidates_for_hosts(["fd00::1"])
    assert "http://[fd00::1]:11434/v1" in urls
    assert brain_discovery._hint_for("http://[fd00::1]:11434/v1") == "Ollama"


def test_expand_cidr_returns_usable_hosts():
    hosts = brain_discovery.expand_cidr("10.0.0.0/30")
    assert hosts == ["10.0.0.1", "10.0.0.2"]


def test_expand_cidr_accepts_a_single_address_block():
    assert brain_discovery.expand_cidr("10.0.0.20/32") == ["10.0.0.20"]


def test_expand_cidr_allows_a_full_slash_24():
    hosts = brain_discovery.expand_cidr("10.0.0.0/24")
    assert len(hosts) <= brain_discovery.MAX_CIDR_HOSTS
    assert hosts[0] == "10.0.0.1"


def test_expand_cidr_rejects_garbage():
    for bad in ["not-a-cidr", "10.0.0.0/33", "999.1.1.1/24", ""]:
        try:
            brain_discovery.expand_cidr(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_expand_cidr_rejects_oversized_block_without_walking_it():
    """A /8 must fail fast on the cap, not materialise 16M addresses."""
    try:
        brain_discovery.expand_cidr("10.0.0.0/8")
    except ValueError as e:
        assert "more than" in str(e)
    else:
        raise AssertionError("a /8 should have been refused")


def test_expand_cidr_refuses_public_address_space():
    try:
        brain_discovery.expand_cidr("8.8.8.0/24")
    except ValueError as e:
        assert "private" in str(e)
    else:
        raise AssertionError("public address space should have been refused")


def test_expand_cidr_allows_loopback_and_link_local():
    assert brain_discovery.expand_cidr("127.0.0.0/30")
    assert brain_discovery.expand_cidr("169.254.1.0/30")


def _targets(argv):
    args = brain_discovery._build_parser().parse_args(argv)
    return brain_discovery._resolve_targets(args)


def test_resolve_targets_default_is_loopback_and_says_so(monkeypatch):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    urls, scope = _targets([])
    assert urls == brain_discovery._default_candidates()
    assert "localhost only" in scope


def test_resolve_targets_names_the_env_override_as_its_scope(monkeypatch):
    monkeypatch.setenv("BRAIN_DISCOVERY_URLS", "http://box:9000/v1")
    urls, scope = _targets([])
    assert urls == ["http://box:9000/v1"]
    assert "BRAIN_DISCOVERY_URLS" in scope


def test_resolve_targets_host_flags_are_repeatable(monkeypatch):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    urls, scope = _targets(["--host", "10.0.0.20", "--host", "10.0.0.30"])
    assert len(urls) == 2 * len(brain_discovery._PORT_HINTS)
    assert "LAN target set" in scope
    assert "2 host(s)" in scope


def test_resolve_targets_host_flags_beat_the_env_override(monkeypatch):
    """--host is a more explicit act than an inherited env var; it must not be
    silently merged with, or lose to, BRAIN_DISCOVERY_URLS."""
    monkeypatch.setenv("BRAIN_DISCOVERY_URLS", "http://box:9000/v1")
    urls, _ = _targets(["--host", "10.0.0.20"])
    assert "http://box:9000/v1" not in urls
    assert "http://10.0.0.20:11434/v1" in urls


def test_resolve_targets_cidr_expands_to_every_host_and_port(monkeypatch):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    urls, scope = _targets(["--cidr", "10.0.0.0/30"])
    assert len(urls) == 2 * len(brain_discovery._PORT_HINTS)
    assert "LAN target set" in scope


def test_resolve_targets_merges_host_and_cidr_without_duplicates(monkeypatch):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    urls, _ = _targets(["--host", "10.0.0.1", "--cidr", "10.0.0.0/30"])
    # 10.0.0.1 is in the block too -- two distinct hosts, not three.
    assert len(urls) == 2 * len(brain_discovery._PORT_HINTS)


def test_resolve_targets_rejects_a_nonsense_timeout():
    for bad in ["0", "-1", "99999"]:
        try:
            _targets(["--timeout", bad])
        except ValueError:
            continue
        raise AssertionError(f"--timeout {bad} should have been rejected")


def test_scan_rounds_bounds_worst_case_wall_clock():
    assert brain_discovery.scan_rounds(0) == 1
    assert brain_discovery.scan_rounds(7) == 1
    assert brain_discovery.scan_rounds(brain_discovery.MAX_SCAN_WORKERS) == 1
    assert brain_discovery.scan_rounds(brain_discovery.MAX_SCAN_WORKERS + 1) == 2


def test_main_default_run_states_localhost_only_scope(monkeypatch, capsys):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    monkeypatch.setattr(brain_discovery, "discover", lambda timeout_s=1.0, urls=None: [])
    assert brain_discovery.main([]) == 0
    assert "localhost only" in capsys.readouterr().out


def test_main_lan_run_states_lan_scope_and_probes_the_hosts(monkeypatch, capsys):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    seen = {}

    def _fake_discover(timeout_s=1.0, urls=None):
        seen["urls"] = list(urls or [])
        return [{"base_url": "http://10.0.0.20:11434/v1", "models": ["llama3"], "hint": "Ollama"}]

    monkeypatch.setattr(brain_discovery, "discover", _fake_discover)
    assert brain_discovery.main(["--host", "10.0.0.20"]) == 0
    out = capsys.readouterr().out
    assert "LAN target set" in out
    assert "localhost only" not in out
    assert "http://10.0.0.20:11434/v1" in seen["urls"]
    # a hit still prints a ready-to-paste brains.json entry
    assert '"base_url": "http://10.0.0.20:11434/v1"' in out


def test_main_lan_run_with_no_hits_says_lan_not_localhost(monkeypatch, capsys):
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    monkeypatch.setattr(brain_discovery, "discover", lambda timeout_s=1.0, urls=None: [])
    assert brain_discovery.main(["--host", "10.0.0.20"]) == 0
    out = capsys.readouterr().out
    assert "No model server answered on the scanned LAN targets" in out
    assert "http://10.0.0.20:11434/v1" in out


def test_main_bad_cidr_exits_nonzero_without_scanning(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(
        brain_discovery, "discover", lambda timeout_s=1.0, urls=None: called.append(1) or []
    )
    assert brain_discovery.main(["--cidr", "10.0.0.0/8"]) == 2
    assert not called
    assert "refusing to scan" in capsys.readouterr().out


def test_main_public_cidr_exits_nonzero_without_scanning(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(
        brain_discovery, "discover", lambda timeout_s=1.0, urls=None: called.append(1) or []
    )
    assert brain_discovery.main(["--cidr", "8.8.8.0/24"]) == 2
    assert not called
    assert "private" in capsys.readouterr().out


def test_main_empty_run_suggests_the_lan_flags(monkeypatch, capsys):
    """The confusion this fixed: a loopback-only run that finds nothing has to
    point at the LAN mode, or the reader concludes the network was scanned."""
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    monkeypatch.setattr(brain_discovery, "discover", lambda timeout_s=1.0, urls=None: [])
    brain_discovery.main([])
    out = capsys.readouterr().out
    assert "--host" in out
    assert "--cidr" in out


def test_main_end_to_end_against_a_real_loopback_server(capsys, monkeypatch):
    """No monkeypatched discover(): --host drives a real probe of a real
    server, proving the CLI wiring reaches the network layer."""
    monkeypatch.delenv("BRAIN_DISCOVERY_URLS", raising=False)
    url, httpd = _serve({"data": [{"id": "llama3"}]})
    port = int(url.rsplit(":", 1)[1].split("/")[0])
    try:
        monkeypatch.setattr(brain_discovery, "_PORT_HINTS", {port: "Ollama"})
        assert brain_discovery.main(["--host", "127.0.0.1", "--timeout", "2"]) == 0
        out = capsys.readouterr().out
        assert "LAN target set" in out
        assert "llama3" in out
        assert '"available": true' in out
    finally:
        httpd.shutdown()


def test_discover_reuses_one_client_across_probes(monkeypatch):
    """Regression guard: module-level httpx.get builds a fresh Client (and
    SSLContext) per call, ~55ms of GIL-bound setup that made a /24 sweep an
    order of magnitude slower than its own timeout budget predicted."""
    clients = []
    monkeypatch.setattr(
        brain_discovery, "_probe", lambda url, t, client=None: clients.append(client) or None
    )
    brain_discovery.discover(timeout_s=0.1, urls=[f"http://10.0.0.{i}:11434/v1" for i in range(20)])
    assert len(clients) == 20
    assert all(c is not None for c in clients)
    assert len(set(id(c) for c in clients)) == 1


def test_discover_wall_clock_stays_within_its_deadline(monkeypatch):
    """A sweep must cost one timeout per *batch*, not per candidate -- 200
    candidates that each stall for the full timeout still has to come back in
    seconds, not the 40s a serial run would take."""
    monkeypatch.setattr(
        brain_discovery, "_probe", lambda url, t, client=None: time.sleep(t) or None
    )
    urls = [f"http://10.0.0.{i // 8}:{9000 + i % 8}/v1" for i in range(200)]
    started = time.monotonic()
    assert brain_discovery.discover(timeout_s=0.2, urls=urls) == []
    elapsed = time.monotonic() - started
    assert elapsed < 0.2 * brain_discovery.scan_rounds(len(urls)) + 2.0, elapsed
