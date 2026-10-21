import json

from dotenv import load_dotenv

from tools import TOOL_SCHEMAS, TOOLS
import llm

if __name__ == "__main__":
    load_dotenv()
    client = llm.LLMClient()
    messages = [
        {   "role": "system",
            "content": "You are a helpful assistant."
        },
        {
            "role": "user",
            "content": "Is Arcane finished, and where can I watch it?"
        }
    ]
    response = client.chat(messages=messages, tools=TOOL_SCHEMAS)
    # append tool call message to the messages lists
    msg = response.response.choices[0].message
    messages.append(msg.model_dump(exclude_none=True))
    tool_call = msg.tool_calls[0]
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)   # arguments is a JSON *string*
    
    fn = TOOLS[name]          # KeyError here = model asked for a tool we don't have
    result = fn(**args)       # TypeError here = model sent bad/missing args
    print(f"[tool] {name}({args}) -> {json.dumps(result, ensure_ascii=False)[:200]}")

    messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": json.dumps(result, ensure_ascii=False),
    })
    # Call client chat again to print with new updates
    response = client.chat(messages=messages, tools=TOOL_SCHEMAS)
    print(response.response.choices[0].model_dump_json(indent=2))