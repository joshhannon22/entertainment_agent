# Field Notes

Friction points found while building, with what happened, the workaround, and the feature I'd ask the product team for. The goal is to turn the best ones into real GitHub issues/PRs.

---

## 2026-10-15 — Gemini `thought_signature` leaks through the OpenAI-compatible API

**Phase:** 1, Step 1 (first chat call through `https://generativelanguage.googleapis.com/v1beta/openai/`, model `gemini-3.8-flash`)

**What happened:** A plain chat call with no tools returned a normal `ChatCompletionMessage`. It also had a field that isn't part of the OpenAI schema:

```
extra_content={'google': {'thought_signature': 'ErofCrcfAWkU...'}}
```

**What it means:** Gemini 3.x models are "thinking" models. They reason internally before answering. The `thought_signature` is an opaque, encrypted token that represents that reasoning. You can't read it, and it isn't the reasoning text. It's a handle that lets Gemini continue the same chain of reasoning on the next turn. Gemini's docs say signatures have to be **sent back unchanged** in multi-turn function calling. If the assistant message that contains `tool_calls` is sent back without its signature, Gemini may reject the request or lose its reasoning context.

**Why it matters for this project:** in the ReAct loop (Phase 1, Steps 3–4), I append the assistant's tool-call message to `messages` before the tool result. If I rebuild that message by hand (only `role`, `content`, `tool_calls`), the signature gets dropped. That's a provider-specific detail showing through a "standard" API, and it's the kind of thing that breaks a portable client without any obvious warning.

**Workaround / to verify in Step 3:** append the assistant message in a way that keeps `extra_content` (e.g. `message.model_dump(exclude_none=True)` rather than hand-building a dict), then check what happens when the signature is deliberately removed.

**Feature request:** have the OpenAI-compat docs show a full multi-turn tool-calling example that round-trips the signature, and/or have the endpoint return a clear error ("missing thought_signature on tool-call turn") instead of a generic 400.

---

## 2026-10-15 — Thinking tokens are hidden in `usage`

**Phase:** 1, Step 1

**What happened:** `response.usage` for a short prompt returned:

```
CompletionUsage(completion_tokens=739, prompt_tokens=14, total_tokens=1504,
                completion_tokens_details=None, prompt_tokens_details=None)
```

`14 + 739 = 753`, but `total_tokens = 1504`. **751 tokens are unaccounted for.** That's roughly 50% of the request. They're the model's internal thinking tokens. `completion_tokens_details.reasoning_tokens`, the OpenAI field meant for this, comes back as `None`, so the compat layer doesn't break them out.

**Why it matters:** thinking tokens are billed (at the output rate). Cost computed as `prompt × input_price + completion × output_price` would **undercount this request by about half**. This affects Step 6 (cost per request) directly.

**Workaround:** compute `thinking = total_tokens - prompt_tokens - completion_tokens` and bill it as output. Check this against the pricing docs and against AI Studio's usage dashboard.

**Feature request:** populate `completion_tokens_details.reasoning_tokens` on the OpenAI-compatible endpoint so standard OpenAI-format cost tooling works without Gemini-specific maths.

**Follow-up — `reasoning_effort=low` (same day):** with `REASONING_EFFORT=low` and the prompt "Say hello in five words", usage was `13 + 7 = 20`, so no hidden thinking at all. That's a big lever on cost and latency. The trade-off (possibly worse tool selection with less thinking) still needs to be measured in the Step 8 evals.

---

## 2026-10-15 — Thinking is non-deterministic, even at `reasoning_effort=low`

**Phase:** 1, Step 1

**What happened:** I ran a trivial prompt ("Say hello in six words") several times with `reasoning_effort=low`:

| Run | prompt | completion | total | hidden thinking | latency |
|---|---|---|---|---|---|
| A | 13 | 8 | **378** | **357** | **1.61 s** |
| B | 13 | 8 | 21 | 0 | 0.73 s |

Same prompt, same settings. One run thought for ~357 tokens, the other didn't think at all. That's **18× the tokens and 2.2× the latency** on identical input. It first looked like going from "five words" to "six words" caused it. Running it again showed it didn't: the model decides on each request whether to think, and that decision varies from run to run.

**Why it matters:**
- **A single run tells you nothing about cost or latency.** Benchmarks need multiple runs and percentiles (p50/p95), not one sample. That applies to Step 6 metrics, Step 8 evals and `docs/benchmarks.md`.
- **`reasoning_effort=low` is a hint, not a cap.** It doesn't guarantee zero thinking tokens. Cost estimates should use observed distributions, not best-case runs.
- Lesson for debugging: change one thing, then **re-run before blaming the change**.

**Workaround:** report cost/latency as averages and percentiles over N runs. Consider a hard cap via max tokens in Phase 6 guardrails.

**Feature request:** expose a hard thinking-token budget through the OpenAI-compatible endpoint (and report reasoning tokens in `usage`, see above) so cost per request can be bounded.
