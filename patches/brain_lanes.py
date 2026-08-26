"""Named lane TYPES for `brains.json` entries -- what a stranger would
otherwise have to already know.

A `brains.json` entry is a config blob: `base_url`, `model`, `api_key_file`,
`api_key_var`, `available`. Every one of those fields assumes the reader
already knows where their model lives and which of a provider's hundreds of
model ids is the right one. A lane TYPE supplies that knowledge: an entry
that says `"type": "openrouter"` inherits OpenRouter's documented base URL and
key-variable convention, is checked for a key BEFORE it is probed, and stops
sending a self-hosted-vLLM template argument no hosted provider wants.

**A `type` is optional and purely additive.** An entry without one behaves
exactly as it did before this module existed, byte for byte -- see
`resolve_entry`.

**Dependency-light on purpose**, same contract (and for the same reason) as
`brain_discovery.py`: stdlib + httpx only, no `speech_to_speech.*` imports, so
the CLI below runs standalone before a pipeline exists. The person this most
helps is stuck at "I have an API key and no idea what to put in brains.json",
and cannot get to a running cockpit panel to ask.

**httpx is imported LAZILY, and that is load-bearing.** SETUP.md points a
stranger here at step 0 -- BEFORE `./setup.sh` has built a venv -- so on a box
without httpx installed system-wide a module-level `import httpx` made this
whole file unrunnable for exactly the person it exists to help. Everything
except the live `/models` calls is pure stdlib and must stay that way: listing
the lane types, and `show`'s fields, key console and paste-ready entry, all
work with nothing installed. `brain_discovery.py` keeps its module-level
import on purpose -- it runs after setup, and probing IS its whole job.

Run it directly:

    python3 patches/brain_lanes.py                    # list the lane types
    python3 patches/brain_lanes.py show openrouter    # fields + paste-ready entry
    python3 patches/brain_lanes.py check              # validate + probe a real brains.json

The import direction is one-way -- `brain_control.py` imports FROM here, never
the other way round. Keep it that way or the standalone CLI stops being
standalone. (`resolve_api_key` lives here rather than in `brain_control.py`
for exactly that reason: `check` has to resolve a key the same way the running
pipeline does, and two copies of that parser would drift.)

**It PRINTS a snippet; it NEVER writes `brains.json`.** Same ruling as
`brain_discovery.py`: the file is the user's, and a tool that edits it behind
their back is a tool they cannot trust with their API keys.

The registry is deliberately small and closed (ruling 2): a lane declares the
eleven fields on `Lane` and nothing else. This is not a plugin framework --
the TTS capability seam shipped three capabilities and stopped, and that
restraint is why it still works.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
from dataclasses import dataclass
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)
# Standard library-module idiom: keeps a warning from this module out of a
# CLI's stdout via logging's lastResort handler, while still propagating
# normally once the pipeline configures logging. The `check` CLI prints its own
# diagnosis of a key it could not resolve, in order and on stdout.
logger.addHandler(logging.NullHandler())

# Mirrors the pipeline's own default (`s2s_pipeline.py`'s BrainControl
# construction). Duplicated rather than imported because importing it would
# drag the whole `speech_to_speech` package in and break this module's
# standalone contract; kept in step by `test_brain_lanes.py`.
DEFAULT_BRAINS_JSON = "~/speech-to-speech/brains.json"

# How many live model ids `show` prints for a provider that serves a public
# catalogue. OpenRouter lists 417; the point is to give the reader real ids to
# copy, not to page a catalogue at them.
MAX_SHOWN_MODEL_IDS = 12

# Seconds for a `/models` GET from the CLI. Matches brain_control's own probe
# timeout so `check` fails where the pipeline would fail, not sooner or later.
PROBE_TIMEOUT_S = 3.0

# The three lane kinds. A kind is what the cockpit reasons about when it has
# no name it recognises -- it selects the persona preset (see
# `brain_control.KIND_PRESETS`) and nothing else.
KIND_LOCAL = "local"
KIND_HOSTED = "hosted"
KIND_AGENT = "agent"


@dataclass(frozen=True)
class Lane:
    """One lane type. Exactly these fields, by ruling -- see module docstring.

    `base_url` is None when only the user can know it (their own server).
    `default_model` is None when the provider's catalogue ids drift fast
    enough that shipping one would rot into a 404; such a lane must serve its
    `/models` publicly instead, so the CLI can print live ids.
    """

    key: str
    label: str
    kind: str
    base_url: Optional[str]
    needs_key: bool
    key_var: Optional[str]
    key_console_url: str
    default_model: Optional[str]
    auto_ok: bool
    sends_chat_template_kwargs: bool
    models_endpoint_public: bool
    note: str


_LANE_LIST: tuple[Lane, ...] = (
    Lane(
        key="openai-compatible",
        label="Any OpenAI-compatible server (llama.cpp, vLLM, LM Studio, Ollama, KoboldCpp, Jan…)",
        kind=KIND_LOCAL,
        # Only the user knows which one they run and on which port --
        # `brain_discovery.py` is the tool that finds out.
        base_url=None,
        needs_key=False,
        key_var=None,
        key_console_url="",
        default_model=None,
        # The one lane where "auto" genuinely works: a local server usually
        # has exactly one model loaded, and llama.cpp's router marks it
        # `status.value == "loaded"` so the pick is not a guess.
        auto_ok=True,
        # `chat_template_kwargs` is a self-hosted-vLLM/Qwen idiom -- this is
        # the lane it was designed for, so keep today's behaviour.
        sends_chat_template_kwargs=True,
        models_endpoint_public=False,  # n/a: it is your own server, key or not
        note=(
            "Your own model server. Run `python3 patches/brain_discovery.py` to find it "
            "and print a filled-in entry. Most such servers ignore the API key entirely; "
            "if yours wants one, add api_key_file + api_key_var."
        ),
    ),
    Lane(
        key="openrouter",
        label="OpenRouter",
        kind=KIND_HOSTED,
        base_url="https://openrouter.ai/api/v1",
        needs_key=True,
        key_var="OPENROUTER_API_KEY",
        key_console_url="https://openrouter.ai/keys",
        # Deliberately none: OpenRouter lists 417 models and the ids move.
        # A hardcoded default rots into a 404, and this lane serves its
        # catalogue without a key, so `show` prints live ids instead -- which
        # is strictly better than a placeholder that was true once.
        default_model=None,
        auto_ok=False,
        sends_chat_template_kwargs=False,
        models_endpoint_public=True,
        note=(
            "Routes to many providers behind one key. Model ids look like "
            "`vendor/model`; ids ending in `:free` are the free tier."
        ),
    ),
    Lane(
        key="nvidia-nim",
        label="NVIDIA NIM",
        kind=KIND_HOSTED,
        base_url="https://integrate.api.nvidia.com/v1",
        needs_key=True,
        key_var="NVIDIA_API_KEY",
        key_console_url="https://build.nvidia.com/settings/api-keys",
        # Same reasoning as OpenRouter: 95 models, ids drift, catalogue is public.
        default_model=None,
        auto_ok=False,
        sends_chat_template_kwargs=False,
        models_endpoint_public=True,
        note=(
            "Signing up grants free credits with no card. Error bodies are RFC7807 "
            "`application/problem+json`, not the OpenAI error shape, so a failure here "
            "reads differently from the other lanes."
        ),
    ),
    Lane(
        key="anthropic",
        label="Anthropic (Claude, via its OpenAI-compatible endpoint)",
        kind=KIND_HOSTED,
        base_url="https://api.anthropic.com/v1",
        needs_key=True,
        key_var="ANTHROPIC_API_KEY",
        key_console_url="https://platform.claude.com/settings/keys",
        # The one lane that MUST ship a literal default: its /models needs a
        # key, so the CLI cannot print live ids to pick from. Sonnet rather
        # than Opus because a voice cockpit wants the faster, cheaper tier by
        # default -- swap the id for `claude-opus-5` if you want the other one.
        default_model="claude-sonnet-5",
        auto_ok=False,
        sends_chat_template_kwargs=False,
        models_endpoint_public=False,
        note=(
            "Anthropic's own docs call this OpenAI-compatible layer \"not considered a "
            "long-term or production-ready solution … primarily intended to test and "
            "compare model capabilities\". It works; treat it as a way in, not a "
            "destination. Other ids: claude-opus-5, claude-haiku-4-5."
        ),
    ),
    Lane(
        key="agent",
        label="A background agent behind an OpenAI-compatible shim",
        kind=KIND_AGENT,
        # Your shim, your port -- see docs/agent-lane.md for the contract and
        # examples/ for a reference server.
        base_url=None,
        needs_key=False,
        key_var=None,
        key_console_url="",
        default_model=None,
        auto_ok=True,
        # A shim is not a chat-template-rendering inference server, so the
        # field is meaningless here at best -- and when the shim fronts a
        # HOSTED model it inherits exactly the problem ruling 6 exists to
        # prevent, passing kwargs straight through to a provider that may
        # reject them. A shim that does want them can add `reasoning_effort`.
        sends_chat_template_kwargs=False,
        models_endpoint_public=False,
        note=(
            "Not a raw model: something with its own planning, tools and memory, which "
            "may work for minutes and report back separately. Talking to this lane is "
            "delegation, so its persona preset says so. See docs/agent-lane.md."
        ),
    ),
)

LANES: dict[str, Lane] = {lane.key: lane for lane in _LANE_LIST}

# Placeholder base URLs for the two lanes only the user can fill in. Real,
# plausible values rather than angle brackets -- a reader is far more likely
# to recognise "that is not my port" than to know what to put in a `<...>`.
_PLACEHOLDER_BASE_URLS: dict[str, str] = {
    "openai-compatible": "http://localhost:11434/v1",
    # The shim port docs/agent-lane.md documents.
    "agent": "http://localhost:8087/v1",
}


def get_lane(type_key: Any) -> Optional[Lane]:
    """The `Lane` for a `brains.json` entry's `type`, or None.

    None for anything unrecognised -- including a non-string, since
    `brains.json` is hand-edited. Callers treat None as "no type", which is
    today's behaviour (fail open, same spirit as `brain_control`'s handling of
    an invalid `reasoning_effort`)."""
    if not isinstance(type_key, str):
        return None
    return LANES.get(type_key)


def resolve_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A `brains.json` entry with its lane type's defaults filled in.

    Inherits exactly `base_url`, `api_key_var` and `model`, and only where the
    entry does not state its own -- **the user's explicit values always win**.
    An entry with no `type`, or an unknown one, is returned UNCHANGED (the same
    object, not a copy) so the no-type path is byte-for-byte what it was
    before lane types existed.

    `model` is inherited because "which model id" is the single hardest thing
    for a newcomer to know, and the alternative is worse than it sounds: an
    `anthropic` entry with no `model` falls through `_effective_model` to
    `"auto"`, on the one lane whose `/models` needs a key -- so the user gets
    "model probe failed" when the real answer is "you have not named a model".
    Only the lanes that ship a `default_model` can supply one; OpenRouter and
    NIM deliberately ship none (their ids drift), which is why the CLI prints
    live ids for those two instead.

    An unknown `type` logs one warning and is otherwise ignored rather than
    rejecting the entry: a typo in a hand-edited config must not take a
    working brain offline.
    """
    if not isinstance(entry, dict):
        return entry
    type_key = entry.get("type")
    if type_key is None:
        return entry
    lane = get_lane(type_key)
    if lane is None:
        logger.warning(
            "brain lane: unknown type %r -- ignoring it and using the entry as written. "
            "Known types: %s",
            type_key,
            ", ".join(sorted(LANES)),
        )
        return entry

    resolved = dict(entry)
    if not resolved.get("base_url") and lane.base_url:
        resolved["base_url"] = lane.base_url
    if not resolved.get("api_key_var") and lane.key_var:
        resolved["api_key_var"] = lane.key_var
    if not resolved.get("model") and lane.default_model:
        resolved["model"] = lane.default_model
    return resolved


