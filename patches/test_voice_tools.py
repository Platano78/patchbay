"""Unit tests for voice_tools.py's knowledge lane -- QMD content retrieval,
collection filtering, the QMD+Genesis fan-out, and the Faulkner decision tool.

Run from repo root: python3 -m pytest patches/test_voice_tools.py -v

voice_tools.py's only speech_to_speech import is `from speech_to_speech import
phone_context`, so the REAL patches/phone_context module is aliased in as that
name before importing it -- same pattern as test_phone_context.py's
_install_voice_tools_stub(). No live service is contacted: every test replaces
voice_tools._mcp_call, the single choke point through which all three backends
(QMD, Agent Genesis, Faulkner-DB) are reached.

The regression these tests exist for: _run_knowledge_lookup used to return
"<title> in <file>" for the top three hits and NO document content whatsoever,
so the LLM answered the user's question from three filenames and confabulated
the rest.
"""

from __future__ import annotations

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


# ── helpers ──────────────────────────────────────────────────────────────

def _qmd_query_result(hits):
    """Shape returned by QMD's `query` tool (structuredContent.results)."""
    return {"structuredContent": {"results": hits}}


def _qmd_get_result(text):
    """Shape returned by QMD's `get` tool -- the text lives under
    content[0].resource.text, NOT content[0].text. Verified against the live
    server; reading content[0].text yields an empty string."""
    return {"content": [{"type": "resource", "resource": {"text": text}}]}


def _hit(file, title, line=10):
    return {"file": file, "title": title, "line": line, "score": 1.0}


class _Router:
    """Stands in for _mcp_call, recording every call and replying per-tool."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def __call__(self, url, tool, arguments, timeout_s):
        self.calls.append({"url": url, "tool": tool, "args": arguments})
        handler = self.handlers.get(tool)
        if handler is None:
            return {}
        return handler(arguments) if callable(handler) else handler


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeHTTP:
    """Minimal stand-in for httpx.Client covering the plain-REST backends.
    Genesis is reached with real httpx rather than _mcp_call, so it needs its
    own seam."""

    def __init__(self, post=None, get=None):
        self._post = post
        self._get = get
        self.posts = []
        self.gets = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, **kwargs):
        self.posts.append({"url": url, "json": json})
        if callable(self._post):
            return self._post(url, json)
        if isinstance(self._post, Exception):
            raise self._post
        return _FakeResponse(self._post)

    def get(self, url, **kwargs):
        self.gets.append({"url": url})
        if isinstance(self._get, Exception):
            raise self._get
        return _FakeResponse(None, status=self._get if self._get else 200)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Default every test to QMD-only with no gateway configured."""
    monkeypatch.setattr(vt, "_GENESIS_ARMED", False)
    monkeypatch.setattr(vt, "_GENESIS_URL", "")
    monkeypatch.setattr(vt, "_ARMED_NAMES", set())
    yield


def _arm_genesis(monkeypatch, post):
    """Arm the Genesis lane and route its HTTP through a fake client. `post`
    is the JSON body it should answer with, a callable, or an Exception."""
    monkeypatch.setattr(vt, "_GENESIS_ARMED", True)
    monkeypatch.setattr(vt, "_GENESIS_URL", "http://localhost:8080")
    fake = _FakeHTTP(post=post)
    monkeypatch.setattr(vt.httpx, "Client", fake)
    return fake


def _genesis_payload(*docs):
    """Shape returned by Agent Genesis POST /search -- `document` is the raw
    conversation text, already readable, so there is no second content fetch."""
    return {"results_count": len(docs), "results": [{"id": "x", "document": d} for d in docs]}


# ── the regression: real content, not titles ─────────────────────────────

def test_knowledge_lookup_returns_document_content_not_titles():
    body = (
        "The echo gate scores incoming audio against the last TTS frame and "
        "drops it when the correlation clears the threshold."
    )
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("knowledge-base/notes.md", "Echo gate")]),
        "get": _qmd_get_result(body),
    })

    out = vt._run_knowledge_lookup("echo gate")

    assert "scores incoming audio" in out, "document content must be returned"
    assert "correlation clears the threshold" in out
    # The old implementation returned exactly this and nothing else.
    assert out != "Echo gate in knowledge-base/notes.md"


