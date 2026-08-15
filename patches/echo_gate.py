"""Server-side echo gate: tell "the assistant's own TTS bleeding back into the
mic" apart from "the user is actually talking", so voice barge-in can work.

The client can't do this reliably -- see the comment at
``webclient/index.html:3234-3238`` explaining why WebAudio-side echo
cancellation isn't trustworthy enough, which is why the client currently
drops all mic frames while the assistant speaks (no barge-in at all). The
server has something the browser doesn't: :class:`WebSocketStreamer` owns
both the mic path (``input_queue``) and the TTS path (``output_queue``) in
one process, so the reference signal (what we just sent) and the incoming
mic audio are in the same place at the same time.

**This is deliberately not acoustic echo cancellation.** AEC tries to
*subtract* the echo cleanly enough to recover a clean residual signal. That's
a much harder problem (needs to model the room, the speaker, the mic, and
adapt continuously) than what barge-in actually requires, which is just a
yes/no *decision*: "is there energy here that the TTS we just played does not
explain?" Getting away with the cheap decision problem instead of the hard
signal-recovery problem is the whole point of this module.

Decision rule
--------------
:meth:`EchoGate.note_playback` appends every chunk of outbound TTS audio,
raw, to a ring buffer (``_ref_buffer``) covering the last
``_REF_MAX_SECONDS`` seconds. :meth:`EchoGate.feed` takes one chunk of
incoming mic audio and computes the best *normalized* cross-correlation
coefficient (rho, in [0, 1]) between that chunk and same-length windows of
the reference buffer. Normalizing by the energy of both signals makes rho
scale-invariant, so playback volume and mic gain don't matter -- only
*shape* does.

Crucially, rho is **not** maximized over every possible delay. It is scored
in a narrow window around a *locked* delay estimate. Why that distinction is
load-bearing -- and why the first version of this module got it wrong -- is
the subject of "Delay search: lock, don't scan" below. Read that section
before changing anything here.

Why this rule handles the barge-in case (the one that matters): if the user
talks *over* the assistant, the mic signal is (approximately) the echo plus
an independent, uncorrelated speech signal. For two independent signals of
comparable energy, textbook correlation algebra says rho drops to roughly
``1 / sqrt(1 + speech_energy / echo_energy)`` -- e.g. ~0.71 at equal energy,
~0.34 if the user's voice is *louder* than the echo -- while a pure echo
scores ~1.0.

Operating envelope
------------------
This is a real constraint, separate from the delay-search defect below, and
it survives the redesign: the gate can only hear a barge-in that is loud
enough to pull rho below the threshold. Measured on **real audio** under the
locked narrow search (echo = ``batch1.wav`` at 0.4x with a 100 ms delay,
user = ``batch3.wav`` scaled to the stated ratio), as percentage of
barge-in frames that get through:

===================  ========  ========  ============  ========
user : echo at mic   thr 0.70  thr 0.80  thr **0.85**  thr 0.90
===================  ========  ========  ============  ========
0.25x                      0%        0%            0%        0%
0.50x                      0%        0%            0%       69%
0.75x                      4%       58%           87%       97%
1.00x                     52%       87%           94%       99%
1.25x                     76%       91%           94%       99%
1.50x                     82%       94%           96%       99%
2.00x                     84%       97%           99%      100%
===================  ========  ========  ============  ========

Note the direction: **raising** the threshold widens the envelope (a higher
bar to call something echo means more gets through). It is easy to get this
backwards.

The default is now **0.85**, raised from 0.7. The old 0.7 was the
equal-energy crossover: the user had to be at least as loud as the echo at
the mic to interrupt at all. That bit hardest in **near-field
configurations** -- phone speaker to phone mic, laptop speaker to laptop
mic -- which is this project's primary deployment shape. At 0.85 a user at
0.75x the echo's amplitude gets through 87% of frames, versus 4% at 0.7.
That is the usability win the redesign buys.

What it costs: a *coloured* echo path scores below 1.000, so a higher
threshold lets more of it slip. Measured true-echo drop rate by echo
degradation:

=================  =========  =========  =========
echo degradation   thr 0.80   thr 0.85   thr 0.90
=================  =========  =========  =========
clean (bit-exact)     97.1%      97.1%      97.1%
noise at -20 dB       97.1%      97.1%      97.1%
noise at -14 dB       97.1%      97.1%      97.1%
soft clipping         97.1%      97.1%      97.1%
1st-order lowpass     96.4%      93.4%      89.1%
=================  =========  =========  =========

(The residual ~2.9% at every setting is the lock-acquisition window -- the
first few chunks of a turn pass by design while the delay is unknown.)
Additive noise and clipping barely dent rho (>=0.977); only spectral
colouring does, and 0.85 keeps that case above 93%. 0.85 is the balance
point picked from these two tables. A severely spectrally-distorted echo
path (a differentiator was tested as an extreme) never clears
``_LOCK_PEAK_FLOOR`` at all, so the gate simply never locks and passes
everything -- the fail-open direction.

Both tables are pinned as executable contracts in ``test_echo_gate.py``
against the real WAVs, so a future retune shows in one test run exactly what
it trades away.

Delay search: lock, don't scan
------------------------------
The first version of this module used the ring buffer cap
(``_REF_MAX_SECONDS`` = 2.0s) as the delay search window: it took the *max*
of rho over every alignment of the mic chunk inside the reference buffer,
on the reasoning that this has "no coarse stride to tune and no risk of
missing the true lag". **That reasoning was wrong, and the resulting gate
would have dropped roughly 72% of genuine user speech.**

The error is a multiple-comparisons / extreme-value effect. A 512-sample
chunk against a 2s reference is a max over ~31,500 candidate alignments.
Speech is highly self-similar (voiced pitch periods, formant structure,
repeated phonemes), so *some* alignment of any speech chunk against any
other speech will happen to line up well. Taking the max over tens of
thousands of tries reliably finds one. Measured on real pocket-TTS audio,
correlating ``bench-wavs/batch3.wav`` against an unrelated
``bench-wavs/batch1.wav`` reference at 512-sample chunks:

=========  ===============  ==================  ====================
ref window  candidate lags  unrelated-speech    false-drop rate
                            median rho          at threshold 0.7
=========  ===============  ==================  ====================
2.00 s          31,489      0.854               **71.6%**
1.00 s          15,489      0.763               61.7%
0.50 s           7,489      0.644               35.5%
0.20 s           2,689      0.273               2.8%
0.10 s           1,089      0.248               0.7%
0.05 s             289      0.179               0.0%
=========  ===============  ==================  ====================

True-echo rho stays 1.000 at every window size, so this is purely a
false-positive problem, and **no threshold rescues the wide scan** --
unrelated speech peaks at 0.98 against a true echo's 1.000. The original
docstring's claimed "~0.15-0.2 noise floor" for unrelated audio was an
artifact of testing with *white noise*, which has essentially no
autocorrelation structure. That floor does reproduce (measured 0.160 on
synthetic noise) -- real speech simply does not behave like white noise.
The synthetic tests in ``test_echo_gate.py`` are retained, but relabelled:
they characterize white-noise behaviour, and are explicitly **not** the
deployability contract.

The fix is *not* to shrink the ring buffer. The buffer cap and the search
width were doing two different jobs under one constant, and shrinking it
would silently disable the gate by discarding the audio the true echo
actually matches. Measured true-echo rho with a shrunken buffer, by real
echo delay:

=======  ==============  =====  ======  ======  =======  =======  =======
buffer   max lag covered  D=0ms  D=20ms  D=50ms  D=100ms  D=150ms  D=200ms
=======  ==============  =====  ======  ======  =======  =======  =======
0.05 s        18 ms      1.000  0.942   0.382   0.175    0.354    0.619
0.10 s        68 ms      1.000  1.000   1.000   0.382    0.354    0.619
0.20 s       168 ms      1.000  1.000   1.000   1.000    1.000    0.619
0.50 s       468 ms      1.000  1.000   1.000   1.000    1.000    1.000
=======  ==============  =====  ======  ======  =======  =======  =======

Real echo delay (network jitter plus client audio buffering) is typically
100-200 ms, so a 0.05s buffer misses all of it.

So the two roles are now separate constants:

* ``_REF_MAX_SECONDS`` (2.0s) -- **buffer depth**: how much played audio we
  retain, kept generous so the true echo is always still in the buffer.
* ``_LAG_SEARCH_SAMPLES`` (400, i.e. +/-25 ms) -- **search width**: how many
  alignments we actually score. Keeping the candidate-lag count in the low
  hundreds is what restores discrimination.

Bridging the two is a **lock**, which exploits a fact the wide scan threw
away: echo delay is a physical property of the audio path and is stable
within a session, whereas a speech-vs-speech coincidence peak lands at a
*random* lag that jumps every chunk. Measured in a streaming simulation
(playback appended in lockstep with mic chunks, lag expressed in absolute
stream coordinates), consecutive-chunk lag agreement within +/-400 samples:

* true echo, every delay from 0 to 200 ms: **100%** agreement, and the
  recovered lag equals the true delay exactly
* unrelated speech: **35.5%** agreement, median jump 736 samples

Note that peak *sharpness* -- peak-to-median ratio of the rho curve, an
obvious-looking lock criterion -- was measured and **rejected**: it does not
separate the classes at all (unrelated speech 4.36, true echo 4.36-4.67).
Lag stability across time is the discriminator; single-chunk peak shape is
not. Don't re-derive the sharpness test.

The gate therefore runs a small state machine:

* **Unlocked** (no delay estimate yet): run the wide scan to *observe* the
  argmax lag, but **pass all audio that reaches the correlation step** -- an
  uncalibrated gate must not drop anything on the strength of a delay it has
  not established, matching this module's fail-open philosophy. A missed echo
  is far cheaper than deafness to the user.
* **Locking**: a chunk whose wide-scan peak clears ``_LOCK_PEAK_FLOOR`` is
  evidence; ``_LOCK_CONSECUTIVE`` consecutive such chunks agreeing on a lag
  within ``_LAG_SEARCH_SAMPLES`` establishes the lock. Evidence must be
  consecutive -- a single sub-floor chunk resets the candidate.
* **Locked**: score only within +/-``_LAG_SEARCH_SAMPLES`` of the locked lag,
  and slew the lock to each chunk's argmax so slow drift is tracked.
* **Unlock** on :meth:`reset`, on reference staleness, and after
  ``_UNLOCK_CONSECUTIVE`` chunks whose in-window peak falls below
  ``_UNLOCK_FLOOR`` -- the last case recovers from a lock that has become
  meaningless, e.g. because the client stopped sending mic frames for a
  while and the stream-coordinate anchor no longer holds.

**The silence rule is not part of this state machine and is not subject to
it.** The RMS check in :meth:`_score` runs *before* any lock-state logic and
returns "drop" in **every** state, unlocked included. It is a separate,
deliberate policy about what is worth handing to ASR (see the comment at the
check), not a claim about whether the chunk is echo. "Unlocked passes
everything" above is scoped to the correlation verdict only. This distinction
is easy to miss and has already caused one reviewer to measure the silent
lead-in of a test WAV, see chunks being dropped while unlocked, and file it
as a contradiction of the fail-open contract -- it is not one.

Design range: <=200 ms, cliff at the buffer depth
--------------------------------------------------
Rejection is **flat, not degrading**, across the whole realistic range and
well beyond it, then falls off a cliff at ``_REF_MAX_SECONDS``. Measured on
``bench-wavs/batch1.wav`` through the streaming path, with the leak cause
instrumented per frame:

========  =========  ==========  ==================================
delay     locked?    echo        why anything leaked
                     dropped
========  =========  ==========  ==================================
0-200 ms  yes        97.1%       lock acquisition only
800 ms    yes        96.6%       lock acquisition only
1200 ms   yes        96.2%       lock acquisition only
1800 ms   yes        96.3%       lock acquisition only
1900 ms   yes        76.0%       transition band -- mixed
>=2000 ms **never**  **0.0%**    no lock: passes everything
========  =========  ==========  ==================================

The cliff sits at 2.0 s because that *is* ``_REF_MAX_SECONDS``: once the
echo lags by more than the buffer depth, the audio it matches has already
been trimmed, so the wide scan cannot find a qualifying peak, no lock is ever
acquired, and the gate passes everything. Past the ceiling it fails open
cleanly rather than misbehaving -- the safe direction, and the reason this is
documented rather than defended against.

Note the mechanism, because the plausible-sounding one is wrong:
``_window_for`` returning ``None`` (the predicted window having slid out of
the retained buffer) is **not** what degrades this. It was instrumented and
fires **zero** times at every delay through 1800 ms, and only 3 times in the
1900 ms transition band. The operative mechanism above the ceiling is failure
to *acquire*, not failure to *score*.

The residual ~2.9% leak at every delay inside the range is **exactly the
``_LOCK_CONSECUTIVE`` frames** spent acquiring the lock at the start of a
turn -- instrumentation shows zero frames leaking because rho fell below the
threshold. It is invariant, not threshold-sensitive: lowering
``VOICE_ECHO_GATE_THRESHOLD`` will not recover those frames, and only a
smaller ``_LOCK_CONSECUTIVE`` would, at the cost of the false-lock margin
that consecutive agreement buys (see the lock table above).

The lag anchor is kept in **absolute stream coordinates** (total ref samples
appended minus total mic samples fed), not as an offset from the end of the
ring buffer. The send loop writes TTS in ~100 ms bursts while mic frames
arrive every 32 ms, so a buffer-end-relative offset would jitter by up to a
full send chunk (~1600 samples) -- far wider than the +/-400 search window,
which would break the lock. Absolute counters are immune to burstiness.

Cost: the locked path correlates a 512-sample chunk against a 1,312-sample
slice (801 candidate lags) instead of the full 32,000-sample buffer (31,489
lags) -- measured 0.043 ms versus 1.88 ms, a 44x saving. The wide scan now
runs only while acquiring a lock, so the steady-state gate is far cheaper
than the original as well as correct.

Silence during playback is treated as echo-tail (dropped), not user speech --
see the comment at the check itself for the reasoning. It's a deliberate
choice, not something the correlation math decides.

Observe mode: calibrate against a real room, not a transform
------------------------------------------------------------
Every number in the tables above comes from *synthetic* transforms of one
real WAV pair: a scaled, delayed, optionally filtered copy of the playback
signal standing in for the echo. Nothing here has been through air. The true
acoustic path -- phone speaker, room, phone mic, the client's own AEC and
AGC -- is exactly the thing most likely to move true-echo rho off the ~1.0
the tables assume, and it is unmeasurable from a transform.

Calibrating it needs the gate scoring live audio, which OFF/ON alone can't
give you: the only way to learn the real rho distribution with ON is to
enable dropping and discover the hard way whether it eats the user's speech.

``VOICE_ECHO_GATE=observe`` is the third state. It records the reference,
runs the *entire* scoring path -- lock acquisition, slew, unlock, silence
policy -- and logs what it found, but :meth:`feed` returns True
unconditionally. The user just uses the assistant normally for a day and the
log holds the real distribution across many turns, distances, and background
conditions. Threshold-setting then reads off data instead of a guess.

The gate cannot label the categories that matter (true echo vs barge-in vs
silence vs user-only) -- that needs to know whether a human was talking,
which is precisely what it is trying to infer. So the log line instead
carries enough state to reconstruct them offline: ``refage_ms`` says whether
the assistant was speaking, ``rms`` says whether anything was arriving, and
``mic`` (absolute sample index) makes chunks orderable and contiguous, so a
turn can be sliced out and its frames histogrammed together. Lines are
``key=value`` after a fixed ``echo_gate_calib`` prefix, on a dedicated
``echo_gate.calib`` logger, so a busy log greps clean.

**Volume.** A line per 512-sample chunk is ~31/s. Lines are emitted at full
rate only while a playback reference is fresh -- the window that carries all
the echo information, and bounded by how long the assistant actually
speaks. When nothing has played (no reference at all, so no rho exists to
record), lines are decimated by ``VOICE_ECHO_GATE_LOG_EVERY`` (default 32,
about one per second) purely so the idle stretches remain *countable*. The
cost is that idle-chunk counts are approximate to within that factor; no
part of the rho distribution is lost, because chunks with no reference have
no rho. Silence *during* playback is logged at full rate -- a calibration
run needs to know what fraction of a turn was silence, and the silence
policy drops those chunks in ON mode, so they are decisions, not idling.

Level is INFO, matching the near-miss logging already here: the data has to
survive at the live log level to be collected at all, and a dedicated logger
name means it can be silenced or routed to its own file without touching
anything else.

Design (mirrors ``wakeword_gate.py``; read that module first):

* **Env-flag gated, default OFF.** Controlled by ``VOICE_ECHO_GATE``; unset
  or falsy leaves ``feed()`` passing everything through, so a plain restart
  with no env changes is byte-identical to today's behaviour.
* **Three states, not two** -- see "Observe mode" below.
* **Construction never raises either.** Every numeric env var falls back to
  its documented default with a warning rather than throwing, and a
  threshold outside rho's [0, 1] range is clamped toward fail-open (see
  :func:`_clamp_threshold`). ``EchoGate()`` is built unguarded inside
  ``WebSocketStreamer.__init__``, so a ``ValueError`` here would take the
  whole assistant down at startup, before any of the fail-open machinery
  below can help. A typo in an env var must not be able to do that.
* **Fail open, permanently.** Any exception anywhere in scoring is logged
  once and the gate passes all audio, unconditionally, for the rest of the
  process -- a broken gate must never brick the assistant, and a half-broken
  gate that sometimes still drops real speech is worse than an inert one.
* **Lazy, dependency-free import.** ``numpy`` is only imported inside
  :meth:`EchoGate._score`, on first use that actually needs it -- this
  module (and its tests) import fine with neither ``speech_to_speech`` nor
  ``numpy`` installed. If numpy is missing, the first real :meth:`feed` call
  raises on that import, which the fail-open handler in :meth:`feed` catches
  like any other scoring exception.
* **Calibration affordance.** Correlations at or above ``_NEAR_MISS_FLOOR``
  that don't cross ``self.threshold`` are logged at INFO, mirroring
  wakeword's near-miss logging -- visible at the live log level so the
  threshold can be tuned against a real microphone later.
* **Cross-thread safe.** :meth:`note_playback` runs on the send-loop thread;
  :meth:`feed` runs on the audio-loop thread (receive path). Both touch
  ``_ref_buffer``/``_last_playback_ts``/``_ref_total`` and take ``_lock``
  around that access, but ``feed()`` only holds the lock long enough to
  snapshot the buffer -- the correlation itself runs unlocked so it never
  blocks the send loop. The mic-side coordinate ``_mic_total`` is advanced
  inside that same critical section, so the ``(ref_total, mic_total)`` pair
  the lag lock is anchored in is always mutually consistent. The rest of the
  lag state (``_locked_lag``, ``_cand_*``, ``_weak_hits``) is touched only by
  :meth:`feed`'s own thread, except that :meth:`reset` clears it -- which it
  does under ``_lock``. ``_fail_open`` is written only from :meth:`feed`'s
  own thread (a single reader/writer, per the above), so reading it in
  :meth:`feed` and :meth:`state` is lock-free by construction.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
# Calibration lines get their own logger and their own fixed line prefix so
# they can be grepped, silenced, or routed to a file independently of the
# gate's ordinary chatter. See "Observe mode" in the module docstring.
calib_logger = logging.getLogger("echo_gate.calib")

_TRUTHY = {"1", "true", "yes", "on"}
_OBSERVE = "observe"

_SAMPLE_RATE = 16000
# Buffer DEPTH only -- how much played audio we retain, kept generous so the
# true echo (typically 100-200ms behind) is always still here. This is NOT
# the delay search width; see _LAG_SEARCH_SAMPLES and the module docstring
# section "Delay search: lock, don't scan".
_REF_MAX_SECONDS = 2.0
_REF_MAX_SAMPLES = int(_REF_MAX_SECONDS * _SAMPLE_RATE)
# Delay search WIDTH: how many alignments we score once locked (+/-25ms at
# 16kHz). Low hundreds of candidate lags is what keeps unrelated speech from
# scoring high by coincidence -- the whole point of the redesign.
_LAG_SEARCH_SAMPLES = 400
# Lock acquisition: this many consecutive chunks whose wide-scan peak clears
# _LOCK_PEAK_FLOOR and whose argmax lags agree within _LAG_SEARCH_SAMPLES.
# Real echo agrees 100% of the time; unrelated speech 35.5% (measured), so
# consecutive agreement is the discriminator.
_LOCK_PEAK_FLOOR = 0.98
_LOCK_CONSECUTIVE = 4
# Lock release: an in-window peak this low, this many times running, means
# the locked lag has stopped meaning anything (e.g. the client paused mic
# frames, breaking the stream-coordinate anchor). Re-acquire from scratch.
_UNLOCK_FLOOR = 0.2
_UNLOCK_CONSECUTIVE = 8
# If nothing has played in this long, treat the reference as expired rather
# than correlate against stale audio from a much earlier turn.
_REF_STALE_S = 1.5
# int16 RMS below this counts as silence (roughly -56 dBFS) -- comfortably
# above quantization/DC noise, comfortably below any audible speech.
_SILENCE_RMS_FLOOR = 50.0
# Calibration affordance: log a correlation this high even when it doesn't
# cross the (higher) drop threshold, so near-misses leave evidence in the log.
_NEAR_MISS_FLOOR = 0.5
_DEFAULT_THRESHOLD = 0.85
_DEFAULT_LOG_EVERY = 32


def _env_number(name: str, default: float, cast: Any) -> Any:
    """Read a numeric env var, falling back to `default` on anything
    unparseable rather than raising.

    ``EchoGate()`` is constructed unguarded in ``WebSocketStreamer.__init__``,
    and the streamer's defensive import at ``websocket_streamer.py:27`` only
    catches import-time failures -- so a ``ValueError`` out of here takes the
    whole voice agent down at startup. That is the exact opposite of this
    module's fail-open contract, and it fails *before* anything is in place to
    fail open: a typo in a hand-edited systemd drop-in would mean the
    assistant simply does not boot. Every value here has a safe documented
    default, so warn and carry on."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using the default %r", name, raw, default)
        return default