def resolve_api_key(entry: dict[str, Any]) -> Optional[str]:
    """Resolve a brain's API key: a literal `api_key`, else the value of
    `api_key_var` parsed out of `api_key_file` (env-style `VAR=value` lines).
    Never logs the resolved value.

    Lives here, not in `brain_control.py`, so the standalone `check` CLI
    resolves a key exactly the way the running pipeline does -- two copies of
    this parser would drift, and the failure that produces (a key the panel
    finds and the CLI doesn't, or the reverse) is the worst kind to debug.
    `api_key_file` accepts an absolute path or a `~/...` one -- `~` is expanded
    here. Writing `~/.config/patchbay/openrouter.env` is the natural thing
    to do, and before this it silently resolved to nothing and read back as a
    missing key, which is a trap rather than a rule.
    """
    if entry.get("api_key"):
        return entry["api_key"]
    api_key_file = entry.get("api_key_file")
    api_key_var = entry.get("api_key_var")
    if not api_key_file or not api_key_var:
        return None
    api_key_file = os.path.expanduser(api_key_file)
    try:
        with open(api_key_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == api_key_var:
                    return value.strip().strip('"').strip("'")
    except OSError as e:
        logger.warning("brain lane: failed to read api_key_file %s: %s", api_key_file, e)
    return None


def missing_key_error(name: str, entry: dict[str, Any], lane: Lane) -> str:
    """The message a lane that needs a key and hasn't got one fails with
    (ruling 7). Names the VARIABLE and the FILE, because "no API key" without
    them sends the reader looking in the wrong place.

    This exists as a separate function because a reachability probe is not a
    key check: OpenRouter and NIM answer `/models` with no key at all, so
    without failing here the panel would show the brain healthy and every
    actual turn would 401.
    """
    var = entry.get("api_key_var") or lane.key_var or "the provider's key variable"
    where = entry.get("api_key_file")
    if where:
        return f"{name}: no API key ({var} not found in {where})"
    return (
        f"{name}: no API key -- set api_key_file to a file containing "
        f"{var}=<your key>, or api_key to the key itself. Get a key at {lane.key_console_url}"
    )


def brains_json_path() -> str:
    """Where the pipeline looks for `brains.json`: `$BRAINS_JSON`, else the
    documented default. Resolved the same way here as in `s2s_pipeline.py` so
    `check` inspects the file the running service actually reads."""
    return os.environ.get("BRAINS_JSON", os.path.expanduser(DEFAULT_BRAINS_JSON))


def example_entry(
    lane: Lane, key: Optional[str] = None, label: Optional[str] = None
) -> dict[str, Any]:
    """A paste-ready `{"name": {...}}` for one lane type.

    `key` and `label` are the user's to choose -- a brain's name in the
    cockpit says what the lane is FOR in their setup, which no registry can
    know. They default to the lane's own.

    `base_url` is always written out, even for a lane whose type would supply
    it: a reader scanning a config wants to see where their words are going,
    and `brains.json.example`'s own guard test requires every entry to carry
    one. It may safely be deleted from a typed entry -- see `resolve_entry`.
    """
    name = key or lane.key
    entry: dict[str, Any] = {
        "label": label or lane.label,
        "type": lane.key,
        "base_url": lane.base_url or _PLACEHOLDER_BASE_URLS.get(lane.key, ""),
    }
    if lane.default_model:
        entry["model"] = lane.default_model
    elif lane.auto_ok:
        entry["model"] = "auto"
    else:
        entry["model"] = f"<{lane.key.upper().replace('-', '_')}_MODEL_ID>"
    if lane.needs_key:
        # `/path/to/...` rather than a plausible-looking `~/.config/...`: this
        # is a value the reader MUST replace, and an obviously-fake path says
        # so where a realistic one invites a straight paste. Either form works
        # at runtime -- `resolve_api_key` expands `~`.
        entry["api_key_file"] = f"/path/to/{lane.key}.env"
        entry["api_key_var"] = lane.key_var
    # `available` gates the switch. A lane needing a key ships false -- there
    # is no key yet, so offering the brain would only produce a 401 the user
    # has no way to read. A keyless lane ships true: its base_url may still
    # be wrong, but that shows up honestly as an unreachable brain in the
    # panel rather than as a lane that silently isn't there.
    entry["available"] = not lane.needs_key
    return {name: entry}


# ── the standalone CLI ─────────────────────────────────────────────────


# What to say when httpx is not installed. Named once so the `show` line and
# the `check` refusal cannot drift into telling the user two different things.
HTTPX_MISSING_MSG = (
    "the httpx package is not installed, so nothing here can reach the network. "
    "`./setup.sh` installs it; or `pip install httpx`."
)


def _load_httpx():
    """The `httpx` module, or None when it is not installed.

    Imported here rather than at module scope so that everything which does
    NOT touch the network stays pure stdlib -- see the module docstring. Never
    raises: a missing httpx is a normal state for someone who has not run
    `./setup.sh` yet, and it is the caller's job to say so in words."""
    try:
        import httpx
    except ImportError:
        return None
    return httpx


def _fetch_model_ids(base_url: str, api_key: Optional[str] = None) -> tuple[Optional[list[str]], str]:
    """`(ids, error)` from a `GET {base_url}/models`. `ids` is None on any
    failure, and `error` is then a sentence a stranger can act on rather than
    a stack trace or a bare status code."""
    httpx = _load_httpx()
    if httpx is None:
        return None, HTTPX_MISSING_MSG
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    url = f"{base_url.rstrip('/')}/models"
    try:
        resp = httpx.get(url, timeout=PROBE_TIMEOUT_S, headers=headers)
    except httpx.TimeoutException:
        return None, f"nothing answered within {PROBE_TIMEOUT_S:g}s at {url}"
    except httpx.ConnectError:
        return None, (
            f"nothing is listening at {url} -- check the host and port, and that the "
            "server is actually running"
        )
    except Exception as e:
        return None, f"could not reach {url} ({type(e).__name__}: {e})"
    if resp.status_code in (401, 403):
        return None, f"{url} rejected the request ({resp.status_code}) -- the API key is missing or wrong"
    if resp.status_code == 404:
        return None, (
            f"{url} returned 404 -- base_url is probably wrong. It should be the part "
            "BEFORE /models, and usually ends in /v1"
        )
    if resp.status_code >= 400:
        return None, f"{url} returned {resp.status_code}"
    try:
        data = resp.json().get("data", [])
    except Exception:
        return None, f"{url} answered but not with JSON -- is that really an OpenAI-compatible endpoint?"
    if not isinstance(data, list):
        return None, f"{url} answered with no model list"
    ids = [e["id"] for e in data if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]]
    if not ids:
        return None, f"{url} answered but served no models"
    return ids, ""


