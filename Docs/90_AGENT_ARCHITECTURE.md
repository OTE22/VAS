# 90 — Agent Architecture: planner, tools and artifacts

How the SQL agent decides what to do, and why the model is never the thing
that decides what it is allowed to do.

Related: [81_SQL_AGENT_QUERY_HISTORY.md](81_SQL_AGENT_QUERY_HISTORY.md) for
history and sessions, [74_SECURITY_CHECKLIST.md](74_SECURITY_CHECKLIST.md)
for the production checklist, [87_DATABASE_RELATIONSHIPS.md](87_DATABASE_RELATIONSHIPS.md)
for the schema.

---

## The problem this replaced

The agent routed every turn through a binary classifier: `CHAT` or
`SQL_QUERY`. That worked for questions and for nothing else. "Make that a
PDF", "make it Arabic", "the last report in English" and "same report but only
for camera 3" all fell into `CHAT`, and the chat model answered them — fluently,
confidently, and about a document it could not see.

Three things were missing, not one:

1. **Working memory.** Nothing recorded what the previous turn produced.
2. **Artifacts.** Exports were bytes in an HTTP response; nothing was stored,
   so there was no "last report" to refer to.
3. **An action vocabulary.** There were two destinations, so there was nothing
   else a turn could resolve to.

---

## The shape now

```
correct_name_typos → plan_action ──┬─ chat / clarify        → chat_response
                                   ├─ query_database        → check_schema → … (unchanged)
                                   ├─ modify_previous_query → check_schema → modify_sql → validate_and_fix_sql
                                   ├─ generate_document     → render_artifact
                                   └─ translate_artifact    → translate_artifact
```

The SQL chain is no longer a straight line. Two seams are conditional, and
both default to the edge they always had:

```
validate_and_fix_sql ─┬─ VALID / FIXED            → prepare_sql_for_execution
                      ├─ INVALID/PARTIAL/ERROR,
                      │    re-plan budget left     → observe_and_replan
                      └─ INVALID/…, budget spent  → chat_response  (honest failure)

execute_sql ───────────┬─ success                  → enrich_co_appearance
                      └─ correctable failure      → observe_and_replan

observe_and_replan ──┬─ corrected query          → check_schema
                      ├─ transient DB error       → prepare_sql_for_execution
                      ├─ corrected document       → render_artifact / translate_artifact
                      └─ clarify or give up       → chat_response
```

`plan_action` sits exactly where `classify_intent` did. The SQL chain below
`check_schema` is untouched, and a modified query rejoins it at
`validate_and_fix_sql` — the same AST authorization guard, by the same edge.

### The planner states intent; Python holds authority

This is the rule the whole design rests on. Every turn runs three stages:

1. **Deterministic resolution.** Python builds a closed candidate set from
   working memory and the caller's own artifacts. An explicit UUID in the
   user's text is accepted only if it is already in that set.
2. **The model chooses.** It picks one action and, at most, points at one of
   the candidates it was handed. It is never asked to remember or rediscover
   state.
3. **Deterministic validation.** Every field is re-checked: the action against
   a fixed vocabulary, `format`/`language` against allow-lists, and
   `artifact_id` against the candidate set — then against the **database**.

There is no field in which the planner can return SQL, a file path, a user id
or an authorization decision. An `artifact_id` it names that was not offered is
discarded, and ownership is re-checked by `get_owned_artifact` before anything
is read. A model that hallucinates another user's document id gets a
clarification, not the document.

### Failure is not silence

A planner failure on an ordinary question falls back to the previous
classifier, so the worst case equals the behaviour that shipped before. But a
failure on a request that is clearly *about* state we hold — a short,
fragmentary command in a session that already has a result or a document —
becomes a **clarification**, never small talk. Answering "make it Arabic" as
conversation is the exact bug this replaced.

### Observing what the action produced

Until this loop existed, a turn was decide → act → narrate. Nothing looked at
the outcome, so a rejected query, an empty result or a stale document
reference all reached the user as if they were the answer.

`observe_and_replan` closes it, and holds no authority of its own:

1. **`reasoning.build_observation(state)`** — a bounded, factual record of
   what happened: action, success, error type, row count, result/artifact
   ids, and a sanitized reason of at most 200 characters. Every field is
   derived in Python from `AgentState`. The model contributes nothing, which
   is the only reason the decision below can be trusted.
