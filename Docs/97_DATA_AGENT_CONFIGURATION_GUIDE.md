# 97 — Data Agent Configuration Guide: development and production, setting by setting

This guide explains **how to configure the SQL bot / data agent** in the two
deployment modes, what every setting does, what the boot guard checks, and
how to verify a deployment. It complements `96_LOCAL_DATA_AGENT_ARCHITECTURE.md`
(the design) and `93_PRODUCTION_RUNBOOK.md` (the wider platform).

Read section 1 first: it explains the one idea everything else follows.

---

## 1. The one idea: two modes, one switch, fail-closed

There are two modes. They are selected by `ENVIRONMENT`, and the offline
policy follows it:

| | `ENVIRONMENT=development` | `ENVIRONMENT=production` |
|---|---|---|
| `OFFLINE_MODE` if you leave it empty | `false` (online allowed) | **`true` (offline enforced)** |
| Cloud LLM (`LLM_DEV_PROVIDER=nim`, `LLM_PROVIDER=nvidia_cloud`) | allowed | **boot refused** |
| Any endpoint that is not internal (LLM, embeddings, MCP, Milvus, STT, telemetry) | allowed | **boot refused** |
| `ALLOW_EXTERNAL_APIS`, `ALLOW_MODEL_DOWNLOADS`, `ALLOW_EXTERNAL_TELEMETRY` | may be `true` | **must be `false`** |
| Opik tracer (`SQL_AGENT_OPIK_ENABLED`) | optional | **boot refused** |
| Missing local model file (`EMBEDDING_MODEL_PATH`, detection/recognition weights) | ignored | **boot refused** |
| Shared database role for generated SQL | allowed | **boot refused** |

"Boot refused" means the API process exits with code **78** before serving
a single request and prints every violation at once, for example:

```
unsafe production configuration: OFFLINE_EXTERNAL_LLM_ENDPOINT, OFFLINE_ALLOW_MODEL_DOWNLOADS
  [OFFLINE_EXTERNAL_LLM_ENDPOINT] LLM_BASE_URL
    PRODUCTION OFFLINE POLICY VIOLATION: external LLM endpoint detected: https://integrate.api.nvidia.com/v1
    fix: Point LLM_BASE_URL at an internal service (a Compose service name, a private address or localhost) ...
```

You never have to remember which cloud services are forbidden: the rules
live in `backend/security/offline_policy.py` and run inside the existing
config guard (`backend/security/config_guard.py`). Two lines are all that
separate the modes at the top of a production env file:

```
ENVIRONMENT=production
OFFLINE_MODE=true
```

What counts as **internal**: `localhost`, `127.0.0.1`, `host.docker.internal`,
any private IPv4/IPv6 range, a single-label Docker/Compose service name
(`ollama`, `vllm`, `milvus`, `mcp-sql`), and names ending in `.local`,
`.internal`, `.lan`, `.localdomain`, `.intranet`. Everything else is the
internet. If your air-gapped network uses a fully-qualified name that does
not match (say `gpu01.corp.example.com`), list it in
`OFFLINE_ALLOWED_HOSTS=gpu01.corp.example.com` — that is an operator's
statement that the host is inside the gap, and it is logged as such.

---

## 2. Where configuration lives

| File | Purpose | Committed? |
|---|---|---|
| `config.py` | The single source of truth: every setting, its default, its description. The app reads settings **only** from here. | yes |
| `.env` (repo root) | Development values, bind-mounted into the dev containers. Start from `.env.example`. | no |
| `docker/.env` | Values Compose interpolates (`${VAR}`) and the production `env_file` for the API. Start from `docker/env.production.example` in production. | no |
| `secrets/` | Docker secrets: `jwt_secret`, `bootstrap_admin_password`, `webhook_api_keys`. Read through `*_FILE` settings. | no |
| `docker/docker-compose.cpu.yml` | Development stack. | yes |
| `docker/docker-compose.prod.yml` (+ `prod.gpu.yml`) | Production stack, profiles `vllm`, `mcp-sql`, `milvus`. | yes |

Precedence: **process environment (Compose) > `.env` > declared default**.
A value set in Compose always wins over the same key in a bind-mounted
`.env`.

