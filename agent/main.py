"""AgentCore Runtime entrypoint — one image, every agent.

`AGENT_ID` picks which `config/agents/*.agent.md` this container becomes. That
is the whole mechanism behind "add an agent without touching code": Terraform
stamps out one runtime per definition file, each with a different `AGENT_ID`.

Two shapes, chosen by the definition's `role`:

- `orchestrator` — classifies, hands off, owns session bookkeeping and memory
- `specialist`   — answers from Atlas via MCP, streams reasoning as it goes
"""

from __future__ import annotations

import json
import os
import uuid

import boto3
from botocore.config import Config
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

import agent_config
import memory
import mongo_mcp
import tools as agent_tools
from tracing import Trace

AGENT = agent_config.load_agent(os.environ["AGENT_ID"])
REGION = mongo_mcp.REGION
RUNTIME_MAP_PARAM = os.environ.get("RUNTIME_MAP_PARAM", "")

app = BedrockAgentCoreApp()

# botocore's default read_timeout is 60s, and on a streaming response that is the
# maximum gap allowed *between chunks* — not the total call. A specialist that
# spends 60s inside one tool therefore kills the stream mid-answer, and the
# candidate sees a truncated response. 15 minutes matches AgentCore's own
# invocation ceiling.
#
# max_attempts=0 is not optional: a botocore retry would re-invoke the specialist,
# running the whole turn a second time.
AGENTCORE_CFG = Config(
    connect_timeout=10,
    read_timeout=900,
    retries={"max_attempts": 0},
    tcp_keepalive=True,
)

_runtime_arns: dict[str, str] | None = None


def runtime_arns() -> dict[str, str]:
    """agentId -> runtime ARN, published by Terraform into one SSM parameter.

    Read lazily and cached: a newly-added agent is picked up on the next cold
    start, with no code change and no redeploy of the orchestrator.
    """
    global _runtime_arns
    if _runtime_arns is None:
        ssm = boto3.client("ssm", region_name=REGION)
        raw = ssm.get_parameter(Name=RUNTIME_MAP_PARAM)["Parameter"]["Value"]
        _runtime_arns = json.loads(raw)
    return _runtime_arns


# ── System prompt assembly ────────────────────────────────────────────────────

_MEMORY_RULES = """
## Using recalled memory

Facts below were recalled from this candidate's earlier sessions. Treat them as
context you already know, not as something the candidate just told you.

- Use them to skip questions you already have the answer to.
- Never read them back as a list, and never say "I remember that…".
- If a fact contradicts what the candidate says now, the candidate is right.
"""


def _facts_block(facts: list[dict]) -> str:
    if not facts:
        return ""
    lines = "\n".join(f"- {f.get('text')}" for f in facts if f.get("text"))
    return f"\n\n## Known facts about this candidate\n\n{lines}\n{_MEMORY_RULES}"


def _history_block(turns: list[dict]) -> str:
    if not turns:
        return ""
    lines = []
    for msg in turns[-10:]:
        role = (msg.get("role") or "").upper()
        content = msg.get("content")
        if isinstance(content, dict):
            content = content.get("text", "")
        elif isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        if content:
            lines.append(f"{role}: {str(content)[:600]}")
    if not lines:
        return ""
    return "\n\n## This session so far\n\n" + "\n".join(lines)


