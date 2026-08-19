"""Unit tests for brain_control.py's voice-delete broadcast (fix #2).

Run from repo root: python3 -m pytest patches/test_brain_control.py -v

The real ``speech_to_speech`` package is not installed in this repo, so the
handful of symbols brain_control.py imports from it are stubbed into
``sys.modules`` before the import below -- same hermetic pattern as
``test_reflex_lane.py``. ``speech_to_speech.voice_clone`` is aliased to the
REAL ``patches.voice_clone`` module (not a stub) so these tests exercise the
actual deployed logic, not a mock of it.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Event

import numpy as np
import pytest

# ── Stub the speech_to_speech surface brain_control.py imports ─────────


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

    from patches import voice_clone as real_voice_clone

    pkg = mod("speech_to_speech")
    mod("speech_to_speech.LLM")
    mod("speech_to_speech.LLM.base_openai_compatible_language_model", BaseOpenAICompatibleHandler=type("_Stub", (), {}))
    vt = mod("speech_to_speech.voice_tools", get_tool_defs=lambda: [], execute=lambda name, kwargs: "")
    pkg.voice_tools = vt
    sys.modules["speech_to_speech.voice_clone"] = real_voice_clone
    pkg.voice_clone = real_voice_clone

    # Minimal surface needed to construct a REAL RemoteSpeechTTSHandler
    # (rather than a fake) so the speech_style tests below can assert on the
    # actual serialized wire payload -- same hermetic pattern as
    # test_remote_speech_tts_handler.py's own `_install_stubs`.
    from typing import Generic, TypeVar

    InT = TypeVar("InT")
    OutT = TypeVar("OutT")

    class _BaseHandler(Generic[InT, OutT]):
        def __init__(self, stop_event, queue_in, queue_out, setup_args=(), setup_kwargs=None):
            self.stop_event = stop_event
            self.queue_in = queue_in
            self.queue_out = queue_out
            self.pipeline_index = None
            self.setup(*setup_args, **(setup_kwargs or {}))

        def setup(self, *a, **kw):
            pass

        @property
        def min_time_to_debug(self):
            return 0.001

    class _EndOfResponse:
        def __init__(self, turn_id=None, turn_revision=None):
            self.turn_id = turn_id
            self.turn_revision = turn_revision

    class _FakePocketTTSHandler(_BaseHandler):
        """Never actually exercised by these tests (the remote is always
        reachable), but `setup()`'s boot-time fallback validation needs it
        to be constructible."""

        def setup(self, should_listen, cancel_scope=None, speculative_turns=None, **kw):
            pass

        def process(self, tts_input):
            yield  # pragma: no cover -- fallback path not exercised here

    mod("speech_to_speech.baseHandler", BaseHandler=_BaseHandler)
    mod("speech_to_speech.pipeline")
    mod("speech_to_speech.pipeline.cancel_scope", CancelScope=type("_CancelScope", (), {}))
    mod("speech_to_speech.pipeline.handler_types", TTSIn=object, TTSOut=object)
    mod("speech_to_speech.pipeline.messages", AUDIO_RESPONSE_DONE=object(), EndOfResponse=_EndOfResponse)
    mod("speech_to_speech.pipeline.speculative_turns", SpeculativeTurnTracker=object)
    mod("speech_to_speech.TTS")
    mod("speech_to_speech.TTS.pocket_tts_handler", PocketTTSHandler=_FakePocketTTSHandler)
    mod("pocket_tts")


_install_stubs()

# `_set_speech_style` lazily imports DEFAULT_INSTRUCTIONS from
# `speech_to_speech.TTS.remote_speech_tts_handler` -- alias that dotted path
# to the REAL module (same pattern as voice_clone above) so the speech_style
# tests exercise the actual constant, not a stub.
import patches.remote_speech_tts_handler as _real_remote_speech_tts_handler  # noqa: E402

sys.modules["speech_to_speech.TTS.remote_speech_tts_handler"] = _real_remote_speech_tts_handler

from patches import brain_control  # noqa: E402

PREDEFINED_UNAVAILABLE_NOTE = "pocket_tts not installed -- _predefined_voices() returns [] safely"


@pytest.fixture(autouse=True)
def _default_voice_choice_env(tmp_path, monkeypatch):
    """A successful voice switch now persists a sidecar file (see `_set_voice`).
    Scope its default location into `tmp_path` for every test -- so a test
    that doesn't explicitly set `VOICE_CHOICE_FILE` (most of the pre-existing
    voice tests, which predate persistence) can never write to the real
    ~/speech-to-speech/voice_choice.json on the machine running the suite.
    Tests that need to exercise the real default resolution override this
    via `monkeypatch.delenv`/`monkeypatch.setenv` themselves.
    """
    monkeypatch.setenv("VOICE_CHOICE_FILE", str(tmp_path / "voice_choice.json"))


# ── fakes ────────────────────────────────────────────────────────────────


class _FakeWakewordGate:
    enabled = False
    phrase = "hey_jarvis"
    # Mirrors the real gate's contract: `model_name` is the stripped display form and
    # is always one of `available_models()`, even when the raw arg is a custom path.
    model_name = "my_wake"
    _model_arg = "/opt/models/my_wake_v1.0.onnx"

    def state(self):
        return "off"

    def available_models(self):
        return ["hey_jarvis", "my_wake"]


class _FakeStreamer:
    def __init__(self):
        self.broadcasts: list[dict] = []
        self.wakeword_gate = _FakeWakewordGate()

    def broadcast_json(self, payload):
        self.broadcasts.append(payload)


class _FakeTTSModel:
    """Stands in for pocket_tts's model: `_set_voice` feature-detects voice
    switching by looking for `.model.get_state_for_audio_prompt` (same
    signal the real pocket handler exposes), so a fake standing in for a
    voice-capable backend needs this to reach the code under test at all."""

    def get_state_for_audio_prompt(self, source, truncate=False):
        return f"state-for:{source}"


class _FakeTTSHandler:
    def __init__(self, voice=None):
        self.voice = voice
        self.voice_state = None
        self.model = _FakeTTSModel()


class _FakeRuntimeConfig:
    def __init__(self, instructions=None):
        self.session = types.SimpleNamespace(instructions=instructions, tools=None)
        self.chat = types.SimpleNamespace(reset=lambda: None)


def _make_brain_control(tmp_path, runtime_config=None, **kwargs):
    brains_path = tmp_path / "brains.json"
    brains_path.write_text("{}")
    return brain_control.BrainControl(
        llm_handler=types.SimpleNamespace(model_name="test-model"),
        runtime_config=runtime_config or _FakeRuntimeConfig(),
        brains_path=str(brains_path),
        **kwargs,
    )


# ── wake-word block: reported model must be selectable in the dropdown ──


def test_wake_word_state_model_is_one_of_the_offered_models(tmp_path):
    """The settings panel marks the active entry by comparing `model` to each of
    `models`. Reporting the raw VOICE_WAKE_WORD_MODEL (a path, for a custom model)
    matched nothing, so the dropdown silently showed the FIRST entry as active and
    misreported the live wake phrase."""
    bc = _make_brain_control(tmp_path, streamer=_FakeStreamer())

    wake = bc._wake_word_state()

    assert wake["model"] in wake["models"]
    assert wake["model"] == "my_wake"


def test_wake_word_state_none_without_a_streamer(tmp_path):
    assert _make_brain_control(tmp_path)._wake_word_state() is None


# ── voice_delete broadcast (fix #2) ─────────────────────────────────────


def test_voice_delete_broadcasts_config_state_on_success(tmp_path, monkeypatch):
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "my_voice.safetensors").write_bytes(b"fake state")
    monkeypatch.setenv("VOICE_CLONE_DIR", str(voices_dir))

    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="other_voice"), streamer=streamer)

    ok, error = bc._voice_delete("my_voice")

    assert (ok, error) == (True, "")
    assert len(streamer.broadcasts) == 1
    assert streamer.broadcasts[0]["type"] == "config_state"
    assert "my_voice" not in streamer.broadcasts[0]["custom_voices"]


def test_voice_delete_no_broadcast_on_active_voice_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CLONE_DIR", str(tmp_path / "voices"))
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="active_voice"), streamer=streamer)

    ok, error = bc._voice_delete("active_voice")

    assert ok is False
    assert "switch to another voice" in error
    assert streamer.broadcasts == []


def test_voice_delete_no_broadcast_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CLONE_DIR", str(tmp_path / "voices"))
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="other_voice"), streamer=streamer)

    ok, error = bc._voice_delete("nonexistent")

    assert ok is False
    assert streamer.broadcasts == []


def test_voice_delete_none_streamer_is_safe(tmp_path, monkeypatch):
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "my_voice.safetensors").write_bytes(b"fake state")
    monkeypatch.setenv("VOICE_CLONE_DIR", str(voices_dir))

    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="other_voice"), streamer=None)

    # Must not raise despite streamer=None.
    ok, error = bc._voice_delete("my_voice")

    assert (ok, error) == (True, "")


def test_voice_delete_logs_request_and_success(tmp_path, monkeypatch, caplog):
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "my_voice.safetensors").write_bytes(b"fake state")
    monkeypatch.setenv("VOICE_CLONE_DIR", str(voices_dir))

    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="other_voice"), streamer=_FakeStreamer())

    with caplog.at_level(logging.INFO, logger=brain_control.__name__):
        ok, _ = bc._voice_delete("my_voice")

    assert ok is True
    assert "voice delete requested for my_voice" in caplog.text
    assert "voice delete succeeded for my_voice" in caplog.text


def test_voice_delete_logs_refusal_reason(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("VOICE_CLONE_DIR", str(tmp_path / "voices"))
    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="active_voice"), streamer=_FakeStreamer())

    with caplog.at_level(logging.INFO, logger=brain_control.__name__):
        ok, error = bc._voice_delete("active_voice")

    assert ok is False
    assert "voice delete requested for active_voice" in caplog.text
    assert f"voice delete refused for active_voice: {error}" in caplog.text


def test_voice_delete_unavailable_without_tts_handler(tmp_path):
    bc = _make_brain_control(tmp_path, tts_handler=None, streamer=_FakeStreamer())

    ok, error = bc._voice_delete("anything")

    assert ok is False
    assert error == "voice switching unavailable"


# ── _set_voice path-traversal guard (ruling: same format gate as delete) ──


def test_set_voice_rejects_path_traversal_name_without_touching_voice_path(tmp_path, monkeypatch):
    """`config_set voice: "../../etc/passwd"` reaches `_set_voice` straight
    off the wire, unvalidated. It must be rejected by format BEFORE
    `voice_clone.voice_path()` ever joins it into a filesystem path -- proven
    here by spying on `voice_path` and asserting it's never called."""
    monkeypatch.setenv("VOICE_CLONE_DIR", str(tmp_path / "voices"))
    calls = []
    real_voice_path = brain_control.voice_clone.voice_path

    def spy(name):
        calls.append(name)
        return real_voice_path(name)

    monkeypatch.setattr(brain_control.voice_clone, "voice_path", spy)
    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="other_voice"), streamer=_FakeStreamer())

    ok, error = bc._set_voice("../../etc/passwd")

    assert ok is False
    assert error == "unknown voice: ../../etc/passwd"
    assert calls == []