Secrets rule: a credential never goes into a committed file. Every
credential setting has a `*_FILE` twin that reads a mounted Docker secret:
`JWT_SECRET_KEY_FILE`, `DATABASE_URL_FILE`, `SQL_AGENT_DB_PASSWORD_FILE`,
`LLM_API_KEY_FILE`, `WEBHOOK_AUTH_TOKEN_FILE`, … Prefer those in production.

---

## 3. The settings, group by group

Defaults are the values in `config.py`. "Guard" says what production does.

### 3.1 Mode

| Setting | Default | Meaning | Guard (production) |
|---|---|---|---|
| `ENVIRONMENT` | `production` | `development` or `production`. Drives every fail-closed rule. | — |
| `OFFLINE_MODE` | `""` | Empty = follow `ENVIRONMENT`. `true` forces offline enforcement even in development (useful to rehearse a production config). `false` in production is a violation. | `OFFLINE_MODE_DISABLED_IN_PRODUCTION` |
| `OFFLINE_ALLOWED_HOSTS` | `""` | Comma-separated hosts inside the air gap that are not private addresses or service names. | honoured by the URL rules |
| `ALLOW_EXTERNAL_APIS` | `false` | Development statement that hosted inference may be used. | must be false |
| `ALLOW_MODEL_DOWNLOADS` | `false` | Development statement that model files may be fetched. | must be false |
| `ALLOW_EXTERNAL_TELEMETRY` | `false` | Development statement that a hosted tracer may be used. | must be false |

### 3.2 Inference (the LLMs)

The agent uses up to three roles: a **general/chat model** (reads each
turn, chats, narrates), a **SQL specialist** (generates and repairs SQL)
and an optional **reader** (the per-turn interpretation). Each provider
maps those roles to its own model names.

| Setting | Default | Meaning | Guard |
|---|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` (local, default) · `vllm` · `nim_local` (both OpenAI-compatible local servers) · `nvidia_cloud` (hosted, development only). | cloud values refused; unknown values refused |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | The Ollama server. | must be internal |
| `OLLAMA_MODEL` | `llama3.2:3b` | General/chat model in Ollama. | — |
| `OLLAMA_SQL_MODEL` | `""` | SQL specialist in Ollama; empty = `OLLAMA_MODEL`. The dev stack uses an Arctic-Text2SQL GGUF. | — |
| `OLLAMA_INTERPRETER_MODEL` | `""` | Reader in Ollama; empty = `OLLAMA_MODEL`. | — |
| `OLLAMA_TEMPERATURE` / `OLLAMA_TIMEOUT` | `0.1` / `300` | Sampling temperature; per-call timeout (seconds) for every provider. | — |
| `LLM_BASE_URL` | `""` | Base URL of the vLLM / local NIM server, e.g. `http://vllm:8000/v1`. Required for `vllm` and `nim_local`. | must be internal |
| `LLM_MODEL` | `""` | Model id served there (general/chat and reader). Required for `vllm` / `nim_local`. | — |
| `LLM_SQL_MODEL` | `""` | SQL specialist served there; empty = `LLM_MODEL`. | — |
| `LLM_API_KEY` / `LLM_API_KEY_FILE` | `""` | Optional bearer for a local NIM (vLLM ignores it). Never logged. | presence only warned |
| `LLM_DEV_PROVIDER` | `""` | `nim` enables the NVIDIA **hosted** endpoint with `NVIDIA_NIM_*`. Development only. | `LLM_EXTERNAL_PROVIDER_IN_PRODUCTION` + `OFFLINE_DEV_PROVIDER_SET` |
| `NVIDIA_NIM_BASE_URL` / `NVIDIA_NIM_API_KEY` / `NVIDIA_NIM_MODEL` / `NVIDIA_NIM_SQL_MODEL` / `NVIDIA_NIM_INTERPRETER_MODEL` / `NVIDIA_NIM_TIMEOUT` | hosted defaults | The hosted development provider's models. | key presence warned; provider refused |