def _print_lane_list() -> None:
    print("Lane types you can put in a brains.json entry's \"type\" field:\n")
    width = max(len(k) for k in LANES)
    for lane in _LANE_LIST:
        key_note = " (API key required)" if lane.needs_key else ""
        print(f"  {lane.key.ljust(width)}  {lane.kind:<7} {lane.label}{key_note}")
    print()
    print("A \"type\" is optional: an entry without one behaves exactly as before.")
    print("For the fields, a key, and a paste-ready entry:")
    print("  python3 patches/brain_lanes.py show <type>")
    print("To validate and probe what you already configured:")
    print("  python3 patches/brain_lanes.py check")


def _print_lane_show(lane: Lane) -> None:
    print(f"{lane.label}")
    print(f"  type        {lane.key}   (kind: {lane.kind})")
    if lane.base_url:
        print(f"  base_url    {lane.base_url}   -- supplied by the type; you may omit it")
    else:
        print("  base_url    YOU supply this -- only you know where your server listens")
        if lane.key == "openai-compatible":
            print("              `python3 patches/brain_discovery.py` will find it for you")
    if lane.needs_key:
        print(f"  API key     REQUIRED, as {lane.key_var}")
        print(f"              get one at {lane.key_console_url}")
        print("              put it in a mode-0600 file as `VAR=value`, then point")
        print("              api_key_file at that file -- an absolute path or")
        print("              `~/...`, both work")
    else:
        print("  API key     not required")
    if lane.default_model:
        print(f"  model       defaults to {lane.default_model}")
    elif lane.auto_ok:
        print("  model       \"auto\" works here -- the server reports what it has loaded")
    else:
        print("  model       no default: this provider's ids drift, so pick a live one")
    if not lane.auto_ok:
        print("              \"auto\" is NOT safe on this lane: it exposes no loaded-model")
        print("              status, so \"auto\" means \"whatever is first in the catalogue\"")
    if not lane.sends_chat_template_kwargs:
        # Worth stating: it is the difference between a lane that reaches a
        # server rendering its own chat template and one that does not, and
        # it is why `reasoning_effort` is the way to ask for thinking control
        # on this lane rather than the kwargs blob.
        print("  requests    sends no chat_template_kwargs -- that is a self-hosted")
        print("              inference-server idiom. Add \"reasoning_effort\" to the")
        print("              entry if you need thinking control here.")
    for i, line in enumerate(textwrap.wrap(lane.note, width=68, break_long_words=False, break_on_hyphens=False)):
        print(f"  {'note        ' if i == 0 else '            '}{line}")
    print()


