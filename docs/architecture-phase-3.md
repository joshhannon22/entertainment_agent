# Architecture one-pager: agent-api + tools-server (Phase 3)

```
 client ──HTTP /chat──▶ agent-api (ADK Runner, LlmAgent)
                          │  ▲
                Gemini ◀──┘  │  MCP over streamable HTTP
                (API key)    │  POST http://tools-server/mcp   (JSON-RPC: tools/list, tools/call)
                             ▼
                        tools-server (MCPServer, stateless)
                             │  httpx, User-Agent, timeouts, retries
                             ▼
              TVMaze · CheapShark · Open Library · Jikan · FreeToGame · OpenTDB · JokeAPI · TMDb
```

**Responsibilities**

| | agent-api | tools-server |
|---|---|---|
| Owns | reasoning, prompt, sessions, LLM cost | integrations, API quirks, trimming, retries |
| Secrets | `GOOGLE_API_KEY` | `TMDB_API_KEY` |
| Egress | Gemini only (Phase 6/7 enforce this) | public APIs on 443 |
| State | sessions (in memory → Postgres in Phase 6) | none (`stateless_http=True`) |
| Scales on | LLM concurrency | external API latency |

**Data flow for one turn**
1. Startup / first use: `McpToolset` calls `tools/list`; ADK turns each MCP tool's JSON schema into a Gemini function declaration.
2. Gemini returns a function call → ADK sends `tools/call {name, arguments}` to tools-server.
3. tools-server runs the Python function, which never raises; its dict comes back as MCP structured content.
4. ADK feeds it to Gemini as a function response, same as Phase 1b.

**Contract between the services:** the MCP tool list (names, descriptions, input schemas). Changing a tool is a tools-server deploy; the agent re-reads the list. Breaking change = renaming a tool or a required argument.

**Failure modes:** tools-server down → MCP connection error on the turn; one public API down → error dict, model falls back; slow API → bounded by the tool's `TIMEOUT_S` and the agent's MCP `timeout`/`sse_read_timeout` (the agent timeout must be the larger one).

**Security posture (now → later):** in-cluster only (ClusterIP), no auth yet → Phase 6 NetworkPolicy (only agent-api may reach tools-server:8000; only tools-server gets internet egress) → Phase 7 FQDN egress allowlist on the tools-server path.