# ── persona persistence ─────────────────────────────────────────────────
#
# The persona editor worked but was in-memory only: every restart reverted to
# the CLI `--init_chat_prompt` default, so the only durable way to change it
# was editing `ExecStart` over SSH. These cover the sidecar file that fixes it.

CLI_DEFAULT = "You are the shipped default."


def _persona_env(tmp_path, monkeypatch):
    path = tmp_path / "persona_dir" / "persona.json"
    monkeypatch.setenv("VOICE_PERSONA_FILE", str(path))
    return path


def _restart(tmp_path, **kwargs):
    """Rebuild BrainControl the way a service restart would: a fresh runtime
    config carrying only the CLI default, nothing else in memory."""
    return _make_brain_control(tmp_path, runtime_config=_FakeRuntimeConfig(instructions=CLI_DEFAULT), **kwargs)


def test_persona_survives_a_restart(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)

    ack = bc._config_set({"persona": "You are a laconic ship's computer."})

    assert ack["ok"] is True
    assert ack["persona"] == "You are a laconic ship's computer."
    assert ack["persona_persisted"] is True

    revived = _restart(tmp_path)
    assert revived.runtime_config.session.instructions == "You are a laconic ship's computer."
    # The CLI default is still the fallback target, not overwritten by the load.
    assert revived.default_persona == CLI_DEFAULT


def test_clearing_persona_restores_the_cli_default_and_deletes_the_file(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc._config_set({"persona": "custom"})
    assert path.is_file()

    ack = bc._config_set({"persona": ""})

    assert ack["ok"] is True
    assert ack["persona"] == CLI_DEFAULT
    assert ack["persona_persisted"] is False
    assert not path.exists()
    assert _restart(tmp_path).runtime_config.session.instructions == CLI_DEFAULT


def test_persisted_persona_takes_precedence_over_the_cli_default(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "persona": "persisted wins"}')

    state = _restart(tmp_path)._config_state()

    assert state["persona"] == "persisted wins"
    assert state["default_persona"] == CLI_DEFAULT
    assert state["persona_persisted"] is True


def test_missing_persona_file_falls_back_to_the_default(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)

    bc = _restart(tmp_path)

    assert bc.runtime_config.session.instructions == CLI_DEFAULT
    assert bc._config_state()["persona_persisted"] is False


def test_corrupt_persona_file_does_not_raise(tmp_path, monkeypatch, caplog):
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "persona": "truncated mid-writ')

    with caplog.at_level(logging.WARNING, logger=brain_control.__name__):
        bc = _restart(tmp_path)

    assert bc.runtime_config.session.instructions == CLI_DEFAULT
    assert "unreadable persona file" in caplog.text


def test_empty_and_non_object_persona_files_fall_back(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)

    for body in ("", "   ", "null", '"a bare string"', "[]", "{}", '{"persona": ""}'):
        path.write_text(body)
        assert brain_control.load_persona_store()["global"] == "", body
        assert _restart(tmp_path).runtime_config.session.instructions == CLI_DEFAULT


def test_unreadable_persona_file_falls_back(tmp_path, monkeypatch):
    # A directory where the file should be: open() raises IsADirectoryError
    # (an OSError), which must be caught like any other read failure. Works as
    # root, unlike a chmod-000 file.
    path = _persona_env(tmp_path, monkeypatch)
    path.mkdir(parents=True)

    assert brain_control.load_persona_store()["global"] == ""
    assert _restart(tmp_path).runtime_config.session.instructions == CLI_DEFAULT


def test_oversized_persona_on_disk_is_ignored(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "persona": "x" * (brain_control.PERSONA_MAX_CHARS + 1)}))

    assert brain_control.load_persona_store()["global"] == ""
    assert _restart(tmp_path).runtime_config.session.instructions == CLI_DEFAULT


def test_oversized_persona_is_rejected_without_persisting(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)

    ack = bc._config_set({"persona": "x" * (brain_control.PERSONA_MAX_CHARS + 1)})

    assert ack["ok"] is False
    assert "character cap" in ack["error"]
    assert not path.exists()
    assert bc.runtime_config.session.instructions == CLI_DEFAULT


def test_control_characters_are_rejected(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)

    ack = bc._config_set({"persona": "sneaky\x1b[31m escape"})

    assert ack["ok"] is False
    assert "control characters" in ack["error"]
    assert bc.runtime_config.session.instructions == CLI_DEFAULT


def test_newlines_and_tabs_are_allowed(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)

    multiline = "You are terse.\n\tRules:\r\n- one\n- two"
    assert bc._config_set({"persona": multiline})["ok"] is True
    assert _restart(tmp_path).runtime_config.session.instructions == multiline


