"""Unit tests for phone_context.py (ambient phone context) and, where
achievable without disproportionate stubbing, voice_tools.py's get_weather
location fallback.

Run from repo root: python3 -m pytest patches/test_phone_context.py -v

phone_context.py is dependency-free (no speech_to_speech import, like
voice_rules.py/think_filter.py) -- the tests below need no stubs and no
installed speech_to_speech package for that module. The weather-fallback
section near the bottom DOES need a light stub (only
speech_to_speech.phone_context, aliased to the REAL module, same pattern as
test_websocket_streamer.py's voice_clone aliasing) plus a faked httpx
client, since voice_tools.py imports the former and makes live HTTP calls
in the latter -- see that section's own setup.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from patches import phone_context


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Every test starts from a clean module-level state and default env."""
    monkeypatch.delenv("VOICE_PHONE_CONTEXT", raising=False)
    phone_context._state = None
    phone_context._geocode_cache.clear()
    phone_context._geocode_inflight.clear()
    yield
    phone_context._state = None
    phone_context._geocode_cache.clear()
    phone_context._geocode_inflight.clear()


def _seed_place(lat, lon, place):
    """Pre-populate the geocode cache so the non-blocking path reports a real
    place name. Mirrors how a background lookup lands its result -- exercises
    the actual cache-hit path instead of stubbing out the function under test."""
    phone_context._geocode_cache[(round(lat, 3), round(lon, 3))] = place


# ── update() validation ──────────────────────────────────────────────────


def test_update_good_payload_stores_everything():
    ok = phone_context.update(
        {
            "lat": 30.2672,
            "lon": -97.7431,
            "accuracy": 15.0,
            "tz": "America/Chicago",
            "battery_pct": 80,
            "charging": True,
        }
    )
    assert ok is True
    snap = phone_context.snapshot()
    assert snap["lat"] == 30.2672
    assert snap["lon"] == -97.7431
    assert snap["accuracy"] == 15.0
    assert snap["tz"] == "America/Chicago"
    assert snap["battery_pct"] == 80
    assert snap["charging"] is True
    assert "ts" in snap and "age_s" in snap


def test_update_partial_payload_merges_onto_previous():
    phone_context.update({"lat": 1.0, "lon": 2.0, "tz": "UTC"})
    ok = phone_context.update({"battery_pct": 42})
    assert ok is True
    snap = phone_context.snapshot()
    assert snap["lat"] == 1.0
    assert snap["lon"] == 2.0
    assert snap["tz"] == "UTC"
    assert snap["battery_pct"] == 42


def test_update_drops_invalid_field_but_keeps_valid_ones():
    ok = phone_context.update({"lat": 999, "lon": 2.0})  # lat out of [-90, 90]
    assert ok is True
    snap = phone_context.snapshot()
    assert "lat" not in snap
    assert snap["lon"] == 2.0


def test_update_all_invalid_rejected():
    ok = phone_context.update({"lat": 999, "lon": -999})
    assert ok is False
    assert phone_context.snapshot() is None


def test_update_empty_dict_rejected():
    assert phone_context.update({}) is False


def test_update_non_dict_rejected():
    assert phone_context.update("not a dict") is False
    assert phone_context.update(None) is False
    assert phone_context.update([1, 2]) is False


def test_update_rejects_bool_for_numeric_fields():
    # bool is an int subclass in Python -- must not slip through lat's range check.
    ok = phone_context.update({"lat": True, "lon": 2.0})
    assert ok is True  # lon alone is valid, so the update as a whole succeeds
    snap = phone_context.snapshot()
    assert "lat" not in snap


def test_update_rejects_non_bool_for_charging():
    ok = phone_context.update({"lon": 2.0, "charging": "yes"})
    assert ok is True
    snap = phone_context.snapshot()
    assert "charging" not in snap


def test_update_rejects_overlong_tz():
    ok = phone_context.update({"lon": 2.0, "tz": "x" * 65})
    assert ok is True
    snap = phone_context.snapshot()
    assert "tz" not in snap


# ── location() staleness gating ──────────────────────────────────────────


def test_location_returns_none_when_never_set():
    assert phone_context.location() is None


def test_location_returns_coords_when_fresh():
    phone_context.update({"lat": 1.5, "lon": 2.5})
    assert phone_context.location() == (1.5, 2.5)


def test_location_none_when_stale():
    phone_context.update({"lat": 1.5, "lon": 2.5})
    phone_context._state["ts"] = time.time() - 3600
    assert phone_context.location(max_age_s=1800) is None


