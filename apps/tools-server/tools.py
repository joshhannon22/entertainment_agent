import html
import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Literal, get_args

import httpx

# ── Shared ───────────────────────────────────────────────
MAX_RESULTS = 5          # enough to give the model choices, few enough to stay cheap
TIMEOUT_S = 10.0
USER_AGENT = "WatchParty/0.1 (+https://github.com/joshhannon22/entertainment_agent)"
HEADERS = {"User-Agent": USER_AGENT}   # CheapShark rejects generic UAs (curl/, python-httpx/) with a 400

# ── TVMaze ───────────────────────────────────────────────
TVMAZE_SEARCH_URL = "https://api.tvmaze.com/search/shows"
SUMMARY_CHARS = 300      # one or two sentences is enough to judge the vibe

# ── CheapShark ───────────────────────────────────────────
CHEAPSHARK_DEALS_URL = "https://www.cheapshark.com/api/1.0/deals"
CHEAPSHARK_STORES_URL = "https://www.cheapshark.com/api/1.0/stores"
CHEAPSHARK_PAGE_SIZE = 20   # fetch more than MAX_RESULTS: dedupe by game shrinks the list

_store_names: dict[str, str] | None = None   # filled on first successful /stores call

# ── Open Library ─────────────────────────────────────────
OPENLIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
OPENLIBRARY_FIELDS = "title,author_name,first_publish_year,edition_count,number_of_pages_median,ratings_average,ratings_count,subject"
MAX_SUBJECTS = 5

# ── Jikan (MyAnimeList) ──────────────────────────────────
JIKAN_SEARCH_URL = "https://api.jikan.moe/v4/anime"   # rate limit ~3 req/s, 60/min -> HTTP 429

# ── FreeToGame ───────────────────────────────────────────
FREETOGAME_URL = "https://www.freetogame.com/api/games"
# Every value verified against the live API (2026-11-07). A Literal becomes an `enum` in the
# tool's JSON schema, so the model can only pick a real one (there is no "co-op" tag).
FreeGameCategory = Literal[
    "mmorpg", "shooter", "strategy", "moba", "racing", "sports", "social", "sandbox",
    "open-world", "survival", "pvp", "pve", "pixel", "voxel", "zombie", "turn-based",
    "first-person", "third-Person", "top-down", "tank", "space", "sailing", "side-scroller",
    "superhero", "permadeath", "card", "battle-royale", "mmo", "mmofps", "mmotps", "3d", "2d",
    "anime", "fantasy", "sci-fi", "fighting", "action-rpg", "action", "military",
    "martial-arts", "flight", "low-spec", "tower-defense", "horror", "mmorts",
]

# ── Open Trivia DB ───────────────────────────────────────
OPENTDB_URL = "https://opentdb.com/api.php"
OPENTDB_CATEGORIES = {   # our name -> OpenTDB category id (/api_category.php)
    "general": 9, "books": 10, "film": 11, "music": 12, "tv": 14, "video_games": 15,
    "board_games": 16, "comics": 29, "anime_manga": 31, "cartoons": 32,
}
TriviaCategory = Literal[
    "general", "books", "film", "music", "tv", "video_games",
    "board_games", "comics", "anime_manga", "cartoons",
]
MAX_TRIVIA = 10
OPENTDB_MIN_INTERVAL_S = 5.0   # one request per 5 s per IP, or response_code 5
OPENTDB_ERRORS = {             # errors arrive inside an HTTP 200, as response_code
    1: "Open Trivia DB doesn't have enough questions for that category/difficulty. Ask for fewer, or drop the difficulty.",
    2: "Open Trivia DB rejected the parameters.",
    5: "Open Trivia DB is rate limiting us. Wait a few seconds before asking for more questions.",
}

_opentdb_lock = threading.Lock()   # tools run in a thread pool: two calls must not both skip the wait
_opentdb_last_call = 0.0

