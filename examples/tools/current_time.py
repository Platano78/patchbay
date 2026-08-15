"""Example drop-in voice tool. Point VOICE_TOOLS_DIR at this directory
(or copy this file into yours) and restart the voice agent."""
from datetime import datetime

TOOL_DEF = {
    "type": "function",
    "name": "current_time",
    "description": "Tell the user the current date and time. Use when asked what time or day it is.",
    "parameters": {"type": "object", "properties": {}},
}
TIMEOUT_S = 2.0

def run(_arg=None) -> str:
    now = datetime.now()
    return now.strftime("It's %A, %B %d, %I:%M %p.")
