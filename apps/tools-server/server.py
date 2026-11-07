"""tools-server: the watch_party tools exposed over MCP (streamable HTTP).

POST /mcp     -> MCP JSON-RPC (tools/list, tools/call)
GET  /healthz -> liveness/readiness; never calls an external API
"""
import logging

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from tools import (
    find_free_games,
    find_game_deals,
    get_joke,
    get_trivia,
    search_anime,
    search_books,
    search_tv_shows,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer(
    "watch-party-tools",
    instructions=(
        "Entertainment lookups: TV shows (TVMaze), anime (Jikan), PC game deals (CheapShark), "
        "free-to-play games (FreeToGame), books (Open Library), trivia (Open Trivia DB), jokes (JokeAPI)."
    ),
)

# The registry. Adding a tool = write the function in tools.py + add it here.
for fn in (
    search_tv_shows, search_anime,
    find_game_deals, find_free_games,
    search_books,
    get_trivia, get_joke,
):
    mcp.tool()(fn)
    
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

# stateless_http: no MCP session kept in pod memory, so any replica can serve any call.
# json_response: plain JSON replies instead of an SSE stream (our tools dont stream).
# host="0.0.0.0": the default 127.0.0.1 turns on DNS-rebinding protection, which
# rejects Host: tool-server with a 421. NetworkPolicy (Phase 6) is the real control.
app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)