# ── JokeAPI ──────────────────────────────────────────────
JOKEAPI_URL = "https://v2.jokeapi.dev/joke/{category}"
JOKE_BLACKLIST = "nsfw,religious,political,racist,sexist,explicit"
JokeCategory = Literal["Any", "Programming", "Misc", "Pun", "Spooky", "Christmas"]   # "Dark" left out on purpose

# ── TMDb ─────────────────────────────────────────────────
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
# v4 "API Read Access Token". Injected from a k8s Secret (or --env-file locally), never baked
# into the image. Read once at import: env vars are fixed at container start anyway, so a
# rotated Secret needs a rollout restart either way.
TMDB_TOKEN = os.getenv("TMDB_API_TOKEN")
TMDB_GENRES = {   # /genre/movie/list, stable for years; search results only carry genre_ids
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
}

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


def _get_json(source: str, url: str, params: dict, fallback: str,
              headers: dict | None = None) -> tuple[object, dict | None]:
    """GET url and parse the JSON body. Returns (data, None) or (None, error_dict). Never raises.

    The same error contract the first three tools write out by hand. `fallback` is the
    next step for the model, appended to every error ("Answer without anime details.").
    The retries step will add retry/backoff here, once, for every tool that uses it.
    """
    try:
        resp = httpx.get(url, params=params, headers={**HEADERS, **(headers or {})}, timeout=TIMEOUT_S)
        resp.raise_for_status()
    except httpx.TimeoutException:
        return None, {"error": f"{source} timed out. {fallback}"}
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        print(f"[{source}] HTTP {status}: {e.response.text[:200]}")
        if status == 429:
            return None, {"error": f"{source} is rate limiting us. Don't call it again this turn. {fallback}"}
        return None, {"error": f"{source} returned HTTP {status}. {fallback}"}
    except httpx.RequestError as e:
        return None, {"error": f"{source} is unreachable ({type(e).__name__}). {fallback}"}

    try:
        return resp.json(), None
    except ValueError:
        return None, {"error": f"{source} returned an unexpected (non-JSON) response. {fallback}"}


# ── Tool: search_tv_shows ────────────────────────────────
def search_tv_shows(query: str) -> dict:
    """Look up TV shows by title on TVMaze.

    Matching is fuzzy on the show's name, so results can include loosely related titles;
    check each result's name before relying on it. Returns up to 5 shows, each with: name,
    type (Scripted/Animation/Documentary...), genres, status (Running/Ended), premiered and
    ended dates, avg_runtime_min (average episode length), rating (0-10, may be null),
    where_to_watch (streaming service or TV network), and a short summary.

    Use this when the user names a specific show or wants details about one: where to watch
    it, episode length, whether it has finished. Do NOT use it to find shows by genre, mood,
    or similarity (e.g. 'dark fantasy shows', 'shows like Arcane'). It only matches titles.

    Args:
        query: A show title or part of one, e.g. 'Arcane' or 'Breaking Bad'. Not a genre,
            mood, or description.
    """
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


# ── Tool: find_game_deals ────────────────────────────────
def find_game_deals(title: str, max_price: float | None = None) -> dict:
    """Look up current prices and deals for a PC game by title, via CheapShark.

    Covers digital stores (Steam, GOG, Humble, Fanatical...). PC only; no console prices.
    Title matching is loose, so results can include DLC, soundtracks, and similarly named
    games (DLC and soundtracks often have no Steam rating); check each title before
    recommending it. Returns up to 5 games, one entry per game at its cheapest store, each
    with: title, store, sale_price and normal_price (USD), savings_pct, on_sale,
    steam_rating_pct (0-100, null if no reviews), steam_rating_count, and release_year.

    Use this when the user names a game, or asks whether specific games are cheap or on sale.
    Do NOT use it to discover games by genre, co-op, player count, or mood: it has no such
    data. For a request like 'a cheap co-op game', pick specific titles you are confident
    fit, then call this once per title to get real prices.

    Args:
        title: A game title or part of one, e.g. 'Overcooked' or 'Stardew Valley'. Not a
            genre, feature, or description.
        max_price: Only return deals at or below this price in USD, e.g. 10. Omit it unless
            the user gave a budget.
    """

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


