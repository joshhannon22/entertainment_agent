"""HTTP front door for the watch_party ADK agent.

POST /chat  -> run one turn through the ADK Runner, return the answer + metrics
GET  /healthz -> liveness: is this process up? (never calls Gemini)
"""
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import socket
import time
import uuid

from fastapi import FastAPI, HTTPException
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig, ToolThreadPoolConfig
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from watch_party.agent import MODEL, ROUTER_NAME, root_agent, tools_server

APP_NAME = "watch_party"
HOSTNAME = socket.gethostname()  # in Kubernetes this is the pod name
CONTROL_TOOLS = {"exit_loop"}    # workflow plumbing, not a data lookup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agent_api")


# ── Per-turn metrics ─────────────────────────────────────
# Specialists run inside AgentTool's own nested Runner, whose events are NOT forwarded to
# ours, so counting events would miss every specialist LLM call and tool call (and make
# multi-agent look cheaper than it is). Plugins ARE inherited by the nested Runner, so the
# counting happens in callbacks. The ContextVar ties callbacks to the /chat request that
# caused them: AgentTool and MCP tools run as asyncio tasks, which copy the context.
@dataclass
class TurnStats:
    llm_calls: Counter = field(default_factory=Counter)   # agent name -> LLM calls
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)   # leaf tools (MCP), incl. inside specialists
    agents_called: list[str] = field(default_factory=list)


_turn: ContextVar[TurnStats | None] = ContextVar("turn_stats", default=None)


class TurnMetricsPlugin(BasePlugin):
    def __init__(self):
        super().__init__(name="turn_metrics")

    async def after_model_callback(self, *, callback_context, llm_response):
        stats, usage = _turn.get(), llm_response.usage_metadata
        if stats is not None and usage is not None:
            stats.llm_calls[callback_context.agent_name] += 1
            stats.prompt_tokens += usage.prompt_token_count or 0
            stats.output_tokens += usage.candidates_token_count or 0
            stats.thinking_tokens += usage.thoughts_token_count or 0
        return None   # observe only; never replace the response

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        stats = _turn.get()
        if stats is not None and tool.name not in CONTROL_TOOLS:
            (stats.agents_called if isinstance(tool, AgentTool) else stats.tool_calls).append(tool.name)
        return None


# Built once per process. InMemorySessionService means sessions live in THIS
# process's memory only: a second replica can't see them (Phase 6 fixes that).
session_service = InMemorySessionService()


def build_runner(agent) -> Runner:
    return Runner(app_name=APP_NAME, agent=agent, session_service=session_service,
                  plugins=[TurnMetricsPlugin()])


runner = build_runner(root_agent)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await runner.close()  # closes the MCP toolsets' HTTP clients cleanly on shutdown


app = FastAPI(title="watch-party agent-api", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None  # omit to start a new conversation
    user_id: str = "local"


class ChatResponse(BaseModel):
    answer: str | None
    # Why the turn ended: "stop" (answered), "max_llm_calls" (hit ADK_MAX_LLM_CALLS), or
    # "empty_response". Facts only; the caller decides what to tell the user (Phase 1 rule).
    stop_reason: str
    session_id: str
    served_by: str
    model: str   # so evals price the tokens with the model that actually ran, not a guess
    tool_calls: list[str]        # MCP tools, wherever they ran (router or a specialist)
    agents_called: list[str]     # specialists the router delegated to
    revisions: int               # drafts rewritten after the critic rejected one (0 or 1)
    llm_calls: int
    llm_calls_by_agent: dict[str, int]
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    latency_s: float


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    # ADK swallows toolset load errors and runs the agent with zero tools, so the model
    # answers from memory. Check tools-server ourselves and fail loudly instead.
    try:
        await tools_server.get_tools()
    except ConnectionError as e:
        log.error("tools-server unreachable: %r", e)
        raise HTTPException(status_code=503, detail="tools-server unavailable")
    if req.session_id is None:
        session = await session_service.create_session(
            app_name=APP_NAME, user_id=req.user_id, session_id=str(uuid.uuid4())
        )
    else:
        session = await session_service.get_session(
            app_name=APP_NAME, user_id=req.user_id, session_id=req.session_id
        )
        if session is None:
            raise HTTPException(
                status_code=404,
                detail=f"session {req.session_id} not found on {HOSTNAME}",
            )

    run_config = RunConfig(tool_thread_pool_config=ToolThreadPoolConfig())
    new_message = types.Content(role="user", parts=[types.Part(text=req.message)])

    stats = TurnStats()
    token = _turn.set(stats)
    answer = None
    stop_reason = None
    drafts = 0
    start = time.perf_counter()
    try:
        async for event in runner.run_async(
            user_id=req.user_id,
            session_id=session.id,
            new_message=new_message,
            run_config=run_config,
        ):
            # The user sees the router's latest draft. The critic speaks last in the loop,
            # but its text is a review for the router, not an answer for the user.
            if event.author == ROUTER_NAME and event.is_final_response() and event.content and event.content.parts:
                text = "".join(p.text for p in event.content.parts if p.text and not p.thought)
                if text:
                    answer, drafts = text, drafts + 1
    except LlmCallsLimitExceededError:
        # The step cap is a guardrail doing its job (the model kept calling tools), not a
        # server fault. Return what we know with 200 instead of letting it escape as a 500.
        log.warning("session=%s hit max_llm_calls after tools=%s", session.id, stats.tool_calls)
        stop_reason = "max_llm_calls"
    except genai_errors.APIError as e:
        log.error("LLM call failed: %r", e)
        raise HTTPException(status_code=502, detail=f"LLM provider error ({e.code})")
    finally:
        _turn.reset(token)
    latency_s = round(time.perf_counter() - start, 2)
    if stop_reason is None:
        stop_reason = "stop" if answer else "empty_response"

    log.info(
        "chat session=%s host=%s stop=%s agents=%s tools=%s llm_calls=%s revisions=%d tokens=%d/%d/%d latency=%.2fs",
        session.id, HOSTNAME, stop_reason, stats.agents_called, stats.tool_calls, dict(stats.llm_calls),
        max(0, drafts - 1), stats.prompt_tokens, stats.output_tokens, stats.thinking_tokens, latency_s,
    )
    return ChatResponse(
        answer=answer,
        stop_reason=stop_reason,
        session_id=session.id,
        served_by=HOSTNAME,
        model=MODEL,
        tool_calls=stats.tool_calls,
        agents_called=stats.agents_called,
        revisions=max(0, drafts - 1),
        llm_calls=sum(stats.llm_calls.values()),
        llm_calls_by_agent=dict(stats.llm_calls),
        prompt_tokens=stats.prompt_tokens,
        output_tokens=stats.output_tokens,
        thinking_tokens=stats.thinking_tokens,
        latency_s=latency_s,
    )