def build_system_prompt(facts: list[dict], turns: list[dict]) -> tuple[str, dict[str, int]]:
    """Assemble the system prompt, and report the size of each part that went in.

    The API bills one `inputTokens` figure for the whole request, so it cannot
    tell an attendee *why* their prompt is large. The character counts here can:
    they are measured, not estimated, and they are usually enough to see that the
    skill docs dwarf everything else.
    """
    parts = [AGENT.system_prompt]
    sizes = {"agentPrompt": len(AGENT.system_prompt)}

    if AGENT.role == "orchestrator":
        roster = agent_config.load_roster()
        cards = "\n\n".join(
            f"### `{a['id']}` — {a['name']}\n{a['description']}" for a in roster
        )
        block = f"\n\n## Available specialists\n\n{cards}"
        parts.append(block)
        sizes["specialistRoster"] = len(block)
    else:
        skills = "".join(
            f"\n\n---\n\n# Skill: {name}\n\n{doc}"
            for name, doc in AGENT.skill_docs.items()
        )
        parts.append(skills)
        sizes["skillDocs"] = len(skills)

    history = _history_block(turns)
    parts.append(history)
    sizes["sessionHistory"] = len(history)

    # Everything recalled from Atlas that rides along in every request.
    memory_block = _facts_block(facts)
    parts.append(memory_block)
    sizes["atlasMemory"] = len(memory_block)

    return "".join(parts), sizes


def build_model() -> BedrockModel:
    """The model for this agent, with extended thinking if the definition asks.

    Thinking is what produces the reasoning stream: without a budget Bedrock
    sends no reasoningContent deltas, Strands emits no `reasoningText`, and the
    UI's reasoning panel stays empty no matter how fast the transport is.
    """
    if not AGENT.thinking_budget:
        return BedrockModel(
            model_id=AGENT.model,
            region_name=REGION,
            max_tokens=AGENT.max_tokens,
            temperature=AGENT.temperature,
        )
    return BedrockModel(
        model_id=AGENT.model,
        region_name=REGION,
        max_tokens=AGENT.max_tokens,
        # Not AGENT.temperature: Bedrock rejects any other value alongside
        # thinking. Sampling is fixed at 1.0 whenever the budget is set.
        temperature=1.0,
        additional_request_fields={
            "thinking": {"type": "enabled", "budget_tokens": AGENT.thinking_budget}
        },
    )


# ── Streaming a Strands agent into our event protocol ─────────────────────────

async def stream_agent(agent: Agent, prompt: str, tr: Trace):
    """Yield UI events for one Strands run. Returns nothing; text is accumulated
    by the caller from the `delta` events it sees."""
    seen_tools: set[str] = set()

    async for event in agent.stream_async(prompt):
        if text := event.get("data"):
            yield {"type": "delta", "agentId": AGENT.id, "text": text}

        if reasoning := (event.get("reasoningText") or event.get("reasoning_text")):
            yield {"type": "reasoning", "agentId": AGENT.id, "text": reasoning}

        # Both halves of a tool call come off `message`, not `current_tool_use`.
        # That mid-stream dict starts life with input="" and grows one JSON
        # fragment per delta, so reading it on first sight — which is what any
        # dedupe by toolUseId does — captures the empty string every time. The
        # message carries the arguments already parsed, and for the model's own
        # message it arrives before the tool runs, so the step is still live.
        message = event.get("message") or {}
        for block in message.get("content", []) or []:
            if not isinstance(block, dict):
                continue

            use = block.get("toolUse")
            if use and (use_id := use.get("toolUseId")) and use_id not in seen_tools:
                seen_tools.add(use_id)
                yield {"type": "trace", **tr.event(
                    "tool.call", tool=use.get("name"), toolUseId=use_id,
                    # dumps, not str: the panel renders this as JSON, and a
                    # Python repr of a dict is not JSON.
                    input=json.dumps(use.get("input"), default=str)[:2000],
                )}

            result = block.get("toolResult")
            if not result:
                continue
            body = " ".join(
                c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)
            )
            yield {"type": "trace", **tr.event(
                "tool.result", toolUseId=result.get("toolUseId"),
                status=result.get("status", "success"),
                resultChars=len(body), preview=body[:1500],
            )}


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _handoff_tool(state: dict):
    @tool(description=(
        "Route this message to a specialist. Call this exactly once, then stop. "
        "agent_id must be one of the IDs in your Available specialists list. "
        "summary is a short brief for the specialist: what the candidate wants, plus "
        "anything from known facts or this session that saves them re-asking."
    ))
    def handoff(agent_id: str, summary: str) -> str:
        state["agentId"] = agent_id
        state["summary"] = summary
        return f"Routed to {agent_id}."
    return handoff


