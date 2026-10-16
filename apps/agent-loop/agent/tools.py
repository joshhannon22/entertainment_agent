import re

import httpx

TVMAZE_SEARCH_URL = "https://api.tvmaze.com/search/shows"
MAX_RESULTS = 5          # enough to give the model choices, few enough to stay cheap
SUMMARY_CHARS = 300      # one or two sentences is enough to judge the vibe
TIMEOUT_S = 10.0


def _strip_html(text: str | None) -> str:
    """TVMaze summaries contain <p>/<b> tags — tokens the model doesn't need."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def search_tv_shows(query: str) -> dict:
    """Keyword search on TVMaze. Never raises — errors come back as {"error": ...}."""
    try:
        resp = httpx.get(TVMAZE_SEARCH_URL, params={"q": query}, timeout=TIMEOUT_S)
        resp.raise_for_status()
    except httpx.TimeoutException:
        return {"error": "TVMaze timed out. Try again or use a different source."}
    except httpx.HTTPStatusError as e:
        return {"error": f"TVMaze returned HTTP {e.response.status_code}."}
    except httpx.RequestError as e:
        return {"error": f"TVMaze is unreachable ({type(e).__name__})."}

    try:
        data = resp.json()
    except ValueError:  # json.JSONDecodeError is a subclass of ValueError
        return {"error": "TVMaze returned an unexpected (non-JSON) response."}

    results = []
    for item in data[:MAX_RESULTS]:
        show = item["show"]
        channel = show.get("webChannel") or show.get("network") or {}
        results.append({
            "name": show.get("name"),
            "type": show.get("type"),
            "genres": show.get("genres", []),
            "status": show.get("status"),
            "premiered": show.get("premiered"),
            "ended": show.get("ended"),
            "avg_runtime_min": show.get("averageRuntime"),
            "rating": (show.get("rating") or {}).get("average"),
            "where_to_watch": channel.get("name"),
            "summary": _strip_html(show.get("summary"))[:SUMMARY_CHARS],
        })

    return {"query": query, "count": len(results), "results": results}


SEARCH_TV_SHOWS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tv_shows",
        "description": (
            "Look up TV shows by title on TVMaze. "
            "Matching is fuzzy on the show's name, so results can include loosely related titles; "
            "check each result's name before relying on it. "
            f"Returns up to {MAX_RESULTS} shows, each with: name, type (Scripted/Animation/Documentary...), "
            "genres, status (Running/Ended), premiered and ended dates, avg_runtime_min (average episode length), "
            "rating (0-10, may be null), where_to_watch (streaming service or TV network), and a short summary. "
            "Use this when the user names a specific show or wants details about one: "
            "where to watch it, episode length, whether it has finished. "
            "Do NOT use it to find shows by genre, mood, or similarity (e.g. 'dark fantasy shows', "
            "'shows like Arcane'). It only matches titles."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A show title or part of one, e.g. 'Arcane' or 'Breaking Bad'. "
                        "Not a genre, mood, or description."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}


# ── Registry ─────────────────────────────────────────────
TOOLS = {
    "search_tv_shows": search_tv_shows,
}

TOOL_SCHEMAS = [
    SEARCH_TV_SHOWS_SCHEMA,
]