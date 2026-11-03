"""Unit tests for the read_webpage voice tool drop-in.

Run from the repo root:
    python3 -m pytest examples/tools/test_read_webpage.py -q

No real network: both the module's fetch hook (`_open`) and
`socket.getaddrinfo` are monkeypatched in every test.
"""
from __future__ import annotations

import io
import socket
import urllib.error
import urllib.request

from examples.tools import read_webpage as rw

# A real public address (example.com) — is_global is True for it.
_PUBLIC_IP = "93.184.216.34"


class _RecordingOpen:
    """Stand-in for the drop-in's fetch hook, recording every call it gets.

    Side effects are consumed one per call: either an exception instance
    (to be raised) or a _FakeResponse (to be returned)."""

    def __init__(self):
        self.calls = []
        self.side_effects = []

    def __call__(self, req, timeout):
        self.calls.append(req)
        if not self.side_effects:
            raise AssertionError("fetch hook called more times than scripted")
        effect = self.side_effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return effect


class _FakeResponse:
    """Stand-in for the object the fetch hook returns.

    Records every size passed to read() so tests can prove the download cap
    is enforced by the read, not by a Content-Length header."""

    def __init__(self, body: bytes, content_type: str):
        self._body = body
        self.headers = {"Content-Type": content_type}
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _redirect(from_url: str, to_url: str, code: int = 302) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        from_url, code, "Found", {"Location": to_url}, io.BytesIO(b"")
    )


