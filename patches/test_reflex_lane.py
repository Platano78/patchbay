"""Unit tests for reflex_lane.py (Slice 2 pre-LLM reflex lane).

Run from repo root: python3 -m unittest patches.test_reflex_lane -v

The real ``speech_to_speech`` package is not installed in this repo, so its
handful of pipeline symbols that reflex_lane imports are stubbed into
``sys.modules`` before the import below. The stubs are hermetic: the tests
exercise reflex_lane's own logic (trigger table, fail-open, short-circuit
emission shape) with mocked ``voice_tools.execute`` and plain ``queue.Queue``
objects -- exactly the seams the gate specifies.
"""

from __future__ import annotations

import sys
import types
import unittest
from queue import Queue
from typing import Generic, TypeVar

# ── Stub the speech_to_speech surface reflex_lane imports ──────────────

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
    """Records the two turn_stats calls the gate makes, for assertions."""

    def __init__(self):
        self.calls = []

    def on_llm_chunk(self, speech_stopped_at_s):
        self.calls.append(("on_llm_chunk", speech_stopped_at_s))

    def set_route(self, route):
        self.calls.append(("set_route", route))


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
    vt = mod("speech_to_speech.voice_tools", execute=lambda name, kwargs: "")
    pkg.voice_tools = vt
    mod("speech_to_speech.baseHandler", BaseHandler=_StubBaseHandler)
    mod("speech_to_speech.pipeline")
    mod("speech_to_speech.pipeline.handler_types", LLMIn=object, LLMOut=object)
    mod(
        "speech_to_speech.pipeline.messages",
        GenerateResponseRequest=_GenerateResponseRequest,
        LLMResponseChunk=_LLMResponseChunk,
        EndOfResponse=_EndOfResponse,
    )
    mod("speech_to_speech.pipeline.queue_types", LMOutItem=object)
    mod("speech_to_speech.turn_stats", turn_stats=_TurnStatsRecorder())


_install_stubs()

from patches import reflex_lane  # noqa: E402


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
    return reflex_lane.GenerateResponseRequest(
        runtime_config=_RuntimeConfig(text),
        language_code=None,
        response=None,
        turn_id="turn_1",
        turn_revision=0,
        speech_stopped_at_s=speech_stopped_at_s,
    )


# ── Trigger table ─────────────────────────────────────────────────────

_SHOULD_MATCH = [
    "turn on the kitchen lights",
    "turn off the bedroom lamp",
    "turn the hallway lights off",
    "toggle the office fan",
    "set the kitchen lights brightness to 40",
    "set the desk lamp brightness to 100 percent",
    "make the living room lights blue",
    "set the desk lamp to red",
    "change the porch lights to warm white",
    "activate the movie scene",
    "run the goodnight scene",
    "is the sun on?",
    "is the porch light on",
    "what's the temperature in the bedroom?",
    "what is the temperature at the office",
    "Turn On The Kitchen Lights.",  # case + trailing punctuation
]

_SHOULD_NOT_MATCH = [
    # explicitly required by the gate
    "turn on the charm",  # no device noun -> LLM
    "is it on",  # vague entity
    "turn it off and play music",  # compound bail
    "what's the temperature outside like today, roughly",  # not the in/at temp shape
    # conversational / adversarial
    "tell me a joke",
    "what games can I play",
    "open spotify",
    "toggle it",  # vague entity
    "set the mood to happy",  # not a color, no device noun
    "activate the plan",  # no literal 'scene'
    "turn everything off",  # vague entity
    "turn on the lights and start the music",  # compound bail
    "",
    "   ",
]


class TriggerTableTest(unittest.TestCase):
    def test_should_match(self):
        for utterance in _SHOULD_MATCH:
            with self.subTest(utterance=utterance):
                self.assertTrue(reflex_lane.is_reflex_candidate(utterance))

    def test_should_not_match(self):
        for utterance in _SHOULD_NOT_MATCH:
            with self.subTest(utterance=utterance):
                self.assertFalse(reflex_lane.is_reflex_candidate(utterance))


# ── Gate behaviour (process) ──────────────────────────────────────────


