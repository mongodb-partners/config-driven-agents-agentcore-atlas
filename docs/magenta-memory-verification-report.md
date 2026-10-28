# Magenta Memory persistence — verification report

> **Historical document.** "Magenta" is now **MongoDB Atlas Agent Engine**
> (`engine` backend, `agent-engine-sdk-memory` SDK). Post-rename verification is
> tracked separately; see `docs/atlas-agent-engine-rename-spec.md` §5.

**Run:** `mem-20260915-1` · **Date:** 15 September 2026 · **Verdict:** persisted correctly, with two platform-side findings to raise.

Companion to the live test plan (`claude.ai/artifact/FiybjJJxrPParVz4LoM5vT`) and the harness in `scripts/memtest/`.

---

## 1. Summary

The `agentic-platform-memory` SDK (0.8.0), wired into the four AgentCore runtimes through `agent/platform_memory.py`, persists every turn to MongoDB Atlas and recalls extracted facts across sessions for the same user, and not for other users.

- **11 of 12 live cases pass**, 1 partial (needs a redeploy), 2 left for the operator (need the client secret), 4 covered by unit tests.
- **The four evidence layers agree**: trace events = CloudWatch POSTs = Atlas documents, for every session in the run.
- **R2 — new session, same user — passes cleanly.** This is the case that proves the feature, because a new session has an empty AgentCore short-term buffer and any recall can only be Magenta's.
- **Extraction takes ~80–90 s after a session goes quiet.** This is the number the workshop's part 3 must be choreographed around.
- **Two findings for the platform team**: the taxonomic extractor writes user-specific facts with `visibility: org`, and episodic memory is never produced for this project.

---

## 2. System under test

| | |
|---|---|
| Runtimes | `orchestrator`, `career-coach`, `company-intel`, `job-match` — all on `memory.backend: magenta` |
| SDK | `agentic-platform-memory 0.8.0`, hosted gateway, project-scoped routes (`/api/v1/projects/6aa0fd8d0749b100d5867289/memory/*`) |
| Auth | OAuth `client_credentials` → bearer token, minted at run time by `platform_memory.py` (fix of 11 Sep) |
| UI | http://34.236.146.146:8501 — pins `userId = workshop-user` for every session |
| Store | Atlas cluster `aws-gp.ls3c2`, database `agentic_memory` |

### How the system writes (traced from `agent/main.py`)

- **Only the orchestrator persists.** Specialists recall but never write. One user turn = exactly two `record_turn` calls (user, assistant).
- `agent_id` on the write is **the agent that answered**: the specialist after a hand-off, `orchestrator` when it answered directly.
- `record_turn` has *write-accepted* semantics per the SDK — the ack means accepted, not committed. Atlas checks therefore poll rather than read immediately.
- Magenta is **long-term only**. Short-term context is AgentCore's own buffer (`memory.short_term_turns`), out of scope here.

---

## 3. Method

Four turns driven directly against the orchestrator with `scripts/memtest/turn.py` (same `invoke_agent_runtime` call as the UI, identity under our control):

| Step | User | Session | Prompt |
|---|---|---|---|
| W1 | `qa-mem-20260915-1-a` | S1 | *"I spent four years at Halcyon Dynamics as a Rust engineer and will only consider fully remote roles. Which open roles fit me?"* |
| R1 | a | S1 | *"Which company did I say I worked at, and what kind of roles will I consider?"* |
| R2 | a | **S2 (new)** | same question |
| I1 | `qa-mem-20260915-1-b` | fresh | same question |

The canary — *Halcyon Dynamics* — is a company no corpus contains, so recall is unambiguous.

Evidence was taken from four independent layers:

1. **Stream trace events** (`memory.write`, `memory.recall`, `traceId`) — what the agent believes it did.
2. **CloudWatch** httpx lines per runtime — what went over the wire.
3. **SDK read-back** (`search_semantic`, `list_episodes`, `build_context`) — what the platform serves. *Operator-run; see §6.*
4. **Atlas documents** via the MongoDB MCP — ground truth.

---

## 4. Results

