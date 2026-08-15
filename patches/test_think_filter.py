"""Unit tests for think_filter.py (streaming <think>...</think> suppressor).

Run from repo root: python3 -m pytest patches/test_think_filter.py -v

think_filter.py is dependency-free (no speech_to_speech, no non-stdlib
imports), so these tests need no stubs and no installed package.
"""

from __future__ import annotations

import pytest

from patches.think_filter import ThinkTagFilter


def _feed_all(chunks: list[str]) -> str:
    """Feed each chunk in order, then flush; join everything emitted."""
    f = ThinkTagFilter()
    out = "".join(f.feed(c) for c in chunks)
    out += f.flush()
    return out


# ── pass-through ──────────────────────────────────────────────────────────


def test_pass_through_untouched():
    assert _feed_all(["hello there, nothing to see here"]) == "hello there, nothing to see here"


# ── single span mid-text ──────────────────────────────────────────────────


def test_single_span_mid_text():
    assert _feed_all(["before <think>secret reasoning</think> after"]) == "before  after"


# ── empty think block + leading-newline swallow after close ──────────────


def test_empty_think_block_with_trailing_newlines_swallowed():
    assert _feed_all(["<think>\n\n</think>\n\nHi"]) == "Hi"


# ── span split across every split position ────────────────────────────────


@pytest.mark.parametrize("split", range(len("<think>abc</think>done") + 1))
def test_span_split_across_every_position(split):
    text = "<think>abc</think>done"
    assert _feed_all([text[:split], text[split:]]) == "done"


# ── false-prefix: looks like the start of a tag but isn't ────────────────


def test_false_prefix_passes_through_whole():
    assert _feed_all(["<this is not a tag"]) == "<this is not a tag"


def test_false_prefix_passes_through_when_split():
    assert _feed_all(["<thi", "s is not a tag"]) == "<this is not a tag"


# ── multiple spans ──────────────────────────────────────────────────────


def test_multiple_spans():
    text = "before<think>x</think>mid<think>y</think>after"
    assert _feed_all([text]) == "beforemidafter"


# ── unclosed think at stream end: suppressed, flush() returns nothing ────


def test_unclosed_think_suppresses_remainder_and_flush_is_empty():
    f = ThinkTagFilter()
    assert f.feed("<think>reasoning that never closes") == ""
    assert f.flush() == ""


# ── flush() surfaces an innocent buffered tag-prefix ──────────────────────


def test_flush_returns_innocent_buffered_prefix():
    f = ThinkTagFilter()
    emitted = f.feed("text ends with <thi")
    assert emitted == "text ends with "
    assert f.flush() == "<thi"
