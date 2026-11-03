"""Post-ASR vocabulary snap: repair project terms the speech recogniser mangled.

Parakeet-TDT hears "VoxCPM" as "Vox EPM". Spans of 1-3 transcript tokens in the
FINAL user transcript are fuzzy-matched against a user-supplied term list and
replaced with the canonical spelling, before the brain (and the cockpit
transcript) see them.

Precision beats recall: a wrong snap silently rewrites what the user said, a miss
is merely the status quo. So a span is rewritten only when

* the term is at least :data:`MIN_TERM_LEN` letters/digits long;
* the span and the term, both normalised (lower-case, alphanumerics only, spaces
  removed), are identical ("patch bay" -> "Patchbay", "home server" ->
  "homeserver"; exact matches skip the other guards), or ALL of these hold for a
  FUZZY match: difflib ratio >= :data:`THRESHOLD`, same first character, lengths
  within :data:`MAX_LEN_DIFF`, NEITHER normalised string is a substring of the
  other (so "Qwen ai", "Qwen3", "Heralds" never snap to "Qwen"/"Herald": a
  neighbouring word, digit or suffix would be silently swallowed), and no token of
  the span is a common English word (:data:`COMMON_WORDS`): "her mess" is 0.92
  similar to "Hermes" and stays as spoken;
* exactly one term wins (two different terms within :data:`AMBIGUITY` of each
  other means no snap);
* no punctuation sits between the tokens of a multi-token span ("Vox, EPM" is two
  phrases, not one name);
* an exact match on a single token that differs from the term only by case, and
  is lowercase in the transcript, is left alone ("put the crate of beer" stays;
  "the herald announced" stays). An ASR-capitalised "Herald" is canonicalised.

Known mishearings that fuzzy matching refuses (substring cases like "fork pie" for
"fork-pi") are declared as ALIASES, matched exactly after normalisation and always
rewritten to the canonical spelling.

The vocab file is plain text, one entry per line, ``#`` starts a comment line. An
entry is ``Canonical`` or ``Canonical | alias one | alias two``, e.g.
``VoxCPM | vox epm``. Path from ``VOICE_VOCAB_FILE`` (default
``~/speech-to-speech/vocab.txt``); a missing file or ``off`` means the feature is
off and nothing changes. The file is read as UTF-8 (a BOM is ignored) and re-read
when its mtime changes.

Dependency-free (stdlib only), like ``think_filter.py``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

THRESHOLD = 0.82
AMBIGUITY = 0.03
MIN_TERM_LEN = 4
MAX_SPAN = 3
MAX_LEN_DIFF = 2

# Hand-written, ~700 high-frequency English words (function words, common verbs
# and their forms, everyday nouns/adjectives). A shipped list instead of
# /usr/share/dict/words, which the target box does not reliably have. It only
# gates FUZZY snaps: a span containing any token from here is left alone. Deliberately
# omits uncommon words ("fork", "pie", "vox") so "fork pie" can still become
# "fork-pi". Extending it only ever makes the snap more conservative.
COMMON_WORDS = frozenset(
    """
