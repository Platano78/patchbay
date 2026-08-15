"""Unit tests for lm_output_processor.py's per-turn tool-call cap and
filler cadence (runaway tool-chain guard).

Run from repo root: python3 -m pytest patches/test_lm_output_processor.py -v

The real ``speech_to_speech`` package is not installed in this repo, so the
symbols lm_output_processor.py imports from it are stubbed into
``sys.modules`` before the import below -- same hermetic pattern as
``test_brain_control.py``/``test_reflex_lane.py``. ``openai`` IS a real,
installed dependency, so ``RealtimeConversationItemFunctionCallOutput`` is
imported for real rather than stubbed -- tests assert against its actual
``.output`` attribute.
"""

from __future__ import annotations

import itertools
import sys
import types
from queue import Queue
from typing import Generic, TypeVar

_id_counter = itertools.count(1)

# ── Stub the speech_to_speech surface lm_output_processor.py imports ───

_I = TypeVar("_I")
_O = TypeVar("_O")


class _StubBaseHandler(Generic[_I, _O]):
    """Minimal stand-in mirroring BaseHandler's constructor contract:
    ``setup(*setup_args, **setup_kwargs)`` is called from __init__, same as
    the real class."""

    def __init__(self, stop_event=None, queue_in=None, queue_out=None, setup_args=(), setup_kwargs=None):
        self.stop_event = stop_event
        self.queue_in = queue_in
        self.queue_out = queue_out
        self.setup(*(setup_args or ()), **(setup_kwargs or {}))

    def setup(self, *args, **kwargs):
        pass


class _Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# process() does isinstance() checks to dispatch between TokenUsage,
# EndOfResponse, and LLMResponseChunk -- these must be genuinely distinct
# classes, not all aliased to the same _Msg, or the first isinstance check
# swallows every message type.
class _TokenUsage(_Msg):
    pass


class _EndOfResponse(_Msg):
    pass


class _LLMResponseChunk(_Msg):
    pass


class _GenerateResponseRequest(_Msg):
    pass


class _TTSInput(_Msg):
    pass


class _TurnStatsRecorder:
    """No-op stand-in for the module-level turn_stats singleton -- these
    tests only need the calls not to raise."""

    def on_llm_chunk(self, speech_stopped_at_s):
        pass

    def on_tts_input(self):
        pass

    def note_followup_pending(self):
        pass

    def end_of_response(self):
        pass

    def set_route(self, route):
        pass


def _install_stubs():
    def mod(name, **attrs):
        # Merge onto an existing sys.modules entry rather than replacing it:
        # multiple test files stub the same speech_to_speech.* submodules,
        # and pytest imports every test module before any test function
        # runs -- a later file's stub would otherwise silently erase
        # attributes an earlier file's LAZY (call-time) import still needs.
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        for k, v in attrs.items():
            setattr(m, k, v)
        return m

    pkg = mod("speech_to_speech")
    vt = mod("speech_to_speech.voice_tools", execute=lambda name, kwargs: "", VISUAL_TOOLS=frozenset({"show_face"}))
    pkg.voice_tools = vt
    mod("speech_to_speech.baseHandler", BaseHandler=_StubBaseHandler)
    mod("speech_to_speech.pipeline")
    mod(
        "speech_to_speech.pipeline.events",
        AssistantTextEvent=_Msg,
        ResponseFailedEvent=_Msg,
        TokenUsageEvent=_Msg,
    )
    mod("speech_to_speech.pipeline.handler_types", LLMOut=object, TTSIn=object)
    mod(
        "speech_to_speech.pipeline.messages",
        EndOfResponse=_EndOfResponse,
        GenerateResponseRequest=_GenerateResponseRequest,
        LLMResponseChunk=_LLMResponseChunk,
        TokenUsage=_TokenUsage,
        TTSInput=_TTSInput,
    )
    mod("speech_to_speech.pipeline.queue_types", TextEventItem=object, TextPromptItem=object)
    mod("speech_to_speech.pipeline.speculative_turns", SpeculativeTurnTracker=object)
    mod("speech_to_speech.turn_stats", turn_stats=_TurnStatsRecorder())
    mod("speech_to_speech.utils")
    # _generate_id must hand back a DISTINCT id per call: the id is an
    # identity key during chat compaction, so a stub that returns a constant
    # would let an id-collision bug through. test_stream_watchdog.py stubs
    # this same attribute with a constant-per-prefix lambda -- passing our
    # own here unconditionally overwrites it whichever file is imported
    # first, and lm_output_processor binds it by value at import (below),
    # so a later re-stub by that file cannot reach us.
    mod(
        "speech_to_speech.utils.utils",
        response_wants_audio=lambda response: True,
        _generate_id=lambda prefix: f"{prefix}_{next(_id_counter)}",
    )


