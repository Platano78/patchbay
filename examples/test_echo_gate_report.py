"""Tests for examples/echo_gate_report.py."""

from __future__ import annotations

import io
import re
import sys

from examples import echo_gate_report as rep


CALIB = ("echo_gate_calib t=1.0 mic={mic} rms=1.0 refage_ms={age} rho={rho} lock=locked "
         "lag=1 lag_ms=0.1 verdict=drop thr=0.85 reason={reason}")


def _line(mic=0, age="10", rho="0.9", reason="scored") -> str:
    return CALIB.format(mic=mic, age=age, rho=rho, reason=reason)


def _run(tmp_path, capsys, lines) -> tuple[int, str, str]:
    path = tmp_path / "log.txt"
    path.write_text("\n".join(lines) + "\n")
    code = rep.main([str(path)])
    out = capsys.readouterr()
    return code, out.out, out.err


# round-trip against real emitter format

def test_round_trip_against_real_emitter(monkeypatch, caplog, tmp_path, capsys):
    # Bind to the REAL emitter format (patches/echo_gate.py), not a fixture.
    monkeypatch.setenv("VOICE_ECHO_GATE", "observe")
    caplog.set_level("INFO")
    import patches.echo_gate as eg
    gate = eg.EchoGate()
    from patches.test_echo_gate import _noise_pcm, _chunks, _echo_frame, _lock_on_echo, SEND
    tts = _noise_pcm(SEND * 8, seed=99)
    for chunk in _chunks(tts, SEND):
        gate.note_playback(chunk)
    k = _lock_on_echo(gate, tts)
    gate.feed(_echo_frame(tts, 0, k))
    calib_msgs = [r.getMessage() for r in caplog.records if r.name == "echo_gate.calib"]
    lock_msgs = [r.getMessage() for r in caplog.records
                 if "locked echo delay" in r.getMessage()]
    assert calib_msgs, "real emitter produced no calib lines"
    assert lock_msgs, "real emitter produced no lock record"
    prefix = "Oct 06 12:00:00 host python3[1]: "
    code, out, _ = _run(tmp_path, capsys, [prefix + m for m in calib_msgs + lock_msgs])
    assert code == 0
    expected = {}
    for m in calib_msgs:
        reason = re.search(r"reason=(\S+)", m).group(1)
        expected[reason] = expected.get(reason, 0) + 1
    names = ("no_ref", "silence", "acquiring", "no_window", "scored")
    other = sum(v for r, v in expected.items() if r not in names)
    reason_line = "reason " + " ".join(f"{n}={expected.get(n, 0)}" for n in names) + f" other={other}"
    assert reason_line in out.splitlines()
    assert f"{len(lock_msgs)} lock acquisitions" in out
    assert "(0 malformed skipped)" in out
    assert "VERDICT: ECHO EVIDENCE" in out


def test_no_lock_is_no_echo_evidence(tmp_path, capsys) -> None:
    code, out, _ = _run(tmp_path, capsys, [
        "echo_gate_calib t=1.0 mic=0 rms=0.0 refage_ms=inf rho=nan lock=unlocked lag=nan lag_ms=nan verdict=pass thr=0.85 reason=no_ref",
        "echo_gate_calib t=2.0 mic=1 rms=0.0 refage_ms=inf rho=nan lock=unlocked lag=nan lag_ms=nan verdict=pass thr=0.85 reason=silence",
        "echo_gate_calib t=3.0 mic=2 rms=0.0 refage_ms=40 rho=0.5 lock=unlocked lag=nan lag_ms=nan verdict=pass thr=0.85 reason=acquiring",
    ])
    assert code == 0
    lines = out.splitlines()
    assert "scored rho (locked): n=0 p10=- p50=- p90=-" in lines
    assert "would drop (scored, rho >= thr): 0.70=- 0.80=- 0.85=- 0.90=-" in lines
    assert lines[-1].startswith("VERDICT: NO ECHO EVIDENCE:")
    assert all("nan" not in ln for ln in lines)


def test_all_empty_percentile_lines_use_dash(tmp_path, capsys) -> None:
    _, out, _ = _run(tmp_path, capsys, [_line(reason="no_ref", rho="nan", age="inf")])
    lines = out.splitlines()
    assert "acquiring rho (wide scan, not comparable to locked rho): n=0 p50=- p90=-" in lines
    assert "refage_ms (scored+acquiring): n=0 p50=- p90=- max=-" in lines
    assert all("nan" not in ln and "inf" not in ln for ln in lines)


def test_prefixes_and_extra_keys_tolerated() -> None:
    payload = "echo_gate_calib t=1.0 mic=10 rms=5.0 refage_ms=20 rho=0.9 lock=locked lag=5 lag_ms=0.3 verdict=drop thr=0.85 reason=scored phase=playback"
    prefixes = [
        "",
        "2026-10-06 12:00:00,000 - echo_gate.calib - INFO - ",
        "Oct 06 12:00:00 host python3[1]: ",
    ]
    for p in prefixes:
        line = p + payload
        calib, malformed, _, _ = rep.parse_lines(line)
        assert len(calib) == 1
        assert malformed == 0
        assert calib[0]["phase"] == "playback"