# ── Tool: search_books ────────────────────────────────
def search_books(query: str) -> dict:
    """Look up books by title or author on Open Library.

    Results can include summaries, study guides, and omnibus/box-set editions alongside the
    real book; prefer the result whose title and author match and that has many editions (a
    summary or knockoff usually has 1). Returns up to 5 books, each with: title, authors,
    first_published (year), editions (a rough popularity signal), pages (median page count,
    may be null), rating (1-5, null if unrated), rating_count, and up to 5 subject tags.

    Use this when the user names a book or an author, or wants details about one: how long it
    is, when it came out, how it's rated, what it's about. Do NOT use it to find books by
    genre, mood, or similarity (e.g. 'cozy mysteries', 'books like Dune'). It only matches
    titles and author names. It has no prices, availability, or audiobook information.

    Args:
        query: A book title or author name, e.g. 'Project Hail Mary' or 'Andy Weir'. Not a
            genre, mood, or description.
    """

    params = {"q": query, "limit": MAX_RESULTS, "fields": OPENLIBRARY_FIELDS}  # fields= : the server trims for us

    try:
        resp = httpx.get(OPENLIBRARY_SEARCH_URL, params=params, headers=HEADERS, timeout=TIMEOUT_S)
        resp.raise_for_status()
    except httpx.TimeoutException:
        return {"error": "Open Library timed out. Try again or answer without book details."}
    except httpx.HTTPStatusError as e:
        print(f"[openlibrary] HTTP {e.response.status_code}: {e.response.text[:200]}")
        return {"error": f"Open Library returned HTTP {e.response.status_code}. Answer without book details."}
    except httpx.RequestError as e:
        return {"error": f"Open Library is unreachable ({type(e).__name__}). Answer without book details."}

    try:
        data = resp.json()
    except ValueError:
        return {"error": "Open Library returned an unexpected (non-JSON) response."}
    if not isinstance(data, dict):
        print(f"[openlibrary] unexpected payload: {str(data)[:200]}")
        return {"error": "Open Library returned an unexpected response. Answer without book details."}

    results = []
    for doc in data.get("docs", []):
        rating = doc.get("ratings_average")
        subjects = [s for s in doc.get("subject", []) if ":" not in s][:MAX_SUBJECTS]  # drop tags like "nyt:..."
        results.append({
            "title": doc.get("title"),
            "authors": doc.get("author_name", []),
            "first_published": doc.get("first_publish_year"),
            "editions": doc.get("edition_count"),
            "pages": doc.get("number_of_pages_median"),
            "rating": round(rating, 1) if rating is not None else None,
            "rating_count": doc.get("ratings_count", 0),
            "subjects": subjects,
        })

    return {"query": query, "count": len(results), "results": results}


# ── Tool: search_anime ───────────────────────────────────
def search_anime(query: str) -> dict:
    """Look up anime by title on MyAnimeList, via Jikan.

    Matches romaji and English titles, so results can include sequels, movies, and specials
    from the same franchise; use type and year to pick the right one. Returns up to 5 entries,
    each with: title, title_english (may be null), type (TV/Movie/OVA/ONA/Special), episodes
    (null while airing), status, year, episode_length (e.g. '24 min per ep'), score
    (MyAnimeList, 1-10, may be null), scored_by (number of ratings), genres, and a short
    synopsis. There is no streaming availability.

    Use this when the user names a specific anime or wants details about one: episode count
    and length, whether it has finished, how well rated it is. Prefer it over search_tv_shows
    for anime. Do NOT use it to find anime by genre, mood, or similarity (e.g. 'anime like
    Cowboy Bebop'); it only matches titles.

    Args:
        query: An anime title or part of one, e.g. 'Cowboy Bebop' or 'Frieren'. Not a genre,
            mood, or description.
    """
    params = {"q": query, "limit": MAX_RESULTS, "sfw": "true"}
    data, err = _get_json("Jikan", JIKAN_SEARCH_URL, params,
                          "Answer without anime details, or try search_tv_shows.")
    if err:
        return err
    if not isinstance(data, dict):
        print(f"[jikan] unexpected payload: {str(data)[:200]}")
        return {"error": "Jikan returned an unexpected response. Answer without anime details."}

    results = []
    for anime in data.get("data", [])[:MAX_RESULTS]:
        aired_from = (anime.get("aired") or {}).get("from") or ""   # "1998-04-03T00:00:00+00:00"
        results.append({
            "title": anime.get("title"),
            "title_english": anime.get("title_english"),
            "type": anime.get("type"),
            "episodes": anime.get("episodes"),
            "status": anime.get("status"),
            "year": anime.get("year") or _num(aired_from[:4], int),   # `year` is null for most movies
            "episode_length": anime.get("duration"),
            "score": anime.get("score"),
            "scored_by": anime.get("scored_by"),
            "genres": [g.get("name") for g in anime.get("genres", [])],
            "synopsis": (anime.get("synopsis") or "")[:SUMMARY_CHARS],
        })

    return {"query": query, "count": len(results), "results": results}

