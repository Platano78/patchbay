"""Unit tests for the stream watchdog + client-side token cap added to
``chat_completions_language_model.py``.

Run from repo root: python3 -m pytest patches/test_stream_watchdog.py -v

The real ``speech_to_speech`` package is not installed in this repo, so the
handful of symbols the module under test imports from it are stubbed into
``sys.modules`` before the import below -- same hermetic pattern as
``test_brain_control.py`` / ``test_parrot_lane.py``. ``speech_to_speech.think_filter``
is aliased to the REAL ``patches.think_filter`` module (not a stub) since
``_iter_stream_events`` uses it for real filtering work that these tests
exercise. ``openai`` and ``httpx`` are real, installed third-party packages
and are imported unstubbed.

``_iter_stream_events`` and ``_request`` are driven directly on a bare
instance built via ``object.__new__`` (no ``__init__``/base-class
construction), with only the attributes each method touches set by hand --
this still runs the real, unbound methods (including their calls to
``self._tool_calls_from_accum``, resolved via the class), not a
reimplementation of them.
"""

from __future__ import annotations

import sys
import types

import pytest

# ── Stub the speech_to_speech surface chat_completions_language_model.py imports ──


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

    class _KwObj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class AssistantMessage(_KwObj):
        pass

    class TextDelta(_KwObj):
        pass

    class ToolCall(_KwObj):
        pass

    class Usage(_KwObj):
        pass

    class ProviderEvent:
        pass

    class _StubBase:
        """The slice of BaseOpenAICompatibleHandler the barge-in code leans on:
        the stale check (same body as upstream's) and two generator seams."""

        def _generation_is_stale(self, gen):
            scope = getattr(self, "cancel_scope", None)
            return gen is not None and scope is not None and scope.is_stale(gen)

        def _generate(self, active_chat, original_chat, turn, optional_kwargs):
            self.gen_seen_by_base = self._turn_gen
            return iter(())

        def _consume_streaming(self, events, state, turn):
            yield from events

    mod("speech_to_speech")
    mod("speech_to_speech.LLM")
    mod(
        "speech_to_speech.LLM.base_openai_compatible_language_model",
        AssistantMessage=AssistantMessage,
        BaseOpenAICompatibleHandler=_StubBase,
        ProviderEvent=ProviderEvent,
        TextDelta=TextDelta,
        ToolCall=ToolCall,
        Usage=Usage,
    )
    mod("speech_to_speech.LLM.chat", Chat=object)
    mod("speech_to_speech.LLM.compaction_prompt", CompactGenerateFn=object)
    # Aliased to the REAL modules (not stubbed), same pattern as
    # test_phone_context.py / test_brain_control.py: these are shared
    # sys.modules entries across test files in the same pytest run, so
    # overwriting an attribute here would corrupt another file's module.
    from patches import phone_context as real_phone_context
    from patches import think_filter as real_think_filter
    from patches import voice_rules as real_voice_rules
    from patches import voice_affect as real_voice_affect

    sys.modules["speech_to_speech.phone_context"] = real_phone_context
    sys.modules["speech_to_speech.think_filter"] = real_think_filter
    sys.modules["speech_to_speech.voice_affect"] = real_voice_affect
    mod("speech_to_speech.utils")
    mod(
        "speech_to_speech.utils.utils",
        _generate_id=lambda prefix: f"{prefix}_1",
        is_out_of_band=lambda response: bool(getattr(response, "out_of_band", False)),
    )
    sys.modules["speech_to_speech.voice_rules"] = real_voice_rules


_install_stubs()

from datetime import datetime  # noqa: E402

import httpx  # noqa: E402

from patches import chat_completions_language_model as ccm  # noqa: E402
from patches.chat_completions_language_model import (  # noqa: E402
    ChatCompletionsApiModelHandler,
    _parse_max_tokens,
    _parse_stream_max_s,
)


def _make_handler(**attrs):
    """A bare instance of the real handler class, skipping __init__/base
    construction, with only the attributes the method under test touches."""
    obj = object.__new__(ChatCompletionsApiModelHandler)
    obj.compactor = None  # setup() always sets it; bare instances skip setup()
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def _fake_chunk(text=None):
    if text is None:
        return types.SimpleNamespace(usage=None, choices=[])
    delta = types.SimpleNamespace(content=text, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta)
    return types.SimpleNamespace(usage=None, choices=[choice])


class _FakeCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return "fake-response"


class _FakeClient:
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions())


