"""Unit tests for voice_affect.py (per-turn affect instruct seam).

Run from repo root: python3 -m pytest patches/test_voice_affect.py -v

voice_affect.py is dependency-free (no speech_to_speech, no non-stdlib
imports), so these tests need no stubs and no installed package -- same
pattern as test_voice_rules.py / test_think_filter.py.
"""

from __future__ import annotations

import copy

from patches.voice_affect import (
    AFFECT_RULE,
    AFFECT_WORDS_RULE,
    TurnAffectTracker,
    _parse,
    apply_affect_rule,
    extract_affect,
    rule_text,
    sanitize_affect,
)

# ── VOICE_AFFECT env parsing (_parse unit tests) ────────────────────────────


def test_parse_unset_is_off():
    assert _parse(None) == "off"


def test_parse_blank_is_off():
    assert _parse("   ") == "off"


def test_parse_zero_is_off():
    assert _parse("0") == "off"


def test_parse_off_case_and_whitespace_insensitive():
    assert _parse("off") == "off"
    assert _parse("  Off  ") == "off"
    assert _parse("OFF") == "off"


def test_parse_one_is_on():
    assert _parse("1") == "on"


def test_parse_on_case_and_whitespace_insensitive():
    assert _parse("on") == "on"
    assert _parse("  On  ") == "on"
    assert _parse("ON") == "on"


def test_parse_words_case_and_whitespace_insensitive():
    assert _parse("words") == "words"
    assert _parse("  Words  ") == "words"
    assert _parse("WORDS") == "words"


def test_parse_junk_value_fails_closed_to_off():
    assert _parse("banana") == "off"
    assert _parse("2") == "off"
    assert _parse("true") == "off"


# ── rule_text ────────────────────────────────────────────────────────────


def test_rule_text_off_is_empty():
    assert rule_text("off") == ""


def test_rule_text_on_is_affect_rule_only():
    assert rule_text("on") == AFFECT_RULE


def test_rule_text_words_appends_words_rule():
    text = rule_text("words")
    assert text == AFFECT_RULE + "\n\n" + AFFECT_WORDS_RULE


# ── apply_affect_rule: disabled is byte-identical (THE most important test) ─


def test_disabled_apply_affect_rule_is_identity_no_op():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]

    result = apply_affect_rule(messages, "off")

    assert result is messages  # same object, not just equal


def test_disabled_apply_affect_rule_default_state_is_off_when_env_unset():
    # STATE is read once at import from VOICE_AFFECT, which is unset in this
    # test process -- the default (no override) call must be a no-op too.
    messages = [{"role": "user", "content": "hi"}]

    result = apply_affect_rule(messages)

    assert result is messages


# ── apply_affect_rule: system message shapes (mirrors voice_rules) ─────────


def test_system_str_content_appended_when_on():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]

    result = apply_affect_rule(messages, "on")

    assert result[0]["content"] == "You are helpful.\n\n" + AFFECT_RULE
    assert result[1] == {"role": "user", "content": "hi"}


def test_system_parts_list_content_appended_when_words():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": "hi"},
    ]

    result = apply_affect_rule(messages, "words")

    assert result[0]["content"] == [
        {"type": "text", "text": "You are helpful."},
        {"type": "text", "text": "\n\n" + AFFECT_RULE + "\n\n" + AFFECT_WORDS_RULE},
    ]


def test_no_system_message_inserts_at_index_zero():
    messages = [{"role": "user", "content": "hi"}]

    result = apply_affect_rule(messages, "on")

    assert result[0] == {"role": "system", "content": AFFECT_RULE}
    assert result[1] == {"role": "user", "content": "hi"}
    assert len(result) == 2


def test_absent_content_shape_treated_as_empty():
    messages = [{"role": "system"}, {"role": "user", "content": "hi"}]

    result = apply_affect_rule(messages, "on")

    assert result[0]["content"] == AFFECT_RULE


# ── apply_affect_rule: never mutates input ──────────────────────────────────


def test_input_not_mutated_str_content():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    original = copy.deepcopy(messages)

    apply_affect_rule(messages, "on")

    assert messages == original


def test_input_not_mutated_list_content():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": "hi"},
    ]
    original = copy.deepcopy(messages)

    apply_affect_rule(messages, "words")

    assert messages == original


def test_input_not_mutated_no_system():
    messages = [{"role": "user", "content": "hi"}]
    original = copy.deepcopy(messages)

    apply_affect_rule(messages, "on")

    assert messages == original


# ── apply_affect_rule: idempotence ──────────────────────────────────────────


def test_idempotent_str_content():
    messages = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]

    once = apply_affect_rule(messages, "on")
    twice = apply_affect_rule(once, "on")

    assert twice == once
    assert twice[0]["content"].count(AFFECT_RULE) == 1