# ── Tool: search_movies ──────────────────────────────────
def search_movies(query: str, year: int | None = None) -> dict:
    """Look up movies by title on The Movie Database (TMDb).

    Matching is on the title, so results can include remakes, sequels, and unrelated films
    with similar names; use release_year and vote_count to pick the right one. Returns up to
    5 movies, each with: title, original_title, release_year, genres, rating (TMDb users,
    0-10, null if unrated), vote_count (how many ratings; a 9.0 from 3 votes means little),
    original_language, and a short overview. There is no runtime or streaming availability.

    Use this when the user names a specific movie or wants details about one: when it came
    out, how well rated it is, what it's about. Use search_tv_shows for series and
    search_anime for anime. Do NOT use it to find movies by genre, mood, or similarity
    (e.g. 'movies like Inception'); it only matches titles.

    Args:
        query: A movie title or part of one, e.g. 'Dune' or 'Spirited Away'. Not a genre,
            mood, or description.
        year: Release year. Only set it when the user gave one, or to tell a remake from
            the original (e.g. 1984 vs 2021 for 'Dune'). Omit it otherwise.
    """
    if not TMDB_TOKEN:
        return {"error": "TMDb isn't configured on this server (no API token). Answer without movie details."}

    params = {"query": query, "include_adult": "false"}
    if year is not None:
        params["primary_release_year"] = year
    data, err = _get_json("TMDb", TMDB_SEARCH_URL, params, "Answer without movie details.",
                          headers={"Authorization": f"Bearer {TMDB_TOKEN}"})
    if err:
        return err
    if not isinstance(data, dict):
        print(f"[tmdb] unexpected payload: {str(data)[:200]}")
        return {"error": "TMDb returned an unexpected response. Answer without movie details."}

    results = []
    for movie in data.get("results", [])[:MAX_RESULTS]:
        results.append({
            "title": movie.get("title"),
            "original_title": movie.get("original_title"),
            "release_year": _num((movie.get("release_date") or "")[:4], int),   # "" for unreleased
            "genres": [TMDB_GENRES.get(g, f"genre {g}") for g in movie.get("genre_ids", [])],
            "rating": movie.get("vote_average") or None,   # 0.0 means "no votes", not "terrible"
            "vote_count": movie.get("vote_count"),
            "original_language": movie.get("original_language"),
            "overview": (movie.get("overview") or "")[:SUMMARY_CHARS],
        })

    return {"query": query, "count": len(results), "results": results}