def _print_live_models(lane: Lane) -> None:
    """Print real ids off the provider's public catalogue. Only ever called
    for a lane whose `/models` needs no key -- never invents an id, and says
    plainly when the catalogue could not be read."""
    assert lane.base_url  # a public catalogue implies a known base_url
    if _load_httpx() is None:
        # NOT a failure, and not silently skipped either: the ids are the only
        # part of `show` that needs the network, so say what is missing and
        # make clear the entry printed below is complete without them. Someone
        # reading this has not run ./setup.sh yet -- which is precisely when
        # they most need the rest of this output.
        for line in textwrap.wrap(
            f"Listing live model ids needs the network, and {HTTPX_MISSING_MSG} "
            "Re-run this afterwards to see real ids.",
            width=76,
        ):
            print(line)
        print("The entry below is complete apart from the model id.\n")
        return
    ids, error = _fetch_model_ids(lane.base_url)
    if ids is None:
        print(f"Could not read the live catalogue ({error}).")
        print(f"Browse it at {lane.key_console_url} instead.\n")
        return
    print(f"Live model ids from {lane.base_url}/models (no key needed to list them):")
    ordered = sorted(ids)
    if len(ordered) <= MAX_SHOWN_MODEL_IDS:
        sample = ordered
    else:
        # An evenly spaced slice of the sorted catalogue, not its first N: 417
        # ids sorted by vendor means the head is one vendor's back-catalogue,
        # which tells the reader nothing about what is on offer.
        step = len(ordered) / MAX_SHOWN_MODEL_IDS
        sample = [ordered[int(i * step)] for i in range(MAX_SHOWN_MODEL_IDS)]
    for model_id in sample:
        print(f"  {model_id}")
    if len(ordered) > len(sample):
        print(f"  … {len(ordered)} ids in total; the above is a spread across the catalogue")
    print()