def test_malformed_lines_counted_not_fatal(tmp_path, capsys) -> None:
    code, out, _ = _run(tmp_path, capsys, [
        "echo_gate_calib t=1.0 mic=0 rms=1.0 refage_ms=10 rho=0.9 lock=locked lag=1 lag_ms=0.1 thr=0.85 reason=scored",
        _line(mic=10, rho="abc"),
        _line(mic=20),
    ])
    assert code == 0
    assert "1 calib lines (2 malformed skipped)" in out


def test_no_calib_lines_exits_2(tmp_path, capsys, monkeypatch) -> None:
    sentence = ("no echo_gate_calib lines found: set VOICE_ECHO_GATE=observe, use the assistant, "
                "then pipe the pipeline's log in (README, Behaviour & safety knobs)\n")
    path = tmp_path / "unrelated.txt"
    path.write_text("some unrelated log\nanother line\n")
    assert rep.main([str(path)]) == 2
    cap = capsys.readouterr()
    assert cap.err == sentence
    assert "VOICE_ECHO_GATE=observe" in cap.err
    assert cap.out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("some unrelated log\n"))
    assert rep.main(["-"]) == 2
    cap = capsys.readouterr()
    assert "VOICE_ECHO_GATE=observe" in cap.err and cap.out == ""


def test_non_finite_values_excluded_from_percentiles(tmp_path, capsys) -> None:
    code, out, _ = _run(tmp_path, capsys, [
        _line(mic=0, age="inf", rho="nan"),
        _line(mic=1, age="20", rho="0.9"),
    ])
    assert code == 0
    lines = out.splitlines()
    assert "scored=2" in lines[1]
    assert lines[2].startswith("scored rho (locked): n=1 ")
    assert lines[4].startswith("refage_ms (scored+acquiring): n=1 ")
    assert all("nan" not in ln and "inf" not in ln for ln in lines)


def test_fixed_format_exact_lines(tmp_path, capsys) -> None:
    code, out, _ = _run(tmp_path, capsys, [
        _line(mic=0, age="inf", rho="nan", reason="no_ref"),
        _line(mic=10, age="40", rho="0.5", reason="acquiring"),
        _line(mic=20, age="60", rho="0.7", reason="acquiring"),
        _line(mic=30, age="20", rho="0.60"),
        _line(mic=40, age="30", rho="0.90"),
        _line(mic=50, age="10", rho="0.82"),
        _line(mic=60, age="20", rho="0.70"),
        "Oct 06 12:00:00 host python3[1]: echo gate: locked echo delay at 5.0 ms",
    ])
    assert code == 0
    assert out == (
        "echo_gate_report: 7 calib lines (0 malformed skipped), 1 lock acquisitions, 0 restarts\n"
        "reason no_ref=1 silence=0 acquiring=2 no_window=0 scored=4 other=0\n"
        "scored rho (locked): n=4 p10=0.600 p50=0.700 p90=0.900\n"
        "acquiring rho (wide scan, not comparable to locked rho): n=2 p50=0.500 p90=0.700\n"
        "refage_ms (scored+acquiring): n=6 p50=20.000 p90=60.000 max=60.000\n"
        "would drop (scored, rho >= thr): 0.70=75.0% 0.80=50.0% 0.85=25.0% 0.90=25.0%\n"
        "VERDICT: ECHO EVIDENCE: 1 lock(s) acquired; the scored rho distribution above is real echo-path data.\n"
    )


def test_restart_detected() -> None:
    calib, malformed, _, restarts = rep.parse_lines(
        "echo_gate_calib t=1.0 mic=5000 rms=1.0 refage_ms=10 rho=0.9 lock=locked lag=1 lag_ms=0.1 verdict=drop thr=0.85 reason=scored\n"
        "echo_gate_calib t=2.0 mic=6000 rms=1.0 refage_ms=10 rho=0.9 lock=locked lag=1 lag_ms=0.1 verdict=drop thr=0.85 reason=scored\n"
        "echo_gate_calib t=3.0 mic=100 rms=1.0 refage_ms=10 rho=0.9 lock=locked lag=1 lag_ms=0.1 verdict=drop thr=0.85 reason=scored"
    )
    assert restarts == 1


def test_lock_without_scored_lines_is_echo_evidence(tmp_path, capsys) -> None:
    code, out, _ = _run(tmp_path, capsys, [
        "echo gate: locked echo delay at 480 samples (10 ms)",
        _line(age="40", rho="0.5", reason="acquiring"),
    ])
    assert code == 0
    verdict = out.splitlines()[-1]
    assert verdict.startswith("VERDICT: ECHO EVIDENCE:")
    assert "1 lock(s) acquired" in verdict


def test_scored_lines_without_lock_is_echo_evidence(tmp_path, capsys) -> None:
    code, out, _ = _run(tmp_path, capsys, [_line(rho="0.9", reason="scored")])
    assert code == 0
    verdict = out.splitlines()[-1]
    assert verdict.startswith("VERDICT: ECHO EVIDENCE:")
    assert "0 lock(s) acquired" in verdict


