"""Unit tests for hermes_cockpit.py's spoken completion announcement and
quest-card auto-clear (delegation finishes silently -> speak it; finished
task stays on the card forever -> auto-clear after 90s unless superseded).

Run from repo root: python3 -m pytest patches/test_hermes_cockpit.py -v

The real ``speech_to_speech`` package is not installed in this repo; the two
symbols hermes_cockpit.py imports from it are stubbed into ``sys.modules``
before the import below, same hermetic pattern as
``test_parrot_lane.py``/``test_brain_control.py``. ``PipelineEvent`` is
aliased to the REAL ``pydantic.BaseModel`` (not a fake) since
``CockpitStateEvent``/``SearchLinksEvent`` rely on real pydantic field
semantics.

No real 90s waits: the generation-counter reset logic is tested by calling
``_maybe_reset`` directly rather than waiting on a real ``threading.Timer``.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest
import types
from queue import Queue


def _install_stubs():
    def mod(name, **attrs):
        # Merge onto an existing sys.modules entry rather than replacing it:
        # multiple test files stub the same speech_to_speech.* submodules,
        # and pytest imports every test module (running every file's
        # _install_stubs()) before any test function runs -- a later file's
        # stub would otherwise silently erase attributes an earlier file's
        # LAZY (call-time) import still needs at test-execution time.
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    import pydantic

    mod("speech_to_speech")
    mod("speech_to_speech.pipeline")
    mod("speech_to_speech.pipeline.events", PipelineEvent=pydantic.BaseModel)

    class _StubTTSInput:
        def __init__(self, text=None, turn_id=None, turn_revision=None, **kw):
            self.text = text
            self.turn_id = turn_id
            self.turn_revision = turn_revision

    mod("speech_to_speech.pipeline.messages", TTSInput=_StubTTSInput)


_install_stubs()

from patches import hermes_cockpit  # noqa: E402


def _make_cockpit(monkeypatch, **kwargs):
    """Construct a HermesCockpit with the real poll thread disabled (no
    network calls, no infinite loop) -- _poll_loop is patched to a no-op
    before the background thread it's handed to actually runs it."""
    monkeypatch.setattr(hermes_cockpit.HermesCockpit, "_poll_loop", lambda self: None)
    return hermes_cockpit.HermesCockpit(**kwargs)


# ── announcement text building (pure) ────────────────────────────────────


def test_build_announcement_done():
    assert hermes_cockpit._build_announcement("done", "All set.") == "Hermes is done. All set."


def test_build_announcement_error():
    assert hermes_cockpit._build_announcement("error", "timed out") == "Hermes hit a problem: timed out"


def test_spoken_result_collapses_whitespace():
    assert hermes_cockpit._spoken_result("line one\n\n  line   two\t\ttabbed") == "line one line two tabbed"


def test_spoken_result_under_cap_unchanged():
    text = "short result"
    assert hermes_cockpit._spoken_result(text) == text


def test_spoken_result_at_cap_boundary_unchanged():
    text = "x" * hermes_cockpit._ANNOUNCE_RESULT_CHARS
    assert hermes_cockpit._spoken_result(text) == text


def test_spoken_result_over_cap_truncated_with_tail():
    text = "x" * (hermes_cockpit._ANNOUNCE_RESULT_CHARS + 50)
    result = hermes_cockpit._spoken_result(text)
    assert result.startswith("x" * hermes_cockpit._ANNOUNCE_RESULT_CHARS)
    assert result.endswith("Ask me for the details if you want more.")
    assert len(result) > hermes_cockpit._ANNOUNCE_RESULT_CHARS


def test_spoken_result_empty_is_empty():
    assert hermes_cockpit._spoken_result("") == ""
    assert hermes_cockpit._spoken_result(None) == ""


# ── VOICE_HERMES_ANNOUNCE env parsing ────────────────────────────────────


def test_announce_enabled_default_true_when_unset():
    assert hermes_cockpit._announce_enabled(None) is True