def test_save_persona_leaves_no_partial_file(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    brain_control.save_persona_store({"global": "first"})

    assert json.loads(path.read_text())["global"] == "first"
    # The temp file the atomic write goes through is always cleaned up, so a
    # later load can never pick up a half-written sibling.
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_save_persona_failure_does_not_raise(tmp_path, monkeypatch):
    # Parent path is a FILE, so mkdir/open both fail -- persistence is
    # best-effort and must never take down a live config_set.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("VOICE_PERSONA_FILE", str(blocker / "persona.json"))

    brain_control.save_persona_store({"global": "anything"})

    bc = _restart(tmp_path)
    assert bc.runtime_config.session.instructions == CLI_DEFAULT
    assert bc._config_state()["persona_persisted"] is False


def test_persona_path_is_env_overridable_outside_the_package(tmp_path, monkeypatch):
    """Same ruling-1 placement as the voices/ sidecar -- outside the package,
    so a pip reinstall can't reach it."""
    path = _persona_env(tmp_path, monkeypatch)
    assert brain_control.persona_path() == path

    monkeypatch.delenv("VOICE_PERSONA_FILE")
    assert brain_control.persona_path() == pathlib.Path("~/speech-to-speech/persona.json").expanduser()


# ── tiered resolution: brain override > preset > global > shipped default ──


def _brains_file(tmp_path):
    """A brains.json with the real four brains, so _set_brain can switch
    without a live endpoint (availability is checked before the model probe)."""
    path = tmp_path / "brains.json"
    path.write_text(json.dumps({name: {"available": False} for name in ("coder", "local", "hermes", "frontier")}))
    return path


def test_resolution_order_at_every_tier(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    store = brain_control.empty_persona_store()
    R = brain_control.resolve_persona

    # 4. nothing configured -> shipped default
    assert R(store, "coder", CLI_DEFAULT) == (CLI_DEFAULT, "default")

    # 3. global set -> global, for every brain
    store["global"] = "global text"
    assert R(store, "coder", CLI_DEFAULT) == ("global text", "global")
    assert R(store, "hermes", CLI_DEFAULT) == ("global text", "global")

    # 2. preset selected for hermes -> preset wins over global, hermes only
    store["brains"]["hermes"] = {"mode": "preset"}
    assert R(store, "hermes", CLI_DEFAULT) == (brain_control.BRAIN_PRESETS["hermes"], "brain_preset")
    assert R(store, "coder", CLI_DEFAULT) == ("global text", "global")

    # 1. a typed override for hermes outranks its own preset
    store["brains"]["hermes"] = {"mode": "custom", "text": "my hermes words"}
    assert R(store, "hermes", CLI_DEFAULT) == ("my hermes words", "brain_custom")


def test_preset_never_applies_unless_selected(tmp_path, monkeypatch):
    """The presets exist to help, not to impose: a brain the user never touched
    must resolve to their global persona, not to our shipped preset."""
    _persona_env(tmp_path, monkeypatch)
    store = brain_control.empty_persona_store()
    store["global"] = "the user's own words"

    for brain in brain_control.BRAIN_PRESETS:
        assert brain_control.resolve_persona(store, brain, CLI_DEFAULT) == ("the user's own words", "global")


def test_switching_brains_yields_that_brains_persona(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _make_brain_control(tmp_path, runtime_config=_FakeRuntimeConfig(instructions=CLI_DEFAULT))
    bc.brains = json.loads(_brains_file(tmp_path).read_text())
    bc._config_set({"persona": "global words"})
    bc._config_set({"persona": "coder words", "persona_scope": "brain"})

    assert bc.active_brain == "coder"
    assert bc.runtime_config.session.instructions == "coder words"

    # Switching to a brain with no override falls back to the global.
    bc.active_brain = "local"
    bc._apply_persona()
    assert bc.runtime_config.session.instructions == "global words"
    assert bc.persona_tier == "global"

    # ...and back again picks the override up.
    bc.active_brain = "coder"
    bc._apply_persona()
    assert bc.runtime_config.session.instructions == "coder words"
    assert bc.persona_tier == "brain_custom"


def test_per_brain_override_survives_a_restart(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc._config_set({"persona": "everywhere"})
    bc._config_set({"persona": "just for coder", "persona_scope": "brain"})

    revived = _restart(tmp_path)

    assert revived.active_brain == "coder"
    assert revived.runtime_config.session.instructions == "just for coder"
    assert revived.persona_tier == "brain_custom"
    assert revived.persona_store["global"] == "everywhere"


def test_preset_selection_persists_and_tracks_the_shipped_text(tmp_path, monkeypatch):
    """Preset mode stores the CHOICE, not a copy of the text — so a preset we
    improve in a later release reaches the users who selected it."""
    path = _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)

    ack = bc._config_set({"persona_scope": "brain", "persona_mode": "preset"})

    assert ack["ok"] is True
    assert json.loads(path.read_text())["brains"]["coder"] == {"mode": "preset"}
    assert bc.runtime_config.session.instructions == brain_control.BRAIN_PRESETS["coder"]

    revived = _restart(tmp_path)
    assert revived.persona_tier == "brain_preset"
    assert revived.runtime_config.session.instructions == brain_control.BRAIN_PRESETS["coder"]


def test_clearing_a_brain_override_falls_back_not_to_empty(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc._config_set({"persona": "my global"})
    bc._config_set({"persona": "coder only", "persona_scope": "brain"})
    assert bc.runtime_config.session.instructions == "coder only"

    bc._config_set({"persona": "", "persona_scope": "brain"})

    assert bc.runtime_config.session.instructions == "my global"
    assert bc.persona_tier == "global"
    assert "coder" not in bc.persona_store["brains"]


def test_clearing_the_global_falls_back_to_the_shipped_default(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc._config_set({"persona": "my global"})

    bc._config_set({"persona": ""})

    assert bc.runtime_config.session.instructions == CLI_DEFAULT
    assert bc.persona_tier == "default"
    # Nothing left in any tier -> the file goes away entirely.
    assert not path.exists()


def test_clearing_the_global_leaves_a_brain_override_standing(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc._config_set({"persona": "my global"})
    bc._config_set({"persona": "coder only", "persona_scope": "brain"})

    bc._config_set({"persona": ""})

    assert bc.runtime_config.session.instructions == "coder only"
    assert path.exists()
    assert _restart(tmp_path).runtime_config.session.instructions == "coder only"


def test_inherit_mode_drops_the_override(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc._config_set({"persona_scope": "brain", "persona_mode": "preset"})
    assert bc.persona_tier == "brain_preset"

    bc._config_set({"persona_scope": "brain", "persona_mode": "inherit"})

    assert bc.persona_tier == "default"
    assert bc.runtime_config.session.instructions == CLI_DEFAULT


def test_unknown_scope_and_mode_are_rejected(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)

    assert "unknown persona scope" in bc._config_set({"persona": "x", "persona_scope": "wat"})["error"]
    assert "unknown persona mode" in bc._config_set(
        {"persona": "x", "persona_scope": "brain", "persona_mode": "wat"}
    )["error"]
    assert bc.runtime_config.session.instructions == CLI_DEFAULT


def test_preset_refused_for_a_brain_without_one(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc.active_brain = "custom_lane"  # a brain id outside BRAIN_PRESETS entirely

    ack = bc._config_set({"persona_scope": "brain", "persona_mode": "preset"})

    assert ack["ok"] is False
    assert "no preset available" in ack["error"]


# ── old (v1, global-only) file shape ────────────────────────────────────


def test_v1_file_loads_as_the_global_persona(tmp_path, monkeypatch):
    """The v1 shape shipped for all of five minutes, but the load path must
    still read it rather than crash — a startup crash here takes the
    assistant down."""
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "persona": "written by the old build"}')

    bc = _restart(tmp_path)

    assert bc.runtime_config.session.instructions == "written by the old build"
    assert bc.persona_tier == "global"
    assert bc.persona_store["brains"] == {}


def test_v1_file_is_rewritten_as_v2_on_the_next_save(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "persona": "old"}')
    bc = _restart(tmp_path)

    bc._config_set({"persona": "for coder", "persona_scope": "brain"})

    payload = json.loads(path.read_text())
    assert payload["version"] == 2
    assert payload["global"] == "old"
    assert payload["brains"]["coder"] == {"mode": "custom", "text": "for coder"}


def test_malformed_pieces_are_dropped_not_fatal(tmp_path, monkeypatch):
    """One unusable brain entry must not cost the user their other tiers."""
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": 2,
        "global": "kept",
        "brains": {
            "coder": {"mode": "custom", "text": "kept too"},
            "local": {"mode": "nonsense"},
            "hermes": 12345,
            "frontier": {"mode": "custom", "text": "bad\x00null"},
        },
    }))

    store = brain_control.load_persona_store()

    assert store["global"] == "kept"
    assert store["brains"] == {"coder": {"mode": "custom", "text": "kept too"}}


def test_brains_key_of_the_wrong_type_is_ignored(tmp_path, monkeypatch):
    path = _persona_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 2, "global": "kept", "brains": "not an object"}')

    store = brain_control.load_persona_store()

    assert store == {"version": 2, "global": "kept", "brains": {}}


def test_config_state_reports_the_tier_and_the_other_tiers(tmp_path, monkeypatch):
    _persona_env(tmp_path, monkeypatch)
    bc = _restart(tmp_path)
    bc._config_set({"persona": "my global"})
    bc._config_set({"persona": "coder only", "persona_scope": "brain"})

    tiers = bc._config_state()["persona_tiers"]

    assert tiers["resolved_from"] == "brain_custom"
    assert tiers["brain"] == "coder"
    assert tiers["brain_mode"] == "custom"
    assert tiers["brain_text"] == "coder only"
    assert tiers["global"] == "my global"
    assert tiers["preset"] == brain_control.BRAIN_PRESETS["coder"]
    assert set(tiers["presets"]) == {"coder", "local", "frontier", "hermes"}


# ── brain reachability: `available` (configured intent) vs `reachable` ──
# (observed). `available` is never mutated by a probe -- these tests cover
# the orthogonal `reachable`/`probe_error` fields and the config_get-driven
# background reconcile that keeps them fresh without blocking the panel.


def test_set_brain_failure_records_unreachable_then_success_flips_back(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "auto"}}

    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: None)
    ok, error = bc._set_brain("coder")
    assert ok is False

    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")
    assert entry["reachable"] is False
    assert entry["probe_error"] == error
    assert error  # non-empty -- the panel has something to show

    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "some-model")
    # The success path of `_set_brain` also rebuilds `_extra_body` via the
    # real `BaseOpenAICompatibleHandler` -- stubbed to a bare `type(...)` with
    # no `_build_extra_body` for the other tests in this file, which never
    # exercise a successful switch. Give it one for just this assertion.
    monkeypatch.setattr(
        brain_control.BaseOpenAICompatibleHandler, "_build_extra_body", staticmethod(lambda *a, **k: None),
        raising=False,
    )
    ok, error = bc._set_brain("coder")
    assert ok is True

    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")
    assert entry["reachable"] is True
    assert entry["probe_error"] == ""