# ── VOICE_STREAM_MAX_S parsing ──────────────────────────────────────────────


def test_stream_max_s_default_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_STREAM_MAX_S", raising=False)
    assert _parse_stream_max_s() == 120.0


def test_stream_max_s_off_disables(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "off")
    assert _parse_stream_max_s() is None


def test_stream_max_s_off_case_insensitive(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "OFF")
    assert _parse_stream_max_s() is None


def test_stream_max_s_zero_disables(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "0")
    assert _parse_stream_max_s() is None


def test_stream_max_s_negative_disables(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "-5")
    assert _parse_stream_max_s() is None


def test_stream_max_s_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "not-a-number")
    assert _parse_stream_max_s() == 120.0


def test_stream_max_s_blank_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "   ")
    assert _parse_stream_max_s() == 120.0


def test_stream_max_s_custom_value(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "45.5")
    assert _parse_stream_max_s() == 45.5


# ── VOICE_MAX_TOKENS parsing ────────────────────────────────────────────────


def test_max_tokens_default_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_MAX_TOKENS", raising=False)
    assert _parse_max_tokens() == 1024


def test_max_tokens_off_disables(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "off")
    assert _parse_max_tokens() is None


def test_max_tokens_off_case_insensitive(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "Off")
    assert _parse_max_tokens() is None


def test_max_tokens_zero_disables(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "0")
    assert _parse_max_tokens() is None


def test_max_tokens_negative_disables(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "-1")
    assert _parse_max_tokens() is None


def test_max_tokens_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "lots")
    assert _parse_max_tokens() == 1024


def test_max_tokens_blank_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "")
    assert _parse_max_tokens() == 1024


def test_max_tokens_custom_value(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "256")
    assert _parse_max_tokens() == 256


# ── _iter_stream_events: total-duration watchdog ────────────────────────────


def test_watchdog_raises_readtimeout_once_deadline_exceeded(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "60")
    # First call computes the deadline (1000 + 60 = 1060); the next two
    # per-chunk checks (1001, 1002) stay under it; the third (1200) is well
    # past it and must trip the watchdog on that chunk's arrival.
    times = iter([1000.0, 1001.0, 1002.0, 1200.0])
    monkeypatch.setattr(ccm.time, "monotonic", lambda: next(times))

    handler = _make_handler()
    chunks = [_fake_chunk("a"), _fake_chunk("b"), _fake_chunk("c")]

    with pytest.raises(httpx.ReadTimeout, match="60s total-duration watchdog"):
        list(handler._iter_stream_events(iter(chunks)))


def test_watchdog_does_not_raise_under_limit(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "60")
    # Deadline = 1000 + 60 = 1060; every subsequent check stays under it.
    times = iter([1000.0, 1001.0, 1002.0, 1003.0])
    monkeypatch.setattr(ccm.time, "monotonic", lambda: next(times))

    handler = _make_handler()
    chunks = [_fake_chunk("hello "), _fake_chunk("world")]

    events = list(handler._iter_stream_events(iter(chunks)))

    text_deltas = [e for e in events if type(e).__name__ == "TextDelta"]
    assert "".join(e.text for e in text_deltas) == "hello world"


def test_watchdog_disabled_never_raises(monkeypatch):
    monkeypatch.setenv("VOICE_STREAM_MAX_S", "off")
    # Wildly increasing times would trip an enabled watchdog immediately;
    # disabled, the deadline is None and the check is skipped outright.
    times = iter([1000.0, 5000.0, 9000.0, 50000.0])
    monkeypatch.setattr(ccm.time, "monotonic", lambda: next(times))

    handler = _make_handler()
    chunks = [_fake_chunk("still "), _fake_chunk("here")]

    events = list(handler._iter_stream_events(iter(chunks)))

    text_deltas = [e for e in events if type(e).__name__ == "TextDelta"]
    assert "".join(e.text for e in text_deltas) == "still here"


# ── _request: client-side token cap ─────────────────────────────────────────


def _handler_for_request(stream=False):
    return _make_handler(
        stream=stream,
        client=_FakeClient(),
        model_name="test-model",
        _extra_body={},
        request_timeout=30,
    )


def test_request_passes_max_tokens_when_enabled_default(monkeypatch):
    monkeypatch.delenv("VOICE_MAX_TOKENS", raising=False)
    handler = _handler_for_request()

    handler._request([{"role": "user", "content": "hi"}], {})

    kwargs = handler.client.chat.completions.calls[0]
    assert kwargs["max_tokens"] == 1024


