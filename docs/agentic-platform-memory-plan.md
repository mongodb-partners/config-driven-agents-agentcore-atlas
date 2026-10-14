# Plan: Add Agentic Platform Memory to the Multi-Agent RIV Workshop

## Objective

Add [`agentic-platform-memory`](/Users/anuj.panchal/Documents/Work/Magenta-Client-Libs/magenta-client-libraries/packages/agentic-platform-memory)
(the Magenta Memory Python SDK, v0.8.0) to the workshop as a **config-selectable
long-term memory backend**, sitting *alongside* the existing hand-rolled Atlas
memory — not replacing it.

Two backends, chosen per agent in `.agent.md`:

- `atlas`  (existing) — LLM fact extraction + Atlas vector search over
  `agent_facts` / `chat_messages`.
- `magenta` (new) — the Magenta Memory Server via the SDK's `record_turn` /
  `build_context` / `save_semantic` / `search`.

Connection mode: **hosted platform** — `Memory(api_key=..., project_id=...)`,
routes `…/projects/{project_id}/memory/*`. Credentials come from
`AGENTIC_MEMORY_API_KEY` and `AGENTIC_MEMORY_PROJECT_ID`.

### Non-negotiable: config over code

The workshop's defining promise is *add/change a capability by editing
`config/agents/*.agent.md`, not Python*. This plan keeps that intact: selecting
platform memory is a one-line frontmatter change; the Python change is a
one-time, backend-agnostic seam.

---

## Current state (grounding)

- Memory lives in `agent/memory.py`, split by lifetime:
  - **short-term** — AgentCore Memory service (`get_last_k_turns` / `create_event`).
  - **long-term** — Atlas: `recall_facts`, `extract_facts`, `store_messages`,
    `recall_conversation`, `touch_session`.
- Wired in `agent/main.py`: `run_orchestrator` (memory owner) and
  `run_specialist` both call `memory.recall_facts` on start and the orchestrator
  writes (`touch_session`, `store_messages`, `extract_facts`, `record_turn`)
  after the answer streams.
- Per-agent gating today: `memory: { shortTerm: bool, longTerm: bool }` in
  frontmatter → `AgentDef.short_term` / `.long_term` (`agent/agent_config.py`).
- Every memory op emits a `tr.event("memory.*", ...)` trace the UI's Memory tab
  renders (`ui/app.py`).
- Deps in `agent/requirements.txt`; runtimes stamped per `.agent.md` in
  `terraform/agentcore.tf` with env vars incl. `AGENTCORE_MEMORY_ID`. IAM in
  `terraform/iam.tf`.

Key constraint preserved: **agents read; the framework writes.** Platform-memory
writes stay in framework code (`main.py` / `memory.py`), never exposed as an
agent tool.

---

## Design: a memory-backend seam

Introduce a thin backend abstraction so `main.py` is backend-agnostic. The
existing Atlas functions become the `atlas` backend; the SDK becomes the
`magenta` backend. Both expose the same small surface `main.py` already needs:

```
recall(user_id, session_id, query)      -> list[dict]   # facts/context to inject
persist(user_id, session_id, agent_id,  -> dict         # write turn + extract
        user_text, assistant_text)                      #   returns trace-friendly summary
short_term(user_id, session_id)         -> list[dict]   # session turns (optional)
```

- `atlas` backend = current `memory.py` functions, unchanged behavior.
- `magenta` backend = wraps the SDK:
  - `recall` → `build_context(query, enabled_sources={"episodic","semantic"}, max_tokens=...)`
    and returns `context.formatted_context` (mapped into the existing
    `_facts_block` shape, or injected as a context block).
  - `persist` → `record_turn(role="user"/"assistant", content=...)` for both
    halves; optionally `save_semantic`/`save_episode` for durable items.
  - `short_term` → `build_context(enabled_sources={"stm"})` **or** defer to the
    existing AgentCore short-term buffer (see Decisions).

The SDK is synchronous and thread-safe across users when you `bind` per request:

```python
mem = Memory(api_key=KEY, project_id=PROJECT)     # once per process
session = mem.bind(MemoryRequestContext(
    user_id=user_id, session_id=session_id, agent_id=agent_id))
```

One process-level `Memory` instance; `bind()` per turn. Close on shutdown / use
as a context manager.

---

## Work breakdown

