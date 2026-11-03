"""Backchannel classifier (lexical acknowledgement detector).

Reimplemented from the idea in speakrail/speakrail (Apache-2.0); the
behaviour below is specified independently. Only meaningful while the
assistant is speaking — when silent, "yeah" is an answer, not an
acknowledgement. Empty means not a backchannel; callers decide what an
empty transcript over the reply means.

Dependency-free by design: stdlib only, no speech_to_speech import.
"""

from __future__ import annotations

import re

PHRASES: frozenset[str] = frozenset([
    "mm", "mmm", "mhm", "mmhm", "mmhmm", "mm hm", "mm hmm", "hmm", "hm",
    "uhhuh", "uh huh", "ah", "oh", "ooh", "aha",
    "yeah", "yea", "ya", "yes", "yep", "yup", "okay", "ok", "kay",
    "alright", "all right", "right", "sure", "true", "correct",
    "exactly", "indeed", "totally", "absolutely", "definitely",
    "for sure", "of course", "fine",
    "i see", "got it", "gotcha", "understood",
    "i understand", "i know", "makes sense", "fair enough",
    "thats right", "thats true", "wow", "oh wow", "cool", "nice",
    "great", "good", "perfect", "interesting",
    "oh okay", "oh right", "oh yeah", "oh i see",
    "sounds good", "go on", "keep going", "carry on",
])

MAX_TOKENS = 5

_PHRASE_TOKENS: dict[str, list[str]] = {
    p: p.split() for p in PHRASES
}


def normalize(text: str) -> list[str]:
    s = text.lower()
    s = s.replace("-", "").replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", s)


def is_backchannel(text: object) -> bool:
    if not isinstance(text, str):
        return False
    toks = normalize(text)
    if not toks:
        return False
    if len(toks) > MAX_TOKENS:
        return False
    # Dynamic programming: reachability of prefix positions.
    n = len(toks)
    reachable = [False] * (n + 1)
    reachable[0] = True
    for i in range(n):
        if not reachable[i]:
            continue
        for toks_list in _PHRASE_TOKENS.values():
            end = i + len(toks_list)
            if end <= n and toks[i:end] == toks_list:
                reachable[end] = True
    return reachable[n]