def _sse_events(body):
    """Parse an AgentCore streaming response back into our event dicts."""
    for raw in body.iter_lines():
        if not raw:
            continue
        line = raw.decode() if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            yield {"type": "delta", "text": payload}


def invoke_specialist(agent_id: str, prompt: str, session_id: str, user_id: str,
                      parent_trace_id: str = ""):
    # ponytail: sync generator inside the async path — iter_lines blocks the event
    # loop. Fine at workshop scale (one attendee, one stack, one conversation).
    # Move to httpx.AsyncClient if this ever serves concurrent sessions.
    arns = runtime_arns()
    if agent_id not in arns:
        raise KeyError(
            f"'{agent_id}' has no deployed runtime. Known: {', '.join(sorted(arns))}"
        )
    client = boto3.client("bedrock-agentcore", region_name=REGION, config=AGENTCORE_CFG)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arns[agent_id],
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        payload=json.dumps({
            "prompt": prompt, "sessionId": session_id, "userId": user_id,
            "parentTraceId": parent_trace_id,
        }).encode(),
    )
    yield from _sse_events(resp["response"])


async def run_orchestrator(prompt: str, session_id: str, user_id: str, tr: Trace):
    facts = []
    if AGENT.long_term:
        facts = memory.recall_facts(user_id, prompt)
        yield {"type": "trace", **tr.event(
            "memory.recall", source="agent_facts", count=len(facts),
            facts=[f.get("text") for f in facts],
        )}

    turns = []
    if AGENT.short_term:
        turns = memory.short_term_turns(user_id, session_id)
        yield {"type": "trace", **tr.event(
            "memory.short_term", source="agentcore", turns=len(turns)
        )}

    state: dict = {}
    system_prompt, prompt_sizes = build_system_prompt(facts, turns)
    orchestrator = Agent(
        model=build_model(),
        system_prompt=system_prompt,
        tools=[_handoff_tool(state)],
        callback_handler=None,
    )

    # Routing is one short classification call, but it is also the whole wait
    # before a specialist exists to stream anything — run to completion it is
    # several seconds of blank UI. Streamed, its reasoning and its handoff tool
    # call reach the candidate as they happen. The text is buffered rather than
    # forwarded: if a specialist takes over, this agent's words are not the
    # answer, and appending them would prepend routing chatter to the response.
    direct = ""
    async for event in stream_agent(orchestrator, prompt, tr):
        if event.get("type") == "delta":
            direct += event.get("text", "")
            continue
        yield event

    metrics = orchestrator.event_loop_metrics
    yield {"type": "trace", **tr.usage(
        "route", AGENT.model, metrics.accumulated_usage,
        latencyMs=metrics.accumulated_metrics.get("latencyMs", 0),
        cycles=metrics.cycle_count,
        promptChars=prompt_sizes, userPromptChars=len(prompt),
    )}

    answer = ""
    if target := state.get("agentId"):
        yield {"type": "trace", **tr.event(
            "route", to=target, summary=state.get("summary", "")
        )}
        yield {"type": "agent", "agentId": target}

        brief = f"{state.get('summary', '')}\n\nCandidate's message: {prompt}"
        for event in invoke_specialist(target, brief, session_id, user_id, tr.trace_id):
            if event.get("type") == "delta":
                answer += event.get("text", "")
            yield event
    else:
        answer = direct
        yield {"type": "trace", **tr.event("route", to=None, reason="no specialist matched")}
        yield {"type": "delta", "agentId": AGENT.id, "text": answer}

    # Persistence runs after the answer has streamed, so it costs the candidate
    # no perceived latency.
    if AGENT.long_term and answer.strip():
        memory.touch_session(user_id, session_id, prompt)
        written = memory.store_messages(
            user_id, session_id, state.get("agentId") or AGENT.id,
            [("user", prompt), ("assistant", answer)],
        )
        yield {"type": "trace", **tr.event(
            "memory.write", collection="chat_messages", count=written)}

        stored, extract_usage = memory.extract_facts(user_id, session_id, prompt, answer)
        yield {"type": "trace", **tr.event(
            "memory.extract", collection="agent_facts", count=len(stored),
            facts=[f["text"] for f in stored],
        )}
        # Easy to forget this one: it runs after the answer has already streamed,
        # so it costs the candidate no latency but every bit as much money.
        yield {"type": "trace", **tr.usage(
            "memory.extract", memory.EXTRACTION_MODEL, extract_usage,
        )}

    if AGENT.short_term and answer.strip():
        memory.record_turn(user_id, session_id, prompt, answer)


