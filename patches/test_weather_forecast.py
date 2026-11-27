"""get_weather: the `when` argument (now / today / tonight / tomorrow) and units.

Run from repo root: python3 -m pytest patches/test_weather_forecast.py -v

weather_fixtures/open_meteo_forecast.json holds two recorded Open-Meteo forecast
responses for one place, one per unit system (forecast_days=2, timezone=auto,
recorded at 22:00 local on the first day). No network in tests.
"""

from __future__ import annotations

import copy
import json
import math
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

_FC = json.loads((Path(__file__).parent / "weather_fixtures" / "open_meteo_forecast.json").read_text())

_US = {"name": "Springfield", "latitude": 40.7, "longitude": -74.3, "feature_code": "PPLA2",
       "population": 15000, "admin1": "New Jersey", "country": "United States", "country_code": "US"}
_FR = {"name": "Paris", "latitude": 48.85, "longitude": 2.35, "feature_code": "PPLC",
       "population": 2138551, "admin1": "Île-de-France", "country": "France", "country_code": "FR"}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _install(monkeypatch, geo_results=(), forecast=None):
    """Fake httpx.Client: geocoding returns geo_results; the forecast is the
    recorded Fahrenheit or Celsius response depending on the request."""
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
                return _Resp({"results": list(geo_results)})
            fc = forecast if forecast is not None else _FC
            return _Resp(copy.deepcopy(fc["fahrenheit" if params.get("temperature_unit") == "fahrenheit" else "celsius"]))

    monkeypatch.setattr(voice_tools.httpx, "Client", _Client)
    return calls


