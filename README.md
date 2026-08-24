# Multi-Agent Streamlit app on Agentcore and MongoDB Atlas

A multi-agent career assistant on **Amazon Bedrock AgentCore** and **MongoDB Atlas**.

**You add or remove an agent by editing
config, never code.** 

## Architecture

```mermaid
flowchart TB
    browser(["Browser"])
    ui["<b>Streamlit UI</b> · EC2 t4g.small<br/>chat · live reasoning<br/>developer trace · live metrics"]

    subgraph ac["Amazon Bedrock AgentCore — 5 runtimes, 5 IAM roles"]
        orch["<b>orchestrator</b><br/>routes · owns memory"]

        subgraph spec["specialists — one runtime per config/agents/*.agent.md"]
            cc["career-coach"]
            jm["job-match"]
            ci["company-intel"]
        end

        mcpr["<b>MongoDB MCP</b><br/>the only door to Atlas"]
        acm[("AgentCore Memory<br/>short-term turns")]
    end

    subgraph atlas["MongoDB Atlas"]
        dom[("<b>domain + vector</b><br/>jobs · companies · candidates<br/>career_paths · skills_taxonomy<br/>compensation_data")]
        ltm[("<b>long-term memory</b><br/>agent_facts<br/>chat_messages · chat_sessions")]
        trc[("<b>agent_traces</b>")]
    end

    models["<b>Amazon Bedrock</b><br/>Claude Haiku 4.5<br/>Titan Embed v2"]
    ssm["SSM Parameter<br/>agentId → runtime ARN"]
    sec["Secrets Manager<br/>Atlas URI"]
    cw["CloudWatch<br/>logs · OTel spans"]

    browser --> ui
    ui -->|"InvokeAgentRuntime · SigV4"| orch

    orch -->|"InvokeAgentRuntime · SigV4"| cc
    orch --> jm
    orch --> ci

    orch -->|"MCP streamable HTTP · SigV4"| mcpr
    cc --> mcpr
    jm --> mcpr
    ci --> mcpr

    mcpr --> dom
    mcpr --> ltm
    mcpr --> trc

    orch <-.-> acm
    orch -.->|"handoff target"| ssm
    orch -.->|"Converse · every agent"| models
    cc -.-> models
    jm -.-> models
    ci -.-> models
    mcpr -.->|"at boot"| sec
    orch -.-> cw
    ui -.->|"read-only: sessions, traces"| ltm

    classDef aws fill:#eaf1fb,stroke:#3f6fae,stroke-width:1px,color:#10243d
    classDef mdb fill:#e6f6ec,stroke:#2f8f5b,stroke-width:1px,color:#0b2e1c
    classDef side fill:#f5f2ec,stroke:#9a8465,stroke-width:1px,color:#2f2718
    class orch,cc,jm,ci,mcpr,acm,models aws
    class dom,ltm,trc mdb
    class ui,ssm,sec,cw,browser side
```

**Five AgentCore runtimes**, each with its own compute and its own IAM role. Two
architectural choices worth naming:

- **No MongoDB driver in the agent image.** Every read and write — agent tool
  calls, memory, trace flushes — goes through the MCP runtime. The dashed line
  from the UI to Atlas is the one exception: it is a read-only `pymongo`
  connection for the session list and trace panel, and the UI is not an agent.

---

## Deploy

Work through [PRE_REQUISITES.md](PRE_REQUISITES.md) first — model access and the
Atlas API key are the two things that block people.

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# fill in atlas_org_id, atlas_public_key, atlas_private_key