_install_stubs()

from patches import lm_output_processor  # noqa: E402

# ── fakes ────────────────────────────────────────────────────────────────


class _FakeToolCall:
    def __init__(self, name, call_id, arguments="{}"):
        self.name = name
        self.call_id = call_id
        self.arguments = arguments


class _FakeChat:
    """Stands in for LLM/chat.py's Chat.

    ``buffer`` mirrors the real ``_append_tool_output_locked``, which does a
    bare ``self.buffer.append(output_item)`` -- notably WITHOUT routing through
    ``add_item()``, so nothing upstream backfills a missing ``id``. That
    asymmetry is what ``test_buffer_items_all_satisfy_compaction_invariant``
    pins down: whatever we construct is exactly what the compactor later sees.
    """

    def __init__(self):
        self.tool_outputs: list[tuple[str, object]] = []
        self.buffer: list[object] = []

    def append_tool_output(self, call_id, output_obj):
        self.tool_outputs.append((call_id, output_obj))
        self.buffer.append(output_obj)


class _FakeRuntimeConfig:
    def __init__(self):
        self.chat = _FakeChat()


def _make_processor(**setup_kwargs) -> lm_output_processor.LMOutputProcessor:
    return lm_output_processor.LMOutputProcessor(
        None,
        queue_in=Queue(),
        queue_out=Queue(),
        setup_kwargs=setup_kwargs,
    )


def _chunk(*, tools, runtime_config, turn_id="turn-1", turn_revision=0, text=""):
    return lm_output_processor.LLMResponseChunk(
        text=text,
        tools=tools,
        runtime_config=runtime_config,
        turn_id=turn_id,
        turn_revision=turn_revision,
        cancel_generation=False,
        language_code=None,
        response=None,
        speech_stopped_at_s=None,
    )


def _run_round(proc, rc, n, *, num_calls=1, turn_id="turn-1"):
    tool_calls = [_FakeToolCall("weather", f"call_r{n}_{i}") for i in range(num_calls)]
    chunk = _chunk(tools=tool_calls, runtime_config=rc, turn_id=turn_id)
    list(proc.process(chunk))
    return tool_calls


# ── VOICE_TOOL_CALL_CAP env parsing (pure) ─────────────────────────────


def test_parse_tool_call_cap_default_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_TOOL_CALL_CAP", raising=False)
    assert lm_output_processor._parse_tool_call_cap() == 8


def test_parse_tool_call_cap_blank_is_default(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "   ")
    assert lm_output_processor._parse_tool_call_cap() == 8


def test_parse_tool_call_cap_zero_disables(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "0")
    assert lm_output_processor._parse_tool_call_cap() is None


def test_parse_tool_call_cap_off_disables_case_insensitive(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "Off")
    assert lm_output_processor._parse_tool_call_cap() is None


def test_parse_tool_call_cap_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "banana")
    assert lm_output_processor._parse_tool_call_cap() == 8


def test_parse_tool_call_cap_negative_disables(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "-1")
    assert lm_output_processor._parse_tool_call_cap() is None


def test_parse_tool_call_cap_custom_value(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "3")
    assert lm_output_processor._parse_tool_call_cap() == 3


# ── VOICE_TOOL_FILLER_EVERY env parsing (pure) ─────────────────────────


def test_parse_filler_every_default_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_TOOL_FILLER_EVERY", raising=False)
    assert lm_output_processor._parse_tool_filler_every() == 3


def test_parse_filler_every_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_FILLER_EVERY", "nope")
    assert lm_output_processor._parse_tool_filler_every() == 3


