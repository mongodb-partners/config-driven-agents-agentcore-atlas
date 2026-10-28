# Spec: Rebrand Magenta → Atlas Agent Engine

Status: implemented (§5.1 offline checks pass; §5.2/§5.3 need live service-account credentials and a deployed stack)
Scope: this repo only (multi-agent RIV workshop on Bedrock AgentCore + MongoDB Atlas)

## 1. Background

At MongoDB .Local NYC, the product previously known as **Magenta** (a.k.a. "Agentic
Platform") was announced in **public preview** as **MongoDB Atlas Agent Engine**:

- Product page: https://www.mongodb.com/products/platform/atlas-agent-engine
- Docs: https://www.mongodb.com/docs/agentengine/
- Memory-only (standalone memory service) guide:
  https://www.mongodb.com/docs/agentengine/add-features/memory-only/

This repo integrates the managed memory service as the optional `magenta` long-term
memory backend (`agent/platform_memory.py`), via the `agentic-platform-memory` SDK
installed from the platform's private registry at image-build time. All user-facing
names, endpoints, package names, and docs must move to the Atlas Agent Engine
naming.

## 2. Naming / reference mapping (old → new)

| What | Old (current repo) | New (Atlas Agent Engine) |
|---|---|---|
| Product name | Magenta / Agentic Platform | MongoDB Atlas Agent Engine |
| Managed memory service | Magenta Memory | Atlas Agent Engine memory service |
| Python SDK package | `agentic-platform-memory` (v0.8.0) | `agent-engine-sdk-memory` |
| Python import | `agentic_platform_memory` | `agent_engine_sdk_memory` |
| Hosted platform host | `agentic-platform.mongodb.com` | `agentengine.mongodb.com` |
| OAuth token endpoint | `https://agentic-platform.mongodb.com/api/v1/oauth/token` | `https://agentengine.mongodb.com/api/v1/oauth/token` |
| Private PyPI registry | `https://ignore:$TOKEN@agentic-platform.mongodb.com/api/v1/packages/python/simple/` | `https://ignore:$TOKEN@agentengine.mongodb.com/api/v1/packages/python/simple` |
| Auth model | OAuth client id/secret exchanged for bearer token | **Service account** client id/secret exchanged for an access token (same `client_credentials` grant); SDK takes `service_account_token=` |
| SDK client ctor | `Memory(api_key=..., project_id=..., base_url=...)` | `Memory(service_account_token=..., project_id=...)` (hosted) / `Memory(base_url=...)` (local). `api_key` still accepted as a deprecated alias; `project_id` still selects the route shape |
| Config backend value | `memory.backend: magenta` | `memory.backend: engine` (proposed; see §6) |
| Env vars | `MAGENTA_CLIENT_ID`, `MAGENTA_CLIENT_SECRET` | `AGENT_ENGINE_CLIENT_ID`, `AGENT_ENGINE_CLIENT_SECRET` (proposed) |
| Project id env var | `AGENTIC_MEMORY_PROJECT_ID` | **unchanged** — the new docs still reference `AGENTIC_MEMORY_PROJECT_ID` (see Troubleshoot section of the memory-only guide) |
| Other env vars | `AGENTIC_MEMORY_API_KEY`, `AGENTIC_MEMORY_BASE_URL`, `AGENTIC_MEMORY_MAX_TOKENS` | keep, or rename to `AGENT_ENGINE_*` for consistency — decide in §6 |
| Terraform variables | `magenta_client_id`, `magenta_client_secret` | `agent_engine_client_id`, `agent_engine_client_secret` |
| Terraform variables (kept) | `agentic_memory_project_id`, `agentic_memory_base_url` | unchanged (match the env vars the SDK reads) |

### Confirmed from the public docs (memory-only guide)

- Hosted install:
  `pip install agent-engine-sdk-memory --extra-index-url "https://ignore:$ACCESS_TOKEN@agentengine.mongodb.com/api/v1/packages/python/simple"`
- Token mint:
  `curl --user <client-id> --data grant_type=client_credentials https://agentengine.mongodb.com/api/v1/oauth/token`
  (client secret supplied at the curl prompt; token valid 1 hour)
- New SDK usage surface: `Memory`, `MemoryRequestContext`, `memory.bind(ctx)`,
  `record_turn(role=, content=)`, `build_context(query=, enabled_sources=...)`,
  `search()` — same conceptual surface our `MagentaBackend` already wraps.
- Service accounts are created with `agentengine service-account create ... --role PROJECT_OWNER`;
  the client secret is shown once.
- Memory data is stored in the customer's Atlas cluster (the `MONGODB_URI` secret
  configured on the project) — this is what makes the §5 end-to-end verification
  against our Atlas cluster meaningful.

