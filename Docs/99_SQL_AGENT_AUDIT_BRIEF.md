# 99 — Audit brief for the SQL agent (paste this to another agent)

Everything below the line is the prompt. It is written for an agent that has
the repository checked out and the development stack running. It is specific on
purpose: a generic "find bugs in this codebase" run produces a list of style
opinions, not the defects this system actually produces.

Keep this file in step with `Docs/57_AGENT_ARCHITECTURE.md`. When a failure
class listed here is fixed for good, say so here rather than deleting it — an
auditor needs to know what was already tried.

---

## MISSION

You are auditing the natural-language SQL agent of a face-recognition
surveillance system. Operators ask questions in English or Arabic; the agent
turns them into read-only SQL over a Postgres database of camera detections,
executes it under a security guard, and narrates the rows.

Find defects that make it **answer wrongly, answer about the wrong subject,
refuse an answerable question, or leak something it should not**. A confidently
wrong answer about a person under surveillance is the worst outcome in this
system: rank findings by that, not by code tidiness.

Do not refactor for taste. Do not restructure modules. Every change you propose
must be justified by an observed failure, with the evidence attached.

## THE ONE RULE THAT OVERRIDES STYLE

**Never fix a behaviour with a phrase list.** No keyword matching on the user's
words, no "if 'report' in text", no regex over the operator's message to decide
intent. This system has been rebuilt once to remove exactly that. A fix must be
one of:

1. **the model reading the turn** into the typed structure in
   `sql_agent/tools/interpreter.py` (`Interpretation`), or
2. **structure measured from the SQL** with `sqlglot` (the parser the security
   guard already uses), or
3. **data validation** — checking the model's claim against the database, the
   stored names, or the rows actually returned, or
4. **a verified example** added to the seed catalogue.

Closed vocabularies of *artefact types* are acceptable (file formats, error
codes, provider names). Closed vocabularies of *user intent* are not.

## ARCHITECTURE, BY FILE

The turn is a LangGraph state machine. Nodes, in the order a data turn hits
them:

```
ingest_query → detect_malicious_intent → plan_action → check_schema
  → retrieve_examples → generate_sql → validate_and_fix_sql
  → prepare_sql_for_execution → execute_sql → observe_and_replan
  → enrich_co_appearance → story_response → learn_from_query
```

Other terminals: `chat_response`, `render_artifact`, `translate_artifact`,
`modify_sql`.

| File | Lines | What it owns |
|---|---|---|
| `sql_agent/tools/agent_tools.py` | 5262 | every graph node; prompts; narration; correction hints |
| `sql_agent/api/routes.py` | 3826 | REST + SSE + WebSocket; artifact persistence; audit line |
| `sql_agent/seed_catalog.py` | 1635 | hand-verified question→SQL examples |
| `sql_agent/knowledge_base.py` | 1334 | ChromaDB retrieval, seeding, placeholder handling |
| `sql_agent/agent.py` | 1039 | the agent object, working context, orchestrator choice |
| `sql_agent/mcp/tools.py` | 837 | the 26-tool catalogue every action goes through |
| `sql_agent/reasoning.py` | 779 | Observation, `decide_next`, error taxonomy, `filtered_names` |
| `sql_agent/tools/planner.py` | 631 | the six actions, candidate resolution, validation |
| `sql_agent/tools/interpreter.py` | 614 | the reading: wants / people / camera / shape |
| `sql_agent/tools/agent_loop.py` | 558 | the model-driven tool loop and its budgets |
| `sql_agent/security/sql_guard.py` | 513 | AST guard, pipeline scope rewriting |
| `sql_agent/dialogue_state.py` | 519 | dialogue fields; `apply_delta` is the only mutation door |

## INVARIANTS YOU MUST NOT BREAK

- **Privacy in logs.** SQL text, prompts and rows never go to the log. The
  audit line carries hashes only; SQL lives in the history row's metadata.
- **The knowledge base holds only verified examples.**
  `SQL_AGENT_LEARN_FROM_QUERIES` stays false. Never let a replay or a test
  teach the bot. If you add seeds, execute each one against the live schema
  first and check the rows against a hand-computed truth.
- **Configuration has one door.** Application code reads `config.py`; it never
  touches `os.environ`. Guard rules take explicit `env=` / probe arguments.
- **The database role is read-only** (`fr_readonly`), and every generated query
  is rewritten to the caller's assigned cameras. Never widen a scope to make a
  query work.
- **Opik tracing is development-only.** Production refuses it at boot.
- **Never replay against the real admin account.** Use the dedicated replay
  account (a regular user with a restricted camera set) so scope bugs are
  visible and the admin's conversation is untouched.

## HOW TO RUN THINGS

Tests execute inside the API container:

```
docker exec -w /app face_recognition_api python -m pytest -q -p no:cacheprovider \
    tests/test_react_contract.py tests/test_reasoning_graph.py tests/test_agent_tools.py
```

