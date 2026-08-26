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
#
# 8087 was on this list and came OFF it: `docs/agent-lane.md` publishes
# `http://localhost:8087/v1/chat/completions` as the documented default for
# HERMES_SHIM_URL, so it is public API any user may run -- not a leaked
# private port. 8084 stays: it is one private box's model server that leaked
# into shipped files, and nothing documents it as a default anyone would run.
OWNER_ONLY_PORTS = {"8084"}

# Shipped files scanned as TEXT for an owner-only port. brains.json.example is
# checked structurally below (it is JSON, with a base_url per entry); a script
# can name a port anywhere, so these are grepped line by line instead.
#
# setup.sh earns its place here: it printed
# `--responses_api_base_url http://localhost:8084/v1` in the "Next steps"
# block after every successful install -- the first thing a stranger reads,
# naming a port only the maintainer's box serves, three lines below a
# discovery scan that had just told them where their model server really is.
TEXT_SCANNED_FILES = ("setup.sh",)


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


def test_shipped_scripts_name_no_owner_only_port():
    """A port banned from brains.json.example must not reach a stranger
    through a script either -- same defect, different file."""
    for name in TEXT_SCANNED_FILES:
        path = os.path.join(REPO_ROOT, name)
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for lineno, line in enumerate(lines, 1):
            for port in OWNER_ONLY_PORTS:
                assert f":{port}" not in line, (
                    f"{name}:{lineno} names owner-only port {port}, which no "
                    f"stranger's box serves: {line.strip()!r}"
                )
