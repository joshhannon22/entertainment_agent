"""Run evals/questions.yaml against a running agent-api over HTTP (POST /chat).

From the repo root, with agent-api reachable (e.g. kubectl port-forward svc/agent-api 8080:80):
    python evals/run_api_evals.py --label single-agent --out evals/results/single-agent.json
    python evals/run_api_evals.py --runs 1 --only tv_single --url http://localhost:8000

Why a second runner: run_evals.py drives the Phase 1 hand-written loop in-process. This one
measures the deployed ADK service from the outside, so the same harness scores the single
agent now and the multi-agent version in Phase 3b: same questions, same checks, only the
service behind the URL changes. That's what makes the comparison fair.

Checks reused from run_evals.py: expected tools, min/max tool calls, answer contains.
Not checked here: tool arguments and $price grounding (/chat returns tool names, not args
or tool output). Groundedness comes from `adk eval` (hallucinations_v1) instead.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals"))
sys.path.insert(0, str(REPO / "apps" / "agent-loop"))

from run_evals import check_answer, check_trajectory, percentile   # noqa: E402
from agent.metrics import cost_usd                                 # noqa: E402

REQUEST_TIMEOUT_S = 120.0   # a multi-agent turn with a critic retry can be slow
MAX_CONSECUTIVE_UNREACHABLE = 3   # then stop: the service (or the port-forward) is gone


def run_once(client: httpx.Client, url: str, case: dict, default_model: str) -> dict:
    """One fresh-session turn. Returns a record; HTTP/transport errors become a failed run."""
    start = time.perf_counter()
    try:
        resp = client.post(f"{url}/chat", json={"message": case["question"]})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"fails": [f"HTTP {e.response.status_code}: {e.response.text[:200]}"]}
    except httpx.HTTPError as e:
        return {"fails": [f"request failed: {type(e).__name__}"]}
    wall_s = time.perf_counter() - start

    # Shape it like the Phase 1 AgentResult so the existing checks work unchanged.
    # args="" means args_must_not_contain can't fire here (see module docstring).
    result = SimpleNamespace(
        answer=data["answer"],
        stop_reason=data.get("stop_reason", "stop" if data["answer"] else "no_answer"),
        tool_calls=[{"name": n, "args": ""} for n in data["tool_calls"]],
    )
    fails = check_trajectory(case, result) + check_answer(case, result)
    model = data.get("model", default_model)   # agent-api < v3 doesn't report it
    output = data["output_tokens"] + data["thinking_tokens"]   # thinking bills at the output rate
    return {
        "fails": fails,
        "model": model,
        "tools": data["tool_calls"],
        "agents": data.get("agents_called", []),          # multi-agent only (agent-api >= v4)
        "revisions": data.get("revisions", 0),
        "llm_calls_by_agent": data.get("llm_calls_by_agent", {}),
        "llm_calls": data["llm_calls"],
        "prompt_tokens": data["prompt_tokens"],
        "output_tokens": data["output_tokens"],
        "thinking_tokens": data["thinking_tokens"],
        "cost_usd": cost_usd(model, data["prompt_tokens"], data["output_tokens"], data["thinking_tokens"]),
        "latency_s": data["latency_s"],   # server-side agent time
        "wall_s": round(wall_s, 2),       # what the caller waited, incl. network
        "output_tps": round(output / data["latency_s"], 1) if data["latency_s"] else None,
        "served_by": data["served_by"],
        "answer": data["answer"],
    }


def summarize(records: list[dict]) -> dict:
    ok = [r for r in records if "llm_calls" in r]   # runs that got a response at all
    def p(key, pct):
        vals = [r[key] for r in ok if r.get(key) is not None]
        return round(percentile(vals, pct), 5) if vals else None
    def mean(key):
        vals = [r[key] for r in ok if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    return {
        "runs": len(records),
        "pass_rate": round(sum(r["passed"] for r in records) / len(records), 3) if records else 0.0,
        "latency_p50_s": p("latency_s", 50), "latency_p95_s": p("latency_s", 95),
        "cost_p50_usd": p("cost_usd", 50), "cost_p95_usd": p("cost_usd", 95),
        "cost_total_usd": round(sum(r["cost_usd"] or 0 for r in ok), 4),
        "llm_calls_mean": mean("llm_calls"),
        "revision_rate": round(sum(r["revisions"] > 0 for r in ok) / len(ok), 3) if ok else None,
        "llm_calls_by_agent_mean": {
            a: round(sum(r["llm_calls_by_agent"].get(a, 0) for r in ok) / len(ok), 2)
            for a in sorted({a for r in ok for a in r["llm_calls_by_agent"]})
        },
        "prompt_tokens_mean": mean("prompt_tokens"),
        "thinking_tokens_mean": mean("thinking_tokens"),
        "output_tps_p50": p("output_tps", 50),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8080", help="agent-api base URL")
    parser.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    parser.add_argument("--only", help="run a single case id")
    parser.add_argument("--model", default="gemini-3.8-flash",
                        help="for pricing, only used if the server doesn't report its model")
    parser.add_argument("--label", default="unlabeled", help="name of this configuration, e.g. single-agent")
    parser.add_argument("--target", type=float, default=0.8, help="pass-rate threshold (default 0.8)")
    parser.add_argument("--out", help="write summary + per-run records as JSON to this path")
    args = parser.parse_args()

    cases = yaml.safe_load((REPO / "evals" / "questions.yaml").read_text())
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]

    records, rows = [], []
    unreachable, aborted = 0, False
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        for case in cases:   # sequential on purpose: parallel runs would skew latency
            passes = 0
            for i in range(args.runs):
                rec = run_once(client, args.url, case, args.model)
                rec.update(id=case["id"], run=i + 1, passed=not rec["fails"])
                records.append(rec)
                passes += rec["passed"]
                unreachable = unreachable + 1 if rec["fails"][:1] and rec["fails"][0].startswith("request failed") else 0
                detail = "" if rec["passed"] else f"  ← {'; '.join(rec['fails'])}"
                cost = f"${rec['cost_usd']:.4f}" if rec.get("cost_usd") is not None else "$?"
                stats = (f"  {rec['latency_s']:>5.1f}s {cost} calls={rec['llm_calls']}"
                         + (f" agents={','.join(rec['agents'])}" if rec["agents"] else "")
                         + (" REVISED" if rec["revisions"] else "")) if "llm_calls" in rec else ""
                print(f"  {case['id']:<20} run {i + 1}: {'PASS' if rec['passed'] else 'FAIL'}{stats}{detail}")
            rows.append((case["id"], passes))
            if unreachable >= MAX_CONSECUTIVE_UNREACHABLE:
                print(f"\nABORT: {unreachable} requests in a row couldn't reach {args.url} "
                      "(port-forward died?). Results below are partial and won't be saved.")
                aborted = True
                break

    summary = summarize(records)
    print(f"\n{'case':<20} pass")
    for cid, n in rows:
        print(f"{cid:<20} {n}/{args.runs}")
    models = sorted({r["model"] for r in records if "model" in r})
    print(f"\nlabel={args.label}  model={','.join(models) or '?'}  runs={summary['runs']}  "
          f"pass rate={summary['pass_rate']:.0%}  (target {args.target:.0%})")
    print(f"latency (s)    p50={summary['latency_p50_s']}  p95={summary['latency_p95_s']}")
    print(f"cost/request   p50=${summary['cost_p50_usd']}  p95=${summary['cost_p95_usd']}  total=${summary['cost_total_usd']}")
    print(f"per request    llm_calls={summary['llm_calls_mean']}  prompt_tokens={summary['prompt_tokens_mean']}  "
          f"thinking_tokens={summary['thinking_tokens_mean']}  output tok/s p50={summary['output_tps_p50']}")
    if summary["llm_calls_by_agent_mean"]:
        print(f"llm calls/agent {summary['llm_calls_by_agent_mean']}  critic revision rate={summary['revision_rate']:.0%}")

    if args.out and not aborted:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"label": args.label, "url": args.url, "models": models,
                                   "summary": summary, "records": records}, indent=2, ensure_ascii=False))
        print(f"wrote {out}")

    return 0 if summary["pass_rate"] >= args.target else 1


if __name__ == "__main__":
    sys.exit(main())