def test_location_respects_custom_max_age():
    phone_context.update({"lat": 1.5, "lon": 2.5})
    phone_context._state["ts"] = time.time() - 100
    assert phone_context.location(max_age_s=50) is None
    assert phone_context.location(max_age_s=200) == (1.5, 2.5)


def test_location_none_when_only_one_coord_present():
    phone_context.update({"lat": 1.5})  # lon never sent
    assert phone_context.location() is None


# ── VOICE_PHONE_CONTEXT=off ───────────────────────────────────────────────


def test_off_env_disables_update(monkeypatch):
    monkeypatch.setenv("VOICE_PHONE_CONTEXT", "off")
    assert phone_context.update({"lat": 1.0, "lon": 2.0}) is False
    assert phone_context.snapshot() is None


def test_off_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("VOICE_PHONE_CONTEXT", "OFF")
    assert phone_context.update({"lat": 1.0, "lon": 2.0}) is False


def test_off_env_hides_already_stored_state(monkeypatch):
    phone_context.update({"lat": 1.0, "lon": 2.0})
    monkeypatch.setenv("VOICE_PHONE_CONTEXT", "off")
    assert phone_context.location() is None
    assert phone_context.snapshot() is None
    assert phone_context.ambient_line() is None


# ── ambient_line() ────────────────────────────────────────────────────────


def test_ambient_line_none_without_location():
    assert phone_context.ambient_line() is None


def test_ambient_line_with_place_and_valid_tz():
    _seed_place(30.27, -97.74, "Austin, Texas")
    phone_context.update({"lat": 30.27, "lon": -97.74, "tz": "America/Chicago"})
    line = phone_context.ambient_line()
    assert line.startswith("The user's approximate location is Austin, Texas; their local time is ")
    assert "(America/Chicago)" in line


def test_ambient_line_omits_time_for_invalid_tz():
    _seed_place(30.27, -97.74, "Austin, Texas")
    phone_context.update({"lat": 30.27, "lon": -97.74, "tz": "Not/A_Real_Zone"})
    line = phone_context.ambient_line()
    assert line == "The user's approximate location is Austin, Texas."


def test_ambient_line_omits_time_when_tz_unset():
    _seed_place(30.27, -97.74, "Austin, Texas")
    phone_context.update({"lat": 30.27, "lon": -97.74})
    line = phone_context.ambient_line()
    assert line == "The user's approximate location is Austin, Texas."


def test_ambient_line_coarse_coords_until_the_lookup_lands(monkeypatch):
    # Nothing cached for these coords, so the line reports coordinates rather
    # than waiting on Nominatim. Submit is stubbed out so the test does not
    # depend on a background thread having run.
    monkeypatch.setattr(phone_context._geocode_pool, "submit", lambda fn: None)
    phone_context.update({"lat": 12.3456, "lon": 65.4321})
    line = phone_context.ambient_line()
    assert line == "The user's approximate location is latitude 12.346, longitude 65.432."


# ── _reverse_geocode() / _place_from_geocode_payload() ─────────────────────


def test_reverse_geocode_uses_payload_and_caches(monkeypatch):
    calls = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"address": {"city": "Austin", "state": "Texas", "country": "USA"}}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return _FakeResponse()

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=fake_get))

    place1 = phone_context._reverse_geocode(30.2672, -97.7431)
    place2 = phone_context._reverse_geocode(30.26721, -97.74311)  # rounds to the same 3-decimal cache key

    assert place1 == "Austin, Texas"
    assert place2 == place1
    assert len(calls) == 1  # second call served from cache, no second GET


def test_place_from_geocode_payload_falls_back_to_display_name():
    payload = {"display_name": "Austin, Travis County, Texas, USA"}
    assert phone_context._place_from_geocode_payload(payload) == "Austin"


def test_place_from_geocode_payload_none_on_unusable_payload():
    assert phone_context._place_from_geocode_payload({}) is None


# ── geocode caching / non-blocking path ──────────────────────────────────


