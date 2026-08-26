"""Unit tests for tts_capabilities.py: capability discovery for the active
TTS backend (local pocket-style vs remote OpenAI-`/v1/audio/speech`-shaped),
liberal wire parsing, and the failure-retains-last-good cache.

Run from repo root: python3 -m pytest patches/test_tts_capabilities.py -v

No stubbing of `speech_to_speech` needed -- this module is dependency-light
by design (stdlib + httpx only), same as voice_clone.py/brain_discovery.py.
"""

from __future__ import annotations

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from patches import tts_capabilities as tc


@pytest.fixture(autouse=True)
def _clear_cache():
    tc._cache.clear()
    yield
    tc._cache.clear()


# ── fakes: the two detectable backend shapes, plus neither ─────────────────


class _FakeLocalModel:
    def get_state_for_audio_prompt(self, *a, **kw):
        pass


class _LocalHandler:
    def __init__(self):
        self.model = _FakeLocalModel()


class _RemoteHandler:
    def __init__(self, base_url):
        self.base_url = base_url


class _BareHandler:
    pass


def _install_fake_pocket(monkeypatch, origins):
    """Injects a fake `pocket_tts.utils.utils._ORIGINS_OF_PREDEFINED_VOICES`
    -- deterministic, unlike relying on the real package being (un)installed
    in whatever environment the suite happens to run in."""
    pkg = types.ModuleType("pocket_tts")
    utils_pkg = types.ModuleType("pocket_tts.utils")
    utils_mod = types.ModuleType("pocket_tts.utils.utils")
    utils_mod._ORIGINS_OF_PREDEFINED_VOICES = origins
    monkeypatch.setitem(sys.modules, "pocket_tts", pkg)
    monkeypatch.setitem(sys.modules, "pocket_tts.utils", utils_pkg)
    monkeypatch.setitem(sys.modules, "pocket_tts.utils.utils", utils_mod)


class _CountingVoicesServer:
    """Serves `GET /v1/audio/voices` from a queue of (status, body) pairs,
    one consumed per request; sticks on the last entry once exhausted.
    `body=None` sends no payload (for a bare error status)."""

    def __init__(self, responses):
        self.requests = 0
        self._responses = responses
        self._req_lock = threading.Lock()
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with outer._req_lock:
                    outer.requests += 1
                    idx = min(outer.requests, len(outer._responses)) - 1
                status, body = outer._responses[idx]
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is not None:
                    self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ── probe selection ──────────────────────────────────────────────────────


def test_local_backend_detected_via_model_hasattr(monkeypatch):
    _install_fake_pocket(monkeypatch, {"alba": object(), "jean": object()})

    caps = tc.get_capabilities(_LocalHandler())

    assert caps.voices == ["alba", "jean"]
    assert caps.can_clone is True
    assert caps.accepts_instructions is False


def test_local_backend_import_failure_yields_empty_voices_but_still_can_clone(monkeypatch):
    monkeypatch.setitem(sys.modules, "pocket_tts", None)

    caps = tc.get_capabilities(_LocalHandler())

    assert caps.voices == []
    assert caps.can_clone is True
    assert caps.accepts_instructions is False


def test_remote_backend_detected_via_base_url():
    server = _CountingVoicesServer([(200, {"voices": ["jean"]})])
    try:
        caps = tc.get_capabilities(_RemoteHandler(server.base_url))
        assert caps.voices == ["jean"]
    finally:
        server.close()


def test_neither_backend_detected_yields_empty_and_never_probes():
    caps = tc.get_capabilities(_BareHandler())
    assert caps == tc._EMPTY
    assert tc._cache == {}


# ── liberal `voices` parsing ────────────────────────────────────────────


def test_remote_voices_parses_dict_shape():
    server = _CountingVoicesServer(
        [(200, {"voices": [{"name": "jean", "kind": "speaker"}, {"name": "alba"}]})]
    )
    try:
        caps = tc.get_capabilities(_RemoteHandler(server.base_url))
        assert caps.voices == ["alba", "jean"]
    finally:
        server.close()


def test_remote_voices_parses_bare_string_list_shape_and_dedupes():
    server = _CountingVoicesServer([(200, {"voices": ["jean", "alba", "jean"]})])
    try:
        caps = tc.get_capabilities(_RemoteHandler(server.base_url))
        assert caps.voices == ["alba", "jean"]
    finally:
        server.close()


def test_remote_voices_drops_entries_with_no_usable_name():
    server = _CountingVoicesServer([(200, {"voices": [{"kind": "speaker"}, 42, None, {"name": "jean"}]})])
    try:
        caps = tc.get_capabilities(_RemoteHandler(server.base_url))
        assert caps.voices == ["jean"]
    finally:
        server.close()


# ── optional flags: default-on-absent, fall back on non-bool ───────────────


def test_remote_flags_default_when_absent():
    server = _CountingVoicesServer([(200, {"voices": ["jean"]})])
    try:
        caps = tc.get_capabilities(_RemoteHandler(server.base_url))
        assert caps.can_clone is False
        assert caps.accepts_instructions is True
    finally:
        server.close()


def test_remote_non_bool_flags_fall_back_to_default():
    server = _CountingVoicesServer(
        [(200, {"voices": ["jean"], "can_clone": "yes", "accepts_instructions": 1})]
    )
    try:
        caps = tc.get_capabilities(_RemoteHandler(server.base_url))
        assert caps.can_clone is False
        assert caps.accepts_instructions is True
    finally:
        server.close()


# ── cache: TTL, failure retention, never-succeeded, key on base_url ───────


def test_cache_within_ttl_does_not_reprobe():
    server = _CountingVoicesServer([(200, {"voices": ["alba"]}), (200, {"voices": ["jean"]})])
    try:
        handler = _RemoteHandler(server.base_url)
        first = tc.get_capabilities(handler)
        second = tc.get_capabilities(handler)

        assert first.voices == ["alba"]
        assert second.voices == ["alba"]  # cached, not the second queued response
        assert server.requests == 1
    finally:
        server.close()


def test_probe_failure_retains_last_good(monkeypatch):
    server = _CountingVoicesServer([(200, {"voices": ["jean"], "can_clone": True}), (500, None)])
    try:
        handler = _RemoteHandler(server.base_url)
        first = tc.get_capabilities(handler)
        assert first.voices == ["jean"]
        assert first.can_clone is True

        monkeypatch.setattr(tc, "_CACHE_TTL_S", 0.0)  # force the next call to re-probe
        second = tc.get_capabilities(handler)

        assert second == first  # last-good retained despite the 500
        assert server.requests == 2
    finally:
        server.close()


def test_empty_default_when_never_succeeded():
    server = _CountingVoicesServer([(500, None)])
    try:
        caps = tc.get_capabilities(_RemoteHandler(server.base_url))
        assert caps == tc._EMPTY
    finally:
        server.close()


def test_cache_keyed_on_base_url_so_a_changed_base_url_reprobes():
    server_a = _CountingVoicesServer([(200, {"voices": ["alba"]})])
    server_b = _CountingVoicesServer([(200, {"voices": ["jean"]})])
    try:
        caps_a = tc.get_capabilities(_RemoteHandler(server_a.base_url))
        caps_b = tc.get_capabilities(_RemoteHandler(server_b.base_url))

        assert caps_a.voices == ["alba"]
        assert caps_b.voices == ["jean"]
        assert server_a.requests == 1
        assert server_b.requests == 1
    finally:
        server_a.close()
        server_b.close()
