"""Tiny terminal client for agent-api: renders answers as Markdown, keeps the session.

Usage: python scripts/chat.py [--url http://localhost:8000]
Commands: /new = start a new conversation, /quit = exit
"""
import argparse

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()

    session_id = None
    with httpx.Client(base_url=args.url, timeout=120) as client:
        while True:
            try:
                message = console.input("[bold cyan]you>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not message:
                continue
            if message == "/quit":
                break
            if message == "/new":
                session_id = None
                console.print("[dim]new conversation[/]")
                continue

            payload = {"message": message, "session_id": session_id}
            try:
                resp = client.post("/chat", json=payload)
            except httpx.RequestError as e:
                console.print(f"[bold red]can't reach {args.url}:[/] {type(e).__name__}")
                continue
            if resp.status_code != 200:
                console.print(f"[bold red]HTTP {resp.status_code}:[/] {resp.text}")
                continue

            data = resp.json()
            session_id = data["session_id"]
            console.print(Panel(Markdown(data["answer"] or "_(no answer)_"), title="agent", border_style="green"))
            tools = ", ".join(data["tool_calls"]) or "none"
            console.print(
                f"[dim]pod={data['served_by']}  session={session_id[:8]}  tools={tools}  "
                f"llm_calls={data['llm_calls']}  tokens in/out/think="
                f"{data['prompt_tokens']}/{data['output_tokens']}/{data['thinking_tokens']}  "
                f"{data['latency_s']}s[/]\n"
            )


if __name__ == "__main__":
    main()
