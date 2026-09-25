"""Tests for the Host-header allowlist (DNS-rebinding guard) in host_guard.py.

Unit cases for every rule, plus one REAL end-to-end case: serve.py's own
Handler on an ephemeral port (same ThreadingHTTPServer pattern as
test_ha_deck.py), with a real HTTP GET carrying a hostile `Host:` header.

Run from repo root: python3 -m pytest webclient/test_host_guard.py -q
"""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import ThreadingHTTPServer

import host_guard


# ── rule fixtures ─────────────────────────────────────────────────────────
#
# Hermetic: the machine-own names and both env vars are pinned per test, so
# the box running pytest (whose real hostname is unknowable here, and which
# may genuinely have Tailscale installed) cannot sway a verdict.


class _FakeProc:
    def __init__(self, stdout: str):
        self.stdout = stdout


def _pin(monkeypatch, own=None):
    """Reset the once-per-process own-names cache and pin it (default: a
    fixed set that does NOT contain evil.example)."""
    monkeypatch.setattr(host_guard, "_own_names", None)
    if own is None:
        own = {"box", "box.local", "box.example.com"}
    monkeypatch.setattr(host_guard, "_machine_own_names", lambda: set(own))
    monkeypatch.delenv("VOICE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("VOICE_WS_ORIGINS", raising=False)


# ── rule 1: no Host header ────────────────────────────────────────────────


def test_missing_host_header_is_allowed(monkeypatch):
    """HTTP/1.0 and native clients send no Host; rebinding needs a browser,
    which always sends one."""
    _pin(monkeypatch)
    assert host_guard.host_allowed(None) is True


def test_empty_host_header_is_allowed(monkeypatch):
    _pin(monkeypatch)
    assert host_guard.host_allowed("") is True
    assert host_guard.host_allowed("   ") is True  # blank counts as absent


def test_unparseable_host_header_fails_closed(monkeypatch):
    """A present-but-unparseable Host is a browser-shaped request with a
    broken hostname -- reject it (fail closed), unlike the absent case."""
    _pin(monkeypatch)
    assert host_guard.host_allowed("[evil") is False
    assert host_guard.host_allowed("[::1") is False  # unterminated IPv6 literal
    assert host_guard.host_allowed("::1") is False  # bare IPv6 parses to no hostname


# ── rule 2: IP literals ───────────────────────────────────────────────────


def test_ip_literal_hosts_are_allowed(monkeypatch):
    _pin(monkeypatch)
    assert host_guard.host_allowed("127.0.0.1:8770") is True
    assert host_guard.host_allowed("[::1]:8770") is True
    assert host_guard.host_allowed("127.0.0.1") is True
    assert host_guard.host_allowed("10.0.0.10:8770") is True
    # NB: a BARE "::1" (no brackets) is not a legal HTTP Host -- RFC 7230
    # brackets IPv6 literals -- and parses to no hostname, so it is now
    # fail-closed rejected, like any other unparseable header.
    assert host_guard.host_allowed("::1") is False


# ── rule 3: localhost and *.localhost ─────────────────────────────────────


def test_localhost_is_allowed_case_insensitively(monkeypatch):
    _pin(monkeypatch)
    assert host_guard.host_allowed("LOCALHOST") is True
    assert host_guard.host_allowed("localhost:8770") is True
    assert host_guard.host_allowed("cockpit.localhost:8770") is True


# ── rule 4: this machine's own names ──────────────────────────────────────


def test_own_names_are_allowed(monkeypatch):
    _pin(monkeypatch)
    assert host_guard.host_allowed("box:8770") is True
    assert host_guard.host_allowed("BOX.local") is True
    assert host_guard.host_allowed("box.example.com") is True


def test_trailing_dot_is_stripped(monkeypatch):
    _pin(monkeypatch)
    assert host_guard.host_allowed("box.") is True
    assert host_guard.host_allowed("box.:8770") is True
    assert host_guard.host_allowed("localhost.") is True


def test_evil_hostname_is_rejected(monkeypatch):
    _pin(monkeypatch)
    assert host_guard.host_allowed("evil.example:8770") is False
    assert host_guard.host_allowed("evil.example") is False
    # the rebinding shape: victim's browser dials the attacker's name
    assert host_guard.host_allowed("http://not-evil.example") is False  # netloc parses to host "http" -- not an allowed name


# ── rule 4, Tailscale probe ───────────────────────────────────────────────
#
# These run the REAL _machine_own_names() (real socket.gethostname/fqdn are
# harmless) with only the subprocess call faked, so the probe itself -- and
# its failure paths -- are what's under test. The probe is the small function
# `subprocess.run` inside `_tailscale_dns_name`, patched per test.


def _pin_probe(monkeypatch, fake_run):
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(host_guard, "_own_names", None)  # force recompute through the fake
    monkeypatch.delenv("VOICE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("VOICE_WS_ORIGINS", raising=False)


def test_tailscale_name_is_allowed_when_installed(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd == ["tailscale", "status", "--json"]
        assert kwargs.get("timeout") == 2
        return _FakeProc(json.dumps({"Self": {"DNSName": "box.tail1234.ts.net."}}))

    _pin_probe(monkeypatch, fake_run)
    assert host_guard.host_allowed("box.tail1234.ts.net:8770") is True
    # trailing dot stripped + lowercased by the probe itself
    assert host_guard.host_allowed("box.tail1234.ts.net.") is True


def test_tailscale_not_installed_is_ignored(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("tailscale: command not found")

    _pin_probe(monkeypatch, fake_run)
    assert host_guard.host_allowed("box.tail1234.ts.net:8770") is False


def test_tailscale_bad_json_is_ignored(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeProc("{not json")

    _pin_probe(monkeypatch, fake_run)
    assert host_guard.host_allowed("box.tail1234.ts.net:8770") is False


# ── rule 5: hostnames named in VOICE_WS_ORIGINS ───────────────────────────


def test_voice_ws_origins_hostnames_are_allowed(monkeypatch):
    _pin(monkeypatch)
    monkeypatch.setenv("VOICE_WS_ORIGINS", "https://cockpit.example:8443,-")
    assert host_guard.host_allowed("cockpit.example:8770") is True
    assert host_guard.host_allowed("other.example:8770") is False  # only '-' skipped, not parsed


# ── rule 6: VOICE_ALLOWED_HOSTS, incl. the * wildcard ─────────────────────


def test_voice_allowed_hosts_entries_are_allowed(monkeypatch):
    _pin(monkeypatch)
    monkeypatch.setenv("VOICE_ALLOWED_HOSTS", "vpn.example, OFFICE.local")
    assert host_guard.host_allowed("vpn.example:8770") is True
    assert host_guard.host_allowed("office.local") is True
    assert host_guard.host_allowed("evil.example:8770") is False


def test_star_disables_the_check(monkeypatch):
    _pin(monkeypatch)
    monkeypatch.setenv("VOICE_ALLOWED_HOSTS", "*")
    assert host_guard.host_allowed("evil.example:8770") is True
    assert host_guard.host_allowed("anything.at.all") is True


# ── the real boundary: serve.py's Handler over a socket ───────────────────


def test_real_server_rejects_evil_host_and_allows_ip_host(monkeypatch):
    """A real HTTP GET through serve.py's own Handler, exactly the shape a
    rebinding victim's browser sends: connection to 127.0.0.1, Host naming
    the attacker. Must be 403; the honest IP-literal Host must still be 200.
    """
    import http.client

    import serve

    _pin(monkeypatch)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/manifest.json", headers={"Host": "evil.example:8770"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 403

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/manifest.json", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 200
    finally:
        srv.shutdown()
