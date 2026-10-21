import json
import re
from datetime import datetime, timezone

import httpx

# ── Shared ───────────────────────────────────────────────
MAX_RESULTS = 5          # enough to give the model choices, few enough to stay cheap
TIMEOUT_S = 10.0
USER_AGENT = "WatchParty/0.1 (+https://github.com/<you>/watch-party)"
HEADERS = {"User-Agent": USER_AGENT}   # CheapShark rejects generic UAs (curl/, python-httpx/) with a 400

# ── TVMaze ───────────────────────────────────────────────
TVMAZE_SEARCH_URL = "https://api.tvmaze.com/search/shows"
SUMMARY_CHARS = 300      # one or two sentences is enough to judge the vibe

# ── CheapShark ───────────────────────────────────────────
CHEAPSHARK_DEALS_URL = "https://www.cheapshark.com/api/1.0/deals"
CHEAPSHARK_STORES_URL = "https://www.cheapshark.com/api/1.0/stores"
CHEAPSHARK_PAGE_SIZE = 20   # fetch more than MAX_RESULTS: dedupe by game shrinks the list

_store_names: dict[str, str] | None = None   # filled on first successful /stores call


# ── Helpers ──────────────────────────────────────────────
def _strip_html(text: str | None) -> str:
    """TVMaze summaries contain <p>/<b> tags — tokens the model doesn't need."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


def _get_store_names() -> dict[str, str]:
    """storeID -> storeName, fetched once per process.

    On failure return {} and DON'T cache it, so the next call retries.
    A missing store name must never fail the whole deals call.
    """
    global _store_names
    if _store_names is not None:
        return _store_names

    try:
        resp = httpx.get(CHEAPSHARK_STORES_URL, headers=HEADERS, timeout=TIMEOUT_S)
        resp.raise_for_status()
        stores = resp.json()
        names = {s["storeID"]: s["storeName"] for s in stores}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {}

    _store_names = names
    return _store_names


def _num(value, cast=float):
    """CheapShark sends numbers as strings. Return cast(value), or None if it's missing/garbage."""
    try:
        return cast(float(value))
    except (TypeError, ValueError):
        return None


# ── Tool: search_tv_shows ────────────────────────────────
def search_tv_shows(query: str) -> dict:
    """Keyword search on TVMaze. Never raises — errors come back as {"error": ...}."""
    try:
        resp = httpx.get(TVMAZE_SEARCH_URL, params={"q": query}, headers=HEADERS, timeout=TIMEOUT_S)
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


# ── Tool: find_game_deals ────────────────────────────────
def find_game_deals(title: str, max_price: float | None = None) -> dict:
    """Current PC game deals from CheapShark, one entry per game (cheapest store). Never raises."""
    params = {"title": title, "pageSize": CHEAPSHARK_PAGE_SIZE}
    if max_price is not None:
        params["upperPrice"] = max_price

    try:
        resp = httpx.get(CHEAPSHARK_DEALS_URL, params=params, headers=HEADERS, timeout=TIMEOUT_S)
        resp.raise_for_status()
    except httpx.TimeoutException:
        return {"error": "CheapShark timed out. Try again or answer without game prices."}
    except httpx.HTTPStatusError as e:
        print(f"[cheapshark] HTTP {e.response.status_code}: {e.response.text[:200]}")
        return {"error": f"CheapShark returned HTTP {e.response.status_code}. Answer without game prices."}
    except httpx.RequestError as e:
        return {"error": f"CheapShark is unreachable ({type(e).__name__}). Answer without game prices."}

    try:
        data = resp.json()
    except ValueError:
        return {"error": "CheapShark returned an unexpected (non-JSON) response."}
    if not isinstance(data, list):
        print(f"[cheapshark] unexpected payload: {str(data)[:200]}")
        return {"error": "CheapShark returned an unexpected response. Answer without game prices."}

    cheapest: dict[str, dict] = {}
    for deal in data:
        game_id = deal.get("gameID")
        price = _num(deal.get("salePrice"))
        if game_id is None or price is None:
            continue
        best = cheapest.get(game_id)
        if best is None or price < _num(best["salePrice"]):
            cheapest[game_id] = deal

    store_names = _get_store_names()
    deals = []
    for deal in list(cheapest.values())[:MAX_RESULTS]:
        store_id = deal.get("storeID")
        rating_count = _num(deal.get("steamRatingCount"), int) or 0
        released = _num(deal.get("releaseDate"), int)
        deals.append({
            "title": deal.get("title"),
            "store": store_names.get(store_id, f"store {store_id}"),
            "sale_price": _num(deal.get("salePrice")),
            "normal_price": _num(deal.get("normalPrice")),
            "savings_pct": _num(deal.get("savings"), round),
            "on_sale": deal.get("isOnSale") == "1",
            "steam_rating_pct": _num(deal.get("steamRatingPercent"), int) if rating_count else None,
            "steam_rating_count": rating_count,
            "release_year": datetime.fromtimestamp(released, tz=timezone.utc).year if released else None,
        })

    return {"title": title, "count": len(deals), "deals": deals}


FIND_GAME_DEALS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_game_deals",
        "description": (
            "Look up current prices and deals for a PC game by title, across digital stores "
            "(Steam, GOG, Humble, Fanatical...), via CheapShark. PC only; no console prices. "
            "Title matching is loose, so results can include DLC, soundtracks, and similarly named games "
            "(DLC and soundtracks often have no Steam rating); check each title before recommending it. "
            f"Returns up to {MAX_RESULTS} games, one entry per game at its cheapest store, each with: "
            "title, store, sale_price and normal_price (USD), savings_pct, on_sale, "
            "steam_rating_pct (0-100, null if no reviews), steam_rating_count, and release_year. "
            "Use this when the user names a game, or asks whether specific games are cheap or on sale. "
            "Do NOT use it to discover games by genre, co-op, player count, or mood: it has no such data. "
            "For a request like 'a cheap co-op game', pick specific titles you are confident fit, "
            "then call this once per title to get real prices."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "A game title or part of one, e.g. 'Overcooked' or 'Stardew Valley'. "
                        "Not a genre, feature, or description."
                    ),
                },
                "max_price": {
                    "type": "number",
                    "description": (
                        "Optional. Only return deals at or below this price in USD, e.g. 10. "
                        "Omit it unless the user gave a budget."
                    ),
                },
            },
            "required": ["title"],
        },
    },
}


# ── Registry ─────────────────────────────────────────────
TOOLS = {
    "search_tv_shows": search_tv_shows,
    "find_game_deals": find_game_deals,
}

TOOL_SCHEMAS = [
    SEARCH_TV_SHOWS_SCHEMA,
    FIND_GAME_DEALS_SCHEMA,
]


if __name__ == "__main__":
    # Smoke tests: call each tool directly (no LLM) and eyeball the trimmed output.
    smoke_tests = [
        ("tv: happy path",       search_tv_shows,  {"query": "Arcane"}),
        ("tv: not found",        search_tv_shows,  {"query": "zzqxblorp"}),
        ("games: dedupe",        find_game_deals,  {"title": "overcooked"}),        # expect each game once, cheapest store
        ("games: price cap",     find_game_deals,  {"title": "stardew", "max_price": 5}),
        ("games: not found",     find_game_deals,  {"title": "zzqxblorp"}),
    ]
    for label, fn, kwargs in smoke_tests:
        print(f"\n=== {label}: {fn.__name__}({kwargs})")
        print(json.dumps(fn(**kwargs), indent=2, ensure_ascii=False))
