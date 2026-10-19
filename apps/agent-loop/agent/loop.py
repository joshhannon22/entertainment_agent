from dotenv import load_dotenv
import json

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
    # parse arguments to a dict
    args = json.loads(msg.tool_calls[0].function.arguments)
    # create new message with function return and append to messages
    messages.append({"role": "tool", "tool_call_id": msg.tool_calls[0].id, "content": json.dumps(TOOLS[msg.tool_calls[0].function.name](**args),ensure_ascii=False)})
    # Call client chat again to print with new updates
    response = client.chat(messages=messages, tools=TOOL_SCHEMAS)
    print(response.response.choices[0].model_dump_json(indent=2))