How selection works (`sql_agent/llm/registry.py`): Ollama models are always
registered. When `LLM_PROVIDER` is `vllm` or `nim_local` **and** both
`LLM_BASE_URL` and `LLM_MODEL` are set, the local OpenAI-compatible models are
registered too and **preferred** for every task, with Ollama as the
fallback. When `LLM_DEV_PROVIDER=nim` (development only), the hosted models
are preferred over both. The gateway adds retries, a circuit breaker and
per-model latency metrics whichever provider is active.

### 3.3 Embeddings and the vector store

| Setting | Default | Meaning | Guard |
|---|---|---|---|
| `EMBEDDING_PROVIDER` | `local` | `local` = the ONNX MiniLM bundled with Chroma (no network). Any non-local value is refused offline. | `OFFLINE_EXTERNAL_EMBEDDING_PROVIDER` |
| `EMBEDDING_BASE_URL` | `""` | A local embedding service, if you run one. | must be internal |
| `EMBEDDING_MODEL_PATH` | `/home/appuser/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx` | Where the embedding model must exist offline. | `OFFLINE_ARTIFACT_MISSING` if absent |
| `VECTOR_STORE` | `chroma` | `chroma` (embedded, default, unchanged behaviour) or `milvus`. | — |
| `MILVUS_URI` | `""` | e.g. `http://milvus:19530` when `VECTOR_STORE=milvus`. | must be internal |
| `CHROMADB_PATH` | `./sql_agent/chromadb_data` | Chroma's persistent directory (a volume in Docker). | — |
| `RAG_TOP_K` | `5` | Examples retrieved per question. | — |
| `SQL_AGENT_LEARN_FROM_QUERIES` | `false` | Whether answered turns are added to the knowledge base. Keep it off: only verified seeds belong there. | — |

The knowledge base holds ~1070 verified question→SQL seeds that load
automatically (a hash detects changes; loading is batched, about 4 minutes
for a full re-seed on CPU). Chroma computes embeddings locally with the
ONNX model at `EMBEDDING_MODEL_PATH`; **that file must be in the offline
bundle** — Chroma would otherwise try to download it on first use.

### 3.4 MCP tools and the orchestrator

| Setting | Default | Meaning | Guard |
|---|---|---|---|
| `MCP_SQL_URL` | `""` | Empty = the catalogue runs in-process (always the case for the API). Set to `http://mcp-sql:9901/mcp` only to advertise the `mcp-sql` profile's server to other local agent hosts. | must be internal |
| `AGENT_ORCHESTRATOR` | `langgraph` | `langgraph` (built-in ReAct loop) or `nemo` (NeMo Agent Toolkit over the catalogue). `nemo` without the toolkit installed logs the reason and runs LangGraph. | — |

Both orchestrators call the same 26-tool catalogue; the loop's retrieval and
execution already go through `vanna.retrieve_sql_examples` and
`database.execute_readonly`, so the audit lines, metrics and Opik spans are
identical whichever orchestrator runs.

### 3.5 Database access for generated SQL

| Setting | Default | Meaning | Guard |
|---|---|---|---|
| `SQL_AGENT_DB_USER` | `""` | The SELECT-only role that executes generated SQL (`fr_readonly`, created by `db/roles.sql`). | `SQL_AGENT_DB_ROLE_SHARED` if it equals the app role |
| `SQL_AGENT_DB_PASSWORD` / `SQL_AGENT_DB_PASSWORD_FILE` | `""` | Its password; prefer the secret file. | — |
| `SQL_AGENT_TOTAL_TIMEOUT` | `300` | Wall-clock budget for one turn (seconds). | — |
| `SQL_AGENT_MAX_ACTIONS_PER_TURN` | `3` | Tool actions the loop may take per turn. | — |
| `SQL_AGENT_MAX_MODEL_CALLS` | `24` | Model calls per turn. | — |

The AST guard (SELECT-only, allow-listed tables, camera scope, LIMIT) runs
on every statement regardless of these; the role is the last line of
defence at the database itself.

### 3.6 Voice

| Setting | Default | Meaning | Guard |
|---|---|---|---|
| `STT_PROVIDER` | `none` | `none` · `local` · `whisper` · `faster_whisper` · `riva` · `external` (development only). | non-local refused |
| `STT_BASE_URL` | `""` | The STT service endpoint, if any. | must be internal |
| `STT_MODEL_PATH` | `""` | Local STT model file, checked at boot when STT is on. | `OFFLINE_ARTIFACT_MISSING` |

