"""Tool-layer behaviour of voice_tools.execute / web_search / knowledge budgets.

Covers: actionable failure results, knowledge timeouts vs the tool deadline,
richer web_search results, the "Checked at" time stamp (built-ins and the
TIME_SENSITIVE drop-in attribute), the set_mood description, and the per-tool
result log line.

Run from repo root: python3 -m pytest patches/test_tool_layer.py -v
"""

from __future__ import annotations

import logging
import re
import sys
import time
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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("VOICE_TOOLS", raising=False)
    monkeypatch.delenv("VOICE_CONFIRM_TOOLS", raising=False)
    monkeypatch.setattr(vt, "_TOOLS_DIR", "")
    monkeypatch.setattr(vt, "_ARMED_NAMES", set())
    monkeypatch.setattr(vt, "_CONSEQUENTIAL_DROPINS", set())
    monkeypatch.setattr(vt, "_CONFIRM_NAMES", set())
    monkeypatch.setattr(vt, "_TIME_SENSITIVE_DROPINS", set(), raising=False)
    # Timed-out tools in earlier tests may still be sleeping; their straggler
    # records must not leak into this test's refusal counts.
    monkeypatch.setattr(vt, "_STRAGGLERS", {}, raising=False)
    yield


def _patch_tool(monkeypatch, name, fn, timeout_s=2.0):
    monkeypatch.setitem(vt._DISPATCH, name, (fn, timeout_s, "query", "a query", "web search", True))


_STAMP = re.compile(r" \(Checked at \d{1,2}:\d{2} (AM|PM) today\.\)$")


# ── ruling 1: failure results tell the model what to do ───────────────────

def test_timeout_result_is_actionable(monkeypatch):
    _patch_tool(monkeypatch, "web_search", lambda q: time.sleep(1.0) or "late", timeout_s=0.05)
    out = vt.execute("web_search", {"query": "x"})
    assert "Couldn't" in out and "web search" in out and "timed out" in out
    assert "don't guess" in out and "don't call it again" in out


def test_exception_result_is_actionable(monkeypatch):
    def boom(q):
        raise RuntimeError("nope")

    _patch_tool(monkeypatch, "web_search", boom)
    out = vt.execute("web_search", {"query": "x"})
    assert "Couldn't" in out and "don't guess" in out and "don't call it again" in out


def test_empty_result_is_actionable(monkeypatch):
    _patch_tool(monkeypatch, "web_search", lambda q: "")
    out = vt.execute("web_search", {"query": "x"})
    assert "Couldn't" in out and "don't guess" in out


def test_failure_is_still_logged(monkeypatch, caplog):
    def boom(q):
        raise RuntimeError("nope")

    _patch_tool(monkeypatch, "web_search", boom)
    with caplog.at_level(logging.WARNING, logger=vt.logger.name):
        vt.execute("web_search", {"query": "x"})
    assert any("web_search failed" in r.getMessage() for r in caplog.records)


# ── ruling 3: knowledge budgets ───────────────────────────────────────────

def test_knowledge_budgets_fit_measured_qmd_latency_and_the_tool_deadline():
    assert vt._QMD_TIMEOUT_S == 4.0
    assert vt._FANOUT_BUDGET_S == 5.0
    assert vt._QMD_TIMEOUT_S > 3.05  # slowest measured live QMD query
    deadline = vt._DISPATCH["knowledge_lookup"][1]
    assert vt._FANOUT_BUDGET_S < deadline
    assert vt._QMD_TIMEOUT_S <= vt._FANOUT_BUDGET_S


# ── ruling 4: richer web_search ───────────────────────────────────────────

class _FakeDDGS:
    hits: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def text(self, query, max_results=3):
        return self.hits[:max_results]


@pytest.fixture
def _fake_ddgs(monkeypatch):
    module = types.ModuleType("ddgs")
    module.DDGS = _FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", module)
    return _FakeDDGS


