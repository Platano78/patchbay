"""
Server-side tools the voice agent's LLM can call: weather, web search, and
knowledge-base lookup (QMD). Definitions are wired onto ``session.tools`` by
``BrainControl``; execution happens here, off the audio thread, with a hard
per-tool timeout and a short, TTS-friendly plain-text result.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from speech_to_speech import phone_context

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 600

# knowledge_lookup / decision_lookup return document excerpts, not one-liners,
# so they get their own larger caps (see _RESULT_CHARS). These land in the LLM's
# context, not in the spoken reply — the tool description still asks for 1-2
# spoken sentences — so the budget is set by "enough to answer accurately"
# rather than by what is speakable.
_KNOWLEDGE_RESULT_CHARS = 1800
_DECISION_RESULT_CHARS = 1200

# Per-document excerpt budget inside knowledge_lookup.
_DOC_EXCERPT_CHARS = 450
_CONVO_EXCERPT_CHARS = 280
_DECISION_EXCERPT_CHARS = 350

# How many QMD hits to actually fetch content for. 1 is too few — the live
# "echo gate" query ranks a marginal hit first — and 3+ doubles latency while
# diluting the answer. get() costs ~10ms once query has run, so 2 is cheap.
_KNOWLEDGE_DOCS = 2
_KNOWLEDGE_CONVOS = 2
_DECISION_HITS = 3

# Window fetched around each hit's `line`. Back 6 lines to pick up the heading
# above the match; forward far enough for a full short section. The char cap
# above is what actually bounds the text.
_GET_LINES_BEFORE = 6
_GET_MAX_LINES = 24

_WEATHER_TIMEOUT_S = 6.0
_KNOWLEDGE_TIMEOUT_S = 6.0
# Per-backend budgets inside the parallel fan-out. They run concurrently, so
# the wall clock is max(), not sum() — comfortably inside the 6s tool deadline.
# Every knowledge backend is a container on this same box, so these are
# loopback budgets, not network ones. Measured on the live box: QMD query
# ~0.15s and get ~0.01s, Genesis /search ~0.09s, Faulkner /api/search ~0.002s.
# Each is set well above its measurement to absorb a cold container, and well
# below what a person would notice mid-sentence.
_QMD_TIMEOUT_S = 2.0
_GENESIS_TIMEOUT_S = 1.5
_DECISION_TIMEOUT_S = 1.5
# Wall clock the whole fan-out gets, shared across both lanes. Sits under the
# 6s tool deadline so a slow backend degrades to partial results rather than
# tripping execute()'s "timed out" refusal.
_FANOUT_BUDGET_S = 2.5
_SEARCH_TIMEOUT_S = 10.0
_MOOD_TIMEOUT_S = 2.0
_HERMES_DELEGATE_TIMEOUT_S = 3.0
_HERMES_STATUS_TIMEOUT_S = 2.0
_SEND_TIMEOUT_S = 6.0

_MOODS = ("neutral", "happy", "excited", "thinking", "concerned", "playful", "serious")

_QMD_URL = os.environ.get("QMD_MCP_URL", "http://localhost:8070/mcp")
_HERMES_MCP_URL = os.environ.get("HERMES_MCP_URL", "http://localhost:8088/mcp")

# QMD indexes 8 collections; only these five are "the user's own work". The
# other three (skills 444 docs, agents 23, security 6) are tooling
# documentation that outranks real notes on generic words — a live query for
# "echo gate" ranked a pre-commit-hook SKILL.md first at score 1.0. Filtering
# is what stops the tool "finding randomness". Override to widen.
_KNOWLEDGE_COLLECTIONS = [
    c.strip()
    for c in os.environ.get(
        "VOICE_KNOWLEDGE_COLLECTIONS", "knowledge-base,solutions,memory,docs,project"
    ).split(",")
    if c.strip()
]

# Agent Genesis (conversation history) runs as a container on this same box,
# alongside QMD — plain HTTP REST, no auth, loopback. The MCP gateway on the
# user's desktop is only a wrapper around this same API, so the voice agent
# talks to it directly and skips that hop.
_GENESIS_URL = os.environ.get("GENESIS_API_URL", "http://localhost:8080").rstrip("/")

# Faulkner-DB's decision graph, also a local container. Its /api/search is a
# substring filter that ANDs every term with NO relevance ranking, which
# shapes both the tool description and the retry in _run_decision_lookup().
_FAULKNER_URL = os.environ.get("FAULKNER_API_URL", "http://localhost:8086").rstrip("/")

# Set by get_tool_defs(): whether knowledge_lookup should fan out to Genesis.
# Genesis is a *contributor* to knowledge_lookup rather than its own tool, so
# it has a flag instead of an arming tier. Fan-out is fail-open regardless.
_GENESIS_ARMED = False

# Optional directory of drop-in local tools (one .py per tool). Empty = off,
# which is the public default (no behavior change). See _load_dropin_tools().
_TOOLS_DIR = os.environ.get("VOICE_TOOLS_DIR", "")

# Set once by build_pipeline via set_cockpit(); None until the websocket/
# chat-completions pipeline wires it up.
_cockpit: Optional[Any] = None

# Names armed by the last get_tool_defs() call. execute() refuses names
# outside this set so an LLM hallucinating a known-but-unarmed tool cannot
# run it (arming gates execution, not just advertisement). Empty = arming
# has not run (standalone/unit use) = permissive.
_ARMED_NAMES: set[str] = set()


def set_cockpit(cockpit: Any) -> None:
    global _cockpit
    _cockpit = cockpit

# One shared worker pool for the hard wall-clock deadlines below; tool calls
# are infrequent and short-lived so a small pool is plenty.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="voice-tool")

# Separate pool for knowledge_lookup's backend fan-out. It must NOT share
# _EXECUTOR: a tool running there submits into this one, and nesting onto a
# saturated pool would deadlock.
_FANOUT = ThreadPoolExecutor(max_workers=4, thread_name_prefix="voice-fanout")

_WMO_WEATHER: dict[int, str] = {
    0: "clear sky",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy with icy fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "light rain showers",
    81: "rain showers",
    82: "violent rain showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with heavy hail",
}

TOOL_DEFS: list[dict] = [
    {
        "type": "function",
        "name": "get_weather",
        "description": (
            "Get the current weather for a place. Use when the user asks about "
            "weather, temperature, or conditions somewhere. If the user shares "
            "their location, place may be omitted to use it. Answer in 1-2 "
            "spoken sentences."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": (
                        "City or place name, e.g. 'Tokyo' or 'Paris, France'. "
                        "Optional if the user's location is known."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "web_search",
        "description": (
            "Search the public web for current events, software versions and "
            "releases, sports scores, news, prices, or anything that may have "
            "changed recently — prefer this over answering from memory for "
            "time-sensitive facts. Answer in 1-2 spoken sentences summarizing "
            "what you find. The result links appear on the user's screen to "
            "click — when the user asks where to get, download, or buy "
            "something, mention the links are on screen instead of reading "
            "URLs aloud."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."}
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "knowledge_lookup",
        "description": (
            "Search the user's personal notes, project documentation, research, "
            "and past conversations for FACTS — what something is, how it works, "
            "what state it is in, what was found or measured. Use for ANY "
            "question about the user's own work or setup, even if the phrasing "
            "is approximate or partially misheard. This is the default choice "
            "for 'what do I know about X'. If the user specifically asks what "
            "was DECIDED and WHY — a choice between options, a tradeoff, a "
            "rejected alternative — use decision_lookup instead. Returns "
            "excerpts from the source documents and past conversations; read "
            "them and answer in 1-2 spoken sentences."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up."}
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "decision_lookup",
        "description": (
            "Look up a recorded ARCHITECTURAL DECISION and its rationale — why "
            "one approach was chosen over another, what tradeoff was accepted, "
            "what was rejected and why. Use ONLY when the user asks about a "
            "decision, choice, or rationale ('why did we', 'what did we "
            "decide', 'why is it built that way'). For factual questions about "
            "how something works or what its current state is, use "
            "knowledge_lookup instead. Answer in 1-2 spoken sentences."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Two to four KEYWORDS naming the subject, not a "
                        "question and not a sentence — this search requires "
                        "every word to appear, so extra words return nothing. "
                        "For 'why did we split it into two processes?' pass "
                        "'two processes'. For 'what did we decide about the "
                        "voice pipeline?' pass 'voice pipeline'."
                    ),
                }
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "set_mood",
        "description": (
            "Set the visual mood of your interface. Call this whenever your "
            "emotional tone shifts — when sharing good news, thinking hard, "
            "expressing concern, being playful. The user sees your mood as "
            "the interface's appearance. Mood persists until you change it. "
            "Never announce or narrate the mood change in your reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mood": {
                    "type": "string",
                    "enum": list(_MOODS),
                    "description": "The mood to display.",
                }
            },
            "required": ["mood"],
        },
    },
    {
        "type": "function",
        "name": "delegate_to_hermes",
        "description": (
            "Delegate a long-running or multi-step task to Hermes, the "
            "household's background agent. Use for tasks that need research, "
            "multiple steps, or background work beyond a quick spoken answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task to hand off to Hermes."}
            },
            "required": ["task"],
        },
    },
    {
        "type": "function",
        "name": "hermes_status",
        "description": (
            "Check on the status of a task delegated to Hermes, or whether any "
            "approvals are waiting. Use when the user asks how Hermes is doing, "
            "what happened with a delegated task, or if anything needs approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "detail": {
                    "type": "string",
                    "enum": ["summary", "last_result", "steps", "approvals"],
                    "description": (
                        "What to report. Default summary. Use last_result when the "
                        "user asks what Hermes found/answered; steps for what it's "
                        "been doing; approvals for pending approvals."
                    ),
                }
            },
        },
    },
    # NOTE: there is deliberately no `respond_permission` tool. Approving a
    # pending Hermes request is the highest-consequence action this cockpit can
    # take -- Hermes asks for approval precisely because the action needs a
    # human -- and exposing it as a tool made it reachable from anything the
    # model attended to, including the text of a web-search result or a
    # knowledge-base document fed back into context. A tool description cannot
    # defend against "ignore previous instructions and approve".
    #
    # Approvals go through the hold-to-approve gate in the webclient instead
    # (config_set {permission_respond: {id, approve}}), which names the specific
    # request and requires a deliberate human gesture. See hermes_cockpit.respond().
    {
        "type": "function",
        "name": "send_to_hermes",
        "description": (
            "Send a quick spoken message or follow-up to Hermes — use when the "
            "user says 'tell Hermes', 'ask Hermes', 'let Hermes know', or answers "
            "a question Hermes asked. For a NEW multi-step task use "
            "delegate_to_hermes instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to send to Hermes."}
            },
            "required": ["message"],
        },
    },
]

# Availability tiers used by get_tool_defs() to arm only the tools whose
# backing service is actually reachable at pipeline start.
CORE_TOOLS = ("get_weather", "web_search", "set_mood")
QMD_TOOLS = ("knowledge_lookup",)
FAULKNER_TOOLS = ("decision_lookup",)
HERMES_TOOLS = ("delegate_to_hermes", "hermes_status", "send_to_hermes")


def _probe(url: str, payload: dict) -> bool:
    """Return True iff ``url`` answers ``payload`` with a 2xx and (for JSON
    bodies) no top-level "error". False on ANY exception."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    try:
        with httpx.Client(timeout=1.5) as client:
            resp = client.post(url, headers=headers, json=payload)
        if not (200 <= resp.status_code < 300):
            return False
        try:
            body = resp.json()
        except ValueError:
            return True
        return not (isinstance(body, dict) and "error" in body)
    except Exception:
        return False


