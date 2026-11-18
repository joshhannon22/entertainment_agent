"""Offline test of the router -> specialists -> critic loop and /chat's metrics.

python tests/test_multi_agent.py      (from apps/agent-api; no Gemini, no tools-server)

A scripted fake model plays every agent (it tells them apart by their system instruction)
and fake tools stand in for MCP. What this proves, that a live run can't show cheaply:
  - specialist LLM calls and tool calls inside AgentTool's nested Runner are counted
  - the user gets the router's revised draft, not the critic's review
  - exit_loop ends the loop and isn't reported as a data tool
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # noqa: E402
from watch_party.agent import build_root_agent  # noqa: E402

USAGE = types.GenerateContentResponseUsageMetadata(prompt_token_count=100, candidates_token_count=10)


def _call(name, **args):
    return types.Part(function_call=types.FunctionCall(id=f"call-{name}", name=name, args=args))


class ScriptedLlm(BaseLlm):
    critic_rejects_first: bool = True
    critic_calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        system = str(llm_request.config.system_instruction or "")
        last = llm_request.contents[-1].parts if llm_request.contents else []
        has_results = any(p.function_response for p in last)

        if "You coordinate specialists" in system:            # router
            if "A reviewer rejected" in system:
                parts = [types.Part(text="DRAFT2: Arcane on Netflix; Overcooked $4.99")]
            elif has_results:
                parts = [types.Part(text="DRAFT1: Arcane on Netflix")]
            else:   # two domains -> two specialists in one step
                parts = [_call("tv_agent", request="Arcane"), _call("games_agent", request="Overcooked")]
        elif "TV, movies and anime specialist" in system:
            parts = [types.Part(text="Arcane: Ended, Netflix")] if has_results else [_call("search_tv_shows", query="Arcane")]
        elif "PC games specialist" in system:
            parts = [types.Part(text="Overcooked: $4.99")] if has_results else [_call("find_game_deals", title="Overcooked")]
        elif "You review a draft" in system:                   # critic
            self.critic_calls += 1
            if self.critic_rejects_first and self.critic_calls == 1:
                parts = [types.Part(text="- The user asked for a game too; include its price.")]
            else:
                parts = [_call("exit_loop")]
        else:
            raise AssertionError(f"unknown agent: {system[:80]}")
        yield LlmResponse(content=types.Content(role="model", parts=parts), usage_metadata=USAGE)


async def search_tv_shows(query: str) -> dict:
    return {"results": [{"name": query, "status": "Ended", "where_to_watch": "Netflix"}]}


async def find_game_deals(title: str) -> dict:
    return {"deals": [{"title": title, "sale_price": 4.99}]}


async def get_trivia() -> dict:
    return {}


async def get_joke() -> dict:
    return {}


class FakeToolset(BaseToolset):
    def __init__(self, names):
        super().__init__()
        self.names = names

    async def get_tools(self, readonly_context=None):
        fns = [search_tv_shows, find_game_deals, get_trivia, get_joke]
        return [FunctionTool(f) for f in fns if f.__name__ in self.names]


def chat(llm: ScriptedLlm) -> dict:
    async def tools_ok():
        return []
    main.tools_server.get_tools = tools_ok
    main.runner = main.build_runner(build_root_agent(model=llm, toolset=FakeToolset))
    resp = TestClient(main.app).post("/chat", json={"message": "a show and a cheap game for tonight"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_critic_rejects_then_router_revises():
    data = chat(ScriptedLlm(model="fake"))
    assert data["answer"].startswith("DRAFT2"), data["answer"]
    assert data["revisions"] == 1
    assert sorted(data["agents_called"]) == ["games_agent", "tv_agent"]
    assert sorted(data["tool_calls"]) == ["find_game_deals", "search_tv_shows"]   # nested calls counted, no exit_loop
    # router: plan + draft1 + draft2; critic: reject + approve; each specialist: tool call + summary
    assert data["llm_calls_by_agent"] == {"router": 3, "critic": 2, "tv_agent": 2, "games_agent": 2}, data["llm_calls_by_agent"]
    assert data["llm_calls"] == 9 and data["prompt_tokens"] == 900
    assert data["stop_reason"] == "stop"


def test_critic_approves_first_draft():
    data = chat(ScriptedLlm(model="fake", critic_rejects_first=False))
    assert data["answer"].startswith("DRAFT1") and data["revisions"] == 0
    assert data["llm_calls_by_agent"]["critic"] == 1 and data["llm_calls_by_agent"]["router"] == 2


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