# ── per-window table for phase=playback lines (floor-management slice 2) ──

WIN = "overlap_observe window={n} state={state} client=1 t={t}"
PB = ("echo_gate_calib t={t} mic={mic} rms=900.0 refage_ms=40 rho={rho} lock={lock} lag=1 lag_ms=0.1 "
      "verdict={verdict} thr=0.85 reason={reason} phase=playback")


def _pb(t, mic, reason="scored", lock="locked", verdict="pass", rho="0.2"):
    return PB.format(t=t, mic=mic, reason=reason, lock=lock, verdict=verdict, rho=rho)


def _table(out: str) -> list[list[str]]:
    lines = out.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith("window chunks"))
    rows = []
    for ln in lines[i + 1:]:
        if ln.startswith("VERDICT"):
            break
        rows.append(ln.split())
    return rows


def test_window_table_columns_and_runs(tmp_path, capsys) -> None:
    lines = [
        WIN.format(n=1, state="start", t="10.0"),
        # in window 1, log order: pass, pass, drop (breaks run), silence (breaks), pass, unlocked, unlocked, unlocked
        _pb("10.1", 0),
        _pb("10.2", 1),
        _pb("10.3", 2, verdict="drop", rho="0.95"),
        _pb("10.4", 3, reason="silence", verdict="drop", rho="nan"),
        _pb("10.5", 4),
        _pb("10.6", 5, reason="acquiring", lock="unlocked", rho="0.4"),
        _pb("10.7", 6, reason="acquiring", lock="locking", rho="0.4"),
        _pb("10.8", 7, reason="acquiring", lock="unlocked", rho="0.4"),
        WIN.format(n=1, state="end", t="11.0"),
        WIN.format(n=2, state="start", t="20.0"),
        _pb("20.1", 8, reason="silence", verdict="drop", rho="nan"),
        WIN.format(n=2, state="cut", t="21.0"),
    ]
    code, out, _ = _run(tmp_path, capsys, lines)
    assert code == 0
    assert _table(out) == [
        # window chunks non_silent locked would_pass_locked longest_would_pass unlocked_ns longest_unlocked
        ["1", "8", "7", "4", "3", "2", "3", "3"],
        ["2", "1", "0", "0", "0", "0", "0", "0"],
    ]


def test_window_table_is_ordered_by_time_and_excludes_lines_outside_windows(tmp_path, capsys) -> None:
    lines = [
        WIN.format(n=2, state="start", t="50.0"),
        _pb("50.1", 5),
        WIN.format(n=2, state="end", t="51.0"),
        WIN.format(n=1, state="start", t="10.0"),
        _pb("10.1", 1),
        WIN.format(n=1, state="end", t="11.0"),
        _pb("99.0", 9),  # outside every window
    ]
    _, out, _ = _run(tmp_path, capsys, lines)
    rows = _table(out)
    assert [r[0] for r in rows[:2]] == ["1", "2"]
    assert "playback-phase chunks outside any logged window: 1" in out


def test_no_playback_lines_means_no_table_and_unchanged_output(tmp_path, capsys) -> None:
    lines = [_line(mic=0), WIN.format(n=1, state="start", t="1.0"), WIN.format(n=1, state="end", t="2.0")]
    _, out, _ = _run(tmp_path, capsys, lines)
    assert "window chunks" not in out
    assert out.splitlines()[-1].startswith("VERDICT")


def test_unclosed_window_runs_to_the_end_of_the_log(tmp_path, capsys) -> None:
    lines = [WIN.format(n=3, state="start", t="5.0"), _pb("5.1", 0), _pb("900.0", 1)]
    _, out, _ = _run(tmp_path, capsys, lines)
    assert _table(out)[0][:3] == ["3", "2", "2"]


def test_playback_lines_without_any_window_lines_are_reported_not_dropped(tmp_path, capsys) -> None:
    _, out, _ = _run(tmp_path, capsys, [_pb("1.0", 0), _pb("1.1", 1)])
    assert "playback-phase chunks outside any logged window: 2" in out


def test_overlapping_windows_never_give_a_negative_outside_count(tmp_path, capsys) -> None:
    """Two clients' windows overlap in time and a playback line carries no client id,
    so a chunk can sit inside both. It is still one chunk: counted once as claimed."""
    lines = [
        "overlap_observe window=1 state=start client=1 t=10.0",
        "overlap_observe window=1 state=start client=2 t=10.5",
        _pb("11.0", 0), _pb("11.1", 1), _pb("11.2", 2),  # inside both windows
        "overlap_observe window=1 state=end client=1 t=12.0",
        "overlap_observe window=1 state=end client=2 t=12.5",
        _pb("99.0", 3),  # inside neither
    ]
    _, out, _ = _run(tmp_path, capsys, lines)
    outside = [ln for ln in out.splitlines() if "outside any logged window" in ln]
    assert outside == ["playback-phase chunks outside any logged window: 1"]
    assert not re.search(r"-\d", outside[0])
    assert [r[1] for r in _table(out)[:2]] == ["3", "3"]  # each row still lists the shared chunks