def test_parse_filler_every_one_is_every_round(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_FILLER_EVERY", "1")
    assert lm_output_processor._parse_tool_filler_every() == 1


def test_parse_filler_every_less_than_one_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_FILLER_EVERY", "0")
    assert lm_output_processor._parse_tool_filler_every() == 3


# ── _next_tool_round: per-turn counter + reset on turn_id change ───────


def test_next_tool_round_increments_within_a_turn():
    proc = _make_processor()
    assert proc._next_tool_round("turn-a") == 1
    assert proc._next_tool_round("turn-a") == 2
    assert proc._next_tool_round("turn-a") == 3


def test_next_tool_round_resets_on_turn_id_change():
    proc = _make_processor()
    proc._next_tool_round("turn-a")
    proc._next_tool_round("turn-a")
    assert proc._next_tool_round("turn-b") == 1


def test_next_tool_round_none_turn_id_shares_one_key():
    proc = _make_processor()
    assert proc._next_tool_round(None) == 1
    assert proc._next_tool_round(None) == 2
    assert proc._next_tool_round("turn-a") == 1  # switching away from None resets
    assert proc._next_tool_round(None) == 1  # switching back to None resets again


# ── cap boundary behaviour, executed through process() end-to-end ──────


def test_cap_boundary_full_sequence(monkeypatch):
    calls = []

    def fake_execute(name, kwargs):
        calls.append((name, kwargs))
        return "ok"

    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", fake_execute)
    tpq = Queue()
    proc = _make_processor(text_prompt_queue=tpq)
    rc = _FakeRuntimeConfig()

    # rounds 1..8 (== default cap) execute normally
    for n in range(1, 9):
        _run_round(proc, rc, n)
    assert len(calls) == 8
    assert tpq.qsize() == 8
    assert all(output_obj.output == "ok" for _, output_obj in rc.chat.tool_outputs)

    # round 9 (cap+1): refused -- every call_id gets the synthetic output,
    # execute is NOT called, follow-up is still pushed
    tool_calls_9 = _run_round(proc, rc, 9, num_calls=2)
    assert len(calls) == 8  # unchanged
    assert tpq.qsize() == 9
    synth_9 = dict(rc.chat.tool_outputs[-2:])
    for tc in tool_calls_9:
        assert synth_9[tc.call_id].output == lm_output_processor._SYNTHETIC_CAP_OUTPUT

    # round 10 (cap+2): still refused, follow-up still pushed
    _run_round(proc, rc, 10)
    assert len(calls) == 8
    assert tpq.qsize() == 10
    assert rc.chat.tool_outputs[-1][1].output == lm_output_processor._SYNTHETIC_CAP_OUTPUT

    # round 11 (cap+3): refused, output still recorded for every call_id,
    # but the follow-up is NOT pushed -- the chain ends here
    tool_calls_11 = _run_round(proc, rc, 11, num_calls=3)
    assert len(calls) == 8
    assert tpq.qsize() == 10  # unchanged from round 10
    synth_11 = dict(rc.chat.tool_outputs[-3:])
    for tc in tool_calls_11:
        assert synth_11[tc.call_id].output == lm_output_processor._SYNTHETIC_CAP_OUTPUT


def test_cap_boundary_custom_cap_value(monkeypatch):
    # Same shape as the default-cap test but with a small custom cap, to
    # make sure the cap isn't hardcoded to 8 anywhere.
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "2")
    calls = []
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: calls.append(1) or "ok")
    tpq = Queue()
    proc = _make_processor(text_prompt_queue=tpq)
    rc = _FakeRuntimeConfig()

    _run_round(proc, rc, 1)
    _run_round(proc, rc, 2)
    assert len(calls) == 2
    assert tpq.qsize() == 2

    # round 3 (cap+1): refused, follow-up still pushed
    _run_round(proc, rc, 3)
    assert len(calls) == 2
    assert tpq.qsize() == 3
    assert rc.chat.tool_outputs[-1][1].output == lm_output_processor._SYNTHETIC_CAP_OUTPUT

    # round 4 (cap+2): refused, follow-up still pushed
    _run_round(proc, rc, 4)
    assert tpq.qsize() == 4

    # round 5 (cap+3): refused, follow-up NOT pushed -- chain ends
    _run_round(proc, rc, 5)
    assert len(calls) == 2
    assert tpq.qsize() == 4
    assert rc.chat.tool_outputs[-1][1].output == lm_output_processor._SYNTHETIC_CAP_OUTPUT


