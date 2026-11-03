"""Read-only web page reader drop-in.

Point VOICE_TOOLS_DIR at this directory (or copy this file into yours) and
restart the voice agent. See the bottom of this file for the one exposed
tool, `read_webpage`.

The voice agent uses this to read a specific page the user named (or one a
search returned) and then speaks a one-or-two-sentence summary of it. It is
deliberately dumb: no logins, no clicking, no forms, no JavaScript — pages
that need any of that will come back empty or as a short refusal.

SECURITY NOTE — DNS rebinding. Each hop's host is resolved ONCE, in
_validate_url, and every returned address must be globally routable. The
connection is then opened to exactly the first validated IP (the
_Pinned*Connection classes), never to the hostname, so a hostile DNS server
cannot answer differently the second time. HTTPS still verifies the
certificate against the ORIGINAL hostname (SNI and hostname check use it) and
the Host header is the original host; verification is never relaxed. Every
redirect hop is validated and pinned on its own. Behaviour change: environment
proxies (HTTP_PROXY etc.) are ignored by this tool, because a proxy would
resolve and connect for us and make the pin meaningless.
Residual risk: only the first address is dialled, so a host whose first
address is unreachable fails rather than falling back to the others.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Optional

TIMEOUT_S = 12.0  # outer bound the pipeline's execute() enforces
_HTTP_TIMEOUT_S = 8.0  # socket timeout on the fetch itself
_MAX_BYTES = 2_000_000  # hard cap on how many body bytes we ever read
_MAX_REDIRECT_HOPS = 3
_ALLOWED_SCHEMES = ("http", "https")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# The content type is checked BEFORE any body bytes are read.
_ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

# Spoken failures. The two unreachable cases are deliberately
# BYTE-IDENTICAL: a blocked non-public address and a refused connection must
# sound the same to the user, or the tool becomes a LAN reachability oracle.
# No message may carry an IP, hostname, port, or stack trace.
_UNREADABLE_MSG = "I couldn't read that page."


class _PageError(Exception):
    """Internal: a kind-tagged failure of one read_webpage run."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _validate_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (kind, None) if `url` is not safe to fetch, else (None, ip).

    `ip` is the first validated address; the caller must connect to exactly
    that, not re-resolve the host.

    Checks are ordered cheapest-first: scheme, then embedded credentials,
    then DNS resolution. Resolution is refused if ANY returned address is
    non-public.
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return "scheme", None
    # A URL that carries its own username/password is refused outright:
    # sending credentials we were never given is not this tool's job.
    if parsed.username or parsed.password:
        return "credentials", None
    host = parsed.hostname
    if not host:
        return "blocked", None
    try:
        port = parsed.port
    except ValueError:  # malformed port component
        return "blocked", None
    if port is None:
        port = 443 if scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return "blocked", None
    # Fail CLOSED on an empty result. getaddrinfo normally raises rather than
    # returning nothing, but the loop below cannot reject what it never sees,
    # so without this an empty list would fall through to "allowed" — the one
    # shape in this function where a resolver quirk becomes an open door.
    if not infos:
        return "blocked", None
    pinned = infos[0][4][0].split("%", 1)[0]
    for info in infos:
        raw_host = info[4][0]
        try:
            # Strip an IPv6 zone id (fe80::1%eth0) before classification.
            addr = ipaddress.ip_address(raw_host.split("%", 1)[0])
        except ValueError:
            return "blocked", None
        # is_global, NOT is_private: the CGNAT range 100.64/10 (Tailscale)
        # is is_private-False but is_global-False, and it must be blocked.
        # is_global-False also covers loopback, private, link-local (which
        # includes the 169.254.169.254 metadata address), multicast and
        # reserved, for both IPv4 and IPv6.
        if addr.is_global is False:
            return "blocked", None
    return None, pinned


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an HTTPError so _fetch re-validates it.

    urllib's default handler would follow a 302 to a non-public address
    without ever running our check — the classic SSRF bypass."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: B027
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to the validated IP, not to the hostname (no second lookup).

    self.host stays the original name, so the Host header is unchanged."""

    def __init__(self, *args, pinned_ip: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    """As above, then TLS with server_hostname = the ORIGINAL host, so SNI and
    certificate/hostname verification are exactly what they would be unpinned."""

    default_port = http.client.HTTPS_PORT

    def __init__(self, *args, context: ssl.SSLContext, **kwargs):
        super().__init__(*args, **kwargs)
        self._context = context

    def connect(self) -> None:
        super().connect()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _ssl_context() -> ssl.SSLContext:
    # create_default_context: CERT_REQUIRED + check_hostname, system CA store.
    return ssl.create_default_context()


def _pin_of(req: urllib.request.Request) -> str:
    pin = getattr(req, "pinned_ip", None)
    if not pin:  # fail closed: never fall back to resolving the name
        raise urllib.error.URLError("no validated address for this request")
    return pin


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        pin = _pin_of(req)
        return self.do_open(
            lambda host, **kw: _PinnedHTTPConnection(host, pinned_ip=pin, **kw), req
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        pin = _pin_of(req)
        ctx = _ssl_context()
        return self.do_open(
            lambda host, **kw: _PinnedHTTPSConnection(host, pinned_ip=pin, context=ctx, **kw),
            req,
        )


# ProxyHandler({}): environment proxies are deliberately off. A proxy would do
# the DNS lookup and the connect itself, which defeats the pin.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler(),
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(),
)


def _open(req: urllib.request.Request, timeout: float):
    """One fetch, no redirects. A module attribute so tests can replace it."""
    return _OPENER.open(req, timeout=timeout)


def _fetch(url: str) -> str:
    """Fetch one URL and return its readable text.

    Every hop — the first included — is re-validated before any network
    call is made for it, and at most _MAX_REDIRECT_HOPS hops are followed.
    Raises _PageError on any failure; never raises anything else.
    """
    current = url
    hops = 0
    while True:
        kind, pinned_ip = _validate_url(current)
        if kind is not None:
            raise _PageError(kind)
        req = urllib.request.Request(
            current,
            headers={
                "User-Agent": "patchbay-page-reader/1.0",
                "Accept": ", ".join(_ALLOWED_CONTENT_TYPES),
            },
        )
        req.pinned_ip = pinned_ip  # this hop's own validated address
        try:
            with _open(req, _HTTP_TIMEOUT_S) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in _ALLOWED_CONTENT_TYPES:
                    raise _PageError("content-type")
                # Never trust Content-Length (absent or lying): bound the
                # read itself.
                raw = resp.read(_MAX_BYTES)
        except _PageError:
            raise
        except urllib.error.HTTPError as e:
            if e.code in _REDIRECT_STATUSES:
                location = e.headers.get("Location") or e.headers.get("Content-Location")
                if location and hops < _MAX_REDIRECT_HOPS:
                    current = urllib.parse.urljoin(current, location)
                    hops += 1
                    continue
                raise _PageError("redirects")
            raise _PageError("http")
        except socket.timeout as e:
            raise _PageError("timeout") from e
        except urllib.error.URLError as e:
            raise _PageError(
                "timeout" if isinstance(e.reason, socket.timeout) else "connection"
            ) from e
        except Exception as e:  # anything unexpected is an unreadable page
            raise _PageError("other") from e
        if not raw:
            raise _PageError("empty")
        # Strip a UTF-8 BOM here rather than in the HTML path: text/plain never
        # reaches the parser, and rfc-editor.org serves .txt with a leading BOM
        # that would otherwise be the first thing spoken.
        text = raw.decode("utf-8-sig", errors="replace")
        if ctype == "text/plain":
            text = re.sub(r"\s+", " ", text).strip()
        else:
            text = _html_to_text(text)
        if not text:
            raise _PageError("empty")
        return text


def _page_error_message(e: _PageError) -> str:
    if e.kind in ("blocked", "connection"):
        return _UNREADABLE_MSG
    if e.kind == "timeout":
        return "That page didn't respond in time."
    if e.kind == "scheme":
        return "I can only read regular web pages over http or https."
    if e.kind == "credentials":
        return "I can't open that link — it carries login details."
    if e.kind == "content-type":
        return "That link isn't a text page, so I didn't open it."
    if e.kind == "redirects":
        return "That page kept redirecting, so I stopped."
    if e.kind == "empty":
        return "That page had no readable text."
    if e.kind == "http":
        return "That page isn't available right now."
    return _UNREADABLE_MSG


# ---------------------------------------------------------------------------
# HTML -> speakable text
# ---------------------------------------------------------------------------

# Contents dropped entirely (they are machine instructions, not page text).
# "Contents" here means the chrome a sighted reader skips past. Measured on
# en.wikipedia.org/wiki/Speech_synthesis: without nav/header, the entire 1800-char
# budget was the menu, the table of contents and 54 language names, with zero
# article prose reaching the brain.
_SKIPPED_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg",
     "nav", "header", "footer", "aside"}
)
# Boundaries rendered as whitespace so adjacent words don't run together.
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "section", "article", "table", "ul", "ol",
        "blockquote", "pre", "main",
    }
)


def _is_main_role(attrs) -> bool:
    """True for an element carrying role="main" (a <div> standing in for <main>)."""
    return any(k == "role" and (v or "").strip().lower() == "main" for k, v in attrs)


class _TextExtractor(HTMLParser):
    """Collects a page's visible text, title kept separately as the lead.

    Depth counters tolerate the malformed nesting real pages are full of:
    a stray closing tag can never drive a counter negative, and an omitted
    <head>/<body> (also common) loses nothing — data outside <head> is body.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.body = ""
        # Most of a real page is furniture. When the document marks its own
        # content region we take that and drop the rest; `main` beats `article`
        # when both exist, and body is the fallback for pages (example.com) that
        # mark nothing. Captured into separate buffers rather than by re-parsing.
        self.main = ""
        self.article = ""
        self._skip_depth = 0
        self._head_depth = 0
        self._in_title = False
        self._main_depth = 0
        self._article_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if tag == "head":
            self._head_depth += 1
        elif tag == "title":
            self._in_title = True
        else:
            if tag == "main" or _is_main_role(attrs):
                self._main_depth += 1
            elif tag == "article":
                self._article_depth += 1
            if tag in _BLOCK_TAGS:
                self._emit(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "head":
            self._head_depth = max(0, self._head_depth - 1)
        elif tag == "title":
            self._in_title = False
        else:
            if tag in _BLOCK_TAGS:
                self._emit(" ")
            # Decremented after the boundary space so the space lands in the
            # same buffer the element's text did.
            if tag == "main":
                self._main_depth = max(0, self._main_depth - 1)
            elif tag == "article":
                self._article_depth = max(0, self._article_depth - 1)

    def handle_data(self, data: str) -> None:
        self._collect(data)
        # Data inside <head> but outside <title> is dropped.

    def _emit(self, chunk: str) -> None:
        self._collect(chunk)

    def _collect(self, chunk: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += chunk
            return
        if self._head_depth:
            return
        # A content region still contributes to body, so a page whose <main>
        # holds nothing usable falls back to the whole document rather than
        # returning an empty string.
        if self._main_depth:
            self.main += chunk
        elif self._article_depth:
            self.article += chunk
        self.body += chunk


def _html_to_text(html: str) -> str:
    """Page source -> one line of speakable text, title leading."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    region = extractor.main.strip() or extractor.article.strip() or extractor.body
    return re.sub(r"\s+", " ", extractor.title + " " + region).strip()


TOOL_DEF = {
    "type": "function",
    "name": "read_webpage",
    "description": (
        "Read the text of a single web page at `url` (http or https) so it "
        "can be summarised aloud. Plain page reading only: it cannot log "
        "in, click anything, fill or submit forms, or run JavaScript — "
        "pages that need any of that will come back empty or as a short "
        "refusal. Give one full URL per call, e.g. 'https://example.com/' "
        "or a link a search just returned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full page address to read, e.g. 'https://example.com/about'.",
            }
        },
        "required": ["url"],
    },
}

ARG_KEY = "url"
REQUIRED = True
TOOL_LABEL = "page reader"
ARG_LABEL = "a web address to read"
RESULT_CHARS = 1800


def run(url: Optional[str] = None) -> str:
    """Read one page and return speakable text. Never raises: every path
    ends in a short string the agent can say out loud."""
    url = (url or "").strip()
    if not url:
        return f"I need {ARG_LABEL}."
    try:
        return _fetch(url)
    except _PageError as e:
        return _page_error_message(e)
    except Exception:
        return _UNREADABLE_MSG