def test_knowledge_lookup_fetches_content_windowed_around_the_hit_line():
    router = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A", line=100)]),
        "get": _qmd_get_result("windowed body text"),
    })
    vt._mcp_call = router

    vt._run_knowledge_lookup("anything")

    get_call = next(c for c in router.calls if c["tool"] == "get")
    assert get_call["args"]["file"] == "kb/a.md"
    assert get_call["args"]["fromLine"] == 100 - vt._GET_LINES_BEFORE
    assert get_call["args"]["maxLines"] == vt._GET_MAX_LINES
    # lineNumbers must be off or every line arrives prefixed "100: ".
    assert get_call["args"]["lineNumbers"] is False


def test_hit_line_near_start_of_file_clamps_to_line_one():
    router = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A", line=2)]),
        "get": _qmd_get_result("body"),
    })
    vt._mcp_call = router

    vt._run_knowledge_lookup("q")

    assert next(c for c in router.calls if c["tool"] == "get")["args"]["fromLine"] == 1


def test_mcp_text_reads_resource_block_and_text_block():
    """Guards the exact shape bug that made the first implementation return
    empty excerpts: QMD's get() wraps content in a `resource` block."""
    assert vt._mcp_text(_qmd_get_result("hello")) == "hello"
    assert vt._mcp_text({"content": [{"type": "text", "text": "plain"}]}) == "plain"
    assert vt._mcp_text({}) == ""


# ── collection filtering ─────────────────────────────────────────────────

def test_query_is_filtered_to_the_users_own_collections():
    router = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("body"),
    })
    vt._mcp_call = router

    vt._run_knowledge_lookup("q")

    first_query = next(c for c in router.calls if c["tool"] == "query")
    assert first_query["args"]["collections"] == vt._KNOWLEDGE_COLLECTIONS
    # skills/agents/security are tooling docs, not the user's notes -- a live
    # query for "echo gate" ranked a pre-commit SKILL.md first at score 1.0.
    for noisy in ("skills", "agents", "security"):
        assert noisy not in vt._KNOWLEDGE_COLLECTIONS


def test_rerank_stays_off_and_intent_is_always_sent():
    """Reranking costs ~85s -- categorically unusable in a voice loop."""
    router = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("body"),
    })
    vt._mcp_call = router

    vt._run_knowledge_lookup("q")

    args = next(c for c in router.calls if c["tool"] == "query")["args"]
    assert args["rerank"] is False
    assert args["intent"]
    # lex catches exact terms, vec catches misheard/approximate phrasing.
    assert [s["type"] for s in args["searches"]] == ["lex", "vec"]


def test_empty_filtered_result_retries_across_all_collections():
    calls = {"n": 0}

    def query(args):
        calls["n"] += 1
        # First (filtered) call finds nothing; the unfiltered retry finds one.
        if "collections" in args:
            return _qmd_query_result([])
        return _qmd_query_result([_hit("skills/x/SKILL.md", "X")])

    vt._mcp_call = _Router({"query": query, "get": _qmd_get_result("wide body")})

    out = vt._run_knowledge_lookup("q")

    assert calls["n"] == 2, "must retry unfiltered rather than giving up"
    assert "wide body" in out


def test_no_results_anywhere_reports_nothing_found():
    vt._mcp_call = _Router({"query": _qmd_query_result([])})
    assert "didn't find anything" in vt._run_knowledge_lookup("obscure thing")


# ── merge + source labelling ─────────────────────────────────────────────

def test_merged_output_labels_each_source_distinctly(monkeypatch):
    _arm_genesis(monkeypatch, _genesis_payload("we talked it through"))
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "Echo gate")]),
        "get": _qmd_get_result("note body here"),
    })

    out = vt._run_knowledge_lookup("echo gate")

    assert "From your notes (Echo gate): note body here" in out
    assert "From a past conversation: we talked it through" in out


def test_genesis_posts_query_and_limit_to_the_search_endpoint(monkeypatch):
    fake = _arm_genesis(monkeypatch, _genesis_payload("body"))
    vt._mcp_call = _Router({"query": _qmd_query_result([])})

    vt._run_knowledge_lookup("wakeword gate")

    assert fake.posts[0]["url"] == "http://localhost:8080/search"
    assert fake.posts[0]["json"] == {"query": "wakeword gate", "limit": vt._KNOWLEDGE_CONVOS}


