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