2. **`reasoning.check_invariants`** — refuses a "success" that contradicts the
   action's contract. A document action reporting success with no registered
   artifact is BUGGY; narrating it is how a system tells somebody their report
   is ready when it does not exist. This defends against our own executors,
   not only against the model.
3. **`reasoning.decide_next`** — a fixed table over the error taxonomy:
   ANSWER, REPLAN, CLARIFY or RETRY_EXECUTION, subject to the budgets.
4. On REPLAN only, **one** model call, whose proposal goes through
   `validate_call` and `action_to_planned` — the same validation as any other
   action.

Three independent, deterministic bounds, all settings:

| Setting | What it bounds |
|---|---|
| `SQL_AGENT_MAX_REASONING_STEPS` | look-ups and re-plans, sharing one budget |
| `SQL_AGENT_MAX_REPLANS` | corrective re-plans (each needs a listed trigger) |
| `SQL_AGENT_MAX_EXECUTION_RETRIES` | retries of the SAME SQL after a transient DB error |

Termination is arithmetic: the routing functions read the counters, and only
`observe_and_replan` increments them. A confused model cannot loop.

**Re-planning is corrective, not repetitive.** Each attempt is fingerprinted
(`action` + normalized arguments); a proposal matching one that already failed
this turn is refused in Python, whatever the prompt said. The single exception
is SQL regeneration, and it is honest: the rejected query and the validator's
reason are fed back through `sql_correction_hint`, so the second attempt has
strictly more to work with than the first. Without that feedback the retry
would be a coin flip and the refusal would be right.

**A transient database error is not reasoning.** A dropped connection retries
the same SQL on its own budget, with no model call — a brief DB hiccup must
not lobotomize the turn.

**Zero rows is usually the answer.** "How many detections yesterday?" answered
with 0 is correct. It is questioned only when the task was narrowed to a named
person, where the name is the likelier culprit — and then the agent asks which
person is meant rather than re-planning around an inconvenient truth.

### A malformed query is not an intrusion

The AST guard prefixes every denial with `Security: `. Enforcement used to
match that prefix, so a model writing broken SQL was treated exactly like
`DELETE FROM users`: CRITICAL audit line, account marked for blocking, 403
returned. Live on 2026-08-30 a user typed **"hello"**, the model emitted
`SELECT statement."}`, and that is what they got.

Denial codes are now classified in `sql_guard`, beside the `_deny` calls that
emit them:

- `ENFORCEABLE_CODES` — genuine forbidden-operation attempts. Refuse, audit,
  enforce. Behaviour unchanged.
- `MALFORMED_CODES` (`PARSE_ERROR`, `EMPTY`, `TOO_COMPLEX`) — mistakes to
  correct. The reasoning layer may re-plan them; no security event.
- `INFRASTRUCTURE_CODES` (`PARSER_UNAVAILABLE`) — our dependency, never the
  user's doing.

`is_enforceable` fails **closed** on an unrecognised code, and a test asserts
that every emitted code is deliberately classified — silently enforcing on a
code nobody classified is how "hello" became a security incident.

### The answer states only what happened

The narrative model is handed a FACTS block built from the Observation, plus
the standing truth that this assistant can only read. Without it the chat node
had the transcript and nothing else — and one turn after a DELETE request was
correctly refused, it answered "hello" with:

> "I've deleted every detection row from the database. The database is now
> empty, and there are no detection events recorded."

Nothing was deleted; nothing could be. Telling an operator their surveillance
database has been emptied is the worst false statement this system can make,
and it came from prose written with no grounding in what actually ran.

### No chain of thought, anywhere

Reasoning prompts ask only for structured JSON, so there is no private
deliberation to leak by design. The trace is fields only:

```
[REASONING] conversation=… turn=… mode=… replans=…
            observation={action=… success=… rows=… error=… artifact=…}
            decision=… reason=… next=…
```

The Observation is asserted never to carry rows, SQL or narrative — it goes
into both a model's context and a log line, and either is a place surveillance
data must not appear.

### Two maps, and why both exist

`_ACTION_TO_INTENT` keeps `intent` populated for the code and tests that read
it; document actions map to `CHAT` there for compatibility.
`_ACTION_TO_NODE` is what routing and the audit line actually use. Read the
wrong one and every translation is recorded as a chat turn.

---

## Artifacts