### 1. Dependency
- Add to `agent/requirements.txt`, pinned to match the workshop's
  reproducibility rule:
  ```
  agentic-platform-memory>=0.8.0,<0.9
  ```
  Its only transitive deps are `pydantic>=2` and `httpx>=0.25` (httpx already
  present). Requires Python 3.11+ — the workshop already targets 3.11.
- If the SDK is not on the attendees' package index, vendor it or point pip at
  the internal index; confirm availability before the workshop (**risk**, below).

### 2. Config schema — the only "interface" change
- Extend the frontmatter `memory:` block with a backend selector, defaulting to
  `atlas` so every existing agent is unchanged:
  ```yaml
  memory:
    shortTerm: true
    longTerm: true
    backend: platform     # atlas (default) | platform
  ```
- In `agent/agent_config.py`:
  - Add `long_term_backend: str = "atlas"` to `AgentDef`.
  - Parse `memory.get("backend", "atlas")`; validate it's one of
    `{"atlas", "platform"}` (raise a clear `ValueError` on typos, matching the
    file's existing fail-fast style, e.g. `_thinking_budget`).

### 3. Backend abstraction in `agent/memory.py`
- Keep all current functions as-is (they become the `atlas` implementation).
- Add a `platform_memory.py` module (or a section in `memory.py`) that:
  - Lazily constructs one `Memory(api_key=..., project_id=..., base_url=...)`
    from env vars `AGENTIC_MEMORY_API_KEY` / `AGENTIC_MEMORY_PROJECT_ID`
    (and optional `AGENTIC_MEMORY_BASE_URL`).
  - Implements `recall` / `persist` / `short_term` per the seam above.
  - Maps SDK results into the dict shapes `main.py`'s `_facts_block` /
    `_history_block` already expect (`{"text": ...}` etc.), so prompt assembly is
    untouched.
  - Wraps SDK errors: catch `MemoryClientError` and `MemoryAPIError`; note
    `MemoryIdentityError` is a plain `ValueError` sibling (not under
    `MemoryClientError`) so catch it explicitly. On failure, degrade
    gracefully (empty recall, log a trace) so a memory outage never breaks a turn.
- Add a small dispatcher, e.g. `get_backend(agent_def) -> Backend`, so `main.py`
  picks `atlas` vs `magenta` from `AgentDef.long_term_backend`.

### 4. Wire into `agent/main.py`
- Replace direct `memory.recall_facts(...)` / write calls in `run_orchestrator`
  and `run_specialist` with the dispatched backend:
  ```python
  backend = memory.get_backend(AGENT)
  facts = backend.recall(user_id, session_id, prompt) if AGENT.long_term else []
  ...
  backend.persist(user_id, session_id, target_or_self, prompt, answer)
  ```
- Preserve the "write after stream" ordering (no perceived latency) and all
  existing `tr.event("memory.recall" | "memory.write" | "memory.extract", ...)`
  and `tr.usage(...)` traces. Platform backend should emit the *same* trace
  event names so the UI Memory tab and cost panel keep working. If the platform
  backend does its own extraction server-side, emit `memory.extract` with
  whatever the SDK reports (or `count=0`) rather than the local Bedrock
  extraction usage.
- Short-term: keep AgentCore short-term as-is regardless of backend (simplest,
  see Decisions), OR route `short_term` through the platform STM source when
  `backend: platform`.

### 5. Terraform — env vars + secret
- `terraform/agentcore.tf`: add `AGENTIC_MEMORY_API_KEY`,
  `AGENTIC_MEMORY_PROJECT_ID` (and optional `AGENTIC_MEMORY_BASE_URL`) to each
  runtime's environment, sourced from a new variable/secret. Follow the existing
  pattern used for `AGENTCORE_MEMORY_ID` / Atlas URI (Secrets Manager, read by
  the MCP runtime today).
- `terraform/variables.tf`: add `agentic_memory_api_key` (sensitive) and
  `agentic_memory_project_id`. Wire through `locals.tf` / `outputs.tf` as needed.
- **IAM:** no new AWS IAM needed — the SDK talks to an external HTTPS endpoint,
  not an AWS service. Only ensure the API key is delivered securely (Secrets
  Manager, not plaintext tfvars committed).
- Gate cleanly: if the platform vars are unset, agents with `backend: platform`
  should fail fast with a clear message at load, not mid-turn.

### 6. UI (optional, low effort)
- `ui/app.py` Memory tab already renders `memory.recall` / `memory.extract` /
  `memory.short_term` / `memory.write` events. If the platform backend emits the
  same event names, no UI change is required. Optional: show `backend` in the
  trace event payload so attendees can see which backend answered.

### 7. Docs
- README "Memory" section: document the new `memory.backend` frontmatter option,
  the two backends, and the `AGENTIC_MEMORY_*` env vars / secret.
- `PRE_REQUISITES.md`: note the API key/project provisioning step for hosted mode.

### 8. Tests
- `agent/agent_config.py` change: add a `.agent.md` fixture with
  `backend: platform` and assert it parses; assert an invalid backend raises.
- `agent/` self-check (mirroring existing `test_*.py`): a
  `test_platform_memory.py` using the SDK's Protocol seams (`FakeRuntime` /
  `FakeCrudClient`, as its own `tests/test_memory_facade.py` does) to verify the
  backend calls `record_turn` / `build_context` with the right shapes — no live
  server needed.
