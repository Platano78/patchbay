"""Unit tests for brain_lanes.py -- the lane-type registry and its CLI.

Run from repo root: python3 -m pytest patches/test_brain_lanes.py -v

No stubbing needed, unlike test_brain_control.py: brain_lanes.py is
dependency-light by design (stdlib + httpx only, no `speech_to_speech.*`
imports -- a property these tests also pin), so it imports as plain
`patches.brain_lanes`.

**No test here touches the network.** Every `/models` probe is mocked; a suite
that needs OpenRouter to be up is a suite that goes red for reasons that have
nothing to do with this code.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import pathlib
import re
import sys

import httpx
import pytest

from patches import brain_lanes


HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text_body=None):
        self.status_code = status_code
        self._payload = payload
        self._text_body = text_body

    def json(self):
        if self._text_body is not None:
            raise ValueError("not JSON")
        return self._payload


def _mock_models(monkeypatch, response=None, raises=None):
    """Point `/models` GETs at a canned answer. Records the calls so a test
    can assert something was NOT probed."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if raises is not None:
            raise raises
        return response

    # Patched on the real httpx module, not on a `brain_lanes.httpx` alias --
    # the import is lazy now, so `_fetch_model_ids` resolves it through
    # sys.modules at call time and sees this patch.
    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


# ── the registry itself (ruling 3: these five, with these fields) ──────


def test_ships_exactly_the_five_ruled_lane_types():
    assert sorted(brain_lanes.LANES) == [
        "agent",
        "anthropic",
        "nvidia-nim",
        "openai-compatible",
        "openrouter",
    ]


@pytest.mark.parametrize(
    "key,kind,base_url,needs_key,key_var,default_model,auto_ok,sends_ctk,models_public",
    [
        ("openai-compatible", "local", None, False, None, None, True, True, False),
        (
            "openrouter",
            "hosted",
            "https://openrouter.ai/api/v1",
            True,
            "OPENROUTER_API_KEY",
            None,
            False,
            False,
            True,
        ),
        (
            "nvidia-nim",
            "hosted",
            "https://integrate.api.nvidia.com/v1",
            True,
            "NVIDIA_API_KEY",
            None,
            False,
            False,
            True,
        ),
        (
            "anthropic",
            "hosted",
            "https://api.anthropic.com/v1",
            True,
            "ANTHROPIC_API_KEY",
            "claude-sonnet-5",
            False,
            False,
            False,
        ),
        ("agent", "agent", None, False, None, None, True, False, False),
    ],
)
def test_each_lane_matches_the_ruled_table(
    key, kind, base_url, needs_key, key_var, default_model, auto_ok, sends_ctk, models_public
):
    """Pins the ruled table verbatim. These are not incidental values -- each
    was probed against the live provider, so a silent edit here is a
    regression, not a refactor."""
    lane = brain_lanes.LANES[key]
    assert (lane.kind, lane.base_url, lane.needs_key, lane.key_var) == (kind, base_url, needs_key, key_var)
    assert lane.default_model == default_model
    assert lane.auto_ok is auto_ok
    assert lane.sends_chat_template_kwargs is sends_ctk
    assert lane.models_endpoint_public is models_public


def test_a_lane_needing_a_key_says_where_to_get_one():
    for lane in brain_lanes.LANES.values():
        if lane.needs_key:
            assert lane.key_var, f"{lane.key}: needs a key but names no variable"
            assert lane.key_console_url.startswith("https://"), f"{lane.key}: no key console"


def test_a_lane_without_a_default_model_can_list_them_instead():
    """A lane with no default model must give the user some other way to name
    one -- either `auto` genuinely works, or its catalogue is public so the
    CLI can print live ids. Neither, and the entry it prints is a dead end."""
    for lane in brain_lanes.LANES.values():
        if lane.default_model is None:
            assert lane.auto_ok or lane.models_endpoint_public, lane.key


def test_hosted_lanes_send_no_chat_template_kwargs():
    for lane in brain_lanes.LANES.values():
        if lane.kind == brain_lanes.KIND_HOSTED:
            assert lane.sends_chat_template_kwargs is False, lane.key
            assert lane.auto_ok is False, lane.key