def _raising_requests():
    return types.SimpleNamespace(get=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no network")))


def test_failed_geocode_is_not_cached(monkeypatch):
    """A transient failure must not latch. The old code cached `place or
    fallback`, so one network blip pinned a location to bare coordinates for
    the life of the process -- there is no TTL, so it never retried."""
    monkeypatch.setitem(sys.modules, "requests", _raising_requests())
    out = phone_context._reverse_geocode(12.3456, 65.4321)
    assert out == "latitude 12.346, longitude 65.432"
    assert phone_context._geocode_cache == {}, "a failed lookup must leave the cache empty so it can retry"


def test_successful_geocode_is_cached(monkeypatch):
    payload = {"address": {"city": "Austin", "state": "Texas"}}
    fake = types.SimpleNamespace(
        get=lambda *a, **kw: types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    )
    monkeypatch.setitem(sys.modules, "requests", fake)
    assert phone_context._reverse_geocode(30.2672, -97.7431) == "Austin, Texas"
    assert phone_context._geocode_cache[(30.267, -97.743)] == "Austin, Texas"


def test_geocode_cache_is_bounded_and_evicts_oldest(monkeypatch):
    for i in range(phone_context._GEOCODE_CACHE_MAX):
        phone_context._geocode_cache[(float(i), 0.0)] = f"place {i}"
    oldest = (0.0, 0.0)
    assert oldest in phone_context._geocode_cache

    payload = {"address": {"city": "Austin", "state": "Texas"}}
    fake = types.SimpleNamespace(
        get=lambda *a, **kw: types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    )
    monkeypatch.setitem(sys.modules, "requests", fake)
    phone_context._reverse_geocode(999.0, 0.0)

    assert len(phone_context._geocode_cache) == phone_context._GEOCODE_CACHE_MAX
    assert oldest not in phone_context._geocode_cache, "LRU must evict the least-recently-used entry"


def test_place_nonblocking_never_calls_the_network(monkeypatch):
    """The LLM request path calls this on every turn -- it must not do I/O."""
    def _boom(*a, **kw):
        raise AssertionError("_place_nonblocking must not touch requests")

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=_boom))
    monkeypatch.setattr(phone_context._geocode_pool, "submit", lambda fn: None)
    assert phone_context._place_nonblocking(1.0, 2.0) == "latitude 1.000, longitude 2.000"


def test_place_nonblocking_returns_cached_place(monkeypatch):
    monkeypatch.setattr(phone_context._geocode_pool, "submit", lambda fn: None)
    _seed_place(1.0, 2.0, "Austin, Texas")
    assert phone_context._place_nonblocking(1.0, 2.0) == "Austin, Texas"


def test_place_nonblocking_schedules_each_key_once(monkeypatch):
    submitted = []
    monkeypatch.setattr(phone_context._geocode_pool, "submit", lambda fn: submitted.append(fn))
    phone_context._place_nonblocking(1.0, 2.0)
    phone_context._place_nonblocking(1.0004, 2.0004)  # rounds to the same key
    assert len(submitted) == 1, "a key already in flight must not be scheduled again"


def test_failed_submit_does_not_wedge_the_key(monkeypatch):
    """If submit() fails the key must leave the in-flight set, or that location
    would never be looked up again for the life of the process."""
    def _boom(fn):
        raise RuntimeError("pool is down")

    monkeypatch.setattr(phone_context._geocode_pool, "submit", _boom)
    assert phone_context._place_nonblocking(1.0, 2.0) == "latitude 1.000, longitude 2.000"
    assert phone_context._geocode_inflight == set()
    assert phone_context._place_from_geocode_payload("not a dict") is None


# ── apply_ambient() ────────────────────────────────────────────────────────


def test_apply_ambient_none_ambient_line_is_passthrough():
    messages = [{"role": "user", "content": "hi"}]
    result = phone_context.apply_ambient(messages)
    assert result is messages


def test_apply_ambient_appends_to_existing_system_str(monkeypatch):
    _seed_place(30.27, -97.74, "Austin, Texas")
    phone_context.update({"lat": 30.27, "lon": -97.74})
    messages = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]

    result = phone_context.apply_ambient(messages)

    assert result[0]["content"].startswith("You are helpful.\n\nThe user's approximate location is Austin, Texas")
    assert result[1] == {"role": "user", "content": "hi"}


def test_apply_ambient_inserts_system_message_when_absent(monkeypatch):
    _seed_place(30.27, -97.74, "Austin, Texas")
    phone_context.update({"lat": 30.27, "lon": -97.74})
    messages = [{"role": "user", "content": "hi"}]

    result = phone_context.apply_ambient(messages)

    assert result[0]["role"] == "system"
    assert result[0]["content"] == phone_context.ambient_line()
    assert result[1] == {"role": "user", "content": "hi"}


def test_apply_ambient_never_mutates_input(monkeypatch):
    import copy

    _seed_place(30.27, -97.74, "Austin, Texas")
    phone_context.update({"lat": 30.27, "lon": -97.74})
    messages = [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}]
    original = copy.deepcopy(messages)

    phone_context.apply_ambient(messages)

    assert messages == original