# ── Tool: find_free_games ────────────────────────────────
def find_free_games(
    category: FreeGameCategory | None = None,
    platform: Literal["pc", "browser", "all"] = "all",
) -> dict:
    """Find free-to-play games by genre or tag, via FreeToGame.

    Every game here is free to play, so there are no prices (for paid games use
    find_game_deals). Sorted by popularity. Returns up to 5 games, each with: title, genre,
    platform, publisher, release_date, and a one-line description.

    Use this when the user wants a free game, or a kind of game with no budget mentioned
    (e.g. 'a free shooter', 'something free in the browser'). Unlike find_game_deals, it CAN
    filter by genre. There is no co-op tag: 'pve' (players vs the game) is the closest, and
    'mmo' covers online multiplayer.

    Args:
        category: A genre or tag to filter by, e.g. 'shooter', 'card', 'pve'. Omit for the
            most popular free games overall.
        platform: 'pc' (Windows download), 'browser', or 'all'. Defaults to 'all'.
    """
    params = {"platform": platform, "sort-by": "popularity"}
    if category is not None:
        params["category"] = category

    data, err = _get_json("FreeToGame", FREETOGAME_URL, params, "Answer without free-to-play suggestions.")
    if err:
        return err
    # "No results" is NOT an error here: FreeToGame answers HTTP 201 with
    # {"status": 0, "status_message": "No results available at the moment, please try again later."}
    # Passing that message on would make the model retry something that will never succeed.
    if isinstance(data, dict) and data.get("status") == 0:
        return {"category": category, "platform": platform, "count": 0, "results": []}
    if not isinstance(data, list):
        print(f"[freetogame] unexpected payload: {str(data)[:200]}")
        return {"error": "FreeToGame returned an unexpected response. Answer without free-to-play suggestions."}

    results = [{
        "title": game.get("title"),
        "genre": game.get("genre"),
        "platform": game.get("platform"),
        "publisher": game.get("publisher"),
        "release_date": game.get("release_date"),
        "description": game.get("short_description"),
    } for game in data[:MAX_RESULTS]]

    return {"category": category, "platform": platform, "count": len(results), "results": results}


# ── Tool: get_trivia ─────────────────────────────────────
def get_trivia(
    category: TriviaCategory = "general",
    difficulty: Literal["easy", "medium", "hard"] | None = None,
    amount: int = 5,
) -> dict:
    """Get trivia questions from Open Trivia DB, for quizzing the user.

    Returns up to 10 questions, each with: question, type ('multiple' or 'boolean'),
    difficulty, choices (already shuffled), and correct_answer. When quizzing, show the
    question and choices but NOT the correct answer; reveal it only after the user answers.

    Use this when the user wants to be quizzed, or wants trivia about TV, film, games, books,
    anime and so on. The API allows one request every 5 seconds, so get all the questions you
    need in one call rather than one at a time.

    Args:
        category: Topic of the questions. Defaults to 'general'.
        difficulty: 'easy', 'medium', or 'hard'. Omit for a mix.
        amount: Number of questions, 1-10. Defaults to 5.
    """
    global _opentdb_last_call

    category_id = OPENTDB_CATEGORIES.get(category)
    if category_id is None:   # MCP validates the enum, but direct callers don't go through MCP
        return {"error": f"Unknown trivia category '{category}'. Use one of: {', '.join(OPENTDB_CATEGORIES)}."}

    params = {"amount": max(1, min(amount, MAX_TRIVIA)), "category": category_id}
    if difficulty is not None:
        params["difficulty"] = difficulty

    # Respect the 5 s rule ourselves instead of burning a call on response_code 5.
    # Per process only: with 2 tools-server replicas behind one egress IP, both could still
    # collide. A shared limiter (Redis) is the real fix; see "Only if curious".
    with _opentdb_lock:
        wait = OPENTDB_MIN_INTERVAL_S - (time.monotonic() - _opentdb_last_call)
        if wait > 0:
            time.sleep(wait)
        data, err = _get_json("Open Trivia DB", OPENTDB_URL, params, "Make up no questions; say trivia is unavailable.")
        _opentdb_last_call = time.monotonic()
    if err:
        return err
    if not isinstance(data, dict):
        return {"error": "Open Trivia DB returned an unexpected response. Say trivia is unavailable."}

    code = data.get("response_code")
    if code != 0:
        return {"error": OPENTDB_ERRORS.get(code, f"Open Trivia DB returned response code {code}.")}

    questions = []
    for q in data.get("results", []):
        correct = html.unescape(q.get("correct_answer", ""))   # text is HTML-escaped: &quot; &#039;
        choices = [correct] + [html.unescape(a) for a in q.get("incorrect_answers", [])]
        if q.get("type") == "boolean":
            choices = ["True", "False"]
        else:
            random.shuffle(choices)   # OpenTDB lists the correct answer separately; don't let position leak it
        questions.append({
            "question": html.unescape(q.get("question", "")),
            "type": q.get("type"),
            "difficulty": q.get("difficulty"),
            "choices": choices,
            "correct_answer": correct,
        })

    return {"category": category, "count": len(questions), "questions": questions}