def test_never_probed_brain_reports_null_reachable(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {
        "coder": {"available": True, "base_url": "http://x"},
        "local": {"available": False},
    }

    for entry in bc._config_state()["brains"]:
        assert entry["reachable"] is None
        assert entry["probe_error"] == ""


def test_config_get_reconcile_probes_only_available_brains(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path)
    bc.brains = {
        "coder": {"available": True, "base_url": "http://coder"},
        "local": {"available": False, "base_url": "http://local"},
        "hermes": {"available": True, "base_url": "http://hermes"},
    }
    calls = []

    def fake_resolve(base_url, model, api_key):
        calls.append(base_url)
        return "model-id"

    monkeypatch.setattr(bc, "_resolve_model", fake_resolve)

    bc.handle({"type": "config_get"})
    bc._reconcile_thread.join(timeout=1)

    assert sorted(calls) == ["http://coder", "http://hermes"]


def test_config_get_returns_immediately_without_waiting_on_the_probe(tmp_path, monkeypatch):
    """The reconcile must never block config_get -- a probe stuck on the
    network must not stall the panel opening. Uses a gated fake probe (not a
    wall-clock race) so the assertion is deterministic."""
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    started = threading.Event()
    release = threading.Event()

    def slow_resolve(*a, **k):
        started.set()
        release.wait(timeout=2)
        return "model-id"

    monkeypatch.setattr(bc, "_resolve_model", slow_resolve)

    t0 = time.monotonic()
    state = bc.handle({"type": "config_get"})
    elapsed = time.monotonic() - t0

    assert state["type"] == "config_state"
    # Returned well before the still-blocked probe could possibly finish.
    assert elapsed < 1.0
    assert started.wait(timeout=1), "reconcile thread never started its probe"
    release.set()
    bc._reconcile_thread.join(timeout=1)


def test_debounce_skips_a_second_reconcile_started_too_soon(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    calls = []

    def fake_resolve(base_url, model, api_key):
        calls.append(base_url)
        return "model-id"

    monkeypatch.setattr(bc, "_resolve_model", fake_resolve)

    bc.handle({"type": "config_get"})
    first_thread = bc._reconcile_thread
    first_thread.join(timeout=1)

    bc.handle({"type": "config_get"})

    # No second thread was started -- the attribute still points at the first.
    assert bc._reconcile_thread is first_thread
    assert len(calls) == 1


def test_reconcile_no_broadcast_when_nothing_changed(tmp_path, monkeypatch):
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, streamer=streamer)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    bc._brain_probes["coder"] = {"ok": True, "at": 0.0, "error": ""}
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "model-id")

    bc._reconcile_brain_reachability()

    assert streamer.broadcasts == []


def test_reconcile_broadcasts_once_when_something_changed(tmp_path, monkeypatch):
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, streamer=streamer)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    bc._brain_probes["coder"] = {"ok": True, "at": 0.0, "error": ""}
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: None)

    bc._reconcile_brain_reachability()

    assert len(streamer.broadcasts) == 1
    assert streamer.broadcasts[0]["type"] == "config_state"
    entry = next(b for b in streamer.broadcasts[0]["brains"] if b["name"] == "coder")
    assert entry["reachable"] is False


def test_reconcile_with_no_streamer_and_a_change_does_not_crash(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path, streamer=None)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: None)

    bc._reconcile_brain_reachability()  # must not raise

    assert bc._brain_probes["coder"]["ok"] is False
    assert bc._reconcile_in_progress is False


def test_garbage_probe_debounce_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("VOICE_BRAIN_PROBE_DEBOUNCE_S", "not-a-number")
    assert brain_control._env_seconds("VOICE_BRAIN_PROBE_DEBOUNCE_S", 20.0) == 20.0


def test_garbage_probe_debounce_env_still_debounces_a_second_config_get(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_BRAIN_PROBE_DEBOUNCE_S", "garbage")
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    calls = []
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: (calls.append(1), "model-id")[1])

    bc.handle({"type": "config_get"})
    bc._reconcile_thread.join(timeout=1)
    bc.handle({"type": "config_get"})

    # Falls back to the 20s default rather than crashing or treating the
    # garbage value as "debounce disabled".
    assert len(calls) == 1


def test_presets_are_plausible_voice_system_prompts():
    """Cheap guard against a preset drifting into markdown or boilerplate."""
    for name, text in brain_control.BRAIN_PRESETS.items():
        assert brain_control.validate_persona(text) == (True, ""), name
        assert 100 < len(text) < brain_control.PERSONA_MAX_CHARS, name
        assert "spoken aloud" in text, name
        assert "*" not in text and "#" not in text, name


# ── per-brain model selector (ruling 2026-07-19) ────────────────────────
#
# A brain's endpoint can serve many models (NVIDIA NIM serves hundreds; a
# llama.cpp router a handful); brains.json's `model` field was fixed at edit
# time. These cover the model-override sidecar, effective-model resolution,
# the `config_set{brain_model}` protocol, and the live/curated model lists
# surfaced in `config_state`.


def _model_overrides_env(tmp_path, monkeypatch):
    path = tmp_path / "model_dir" / "model_overrides.json"
    monkeypatch.setenv("VOICE_MODEL_OVERRIDES_FILE", str(path))
    return path


def _stub_switch(monkeypatch):
    """Let a real `_set_brain` probe/switch succeed without a live endpoint --
    same stub as the reachability tests above, needed here because
    `_set_brain_model` re-runs `_set_brain` on the active brain."""
    monkeypatch.setattr(
        brain_control.BaseOpenAICompatibleHandler, "_build_extra_body", staticmethod(lambda *a, **k: None),
        raising=False,
    )


# ── per-brain reasoning_effort (chat_template_kwargs override trap) ────────
#
# On at least one llama-server build, a client-sent `chat_template_kwargs`
# OVERRIDES the server's own pinned `reasoning_effort` rather than merging
# with it -- our own disable_thinking kwargs silently clobber the pin and
# fall back to the chat template's own default (verified: gpt-oss defaults
# to xhigh). A brain that needs a specific effort declares it in brains.json.


