# Most tests import the patch pack's own runtime dependencies, which live in the framework venv
# that setup.sh builds -- not in a system python3. Without them pytest would print one import
# traceback per test module, which reads as "this project is broken". Stop with one sentence instead.
import importlib.util

import pytest

_MISSING = [m for m in ("httpx", "numpy", "openai", "pydantic") if importlib.util.find_spec(m) is None]


# Patch-pack modules that s2s_pipeline.py imports as `speech_to_speech.<...>` but that a framework
# install which has not had apply.sh run since they were added does not contain yet. Tests that
# drive the real s2s_pipeline against the real framework (mostly in subprocess probes, which cannot
# see this file's pytest hooks) call alias_unapplied_pack_modules() first, so a stale install does
# not fail them with ModuleNotFoundError. A module the install already has is left alone.
_PACK_MODULES = (
    (
        "speech_to_speech.arguments_classes.remote_transcription_stt_arguments",
        "patches.remote_transcription_stt_arguments",
    ),
    (
        "speech_to_speech.STT.remote_transcription_stt_handler",
        "patches.remote_transcription_stt_handler",
    ),
)


def alias_unapplied_pack_modules():
    import importlib
    import sys

    for installed_name, pack_name in _PACK_MODULES:
        try:
            if importlib.util.find_spec(installed_name) is not None:
                continue
        except (ImportError, ValueError):
            pass  # parent package missing/stubbed: alias anyway
        sys.modules[installed_name] = importlib.import_module(pack_name)


def pytest_configure(config):
    if _MISSING:
        pytest.exit(
            "These tests run in the framework venv, and this python3 is missing "
            + ", ".join(_MISSING)
            + ". Run ./setup.sh, then: ~/speech-to-speech-main/.venv/bin/python3 -m pytest patches/ examples/ webclient/",
            returncode=4,
        )
    # Dependency-light pack modules that other pack modules import as
    # `speech_to_speech.<name>`: stubbed-package tests need them importable
    # before apply.sh has put them into an install. Real module, never a stub.
    import importlib
    import sys

    for _name in ("filler_clips", "turn_classifier", "voice_design"):
        sys.modules.setdefault(f"speech_to_speech.{_name}", importlib.import_module(f"patches.{_name}"))
