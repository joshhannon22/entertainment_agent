import os
from datetime import date

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext

from .tools import find_game_deals, search_books, search_tv_shows


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
    tools=[search_tv_shows, find_game_deals, search_books],
)