def _clamp_threshold(value: float) -> float:
    """Bring a parseable-but-meaningless threshold back into rho's range.

    rho is a normalized correlation coefficient, so it is bounded [0, 1] by
    construction and a threshold outside that range is always a mistake. The
    two directions are not equally dangerous, so they are not treated alike:

    * **Above 1.0** -- no rho can ever reach it, so the gate never drops.
      That is the fail-open direction this module already prefers everywhere
      else, so it is honoured: clamp to 1.0 (which still drops a bit-exact
      echo scoring exactly 1.0) and warn. Silently rewriting it to the
      default would be worse -- a typo that reads as harmless would quietly
      *start* dropping audio.
    * **Below 0.0** -- every rho clears it, so the gate drops *everything*:
      total deafness to the user, the one failure the module docstring says
      this pipeline must not have. Clamping to 0.0 would preserve exactly
      that, so it is refused outright and the documented default is used.

    * **nan** -- must be tested for explicitly, because it is the one value
      that satisfies *neither* comparison: ``nan > 1.0`` and ``nan < 0.0``
      are both False, so it would sail past both checks above. The effect
      would be a gate that never drops (``rho >= nan`` is False for every
      rho) while ``state()`` still reports ``"gating"`` -- working to the
      operator, inert in fact, which is exactly the silent-no-op class this
      hardening exists to close. Fail-open, so it cannot deafen anyone, but
      it is refused: there is no direction to clamp nan toward.

    Note ``inf`` and ``-inf`` need no special case: ``inf > 1.0`` is True so
    it takes the clamp path, and ``-inf < 0.0`` is True so it takes the
    refuse path -- both land where the rules above say they should. (Note
    ``float("1e999")`` overflows to ``inf``, so it is covered by the same
    path.)

    Asymmetric on purpose. The rule is not "keep it in range", it is "clamp
    toward fail-open, never toward deafness"."""
    if math.isnan(value):
        logger.warning(
            "VOICE_ECHO_GATE_THRESHOLD is nan, which would silently disable dropping "
            "while still reporting as active; using the default %r instead",
            _DEFAULT_THRESHOLD,
        )
        return _DEFAULT_THRESHOLD
    if value > 1.0:
        logger.warning(
            "VOICE_ECHO_GATE_THRESHOLD=%r is above rho's maximum of 1.0; clamping to 1.0 "
            "(the gate will drop only a bit-exact echo)",
            value,
        )
        return 1.0
    if value < 0.0:
        logger.warning(
            "VOICE_ECHO_GATE_THRESHOLD=%r is negative, which would drop ALL mic audio; "
            "using the default %r instead",
            value,
            _DEFAULT_THRESHOLD,
        )
        return _DEFAULT_THRESHOLD
    return value


