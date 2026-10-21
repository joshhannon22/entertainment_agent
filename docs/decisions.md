# Decisions

One paragraph per real choice, written so it can be said out loud in an interview.

## The agent loop branches on `tool_calls`, and a missing message is terminal (Phase 1, Step 4)

The loop decides "run tools or return an answer" by checking whether the assistant message actually contains `tool_calls`, not by checking `finish_reason`. What the loop needs to know is whether there's work to do, and providers aren't consistent about which `finish_reason` they send. Gemini's OpenAI-compatible endpoint even sends non-standard values like `"content_filter: PROHIBITED_CONTENT"` where the spec says `"content_filter"`. `finish_reason` is still recorded raw in `stop_reason`, so evals and metrics can tell "answered" apart from "truncated", "blocked" or "hit the step cap". A response with no message at all (a safety block) ends the run immediately instead of being retried. The first version retried, and re-sent identical input six times against a filter that was certain to give the same result. Every LLM call is counted toward latency and tokens whatever its outcome, because the requests that fail are the ones a cost dashboard most needs to show.