| Id | Case | Result | Evidence |
|---|---|---|---|
| W1 | One turn writes exactly two turns | **pass** | Trace `write count=2 error=None`; CloudWatch 4× `POST /memory/turns 201` for 2 turns; Atlas exactly 2 documents per turn |
| W2 | Content is verbatim | **pass** | User content 125 chars = prompt; assistant content 1877 chars = streamed answer, identical first/last 60 chars |
| W3 | Attribution: user, session, agent, time | **pass** | `user_id`, `session_id`, `project_id` exact; timestamps inside the invoke window; `agent_id=job-match` on the hand-off turn, `orchestrator` on the direct one |
| W4 | Sequence numbers increase | **pass** | `turn_seq` 1→4 in S1; restarts at 1 per session |
| W5 | Blank half not written | unit | `test_persist_skips_empty_halves` |
| D1 | Survives time and cold start | **partial** | All four `_id`s present at +12 min, same content lengths. Cold-start half needs a redeploy |
| D2 | No duplicates | **pass** | 8 documents for 4 turns across 3 sessions, 8 distinct `idempotency_hash`, POSTs = documents |
| D3 | Turns are embedded | **pass** | `has_embedding=false` at write → `true`, `voyage-4-large`, at +147 s (79 s after last write) |
| R1 | Same-session recall | **pass** (see note) | Answer correct, but `memory.recall count=0` — at +84 s extraction had not run; the answer came from AgentCore's buffer |
| R2 | Cross-session recall, same user | **pass** | `recall count=2`, both facts verbatim from `memory_semantic`; answer names Halcyon Dynamics and remote-only |
| R3 | Recall respects token budget | operator | `verify_sdk.py` |
| X1 | Fact extracted, latency measured | **pass** | Two semantic facts (scores 0.98, 0.96) citing the canary turn: **150 s after first write, 82 s after last** |
| X2 | Session listed as an episode | operator | Expected to return nothing — finding 2 |
| I1 | Another user cannot recall it | **pass** | User b: `recall count=0`, answer says it knows nothing. Boundary holds at the agent, not fully in the data — finding 1 |
| O1 | UI Memory tab shows write and recall | operator | Two turns in the UI |
| O2 | Token lifecycle | **pass** | Exactly 1× `POST /oauth/token 200` on the orchestrator's cold start; 0 `AUTH_FAILED` in the run. Re-mint on expiry not reached |
| F1 | Auth failure degrades | unit | `test_auth_failure_drops_the_cached_token` |
| F2 | Unreadable shape degrades | unit | `test_recall_handles_formatted_context_as_a_list` + in-image check |

### Extracted facts (from `memory_semantic`)

```
"The user worked at Halcyon Dynamics for four years as a Rust engineer."   combined_score 0.98
"The user will only consider fully remote roles."                            combined_score 0.96
```

Both cite the canary turn (`citations: [0, 1]`), `visibility: private`, `has_embedding: true`.

---

## 5. Findings

### Finding 1 — Taxonomic extraction writes user facts with `visibility: org`

**Severity: medium. Owner: platform team.**

From user a's private turn, the taxonomic extractor wrote:

```
term:        Halcyon Dynamics
domain:      employment
definition:  A company where the user worked for four years, specifically in engineering roles.
user_id:     qa-mem-20260915-1-a
visibility:  org
```

Semantic facts are `private`; taxonomic terms are `org`. The agent's recall sources are `{episodic, semantic}` so I1 passes at the agent level — but a `search()` without a source filter, or any agent that adds `taxonomic` to its sources, exposes one user's employment history to every user in the org.

Not caused by our code — it is the extractor's default. For this workshop it is moot only because every attendee shares `workshop-user`.

**Recommendation:** raise with the platform team; ask whether taxonomic visibility can be set per project, or whether user-identifying definitions ("where *the user* worked") should be filtered from org-visible terms.

### Finding 2 — Episodic memory is never produced

**Severity: low. Owner: platform team.**

`memory_episodic` holds **zero documents for the entire project**, including the 30 `workshop-user` turns — yet every `episodic` task in `tasks` reports `succeeded`, in ~1 s (the semantic task takes ~3 s). `extraction_config` is empty, so either the extractor is off for this project or it has a threshold no session has met.

Harmless today: recall is served entirely by `memory_semantic`. But `list_episodes` / `search_episodes` will always be empty, and `enabled_sources={"episodic", "semantic"}` is half dead.

**Recommendation:** ask the platform team whether episodic extraction is expected for this project tier.

### Finding 3 — Extraction latency sets the workshop choreography

**Severity: informational, but decisive for part 3. Owner: us.**

Observed pipeline: a worker wakes ~80 s after a session's **last** write, batch-embeds the pending turns (`voyage-4-large`), sets `promoted`, then spawns four tasks — episodic, semantic, taxonomic, procedural — over the whole session transcript. Semantic completes in ~3 s.

| Event | Time | Δ from first write | Δ from last write |
|---|---|---|---|
| First write (canary) | 11:11:53.5 | — | — |
| Last write (turn 2) | 11:13:01.7 | +68 s | — |
| Turns embedded | 11:14:20.9 | +147 s | +79 s |
| Extraction tasks created | 11:14:21.1 | +148 s | +79 s |
| Semantic facts written | 11:14:24.1 | **+150 s** | **+82 s** |

Inside that window a *new* session recalls nothing (R2 would fail). The *same* session only appears to remember because of AgentCore's buffer (R1 showed exactly this: `recall count=0`, correct answer).

**Recommendation:** part 3 must run as *tell the agent → wait 90 s → new session → ask*. Facilitator notes should say why.

Reinforcement is real: the 11 Sep fact "IC engineer → engineering management" gained `reinforcement_count: 1` when the 15 Sep turns cited it again.

