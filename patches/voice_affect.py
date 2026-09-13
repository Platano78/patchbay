"""Per-turn affect instruct seam (owner ruling 2026-09-13).

The brain is asked to prefix its reply with a short delivery note in the
form ``[affect: ...]`` describing what the moment is and how it should
sound. This module owns the whole thing: the env gate, the rule text
injected into the system prompt, extraction of the marker from streamed
text, and sanitising the extracted affect before it is safe to send
anywhere (an HTTP ``instructions`` field).

Dependency-free by design (stdlib only, no ``speech_to_speech`` import),
like ``think_filter.py``/``voice_rules.py`` -- importable and unit-testable
standalone.

Measured 2026-09-13 against the live brain (``gemma4-12b``) and the live
TTS server: marker compliance 85/85 across four runs, zero misses, with
markers left in chat history or not (12/12 each); the word "affect" never
leaked into a spoken reply (0/24), and 70 real brain turns replayed
through :func:`extract_affect` stripped clean with no text drift.

What the instruct actually buys, with the spoken text held byte-identical
across arms (n=20 pooled, detector ``small.en`` WITHOUT ``-nt``): no
instructions 0/20 laughter and 6.84s mean duration; the brain's own affect
note 2/20 and 7.65s (**+12%**); a hand-written content-tied string 2/20 and
7.49s. So this is a DELIVERY channel -- duration and register shift
consistently and reproducibly. It is NOT an event generator: the much-quoted
"0 -> 6/10 laughter, +61% duration" is real but **sentence-specific**, and
does not transfer to what the brain actually says. Promise delivery
conditioning, never a paralinguistic event. See Law 10 in
``docs/research/voice-stack-review-2026-08-11.md`` (maintainer notes, not in the public export).
"""

from __future__ import annotations

import os
import re
from typing import Any

AFFECT_RULE = (
    "Begin every reply with a delivery note in square brackets: "
    "[affect: ...]. First name what this moment IS, in plain words - the "
    "actual thing, not a category. Then, after a dash, say how it should "
    "sound. If something is funny, say so and be amused; you are allowed to "
    "find things funny. If something is bad, say so and be serious. "
    "Examples: [affect: this is a funny mix-up - light, warm, amused] / "
    "[affect: their backup is gone - serious, measured, no levity] / "
    "[affect: a plain factual answer - brisk and matter-of-fact]. Under "
    "fifteen words, and NEVER put a full stop inside the brackets. Note "
    "first, then a space, then your reply. Never mention the note."
)

AFFECT_WORDS_RULE = (
    "Then write the reply the way the note says. The words themselves have "
    "to match it: if the note says amused, be actually amused and say "
    "something with some play in it - do not answer a funny thing with a "
    "polite acknowledgement. If the note says serious, do not soften it. A "
    "note that does not match the words it introduces is wrong."
)

# States: "off" (disabled, default) / "on" (note only) / "words" (note + the
# reply's own wording must match the note).
_VALID_STATES = ("off", "on", "words")


def _parse(raw: str | None) -> str:
    """``VOICE_AFFECT`` parsing: unset/blank/``"0"``/``"off"``
    (case-insensitive, stripped) -> ``"off"``; ``"1"``/``"on"`` -> ``"on"``;
    ``"words"`` -> ``"words"``; anything else -> ``"off"`` (fail closed).
    Never raises.
    """
    if raw is None:
        return "off"
    stripped = raw.strip().lower()
    if not stripped or stripped == "0" or stripped == "off":
        return "off"
    if stripped == "1" or stripped == "on":
        return "on"
    if stripped == "words":
        return "words"
    return "off"


STATE = _parse(os.environ.get("VOICE_AFFECT"))
ENABLED = STATE != "off"

_MARKER_RE = re.compile(r"\[\s*affect\s*:\s*([^\]\n]{1,200})\]", re.IGNORECASE)
# Same as _MARKER_RE, but with a fallback alternative that matches a bare,
# unclosed opener -- tried only once the first (complete-marker) alternative
# fails to find a matching "]" within the same {1,200}-char/no-newline
# window. Powers TurnAffectTracker's hold-back path: a dangling "[affect:"
# at the end of a chunk (the sentence-split case: the LM layer tokenizes on
# a full stop, and a marker containing one can straddle two chunks) is
# distinguished from a complete marker this way.
_MARKER_OR_OPENER_RE = re.compile(r"\[\s*affect\s*:\s*([^\]\n]{1,200})\]|\[\s*affect\s*:", re.IGNORECASE)

_MAX_AFFECT_LEN = 120
_CONTROL_WS_RE = re.compile(r"[\x00-\x1f\x7f\s]+")
# Bound on an accumulating held-open marker fragment (mirrors _MARKER_RE's
# own {1,200} content cap). Past this, it was never a marker -- e.g. a user
# quoting the literal text "[affect: ..." in an ordinary long sentence --
# and the held text is released verbatim rather than swallowed forever.
_HOLD_BOUND = 200


def rule_text(state: str = STATE) -> str:
    """Return the system-prompt rule text for ``state`` ("on"/"words"), or
    ``""`` when disabled."""
    if state == "words":
        return AFFECT_RULE + "\n\n" + AFFECT_WORDS_RULE
    if state == "on":
        return AFFECT_RULE
    return ""


def sanitize_affect(raw: str) -> str | None:
    """Collapse whitespace/newlines/control characters to single spaces,
    strip, and truncate to :data:`_MAX_AFFECT_LEN` characters on a word
    boundary where possible. Returns ``None`` if nothing survives.

    Free brain text must not go unbounded into an HTTP request body -- this
    is the only thing standing between the model's own words and the wire.
    """
    collapsed = _CONTROL_WS_RE.sub(" ", raw).strip()
    if not collapsed:
        return None
    if len(collapsed) <= _MAX_AFFECT_LEN:
        return collapsed
    truncated = collapsed[:_MAX_AFFECT_LEN]
    # Prefer breaking on a word boundary; fall back to a hard cut if the
    # truncated slice has no space at all (one very long "word").
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    truncated = truncated.strip()
    return truncated or None


