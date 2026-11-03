"""Unit tests for voice_tools.py's consequential-action confirmation gate:
VOICE_CONFIRM_TOOLS (pipe-separated; "off" disables the mechanism), drop-in
modules that opt in via CONSEQUENTIAL = True, the single-use, exact-argument
(refused, confirmed) token pair, and the confirm_action tool that promotes one
token to the other.

Run from repo root: python3 -m pytest patches/test_tool_confirm.py -v

Same import stub as test_voice_tools.py: voice_tools.py's only
speech_to_speech import is phone_context, so the real module is aliased in
before import. No live service is contacted -- every gated tool's dispatch
entry is swapped for a recorder via monkeypatch.setitem, so a refusal is
provable (the recorder's call list stays empty) and a confirmation is
provable (exactly one recorded call).
"""

from __future__ import annotations

import re
import sys
import types

import pytest

from patches import phone_context


def _install_voice_tools_stub():
    pkg = sys.modules.get("speech_to_speech")
    if pkg is None:
        pkg = types.ModuleType("speech_to_speech")
        sys.modules["speech_to_speech"] = pkg
    sys.modules["speech_to_speech.phone_context"] = phone_context
    pkg.phone_context = phone_context


_install_voice_tools_stub()

from patches import voice_tools as vt  # noqa: E402

# get_weather is time-sensitive, so a successful result carries the exact stamp.
_DONE_TOKYO = r"done: 'Tokyo' \(Checked at \d{1,2}:\d{2} (AM|PM) today\.\)"


# A minimal consequential drop-in, one per tmp_path per test that needs it.
_DROPIN = '''\
TOOL_DEF = {
    "name": "send_sms",
    "description": "Send a text message (test double).",
    "parameters": {
        "type": "object",
        "properties": {"message": {"type": "string", "description": "The text."}},
        "required": ["message"],
    },
}
CONSEQUENTIAL = True


def run(message):
    return f"sms sent: {message}"
'''


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No env, nothing armed, and -- the point of this file -- no gate state
    leaking across tests: a confirmation that survived to the next test would
    silently authorize the next test's first call."""
    monkeypatch.delenv("VOICE_TOOLS", raising=False)
    monkeypatch.delenv("VOICE_CONFIRM_TOOLS", raising=False)
    monkeypatch.delenv("VOICE_TOOLS_DIR", raising=False)
    monkeypatch.setattr(vt, "_TOOLS_DIR", "")
    monkeypatch.setattr(vt, "_ARMED_NAMES", set())
    monkeypatch.setattr(vt, "_CONFIRM_NAMES", set())
    monkeypatch.setattr(vt, "_CONSEQUENTIAL_DROPINS", set())
    monkeypatch.setattr(vt, "_CONFIRM_REFUSED", None)
    monkeypatch.setattr(vt, "_CONFIRM_CONFIRMED", None)
    yield


def _record(name, monkeypatch):
    """Swap the dispatch entry for `name` for a recorder; returns the list of
    (tool, arg) tuples it fills. Restored after the test by monkeypatch."""
    calls = []
    entry = list(vt._DISPATCH[name])
    entry[0] = lambda arg, _n=name: calls.append((_n, arg)) or f"done: {arg!r}"
    monkeypatch.setitem(vt._DISPATCH, name, tuple(entry))
    return calls


def _no_probe(monkeypatch):
    monkeypatch.setattr(vt, "_probe", lambda *a, **k: False)
    monkeypatch.setattr(vt, "_probe_health", lambda *a, **k: False)


def _write_dropin(tmp_path, source=None):
    (tmp_path / "send_sms.py").write_text(source if source is not None else _DROPIN)


# ── default: empty set, nothing gated, confirm_action stays out of the catalog ──

def test_default_is_no_gating_and_no_confirm_action(monkeypatch):
    _no_probe(monkeypatch)
    calls = _record("get_weather", monkeypatch)
    armed = vt.get_tool_defs()

    assert vt._CONFIRM_NAMES == set()
    armed_names = {t["name"] for t in armed}
    assert "confirm_action" not in armed_names
    assert "confirm_action" not in vt._ARMED_NAMES

    # Ungated: the call runs straight through.
    assert re.fullmatch(_DONE_TOKYO, vt.execute("get_weather", {"place": "Tokyo"}))
    assert calls == [("get_weather", "Tokyo")]