There is no speech-to-text in the repository today; these settings define
the contract a local STT service plugs into and what the bundle must carry.

### 3.7 Telemetry and tracing

| Setting | Default | Meaning | Guard |
|---|---|---|---|
| `OTEL_EXPORTER_ENDPOINT` | `""` | An OTLP collector for traces/metrics. | must be internal |
| `SQL_AGENT_OPIK_ENABLED` | `false` | The Opik tracer: every node, model call, tool call (`mcp:<tool>` spans), prompts and rows. **Development only.** | `OFFLINE_OPIK_ENABLED` |
| `OPIK_URL_OVERRIDE` | `http://host.docker.internal:5173/api/` | Self-hosted Opik on the developer machine. | must be internal (and disabled anyway) |
| `OPIK_PROJECT_NAME` / `OPIK_WORKSPACE` / `OPIK_API_KEY` | `face-detector-sql-agent` / `default` / `""` | Opik project; the key only for the hosted service. | key presence warned |

Prometheus and Grafana need no agent-specific settings; the metrics are
exported on the API's `/metrics` as before.

### 3.8 Offline bundle and library switches

| Setting | Default | Meaning | Guard |
|---|---|---|---|
| `OFFLINE_BUNDLE_MANIFEST` | `""` | Path of the verified bundle's `manifest.json`. When set, it must exist. | `OFFLINE_BUNDLE_MANIFEST_MISSING` |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` (process env) | set to `1` in `docker-compose.prod.yml` | Tell the model libraries never to try the hub. | warned if unset |

---

## 4. Development: step by step

### 4.1 Minimal local setup (Ollama, everything local, online allowed)

1. Copy the example and edit the "Deployment mode & offline policy" block:

   ```
   cp .env.example .env
   ```

   ```
   ENVIRONMENT=development
   OFFLINE_MODE=false
   LLM_PROVIDER=ollama
   OLLAMA_BASE_URL=http://ollama:11434
   OLLAMA_MODEL=qwen2.5:1.5b
   OLLAMA_SQL_MODEL=hf.co/mradermacher/Arctic-Text2SQL-R1-7B-GGUF:Q4_K_M
   OLLAMA_INTERPRETER_MODEL=
   EMBEDDING_PROVIDER=local
   VECTOR_STORE=chroma
   AGENT_ORCHESTRATOR=langgraph
   ALLOW_EXTERNAL_APIS=true
   ALLOW_MODEL_DOWNLOADS=true
   ALLOW_EXTERNAL_TELEMETRY=false
   ```

2. Start the development stack:

   ```
   docker compose -f docker/docker-compose.cpu.yml up -d
   ```

   The first start downloads the Ollama models (`ollama pull` happens inside
   the container) and Chroma's ONNX embedding model; development allows it.

3. Check readiness (`offline_policy` is reported but not required in
   development):

   ```
   curl -s http://localhost/health/ready | python -m json.tool
   ```

### 4.2 Faster iteration with the NVIDIA hosted endpoint (development only)

Add to `.env` (the key comes from build.nvidia.com; never commit it):

```
LLM_DEV_PROVIDER=nim
NVIDIA_NIM_API_KEY=nvapi-...
NVIDIA_NIM_MODEL=meta/llama-3.2-11b-vision-instruct
NVIDIA_NIM_SQL_MODEL=openai/gpt-oss-120b
NVIDIA_NIM_INTERPRETER_MODEL=
ALLOW_EXTERNAL_APIS=true
```

The hosted models are then preferred for every task; Ollama stays as the
fallback. The startup log says `DEVELOPMENT provider enabled: NVIDIA NIM`.
The same `.env` in production stops the boot with
`LLM_EXTERNAL_PROVIDER_IN_PRODUCTION` — that is the point.

### 4.3 A local vLLM or NIM container in development

```
LLM_PROVIDER=vllm
LLM_BASE_URL=http://vllm:8000/v1
LLM_MODEL=Qwen/Qwen2.5-14B-Instruct
LLM_SQL_MODEL=
```

Run the server yourself or reuse the production profile
(`--profile vllm`); the registry logs
`local OpenAI-compatible provider (vllm) at http://vllm:8000/v1`.