# ── degradation ──────────────────────────────────────────────────────────

def test_genesis_failure_degrades_to_qmd_only(monkeypatch):
    _arm_genesis(monkeypatch, RuntimeError("genesis container is down"))
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("qmd body survives"),
    })

    out = vt._run_knowledge_lookup("q")

    assert "qmd body survives" in out, "a dead Genesis must not take the tool down"
    assert "From a past conversation" not in out


def test_genesis_malformed_payload_degrades_quietly(monkeypatch):
    _arm_genesis(monkeypatch, lambda url, body: _FakeResponse(ValueError("not json")))
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("qmd body"),
    })

    assert "qmd body" in vt._run_knowledge_lookup("q")


def test_genesis_http_error_degrades_quietly(monkeypatch):
    _arm_genesis(monkeypatch, lambda url, body: _FakeResponse({}, status=500))
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("qmd body"),
    })

    assert "qmd body" in vt._run_knowledge_lookup("q")


def test_genesis_not_consulted_when_unarmed(monkeypatch):
    fake = _FakeHTTP(post=_genesis_payload("leak"))
    monkeypatch.setattr(vt.httpx, "Client", fake)
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("body"),
    })

    out = vt._run_knowledge_lookup("q")

    assert fake.posts == [], "unarmed Genesis must not be contacted at all"
    assert "leak" not in out


def test_qmd_failure_still_returns_genesis_results(monkeypatch):
    _arm_genesis(monkeypatch, _genesis_payload("conversation body"))

    def explode(args):
        raise RuntimeError("qmd down")

    vt._mcp_call = _Router({"query": explode})

    assert "conversation body" in vt._run_knowledge_lookup("q")


def test_a_hung_lane_does_not_cost_the_other_lane_its_results(monkeypatch):
    """The fan-out shares one wall clock, so a wedged Genesis must not eat the
    whole tool deadline -- QMD's results still come back."""
    def hang(url, body):
        time.sleep(5)
        return _FakeResponse(_genesis_payload("too late"))

    _arm_genesis(monkeypatch, hang)
    monkeypatch.setattr(vt, "_FANOUT_BUDGET_S", 0.3)

    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("fast qmd body"),
    })

    started = time.monotonic()
    out = vt._run_knowledge_lookup("q")
    elapsed = time.monotonic() - started

    assert "fast qmd body" in out
    assert elapsed < 2.0, f"fan-out did not honour its budget ({elapsed:.1f}s)"


# ── arming ───────────────────────────────────────────────────────────────

def _stub_arming(monkeypatch, genesis_up):
    monkeypatch.delenv("VOICE_TOOLS", raising=False)
    monkeypatch.setattr(vt, "_GENESIS_URL", "http://localhost:8080")
    # QMD/Hermes probes are irrelevant here; fail them so only CORE arms.
    monkeypatch.setattr(vt, "_probe", lambda url, payload: False)
    monkeypatch.setattr(vt, "_probe_health", lambda url: genesis_up)


def test_genesis_lane_arms_when_health_probe_succeeds(monkeypatch):
    _stub_arming(monkeypatch, True)
    vt.get_tool_defs()
    assert vt._GENESIS_ARMED is True


def test_genesis_lane_disarms_when_container_is_down(monkeypatch):
    """Dropped from the fan-out entirely, rather than paying its timeout on
    every single call."""
    _stub_arming(monkeypatch, False)
    vt.get_tool_defs()
    assert vt._GENESIS_ARMED is False


def test_probe_health_hits_the_health_endpoint(monkeypatch):
    fake = _FakeHTTP(get=200)
    monkeypatch.setattr(vt.httpx, "Client", fake)

    assert vt._probe_health("http://localhost:8080") is True
    assert fake.gets[0]["url"] == "http://localhost:8080/health"


def test_probe_health_is_false_on_error(monkeypatch):
    monkeypatch.setattr(vt.httpx, "Client", _FakeHTTP(get=ConnectionError("down")))
    assert vt._probe_health("http://localhost:8080") is False
    assert vt._probe_health("") is False


