import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date

from dotenv import load_dotenv

from . import llm, metrics
from .tools import TOOL_SCHEMAS, TOOLS

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a helpful entertainment concierge. Today's date is {today}. "
    "Use tools for facts. Only state prices, dates, ratings, availability and season/release "
    "information that appear in a tool result. If the tools didn't return it, say you don't "
    "know rather than guessing (no price history, no future seasons from memory)."
)
DEFAULT_MAX_STEPS = 6

@dataclass
class AgentResult:
    answer: str | None              # None if we stopped without a final answer
    stop_reason: str                # "stop", "max_steps", "length", ...
    steps: int                      # number of LLM calls made
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0           # includes hidden thinking tokens
    llm_latency_s: float = 0.0      # summed over all LLM calls
    tool_calls: list[dict] = field(default_factory=list)   # [{"name", "args", "error": bool, "latency_s"}]
    llm_calls: list[dict] = field(default_factory=list)    # one per LLM call: step, latency_s, tokens, finish_reason
    messages: list[dict] = field(default_factory=list)     # full transcript, for debugging/evals

def _run_tool_call(tool_call) -> dict:
    """Run one tool call and return the dictionary to put in the tool message.

    Never raises. Every failure becomes {"error": "..."} so the model can reason about it.
    """
    name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as e:
        return {"error": f"Arguments for '{name}' were not valid JSON. Retry with a JSON object."}
    if not isinstance(args, dict):
        return {"error": f"Arguments for '{name}' must be a JSON object, got {type(args).__name__}."}

    try:
        fn = TOOLS[name]
    except KeyError:
        return {"error": f"Unknown tool: {name}. Available tools: {sorted(TOOLS)}"}
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as e:
        return {"error": f"Bad arguments for '{name}': {e}. Check the parameter names in the tool schema."}
    try:
        return fn(**args)
    except Exception as e:
        log.error("[tool-bug] %s(%s) raised %r", name, args, e)
        return {"error": f"Tool '{name}' failed internally ({type(e).__name__}). Try another approach."}


def run_agent(client: llm.LLMClient, user_message: str, max_steps: int = DEFAULT_MAX_STEPS) -> AgentResult:
    """ReAct loop: call the LLM, run any requested tools, feed results back, repeat."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=date.today().isoformat())},
        {"role": "user", "content": user_message},
    ]
    result = AgentResult(answer=None, stop_reason="max_steps", steps=0, messages=messages)

    for step in range(max_steps):
        chat = client.chat(messages=messages, tools=TOOL_SCHEMAS)
        result.llm_latency_s += chat.latency_s
        usage = chat.response.usage
        result.steps += 1
        choice = chat.response.choices[0]
        msg = choice.message
        
        call = {
            "step": result.steps,
            "latency_s": chat.latency_s,
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
            "finish_reason": choice.finish_reason,
        }
        result.llm_calls.append(call)
        result.prompt_tokens += call["prompt_tokens"]
        result.completion_tokens += call["completion_tokens"]
        result.total_tokens += call["total_tokens"]
        log.info("[llm] step %d %.2fs in=%d out=%d total=%d finish=%s", call["step"], call["latency_s"],
                 call["prompt_tokens"], call["completion_tokens"], call["total_tokens"], call["finish_reason"])
        
        if msg is None:
            result.stop_reason = choice.finish_reason or "no_message"
            return result   # no message returned, stop the loop
            
        result.messages.append(msg.model_dump(exclude_none=True))  # exclude_nones to avoid cluttering with null tool_calls
        
        if msg.tool_calls:
            for tool_call in msg.tool_calls:
                start = time.perf_counter()
                tool_result = _run_tool_call(tool_call)
                tool_latency = time.perf_counter() - start
                content = json.dumps(tool_result, ensure_ascii=False)
                log.info("[tool] %s(%s) %.2fs -> %s", tool_call.function.name,
                         tool_call.function.arguments, tool_latency, content[:200])
                result.tool_calls.append({
                    "name": tool_call.function.name,
                    "args": tool_call.function.arguments,
                    "error": "error" in tool_result,
                    "latency_s": tool_latency,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": content
                })
        else:
            if msg.content:
                result.answer = msg.content
                result.stop_reason = choice.finish_reason or "stop"
            else:
                # Turn ended with no text and no tool calls: not a success.
                # Keep a meaningful finish_reason (e.g. "length" = thinking used the budget).
                result.stop_reason = (
                    choice.finish_reason if choice.finish_reason not in (None, "stop")
                    else "empty_response"
                )
            return result
            
    return result   # only reached if we ran out of steps


if __name__ == "__main__":
    # Run from apps/agent-loop:  python -m agent.loop
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines duplicate our [tool] trace
    load_dotenv()
    client = llm.LLMClient()
    # (label, question, max_steps): one per exit path of the loop
    smoke_tests = [
        # ("happy path",       "Is Arcane finished, and where can I watch it?", DEFAULT_MAX_STEPS),
        # ("step cap",         "Is Arcane finished?", 1),
        # ("parallel tools",   "Are Arcane and Severance both finished, and where can I watch each?", DEFAULT_MAX_STEPS),
        # ("not found",        "Where can I watch The Zorblax Saga?", DEFAULT_MAX_STEPS),
        # ("content filter",   "Where can I watch The Glorbnax Chronicles?", DEFAULT_MAX_STEPS),
        # ("co-op games",      "Find me a cheap co-op game to play tonight.", DEFAULT_MAX_STEPS),
        ("three tools",      "I've got a free evening: find me a cheap co-op game, a show to watch after, and a short sci-fi book for the weekend.", DEFAULT_MAX_STEPS),
    ]

    for label, question, max_steps in smoke_tests:
        r = run_agent(client, question, max_steps=max_steps)
        print(f"\n=== {label}: {question}")
        print(f"Answer: {r.answer}")
        print(f"stop={r.stop_reason}")
        print(f"tools: {[(t['name'], t['args'], 'ERR' if t['error'] else 'ok') for t in r.tool_calls]}")
        print(metrics.format_report(metrics.summarize(r, client.model), per_step=True))