def test_only_a_real_inference_server_gets_chat_template_kwargs():
    """`chat_template_kwargs` is for a server that renders a chat template
    itself. That is the local lane and nothing else -- an agent shim renders
    none, and may be fronting a hosted model that rejects the field."""
    senders = {k for k, lane in brain_lanes.LANES.items() if lane.sends_chat_template_kwargs}
    assert senders == {"openai-compatible"}


def test_every_lane_has_a_note_and_a_label():
    for lane in brain_lanes.LANES.values():
        assert lane.label.strip()
        assert lane.note.strip()


def test_module_imports_nothing_from_speech_to_speech():
    """The standalone contract, pinned: `check` has to run before a pipeline
    exists, which is exactly when the person reading it is stuck.

    Walks the parsed IMPORT nodes rather than grepping the text -- the module
    docstring and DEFAULT_BRAINS_JSON both mention the package by name, and a
    grep would pass or fail on prose instead of on what is imported."""
    import ast

    tree = ast.parse((HERE / "brain_lanes.py").read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported, "no imports parsed -- the test is looking at the wrong thing"
    for name in imported:
        assert not name.startswith("speech_to_speech"), name
    # And nothing beyond stdlib + httpx.
    assert set(imported) <= {
        "__future__", "argparse", "ast", "dataclasses", "json", "logging",
        "os", "sys", "textwrap", "typing", "httpx",
    }, sorted(set(imported))


def test_httpx_is_not_imported_at_module_scope():
    """A companion to the absent-httpx tests below, and a cheaper failure: it
    points at the line, where those only tell you the command died. Neither is
    sufficient alone -- this one checks a proxy (where the import sits), they
    check the requirement (does it run)."""
    import ast

    tree = ast.parse((HERE / "brain_lanes.py").read_text())
    for node in tree.body:  # top level only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert "httpx" not in names, "httpx must be imported lazily, inside the functions that use it"


# ── get_lane ──────────────────────────────────────────────────────────


def test_get_lane_returns_none_for_anything_unrecognised():
    assert brain_lanes.get_lane("openrouter") is brain_lanes.LANES["openrouter"]
    assert brain_lanes.get_lane("nope") is None
    assert brain_lanes.get_lane(None) is None
    # brains.json is hand-edited: a non-string must not raise.
    assert brain_lanes.get_lane(17) is None
    assert brain_lanes.get_lane(["openrouter"]) is None


# ── resolve_entry (ruling 4) ──────────────────────────────────────────


def test_an_untyped_entry_is_returned_unchanged_and_unwrapped():
    """The whole no-type path, pinned: not merely equal, the SAME object.
    Existing configs must behave byte for byte as they did."""
    entry = {"base_url": "http://10.0.0.5:8080/v1", "model": "auto", "available": True}
    assert brain_lanes.resolve_entry(entry) is entry


def test_a_typed_entry_inherits_base_url_and_key_var():
    resolved = brain_lanes.resolve_entry({"type": "openrouter", "available": True})
    assert resolved["base_url"] == "https://openrouter.ai/api/v1"
    assert resolved["api_key_var"] == "OPENROUTER_API_KEY"


def test_explicit_values_always_beat_the_lane():
    resolved = brain_lanes.resolve_entry(
        {"type": "openrouter", "base_url": "http://10.0.0.9:9000/v1", "api_key_var": "MY_KEY"}
    )
    assert resolved["base_url"] == "http://10.0.0.9:9000/v1"
    assert resolved["api_key_var"] == "MY_KEY"


def test_resolve_entry_does_not_mutate_the_caller_dict():
    entry = {"type": "openrouter"}
    brain_lanes.resolve_entry(entry)
    assert entry == {"type": "openrouter"}


def test_a_typed_entry_inherits_its_lanes_default_model():
    """The lane supplies the hardest field for a newcomer to know. Anthropic
    is the one lane that ships a default -- its /models needs a key, so
    nothing else can tell the user what to write, and falling through to
    "auto" yields "model probe failed" for what is really "you never named a
    model"."""
    resolved = brain_lanes.resolve_entry({"type": "anthropic", "available": True})
    assert resolved["model"] == "claude-sonnet-5"


def test_an_explicit_model_beats_the_lanes_default():
    resolved = brain_lanes.resolve_entry({"type": "anthropic", "model": "claude-opus-5"})
    assert resolved["model"] == "claude-opus-5"


def test_a_lane_with_no_default_model_supplies_none():
    """OpenRouter and NIM ship no default on purpose -- their ids drift, so a
    hardcoded one rots into a 404. Inheritance must not invent one."""
    for key in ("openrouter", "nvidia-nim"):
        assert "model" not in brain_lanes.resolve_entry({"type": key}), key


def test_a_lane_with_no_base_url_of_its_own_supplies_nothing():
    resolved = brain_lanes.resolve_entry({"type": "openai-compatible"})
    assert "base_url" not in resolved


def test_an_unknown_type_warns_once_and_is_otherwise_ignored(caplog):
    entry = {"type": "openrouterr", "base_url": "http://localhost:8080/v1"}
    with caplog.at_level(logging.WARNING, logger=brain_lanes.__name__):
        resolved = brain_lanes.resolve_entry(entry)
    assert resolved is entry  # fail open: a typo must not take a brain offline
    assert len(caplog.records) == 1
    assert "openrouterr" in caplog.text
    assert "openrouter" in caplog.text  # the known types, so the typo is findable


# ── resolve_api_key ───────────────────────────────────────────────────


def test_a_literal_api_key_wins(tmp_path):
    key_file = tmp_path / "k.env"
    key_file.write_text("SOME_KEY=from-the-file\n")
    entry = {"api_key": "literal", "api_key_file": str(key_file), "api_key_var": "SOME_KEY"}
    assert brain_lanes.resolve_api_key(entry) == "literal"


def test_a_key_file_is_parsed_env_style(tmp_path):
    key_file = tmp_path / "k.env"
    key_file.write_text(
        "# a comment\n"
        "\n"
        "OTHER=nope\n"
        'OPENROUTER_API_KEY="sk-quoted"\n'
    )
    entry = {"api_key_file": str(key_file), "api_key_var": "OPENROUTER_API_KEY"}
    assert brain_lanes.resolve_api_key(entry) == "sk-quoted"


def test_a_key_file_path_may_start_with_a_tilde(tmp_path, monkeypatch):
    """`~/.config/patchbay/openrouter.env` is the natural thing to write.
    Before this it silently resolved to nothing and read back as a missing
    key -- a trap, not a rule."""
    monkeypatch.setenv("HOME", str(tmp_path))
    key_file = tmp_path / ".config" / "patchbay" / ("openrouter" + ".env")
    key_file.parent.mkdir(parents=True)
    key_file.write_text("OPENROUTER_API_KEY=sk-tilde\n")

    entry = {
        "api_key_file": "~/.config/patchbay/openrouter.env",
        "api_key_var": "OPENROUTER_API_KEY",
    }

    assert brain_lanes.resolve_api_key(entry) == "sk-tilde"


def test_a_missing_variable_or_file_resolves_to_none(tmp_path, caplog):
    key_file = tmp_path / "k.env"
    key_file.write_text("OTHER=nope\n")
    assert brain_lanes.resolve_api_key({"api_key_file": str(key_file), "api_key_var": "MISSING"}) is None
    assert brain_lanes.resolve_api_key({"api_key_var": "MISSING"}) is None
    assert brain_lanes.resolve_api_key({}) is None
    with caplog.at_level(logging.WARNING, logger=brain_lanes.__name__):
        assert (
            brain_lanes.resolve_api_key(
                {"api_key_file": str(tmp_path / "gone.env"), "api_key_var": "X"}
            )
            is None
        )
    assert "gone.env" in caplog.text


def test_a_resolved_key_is_never_logged(tmp_path, caplog):
    key_file = tmp_path / "k.env"
    key_file.write_text("K=sk-super-secret\n")
    with caplog.at_level(logging.DEBUG, logger=brain_lanes.__name__):
        brain_lanes.resolve_api_key({"api_key_file": str(key_file), "api_key_var": "K"})
    assert "sk-super-secret" not in caplog.text


# ── missing_key_error (ruling 7's message) ────────────────────────────


def test_the_missing_key_message_names_the_variable_and_the_file():
    lane = brain_lanes.LANES["openrouter"]
    msg = brain_lanes.missing_key_error(
        "frontier", {"api_key_var": "OPENROUTER_API_KEY", "api_key_file": "/etc/keys.env"}, lane
    )
    assert "frontier" in msg
    assert "OPENROUTER_API_KEY" in msg
    assert "/etc/keys.env" in msg


def test_with_no_file_configured_the_message_says_what_to_add():
    lane = brain_lanes.LANES["openrouter"]
    msg = brain_lanes.missing_key_error("frontier", {}, lane)
    assert "api_key_file" in msg
    assert "OPENROUTER_API_KEY" in msg
    assert lane.key_console_url in msg


# ── brains.json path resolution ───────────────────────────────────────


def test_brains_json_path_prefers_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAINS_JSON", str(tmp_path / "b.json"))
    assert brain_lanes.brains_json_path() == str(tmp_path / "b.json")


def test_brains_json_path_falls_back_to_the_documented_default(monkeypatch):
    monkeypatch.delenv("BRAINS_JSON", raising=False)
    assert brain_lanes.brains_json_path() == os.path.expanduser(brain_lanes.DEFAULT_BRAINS_JSON)


def test_the_default_path_matches_the_pipelines_own(monkeypatch):
    """`check` is worthless if it inspects a different file from the one the
    running service reads, so the two literals are pinned to each other."""
    source = (HERE / "s2s_pipeline.py").read_text()
    found = re.findall(r'os\.environ\.get\("BRAINS_JSON", os\.path\.expanduser\("([^"]+)"\)\)', source)
    assert found == [brain_lanes.DEFAULT_BRAINS_JSON]


# ── example_entry + the shipped brains.json.example ───────────────────


def test_a_keyless_lane_ships_ready_to_use():
    entry = brain_lanes.example_entry(brain_lanes.LANES["openai-compatible"])["openai-compatible"]
    assert entry["available"] is True
    assert entry["model"] == "auto"
    assert "api_key_file" not in entry
    assert entry["base_url"]  # a placeholder, but a real, plausible one


def test_a_lane_needing_a_key_ships_unavailable_with_an_absolute_key_path():
    entry = brain_lanes.example_entry(brain_lanes.LANES["openrouter"])["openrouter"]
    assert entry["available"] is False
    assert entry["api_key_var"] == "OPENROUTER_API_KEY"
    # `/path/to/...` is a value the reader MUST replace, and an obviously-fake
    # path says so where a realistic `~/.config/...` invites a straight paste.
    # Both forms work at runtime; this is about the snippet, not the parser.
    assert entry["api_key_file"].startswith("/path/to/")


def test_no_snippet_suggests_auto_on_a_lane_where_auto_is_wrong():
    for lane in brain_lanes.LANES.values():
        entry = brain_lanes.example_entry(lane)[lane.key]
        if not lane.auto_ok:
            assert entry["model"] != "auto", lane.key


def test_key_and_label_are_the_users_to_choose():
    out = brain_lanes.example_entry(brain_lanes.LANES["openrouter"], key="frontier", label="My lane")
    assert list(out) == ["frontier"]
    assert out["frontier"]["label"] == "My lane"


def test_the_shipped_example_agrees_with_the_registry():
    """brains.json.example is generated from the lane types; this is what
    stops it drifting back into a hand-written guess (the defect that shipped
    four of the maintainer's own lanes to strangers)."""
    data = json.loads((REPO_ROOT / "brains.json.example").read_text())
    assert data, "brains.json.example is empty"
    for name, entry in data.items():
        lane = brain_lanes.get_lane(entry.get("type"))
        assert lane is not None, f"{name}: unknown or missing lane type {entry.get('type')!r}"
        if lane.base_url:
            assert entry["base_url"] == lane.base_url, name
        if lane.needs_key:
            assert entry["api_key_var"] == lane.key_var, name
            assert entry["api_key_file"].startswith("/"), name
            assert entry["available"] is False, f"{name}: shipped available with no key"
        if not lane.auto_ok:
            assert entry.get("model") != "auto", name


# ── _fetch_model_ids: every failure becomes a sentence ────────────────


def test_a_served_catalogue_comes_back_as_ids(monkeypatch):
    _mock_models(monkeypatch, _FakeResponse(200, {"data": [{"id": "a"}, {"id": "b"}, {"junk": 1}]}))
    ids, error = brain_lanes._fetch_model_ids("https://example.invalid/v1")
    assert (ids, error) == (["a", "b"], "")


@pytest.mark.parametrize(
    "response,raises,expected",
    [
        (_FakeResponse(401, {}), None, "key"),
        (_FakeResponse(403, {}), None, "key"),
        (_FakeResponse(404, {}), None, "base_url"),
        (_FakeResponse(500, {}), None, "500"),
        (_FakeResponse(200, None, text_body="<html>"), None, "OpenAI-compatible"),
        (_FakeResponse(200, {"data": []}), None, "served no models"),
        (None, httpx.ConnectError("refused"), "nothing is listening"),
        (None, httpx.ReadTimeout("slow"), "within 3s"),
    ],
)
def test_every_probe_failure_is_stated_as_something_to_act_on(monkeypatch, response, raises, expected):
    _mock_models(monkeypatch, response, raises)
    ids, error = brain_lanes._fetch_model_ids("https://example.invalid/v1")
    assert ids is None
    assert expected in error


def test_the_probe_sends_the_key_as_a_bearer_token(monkeypatch):
    calls = _mock_models(monkeypatch, _FakeResponse(200, {"data": [{"id": "a"}]}))
    brain_lanes._fetch_model_ids("https://example.invalid/v1", "sk-abc")
    assert calls[0][1]["headers"] == {"Authorization": "Bearer sk-abc"}
    calls.clear()
    brain_lanes._fetch_model_ids("https://example.invalid/v1")
    assert calls[0][1]["headers"] is None


# ── the CLI ───────────────────────────────────────────────────────────


def test_bare_invocation_lists_every_lane(capsys):
    assert brain_lanes.main([]) == 0
    out = capsys.readouterr().out
    for key in brain_lanes.LANES:
        assert key in out


def test_show_prints_the_fields_and_a_parseable_entry(monkeypatch, capsys):
    _mock_models(monkeypatch, _FakeResponse(200, {"data": [{"id": f"vendor/m{i}"} for i in range(50)]}))
    assert brain_lanes.main(["show", "openrouter"]) == 0
    out = capsys.readouterr().out
    assert "OPENROUTER_API_KEY" in out
    assert "https://openrouter.ai/keys" in out
    # The snippet must be valid JSON a reader can literally paste.
    snippet = out[out.index("{") : out.rindex("}") + 1]
    parsed = json.loads("\n".join(line.strip() for line in snippet.splitlines()))
    assert list(parsed) == ["openrouter"]


def test_show_prints_real_ids_for_a_public_catalogue_and_never_invents_them(monkeypatch, capsys):
    served = [{"id": f"vendor/model-{i:03d}"} for i in range(40)]
    _mock_models(monkeypatch, _FakeResponse(200, {"data": served}))
    brain_lanes.main(["show", "openrouter"])
    out = capsys.readouterr().out
    printed = re.findall(r"^  (vendor/model-\d{3})$", out, re.M)
    assert printed, "no live ids printed"
    assert len(printed) <= brain_lanes.MAX_SHOWN_MODEL_IDS
    assert set(printed) <= {e["id"] for e in served}
    assert "40 ids in total" in out


def test_show_says_so_when_the_catalogue_cannot_be_read(monkeypatch, capsys):
    import httpx

    _mock_models(monkeypatch, None, httpx.ConnectError("down"))
    assert brain_lanes.main(["show", "openrouter"]) == 0
    out = capsys.readouterr().out
    assert "Could not read the live catalogue" in out
    # Still prints a usable entry -- a provider being down is not a reason to
    # leave the reader with nothing.
    assert "OPENROUTER_API_KEY" in out


def test_show_never_probes_a_lane_whose_catalogue_needs_a_key(monkeypatch, capsys):
    calls = _mock_models(monkeypatch, _FakeResponse(200, {"data": []}))
    assert brain_lanes.main(["show", "anthropic"]) == 0
    assert calls == [], "show must not probe a catalogue it cannot read without a key"
    assert "claude-sonnet-5" in capsys.readouterr().out


def test_show_of_an_unknown_type_exits_two_and_lists_the_known_ones(capsys):
    assert brain_lanes.main(["show", "nope"]) == 2
    out = capsys.readouterr().out
    assert "openrouter" in out


# ── check ─────────────────────────────────────────────────────────────


def test_check_names_the_file_it_looked_for_when_it_is_absent(tmp_path, capsys):
    missing = tmp_path / "nowhere" / "brains.json"
    assert brain_lanes.main(["check", "--brains-json", str(missing)]) == 1
    out = capsys.readouterr().out
    assert str(missing) in out
    # And says plainly that this is not fatal -- the whole point of ruling 8.
    assert "not fatal" in out


def test_check_reports_the_line_and_column_of_a_json_error(tmp_path, capsys):
    bad = tmp_path / "brains.json"
    bad.write_text('{"a": }')
    assert brain_lanes.main(["check", "--brains-json", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "line 1" in out
    assert "column" in out


def test_check_refuses_a_keyless_hosted_entry_before_probing_it(tmp_path, monkeypatch, capsys):
    path = tmp_path / "brains.json"
    path.write_text(json.dumps({"frontier": {"type": "openrouter", "model": "a/b", "available": True}}))
    calls = _mock_models(monkeypatch, _FakeResponse(200, {"data": [{"id": "a/b"}]}))

    assert brain_lanes.main(["check", "--brains-json", str(path)]) == 1

    out = capsys.readouterr().out
    assert "no API key" in out
    assert "OPENROUTER_API_KEY" in out
    assert calls == [], "a lane with no key must not be probed at all"


def test_check_warns_that_a_public_catalogue_does_not_validate_a_key(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / "k.env"
    key_file.write_text("OPENROUTER_API_KEY=sk-whatever\n")
    path = tmp_path / "brains.json"
    path.write_text(
        json.dumps(
            {
                "frontier": {
                    "type": "openrouter",
                    "model": "a/b",
                    "available": True,
                    "api_key_file": str(key_file),
                }
            }
        )
    )
    _mock_models(monkeypatch, _FakeResponse(200, {"data": [{"id": "a/b"}]}))

    assert brain_lanes.main(["check", "--brains-json", str(path)]) == 0

    out = capsys.readouterr().out
    assert "does NOT prove your key is valid" in out
    # Names the one command that would actually test it -- and does not run it.
    assert "curl" in out
    assert "chat/completions" in out
    assert "sk-whatever" not in out


def test_check_flags_auto_on_a_lane_where_auto_is_wrong(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / "k.env"
    key_file.write_text("OPENROUTER_API_KEY=sk-x\n")
    path = tmp_path / "brains.json"
    path.write_text(
        json.dumps(
            {
                "frontier": {
                    "type": "openrouter",
                    "model": "auto",
                    "available": True,
                    "api_key_file": str(key_file),
                }
            }
        )
    )
    _mock_models(monkeypatch, _FakeResponse(200, {"data": [{"id": "a/b"}]}))
    brain_lanes.main(["check", "--brains-json", str(path)])
    assert '"auto" is not safe' in capsys.readouterr().out


def test_check_diagnoses_an_untyped_entry_without_a_base_url(tmp_path, capsys):
    path = tmp_path / "brains.json"
    path.write_text(json.dumps({"mystery": {"available": True}, "junk": "not an object"}))
    assert brain_lanes.main(["check", "--brains-json", str(path)]) == 1
    out = capsys.readouterr().out
    assert "no base_url" in out
    assert "must be" in out  # the non-object entry


def test_check_never_writes_the_file_it_is_checking(tmp_path, monkeypatch, capsys):
    """Same ruling as brain_discovery: it prints a snippet, it never edits the
    user's config. A tool that rewrites the file holding your API keys is a
    tool you cannot leave running."""
    path = tmp_path / "brains.json"
    before = json.dumps({"local": {"type": "openai-compatible", "base_url": "http://x/v1", "available": True}})
    path.write_text(before)
    _mock_models(monkeypatch, _FakeResponse(200, {"data": [{"id": "m"}]}))

    brain_lanes.main(["check", "--brains-json", str(path)])

    assert path.read_text() == before


def test_check_uses_the_env_path_when_none_is_given(tmp_path, monkeypatch, capsys):
    path = tmp_path / "from-env.json"
    path.write_text("{}")
    monkeypatch.setenv("BRAINS_JSON", str(path))
    assert brain_lanes.main(["check"]) == 0
    assert str(path) in capsys.readouterr().out


# ── the stranger with no venv: httpx genuinely absent ─────────────────
#
# SETUP.md points here at step 0, BEFORE ./setup.sh has built a venv. A
# module-level `import httpx` therefore made this whole file unrunnable for
# exactly the person it exists to help. These tests import the module fresh
# with httpx really blocked -- asserting "the import statement is inside a
# function" would be checking a proxy; the requirement is that it RUNS.


class _BlockHttpx:
    """A sys.meta_path finder that makes `import httpx` raise, the way a box
    with no venv does."""

    def find_spec(self, name, path=None, target=None):
        if name == "httpx" or name.startswith("httpx."):
            raise ImportError("No module named 'httpx'")
        return None


@contextlib.contextmanager
def _brain_lanes_without_httpx():
    """Re-import brain_lanes from scratch with httpx unavailable, then put
    sys.modules back exactly as it was -- the module-level `brain_lanes` every
    other test in this file holds must survive unchanged."""
    saved = {k: sys.modules[k] for k in ("httpx", "patches.brain_lanes") if k in sys.modules}
    blocker = _BlockHttpx()
    sys.meta_path.insert(0, blocker)
    for name in ("httpx", "patches.brain_lanes"):
        sys.modules.pop(name, None)
    try:
        yield importlib.import_module("patches.brain_lanes")
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.pop("patches.brain_lanes", None)
        sys.modules.update(saved)


def test_the_module_imports_at_all_without_httpx():
    with _brain_lanes_without_httpx() as fresh:
        assert fresh.LANES.keys() == brain_lanes.LANES.keys()
        assert fresh._load_httpx() is None


def test_listing_the_lane_types_needs_no_packages(capsys):
    with _brain_lanes_without_httpx() as fresh:
        assert fresh.main([]) == 0
    out = capsys.readouterr().out
    for key in brain_lanes.LANES:
        assert key in out


@pytest.mark.parametrize("lane_key", ["openrouter", "anthropic", "openai-compatible", "agent"])
def test_show_still_prints_a_usable_entry_without_httpx(capsys, lane_key):
    """The fields, the key console and the paste-ready entry are all stdlib.
    Only the live model ids need the network."""
    with _brain_lanes_without_httpx() as fresh:
        assert fresh.main(["show", lane_key]) == 0
    out = capsys.readouterr().out
    snippet = out[out.index("{") : out.rindex("}") + 1]
    parsed = json.loads("\n".join(line.strip() for line in snippet.splitlines()))
    assert list(parsed) == [lane_key]
    assert parsed[lane_key]["base_url"]
    lane = brain_lanes.LANES[lane_key]
    if lane.needs_key:
        assert lane.key_var in out
        assert lane.key_console_url in out


def test_show_says_why_the_live_ids_are_missing_rather_than_omitting_them(capsys):
    """Not a silent skip: omitting the section would read as "this provider
    serves no models", which is a different and wrong claim."""
    with _brain_lanes_without_httpx() as fresh:
        assert fresh.main(["show", "openrouter"]) == 0
    out = capsys.readouterr().out
    assert "httpx" in out
    assert "setup.sh" in out
    # And it says the entry is still usable, so the reader does not stop here.
    assert "complete apart from the model id" in out


def test_check_refuses_clearly_without_httpx_instead_of_raising(tmp_path, capsys):
    """`check` is nothing but probing, so this is a real requirement -- but a
    stated one, not a traceback."""
    path = tmp_path / "brains.json"
    path.write_text(json.dumps({"local": {"type": "openai-compatible", "base_url": "http://x/v1"}}))
    with _brain_lanes_without_httpx() as fresh:
        assert fresh.main(["check", "--brains-json", str(path)]) == 1
    out = capsys.readouterr().out
    assert "httpx" in out
    assert "setup.sh" in out
    # ...and points at what DOES work with nothing installed.
    assert "show <type>" in out
