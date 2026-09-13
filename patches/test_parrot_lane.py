"""Unit tests for parrot_lane.py (B6 ear-test instrument).

Run from repo root: python3 -m unittest patches.test_parrot_lane -v

Mirrors test_reflex_lane.py's stubbing of the ``speech_to_speech`` surface
(not installed in this repo). The stub modules are hermetic and merged with
whatever test_reflex_lane.py already installed, since pytest imports every
test module before any test function runs.
"""

from __future__ import annotations

import sys
import types
import unittest
from queue import Queue
from typing import Generic, TypeVar

# ── Stub the speech_to_speech surface parrot_lane (and reflex_lane) import ──

_I = TypeVar("_I")
_O = TypeVar("_O")


class _StubBaseHandler(Generic[_I, _O]):
    """Minimal stand-in mirroring BaseHandler's constructor contract."""

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


class _GenerateResponseRequest(_Msg):
    pass


class _LLMResponseChunk(_Msg):
    pass


class _EndOfResponse(_Msg):
    pass


class _TurnStatsRecorder:
    """Records the two turn_stats calls the gates make, for assertions."""

    def __init__(self):
        self.calls = []

    def on_llm_chunk(self, speech_stopped_at_s):
        self.calls.append(("on_llm_chunk", speech_stopped_at_s))

    def set_route(self, route):
        self.calls.append(("set_route", route))


def _install_stubs():
    def mod(name, **attrs):
        # Merge onto an existing sys.modules entry rather than replacing it:
        # test_reflex_lane.py stubs the same speech_to_speech.* submodules,
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
    vt = mod("speech_to_speech.voice_tools", execute=lambda name, kwargs: "")
    pkg.voice_tools = vt
    mod("speech_to_speech.baseHandler", BaseHandler=_StubBaseHandler)
    mod("speech_to_speech.pipeline")
    mod("speech_to_speech.pipeline.handler_types", LLMIn=object, LLMOut=object)
    # Message classes are ADOPTED, not overwritten. reflex_lane binds these by
    # `from ... import` at *import* time, so if test_reflex_lane.py imported
    # first, reflex_lane already holds ITS classes and no later setattr can
    # retarget that binding. Overwriting here would leave the gate doing
    # isinstance() against a class this file never constructs -- the gate then
    # falls through to passthrough and the short-circuit assertions fail,
    # but only in that collection order. Adopt-if-present keeps every module
    # and this file on ONE class object regardless of which file loads first.
    msgs = mod("speech_to_speech.pipeline.messages")
    for _name, _cls in (
        ("GenerateResponseRequest", _GenerateResponseRequest),
        ("LLMResponseChunk", _LLMResponseChunk),
        ("EndOfResponse", _EndOfResponse),
    ):
        if not hasattr(msgs, _name):
            setattr(msgs, _name, _cls)
    mod("speech_to_speech.pipeline.queue_types", LMOutItem=object)
    mod("speech_to_speech.turn_stats", turn_stats=_TurnStatsRecorder())


_install_stubs()

# Rebind the module-level names to whatever _install_stubs actually settled on,
# so requests built below are instances of the SAME class the modules under test
# are bound to -- by construction, not by hoping about collection order.
_msgs = sys.modules["speech_to_speech.pipeline.messages"]
_GenerateResponseRequest = _msgs.GenerateResponseRequest
_LLMResponseChunk = _msgs.LLMResponseChunk
_EndOfResponse = _msgs.EndOfResponse

from patches import reflex_lane  # noqa: E402

# parrot_lane imports its shared helpers via `speech_to_speech.reflex_lane`
# (matching how it is deployed by apply.sh into the installed package), so
# that name must resolve before parrot_lane is imported. Register the REAL
# patches.reflex_lane module under it rather than a fake, so parrot_lane
# exercises the actual _last_user_text/emit_synthetic_reply code.
sys.modules.setdefault("speech_to_speech.reflex_lane", reflex_lane)

from patches import parrot_lane  # noqa: E402


# ── Fakes for the chat-buffer read path ───────────────────────────────


class _Part:
    def __init__(self, text):
        self.type = "input_text"
        self.text = text


class _UserMsg:
    def __init__(self, text):
        self.role = "user"
        self.content = [_Part(text)]


class _Chat:
    def __init__(self, text):
        self.buffer = [_UserMsg(text)]


class _RuntimeConfig:
    def __init__(self, text):
        self.chat = _Chat(text)


def _request(text, *, speech_stopped_at_s=100.0):
    return parrot_lane.GenerateResponseRequest(
        runtime_config=_RuntimeConfig(text),
        language_code=None,
        response=None,
        turn_id="turn_1",
        turn_revision=0,
        speech_stopped_at_s=speech_stopped_at_s,
    )