def test_faulkner_arms_on_health_and_disarms_when_down(monkeypatch):
    monkeypatch.delenv("VOICE_TOOLS", raising=False)
    monkeypatch.setattr(vt, "_probe", lambda url, payload: False)

    monkeypatch.setattr(vt, "_probe_health", lambda url: True)
    assert "decision_lookup" in [t["name"] for t in vt.get_tool_defs()]

    monkeypatch.setattr(vt, "_probe_health", lambda url: False)
    assert "decision_lookup" not in [t["name"] for t in vt.get_tool_defs()]


# ── decision_lookup (Faulkner) ───────────────────────────────────────────

class _FakeFaulkner:
    """Records each /api/search query and replies per-query."""

    def __init__(self, by_query):
        self.by_query = by_query
        self.queries = []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, **kwargs):
        self.queries.append(params["q"])
        return _FakeResponse({"nodes": self.by_query.get(params["q"], [])})


def _decision(description, rationale=None, type="Decision"):
    return {"id": "D-1", "type": type, "description": description, "rationale": rationale}


def test_decision_lookup_speaks_description_and_rationale(monkeypatch):
    monkeypatch.setattr(vt.httpx, "Client", _FakeFaulkner({
        "two processes": [_decision("Split the APK into two processes.", "lmkd kills processes.")]
    }))

    out = vt._run_decision_lookup("two processes")

    assert "Decision: Split the APK into two processes. lmkd kills processes." in out


def test_decision_lookup_ignores_non_decision_nodes(monkeypatch):
    monkeypatch.setattr(vt.httpx, "Client", _FakeFaulkner({
        "q": [_decision("a pattern", type="Pattern"), _decision("the real decision")]
    }))

    out = vt._run_decision_lookup("q")

    assert "the real decision" in out
    assert "a pattern" not in out


def test_sentence_query_retries_with_content_words_only(monkeypatch):
    """/api/search ANDs every term, so the full question matches nothing while
    its content words match -- verified live: 0 nodes vs 36."""
    fake = _FakeFaulkner({"two processes": [_decision("Split the APK.")]})
    monkeypatch.setattr(vt.httpx, "Client", fake)

    out = vt._run_decision_lookup("why is it two processes")

    assert fake.queries == ["why is it two processes", "two processes"]
    assert "Split the APK." in out


def test_no_retry_when_the_first_search_already_matched(monkeypatch):
    """The retry may add recall but must never reorder a working result set."""
    fake = _FakeFaulkner({"why is it two processes": [_decision("Found first try.")]})
    monkeypatch.setattr(vt.httpx, "Client", fake)

    vt._run_decision_lookup("why is it two processes")

    assert fake.queries == ["why is it two processes"]


def test_decision_lookup_failure_is_non_blocking(monkeypatch):
    monkeypatch.setattr(vt.httpx, "Client", _FakeHTTP(post=RuntimeError("down")))
    # A transport error must surface as plain speech, never raise into the
    # audio thread.
    assert "didn't find a recorded decision" in vt._run_decision_lookup("anything")


def test_the_two_knowledge_tools_point_at_each_other():
    assert "decision_lookup" in vt._DISPATCH
    descriptions = {t["name"]: t["description"] for t in vt.TOOL_DEFS}
    # The two tools overlap in subject matter, so each description must name
    # the other explicitly or the LLM will mis-route between them.
    assert "decision_lookup" in descriptions["knowledge_lookup"]
    assert "knowledge_lookup" in descriptions["decision_lookup"]


def test_decision_query_param_documents_the_keyword_requirement():
    """The AND-substring backend is only usable if the model sends keywords,
    so the parameter description is load-bearing, not decoration."""
    tool = next(t for t in vt.TOOL_DEFS if t["name"] == "decision_lookup")
    param = tool["parameters"]["properties"]["query"]["description"].lower()
    assert "keyword" in param
    assert "not a question" in param or "not a sentence" in param


# ── result budget + speakability ─────────────────────────────────────────

def test_knowledge_results_get_a_larger_cap_than_one_line_tools():
    assert vt._RESULT_CHARS["knowledge_lookup"] > vt._MAX_RESULT_CHARS
    # web_search and friends keep the tight default.
    assert "web_search" not in vt._RESULT_CHARS


