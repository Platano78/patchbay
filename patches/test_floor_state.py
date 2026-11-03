"""Unit tests for floor_state.py (floor-management slice 2). Pure logic, injected clock."""

from __future__ import annotations

import math
import threading

from patches.floor_state import FloorState


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _fs():
    clock = _Clock()
    return FloorState(clock=clock), clock


def test_fresh_state_is_quiet_forever():
    fs, _ = _fs()
    assert fs.any_client_playing() is False
    assert fs.user_speaking is False
    assert fs.is_playing("a") is False
    assert fs.mutual_quiet_s() == math.inf
    assert fs.playback_times("a") == (None, None)


def test_playback_start_end_per_client_with_times():
    fs, clock = _fs()
    fs.playback_start("a")
    clock.t = 103.0
    assert fs.is_playing("a") and not fs.is_playing("b")
    assert fs.any_client_playing()
    fs.playback_stop("a")
    assert not fs.is_playing("a") and not fs.any_client_playing()
    assert fs.playback_times("a") == (100.0, 103.0)


def test_cut_is_the_same_transition_as_end():
    fs, _ = _fs()
    fs.playback_start("a")
    fs.playback_stop("a")  # a `cut` frame maps to playback_stop as well
    assert not fs.is_playing("a")


def test_one_client_stopping_does_not_stop_another():
    fs, _ = _fs()
    fs.playback_start("a")
    fs.playback_start("b")
    fs.playback_stop("a")
    assert fs.any_client_playing() and fs.is_playing("b")


def test_disconnect_clears_only_that_client():
    fs, _ = _fs()
    fs.playback_start("a")
    fs.playback_start("b")
    fs.drop_client("a")
    assert not fs.is_playing("a") and fs.is_playing("b")
    fs.drop_client("b")
    assert not fs.any_client_playing()
    fs.drop_client("never-seen")  # no error


def test_user_speaking_set_and_cleared():
    fs, _ = _fs()
    fs.user_speech_started()
    assert fs.user_speaking
    fs.user_speech_stopped()
    assert not fs.user_speaking


def test_mutual_quiet_is_zero_while_user_speaks_or_client_plays():
    fs, clock = _fs()
    fs.user_speech_started()
    clock.t += 50
    assert fs.mutual_quiet_s() == 0.0
    fs.user_speech_stopped()
    fs.playback_start("a")
    clock.t += 50
    assert fs.mutual_quiet_s() == 0.0


def test_mutual_quiet_counts_from_the_latest_event():
    fs, clock = _fs()
    fs.user_speech_started()
    clock.t = 110.0
    fs.user_speech_stopped()  # user quiet since 110
    clock.t = 112.0
    fs.playback_start("a")
    clock.t = 120.0
    fs.playback_stop("a")  # playback ended at 120: the later of the two
    assert fs.mutual_quiet_s(now=125.0) == 5.0
    clock.t = 130.0
    assert fs.mutual_quiet_s() == 10.0


def test_audio_sent_counts_as_not_quiet_until_the_client_reports():
    fs, clock = _fs()
    fs.user_speech_started()
    fs.user_speech_stopped()  # at 100
    clock.t = 108.0
    fs.audio_sent()  # server just sent audio the client has not acknowledged
    assert fs.mutual_quiet_s(now=109.0) == 1.0


def test_mutual_quiet_never_negative():
    fs, _ = _fs()
    fs.user_speech_stopped()
    assert fs.mutual_quiet_s(now=50.0) == 0.0


def test_thread_safe_under_contention():
    fs = FloorState()

    def work(name):
        for _ in range(500):
            fs.playback_start(name)
            fs.mutual_quiet_s()
            fs.playback_stop(name)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not fs.any_client_playing()