def test_set_brain_reasoning_effort_sets_chat_template_kwargs(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path)
    bc.brains = {
        "herald": {"available": True, "base_url": "http://x", "model": "auto", "reasoning_effort": "medium"},
    }
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "gpt-oss-120b")
    _stub_switch(monkeypatch)

    ok, error = bc._set_brain("herald")

    assert (ok, error) == (True, "")
    assert bc.llm_handler._extra_body == {
        "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "medium"}
    }


def test_set_brain_without_reasoning_effort_extra_body_unchanged(tmp_path, monkeypatch):
    """Regression pin: a brain with no `reasoning_effort` key must still go
    through the existing `_build_extra_body` call, untouched by this slice."""
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "auto"}}
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "some-model")
    monkeypatch.setattr(
        brain_control.BaseOpenAICompatibleHandler,
        "_build_extra_body",
        staticmethod(lambda *a, **k: {"sentinel": "unchanged"}),
        raising=False,
    )

    ok, error = bc._set_brain("coder")

    assert (ok, error) == (True, "")
    assert bc.llm_handler._extra_body == {"sentinel": "unchanged"}


def test_set_brain_invalid_reasoning_effort_warns_and_falls_back(tmp_path, monkeypatch, caplog):
    bc = _make_brain_control(tmp_path)
    bc.brains = {
        "herald": {"available": True, "base_url": "http://x", "model": "auto", "reasoning_effort": "bogus"},
    }
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "gpt-oss-120b")
    monkeypatch.setattr(
        brain_control.BaseOpenAICompatibleHandler,
        "_build_extra_body",
        staticmethod(lambda *a, **k: {"sentinel": "fallback"}),
        raising=False,
    )

    with caplog.at_level(logging.WARNING, logger=brain_control.__name__):
        ok, error = bc._set_brain("herald")

    assert (ok, error) == (True, "")  # a typo must not kill the switch
    assert bc.llm_handler._extra_body == {"sentinel": "fallback"}
    assert "bogus" in caplog.text
    assert "herald" in caplog.text


# ── validate_model_override ──────────────────────────────────────────────


def test_validate_model_override_accepts_a_normal_id():
    assert brain_control.validate_model_override("deepseek-ai/deepseek-v3.2") == (True, "")


def test_validate_model_override_rejects_empty():
    ok, error = brain_control.validate_model_override("")
    assert ok is False
    assert "empty" in error


def test_validate_model_override_rejects_non_string():
    ok, error = brain_control.validate_model_override(None)
    assert ok is False
    assert "text" in error


def test_validate_model_override_rejects_auto():
    """Clearing the override is the only way back to the configured default --
    an override of literally "auto" would be indistinguishable from none."""
    ok, error = brain_control.validate_model_override("auto")
    assert ok is False
    assert "auto" in error


def test_validate_model_override_rejects_oversize():
    ok, error = brain_control.validate_model_override("x" * (brain_control.MODEL_OVERRIDE_MAX_CHARS + 1))
    assert ok is False
    assert "character cap" in error


def test_validate_model_override_rejects_control_characters():
    ok, error = brain_control.validate_model_override("sneaky\x1b[31m escape")
    assert ok is False
    assert "control characters" in error


def test_validate_model_override_rejects_newline_unlike_persona():
    """Unlike the multi-line persona, a model id is single-line free text --
    no control character is allowed through, not even newline/tab."""
    ok, error = brain_control.validate_model_override("model\nid")
    assert ok is False
    assert "control characters" in error


# ── model override store: load/save, fail-safe, defaults ────────────────


def test_model_overrides_path_defaults_next_to_the_persona_file(tmp_path, monkeypatch):
    persona_file = tmp_path / "persona_dir" / "persona.json"
    monkeypatch.setenv("VOICE_PERSONA_FILE", str(persona_file))
    monkeypatch.delenv("VOICE_MODEL_OVERRIDES_FILE", raising=False)

    assert brain_control.model_overrides_path() == persona_file.parent / "model_overrides.json"


def test_model_overrides_path_is_env_overridable(tmp_path, monkeypatch):
    path = tmp_path / "elsewhere" / "overrides.json"
    monkeypatch.setenv("VOICE_MODEL_OVERRIDES_FILE", str(path))
    assert brain_control.model_overrides_path() == path