def _patch_getaddrinfo(monkeypatch, mapping: dict[str, str]) -> None:
    """Fake DNS: `mapping` of hostname -> address; unknown hosts fail."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(8, "no address for unknown host in test")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], 443), b"")]

    monkeypatch.setattr(rw.socket, "getaddrinfo", fake_getaddrinfo)


def test_non_http_schemes_refused_without_network(monkeypatch):
    urls = (
        "file:///etc/passwd",
        "ftp://x/",
        "javascript:alert(1)",
        "data:text/html,x",
    )
    opener = _RecordingOpen()
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {})  # any resolution attempt fails loudly
    messages = [rw.run(url) for url in urls]
    assert opener.calls == [], "scheme check must fire before any network call"
    # All four are the same spoken refusal, and it names the real restriction.
    assert all(m == messages[0] for m in messages)
    assert "http or https" in messages[0]


def test_embedded_credentials_refused(monkeypatch):
    opener = _RecordingOpen()
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {"example.com": _PUBLIC_IP})
    msg = rw.run("https://user:pass@example.com/")
    assert opener.calls == []
    assert "login" in msg.lower()


def test_non_public_destinations_refused(monkeypatch):
    # The CGNAT-range row (RFC 6598, used by carrier-grade NAT and Tailscale)
    # is the load-bearing one: ipaddress.is_private is False for it while
    # is_global is also False, so an is_private-based check passes it
    # straight through. Built from octets so it doesn't read as a literal
    # host address.
    cgnat_ip = ".".join(("100", "64", "0", "1"))
    for ip in ("127.0.0.1", "10.0.0.5", "172.16.0.5", "169.254.169.254", cgnat_ip):
        opener = _RecordingOpen()
        monkeypatch.setattr(rw, "_open", opener)
        _patch_getaddrinfo(monkeypatch, {"internal.example": ip})
        msg = rw.run("https://internal.example/")
        assert opener.calls == [], f"{ip} must be blocked before any fetch"
        assert msg == rw._UNREADABLE_MSG, f"{ip} refused with wrong message: {msg!r}"


def test_empty_resolution_fails_closed(monkeypatch):
    # getaddrinfo normally raises rather than returning nothing, but the
    # per-address loop cannot reject what it never sees: before the explicit
    # guard, an empty list fell through to "allowed". A security check that
    # fails open on a resolver quirk is the one shape worth a regression test.
    opener = _RecordingOpen()
    monkeypatch.setattr(rw, "_open", opener)
    monkeypatch.setattr(rw.socket, "getaddrinfo", lambda *a, **k: [])
    msg = rw.run("https://example.com/")
    assert opener.calls == [], "an unresolvable host must not be fetched"
    assert msg == rw._UNREADABLE_MSG


def test_public_destination_allowed_through_to_fetch(monkeypatch):
    opener = _RecordingOpen()
    opener.side_effects.append(
        _FakeResponse(
            b"<html><head><title>Demo</title></head>"
            b"<body><p>Hello world</p></body></html>",
            "text/html",
        )
    )
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {"example.com": _PUBLIC_IP})
    msg = rw.run("https://example.com/page")
    assert len(opener.calls) == 1
    assert "Hello world" in msg
    assert "Demo" in msg


def test_redirect_to_non_public_address_is_revalidated(monkeypatch):
    # The most important test in this file: a PUBLIC url that 302s to the
    # cloud-metadata address must be refused, and the redirect target must
    # never be fetched. A urllib auto-redirect implementation (which does
    # not re-resolve through our check) fails this.
    opener = _RecordingOpen()
    opener.side_effects.append(
        _redirect("https://example.com/", "http://169.254.169.254/latest/meta-data/")
    )
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(
        monkeypatch,
        {"example.com": _PUBLIC_IP, "169.254.169.254": "169.254.169.254"},
    )
    msg = rw.run("https://example.com/")
    assert len(opener.calls) == 1, "redirect target must not be fetched"
    assert msg == rw._UNREADABLE_MSG


def test_redirect_cap(monkeypatch):
    opener = _RecordingOpen()
    opener.side_effects.extend(
        _redirect(f"https://example.com/{n}", f"https://example.com/{n + 1}")
        for n in range(1, 5)
    )
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {"example.com": _PUBLIC_IP})
    msg = rw.run("https://example.com/1")
    assert len(opener.calls) == 4, "at most 3 hops may be followed"
    assert "redirect" in msg.lower()


def test_disallowed_content_types_refused_without_body(monkeypatch):
    for ctype in ("application/pdf", "image/png"):
        resp = _FakeResponse(b"binary junk", ctype)
        opener = _RecordingOpen()
        opener.side_effects.append(resp)
        monkeypatch.setattr(rw, "_open", opener)
        _patch_getaddrinfo(monkeypatch, {"example.com": _PUBLIC_IP})
        msg = rw.run("https://example.com/file")
        assert resp.read_sizes == [], f"{ctype}: body must not be downloaded"
        assert "text page" in msg.lower()


def test_download_capped_by_read_size(monkeypatch):
    # No Content-Length involved: the read itself must be capped.
    resp = _FakeResponse(b"x" * 3_000_000, "text/plain")
    opener = _RecordingOpen()
    opener.side_effects.append(resp)
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {"example.com": _PUBLIC_IP})
    result = rw.run("https://example.com/big")
    assert resp.read_sizes, "expected a bounded read"
    assert max(resp.read_sizes) == 2_000_000
    assert len(result) == 2_000_000


def test_main_container_wins_over_page_chrome():
    # Measured defect this guards: against the live Wikipedia article the whole
    # 1800-char budget was menu + table of contents + 54 language names, with no
    # article prose reaching the brain at all.
    html = (
        "<html><head><title>T</title></head><body>"
        "<nav>Jump to content Main menu Random article</nav>"
        "<header>Site banner</header>"
        "<main><p>Real article prose here.</p></main>"
        "<footer>Privacy policy</footer>"
        "<aside>Related links</aside>"
        "</body></html>"
    )
    text = rw._html_to_text(html)
    assert "Real article prose" in text
    for chrome in ("Jump to content", "Main menu", "Site banner", "Privacy policy", "Related links"):
        assert chrome not in text, f"chrome leaked into the result: {chrome!r}"


def test_nav_nested_inside_main_is_still_dropped():
    # Wikipedia nests nav elements *within* <main>, so picking the container is
    # not enough on its own.
    html = "<body><main><nav>Toggle subsection</nav><p>Body text.</p></main></body>"
    text = rw._html_to_text(html)
    assert "Body text." in text
    assert "Toggle subsection" not in text


def test_article_used_when_no_main():
    html = "<body><nav>junk</nav><article><p>Article body.</p></article></body>"
    text = rw._html_to_text(html)
    assert "Article body." in text and "junk" not in text


def test_main_beats_article_when_both_present():
    html = "<body><article><p>From article.</p></article><main><p>From main.</p></main></body>"
    text = rw._html_to_text(html)
    assert "From main." in text
    assert "From article." not in text


def test_role_main_acts_as_the_container():
    html = '<body><nav>junk</nav><div role="MAIN"><p>Roled content.</p></div></body>'
    text = rw._html_to_text(html)
    assert "Roled content." in text and "junk" not in text


def test_page_with_no_container_falls_back_to_body():
    # The example.com shape: no main, article, nav, header or footer at all.
    html = "<html><head><title>Example Domain</title></head><body>" \
           "<div><h1>Example Domain</h1><p>This domain is for use in examples.</p></div>" \
           "</body></html>"
    text = rw._html_to_text(html)
    assert "This domain is for use in examples." in text
    assert "Example Domain" in text


def test_empty_main_falls_back_rather_than_returning_nothing():
    html = "<body><main>   </main><p>Only real text.</p></body>"
    text = rw._html_to_text(html)
    assert "Only real text." in text


def test_utf8_bom_is_stripped_from_plain_text(monkeypatch):
    # rfc-editor.org serves .txt with a leading BOM; it must not be the first
    # thing spoken. text/plain never reaches the HTML parser, so this is decoded
    # with utf-8-sig rather than stripped in _html_to_text.
    opener = _RecordingOpen()
    opener.side_effects = [
        _FakeResponse("\ufeffInternet Engineering Task Force".encode("utf-8"), "text/plain")
    ]
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {"example.com": _PUBLIC_IP})
    out = rw.run("https://example.com/rfc.txt")
    assert not out.startswith("\ufeff"), "BOM must not survive into the spoken text"
    assert out.startswith("Internet Engineering")


def test_html_to_text():
    html = (
        "<html><head><title>My Title</title>"
        "<style>body { color: red; }</style></head><body>"
        "<script>var secret = 'evil()';</script>"
        "<p>One</p><p>Two</p>"
        "<h1>Head &amp; Shoulders</h1>"
        "</body></html>"
    )
    text = rw._html_to_text(html)
    assert "evil" not in text, "script contents must be dropped"
    assert "color" not in text, "style contents must be dropped"
    assert "OneTwo" not in text, "adjacent paragraphs must not run together"
    assert "One Two" in text
    assert "My Title" in text, "title must lead the output"
    assert "Head & Shoulders" in text, "entities must be unescaped"
    assert "<" not in text and ">" not in text


def test_empty_page(monkeypatch):
    resp = _FakeResponse(b"<html><body>&nbsp;</body></html>", "text/html")
    opener = _RecordingOpen()
    opener.side_effects.append(resp)
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {"example.com": _PUBLIC_IP})
    msg = rw.run("https://example.com/empty")
    assert "no readable" in msg.lower()


def test_failure_messages_safe_and_indistinguishable(monkeypatch):
    # A blocked non-public address and a refused connection must sound
    # identical to the user, or the tool is a LAN reachability oracle.
    opener = _RecordingOpen()
    monkeypatch.setattr(rw, "_open", opener)
    _patch_getaddrinfo(monkeypatch, {"blocked.example": "10.0.0.5"})
    blocked_msg = rw.run("https://blocked.example/")
    assert opener.calls == []

    def _refused(req, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(rw, "_open", _refused)
    _patch_getaddrinfo(monkeypatch, {"down.example": _PUBLIC_IP})
    refused_msg = rw.run("https://down.example/")

    assert blocked_msg == refused_msg, "must not leak which addresses are reachable"
    for m in (blocked_msg, refused_msg):
        assert "10.0.0.5" not in m and _PUBLIC_IP not in m
        assert "Traceback" not in m and len(m) < 120


def test_timeout_is_speakable(monkeypatch):
    def _slow(req, timeout):
        raise socket.timeout

    monkeypatch.setattr(rw, "_open", _slow)
    _patch_getaddrinfo(monkeypatch, {"slow.example": _PUBLIC_IP})
    msg = rw.run("https://slow.example/")
    assert "respond" in msg.lower() or "time" in msg.lower()
    assert "Traceback" not in msg


def test_missing_url():
    assert "web address" in rw.run(None)
    assert "web address" in rw.run("   ")


# ---------------------------------------------------------------------------
# DNS rebinding: the connection goes to the validated IP, nothing re-resolves
# ---------------------------------------------------------------------------

_LOOPBACK = "127.0.0.1"


def _rebinding_dns(monkeypatch, first_ip, then_ip=_LOOPBACK):
    """Public answer on the first lookup, loopback on every later one."""
    calls = []

    def fake(host, port, *a, **k):
        calls.append(host)
        ip = first_ip if len(calls) == 1 else then_ip
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port), b"")]

    monkeypatch.setattr(rw.socket, "getaddrinfo", fake)
    return calls


def test_connection_is_pinned_to_the_validated_ip(monkeypatch):
    lookups = _rebinding_dns(monkeypatch, _PUBLIC_IP)
    dialled = []

    def fake_create_connection(address, *a, **k):
        dialled.append(address)
        raise OSError("stop here: no network in tests")

    monkeypatch.setattr(rw.socket, "create_connection", fake_create_connection)
    rw.run("http://rebind.example/")
    assert dialled == [(_PUBLIC_IP, 80)], f"dialled {dialled!r}, not the validated IP"
    assert len(lookups) == 1, "the host must be resolved exactly once"


class _FakeSock:
    """Just enough socket for http.client: records the request, serves a reply."""

    def __init__(self, reply: bytes):
        self.sent = b""
        self._reply = reply

    def setsockopt(self, *a):
        pass

    def settimeout(self, *a):
        pass

    def sendall(self, data):
        self.sent += data

    def makefile(self, *a, **k):
        return io.BytesIO(self._reply)

    def close(self):
        pass


def test_each_redirect_hop_pins_its_own_validated_ip(monkeypatch):
    ip_a, ip_b = _PUBLIC_IP, "8.8.8.8"
    mapping = {"a.example": ip_a, "b.example": ip_b}

    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port), b"")]

    monkeypatch.setattr(rw.socket, "getaddrinfo", fake_getaddrinfo)
    socks = [
        _FakeSock(b"HTTP/1.1 302 Found\r\nLocation: http://b.example/x\r\n"
                  b"Content-Length: 0\r\n\r\n"),
        _FakeSock(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                  b"Content-Length: 5\r\n\r\nhello"),
    ]
    dialled = []

    def fake_create_connection(address, *a, **k):
        dialled.append(address)
        return socks[len(dialled) - 1]

    monkeypatch.setattr(rw.socket, "create_connection", fake_create_connection)
    assert rw.run("http://a.example/") == "hello"
    assert dialled == [(ip_a, 80), (ip_b, 80)]
    assert b"Host: a.example" in socks[0].sent
    assert b"Host: b.example" in socks[1].sent


def test_https_verifies_against_the_original_hostname(monkeypatch):
    _rebinding_dns(monkeypatch, _PUBLIC_IP)
    dialled, wrapped = [], []

    class _FakeContext:
        def wrap_socket(self, sock, **kwargs):
            wrapped.append(kwargs)
            raise OSError("stop here: no TLS in tests")

    monkeypatch.setattr(rw, "_ssl_context", lambda: _FakeContext())
    monkeypatch.setattr(
        rw.socket, "create_connection",
        lambda address, *a, **k: dialled.append(address) or _FakeSock(b""),
    )
    rw.run("https://rebind.example/")
    assert dialled == [(_PUBLIC_IP, 443)]
    assert wrapped == [{"server_hostname": "rebind.example"}]


def test_default_tls_context_verifies_certificates():
    import ssl
    ctx = rw._ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_request_without_a_pin_is_refused_not_resolved(monkeypatch):
    def no_connect(*a, **k):
        raise AssertionError("must not connect")

    monkeypatch.setattr(rw.socket, "create_connection", no_connect)
    req = urllib.request.Request("http://example.com/")  # no pinned_ip set
    try:
        rw._open(req, 1.0)
    except urllib.error.URLError:
        pass
    else:
        raise AssertionError("an unpinned request must fail closed")


def test_env_proxies_are_ignored(monkeypatch):
    # A proxy would resolve and connect for us, making the pin meaningless.
    # Rebuild the module's opener with a proxy in the environment.
    import importlib
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    importlib.reload(rw)
    _rebinding_dns(monkeypatch, _PUBLIC_IP)
    dialled = []

    def fake_create_connection(address, *a, **k):
        dialled.append(address)
        raise OSError("stop")

    monkeypatch.setattr(rw.socket, "create_connection", fake_create_connection)
    rw.run("http://rebind.example/")
    assert dialled == [(_PUBLIC_IP, 80)], "a proxy must never receive the connection"
