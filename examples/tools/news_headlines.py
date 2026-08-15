"""Example drop-in voice tool: latest news headlines, optionally by topic.

Copy into your VOICE_TOOLS_DIR. Uses the ddgs package the pipeline already
depends on — no API key needed.
"""
TOOL_DEF = {
    "type": "function",
    "name": "news_headlines",
    "description": (
        "Get the latest news headlines, optionally about a topic. Use when the "
        "user asks what's in the news, what's happening, or for headlines "
        "about something. Speak the headlines; do not read URLs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Topic to get headlines about, e.g. 'nvidia' or 'AI'.",
            }
        },
    },
}
TIMEOUT_S = 10.0
ARG_KEY = "topic"
REQUIRED = False


def run(topic=None) -> str:
    from ddgs import DDGS

    query = topic or "top world news today"
    with DDGS(timeout=int(TIMEOUT_S)) as ddgs:
        hits = list(ddgs.news(query, max_results=3))
    if not hits:
        return "I couldn't find fresh headlines right now."

    parts = []
    for hit in hits[:3]:
        title = (hit.get("title") or "").strip()
        source = (hit.get("source") or "").strip()
        if title and source:
            parts.append(f"{title}, from {source}")
        elif title:
            parts.append(title)

    sentence = " ".join(("Top headlines: " + ". ".join(parts) + ".").split())
    if len(sentence) > 400:
        sentence = sentence[:399].rstrip() + "…"
    return sentence
