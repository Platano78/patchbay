"""Unit test for the current_time voice tool drop-in.

Run from the repo root: python3 -m pytest examples/tools/test_current_time.py -q
"""
from __future__ import annotations

from datetime import datetime

from examples.tools import current_time


def test_output_includes_the_current_year():
    # Without the year a model fills it from its training-era prior.
    assert str(datetime.now().year) in current_time.run()