a able about above across act actually add after again against age ago agree ah air all allow almost alone along
already also although always am among amount an and angry another answer any anyone anything anyway appear are area
aren't arm around arrive art as ask at away baby back bad bag ball bank base be beat beautiful because become bed
been before begin behind believe bell below best better between big bill bit black blood blue board boat body book
born both bottom box boy brain break bring brother brought brown build building burn bus business busy but buy by
call came can can't car card care carry case cat catch cause certain chair chance change check child children choose
church city class clean clear close coffee cold college color come common company continue control cook cool copy
corner cost could couldn't country course cover create cross cry cup cut dad dark data date daughter day dead dear
decide deep did didn't die different dinner do does doesn't dog doing don't done door down draw dream dress drink
drive drop dry during each ear early earth easy eat edge egg eight either else end enough even evening ever every
everyone everything everywhere exactly example eye face fact fall family far fast father feel feet fell few field
fight fill find fine finish fire first fish fit five floor fly follow food foot for forget form found four free
friend from front full fun funny game gave get girl give glad glass go god goes going gone good got great green
ground group grow guess guy had hair half hand happen happy hard has hasn't have haven't he head hear heard heart
heavy held hello help her here hers herself hey hi high hill him himself his history hit hold hole home hope horse
hospital hot hotel hour house how however huge hundred hurt i i'd i'll i'm i've idea if important in inside instead
into is isn't it it's its itself job join just keep kept key kid kill kind king kitchen knew know known lady land
language large last late later laugh lay lead learn least leave led left leg less let let's letter lie life light
like line list listen little live long look lose lost lot loud love low lunch machine made mail make man many map
mark matter may maybe me mean meet member mess met middle might mile milk mind mine minute miss mom moment money
month moon more morning most mother mouth move movie mr mrs much music must my myself name near need never new news
next nice night nine no nobody noise none nor normal north nose not note nothing notice now number of off offer
office often oh oil ok okay old on once one only open or order other our out outside over own page paid paper parent
park part party pass past pay people perfect person phone pick picture piece place plan plant play please point poor
possible power present press pretty probably problem program public pull push put question quick quiet quite radio
rain ran rather read ready real really reason red remember rest return rich ride right ring river road rock roll
room round run sad safe said same sat saw say school sea second see seem sell send sense sent serious set seven
several shall she shop short should shouldn't show shut sick side sign simple since sing sister sit six size skin
sky sleep slow small smile so some someone something sometimes son song soon sorry sound south speak special spend
spent spoke spring stand star start state stay step still stop store story street strong student study such summer
sun sure sweet table take talk tall teach teacher team tell ten test than thank thanks that that's the their them
themselves then there there's these they they're thing think third this those though thought three through throw
till time tire to today together told tomorrow tonight too took top touch toward town toy tree trip true try turn
two under understand until up upon us use used usual very visit voice wait wake walk wall want war warm was wasn't
watch water way we weather week weight well went were weren't west what what's whatever when where whether which
while white who whole whom whose why wide wife will win wind window winter wish with within without woman women
wonder won't wood word work world worry would wouldn't write wrong yard yeah year yellow yes yesterday yet you
you'd you'll you're you've young your yours yourself
""".split()
)

_SPAN_RE = re.compile(r"\S+")
# leading punctuation, core, optional possessive plus trailing punctuation
_TOKEN_RE = re.compile(r"^(\W*)(.*?)((?:['’]s)?\W*)$", re.DOTALL)


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def parse_terms(raw: str) -> list[tuple[str, str, bool]]:
    """File text -> [(canonical, normalised, is_alias)].

    ``Canonical | alias | alias`` lines yield one entry for the canonical term and one per
    alias (``is_alias`` True: exact match only). Comments, blanks, entries whose normalised
    form is shorter than :data:`MIN_TERM_LEN`, and duplicate normalised forms (first wins)
    are dropped.
    """
    out: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for line in raw.replace("\ufeff", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        canonical, *aliases = (part.strip() for part in line.split("|"))
        if not canonical:
            continue
        for text, is_alias in [(canonical, False), *((a, True) for a in aliases)]:
            n = _norm(text)
            if len(n) < MIN_TERM_LEN or n in seen:
                continue
            seen.add(n)
            out.append((canonical, n, is_alias))
    return out


def _has_common(core_tokens: list[str]) -> bool:
    return any(t.lower() in COMMON_WORDS for t in core_tokens)


def _match(span_norm: str, core_tokens: list[str], terms: list[tuple[str, str, bool]]) -> str | None:
    """Canonical term for this span, or None. Returning the span's own text means "leave it"."""
    for term, tn, is_alias in terms:
        if span_norm == tn:
            core = core_tokens[0]
            if not is_alias and len(core_tokens) == 1 and core == core.lower() and core.lower() == term.lower():
                return core  # plain lowercase speech that merely equals the term bar case
            return term
    if _has_common(core_tokens):
        return None
    scored: list[tuple[float, str]] = []
    for term, tn, is_alias in terms:
        if is_alias or span_norm[0] != tn[0] or abs(len(span_norm) - len(tn)) > MAX_LEN_DIFF:
            continue
        if span_norm in tn or tn in span_norm:  # swallowed neighbour / dropped digit or suffix
            continue
        r = SequenceMatcher(None, span_norm, tn).ratio()
        if r >= THRESHOLD:
            scored.append((r, term))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY:
        return None
    return scored[0][1]


