"""Drop-in voice tool: describe (or scan -> list) what the user's camera sees, via
the local Gemma-12B vision endpoint (:8084, multimodal). Server-side, off the audio
thread, TTS-safe plain-text result - same contract as the other voice tools.

Two modes, chosen DETERMINISTICALLY from the request wording (pattern borrowed from the
phonelinux/Embodiment IntentTriage.isCameraScan route - small models won't reliably
pick a mode, so a regex decides, not the model):
  - describe (default): free-text 1-2 sentence answer - "for a human to hear".
  - scan/list: a json-schema-constrained {"items":[...]} -> spoken list - "for tools to
    consume" (the describe->do upgrade; NOT prompt-only, which drifts).

Frame source (decoupled): the webclient uploads the latest camera frame and the websocket
layer writes it to $VOICE_CAMERA_FRAME (default /dev/shm/voice_camera_frame.jpg). This tool
only READS that file; degrades to a calm spoken "can't see" when no fresh frame. Never raises.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request

TOOL_DEF = {
    "type": "function",
    "name": "look",
    "description": (
        "Look at what the user's camera currently shows and answer about it. Use when they ask "
        "what you can see, to identify or read something they hold up, describe their surroundings, "
        "OR to scan/list the objects in view. Pass the user's request as `question` VERBATIM - keep "
        "words like 'list', 'scan', 'itemize', 'what items' so a list request returns a clean list. "
        "Answer in 1-2 spoken sentences."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The user's request, verbatim - e.g. 'scan this as a list', 'read this label', "
                    "'what am I holding', 'describe the room'."
                ),
            }
        },
        "required": [],
    },
}
ARG_KEY = "question"
REQUIRED = False
TIMEOUT_S = 20.0
TOOL_LABEL = "camera view"

_FRAME_PATH = os.environ.get("VOICE_CAMERA_FRAME", "/dev/shm/voice_camera_frame.jpg")
_VISION_URL = os.environ.get("VISION_LLM_URL", "http://localhost:8084/v1/chat/completions")
_VISION_MODEL = os.environ.get("VISION_LLM_MODEL", "gemma4-12b")
_MAX_FRAME_AGE_S = float(os.environ.get("VOICE_CAMERA_MAX_AGE_S", "10"))
_MAX_CHARS = 600

_SCAN_RE = re.compile(
    r"\b(scan|make a list|list (?:the|what|out|everything|all|them|these|those)|itemi[sz]e|inventory|"
    r"read (?:the |this )?(?:receipt|label|menu|list|sign|text|ingredients)|what (?:items|objects|things))\b",
    re.I,
)
_LIST_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
}


def _vision(text_prompt, b64, response_format=None):
    payload = {
        "model": _VISION_MODEL,
        "max_tokens": 160,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text_prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                ],
            }
        ],
    }
    if response_format:
        payload["response_format"] = response_format
    req = urllib.request.Request(
        _VISION_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    resp = json.load(urllib.request.urlopen(req, timeout=TIMEOUT_S))
    return (resp["choices"][0]["message"]["content"] or "").strip()


def _spoken_list(items):
    items = [str(x).strip() for x in items if str(x).strip()][:8]
    if not items:
        return "I don't see anything I can pick out clearly."
    if len(items) == 1:
        return "I see " + items[0] + "."
    return "I see " + ", ".join(items[:-1]) + ", and " + items[-1] + "."


def run(question=None):
    q = (question or "").strip() or "What do you see?"
    try:
        age = time.time() - os.stat(_FRAME_PATH).st_mtime
    except OSError:
        return "I can't see anything right now - the camera isn't on."
    if age > _MAX_FRAME_AGE_S:
        return "I can't see anything right now - the camera view is stale."
    with open(_FRAME_PATH, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    try:
        if _SCAN_RE.search(q):
            content = _vision(
                "List the distinct objects and items visible in this image.",
                b64,
                response_format={"type": "json_schema", "json_schema": {"name": "scene_items", "schema": _LIST_SCHEMA}},
            )
            try:
                items = json.loads(content).get("items", [])
            except Exception:
                items = []
            return _spoken_list(items)
        text = _vision(q + " Answer in 1-2 short spoken sentences, plain text, no markdown or lists.", b64)
        return text[:_MAX_CHARS] if text else "I couldn't make out the view."
    except Exception:
        return "I had trouble seeing that just now."


if __name__ == "__main__":
    print("describe:", run("what do you see"))
    print("scan:", run("scan this and list what you see"))