./deploy.sh
```

On the first run `deploy.sh` asks how you want text embedded and writes the answer
into `terraform.tfvars` as `embedding_mode`. The three options are covered in
[Three ways to embed](#three-ways-to-embed--pick-one-before-you-deploy); the short
version is that the default needs a Voyage/Atlas API key, `auto` needs no key at
all, and `titan` needs Bedrock model access. Whichever you pick, the required
secret is checked before anything is created — not fifteen minutes in.

15–20 minutes. The Atlas cluster and the ARM64 image builds run in parallel; the
instance then seeds Atlas and waits for the vector indexes before serving. When
the UI answers, the data is ready.

Open the `ui_url` from the output.

```bash
./deploy.sh destroy      # removes everything, including the Atlas project
```

### Bring your own cluster

Provisioning an M10 is about ten of those twenty minutes. If you already have a
cluster — running the workshop a second time, or reusing a dev cluster — set its
connection string instead and Terraform creates no Atlas resources at all:

```hcl
# terraform/terraform.tfvars
mongodb_uri = "mongodb+srv://user:password@cluster.abcde.mongodb.net/"
```

Deploy drops to 8–12 minutes, `atlas_org_id` and the Atlas API keys become
unnecessary, and `destroy` leaves your cluster and its data alone. In exchange you
own the parts Terraform was doing for you: network access reaching the AgentCore
runtimes and the EC2 box, a user with `readWrite` on the database plus
`atlasAdmin` on `admin` (the seed creates Search indexes), and a tier with room
for 7 vector indexes. `deploy.sh` prints that list before it starts, and
[PRE_REQUISITES §3.0](PRE_REQUISITES.md#30-new-cluster-or-one-you-already-have)
has the detail.

### Run the UI locally

The agents live in AgentCore, so nothing about them runs on your laptop. The UI
is the exception, and it is the part you are most likely to change:

```bash
./run-local.sh          # http://localhost:8501
```

It reads the runtime ARNs and the database out of Terraform state, reuses the
repo's `.venv`, and serves `ui/app.py` against the deployed stack. Edit the file
and Streamlit reloads. It needs live AWS credentials — the UI signs its calls to
the orchestrator with them — and an Atlas network access rule that admits your
laptop, so it stops with a clear message rather than a stack trace if either is
missing.

Anything under `agent/`, `config/`, `mcp/` or `seed/` still needs `./deploy.sh`:
that code runs in the runtimes, not locally, and a trace panel that looks wrong
locally is usually a deployed agent that has not been rebuilt yet.

---

## Add an agent without touching code

This is the demo. Create `config/agents/salary-negotiator.agent.md`:

```markdown
---
id: salary-negotiator
name: Salary Negotiator
description: >-
  Rehearses a compensation negotiation with the candidate — practises the ask,
  the counter, and the walk-away. Keywords: negotiate, counter-offer, rehearse,
  practise the conversation, how do I ask for more.
role: specialist
model: us.anthropic.claude-haiku-4-5-20251001-v1:0
maxTokens: 4096
thinking: 1024
temperature: 0.4
skills: []
collections:
  - compensation_data
memory:
  shortTerm: true
  longTerm: true
---

# Salary Negotiator

You rehearse compensation conversations. Ground every number in a band you
retrieved this turn — never assert a figure from your own knowledge.
```

Then:

```bash
./deploy.sh
```

Terraform's `for_each` sees the new file, so it builds a fifth runtime; the agent
image is rebuilt with the definition baked in, so the orchestrator's routing
roster includes it; and the SSM runtime map is republished, so the handoff
resolves. **No Python changed.**

Verify the contract without deploying:

```bash
python agent/test_config.py
```

`test_a_new_agent_needs_no_code_change` does exactly the above against a
temporary config tree.

---

## How it fits together

### Config is the interface

```
config/
├── agents/<id>.agent.md          frontmatter = wiring, markdown = system prompt
└── skills/<skill>/
    ├── SKILL.md                  the retrieval procedure the agent follows
    └── references/
        └── collections-schema.md field names, enums, filterable paths