# ── env-named tool: first call refused, underlying function not called ───────

def test_env_named_tool_is_refused_before_it_runs(monkeypatch):
    monkeypatch.setenv("VOICE_CONFIRM_TOOLS", "get_weather")
    _no_probe(monkeypatch)
    calls = _record("get_weather", monkeypatch)

    vt.get_tool_defs()

    assert vt._CONFIRM_NAMES == {"get_weather"}
    out = vt.execute("get_weather", {"place": "Tokyo"})
    assert calls == [], "a refused call must not reach the underlying function"
    assert vt._CONFIRM_REFUSED == ("get_weather", "Tokyo")
    assert vt._CONFIRM_CONFIRMED is None
    assert "confirm_action" in out, "the refusal must tell the model what to do next"


# ── refused -> confirm_action -> same call with the same arg runs ────────────

def test_confirm_then_same_call_with_same_arg_runs(monkeypatch):
    calls = _record("get_weather", monkeypatch)
    vt._CONFIRM_NAMES = {"get_weather"}

    vt.execute("get_weather", {"place": "Tokyo"})
    assert "confirmed" in vt.execute("confirm_action", {"action": "get_weather"}).lower()

    assert re.fullmatch(_DONE_TOKYO, vt.execute("get_weather", {"place": "Tokyo"}))
    assert calls == [("get_weather", "Tokyo")]


# ── single use: an immediate identical repeat is refused again ───────────────

def test_confirmation_token_is_single_use(monkeypatch):
    calls = _record("get_weather", monkeypatch)
    vt._CONFIRM_NAMES = {"get_weather"}

    vt.execute("get_weather", {"place": "Tokyo"})
    vt.execute("confirm_action", {"action": "get_weather"})
    assert re.fullmatch(_DONE_TOKYO, vt.execute("get_weather", {"place": "Tokyo"}))

    out = vt.execute("get_weather", {"place": "Tokyo"})
    assert calls == [("get_weather", "Tokyo")], "the repeat must be refused"
    assert "confirm_action" in out


# ── exactness: confirming the tool does not authorize a different arg ────────

def test_confirmation_is_scoped_to_the_refused_argument(monkeypatch):
    calls = _record("send_to_hermes", monkeypatch)
    vt._CONFIRM_NAMES = {"send_to_hermes"}

    vt.execute("send_to_hermes", {"message": "A"})
    vt.execute("confirm_action", {"action": "send_to_hermes"})

    out = vt.execute("send_to_hermes", {"message": "B"})
    assert calls == [], "confirming a send of A must not authorize a send of B"
    assert "confirm_action" in out


# ── confirm_action naming a tool that was not (yet) refused confirms nothing ─

def test_confirm_action_naming_a_never_refused_tool_confirms_nothing(monkeypatch):
    _record("get_weather", monkeypatch)
    vt._CONFIRM_NAMES = {"get_weather"}

    out = vt.execute("confirm_action", {"action": "get_weather"})
    assert "waiting" in out.lower(), "nothing was refused, so nothing could be confirmed"
    assert vt._CONFIRM_CONFIRMED is None
    assert vt._CONFIRM_REFUSED is None

    # And the first call is still refused, recording the refusal.
    out = vt.execute("get_weather", {"place": "Tokyo"})
    assert "confirm_action" in out
    assert vt._CONFIRM_REFUSED == ("get_weather", "Tokyo")


def test_confirm_action_naming_a_different_tool_confirms_nothing(monkeypatch):
    _record("get_weather", monkeypatch)
    vt._CONFIRM_NAMES = {"get_weather"}

    vt.execute("get_weather", {"place": "Tokyo"})

    vt.execute("confirm_action", {"action": "send_to_hermes"})
    assert vt._CONFIRM_CONFIRMED is None

    # The original refusal stands: the right name still works afterwards.
    vt.execute("confirm_action", {"action": "get_weather"})
    assert vt._CONFIRM_CONFIRMED == ("get_weather", "Tokyo")