def test_execute_does_not_truncate_knowledge_results_to_the_short_cap():
    # Each excerpt is capped at _DOC_EXCERPT_CHARS, so it takes both fetched
    # documents to exceed the 600-char default that every other tool uses.
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A"), _hit("kb/b.md", "B")]),
        "get": _qmd_get_result("sentence. " * 200),
    })

    out = vt.execute("knowledge_lookup", {"query": "q"})

    assert len(out) > vt._MAX_RESULT_CHARS
    assert len(out) <= vt._RESULT_CHARS["knowledge_lookup"]


def test_speakable_strips_frontmatter_context_comment_and_markdown():
    raw = (
        "<!-- Context: Curated research notes -->\n"
        "\n"
        "---\n"
        "name: some-doc\n"
        "type: reference\n"
        "---\n"
        "\n"
        "# The Heading\n"
        "\n"
        "The **real** body with `code` in it.\n"
    )

    out = vt._speakable(raw, 500)

    assert out == "The Heading The real body with code in it."
    for artifact in ("<!--", "---", "name:", "**", "`", "#"):
        assert artifact not in out


def test_speakable_caps_length_with_ellipsis():
    out = vt._speakable("word " * 100, 20)
    assert out.endswith("…")
    assert len(out) == 20


def test_lane_never_speaks_qmds_diff_formatted_snippet():
    """QMD's snippet field carries line numbers and @@ hunk markers -- the
    reason the original implementation avoided content at all. get() is clean."""
    router = _Router({
        "query": _qmd_query_result([
            dict(_hit("kb/a.md", "A"), snippet="3: @@ -2,4 @@ (1 before)\n4: junk")
        ]),
        "get": _qmd_get_result("clean body"),
    })
    vt._mcp_call = router

    out = vt._run_knowledge_lookup("q")

    assert "@@" not in out
    assert "clean body" in out


# ── MCP transport ────────────────────────────────────────────────────────

def test_mcp_json_parses_sse_frames():
    """The gateway answers text/event-stream; QMD answers plain JSON."""

    class _Resp:
        headers = {"content-type": "text/event-stream"}
        text = 'event: message\ndata: {"result": {"ok": true}}\n\n'

    assert vt._mcp_json(_Resp()) == {"result": {"ok": True}}


def test_mcp_json_returns_empty_on_garbage():
    class _Resp:
        headers = {"content-type": "application/json"}
        text = "<html>502</html>"

        def json(self):
            raise ValueError("not json")

    assert vt._mcp_json(_Resp()) == {}


# ── relevance floor: the honest miss ─────────────────────────────────────
#
# QMD's `vec` sub-query always returns its nearest neighbours, so the "found
# nothing" branch above was nearly unreachable and knowledge_lookup answered
# nonsense questions with real content from unrelated documents. Probed live
# against the box: every result set -- sensible or nonsense -- comes back scored
# exactly 1.0 / 0.5 / 0.33 / 0.25 / 0.2 (reciprocal-rank fusion), so `minScore`
# ranks by position and cannot gate on relevance. The floor is instead a check
# that at least one excerpt mentions a topic word from the question.

def test_nonsense_query_reports_an_honest_miss_despite_neighbour_hits():
    """The live failure: 'zzzqqq nonexistent topic' returned a confident 1631-
    character answer about an unrelated CUDA pull request."""
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/cuda.md", "Q1_0 CUDA byte_perm PR")]),
        "get": _qmd_get_result(
            "Optimizes the Q1_0 CUDA dequant kernel to unpack ternary elements "
            "via the byte_perm PTX instruction."
        ),
    })

    out = vt._run_knowledge_lookup("zzzqqq nonexistent topic")

    assert "didn't find anything" in out
    assert "CUDA" not in out, "an unrelated neighbour must not be spoken as an answer"


def test_relevant_hit_still_answers_in_full():
    """The floor must not undo the content fix -- a real hit answers as before."""
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/notes.md", "Echo gate")]),
        "get": _qmd_get_result("The echo gate drops audio correlated with the last frame."),
    })

    out = vt._run_knowledge_lookup("what do my notes say about the echo gate")

    assert "drops audio correlated" in out


