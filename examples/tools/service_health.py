"""Example drop-in voice tool: are your services up, and how's the disk?

Copy into your VOICE_TOOLS_DIR and EDIT ``CHECKS`` for your own services —
any URL that answers at the transport level counts as "up".
"""
import shutil

import httpx

# EDIT ME: spoken label -> URL. A received response (any status) means "up".
CHECKS = [
    ("the web UI", "http://localhost:3001"),
]

TOOL_DEF = {
    "type": "function",
    "name": "service_health",
    "description": (
        "Health check of this box's services and disk space. Use when the "
        "user asks if everything is up, how the server is doing, or about "
        "disk space."
    ),
    "parameters": {"type": "object", "properties": {}},
}
TIMEOUT_S = 5.0


def run(_arg=None) -> str:
    down = []
    for label, url in CHECKS:
        try:
            httpx.get(url, timeout=1.5)
        except Exception:
            down.append(label)

    usage = shutil.disk_usage("/")
    pct = round(usage.used / usage.total * 100)

    if not CHECKS:
        return f"No services are configured to check. Disk is at {pct} percent."
    if not down:
        n = len(CHECKS)
        if n == 1:
            return f"{CHECKS[0][0].capitalize()} is up and the disk is at {pct} percent."
        return f"All {n} services are up and the disk is at {pct} percent."
    up_count = len(CHECKS) - len(down)
    verb = "is" if len(down) == 1 else "are"
    return f"{', '.join(down)} {verb} down; {up_count} up. Disk at {pct} percent."
