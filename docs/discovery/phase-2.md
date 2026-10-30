# Discovery brief — Phase 2: the agent as a Kubernetes workload

**Customer problem:** "The agent works on a developer's laptop through `adk web`. Platform won't run it until it's a normal service: an HTTP contract, an image, config and secrets injected at deploy time, health checks, and more than one replica."

**Success metric:**
- `curl localhost:8080/chat -d '{"message":"..."}'` returns a grounded answer from a pod in kind.
- Image runs as non-root, contains no API key (`docker history` / `docker run ... env` show nothing).
- Same model + prompt → cost per request within ~10% of the Phase 1b eval numbers (the HTTP layer adds no LLM calls).

**Constraints:** local kind only, Gemini API key (free tier), no registry (`kind load`), key lives only in a hand-created Kubernetes Secret.

**Out of scope:** MCP / splitting tools out (Phase 3), persistent sessions (Phase 6), ingress, auth on the endpoint, autoscaling.

**Risks / questions to answer:**
- Sessions are in-memory per pod. At 3 replicas, does a follow-up turn on another pod fail loudly or silently lose context? (We chose loudly: 404.)
- Do concurrent requests on one pod block each other? (Sync tools run on the event loop by default in ADK.)
- What should liveness/readiness check? Calling Gemini from a probe would turn a provider blip into a restart storm.
