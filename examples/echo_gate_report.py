"""Fixed-format summary of `echo_gate.calib` log lines.

Usage (as documented in README.md):

    journalctl -u patchbay -o cat | python3 examples/echo_gate_report.py

Reads from PATH or stdin (`-` or no argument). Stdlib only; Python ≥3.10.

With VOICE_OVERLAP_OBSERVE on, calib lines labelled `phase=playback` (mic
audio captured while the reply was playing) are also tabulated per playback
window, using the `overlap_observe window=<n> state=... t=<epoch>` lines the
streamer logs. Without such lines the output is unchanged.
"""

from __future__ import annotations

import math
import re
import sys
from typing import Sequence

REQUIRED_KEYS = {
    "t", "mic", "rms", "refage_ms", "rho", "lock", "lag", "lag_ms",
    "verdict", "thr", "reason",
}
REASONS = ("no_ref", "silence", "acquiring", "no_window", "scored")
THRESHOLDS = (0.70, 0.80, 0.85, 0.90)


def _percentile(values: list[float], p: float) -> str:
    """Nearest-rank percentile to 3 decimals, `-` when there is nothing to rank."""
    if not values:
        return "-"
    ranked = sorted(values)
    k = max(0, min(math.ceil(p / 100.0 * len(ranked)) - 1, len(ranked) - 1))
    return f"{ranked[k]:.3f}"


def _finite(lines: list[dict], key: str) -> list[float]:
    """Finite float values of `key`; non-finite ones never enter a percentile set."""
    out = []
    for line in lines:
        value = float(line[key])
        if math.isfinite(value):
            out.append(value)
    return out


def _would_drop(scored_rhos: list[float]) -> str:
    """Share of scored chunks a fixed threshold would have dropped."""
    parts = []
    for thr in THRESHOLDS:
        if scored_rhos:
            share = sum(1 for r in scored_rhos if r >= thr) / len(scored_rhos) * 100
            parts.append(f"{thr:.2f}={share:.1f}%")
        else:
            parts.append(f"{thr:.2f}=-")
    return " ".join(parts)


_WINDOW_RE = re.compile(r"overlap_observe window=(\d+) state=(start|end|cut) client=(\S+) t=(\S+)")
# Reasons that are only reached AFTER the gate's silence check, i.e. a chunk
# with real signal in it. `silence` is below the floor; `no_ref` is decimated
# and says nothing about the signal.
_NON_SILENT = ("acquiring", "no_window", "scored")


def parse_windows(text: str) -> list[dict]:
    """Playback windows from `overlap_observe window=` lines, ordered by start
    time. One record per `start`; the matching `end`/`cut` (same window number
    and client) closes it. A window never closed runs to the end of the log."""
    windows: list[dict] = []
    open_by_client: dict[str, dict] = {}
    for line in text.splitlines():
        m = _WINDOW_RE.search(line)
        if not m:
            continue
        n, state, client, t_raw = m.groups()
        try:
            t = float(t_raw)
        except ValueError:
            continue
        if state == "start":
            rec = {"n": int(n), "client": client, "t0": t, "t1": math.inf}
            windows.append(rec)
            open_by_client[client] = rec
        else:
            rec = open_by_client.get(client)
            if rec is not None and rec["n"] == int(n):
                rec["t1"] = t
                del open_by_client[client]
    return sorted(windows, key=lambda w: w["t0"])


def _longest_run(flags: list[bool]) -> int:
    best = run = 0
    for flag in flags:
        run = run + 1 if flag else 0
        best = max(best, run)
    return best


def _window_table(calib_lines: list[dict], windows: list[dict]) -> list[str]:
    """Per-window rows for `phase=playback` calib lines. A chunk belongs to the
    window whose [start, end] contains its `t`. Runs are consecutive chunks in
    log order; any chunk that does not qualify (including a silent one) ends a run."""
    playback = []
    for ln in calib_lines:
        if ln.get("phase") != "playback":
            continue
        try:
            playback.append((float(ln["t"]), ln))
        except ValueError:
            continue
    if not playback:
        return []
    header = ("window chunks non_silent locked_chunks would_pass_locked "
              "longest_would_pass_run unlocked_non_silent longest_unlocked_run")
    rows = ["playback-phase windows (chunks are 512 samples = 32 ms; non_silent excludes silence and no_ref):",
            header]
    claimed: set[int] = set()  # chunks (by index) inside at least one window
    for w in windows:
        idx = [i for i, (t, _) in enumerate(playback) if w["t0"] <= t <= w["t1"]]
        claimed.update(idx)
        chunks = [playback[i][1] for i in idx]
        non_silent = [ln["reason"] in _NON_SILENT for ln in chunks]
        locked = [ns and ln["lock"] == "locked" for ns, ln in zip(non_silent, chunks)]
        would_pass = [lk and ln["verdict"] == "pass" for lk, ln in zip(locked, chunks)]
        unlocked = [ns and ln["lock"] != "locked" for ns, ln in zip(non_silent, chunks)]
        rows.append(
            f"{w['n']} {len(chunks)} {sum(non_silent)} {sum(locked)} {sum(would_pass)} "
            f"{_longest_run(would_pass)} {sum(unlocked)} {_longest_run(unlocked)}"
        )
    outside = len(playback) - len(claimed)
    if outside or not windows:
        rows.append(f"playback-phase chunks outside any logged window: {outside}")
    return rows


