"""Unit tests for the stream watchdog + client-side token cap added to
``chat_completions_language_model.py``.

Run from repo root: python3 -m pytest patches/test_stream_watchdog.py -v

The real ``speech_to_speech`` package is not installed in this repo, so the
handful of symbols the module under test imports from it are stubbed into
``sys.modules`` before the import below -- same hermetic pattern as
``test_brain_control.py`` / ``test_reflex_lane.py``. ``speech_to_speech.think_filter``
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

    mod("speech_to_speech")
    mod("speech_to_speech.LLM")
    mod(
        "speech_to_speech.LLM.base_openai_compatible_language_model",
        AssistantMessage=AssistantMessage,
        BaseOpenAICompatibleHandler=type("_Stub", (), {}),
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

    sys.modules["speech_to_speech.phone_context"] = real_phone_context
    sys.modules["speech_to_speech.think_filter"] = real_think_filter
    mod("speech_to_speech.utils")
    mod("speech_to_speech.utils.utils", _generate_id=lambda prefix: f"{prefix}_1")
    mod("speech_to_speech.voice_rules", apply_system_rules=lambda messages: messages)


_install_stubs()

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
