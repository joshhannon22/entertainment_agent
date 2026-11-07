# Discovery brief: Phase 3, tools as a separate MCP service

**Customer problem:** "Every new data source means redeploying the agent, and the agent pod holds every third-party key and egress path. Platform wants the integrations owned, scaled and secured separately from the reasoning service, through a standard protocol other agents can reuse."

**Success metric:**
- A new tool ships by changing **only** `tools-server` + a redeploy; `agent-api` picks it up with no code or config change.
- `agent-api` reaches tools only via `http://tools-server.watch-party.svc.cluster.local/mcp`; its pod holds no third-party API key (TMDb key lives in a Secret only `tools-server` mounts).
- Same eval set still passes (`adk eval` ≥ 8/10) through MCP; added per-request latency from the extra hop is measured and small (target < 100 ms p50 for the in-cluster round trip, excluding the external API).
- One public API failing (timeout / 5xx / 429) yields an error dict the model reasons about, never a crashed turn.

**Constraints:** local kind, no registry (`kind load`), Python MCP SDK 2.x over streamable HTTP, ADK `McpToolset` (`google-adk[mcp]==2.13.0`). Free public APIs only, plus one free-key API (TMDb).

**Out of scope:** auth between agent and tools-server (NetworkPolicy in Phase 6, identity in Phase 7), multi-agent (3b), RAG/catalog tools (4), MCP resources/prompts (tools only).

**Risks / questions to answer:**
- Is the tools-server stateless? If MCP sessions live in pod memory we recreate the Phase 2 replica bug. (Plan: `stateless_http=True`.)
- What does the agent do if tools-server is down at startup or mid-conversation? Does ADK fail the turn, or hide the tools?
- Tool descriptions: ADK parsed `Args:` docstrings into per-parameter descriptions; does the MCP SDK? (Checked: no, the whole docstring becomes the tool description.) Does tool selection degrade?
- Retries: where do they belong (inside each tool, per API's rate limit rules) and how do they interact with the MCP call timeout on the agent side?
