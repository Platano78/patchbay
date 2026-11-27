"""sports_scores: ESPN's public scoreboard JSON turned into short spoken lines.

Run from repo root: python3 -m pytest patches/test_sports_scores.py -v

sports_fixtures/espn_nba_scoreboards.json holds trimmed recordings of real
scoreboard responses for three consecutive days (a final, a live game and a
scheduled game on the middle day). No network in tests; the server's local
timezone is pinned and "today" is faked.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import time
import types
from pathlib import Path

import pytest

from patches import phone_context


def _install_voice_tools_stub():
    pkg = sys.modules.get("speech_to_speech")
    if pkg is None:
        pkg = types.ModuleType("speech_to_speech")
        sys.modules["speech_to_speech"] = pkg
    sys.modules["speech_to_speech.phone_context"] = phone_context
    pkg.phone_context = phone_context


_install_voice_tools_stub()

from patches import voice_tools  # noqa: E402

_BOARDS = json.loads((Path(__file__).parent / "sports_fixtures" / "espn_nba_scoreboards.json").read_text())
_TODAY = dt.date(2026, 11, 24)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _install(monkeypatch, boards=None, error=None):
    boards = _BOARDS if boards is None else boards
    calls: list[tuple[str, dict]] = []

    class _Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            calls.append((url, params))
            if error is not None:
                raise error
            return _Resp(copy.deepcopy(boards.get(params["dates"], {"events": []})))

    monkeypatch.setattr(voice_tools.httpx, "Client", _Client)
    return calls


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    monkeypatch.setattr(voice_tools, "_today", lambda: _TODAY)
    yield
    monkeypatch.undo()
    time.tzset()


def test_a_final_is_spoken_winner_first_with_the_date(monkeypatch):
    _install(monkeypatch)
    out = voice_tools._run_sports_scores("nba")
    assert out.startswith("NBA games, Tuesday, November 24:")
    assert "Pistons 104, Magic 86, final." in out


def test_a_live_game_has_the_score_and_the_period_clock(monkeypatch):
    _install(monkeypatch)
    assert "Celtics 109, Wizards 96, live, 8:12 - 4th." in voice_tools._run_sports_scores("nba")


def test_a_scheduled_game_has_its_start_in_server_local_time(monkeypatch):
    _install(monkeypatch)
    assert "Mavericks at Nuggets, 10:00 PM." in voice_tools._run_sports_scores("nba")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    assert "Mavericks at Nuggets, 7:00 PM." in voice_tools._run_sports_scores("nba")


def test_request_url_and_date_param(monkeypatch):
    calls = _install(monkeypatch)
    voice_tools._run_sports_scores("nba")
    voice_tools._run_sports_scores("epl", day="2026-11-25")
    assert calls[0] == ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", {"dates": "20261124"})
    assert calls[1] == ("https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard", {"dates": "20261125"})


@pytest.mark.parametrize(
    "league,path",
    [
        ("nba", "basketball/nba"), ("nfl", "football/nfl"), ("mlb", "baseball/mlb"), ("nhl", "hockey/nhl"),
        ("wnba", "basketball/wnba"), ("mls", "soccer/usa.1"), ("ncaaf", "football/college-football"),
        ("ncaam", "basketball/mens-college-basketball"), ("epl", "soccer/eng.1"),
    ],
)
def test_every_league_maps_to_its_espn_path(monkeypatch, league, path):
    calls = _install(monkeypatch)
    voice_tools._run_sports_scores(league)
    assert calls[0][0] == f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


def test_day_words_and_iso_dates(monkeypatch):
    calls = _install(monkeypatch)
    for day in ("yesterday", "tomorrow", "2026-11-25", " Tomorrow "):
        voice_tools._run_sports_scores("nba", day=day)
    assert [p["dates"] for _, p in calls] == ["20261123", "20261125", "20261125", "20261125"]


def test_unreadable_day_is_refused_without_a_request(monkeypatch):
    calls = _install(monkeypatch)
    out = voice_tools._run_sports_scores("nba", day="next tuesday")
    assert "today, yesterday, tomorrow or a date like 2026-11-24" in out
    assert calls == []


def test_unknown_league_is_refused_with_the_choices(monkeypatch):
    calls = _install(monkeypatch)
    out = voice_tools._run_sports_scores("cricket")
    assert "nba, nfl, mlb, nhl, wnba, mls, ncaaf, ncaam, epl" in out
    assert calls == []


def test_no_games_says_so_with_the_date(monkeypatch):
    _install(monkeypatch)
    assert voice_tools._run_sports_scores("nba", day="2026-12-25") == "No NBA games on Friday, December 25."


def test_at_most_eight_games_and_the_rest_counted(monkeypatch):
    board = {"events": [copy.deepcopy(_BOARDS["20261123"]["events"][0]) for _ in range(11)]}
    _install(monkeypatch, {"20261124": board})
    out = voice_tools._run_sports_scores("nba")
    assert out.count("final.") == 8
    assert "and 3 more." in out


def test_team_filter_matches_name_city_abbreviation_and_full_name(monkeypatch):
    _install(monkeypatch)
    for team in ("pistons", "DETROIT", "det", "Detroit Pistons"):
        out = voice_tools._run_sports_scores("nba", team)
        assert "Pistons 104, Magic 86, final." in out, team
        assert "Celtics" not in out and "Mavericks" not in out, team


def test_team_filter_does_not_match_inside_other_words(monkeypatch):
    _install(monkeypatch)
    out = voice_tools._run_sports_scores("nba", "at")
    assert "Pistons" not in out


def test_team_with_no_game_that_day_looks_a_day_back_and_forward(monkeypatch):
    _install(monkeypatch)
    out = voice_tools._run_sports_scores("nba", "Heat")
    assert "No Heat game on Tuesday, November 24." in out
    assert "Wednesday, November 25" in out
    assert "Heat at 76ers, 5:00 PM." in out
    out = voice_tools._run_sports_scores("nba", "Grizzlies")
    assert "Monday, November 23" in out and "Grizzlies" in out and "final." in out


def test_team_with_no_game_in_three_days_says_so(monkeypatch):
    _install(monkeypatch)
    out = voice_tools._run_sports_scores("nba", "Lakers")
    assert out == "No Lakers game on Tuesday, November 24, or the day before or after."


def test_http_failure_goes_through_the_failure_result_path(monkeypatch):
    voice_tools._ARMED_NAMES.clear()
    _install(monkeypatch, error=voice_tools.httpx.ConnectError("down"))
    out = voice_tools.execute("sports_scores", {"league": "nba"})
    assert out.startswith("Couldn't complete the sports scores lookup: it failed.")


def test_execute_passes_team_and_day_and_stamps_the_result(monkeypatch):
    voice_tools._ARMED_NAMES.clear()
    calls = _install(monkeypatch)
    out = voice_tools.execute("sports_scores", {"league": "nba", "team": "pistons", "day": "yesterday"})
    assert calls[0][1] == {"dates": "20261123"}
    assert "Checked at" in out


def test_execute_without_a_league_asks_for_one(monkeypatch):
    voice_tools._ARMED_NAMES.clear()
    assert voice_tools.execute("sports_scores", {}) == "I need a league."


def test_tool_is_registered_described_and_armed():
    tool = next(t for t in voice_tools.TOOL_DEFS if t["name"] == "sports_scores")
    props = tool["parameters"]["properties"]
    assert props["league"]["enum"] == ["nba", "nfl", "mlb", "nhl", "wnba", "mls", "ncaaf", "ncaam", "epl"]
    assert tool["parameters"]["required"] == ["league"]
    assert {"team", "day"} <= set(props)
    assert "sports_scores" in voice_tools.CORE_TOOLS
    assert "sports_scores" in voice_tools._TIME_SENSITIVE_BUILTINS
    web = next(t for t in voice_tools.TOOL_DEFS if t["name"] == "web_search")
    assert "sports_scores" in web["description"]


# ── city aliases ──────────────────────────────────────────────────────

def _team(location, name, abbr):
    return {"location": location, "name": name, "abbreviation": abbr,
            "displayName": f"{location} {name}", "shortDisplayName": name}


def _event(away, home, *, state="post", completed=True, type_name="STATUS_FINAL", description="Final",
           detail="Final", scores=("100", "90"), date="2026-11-25T01:00Z"):
    return {
        "date": date,
        "name": f"{away['displayName']} at {home['displayName']}",
        "status": {"displayClock": "0.0", "period": 4, "type": {
            "name": type_name, "state": state, "completed": completed, "description": description, "shortDetail": detail}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "score": scores[1], "team": home},
            {"homeAway": "away", "score": scores[0], "team": away},
        ]}],
    }


_LAKERS = _team("Los Angeles", "Lakers", "LAL")
_CLIPPERS = _team("LA", "Clippers", "LAC")
_KNICKS = _team("New York", "Knicks", "NY")
_CELTICS = _team("Boston", "Celtics", "BOS")
_NETS = _team("Brooklyn", "Nets", "BKN")
_SUNS = _team("Phoenix", "Suns", "PHX")
_WIZARDS = _team("Washington", "Wizards", "WSH")
_SIXERS = _team("Philadelphia", "76ers", "PHI")
_WARRIORS = _team("Golden State", "Warriors", "GS")
_KINGS = _team("Sacramento", "Kings", "SAC")


def _board(*events):
    return {"20261124": {"events": list(events)}}


@pytest.mark.parametrize("query", ["LA", "la", "L.A.", "Los Angeles", "los  angeles"])
def test_la_in_any_spelling_finds_both_teams_of_the_city(monkeypatch, query):
    _install(monkeypatch, _board(_event(_SUNS, _LAKERS), _event(_CLIPPERS, _CELTICS), _event(_NETS, _KNICKS)))
    out = voice_tools._run_sports_scores("nba", query)
    assert "Lakers" in out and "Clippers" in out
    assert "Nets" not in out


@pytest.mark.parametrize("query", ["NY", "n.y.", "NYC", "New York"])
def test_new_york_aliases(monkeypatch, query):
    _install(monkeypatch, _board(_event(_SUNS, _LAKERS), _event(_NETS, _KNICKS)))
    out = voice_tools._run_sports_scores("nba", query)
    assert "Knicks" in out and "Lakers" not in out


@pytest.mark.parametrize(
    "query,away,home,expected",
    [
        ("SF", _SUNS, _team("San Francisco", "Giants", "SF"), "Giants"),
        ("San Francisco", _SUNS, _team("San Francisco", "Giants", "SF"), "Giants"),
        ("DC", _WIZARDS, _SUNS, "Wizards"),
        ("D.C.", _WIZARDS, _SUNS, "Wizards"),
        ("Philly", _SIXERS, _SUNS, "76ers"),
        ("Vegas", _team("Las Vegas", "Aces", "LV"), _SUNS, "Aces"),
        ("Las Vegas", _team("Las Vegas", "Aces", "LV"), _SUNS, "Aces"),
    ],
)
def test_other_city_aliases(monkeypatch, query, away, home, expected):
    _install(monkeypatch, _board(_event(away, home), _event(_NETS, _CELTICS)))
    out = voice_tools._run_sports_scores("nba", query)
    assert expected in out and "Nets" not in out


def test_an_alias_does_not_match_unrelated_teams(monkeypatch):
    _install(monkeypatch, _board(_event(_SUNS, _LAKERS), _event(_WARRIORS, _KINGS)))
    assert "Kings" not in voice_tools._run_sports_scores("nba", "LA")


# ── statuses that are not a played game ───────────────────────────────

@pytest.mark.parametrize(
    "type_name,description,completed,word",
    [
        ("STATUS_CANCELED", "Canceled", True, "canceled"),
        ("STATUS_CANCELED", "Canceled", False, "canceled"),
        ("STATUS_CANCELLED", "Cancelled", True, "canceled"),
        ("STATUS_POSTPONED", "Postponed", False, "postponed"),
        ("STATUS_POSTPONED", "Postponed", True, "postponed"),
        ("STATUS_SUSPENDED", "Suspended", True, "suspended"),
        ("STATUS_FORFEIT", "Forfeit", True, "forfeit"),
        ("STATUS_DELAYED", "Delayed", False, "delayed"),
    ],
)
def test_a_game_not_played_has_no_score(monkeypatch, type_name, description, completed, word):
    ev = _event(_SUNS, _LAKERS, state="post", completed=completed, type_name=type_name,
                description=description, detail=description, scores=("0", "0"))
    _install(monkeypatch, _board(ev))
    out = voice_tools._run_sports_scores("nba")
    assert f"Suns at Lakers, {word}." in out
    assert "final" not in out and " 0," not in out


def test_the_word_is_found_in_the_description_when_the_name_is_generic(monkeypatch):
    ev = _event(_SUNS, _LAKERS, state="post", completed=True, type_name="STATUS_UNKNOWN", description="Postponed")
    _install(monkeypatch, _board(ev))
    assert "Suns at Lakers, postponed." in voice_tools._run_sports_scores("nba")


def test_a_rain_delay_with_a_live_score_keeps_the_score(monkeypatch):
    ev = _event(_SUNS, _LAKERS, state="in", completed=False, type_name="STATUS_RAIN_DELAY",
                description="Rain Delay", detail="Rain Delay", scores=("3", "5"))
    _install(monkeypatch, _board(ev))
    assert "Lakers 5, Suns 3, delayed." in voice_tools._run_sports_scores("nba")


def test_a_suspended_game_with_a_live_score_keeps_the_score(monkeypatch):
    ev = _event(_SUNS, _LAKERS, state="in", completed=False, type_name="STATUS_SUSPENDED",
                description="Suspended", detail="Suspended", scores=("3", "5"))
    _install(monkeypatch, _board(ev))
    assert "Lakers 5, Suns 3, suspended." in voice_tools._run_sports_scores("nba")


def test_an_ordinary_final_is_still_a_final(monkeypatch):
    _install(monkeypatch, _board(_event(_SUNS, _LAKERS)))
    assert "Lakers 90, Suns 100" not in voice_tools._run_sports_scores("nba")  # away won: away first
    assert "Suns 100, Lakers 90, final." in voice_tools._run_sports_scores("nba")