Relevant suites: `test_react_contract`, `test_reasoning_graph`, `test_reasoning`,
`test_agent_tools`, `test_agent_planner`, `test_interpreter`,
`test_reader_subject_source`, `test_model_driven_tool_loop`,
`test_direct_answers`, `test_sql_scope_hint`, `test_prompt_label_leak`,
`test_entity_resolution_loop`, `test_correctable_sql_errors`, `test_sql_guard`,
`test_sql_agent_authz`, `test_seed_catalog_hygiene`, `test_mcp_tools`,
`test_loop_uses_mcp_catalogue`, `test_knowledge_base_switch`,
`test_document_and_follow_up_turns`, `test_agent_artifacts`,
`test_sql_agent_imports` (run this one first; an import error under
`sql_agent/` does not fail start-up, it silently unmounts the router).

**Never diagnose from logs.** Read the Opik trace. The self-hosted API:

```
GET http://localhost:5173/api/v1/private/traces?project_name=face-detector-sql-agent&size=20
GET http://localhost:5173/api/v1/private/spans?trace_id=<id>&project_name=face-detector-sql-agent&size=500
```

For each span read BOTH `input` and `output`. Most defects this system has
produced were invisible in the answer and obvious in a span's input — a prompt
that carried the wrong thing, or a hint that silently carried nothing.

Ask the bot through the streaming endpoint (`POST /api/sql-agent/query/stream`)
with a fresh session per question (`POST /api/sql-agent/session/new`), as the
replay account.

## HOW TO JUDGE AN ANSWER

Compute the truth yourself, in the replay account's camera scope, with a direct
`psycopg2` connection (the guarded manager refuses `users` and
`user_pipeline_access`). Compare figures, not vibes. An answer that names the
right entity and the wrong number is a failure.

## FAILURE CLASSES ALREADY SEEN (check each is still fixed, then look for more)

1. **A prompt heading read as a value.** With a request that named nobody, the
   SQL model took the heading `PLANNER PARAPHRASE` as a person and wrote
   `WHERE f.name = 'PLANNER'`. Headings are now lower-case prose. Audit every
   prompt in `agent_tools.py` for any bare capitalised token adjacent to
   content.
2. **Subject inheritance.** A follow-up composition imported the previous
   turn's person into an unrelated new request. The previous QUESTION may be
   named; the previous SUBJECT may not.
3. **The reader handing back the enrolled list.** Asked to "find a person" it
   returned twelve people — the whole ENROLLED block — which surfaced test
   identities as clarification candidates. A subject must be grounded in the
   message or the conversation.
4. **CTE column scope.** Ambiguous names across two CTEs; a column read from a
   CTE that does not emit it; an output alias used inside that same SELECT's
   `OVER (...)`. `_sql_scope_facts` measures these and feeds the repair prompt.
5. **A share whose denominator was filtered away.** The condition that defines
   the numerator was put in `WHERE`, so every group returned 100%. Also check
   integer division (`100` instead of `100.0`).
6. **A repair aid that degrades silently.** The correction hint stored the
   rejected SQL truncated to 600 characters; the parser could not read it, so
   the facts were empty and every trace looked like model weakness. Audit every
   truncation that feeds an analyser.
7. **Answer shape.** `report` (every row) versus `summary` (a computed figure)
   versus `answer` (one fact) is a model decision and is often wrong for
   one-fact follow-ups.
8. **Narration.** Check the narrator is given every row it is told to report,
   and that it states no total, range or average the rows do not contain.
9. **Repeat execution.** After a successful query the completion check can say
   "more to do"; a guard refuses the repeat and re-narrates. Verify it still
   fires and that the second execution is the deliberate re-run, not a new
   query.
10. **Retrieval is decisive.** With `SQL_AGENT_USE_KNOWLEDGE_BASE=false` the
    same model fails questions it answers correctly with a seed at 0.95
    similarity. When you judge "the model is weak", check what was retrieved.

## ENVIRONMENT TRAPS THAT INVALIDATE RESULTS

- Recreating the API container re-reads `docker/.env`. If a variable is missing
  the stack silently drops to a much smaller local model with tracing off, and
  your measurement means nothing. Check
  `docker exec face_recognition_api printenv LLM_DEV_PROVIDER SQL_AGENT_OPIK_ENABLED`
  before trusting a run.
- The Opik SDK is pinned in `requirements-dev.txt` but is not in the current
  image; a recreate removes it and tracing stops silently. A restart keeps it.
- Adding or editing seeds changes the catalogue hash and triggers a full
  re-seed on the next boot (several minutes). Wait for health before asking.

## WHAT TO DELIVER

For each finding:

1. **What is wrong**, in one sentence, as a user-visible consequence.
2. **The evidence**: the Opik trace id, the span name, and the exact field that
   proves it. Quote the value.
3. **Why it happens**, traced to a file and line.
4. **The fix**, obeying the rule above, with the test that fails before it and
   passes after.
5. **What you verified after**: the suites you ran, and a live replay of the
   original question with the answer compared against a hand-computed truth.

Rank findings by user impact: wrong figures first, wrong subject second,
refusing an answerable question third, everything else after.

If you find nothing in an area, say so explicitly and say what you checked.
Silence reads as "not looked at".