### Resolved questions (answered from the new SDK docs)

1. **Constructor** — `Memory(*, runtime=None, client=None, _bound_ctx=None,
   service_account_token=None, api_key=None, base_url=None, project_id=None)`.
   `service_account_token` is the current auth input; `api_key` is a **deprecated
   alias** that still works, so our existing `_client()` call would not hard-break —
   but we migrate to `service_account_token=` regardless. `project_id` still exists
   and alone controls the route shape (set → `/api/v1/projects/{id}/memory/*`;
   empty → flat `/api/v1/memory/*`); auth does not influence routing.
2. **Exceptions** — only `MemoryIdentityError`, `MemoryNotSupportedError`, and
   `MemoryRouteNotFoundError` are confirmed by name in the docs. The package has a
   "typed transport-error family" in `errors.py`, but `MemoryAuthError`,
   `MemoryAPIError`, `MemoryClientError` are **not confirmed**. Action: at
   implementation time, `python -c "import agent_engine_sdk_memory as m;
   print(dir(m))"` and fix the import list in `platform_memory.py` to the real
   names; keep the generic `except Exception` degrade guards as the safety net so
   an unconfirmed name can never break a turn.
3. **Method kwargs** — unchanged: `record_turn(role=, content=)`,
   `build_context(query=, enabled_sources=, max_tokens=)`,
   `MemoryRequestContext(user_id=, session_id=)`. No logic changes needed in
   `recall`/`persist`/`short_term` beyond names.
4. **Old endpoints** — not documented as supported; only `agentengine.mongodb.com`
   appears in the current docs. **Treat as hard cutover**: switch all URLs, do not
   rely on redirects.
5. **Old service accounts** — not confirmed valid; project API keys can no longer
   be minted over HTTP. **Documented path: create a fresh service account** with
   `agentengine service-account create ... --role PROJECT_OWNER` and mint tokens
   from the new token endpoint. PRE_REQUISITES must say existing Magenta-era
   credentials need re-provisioning.

Practical summary: hosted = `Memory(service_account_token=..., project_id=...)`.
The migration is mostly a rename; the only real code risk is the exception import
list (question 2).

## 3. Code and script changes (file by file)

23 files reference Magenta today (~120 occurrences). Grouped by change type:

### 3.1 Agent code (`agent/`)

- **`agent/platform_memory.py`**
  - Module docstring: Magenta → Atlas Agent Engine memory service.
  - Import block: `agentic_platform_memory` → `agent_engine_sdk_memory`; only
    `MemoryIdentityError` (+ `MemoryNotSupportedError`, `MemoryRouteNotFoundError`)
    are confirmed names — introspect the installed package and fix the exception
    import list to the real transport-error names (§2 question 2).
  - `_TOKEN_URL` → `https://agentengine.mongodb.com/api/v1/oauth/token`.
  - `MagentaBackend` → `AgentEngineBackend`; `name = "magenta"` → `"engine"`.
  - `_mint_token()`: env vars `MAGENTA_CLIENT_ID/SECRET` → `AGENT_ENGINE_CLIENT_ID/SECRET`;
    error messages updated ("service account" wording per new docs).
  - `_client()`: pass the minted token as `service_account_token=` (the `api_key=`
    kwarg is a deprecated alias — do not keep using it); keep the three auth paths
    (pre-minted token / service-account exchange / unauthenticated self-hosted via
    base_url). `project_id` handling is unchanged — it still picks the
    project-scoped route shape, so hosted keeps setting it and local/self-hosted
    leaves it empty.
  - `_require_sdk()` message: reference `agent-engine-sdk-memory` and the new
    install instructions.
- **`agent/memory.py`** — lazy-import seam: backend value `"magenta"` → `"engine"`,
  `MagentaBackend` → `AgentEngineBackend`, comment updates.
- **`agent/agent_config.py`** — `_MEMORY_BACKENDS = ("atlas", "magenta")` →
  `("atlas", "engine")`; comments. Decide on backwards compatibility here (§6).
- **`agent/main.py`** — trace-event labels currently emit `"magenta"` for
  `source`/`collection` fields on the non-atlas path (lines ~290, 362, 373, 398);
  change to `"engine"`; cold-start comment update.
- **`agent/requirements.txt`** — the trailing comment block: package name and
  registry description updated. (Package stays out of requirements.txt — it is
  still served from the private registry, installed by the Dockerfile.)