def test_announce_enabled_blank_is_default():
    assert hermes_cockpit._announce_enabled("   ") is True


def test_announce_enabled_off_disables_case_insensitive():
    assert hermes_cockpit._announce_enabled("off") is False
    assert hermes_cockpit._announce_enabled("  Off  ") is False
    assert hermes_cockpit._announce_enabled("OFF") is False


def test_announce_enabled_other_value_stays_enabled():
    assert hermes_cockpit._announce_enabled("on") is True
    assert hermes_cockpit._announce_enabled("1") is True


# ── _announce_completion (tts_queue wiring) ──────────────────────────────


def test_announce_completion_noop_without_tts_queue(monkeypatch):
    cockpit = _make_cockpit(monkeypatch, tts_queue=None)
    cockpit._announce_completion("done", "fine")  # must not raise


def test_announce_completion_puts_tts_input_when_enabled(monkeypatch):
    tts_queue = Queue()
    cockpit = _make_cockpit(monkeypatch, tts_queue=tts_queue)

    cockpit._announce_completion("done", "All set.")

    item = tts_queue.get_nowait()
    assert item.text == "Hermes is done. All set."
    assert item.turn_id is None
    assert item.turn_revision is None


def test_announce_completion_error_status(monkeypatch):
    tts_queue = Queue()
    cockpit = _make_cockpit(monkeypatch, tts_queue=tts_queue)

    cockpit._announce_completion("error", "connection refused")

    item = tts_queue.get_nowait()
    assert item.text == "Hermes hit a problem: connection refused"


def test_announce_completion_respects_off_env(monkeypatch):
    monkeypatch.setenv("VOICE_HERMES_ANNOUNCE", "off")
    tts_queue = Queue()
    cockpit = _make_cockpit(monkeypatch, tts_queue=tts_queue)

    cockpit._announce_completion("done", "All set.")

    assert tts_queue.empty()


# ── generation-counter reset logic (_maybe_reset) ────────────────────────


def _active_delegation(**overrides):
    d = {"active": False, "task": "do a thing", "status": "done", "started_ts": 1.0, "result": "ok", "steps": []}
    d.update(overrides)
    return d


def test_maybe_reset_fires_when_generation_matches(monkeypatch):
    text_output_queue = Queue()
    cockpit = _make_cockpit(monkeypatch, text_output_queue=text_output_queue)
    cockpit._delegation_generation = 1
    cockpit._delegation = _active_delegation()

    cockpit._maybe_reset(1)

    assert cockpit._delegation == {
        "active": False,
        "task": None,
        "status": "idle",
        "started_ts": None,
        "result": None,
        "steps": [],
    }
    event = text_output_queue.get_nowait()
    assert event.delegation.status == "idle"


def test_maybe_reset_ignored_when_new_delegation_started(monkeypatch):
    text_output_queue = Queue()
    cockpit = _make_cockpit(monkeypatch, text_output_queue=text_output_queue)
    # A new delegation started meanwhile: generation bumped past what the
    # stale timer was scheduled for.
    cockpit._delegation_generation = 2
    new_delegation = _active_delegation(active=True, status="running", task="a newer task")
    cockpit._delegation = new_delegation

    cockpit._maybe_reset(1)  # stale generation from the finished delegation

    assert cockpit._delegation == new_delegation
    assert text_output_queue.empty()


def test_maybe_reset_ignored_when_still_active(monkeypatch):
    # Same generation but somehow still active -- safety net, must not reset.
    text_output_queue = Queue()
    cockpit = _make_cockpit(monkeypatch, text_output_queue=text_output_queue)
    cockpit._delegation_generation = 1
    still_active = _active_delegation(active=True, status="running")
    cockpit._delegation = still_active

    cockpit._maybe_reset(1)

    assert cockpit._delegation == still_active
    assert text_output_queue.empty()