def test_web_search_keeps_two_sentences_per_hit(_fake_ddgs):
    _fake_ddgs.hits = [
        {"title": "Game", "body": "The Knicks beat the Celtics 110-102. Brunson scored 35. Third sentence here.",
         "href": "https://e.com/a"},
    ]
    out = vt._run_web_search("knicks")
    assert "110-102" in out and "Brunson scored 35" in out
    assert "Third sentence" not in out


def test_web_search_long_body_cut_at_a_boundary_within_300_chars(_fake_ddgs):
    body = " ".join(f"word{i}" for i in range(200))  # one run-on sentence, ~1400 chars
    _fake_ddgs.hits = [{"title": "T", "body": body, "href": "https://e.com/a"}]
    out = vt._run_web_search("q")
    snippet = out.split(": ", 1)[1].rstrip(".")
    assert 0 < len(snippet) <= 300
    assert re.fullmatch(r"(word\d+ )*word\d+", snippet), f"cut mid-word: {snippet[-20:]!r}"


def test_web_search_includes_a_date_when_the_hit_has_one(_fake_ddgs):
    _fake_ddgs.hits = [{"title": "Score", "body": "Final.", "date": "2026-10-23", "href": "https://e.com/a"}]
    assert "2026-10-23" in vt._run_web_search("q")
    _fake_ddgs.hits = [{"title": "Score", "body": "Final.", "published": "Oct 23", "href": "https://e.com/a"}]
    assert "Oct 23" in vt._run_web_search("q")


def test_web_search_result_cap_is_1200_and_others_keep_the_default():
    assert vt._RESULT_CHARS["web_search"] == 1200
    assert vt._RESULT_CHARS.get("get_weather", vt._MAX_RESULT_CHARS) == vt._MAX_RESULT_CHARS


def test_execute_does_not_cut_a_web_search_result_at_the_default_cap(monkeypatch):
    _patch_tool(monkeypatch, "web_search", lambda q: "word " * 200)  # ~1000 chars
    out = vt.execute("web_search", {"query": "x"})
    assert len(out) > vt._MAX_RESULT_CHARS


# ── ruling 5: time stamp ──────────────────────────────────────────────────

def test_web_search_success_is_stamped(monkeypatch):
    _patch_tool(monkeypatch, "web_search", lambda q: "Knicks won.")
    out = vt.execute("web_search", {"query": "x"})
    assert out.startswith("Knicks won.") and _STAMP.search(out)


def test_stamp_survives_truncation(monkeypatch):
    _patch_tool(monkeypatch, "web_search", lambda q: "word " * 1000)
    out = vt.execute("web_search", {"query": "x"})
    assert _STAMP.search(out)


def test_get_weather_is_stamped(monkeypatch):
    monkeypatch.setitem(vt._DISPATCH, "get_weather", (lambda p: "Sunny.", 2.0, "place", "a place", "weather lookup", False))
    assert _STAMP.search(vt.execute("get_weather", {"place": "Paris"}))


def test_other_tools_and_failures_are_not_stamped(monkeypatch):
    monkeypatch.setitem(vt._DISPATCH, "knowledge_lookup", (lambda q: "note", 2.0, "query", "q", "knowledge base lookup", True))
    assert "Checked at" not in vt.execute("knowledge_lookup", {"query": "x"})
    _patch_tool(monkeypatch, "web_search", lambda q: "")
    assert "Checked at" not in vt.execute("web_search", {"query": "x"})


def _write_dropin(tmp_path, extra):
    (tmp_path / "scores.py").write_text(
        'TOOL_DEF = {"name": "scores", "description": "d", "parameters": {"type": "object", '
        '"properties": {"query": {"type": "string"}}, "required": ["query"]}}\n'
        f"{extra}\n"
        'def run(query):\n    return "3-2"\n'
    )