- **`agent/Dockerfile`** — the whole `── Magenta Memory SDK ──` RUN block:
  - comments → Atlas Agent Engine;
  - BuildKit secret ids `magenta_client_id/secret` → `agent_engine_client_id/secret`;
  - build arg `MAGENTA_CREDS_FINGERPRINT` → `AGENT_ENGINE_CREDS_FINGERPRINT`;
  - token URL → `agentengine.mongodb.com`;
  - `uv pip install --system "agent-engine-sdk-memory" --extra-index-url "https://ignore:${ACCESS_TOKEN}@agentengine.mongodb.com/api/v1/packages/python/simple"`;
    pin policy for the new package (the old pin was `>=0.8.0,<0.9`).
- **`agent/test_platform_memory.py`** — rename tests and fixtures
  (`test_backend_magenta_parses` → `test_backend_engine_parses`, `MagentaBackend`
  → `AgentEngineBackend`, env var names). Consider renaming the file to
  `test_agent_engine_memory.py`.

### 3.2 Agent configs (`config/agents/`)

All four agents opt into the managed backend:
`orchestrator`, `career-coach`, `company-intel`, `job-match` —
`backend: magenta` → `backend: engine` in each `.agent.md`.

### 3.3 IaC (`terraform/`)

- **`variables.tf`** — rename `magenta_client_id` → `agent_engine_client_id`,
  `magenta_client_secret` → `agent_engine_client_secret`; rewrite the description
  blocks (service-account wording, new package name, new registry). Keep
  `agentic_memory_project_id` / `agentic_memory_base_url` names.
- **`agentcore.tf`** — env injection block (~lines 103–119): `MAGENTA_CLIENT_ID` →
  `AGENT_ENGINE_CLIENT_ID`, `MAGENTA_CLIENT_SECRET` → `AGENT_ENGINE_CLIENT_SECRET`;
  variable references renamed; comments.
- **`images.tf`** — `magenta = sha256(var.magenta_client_id)` cache-buster local →
  renamed; build-arg/secret plumbing renamed to match the Dockerfile.
- **`terraform.tfvars.example`** — the Magenta section (~lines 70–94): new variable
  names, new prose (Atlas Agent Engine, service account via `agentengine` CLI,
  private registry).

### 3.4 Scripts (`scripts/`)

- **`scripts/build-and-push.sh`** — `MAGENTA_ARGS` → `AGENT_ENGINE_ARGS`; env vars
  `MAGENTA_CLIENT_ID/SECRET` → `AGENT_ENGINE_*`; secret ids and build-arg name
  matching the Dockerfile; comments.
- **`scripts/memtest/verify_sdk.py`** — docstring exports, `MagentaBackend()` →
  `AgentEngineBackend()`; new env var names in usage text.
- **`run-local.sh`, `deploy.sh`** — check for Magenta references (none found in
  grep; confirm) — no change expected.

### 3.5 UI (`ui/app.py`)

Two spots (~lines 289, 315): explainer text naming "magenta (the managed Magenta
Memory service)" → "engine (the Atlas Agent Engine memory service)".

### 3.6 Documentation

