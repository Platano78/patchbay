"""get_weather place resolution: which geocoding candidate wins, and the
VOICE_DEFAULT_PLACE fallback for callers with no place and no client location.

Run from repo root: python3 -m pytest patches/test_weather_geocoding.py -v

The JSON fixtures in weather_fixtures/ are trimmed recordings of real Open-Meteo
geocoding responses (count=10) for a bare place name. No network in tests.
"""

from __future__ import annotations

import json
import sys
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

_FIXTURES = json.loads((Path(__file__).parent / "weather_fixtures" / "open_meteo_geocoding.json").read_text())
_FORECAST = {"current_weather": {"temperature": 20.0, "windspeed": 5.0, "weathercode": 0}}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _install_client(monkeypatch, geo_results):
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
            if "geocoding" in url:
                return _Resp({"results": geo_results} if geo_results is not None else {})
            return _Resp(_FORECAST)

    monkeypatch.setattr(voice_tools.httpx, "Client", _Client)
    return calls


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("VOICE_DEFAULT_PLACE", raising=False)
    monkeypatch.delenv("VOICE_PHONE_CONTEXT", raising=False)
    phone_context._state = None
    yield
    phone_context._state = None


def _forecast_coords(calls):
    fc = [p for u, p in calls if "forecast" in u][0]
    return fc["latitude"], fc["longitude"]


def test_geocoding_asks_for_several_candidates(monkeypatch):
    calls = _install_client(monkeypatch, _FIXTURES["Chicago"])
    voice_tools._run_get_weather("Chicago")
    assert calls[0][1]["count"] == 10


def test_chicago_resolves_to_the_big_city_not_a_hamlet(monkeypatch):
    results = _FIXTURES["Chicago"]
    calls = _install_client(monkeypatch, results)
    out = voice_tools._run_get_weather("Chicago")
    big = next(r for r in results if r.get("population") == 2664452)
    assert _forecast_coords(calls) == (big["latitude"], big["longitude"])
    assert "Illinois" in out


def test_paris_is_the_capital_and_the_name_carries_the_country(monkeypatch):
    results = _FIXTURES["Paris"]
    calls = _install_client(monkeypatch, results)
    out = voice_tools._run_get_weather("Paris")
    capital = next(r for r in results if r["feature_code"] == "PPLC")
    assert _forecast_coords(calls) == (capital["latitude"], capital["longitude"])
    assert "France" in out


def test_ambiguous_name_is_spoken_with_its_region(monkeypatch):
    # Several places are called Springfield, so the answer must say which one.
    results = _FIXTURES["Springfield"]
    _install_client(monkeypatch, results)
    out = voice_tools._run_get_weather("Springfield")
    assert "Springfield, Illinois, United States" in out


def test_georgia_resolves_to_the_country_not_a_village(monkeypatch):
    results = _FIXTURES["Georgia"]
    calls = _install_client(monkeypatch, results)
    voice_tools._run_get_weather("Georgia")
    country = next(r for r in results if r["feature_code"] == "PCLI")
    assert _forecast_coords(calls) == (country["latitude"], country["longitude"])


def test_new_jersey_is_not_the_trinidad_village(monkeypatch):
    results = _FIXTURES["New Jersey"]
    calls = _install_client(monkeypatch, results)
    out = voice_tools._run_get_weather("New Jersey")
    trinidad = results[0]
    assert trinidad["country_code"] == "TT"  # the recorded first hit
    assert _forecast_coords(calls) != (trinidad["latitude"], trinidad["longitude"])
    assert "Trinidad" not in out
    assert "New Jersey, United States" in out


def test_ties_keep_the_apis_own_order(monkeypatch):
    results = [
        {"name": "Foo", "latitude": 1.0, "longitude": 1.0, "feature_code": "PPL", "country": "A"},
        {"name": "Foo", "latitude": 2.0, "longitude": 2.0, "feature_code": "PPL", "country": "B"},
    ]
    calls = _install_client(monkeypatch, results)
    voice_tools._run_get_weather("Foo")
    assert _forecast_coords(calls) == (1.0, 1.0)


def test_single_result_without_feature_code_still_works(monkeypatch):
    results = [{"latitude": 35.6, "longitude": 139.6, "name": "Tokyo", "country": "Japan"}]
    _install_client(monkeypatch, results)
    assert "Tokyo, Japan" in voice_tools._run_get_weather("Tokyo")


def test_no_results_message_unchanged(monkeypatch):
    _install_client(monkeypatch, None)
    assert voice_tools._run_get_weather("Atlantis") == "I couldn't find a place called Atlantis."


# ── VOICE_DEFAULT_PLACE ───────────────────────────────────────────────