@pytest.mark.parametrize("extra,stamped", [("TIME_SENSITIVE = True", True), ("", False), ("TIME_SENSITIVE = False", False)])
def test_dropin_time_sensitive_opt_in(monkeypatch, tmp_path, extra, stamped):
    _write_dropin(tmp_path, extra)
    monkeypatch.setattr(vt, "_probe", lambda *a, **k: False)
    monkeypatch.setattr(vt, "_probe_health", lambda *a, **k: False)
    monkeypatch.setattr(vt, "_TOOLS_DIR", str(tmp_path))
    vt.get_tool_defs()
    monkeypatch.setattr(vt, "_ARMED_NAMES", set(vt._ARMED_NAMES) | {"scores"})
    out = vt.execute("scores", {"query": "x"})
    assert out.startswith("3-2")
    assert bool(_STAMP.search(out)) is stamped


# ── ruling 6: set_mood only when asked ────────────────────────────────────

def test_set_mood_description_is_only_when_asked():
    desc = next(t for t in vt.TOOL_DEFS if t["name"] == "set_mood")["description"]
    assert "whenever your emotional tone shifts" not in desc
    assert "only when the user asks" in desc
    assert "Do not call it to accompany ordinary answers" in desc


# ── ruling 7: one INFO line per executed tool ─────────────────────────────

def test_execute_logs_name_elapsed_and_flattened_result(monkeypatch, caplog):
    _patch_tool(monkeypatch, "web_search", lambda q: "line one\nline two " + "x" * 400)
    with caplog.at_level(logging.INFO, logger=vt.logger.name):
        vt.execute("web_search", {"query": "x"})
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 1
    line = lines[0]
    assert "web_search" in line and re.search(r"\d+ms", line)
    assert "line one line two" in line and "\n" not in line
    assert len(line.split("-> ", 1)[1]) <= 200


# ── review fixes ──────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _fake_weather_client(geo, forecast):
    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return _FakeResp(geo if "geocoding" in url else forecast)

    return _C


def test_weather_failure_strings_are_not_stamped(monkeypatch):
    monkeypatch.setattr(vt.httpx, "Client", _fake_weather_client({"results": []}, {}))
    out = vt.execute("get_weather", {"place": "Atlantis"})
    assert out.startswith("I couldn't find a place") and "Checked at" not in out

    geo = {"results": [{"latitude": 1, "longitude": 2, "name": "Paris"}]}
    monkeypatch.setattr(vt.httpx, "Client", _fake_weather_client(geo, {"current_weather": {}}))
    out = vt.execute("get_weather", {"place": "Paris"})
    assert out.startswith("I couldn't get current conditions") and "Checked at" not in out


def test_weather_success_is_stamped_through_the_real_runner(monkeypatch):
    geo = {"results": [{"latitude": 1, "longitude": 2, "name": "Paris"}]}
    fc = {"current_weather": {"temperature": 20, "windspeed": 5, "weathercode": 0}}
    monkeypatch.setattr(vt.httpx, "Client", _fake_weather_client(geo, fc))
    assert _STAMP.search(vt.execute("get_weather", {"place": "Paris"}))


def test_web_search_no_results_is_not_stamped(monkeypatch, _fake_ddgs):
    _fake_ddgs.hits = []
    out = vt.execute("web_search", {"query": "zzz"})
    assert out.startswith("I didn't find any web results") and "Checked at" not in out


def test_real_web_search_runner_is_not_cut_at_the_default_cap(monkeypatch, _fake_ddgs):
    body = ("alpha " * 22).strip() + ". " + ("beta " * 22).strip() + ". tail."
    _fake_ddgs.hits = [{"title": f"Hit {i}", "body": body, "href": f"https://e.com/{i}"} for i in range(3)]
    out = vt._run_web_search("q")
    assert len(out) > vt._MAX_RESULT_CHARS