def _forecast_params(calls):
    return [p for u, p in calls if "forecast" in u][0]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("VOICE_DEFAULT_PLACE", "VOICE_WEATHER_UNITS", "VOICE_PHONE_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    phone_context._state = None
    yield
    phone_context._state = None


def _round(x):
    return math.floor(x + 0.5)


# ── when ─────────────────────────────────────────────────────────────

def test_request_asks_for_daily_and_hourly_in_the_places_timezone(monkeypatch):
    calls = _install(monkeypatch, [_US])
    voice_tools._run_get_weather("Springfield", "tomorrow")
    p = _forecast_params(calls)
    assert p["timezone"] == "auto"
    for field in ("temperature_2m_max", "temperature_2m_min", "precipitation_probability_max", "weathercode"):
        assert field in p["daily"]
    assert "temperature_2m" in p["hourly"]


def test_tomorrow_gives_high_low_conditions_and_precipitation(monkeypatch):
    _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield", "tomorrow", "celsius")
    assert out.startswith("Tomorrow in Springfield, United States")
    assert "high of 8.6°C" in out and "low of -0.9°C" in out
    assert "overcast" in out
    assert "0 percent chance of precipitation" in out


def test_today_uses_the_first_day(monkeypatch):
    _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield", "today", "celsius")
    assert out.startswith("Today in ")
    assert "high of 10.1°C" in out and "low of 1.8°C" in out


def test_tonight_is_the_overnight_low_not_the_day_low(monkeypatch):
    _install(monkeypatch, [_US])
    hourly = _FC["celsius"]["hourly"]
    overnight = [
        t for ts, t in zip(hourly["time"], hourly["temperature_2m"])
        if "2026-11-24T22:00" <= ts < "2026-11-25T07:00"
    ]
    out = voice_tools._run_get_weather("Springfield", "tonight", "celsius")
    assert out.startswith("Tonight in ")
    assert f"low of {min(overnight):.1f}°C" in out
    assert "high" not in out


def test_tonight_before_evening_starts_at_six_pm(monkeypatch):
    fc = copy.deepcopy(_FC)
    for unit in fc.values():
        unit["current_weather"]["time"] = "2026-11-24T14:00"
    _install(monkeypatch, [_US], forecast=fc)
    hourly = fc["celsius"]["hourly"]
    overnight = [
        t for ts, t in zip(hourly["time"], hourly["temperature_2m"])
        if "2026-11-24T18:00" <= ts < "2026-11-25T07:00"
    ]
    out = voice_tools._run_get_weather("Springfield", "tonight", "celsius")
    assert f"low of {min(overnight):.1f}°C" in out


def test_now_appends_todays_high_and_low(monkeypatch):
    _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield", None, "celsius")
    assert out.startswith("It's currently 3.1°C and clear sky in Springfield, United States")
    assert "Today's high is 10.1°C and low is 1.8°C." in out
    assert voice_tools._run_get_weather("Springfield", "now", "celsius") == out


def test_unknown_when_is_refused_with_the_choices(monkeypatch):
    calls = _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield", "next week")
    assert "now, today, tonight or tomorrow" in out
    assert calls == []


def test_a_forecast_without_daily_data_still_answers_now(monkeypatch):
    fc = copy.deepcopy(_FC)
    for unit in fc.values():
        del unit["daily"]
    _install(monkeypatch, [_US], forecast=fc)
    out = voice_tools._run_get_weather("Springfield", None, "celsius")
    assert "It's currently 3.1°C" in out and "high" not in out


# ── units ────────────────────────────────────────────────────────────

def test_us_place_defaults_to_fahrenheit_and_mph(monkeypatch):
    calls = _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield")
    p = _forecast_params(calls)
    assert p["temperature_unit"] == "fahrenheit" and p["windspeed_unit"] == "mph"
    assert "38°F" in out and "5 mph" in out
    assert "high is 50°F and low is 35°F" in out


def test_non_us_place_defaults_to_celsius_and_kmh(monkeypatch):
    calls = _install(monkeypatch, [_FR])
    out = voice_tools._run_get_weather("Paris")
    assert "temperature_unit" not in _forecast_params(calls)
    assert "3.1°C" in out and "7.3 km/h" in out


@pytest.mark.parametrize("code", ["US", "BS", "BZ", "KY", "LR", "PW", "FM", "MH"])
def test_fahrenheit_countries(monkeypatch, code):
    calls = _install(monkeypatch, [{**_US, "country_code": code}])
    voice_tools._run_get_weather("Springfield")
    assert _forecast_params(calls)["temperature_unit"] == "fahrenheit"


def test_fahrenheit_is_spoken_in_whole_degrees(monkeypatch):
    _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield", "tomorrow")
    assert "high of 48°F" in out and "low of 31°F" in out  # 47.5 and 30.5 round up


def test_env_override_beats_the_country_rule_for_every_place(monkeypatch):
    monkeypatch.setenv("VOICE_WEATHER_UNITS", "celsius")
    calls = _install(monkeypatch, [_US])
    voice_tools._run_get_weather("Springfield")
    assert "temperature_unit" not in _forecast_params(calls)
    monkeypatch.setenv("VOICE_WEATHER_UNITS", "Fahrenheit")
    calls = _install(monkeypatch, [_FR])
    voice_tools._run_get_weather("Paris")
    assert _forecast_params(calls)["temperature_unit"] == "fahrenheit"


def test_argument_beats_env_and_country_rule(monkeypatch):
    monkeypatch.setenv("VOICE_WEATHER_UNITS", "fahrenheit")
    calls = _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield", None, "celsius")
    assert "temperature_unit" not in _forecast_params(calls)
    assert "°C" in out and "°F" not in out
    calls = _install(monkeypatch, [_FR])
    voice_tools._run_get_weather("Paris", None, "f")
    assert _forecast_params(calls)["temperature_unit"] == "fahrenheit"


def test_unrecognised_units_argument_is_refused_with_the_choices(monkeypatch):
    calls = _install(monkeypatch, [_US])
    out = voice_tools._run_get_weather("Springfield", None, "kelvin")
    assert "fahrenheit or celsius" in out
    assert calls == []


def test_bad_units_env_is_ignored_and_warned_once(monkeypatch, caplog):
    monkeypatch.setenv("VOICE_WEATHER_UNITS", "kelvin")
    monkeypatch.setattr(voice_tools, "_WARNED_BAD_UNITS_ENV", False)
    calls = _install(monkeypatch, [_US])
    with caplog.at_level("WARNING", logger="patches.voice_tools"):
        out = voice_tools._run_get_weather("Springfield")
        voice_tools._run_get_weather("Springfield")
    assert "°F" in out  # the country rule still applies; the request is not refused
    warnings = [r for r in caplog.records if "VOICE_WEATHER_UNITS" in r.getMessage()]
    assert len(warnings) == 1


def test_lat_lon_default_without_env_is_celsius(monkeypatch):
    monkeypatch.setenv("VOICE_DEFAULT_PLACE", "40.7,-74.3")
    calls = _install(monkeypatch, [])
    out = voice_tools._run_get_weather(None)
    assert "temperature_unit" not in _forecast_params(calls)
    assert "°C" in out


def test_lat_lon_default_with_env_uses_the_env_unit(monkeypatch):
    monkeypatch.setenv("VOICE_DEFAULT_PLACE", "40.7,-74.3")
    monkeypatch.setenv("VOICE_WEATHER_UNITS", "fahrenheit")
    calls = _install(monkeypatch, [])
    out = voice_tools._run_get_weather(None)
    assert _forecast_params(calls)["temperature_unit"] == "fahrenheit"
    assert "°F" in out


def test_phone_location_follows_env_else_celsius(monkeypatch):
    phone_context.update({"lat": 40.7, "lon": -74.3})
    calls = _install(monkeypatch, [])
    assert "°C" in voice_tools._run_get_weather(None)
    monkeypatch.setenv("VOICE_WEATHER_UNITS", "fahrenheit")
    calls = _install(monkeypatch, [])
    assert "°F" in voice_tools._run_get_weather(None)


# ── through execute(): the extra arguments reach the runner ─────────────

def test_execute_passes_when_and_units_to_get_weather(monkeypatch):
    voice_tools._ARMED_NAMES.clear()
    _install(monkeypatch, [_US])
    out = voice_tools.execute("get_weather", {"place": "Springfield", "when": "tomorrow", "units": "celsius"})
    assert "Tomorrow in" in out and "8.6°C" in out
    assert "Checked at" in out


def test_get_weather_schema_offers_when_and_units():
    tool = next(t for t in voice_tools.TOOL_DEFS if t["name"] == "get_weather")
    props = tool["parameters"]["properties"]
    assert props["when"]["enum"] == ["now", "today", "tonight", "tomorrow"]
    assert props["units"]["enum"] == ["fahrenheit", "celsius"]
    assert tool["parameters"]["required"] == []


# ── truncated or missing arrays answer cleanly ──────────────────────────

def _truncated(mutate):
    fc = copy.deepcopy(_FC)
    for unit in fc.values():
        mutate(unit)
    return fc


def test_short_hourly_weathercode_and_missing_precipitation_still_answer(monkeypatch):
    def cut(unit):
        unit["hourly"]["weathercode"] = unit["hourly"]["weathercode"][:3]
        del unit["hourly"]["precipitation_probability"]

    _install(monkeypatch, [_US], forecast=_truncated(cut))
    out = voice_tools._run_get_weather("Springfield", "tonight", "celsius")
    assert out.startswith("Tonight in ") and "low of" in out


def test_hourly_temperatures_missing_for_the_window_is_the_clean_refusal(monkeypatch):
    def cut(unit):
        unit["hourly"]["temperature_2m"] = unit["hourly"]["temperature_2m"][:5]

    _install(monkeypatch, [_US], forecast=_truncated(cut))
    out = voice_tools._run_get_weather("Springfield", "tonight", "celsius")
    assert out == "I couldn't get tonight's forecast for Springfield, United States."


def test_daily_arrays_shorter_than_the_dates_do_not_raise(monkeypatch):
    def cut(unit):
        for key in ("temperature_2m_max", "temperature_2m_min", "weathercode", "precipitation_probability_max"):
            unit["daily"][key] = unit["daily"][key][:1]

    _install(monkeypatch, [_US], forecast=_truncated(cut))
    out = voice_tools._run_get_weather("Springfield", "tomorrow", "celsius")
    assert out == "I couldn't get the forecast for Springfield, United States."
    assert "Today" in voice_tools._run_get_weather("Springfield", "today", "celsius")


def test_missing_daily_and_hourly_blocks_do_not_raise(monkeypatch):
    def cut(unit):
        unit.pop("daily", None)
        unit.pop("hourly", None)

    _install(monkeypatch, [_US], forecast=_truncated(cut))
    for when in ("today", "tomorrow", "tonight"):
        assert "couldn't get" in voice_tools._run_get_weather("Springfield", when, "celsius")