def test_one_relevant_hit_carries_its_semantically_matched_neighbours():
    """Gated on the whole result set, not per document: vec earns its keep by
    finding documents that match by meaning without sharing vocabulary, so those
    ride along as long as something in the set is literally on topic."""
    vt._mcp_call = _Router({
        "query": _qmd_query_result([
            _hit("kb/a.md", "A"),
            _hit("kb/b.md", "B"),
        ]),
        "get": _qmd_get_result("wakeword arming is handled by the gate"),
    })

    out = vt._run_knowledge_lookup("wakeword")

    assert "wakeword arming" in out


def test_query_with_no_topic_words_is_never_gated():
    """Fails open: if stripping leaves nothing to match on, answer anyway."""
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/a.md", "A")]),
        "get": _qmd_get_result("some unrelated body text"),
    })

    out = vt._run_knowledge_lookup("what do you know about that")

    assert "some unrelated body text" in out


def test_honest_miss_does_not_speak_repr_quotes():
    vt._mcp_call = _Router({"query": _qmd_query_result([])})
    out = vt._run_knowledge_lookup("banana helicopter zebra")
    # {!r} used to wrap the topic in quote characters some engines pronounce.
    assert "'banana helicopter zebra'" not in out
    assert "banana helicopter zebra" in out


# ── speakability ─────────────────────────────────────────────────────────
#
# Everything these tools return is spoken by a TTS engine, never read. A bare
# URL is the worst case -- engines spell them out character by character.

def test_urls_never_survive_into_spoken_output():
    vt._mcp_call = _Router({
        "query": _qmd_query_result([_hit("kb/esp.md", "ESP VoCat")]),
        "get": _qmd_get_result(
            "The esp vocat board is open hardware.\n"
            "URL: https://gitee.com/esp-friends/esp-vocat-base\n"
            "See www.example.com for more."
        ),
    })

    out = vt._run_knowledge_lookup("esp vocat")

    assert "https://" not in out and "gitee" not in out
    assert "www." not in out
    assert "open hardware" in out, "dropping the link must not drop the substance"


def test_markdown_link_keeps_its_anchor_text_and_loses_the_target():
    assert vt._speakable("See [the wakeword notes](https://example.com/x) today.", 200) == (
        "See the wakeword notes today."
    )


def test_bullets_and_frontmatter_remnants_are_not_spoken():
    text = (
        "date: 20260511\n"
        "source: nightly-synthesis\n"
        "degraded: false\n"
        "Decisions\n"
        "- The spine was updated for NPC-M.\n"
        "* Saves are multi-slot from the start.\n"
        "1. Manual load is in for 1.0.\n"
    )
    out = vt._speakable(text, 400)

    assert "date:" not in out and "degraded:" not in out and "nightly-synthesis" not in out
    assert not out.startswith("-") and "- The spine" not in out
    # The substance of every bullet survives; only the marker goes.
    assert "The spine was updated for NPC-M." in out
    assert "Saves are multi-slot from the start." in out
    assert "Manual load is in for 1.0." in out


def test_prose_with_a_colon_is_not_mistaken_for_frontmatter():
    """The frontmatter rule must not eat real sentences."""
    out = vt._speakable("Result: it worked on the second try.", 200)
    assert "it worked on the second try" in out


def test_backticked_identifiers_keep_their_text():
    """A listener asking about their own notes wants to hear the real filename;
    softening it to 'a file' throws away the one concrete thing in the sentence."""
    out = vt._speakable("The rules live in `voice_rules.py` now.", 200)
    assert "voice_rules.py" in out
    assert "`" not in out


def test_ellipsis_inside_note_prose_is_not_spoken():
    out = vt._speakable("It works... mostly, and the rest is fine.", 200)
    assert "..." not in out
    assert "mostly" in out and "the rest is fine" in out


# ── web_search reads as prose, not as a SERP ─────────────────────────────

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


def test_web_search_returns_prose_not_pipe_delimited_rows(_fake_ddgs):
    """TTS reads '|' aloud as a word and '- Wikipedia' as part of the fact."""
    _fake_ddgs.hits = [
        {"title": "Paris - Wikipedia",
         "body": "As the capital of France, Paris is a major European city. It has museums.",
         "href": "https://en.wikipedia.org/wiki/Paris"},
        {"title": "List of capitals of France - Wikipedia",
         "body": "This is a chronological list.",
         "href": "https://en.wikipedia.org/wiki/List"},
    ]

    out = vt._run_web_search("capital of France")

    assert "|" not in out
    assert "Wikipedia" not in out
    assert "As the capital of France" in out, "the substance must survive"