A document the agent generated is a row in `agent_artifacts` plus a file under
`ARTIFACTS_DIR` (`<STORAGE_DIR>/artifacts`, a derived path — see
[36_CONFIGURATION_GUIDE.md](36_CONFIGURATION_GUIDE.md)).

**Lineage is the point.** Each row records:

| Column | Why |
|---|---|
| `source_content` | the narrative it was rendered FROM, so translation is text→text and a PDF is never parsed back |
| `source_sql` | the originating query, so "same report but camera 3" can modify the right one |
| `source_message_id`, `source_result_id` | immutable pointers to where it came from |
| `parent_artifact_id` | set ONLY when genuinely derived (a translation, a re-filter) |

`parent_artifact_id` must never be filled in with "the newest artifact around".
Doing that made every new report claim an unrelated one as its parent and sent
later references to the wrong source query — lineage that records a
relationship which never happened is worse than none.

### One persistence path

`export_builders.render_and_register` is the only way an artifact is created.
The HTTP export endpoints and the graph nodes both use it, so there are never
two sets of semantics to drift apart. It writes the **file first, then the
row**, and on failure unlinks the file *and expunges the pending row from the
caller's session* — otherwise the request's own commit would resurrect a row
pointing at a file that no longer exists.

Graph nodes are synchronous and persistence needs the database, so nodes
produce bytes (`artifact_payload`) or a request (`translation_request`) and the
async API layer finishes the work — identically on the REST and streaming
transports.

### Downloading

`GET /api/sql-agent/artifacts/{id}` — and **never** `GET /storage/{path}`,
which authenticates but performs no ownership check at all.

- ownership is answered by the database, scoped to the caller;
- missing, foreign and soft-deleted all return one byte-identical `404`, so an
  id cannot be used to probe for another user's reports;
- the path comes from the row, re-anchored inside `ARTIFACTS_DIR` after
  `realpath`;
- an administrator is refused another user's document like anyone else.

### Retention

Artifacts expire on `DATA_RETENTION_DAYS`, the same window as the detections
they were rendered from — a report must not outlive its source data. The file
goes first, then the row; stale `.incoming/*.part` files from an interrupted
render are swept after a day.

---

## The turn lifecycle: one contract, three transports

REST, SSE and WebSocket each used to hand-roll their own pre/post-turn
sequence — and the sequences drifted. The artifact index and durable memory
loaded only on REST, so on SSE (the transport the browser uses) "same report
but camera 3" silently bound to *recency* instead of lineage, and WebSocket
discarded rendered documents outright. The drift is the bug class; the
lifecycle is its fix (`sql_agent/api/routes.py`):

| Function | When | Does |
|---|---|---|
| `prepare_turn()` | before the graph runs, inside the user's lock | artifact index + source-SQL map, durable memory |
| `complete_turn_document()` | at the transport's completion boundary, before the terminal event is serialized | finishes pending render/translation so the client learns of the document in the same event |
| `finalize_turn()` | after the terminal event | shielded history persist + the working-memory row pointer |

A transport may add framing around these; it may **not** reimplement them.
The parity tests in `tests/test_agent_e2e.py` pin that the transports resolve
the same artifact, provenance and language for the same reference.

Related rules that live beside the lifecycle:

- **Lock lifetime ≠ agent lifetime.** The per-user lock survives its agent's
  LRU eviction while held or awaited (`_maybe_release_user_lock`); dropping a
  held lock let two turns run concurrently for one user with 11+ active users.
- **REST idempotency**: `POST /query` accepts a `request_id` with the same
  exactly-once contract SSE/WS always had; a duplicate is refused with
  `409 DUPLICATE_REQUEST`. `AGENT_BUSY` removes the entry so an honest retry
  is not 409'd.
- **A stale lineage pointer costs the lineage, never the artifact.** Working
  memory can outlive the history row it names (retention, user deletion).
  `render_and_register` retries once with the optional lineage references
  stripped rather than letting a foreign-key violation eat the document —
  and every later document, since the stale pointer persists.
- **Metrics** (`sql_agent/observability.py`): planner actions/fallbacks,
  modify-provenance source (`artifact` vs `last_result` — the
  fallback-to-recency detector), document completions, memory-load failures,
  agent/lock evictions.

## Auditing

One line per turn:

```
[AGENT_AUDIT] user_id=… conversation=… action=… source=… confidence=…
              resolution=… executed=… artifact=… result=… outcome=…
```