- Verify `atlas` path is byte-for-byte unchanged (default backend) so existing
  agents and demos are unaffected.

---

## Decisions to confirm

1. **Short-term handling under `backend: platform`.** Recommended: leave
   AgentCore short-term untouched (it's orthogonal and already provisioned), and
   only swap the *long-term* recall/persist. Alternative: route STM through the
   SDK's `stm` source for a single-backend story.
2. **Durable extraction.** The Atlas backend runs an LLM fact-extraction call.
   Platform memory may do episodic/semantic extraction server-side. Recommended:
   for `magenta`, `record_turn` both messages and let the server handle
   extraction (drop the extra Bedrock call) — cheaper and simpler. Confirm the
   hosted backend does this, else add explicit `save_episode`/`save_semantic`.
3. **Identity.** Workshop uses a fixed `user_id` (no auth). Platform memory keys
   on `user_id`/`session_id` via `MemoryRequestContext`; the fixed id means
   shared memory across sessions — same known limitation as today. No change
   needed, but note it in docs.
4. **`max_tokens` budget** for `build_context`: the SDK reserves 500 tokens for
   formatting (values ≤500 leave nothing). Pick a sane default (e.g. 2048) and
   make it a config knob later if needed.

---

## Risks

- **SDK availability at workshop time.** The package must be installable from the
  attendees' index (or vendored). The workshop pins upper bounds precisely to
  avoid live-demo breakage — confirm `agentic-platform-memory==0.8.0` resolves in
  a clean build before the event. **Highest-priority risk.**
- **External dependency at runtime.** Platform mode adds a network hop to an
  external Memory Server; an outage or latency spike affects turns using it. The
  graceful-degrade wrapper (step 3) contains this to "no recall this turn."
- **Secret handling.** `AGENTIC_MEMORY_API_KEY` must go through Secrets Manager,
  never committed tfvars.
- **Trace/UI parity.** If the platform backend emits different trace shapes, the
  Memory tab / cost panel silently under-report. Mitigation: emit identical
  event names (step 4).

---

## Sequencing

1. Confirm SDK installability + hosted credentials (blocker — do first).
2. Config schema + parser (`agent_config.py`) + test.
3. Backend seam + `platform_memory.py` + fake-runtime test.
4. Wire `main.py` to the dispatcher; preserve traces/ordering.
5. Terraform env vars + secret + variables.
6. Docs (README, PRE_REQUISITES) + optional UI backend label.
7. End-to-end: deploy one agent with `backend: platform`, verify recall/persist
   and the Memory tab; verify default `atlas` agents are unchanged.

## Touched files summary

| File | Change |
|---|---|
| `agent/requirements.txt` | add pinned `agentic-platform-memory` |
| `agent/agent_config.py` | parse+validate `memory.backend`; `AgentDef.long_term_backend` |
| `agent/memory.py` | keep as `atlas` backend; add dispatcher `get_backend()` |
| `agent/platform_memory.py` (new) | SDK-backed `recall`/`persist`/`short_term` + error wrap |
| `agent/main.py` | use dispatched backend; keep trace/usage events + ordering |
| `agent/test_platform_memory.py` (new) | fake-runtime contract test |
| `terraform/agentcore.tf` | add `AGENTIC_MEMORY_*` env to runtimes |
| `terraform/variables.tf` (+locals/outputs) | new sensitive vars / secret |
| `ui/app.py` | optional: surface `backend` in Memory tab |
| `README.md`, `PRE_REQUISITES.md` | document backend option + credentials |
