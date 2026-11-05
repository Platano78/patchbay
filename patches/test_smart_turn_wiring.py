"""s2s_pipeline must hand the VAD the Smart Turn server address under the name
rename_args() leaves on the remote-transcription kwargs. rename_args strips the
`remote_transcription_` prefix, so the attribute is `base_url`; reading the
prefixed name returns None and Smart Turn silently stays off (live 2026-11-03)."""
import ast
from pathlib import Path

_PIPELINE = Path(__file__).with_name("s2s_pipeline.py")


def _smart_turn_assignments():
    tree = ast.parse(_PIPELINE.read_text())
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
                sl = node.targets[0].slice
                if isinstance(sl, ast.Constant) and sl.value == "smart_turn_base_url":
                    found.append((fn.name, node.value))
    return found


def test_smart_turn_url_is_set_once_in_the_builder_every_mode_shares():
    # The live service runs --mode websocket; wiring only the realtime unit
    # left Smart Turn off there (live 2026-11-03).
    found = _smart_turn_assignments()
    assert [name for name, _ in found] == ["_build_pipeline_handlers"]
    call = found[0][1]
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "getattr"
    assert call.args[0].id == "remote_transcription_stt_handler_kwargs"
    assert call.args[1].value == "base_url"