- **`README.md`** — the "Long-term memory backend: `atlas` or `magenta`" section
  (~lines 253–291): new backend name, new SDK name/link (the PyPI link to
  `agentic-platform-memory` is dead weight — the package was never on PyPI; point
  to https://www.mongodb.com/docs/agentengine/add-features/memory-only/ instead),
  new env/terraform variable names.
- **`PRE_REQUISITES.md`** — section 2.6 (~lines 122–145): retitle "Optional:
  Atlas Agent Engine memory (only for the `engine` backend)"; replace the
  provisioning paragraph with the new flow:
  1. Install + authenticate the `agentengine` CLI.
  2. `agentengine create --memory-only --name ...`; `agentengine project create ...`;
     `agentengine auth login --project-id ...`.
  3. Set secrets (`MONGODB_URI`, `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`) and run
     `agentengine memory configure` + `agentengine memory apply --wait`.
  4. `agentengine service-account create memory-service --project-id ... --role PROJECT_OWNER`
     → put client id/secret into `terraform.tfvars`.
  Call out explicitly: **Magenta-era OAuth clients are not confirmed to keep
  working** — anyone who ran the workshop before the rebrand must create a new
  service account (old endpoints are treated as a hard cutover).
  Note the Atlas cluster prerequisites from the docs: Flex/M10+ tier, IP access
  list allowing the Agent Engine data plane.
- **`docs/agentic-platform-memory-plan.md`, `docs/agentic-platform-memory-proposal.md`**
  — historical design docs. Add a one-line header noting the rename, or rename the
  files; do not rewrite history in place.
- **`docs/magenta-memory-verification-report.md`** — historical report; same
  treatment (header note + optional rename to
  `agent-engine-memory-verification-report.md`). New verification results from §5
  get a new report file.
- **`PROMPT.md`, `iam-permissions.json`** — confirm no references (none in grep).

## 4. SDK installation instructions (new, to be reflected in docs + Dockerfile)

Per the public-preview docs:

```bash
# 1. Mint an access token from the service-account credentials
export ACCESS_TOKEN=$(curl --fail-with-body --silent --show-error \
  --user <client-id> \
  --data grant_type=client_credentials \
  "https://agentengine.mongodb.com/api/v1/oauth/token" | jq -er .access_token)

# 2. Install the Memory SDK from the platform's private registry
pip install agent-engine-sdk-memory \
  --extra-index-url "https://ignore:$ACCESS_TOKEN@agentengine.mongodb.com/api/v1/packages/python/simple"
```

(`ignore` is a literal placeholder username; the registry authenticates on the
token in the password field. Tokens expire after 1 hour — fine for a build step,
which is exactly how our Dockerfile uses it.)

Local-development alternative (no token needed, for contributors without platform
access): `agentengine dev up` runs a local memory stack; SDK connects with
`Memory(base_url="http://localhost:<oe-port>")` and no `project_id`. Document as
optional in PRE_REQUISITES; keep the hosted flow the default for the workshop.

## 5. Test & verification plan (end-to-end)

### 5.1 Unit / offline

- `python -m pytest agent/test_agent_engine_memory.py` (renamed suite) — config
  seam (`backend: engine` parses, unknown backend still errors at load),
  `AgentEngineBackend` behaviour with a stubbed SDK (recall/persist degrade, token
  refresh, auth-failure invalidation).
- `terraform fmt -check && terraform validate`.
- `docker build` of `agent/` with and without the BuildKit secrets — image must
  still build credential-less, with the new skip message.

### 5.2 SDK smoke (hosted, against the real service)

- Update `scripts/memtest/verify_sdk.py`; run it in the agent image with
  `AGENT_ENGINE_CLIENT_ID`, `AGENT_ENGINE_CLIENT_SECRET`, `AGENTIC_MEMORY_PROJECT_ID`
  set. Assert: token mint → `record_turn` user+assistant → (after consolidation
  delay) `build_context`/`search` returns the recorded fact.

### 5.3 End-to-end with the Atlas cluster

1. `terraform apply` with the renamed variables populated; `deploy.sh` builds and
   pushes images, creates the four AgentCore runtimes.
2. Through the UI (or a scripted `/invocations` call), run the two-session memory
   probe the previous verification report used:
   - **Session A**: tell an agent a durable fact (e.g. "I always fly out of
     Boston").
   - **Session B** (new session id, same user): ask a question that needs the fact
     ("which airport should I book from?"). A new session has an empty AgentCore
     short-term buffer, so a correct answer can only come from long-term memory.
3. **Verify persistence in Atlas directly**: connect to the workshop Atlas cluster
   (the `MONGODB_URI` configured on the Agent Engine project) with `mongosh`/Compass
   and confirm the memory service's collections contain the consolidated memory
   documents for the test user. Record database/collection names and sample
   (redacted) documents in the new report.
4. UI Memory tab still renders recall/persist trace events under the new `"engine"`
   label.
5. Write results to `docs/agent-engine-memory-verification-report.md` (new file;
   keep the old Magenta report as history).

### 5.4 Acceptance criteria

- `rg -i magenta` over the repo (excluding `.git`, `.venv`, and the clearly-marked
  historical docs) returns nothing.
- All tests in 5.1 pass; 5.2 smoke passes; 5.3 session-B recall works and memory
  documents are visible in the Atlas cluster.
- No live reference to `agentic-platform.mongodb.com` or `agentic-platform-memory`
  outside historical docs.

## 6. Decisions to confirm before implementation

1. **Backend config value**: rename `magenta` → `engine` (clean, matches product).
   Alternative: `agentengine`. Should the loader accept `magenta` as a deprecated
   alias for one release, or hard-cut? (Recommendation: hard-cut — this is a
   workshop repo redeployed fresh each time.)
2. **Env var prefix**: `AGENT_ENGINE_CLIENT_ID/SECRET` vs keeping
   `MAGENTA_*`. Recommendation: rename; terraform variables follow suit.
3. **Keep `AGENTIC_MEMORY_*`** (`PROJECT_ID`, `BASE_URL`, `API_KEY`, `MAX_TOKENS`)
   as-is, since the new docs still use `AGENTIC_MEMORY_PROJECT_ID`. Confirmed by
   the SDK docs that `project_id` (and its env var) still drives routing — lean
   keep.
4. **SDK pin**: what version constraint for `agent-engine-sdk-memory` during
   public preview (docs warn of breaking changes — pin tight, e.g. `==x.y.z`,
   once we know the published versions).
