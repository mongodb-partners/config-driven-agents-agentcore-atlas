# Proposal: Integrate Agentic Platform Memory into the Multi-Agent RIV Workshop

> **Historical document.** "Magenta" / "Agentic Platform" was renamed to
> **MongoDB Atlas Agent Engine** at public preview. Current naming, SDK
> (`agent-engine-sdk-memory`), and endpoints are in
> `docs/atlas-agent-engine-rename-spec.md`.

**Audience:** Engineering & Product Leadership
**Status:** For review / decision
**Prepared by:** RIV Workshop team
**Date:** 2026-09-07

---

## Executive summary

We propose adding **Agentic Platform Memory** (the Magenta Memory SDK) to the
Multi-Agent RIV Workshop as a **new, config-selectable memory backend**, running
*alongside* the workshop's existing memory implementation rather than replacing
it.

Today the workshop demonstrates a multi-agent career assistant whose defining
message is *"add or change a capability by editing configuration, not code."*
Memory is currently hand-built on MongoDB Atlas. Adding the platform SDK lets us
showcase a **managed, enterprise memory service** through that same
config-only interface — a compelling before/after story for the same audience,
with low engineering cost and a clean fallback.

**The ask:** approval to proceed, plus a decision on the two items in
[Decisions required](#decisions-required).

---

## Why now

1. **Strategic alignment.** The workshop is a public-facing reference for how we
   build agentic systems. Demonstrating our own platform memory service in it
   directly showcases first-party capability to the exact audience we want
   adopting it.
2. **Low incremental cost.** The workshop already treats memory as a first-class,
   cleanly-abstracted concern. Adding a second backend is a contained,
   well-understood change — not a rewrite.
3. **Config-over-code narrative.** Switching an agent to platform memory becomes
   a **one-line configuration change**. That reinforces the workshop's core
   thesis and makes the platform look effortless to adopt.

---

## What we have today

The workshop is a multi-agent career assistant (an orchestrator routing to three
specialist agents) built on Amazon Bedrock AgentCore and MongoDB Atlas. Its
signature feature: **you add an agent by dropping a config file, with zero code
changes.**

Memory is split by lifetime:

- **Short-term** (in-session) — AgentCore's managed memory service.
- **Long-term** (across sessions) — **hand-built** on Atlas: an LLM extracts
  durable facts about the user, stores them, and recalls them semantically in
  future sessions.

The long-term path works, but it is bespoke: we own the extraction logic, the
storage schema, the recall tuning, and the extra model-inference cost it incurs
on every turn.

---

## What we're proposing

Introduce a **memory backend selector** so each agent can choose its long-term
memory provider in configuration:

| Backend | What it is | Who owns it |
|---|---|---|
| `atlas` *(default, unchanged)* | Today's hand-built fact extraction + search | Us |
| `platform` *(new)* | Managed Magenta Memory service via the SDK | The platform |

Selecting the platform backend is a single line in an agent's config file:

```yaml
memory:
  longTerm: true
  backend: platform      # was implicitly "atlas"
```

No other change is needed to switch a given agent. Existing agents keep working
exactly as before, because `atlas` remains the default.

### What the platform backend gives us

- **Managed memory as a service** — record a conversation turn, ask for relevant
  context back. Extraction, storage, and retrieval are handled by the service.
- **Richer memory types** out of the box (facts/profiles, past-conversation
  summaries, domain knowledge, procedures) versus our single hand-rolled
  fact store.
- **Less code we maintain** — the bespoke extraction and storage logic can be
  retired for agents that opt into the platform.

---

## Value and impact

**For the workshop / demo:**
- A live, side-by-side demonstration: the same agent, same config interface, two
  memory backends. Powerful proof of both the config-driven architecture *and*
  the platform's ease of adoption.

**For the platform / org:**
- A public reference integration of our memory service, exercising its real API
  surface and surfacing gaps early.
- Dogfooding: we validate the SDK's developer experience in a realistic,
  end-to-end deployment.

**For engineering:**
- A reusable backend-selector pattern that keeps the door open to future memory
  providers without further architectural churn.

---

## Approach and effort

The change is deliberately contained and reversible:

1. Add the SDK as a pinned dependency.
2. Add a `backend` option to the agent config schema (defaulting to today's
   behavior).
3. Introduce a thin backend seam so the two implementations are interchangeable.
4. Deliver platform credentials securely to the runtimes (via our existing
   secrets mechanism).
5. Documentation and a demo agent configured for platform memory.

**Estimated effort:** small — on the order of a few engineer-days, dominated by
integration testing and credential/secret plumbing rather than new logic. A
detailed engineering plan already exists
(`docs/agentic-platform-memory-plan.md`).

**No new AWS infrastructure or IAM permissions** are required — the SDK talks to
the platform's managed HTTPS endpoint.

---

## Cost considerations

- **Reduced per-turn inference cost** for platform-backend agents: today's Atlas
  path runs an *extra* model call on every turn to extract facts. The platform
  service handles extraction, so that cost can go away for agents that opt in.
- **New dependency:** usage of the managed memory service (an internal/platform
  cost rather than a new external vendor spend).
- **Net:** roughly cost-neutral to favorable at workshop scale, with the benefit
  of retiring bespoke code we maintain.

---

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **SDK/credentials not available at workshop time** | Demo can't run in platform mode | Verify installability and provision credentials *before* the event; `atlas` remains the default and always works |
| **External service dependency at runtime** | Latency/outage affects platform-mode turns | Graceful degradation — a memory hiccup means "no recall this turn," never a broken conversation. `atlas` unaffected |
| **Secret handling** | Credential exposure | Delivered via existing secrets management, never committed in plaintext |
| **Feature parity / behavior differences** | Demo tells an inconsistent story | Keep both backends live for A/B; default stays `atlas`; document differences |

The design is **low-blast-radius by construction**: the new backend is opt-in
per agent, defaults off, and can be removed by reverting one config line.

---

## Decisions required

1. **Approval to proceed** with the alongside (non-replacing) integration in
   hosted mode.
2. **Credential provisioning** — confirm we can obtain a platform API key and
   project for the workshop environment, and who owns that.
3. *(Engineering detail, for awareness)* Whether platform-mode agents also route
   *short-term* memory through the platform, or keep AgentCore short-term as-is.
   Recommended: keep short-term unchanged for now to minimize scope.

---

## Recommendation

Proceed. This is a low-cost, low-risk, high-visibility integration that
strengthens the workshop's central narrative while showcasing first-party
platform memory to precisely the right audience. Default behavior is unchanged,
the new capability is opt-in, and the change is reversible in one line.

**Next step:** confirm credential provisioning, then execute the existing
engineering plan.

---

*Companion document: `docs/agentic-platform-memory-plan.md` (detailed
engineering plan).*