### 4.4 Tracing every turn with Opik (development only)

Self-hosted Opik runs on the developer machine (`http://localhost:5173`).
In `.env`:

```
SQL_AGENT_OPIK_ENABLED=true
OPIK_URL_OVERRIDE=http://host.docker.internal:5173/api/
OPIK_PROJECT_NAME=face-detector-sql-agent
```

Each turn becomes one trace with every graph node, every model call, and
every MCP tool call as an `mcp:<tool>` span with its arguments and
envelope. The traces contain questions, names, SQL and rows: use a synthetic
development database.

`./deploy.sh dev` handles the container for you (stage **D4b**): when
`SQL_AGENT_OPIK_ENABLED=true` is in `docker/.env` and a self-hosted Opik
checkout exists at `$OPIK_HOME` (default `$HOME/opik`, a clone of
comet-ml/opik), it runs `docker compose -f $OPIK_HOME/docker-compose.yaml up -d`
before starting the stack and stops it again on `./deploy.sh dev stop`. When
tracing is off it only prints how to enable it. The production path never
starts Opik and refuses a `docker/.env` that enables it (stage 06b).

### 4.5 Trying the NeMo Agent Toolkit orchestrator

```
pip install -r requirements-agent.txt   # in a scratch container, see the caveat
AGENT_ORCHESTRATOR=nemo
```

The toolkit needs numpy 2.3; since 2026-09-06 the image is on numpy 2 with
OpenCV 4.10 (`requirements-base.txt`), so it installs cleanly next to the
face-recognition stack. Rebuild the image after pulling that change:

```
docker compose -f docker/docker-compose.cpu.yml build face_recognition
docker compose -f docker/docker-compose.cpu.yml up -d
```

The adapter falls back to LangGraph with a logged reason when the toolkit
is absent.

### 4.6 Rehearsing production rules in development

Set `OFFLINE_MODE=true` in `.env` and restart: every offline rule now
applies while `ENVIRONMENT` stays `development`, so you can fix a
production env file before the real deployment. `GET /api/health/offline-policy`
(admin token) prints the checklist.

---

## 5. Production: step by step (air-gapped)

### 5.1 Prepare the bundle on an online machine

1. Build the application image and pull the base images.
2. Write a spec from `docker/offline_bundle.spec.example.json`: the Ollama
   model directory (or vLLM weights), the Chroma ONNX embedding directory,
   `weights/` (detection and recognition ONNX), wheels for any optional
   package (`pymilvus`, the agent requirements), `frontend/vendor`, certs,
   the env template, and every image the production compose references.
3. `scripts/prepare_offline_bundle.sh spec.json /media/bundle` — copies
   everything with SHA-256 checksums into `manifest.json` (+ `sbom.json`).
4. `scripts/verify_offline_bundle.sh /media/bundle` must print
   `[PASS] bundle verified`. Move the bundle across the gap.

### 5.2 Import on the production host

```
scripts/import_offline_bundle.sh /media/bundle /
```

This verifies again (refuses a tampered bundle), `docker load`s the images
and places files at their `dest` paths (Ollama models into the volume,
the embedding model into the Chroma cache path, weights into `/app/weights`).

### 5.3 Database roles and secrets

- Apply `db/roles.sql` once: it creates `fr_app`, `fr_migrator`,
  `fr_readonly` (SELECT only) and `fr_backup`. Generated SQL runs as
  `fr_readonly`; the API refuses to boot if `SQL_AGENT_DB_USER` equals the
  app role.
- Create the Docker secrets under `secrets/`: `jwt_secret`,
  `bootstrap_admin_password`, `webhook_api_keys` (strong, random; the guard
  measures entropy and refuses weak ones).

### 5.4 Write `docker/.env`

Start from `docker/env.production.example`. Compose interpolates the
role passwords and the origin; these are required (`${VAR:?}`):

```
POSTGRES_SUPERUSER_PASSWORD=...
FR_APP_PASSWORD=...
FR_MIGRATOR_PASSWORD=...
FR_READONLY_PASSWORD=...
FR_BACKUP_PASSWORD=...
REDIS_PASSWORD=...
GRAFANA_ADMIN_PASSWORD=...
PUBLIC_ORIGIN=https://faces.corp.internal
```