def extract_affect(text: str) -> tuple[str, str | None]:
    """Remove every ``[affect: ...]`` marker from ``text``, returning
    ``(stripped_text, sanitized_affect)``. ``sanitized_affect`` is the LAST
    match found (a chunk may carry more than one; the latest wins), sanitized
    via :func:`sanitize_affect`, or ``None`` if no marker was present or
    nothing survived sanitising. The stripped text has the removed markers'
    whitespace collapsed and is ``.strip()``-ed.
    """
    matches = list(_MARKER_RE.finditer(text))
    if not matches:
        return text, None
    stripped = _MARKER_RE.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    affect = sanitize_affect(matches[-1].group(1))
    return stripped, affect


class TurnAffectTracker:
    """Keyed on turn_id (which may legitimately be ``None``), tracks the
    last-seen affect for the current turn and resets when the turn changes.
    Mirrors ``lm_output_processor._next_tool_round``'s per-turn-state idiom.

    Also owns the cross-chunk hold-back guard for a marker split across two
    ``LLMResponseChunk``s: the LM layer sentence-splits on nltk BEFORE
    extraction ever sees the text, and a marker containing a full stop (or a
    tool-call flush landing mid-marker) can straddle the boundary -- e.g.
    ``"[affect: this is a funny mix-up."`` arrives as one chunk and
    ``"Light, warm, amused] Oh no..."`` as the next. Mirrors
    ``ThinkTagFilter``'s straddle-a-chunk-boundary design in
    ``think_filter.py``: an opener with no closer yet is held rather than
    passed through (where it would be spoken aloud), and a marker still open
    when the turn ends is dropped, never spoken -- same as an unclosed
    ``<think>`` at end of stream.
    """

    _UNSET = object()

    def __init__(self) -> None:
        self._turn_id: Any = self._UNSET
        self._affect: str | None = None
        self._held: str = ""  # accumulating fragment, starting at the opener
        self._holding: bool = False

    def feed(self, turn_id: Any, text: str) -> tuple[str, str | None]:
        """Stateful per-turn marker extraction. Returns ``(text_to_emit,
        affect_for_this_chunk)``. A dangling ``"[affect:"`` opener with no
        closing ``"]"`` yet is held back (nothing emitted for it this
        chunk); the next call completes it once a ``"]"`` arrives, or -- past
        :data:`_HOLD_BOUND` accumulated characters -- releases it verbatim as
        ordinary speech (it was never a marker). A turn change drops any
        held fragment outright.
        """
        if turn_id != self._turn_id:
            self._turn_id = turn_id
            self._affect = None
            self._held = ""
            self._holding = False

        if self._holding:
            close_idx = text.find("]")
            if close_idx == -1:
                self._held += text
                if len(self._held) > _HOLD_BOUND:
                    released = self._held
                    self._held = ""
                    self._holding = False
                    return re.sub(r"\s+", " ", released).strip(), self._affect
                return "", self._affect
            # The LM layer's sentence-splitter strips the separating
            # whitespace at the boundary it split on (verified against the
            # live tokenizer repro), so the join needs its own space -- safe
            # even when the boundary DID keep its space, since sanitize_affect
            # collapses any run of whitespace (including "  ") to one.
            joined = self._held + " " + text[: close_idx + 1]
            remainder = text[close_idx + 1 :]
            self._held = ""
            self._holding = False
            _, affect = extract_affect(joined)
            if affect is not None:
                self._affect = affect
            return self._consume(remainder), self._affect

        return self._consume(text), self._affect

    def _consume(self, text: str) -> str:
        """Strip every complete marker in ``text`` (updating ``self._affect``
        with the latest), and -- if the text ends in a dangling, unclosed
        opener -- hold that fragment back and emit only what precedes it."""
        out_parts: list[str] = []
        idx = 0
        for m in _MARKER_OR_OPENER_RE.finditer(text):
            out_parts.append(text[idx:m.start()])
            if m.group(1) is not None:
                affect = sanitize_affect(m.group(1))
                if affect is not None:
                    self._affect = affect
                idx = m.end()
            else:
                self._held = text[m.start() :]
                self._holding = True
                idx = len(text)
                break
        out_parts.append(text[idx:])
        return re.sub(r"\s+", " ", "".join(out_parts)).strip()


def apply_affect_rule(messages: list[dict[str, Any]], state: str = STATE) -> list[dict[str, Any]]:
    """Return ``messages`` with the affect rule appended to (or inserted as)
    the system message. Mirrors ``voice_rules.apply_system_rules``'s exact
    contract: never mutates the input list or its dicts (copy-on-write
    only), no-op returning ``messages`` unchanged when disabled, and an
    idempotence guard against a double-serialize path appending it twice.
    """
    rules = rule_text(state)
    if not rules:
        return messages

    system_index = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)

    if system_index is None:
        return [{"role": "system", "content": rules}, *messages]

    system_message = messages[system_index]
    content = system_message.get("content")

    if isinstance(content, str):
        if rules in content:
            return messages
        new_content: Any = content + "\n\n" + rules
    elif isinstance(content, list):
        if any(isinstance(part, dict) and rules in (part.get("text") or "") for part in content):
            return messages
        new_content = [*content, {"type": "text", "text": "\n\n" + rules}]
    else:
        new_content = rules

    new_messages = list(messages)
    new_messages[system_index] = {**system_message, "content": new_content}
    return new_messages