def test_apply_ambient_idempotent_when_line_already_present(monkeypatch):
    _seed_place(30.27, -97.74, "Austin, Texas")
    phone_context.update({"lat": 30.27, "lon": -97.74})
    messages = [{"role": "user", "content": "hi"}]

    once = phone_context.apply_ambient(messages)
    twice = phone_context.apply_ambient(once)

    assert twice == once


# ── voice_tools.py get_weather location fallback ────────────────────────
#
# voice_tools.py's only speech_to_speech import is `from speech_to_speech
# import phone_context`, and it makes no other speech_to_speech-package
# calls at import time -- so the REAL module imports cleanly here with just
# that one alias stubbed in, same pattern as test_websocket_streamer.py's
# voice_clone aliasing. httpx itself is a real, installed dependency; only
# its outbound network calls are faked below.


def _install_voice_tools_stub():
    pkg = sys.modules.get("speech_to_speech")
    if pkg is None:
        pkg = types.ModuleType("speech_to_speech")
        sys.modules["speech_to_speech"] = pkg
    sys.modules["speech_to_speech.phone_context"] = phone_context
    pkg.phone_context = phone_context


_install_voice_tools_stub()

from patches import voice_tools  # noqa: E402


class _FakeWeatherResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_httpx_client(responses):
    """Stands in for httpx.Client(timeout=...) as a context manager;
    records every GET call (url, params) and returns the next canned
    response in sequence -- popping past the end raises loudly instead of
    silently reaching the network."""
    calls: list[tuple[str, dict]] = []

    class _FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            calls.append((url, params))
            return responses.pop(0)

    return _FakeClient, calls


def test_get_weather_falls_back_to_phone_context_location(monkeypatch):
    phone_context.update({"lat": 30.27, "lon": -97.74})
    forecast_payload = {"current_weather": {"temperature": 21.0, "windspeed": 5.0, "weathercode": 1}}
    fake_client_cls, calls = _fake_httpx_client([_FakeWeatherResponse(forecast_payload)])
    monkeypatch.setattr(voice_tools.httpx, "Client", fake_client_cls)

    result = voice_tools._run_get_weather(None)

    assert len(calls) == 1  # geocoding skipped -- only the forecast call happened
    url, params = calls[0]
    assert "forecast" in url
    assert params["latitude"] == 30.27
    assert params["longitude"] == -97.74
    assert "your location" in result
    assert "21.0" in result


def test_get_weather_refuses_when_no_place_and_no_location(monkeypatch):
    # No phone_context.update() call -- location() is None.
    fake_client_cls, calls = _fake_httpx_client([])  # any GET here is a bug -- fail loudly, not over the network
    monkeypatch.setattr(voice_tools.httpx, "Client", fake_client_cls)

    result = voice_tools._run_get_weather(None)

    assert result == f"I need {voice_tools._WEATHER_PLACE_ARG_LABEL}."
    assert calls == []


def test_get_weather_explicit_place_skips_phone_context(monkeypatch):
    # Even with a stored location available, an explicit place still geocodes it normally.
    phone_context.update({"lat": 30.27, "lon": -97.74})
    geo_payload = {"results": [{"latitude": 35.6762, "longitude": 139.6503, "name": "Tokyo", "country": "Japan"}]}
    forecast_payload = {"current_weather": {"temperature": 18.0, "windspeed": 2.0, "weathercode": 0}}
    fake_client_cls, calls = _fake_httpx_client([_FakeWeatherResponse(geo_payload), _FakeWeatherResponse(forecast_payload)])
    monkeypatch.setattr(voice_tools.httpx, "Client", fake_client_cls)

    result = voice_tools._run_get_weather("Tokyo")

    assert len(calls) == 2  # geocode, then forecast
    assert "geocoding" in calls[0][0]
    assert "Tokyo, Japan" in result


def test_execute_get_weather_omitted_place_uses_dispatch_fallback(monkeypatch):
    # End-to-end through execute()/the dispatch table: required=False must
    # let an omitted place reach the runner instead of auto-refusing.
    voice_tools._ARMED_NAMES.clear()  # permissive (no session-armed restriction)
    phone_context.update({"lat": 10.0, "lon": 20.0})
    forecast_payload = {"current_weather": {"temperature": 15.0, "windspeed": 3.0, "weathercode": 0}}
    fake_client_cls, calls = _fake_httpx_client([_FakeWeatherResponse(forecast_payload)])
    monkeypatch.setattr(voice_tools.httpx, "Client", fake_client_cls)

    result = voice_tools.execute("get_weather", {})

    assert "your location" in result
    assert len(calls) == 1
