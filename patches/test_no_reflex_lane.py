"""The reflex lane was removed (see CHANGELOG, Unreleased); nothing may bring it back by accident.

* ``TestPipelineBuildIgnoresVoiceReflex`` drives the REAL call path in a SUBPROCESS against the real
  framework, the way ``test_vocab_snap.TestRealHookPoint`` does: ``s2s_pipeline._build_pipeline_handlers``
  with ``VOICE_REFLEX=1`` in the environment must build, must not import ``speech_to_speech.reflex_lane``,
  and must hand the LM the ParrotGate's output queue. Skips when the framework is not importable.
* ``test_apply_sh_ships_no_reflex_file`` needs no framework.

Run from repo root (framework venv): python3 -m pytest patches/test_no_reflex_lane.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBE = textwrap.dedent(
    """
    import json, sys
    from queue import Queue
    from threading import Event
    from types import SimpleNamespace
    from unittest import mock

    # patches/ is not installed into the framework in a test run: serve the modules under test
    # under the names s2s_pipeline imports them by (an installed copy may be stale).
    import conftest; conftest.alias_unapplied_pack_modules()
    import speech_to_speech
    import patches.vocab_snap as vs
    import patches.parrot_lane as pl
    sys.modules["speech_to_speech.vocab_snap"] = vs
    speech_to_speech.vocab_snap = vs
    sys.modules["speech_to_speech.parrot_lane"] = pl
    speech_to_speech.parrot_lane = pl
    from patches import s2s_pipeline as sp

    from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig
    from speech_to_speech.LLM.chat import Chat
    from openai.types.realtime import RealtimeSessionCreateRequest

    class Dummy:
        pipeline_index = 0
        def __init__(self, *a, **k): pass

    seen = {}
    def fake_llm(module_kwargs, stop_event, queue_in, *rest):
        seen["lm_queue_in"] = queue_in
        return Dummy()

    text_output_queue = Queue()
    runtime_config = RuntimeConfig(chat=Chat(10), session=RealtimeSessionCreateRequest(type="realtime", instructions="x"))
    kwargs = {k: SimpleNamespace() for k in (
        "vad_handler_kwargs", "whisper_stt_handler_kwargs", "faster_whisper_stt_handler_kwargs",
        "paraformer_stt_handler_kwargs", "mlx_audio_whisper_stt_handler_kwargs",
        "parakeet_tdt_stt_handler_kwargs", "language_model_handler_kwargs",
        "responses_api_language_model_handler_kwargs", "chat_tts_handler_kwargs",
        "facebook_mms_tts_handler_kwargs", "pocket_tts_handler_kwargs", "kokoro_tts_handler_kwargs",
        "qwen3_tts_handler_kwargs", "remote_speech_tts_handler_kwargs")}
    with mock.patch.object(sp, "VADHandler", Dummy), \\
         mock.patch.object(sp, "get_stt_handler", lambda *a, **k: Dummy()), \\
         mock.patch.object(sp, "get_llm_handler", fake_llm), \\
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

    parrots = [h for h in handlers if isinstance(h, pl.ParrotGate)]
    print("RESULT " + json.dumps({
        "built": True,
        "parrot_gates": len(parrots),
        "lm_reads_parrot_output": len(parrots) == 1 and seen["lm_queue_in"] is parrots[0].queue_out,
        "reflex_modules": sorted(m for m in sys.modules if "reflex" in m),
        "handler_classes": [type(h).__name__ for h in handlers],
    }))
    """
)


def _framework_importable():
    p = subprocess.run(
        [sys.executable, "-c", "import speech_to_speech, torch, transformers, nltk"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=120,
    )
    return p.returncode == 0


@pytest.mark.skipif(not _framework_importable(), reason="the real speech_to_speech framework is not installed in this venv")
class TestPipelineBuildIgnoresVoiceReflex:
    def test_voice_reflex_1_builds_without_importing_reflex_lane(self):
        env = dict(os.environ, VOICE_REFLEX="1")
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE], cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True, timeout=180
        )
        assert proc.returncode == 0, proc.stderr[-1500:]
        line = next(l for l in proc.stdout.splitlines() if l.startswith("RESULT "))
        res = json.loads(line[len("RESULT "):])
        assert res["built"] is True
        assert res["parrot_gates"] == 1
        # the LM's input queue IS the ParrotGate's output queue: nothing sits between them
        assert res["lm_reads_parrot_output"] is True
        assert "speech_to_speech.reflex_lane" not in res["reflex_modules"]
        assert res["reflex_modules"] == []
        assert not any("Reflex" in n for n in res["handler_classes"])


def test_apply_sh_ships_no_reflex_file():
    assert "reflex" not in (_REPO_ROOT / "patches" / "apply.sh").read_text().lower()
    assert not (_REPO_ROOT / "patches" / "reflex_lane.py").exists()