The agent block, local everything:

```
ENVIRONMENT=production
OFFLINE_MODE=true

LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:7b-instruct
OLLAMA_SQL_MODEL=arctic-text2sql:7b
LLM_DEV_PROVIDER=
NVIDIA_NIM_API_KEY=

EMBEDDING_PROVIDER=local
EMBEDDING_MODEL_PATH=/home/appuser/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx
VECTOR_STORE=chroma
MCP_SQL_URL=
AGENT_ORCHESTRATOR=langgraph
STT_PROVIDER=none
OTEL_EXPORTER_ENDPOINT=
SQL_AGENT_OPIK_ENABLED=false
ALLOW_EXTERNAL_APIS=false
ALLOW_MODEL_DOWNLOADS=false
ALLOW_EXTERNAL_TELEMETRY=false
OFFLINE_BUNDLE_MANIFEST=/opt/face_detector/bundle/manifest.json
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
```

Model names must match what the bundle placed in the Ollama volume
(`ollama list` inside the container shows them).

### 5.5 Start, and read the checklist

The guided way is the deployment script, which now carries the offline
policy as its own stage:

```
sudo ./deploy.sh --public-origin=https://faces.corp.internal
```

Stage **06b offline policy** runs on `install`, `validate` and `start`:

- it **refuses** the deployment when `docker/.env` enables the Opik tracer
  (`SQL_AGENT_OPIK_ENABLED=true`), the hosted NIM provider
  (`LLM_DEV_PROVIDER`), a cloud `LLM_PROVIDER`, `OFFLINE_MODE=false`, any
  `ALLOW_*=true`, or an endpoint on a public inference/telemetry host - the
  exact mistake of copying a development `.env` to the server;
