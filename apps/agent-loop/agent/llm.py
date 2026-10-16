import os
from dotenv import load_dotenv
import time
from dataclasses import dataclass

from openai import OpenAI
from openai.types.chat import ChatCompletion


@dataclass
class ChatResult:
    response: ChatCompletion   # full object: the loop needs .choices, metrics needs .usage
    latency_s: float           # wall-clock time of the API call only


class LLMClient:
    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 reasoning_effort: str | None = None):
        self.model    = model or os.getenv("MODEL")
        self.base_url = base_url or os.getenv("BASE_URL")
        self.api_key  = api_key or os.getenv("GEMINI_API_KEY")
        # Optional: not every provider/model supports it (e.g. Ollama), so None means "don't send it"
        self.reasoning_effort = reasoning_effort or os.getenv("REASONING_EFFORT")
        
        for k, v in {"MODEL": self.model, "BASE_URL": self.base_url, "GEMINI_API_KEY": self.api_key}.items():
            if not v:
                raise ValueError(f"Missing required environment variable: {k}")
        
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort

        start = time.perf_counter()
        response = self._client.chat.completions.create(**kwargs)
        end = time.perf_counter()

        return ChatResult(
            response=response,
            latency_s=end - start
        )

if __name__ == "__main__":
    load_dotenv()
    client = LLMClient()
    response = client.chat(messages=[
        {   "role": "system",
            "content": "You are a helpful assistant."
        },
        {
            "role": "user",
            "content": "Say hello in five words"
        }
    ])

    print(response.response.choices[0].message.content)
    print(response.response.usage)
    print(f"Latency: {response.latency_s:.2f} s")