```

The filename is the agent id. `role: orchestrator` marks the router; everything
else is a specialist. `collections:` is the agent's data scope — enforced in code,
not just described in the prompt, so an agent cannot search a collection it was
not given.

[`agent/main.py`](agent/main.py) is one image for all of them; `AGENT_ID` decides
which definition the container becomes.

`thinking:` is the one key with consequences beyond its own agent. It sets the
extended-thinking budget, and extended thinking is the only thing that makes
Bedrock stream `reasoningContent` — with no budget the UI's reasoning panel is
structurally empty, however fast the transport is. Two constraints come with it,
both enforced at config load rather than as a runtime `ValidationException`: the
budget must be at least 1024 and must leave room under `maxTokens` for the
answer. Bedrock also rejects any temperature but 1.0 alongside thinking, so
`build_model()` overrides `temperature:` while a budget is set. Thinking tokens
are billed as output on every turn — `thinking: false` turns an agent's reasoning
stream back off.

### Atlas does four jobs

| Collection | Role |
|---|---|
| `jobs`, `career_paths`, `skills_taxonomy`, `compensation_data`, `companies` | Operational + vector store, one Atlas Vector Search index each |
| `candidates` | Operational, looked up by id |
| `agent_facts` | Long-term memory — LLM-extracted, embedded, recalled semantically at session start |
| `chat_messages` | Every turn, individually embedded, semantically searchable mid-conversation |
| `chat_sessions` | Session list for the UI |
| `agent_traces` | Full turn traces — what the developer panel renders |

Short-term memory is the only thing that is *not* in Atlas: it lives in the
**AgentCore Memory service**, keyed by actor and session.

### Everything goes through MCP

The agent image has no MongoDB driver. Agent tool calls, memory reads and writes,
and trace flushes all go through the **official `mongodb-mcp-server`** running in
its own AgentCore Runtime. Requests are SigV4-signed with the calling runtime's
IAM role — that role *is* the authentication on this path.

Destructive tools (`drop-*`, `delete-many`, `export`) are disabled at the MCP
server, so an agent cannot call them regardless of what a prompt says.

### Vector search without vectors in the prompt

The MCP server has no vector-search tool, and a 1024-float array has no business
in a model's context anyway. So `vector_search(collection, query_text)` is a local
tool: it passes text to [`mongo_mcp.vector_search`](agent/mongo_mcp.py), which calls
the MCP `aggregate` tool with a `$vectorSearch` stage. The model sends text and
gets documents back, and never sees a vector in either direction.

### Three ways to embed — pick one before you deploy

`embedding_mode` decides *who* turns text into vectors. `./deploy.sh` asks on the
first run and records the answer in `terraform.tfvars`; after that both the script
and Terraform refuse to deploy if the mode and the secrets disagree.

| `embedding_mode` | Who embeds | Needs | Document field | Index field type |
|---|---|---|---|---|
| `atlas-voyage` *(default)* | The app, via MongoDB's Atlas Embedding API | `voyage_api_key` | `embedding` (1024 floats) | `vector` |
| `auto` | Atlas, inside the cluster | nothing | `searchText` (the text itself) | `autoEmbed` |
| `titan` | The app, via Bedrock Titan v2 | Bedrock model access | `embedding` (1024 floats) | `vector` |

It is a deploy-time choice, not a per-query one: an Atlas index cannot mix `vector`
and `autoEmbed` fields, so changing your mind means rebuilding the indexes and
re-running the seed.

The whole difference lives in two mirrored functions in
[`embeddings.py`](agent/embeddings.py) — `index_fields()` (what the seed writes)
and `query_clause()` (what the search sends). Everything else is mode-blind.
[`test_embedding_modes.py`](agent/test_embedding_modes.py) asserts the pair agrees
on the field name in all three modes, because a mismatch there returns zero
documents without raising anything.

**`atlas-voyage`** — the app calls the embedding API and stores the vector itself.
Voyage keys come in two flavours that hit different endpoints, and the prefix
decides: an `al-…` key is issued by MongoDB Atlas and goes to `ai.mongodb.com`, a
`pa-…` key is native Voyage and goes to `api.voyageai.com`. Using one against the
other's endpoint returns `403`. `VOYAGE_API_BASE` overrides the detection.

> **Rate limits are a runtime concern here, not just a seeding one.** A single turn
> embeds several times — memory recall in the orchestrator, again in the
> specialist, once per `vector_search`, and once each for the batched message and
> fact writes. That is roughly 4–6 calls per turn. Seeding batches into one
> request; live turns cannot. A key rated in the low single-digit requests per
> minute will `429` mid-conversation, and the answer stalls.

**`auto`** — MongoDB Atlas Automated Embedding. There is no embedding code on the
path at all: the seed writes the text, the index says which model to use, and
Atlas embeds both the stored documents and the incoming query string in-cluster.

```javascript
// seed writes text, not vectors
{ "type": "autoEmbed", "modality": "text", "path": "searchText", "model": "voyage-4" }

