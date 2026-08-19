"""Finds local OpenAI-compatible model servers already running on this box.

Exists because `brains.json.example` can only ship guesses at where a
newcomer's model server lives -- this probes for it instead. See
`brain_control.py::_config_set`'s `discover_brains` branch for how the
running cockpit surfaces this, and README's "Finding your local model server".

**Dependency-light on purpose**: stdlib + httpx only, no `speech_to_speech.*`
imports of its own. This is what lets it run standalone, before a pipeline
(and therefore the cockpit panel) exists at all -- the person this most helps
is stuck at "the pipeline won't start because no brain is reachable", and
can't get to a running panel to ask it what's running. Run it directly:

    python3 patches/brain_discovery.py

`_extract_model_ids`/`MAX_PROBED_MODEL_IDS` live here (not in brain_control.py)
for that reason: brain_control.py imports FROM this module, never the other
way around -- keep it that way, or the standalone CLI stops being standalone.

RULING -- loopback only by default. Probing a user's LAN unprompted looks
like port scanning and is bad manners even when well-intentioned; every
default candidate below is `localhost`. Set `BRAIN_DISCOVERY_URLS` (a
comma-separated list of full base URLs, e.g. `http://localhost:11434/v1`) to
probe something else instead -- it REPLACES the default list, it does not
extend it, so an operator who opts in still isn't scanned beyond what they
named.

Never raises: a candidate that errors, 404s, or times out is just absent from
the result, same as one nothing is listening on.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Cap on how many served model ids a `/models` probe records per server
# (ruling 4, ported from brain_control.py) -- an endpoint serving hundreds
# (NVIDIA NIM) must not bloat a result without bound.
MAX_PROBED_MODEL_IDS = 500

# port -> the local server family that commonly listens there. Loopback only
# (see module docstring) -- this is the entire default candidate set.
_PORT_HINTS: dict[int, str] = {
    11434: "Ollama",
    1234: "LM Studio",
    8080: "llama.cpp llama-server",
    8081: "llama.cpp llama-server (alt port / router)",
    8000: "vLLM / LocalAI",
    5001: "KoboldCpp",
    1337: "Jan",
}

_PORT_RE = re.compile(r":(\d+)(?:/|$)")


def _default_candidates() -> list[str]:
    return [f"http://localhost:{port}/v1" for port in _PORT_HINTS]


def _candidate_urls() -> list[str]:
    override = os.environ.get("BRAIN_DISCOVERY_URLS")
    if override:
        return [u.strip() for u in override.split(",") if u.strip()]
    return _default_candidates()


def _hint_for(base_url: str) -> str:
    m = _PORT_RE.search(base_url)
    if m and int(m.group(1)) in _PORT_HINTS:
        return _PORT_HINTS[int(m.group(1))]
    return "custom"


def _extract_model_ids(data: Any) -> list[str]:
    """Defensively parse served model ids out of a `/models` GET's `data`
    list -- llama.cpp router entries carry `{"id", "status": {...}}`, plain
    OpenAI-style lists just `{"id"}`. Non-dict entries and non-string/empty
    ids are skipped rather than raising; capped at `MAX_PROBED_MODEL_IDS`.
    Shared with `brain_control.py::_resolve_model`'s own `/models` probe --
    see module docstring for why this lives here, not there."""
    ids: list[str] = []
    if not isinstance(data, list):
        return ids
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
            ids.append(entry["id"])
        if len(ids) >= MAX_PROBED_MODEL_IDS:
            break
    return ids


def _probe(base_url: str, timeout_s: float) -> Optional[dict[str, Any]]:
    """GET {base_url}/models. Returns None on any failure (dead port, non-2xx,
    timeout, unparseable body) -- reuses `_extract_model_ids` for the response
    schema so this can't drift from what `brain_control._resolve_model` also
    handles (llama-cpp-router `status.value == "loaded"` entries and plain
    OpenAI-style lists alike)."""
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/models", timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        logger.debug("brain_discovery: %s did not answer: %s", base_url, e)
        return None
    return {"base_url": base_url, "models": _extract_model_ids(data), "hint": _hint_for(base_url)}


def discover(timeout_s: float = 1.0) -> list[dict[str, Any]]:
    """Probe every candidate concurrently and return the ones that answered.

    Bounded total time: candidates run in parallel, each capped at
    `timeout_s`, so a handful of dead ports fails fast rather than serially.
    Never raises -- a probing failure just means fewer (or zero) results."""
    urls = _candidate_urls()
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=max(len(urls), 1)) as pool:
            futures = {pool.submit(_probe, url, timeout_s): url for url in urls}
            for fut in as_completed(futures, timeout=timeout_s + 2.0):
                try:
                    hit = fut.result()
                except Exception as e:
                    logger.debug("brain_discovery: probe of %s raised: %s", futures[fut], e)
                    hit = None
                if hit is not None:
                    results.append(hit)
    except Exception as e:
        # Covers as_completed's own TimeoutError if stragglers never finish --
        # whatever's already in `results` is still returned.
        logger.debug("brain_discovery: discovery round did not fully complete: %s", e)
    return results


def _slug(hint: str) -> str:
    """Turn a hint ("Ollama", "llama.cpp llama-server") into a brains.json
    key -- same rule the webclient panel uses for its own snippet, kept in
    sync by hand since one's Python and one's JS."""
    slug = re.sub(r"[^a-z0-9]+", "-", hint.lower()).strip("-")
    return slug or "local"


def brains_json_snippet(hit: dict[str, Any]) -> str:
    """A ready-to-paste `"key": {...}` entry for `brains.json`, for one
    `discover()` result."""
    entry = {
        "label": hit["hint"],
        "base_url": hit["base_url"],
        "model": hit["models"][0] if hit["models"] else "auto",
        "available": True,
    }
    return json.dumps({_slug(hit["hint"]): entry}, indent=2)


def main() -> None:
    """`python3 patches/brain_discovery.py` -- prints what discover() found
    in plain text, with a ready-to-paste brains.json snippet per hit. Meant
    to work with nothing installed beyond httpx, before any pipeline exists
    (see module docstring) -- so this prints, it never raises out to the
    caller, and it never writes brains.json itself."""
    try:
        results = discover()
    except Exception as e:  # discover() already never raises; belt and suspenders for a CLI entry point
        print(f"brain discovery failed unexpectedly: {e}")
        results = []

    if not results:
        print("No local model server answered on the usual ports:")
        for url in _default_candidates():
            print(f"  {url}")
        print()
        print("If yours listens somewhere else, set BRAIN_DISCOVERY_URLS to its")
        print("base URL(s) (comma-separated) and run this again, e.g.:")
        print("  BRAIN_DISCOVERY_URLS=http://localhost:9000/v1 python3 patches/brain_discovery.py")
        return

    print(f"Found {len(results)} local model server(s):\n")
    for hit in results:
        print(f"{hit['hint']} — {hit['base_url']}")
        print(f"  models: {', '.join(hit['models']) if hit['models'] else '(none reported)'}")
        print("  brains.json entry:")
        for line in brains_json_snippet(hit).splitlines():
            print(f"    {line}")
        print()


if __name__ == "__main__":
    main()
