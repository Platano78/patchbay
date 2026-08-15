"""Pipeline-invariant system rules injected at request-assembly time.

Probed regression: qwen3.6 in no-think mode collapses answer length as chat
history grows (27 completion tokens for a 400-word ask, vs 415 with a
system-prompt line telling it to answer completely). The mitigation must
live at the pipeline layer, not as a runtime persona edit -- a persona
author has no way to know they need to include an anti-truncation
instruction, and every persona (and every brain swap) would otherwise need
to carry it individually.

:func:`apply_system_rules` appends (or inserts) a short system-level
instruction into the serialised message list right before it goes out on
the wire, independent of whatever persona/instructions the user configured.
It runs *after* the persona's own system message is built, so it augments
rather than replaces persona instructions.

Dependency-free by design (stdlib only, no ``speech_to_speech`` import),
like ``think_filter.py`` -- importable and unit-testable standalone.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_RULES = (
    "Always answer completely and thoroughly - never cut an answer short. "
    "When asked for detail, deliver the full length requested. "
    "Never end your reply with a promise to do something later. If "
    "answering needs a tool, call the tool now, in this same turn; if no "
    "tool is needed, give the answer directly. Saying you will check, look "
    "up, or get back to the user without actually doing it is a failure."
)


def _parse(raw: str | None) -> str:
    """``VOICE_SYSTEM_RULES`` parsing: unset/blank -> :data:`DEFAULT_RULES`;
    ``"off"`` (case-insensitive, stripped) -> ``""`` (disabled); any other
    non-blank string -> used verbatim."""
    if raw is None:
        return DEFAULT_RULES
    stripped = raw.strip()
    if not stripped:
        return DEFAULT_RULES
    if stripped.lower() == "off":
        return ""
    return stripped


RULES = _parse(os.environ.get("VOICE_SYSTEM_RULES"))


def apply_system_rules(messages: list[dict[str, Any]], rules: str = RULES) -> list[dict[str, Any]]:
    """Return ``messages`` with ``rules`` appended to (or inserted as) the
    system message. Never mutates the input list or its dicts -- only
    copies are modified, since the ``Chat`` object owns the originals.

    Treat the RETURNED list as read-only: it deliberately shares unmodified
    message dicts with the input (copy-on-write -- deep-copying would
    duplicate large multimodal payloads per request). Mutating the result
    would therefore reach back into the ``Chat``-owned originals.

    No-op (returns ``messages`` unchanged) when ``rules`` is empty, or when
    ``rules`` is already present in the system content (idempotence guard
    against any double-serialize path).
    """
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
        # Unexpected/absent content shape: treat as empty and just set the rules.
        new_content = rules

    new_messages = list(messages)
    new_messages[system_index] = {**system_message, "content": new_content}
    return new_messages
