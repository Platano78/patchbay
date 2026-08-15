"""Example drop-in voice tool: are your model servers up, and what's loaded?

Copy into your VOICE_TOOLS_DIR and EDIT ``SERVERS`` for your own endpoints
(any OpenAI-compatible /v1/models URL works).
"""
from typing import Optional

import httpx

# EDIT ME: label -> /v1/models URL for each server you want probed.
SERVERS = [
    ("the model server", "http://localhost:8084/v1/models"),
]

TOOL_DEF = {
    "type": "function",
    "name": "model_server_status",
    "description": (
        "Check whether the local AI model servers are up and what model is "
        "loaded. Use when the user asks whether the model, brain, or server "
        "is running."
    ),
    "parameters": {"type": "object", "properties": {}},
}
TIMEOUT_S = 4.0


def _model_id(payload: dict) -> Optional[str]:
    data = payload.get("data") or []
    if data and isinstance(data[0], dict):
        return data[0].get("id")
    return None


def run(_arg=None) -> str:
    parts = []
    for label, url in SERVERS:
        try:
            resp = httpx.get(url, timeout=1.5)
            resp.raise_for_status()
            model = _model_id(resp.json())
            parts.append(f"{label} is up, serving {model}." if model else f"{label} is up.")
        except Exception:
            parts.append(f"{label} isn't responding right now.")
    return " ".join(parts).capitalize() if parts else "No servers are configured to check."
