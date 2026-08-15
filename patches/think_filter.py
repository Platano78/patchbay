"""Streaming ``<think>...</think>`` suppressor.

Some local reasoning models (e.g. ``reasoning-qwen36-27b-mtp`` behind
llama.cpp) emit their reasoning as literal ``<think>...</think>`` spans in
the regular chat-completions ``content`` field, even when
``chat_template_kwargs.enable_thinking=false`` is requested -- probed live:
a response's ``content`` was ``"<think>\\n\\n</think>\\n\\nHi there friend"``.
The upstream chat handler has no reasoning-channel handling, so with a
model that doesn't honour the disable flag, the full reasoning text lands in
``content`` and gets spoken by TTS.

:class:`ThinkTagFilter` is a small, dependency-free, stateful suppressor
sitting between the raw per-delta text and whatever consumes it (a
streaming ``TextDelta`` or a one-shot non-streaming string): feed it text
piece by piece via :meth:`feed`, call :meth:`flush` once the stream ends.

Design:

* Drops everything from ``<think>`` through ``</think>`` inclusive; supports
  multiple spans in one stream.
* A ``<think>``/``</think>`` tag can straddle a chunk boundary (e.g. one
  delta ends in ``"<thi"``, the next starts with ``"nk>..."``) -- a trailing
  partial-tag prefix is buffered across :meth:`feed` calls and only resolved
  (emitted verbatim, or consumed as part of a real tag) once enough text has
  arrived to tell the two cases apart.
* An unclosed ``<think>`` still open when the stream ends is suppressed in
  full -- it was reasoning that never finished, not innocent text.
* :meth:`flush` returns any text buffered as a *possible* tag prefix that
  turned out, by end of stream, to never have been completed into a real
  tag (so it was innocent text all along and must not be dropped).
* Swallows the leading run of newlines immediately after a closed think
  span (the ``"\\n\\n"`` typically separating reasoning from the reply) so
  TTS never sees a leading blank chunk.

Dependency-free by design: this module must not import ``speech_to_speech``
or anything non-stdlib, so it (and its tests) can be imported and unit
tested with neither the package nor any of its dependencies installed.
"""

from __future__ import annotations

_OPEN = "<think>"
_CLOSE = "</think>"


def _partial_match_len(buf: str, tag: str) -> int:
    """Length of the longest suffix of ``buf`` that is also a proper
    (shorter-than-``tag``) prefix of ``tag``.

    This is the "could this be the start of the tag, split across a chunk
    boundary?" check: e.g. ``buf`` ending in ``"<thi"`` against
    ``tag="<think>"`` returns 4.
    """
    max_len = min(len(buf), len(tag) - 1)
    for length in range(max_len, 0, -1):
        if buf.endswith(tag[:length]):
            return length
    return 0


class ThinkTagFilter:
    """Stateful streaming suppressor for ``<think>...</think>`` spans."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False
        self._swallow_newlines = False

    def feed(self, text: str) -> str:
        """Feed the next chunk of raw text; returns the portion (if any)
        that is safe to emit now (outside any think span, with any partial
        tag prefix held back until it can be resolved)."""
        if not text:
            return ""
        buf = self._buffer + text
        self._buffer = ""
        out: list[str] = []

        while True:
            if self._in_think:
                idx = buf.find(_CLOSE)
                if idx == -1:
                    keep = _partial_match_len(buf, _CLOSE)
                    self._buffer = buf[len(buf) - keep :] if keep else ""
                    return "".join(out)
                buf = buf[idx + len(_CLOSE) :]
                self._in_think = False
                self._swallow_newlines = True
                continue

            idx = buf.find(_OPEN)
            if idx == -1:
                keep = _partial_match_len(buf, _OPEN)
                emit, buf = (buf[: len(buf) - keep], buf[len(buf) - keep :]) if keep else (buf, "")
                self._buffer = buf
                emitted = self._apply_newline_swallow(emit)
                if emitted:
                    out.append(emitted)
                return "".join(out)

            pre = buf[:idx]
            emitted = self._apply_newline_swallow(pre)
            if emitted:
                out.append(emitted)
            buf = buf[idx + len(_OPEN) :]
            self._in_think = True

        # unreachable

    def flush(self) -> str:
        """Call once the stream ends. Returns any innocent buffered prefix
        that never resolved into a real tag; a still-open ``<think>`` (never
        closed) is suppressed instead of surfaced."""
        if self._in_think:
            self._buffer = ""
            return ""
        remnant = self._apply_newline_swallow(self._buffer)
        self._buffer = ""
        return remnant

    def _apply_newline_swallow(self, text: str) -> str:
        if not self._swallow_newlines or not text:
            return text
        stripped = text.lstrip("\n")
        if stripped:
            self._swallow_newlines = False
        return stripped
