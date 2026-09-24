# Most tests import the patch pack's own runtime dependencies, which live in the framework venv
# that setup.sh builds -- not in a system python3. Without them pytest would print one import
# traceback per test module, which reads as "this project is broken". Stop with one sentence instead.
import importlib.util

import pytest

_MISSING = [m for m in ("httpx", "numpy", "openai", "pydantic") if importlib.util.find_spec(m) is None]


def pytest_configure(config):
    if _MISSING:
        pytest.exit(
            "These tests run in the framework venv, and this python3 is missing "
            + ", ".join(_MISSING)
            + ". Run ./setup.sh, then: ~/speech-to-speech-main/.venv/bin/python3 -m pytest patches/ examples/ webclient/",
            returncode=4,
        )