def _cmd_show(type_key: str) -> int:
    lane = get_lane(type_key)
    if lane is None:
        print(f"unknown lane type: {type_key!r}")
        print(f"known types: {', '.join(sorted(LANES))}")
        return 2
    _print_lane_show(lane)
    if lane.models_endpoint_public:
        _print_live_models(lane)
    print("brains.json entry -- paste this in (rename the key to whatever you")
    print("want the brain called in the cockpit):")
    for line in json.dumps(example_entry(lane), indent=2, ensure_ascii=False).splitlines():
        print(f"  {line}")
    print()
    if lane.needs_key:
        print("Then flip \"available\" to true once the key file is in place.")
    return 0


def _load_brains_for_check(path: str) -> tuple[Optional[dict[str, Any]], str]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, f"no brains.json at {path}"
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})"
    except OSError as e:
        return None, f"could not read {path}: {e}"
    if not isinstance(data, dict):
        return None, f"{path} must be a JSON object of brain-name -> entry"
    return data, ""


def _check_entry(name: str, raw: Any) -> bool:
    """Print one entry's verdict; True when that entry looks usable. Every
    failure is stated as something to do, not as a status line -- this runs for
    someone whose cockpit is not working and who does not yet know which of
    five fields is at fault."""
    print(f"{name}")
    if not isinstance(raw, dict):
        print("  ✗ not an object -- every brains.json value must be `{ ... }`\n")
        return False

    lane = get_lane(raw.get("type"))
    if raw.get("type") is not None and lane is None:
        print(f"  ! unknown type {raw['type']!r} -- ignored; known types: {', '.join(sorted(LANES))}")
    entry = resolve_entry(raw)

    if lane is not None:
        print(f"  type       {lane.key} ({lane.kind})")
    base_url = entry.get("base_url")
    if not base_url:
        print("  ✗ no base_url, and this entry's type does not supply one\n")
        return False
    print(f"  base_url   {base_url}")

    model = entry.get("model", "auto")
    print(f"  model      {model}")
    if model == "auto" and lane is not None and not lane.auto_ok:
        print(f"  ! \"auto\" is not safe on a {lane.key} lane -- it will pick whatever is")
        print("    first in the catalogue. Name a model id instead.")

    if not entry.get("available", False):
        print("  ! available is false -- the cockpit will not offer this brain")

    api_key = resolve_api_key(entry)
    if lane is not None and lane.needs_key and not api_key:
        print(f"  ✗ {missing_key_error(name, entry, lane)}")
        print("    Switching to this brain will fail here, before any probe.\n")
        return False
    if api_key:
        print("  api key    resolved")
    elif lane is not None and not lane.needs_key:
        print("  api key    not needed by this lane")

    ids, error = _fetch_model_ids(base_url, api_key)
    if ids is None:
        print(f"  ✗ probe failed: {error}\n")
        return False
    print(f"  ✓ probe ok: {len(ids)} model(s) served")
    if model != "auto" and model not in ids:
        print(f"  ! {model!r} is not in that list -- it may still work (some providers")
        print("    do not list every id they serve), but check it if turns fail.")
    if lane is not None and lane.models_endpoint_public:
        print("  ! this provider lists its models WITHOUT a key, so a green probe here")
        print("    does NOT prove your key is valid. To actually test the key:")
        print(f"      curl -sS {base_url.rstrip('/')}/chat/completions \\")
        print("        -H \"Authorization: Bearer $YOUR_KEY\" -H 'Content-Type: application/json' \\")
        print(f"        -d '{{\"model\":\"{model}\",\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}],\"max_tokens\":1}}'")
        print("    (that is a real request against your account -- one token, but not free)")
    print()
    return True


