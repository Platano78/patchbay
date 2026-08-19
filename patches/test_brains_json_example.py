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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, ".."))
EXAMPLE_PATH = os.path.join(REPO_ROOT, "brains.json.example")

# Ports the owner's private fleet actually uses -- never allowed in the
# shipped example, regardless of host, so the owner's setup can't drift back
# into the file strangers copy.
OWNER_ONLY_PORTS = {"8084", "8087"}


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