def test_model_override_store_roundtrip(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    store = {"version": 1, "brains": {"frontier": "deepseek-ai/deepseek-v3.2"}}

    brain_control.save_model_override_store(store)

    assert json.loads(path.read_text()) == store
    assert brain_control.load_model_override_store() == store


def test_missing_model_overrides_file_falls_back_to_empty(tmp_path, monkeypatch):
    _model_overrides_env(tmp_path, monkeypatch)
    assert brain_control.load_model_override_store() == brain_control.empty_model_override_store()


def test_save_model_overrides_deletes_the_file_when_empty(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    brain_control.save_model_override_store({"version": 1, "brains": {"coder": "x"}})
    assert path.is_file()

    brain_control.save_model_override_store({"version": 1, "brains": {}})

    assert not path.exists()


def test_corrupt_model_overrides_file_does_not_raise(tmp_path, monkeypatch, caplog):
    path = _model_overrides_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "brains": {truncated')

    with caplog.at_level(logging.WARNING, logger=brain_control.__name__):
        store = brain_control.load_model_override_store()

    assert store == brain_control.empty_model_override_store()
    assert "unreadable model override file" in caplog.text


def test_unreadable_model_overrides_file_falls_back(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    path.mkdir(parents=True)  # a directory where the file should be

    assert brain_control.load_model_override_store() == brain_control.empty_model_override_store()


def test_non_object_model_overrides_file_falls_back(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("[]")

    assert brain_control.load_model_override_store() == brain_control.empty_model_override_store()


def test_malformed_model_override_entries_are_dropped_not_fatal(tmp_path, monkeypatch):
    """One unusable brain's override must not cost the user the others."""
    path = _model_overrides_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": 1,
        "brains": {
            "coder": "good-model",
            "local": "",
            "hermes": 12345,
            "frontier": "auto",
        },
    }))

    store = brain_control.load_model_override_store()

    assert store["brains"] == {"coder": "good-model"}


def test_brains_key_of_the_wrong_type_is_ignored(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "brains": "not an object"}')

    assert brain_control.load_model_override_store() == {"version": 1, "brains": {}}


# ── _extract_model_ids / _curated_models ────────────────────────────────


def test_extract_model_ids_parses_llama_cpp_router_shape():
    data = [{"id": "a", "status": {"value": "loaded"}}, {"id": "b", "status": {"value": "unloaded"}}]
    assert brain_control._extract_model_ids(data) == ["a", "b"]


def test_extract_model_ids_parses_plain_openai_shape():
    assert brain_control._extract_model_ids([{"id": "x"}, {"id": "y"}]) == ["x", "y"]


def test_extract_model_ids_is_defensive_against_garbage():
    assert brain_control._extract_model_ids(None) == []
    assert brain_control._extract_model_ids("not a list") == []
    assert brain_control._extract_model_ids([{"no_id": 1}, "not a dict", {"id": 123}, {"id": ""}]) == []


def test_extract_model_ids_caps_at_500():
    data = [{"id": f"m{i}"} for i in range(600)]
    ids = brain_control._extract_model_ids(data)
    assert len(ids) == 500
    assert ids[0] == "m0" and ids[-1] == "m499"


def test_curated_models_filters_non_string_entries():
    assert brain_control._curated_models({"models": ["a", 1, "", None, "b"]}) == ["a", "b"]


def test_curated_models_absent_yields_empty():
    assert brain_control._curated_models({}) == []
    assert brain_control._curated_models({"models": "not a list"}) == []


# ── effective-model resolution ───────────────────────────────────────────


def test_effective_model_falls_back_to_configured_without_an_override(tmp_path):
    bc = _make_brain_control(tmp_path)
    assert bc._effective_model("coder", {"model": "configured-model"}) == "configured-model"


def test_effective_model_falls_back_to_auto_with_no_model_field(tmp_path):
    bc = _make_brain_control(tmp_path)
    assert bc._effective_model("coder", {}) == "auto"


def test_effective_model_override_beats_configured(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.model_overrides = {"version": 1, "brains": {"coder": "override-model"}}
    assert bc._effective_model("coder", {"model": "configured-model"}) == "override-model"


def test_effective_model_override_is_per_brain(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.model_overrides = {"version": 1, "brains": {"coder": "override-model"}}
    assert bc._effective_model("local", {"model": "configured-model"}) == "configured-model"


def test_clearing_an_override_restores_the_configured_default(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.model_overrides = {"version": 1, "brains": {"coder": "override-model"}}
    assert bc._effective_model("coder", {"model": "configured-model"}) == "override-model"

    bc.model_overrides["brains"].pop("coder")

    assert bc._effective_model("coder", {"model": "configured-model"}) == "configured-model"


# ── config_set{brain_model} protocol ─────────────────────────────────────


def test_brain_model_unknown_brain_errors(tmp_path):
    bc = _make_brain_control(tmp_path)

    ack = bc._config_set({"brain_model": {"brain": "nope", "model": "x"}})

    assert ack["ok"] is False
    assert "unknown brain" in ack["error"]


def test_brain_model_rejects_auto(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}

    ack = bc._config_set({"brain_model": {"brain": "coder", "model": "auto"}})

    assert ack["ok"] is False
    assert "auto" in ack["error"]


def test_brain_model_rejects_oversize(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}

    ack = bc._config_set(
        {"brain_model": {"brain": "coder", "model": "x" * (brain_control.MODEL_OVERRIDE_MAX_CHARS + 1)}}
    )

    assert ack["ok"] is False
    assert "character cap" in ack["error"]


def test_brain_model_rejects_control_characters(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}

    ack = bc._config_set({"brain_model": {"brain": "coder", "model": "sneaky\x1b[31m"}})

    assert ack["ok"] is False
    assert "control characters" in ack["error"]


def test_brain_model_active_brain_probes_persists_and_resets_chat(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, streamer=streamer)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}
    _stub_switch(monkeypatch)
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "new-model")

    ack = bc._config_set({"brain_model": {"brain": "coder", "model": "new-model"}})

    assert ack["ok"] is True
    assert ack["chat_reset"] is True
    assert bc.model_overrides["brains"]["coder"] == "new-model"
    assert json.loads(path.read_text())["brains"]["coder"] == "new-model"
    assert len(streamer.broadcasts) == 1
    assert streamer.broadcasts[0]["type"] == "config_state"


def test_brain_model_active_brain_failed_probe_does_not_persist_or_reset(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, streamer=streamer)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: None)

    ack = bc._config_set({"brain_model": {"brain": "coder", "model": "new-model"}})

    assert ack["ok"] is False
    assert bc.model_overrides.get("brains", {}).get("coder") is None
    assert not path.exists()
    assert streamer.broadcasts == []
    # The switch's own failed probe still recorded the brain unreachable.
    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")
    assert entry["reachable"] is False


def test_brain_model_active_brain_set_brain_raising_rolls_back_the_staged_override(tmp_path, monkeypatch):
    """`_set_brain` isn't expected to raise, but anything outside
    `_resolve_model`'s own catches (e.g. the OpenAI client constructor) must
    still roll the staged override back -- otherwise it's left live in
    memory (config_state would report an override that never probed and
    isn't on disk) and silently evaporates on the next restart."""
    path = _model_overrides_env(tmp_path, monkeypatch)
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, streamer=streamer)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(bc, "_resolve_model", _boom)

    # Via handle(): its own top-level try/except is what turns the raise
    # into an error ack -- _set_brain_model's job is only to not leave
    # staged state behind before that happens.
    ack = bc.handle({"type": "config_set", "brain_model": {"brain": "coder", "model": "new-model"}})

    assert ack["type"] == "config_ack"
    assert ack["ok"] is False
    assert bc.model_overrides.get("brains", {}).get("coder") is None
    assert not path.exists()
    assert streamer.broadcasts == []
    assert bc._config_state()["brains"][0]["model_override"] == ""


def test_brain_model_non_active_brain_persists_without_probing(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    streamer = _FakeStreamer()
    bc = _make_brain_control(tmp_path, streamer=streamer)  # active_brain defaults to "coder"
    bc.brains = {
        "coder": {"available": True, "base_url": "http://x", "model": "base-model"},
        "local": {"available": True, "base_url": "http://y", "model": "local-model"},
    }
    probe_calls = []
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: probe_calls.append(1))

    ack = bc._config_set({"brain_model": {"brain": "local", "model": "custom-local-model"}})

    assert ack["ok"] is True
    assert ack["chat_reset"] is False
    assert probe_calls == []
    assert json.loads(path.read_text())["brains"]["local"] == "custom-local-model"
    assert len(streamer.broadcasts) == 1


def test_brain_model_empty_clears_the_override(tmp_path, monkeypatch):
    path = _model_overrides_env(tmp_path, monkeypatch)
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}
    _stub_switch(monkeypatch)
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "override-model")
    bc._config_set({"brain_model": {"brain": "coder", "model": "override-model"}})
    assert path.is_file()

    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "base-model")
    ack = bc._config_set({"brain_model": {"brain": "coder", "model": ""}})

    assert ack["ok"] is True
    assert "coder" not in bc.model_overrides.get("brains", {})
    assert not path.exists()


def test_brain_model_none_streamer_is_safe(tmp_path, monkeypatch):
    _model_overrides_env(tmp_path, monkeypatch)
    bc = _make_brain_control(tmp_path, streamer=None)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}
    _stub_switch(monkeypatch)
    monkeypatch.setattr(bc, "_resolve_model", lambda *a, **k: "new-model")

    ack = bc._config_set({"brain_model": {"brain": "coder", "model": "new-model"}})  # must not raise

    assert ack["ok"] is True


# ── config_state: model_override / models (curated vs live) ─────────────


def test_config_state_reports_model_override_and_empty_models_by_default(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}

    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")

    assert entry["model"] == "base-model"
    assert entry["model_override"] == ""
    assert entry["models"] == []


def test_config_state_reports_the_persisted_override(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}
    bc.model_overrides = {"version": 1, "brains": {"coder": "override-model"}}

    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")

    assert entry["model_override"] == "override-model"


def test_config_state_curated_models_win_over_live_list(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "models": ["curated-a", "curated-b"]}}
    bc._brain_probes["coder"] = {"ok": True, "at": 0.0, "error": "", "models": ["live-a", "live-b"]}

    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")

    assert entry["models"] == ["curated-a", "curated-b"]


def test_config_state_falls_back_to_the_live_list_without_a_curated_one(tmp_path):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    bc._brain_probes["coder"] = {"ok": True, "at": 0.0, "error": "", "models": ["live-a", "live-b"]}

    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")

    assert entry["models"] == ["live-a", "live-b"]


def test_live_model_list_captured_from_a_probe_shows_up_in_config_state(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x"}}
    _stub_switch(monkeypatch)

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "model-a"}, {"id": "model-b", "status": {"value": "loaded"}}]}

    monkeypatch.setattr(brain_control.httpx, "get", lambda *a, **k: _FakeResp())

    ok, error = bc._set_brain("coder")

    assert ok is True
    entry = next(b for b in bc._config_state()["brains"] if b["name"] == "coder")
    assert entry["models"] == ["model-a", "model-b"]


def test_live_model_list_capped_at_500(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path)
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "fixed-model"}}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": f"m{i}"} for i in range(600)]}

    monkeypatch.setattr(brain_control.httpx, "get", lambda *a, **k: _FakeResp())

    resolved = bc._resolve_model("http://x", "fixed-model", None)

    assert resolved == "fixed-model"
    assert len(bc._probed_model_ids.ids) == 500


# ── one snapshot, two envelopes ─────────────────────────────────────────
# `config_state` and a successful `config_ack` are the same state. They were
# hand-written separately and drifted: the ack had `model` the state lacked,
# the state had `voices`/`brains` the ack lacked, and the client carried three
# workarounds for the gap. These pin the shape so it cannot drift again.


def _both_frames(tmp_path):
    bc = _make_brain_control(tmp_path, streamer=_FakeStreamer(), tts_handler=_FakeTTSHandler("jean"))
    bc.brains = {"coder": {"available": True, "base_url": "http://x", "model": "base-model"}}
    return bc._config_state(), bc._config_set({"reset_chat": True})


ENVELOPE = {"type", "ok", "chat_reset"}


def test_config_state_and_config_ack_carry_the_same_fields(tmp_path):
    state, ack = _both_frames(tmp_path)

    assert ack["ok"] is True
    assert set(state) - ENVELOPE == set(ack) - ENVELOPE