def test_web_search_keeps_a_real_title_that_contains_a_dash(_fake_ddgs):
    """Only a short trailing site-name segment is dropped."""
    _fake_ddgs.hits = [
        {"title": "Paris - a history of the left bank",
         "body": "A cultural history.", "href": "https://example.com/x"},
    ]

    out = vt._run_web_search("paris history")

    assert "a history of the left bank" in out


# ── decision_lookup on a spoken sentence ─────────────────────────────────

def test_decision_lookup_succeeds_on_a_natural_spoken_sentence(monkeypatch):
    """Measured live before the fix: 'echo gate' -> 3 hits, but 'what was the
    decision about the echo gate' -> 0, and the retry kept the word 'decision'
    and ANDed it, so that was 0 too. People speak sentences at this tool."""
    fake = _FakeFaulkner({"echo gate": [_decision("Gate echo server-side.")]})
    monkeypatch.setattr(vt.httpx, "Client", fake)

    out = vt._run_decision_lookup("what was the decision about the echo gate")

    assert fake.queries == ["what was the decision about the echo gate", "echo gate"]
    assert "Gate echo server-side." in out


def test_scaffolding_nouns_are_stripped_from_the_retry():
    assert vt._content_words("what was the decision about the echo gate") == "echo gate"
    assert vt._content_words("tell me the reason we chose two processes") == "two processes"
    assert vt._content_words("what do my notes say about the wakeword") == "wakeword"


def test_decision_miss_does_not_speak_repr_quotes(monkeypatch):
    fake = _FakeFaulkner({})
    monkeypatch.setattr(vt.httpx, "Client", fake)
    out = vt._run_decision_lookup("why did we do that")
    assert "'why did we do that'" not in out
    assert "why did we do that" in out


# ── arming must rebind, never mutate in place ────────────────────────────
#
# execute() gates on `if _ARMED_NAMES and name not in _ARMED_NAMES`. An empty
# set is falsy, so that gate short-circuits to "allow everything" whenever the
# set is momentarily empty. The old implementation did clear() then update() --
# two operations, with exactly that empty window in between, readable by any of
# the four _EXECUTOR worker threads mid-reload. It failed OPEN.
#
# Asserting on object identity is the deterministic way to pin this: a rebind
# swaps the object, an in-place clear()/update() keeps it. Racing real threads
# would be flaky; this fails reliably against the old shape.

def _armed_identity_is_replaced(monkeypatch, **env):
    """Call get_tool_defs() and report whether _ARMED_NAMES was rebound."""
    monkeypatch.delenv("VOICE_TOOLS", raising=False)
    monkeypatch.delenv("VOICE_TOOLS_DIR", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sentinel = set()
    monkeypatch.setattr(vt, "_ARMED_NAMES", sentinel)
    armed = vt.get_tool_defs()
    return sentinel is not vt._ARMED_NAMES, armed


def test_env_pinned_arming_rebinds_instead_of_mutating(monkeypatch):
    rebound, armed = _armed_identity_is_replaced(monkeypatch, VOICE_TOOLS="get_weather")
    assert rebound, "get_tool_defs() mutated _ARMED_NAMES in place; it must rebind"
    assert vt._ARMED_NAMES == {"get_weather"}
    assert [t["name"] for t in armed] == ["get_weather"]


def test_probed_arming_rebinds_instead_of_mutating(monkeypatch):
    # Every endpoint down -> only CORE_TOOLS arm, and nothing touches the network.
    monkeypatch.setattr(vt, "_probe", lambda *a, **k: False)
    monkeypatch.setattr(vt, "_probe_health", lambda *a, **k: False)
    rebound, armed = _armed_identity_is_replaced(monkeypatch)
    assert rebound, "get_tool_defs() mutated _ARMED_NAMES in place; it must rebind"
    assert vt._ARMED_NAMES == set(vt.CORE_TOOLS)
    assert vt._ARMED_NAMES, "the armed set must never be left empty -- execute() fails open on falsy"
    assert {t["name"] for t in armed} == set(vt.CORE_TOOLS)