def test_idempotent_parts_list_content():
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "You are helpful."}]},
        {"role": "user", "content": "hi"},
    ]

    once = apply_affect_rule(messages, "words")
    twice = apply_affect_rule(once, "words")

    assert twice == once
    assert len(twice[0]["content"]) == 2


# ── extract_affect: marker extraction ───────────────────────────────────────


def test_extract_leading_marker():
    text, affect = extract_affect("[affect: this is funny. Light, amused] Well that's a surprise.")
    assert text == "Well that's a surprise."
    assert affect == "this is funny. Light, amused"


def test_extract_mid_text_marker():
    text, affect = extract_affect("Well [affect: mild surprise. Amused] that's unexpected.")
    assert text == "Well that's unexpected."
    assert affect == "mild surprise. Amused"


def test_extract_two_markers_latest_wins():
    text, affect = extract_affect("[affect: first note] some text [affect: second note] more text")
    assert text == "some text more text"
    assert affect == "second note"


def test_malformed_unclosed_marker_left_alone():
    raw = "[affect: no close bracket here, keep going"
    text, affect = extract_affect(raw)
    assert text == raw
    assert affect is None


def test_chunk_that_is_only_a_marker_yields_empty_text():
    text, affect = extract_affect("[affect: just a note]")
    assert text == ""
    assert affect == "just a note"


def test_no_marker_present_returns_unchanged_text_and_none_affect():
    text, affect = extract_affect("plain text, nothing to extract")
    assert text == "plain text, nothing to extract"
    assert affect is None


def test_extraction_is_case_insensitive():
    text, affect = extract_affect("[AFFECT: shouting] loud reply")
    assert text == "loud reply"
    assert affect == "shouting"


# ── sanitize_affect ──────────────────────────────────────────────────────


def test_sanitize_truncates_over_120_chars_on_word_boundary():
    raw = ("word " * 40).strip()  # far over 120 chars
    result = sanitize_affect(raw)
    assert result is not None
    assert len(result) <= 120
    assert not result.endswith(" ")
    # Truncating mid-word would produce a fragment not present as a whole
    # word in the source; every word in the result must be a real word.
    assert all(w == "word" for w in result.split())


def test_sanitize_collapses_control_characters_and_newlines():
    raw = "line one\nline\ttwo\x00\x01  spaced   out"
    result = sanitize_affect(raw)
    assert result == "line one line two spaced out"


def test_sanitize_whitespace_only_is_none():
    assert sanitize_affect("   \n\t  ") is None


def test_sanitize_empty_is_none():
    assert sanitize_affect("") is None


def test_sanitize_under_limit_passes_through_stripped():
    assert sanitize_affect("  be amused  ") == "be amused"


def test_sanitize_one_long_word_hard_cuts_when_no_space():
    raw = "x" * 200
    result = sanitize_affect(raw)
    assert result is not None
    assert len(result) == 120


# ── TurnAffectTracker.feed: per-turn carry-over ─────────────────────────────
# Ported from the removed update()-only tests (update() itself is now dead
# code -- feed() is the only entry point process() ever calls -- so these
# assert the same three properties (carry-over, turn reset, None-as-an-
# ordinary-key) through feed(), using complete single-chunk markers so the
# hold-back machinery tested separately below isn't in play here.


def test_feed_carries_affect_across_chunks_without_a_marker():
    tracker = TurnAffectTracker()
    text1, affect1 = tracker.feed("turn-1", "[affect: amused] hello")
    assert affect1 == "amused"
    text2, affect2 = tracker.feed("turn-1", "no marker here")
    assert affect2 == "amused"  # no new marker -> inherits
    assert text2 == "no marker here"
    text3, affect3 = tracker.feed("turn-1", "still none")
    assert affect3 == "amused"


def test_feed_resets_on_new_turn_id():
    tracker = TurnAffectTracker()
    tracker.feed("turn-1", "[affect: amused] hello")
    _, affect2 = tracker.feed("turn-2", "no marker yet")
    assert affect2 is None  # new turn, no marker yet -> None


def test_feed_second_marker_updates_within_same_turn():
    tracker = TurnAffectTracker()
    tracker.feed("turn-1", "[affect: amused] hello")
    _, affect2 = tracker.feed("turn-1", "[affect: serious] world")
    assert affect2 == "serious"
    _, affect3 = tracker.feed("turn-1", "no marker")
    assert affect3 == "serious"  # inherits the LATEST


def test_feed_none_turn_id_is_an_ordinary_key():
    tracker = TurnAffectTracker()
    _, affect1 = tracker.feed(None, "[affect: amused] hello")
    assert affect1 == "amused"
    _, affect2 = tracker.feed(None, "no marker")
    assert affect2 == "amused"
    _, affect3 = tracker.feed("turn-1", "no marker")
    assert affect3 is None  # switching away resets
    _, affect4 = tracker.feed(None, "no marker")
    assert affect4 is None  # switching back resets again


