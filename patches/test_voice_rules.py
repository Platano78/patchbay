"""Unit tests for voice_rules.py (pipeline-invariant system rules).

Run from repo root: python3 -m pytest patches/test_voice_rules.py -v

voice_rules.py is dependency-free (no speech_to_speech, no non-stdlib
imports), so these tests need no stubs and no installed package.
"""

from __future__ import annotations

import copy

from patches.voice_rules import DEFAULT_RULES, _parse, apply_system_rules

RULES = "Always answer completely and thoroughly."


# ── system message: str content ───────────────────────────────────────────


def test_system_str_content_appended():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]

    result = apply_system_rules(messages, RULES)

    assert result[0]["content"] == "You are helpful.\n\n" + RULES
    assert result[1] == {"role": "user", "content": "hi"}


# ── system message: list-of-parts content ─────────────────────────────────


def test_system_parts_list_content_appended():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": "hi"},
    ]

    result = apply_system_rules(messages, RULES)

    assert result[0]["content"] == [
        {"type": "text", "text": "You are helpful."},
        {"type": "text", "text": "\n\n" + RULES},
    ]


# ── no system message: inserted at index 0 ────────────────────────────────


def test_no_system_message_inserts_at_index_zero():
    messages = [{"role": "user", "content": "hi"}]

    result = apply_system_rules(messages, RULES)

    assert result[0] == {"role": "system", "content": RULES}
    assert result[1] == {"role": "user", "content": "hi"}
    assert len(result) == 2


# ── empty rules ("off") is a no-op identity ───────────────────────────────


def test_empty_rules_is_identity_no_op():
    messages = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]

    result = apply_system_rules(messages, "")

    assert result is messages


# ── input is never mutated ────────────────────────────────────────────────


def test_input_not_mutated_str_content():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    original = copy.deepcopy(messages)

    apply_system_rules(messages, RULES)

    assert messages == original


def test_input_not_mutated_list_content():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": "hi"},
    ]
    original = copy.deepcopy(messages)

    apply_system_rules(messages, RULES)

    assert messages == original


def test_input_not_mutated_no_system():
    messages = [{"role": "user", "content": "hi"}]
    original = copy.deepcopy(messages)

    apply_system_rules(messages, RULES)

    assert messages == original


# ── idempotence: applying twice == applying once ──────────────────────────


def test_idempotent_str_content():
    messages = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]

    once = apply_system_rules(messages, RULES)
    twice = apply_system_rules(once, RULES)

    assert twice == once
    assert twice[0]["content"].count(RULES) == 1


def test_idempotent_parts_list_content():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": "hi"},
    ]

    once = apply_system_rules(messages, RULES)
    twice = apply_system_rules(once, RULES)

    assert twice == once
    assert len(twice[0]["content"]) == 2


# ── VOICE_SYSTEM_RULES env parsing (_parse unit tests) ────────────────────


def test_parse_unset_is_default():
    assert _parse(None) == DEFAULT_RULES


def test_parse_blank_is_default():
    assert _parse("   ") == DEFAULT_RULES


def test_parse_off_disables():
    assert _parse("off") == ""
    assert _parse("  Off  ") == ""
    assert _parse("OFF") == ""


def test_parse_custom_used_verbatim():
    assert _parse("  Be concise.  ") == "Be concise."