def _cmd_check(brains_json: Optional[str]) -> int:
    if _load_httpx() is None:
        # Unlike `show`, this subcommand is nothing BUT network: its whole job
        # is probing every configured brain. A real requirement, so say so in
        # one line and exit -- rather than raising a traceback, or pretending
        # to check something it cannot reach.
        print(f"Cannot check brains: {HTTPX_MISSING_MSG}")
        print()
        print("Everything else here still works with no packages installed:")
        print("  python3 patches/brain_lanes.py              # list the lane types")
        print("  python3 patches/brain_lanes.py show <type>  # a paste-ready entry")
        return 1
    path = brains_json or brains_json_path()
    data, error = _load_brains_for_check(path)
    if data is None:
        print(error)
        print()
        if "no brains.json" in error:
            print("That is not fatal: the pipeline starts without it and serves the brain")
            print("named on its command line. brains.json is what lets you SWITCH brains")
            print("from the cockpit. To create one:")
            print("  cp brains.json.example brains.json    # then edit it")
            print("  python3 patches/brain_lanes.py show <type>")
            print("  python3 patches/brain_discovery.py    # finds a local server for you")
        return 1

    print(f"Checking {len(data)} brain(s) in {path}\n")
    failed = [name for name, raw in data.items() if not _check_entry(name, raw)]
    if failed:
        # Non-zero so this is usable as a setup gate. Not an error in itself --
        # a brain you have not finished configuring is expected to fail here,
        # which is what the per-entry lines above say.
        print(f"{len(failed)} of {len(data)} brain(s) are not usable yet: {', '.join(failed)}")
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brain_lanes.py",
        description=(
            "Lane types for brains.json: list them, print a paste-ready entry for one, "
            "or check the entries you already wrote. Prints; never writes brains.json."
        ),
    )
    sub = parser.add_subparsers(dest="command")
    show = sub.add_parser("show", help="fields, where to get a key, and a paste-ready entry")
    show.add_argument("type", metavar="TYPE", help=f"one of: {', '.join(sorted(LANES))}")
    check = sub.add_parser("check", help="validate and probe every entry in a brains.json")
    check.add_argument(
        "--brains-json",
        metavar="PATH",
        default=None,
        help="which file to check (default: $BRAINS_JSON, else %s)" % DEFAULT_BRAINS_JSON,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Prints; never raises out to the caller, and never writes brains.json.
    0 on success, 1 when `check` found nothing to check, 2 for bad arguments."""
    args = _build_parser().parse_args(argv)
    if args.command == "show":
        return _cmd_show(args.type)
    if args.command == "check":
        return _cmd_check(args.brains_json)
    _print_lane_list()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Same reason as brain_discovery.py's: `... | head` closes the pipe
        # under us, and this module's contract is that the CLI prints rather
        # than raises. Point stdout at /dev/null first so the interpreter's
        # own shutdown flush doesn't reprint the error to stderr.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