# ── cap disable ("0" / "off") ────────────────────────────────────────────


def test_cap_disabled_via_zero_never_refuses(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "0")
    calls = []
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: calls.append(1) or "ok")
    tpq = Queue()
    proc = _make_processor(text_prompt_queue=tpq)
    rc = _FakeRuntimeConfig()

    for n in range(1, 21):  # well past the default cap of 8
        _run_round(proc, rc, n)

    assert len(calls) == 20
    assert tpq.qsize() == 20
    assert all(output_obj.output == "ok" for _, output_obj in rc.chat.tool_outputs)


def test_cap_disabled_via_off_never_refuses(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "off")
    calls = []
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: calls.append(1) or "ok")
    tpq = Queue()
    proc = _make_processor(text_prompt_queue=tpq)
    rc = _FakeRuntimeConfig()

    for n in range(1, 21):
        _run_round(proc, rc, n)

    assert len(calls) == 20
    assert all(output_obj.output == "ok" for _, output_obj in rc.chat.tool_outputs)


# ── counter reset via process(), not just the raw helper ───────────────


def test_process_resets_round_counter_when_turn_id_changes(monkeypatch):
    # A custom cap of 1 makes it easy to see the reset: round 1 of a NEW
    # turn must still be treated as round 1 (within cap), not round 2 of
    # the previous turn's counter (which would be refused).
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "1")
    calls = []
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: calls.append(1) or "ok")
    proc = _make_processor()
    rc_a = _FakeRuntimeConfig()
    rc_b = _FakeRuntimeConfig()

    _run_round(proc, rc_a, 1, turn_id="turn-a")
    assert len(calls) == 1
    assert rc_a.chat.tool_outputs[-1][1].output == "ok"

    # New turn: round 1 again, still within cap=1, must execute (not refuse).
    _run_round(proc, rc_b, 1, turn_id="turn-b")
    assert len(calls) == 2
    assert rc_b.chat.tool_outputs[-1][1].output == "ok"


# ── filler cadence ───────────────────────────────────────────────────────


def test_filler_cadence_default_every_third_round(monkeypatch):
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    picks = []
    monkeypatch.setattr(lm_output_processor, "_pick_filler", lambda: picks.append(1) or "filler text")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    for n in range(1, 8):
        _run_round(proc, rc, n)

    assert len(picks) == 3  # rounds 1, 4, 7


def test_filler_cadence_every_round_when_set_to_one(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_FILLER_EVERY", "1")
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    picks = []
    monkeypatch.setattr(lm_output_processor, "_pick_filler", lambda: picks.append(1) or "filler text")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    for n in range(1, 8):
        _run_round(proc, rc, n)

    assert len(picks) == 7  # every round


def test_filler_cadence_malformed_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_FILLER_EVERY", "not-a-number")
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    picks = []
    monkeypatch.setattr(lm_output_processor, "_pick_filler", lambda: picks.append(1) or "filler text")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    for n in range(1, 8):
        _run_round(proc, rc, n)

    assert len(picks) == 3  # falls back to default cadence of 3


def test_filler_cadence_applies_even_when_rounds_are_refused(monkeypatch):
    # Cadence gating uses the same round counter as the cap, so a refused
    # round (past the cap) still participates in the cadence count.
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "2")
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    picks = []
    monkeypatch.setattr(lm_output_processor, "_pick_filler", lambda: picks.append(1) or "filler text")
    proc = _make_processor(text_prompt_queue=Queue())
    rc = _FakeRuntimeConfig()

    for n in range(1, 5):  # rounds 1,2 execute; 3,4 refused (cap+1, cap+2)
        _run_round(proc, rc, n)

    assert len(picks) == 2  # rounds 1 and 4 (default cadence of 3)


# ── visual-tool / no-audio filler gates untouched on cadence-selected rounds