def test_schedule_reset_cancels_previous_timer(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    cockpit._schedule_reset(1)
    first_timer = cockpit._reset_timer

    cockpit._schedule_reset(2)

    assert first_timer.finished.is_set()  # cancelled, will never fire
    cockpit._reset_timer.cancel()  # don't leave a live 90s timer thread past this test


def test_finish_delegation_announces_and_schedules_with_current_generation(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    calls = {}
    monkeypatch.setattr(cockpit, "_announce_completion", lambda status, result: calls.setdefault("announce", (status, result)))
    monkeypatch.setattr(cockpit, "_schedule_reset", lambda generation: calls.setdefault("schedule", generation))
    cockpit._delegation_generation = 5
    cockpit._delegation = _active_delegation(active=True, status="running")

    cockpit._finish_delegation("done", "the result")

    assert calls["announce"] == ("done", "the result")
    assert calls["schedule"] == 5
    assert cockpit._delegation["active"] is False
    assert cockpit._delegation["status"] == "done"
    assert cockpit._delegation["result"] == "the result"


# ── transport-failure log suppression + reset (D2) ───────────────────────


class _RaisingClient:
    """Stand-in for httpx.Client whose .post always raises -- simulates a
    dead/unreachable MCP endpoint without touching the network."""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        raise ConnectionError("nobody home")


class _OkClient:
    """Stand-in for httpx.Client whose .post always succeeds with an empty
    JSON-RPC result -- simulates the endpoint recovering."""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        return _OkResponse()


class _OkResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_mcp_call_first_transport_failure_warns_subsequent_are_debug(monkeypatch, caplog):
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(hermes_cockpit.httpx, "Client", _RaisingClient)

    with caplog.at_level("DEBUG", logger="patches.hermes_cockpit"):
        first = cockpit._mcp_call("events_poll", {})
        second = cockpit._mcp_call("events_poll", {})
        third = cockpit._mcp_call("events_poll", {})

    assert first is None and second is None and third is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    debugs = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(warnings) == 1
    assert len(debugs) == 2
    assert cockpit._consecutive_transport_failures == 3


def test_mcp_call_first_warning_names_url_and_docs(monkeypatch, caplog):
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(hermes_cockpit.httpx, "Client", _RaisingClient)

    with caplog.at_level("DEBUG", logger="patches.hermes_cockpit"):
        cockpit._mcp_call("events_poll", {})

    msg = caplog.records[0].getMessage()
    assert hermes_cockpit._MCP_URL in msg
    assert "docs/agent-lane.md" in msg
    assert "examples/agent-lane/verify_contract.py" in msg


def test_mcp_call_success_resets_failure_counter_and_rewarn(monkeypatch, caplog):
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(hermes_cockpit.httpx, "Client", _RaisingClient)

    with caplog.at_level("DEBUG", logger="patches.hermes_cockpit"):
        cockpit._mcp_call("events_poll", {})  # 1st failure -> WARNING
        cockpit._mcp_call("events_poll", {})  # 2nd failure -> DEBUG

        monkeypatch.setattr(hermes_cockpit.httpx, "Client", _OkClient)
        result = cockpit._mcp_call("events_poll", {})
        assert result == {"ok": True}
        assert cockpit._consecutive_transport_failures == 0

        monkeypatch.setattr(hermes_cockpit.httpx, "Client", _RaisingClient)
        cockpit._mcp_call("events_poll", {})  # fresh outage -> WARNING again

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2


def test_mcp_call_payload_error_always_warns_and_does_not_touch_counter(monkeypatch, caplog):
    cockpit = _make_cockpit(monkeypatch)

    class _ErrorResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}}

    class _ErrorClient(_OkClient):
        def post(self, *a, **kw):
            return _ErrorResponse()

    monkeypatch.setattr(hermes_cockpit.httpx, "Client", _ErrorClient)

    with caplog.at_level("DEBUG", logger="patches.hermes_cockpit"):
        cockpit._mcp_call("events_poll", {})
        cockpit._mcp_call("events_poll", {})

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2  # every payload-error call warns, unlike transport failures
    assert cockpit._consecutive_transport_failures == 0