def test_config_state_and_config_ack_carry_the_same_values(tmp_path):
    """Same keys is not enough -- two builders can agree on names and still
    disagree on content, which is the drift this slice removed."""
    state, ack = _both_frames(tmp_path)

    shared = set(state) - ENVELOPE
    assert {k: state[k] for k in shared} == {k: ack[k] for k in shared}


def test_both_frames_carry_the_fields_that_used_to_be_on_only_one(tmp_path):
    """The specific drift: `model` was ack-only, `voices`/`brains` state-only.

    The client needed all three from both -- it merged custom voices against a
    stale cache, hid the voice controls on the wrong frame, and chased every
    ack with a `config_get` to fetch the brain list.
    """
    state, ack = _both_frames(tmp_path)

    for frame in (state, ack):
        assert frame["model"] == "test-model"
        assert isinstance(frame["voices"], list)
        assert [b["name"] for b in frame["brains"]] == ["coder"]


def test_chat_reset_stays_ack_only(tmp_path):
    """It describes what the operation did, not what the state is -- a
    `config_state` broadcast has no operation to report."""
    state, ack = _both_frames(tmp_path)

    assert ack["chat_reset"] is True
    assert "chat_reset" not in state


# ── speech_style: the live expression/prosody handle (S3+S4) ───────────────
#
# Exercises a REAL RemoteSpeechTTSHandler (not a fake) against a local HTTP
# server so `_config_set`'s effect is proven on the actual serialized wire
# payload of the NEXT synthesis request, not just on an in-memory attribute.


class _CapturingServer:
    def __init__(self, captured: list, pcm_bytes: bytes):
        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                captured.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(pcm_bytes)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_remote_speech_handler(base_url):
    import patches.remote_speech_tts_handler as rsh

    return rsh.RemoteSpeechTTSHandler(
        Event(),
        queue_in=Queue(),
        queue_out=Queue(),
        setup_args=(Event(),),
        setup_kwargs=dict(
            # Unprefixed -- matches RemoteSpeechTTSHandler.setup()'s real
            # parameter names (what the pipeline hands it after rename_args
            # strips the remote_speech_ prefix), not the ArgumentsClass field
            # names. See test_remote_speech_tts_handler.py's
            # test_setup_signature_matches_what_rename_args_produces.
            base_url=base_url,
            connect_timeout_s=0.3,
            read_timeout_s=2.0,
            max_duration_s=1.0,
            pocket_kwargs={"voice": "jean"},
        ),
    )


class _RemoteTTSInput:
    def __init__(self, text: str):
        self.text = text


def test_speech_style_config_set_takes_effect_on_next_wire_payload(tmp_path):
    """The whole point of the slice: `config_set {speech_style}` must reach
    the ACTIVE handler and be sent on the very next synthesis request --
    proven by inspecting the serialized request body the fake server
    received, not merely a Python attribute BrainControl happened to set."""
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()  # above the response floor
    server = _CapturingServer(captured, pcm_bytes)
    try:
        handler = _make_remote_speech_handler(server.base_url)
        bc = _make_brain_control(tmp_path, tts_handler=handler)

        ack = bc._config_set({"speech_style": "cheerful and upbeat, like a game show host"})
        assert ack["ok"] is True

        list(handler.process(_RemoteTTSInput("hello there")))

        assert len(captured) == 1
        assert captured[0]["instructions"] == "cheerful and upbeat, like a game show host"
    finally:
        server.close()


def test_speech_style_empty_or_whitespace_substitutes_default_never_empty_on_wire(tmp_path):
    import patches.remote_speech_tts_handler as rsh

    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _CapturingServer(captured, pcm_bytes)
    try:
        handler = _make_remote_speech_handler(server.base_url)
        bc = _make_brain_control(tmp_path, tts_handler=handler)
        # First set something real, so a naive "empty means leave it alone"
        # implementation would still show the OLD value on the wire. Checked
        # directly (not just via the ack) so this precondition itself fails
        # loudly if speech_style isn't wired up at all -- otherwise the two
        # no-op paths (unwired vs. correctly-reverted) are indistinguishable.
        bc._config_set({"speech_style": "whispered and conspiratorial"})
        assert handler.instructions == "whispered and conspiratorial"

        ack = bc._config_set({"speech_style": "   "})
        assert ack["ok"] is True

        list(handler.process(_RemoteTTSInput("hello there")))

        assert captured[0]["instructions"] == rsh.DEFAULT_INSTRUCTIONS
        assert captured[0]["instructions"]  # never empty on the wire
    finally:
        server.close()


def test_speech_style_unsupported_backend_returns_clear_error_not_silent_success(tmp_path):
    """pocket (and kokoro, qwen3 -- anything without an `instructions` handle)
    must fail loudly, naming the active backend, rather than accepting the
    value and silently doing nothing. This is the same defect class as the
    NPU vision json_schema silent-ignore and the remote TTS's own
    empty-instructions 200-with-zero-bytes bug."""
    bc = _make_brain_control(tmp_path, tts_handler=_FakeTTSHandler(voice="jean"))

    ack = bc._config_set({"speech_style": "cheerful"})

    assert ack["ok"] is False
    assert "speech_style" in ack["error"]
    assert "not supported" in ack["error"]
    assert "_FakeTTSHandler" in ack["error"]  # names the active backend


def test_speech_style_no_tts_handler_returns_clear_error(tmp_path):
    bc = _make_brain_control(tmp_path, tts_handler=None)

    ack = bc._config_set({"speech_style": "cheerful"})

    assert ack["ok"] is False
    assert "unavailable" in ack["error"]


def test_speech_style_config_get_reports_current_value(tmp_path):
    captured: list = []
    pcm_bytes = np.zeros(3000, dtype="<i2").tobytes()
    server = _CapturingServer(captured, pcm_bytes)
    try:
        handler = _make_remote_speech_handler(server.base_url)
        bc = _make_brain_control(tmp_path, tts_handler=handler)

        assert bc._config_state()["speech_style"] == handler.instructions  # the boot-time default

        bc._config_set({"speech_style": "slow and deliberate"})

        assert bc._config_state()["speech_style"] == "slow and deliberate"
    finally:
        server.close()


# ── voice: same unsupported-backend discipline as speech_style ─────────────
#
# Broadening `s2s_pipeline.py`'s tts_handler resolution to any TTS backend
# (not just pocket) means `_set_voice` can now be reached with a handler
# that has no pocket-style voice-state API at all. `remote-speech`'s `.model`
# is a plain string like "tts-1", not an object with `get_state_for_audio_
# prompt` -- but it DOES support voice switching, per-request against the
# remote server (see the `_set_voice_remote_*` tests below). A backend with
# NEITHER API (kokoro, qwen3) must still fail loudly and name itself, rather
# than producing a misleading "unknown voice" that blames the name instead
# of the backend.


class _FakeKokoroHandler:
    """Stands in for a backend with neither the pocket-style voice-state API
    (`.model.get_state_for_audio_prompt`) nor the remote-speech capability
    surface (`base_url` + a settable `voice`) -- `_set_voice` must still
    reject it, naming the backend."""

    def __init__(self, voice=None):
        self.voice = voice
        self.model = "kokoro-v1"  # a plain string, same shape as remote-speech's .model


def test_set_voice_unsupported_backend_returns_clear_error_not_unknown_voice(tmp_path):
    handler = _FakeKokoroHandler(voice="af_bella")

    bc = _make_brain_control(tmp_path, tts_handler=handler)

    ok, error = bc._set_voice("af_sky")

    assert ok is False
    assert "not supported" in error
    assert "_FakeKokoroHandler" in error  # names the active backend
    assert "unknown voice" not in error  # must not misdirect blame onto the name


# ── voice: remote-speech backend (the fix) ──────────────────────────────────


def test_set_voice_remote_preset_switch_updates_handler_and_config_state(tmp_path, monkeypatch):
    handler = _make_remote_speech_handler("http://127.0.0.1:1")  # never dialed -- switching must not probe
    bc = _make_brain_control(tmp_path, tts_handler=handler)
    monkeypatch.setattr(bc, "_predefined_voices", lambda: ["jean", "javert"])

    ack = bc._config_set({"voice": "javert"})

    assert ack["ok"] is True
    assert handler.voice == "javert"
    assert ack["voice"] == "javert"  # `_state_payload` reads `tts_handler.voice`
    assert bc._config_state()["voice"] == "javert"


