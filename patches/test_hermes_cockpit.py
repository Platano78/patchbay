"""Unit tests for hermes_cockpit.py's spoken completion announcement and
quest-card auto-clear (delegation finishes silently -> speak it; finished
task stays on the card forever -> auto-clear after 90s unless superseded).

Run from repo root: python3 -m pytest patches/test_hermes_cockpit.py -v

The real ``speech_to_speech`` package is not installed in this repo; the two
symbols hermes_cockpit.py imports from it are stubbed into ``sys.modules``
before the import below, same hermetic pattern as
``test_reflex_lane.py``/``test_brain_control.py``. ``PipelineEvent`` is
aliased to the REAL ``pydantic.BaseModel`` (not a fake) since
``CockpitStateEvent``/``SearchLinksEvent`` rely on real pydantic field
semantics.

No real 90s waits: the generation-counter reset logic is tested by calling
``_maybe_reset`` directly rather than waiting on a real ``threading.Timer``.
"""

from __future__ import annotations

import sys
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
