"""Finds OpenAI-compatible model servers already running on this box (or, on
request, on named LAN hosts).

Exists because `brains.json.example` can only ship guesses at where a
newcomer's model server lives -- this probes for it instead. See
`brain_control.py::_config_set`'s `discover_brains` branch for how the
running cockpit surfaces this, and README's "Finding your local model server".

**Dependency-light on purpose**: stdlib + httpx only, no `speech_to_speech.*`
imports of its own. This is what lets it run standalone, before a pipeline
(and therefore the cockpit panel) exists at all -- the person this most helps
is stuck at "the pipeline won't start because no brain is reachable", and
can't get to a running panel to ask it what's running. Run it directly:

    python3 patches/brain_discovery.py                     # loopback only
    python3 patches/brain_discovery.py --host 10.0.0.20 # one LAN host
    python3 patches/brain_discovery.py --cidr 10.0.0.0/24

`_extract_model_ids`/`MAX_PROBED_MODEL_IDS` live here (not in brain_control.py)
for that reason: brain_control.py imports FROM this module, never the other
way around -- keep it that way, or the standalone CLI stops being standalone.

RULING -- loopback only *by default*, LAN only when asked in so many words.
Probing a user's LAN unprompted looks like port scanning and is bad manners
even when well-intentioned, so every default candidate below is `localhost`
and `discover()` called with no arguments (which is how `brain_control.py`
calls it, i.e. how the cockpit's "Scan for local models" button calls it)
never leaves the loopback interface. Widening the net takes an explicit act
at the CLI:

  * `--host HOST` (repeatable) -- probe the known port list on exactly the
    hosts named. Any address is allowed; you typed it.
  * `--cidr BLOCK` (repeatable) -- probe the known port list across a block.
    Guarded twice: refused outright for non-private address space (see
    `expand_cidr`), and capped at `MAX_CIDR_HOSTS` addresses so a slipped
    prefix can't turn into an unbounded sweep.
  * `BRAIN_DISCOVERY_URLS` -- a comma-separated list of full base URLs, e.g.
    `http://localhost:11434/v1`. Unchanged, and still the way to name an
    endpoint on a port this module doesn't know about. It REPLACES the
    default list, it does not extend it, so an operator who opts in still
    isn't scanned beyond what they named. `--host`/`--cidr` take precedence
    over it when both are present.

Whatever the mode, the CLI says out loud which target set it scanned before
it prints a result -- a discovery run that quietly covered less than the
reader assumed is the failure this module exists to avoid.

Never raises: a candidate that errors, 404s, or times out is just absent from
the result, same as one nothing is listening on.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable, Optional, Sequence

import httpx

logger = logging.getLogger(__name__)

# Cap on how many served model ids a `/models` probe records per server
# (ruling 4, ported from brain_control.py) -- an endpoint serving hundreds
# (NVIDIA NIM) must not bloat a result without bound.
MAX_PROBED_MODEL_IDS = 500

# Ceiling on in-flight probes. Loopback runs never come near it (7 ports);
# it exists so a `--cidr` sweep opens a bounded number of sockets at once
# instead of one per candidate.
MAX_SCAN_WORKERS = 64

# Ceiling on addresses one `--cidr` may expand to -- a /24 and no more.
# Anything larger is refused rather than silently truncated, so nobody gets a
# result that looks like a full sweep but wasn't.
MAX_CIDR_HOSTS = 256

# Ceiling on `--timeout`. A per-probe timeout is what bounds a scan's total
# wall clock; an absurd one turns a sweep into a hang.
MAX_TIMEOUT_S = 30.0

# port -> the server family that commonly listens there. This is the entire
# candidate port list, for loopback and LAN targets alike.
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


def _url_for(host: str, port: int) -> str:
    """`http://host:port/v1`, bracketing IPv6 literals so the result is a URL
    a client can actually parse (and so `_PORT_RE` still reads the port off
    the end rather than out of the address)."""
    try:
        if ipaddress.ip_address(host).version == 6:
            return f"http://[{host}]:{port}/v1"
    except ValueError:
        pass  # a hostname, not an IP literal -- no bracketing needed
    return f"http://{host}:{port}/v1"


def _default_candidates() -> list[str]:
    return [f"http://localhost:{port}/v1" for port in _PORT_HINTS]


def candidates_for_hosts(hosts: Iterable[str]) -> list[str]:
    """Every known port on every host given, deduped, order preserved -- the
    same port list the loopback default uses, pointed somewhere else."""
    urls: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        for port in _PORT_HINTS:
            url = _url_for(host, port)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def expand_cidr(cidr: str) -> list[str]:
    """Addresses in `cidr`, as strings. Raises `ValueError` -- with a message
    written to be printed at a CLI -- rather than returning a surprise.

    Two guards, both deliberate (see the module docstring's ruling):

    * **Private address space only.** A mistyped prefix must not become a
      sweep of somebody else's network. `--host` stays unrestricted: naming
      one address is an unambiguous act, fat-fingering a prefix isn't.
    * **`MAX_CIDR_HOSTS` addresses, refused not truncated.** Counting is lazy
      (`.hosts()` is a generator; a /8 is never materialised), and a block
      that's too big is an error, because a half-scan reported as a scan is
      exactly the wrong answer here.
    """
    try:
        network = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError as e:
        raise ValueError(f"{cidr!r} is not a valid CIDR block ({e})") from e

    if not (network.is_private or network.is_loopback or network.is_link_local):
        raise ValueError(
            f"{cidr!r} is not private address space -- refusing to sweep it. "
            "Use --host to name a single address you really do mean to probe."
        )

    # Pull one more than the cap so "too big" is detectable without walking
    # (or allocating) the whole block.
    hosts = [str(ip) for _, ip in zip(range(MAX_CIDR_HOSTS + 1), network.hosts())]
    if not hosts:
        # A single-address network on a Python whose .hosts() yields nothing
        # for it -- the address itself is plainly what was meant.
        hosts = [str(network.network_address)]
    if len(hosts) > MAX_CIDR_HOSTS:
        raise ValueError(
            f"{cidr!r} covers more than {MAX_CIDR_HOSTS} addresses -- refusing to "
            f"scan it. Narrow the prefix (a /24 is the widest block allowed) or "
            "name hosts individually with --host."
        )
    return hosts


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


def _probe(
    base_url: str, timeout_s: float, client: Optional[httpx.Client] = None
) -> Optional[dict[str, Any]]:
    """GET {base_url}/models. Returns None on any failure (dead port, non-2xx,
    timeout, unparseable body) -- reuses `_extract_model_ids` for the response
    schema so this can't drift from what `brain_control._resolve_model` also
    handles (llama-cpp-router `status.value == "loaded"` entries and plain
    OpenAI-style lists alike).

    `client` is `discover()`'s shared `httpx.Client`. It matters more than it
    looks: module-level `httpx.get` builds a whole Client (and with it an
    SSLContext, CA bundle and all) per call -- ~55ms of GIL-bound setup that
    dwarfs a 1s network timeout once a `--cidr` sweep is measured in
    thousands of probes. Falls back to `httpx.get` when called without one so
    a single ad-hoc probe still works standalone."""
    url = f"{base_url.rstrip('/')}/models"
    try:
        if client is not None:
            resp = client.get(url, timeout=timeout_s)
        else:
            resp = httpx.get(url, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        logger.debug("brain_discovery: %s did not answer: %s", base_url, e)
        return None
    return {"base_url": base_url, "models": _extract_model_ids(data), "hint": _hint_for(base_url)}


def scan_rounds(candidate_count: int) -> int:
    """How many full batches of `MAX_SCAN_WORKERS` a candidate list takes --
    the multiplier on `timeout_s` for a worst-case (nothing answers) run.
    Shared by `discover()`'s own deadline and the CLI's printed estimate so
    the two can't disagree."""
    return max(1, math.ceil(max(candidate_count, 1) / MAX_SCAN_WORKERS))


def discover(timeout_s: float = 1.0, urls: Optional[Sequence[str]] = None) -> list[dict[str, Any]]:
    """Probe every candidate concurrently and return the ones that answered.

    `urls` defaults to `_candidate_urls()` -- loopback, or whatever
    `BRAIN_DISCOVERY_URLS` names. **Calling this with no arguments never
    leaves loopback**, which is the contract `brain_control.py` (and so the
    cockpit's scan button) relies on; the CLI is what passes a LAN target set
    in, after the user asked for one.

    Bounded total time: candidates run in parallel at most `MAX_SCAN_WORKERS`
    at a time, each capped at `timeout_s`, so worst case is one `timeout_s`
    per batch rather than per candidate. Never raises -- a probing failure
    just means fewer (or zero) results."""
    candidates = list(urls) if urls is not None else _candidate_urls()
    if not candidates:
        return []
    results: list[dict[str, Any]] = []
    workers = max(1, min(len(candidates), MAX_SCAN_WORKERS))
    deadline = timeout_s * scan_rounds(len(candidates)) + 2.0
    # One client for the whole round -- see `_probe`. Keepalive is pointless
    # when every candidate is a different host:port, and capping connections
    # at the worker count keeps the pool from becoming a second queue behind
    # the executor (which would make `deadline` a fiction).
    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=0)
    pool: Optional[ThreadPoolExecutor] = None
    try:
        with httpx.Client(timeout=timeout_s, limits=limits) as client:
            pool = ThreadPoolExecutor(max_workers=workers)
            futures = {pool.submit(_probe, url, timeout_s, client): url for url in candidates}
            for fut in as_completed(futures, timeout=deadline):
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
    finally:
        if pool is not None:
            # `wait=False` on purpose: a `with ThreadPoolExecutor(...)` block
            # joins every worker on exit, which would silently outlast the
            # `deadline` above and make it decorative. Stragglers are each
            # already capped at `timeout_s` and their results are discarded.
            pool.shutdown(wait=False, cancel_futures=True)
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