# ── Tool: get_joke ───────────────────────────────────────
def get_joke(category: JokeCategory = "Any") -> dict:
    """Get one clean joke from JokeAPI.

    Always family-friendly: the tool enforces safe mode whatever is asked. Returns either
    'joke' (a one-liner) or 'setup' and 'delivery' (a two-part joke: tell the setup, then the
    delivery).

    Use this when the user asks for a joke, or wants something light between recommendations.

    Args:
        category: Kind of joke. Defaults to 'Any'.
    """
    if category not in get_args(JokeCategory):   # it goes into the URL path, so allowlist it
        return {"error": f"Unknown joke category '{category}'. Use one of: {', '.join(get_args(JokeCategory))}."}

    # safe-mode + blacklist are set here, not left to the model: a guardrail in code can't be
    # talked out of by a prompt.
    params = {"safe-mode": "", "blacklistFlags": JOKE_BLACKLIST}
    data, err = _get_json("JokeAPI", JOKEAPI_URL.format(category=category), params, "Skip the joke.")
    if err:
        return err
    if not isinstance(data, dict) or data.get("error"):
        print(f"[jokeapi] error payload: {str(data)[:200]}")
        return {"error": "JokeAPI couldn't provide a joke. Skip the joke."}

    if data.get("type") == "twopart":
        return {"category": data.get("category"), "setup": data.get("setup"), "delivery": data.get("delivery")}
    return {"category": data.get("category"), "joke": data.get("joke")}


if __name__ == "__main__":
    # Smoke tests: call each tool directly (no LLM) and eyeball the trimmed output.
    smoke_tests = [
        ("tv: happy path",       search_tv_shows,  {"query": "Arcane"}),
        ("tv: not found",        search_tv_shows,  {"query": "zzqxblorp"}),
        ("games: dedupe",        find_game_deals,  {"title": "overcooked"}),        # expect each game once, cheapest store
        ("games: price cap",     find_game_deals,  {"title": "stardew", "max_price": 5}),
        ("games: not found",     find_game_deals,  {"title": "zzqxblorp"}),
        ("books: by title",      search_books,     {"query": "Project Hail Mary"}),  # expect knockoffs with editions=1
        ("books: by author",     search_books,     {"query": "Andy Weir"}),
        ("books: not found",     search_books,     {"query": "zzqxblorp"}),
        ("anime: happy path",    search_anime,     {"query": "Cowboy Bebop"}), 
        ("movies: happy path",   search_movies,    {"query": "Dune"}),             # expect 2021 + 1984 + others
        ("movies: with year",    search_movies,    {"query": "Dune", "year": 1984}),
        ("movies: not found",    search_movies,    {"query": "zzqxblorp"}),
        ("free: by category",    find_free_games,  {"category": "card", "platform": "pc"}),
        ("free: no results",     find_free_games,  {"category": "horror", "platform": "browser"}),  # HTTP 201 -> count 0
        ("trivia: tv easy",      get_trivia,       {"category": "tv", "difficulty": "easy", "amount": 2}),
        ("trivia: back-to-back", get_trivia,       {"category": "video_games", "amount": 1}),  # should wait ~5 s, not error
        ("joke: pun",            get_joke,         {"category": "Pun"}),
        ("joke: bad category",   get_joke,         {"category": "Dark"}),           # allowlist rejects it
    ]
    for label, fn, kwargs in smoke_tests:
        print(f"\n=== {label}: {fn.__name__}({kwargs})")
        print(json.dumps(fn(**kwargs), indent=2, ensure_ascii=False))
