"""Unit tests for the home_assistant voice tool drop-in.

Run from the repo root:
    python3 -m unittest examples.tools.test_home_assistant -v

urllib is fully mocked — no test here reaches an actual network.
"""
from __future__ import annotations

import inspect
import json
import os
import socket
import unittest
import urllib.error
from unittest import mock

from examples.tools import home_assistant as ha


class _FakeResponse:
    """Stand-in for the object returned by urllib.request.urlopen()."""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RawResponse:
    """Like _FakeResponse but returns raw, possibly-malformed bytes as-is —
    for exercising _http_request's json.loads()/decode() failure path."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeHA:
    """Routes mocked urlopen() calls to canned HA REST responses."""

    def __init__(
        self,
        states=None,
        states_error=None,
        entity_states=None,
        service_error=None,
        service_raw_body=None,
    ):
        self.states = states if states is not None else []
        self.states_error = states_error
        self.entity_states = entity_states or {}
        self.service_error = service_error
        self.service_raw_body = service_raw_body
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        self.calls.append((method, url))

        if method == "GET" and url.endswith("/api/states"):
            if self.states_error == "connection":
                raise urllib.error.URLError("refused")
            if self.states_error == "timeout":
                raise socket.timeout()
            return _FakeResponse(self.states)

        if method == "GET" and "/api/states/" in url:
            entity_id = url.rsplit("/", 1)[-1]
            if entity_id not in self.entity_states:
                raise urllib.error.HTTPError(url, 404, "not found", {}, None)
            return _FakeResponse(self.entity_states[entity_id])

        if method == "POST":
            if self.service_error == "connection":
                raise urllib.error.URLError("refused")
            if self.service_error == "timeout":
                raise socket.timeout()
            if isinstance(self.service_error, int):
                raise urllib.error.HTTPError(url, self.service_error, "err", {}, None)
            if self.service_raw_body is not None:
                return _RawResponse(self.service_raw_body)
            return _FakeResponse({})

        raise AssertionError(f"unexpected request {method} {url}")


def _state_entry(entity_id: str, friendly_name: str) -> dict:
    return {"entity_id": entity_id, "attributes": {"friendly_name": friendly_name}, "state": "off"}


class HAToolTestCase(unittest.TestCase):
    """Base class: resets module globals and env for every test."""

    def setUp(self):
        ha._ALIASES = {}
        ha._ALIASES_LOADED = False
        # Must reset, or the refresh cooldown leaks between tests and makes
        # the miss-triggered-refresh cases order-dependent.
        ha._ALIASES_REFRESHED_AT = 0.0
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"HA_TOKEN": "test-token", "HA_URL": "http://ha.invalid:8123", "VOICE_TOOLS_DIR": ""},
            clear=False,
        )
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def _patch_urlopen(self, fake: _FakeHA):
        patcher = mock.patch("examples.tools.home_assistant.urllib.request.urlopen", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


# ---------------------------------------------------------------------------
# parse_command — pure, no I/O, no mocking needed.
# ---------------------------------------------------------------------------


class TestParseCommand(unittest.TestCase):
    def test_turn_on(self):
        self.assertEqual(
            ha.parse_command("turn on the kitchen lights"),
            ha.Intent("turn_on", "kitchen lights"),
        )

    def test_turn_off_reversed(self):
        self.assertEqual(
            ha.parse_command("turn the kitchen lights off"),
            ha.Intent("turn_off", "kitchen lights"),
        )

    def test_toggle(self):
        self.assertEqual(ha.parse_command("toggle the office lamp"), ha.Intent("toggle", "office lamp"))

    def test_brightness_digits(self):
        self.assertEqual(
            ha.parse_command("set the office lamp brightness to 75 percent"),
            ha.Intent("set_brightness", "office lamp", 75),
        )

    def test_brightness_half(self):
        self.assertEqual(
            ha.parse_command("set the office lamp brightness to half"),
            ha.Intent("set_brightness", "office lamp", 50),
        )

    def test_brightness_full(self):
        self.assertEqual(
            ha.parse_command("set the office lamp brightness to full"),
            ha.Intent("set_brightness", "office lamp", 100),
        )

    def test_color(self):
        self.assertEqual(
            ha.parse_command("set the kitchen lights to blue"),
            ha.Intent("set_color", "kitchen lights", "blue"),
        )

    def test_scene(self):
        self.assertEqual(
            ha.parse_command("activate the movie night scene"),
            ha.Intent("activate_scene", "movie night"),
        )
        self.assertEqual(
            ha.parse_command("run morning scene"),
            ha.Intent("activate_scene", "morning"),
        )

    def test_query_power(self):
        self.assertEqual(
            ha.parse_command("is the kitchen light on?"),
            ha.Intent("query_power", "kitchen light"),
        )

    def test_query_temperature(self):
        self.assertEqual(
            ha.parse_command("what's the temperature in the living room?"),
            ha.Intent("query_temperature", "living room"),
        )

    def test_compound_bail(self):
        self.assertIsNone(ha.parse_command("turn on the lamp and play music"))
        self.assertIsNone(ha.parse_command("turn on the lamp then close the blinds"))

    def test_vague_entity_bail(self):
        self.assertIsNone(ha.parse_command("turn it off"))
        self.assertIsNone(ha.parse_command("turn those lights off"))
        self.assertIsNone(ha.parse_command("toggle everything"))

    def test_unparseable_returns_none(self):
        self.assertIsNone(ha.parse_command("what's the weather like"))
        self.assertIsNone(ha.parse_command(""))
        self.assertIsNone(ha.parse_command("   "))


# ---------------------------------------------------------------------------
# _resolve_entity — chain: exact, suffix, fuzzy, raw entity_id, miss.
# ---------------------------------------------------------------------------


class TestResolveEntity(unittest.TestCase):
    def setUp(self):
        ha._ALIASES.clear()
        ha._ALIASES.update(
            {
                "kitchen lights": "light.kitchen",
                "downstairs office ceiling light": "light.downstairs_office_ceiling",
            }
        )

    def test_exact_match(self):
        self.assertEqual(ha._resolve_entity("the kitchen lights"), "light.kitchen")

    def test_suffix_strip(self):
        # Alias has no suffix; the spoken query adds one ("office" + "lamp") —
        # the trailing suffix word is stripped to find the alias.
        ha._ALIASES["office"] = "light.office"
        self.assertEqual(ha._resolve_entity("office lamp"), "light.office")

    def test_suffix_add(self):
        # Alias carries a suffix; the spoken query omits it — a suffix is
        # appended to each candidate to find the alias.
        ha._ALIASES["office lamp"] = "light.office_lamp"
        self.assertEqual(ha._resolve_entity("office"), "light.office_lamp")

    def test_fuzzy_match(self):
        self.assertEqual(
            ha._resolve_entity("downstairs office"), "light.downstairs_office_ceiling"
        )

    def test_raw_entity_id_passthrough(self):
        self.assertEqual(ha._resolve_entity("light.some_unknown_entity"), "light.some_unknown_entity")

    def test_miss_returns_none(self):
        self.assertIsNone(ha._resolve_entity("nonexistent gadget"))


# ---------------------------------------------------------------------------
# run() — full flow, urllib mocked.
# ---------------------------------------------------------------------------


class TestRunMissingToken(HAToolTestCase):
    def test_missing_token_no_network_call(self):
        with mock.patch.dict(os.environ, {"HA_TOKEN": ""}):
            fake = self._patch_urlopen(_FakeHA())
            result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "Home Assistant isn't set up yet.")
        self.assertEqual(fake.calls, [])


class TestRunUnparseable(HAToolTestCase):
    def test_unparseable_returns_capabilities(self):
        fake = self._patch_urlopen(_FakeHA())
        result = ha.run("what's the weather like")
        self.assertEqual(
            result, "I can turn things on or off, set brightness or color, or run scenes."
        )
        self.assertEqual(fake.calls, [])


class TestAliasRefreshOnMiss(HAToolTestCase):
    """The alias map is a snapshot. Renaming or adding a device in Home
    Assistant used to break voice control for it until the process restarted,
    because `_ALIASES_LOADED` latched True forever with no refresh path."""

    def _states_call_count(self, fake):
        return sum(1 for method, url in fake.calls if method == "GET" and url.endswith("/api/states"))

    def test_renamed_entity_resolves_after_a_miss(self):
        fake = self._patch_urlopen(_FakeHA(states=[_state_entry("light.kitchen", "Kitchen Lights")]))
        self.assertEqual(ha.run("turn on the kitchen lights"), "Okay, turning on kitchen lights.")

        # The user renames it in the HA UI while the process keeps running.
        fake.states = [_state_entry("light.kitchen", "Kitchen Ceiling Lights")]

        result = ha.run("turn on the kitchen ceiling lights")
        self.assertEqual(result, "Okay, turning on kitchen ceiling lights.")
        self.assertEqual(self._states_call_count(fake), 2, "the miss should have triggered one refresh")

    def test_newly_added_entity_resolves_after_a_miss(self):
        fake = self._patch_urlopen(_FakeHA(states=[_state_entry("light.kitchen", "Kitchen Lights")]))
        ha.run("turn on the kitchen lights")

        fake.states = fake.states + [_state_entry("light.porch", "Porch Light")]

        self.assertEqual(ha.run("turn on the porch light"), "Okay, turning on porch light.")

    def test_cooldown_stops_a_miss_storm_refetching_every_time(self):
        fake = self._patch_urlopen(_FakeHA(states=[_state_entry("light.kitchen", "Kitchen Lights")]))
        ha.run("turn on the kitchen lights")
        before = self._states_call_count(fake)

        for _ in range(3):
            self.assertEqual(
                ha.run("turn on the nonexistent gadget"),
                "I couldn't find anything called nonexistent gadget.",
            )

        after = self._states_call_count(fake)
        self.assertEqual(after - before, 1, "repeated misses inside the cooldown must refetch at most once")

    def test_failed_refresh_keeps_the_old_map_and_speaks_no_error(self):
        """A forced refresh that can't reach HA must degrade to 'possibly
        stale', never to a spoken connection error -- we still hold a working
        snapshot."""
        fake = self._patch_urlopen(_FakeHA(states=[_state_entry("light.kitchen", "Kitchen Lights")]))
        ha.run("turn on the kitchen lights")

        fake.states_error = "connection"
        result = ha.run("turn on the nonexistent gadget")

        self.assertEqual(result, "I couldn't find anything called nonexistent gadget.")
        self.assertEqual(ha._ALIASES.get("kitchen lights"), "light.kitchen", "the old map must survive")

    def test_resolution_never_sees_a_half_built_map(self):
        """`_resolve_entity` reads `_ALIASES` without the lock, so a refresh
        must swap the dict in one rebind rather than clear()+update()."""
        fake = self._patch_urlopen(_FakeHA(states=[_state_entry("light.kitchen", "Kitchen Lights")]))
        ha.run("turn on the kitchen lights")
        first = ha._ALIASES

        fake.states = [_state_entry("light.kitchen", "Kitchen Ceiling Lights")]
        ha.run("turn on the kitchen ceiling lights")

        self.assertIsNot(first, ha._ALIASES, "refresh must rebind, not mutate in place")


class TestRunServiceCall(HAToolTestCase):
    def _states(self):
        return [_state_entry("light.kitchen", "Kitchen Lights")]

    def test_turn_on_success(self):
        self._patch_urlopen(_FakeHA(states=self._states()))
        result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "Okay, turning on kitchen lights.")

    def test_entity_not_found(self):
        self._patch_urlopen(_FakeHA(states=self._states()))
        result = ha.run("turn on the nonexistent gadget")
        self.assertEqual(result, "I couldn't find anything called nonexistent gadget.")

    def test_service_call_404(self):
        self._patch_urlopen(_FakeHA(states=self._states(), service_error=404))
        result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "I couldn't find anything called kitchen lights.")

    def test_service_call_connection_error(self):
        self._patch_urlopen(_FakeHA(states=self._states(), service_error="connection"))
        result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "I couldn't reach Home Assistant.")

    def test_service_call_timeout(self):
        self._patch_urlopen(_FakeHA(states=self._states(), service_error="timeout"))
        result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "Home Assistant didn't respond in time.")

    def test_non_json_response_treated_as_unreachable(self):
        # E.g. a reverse proxy in front of HA returning an HTML error page
        # with a 200 status instead of a JSON body.
        self._patch_urlopen(
            _FakeHA(states=self._states(), service_raw_body=b"<html>Bad Gateway</html>")
        )
        result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "I couldn't reach Home Assistant.")

    def test_invalid_utf8_response_treated_as_unreachable(self):
        self._patch_urlopen(_FakeHA(states=self._states(), service_raw_body=b"\x80\x80\x80"))
        result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "I couldn't reach Home Assistant.")

    def test_alias_fetch_failure_not_poisoned(self):
        fake = _FakeHA(states=self._states(), states_error="connection")
        self._patch_urlopen(fake)
        result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "I couldn't reach Home Assistant.")
        self.assertFalse(ha._ALIASES_LOADED)

        # A fresh, healthy backend on the next call should succeed — proving
        # the failed fetch above did not poison the cache.
        fake2 = _FakeHA(states=self._states())
        with mock.patch("examples.tools.home_assistant.urllib.request.urlopen", side_effect=fake2):
            result2 = ha.run("turn on the kitchen lights")
        self.assertEqual(result2, "Okay, turning on kitchen lights.")


class TestRunDenylist(HAToolTestCase):
    def test_denylisted_entity_blocked(self):
        import tempfile

        states = [_state_entry("light.kitchen", "Kitchen Lights")]
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "ha_denylist.json"), "w", encoding="utf-8") as f:
                json.dump(["light.kitchen"], f)
            with mock.patch.dict(os.environ, {"VOICE_TOOLS_DIR": tmpdir}):
                fake = self._patch_urlopen(_FakeHA(states=states))
                result = ha.run("turn on the kitchen lights")
        self.assertEqual(result, "Sorry, kitchen lights isn't available for voice control.")
        # Resolution + deny check happened, but no POST service call was made.
        self.assertNotIn("POST", [m for m, _ in fake.calls])

    def test_denylisted_entity_blocked_query_power(self):
        import tempfile

        states = [_state_entry("light.kitchen", "Kitchen Lights")]
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "ha_denylist.json"), "w", encoding="utf-8") as f:
                json.dump(["light.kitchen"], f)
            with mock.patch.dict(os.environ, {"VOICE_TOOLS_DIR": tmpdir}):
                fake = self._patch_urlopen(_FakeHA(states=states))
                result = ha.run("is the kitchen lights on?")
        self.assertEqual(result, "Sorry, kitchen lights isn't available for voice control.")
        # No per-entity state GET reached HA — only the alias-list fetch did.
        entity_gets = [
            url for method, url in fake.calls
            if method == "GET" and url.endswith("/api/states/light.kitchen")
        ]
        self.assertEqual(entity_gets, [])

    def test_denylisted_entity_blocked_query_temperature(self):
        import tempfile

        states = [_state_entry("light.kitchen", "Kitchen Lights")]
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "ha_denylist.json"), "w", encoding="utf-8") as f:
                json.dump(["light.kitchen"], f)
            with mock.patch.dict(os.environ, {"VOICE_TOOLS_DIR": tmpdir}):
                fake = self._patch_urlopen(_FakeHA(states=states))
                result = ha.run("what's the temperature in the kitchen lights?")
        self.assertEqual(result, "Sorry, kitchen lights isn't available for voice control.")
        entity_gets = [
            url for method, url in fake.calls
            if method == "GET" and url.endswith("/api/states/light.kitchen")
        ]
        self.assertEqual(entity_gets, [])


class TestRunBrightnessColorScene(HAToolTestCase):
    def _states(self):
        return [
            _state_entry("light.office", "Office Lamp"),
            _state_entry("scene.movie_night", "Movie Night"),
        ]

    def test_set_brightness(self):
        self._patch_urlopen(_FakeHA(states=self._states()))
        result = ha.run("set the office lamp brightness to 50 percent")
        self.assertEqual(result, "Setting office lamp brightness to 50 percent.")

    def test_set_color(self):
        self._patch_urlopen(_FakeHA(states=self._states()))
        result = ha.run("set the office lamp to blue")
        self.assertEqual(result, "Setting office lamp to blue.")

    def test_activate_scene(self):
        self._patch_urlopen(_FakeHA(states=self._states()))
        result = ha.run("activate the movie night scene")
        self.assertEqual(result, "Activating movie night.")


class TestRunQueries(HAToolTestCase):
    def _states(self):
        return [
            _state_entry("light.kitchen", "Kitchen Lights"),
            _state_entry("climate.living_room", "Living Room"),
        ]

    def test_query_power_on(self):
        entity_states = {"light.kitchen": {"entity_id": "light.kitchen", "state": "on", "attributes": {}}}
        self._patch_urlopen(_FakeHA(states=self._states(), entity_states=entity_states))
        result = ha.run("is the kitchen lights on?")
        self.assertEqual(result, "Yes, kitchen lights is on.")

    def test_query_temperature(self):
        entity_states = {
            "climate.living_room": {
                "entity_id": "climate.living_room",
                "state": "heat",
                "attributes": {"current_temperature": 71.0},
            }
        }
        self._patch_urlopen(_FakeHA(states=self._states(), entity_states=entity_states))
        result = ha.run("what's the temperature in the living room?")
        self.assertEqual(result, "It's 71 degrees in living room.")


# ---------------------------------------------------------------------------
# Drop-in contract conformance.
# ---------------------------------------------------------------------------


class TestContract(unittest.TestCase):
    def test_tool_def_shape(self):
        self.assertEqual(ha.TOOL_DEF["type"], "function")
        self.assertEqual(ha.TOOL_DEF["name"], "home_assistant")
        self.assertIsInstance(ha.TOOL_DEF["description"], str)
        params = ha.TOOL_DEF["parameters"]
        self.assertEqual(params["required"], ["command"])
        self.assertIn("command", params["properties"])

    def test_run_accepts_one_positional_arg(self):
        sig = inspect.signature(ha.run)
        params = list(sig.parameters.values())
        self.assertEqual(len(params), 1)
        self.assertIn(
            params[0].kind,
            (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD),
        )


if __name__ == "__main__":
    unittest.main()
