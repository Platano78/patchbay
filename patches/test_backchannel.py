"""Tests for patches/backchannel.py — proven negative controls included."""

from __future__ import annotations

import ast
import pathlib

import pytest

from patches.backchannel import (
    PHRASES,
    MAX_TOKENS,
    normalize,
    is_backchannel,
)


# ── acknowledgements ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "yeah", "Yeah.", "Mm-hmm.", "Uh-huh", "uh huh", "OK", "okay",
    "Yeah, okay.", "oh i see", "Oh, I see. Okay.", "right right",
    "yeah yeah yeah", "That's right.", "That’s right", "sounds good",
    "go on", "mhm yeah",
])
def test_acknowledgements_are_backchannels(text: str) -> None:
    assert is_backchannel(text) is True


# ── content (not backchannel) ────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "okay so what about Tuesday",
    "yeah but what about Tuesday",
    "what time is it",
    "I", "see", "go",
    "tell me more about that",
])
def test_content_is_not_backchannel(text: str) -> None:
    assert is_backchannel(text) is False


# ── exclusions ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "no", "nah", "nope", "no way", "oh no", "uh uh", "uh-uh",
    "wait", "hold on", "stop",
    "huh", "what", "really", "sorry", "pardon",
    "um", "uh", "so", "well",
    "thanks", "thank you",
])
def test_negations_repairs_and_floor_claims_are_content(text: str) -> None:
    assert is_backchannel(text) is False


# ── digits block tiling ───────────────────────────────────────────────────

def test_digits_block_tiling() -> None:
    assert is_backchannel("yeah 5") is False
    assert is_backchannel("okay 2") is False
    assert is_backchannel("5") is False


# ── token limit boundary ──────────────────────────────────────────────────

def test_token_limit_boundary() -> None:
    assert is_backchannel("yeah okay right sure yep") is True  # 5
    assert is_backchannel("yeah okay right sure yep yeah") is False  # 6


# ── empty / non-text ──────────────────────────────────────────────────────

def test_empty_and_non_text() -> None:
    assert is_backchannel("") is False
    assert is_backchannel("   ") is False
    assert is_backchannel("...") is False
    assert is_backchannel(None) is False
    assert is_backchannel(42) is False


# ── multiword phrases tile as units ───────────────────────────────────────

def test_multiword_phrases_tile_as_units() -> None:
    assert is_backchannel("oh i see") is True
    assert is_backchannel("i") is False
    assert is_backchannel("see") is False


# ── normalize ─────────────────────────────────────────────────────────────

def test_normalize() -> None:
    assert normalize("Mm-hmm, THAT’S right!") == ["mmhmm", "thats", "right"]


# ── stdlib-only check ─────────────────────────────────────────────────────

def test_module_is_stdlib_only() -> None:
    source = pathlib.Path(__file__).parent / "backchannel.py"
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in {"__future__", "re", "typing"}
        elif isinstance(node, ast.ImportFrom):
            assert node.module in {"__future__", "re", "typing"}