It records the **action taken and the rows touched** — never the prompt, the
user's text, or the model's reasoning. An audit trail of model thoughts is
unverifiable and copies surveillance questions into a second place; an audit
trail of executed tools is evidence.

---

## Starting a clean conversation

`POST /api/sql-agent/session/new` clears the caller's conversation: the
transcript and the working context — dialogue state, task history, last
result, artifact references. The user's artifacts and query history are NOT
touched; those are their data and live in the database.

It did not always. For a signed-in user `start_session()` RELOADS the
persistent `user_{id}_main` session, so the endpoint answered

```json
{"success": true, "message": "New session created"}
```

and handed back the same accumulated conversation. There was no way, from
inside the product, to start over.

**Deleting the session file does not reset anything.** The agent and its
`ConversationMemory` are cached per user in the API process (`_user_agents`,
routes.py), and the cached object holds the state in memory and writes it
straight back on the next turn. Anything that needs a clean conversation has
to go through the endpoint, which acts on that cached object — or restart the
container, which evicts the cache.

This is not a footnote. A whole day of measurement of the SSE acceptance test
was invalid because "delete the session file" was used as the isolation step:
a captured prompt showed the FIRST question of a supposedly fresh run arriving
with `active_task = 'Filter the query to only include camera 3.'` from the run
before, which makes `modify_active_query` a reasonable answer to "how many
cameras are registered?" and the reading meaningless.

### The reset must REPLACE, not merge

`save_session` is a deliberate read-merge-write: it preserves unknown
top-level keys and merges the working context so that a concurrent turn
cannot lose a field it just set. So emptying `self.working_context` and
calling save writes the OLD context back. `reset_session` replaces the
document instead, and a test pins the sequence — reset, then an ordinary
save, then assert nothing stale returns.

`migrate_working_context` re-adds the schema keys set to `None` on that next
save. That is fine: an empty slot is not stale state. The test asserts on
VALUES rather than key presence for exactly this reason.

### Tests

`tests/test_agent_e2e.py` calls the endpoint in an autouse fixture. It uses
the product's own path rather than a test backdoor, which means the isolation
is exercised by the suite that depends on it. `chat_sandbox` remains
responsible for the DB rows; the two together are what makes an agent E2E
test reproducible.

---

## What a failure tells the user

A failed turn used to interpolate the raw driver error straight into the
reply:

```python
error_msg = query_result.get("error", "Unknown error occurred")
state["final_response"] = f"...retrieve that information: {error_msg}"
```

Postgres names the table and column it objected to; an AST denial carries the
rejected query itself. So a failure doubled as a description of the schema and
of what the system had tried to run — available to anyone who can make a query
fail, which is anyone who can type. Both messages were also hardcoded English,
so an Arabic conversation got an Arabic report when it worked and an English
apology when it did not.

The reply is now built from the Observation's **error category** — a closed
enum this codebase owns, mapped to a fixed phrase per language in
`_FAILURE_PHRASES`. Nothing from the database can reach a reply through that
path, because there is no filter to get wrong.

| category | what the user is told |
|---|---|
| `sql_invalid` | could not build a valid query — rephrase |
| `sql_generation_error` | ran out of time — try something simpler |
| `sql_forbidden` | not permitted; this assistant can only read |
| `sql_execution_error_transient` | briefly unavailable — try again |
| `sql_execution_error_permanent` | not answerable from the data as stored |
| `artifact_missing` / `artifact_forbidden` | that document is gone / not yours |
| `invariant_violation` | something went wrong here; no reliable answer |

A test fails if a new `ErrorType` arrives without wording in both languages —
otherwise a new category silently degrades to the generic apology, which is
the moment the user stops learning anything.

**`_sanitize` is not a redactor.** Its docstring used to claim "never SQL,
never rows"; it only flattens newlines and clips to 200 characters, so
`column "cam" does not exist LINE 1: SELECT cam FROM detections` survives it
nearly whole. A user-facing message was written on the strength of that
promise and leaked exactly what it was meant to prevent. The detail is for
LOGS and for the model's own context, where an operator needs the real reason;
never for a reply. The docstring now says so.

**Zero rows is worded as an answer, not an apology.** "How many detections
yesterday" answered with none is correct, and dressing it as a failure teaches
people to distrust a true result. The case where zero rows IS suspicious — a
task narrowed to a named person — is caught earlier by the reasoning layer,
which asks which person is meant.

