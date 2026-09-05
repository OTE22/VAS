# Agent production assessment and acceptance

Assessment started 2026-09-05. This is an evidence ledger, not a production certification.
Baseline: isolated regression `47_2b406b71`, **363 passed, 6 warnings in 46.27s**.
Command: `scripts/run_regression_isolated.sh` with agent tool/planner, SQL guard,
gateway/provider, working memory, dialogue, reasoning, scope, session path and
logging-redaction suites. Logs: `logs/regression/regression_47_2b406b71.log`.
Isolation assertions passed; runner reported development DB list, Redis size,
and storage markers unchanged. No production writes or live inference used.

## Runtime assessment

`sql_agent/api/routes.py` serves REST, SSE and WebSocket requests. Authentication
uses `require_chatbot_access`; per-user locks and a global semaphore precede
execution. The API binds camera scope to the same `DatabaseManager` held by the
graph, loads owner-scoped artifacts and memory, and invokes `agent.py` in a
worker thread. `graph.py` routes ingestion/security to `plan_action`, then SQL,
chat/clarification or artifacts. `agent_loop.py` permits bounded read-only
lookups before returning a validated action to the graph. SQL generation uses
Chroma examples; `sql_guard.py` validates the AST, applies scope, and the
database connection enforces read-only transactions and statement timeout.
Artifacts are rendered in the graph and registered asynchronously by the API.
History, conversation messages, references and optional enrichment are persisted
after the run. Browser transport renders events with request IDs and sequences;
`frontend/js/conversations.js` links artifacts through authenticated API routes.

The production deployment is offline, with local Ollama, PostgreSQL, Redis,
single API worker, Prometheus and Grafana. NIM is explicitly development-only.
The SQL agent has no approved shell, arbitrary URL, payment or destructive tool.
ML jobs are a separate governed API/worker, not SQL-agent tools. There is no
evidence justifying specialist agents or MCP servers; retain one orchestrator.

## Requirements matrix

| Capability | Existing implementation | Evidence | Gap | Proposed change | Acceptance test |
|---|---|---|---|---|---|
| Chat, query, clarification | Model tool loop and structured fallback | `tools/agent_loop.py`, `tools/interpreter.py`; baseline tool tests | Output parsing/coercion weak; native observations lose call linkage | Validate contracts; preserve tool protocol | Invalid/malicious/multiple calls; natural chat |
| Multi-step control | Graph, action/replan/lookup ceilings | `graph.py`, `reasoning.py`; reasoning tests | No shared call/token budget across graph nodes | Shared run budget and node boundaries | Exhaustion, cancellation, repeated calls |
| Tool authority | Closed names, fixed lookup SQL, scoped executor | `tool_registry.py`, `tool_executors.py`, `sql_guard.py` | No versioned policies/output schemas; exception text logged | Formal contracts and sanitized observations | Invalid parameters/results, timeout, unauthorized action |
| Working/conversation memory | Typed state, structured dialogue, atomic files | `state.py`, `conversation_memory.py`; persistence tests | Missing/corrupt reload can retain stale RAM; no session expiry | Expiry and stale-state invalidation | Missing, corrupt, expired, restart, isolation |
| Persistent memory | Owner-scoped DB/API, expiry and deletion | `user_query_history_service.py`, `UserConversationMemory` | Implicit preference inference; weak API validation; no revision provenance | Explicit writes only, bounded schema, retention/provenance | Explicit preference, expiry, deletion, isolation |
| RAG | Curated and user-scoped Chroma SQL examples | `knowledge_base.py`; KB isolation tests | Result IDs/version omitted; raw example interpolation | Source metadata and bounded untrusted-data envelope | Retrieval relevance, no evidence, injection, cross-user |
| Factual grounding | Validated SQL results and artifact lineage | `agent_tools.py`, artifact registry | SQL examples are not factual evidence; provenance not unified | Preserve source references; distinguish planning examples | No-result handling, source lineage, fabricated success |
| Authentication/authorization | Fail-closed import, chatbot permission, AST scope | API, auth service, SQL guard | Must preserve at every new seam | Reuse existing boundaries | Authz/scope/artifact isolation suites |
| Provider resilience | Eligible registry, retry, breaker, ledger | `llm/gateway.py`; gateway baseline | Invocation failures do not try eligible fallback; no run attribution | Runtime fallback with same eligibility and budget | Outage, incompatible fallback, partial stream |
| Cancellation/backpressure | Timeout, semaphore, cancellation endpoint, worker drain | API request registry and stream bridge | Active requests evicted when registry full; thread loses contextvars | Reject capacity, propagate context | Saturation, concurrent users, cancellation |
| Artifact completion | Render then API registration | `reasoning.build_observation`, `_persist_agent_artifact` | Rendered bytes incorrectly trigger missing-registration invariant | Model pending versus persisted stage explicitly | Pending render, persistence failure, restart |
| Observability | Correlation logging, planner metrics, stage timing | `utils/logging.py`, `observability.py` | New sources mapped to other; no per-run usage/events | Structured redacted events and compatible metrics | Trace propagation, redaction, outcomes, cost |
| Evaluation | Extensive deterministic tests; optional live e2e | `tests/`, isolated runner | No single versioned scenario/acceptance report | Versioned scenario manifest and reproducible report | Required 20 scenarios; regression comparison |
| Deployment | Read-only role, secret files, offline guard, isolated migrations | production compose, config guard, runbook | New controls need operational documentation | Defaults, deployment/rollback/troubleshooting | Config/deployment contracts |
| Multi-agent/MCP | Not deployed in SQL runtime | Closed internal registry and single graph | None justified | Keep internal schemas portable; no new services | No arbitrary delegation or remote tool execution |

## Acceptance status

Baseline complete. Implementation and final verification pending. Requirements
must be marked complete only when corresponding behavior and tests support it.
Single-worker in-memory idempotency does not survive process restart; do not
claim exactly-once external side effects. Model factual accuracy needs separate
behavioral evaluation; passing scripted tests proves control contracts only.
