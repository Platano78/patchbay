"""Host-header allowlist: the DNS-rebinding guard for the cockpit HTTP server.

Why this exists: a page at ``http://evil.example:8770`` whose DNS answer flips
to ``127.0.0.1`` (or this box's LAN IP) makes the browser treat the cockpit as
SAME-ORIGIN with evil.example -- it can then read every response and open the
WebSocket. Origin checks cannot stop it: Origin and Host both say
evil.example, so a same-host Origin rule passes. The only reliable signal is
the Host header: a rebinding request always carries the ATTACKER's hostname.

``host_allowed(host_header)`` is the rule, mirrored verbatim (in logic) in
``patches/websocket_streamer.py`` for the pipeline's WebSocket listener --
this module is stdlib-only and must stay that way; that side may not add new
files (``patches/apply.sh`` copies a fixed file list), so it carries its own
copy instead of importing this one. Keep the two in sync.

Env reads are at CHECK time (every call), not at server start: setting
VOICE_ALLOWED_HOSTS then restarting just works, and tests can retune per
call. The one exception is the own-names set (rule 4): it probes Tailscale
with a subprocess, so it is computed once per process and cached.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
from urllib.parse import urlsplit

# The machine's own names (rule 4), computed once on first use. The tailscale
# probe behind it spawns a subprocess; doing that per request would be absurd.
_own_names: set[str] | None = None


def _hostname_from_host(host_header: str | None) -> str | None:
    """Extract the lowercase, dot-stripped hostname from a Host header value
    ("box:8770" -> "box", "[::1]:8770" -> "::1"). None when there is nothing
    to parse (missing/empty header, or a header that parses to no hostname).
    """
    if host_header is None:
        return None
    try:
        # "//"-prefixed: a Host header is a netloc, not a URL; urlsplit then
        # gives us bracketed-IPv6 and port stripping for free.
        name = urlsplit(f"//{host_header.strip()}").hostname
    except ValueError:
        return None
    # urlsplit lowercases but keeps a domain's trailing dot (only IP-shaped
    # hosts get it stripped) -- drop the one FQDN-form trailing dot here.
    if name and name.endswith("."):
        return name[:-1]
    return name


def _tailscale_dns_name() -> str | None:
    """This machine's Tailscale MagicDNS name, or None. Any failure -- not
    installed, tailscaled not running, timeout, bad JSON -- returns None: the
    probe is best-effort and must never break a request or the boot."""
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            timeout=2,
            text=True,
            check=True,
        )
        dns_name = json.loads(proc.stdout)["Self"]["DNSName"]
    except Exception:
        return None
    return str(dns_name).strip().rstrip(".").lower() or None


def _machine_own_names() -> set[str]:
    """Rule 4's name set: bare hostname, FQDN, <hostname>.local, plus the
    Tailscale MagicDNS name when Tailscale answers. Lowercase."""
    names = set()
    try:
        host = socket.gethostname().strip().rstrip(".").lower()
    except Exception:
        host = ""
    if host:
        names.add(host)
        names.add(f"{host}.local")
    try:
        fqdn = socket.getfqdn().strip().rstrip(".").lower()
    except Exception:
        fqdn = ""
    if fqdn:
        names.add(fqdn)
    ts = _tailscale_dns_name()
    if ts:
        names.add(ts)
    return names


def own_names() -> set[str]:
    """Cached (once per process) machine-own names."""
    global _own_names
    if _own_names is None:
        _own_names = _machine_own_names()
    return _own_names


def host_allowed(host_header: str | None) -> bool:
    """Is a request's Host header acceptable? See this module's docstring
    for why. Rules, in order:

    1. No Host header (or a blank one) -> allowed (HTTP/1.0 / native
       clients; rebinding needs a browser, and a browser always sends Host).
    2. Hostname is an IP literal -> allowed (rebinding needs a hostname).
    3. ``localhost`` or ``*.localhost`` -> allowed.
    4. One of this machine's own names (hostname, FQDN, .local, Tailscale).
    5. A hostname named in VOICE_WS_ORIGINS (so a cockpit reached under a
       name its operator already allowlisted keeps working).
    6. An entry of VOICE_ALLOWED_HOSTS; the single value ``*`` disables the
       check entirely.

    Everything else -> rejected. A present-but-unparseable Host header fails
    closed (rejected): same rule the same-host Origin check follows.
    """
    if host_header is None or not host_header.strip():
        return True  # rule 1: header absent or blank
    hostname = _hostname_from_host(host_header)
    if not hostname:
        return False  # present but unparseable (or parses to no hostname)

    raw = (os.environ.get("VOICE_ALLOWED_HOSTS") or "").strip()
    if raw == "*":
        return True
    if hostname in {e.strip().lower().rstrip(".") for e in raw.split(",") if e.strip()}:
        return True

    try:
        ipaddress.ip_address(hostname)
        return True  # rule 2
    except ValueError:
        pass

    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True  # rule 3

    if hostname in own_names():
        return True  # rule 4

    for entry in (os.environ.get("VOICE_WS_ORIGINS") or "").split(","):
        entry = entry.strip()
        if not entry or entry == "-":
            continue
        try:
            origin_host = urlsplit(entry).hostname
        except ValueError:
            continue
        if origin_host and origin_host.rstrip(".") == hostname:
            return True  # rule 5

    return False