class EchoGate:
    """Scores incoming mic PCM against recently-sent TTS PCM to decide
    whether it's an echo (drop) or real user audio, including user speech
    layered on top of an echo (still pass)."""

    def __init__(self) -> None:
        flag = os.environ.get("VOICE_ECHO_GATE", "").strip().lower()
        # "off" | "on" | "observe". `enabled` is kept as the derived bool it
        # has always been -- "this gate is doing work" -- because observe does
        # record the reference and does score, it just never drops. Anything
        # reading `.enabled` to decide whether the gate is inert stays correct.
        self.mode = _OBSERVE if flag == _OBSERVE else ("on" if flag in _TRUTHY else "off")
        self.enabled = self.mode != "off"
        self.threshold = _clamp_threshold(
            _env_number("VOICE_ECHO_GATE_THRESHOLD", _DEFAULT_THRESHOLD, float)
        )
        # Decimation for calibration lines logged while nothing is playing.
        # max(1, ...) also absorbs 0 and negatives, which mean "log everything".
        self._log_every = max(1, int(_env_number("VOICE_ECHO_GATE_LOG_EVERY", _DEFAULT_LOG_EVERY, int)))
        self._idle_chunks = 0
        self._ref_buffer = bytearray()
        self._last_playback_ts = 0.0
        self._fail_open = False
        # Total samples ever appended to the reference. Monotonic, never
        # trimmed with the ring buffer -- it is the absolute stream coordinate
        # the lag lock is anchored in (see module docstring).
        self._ref_total = 0
        # Total mic samples ever fed. The other half of that coordinate.
        self._mic_total = 0
        # Lag lock state, all in absolute stream coordinates.
        self._locked_lag: int | None = None
        self._cand_lag: int | None = None
        self._cand_hits = 0
        self._weak_hits = 0
        # Guards _ref_buffer/_last_playback_ts/_ref_total across
        # note_playback() (send-loop thread) vs feed() (audio-loop thread).
        # The lag state (_locked_lag/_cand_*/_weak_hits/_mic_total) is touched
        # only by feed()'s own thread, but reset() can run from elsewhere, so
        # it is cleared under the same lock. See module docstring.
        self._lock = threading.Lock()

    def state(self) -> str:
        """Coarse status for the settings panel: ``"off"`` (not enabled),
        ``"gating"`` (a fresh playback reference exists -- mic audio is
        actively being compared against it), ``"observing"`` (same, but in
        observe mode, so nothing will be dropped), or ``"idle"`` (enabled, but
        nothing recent to gate against, so everything passes through).

        Observe deliberately gets its own word rather than reusing
        ``"gating"``: a panel that said "gating" while the gate cannot drop
        anything would be lying about the one thing an operator checks it
        for. It shares ``"idle"`` with ON, because with no reference the two
        modes genuinely do the same thing -- pass everything."""
        if not self.enabled:
            return "off"
        with self._lock:
            last_ts = self._last_playback_ts
        if last_ts and (time.monotonic() - last_ts) <= _REF_STALE_S:
            return "observing" if self.mode == _OBSERVE else "gating"
        return "idle"

    def note_playback(self, pcm: bytes) -> None:
        """Record a chunk of outbound TTS audio as it's sent to clients.
        Called from the send loop -- keep this cheap. No-ops when disabled
        so a shipped-off gate never pays even the bytearray append cost."""
        if not self.enabled or not pcm:
            return
        with self._lock:
            self._ref_buffer.extend(pcm)
            self._ref_total += len(pcm) // 2  # int16 mono
            self._last_playback_ts = time.monotonic()
            cap_bytes = _REF_MAX_SAMPLES * 2  # int16 mono
            if len(self._ref_buffer) > cap_bytes:
                del self._ref_buffer[: len(self._ref_buffer) - cap_bytes]

    def feed(self, pcm: bytes) -> bool:
        """Feed one chunk of incoming mic PCM. Returns True to pass it on to
        VAD/ASR, False to drop it as echo. Never raises -- any scoring
        failure fails open (returns True) for the rest of the session.

        In observe mode the full scoring path still runs (and logs), but the
        verdict is discarded and everything passes."""
        if not self.enabled or self._fail_open:
            return True
        try:
            passed = self._score(pcm)
            return True if self.mode == _OBSERVE else passed
        except Exception:
            logger.exception("echo gate scoring failed; failing open (passing all audio) for the rest of the session")
            self._fail_open = True
            return True

    def _score(self, pcm: bytes) -> bool:
        import numpy as np  # lazy: see module docstring

        mic = np.frombuffer(pcm, dtype=np.int16)
        if mic.size == 0:
            return True

        with self._lock:
            ref_snapshot = bytes(self._ref_buffer)
            last_ts = self._last_playback_ts
            ref_total = self._ref_total
            # Mic samples are contiguous real time: advance the absolute
            # coordinate for EVERY chunk we are handed, including silent ones
            # and ones we return early on, or the lag anchor drifts.
            mic_start = self._mic_total
            self._mic_total += mic.size

        if not ref_snapshot or (time.monotonic() - last_ts) > _REF_STALE_S:
            # Nothing recently played -- nothing to gate against. The delay
            # estimate belongs to that finished playback, so drop it too.
            self._unlock()
            if self.mode == _OBSERVE:
                # Decimated: no reference means no rho to record, so only the
                # chunk count is at stake. See "Volume" in the docstring.
                self._idle_chunks += 1
                if self._idle_chunks % self._log_every == 0:
                    self._calib(mic_start, self._rms(mic), last_ts, float("nan"), None, True, "no_ref")
            return True

        mic_rms = float(np.sqrt(np.mean(mic.astype(np.float64) ** 2)))
        if mic_rms < _SILENCE_RMS_FLOOR:
            # Silence while TTS is (recently) playing carries nothing worth
            # passing to ASR either way -- treat it as an echo-tail/inter-word
            # gap rather than user speech. This is a deliberate policy choice
            # (either answer is "safe" here), independent of the correlation
            # math below, so it applies in every lock state.
            #
            # Logged at full rate, unlike the no-reference case above: this is
            # a real drop decision, and a calibration run needs to know what
            # fraction of a turn it accounts for. `feed` still passes it in
            # observe -- observe never drops, silence policy included.
            if self.mode == _OBSERVE:
                self._calib(mic_start, mic_rms, last_ts, float("nan"), None, False, "silence")
            return False

        ref = np.frombuffer(ref_snapshot, dtype=np.int16)
        # Absolute stream index of ref[0], the oldest retained sample.
        ref_base = ref_total - ref.size

        if self._locked_lag is None:
            rho, lag = self._observe_for_lock(mic, ref, ref_base, mic_start)
            # Uncalibrated: never drop. See "Delay search: lock, don't scan".
            if self.mode == _OBSERVE:
                self._calib(mic_start, mic_rms, last_ts, rho, lag, True, "acquiring")
            return True

        lo, hi = self._window_for(self._locked_lag, mic, ref, ref_base, mic_start)
        if lo is None:
            # The locked lag points outside what the buffer still holds.
            self._note_weak()
            if self.mode == _OBSERVE:
                self._calib(mic_start, mic_rms, last_ts, float("nan"), self._locked_lag, True, "no_window")
            return True

        rho, idx = self._best_correlation(mic, ref, lo, hi)
        # idx is already an absolute offset into `ref` (not relative to lo).
        lag = mic_start - (ref_base + idx)
        if rho < _UNLOCK_FLOOR:
            self._note_weak()
        else:
            self._weak_hits = 0
            self._locked_lag = lag  # slew to track slow drift

        if self.mode == _OBSERVE:
            self._calib(mic_start, mic_rms, last_ts, rho, lag, rho < self.threshold, "scored")
        if rho >= self.threshold:
            logger.debug("echo gate: rho=%.3f lag=%d (dropped)", rho, lag)
            return False
        if rho >= _NEAR_MISS_FLOOR:
            logger.info("echo gate near miss: rho=%.3f (passed, below threshold %.2f)", rho, self.threshold)
        return True

    def _observe_for_lock(self, mic: Any, ref: Any, ref_base: int, mic_start: int) -> tuple[float, int | None]:
        """Wide-scan one chunk purely to gather delay evidence. Never decides
        pass/drop -- the caller passes the audio regardless.

        Returns the wide-scan ``(rho, lag)`` for observe-mode logging; the
        return value is ignored in every other mode, so this is reporting
        only. **That rho is not the locked rho**: it is a max over tens of
        thousands of alignments rather than 801, which is exactly the
        inflation "Delay search: lock, don't scan" is about. Calibration must
        histogram it separately -- ``reason=acquiring`` marks it."""
        rho, idx = self._best_correlation(mic, ref)
        if rho < _LOCK_PEAK_FLOOR:
            # Weak evidence breaks the run: lock evidence must be consecutive.
            self._cand_lag = None
            self._cand_hits = 0
            return rho, None
        lag = mic_start - (ref_base + idx)
        if self._cand_lag is not None and abs(lag - self._cand_lag) <= _LAG_SEARCH_SAMPLES:
            self._cand_hits += 1
        else:
            self._cand_hits = 1
        self._cand_lag = lag
        if self._cand_hits >= _LOCK_CONSECUTIVE:
            self._locked_lag = lag
            self._cand_lag = None
            self._cand_hits = 0
            self._weak_hits = 0
            logger.info("echo gate: locked echo delay at %d samples (%.0f ms)", lag, lag * 1000.0 / _SAMPLE_RATE)
        return rho, lag

    @staticmethod
    def _rms(mic: Any) -> float:
        import numpy as np  # lazy: see module docstring

        return float(np.sqrt(np.mean(mic.astype(np.float64) ** 2)))

    def _lock_state(self) -> str:
        if self._locked_lag is not None:
            return "locked"
        return "locking" if self._cand_hits else "unlocked"

    def _calib(
        self,
        mic_start: int,
        rms: float,
        last_ts: float,
        rho: float,
        lag: int | None,
        would_pass: bool,
        reason: str,
    ) -> None:
        """Emit one machine-parseable calibration record. Observe mode only --
        ON is deliberately left byte-identical by this slice.

        ``key=value`` after a fixed prefix, on the ``echo_gate.calib`` logger.
        ``verdict`` is what ON *would* have returned for this chunk at the
        current threshold, which is the whole point: it lets a threshold sweep
        be replayed offline against rho without re-deriving the policy."""
        age_ms = (time.monotonic() - last_ts) * 1000.0 if last_ts else float("inf")
        calib_logger.info(
            "echo_gate_calib t=%.3f mic=%d rms=%.1f refage_ms=%.0f rho=%.4f lock=%s lag=%s lag_ms=%s verdict=%s thr=%.2f reason=%s",
            time.time(),
            mic_start,
            rms,
            age_ms,
            rho,
            self._lock_state(),
            "nan" if lag is None else lag,
            "nan" if lag is None else "%.1f" % (lag * 1000.0 / _SAMPLE_RATE),
            "pass" if would_pass else "drop",
            self.threshold,
            reason,
        )

    @staticmethod
    def _window_for(lag: int, mic: Any, ref: Any, ref_base: int, mic_start: int) -> tuple[Any, Any]:
        """Index range into `ref` covering +/-_LAG_SEARCH_SAMPLES around the
        window that `lag` predicts. Returns (None, None) if that range has
        already slid out of the retained buffer."""
        centre = (mic_start - lag) - ref_base
        lo = max(0, centre - _LAG_SEARCH_SAMPLES)
        hi = min(ref.size - mic.size, centre + _LAG_SEARCH_SAMPLES)
        if hi < lo:
            return None, None
        return lo, hi

    def _note_weak(self) -> None:
        self._weak_hits += 1
        if self._weak_hits >= _UNLOCK_CONSECUTIVE:
            logger.info("echo gate: releasing stale delay lock after %d weak chunks", self._weak_hits)
            self._unlock()

    def _unlock(self) -> None:
        self._locked_lag = None
        self._cand_lag = None
        self._cand_hits = 0
        self._weak_hits = 0

    @staticmethod
    def _best_correlation(mic: Any, ref: Any, lo: int = 0, hi: int | None = None) -> tuple[float, int]:
        """Best normalized cross-correlation of `mic` against the same-length
        windows of `ref` starting at offsets ``lo..hi`` inclusive. Returns
        ``(rho, idx)``: rho in [0, 1], where 1.0 means that window matches
        `mic`'s shape exactly (up to scale), and idx the offset into `ref`
        where it was found.

        ``lo``/``hi`` default to the full buffer -- the *wide scan*, which is
        now used only to acquire a lock, never to decide pass/drop. See the
        module docstring: maximizing over the full buffer is precisely the
        defect this redesign fixes, so callers deciding a verdict must pass a
        narrow range.

        Isolated as its own static method so it can be monkeypatched to fail
        deliberately in tests, and so the numeric work is easy to spot
        separately from the bookkeeping in :meth:`_score`."""
        import numpy as np  # lazy: see module docstring

        frame_len = mic.size
        if ref.size < frame_len:
            return 0.0, 0

        last = ref.size - frame_len
        lo = max(0, min(lo, last))
        hi = last if hi is None else max(lo, min(hi, last))

        # Slice to the search range first, so a locked (narrow) scan costs
        # O(range * frame_len) rather than the full buffer.
        seg = ref[lo : hi + frame_len].astype(np.float64)
        mic_f = mic.astype(np.float64)
        # Dot product of `mic` against every same-length window of `seg`, in
        # one vectorized call -- no per-lag Python loop needed.
        raw = np.correlate(seg, mic_f, mode="valid")
        # Energy of each of those windows, via a cumulative sum (O(n)) rather
        # than recomputing a sum over `frame_len` samples per window.
        csum = np.concatenate(([0.0], np.cumsum(seg * seg)))
        ref_energy = csum[frame_len:] - csum[:-frame_len]
        mic_energy = float(np.dot(mic_f, mic_f))
        denom = np.sqrt(ref_energy * mic_energy) + 1e-9
        rho = np.abs(raw) / denom
        best = int(np.argmax(rho))
        return float(rho[best]), lo + best

    def reset(self) -> None:
        """Re-arm for a new session: drop the playback reference. Unlike
        ``wakeword_gate.WakewordGate.reset``, there's no per-session state to
        preserve on success here -- but ``_fail_open`` is deliberately NOT
        cleared, mirroring that same method: a scorer that already proved
        broken this run stays fail-open for the rest of the process rather
        than silently going back to gating on a detector that just failed."""
        with self._lock:
            self._ref_buffer.clear()
            self._last_playback_ts = 0.0
            # The delay lock describes the previous session's audio path;
            # a new session re-acquires it from scratch.
            self._unlock()
