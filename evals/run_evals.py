"""Run the Phase 1 eval set against the hand-written agent.

From the repo root:
    python evals/run_evals.py                  # every case, 3 runs each
    python evals/run_evals.py --runs 1 --only tv_single
    python evals/run_evals.py --model gemini-3.8-flash

Each case runs N times because the model is non-deterministic: one passing run proves little.
Exit code 0 if the pass rate meets --target, else 1 (so CI can gate on it in Phase 5).
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO / "apps" / "agent-loop"
sys.path.insert(0, str(AGENT_DIR))   # make the `agent` package importable from anywhere

from agent import llm, metrics          # noqa: E402
from agent.loop import run_agent        # noqa: E402

PRICE_RE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
BUDGET_RE = re.compile(r"(under|below|less than|up to|max(imum)?|budget of)\s*$")


# ── Checks: each returns a list of failure reasons (empty = pass) ─────────────

def _tool_texts(result) -> list[str]:
    return [m["content"] for m in result.messages if m.get("role") == "tool"]


def check_trajectory(case: dict, result) -> list[str]:
    called = [t["name"] for t in result.tool_calls]
    fails = [f"expected tool '{t}' not called (called: {called})"
             for t in case.get("expected_tools", []) if t not in called]
    if "min_tool_calls" in case and len(called) < case["min_tool_calls"]:
        fails.append(f"{len(called)} tool calls, expected at least {case['min_tool_calls']}")
    if "max_tool_calls" in case and len(called) > case["max_tool_calls"]:
        fails.append(f"{len(called)} tool calls, expected at most {case['max_tool_calls']}")
    for bad in case.get("args_must_not_contain", []):
        for t in result.tool_calls:
            if bad.lower() in t["args"].lower():
                fails.append(f"tool arg contains '{bad}': {t['name']}({t['args']})")
    return fails


def check_answer(case: dict, result) -> list[str]:
    if result.answer is None:
        return [f"no answer (stop_reason={result.stop_reason})"]
    text = result.answer.lower()
    fails = [f"answer missing '{s}'" for s in case.get("answer_contains_all", []) if s.lower() not in text]
    anys = case.get("answer_contains_any", [])
    if anys and not any(s.lower() in text for s in anys):
        fails.append(f"answer contains none of {anys}")
    return fails


def check_grounded_prices(result) -> list[str]:
    """Every $price in the answer must appear as a number in some tool result.

    Deliberately simple: it catches invented prices (the Portal 2 "$1-2" history) but would
    accept a price that matches an unrelated number. An LLM judge (ADK, Phase 1b) goes further.
    """
    if not result.answer:
        return []
    tool_numbers = {float(n) for text in _tool_texts(result) for n in NUMBER_RE.findall(text)}
    fails = []
    for match in PRICE_RE.finditer(result.answer):
        before = result.answer[max(0, match.start() - 15):match.start()].lower()
        if BUDGET_RE.search(before):
            continue   # "under $10" is a budget the model chose, not a price it claims
        if float(match.group(1)) not in tool_numbers:
            fails.append(f"price {match.group(0)} not found in any tool result")
    return fails


# ── Runner ────────────────────────────────────────────────────────────────────

def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Fine for small N and has no numpy dependency."""
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(pct / 100 * len(s)) - 1))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    parser.add_argument("--only", help="run a single case id")
    parser.add_argument("--model", help="override MODEL from .env")
    parser.add_argument("--target", type=float, default=0.8, help="pass-rate threshold (default 0.8)")
    parser.add_argument("--out", help="write per-run results as JSON to this path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    load_dotenv(AGENT_DIR / ".env")
    client = llm.LLMClient(model=args.model)

    cases = yaml.safe_load((REPO / "evals" / "questions.yaml").read_text())
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]

    rows, records = [], []
    for case in cases:
        passes = 0
        for i in range(args.runs):
            try:
                result = run_agent(client, case["question"])
            except Exception as e:   # an LLM/API error is a failed run, not a crashed eval
                fails, m = [f"run raised {type(e).__name__}: {e}"], None
            else:
                fails = check_trajectory(case, result) + check_answer(case, result) + check_grounded_prices(result)
                m = metrics.summarize(result, client.model)
            passes += not fails
            records.append({
                "id": case["id"], "run": i + 1, "passed": not fails, "fails": fails,
                "tools": [t["name"] for t in result.tool_calls] if m else [],
                "cost_usd": m.cost_usd if m else None,
                "latency_s": (m.llm_latency_s + m.tool_latency_s) if m else None,
                "total_tokens": m.total_tokens if m else None,
                "answer": result.answer if m else None,
            })
            mark = "PASS" if not fails else "FAIL"
            print(f"  {case['id']:<20} run {i + 1}: {mark}" + ("" if not fails else f"  ← {'; '.join(fails)}"))
        rows.append((case["id"], passes))

    total_runs = len(records)
    total_pass = sum(r["passed"] for r in records)
    rate = total_pass / total_runs if total_runs else 0.0

    print(f"\n{'case':<20} pass")
    for cid, p in rows:
        print(f"{cid:<20} {p}/{args.runs}")

    costs = [r["cost_usd"] for r in records if r["cost_usd"] is not None]
    lats = [r["latency_s"] for r in records if r["latency_s"] is not None]
    print(f"\nmodel={client.model}  runs={total_runs}  pass rate={rate:.0%}  (target {args.target:.0%})")
    if costs:
        print(f"cost/request   p50=${percentile(costs, 50):.5f}  p95=${percentile(costs, 95):.5f}  "
              f"total=${sum(costs):.4f}")
    if lats:
        print(f"latency (s)    p50={percentile(lats, 50):.2f}  p95={percentile(lats, 95):.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(records, indent=2, ensure_ascii=False))
        print(f"wrote {args.out}")

    return 0 if rate >= args.target else 1


if __name__ == "__main__":
    sys.exit(main())