# ── "off" disables the mechanism even against drop-in declarations ───────────

def test_off_disables_the_mechanism_even_with_consequential_dropin(monkeypatch, tmp_path):
    _write_dropin(tmp_path)
    monkeypatch.setattr(vt, "_TOOLS_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_CONFIRM_TOOLS", "off")
    _no_probe(monkeypatch)

    armed = {t["name"] for t in vt.get_tool_defs()}

    assert vt._CONFIRM_NAMES == set()
    assert "confirm_action" not in vt._ARMED_NAMES
    calls = _record("send_sms", monkeypatch)
    assert vt.execute("send_sms", {"message": "hi"}) == "done: 'hi'"
    assert calls == [("send_sms", "hi")]


# ── a drop-in declaring CONSEQUENTIAL = True is gated ────────────────────────

def test_consequential_dropin_is_gated(monkeypatch, tmp_path):
    _write_dropin(tmp_path)
    monkeypatch.setattr(vt, "_TOOLS_DIR", str(tmp_path))
    _no_probe(monkeypatch)

    vt.get_tool_defs()

    assert vt._CONFIRM_NAMES == {"send_sms"}
    assert "confirm_action" in vt._ARMED_NAMES, "a gated drop-in must arm confirm_action"
    calls = _record("send_sms", monkeypatch)

    out = vt.execute("send_sms", {"message": "hi"})
    assert calls == []
    assert "confirm_action" in out

    vt.execute("confirm_action", {"action": "send_sms"})
    assert vt.execute("send_sms", {"message": "hi"}) == "done: 'hi'"
    assert calls == [("send_sms", "hi")]


# ── confirm_action must never itself be gated, whatever the env says ─────────

def test_confirm_action_is_never_gated_even_when_named(monkeypatch):
    _record("get_weather", monkeypatch)
    monkeypatch.setenv("VOICE_CONFIRM_TOOLS", "confirm_action|get_weather")
    _no_probe(monkeypatch)

    vt.get_tool_defs()

    assert "confirm_action" not in vt._CONFIRM_NAMES
    vt.execute("get_weather", {"place": "Tokyo"})
    vt.execute("confirm_action", {"action": "get_weather"})
    assert vt._CONFIRM_CONFIRMED == ("get_weather", "Tokyo"), (
        "confirm_action was gated; the refusal/confirm pair would be unbreakable"
    )


# ── confirm_action arms on BOTH get_tool_defs() return paths, iff needed ─────

def test_confirm_action_arms_on_env_pin_path_only_when_something_is_gated(monkeypatch):
    monkeypatch.setenv("VOICE_TOOLS", "get_weather,web_search")
    monkeypatch.setenv("VOICE_CONFIRM_TOOLS", "get_weather")
    _no_probe(monkeypatch)

    armed = {t["name"] for t in vt.get_tool_defs()}
    assert "confirm_action" in armed
    assert "confirm_action" in vt._ARMED_NAMES

    # Nothing gated -> an unusable tool is prompt noise; it stays out.
    monkeypatch.delenv("VOICE_CONFIRM_TOOLS")
    armed = {t["name"] for t in vt.get_tool_defs()}
    assert "confirm_action" not in armed
    assert "confirm_action" not in vt._ARMED_NAMES


def test_confirm_action_arms_on_probe_path_only_when_something_is_gated(monkeypatch):
    _no_probe(monkeypatch)
    monkeypatch.setenv("VOICE_CONFIRM_TOOLS", "web_search")

    armed = {t["name"] for t in vt.get_tool_defs()}
    assert "confirm_action" in armed
    assert "confirm_action" in vt._ARMED_NAMES

    monkeypatch.delenv("VOICE_CONFIRM_TOOLS")
    armed = {t["name"] for t in vt.get_tool_defs()}
    assert "confirm_action" not in armed
    assert "confirm_action" not in vt._ARMED_NAMES
