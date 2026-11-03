"""Tests for vocab_snap.py (post-ASR vocabulary snap).

Two layers:

* ``snap()`` / loader unit tests -- stdlib only, always run.
* ``TestRealHookPoint`` -- drives the REAL call path in a SUBPROCESS against the real
  framework: ``s2s_pipeline._build_pipeline_handlers`` builds the handler chain, the
  ``TranscriptionNotifier`` it contains receives a real ``Transcription`` message, and the
  test reads what the brain (``runtime_config.chat`` + the ``GenerateResponseRequest`` it
  yields) and the cockpit (``TranscriptionCompletedEvent`` on ``text_output_queue``) get.
  Skips when the framework is not importable.

Run from repo root (framework venv): python3 -m pytest patches/test_vocab_snap.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from patches import vocab_snap as v

_REPO_ROOT = Path(__file__).resolve().parent.parent

VOCAB = "VoxCPM | vox epm\nPatchbay\nHermes\nFaulkner\nGalactus\nhomeserver\nParakeet-TDT\nfork-pi | fork pie\n"
TERMS = v.parse_terms(VOCAB)


def _snap(text):
    return v.snap(text, TERMS)[0]


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("Vox EPM", "VoxCPM"),  # alias
        ("vox cpm", "VoxCPM"),
        ("patch bay", "Patchbay"),
        ("Patchbay", "Patchbay"),
        ("parakeet tdt", "Parakeet-TDT"),
        ("fork pie", "fork-pi"),  # alias
        ("home server", "homeserver"),  # two common words, but exactly the term once normalised
        ("falkner", "Faulkner"),
        ("Is Vox EPM running?", "Is VoxCPM running?"),
        ("ask Vox EPM's voice", "ask VoxCPM's voice"),  # possessive survives
        ("I heard patch bay, and Faulkner", "I heard Patchbay, and Faulkner"),
        ("Vox EPM and vox cpm", "VoxCPM and VoxCPM"),
    ],
)
def test_positive_snaps(heard, expected):
    assert _snap(heard) == expected


@pytest.mark.parametrize(
    "said",
    [
        # required negatives
        "what's the weather in Boston",
        "box of chocolates",
        "turn on the patch light",
        "her mess is everywhere",
        "I need a falconer",
        # ordinary conversation
        "hello how are you today",
        "can you set a timer for ten minutes",
        "what time is it in Tokyo",
        "tell me a story about a dragon",
        "the fork is on the table",
        "I like apple pie",
        "play some music please",
        "my brother is coming over",
        "the galaxy is huge",
        "what is the capital of France",
        "remind me to call the doctor",
        "how do I patch a hole in drywall",
        "the bay was calm this morning",
        "she has a mess of papers",
        "a parakeet is a small bird",
        "go to the local pharmacy",
        "vocal cords hurt",
        "read me my notes",
        "hermit crabs are cute",
        # already canonical / punctuation between tokens
        "Is VoxCPM running?",
        "Hermes and Faulkner",
        "Vox, EPM is odd",
    ],
)
def test_negatives_are_untouched(said):
    assert _snap(said) == said


def test_exact_term_with_different_case_is_canonicalised_but_canonical_is_not_logged():
    assert v.snap("Voxcpm", TERMS) == ("VoxCPM", [("Voxcpm", "VoxCPM")])
    assert v.snap("voxcpm", TERMS) == ("voxcpm", [])  # lowercase single token: plain speech, left alone
    assert v.snap("VoxCPM", TERMS) == ("VoxCPM", [])


def test_unlisted_mishearing_still_snaps_fuzzily():
    terms = v.parse_terms("VoxCPM\nfork-pi\n")
    assert v.snap("Vox EPM", terms)[0] == "VoxCPM"  # ratio 0.833, no alias needed
    assert v.snap("fork pie", terms)[0] == "fork pie"  # substring case: only an alias can do this


def test_changes_report_the_original_span():
    assert v.snap("use Vox EPM now", TERMS)[1] == [("Vox EPM", "VoxCPM")]


def test_short_terms_are_never_loaded():
    assert v.parse_terms("abc\nGo\nab-c\n") == []


def test_ambiguous_fuzzy_match_does_not_snap():
    terms = v.parse_terms("Hermesa\nHermesb\n")
    assert v.snap("Hermesc", terms)[0] == "Hermesc"


def test_truncation_of_a_term_does_not_snap():
    assert _snap("fork") == "fork"


_D = v.parse_terms("Qwen\nGemma\nKokoro\nTailscale\nhomeserver\nHerald\nCrate\n")


@pytest.mark.parametrize(
    "said",
    [
        # D1: a fuzzy span must not swallow a neighbour word
        "Qwen ai is cool",
        "I use Gemma ai daily",
        "the homeserver x box",
        # D2: digits and suffixes survive
        "Qwen 3 is good",
        "Qwen 2.5 is out",
        "Qwen3 is out",
        "Heralds of the king",
        "gemmas are stones",
        # D4: near-miss common words
        "the gamma ray burst",
        "crater on the moon",
        # D5: plain lowercase speech equal to a term bar case
        "put the crate of beer",
        "the herald announced",
    ],
)
def test_adversarial_regressions_are_untouched(said):
    assert v.snap(said, _D)[0] == said


def test_capitalised_single_token_still_gets_canonical_spelling():
    assert v.snap("the Herald announced", _D)[0] == "the Herald announced"
    assert v.snap("the HERALD announced", _D)[0] == "the Herald announced"
    assert v.snap("home server", _D)[0] == "homeserver"


def test_alias_is_exact_only_and_rewrites_to_canonical():
    terms = v.parse_terms("VoxCPM | vox epm | vox c pm\n")
    assert v.snap("run vox epm now", terms)[0] == "run VoxCPM now"
    assert v.snap("run Vox C PM now", terms)[0] == "run VoxCPM now"
    assert v.snap("vox epmm", terms)[0] == "vox epmm"  # an alias is never fuzzy


def test_alias_line_parsing():
    assert v.parse_terms("fork-pi | fork pie | x\n") == [("fork-pi", "forkpi", False), ("fork-pi", "forkpie", True)]


def test_bom_is_ignored(tmp_path, monkeypatch):
    assert v.parse_terms("\ufeffVoxCPM\n") == [("VoxCPM", "voxcpm", False)]
    assert v.parse_terms("\ufeff# a comment\nPatchbay\n") == [("Patchbay", "patchbay", False)]
    f = tmp_path / "vocab.txt"
    f.write_bytes(b"\xef\xbb\xbfVoxCPM | vox epm\n")
    store = v.VocabStore()
    monkeypatch.setenv("VOICE_VOCAB_FILE", str(f))
    assert store.terms() == [("VoxCPM", "voxcpm", False), ("VoxCPM", "voxepm", True)]


def test_parse_terms_comments_blanks_dupes():
    assert v.parse_terms("# c\n\n  VoxCPM  \nvoxcpm\nPatchbay # no inline comments\n") == [
        ("VoxCPM", "voxcpm", False),
        ("Patchbay # no inline comments", "patchbaynoinlinecomments", False),
    ]


# ── loader ────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_store(monkeypatch):
    store = v.VocabStore()
    monkeypatch.setattr(v, "_store", store)
    return store


def test_missing_file_is_off_and_silent(fresh_store, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("VOICE_VOCAB_FILE", str(tmp_path / "nope.txt"))
    with caplog.at_level("DEBUG"):
        assert v.snap_text("Vox EPM") == "Vox EPM"
    assert caplog.records == []


def test_off_disables_even_when_default_file_exists(fresh_store, monkeypatch, tmp_path):
    f = tmp_path / "vocab.txt"
    f.write_text(VOCAB)
    monkeypatch.setenv("VOICE_VOCAB_FILE", "OFF")
    assert v.snap_text("Vox EPM") == "Vox EPM"
    monkeypatch.setenv("VOICE_VOCAB_FILE", str(f))
    assert v.snap_text("Vox EPM") == "VoxCPM"


def test_reload_on_mtime_change(fresh_store, monkeypatch, tmp_path):
    f = tmp_path / "vocab.txt"
    f.write_text("Patchbay\n")
    monkeypatch.setenv("VOICE_VOCAB_FILE", str(f))
    assert v.snap_text("Vox EPM") == "Vox EPM"
    f.write_text("Patchbay\nVoxCPM\n")
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 5))
    assert v.snap_text("Vox EPM") == "VoxCPM"


def test_snap_logs_original_at_info(fresh_store, monkeypatch, tmp_path, caplog):
    f = tmp_path / "vocab.txt"
    f.write_text(VOCAB)
    monkeypatch.setenv("VOICE_VOCAB_FILE", str(f))
    with caplog.at_level("INFO", logger="patches.vocab_snap"):
        v.snap_text("Vox EPM")
    assert "vocab snap: 'Vox EPM' -> 'VoxCPM'" in caplog.text


def test_snap_fails_open(fresh_store, monkeypatch):
    monkeypatch.setattr(v, "snap", lambda *a, **k: 1 / 0)
    assert v.snap_text("Vox EPM") == "Vox EPM"


def test_partials_and_non_text_pass_through_untouched(fresh_store, monkeypatch, tmp_path):
    f = tmp_path / "vocab.txt"
    f.write_text(VOCAB)
    monkeypatch.setenv("VOICE_VOCAB_FILE", str(f))

    class Partial:
        tag = "partial_transcription"
        text = "Vox EPM"

    p = Partial()
    assert v.snap_stt_output(p) is p
    assert v.snap_stt_output(None) is None
    assert v.snap_stt_output("Vox EPM") == "VoxCPM"


# ── the real call path ────────────────────────────────────────────────

_PROBE = textwrap.dedent(
    """
    import os, sys, json
    from queue import Queue
    from threading import Event
    from types import SimpleNamespace
    from unittest import mock

    # patches/ is not installed into the framework in a test run: serve it under the name
    # s2s_pipeline imports it by.
    import conftest; conftest.alias_unapplied_pack_modules()
    import patches.vocab_snap as vs
    sys.modules["speech_to_speech.vocab_snap"] = vs
    import speech_to_speech
    speech_to_speech.vocab_snap = vs
    from patches import s2s_pipeline as sp

    from speech_to_speech.STT.transcription_notifier import TranscriptionNotifier
    from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig
    from speech_to_speech.LLM.chat import Chat
    from speech_to_speech.pipeline.messages import Transcription, PartialTranscription
    from openai.types.realtime import RealtimeSessionCreateRequest

    class Dummy:
        pipeline_index = 0
        def __init__(self, *a, **k): pass

    text_output_queue = Queue()
    runtime_config = RuntimeConfig(chat=Chat(10), session=RealtimeSessionCreateRequest(type="realtime", instructions="x"))
    kwargs = {k: SimpleNamespace() for k in (
        "vad_handler_kwargs", "whisper_stt_handler_kwargs", "faster_whisper_stt_handler_kwargs",
        "paraformer_stt_handler_kwargs", "mlx_audio_whisper_stt_handler_kwargs",
        "parakeet_tdt_stt_handler_kwargs", "language_model_handler_kwargs",
        "responses_api_language_model_handler_kwargs", "chat_tts_handler_kwargs",
        "facebook_mms_tts_handler_kwargs", "pocket_tts_handler_kwargs", "kokoro_tts_handler_kwargs",
        "qwen3_tts_handler_kwargs", "remote_speech_tts_handler_kwargs")}
    # Only the model-loading handlers are stood in for; the notifier, ParrotGate and the builder are real.
    with mock.patch.object(sp, "VADHandler", Dummy), \\
         mock.patch.object(sp, "get_stt_handler", lambda *a, **k: Dummy()), \\
         mock.patch.object(sp, "get_llm_handler", lambda *a, **k: Dummy()), \\
         mock.patch.object(sp, "get_tts_handler", lambda *a, **k: Dummy()), \\
         mock.patch("speech_to_speech.LLM.lm_output_processor.LMOutputProcessor", Dummy):
        handlers = sp._build_pipeline_handlers(
            stop_event=Event(), should_listen=Event(),
            recv_audio_chunks_queue=Queue(), spoken_prompt_queue=Queue(), stt_output_queue=Queue(),
            text_prompt_queue=Queue(), lm_response_queue=Queue(), lm_processed_queue=Queue(),
            send_audio_chunks_queue=Queue(), text_output_queue=text_output_queue,
            transcription_notifier_setup={"text_output_queue": text_output_queue,
                                          "should_listen": Event(), "runtime_config": runtime_config},
            module_kwargs=SimpleNamespace(), **kwargs)

    notifier = next(h for h in handlers if isinstance(h, TranscriptionNotifier))
    spoken = json.loads(os.environ["PROBE_TEXTS"])
    out = []
    for t in spoken:
        gen = notifier.process(Transcription(text=t, language_code="en", turn_id="t1", turn_revision=1))
        reqs = list(gen)
        events = []
        while not text_output_queue.empty():
            events.append(text_output_queue.get())
        out.append({"chat_last": runtime_config.chat.buffer[-1].content[0].text,
                    "event": [e.transcript for e in events if hasattr(e, "transcript")],
                    "requests": len(reqs)})
    # a partial must come through unsnapped
    list(notifier.process(PartialTranscription(text="Vox EPM", turn_id="t1", turn_revision=1)))
    partial = [e.delta for e in iter(lambda: None if text_output_queue.empty() else text_output_queue.get(), None)]
    print("RESULT " + json.dumps({"turns": out, "partial": partial}))
    """
)


def _run_probe(vocab_env, texts):
    env = dict(os.environ, VOICE_VOCAB_FILE=vocab_env, PROBE_TEXTS=__import__("json").dumps(texts))
    return subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=180
    )


def _framework_importable():
    p = subprocess.run(
        [sys.executable, "-c", "import speech_to_speech, torch, transformers, nltk"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    return p.returncode == 0


@pytest.mark.skipif(not _framework_importable(), reason="the real speech_to_speech framework is not installed in this venv")
class TestRealHookPoint:
    def _result(self, vocab_env, texts):
        import json

        proc = _run_probe(vocab_env, texts)
        assert proc.returncode == 0, proc.stderr[-1500:]
        line = next(l for l in proc.stdout.splitlines() if l.startswith("RESULT "))
        return json.loads(line[len("RESULT "):])

    def test_brain_and_cockpit_both_get_the_snapped_transcript(self, tmp_path):
        f = tmp_path / "vocab.txt"
        f.write_text(VOCAB)
        res = self._result(str(f), ["what is Vox EPM doing", "box of chocolates"])
        first, second = res["turns"]
        # what the brain reads (runtime_config.chat) and what the cockpit displays
        assert first["chat_last"] == "what is VoxCPM doing"
        assert first["event"] == ["what is VoxCPM doing"]
        assert first["requests"] == 1  # the turn still reaches the LM
        assert second["chat_last"] == second["event"][0] == "box of chocolates"
        assert res["partial"] == ["Vox EPM"]  # live partials are not rewritten

    def test_no_vocab_file_changes_nothing(self, tmp_path):
        res = self._result(str(tmp_path / "absent.txt"), ["what is Vox EPM doing"])
        assert res["turns"][0]["chat_last"] == "what is Vox EPM doing"
        assert res["turns"][0]["event"] == ["what is Vox EPM doing"]
