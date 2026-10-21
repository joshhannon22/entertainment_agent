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

**Experiments (run 2026-10-20, finishes Step 3):**

- **Accidental experiment 0: assistant message missing entirely.** While refactoring, I dropped the `messages.append(msg.model_dump(...))` line, so a `role: "tool"` message followed the user message directly. Result: `400 INVALID_ARGUMENT` — `GenerateContentRequest.contents[1].parts[0].function_response.name: Name cannot be empty.`
  - `GenerateContentRequest` is Gemini's **native** request type: the compat endpoint translates OpenAI-shaped messages into a native request *before* validating.
  - `system` isn't in `contents` (it becomes `system_instruction`), so `contents[1]` was the tool message.
  - OpenAI's tool result carries a `tool_call_id` and no name; Gemini's `functionResponse` needs a `name` and has no id. The translator resolves the name by finding the assistant `tool_calls[i]` whose `id` matches. No assistant message → no match → empty name.
- **Experiment 1: fake `tool_call_id` (`"call_000"`), assistant message present.** Result: the **same** error, one slot later — `contents[2].parts[0].function_response.name: Name cannot be empty.` (`[0]` user, `[1]` assistant `functionCall`, `[2]` tool result.) So Gemini does not ignore the id; it's the join key. But a bad id is reported as its *side effect* (empty name), not as "unknown tool_call_id". A missing assistant message and a wrong id give the same message; only the index differs.
- **Experiment 2: `thought_signature` removed.** Dumped the assistant message with `msg.model_dump(exclude_none=True, exclude={"tool_calls": {"__all__": {"extra_content"}}})` (Pydantic nested exclude; `"__all__"` applies to every item in the list). Result: `400 INVALID_ARGUMENT` — `Function call is missing a thought_signature in functionCall parts. This is required for tools to work correctly ... function call 'default_api:search_tv_shows', position 2.` This one is explicit and links to the docs. Tools are namespaced as `default_api:` internally by the compat layer.

**Key lessons:**
- Protocol mistakes in how I build `messages` surface as **exceptions in my code**; the model never sees them. Confirms the Q2 correction above.
- The assistant tool-call message must be appended **as returned** (`model_dump(exclude_none=True)`), never rebuilt by hand from `id`/`name`/`arguments`. This matters again in Phase 6: if session state is persisted to Postgres with only the standard OpenAI fields, resumed conversations on Gemini 3 will 400.
- Error text is provider-specific even when the rule is the same, so don't key error handling on message strings.

**Feature request:** the OpenAI-compat endpoint should validate `tool_call_id` directly and say so ("tool message `call_000` does not match any tool_call in the preceding assistant message"), instead of reporting the downstream symptom `function_response.name: Name cannot be empty`. The thought-signature error is the model to copy: it names the field, the function, the position and links to docs.

## 2026-10-20 — Step 4: the ReAct loop, smoke-tested on every exit path

**Phase:** 1, Step 4 (`run_agent` in `apps/agent-loop/agent/loop.py`, model `gemini-3.8-flash`). Five smoke tests in `__main__`, one per exit path: happy path, `max_steps=1` cap, two shows in one question, unknown title, and a content-filtered request.

**Findings:**
- **Gemini sends parallel tool calls.** "Are Arcane and Severance both finished...?" → `steps=2` with **two** `search_tv_shows` calls in a single assistant turn. One assistant message, then one `role: "tool"` message per call, each with its own `tool_call_id`. Two lookups cost one LLM round-trip, not two. That's relevant to the Phase 3b router design.
- **False-positive safety block.** "Where can I watch The Glorbnax Chronicles?" was blocked on the **first** call, before any tool ran: the choice came back with `message=None`, `finish_reason="content_filter: PROHIBITED_CONTENT"` and zero usage. "The Zorblax Saga" and "Blorptastic" went through and correctly returned "not found", so the trigger is specific to that token, not to made-up titles in general. Reproducible across runs.
- **`finish_reason` isn't the standard string.** OpenAI's spec says `"content_filter"`; Gemini's compat layer appends the native reason: `"content_filter: PROHIBITED_CONTENT"`. `== "content_filter"` would never match. Use `startswith`, or store the raw value (what `AgentResult.stop_reason` does now).
- **A first guard turned a crash into a retry loop.** Wrapping the body in `if msg:` stopped the `AttributeError` but fell through and re-sent identical `messages` six times (`steps=6`), and because the latency/usage lines had moved inside the `if`, it reported `latency 0.0` for six real calls. Fix: a no-message response is **terminal** (guard clause, return with the real `finish_reason`), and accounting runs for **every** call, whatever the outcome.
- **Grounding drift.** Arcane's answer added "the creators always intended it as a two-season story" in all four runs (not in any tool field). Severance once got "Season 2 premieres January 2025", stale by 21 months, because TVMaze only said `Running` and the model filled in the rest from training data; the next run said something different. Consistent ungrounded claims are the more dangerous kind: one spot check would pass them. Candidate mitigations for Step 8: put today's date in the system prompt; replace "don't guess" with "only state facts present in tool results".
- `hidden_thinking=0` on all calls this session, vs 751 on one call on 10-18. Same model and config. More evidence that cost needs percentiles over N runs.