// and the query is a string
{ "$vectorSearch": { "index": "jobs_vector_index", "path": "searchText",
                     "query": { "text": "senior backend roles in Berlin" } } }
```

No key anywhere in the stack, no rate limit to trip, and no way for the stored
vectors to drift out of sync with the model — Atlas re-embeds on write. The costs:
it is a **Preview** feature, it needs a dedicated cluster (M10+) with storage
auto-scaling on (`atlas.tf` enables it), embedding runs on MongoDB infrastructure
in a US region regardless of where your cluster lives, and generation is
*asynchronous* — the index reports `READY` before the documents behind it are
embedded. `seed.py` handles that last one by polling a real `$vectorSearch` until
it answers, so "the UI is up" still means "the data is searchable".

**`titan`** — Bedrock Titan v2, no account beyond AWS. The fallback for attendees
who have neither an Atlas model API key nor a Voyage one.

### Observability, two ways

- **CloudWatch** — the runtimes are started under `opentelemetry-instrument`, so
  AgentCore's built-in observability ships spans and logs. Every trace event is
  also one JSON line on stdout. Log groups are in the `cloudwatch_log_groups`
  output.
- **Atlas** — each turn is flushed to `agent_traces`, so you can query agent
  behaviour with an aggregation pipeline instead of CloudWatch Insights. This is
  what the UI's **Developer trace** panel reads.

The panel has five tabs and nothing else: **Flow** (every step in order, with the
offset from turn start), **Tokens & cost**, **Atlas calls** (each query with its
arguments and a result preview), **Memory** (what was recalled and written), and
**Raw events**.

### What a turn costs, and where the context went

One turn is never one model call. A typical turn makes three: the orchestrator's
routing call, the specialist's answer, and a fact-extraction call that runs after
the answer has streamed — no latency, full price. **Tokens & cost** lists them
separately, with input, output and cached tokens per call, so the third one is
visible rather than folded into a total.

Two numbers in that tab are measured differently, and the tab says so:

- **Tokens and cost** come from the API's own usage figures. Cache reads bill at
  0.1× and writes at 1.25×, and are counted apart from uncached input.
- **What filled the context window** is measured in *characters*. The API bills a
  single `inputTokens` figure and will not break it down, so
  [`build_system_prompt`](agent/main.py) reports the size of each segment as it
  assembles the prompt — agent prompt, specialist roster, skill docs, session
  history, recalled memory — and the turn adds the tool results it read and the
  candidate's own message alongside them. The token column is that character
  count divided by four: a ratio, not a measurement. It is there to answer "why
  is this prompt so large", and the answer is usually skill docs, by an order of
  magnitude over recalled memory.

Prices live in a table in [`tracing.py`](agent/tracing.py) and are **Anthropic's
published list prices**, not Bedrock's — Bedrock is partner-operated and bills
separately. Confirm them against the Bedrock pricing page before quoting a figure.
`MODEL_PRICING` overrides the table as JSON without a rebuild, and a model the
table does not know prices at zero rather than guessing.

### Live metrics, in the sidebar

The sidebar totals every turn the user has ever run: total tokens, cost, and
per-request averages for latency, tool calls, tokens and LLM calls. It is one
aggregation over `agent_traces`, recomputed on each script run — refresh the page
to update it, there is no live stream.

The join is the interesting part. The orchestrator and the specialist run in
separate runtimes and flush separate trace documents, so counting documents would
report double the requests and half the tokens per request. The orchestrator
passes its `traceId` down and the specialist stores it as `parentTraceId`: tokens
sum across both, while the request count and the latency come from root traces
only.

---

## Repository layout

```
config/            agent definitions and skills — the only thing you edit to add an agent
agent/             one container image for every agent
  main.py            AgentCore entrypoint; orchestrator and specialist paths
  agent_config.py    parses config/agents/*.agent.md
  mongo_mcp.py       SigV4 MCP client — the only door to Atlas
  tools.py           vector_search, recall_conversation, read_skill_resource
  memory.py          facts, chat messages, sessions, AgentCore short-term
  tracing.py         trace events → stdout (CloudWatch) + Atlas
  embeddings.py      the three embedding modes, and nothing else knows about them
  test_config.py     self-check for the config-only contract
  test_embedding_modes.py  self-check that seed and search agree on the index field
  test_pricing.py    self-check for the cost arithmetic
  test_tool_trace.py self-check that a tool call is traced with its arguments
mcp/               official mongodb-mcp-server, packaged for AgentCore Runtime
seed/              data + embeddings + Atlas index creation (replaces a Bedrock KB)
ui/
  app.py             Streamlit — chat, live reasoning, developer trace, live metrics
  .streamlit/config.toml   the MongoDB design system as native Streamlit theming
terraform/         the whole stack
scripts/           build-and-push.sh — ARM64 build + ECR push, called by Terraform
deploy.sh          one command up, one command down
run-local.sh       serve the UI from your laptop against the deployed stack
```

---

## Operating it

```bash
# reach the box (needs the SSM plugin)
aws ssm start-session --target <instance-id>

sudo tail -f /var/log/riv-seed.log        # seeding and index builds
sudo tail -f /var/log/riv-ui.log          # Streamlit
sudo systemctl restart riv-ui

# re-seed by hand
sudo -i
export MONGODB_URI="$(aws secretsmanager get-secret-value \
  --secret-id <secret-arn> --query SecretString --output text)"
/opt/riv/venv/bin/python /opt/riv/seed/seed.py
```

**Agent logs:**

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT --follow
```

**Trace a turn in Atlas:**

```javascript
db.agent_traces.find({ traceId: "<id>" })
db.agent_traces.aggregate([
  { $unwind: "$events" },
  { $match: { "events.kind": "tool.call" } },
  { $group: { _id: "$events.tool", calls: { $sum: 1 } } }
])

// tokens and cost per agent, across every turn
db.agent_traces.aggregate([
  { $unwind: "$events" },
  { $match: { "events.kind": "usage" } },
  { $group: {
      _id: "$agentId",
      calls:  { $sum: 1 },
      tokens: { $sum: "$events.totalTokens" },
      usd:    { $sum: "$events.costUsd" } } }
])
```

---

## Known limits

Deliberate, and marked `ponytail:` in the source where they are load-bearing.

- **The UI has no authentication.** Cognito was explicitly out of scope. `ui_allowed_cidr`
  is the only control — narrow it to your own IP if your network allows. Do not
  leave this stack running unattended.
- **All traffic is over the public internet.** No VPC peering or PrivateLink, and
  the Atlas project accepts `0.0.0.0/0`. That is a simplification, not a
  pattern to copy into production.
- **One conversation at a time.** The orchestrator reads the specialist's response
  stream synchronously inside the async path. Fine for one attendee per stack;
  it would need an async HTTP client to serve concurrent sessions.
- **Agents read; the framework writes.** Only read tools are exposed to the models.
  Memory and trace writes go through MCP from framework code, so an agent cannot
  corrupt its own history.
- **The cost figure is an estimate.** It is computed from a price table in
  `tracing.py` holding Anthropic's published list prices, not rates read from
  AWS. Bedrock bills separately, so treat the number as an order of magnitude
  and a way to compare turns against each other — not as your invoice.
- **`user_id` is a fixed string.** With no auth there is no real identity, so
  long-term memory is shared across every session on a deployment. That makes the
  memory demo work; it is not a multi-tenant design.