# ── the standalone CLI ─────────────────────────────────────────────────

_EPILOG = """\
examples:
  brain_discovery.py                              loopback only (the default)
  brain_discovery.py --host 10.0.0.20 --host 10.0.0.30
  brain_discovery.py --cidr 10.0.0.0/24        private blocks only, /24 max
  BRAIN_DISCOVERY_URLS=http://box:9000/v1 brain_discovery.py

BRAIN_DISCOVERY_URLS names full base URLs (comma-separated) and is the way to
reach a port this tool doesn't know about; it replaces the default loopback
list rather than extending it. --host/--cidr win over it when both are set.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain_discovery.py",
        description=(
            "Probe for OpenAI-compatible model servers (Ollama, LM Studio, "
            "llama.cpp, vLLM/LocalAI, KoboldCpp, Jan) and print a ready-to-paste "
            "brains.json entry for each one that answers. Scans localhost only "
            "unless you opt into a LAN target set with --host or --cidr."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="HOST",
        help="probe the known ports on this host (IP or hostname). Repeatable.",
    )
    parser.add_argument(
        "--cidr",
        action="append",
        default=[],
        metavar="BLOCK",
        help=(
            f"probe the known ports across a private CIDR block, at most "
            f"{MAX_CIDR_HOSTS} addresses (a /24). Repeatable."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help=f"per-probe timeout (default: %(default)s, max: {MAX_TIMEOUT_S})",
    )
    return parser


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _resolve_targets(args: argparse.Namespace) -> tuple[list[str], str]:
    """(candidate urls, one-line description of the target set).

    The description is not decoration: the issue this solved was a run that
    *looked* like a network scan and wasn't, so every CLI run states its
    scope. Raises `ValueError` for a bad `--cidr` or `--timeout` -- `main`
    turns that into a message and a non-zero exit, never a traceback."""
    if not (0 < args.timeout <= MAX_TIMEOUT_S):
        raise ValueError(
            f"--timeout must be greater than 0 and at most {MAX_TIMEOUT_S} seconds"
        )

    hosts = _dedupe(h.strip() for h in args.host)
    for cidr in args.cidr:
        hosts.extend(h for h in expand_cidr(cidr) if h not in hosts)

    if hosts:
        urls = candidates_for_hosts(hosts)
        return urls, (
            f"LAN target set — {len(hosts)} host(s) × {len(_PORT_HINTS)} known "
            f"port(s) = {len(urls)} candidate(s)"
        )

    urls = _candidate_urls()
    if os.environ.get("BRAIN_DISCOVERY_URLS"):
        return urls, f"BRAIN_DISCOVERY_URLS — {len(urls)} explicit URL(s), no port scan"
    return urls, f"localhost only — {len(urls)} known port(s), no LAN hosts probed"


def _print_candidates(urls: Sequence[str], limit: int = 20) -> None:
    for url in urls[:limit]:
        print(f"  {url}")
    if len(urls) > limit:
        print(f"  … and {len(urls) - limit} more")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python3 patches/brain_discovery.py [--host H] [--cidr BLOCK]` -- prints
    what `discover()` found in plain text, with a ready-to-paste brains.json
    snippet per hit, above a line naming exactly which target set was scanned.
    Meant to work with nothing installed beyond httpx, before any pipeline
    exists (see module docstring) -- so this prints, it never raises out to
    the caller, and it never writes brains.json itself. Returns an exit code:
    0 for a completed scan (hits or not), 2 for bad arguments."""
    args = _build_parser().parse_args(argv)

    try:
        urls, scope = _resolve_targets(args)
    except ValueError as e:
        print(f"brain discovery: {e}")
        return 2

    rounds = scan_rounds(len(urls))
    print(f"Scanning: {scope}")
    if rounds > 1:
        print(
            f"  up to ~{args.timeout * rounds:.0f}s if nothing answers "
            f"({MAX_SCAN_WORKERS} probes in flight, {args.timeout:g}s each)"
        )
    print()

    try:
        results = discover(timeout_s=args.timeout, urls=urls)
    except Exception as e:  # discover() already never raises; belt and braces for a CLI entry point
        print(f"brain discovery failed unexpectedly: {e}")
        results = []

    lan_mode = bool(args.host or args.cidr)

    if not results:
        if lan_mode:
            print("No model server answered on the scanned LAN targets:")
            _print_candidates(urls)
            print()
            print("Check the host is up and its server is bound to the LAN (Ollama")
            print("needs OLLAMA_HOST=0.0.0.0, llama-server needs --host 0.0.0.0),")
            print("and that no firewall is in the way. If it listens on a port this")
            print("tool doesn't know, name the full URL instead:")
            print("  BRAIN_DISCOVERY_URLS=http://10.0.0.20:9000/v1 python3 patches/brain_discovery.py")
            return 0
        print("No local model server answered on the usual ports:")
        _print_candidates(urls)
        print()
        print("If yours listens somewhere else, set BRAIN_DISCOVERY_URLS to its")
        print("base URL(s) (comma-separated) and run this again, e.g.:")
        print("  BRAIN_DISCOVERY_URLS=http://localhost:9000/v1 python3 patches/brain_discovery.py")
        print()
        print("If it runs on another box, scan that instead, e.g.:")
        print("  python3 patches/brain_discovery.py --host 10.0.0.20")
        print("  python3 patches/brain_discovery.py --cidr 10.0.0.0/24")
        return 0

    where = "model server(s)" if lan_mode else "local model server(s)"
    print(f"Found {len(results)} {where}:\n")
    for hit in results:
        print(f"{hit['hint']} — {hit['base_url']}")
        print(f"  models: {', '.join(hit['models']) if hit['models'] else '(none reported)'}")
        print("  brains.json entry:")
        for line in brains_json_snippet(hit).splitlines():
            print(f"    {line}")
        print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `brain_discovery.py --cidr ... | head` closes the pipe under us. A
        # long candidate list makes that a normal way to read this, and the
        # module's contract is that the CLI prints rather than raises -- so
        # swallow it, pointing stdout at /dev/null first so the interpreter's
        # own shutdown flush doesn't reprint the same error to stderr.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