def test_set_voice_remote_switch_updates_pocket_fallback_seam(tmp_path, monkeypatch):
    """The pocket fallback must follow a voice switch too, so a mid-
    conversation failover speaks the newly chosen voice, not the one it was
    built with. Forces the fallback into existence first (the way a real
    failover would) so the switch has to invalidate/rebuild it, not just
    write kwargs nobody will read again."""
    handler = _make_remote_speech_handler("http://127.0.0.1:1")
    bc = _make_brain_control(tmp_path, tts_handler=handler)
    monkeypatch.setattr(bc, "_predefined_voices", lambda: ["jean", "javert"])
    handler._pocket_instance()
    assert handler._pocket is not None

    ok, error = bc._set_voice("javert")

    assert (ok, error) == (True, "")
    assert handler._pocket_kwargs["voice"] == "javert"
    assert handler._pocket is None  # invalidated -- rebuilt with the new voice on next use


def test_set_voice_remote_custom_voice_returns_honest_error(tmp_path, monkeypatch):
    handler = _make_remote_speech_handler("http://127.0.0.1:1")
    bc = _make_brain_control(tmp_path, tts_handler=handler)
    monkeypatch.setattr(bc, "_predefined_voices", lambda: ["jean"])

    ok, error = bc._set_voice("my_cloned_voice")

    assert ok is False
    assert "not available" in error
    assert "remote" in error.lower()
    assert handler.voice is None  # unchanged
    assert handler._pocket is None  # never even attempted to build/probe a fallback


def test_set_voice_remote_rejects_path_traversal_name(tmp_path, monkeypatch):
    handler = _make_remote_speech_handler("http://127.0.0.1:1")
    bc = _make_brain_control(tmp_path, tts_handler=handler)
    monkeypatch.setattr(bc, "_predefined_voices", lambda: ["jean"])

    ok, error = bc._set_voice("../../etc/passwd")

    assert ok is False
    assert error == "unknown voice: ../../etc/passwd"
    assert handler.voice is None


# ── voice choice persistence ────────────────────────────────────────────
#
# `config_set {voice}` worked but was in-memory only: every restart reverted
# to the argv `--pocket_tts_voice`/remote default, so a chosen voice never
# survived past the next service restart. These cover the sidecar file that
# fixes it, mirroring the persona persistence tests above.


def _voice_choice_env(tmp_path, monkeypatch):
    path = tmp_path / "voice_dir" / "voice_choice.json"
    monkeypatch.setenv("VOICE_CHOICE_FILE", str(path))
    return path


def test_voice_switch_persists_the_choice(tmp_path, monkeypatch):
    path = _voice_choice_env(tmp_path, monkeypatch)
    handler = _make_remote_speech_handler("http://127.0.0.1:1")
    bc = _make_brain_control(tmp_path, tts_handler=handler)
    monkeypatch.setattr(bc, "_predefined_voices", lambda: ["jean", "javert"])

    ack = bc._config_set({"voice": "javert"})

    assert ack["ok"] is True
    assert json.loads(path.read_text())["voice"] == "javert"


def test_voice_choice_survives_a_restart(tmp_path, monkeypatch):
    """Construct a SECOND BrainControl (same env) with a fresh remote
    handler whose voice is the argv default -- the boot-apply site in
    `__init__` must bring it to the stored voice, silently (no audition)."""
    _voice_choice_env(tmp_path, monkeypatch)
    # Patched on the CLASS, not the instance: the boot-apply site runs
    # inside __init__, before a per-instance monkeypatch could ever attach.
    monkeypatch.setattr(brain_control.BrainControl, "_predefined_voices", lambda self: ["jean", "javert"])

    handler = _make_remote_speech_handler("http://127.0.0.1:1")
    bc = _make_brain_control(tmp_path, tts_handler=handler)
    ok, error = bc._set_voice("javert")
    assert (ok, error) == (True, "")

    revived_handler = _make_remote_speech_handler("http://127.0.0.1:1")
    assert revived_handler.voice is None  # argv default, unmodified so far
    tts_queue = Queue()
    revived = _make_brain_control(tmp_path, tts_handler=revived_handler, tts_queue=tts_queue)

    assert revived_handler.voice == "javert"
    assert tts_queue.empty()  # no audition queued -- boot must never speak


def test_voice_choice_already_matching_argv_skips_the_switch(tmp_path, monkeypatch):
    """When the stored voice already equals the argv default, boot must not
    even attempt a switch (no-op, not a same-value round trip)."""
    _voice_choice_env(tmp_path, monkeypatch)
    monkeypatch.setattr(brain_control.BrainControl, "_predefined_voices", lambda self: ["jean", "javert"])
    handler = _make_remote_speech_handler("http://127.0.0.1:1")
    bc = _make_brain_control(tmp_path, tts_handler=handler)
    bc._set_voice("javert")

    tts_queue = Queue()
    handler2 = _make_remote_speech_handler("http://127.0.0.1:1")
    handler2.voice = "javert"  # argv already matches the stored choice
    called = []
    monkeypatch.setattr(brain_control.BrainControl, "_set_voice", lambda self, *a, **kw: (called.append(1), (True, ""))[1])
    _make_brain_control(tmp_path, tts_handler=handler2, tts_queue=tts_queue)

    assert called == []


def test_corrupt_voice_choice_file_does_not_raise(tmp_path, monkeypatch, caplog):
    path = _voice_choice_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text('{"voice": "javert"')  # truncated mid-write
    handler = _make_remote_speech_handler("http://127.0.0.1:1")

    with caplog.at_level(logging.WARNING, logger=brain_control.__name__):
        bc = _make_brain_control(tmp_path, tts_handler=handler)

    assert handler.voice is None  # argv default kept
    assert "unreadable voice choice" in caplog.text


def test_stored_voice_invalid_for_backend_keeps_default_and_does_not_delete_file(tmp_path, monkeypatch, caplog):
    path = _voice_choice_env(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"voice": "af_bella"}))
    handler = _FakeKokoroHandler(voice="af_sky")  # backend without switching support

    with caplog.at_level(logging.WARNING, logger=brain_control.__name__):
        bc = _make_brain_control(tmp_path, tts_handler=handler)

    assert handler.voice == "af_sky"  # argv default kept, boot never crashed
    assert "failed to apply at boot" in caplog.text
    assert json.loads(path.read_text())["voice"] == "af_bella"  # file left alone


def test_load_voice_choice_helpers(tmp_path, monkeypatch, caplog):
    path = _voice_choice_env(tmp_path, monkeypatch)

    assert brain_control.load_voice_choice() == ""  # missing file

    path.parent.mkdir(parents=True)
    path.write_text("not json")
    with caplog.at_level(logging.WARNING, logger=brain_control.__name__):
        assert brain_control.load_voice_choice() == ""

    path.write_text("[]")
    assert brain_control.load_voice_choice() == ""

    path.write_text(json.dumps({"voice": "x" * (brain_control.VOICE_CHOICE_MAX_CHARS + 1)}))
    assert brain_control.load_voice_choice() == ""

    brain_control.save_voice_choice("javert")
    assert brain_control.load_voice_choice() == "javert"

    brain_control.save_voice_choice("")
    assert not path.exists()


# ── armed tool names published alongside the count ──────────────────────


def test_state_payload_carries_armed_tool_names(tmp_path, monkeypatch):
    """`_state_payload` used to expose only `tools_armed` (a count) -- the
    panel wants the actual names, both at boot and after a live reload."""
    monkeypatch.setattr(
        brain_control.voice_tools,
        "get_tool_defs",
        lambda: [{"name": "get_weather"}, {"name": "web_search"}],
    )

    bc = _make_brain_control(tmp_path)

    payload = bc._state_payload()
    assert payload["tools"] == ["get_weather", "web_search"]
    assert payload["tools_armed"] == 2


def test_reload_tools_config_set_updates_armed_tool_names(tmp_path, monkeypatch):
    bc = _make_brain_control(tmp_path)

    monkeypatch.setattr(
        brain_control.voice_tools,
        "get_tool_defs",
        lambda: [{"name": "home_assistant"}],
    )

    ack = bc._config_set({"reload_tools": True})

    assert ack["ok"] is True
    assert ack["tools"] == ["home_assistant"]
    assert ack["tools_armed"] == 1