# ── TurnAffectTracker.feed: stateful hold-back guard (sentence-split fix) ──
#
# Correction 3: the LM layer sentence-splits with nltk BEFORE extraction ever
# sees the text, and a marker containing a full stop (the AFFECT_RULE-era
# wording) can straddle that split -- verified live:
#   sent_tokenize(...) -> ['[affect: this is a funny mix-up.',
#                           'Light, warm, amused] Oh no, ...']
# feed() must hold the dangling opener back rather than ever emitting half a
# marker as speech.


def test_marker_split_across_two_chunks_at_full_stop_extracts_cleanly():
    tracker = TurnAffectTracker()
    chunk1 = "[affect: this is a funny mix-up."
    chunk2 = "Light, warm, amused] Oh no, your poor keyboard is now a swimming pool!"

    text1, affect1 = tracker.feed("turn-1", chunk1)
    text2, affect2 = tracker.feed("turn-1", chunk2)

    assert text1 == ""  # nothing to emit yet -- the whole chunk is the dangling opener
    assert affect1 is None
    assert "[affect" not in text2 and "]" not in text2
    assert text2 == "Oh no, your poor keyboard is now a swimming pool!"
    assert affect2 is not None and "mix-up" in affect2 and "amused" in affect2


def test_dangling_opener_dropped_on_turn_change_never_emitted():
    tracker = TurnAffectTracker()
    text1, affect1 = tracker.feed("turn-1", "[affect: never closes in this turn")
    assert text1 == ""
    assert affect1 is None

    text2, affect2 = tracker.feed("turn-2", "a fresh turn, unrelated reply.")

    assert "[affect" not in text2
    assert "never closes" not in text2
    assert text2 == "a fresh turn, unrelated reply."
    assert affect2 is None


def test_opener_that_never_closes_past_bound_released_verbatim():
    tracker = TurnAffectTracker()
    tracker.feed("turn-1", "[affect: ")
    long_runon = "word " * 60  # far past the 200-char hold bound, no "]" anywhere

    text, affect = tracker.feed("turn-1", long_runon)

    # Released as ordinary speech -- a user legitimately saying "[affect: ..."
    # in a long sentence must not be swallowed forever.
    assert text != ""
    assert affect is None


def test_complete_marker_in_one_chunk_behaves_as_before():
    # Regression guard: the working single-chunk path must not change.
    tracker = TurnAffectTracker()

    text, affect = tracker.feed("turn-1", "[affect: amused] hello there")

    assert text == "hello there"
    assert affect == "amused"


def test_text_before_opener_in_same_chunk_is_emitted_not_swallowed():
    tracker = TurnAffectTracker()

    text, affect = tracker.feed("turn-1", "leading words [affect: still open, no closer yet")

    assert text == "leading words"
    assert affect is None


def test_complete_marker_and_trailing_dangling_opener_in_same_chunk():
    # A chunk can carry a complete marker AND the start of the next one in
    # the same breath -- the complete marker's affect must be taken and its
    # text emitted, the trailing opener held (not emitted), and a later
    # chunk that closes it must overwrite the affect.
    tracker = TurnAffectTracker()

    text1, affect1 = tracker.feed("turn-1", "[affect: done - brisk] some text [affect: a new one starts")

    assert text1 == "some text"
    assert affect1 == "done - brisk"

    text2, affect2 = tracker.feed("turn-1", "still open] and now the reply")

    assert "[affect" not in text2 and "]" not in text2
    assert text2 == "and now the reply"
    assert affect2 == "a new one starts still open"


def test_split_marker_join_gains_a_space_at_the_boundary():
    # The LM layer's sentence-splitter strips the separating whitespace at
    # the boundary it split on (verified against the live tokenizer repro in
    # Correction 3), so the join must supply its own space or the affect
    # reads as one run-on word ("mix-up.Light").
    tracker = TurnAffectTracker()
    tracker.feed("turn-1", "[affect: this is a funny mix-up.")
    _, affect = tracker.feed("turn-1", "Light, warm, amused] rest")

    assert affect == "this is a funny mix-up. Light, warm, amused"


def test_split_marker_join_does_not_double_space_when_boundary_kept_its_space():
    # A boundary that DID preserve its space must not end up double-spaced --
    # sanitize_affect collapses any whitespace run to exactly one space.
    tracker = TurnAffectTracker()
    tracker.feed("turn-1", "[affect: this is a funny mix-up.")
    _, affect = tracker.feed("turn-1", " Light, warm, amused] rest")

    assert affect == "this is a funny mix-up. Light, warm, amused"