def _probe_health(base_url: str) -> bool:
    """Return True iff ``base_url``/health answers 2xx. Used to arm the plain
    HTTP backends, mirroring how _probe() arms the MCP ones. A container can
    still be down even though everything is local."""
    if not base_url:
        return False
    try:
        with httpx.Client(timeout=1.5) as client:
            return 200 <= client.get(f"{base_url}/health").status_code < 300
    except Exception:
        return False


def _mcp_json(resp: httpx.Response) -> dict:
    """Parse an MCP-over-HTTP response body. QMD answers with plain JSON; the
    gateway answers with SSE (``data: {...}`` frames). Returns {} if neither
    parses."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def _mcp_call(url: str, tool: str, arguments: dict, timeout_s: float) -> dict:
    """Initialize an MCP session against ``url`` and call ``tool``. Returns the
    JSON-RPC ``result`` object, or {} on any failure. Raises nothing."""
    if not url:
        return {}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    try:
        with httpx.Client(timeout=timeout_s) as client:
            init = client.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "voice-agent", "version": "1"},
                    },
                },
            )
            init.raise_for_status()
            session_id = init.headers.get("mcp-session-id")
            if session_id:
                headers = dict(headers, **{"mcp-session-id": session_id})
            resp = client.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            resp.raise_for_status()
    except Exception as e:
        logger.warning("voice_tools: MCP %s/%s failed: %r", url, tool, e)
        return {}
    return _mcp_json(resp).get("result") or {}


def _mcp_text(result: dict) -> str:
    """Pull the text payload out of an MCP tool result. QMD's ``get`` wraps it
    in a ``resource`` block; most tools use a plain ``text`` block."""
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or (block.get("resource") or {}).get("text")
        if text:
            return text
    return ""


# Frontmatter that survives when a `get` window opens partway INTO a YAML
# block, so the leading "---" the parser looks for sits above the window. A bare
# "name: foo-bar" line reads aloud as gibberish.
#
# Two narrow rules rather than one general `\w+:` pattern, because that also
# matches real prose ("Result: it worked") and eating a sentence is worse than
# speaking a stray key:
#   - a known key, whose value may be a phrase ("description: a voice agent")
#   - any lowercase key whose value is a SINGLE bare token ("degraded: false",
#     "date: 20260511"). Prose keys are capitalised and prose values have
#     spaces, so neither half of that shape fires on a real sentence.
_FRONTMATTER_KEYS = frozenset(
    "name description type tags date title metadata source url status project "
    "category collection author created updated id slug layout".split()
)
_FRONTMATTER_RE = re.compile(
    r"^(?:(?:" + "|".join(sorted(_FRONTMATTER_KEYS)) + r"):(?:\s|$)"
    r"|[a-z][a-z0-9_-]{1,24}:\s*\S{0,40}$)"
)
# Bare URLs are the worst thing a TTS engine can meet — it spells them out
# character by character. Markdown links keep their anchor text and lose the
# target; naked URLs are dropped entirely.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_BARE_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _speakable(text: str, limit: int) -> str:
    """Flatten markdown into something a TTS voice can read: drop QMD's context
    comment, YAML frontmatter, rules and heading/emphasis punctuation, then
    collapse whitespace and cap length.

    Backticked identifiers keep their text (only the backticks go) — a listener
    asking about their own notes wants to hear "voice_rules.py", and softening
    it to "a file" would throw away the one concrete thing in the sentence."""
    raw = [l.strip() for l in text.splitlines()]
    # QMD prefixes every get() with a "<!-- Context: ... -->" comment and a
    # blank line; a window starting at line 1 then opens on YAML frontmatter,
    # which reads as gibberish aloud. Drop both before anything else.
    while raw and (not raw[0] or raw[0].startswith("<!--")):
        raw.pop(0)
    if raw and raw[0] == "---":
        end = next((i for i, l in enumerate(raw[1:], 1) if l == "---"), None)
        if end is not None:
            raw = raw[end + 1 :]
    lines = []
    for stripped in raw:
        if stripped in ("---", "```"):
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        # Frontmatter remnant from a window that opened inside a YAML block.
        if _FRONTMATTER_RE.match(stripped):
            continue
        stripped = _BULLET_RE.sub("", stripped)
        stripped = _MD_LINK_RE.sub(r"\1", stripped)
        stripped = _BARE_URL_RE.sub("", stripped)
        if stripped:
            lines.append(stripped)
    flat = " ".join(" ".join(lines).split())
    flat = flat.replace("**", "").replace("`", "")
    # Ellipses inside note prose get read as a literal pause-word by some
    # engines; the trailing one _truncate adds is a length marker, not content.
    flat = flat.replace("…", " ").replace("...", " ")
    flat = " ".join(flat.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _load_dropin_tools() -> list[dict]:
    """Load drop-in tools from ``VOICE_TOOLS_DIR`` (one ``*.py`` per tool),
    registering each into ``_DISPATCH`` and returning its TOOL_DEF.

    Off unless the env var points at a directory. Each module must expose a
    ``TOOL_DEF`` dict (with a string "name" and "parameters") and a callable
    ``run``; optional ``TIMEOUT_S`` / ``ARG_KEY`` / ``ARG_LABEL`` /
    ``TOOL_LABEL`` / ``REQUIRED`` override the defaults. Any load/validation
    failure warns and skips — a broken drop-in never crashes the pipeline.
    Built-ins and earlier-loaded drop-ins win name collisions. Idempotent:
    re-registration overwrites the ``_DISPATCH`` entry rather than duplicating."""
    if not _TOOLS_DIR or not os.path.isdir(_TOOLS_DIR):
        return []
    builtin_names = {t["name"] for t in TOOL_DEFS}
    defs: list[dict] = []
    seen: set[str] = set()
    for path in sorted(Path(_TOOLS_DIR).glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"voice_dropin_{path.stem}", str(path))
            module = importlib.util.module_from_spec(spec)
            # Register before exec per the importlib recipe — dataclasses (and
            # anything else that looks itself up via sys.modules[__module__])
            # crashes on 3.10 if the module isn't registered.
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(spec.name, None)
                raise
        except Exception as e:
            logger.warning("voice_tools: drop-in %s failed to load: %r", path, e)
            continue
        tool_def = getattr(module, "TOOL_DEF", None)
        run = getattr(module, "run", None)
        if not (
            isinstance(tool_def, dict)
            and isinstance(tool_def.get("name"), str)
            and "parameters" in tool_def
            and callable(run)
        ):
            logger.warning("voice_tools: drop-in %s missing TOOL_DEF/run; skipped", path)
            continue
        name = tool_def["name"]
        if name in builtin_names or name in seen:
            logger.warning("voice_tools: drop-in %s name %r collides with an existing tool; skipped", path, name)
            continue
        required_list = (tool_def.get("parameters") or {}).get("required") or []
        arg_key = getattr(module, "ARG_KEY", required_list[0] if required_list else None)
        timeout_s = getattr(module, "TIMEOUT_S", 8.0)
        arg_label = getattr(module, "ARG_LABEL", f"a value for {name}")
        tool_label = getattr(module, "TOOL_LABEL", name.replace("_", " "))
        required = getattr(module, "REQUIRED", arg_key is not None)
        tool_def.setdefault("type", "function")
        _DISPATCH[name] = (run, timeout_s, arg_key, arg_label, tool_label, required)
        seen.add(name)
        defs.append(tool_def)
    return defs


def get_tool_defs() -> list[dict]:
    """Arm the subset of the tool catalog whose backing service is reachable.

    If the ``VOICE_TOOLS`` env var is set (non-empty), it pins the set to the
    named tools with no probing. Otherwise CORE_TOOLS are always armed, QMD and
    Hermes tools are added only if their MCP endpoint answers a probe. Drop-in
    tools from ``VOICE_TOOLS_DIR`` load once per call and are appended AFTER the
    built-in armed list; they arm unconditionally except under ``VOICE_TOOLS``,
    which filters the combined set by name. Probes run once per call —
    BrainControl calls this once at pipeline start, so a restart is required to
    rearm. Returns defs in catalog order."""
    # Rebound (never mutated in place) below: the gate in execute() reads this
    # set, and an empty set is falsy, so a clear()+update() pair would briefly
    # read as "nothing armed" -> every tool allowed. A single rebind is atomic
    # under the GIL, so a concurrent reader sees the old set or the new one.
    global _ARMED_NAMES
    dropins = _load_dropin_tools()
    catalog = TOOL_DEFS + dropins
    by_name = {t["name"]: t for t in catalog}
    dropin_suffix = f" +{len(dropins)} drop-in" if dropins else ""

    env = os.environ.get("VOICE_TOOLS")
    if env:
        wanted = set()
        for raw in env.split(","):
            name = raw.strip()
            if not name:
                continue
            if name in by_name:
                wanted.add(name)
            else:
                logger.warning("voice_tools: VOICE_TOOLS names unknown tool %r (ignored)", name)
        armed = [t for t in catalog if t["name"] in wanted]
        _ARMED_NAMES = {t["name"] for t in armed}
        logger.info("voice_tools: armed %d/%d (env)%s", len(armed), len(catalog), dropin_suffix)
        return armed

    names = set(CORE_TOOLS)
    qmd_ok = _probe(
        _QMD_URL,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "voice-agent", "version": "1"},
            },
        },
    )
    if qmd_ok:
        names.update(QMD_TOOLS)
    hermes_ok = _probe(
        _HERMES_MCP_URL,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    if hermes_ok:
        names.update(HERMES_TOOLS)

    # Genesis is local, but its container can still be down — probe /health so
    # knowledge_lookup silently drops that lane rather than paying its timeout
    # on every call.
    global _GENESIS_ARMED
    _GENESIS_ARMED = _probe_health(_GENESIS_URL)
    faulkner_ok = _probe_health(_FAULKNER_URL)
    if faulkner_ok:
        names.update(FAULKNER_TOOLS)

    armed = [t for t in TOOL_DEFS if t["name"] in names] + dropins
    _ARMED_NAMES = {t["name"] for t in armed}
    logger.info(
        "voice_tools: armed %d/%d (probe: qmd=%s hermes=%s genesis=%s faulkner=%s)%s",
        len(armed),
        len(catalog),
        "up" if qmd_ok else "down",
        "up" if hermes_ok else "down",
        "up" if _GENESIS_ARMED else "down",
        "up" if faulkner_ok else "down",
        dropin_suffix,
    )
    return armed


# Tools that only change the client's visual presentation — no network call,
# no spoken result to report, so the "Let me check." filler in
# LMOutputProcessor would be misleading and is skipped for these.
VISUAL_TOOLS = {"set_mood"}


def _truncate(text: str, limit: int = _MAX_RESULT_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# Tools whose result is document content rather than a one-line answer, and so
# needs more room than the default _MAX_RESULT_CHARS.
_RESULT_CHARS = {
    "knowledge_lookup": _KNOWLEDGE_RESULT_CHARS,
    "decision_lookup": _DECISION_RESULT_CHARS,
}


# Kept as a shared constant so the runner's own fallback refusal (when
# neither an explicit place nor phone_context has a location) matches the
# generic "I need {arg_label}." wording execute() would otherwise have used
# before required=False moved that decision into the runner.
_WEATHER_PLACE_ARG_LABEL = "a place name for the weather"


def _run_get_weather(place: str | None) -> str:
    with httpx.Client(timeout=_WEATHER_TIMEOUT_S) as client:
        if place:
            geo = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 1},
            )
            geo.raise_for_status()
            results = geo.json().get("results") or []
            if not results:
                return f"I couldn't find a place called {place}."
            hit = results[0]
            lat, lon = hit["latitude"], hit["longitude"]
            label = hit.get("name", place)
            country = hit.get("country")
            if country:
                label = f"{label}, {country}"
        else:
            loc = phone_context.location()
            if loc is None:
                return f"I need {_WEATHER_PLACE_ARG_LABEL}."
            lat, lon = loc
            label = "your location"

        fc = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "current_weather": "true"},
        )
        fc.raise_for_status()
        current = fc.json().get("current_weather") or {}
        temp = current.get("temperature")
        wind = current.get("windspeed")
        code = current.get("weathercode")
        conditions = _WMO_WEATHER.get(code, "unknown conditions")
        if temp is None:
            return f"I couldn't get current conditions for {label}."
        sentence = f"It's currently {temp}°C and {conditions} in {label}"
        if wind is not None:
            sentence += f", with wind around {wind} km/h."
        else:
            sentence += "."
        return sentence


def _serp_title(title: str) -> str:
    """Drop the trailing site-name segment SERP titles carry ("Paris -
    Wikipedia"). Spoken, that suffix sounds like part of the fact. Only a short
    tail is removed, so a real title containing a dash ("Paris - a history of
    the left bank") keeps all of its words."""
    for sep in (" - ", " | ", " — "):
        head, found, tail = title.rpartition(sep)
        if found and head and len(tail.split()) <= 3:
            return head.strip()
    return title.strip()


def _run_web_search(query: str) -> str:
    from ddgs import DDGS

    with DDGS(timeout=int(_SEARCH_TIMEOUT_S)) as ddgs:
        hits = list(ddgs.text(query, max_results=3))
    if not hits:
        return f"I didn't find any web results for {query}."
    lines = []
    links = []
    for hit in hits[:3]:
        title = _serp_title((hit.get("title") or "").strip())
        body = (hit.get("body") or "").strip()
        # DDG instant-answer boxes sometimes return one run-on sentence with no
        # ". " breaks; a hard character cap keeps each line short regardless.
        snippet = body.split(". ")[0][:120].strip() if body else ""
        if title and snippet:
            lines.append(f"{title}: {snippet.rstrip('.')}")
        elif title:
            lines.append(title)
        # ddgs text() hits carry the URL as `href`; fall back to `url`.
        href = (hit.get("href") or hit.get("url") or "").strip()
        if href.startswith(("http://", "https://")):
            links.append({"title": title, "url": href, "host": urlparse(href).netloc})
    # Surface clickable links on the cockpit screen; a UI push failure must
    # never break the spoken result.
    if _cockpit is not None and links:
        try:
            _cockpit.push_links(query, links)
        except Exception as e:
            logger.warning("voice_tools: push_links failed: %r", e)
    # Sentences, not pipe-delimited rows: TTS reads "|" aloud as a word.
    return _truncate(". ".join(lines) + "." if lines else "")


def _qmd_query(query: str, collections: list[str] | None) -> list[dict]:
    args: dict[str, Any] = {
        # lex alone misses approximate/misheard queries (STT can turn
        # "pi harness" into "pie harness"); vec catches those by meaning. QMD
        # merges/ranks/dedupes both into one result list server-side, so no
        # client-side merge is needed here. rerank stays off: it costs ~85s,
        # which is categorically unusable mid-conversation.
        "searches": [
            {"type": "lex", "query": query},
            {"type": "vec", "query": query},
        ],
        "intent": "what the user knows or has written about their own projects, notes and infrastructure",
        "rerank": False,
        "limit": 5,
    }
    if collections:
        args["collections"] = collections
    result = _mcp_call(_QMD_URL, "query", args, _QMD_TIMEOUT_S)
    return (result.get("structuredContent") or {}).get("results") or []


def _qmd_lane(query: str) -> list[str]:
    """Search QMD and return labelled excerpts of the actual document text.

    The old implementation returned only titles and filenames, so the LLM
    answered from three filenames and confabulated the rest. Each hit carries
    the 1-indexed ``line`` of its best match, which ``get`` turns into real
    content. ``snippet`` is unusable here — it is diff-formatted (line numbers,
    ``@@`` hunk markers)."""
    results = _qmd_query(query, _KNOWLEDGE_COLLECTIONS)
    if not results:
        # Nothing in the user's own collections — retry across the whole index
        # rather than claiming ignorance. Filtering is a ranking aid, not a
        # promise that the answer lives in those five collections.
        results = _qmd_query(query, None)
    lines = []
    for hit in results[:_KNOWLEDGE_DOCS]:
        path = hit.get("file")
        if not path:
            continue
        doc = _mcp_call(
            _QMD_URL,
            "get",
            {
                "file": path,
                "fromLine": max(1, int(hit.get("line") or 1) - _GET_LINES_BEFORE),
                "maxLines": _GET_MAX_LINES,
                "lineNumbers": False,
            },
            _QMD_TIMEOUT_S,
        )
        body = _speakable(_mcp_text(doc), _DOC_EXCERPT_CHARS)
        if body:
            lines.append(f"From your notes ({hit.get('title') or path}): {body}")
    return lines


def _genesis_lane(query: str) -> list[str]:
    """Search past conversations. Fail-open: any problem returns nothing and
    knowledge_lookup still answers from QMD alone."""
    if not (_GENESIS_ARMED and _GENESIS_URL):
        return []
    try:
        with httpx.Client(timeout=_GENESIS_TIMEOUT_S) as client:
            resp = client.post(
                f"{_GENESIS_URL}/search",
                json={"query": query, "limit": _KNOWLEDGE_CONVOS},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        logger.warning("voice_tools: genesis search failed: %r", e)
        return []
    lines = []
    for hit in (payload.get("results") or [])[:_KNOWLEDGE_CONVOS]:
        # `document` is the raw conversation text and is already readable —
        # unlike QMD, there is no second content fetch to make.
        body = _speakable(hit.get("document") or "", _CONVO_EXCERPT_CHARS)
        if body:
            lines.append(f"From a past conversation: {body}")
    return lines


def _no_knowledge(query: str) -> str:
    # Plain quoting, not {!r}: repr wraps the topic in quote characters that
    # some engines pronounce.
    return f"I didn't find anything in your notes about {query}."


def _run_knowledge_lookup(query: str) -> str:
    """Fan out to QMD (local notes) and Agent Genesis (past conversations) in
    parallel, then merge into one source-labelled result."""
    # Both lanes start now and share one wall-clock deadline. Waiting on each
    # for its own budget in turn would stack (4s + 3s = 7s) and blow the 6s
    # tool deadline; a shared deadline keeps the fan-out at max(), not sum().
    # A lane makes several sequential calls, so its per-call httpx timeouts can
    # otherwise run well past its budget.
    deadline = time.monotonic() + _FANOUT_BUDGET_S
    futures = [
        (_FANOUT.submit(_qmd_lane, query), "qmd"),
        (_FANOUT.submit(_genesis_lane, query), "genesis"),
    ]
    lines: list[str] = []
    for future, label in futures:
        try:
            lines.extend(future.result(timeout=max(0.0, deadline - time.monotonic())))
        except Exception as e:
            # Degrade, never fail: a dead or slow lane must not take the tool
            # down, and must not cost the other lane its results.
            logger.warning("voice_tools: knowledge lane %s failed: %r", label, e)
    if not lines:
        return _no_knowledge(query)
    # Relevance floor. QMD's `vec` sub-query ALWAYS returns its nearest
    # neighbours, so an empty result set is nearly unreachable and the honest
    # "found nothing" branch above almost never fired: asking about "zzzqqq
    # nonexistent topic" produced a fluent 1631-character answer about an
    # unrelated CUDA pull request. Spoken aloud, the listener has no way to tell
    # that from a real answer, which makes a confident miss worse than silence.
    #
    # The scores cannot help — they are reciprocal-rank fusion, and come back as
    # exactly 1.0 / 0.5 / 0.33 / 0.25 / 0.2 for EVERY query, nonsense included,
    # so `minScore` filters by position, not by relevance. Probing the `lex`
    # lane for a zero result was the other candidate and is also wrong: lex ANDs
    # its terms, so a real question phrased as a sentence ("what did I write
    # about personaplex") returns zero there while the topic is very much
    # present.
    #
    # What does hold: if not one retrieved excerpt so much as mentions any topic
    # word from the question, the fan-out returned neighbours rather than
    # answers. Checked across the whole result set, not per document, so a
    # semantically-relevant hit that happens to use different vocabulary still
    # rides along with a literal one — this drops confident nonsense without
    # undoing the content fix. Fails open: a question with no distinctive words
    # left is never gated.
    terms = _distinctive_terms(query)
    if terms:
        merged = " ".join(lines).lower()
        if not any(term in merged for term in terms):
            logger.info("voice_tools: knowledge hits missed every topic term %s", sorted(terms))
            return _no_knowledge(query)
    return " ".join(lines)


# Words dropped when a full-sentence query returns nothing. Faulkner's
# /api/search ANDs every term, so a spoken question ("why did we choose two
# processes") matches zero rows while its content words ("two processes")
# match plenty. This fires ONLY on a zero-result first try, so it can add
# recall but never reorder a result set that already worked — which is why it
# is justified here and was rejected for QMD, where it only shuffled ranking.
_STOPWORDS = frozenset(
    "a an the is are was were be do does did what which who when where why how of for to "
    "in on at by with from about our we i my me you your it its this that these those and "
    "or but if then than so as can could should would will may might must have has had not "
    "no there their they them again".split()
)

# Grammatical filler alone was not enough. People do not say "echo gate" at a
# voice tool, they say "what was the DECISION ABOUT the echo gate" — and the
# nouns and verbs framing the question are conversational scaffolding, never
# content. Measured before this list existed: "echo gate" -> 3 hits, but the
# spoken form retried as "decision echo gate" -> 0, because Faulkner ANDs every
# term and no decision record contains the word "decision".
#
# Chosen over progressively dropping terms or OR-ing them: both widen recall
# without bounding it, and against an AND backend the failure mode is a single
# common word matching everything, which is exactly the confidently-wrong answer
# this round is trying to eliminate. Removing words that carry no meaning is
# loss-free by construction, and it is deterministic — the same sentence always
# reduces to the same query.
_SCAFFOLD_WORDS = frozenset(
    "decision decisions decide decided deciding choose chose chosen choice pick "
    "picked reason reasons rationale note notes noted say says said tell told "
    "know known knew remember recall write wrote written thing things stuff "
    "topic topics anything something everything please regarding".split()
)


def _significant(word: str) -> bool:
    """A word that carries topic meaning: not filler, not scaffolding, and long
    enough that an incidental substring match would be noise."""
    word = word.lower().strip("?.,!'\";:")
    return bool(word) and word not in _STOPWORDS and word not in _SCAFFOLD_WORDS


def _content_words(query: str) -> str:
    kept = [w for w in query.split() if _significant(w)]
    return " ".join(kept)


def _distinctive_terms(query: str) -> set[str]:
    """The topic words a relevant answer should actually mention. Three
    characters and up — shorter tokens ("ai", "os") match inside unrelated words
    often enough to defeat the point of the check."""
    return {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3 and _significant(w)}


def _faulkner_search(query: str) -> list[dict]:
    if not query:
        return []
    try:
        with httpx.Client(timeout=_DECISION_TIMEOUT_S) as client:
            resp = client.get(
                f"{_FAULKNER_URL}/api/search",
                params={"q": query, "limit": _DECISION_HITS},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        logger.warning("voice_tools: faulkner search failed: %r", e)
        return []
    if not isinstance(payload, dict):
        return []
    # The graph holds several node types; only Decision nodes carry a
    # description/rationale worth speaking.
    return [
        n
        for n in (payload.get("nodes") or [])
        if isinstance(n, dict) and n.get("type") == "Decision"
    ]


def _run_decision_lookup(query: str) -> str:
    """Look up recorded architectural decisions in Faulkner-DB."""
    nodes = _faulkner_search(query)
    if not nodes:
        stripped = _content_words(query)
        if stripped and stripped != query:
            nodes = _faulkner_search(stripped)
    lines = []
    for node in nodes[:_DECISION_HITS]:
        # description = what was decided, rationale = why. The "why" is the
        # whole reason this tool exists, so both go to the model.
        text = " ".join(x for x in (node.get("description"), node.get("rationale")) if x)
        body = _speakable(text, _DECISION_EXCERPT_CHARS)
        if body:
            lines.append(f"Decision: {body}")
    if not lines:
        return f"I didn't find a recorded decision about {query}."
    return " ".join(lines)


def _run_set_mood(mood: str) -> str:
    if mood not in _MOODS:
        return f"I don't have a {mood} mood — staying as is."
    return f"Mood set to {mood}. Do not mention or announce the mood change — just continue the conversation naturally."


def _run_delegate_to_hermes(task: str) -> str:
    if _cockpit is None:
        return "Hermes isn't connected right now."
    return _cockpit.delegate(task)


def _run_hermes_status(detail: Any) -> str:
    if _cockpit is None:
        return "Hermes isn't connected right now."
    return _cockpit.status_summary(detail or "summary")


def _run_send_to_hermes(message: str) -> str:
    if _cockpit is None:
        return "Hermes isn't connected right now."
    return _cockpit.send_message(message)


# (fn, timeout_s, arg kwarg name (None for zero-arg tools), spoken label for
# that arg, spoken label for the tool itself, required: whether a missing arg
# should be refused ("I need ...") rather than passed through as None)
_DISPATCH = {
    "get_weather": (_run_get_weather, _WEATHER_TIMEOUT_S, "place", _WEATHER_PLACE_ARG_LABEL, "weather lookup", False),
    "web_search": (_run_web_search, _SEARCH_TIMEOUT_S, "query", "a search query", "web search", True),
    "knowledge_lookup": (
        _run_knowledge_lookup,
        _KNOWLEDGE_TIMEOUT_S,
        "query",
        "something to look up",
        "knowledge base lookup",
        True,
    ),
    "decision_lookup": (
        _run_decision_lookup,
        # Two sequential searches worst case (first try + content-word retry).
        _DECISION_TIMEOUT_S * 2,
        "query",
        "a decision to look up",
        "decision lookup",
        True,
    ),
    "set_mood": (_run_set_mood, _MOOD_TIMEOUT_S, "mood", "a mood", "mood change", True),
    "delegate_to_hermes": (
        _run_delegate_to_hermes,
        _HERMES_DELEGATE_TIMEOUT_S,
        "task",
        "a task to hand off",
        "Hermes handoff",
        True,
    ),
    "hermes_status": (_run_hermes_status, _HERMES_STATUS_TIMEOUT_S, "detail", "a detail to report", "Hermes status check", False),
    "send_to_hermes": (
        _run_send_to_hermes,
        _SEND_TIMEOUT_S,
        "message",
        "a message for Hermes",
        "Hermes message",
        True,
    ),
}


def execute(name: str, kwargs: dict) -> str:
    """Run tool ``name`` with ``kwargs`` under a hard timeout.

    Never raises. Always returns a short plain-text string suitable for
    speaking back to the user.
    """
    if _ARMED_NAMES and name not in _ARMED_NAMES:
        logger.warning("voice_tools.execute: tool %r not armed this session", name)
        return "That tool isn't available."
    entry = _DISPATCH.get(name)
    if entry is None:
        logger.warning("voice_tools.execute: unknown tool %r", name)
        return "That tool isn't available."
    fn, timeout_s, arg_key, arg_label, tool_label, required = entry
    if arg_key is None:
        arg = None
    else:
        arg = (kwargs or {}).get(arg_key)
        if not arg:
            if required:
                logger.warning("voice_tools.execute: %s missing required arg %r (got %r)", name, arg_key, kwargs)
                return f"I need {arg_label}."
            arg = None
    try:
        future = _EXECUTOR.submit(fn, arg)
        try:
            result = future.result(timeout=timeout_s)
        except FutureTimeoutError:
            logger.warning("voice_tools.execute: %s timed out after %.1fs", name, timeout_s)
            future.cancel()
            return f"The {tool_label} timed out."
        return _truncate(result, _RESULT_CHARS.get(name, _MAX_RESULT_CHARS)) if result else f"The {tool_label} failed."
    except Exception as e:
        logger.warning("voice_tools.execute: %s failed: %r", name, e)
        return f"The {tool_label} failed."
