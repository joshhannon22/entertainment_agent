"""watch_party: a router that delegates to domain specialists, checked by a critic.

    root_agent = LoopAgent "watch_party" (max 2 passes)
      ├─ router  (LlmAgent)  -> AgentTool(tv_agent | games_agent | books_agent) + trivia/joke tools
      │                          writes its answer to temp:draft
      └─ critic  (LlmAgent)  -> exit_loop if the draft meets the user's constraints,
                                 else writes temp:critique and the router revises once

Each specialist sees only its own MCP tools (tool_filter), so its prompt carries 1-3 tool
schemas instead of all 8, and the router never sees raw tool JSON, only the specialists'
summaries. That's the context-size bet. The price is extra LLM calls (router + critic +
one or more specialists), which evals/run_api_evals.py measures against the single agent.
"""
import os
from datetime import date

from google.adk.agents import Agent, LoopAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import exit_loop
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

# Local default; in Kubernetes the ConfigMap sets the cluster DNS name.
TOOLS_SERVER_URL = os.getenv("TOOLS_SERVER_URL", "http://localhost:8001/mcp")
MCP_TIMEOUT_S = 30.0  # must exceed tools-server's per-tool-call deadline (TOOL_DEADLINE_S = 20)
MODEL = os.getenv("MODEL", "gemini-3.8-flash")

# Gemini 3 thinks at its default level unless told otherwise, and thinking tokens bill at the
# output rate. "low" matched or beat default on every eval metric (docs/decisions.md).
# Like reasoning_effort, it's a hint, not a cap. minimal / low / medium / high, or "default"
# to send no thinking config (Gemini's own default), for before/after comparisons.
THINKING_LEVEL = os.getenv("THINKING_LEVEL", "low").upper()
GENERATE_CONFIG = None if THINKING_LEVEL == "DEFAULT" else types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(THINKING_LEVEL))
)

ROUTER_NAME = "router"   # main.py takes the user-facing answer from this agent's final text

# Which MCP tools each agent may see. Adding a tool to tools-server now also means adding
# its name here, or no agent gets it: the price of least-privilege per specialist.
TOOLS_BY_AGENT = {
    "tv_agent": ["search_tv_shows", "search_movies", "search_anime"],
    "games_agent": ["find_game_deals", "find_free_games"],
    "books_agent": ["search_books"],
    ROUTER_NAME: ["get_trivia", "get_joke"],
}


def _mcp(tool_filter: list[str] | None = None) -> McpToolset:
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=TOOLS_SERVER_URL, timeout=MCP_TIMEOUT_S),
        tool_filter=tool_filter,
    )


# Unfiltered, used only by /chat's preflight: ADK silently drops a toolset it can't load,
# so main.py checks tools-server itself and returns 503 instead.
tools_server = _mcp()

GROUNDING = (
    "Only state prices, dates, ratings, availability and season/release information that "
    "appear in a tool result. If the tools didn't return it, say you don't know rather than "
    "guessing (no price history, no future seasons from memory). Don't add plot summaries, "
    "genres or 'vibe' from your own knowledge. If a tool fails or a source is unavailable, "
    "say so; never fill in prices, ratings or dates from memory."
)


def _today() -> str:
    return f"Today's date is {date.today().isoformat()}. "


# ── Specialists ──────────────────────────────────────────
SPECIALISTS = {
    "tv_agent": (
        "Looks up specific TV shows, movies and anime by title: status, seasons/episodes, "
        "episode length, ratings, where to watch. Give it the exact titles to look up.",
        "TV, movies and anime",
    ),
    "games_agent": (
        "Looks up current PC game prices and deals by title, and finds free-to-play games by "
        "genre. Give it the exact titles to price, or the kind of free game wanted.",
        "PC games",
    ),
    "books_agent": (
        "Looks up books by title or author: page count, first published, ratings, subjects. "
        "Give it the exact titles or authors to look up.",
        "books",
    ),
}