# ── Specialist ────────────────────────────────────────────────────────────────

async def run_specialist(prompt: str, session_id: str, user_id: str, tr: Trace):
    facts = []
    if AGENT.long_term:
        facts = memory.recall_facts(user_id, prompt)
        yield {"type": "trace", **tr.event(
            "memory.recall", source="agent_facts", count=len(facts),
            facts=[f.get("text") for f in facts],
        )}

    built = agent_tools.build_tools(AGENT)
    yield {"type": "trace", **tr.event(
        "tools.loaded", count=len(built),
        tools=[getattr(t, "tool_name", getattr(t, "__name__", "?")) for t in built],
        collections=AGENT.collections,
    )}

    system_prompt, prompt_sizes = build_system_prompt(facts, [])
    specialist = Agent(
        model=build_model(),
        system_prompt=system_prompt,
        tools=built,
        callback_handler=None,
    )
    async for event in stream_agent(specialist, prompt, tr):
        yield event

    # Only complete once the stream has drained — Strands accumulates usage
    # across every cycle of the loop, including the ones spent on tool calls.
    m = specialist.event_loop_metrics
    yield {"type": "trace", **tr.usage(
        "answer", AGENT.model, m.accumulated_usage,
        latencyMs=m.accumulated_metrics.get("latencyMs", 0),
        cycles=m.cycle_count,
        promptChars=prompt_sizes, userPromptChars=len(prompt),
        toolResultChars=sum(
            e.get("resultChars", 0) for e in tr.events if e.get("kind") == "tool.result"
        ),
    )}


# ── Entrypoint ────────────────────────────────────────────────────────────────

@app.entrypoint
async def invoke(payload: dict):
    prompt = (payload or {}).get("prompt", "").strip()
    user_id = (payload or {}).get("userId") or "anonymous"

    # AgentCore rejects a runtimeSessionId under 33 chars, and the orchestrator
    # forwards this one verbatim when it invokes a specialist.
    session_id = (payload or {}).get("sessionId") or ""
    if len(session_id) < 33:
        session_id = f"sess-{uuid.uuid4().hex}"

    if not prompt:
        yield {"type": "error", "message": "payload.prompt is required"}
        return

    tr = Trace(AGENT.id, session_id, user_id,
               parent_trace_id=(payload or {}).get("parentTraceId", ""))
    agent_tools.ctx.set({"userId": user_id, "sessionId": session_id})
    yield {"type": "trace", **tr.event(
        "turn.start", role=AGENT.role, model=AGENT.model, promptChars=len(prompt)
    )}

    runner = run_orchestrator if AGENT.role == "orchestrator" else run_specialist
    try:
        async for event in runner(prompt, session_id, user_id, tr):
            yield event
    except Exception as exc:  # noqa: BLE001 — surface the failure to the UI, then re-raise into logs
        tr.event("turn.error", error=str(exc), errorType=type(exc).__name__)
        yield {"type": "error", "message": str(exc)}
        tr.flush(status="error", error=str(exc))
        raise

    yield {"type": "trace", **tr.event("turn.end")}
    tr.flush()
    yield {"type": "done", "sessionId": session_id, "traceId": tr.trace_id}


if __name__ == "__main__":
    app.run()