class ParrotGateTest(unittest.TestCase):
    def setUp(self):
        reflex_lane.turn_stats.calls = []
        self.lm_response_queue = Queue()
        # armed=True: these tests exercise the historical VOICE_PARROT=1
        # startup behaviour. The disarmed/live-toggle behaviour has its own
        # tests below.
        self.gate = parrot_lane.ParrotGate(
            None,
            queue_in=Queue(),
            queue_out=Queue(),
            setup_kwargs={"lm_response_queue": self.lm_response_queue, "armed": True},
        )

    def test_armed_turn_is_echoed_and_not_forwarded(self):
        req = _request("what time is it")
        out = list(self.gate.process(req))
        self.assertEqual(out, [])  # LM never sees this turn
        self.lm_response_queue.get_nowait()
        self.lm_response_queue.get_nowait()
        self.assertTrue(self.lm_response_queue.empty())

    def test_emitted_chunk_carries_exact_transcript(self):
        req = _request("open the pod bay doors")
        list(self.gate.process(req))
        chunk = self.lm_response_queue.get_nowait()
        self.assertIsInstance(chunk, reflex_lane.LLMResponseChunk)
        self.assertEqual(chunk.text, "open the pod bay doors")

    def test_route_is_parrot(self):
        req = _request("hello")
        list(self.gate.process(req))
        self.assertIn(("set_route", "parrot"), reflex_lane.turn_stats.calls)

    def test_non_request_message_is_passthrough(self):
        sentinel = object()
        out = list(self.gate.process(sentinel))
        self.assertEqual(out, [sentinel])
        self.assertTrue(self.lm_response_queue.empty())

    def test_followup_generation_is_passthrough(self):
        # speech_stopped_at_s=None marks a tool-call follow-up; must reach the LM.
        req = _request("open the pod bay doors", speech_stopped_at_s=None)
        out = list(self.gate.process(req))
        self.assertEqual(out, [req])
        self.assertTrue(self.lm_response_queue.empty())

    def test_empty_transcript_forwards_to_lm(self):
        req = _request("   ")
        out = list(self.gate.process(req))
        self.assertEqual(out, [req])
        self.assertTrue(self.lm_response_queue.empty())
        self.assertEqual(reflex_lane.turn_stats.calls, [])


class ParrotGateLiveToggleTest(unittest.TestCase):
    """B6 UI slice: parrot mode is armed/disarmed at runtime, not only at
    process startup via VOICE_PARROT."""

    def setUp(self):
        reflex_lane.turn_stats.calls = []
        self.lm_response_queue = Queue()

    def _gate(self, **setup_kwargs):
        return parrot_lane.ParrotGate(
            None,
            queue_in=Queue(),
            queue_out=Queue(),
            setup_kwargs={"lm_response_queue": self.lm_response_queue, **setup_kwargs},
        )

    def test_default_is_disarmed(self):
        # No `armed` kwarg at all: an always-inserted gate (R1) must default
        # to a no-op passthrough, matching an unset VOICE_PARROT startup.
        gate = self._gate()
        self.assertFalse(gate.armed)

    def test_disarmed_turn_passes_through_untouched(self):
        gate = self._gate(armed=False)
        req = _request("what time is it")
        out = list(gate.process(req))
        self.assertEqual(out, [req])
        self.assertTrue(self.lm_response_queue.empty())

    def test_toggling_armed_flag_live_changes_behaviour(self):
        # The gate object itself is the toggle target -- brain_control.py's
        # config_set flips `.armed` directly on the running instance, no
        # re-construction and no restart.
        gate = self._gate(armed=False)
        req = _request("what time is it")
        list(gate.process(req))
        self.assertTrue(self.lm_response_queue.empty())  # disarmed: no echo

        gate.armed = True
        list(gate.process(req))
        self.assertFalse(self.lm_response_queue.empty())  # now armed: echoed

    def test_reflex_present_logged_when_armed_turn_is_echoed(self):
        gate = self._gate(armed=True, reflex_present=True)
        with self.assertLogs(parrot_lane.logger, level="INFO") as cm:
            list(gate.process(_request("hello")))
        self.assertTrue(any("reflex lane present" in line for line in cm.output))


class SharedEmissionHelperTest(unittest.TestCase):
    """reflex_lane's extracted helper must still behave identically for reflex."""

    def setUp(self):
        reflex_lane.turn_stats.calls = []
        self.lm_response_queue = Queue()

    def test_reflex_gate_emission_unchanged_via_shared_helper(self):
        gate = reflex_lane.ReflexGate(
            None,
            queue_in=Queue(),
            queue_out=Queue(),
            setup_kwargs={"lm_response_queue": self.lm_response_queue},
        )
        req = _request("is the sun on?")

        def fake_execute(name, kwargs):
            return "The sun's state is below_horizon."

        reflex_lane.voice_tools.execute = fake_execute
        out = list(gate.process(req))
        self.assertEqual(out, [])
        chunk = self.lm_response_queue.get_nowait()
        eor = self.lm_response_queue.get_nowait()
        self.assertTrue(self.lm_response_queue.empty())
        self.assertIsInstance(chunk, reflex_lane.LLMResponseChunk)
        self.assertIsInstance(eor, reflex_lane.EndOfResponse)
        self.assertEqual(chunk.text, "The sun's state is below_horizon.")
        self.assertIsNone(chunk.speech_stopped_at_s)
        self.assertEqual(chunk.turn_id, req.turn_id)
        self.assertEqual(reflex_lane.turn_stats.calls[0], ("on_llm_chunk", 100.0))
        self.assertIn(("set_route", "reflex"), reflex_lane.turn_stats.calls)


if __name__ == "__main__":
    unittest.main()
