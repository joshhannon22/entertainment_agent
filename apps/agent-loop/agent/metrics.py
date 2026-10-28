"""LLM-native metrics for one agent run: cost, tokens/sec, per-step latency.

Pure functions over AgentResult. No I/O and no API calls, so evals (Step 8) and later
Prometheus/OTel exporters (Phase 6) can reuse them unchanged.
"""
from dataclasses import dataclass, field

# USD per 1M tokens, paid tier, standard (not batch, not cached).
# Source: https://ai.google.dev/gemini-api/docs/pricing, checked 2026-10-27.
# Hidden thinking tokens are billed at the OUTPUT rate.
PRICING = {
    "gemini-3.8-flash":      {"input": 0.75, "output": 3.75},   # doubles on 2027-01-01
    "gemini-3.6-flash":      {"input": 0.75, "output": 3.75},   # doubles on 2027-01-01
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
}


@dataclass
class CallMetrics:
    step: int
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    thinking_tokens: int            # total - prompt - completion (Gemini hides these)
    cost_usd: float | None          # None if the model isn't in PRICING
    output_tps: float | None        # (completion + thinking) / latency
    finish_reason: str | None


@dataclass
class RunMetrics:
    model: str
    steps: int
    llm_latency_s: float
    tool_latency_s: float
    prompt_tokens: int
    completion_tokens: int
    thinking_tokens: int
    total_tokens: int
    cost_usd: float | None
    output_tps: float | None
    calls: list[CallMetrics] = field(default_factory=list)


def price_for(model: str) -> dict | None:
    return PRICING.get(model.removeprefix("models/"))


def cost_usd(model: str, prompt: int, completion: int, thinking: int) -> float | None:
    """Cost of one LLM call. Returns None for an unknown model; metrics must never crash a run."""
    price = price_for(model)
    if price is None:
        return None
    return (prompt * price["input"] + (completion + thinking) * price["output"]) / 1_000_000


def _tps(tokens: int, seconds: float) -> float | None:
    return tokens / seconds if seconds > 0 else None


def summarize(result, model: str) -> RunMetrics:
    """Build RunMetrics from an AgentResult (duck-typed to avoid a circular import with loop.py)."""
    calls = []
    for c in result.llm_calls:
        thinking = max(c["total_tokens"] - c["prompt_tokens"] - c["completion_tokens"], 0)
        calls.append(CallMetrics(
            step=c["step"],
            latency_s=c["latency_s"],
            prompt_tokens=c["prompt_tokens"],
            completion_tokens=c["completion_tokens"],
            thinking_tokens=thinking,
            cost_usd=cost_usd(model, c["prompt_tokens"], c["completion_tokens"], thinking),
            output_tps=_tps(c["completion_tokens"] + thinking, c["latency_s"]),
            finish_reason=c["finish_reason"],
        ))

    prompt = sum(c.prompt_tokens for c in calls)
    completion = sum(c.completion_tokens for c in calls)
    thinking = sum(c.thinking_tokens for c in calls)
    llm_latency = sum(c.latency_s for c in calls)
    known_costs = [c.cost_usd for c in calls if c.cost_usd is not None]

    return RunMetrics(
        model=model,
        steps=len(calls),
        llm_latency_s=llm_latency,
        tool_latency_s=sum(t.get("latency_s", 0.0) for t in result.tool_calls),
        prompt_tokens=prompt,
        completion_tokens=completion,
        thinking_tokens=thinking,
        total_tokens=prompt + completion + thinking,
        cost_usd=sum(known_costs) if len(known_costs) == len(calls) else None,
        output_tps=_tps(completion + thinking, llm_latency),
        calls=calls,
    )


def _fmt_cost(c: float | None) -> str:
    return f"${c:.6f}" if c is not None else "n/a"


def _fmt_tps(t: float | None) -> str:
    return f"{t:.0f} tok/s" if t is not None else "n/a"


def format_report(m: RunMetrics, per_step: bool = False) -> str:
    lines = []
    if per_step:
        for c in m.calls:
            lines.append(
                f"  step {c.step}: {c.latency_s:5.2f}s  in={c.prompt_tokens:>5} out={c.completion_tokens:>4} "
                f"think={c.thinking_tokens:>4}  {_fmt_tps(c.output_tps):>11}  {_fmt_cost(c.cost_usd)}  "
                f"finish={c.finish_reason}"
            )
    lines.append(
        f"[metrics] model={m.model} steps={m.steps} "
        f"llm={m.llm_latency_s:.2f}s tools={m.tool_latency_s:.2f}s | "
        f"tokens in={m.prompt_tokens} out={m.completion_tokens} think={m.thinking_tokens} total={m.total_tokens} | "
        f"{_fmt_tps(m.output_tps)} | cost={_fmt_cost(m.cost_usd)}"
    )
    if m.cost_usd is None:
        lines.append(f"[metrics] no price for '{m.model}': add it to metrics.PRICING")
    return "\n".join(lines)
