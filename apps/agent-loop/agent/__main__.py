"""CLI for the hand-written agent.

Run from apps/agent-loop:
    python -m agent "find me a cheap co-op game and a show to watch after"
    python -m agent -v "..."            # per-step LLM/tool trace + per-step metrics

stdout = the answer only (pipeable). stderr = trace + metrics.
Exit code: 0 answered, 1 stopped without an answer or the LLM call failed, 2 bad config.
"""
import argparse
import logging
import sys

import openai
from dotenv import load_dotenv

from . import llm, metrics
from .loop import DEFAULT_MAX_STEPS, run_agent

# run_agent reports facts (answer=None + stop_reason); the CLI owns the wording.
STOP_MESSAGES = {
    "max_steps": "I ran out of steps before finishing. Try a narrower question, or raise --max-steps.",
    "length": "The model hit its output limit before answering (thinking may have used the budget). Try again.",
    "empty_response": "The model returned an empty response. Try again.",
    "content_filter": "The model's safety filter blocked this request. Try rephrasing it.",
    "no_message": "The model returned no message. Try again.",
}


def explain_stop(stop_reason: str) -> str:
    # Gemini sends e.g. "content_filter: PROHIBITED_CONTENT", so match on the part before ':'
    key = stop_reason.split(":", 1)[0].strip()
    return STOP_MESSAGES.get(key, f"The agent stopped without an answer ({stop_reason}).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description="Watch Party entertainment concierge")
    parser.add_argument("question", help="what you want to watch, play or read")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("-v", "--verbose", action="store_true", help="show the per-step trace and metrics")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines duplicate our [tool] trace
    load_dotenv()

    try:
        client = llm.LLMClient()
    except ValueError as e:
        print(f"Config error: {e}. Check apps/agent-loop/.env.", file=sys.stderr)
        return 2

    try:
        result = run_agent(client, args.question, max_steps=args.max_steps)
    except openai.APIError as e:
        # Tools never raise, but the LLM call itself can (bad key, 429, 5xx). Handle it at the edge.
        print(f"LLM call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(result.answer if result.answer else explain_stop(result.stop_reason))
    print(metrics.format_report(metrics.summarize(result, client.model), per_step=args.verbose),
          file=sys.stderr)
    return 0 if result.answer else 1


if __name__ == "__main__":
    sys.exit(main())