def test_default_place_name_is_used_when_no_place_and_no_location(monkeypatch):
    monkeypatch.setenv("VOICE_DEFAULT_PLACE", "Springfield")
    calls = _install_client(monkeypatch, _FIXTURES["Springfield"])
    out = voice_tools._run_get_weather(None)
    assert calls[0][1]["name"] == "Springfield"
    assert "Springfield" in out


def test_default_place_coordinates_skip_geocoding(monkeypatch):
    monkeypatch.setenv("VOICE_DEFAULT_PLACE", "40.0,-75.0")
    calls = _install_client(monkeypatch, [])
    out = voice_tools._run_get_weather(None)
    assert [u for u, _ in calls if "geocoding" in u] == []
    assert _forecast_coords(calls) == (40.0, -75.0)
    assert "20.0" in out


def test_phone_location_beats_the_default(monkeypatch):
    monkeypatch.setenv("VOICE_DEFAULT_PLACE", "Springfield")
    phone_context.update({"lat": 30.27, "lon": -97.74})
    calls = _install_client(monkeypatch, [])
    out = voice_tools._run_get_weather(None)
    assert _forecast_coords(calls) == (30.27, -97.74)
    assert "your location" in out


def test_explicit_place_beats_the_default(monkeypatch):
    monkeypatch.setenv("VOICE_DEFAULT_PLACE", "Springfield")
    calls = _install_client(monkeypatch, _FIXTURES["Paris"])
    voice_tools._run_get_weather("Paris")
    assert calls[0][1]["name"] == "Paris"


def test_unset_default_keeps_the_refusal(monkeypatch):
    calls = _install_client(monkeypatch, [])
    assert voice_tools._run_get_weather(None) == f"I need {voice_tools._WEATHER_PLACE_ARG_LABEL}."
    assert calls == []


def test_blank_default_keeps_the_refusal(monkeypatch):
    monkeypatch.setenv("VOICE_DEFAULT_PLACE", "   ")
    calls = _install_client(monkeypatch, [])
    assert voice_tools._run_get_weather(None) == f"I need {voice_tools._WEATHER_PLACE_ARG_LABEL}."
    assert calls == []


# ── exact name beats a bigger near-miss; label defects ───────────────────

def test_victoria_is_a_place_literally_named_victoria(monkeypatch):
    results = _FIXTURES["Victoria"]
    # the recorded trap: Vitória out-populates Victoria, BC at the same tier
    bc = next(r for r in results if r["name"] == "Victoria" and r.get("admin1") == "British Columbia")
    vitoria = next(r for r in results if r["name"] == "Vitória")
    assert vitoria["feature_code"] == bc["feature_code"] and vitoria["population"] > bc["population"]
    calls = _install_client(monkeypatch, results)
    out = voice_tools._run_get_weather("Victoria")
    lat, lon = _forecast_coords(calls)
    picked = next(r for r in results if (r["latitude"], r["longitude"]) == (lat, lon))
    assert picked["name"] == "Victoria"
    assert picked is bc
    assert "Vitória" not in out


def test_exact_match_ignores_case_and_spacing_but_not_accents(monkeypatch):
    results = [
        {"name": "Vitória", "latitude": 1.0, "longitude": 1.0, "feature_code": "PPLA", "population": 900, "country": "A"},
        {"name": "Victoria", "latitude": 2.0, "longitude": 2.0, "feature_code": "PPL", "population": 5, "country": "B"},
    ]
    calls = _install_client(monkeypatch, results)
    voice_tools._run_get_weather("  VICTORIA ")
    assert _forecast_coords(calls) == (2.0, 2.0)


def test_no_exact_match_ranks_as_before(monkeypatch):
    results = [
        {"name": "Vitória", "latitude": 1.0, "longitude": 1.0, "feature_code": "PPLA", "population": 900, "country": "A"},
        {"name": "Victoriaville", "latitude": 2.0, "longitude": 2.0, "feature_code": "PPL", "population": 5, "country": "B"},
    ]
    calls = _install_client(monkeypatch, results)
    voice_tools._run_get_weather("Victoria")
    assert _forecast_coords(calls) == (1.0, 1.0)


def test_label_drops_a_part_equal_to_the_previous_one(monkeypatch):
    results = [{"name": "Georgia", "latitude": 1.0, "longitude": 1.0, "feature_code": "PCLI", "country": "Georgia"}]
    _install_client(monkeypatch, results)
    out = voice_tools._run_get_weather("Georgia")
    assert "in Georgia," in out and "Georgia, Georgia" not in out


def test_label_falls_back_to_the_query_when_the_name_is_missing_or_empty(monkeypatch):
    for hit_name in ({"name": ""}, {}):
        results = [{"latitude": 1.0, "longitude": 1.0, "feature_code": "PPL", "country": "Peru", **hit_name}]
        _install_client(monkeypatch, results)
        out = voice_tools._run_get_weather("Lima")
        assert "in Lima, Peru" in out
        assert "in , " not in out
