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

---

## 2026-10-18 — Step 3: first manual tool-call round-trip (findings + Q&A)

**Phase:** 1, Step 3 (`apps/agent-loop/agent/loop.py`, model `gemini-3.8-flash`, question "Is Arcane finished, and where can I watch it?")

**What happened:** The round-trip worked end to end. Call 1 came back with `finish_reason='tool_calls'`, `content=None` and a single tool call: `search_tv_shows` with `arguments='{"query":"Arcane"}'`. I appended the assistant message (`msg.model_dump(exclude_none=True)`), ran the tool with `TOOLS[name](**json.loads(arguments))`, and appended a `role: "tool"` message with the matching `tool_call_id`. Call 2 returned `finish_reason='stop'` and a text answer: finished in November 2024, on Netflix.

**Things I learned from the raw output:**
- **`content=None` is normal on a tool-call turn.** The model sends a request *instead of* an answer, because it can't run code. `finish_reason` is the field the loop branches on: `"tool_calls"` means "run these and call me again", `"stop"` means "final answer".
- **`arguments` is a JSON *string*, not a dict.** It needs `json.loads` before it can be unpacked with `**`.
- **The `thought_signature` moved.** In Step 1 (no tools) it was on the message. On a tool-call turn it sits on **each tool call** (`ChatCompletionMessageFunctionToolCall.extra_content`). Building the assistant message by hand from `id`/`name`/`arguments` would silently drop it. `model_dump(exclude_none=True)` keeps it because it serializes nested fields.
- **Grounding gap:** status and `where_to_watch` came from the TVMaze result. But the answer also said "other series set in the same League of Legends universe are in development", which isn't in any field my tool returns. It came from the model's training data. That's the same mechanism behind confident wrong answers, so Step 8 evals should check answers against the tool output.
- **Readable output:** SDK objects are Pydantic models, so `choice.model_dump_json(indent=2, exclude_none=True)` prints them as clean JSON.

**Q1: Why must the assistant's tool-call message go in *before* the tool result?**
- *My answer:* "It needs to know which tool call the content is for, so it knows why it has the content."
- *What was right / missing:* That's the right direction but incomplete. It leaves out the two actual mechanisms.
- *Correct answer:*
  1. **The model is stateless.** It doesn't remember asking for the tool. Every `chat` call sends the whole conversation again, and that `messages` list is the model's only memory. Without the assistant's tool-call message, a tool result shows up with no request before it.
  2. **The API checks the structure.** By the OpenAI chat-completions spec, a `role: "tool"` message must answer a `tool_calls` entry in the assistant message right before it. Break that and the request typically fails with a 400 *before the model sees anything*.
  3. **On Gemini specifically,** that assistant message also carries the `thought_signature`, the model's reasoning context for the request.

**Q2: What breaks if `tool_call_id` doesn't match?**
- *My answer:* "It would get an error that it wasn't found, and the agent would say something like it tried to call this and got an error."
- *Why that's wrong:* It assumes the model sees the mismatch and talks about it. More likely the **API rejects the malformed request outright** and my Python code gets an exception, so the model never sees anything. The two outcomes are very different:

  | What happens | Who handles it | Effect on the loop |
  |---|---|---|
  | My tool returns `{"error": ...}` | The model reasons about it and explains it to the user | Keeps running |
  | The API rejects a malformed request (bad id, bad order) | My code gets an exception | Crashes unless caught |

  The "agent explains the error" behavior only exists because I designed tools to return error dicts. Protocol mistakes in how I build `messages` are *my* bugs, and they show up as exceptions.
- *Correct answer:* The `tool_call_id` is the key that links each result to the specific call that asked for it. That matters as soon as the model makes several tool calls in one turn. By the OpenAI spec, a mismatch is an invalid request (400).
- **Gemini caveat, still to verify:** Gemini's *native* API matches function results by **name**, not by id, so the OpenAI-compat layer might tolerate or ignore a wrong id. If it does, that's a portability trap: code that works on Gemini would break on OpenAI/Azure.

**Still to run (finishes Step 3):**
- [ ] Set `tool_call_id` to a fake value (e.g. `"call_000"`) and record exactly what Gemini does.
- [ ] Remove `extra_content` from the tool call in the appended assistant message and record whether Gemini rejects it (and the error text).

**Feature request:** (pending the two experiments) document how the OpenAI-compat endpoint validates `tool_call_id` and missing signatures, and return explicit error messages for both.