### Finding 4 — CloudWatch trace-id filtering lags

**Severity: low. Owner: nobody; a note for the next tester.**

The `otel-rt-logs` stream (which carries trace ids) was ~30 min behind the plain `[runtime-logs]` stream. Per-trace layer-2 checks work eventually; per-window counts work immediately. Each line is written twice (JSON + plain), so halve raw counts.

### Instrumentation gaps in our code (not defects)

- **`persist` discards the ack.** `record_turn` returns `WriteTurnResult{id, turn_seq, has_embedding}`; the backend counts to 2 and drops it. Surfacing `turn_seq` on the `memory.write` trace event would let the UI Memory tab prove W4 on its own. ~10 lines.
- **The UI cannot express a second user.** `USER_ID` is one environment value. Fine for the workshop; it means I1 is only ever runnable by direct invoke.
- **`MagentaBackend.short_term` is dead code.** `main.py` reads short-term from AgentCore, never from Magenta's `stm` source.
- **`expires_in` is not logged.** The token TTL is only visible from the SDK script.

---

## 6. Remaining work

| Item | Who | How |
|---|---|---|
| R3, X2, O2-TTL | operator | `verify_sdk.py` — command below. Needs the client secret, which the tester does not handle |
| O1 | operator | Two turns in the UI, then the Memory tab |
| D1 cold-start half | either | Redeploy, then re-query the four S1 `_id`s |

```bash
export MAGENTA_CLIENT_ID=… MAGENTA_CLIENT_SECRET=… AGENTIC_MEMORY_PROJECT_ID=6aa0fd8d0749b100d5867289
docker run --rm -e MAGENTA_CLIENT_ID -e MAGENTA_CLIENT_SECRET -e AGENTIC_MEMORY_PROJECT_ID \
  -v "$PWD/scripts/memtest:/t:ro" \
  979559056307.dkr.ecr.us-east-1.amazonaws.com/auto-embed-byoc-riv-workshop/agent:latest \
  python /t/verify_sdk.py --user qa-mem-20260915-1-a \
    --session sess-qa-mem-20260915-1-a-s1-000000000000 --needle Halcyon
# expect: token_ttl_seconds; list_episodes count 0 (finding 2); search_semantic 2 hits;
#         build_context token_count ≤ 2048, memory_counts.semantic = 2
# then --user qa-mem-20260915-1-b → every search empty (I1, SDK side)
```

---

## Appendix A — Atlas layout (pinned 15 Sep)

Database `agentic_memory`. The `mdb_store_<project_id>` database beside it is the platform's workflow store, not memory.

| Collection | Holds | Fields the cases key on |
|---|---|---|
| `memory_short_term` | Every `record_turn` — the raw turns | `user_id session_id agent_id role content turn_seq timestamp has_embedding embedded_at embedding_model_id promoted idempotency_hash project_id` |
| `memory_semantic` | Extracted facts, one per fact | `user_id content citations contextual_metadata.combined_score reinforcement_count visibility` |
| `memory_taxonomic` | Extracted terms — **org-visible** | `term domain definition user_id visibility` |
| `memory_episodic` | Episodes — **empty for this project** | — |
| `tasks` | Extraction pipeline, one task per memory type per session pass | `task_type status created_at completed_at payload.session_id idempotency_key` |
| `session_counters` | The `turn_seq` source, one per session | — |

## Appendix B — Run identifiers

| | |
|---|---|
| Users | `qa-mem-20260915-1-a`, `qa-mem-20260915-1-b` |
| Sessions | `sess-qa-mem-20260915-1-a-s1-000000000000`, `…-a-s2-…`, `…-b-s1-…` |
| Trace ids | W1 `4db56110ab8a43358cd609a947fd7097` · R1 `75b3901cad5c4bbd8462199536c0e8d3` · R2 `88ff656d8bf749038d1cd46e7676f98e` · I1 `18abc50c5fa44f7d893dcba3b40c0af8` |
| S1 document ids | `6aa927f9ce4f4efb28a70252`, `…0253`, `6aa9283dce4f4efb28a70254`, `…0255` |
| Semantic fact ids | `6aa92890ce4f4efb28a7025b`, `…025c` |
| Orchestrator log group | `/aws/bedrock-agentcore/runtimes/auto_embed_byoc_riv_workshop_orchestrator-NhGvZqHICb-DEFAULT` |

## Appendix C — Corrections to the plan made from data

- **W3:** the first draft required `agent_id` to be "the specialist, never `orchestrator`". The data showed `orchestrator` on a turn it answered without a hand-off. The rule is "the agent that answered".
- **X2:** the first draft expected ≥ 1 episode. Atlas shows episodic memory is never produced (finding 2).
- **R1:** kept as written, but its caveat — that a same-session answer may come from AgentCore's buffer, not Magenta — was observed, not hypothetical.
