import os
from datetime import date

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

# Local default; in Kubernetes the ConfigMap sets the cluster DNS name.
TOOLS_SERVER_URL = os.getenv("TOOLS_SERVER_URL", "http://localhost:8001/mcp")
MCP_TIMEOUT_S = 30.0  # must exceed tools-server's own per-API timeout (TIMEOUT_S = 10)

tools_server = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=TOOLS_SERVER_URL,
        timeout=MCP_TIMEOUT_S,
    )
)

def build_instruction(ctx: ReadonlyContext) -> str:
    """System prompt, rebuilt on every turn so today's date never goes stale."""
    return (
        "You are a helpful entertainment concierge. "
        f"Today's date is {date.today().isoformat()}. "
        "Use tools for facts. Only state prices, dates, ratings, availability and season/release "
        "information that appear in a tool result. If the tools didn't return it, say you don't "
        "know rather than guessing (no price history, no future seasons from memory). "
        "Describe each item only with details from the tool results (e.g. the TVMaze summary, "
        "Open Library subjects, Steam ratings). Don't add plot summaries, genres or 'vibe' from "
        "your own knowledge. The one exception: one short phrase on why you picked a title "
        "(e.g. 'a well-known two-player co-op game'), since the tools can't search by genre."
    )


root_agent = Agent(
    name="watch_party",
    model=os.getenv("MODEL", "gemini-3.8-flash"),
    description="Entertainment concierge: TV shows, PC game deals, and books.",
    instruction=build_instruction,
    tools=[tools_server],
)
