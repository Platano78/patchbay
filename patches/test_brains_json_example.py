"""Guards brains.json.example against the owner's own fleet leaking back in.

A stranger's first brain-config step is copying this file. It must describe
shapes any self-hoster plausibly runs (Ollama, llama.cpp/LM Studio, a hosted
API) -- never the owner's private LAN topology or owner-only ports, which
would look broken to everyone else (see brain_discovery.py's docstring for
the incident this guards against: brains.json.example once shipped four of
the owner's own lanes, three of which meant nothing to a stranger).

Run from repo root: python3 -m pytest patches/test_brains_json_example.py -v
"""

import ipaddress
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, ".."))
EXAMPLE_PATH = os.path.join(REPO_ROOT, "brains.json.example")

# Ports the owner's private fleet actually uses -- never allowed in the
# shipped example, regardless of host, so the owner's setup can't drift back
# into the file strangers copy.
#
# 8087 was on this list and came OFF it: `docs/agent-lane.md` publishes
# `http://localhost:8087/v1/chat/completions` as the documented default for
# HERMES_SHIM_URL, so it is public API any user may run -- not a leaked
# private port. 8084 stays: it is one private box's model server that leaked
# into shipped files, and nothing documents it as a default anyone would run.
OWNER_ONLY_PORTS = {"8084"}

# Mirrors scripts/export-public.sh's EXCLUDES (that script itself lives under
# one of these prefixes and is not importable from here, so this list is kept
# in step by hand, not by import). Prefix-matched the same way: a `git
# ls-files` entry is excluded when it equals or starts with one of these.
EXPORT_EXCLUDE_PREFIXES = (
    "docs/plans/",  # maintainer notes, not in the public export
    "docs/research/",  # maintainer notes, not in the public export
    "_pickup-handoff.md",
    "scripts/",
    "webclient/revamp-mockups/INDEX.html",
    "webclient/revamp-mockups/main-A-avatar-state/",
    "webclient/revamp-mockups/main-B-avatar-plus-wave/",
    "webclient/revamp-mockups/main-C-needle/",
    "webclient/revamp-mockups/mobile/",
    "webclient/revamp-mockups/neutral/",
    "webclient/revamp-mockups/settings-tiered/",
    "webclient/revamp-mockups/firstrun/",
    "webclient/revamp-mockups/screenshots/",
)


def _exported_text_files():
    """`(path, lines)` for every tracked file the public export actually
    ships, readable as UTF-8 text.

    Built from `git ls-files` minus `EXPORT_EXCLUDE_PREFIXES`, so the port
    guard scans what a stranger's checkout really contains rather than a
    hand-picked handful of files (that hand-picked list -- just `setup.sh` --
    is exactly what missed `patches/README.md` and
    `examples/tools/model_server_status.py` shipping port 8084 as a copy-pasteable
    default). A file that isn't valid UTF-8 (a binary asset) is skipped: this
    guard is about a port named in text, not about enumerating binaries.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    for raw in out.split(b"\x00"):
        if not raw:
            continue
        f = raw.decode("utf-8")
        if any(f == ex.rstrip("/") or f.startswith(ex) for ex in EXPORT_EXCLUDE_PREFIXES):
            continue
        try:
            with open(os.path.join(REPO_ROOT, f), encoding="utf-8") as fh:
                lines = fh.readlines()
        except (UnicodeDecodeError, OSError):
            continue
        yield f, lines


def _load():
    with open(EXAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _is_private_host(host):
    if host in ("localhost",):
        return False
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False  # a hostname, e.g. openrouter.ai -- treated as public


def test_brains_json_example_parses():
    data = _load()
    assert isinstance(data, dict)
    assert data, "brains.json.example is empty"


def test_no_lan_addresses_or_owner_ports():
    data = _load()
    for name, entry in data.items():
        base_url = entry["base_url"]
        m = re.match(r"^https?://([^/:]+)(?::(\d+))?", base_url)
        assert m, f"{name}: unparseable base_url {base_url!r}"
        host, port = m.group(1), m.group(2)
        assert not _is_private_host(host), (
            f"{name}: base_url {base_url!r} is a LAN/RFC1918 address, "
            f"not usable by a stranger's checkout"
        )
        assert port not in OWNER_ONLY_PORTS, (
            f"{name}: base_url {base_url!r} uses an owner-only port {port}"
        )


def test_shipped_files_name_no_owner_only_port():
    """A port banned from brains.json.example must not reach a stranger
    through any other shipped file either -- same defect, different file.
    Widened 2026-08-26 (Slice G) from a hand-picked `("setup.sh",)` tuple to
    every file the public export actually ships."""
    for name, lines in _exported_text_files():
        for lineno, line in enumerate(lines, 1):
            for port in OWNER_ONLY_PORTS:
                assert f":{port}" not in line, (
                    f"{name}:{lineno} names owner-only port {port}, which no "
                    f"stranger's box serves: {line.strip()!r}"
                )