class GateProcessTest(unittest.TestCase):
    def setUp(self):
        reflex_lane.turn_stats.calls = []
        self._orig_execute = reflex_lane.voice_tools.execute
        self.lm_response_queue = Queue()
        self.gate = reflex_lane.ReflexGate(
            None,
            queue_in=Queue(),
            queue_out=Queue(),
            setup_kwargs={"lm_response_queue": self.lm_response_queue},
        )

    def tearDown(self):
        reflex_lane.voice_tools.execute = self._orig_execute

    def _set_execute(self, result):
        self.calls = []

        def fake(name, kwargs):
            self.calls.append((name, kwargs))
            return result

        reflex_lane.voice_tools.execute = fake

    def test_fail_open_on_each_sentinel(self):
        sentinels = [
            "Home Assistant isn't set up yet.",
            "That tool isn't available.",
            "I can turn things on or off, set brightness or color, or run scenes.",
        ]
        for sentinel in sentinels:
            with self.subTest(sentinel=sentinel):
                self._set_execute(sentinel)
                req = _request("turn on the kitchen lights")
                out = list(self.gate.process(req))
                # forwarded unchanged, nothing injected downstream
                self.assertEqual(out, [req])
                self.assertTrue(self.lm_response_queue.empty())
                self.assertEqual(len(self.calls), 1)  # HA was tried, then bailed

    def test_short_circuit_emission_shape(self):
        self._set_execute("The sun's state is below_horizon.")
        req = _request("is the sun on?")
        out = list(self.gate.process(req))
        # LM never sees this turn
        self.assertEqual(out, [])
        # exactly a chunk then an end-of-response on the LM output queue
        chunk = self.lm_response_queue.get_nowait()
        eor = self.lm_response_queue.get_nowait()
        self.assertTrue(self.lm_response_queue.empty())
        self.assertIsInstance(chunk, reflex_lane.LLMResponseChunk)
        self.assertIsInstance(eor, reflex_lane.EndOfResponse)
        self.assertEqual(chunk.text, "The sun's state is below_horizon.")
        # None so LMOutputProcessor.on_llm_chunk won't flush/reset the route
        self.assertIsNone(chunk.speech_stopped_at_s)
        self.assertEqual(chunk.turn_id, req.turn_id)
        self.assertEqual(eor.turn_id, req.turn_id)
        self.assertEqual(chunk.turn_revision, req.turn_revision)
        # route stamped reflex, turn started before the chunk was queued
        self.assertEqual(reflex_lane.turn_stats.calls[0], ("on_llm_chunk", 100.0))
        self.assertIn(("set_route", "reflex"), reflex_lane.turn_stats.calls)

    def test_non_candidate_is_pure_passthrough(self):
        self._set_execute("should not be called")
        req = _request("tell me a joke")
        out = list(self.gate.process(req))
        self.assertEqual(out, [req])
        self.assertTrue(self.lm_response_queue.empty())
        self.assertEqual(self.calls, [])  # execute never invoked
        self.assertEqual(reflex_lane.turn_stats.calls, [])

    def test_followup_generation_is_passthrough(self):
        # A tool-call follow-up carries speech_stopped_at_s=None and must reach the LM
        # even though its text could look actionable.
        self._set_execute("should not be called")
        req = _request("turn on the kitchen lights", speech_stopped_at_s=None)
        out = list(self.gate.process(req))
        self.assertEqual(out, [req])
        self.assertEqual(self.calls, [])

    def test_hit_fails_open_when_no_response_queue(self):
        # A gate wired without lm_response_queue must NOT swallow a real hit: it has
        # nowhere to answer, so it forwards the request to the LM (fail open).
        gate = reflex_lane.ReflexGate(
            None,
            queue_in=Queue(),
            queue_out=Queue(),
            setup_kwargs={"lm_response_queue": None},
        )
        self._set_execute("The sun's state is below_horizon.")
        req = _request("is the sun on?")
        out = list(gate.process(req))
        self.assertEqual(out, [req])  # forwarded downstream
        self.assertEqual(len(self.calls), 1)  # HA was tried

    def test_non_request_message_is_passthrough(self):
        # Control/sentinel items (anything not a GenerateResponseRequest) pass through.
        self._set_execute("should not be called")
        sentinel = object()
        out = list(self.gate.process(sentinel))
        self.assertEqual(out, [sentinel])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