def test_request_passes_max_tokens_when_enabled_custom(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "256")
    handler = _handler_for_request()

    handler._request([{"role": "user", "content": "hi"}], {})

    kwargs = handler.client.chat.completions.calls[0]
    assert kwargs["max_tokens"] == 256


def test_request_omits_max_tokens_when_disabled(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "off")
    handler = _handler_for_request()

    handler._request([{"role": "user", "content": "hi"}], {})

    kwargs = handler.client.chat.completions.calls[0]
    assert "max_tokens" not in kwargs


def test_request_streaming_still_gets_max_tokens_and_stream_options(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_TOKENS", "512")
    handler = _handler_for_request(stream=True)

    handler._request([{"role": "user", "content": "hi"}], {})

    kwargs = handler.client.chat.completions.calls[0]
    assert kwargs["max_tokens"] == 512
    assert kwargs["stream_options"] == {"include_usage": True}


# ── barge-in cancel (websocket mode): stop reading, remember what was said ──


class _Scope:
    """CancelScope's contract here: is_stale(gen) is `gen != current generation`."""

    def __init__(self, generation=1):
        self.generation = generation

    def is_stale(self, gen):
        return gen != self.generation

    def cancel(self):
        self.generation += 1


class _FakeChat:
    SOFT_LIMIT = 3

    def __init__(self):
        self.items = []
        self.trim_compactors = []

    def add_item(self, item):
        self.items.append(item)

    def trim_if_needed(self, compactor=None):
        self.trim_compactors.append(compactor)
        del self.items[: max(0, len(self.items) - self.SOFT_LIMIT)]


def _turn(chat, gen=1, out_of_band=False):
    return types.SimpleNamespace(
        gen=gen,
        response=types.SimpleNamespace(out_of_band=out_of_band),
        runtime_config=types.SimpleNamespace(chat=chat),
    )


def test_stream_stops_reading_the_moment_the_generation_is_stale():
    scope = _Scope(generation=1)
    handler = _make_handler(cancel_scope=scope, _turn_gen=1)
    consumed = []

    def stream():
        for i in range(100):
            consumed.append(i)
            if i == 2:
                scope.cancel()  # the user barges in while the model is mid-reply
            yield _fake_chunk("word ")

    events = list(handler._iter_stream_events(stream()))

    assert consumed == [0, 1, 2]  # the chunk that arrived with the cancel is the last one read
    assert not any(isinstance(e, ccm.AssistantMessage) for e in events)  # no end-of-stream write-back


def test_stream_stops_even_when_no_event_would_be_yielded():
    """A trickle of empty-delta chunks never reaches the base class's per-event stale check."""
    scope = _Scope(generation=2)
    handler = _make_handler(cancel_scope=scope, _turn_gen=1)  # this turn started under generation 1
    consumed = []

    def trickle():
        for i in range(1000):
            consumed.append(i)
            yield _fake_chunk(None)

    assert list(handler._iter_stream_events(trickle())) == []
    assert consumed == [0]


def test_stream_runs_to_the_end_when_nothing_cancelled():
    handler = _make_handler(cancel_scope=_Scope(generation=1), _turn_gen=1)

    events = list(handler._iter_stream_events(iter([_fake_chunk("hello "), _fake_chunk("there")])))

    assert any(isinstance(e, ccm.AssistantMessage) for e in events)


def test_stream_ignores_cancel_state_without_a_scope():
    """VOICE_BARGE_CANCEL=off: no scope wired, so a stale-looking generation changes nothing."""
    handler = _make_handler(cancel_scope=None, _turn_gen=0)

    events = list(handler._iter_stream_events(iter([_fake_chunk("hello "), _fake_chunk("there")])))

    assert any(isinstance(e, ccm.AssistantMessage) for e in events)


def test_generate_hands_the_turn_generation_to_the_stream_reader():
    handler = _make_handler(cancel_scope=_Scope())

    list(handler._generate(None, None, _turn(_FakeChat(), gen=7), {}))

    assert handler.gen_seen_by_base == 7


def test_cancelled_reply_text_so_far_goes_into_history():
    scope = _Scope(generation=2)  # cancelled: the turn began under generation 1
    handler = _make_handler(cancel_scope=scope)
    chat = _FakeChat()
    state = types.SimpleNamespace(clean_text="  Once upon a time there was a dragon who ")

    list(handler._consume_streaming(iter(()), state, _turn(chat, gen=1)))

    assert len(chat.items) == 1
    item = chat.items[0]
    assert item.role == "assistant"
    assert item.content[0].text == "Once upon a time there was a dragon who"


def test_history_note_is_written_even_if_the_consumer_abandons_the_stream():
    scope = _Scope(generation=2)
    handler = _make_handler(cancel_scope=scope)
    chat = _FakeChat()
    state = types.SimpleNamespace(clean_text="partial answer")

    gen = handler._consume_streaming(iter([1, 2, 3]), state, _turn(chat, gen=1))
    next(gen)
    gen.close()

    assert [i.content[0].text for i in chat.items] == ["partial answer"]


def test_finished_reply_is_not_recorded_by_the_cancel_path():
    handler = _make_handler(cancel_scope=_Scope(generation=1))
    chat = _FakeChat()

    list(handler._consume_streaming(iter(()), types.SimpleNamespace(clean_text="all of it"), _turn(chat, gen=1)))

    assert chat.items == []  # the base class writes completed replies back itself


def test_cancelled_reply_with_no_text_or_out_of_band_leaves_history_alone():
    handler = _make_handler(cancel_scope=_Scope(generation=2))
    chat = _FakeChat()

    list(handler._consume_streaming(iter(()), types.SimpleNamespace(clean_text="   "), _turn(chat, gen=1)))
    list(handler._consume_streaming(iter(()), types.SimpleNamespace(clean_text="words"), _turn(chat, gen=1, out_of_band=True)))

    assert chat.items == []


def test_no_scope_means_history_is_never_touched():
    handler = _make_handler(cancel_scope=None)
    chat = _FakeChat()

    list(handler._consume_streaming(iter(()), types.SimpleNamespace(clean_text="words"), _turn(chat, gen=0)))

    assert chat.items == []


def test_cancelled_reply_write_trims_history_over_the_soft_limit():
    """Chat.add_item leaves the soft size limit to the caller, as the base write-back does."""
    marker = object()
    handler = _make_handler(cancel_scope=_Scope(generation=2), compactor=marker)
    chat = _FakeChat()
    chat.items = ["u1", "a1", "u2"]  # at the limit before the write
    state = types.SimpleNamespace(clean_text="the cancelled reply")

    list(handler._consume_streaming(iter(()), state, _turn(chat, gen=1)))

    assert len(chat.items) == _FakeChat.SOFT_LIMIT  # 4 after the write, trimmed back
    assert chat.items[-1].content[0].text == "the cancelled reply"
    assert chat.trim_compactors == [marker]


# ── _serialize: the date anchor reaches the outgoing request ───────────────


class _ClockedDatetime(datetime):
    current = datetime(2026, 10, 24, 9, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current


class _SerializeChat:
    def to_transformers_chat(self):
        return [{"role": "system", "content": "Persona."}, {"role": "user", "content": "what year is it"}]


def test_serialize_puts_date_line_in_the_outgoing_system_message(monkeypatch):
    from patches import voice_rules

    monkeypatch.setattr(voice_rules, "datetime", _ClockedDatetime)
    # `enabled` is bound at def time: patch the default, not the DATE_ANCHOR global.
    monkeypatch.setattr(voice_rules.apply_date_anchor, "__defaults__", (None, True))
    handler = _handler_for_request()

    api_input = handler._serialize(_SerializeChat())
    handler._request(api_input, {})

    sent = handler.client.chat.completions.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[0]["content"].startswith("Persona.")
    assert "Today's date is Saturday, October 24, 2026." in sent[0]["content"]
    assert voice_rules.DEFAULT_RULES in sent[0]["content"]


def test_serialize_recomputes_the_date_every_request(monkeypatch):
    from patches import voice_rules

    monkeypatch.setattr(voice_rules, "datetime", _ClockedDatetime)
    monkeypatch.setattr(voice_rules.apply_date_anchor, "__defaults__", (None, True))
    handler = _handler_for_request()
    chat = _SerializeChat()

    monkeypatch.setattr(_ClockedDatetime, "current", datetime(2026, 10, 24, 23, 59))
    first = handler._serialize(chat)[0]["content"]
    monkeypatch.setattr(_ClockedDatetime, "current", datetime(2026, 10, 25, 0, 1))
    second = handler._serialize(chat)[0]["content"]

    assert "Saturday, October 24, 2026" in first
    assert "Sunday, October 25, 2026" in second
    assert "October 24" not in second
