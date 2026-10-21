import inspect
import json
from dataclasses import dataclass, field

from dotenv import load_dotenv

from tools import TOOL_SCHEMAS, TOOLS
import llm

SYSTEM_PROMPT = "You are a helpful entertainment concierge. Use tools for facts; don't guess."
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
    tool_calls: list[dict] = field(default_factory=list)   # [{"name":..., "args":..., "error": bool}]
    messages: list[dict] = field(default_factory=list)     # full transcript, for debugging/evals

def _run_tool_call(tool_call) -> dict:
    """Run one tool call and return the JSON string to put in the tool message.

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
        print(f"[tool-bug] {name}({args}) raised {e!r}")
        return {"error": f"Tool '{name}' failed internally ({type(e).__name__}). Try another approach."}


def run_agent(client: llm.LLMClient, user_message: str, max_steps: int = DEFAULT_MAX_STEPS) -> AgentResult:
    """ReAct loop: call the LLM, run any requested tools, feed results back, repeat."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
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
        
        if usage:
            result.prompt_tokens += usage.prompt_tokens
            result.completion_tokens += usage.completion_tokens
            result.total_tokens += usage.total_tokens
        
        if msg is None:
            result.stop_reason = choice.finish_reason or "no_message"
            return result   # no message returned, stop the loop
            
        result.messages.append(msg.model_dump(exclude_none=True))  # exclude_nones to avoid cluttering with null tool_calls
        
        if msg.tool_calls:
            for tool_call in msg.tool_calls:
                tool_result = _run_tool_call(tool_call)
                result.tool_calls.append({
                    "name": tool_call.function.name,
                    "args": tool_call.function.arguments,
                    "error": isinstance(tool_result, dict) and "error" in tool_result
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False)
                })
        else:
            result.answer = msg.content
            result.stop_reason = choice.finish_reason or "stop"
            return result   # we have an answer, stop the loop
            
    return result   # only reached if we ran out of steps


if __name__ == "__main__":
    load_dotenv()
    client = llm.LLMClient()
    # (label, question, max_steps): one per exit path of the loop
    smoke_tests = [
        ("happy path",       "Is Arcane finished, and where can I watch it?", DEFAULT_MAX_STEPS),
        ("step cap",         "Is Arcane finished?", 1),
        ("parallel tools",   "Are Arcane and Severance both finished, and where can I watch each?", DEFAULT_MAX_STEPS),
        ("not found",        "Where can I watch The Zorblax Saga?", DEFAULT_MAX_STEPS),
        ("content filter",   "Where can I watch The Glorbnax Chronicles?", DEFAULT_MAX_STEPS),
    ]

    for label, question, max_steps in smoke_tests:
        r = run_agent(client, question, max_steps=max_steps)
        hidden = r.total_tokens - r.prompt_tokens - r.completion_tokens
        print(f"\n=== {label}: {question}")
        print(f"Answer: {r.answer}")
        print(f"stop={r.stop_reason}  steps={r.steps}  latency={r.llm_latency_s:.2f}s")
        print(f"tokens: prompt={r.prompt_tokens} completion={r.completion_tokens} "
              f"hidden_thinking={hidden} total={r.total_tokens}")
        print(f"tools: {[(t['name'], t['args'], 'ERR' if t['error'] else 'ok') for t in r.tool_calls]}")