def test_filler_not_consulted_for_all_visual_tools_on_cadence_round(monkeypatch):
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    picks = []
    monkeypatch.setattr(lm_output_processor, "_pick_filler", lambda: picks.append(1) or "filler text")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    chunk = _chunk(tools=[_FakeToolCall("show_face", "call_1")], runtime_config=rc, turn_id="turn-1")
    list(proc.process(chunk))  # round 1, would normally play a filler

    assert picks == []


def test_filler_not_consulted_when_response_does_not_want_audio(monkeypatch):
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    monkeypatch.setattr(lm_output_processor, "response_wants_audio", lambda response: False)
    picks = []
    monkeypatch.setattr(lm_output_processor, "_pick_filler", lambda: picks.append(1) or "filler text")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    chunk = _chunk(tools=[_FakeToolCall("weather", "call_1")], runtime_config=rc, turn_id="turn-1")
    list(proc.process(chunk))  # round 1

    assert picks == []


# ── tool-output item ids (chat-compaction invariant) ────────────────────
#
# Every item that lands in the chat buffer must carry a non-empty id.
# Compaction asserts exactly that (chat.py's _to_responses_api_chat_locked:
# ``assert item.id is not None and item.id != ""``) and an id-less item
# there raises mid-_generate, poisoning chat state for the life of the
# process -- the assistant goes permanently silent, no crash, until restart.
# append_tool_output() bypasses add_item(), which is the only thing that
# backfills ids, so these two construction sites have to supply their own.


def _assert_compaction_invariant(chat):
    """Replicate the assert compaction runs over the buffer."""
    assert chat.buffer, "expected at least one buffered item to check"
    for item in chat.buffer:
        item_id = getattr(item, "id", None)
        assert item_id is not None and item_id != "", f"item.id is {item_id!r}"


def test_real_result_tool_output_has_non_empty_id(monkeypatch):
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    _run_round(proc, rc, 1)

    assert len(rc.chat.tool_outputs) == 1
    output_obj = rc.chat.tool_outputs[0][1]
    assert output_obj.output == "ok"  # the real-result path, not the synthetic one
    assert output_obj.id


def test_synthetic_cap_tool_output_has_non_empty_id(monkeypatch):
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "1")
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    _run_round(proc, rc, 1)  # within cap
    _run_round(proc, rc, 2)  # cap+1 -> synthetic refusal

    output_obj = rc.chat.tool_outputs[-1][1]
    assert output_obj.output == lm_output_processor._SYNTHETIC_CAP_OUTPUT
    assert output_obj.id


def test_tool_output_ids_are_unique_across_calls(monkeypatch):
    # A constant id would satisfy "non-empty" while still corrupting
    # compaction: ids are identity keys (marker_ids/drop_ids), so a
    # collision makes the compactor drop the wrong items.
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "2")
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    _run_round(proc, rc, 1, num_calls=3)  # real-result path, 3 calls
    _run_round(proc, rc, 2, num_calls=2)  # real-result path, 2 calls
    _run_round(proc, rc, 3, num_calls=3)  # cap+1 -> synthetic path, 3 calls

    ids = [output_obj.id for _, output_obj in rc.chat.tool_outputs]
    assert len(ids) == 8
    assert len(set(ids)) == len(ids), f"duplicate tool-output ids: {ids}"


def test_buffer_items_all_satisfy_compaction_invariant(monkeypatch):
    """Regression test for the permanent-silence bug.

    Drives real tool calls through process() across both the executed and
    the over-cap synthetic paths, then asserts the buffer satisfies the same
    invariant compaction asserts. Fails against the pre-fix code, where
    neither construction site passed an id.
    """
    monkeypatch.setenv("VOICE_TOOL_CALL_CAP", "3")
    monkeypatch.setattr(lm_output_processor.voice_tools, "execute", lambda name, kwargs: "ok")
    proc = _make_processor()
    rc = _FakeRuntimeConfig()

    for n in range(1, 6):  # rounds 1-3 execute, 4-5 go over cap
        _run_round(proc, rc, n, num_calls=2)

    assert len(rc.chat.buffer) == 10
    _assert_compaction_invariant(rc.chat)
