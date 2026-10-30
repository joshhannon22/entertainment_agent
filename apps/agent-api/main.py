"""HTTP front door for the watch_party ADK agent.

POST /chat  -> run one turn through the ADK Runner, return the answer + metrics
GET  /healthz -> liveness: is this process up? (never calls Gemini)
"""
import logging
import socket
import time
import uuid

from fastapi import FastAPI, HTTPException
from google.adk.agents.run_config import RunConfig, ToolThreadPoolConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from watch_party.agent import root_agent

APP_NAME = "watch_party"
HOSTNAME = socket.gethostname()  # in Kubernetes this is the pod name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agent_api")

# Built once per process. InMemorySessionService means sessions live in THIS
# process's memory only: a second replica can't see them (Phase 6 fixes that).
session_service = InMemorySessionService()
runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

app = FastAPI(title="watch-party agent-api")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None  # omit to start a new conversation
    user_id: str = "local"


class ChatResponse(BaseModel):
    answer: str | None
    session_id: str
    served_by: str
    tool_calls: list[str]
    llm_calls: int
    prompt_tokens: int
    output_tokens: int
    thinking_tokens: int
    latency_s: float


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
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

    answer = None
    tool_calls: list[str] = []
    llm_calls = prompt_tokens = output_tokens = thinking_tokens = 0
    start = time.perf_counter()
    try:
        async for event in runner.run_async(
            user_id=req.user_id,
            session_id=session.id,
            new_message=new_message,
            run_config=run_config,
        ):
            if event.usage_metadata:
                llm_calls += 1
                prompt_tokens += event.usage_metadata.prompt_token_count or 0
                output_tokens += event.usage_metadata.candidates_token_count or 0
                thinking_tokens += event.usage_metadata.thoughts_token_count or 0
            tool_calls += [fc.name for fc in event.get_function_calls()]
            if event.is_final_response() and event.content and event.content.parts:
                answer = "".join(p.text for p in event.content.parts if p.text) or None
    except genai_errors.APIError as e:
        log.error("LLM call failed: %r", e)
        raise HTTPException(status_code=502, detail=f"LLM provider error ({e.code})")
    latency_s = round(time.perf_counter() - start, 2)

    log.info(
        "chat session=%s host=%s tools=%s llm_calls=%d tokens=%d/%d/%d latency=%.2fs",
        session.id, HOSTNAME, tool_calls, llm_calls,
        prompt_tokens, output_tokens, thinking_tokens, latency_s,
    )
    return ChatResponse(
        answer=answer,
        session_id=session.id,
        served_by=HOSTNAME,
        tool_calls=tool_calls,
        llm_calls=llm_calls,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        latency_s=latency_s,
    )