**Feature request:** when the compat endpoint blocks a request (`content_filter: PROHIBITED_CONTENT`), return *which* part of the input triggered it, or at least a category. Right now a fictional show title can't be told apart from a real policy violation, and a customer can't tune around false positives they can't see.

---

## 2026-10-21: Step 5, CheapShark and Open Library (and what the schema description actually controls)

**Phase:** 1, Step 5 (`find_game_deals` and `search_books` in `apps/agent-loop/agent/tools.py`)

**What happened:**
- **CheapShark needs an undocumented User-Agent.** The first `curl` returned `HTTP 400` with `{"error": "Missing or generic User-Agent header detected. Please identify your client with a descriptive User-Agent (e.g., 'MyApp/1.0 (contact@example.com)')."}`. curl's `curl/8.7.1` and httpx's `python-httpx/x` both count as generic. The plan's API table said "auth: none". Workaround: one shared `HEADERS = {"User-Agent": "WatchParty/0.1 (+<repo url>)"}` sent by every tool. Use a repo URL rather than an email, because the code is public.
- **CheapShark latency spikes to about 10 s.** Identical requests took 0.10 / 0.11 / 0.17 s, then 10.19 s, right at `TIMEOUT_S = 10`. One smoke test hit the timeout. The tool returned its error dict as designed. Retries with backoff are Phase 3.
- **CheapShark sends every number as a string** (`"salePrice": "0.89"`, `"savings": "91.091091"`, `"isOnSale": "0"`), and `"0"` means "missing". `bool("0")` is `True`, which makes this an easy trap.
- **Open Library hides useful fields by default.** `search.json` returns no ratings, page count or subjects unless you ask for them with `fields=`. Missing values are left out rather than sent as `null`.
- **Open Library results are mostly noise for popular books.** For "Project Hail Mary", 4 of 5 results were "SUMMARY and REVIEW"-style knockoffs or an omnibus. `edition_count` separates them (32 vs 1). An omnibus with one review shows a 5.0 rating.
- **Invented price history.** In the three-tool smoke test, the answer said Portal 2 "routinely drops to around $1–2 during sales", right next to the real tool price ($9.99). The tool has no price history. This is worse than an ungrounded "it's co-op", because it's a fabricated fact in the tool's own domain. Step 8 eval case: every price in the answer must appear in a tool result. Prompt fix to try: "only state prices, dates, ratings and availability that appear in a tool result."
- **Vague vs precise schema, measured.** Same question ("Find me a cheap co-op game to play tonight."), with only the `find_game_deals` descriptions changed (in memory, by mutating the schema dict):
  - **Precise:** the model named 5 specific titles, made parallel calls, never set `max_price`, and filtered "cheap" on real prices.
  - **Vague** ("Finds game deals." / "The search term." / "Max price."): `title="co-op"`, then invented `max_price=10` and `15`. It Takes Two came back `count: 0` because of the invented filter (a silent false negative). The answer recommended games *with "CO-OP" in their names*.

**Key lesson:** an API without the data the user's question needs (CheapShark has no genre or co-op info) can be worked around in the schema description, by telling the model *how* to use the tool, not just what it is. The trade-off is that the "fits the request" part of the answer is now the model's own knowledge and isn't grounded, so evals have to treat it separately.

**Feature request (CheapShark):** document the User-Agent requirement on the API docs page and return it with a `403`/`400` plus a docs link, not only in the body. Return numbers as JSON numbers. **(Gemini compat):** nothing new this step.