# ── idle-poll backoff (D2) ─────────────────────────────────────────────────


def test_next_poll_interval_normal_idle_cadence_when_ok(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    cockpit.hermes_ok = True
    assert cockpit._next_poll_interval() == hermes_cockpit._POLL_IDLE_S
    assert cockpit._idle_failure_streak == 0


def test_next_poll_interval_backs_off_and_caps(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    cockpit.hermes_ok = False

    intervals = [cockpit._next_poll_interval() for _ in range(6)]

    assert intervals == [10.0, 20.0, 40.0, 60.0, 60.0, 60.0]
    assert all(i <= hermes_cockpit._POLL_BACKOFF_MAX_S for i in intervals)


def test_next_poll_interval_snaps_back_on_success(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    cockpit.hermes_ok = False
    for _ in range(3):
        cockpit._next_poll_interval()
    assert cockpit._idle_failure_streak == 3

    cockpit.hermes_ok = True
    assert cockpit._next_poll_interval() == hermes_cockpit._POLL_IDLE_S
    assert cockpit._idle_failure_streak == 0


def test_next_poll_interval_active_delegation_ignores_backoff(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    cockpit.hermes_ok = False
    cockpit._idle_failure_streak = 4  # pretend we were already backed off
    cockpit._delegation["active"] = True

    assert cockpit._next_poll_interval() == hermes_cockpit._POLL_ACTIVE_S
    assert cockpit._idle_failure_streak == 0


def test_next_poll_interval_pending_approvals_ignore_backoff(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    cockpit.hermes_ok = False
    cockpit._idle_failure_streak = 4
    cockpit._permissions = [{"id": "p1"}]

    assert cockpit._next_poll_interval() == hermes_cockpit._POLL_ACTIVE_S
    assert cockpit._idle_failure_streak == 0


# ── failed delegation is spoken as a plain phrase, never a class name or URL ──


def _failing_shim(exc):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            raise exc

    return _Client


def _run_failed_delegation(monkeypatch, exc):
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(hermes_cockpit, "_resolve_shim_token", lambda: "t")
    monkeypatch.setattr(hermes_cockpit.httpx, "Client", _failing_shim(exc))
    cockpit._run_delegation("do the thing")
    return cockpit._delegation["status"], cockpit._delegation["result"]


_PRIVATE_URL = "http://192.0.2.7:8087/v1/chat/completions"


def _status_error(code):
    request = hermes_cockpit.httpx.Request("POST", _PRIVATE_URL)
    response = hermes_cockpit.httpx.Response(code, request=request)
    return hermes_cockpit.httpx.HTTPStatusError(f"Server error '{code}' for url '{_PRIVATE_URL}'", request=request, response=response)


def test_failed_delegation_504_says_the_gateway_timed_out(monkeypatch):
    status, result = _run_failed_delegation(monkeypatch, _status_error(504))
    assert status == "error"
    assert result == "Delegation failed: the Hermes gateway timed out (504)."


def test_failed_delegation_other_status_says_error_and_code(monkeypatch):
    _, result = _run_failed_delegation(monkeypatch, _status_error(500))
    assert result == "Delegation failed: Hermes answered with an error (500)."


def test_failed_delegation_timeout_says_how_long(monkeypatch):
    exc = hermes_cockpit.httpx.ReadTimeout("read timed out", request=hermes_cockpit.httpx.Request("POST", _PRIVATE_URL))
    _, result = _run_failed_delegation(monkeypatch, exc)
    assert result == "Delegation failed: Hermes timed out after 15 minutes."


def test_failed_delegation_connection_error_says_unreachable(monkeypatch):
    exc = hermes_cockpit.httpx.ConnectError("refused", request=hermes_cockpit.httpx.Request("POST", _PRIVATE_URL))
    _, result = _run_failed_delegation(monkeypatch, exc)
    assert result == "Delegation failed: Hermes could not be reached."


def test_failed_delegation_connect_timeout_is_unreachable_not_a_long_wait(monkeypatch):
    exc = hermes_cockpit.httpx.ConnectTimeout("slow", request=hermes_cockpit.httpx.Request("POST", _PRIVATE_URL))
    _, result = _run_failed_delegation(monkeypatch, exc)
    assert result == "Delegation failed: Hermes could not be reached."


def test_failed_delegation_unknown_error_has_no_class_name(monkeypatch):
    _, result = _run_failed_delegation(monkeypatch, KeyError("boom"))
    assert result == "Delegation failed: something went wrong on the way to Hermes."


def test_failed_delegation_text_never_carries_url_or_host(monkeypatch):
    for exc in (_status_error(502), _status_error(504)):
        _, result = _run_failed_delegation(monkeypatch, exc)
        assert "192.0.2.7" not in result and "http" not in result and "8087" not in result


def test_failed_delegation_still_logs_the_detail(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="patches.hermes_cockpit"):
        _run_failed_delegation(monkeypatch, _status_error(500))
    assert any(_PRIVATE_URL in r.getMessage() for r in caplog.records)


# ── the last delegation outlives the quest-card reset ────────────────────


def _finished_cockpit(monkeypatch, status="done", result="Saved the shortlist to /data/notes/report.md"):
    cockpit = _make_cockpit(monkeypatch)
    cockpit.hermes_ok = True
    monkeypatch.setattr(cockpit, "_schedule_reset", lambda generation: None)
    cockpit._delegation_generation = 1
    cockpit._delegation = _active_delegation(active=True, status="running", task="research it", steps=[])
    cockpit._finish_delegation(status, result)
    return cockpit


def test_status_after_reset_still_reports_the_finished_result(monkeypatch):
    cockpit = _finished_cockpit(monkeypatch)
    cockpit._maybe_reset(1)
    assert cockpit._delegation["status"] == "idle"  # the card did clear
    summary = cockpit.status_summary("summary")
    assert "Saved the shortlist to /data/notes/report.md" in summary
    assert "finished" in summary
    assert "ago" in summary
    assert summary != "Hermes is idle."


def test_last_result_after_reset_returns_the_result(monkeypatch):
    cockpit = _finished_cockpit(monkeypatch)
    cockpit._maybe_reset(1)
    assert cockpit.status_summary("last_result") == (
        "Hermes's last result: Saved the shortlist to /data/notes/report.md"
    )


def test_steps_after_reset_return_the_final_step(monkeypatch):
    cockpit = _finished_cockpit(monkeypatch)
    cockpit._maybe_reset(1)
    steps = cockpit.status_summary("steps")
    assert steps.startswith("Recent Hermes activity: ")
    assert "done: Saved the shortlist" in steps


def test_error_finish_after_reset_reports_the_error(monkeypatch):
    cockpit = _finished_cockpit(monkeypatch, status="error", result="Hermes could not be reached")
    cockpit._maybe_reset(1)
    summary = cockpit.status_summary("summary")
    assert "hit an error" in summary
    assert "ago" in summary
    assert "Hermes could not be reached" in summary


def test_fresh_cockpit_strings_are_unchanged(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    cockpit.hermes_ok = True
    assert cockpit.status_summary("summary") == "Hermes is idle."
    assert cockpit.status_summary("steps") == "No Hermes activity recorded yet."
    assert cockpit.status_summary("last_result") == "Hermes hasn't finished anything yet."


def test_new_active_delegation_wins_and_old_record_survives_until_it_finishes(monkeypatch):
    import time

    cockpit = _finished_cockpit(monkeypatch, result="first result")
    cockpit._maybe_reset(1)
    cockpit._delegation_generation = 2
    cockpit._delegation = _active_delegation(
        active=True, status="running", task="second", started_ts=time.time(), steps=[]
    )
    summary = cockpit.status_summary("summary")
    assert "working" in summary
    assert "first result" not in summary
    assert cockpit._last_delegation["result"] == "first result"  # not cleared by the new delegation
    cockpit._finish_delegation("done", "second result")
    assert cockpit._last_delegation["result"] == "second result"
    assert cockpit._last_delegation["task"] == "second"


def test_permission_suffix_kept_on_the_last_record_summary(monkeypatch):
    cockpit = _finished_cockpit(monkeypatch)
    cockpit._maybe_reset(1)
    cockpit._permissions = [{"id": "p1"}]
    assert cockpit.status_summary("summary").endswith("1 approval waiting for your decision.")


# ── spoken announcement fits the TTS cap ─────────────────────────────────

_TAIL = " Ask me for the details if you want more."


def test_spoken_result_replaces_absolute_paths_with_a_file():
    spoken = hermes_cockpit._spoken_result("Saved it to /data/notes/report.md and ~/docs/a.txt. Done.")
    assert spoken == "Saved it to a file and a file. Done."
    assert "/" not in spoken


def test_spoken_result_keeps_single_slash_words_and_non_paths():
    text = "Use and/or the /etc setting, about 3/4 done"
    assert hermes_cockpit._spoken_result(text) == text


def test_build_announcement_has_no_path():
    assert "/" not in hermes_cockpit._build_announcement("done", "Saved to /data/notes/report.md")


def test_spoken_result_long_text_cuts_at_a_sentence_end():
    text = "This is a sentence of moderate length that says something. " * 10
    spoken = hermes_cockpit._spoken_result(text)
    assert spoken.endswith("something." + _TAIL)
    assert len(spoken) <= hermes_cockpit._ANNOUNCE_RESULT_CHARS + len(_TAIL)
    body = spoken[: -len(_TAIL)]
    assert len(body) > 60


def test_spoken_result_without_sentence_end_cuts_at_a_word_boundary():
    spoken = hermes_cockpit._spoken_result("alpha beta gamma delta " * 30)
    assert spoken.endswith(_TAIL)
    body = spoken[: -len(_TAIL)]
    assert body.split()[-1] in {"alpha", "beta", "gamma", "delta"}  # never mid-word
    assert len(spoken) <= hermes_cockpit._ANNOUNCE_RESULT_CHARS + len(_TAIL)


def test_spoken_result_early_sentence_end_is_ignored():
    spoken = hermes_cockpit._spoken_result("Short. " + "alpha beta gamma delta " * 30)
    body = spoken[: -len(_TAIL)]
    assert body != "Short."
    assert len(body) > 60
    assert body.split()[-1] in {"alpha", "beta", "gamma", "delta"}


def test_announce_cap_is_tight_enough_for_the_tts_limit():
    assert hermes_cockpit._ANNOUNCE_RESULT_CHARS <= 180


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Saved it to ~/notes.txt for you.", "Saved it to a file for you."),
        ('Path is "/home/a/b" now.', "Path is a file now."),
        ("Path is (/home/a/b) now.", "Path is a file now."),
        ("Path is `/home/a/b` now.", "Path is a file now."),
        ("Path is [/home/a/b] now.", "Path is a file now."),
        ("Path is '/home/a/b'.", "Path is a file."),
        ("Saved (to `~/x/y.md`), done.", "Saved (to a file, done."),
        ("Wrote /home/a/b.", "Wrote a file."),
        ("See https://example.com/a/b now", "See https://example.com/a/b now"),
        ("about 1/2 of it, and/or 24/7", "about 1/2 of it, and/or 24/7"),
        ("the /etc setting", "the /etc setting"),
        ("a ~/ alone", "a ~/ alone"),
    ],
)
def test_spoken_paths_wrapped_and_single_segment(text, expected):
    assert hermes_cockpit._spoken_result(text) == expected


def test_finish_delegation_record_is_built_from_the_finishing_task(monkeypatch):
    cockpit = _finished_cockpit(monkeypatch, result="the result")
    rec = cockpit._last_delegation
    assert rec["task"] == "research it"
    assert rec["status"] == "done"
    assert rec["steps"][-1]["text"] == "done: the result"


def test_finish_delegation_record_ignores_a_delegation_landing_after_the_lock(monkeypatch):
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(cockpit, "_schedule_reset", lambda generation: None)
    cockpit._delegation = _active_delegation(active=True, status="running", task="first", steps=[])
    real_append = cockpit._append_step

    def _append_then_new_delegation(role, text):
        real_append(role, text)
        cockpit._delegation = _active_delegation(active=True, status="running", task="newer", steps=[])

    monkeypatch.setattr(cockpit, "_append_step", _append_then_new_delegation)
    cockpit._finish_delegation("done", "r")
    assert cockpit._last_delegation["task"] == "first"
    assert cockpit._last_delegation["steps"][-1]["text"] == "done: r"


# ── delegation outcome: Telegram post (VOICE_HERMES_NOTIFY) + brain memory ───


def _finish_with_posts(monkeypatch, status="done", result="short", full_text=None, target="telegram:test-chat", **kw):
    monkeypatch.setattr(hermes_cockpit, "_HERMES_TARGET", target)
    cockpit = _make_cockpit(monkeypatch, **kw)
    posted = []
    sent = threading.Event()

    def fake_tool_call(name, args):
        posted.append((name, args))
        sent.set()
        return {"ok": True}

    monkeypatch.setattr(cockpit, "_tool_call", fake_tool_call)
    monkeypatch.setattr(cockpit, "_schedule_reset", lambda generation: None)
    cockpit._delegation = _active_delegation(active=True, status="running")
    cockpit._delegation["task"] = "make the list"
    cockpit._finish_delegation(status, result, full_text=full_text)
    if target and os.environ.get("VOICE_HERMES_NOTIFY", "").strip().lower() != "off":
        sent.wait(2)
    else:
        time.sleep(0.05)  # give a (wrongly) started post thread a chance to show itself
    return cockpit, posted


def test_finish_posts_full_untruncated_result_to_target(monkeypatch):
    monkeypatch.delenv("VOICE_HERMES_NOTIFY", raising=False)
    full = "line one\n\nline two " + "y" * 600  # longer than the 400-char record result
    _, posted = _finish_with_posts(monkeypatch, result="trunc...", full_text=full)
    assert posted == [
        (
            "messages_send",
            {
                "target": "telegram:test-chat",
                "message": f"Hermes finished your voice request: make the list\n\n{full}",
            },
        )
    ]


def test_finish_error_wording(monkeypatch):
    monkeypatch.delenv("VOICE_HERMES_NOTIFY", raising=False)
    _, posted = _finish_with_posts(monkeypatch, status="error", result="Delegation failed: x.")
    assert posted[0][1]["message"] == "Hermes couldn't finish your voice request: make the list\n\nDelegation failed: x."


def test_finish_post_is_capped_at_3500_with_ellipsis(monkeypatch):
    monkeypatch.delenv("VOICE_HERMES_NOTIFY", raising=False)
    _, posted = _finish_with_posts(monkeypatch, full_text="z" * 5000)
    body = posted[0][1]["message"].split("\n\n", 1)[1]
    assert len(body) == 3500 and body.endswith("…")


def test_finish_does_not_post_without_target(monkeypatch):
    monkeypatch.delenv("VOICE_HERMES_NOTIFY", raising=False)
    _, posted = _finish_with_posts(monkeypatch, target="")
    assert posted == []


def test_finish_does_not_post_when_notify_off(monkeypatch):
    monkeypatch.setenv("VOICE_HERMES_NOTIFY", "OFF")
    _, posted = _finish_with_posts(monkeypatch)
    assert posted == []


def test_finish_survives_a_failed_post(monkeypatch):
    monkeypatch.delenv("VOICE_HERMES_NOTIFY", raising=False)
    monkeypatch.setattr(hermes_cockpit, "_HERMES_TARGET", "telegram:test-chat")
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(cockpit, "_tool_call", lambda n, a: None)
    monkeypatch.setattr(cockpit, "_schedule_reset", lambda generation: None)
    cockpit._delegation = _active_delegation(active=True, status="running")
    cockpit._finish_delegation("done", "ok")  # must not raise
    assert cockpit._last_delegation["status"] == "done"


class _FakeChat:
    def __init__(self):
        self.items = []

    def add_item(self, item):
        self.items.append(item)


def test_finish_appends_one_assistant_item_to_chat(monkeypatch):
    monkeypatch.setenv("VOICE_HERMES_ANNOUNCE", "off")  # independent of speech
    chat = _FakeChat()
    _finish_with_posts(
        monkeypatch, result="the GPU list is ready", runtime_config=types.SimpleNamespace(chat=chat)
    )
    assert len(chat.items) == 1
    item = chat.items[0]
    assert item.role == "assistant"
    assert item.content[0].type == "output_text"
    assert item.content[0].text == 'Hermes finished the task "make the list": the GPU list is ready'


def test_finish_without_runtime_config_is_fine(monkeypatch):
    cockpit, _ = _finish_with_posts(monkeypatch)
    assert cockpit._last_delegation["status"] == "done"


def test_chat_failure_never_raises_into_finish(monkeypatch):
    class _Boom:
        def add_item(self, item):
            raise RuntimeError("nope")

    cockpit, _ = _finish_with_posts(monkeypatch, runtime_config=types.SimpleNamespace(chat=_Boom()))
    assert cockpit._last_delegation["status"] == "done"


def test_send_message_result_is_honest(monkeypatch):
    monkeypatch.setattr(hermes_cockpit, "_HERMES_TARGET", "telegram:test-chat")
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(cockpit, "_tool_call", lambda n, a: {"ok": True})
    assert cockpit.send_message("hi") == (
        "Posted to your chat with Hermes. Hermes doesn't act on these; to have it do something, delegate it."
    )


def test_finish_survives_a_notify_thread_that_cannot_start(monkeypatch):
    monkeypatch.delenv("VOICE_HERMES_NOTIFY", raising=False)
    monkeypatch.setattr(hermes_cockpit, "_HERMES_TARGET", "telegram:test-chat")
    cockpit = _make_cockpit(monkeypatch)
    monkeypatch.setattr(cockpit, "_schedule_reset", lambda generation: None)
    announced = []
    monkeypatch.setattr(cockpit, "_announce_completion", lambda status, result: announced.append(status))
    real_start = threading.Thread.start

    def start(self):
        if self.name == "hermes-cockpit-notify":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    cockpit._delegation = _active_delegation(active=True, status="running")
    cockpit._finish_delegation("done", "r", full_text="r")
    assert cockpit._last_delegation["status"] == "done"
    assert announced == ["done"]


def test_finish_post_caps_a_long_task_line(monkeypatch):
    monkeypatch.delenv("VOICE_HERMES_NOTIFY", raising=False)
    monkeypatch.setattr(hermes_cockpit, "_HERMES_TARGET", "telegram:test-chat")
    cockpit = _make_cockpit(monkeypatch)
    posted, sent = [], threading.Event()
    monkeypatch.setattr(cockpit, "_tool_call", lambda n, a: (posted.append(a), sent.set(), {"ok": True})[-1])
    monkeypatch.setattr(cockpit, "_schedule_reset", lambda generation: None)
    cockpit._delegation = _active_delegation(active=True, status="running")
    cockpit._delegation["task"] = "t" * 1000
    cockpit._finish_delegation("done", "r", full_text="z" * 5000)
    sent.wait(2)
    assert len(posted[0]["message"]) < 4096
