"""Unit tests for voice_rules.py (pipeline-invariant system rules).

Run from repo root: python3 -m pytest patches/test_voice_rules.py -v

voice_rules.py is dependency-free (no speech_to_speech, no non-stdlib
imports), so these tests need no stubs and no installed package.
"""

from __future__ import annotations

import copy
from datetime import datetime

from patches.voice_rules import (
    DEFAULT_RULES,
    _parse,
    _parse_date_anchor,
    apply_date_anchor,
    apply_system_rules,
)

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


# ── date anchor ───────────────────────────────────────────────────────────

SAT = datetime(2026, 10, 24, 9, 5)
LINE = "Today's date is Saturday, October 24, 2026."


def test_date_anchor_appended_to_system_message():
    messages = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]

    result = apply_date_anchor(messages, SAT, True)

    assert result[0]["content"] == "You are helpful.\n\n" + LINE
    assert result[1] == {"role": "user", "content": "hi"}


def test_date_anchor_day_is_not_zero_padded():
    result = apply_date_anchor([], datetime(2026, 3, 4), True)

    assert result[0]["content"] == "Today's date is Wednesday, March 4, 2026."


def test_date_anchor_creates_system_message_when_absent():
    result = apply_date_anchor([{"role": "user", "content": "hi"}], SAT, True)

    assert result == [{"role": "system", "content": LINE}, {"role": "user", "content": "hi"}]


def test_date_anchor_never_mutates_input():
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    snapshot = copy.deepcopy(messages)

    result = apply_date_anchor(messages, SAT, True)

    assert messages == snapshot
    assert result is not messages


def test_date_anchor_disabled_is_identity():
    messages = [{"role": "system", "content": "S"}]

    assert apply_date_anchor(messages, SAT, False) is messages


def test_date_anchor_day_granularity_same_text_all_day():
    messages = [{"role": "system", "content": "S"}]

    a = apply_date_anchor(messages, datetime(2026, 10, 24, 0, 0, 1), True)
    b = apply_date_anchor(messages, datetime(2026, 10, 24, 23, 59, 59), True)

    assert a == b


def test_date_anchor_changes_next_day():
    messages = [{"role": "system", "content": "S"}]

    a = apply_date_anchor(messages, datetime(2026, 10, 24, 23, 59), True)
    b = apply_date_anchor(messages, datetime(2026, 10, 25, 0, 1), True)

    assert a != b
    assert "Sunday, October 25, 2026" in b[0]["content"]


def test_date_anchor_idempotent():
    once = apply_date_anchor([{"role": "system", "content": "S"}], SAT, True)

    assert apply_date_anchor(once, SAT, True)[0]["content"] == once[0]["content"]


def test_date_anchor_list_content():
    messages = [{"role": "system", "content": [{"type": "text", "text": "S"}]}]

    result = apply_date_anchor(messages, SAT, True)

    assert result[0]["content"][-1] == {"type": "text", "text": "\n\n" + LINE}
    assert messages[0]["content"] == [{"type": "text", "text": "S"}]


def test_parse_date_anchor():
    assert _parse_date_anchor(None) is True
    assert _parse_date_anchor("") is True
    assert _parse_date_anchor("  ") is True
    assert _parse_date_anchor("on") is True
    assert _parse_date_anchor("off") is False
    assert _parse_date_anchor(" OFF ") is False


def test_date_anchor_default_now_is_read_per_call(monkeypatch):
    """No injected ``now``: the clock must be read on every call, not bound once."""
    from patches import voice_rules

    class _Clock(datetime):
        current = datetime(2026, 10, 24, 23, 59)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(voice_rules, "datetime", _Clock)
    messages = [{"role": "system", "content": "S"}]

    first = apply_date_anchor(messages, enabled=True)[0]["content"]
    monkeypatch.setattr(_Clock, "current", datetime(2026, 10, 25, 0, 1))
    second = apply_date_anchor(messages, enabled=True)[0]["content"]

    assert "Saturday, October 24, 2026" in first
    assert "Sunday, October 25, 2026" in second


# ── clarify-instead-of-guess rule ─────────────────────────────────────────

_CLARIFY_RULE = (
    "If a request is missing something you need (which device, what to "
    "remind about, when), ask one short question instead of calling a tool "
    "or guessing."
)


def test_default_rules_end_with_the_clarify_rule():
    assert DEFAULT_RULES.endswith(_CLARIFY_RULE)
    assert DEFAULT_RULES.count(_CLARIFY_RULE) == 1


def test_clarify_rule_comes_before_the_date_line():
    messages = [{"role": "system", "content": "persona"}]
    out = apply_date_anchor(apply_system_rules(messages), now=datetime(2026, 10, 24), enabled=True)
    content = out[0]["content"]
    assert content.index(_CLARIFY_RULE) < content.index("Today's date is")
    assert content.endswith("Saturday, October 24, 2026.")
