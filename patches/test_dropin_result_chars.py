"""Unit tests for voice_tools.py's optional RESULT_CHARS drop-in attribute:
a per-drop-in override of the _MAX_RESULT_CHARS (600) result cap, read in
_load_dropin_tools() next to TIMEOUT_S / ARG_KEY & co, stored in
_DROPIN_RESULT_CHARS, and consulted in execute() after the built-in
_RESULT_CHARS table (built-ins keep priority on a name collision).

Run from repo root: python3 -m pytest patches/test_dropin_result_chars.py -v

Same import stub as test_tool_confirm.py: voice_tools.py's only
speech_to_speech import is phone_context, so the real module is aliased in
before import. Drop-ins are written to a tmp_path and armed through the real
get_tool_defs() -> _load_dropin_tools() -> execute() path.
"""

from __future__ import annotations

import logging
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


_UNDECLARED = object()


def _write_dropin(tmp_path, name="doc_excerpt", result_chars=_UNDECLARED):
    """One minimal drop-in whose run() returns a fixed 2000-char string, with
    an optional RESULT_CHARS declaration spliced in before ``def run``."""
    rc = (
        f"RESULT_CHARS = {result_chars!r}\n"
        if result_chars is not _UNDECLARED
        else ""
    )
    (tmp_path / f"{name}.py").write_text(
        f'''
TOOL_DEF = {{
    "name": "{name}",
    "description": "Return a fixed excerpt (test double).",
    "parameters": {{
        "type": "object",
        "properties": {{"query": {{"type": "string", "description": "The query."}}}},
        "required": ["query"],
    }},
}}
{rc}

def run(query):
    return "x" * 2000
'''
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No env, nothing armed, no drop-in state leaking across tests: a cap
    entry that survived to the next test would make the reload test pass
    for the wrong reason."""
    monkeypatch.delenv("VOICE_TOOLS", raising=False)
    monkeypatch.delenv("VOICE_CONFIRM_TOOLS", raising=False)
    monkeypatch.delenv("VOICE_TOOLS_DIR", raising=False)
    monkeypatch.setattr(vt, "_TOOLS_DIR", "")
    monkeypatch.setattr(vt, "_ARMED_NAMES", set())
    monkeypatch.setattr(vt, "_CONSEQUENTIAL_DROPINS", set())
    # getattr fallback: pre-implementation, the attribute doesn't exist yet.
    monkeypatch.setattr(vt, "_DROPIN_RESULT_CHARS", getattr(vt, "_DROPIN_RESULT_CHARS", {}), raising=False)
    yield


def _no_probe(monkeypatch):
    monkeypatch.setattr(vt, "_probe", lambda *a, **k: False)
    monkeypatch.setattr(vt, "_probe_health", lambda *a, **k: False)


def _arm(monkeypatch, tmp_path, *extra):
    _no_probe(monkeypatch)
    monkeypatch.setattr(vt, "_TOOLS_DIR", str(tmp_path))
    vt.get_tool_defs()
    if extra:
        monkeypatch.setattr(vt, "_ARMED_NAMES", set(vt._ARMED_NAMES) | set(extra))


# ── a declared RESULT_CHARS is honoured through the real execute() path ─────

def test_declared_result_chars_is_honored_through_execute(monkeypatch, tmp_path):
    _write_dropin(tmp_path, result_chars=1800)
    _arm(monkeypatch, tmp_path)

    out = vt.execute("doc_excerpt", {"query": "q"})

    assert len(out) == 1800
    assert out.endswith("…")
    assert vt._DROPIN_RESULT_CHARS.get("doc_excerpt") == 1800


# ── no declaration: the default 600 cap still applies ────────────────────────

def test_undeclared_result_chars_keeps_the_default_cap(monkeypatch, tmp_path):
    _write_dropin(tmp_path)
    _arm(monkeypatch, tmp_path)

    out = vt.execute("doc_excerpt", {"query": "q"})

    assert len(out) == 600
    assert out.endswith("…")


# ── bad values warn, fall back to 600, and never break the drop-in ───────────

@pytest.mark.parametrize("bad", [0, -5, "big", None, True])
def test_bad_result_chars_falls_back_to_default_and_warns(
    monkeypatch, tmp_path, caplog, bad
):
    _write_dropin(tmp_path, result_chars=bad)
    with caplog.at_level(logging.WARNING):
        _arm(monkeypatch, tmp_path)

    out = vt.execute("doc_excerpt", {"query": "q"})

    assert len(out) == 600, "a bad RESULT_CHARS must fall back to the default cap"
    assert any(
        "RESULT_CHARS" in r.getMessage() and str(tmp_path) in r.getMessage()
        for r in caplog.records
    ), "an invalid RESULT_CHARS must log a warning naming the drop-in path"


# ── a colliding drop-in cannot raise the built-in's cap ──────────────────────

def test_same_named_dropin_cannot_raise_a_builtin_cap(monkeypatch, tmp_path):
    # The loader refuses the name collision, so the dispatch entry is still
    # the built-in's; swap its fn for a 2000-char recorder and prove the cap
    # applied is the built-in's 1800, not the drop-in's declared 5000.
    _write_dropin(tmp_path, name="knowledge_lookup", result_chars=5000)
    _arm(monkeypatch, tmp_path, "knowledge_lookup")

    entry = list(vt._DISPATCH["knowledge_lookup"])
    entry[0] = lambda arg: "x" * 2000
    monkeypatch.setitem(vt._DISPATCH, "knowledge_lookup", tuple(entry))

    out = vt.execute("knowledge_lookup", {"query": "q"})

    assert len(out) == 1800
    assert "knowledge_lookup" not in vt._DROPIN_RESULT_CHARS


# ── a reload with an empty dir must not retain a stale cap entry ─────────────

def test_reload_forgets_caps_of_dropins_that_are_gone(monkeypatch, tmp_path):
    _write_dropin(tmp_path, result_chars=1800)
    _arm(monkeypatch, tmp_path)
    assert vt._DROPIN_RESULT_CHARS.get("doc_excerpt") == 1800

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(vt, "_TOOLS_DIR", str(empty))
    vt.get_tool_defs()

    assert vt._DROPIN_RESULT_CHARS == {}, "a reload must not retain a stale cap entry"


# ── test-prefixed files in VOICE_TOOLS_DIR are quiet non-tools ───────────────
# The repo ships its drop-in tests beside the tools in examples/tools/, and the
# README points VOICE_TOOLS_DIR there: a future test module with a module-level
# TOOL_DEF + run must not arm itself as a live voice tool.

def test_test_prefixed_file_is_not_armed_even_with_valid_tool(monkeypatch, tmp_path):
    _write_dropin(tmp_path, name="test_sneaky")  # a fully valid drop-in
    _arm(monkeypatch, tmp_path)

    assert "test_sneaky" not in vt._ARMED_NAMES
    assert "test_sneaky" not in vt._DISPATCH, "a test file must not register a dispatch entry"


def test_test_prefixed_file_logs_no_warning(monkeypatch, tmp_path, caplog):
    _write_dropin(tmp_path, name="test_home_assistant")
    with caplog.at_level(logging.WARNING):
        _arm(monkeypatch, tmp_path)

    assert not any(
        "test_home_assistant" in r.getMessage() for r in caplog.records
    ), "an expected test-file neighbour is a quiet skip, not a warning"


def test_real_tool_still_loads_beside_skipped_test_file(monkeypatch, tmp_path):
    _write_dropin(tmp_path)  # doc_excerpt
    _write_dropin(tmp_path, name="test_sneaky")
    _arm(monkeypatch, tmp_path)

    assert "doc_excerpt" in vt._ARMED_NAMES
    assert "test_sneaky" not in vt._ARMED_NAMES

    out = vt.execute("doc_excerpt", {"query": "q"})

    assert len(out) == 600


def test_underscore_prefixed_file_still_skipped(monkeypatch, tmp_path, caplog):
    _write_dropin(tmp_path, name="_private_helper")
    with caplog.at_level(logging.WARNING):
        _arm(monkeypatch, tmp_path)

    assert "_private_helper" not in vt._ARMED_NAMES
    assert not any(
        "_private_helper" in r.getMessage() for r in caplog.records
    ), "the _-prefix skip predates this change and must stay quiet too"