---

## Reading one user's history

`get_query_by_id(db, query_id, user_id=None)` gated its ownership filter on
`if user_id:`. `None` returned any user's row, and so did `0`. The single
caller passes a real `current_user.id`, so nothing was exposed — the hazard
was the shape: the SAFE behaviour was the caller's job and the DANGEROUS one
was the default, and the next caller to pass a missing id would have read
another user's surveillance questions with no error.

`get_query_by_id_for_user(db, query_id, user_id)` is the accessor now: the
filter is unconditional, `user_id` has no default, and a falsy id returns
`None` **before touching the database**. The old name delegates to it and its
`user_id` is required too, so forgetting is a `TypeError` at the call site
rather than a quiet cross-user read. This is the contract `delete_query` has
always had.

A falsy or foreign id returns the same `None` as a missing row. "Not yours"
and "not there" must be indistinguishable — the difference is itself a
disclosure about other people's queries, which here are questions about named
individuals.

The AST is what pins this (`test_the_scoped_accessor_filters_on_owner_unconditionally`),
not a text search: the first version of that test grepped for `if user_id:`
and matched the docstring explaining why it was wrong.

---

## Watching it work

Three log vocabularies, all at INFO, all sanitized. Together they answer
"is it really calling tools, and is it really reasoning?" without anyone
having to trust an assertion.

```
docker logs -f face_recognition_api | grep -E "TOOL_LOOP|REASONING|AGENT_AUDIT"
```

A real two-turn conversation looks like this:

```
[TOOL_LOOP] start model=meta/llama-3.2-11b-vision-instruct mechanism=native tools=11 budget=3
[TOOL_LOOP] step=0 mechanism=native proposed=query_database args={question=<str:54>}
[TOOL_LOOP] committed to query_database after 0 look-up(s) via native calling
[AGENT_AUDIT] action=query_database source=planner resolution=tools:1/CONTEXTUAL executed=check_schema
[REASONING] validation=VALID -> executing
[REASONING] mode=CONTEXTUAL replans=0 observation={action=query_database success=True rows=1 …} decision=ANSWER next=enrich_co_appearance
[DIALOGUE_STATE] context_version=0->2 action=query_database delta={REPLACE active_task …}

[TOOL_LOOP] step=0 mechanism=native proposed=list_cameras args={}
[TOOL_LOOP] step=0 lookup=list_cameras -> ok cameras[16] count note
[TOOL_LOOP] step=1 mechanism=native proposed=query_database args={question=<str:32>}
[TOOL_LOOP] committed to query_database after 1 look-up(s) via native calling
```

`mechanism` is the one people most often want and could not previously get:
**native** means the specs went over the OpenAI-compatible `tools` payload
and the model replied with `tool_calls`; **prompted** means the model ignored
that payload, so the same specs are rendered into the prompt and parsed back
out of JSON. Both converge in `parse_tool_response`, so nothing downstream
behaves differently — but which one ran is not something you should have to
infer. It is stated on every turn, not once per process.

`[REASONING]` now fires on successful turns too. Deciding NOT to intervene is
still a decision, and while it was silent the reasoning layer was invisible on
exactly the turns anyone watches.

### What the trace does not contain

Argument **values** are shapes: `question=<str:54>`, not the question. A
`resolve_person` argument is somebody's name and a `question` is the user's
own words, and the audit rule here has always been that neither goes into a
log file. Set `SQL_AGENT_TRACE_CONTEXT=1` in **development** to get the
values, together with the prompt-envelope trace.

### What is not covered

The observe/re-plan edges wrap the **SQL path only**. A turn that ends in
`chat_response`, `render_artifact` or `translate_artifact` goes straight to
END, so it produces no `[REASONING]` line — there is no observation of a
rendered document beyond the invariant check inside the node itself. If you
ask why a document turn shows no reasoning trace, that is why, and it is a
gap rather than a decision.

---

## Choosing the action: the prompt is the lever

Every follow-up in this system routes through one choice — which action tool
the model commits to — and that choice is governed almost entirely by the
default stated in `TOOL_SYSTEM_PROMPT`. Two failures, in sequence, made the
shape of the trap clear.

**First**, the prompt said when to clarify and nothing about when to answer.
The model clarified everything:

```
[TOOL_LOOP] step=0 proposed=ask_clarifying_question
-> "Do you want the previous result as a document?"      # for "how many cameras are registered?"
```

**Then**, fixing that, the default was written as "call query_database" — one
tool named where a choice was needed. The reflex simply moved:

```
[TOOL_LOOP] step=0 proposed=query_database args={question=<str:68>}
[REASONING] observation={action=query_database success=True rows=0 …}
-> "I searched the database but found no matching records."   # for "make that a PDF"
```

The context block was never the problem — it said `last_result: 16 row(s)`
plainly in both cases. The model had what it needed and the prompt pointed it
elsewhere. **A default that names one tool creates a reflex for that tool.**

The default now states the CHOICE, keyed on what the request is about:

| the request is about | tool |
|---|---|
| the DATA, a new question | `query_database` |
| the SAME question, something changed | `modify_active_query` |
| the result just produced, as a file | `generate_document` |
| a file that exists, in another language | `translate_document` |

That distinction — the data, or the thing just produced — is the one the tool
vocabulary is already built around, so it is a structural guide rather than a
list of phrases. A real sequence:

```
"how many cameras are registered?"  -> query_database       -> 16
"make that a PDF"                   -> generate_document    -> artifact
"only camera 3"                     -> modify_active_query  -> narrowed, artifact-bound
```

Four `tests/test_agent_e2e.py` failures were traced to this and nothing else.
They were mistaken for structural problems for some time; they were the
prompt.

---

## Clarification is a last resort

`ask_clarifying_question` ends the turn before anything is tried, which makes
it the cheapest tool for a model under pressure and the most damaging one to
over-use. Observed on a clean session:

```
[TOOL_LOOP] step=0 proposed=ask_clarifying_question
-> "Do you want the previous result as a document?"
```

for the question "How many cameras are registered?" — about a previous result
that did not exist. The SQL chain was never reached, so no reasoning edge
could run. **The agent looked like it was not thinking because it was quitting
before it started.**

The prompt and the tool description now state that answering is the default,
but the part that holds is enforced in Python: an opening clarification — one
proposed before **anything has been looked up** — is rejected, and the model is
told why so it can choose again through the loop's existing rejection path.

The first version of that guard also required an empty session, and so it
almost never fired: `artifact_index` is non-empty for anybody who has ever
generated a document. Whether the claim has been CHECKED is the property that
matters, and it does not depend on session contents.

The guard is structural. It reads the TRACE — has anything been tried? —
never what the request says. The same words are refused at step 0 and allowed
after a look-up, which is what keeps it from becoming the keyword rule it
replaced. A genuinely ambiguous request costs one step out of three; the
alternative is answering a plain question with a question.

---

## Model routing

`SQL_MODIFICATION` is its own `TaskType`, routed with the SQL specialists.
It is deliberately *not* `SQL_REPAIR`: rewriting a valid query under a new
constraint is not fixing a broken one, and overloading the repair prompt tells
the model its input is wrong when it is not.

In development a remote provider (NIM) may lead SQL generation; production
refuses one at boot, so the local specialist leads there. Either way the local
model stays in the routing list as a fallback.

---

## Things that will bite you

- **Undeclared state keys are dropped.** LangGraph merges each node's result
  against `AgentState`; anything not declared there vanishes with no error.
  This has caused three separate "impossible" bugs.
- **`SQLAgent.query()` returns a tuple** only when there is work for the API
  layer. Adding a new kind of pending work means adding it to that condition,
  or it is silently discarded.
- **A fallback can hide a total failure.** `modify_sql` falls back to the
  unmodified query so the user sees something; `sql_was_modified` exists so
  that fallback is distinguishable from a real rewrite. It once passed its
  own gate while never modifying anything.
- **The reasoning loop's default path is the old path.** Both new routers
  return the pre-existing edge unless a trigger fires AND budget remains. If
  that changes, a successful turn stops being byte-identical to the graph that
  shipped, and every "unchanged behaviour" claim above expires.
- **INVALID SQL must never execute, budget or no budget.** Exhausting the
  re-plan budget means answering honestly — never "run it anyway". `PARTIAL`
  counts as invalid: it means the fix also failed to validate and the original
  bad query was kept.
- **Response headers are lowercase on the wire.** `dict(headers)["X-Artifact-Id"]`
  fails on a response that has it.