- it **writes** the missing production keys under the managed section
  (`ENVIRONMENT=production`, `OFFLINE_MODE=true`, the three `ALLOW_*=false`,
  `SQL_AGENT_OPIK_ENABLED=false`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`)
  on `install`/`start` only, never on `validate` or `--dry-run`, and never
  changes a value you set;
- it never starts, pulls or configures an Opik container.

The manual equivalent is:

```
docker compose -f docker/docker-compose.prod.yml -f docker/docker-compose.prod.gpu.yml up -d
```

- If the API exits with code 78, read its log: every violation is listed
  with a fix. Correct `docker/.env` and start again.
- Once up, `/health/ready` must be `ready`; in production the
  `offline_policy` component is **required**, so a violation keeps the
  service `not_ready` for the load balancer.
- As an administrator:

  ```
  curl -s -H "Authorization: Bearer <admin token>" https://faces.corp.internal/api/health/offline-policy
  ```

  shows the `[PASS]/[FAIL]` list: offline mode, no external inference or
  telemetry endpoint, MCP and vector store internal, STT local, no
  downloads permitted, artifacts found, policy validated, and reachability
  of PostgreSQL, the local LLM and any configured service.

- Final proof: disconnect the gateway and run the acceptance battery
  (`95_AGENT_PRODUCTION_ACCEPTANCE.md`).

### 5.6 Optional production profiles

| Want | Set | Start with |
|---|---|---|
| vLLM instead of Ollama | `LLM_PROVIDER=vllm`, `LLM_BASE_URL=http://vllm:8000/v1`, `LLM_MODEL=<served name>`, `VLLM_MODEL=/models/<dir>` | `--profile vllm` (weights in the `vllm_models` volume from the bundle) |
| Milvus instead of embedded Chroma | `VECTOR_STORE=milvus`, `MILVUS_URI=http://milvus:19530` (+ `pymilvus` wheel in the image) | `--profile milvus` (starts etcd, minio, milvus on the internal `data` network) |
| Tool catalogue over MCP for other local agent hosts | `MCP_SQL_URL=http://mcp-sql:9901/mcp` | `--profile mcp-sql` (internal `ai`/`data` networks, never published) |
| NeMo Agent Toolkit orchestration | `AGENT_ORCHESTRATOR=nemo`; add `pip install -r requirements-agent.txt` to the image build | the application image (numpy 2 since 2026-09-06, see 4.5) |

None of the profiles publishes a port; only nginx (80/443) and Grafana
(3000) are exposed, as before.

---

## 5.7 The admin settings page

Admin → Settings has a **deployment** category with every setting from
section 3, so an operator can see what the box enforces without reading the
container environment:

- **Read-only** (marked as such, changed only through the container
  environment and validated by the boot guard): `ENVIRONMENT`,
  `OFFLINE_MODE`, `OFFLINE_ALLOWED_HOSTS`, the three `ALLOW_*` flags,
  `LLM_PROVIDER`, `LLM_BASE_URL`, `EMBEDDING_PROVIDER`, `EMBEDDING_BASE_URL`,
  `MCP_SQL_URL`, `MILVUS_URI`, `STT_PROVIDER`, `STT_BASE_URL`,
  `OTEL_EXPORTER_ENDPOINT`, `SQL_AGENT_OPIK_ENABLED`. They are in the
  guard's security-critical set: the admin API neither persists nor applies
  them, so a stolen admin token cannot point a production box at the internet.
- **Editable, applied on container recreate**: `LLM_MODEL`, `LLM_SQL_MODEL`,
  `OLLAMA_INTERPRETER_MODEL`, `VECTOR_STORE` (`chroma`/`milvus`),
  `AGENT_ORCHESTRATOR` (`langgraph`/`nemo`), `EMBEDDING_MODEL_PATH`,
  `STT_MODEL_PATH`, `OFFLINE_BUNDLE_MANIFEST`. The page stores the value and
  shows that a `docker compose up -d` is needed for it to take effect.
- **Editable, applied on API restart**: `SQL_AGENT_LEARN_FROM_QUERIES`
  (keep it off).

Every entry carries a description written for the page
(`backend/routes/settings.py`), and the registry metadata
(`backend/core/runtime_settings.py`) fixes the type, the allowed values
and the apply mode.

---

## 6. Worked scenarios

**"The API refuses to start after I copied my dev .env to the server."**
Expected, and `./deploy.sh` stage 06b stops even earlier with the same
list. Look for `LLM_EXTERNAL_PROVIDER_IN_PRODUCTION`, `OFFLINE_OPIK_ENABLED`,
`OFFLINE_ALLOW_MODEL_DOWNLOADS` in the log; unset `LLM_DEV_PROVIDER`,
`SQL_AGENT_OPIK_ENABLED`, the `ALLOW_*` flags.

**"`/health/ready` is `not_ready` and the checklist says `required model
artifacts found` failed."** The bundle was not imported on this host, or the
Chroma cache volume is new: check `EMBEDDING_MODEL_PATH` exists inside the
container, then import the bundle.

**"I want a stronger reader than the general model."** Set
`OLLAMA_INTERPRETER_MODEL` (or `NVIDIA_NIM_INTERPRETER_MODEL` in dev) to a
model that follows instructions well; the reading (`wants`, `shape`,
people, camera) decides the intent, so it pays back more than a larger SQL
model does.

**"The bot answers slowly."** Check `fr_agent_stage_duration_seconds` by
stage: `llm` dominates on CPU Ollama; `db_query` is normally milliseconds.
A GPU vLLM profile is the usual answer.

**"Can I see what a turn did without Opik in production?"** Yes: the
`sql_agent_turn` audit line (intent, tools, tables, SQL hashes, rows,
status) and the history row (validated SQL in its metadata). Opik is for
development only.

---

## 7. Verification commands

```
# unit tests for both modes
docker exec face_recognition_api python -m pytest tests/test_offline_policy.py tests/test_llm_provider_selection.py \
    tests/test_mcp_tools.py tests/test_degradation_modes.py tests/test_loop_uses_mcp_catalogue.py -q

# the guard, in-process, against the live settings
docker exec face_recognition_api python -c "from backend.security.config_guard import enforce; enforce()"

# the checklist (admin token)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/health/offline-policy | python -m json.tool

# the MCP catalogue over the protocol (optional profile)
docker compose ... --profile mcp-sql up -d && docker exec mcp-sql python -c "import mcp; print('ok')"

# the offline bundle
scripts/verify_offline_bundle.sh /opt/face_detector/bundle
```