def parse_lines(text: str) -> tuple[list[dict], int, int, int]:
    calib_lines = []
    malformed = 0
    lock_acquisitions = 0
    restarts = 0
    prev_mic = None
    for line in text.splitlines():
        if "echo gate: locked echo delay at" in line:
            lock_acquisitions += 1
        if "echo_gate_calib " not in line:
            continue
        payload = line.split("echo_gate_calib ", 1)[1]
        pairs = {}
        try:
            for part in payload.split():
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                pairs[k] = v
        except Exception:
            malformed += 1
            continue
        missing = REQUIRED_KEYS - pairs.keys()
        if missing:
            malformed += 1
            continue
        # Validate float parseability for selected keys
        try:
            float(pairs["rho"])
            float(pairs["refage_ms"])
            float(pairs["thr"])
        except ValueError:
            malformed += 1
            continue
        # mic as int
        try:
            mic_val = int(pairs["mic"])
        except ValueError:
            malformed += 1
            continue
        # Restart detection
        if prev_mic is not None and mic_val < prev_mic:
            restarts += 1
        prev_mic = mic_val
        calib_lines.append(pairs)
    return calib_lines, malformed, lock_acquisitions, restarts


def build_report(calib_lines: list[dict], malformed: int,
                 lock_acquisitions: int, restarts: int,
                 windows: list[dict] | None = None) -> str:
    counts = {name: 0 for name in REASONS}
    other = 0
    for line in calib_lines:
        if line["reason"] in counts:
            counts[line["reason"]] += 1
        else:
            other += 1

    # Ruling: only finite rho/refage_ms count toward n and percentiles; a
    # non-finite scored line still counts in the reason line above.
    scored = [ln for ln in calib_lines if ln["reason"] == "scored"]
    acquiring = [ln for ln in calib_lines if ln["reason"] == "acquiring"]
    scored_rho = _finite(scored, "rho")
    acquiring_rho = _finite(acquiring, "rho")
    refage = _finite(scored + acquiring, "refage_ms")
    refage_max = f"{max(refage):.3f}" if refage else "-"

    if lock_acquisitions > 0 or counts["scored"] > 0:
        verdict = (f"VERDICT: ECHO EVIDENCE: {lock_acquisitions} lock(s) acquired; "
                   "the scored rho distribution above is real echo-path data.")
    else:
        verdict = ("VERDICT: NO ECHO EVIDENCE: no mic chunk was ever scored against a "
                   "locked echo delay, so this log says nothing about how echo and "
                   "speech separate on your device.")

    reason_line = " ".join(f"{name}={counts[name]}" for name in REASONS)
    table = _window_table(calib_lines, windows or [])
    return "\n".join([
        f"echo_gate_report: {len(calib_lines)} calib lines ({malformed} malformed skipped), "
        f"{lock_acquisitions} lock acquisitions, {restarts} restarts",
        f"reason {reason_line} other={other}",
        f"scored rho (locked): n={len(scored_rho)} p10={_percentile(scored_rho, 10)} "
        f"p50={_percentile(scored_rho, 50)} p90={_percentile(scored_rho, 90)}",
        "acquiring rho (wide scan, not comparable to locked rho): "
        f"n={len(acquiring_rho)} p50={_percentile(acquiring_rho, 50)} "
        f"p90={_percentile(acquiring_rho, 90)}",
        f"refage_ms (scored+acquiring): n={len(refage)} p50={_percentile(refage, 50)} "
        f"p90={_percentile(refage, 90)} max={refage_max}",
        f"would drop (scored, rho >= thr): {_would_drop(scored_rho)}",
        *table,
        verdict,
    ])


def main(argv: Sequence[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = args[0] if args else "-"
    if path == "-" or not args:
        text = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    calib_lines, malformed, lock_acquisitions, restarts = parse_lines(text)
    if not calib_lines:
        sys.stderr.write(
            "no echo_gate_calib lines found: set VOICE_ECHO_GATE=observe, "
            "use the assistant, then pipe the pipeline's log in (README, Behaviour & safety knobs)\n"
        )
        return 2
    print(build_report(calib_lines, malformed, lock_acquisitions, restarts, parse_windows(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