def _specialist_instruction(domain: str):
    def instruction(ctx: ReadonlyContext) -> str:
        return (
            f"You are the {domain} specialist. You work for a coordinator, not the user: no "
            "greetings, no recommendations of your own. " + _today() +
            "Look up every title in the request with your tools; call tools in parallel when "
            "there are several. Then reply with a compact factual summary per title: the fields "
            "the request needs (e.g. status, years, episode length, where to watch, price, "
            "store, rating and its count, pages). Say plainly when a title wasn't found or a "
            "result looks like a different item (DLC, soundtrack, a similarly named title). "
            + GROUNDING
        )
    return instruction


def _specialist(name: str, model, toolset) -> Agent:
    description, domain = SPECIALISTS[name]
    return Agent(
        name=name,
        model=model,
        description=description,   # AgentTool uses this as the tool description the router sees
        instruction=_specialist_instruction(domain),
        generate_content_config=GENERATE_CONFIG,
        tools=[toolset(TOOLS_BY_AGENT[name])],
    )


# ── Router ───────────────────────────────────────────────
def router_instruction(ctx: ReadonlyContext) -> str:
    critique = ctx.state.get("temp:critique")
    revise = (
        "\n\nA reviewer rejected your previous draft for this request:\n" + critique +
        "\nRevise the answer to fix exactly these points. Call a specialist again only if a fix "
        "needs data you don't have yet."
    ) if critique else ""
    return (
        "You are a helpful entertainment concierge. You coordinate specialists: tv_agent (TV, "
        "movies, anime), games_agent (PC games), books_agent (books). Trivia and jokes you "
        "handle yourself with your own tools. " + _today() +
        "The specialists' tools search by title only, so when the user is vague ('a cheap co-op "
        "game', 'a short sci-fi book'), pick specific titles you are confident fit and ask the "
        "specialist to look those up. Call each specialist at most once per turn: put every "
        "title and every constraint (time budget, price, players, platform) in that one "
        "request. When the question spans domains, call those specialists in the same step. "
        "Answer small talk directly without any specialist. "
        "Write the final answer for the user using only what the specialists returned. " + GROUNDING +
        " The one exception: one short phrase on why you picked a title (e.g. 'a well-known "
        "two-player co-op game')." + revise
    )


# ── Critic ───────────────────────────────────────────────
def critic_instruction(ctx: ReadonlyContext) -> str:
    draft = ctx.state.get("temp:draft") or "(no draft)"
    return (
        "You review a draft answer to the user's latest message before it is sent. Check only:\n"
        "1. Every explicit constraint in the user's message is respected: time budget (add up "
        "runtimes), price or budget, number of players or co-op, platform, things to avoid, how "
        "many items they asked for.\n"
        "2. Every part of the request is answered (e.g. they asked for a show AND a game).\n"
        "3. Nothing recommended is something the draft itself says wasn't found or is unavailable.\n"
        "Don't judge style or length. Don't demand data the tools can't provide: saying a source "
        "is unavailable or a title wasn't found is acceptable. Small talk with no constraints passes.\n"
        "If the draft passes, call exit_loop and say nothing else. If it fails, reply with 1-3 "
        "short bullets saying exactly what to fix, and don't call exit_loop.\n\n"
        f"Draft:\n{draft}"
    )


def build_root_agent(model=MODEL, toolset=_mcp) -> LoopAgent:
    """`model`: a model name or a BaseLlm. `toolset(names)`: the tools one agent may use.
    Tests pass a scripted fake model and local fake tools, so no Gemini or tools-server."""
    router = Agent(
        name=ROUTER_NAME,
        model=model,
        description="Entertainment concierge: answers using specialist agents.",
        instruction=router_instruction,
        generate_content_config=GENERATE_CONFIG,
        tools=[AgentTool(_specialist(n, model, toolset)) for n in SPECIALISTS] + [toolset(TOOLS_BY_AGENT[ROUTER_NAME])],
        output_key="temp:draft",   # temp: = this invocation only, so last turn's draft never leaks in
    )
    critic = Agent(
        name="critic",
        model=model,
        description="Checks the router's draft against the user's constraints.",
        instruction=critic_instruction,
        generate_content_config=GENERATE_CONFIG,
        tools=[exit_loop],
        output_key="temp:critique",
    )
    # A workflow agent, not an LLM decision: the critic always runs, and at most one revision.
    return LoopAgent(name="watch_party", sub_agents=[router, critic], max_iterations=2)


root_agent = build_root_agent()