@pytest.mark.parametrize("body", ["a" * 1000, "字" * 1000], ids=["ascii", "cjk"])
def test_snippet_without_any_boundary_is_dropped_not_cut_mid_word(body):
    assert vt._snippet(body) == ""


def test_snippet_without_space_cuts_at_last_punctuation():
    body = "字" * 100 + "。" + "字" * 500
    out = vt._snippet(body)
    assert out.endswith("。") and len(out) <= vt._WEB_SNIPPET_CHARS


def test_timeout_and_exception_also_log_one_info_line(monkeypatch, caplog):
    def boom(q):
        raise RuntimeError("nope")

    for fn, t in ((boom, 2.0), (lambda q: time.sleep(1.0), 0.05)):
        _patch_tool(monkeypatch, "web_search", fn, timeout_s=t)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=vt.logger.name):
            vt.execute("web_search", {"query": "x"})
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1 and "web_search" in infos[0] and "Couldn't complete" in infos[0]
        assert any(r.levelno == logging.WARNING for r in caplog.records)


# ── tool worker starvation: one hung tool must never block another ────────

def _hang_tools(monkeypatch, names, release, started=None):
    for n in names:
        def fn(q, n=n):
            if started is not None:
                started.append(n)
            release.wait(10)
            return "late"
        monkeypatch.setitem(vt._DISPATCH, n, (fn, 0.05, "query", "a query", n, True))


def test_hung_tools_do_not_starve_a_later_tool(monkeypatch):
    import threading
    release = threading.Event()
    names = [f"hang{i}" for i in range(4)]
    _hang_tools(monkeypatch, names, release)
    try:
        for n in names:
            assert "timed out" in vt.execute(n, {"query": "x"})
        _patch_tool(monkeypatch, "fast_tool", lambda q: "fast answer", timeout_s=2.0)
        assert vt.execute("fast_tool", {"query": "x"}) == "fast answer"
    finally:
        release.set()


def test_tool_with_two_stragglers_is_refused_without_a_new_thread(monkeypatch):
    import threading
    release = threading.Event()
    started = []
    _hang_tools(monkeypatch, ["hang"], release, started)
    try:
        assert "timed out" in vt.execute("hang", {"query": "x"})
        assert "timed out" in vt.execute("hang", {"query": "x"})
        out = vt.execute("hang", {"query": "x"})
        assert "still stuck on an earlier request" in out and "Couldn't" in out
        time.sleep(0.1)
        assert len(started) == 2, "the refused call must not start a thread"
    finally:
        release.set()


def test_total_straggler_cap_refuses_every_tool(monkeypatch):
    import threading
    release = threading.Event()
    monkeypatch.setattr(vt, "_MAX_STRAGGLERS_TOTAL", 3)
    names = [f"hang{i}" for i in range(3)]
    _hang_tools(monkeypatch, names, release)
    _patch_tool(monkeypatch, "fast_tool", lambda q: "fast answer", timeout_s=2.0)
    try:
        for n in names:
            vt.execute(n, {"query": "x"})
        assert "still stuck" in vt.execute("fast_tool", {"query": "x"})
    finally:
        release.set()


def test_stragglers_clear_when_blocked_threads_finish(monkeypatch, caplog):
    import threading
    release = threading.Event()
    _hang_tools(monkeypatch, ["hang"], release)
    vt.execute("hang", {"query": "x"})
    vt.execute("hang", {"query": "x"})
    assert len(vt._STRAGGLERS) == 2
    with caplog.at_level(logging.WARNING, logger=vt.logger.name):
        release.set()
        deadline = time.monotonic() + 3
        while vt._STRAGGLERS and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
    assert not vt._STRAGGLERS
    assert sum("finally finished" in r.getMessage() and "hang" in r.getMessage()
               for r in caplog.records) == 2
    _patch_tool(monkeypatch, "hang", lambda q: "ok", timeout_s=2.0)
    assert vt.execute("hang", {"query": "x"}) == "ok"