def snap(text: str, terms: list[tuple[str, str, bool]]) -> tuple[str, list[tuple[str, str]]]:
    """Return (new_text, [(original_span, canonical_term), ...]).

    ``terms`` is the output of :func:`parse_terms`. Left to right, longest span first;
    a token is never rewritten twice.
    """
    if not text or not terms:
        return text, []
    toks = []
    for m in _SPAN_RE.finditer(text):
        lead, core, trail = _TOKEN_RE.match(m.group(0)).groups()  # type: ignore[union-attr]
        toks.append((m.start(), m.end(), lead, core, trail))

    out: list[str] = []
    changes: list[tuple[str, str]] = []
    pos = 0
    i = 0
    while i < len(toks):
        hit = None
        for n in range(min(MAX_SPAN, len(toks) - i), 0, -1):
            group = toks[i : i + n]
            if any(not _norm(g[3]) for g in group):
                continue
            # punctuation between tokens ends a phrase: only the first lead / last trail may be non-empty
            if any(g[4] for g in group[:-1]) or any(g[2] for g in group[1:]):
                continue
            cores = [g[3] for g in group]
            span_norm = _norm("".join(cores))
            if len(span_norm) < MIN_TERM_LEN:
                continue
            term = _match(span_norm, cores, terms)
            if term is None:
                continue
            hit = (n, term, " ".join(cores))
            break
        if hit is None:
            i += 1
            continue
        n, term, orig = hit
        group = toks[i : i + n]
        i += n
        if orig == term:  # already spelled canonically
            continue
        out.append(text[pos : group[0][0]])
        out.append(group[0][2] + term + group[-1][4])
        pos = group[-1][1]
        changes.append((orig, term))
    out.append(text[pos:])
    return "".join(out), changes


class VocabStore:
    """Vocab file loader: path from the environment, reloaded when its mtime changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[str, float] | None = None
        self._terms: list[tuple[str, str, bool]] = []
        self._warned: str | None = None

    @staticmethod
    def path() -> str | None:
        raw = os.environ.get("VOICE_VOCAB_FILE", "").strip()
        if raw.lower() == "off":
            return None
        return os.path.expanduser(raw or "~/speech-to-speech/vocab.txt")

    def terms(self) -> list[tuple[str, str, bool]]:
        path = self.path()
        if path is None:
            return []
        try:
            key = (path, os.stat(path).st_mtime)
        except OSError:
            self._key = None
            return []  # missing file: feature off, silent
        with self._lock:
            if key != self._key:
                try:
                    with open(path, encoding="utf-8-sig") as fh:
                        self._terms = parse_terms(fh.read())
                except (OSError, UnicodeDecodeError) as exc:
                    if self._warned != path:
                        logger.warning("vocab snap: cannot read %s (%s); feature off", path, exc)
                        self._warned = path
                    self._terms = []
                else:
                    logger.info("vocab snap: loaded %d terms from %s", len(self._terms), path)
                self._key = key
            return self._terms


_store = VocabStore()


def snap_text(text: str) -> str:
    """Snap against the configured vocab file. Fails open: any error returns ``text`` unchanged."""
    try:
        new, changes = snap(text, _store.terms())
    except Exception:  # noqa: BLE001 - a bug here must never cost the user their turn
        logger.exception("vocab snap failed; transcript passed through unchanged")
        return text
    for orig, term in changes:
        logger.info("vocab snap: %r -> %r", orig, term)
    return new


def snap_stt_output(item: Any) -> Any:
    """Snap one STT output as TranscriptionNotifier receives it.

    Only FINAL transcripts (a ``Transcription`` message, tag ``"transcription"``, or a bare
    ``str``) are touched; partials and anything else pass through untouched.
    """
    if isinstance(item, str):
        return snap_text(item)
    if getattr(item, "tag", None) != "transcription":
        return item
    text = getattr(item, "text", None)
    if not isinstance(text, str):
        return item
    new = snap_text(text)
    if new == text:
        return item
    return item.model_copy(update={"text": new})
