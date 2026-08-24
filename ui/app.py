"""Multi-agent RIV workshop — chat UI.

There is no backend service. This process talks to two things: the orchestrator
AgentCore Runtime (over SigV4, using the EC2 instance role) and Atlas (read-only,
for session history and traces). Everything else happens inside the runtimes.

Three surfaces, and nothing else:
  1. the agents' reasoning as it happens
  2. the response, streamed
  3. a developer trace of the turn — routing, memory, tool calls, timings
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
import streamlit as st
from botocore.config import Config as BotoConfig
from pymongo import MongoClient

REGION = os.environ.get("AWS_REGION", "us-east-1")
ORCHESTRATOR_ARN = os.environ["ORCHESTRATOR_RUNTIME_ARN"]
DB_NAME = os.environ.get("MONGODB_DB", "riv_workshop")
USER_ID = os.environ.get("WORKSHOP_USER_ID", "workshop-user")
LOG_GROUP = os.environ.get("AGENT_LOG_GROUP", "")

st.set_page_config(page_title="Multi-Agent RIV Workshop", layout="wide")

# ── MongoDB design system ─────────────────────────────────────────────────────
# Palette, type scale and the Euclid / Value Serif / Source Code Pro faces are
# all in .streamlit/config.toml. Two LeafyGreen rules have no config equivalent:
# H1 and H2 are green.dark2 (gray.light2 in dark mode), and the serif face stops
# at H2 — H3 down is Euclid.
st.html(
    "<style>"
    "h1,h2{color:%s}"
    "h3,h4,h5,h6{font-family:'Euclid Circular A',Helvetica,Arial,sans-serif}"
    "</style>" % ("#E8EDEB" if st.context.theme.type == "dark" else "#00684A")
)


# ── Connections ───────────────────────────────────────────────────────────────

@st.cache_resource
def mongo():
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        secret = boto3.client("secretsmanager", region_name=REGION).get_secret_value(
            SecretId=os.environ["MONGODB_URI_SECRET_ARN"]
        )["SecretString"].strip()
        uri = json.loads(secret)["connectionString"] if secret.startswith("{") else secret
    return MongoClient(uri, serverSelectionTimeoutMS=15000)[DB_NAME]


@st.cache_resource
def agentcore():
    # read_timeout is the gap allowed between streamed chunks, not the total
    # call. The 60s default truncates any turn with a slow tool call in it.
    # Retries must be 0 — a retry would re-run the whole turn.
    return boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=BotoConfig(
            connect_timeout=10,
            read_timeout=900,
            retries={"max_attempts": 0},
            tcp_keepalive=True,
        ),
    )


def new_session_id() -> str:
    # AgentCore requires 33+ characters.
    return f"sess-{uuid.uuid4().hex}"


# ── Turn execution ────────────────────────────────────────────────────────────

def stream_turn(prompt: str, session_id: str):
    """Invoke the orchestrator and yield the event dicts it emits."""
    resp = agentcore().invoke_agent_runtime(
        agentRuntimeArn=ORCHESTRATOR_ARN,
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        payload=json.dumps({
            "prompt": prompt, "sessionId": session_id, "userId": USER_ID,
        }).encode(),
    )
    for raw in resp["response"].iter_lines():
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


# ── Developer trace ───────────────────────────────────────────────────────────

# Enough to show the shape of the turn without pushing the answer off screen.
_VISIBLE_STEPS = 6

_ACTIVITY = {
    "turn.start": "Starting turn",
    "memory.recall": "Recalling what we know about you",
    "memory.short_term": "Loading this session",
    "route": "Choosing a specialist",
    "tools.loaded": "Preparing tools",
    "tool.call": "Querying MongoDB Atlas",
    "tool.result": "Reading results",
    "usage": "Counting tokens",
    "memory.write": "Saving this exchange",
    "memory.extract": "Extracting what's worth remembering",
    "turn.end": "Done",
}


def describe(ev: dict) -> str:
    kind = ev.get("kind", "")
    if kind == "route":
        return f"Routing to **{ev['to']}**" if ev.get("to") else "Answering directly"
    if kind == "tool.call":
        return f"Calling `{ev.get('tool')}`"
    if kind == "memory.recall":
        return f"Recalled {ev.get('count', 0)} fact(s)"
    if kind == "memory.extract":
        return f"Stored {ev.get('count', 0)} new fact(s)"
    if kind == "usage":
        return (f"`{ev.get('source')}` used {ev.get('totalTokens', 0):,} tokens "
                f"(${ev.get('costUsd', 0):.5f})")
    return _ACTIVITY.get(kind, kind)


_SEGMENT_LABELS = {
    "agentPrompt": "Agent system prompt",
    "skillDocs": "Skill documents",
    "specialistRoster": "Specialist roster",
    "sessionHistory": "Session history",
    "atlasMemory": "Atlas memory (recalled facts)",
}

# The API bills one input figure for the whole request, so a per-segment token
# count does not exist. Characters are measured exactly; this only converts them
# into a number that is comparable to the billed one.
CHARS_PER_TOKEN = 4


def render_tokens(events: list[dict]) -> None:
    """Where the tokens went, and what they cost."""
    calls = [e for e in events if e.get("kind") == "usage"]
    if not calls:
        st.caption("No token usage reported for this turn.")
        return

    def total(field: str) -> int:
        return sum(e.get(field, 0) for e in calls)

    cost = sum(e.get("costUsd", 0.0) for e in calls)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tokens in", f"{total('inputTokens'):,}")
    c2.metric("Tokens out", f"{total('outputTokens'):,}")
    c3.metric("LLM calls", len(calls))
    c4.metric("Cost", f"${cost:.5f}")

    cached = total("cacheReadInputTokens")
    if cached:
        st.caption(f"{cached:,} input token(s) served from cache at 0.1x, "
                   f"{total('cacheWriteInputTokens'):,} written at 1.25x.")

    st.markdown("**Per call** — one turn is never one call.")
    st.dataframe(
        [
            {
                "Call": e.get("source"),
                "Agent": e.get("agentId"),
                "In": e.get("inputTokens", 0),
                "Out": e.get("outputTokens", 0),
                "Cached in": e.get("cacheReadInputTokens", 0),
                "Cycles": e.get("cycles", 1),
                "Cost (USD)": round(e.get("costUsd", 0.0), 6),
            }
            for e in calls
        ],
        hide_index=True,
        width="stretch",
    )

    # What was actually in the prompt. This is the part the token total cannot
    # explain on its own.
    segments: dict[str, int] = {}
    for e in calls:
        for name, chars in (e.get("promptChars") or {}).items():
            if chars:
                segments[_SEGMENT_LABELS.get(name, name)] = (
                    segments.get(_SEGMENT_LABELS.get(name, name), 0) + chars
                )
    atlas_chars = sum(e.get("toolResultChars", 0) for e in calls)
    if atlas_chars:
        segments["Atlas tool results (fed back to the model)"] = atlas_chars
    user_chars = sum(e.get("userPromptChars", 0) for e in calls)
    if user_chars:
        segments["The candidate's message"] = user_chars

    if segments:
        st.markdown("**What filled the context window**")
        st.dataframe(
            [
                {
                    "Segment": label,
                    "Characters": f"{chars:,}",
                    "≈ Tokens": f"{chars // CHARS_PER_TOKEN:,}",
                }
                for label, chars in sorted(segments.items(), key=lambda s: -s[1])
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(
            f"Characters are measured. The token column divides by {CHARS_PER_TOKEN} "
            "to make the segments comparable — the billed figure is 'Tokens in' "
            "above, which also covers the assistant's own turns across cycles."
        )


def render_trace(events: list[dict], trace_id: str) -> None:
    """The developer surface. Deliberately five things, not forty."""
    with st.expander(f"Developer trace · `{trace_id[:12]}`", expanded=False):
        total = max((e.get("atMs", 0) for e in events), default=0)
        routed = next((e.get("to") for e in events if e.get("kind") == "route"), None)
        tool_calls = [e for e in events if e.get("kind") == "tool.call"]
        turn_cost = sum(e.get("costUsd", 0.0) for e in events if e.get("kind") == "usage")
        turn_tokens = sum(e.get("totalTokens", 0) for e in events if e.get("kind") == "usage")

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Duration", f"{total / 1000:.1f}s")
        c2.metric("Specialist", routed or "—")
        c3.metric("Tool calls", len(tool_calls))
        c4.metric("Tokens", f"{turn_tokens:,}")
        c5.metric("Cost", f"${turn_cost:.5f}")

        tab_flow, tab_tokens, tab_tools, tab_memory, tab_raw = st.tabs(
            ["Flow", "Tokens & cost", "Atlas calls", "Memory", "Raw events"]
        )

        with tab_tokens:
            render_tokens(events)

        with tab_flow:
            st.caption("Every step, in order, with the wall-clock offset from turn start.")
            for ev in events:
                st.markdown(
                    f"`{ev.get('atMs', 0):>6}ms`  **{ev.get('agentId')}**  ·  {describe(ev)}"
                )

        with tab_tools:
            results = {e.get("toolUseId"): e for e in events if e.get("kind") == "tool.result"}
            if not tool_calls:
                st.caption("No Atlas calls this turn.")
            for call in tool_calls:
                result = results.get(call.get("toolUseId"), {})
                st.markdown(f"**`{call.get('tool')}`** · {call.get('atMs')}ms · "
                            f"{result.get('resultChars', 0)} chars returned")
                if call.get("input"):
                    st.code(call["input"], language="json")
                else:
                    # Traces written before the arguments were captured correctly
                    # keep their blank input forever. Say so, rather than showing
                    # an empty box that reads as a UI fault.
                    st.caption("Arguments not recorded for this call.")
                if preview := result.get("preview"):
                    st.caption("Result preview")
                    st.code(preview, language="json")
                st.divider()

        with tab_memory:
            for ev in events:
                if ev.get("kind") == "memory.recall":
                    st.markdown(f"**Recalled from `agent_facts`** ({ev.get('count', 0)})")
                    for fact in ev.get("facts") or []:
                        st.markdown(f"- {fact}")
                if ev.get("kind") == "memory.extract":
                    st.markdown(f"**Extracted into `agent_facts`** ({ev.get('count', 0)})")
                    for fact in ev.get("facts") or []:
                        st.markdown(f"- {fact}")
                if ev.get("kind") == "memory.short_term":
                    st.caption(f"AgentCore short-term memory: {ev.get('turns', 0)} turn(s) loaded")
            st.caption("Long-term facts and chat messages live in Atlas; short-term turns "
                       "live in the AgentCore Memory service.")

        with tab_raw:
            st.caption("Exactly what the runtimes emitted — the same JSON lines that reach "
                       "CloudWatch Logs.")
            st.code(json.dumps(events, indent=2), language="json")

        if LOG_GROUP:
            st.caption(f"CloudWatch log group: `{LOG_GROUP}` · filter on `{trace_id}`")


# ── Session state ─────────────────────────────────────────────────────────────

if "session_id" not in st.session_state:
    st.session_state.session_id = new_session_id()
if "messages" not in st.session_state:
    st.session_state.messages = []

def workshop_metrics() -> dict:
    """Aggregate every turn this user has ever run, in one round trip.

    A "request" is one orchestrator trace. The specialist it invokes flushes its
    own document from its own runtime, joined back by `parentTraceId` — so tokens
    sum across both, while the request count and latency come from the root only.
    """
    return next(iter(mongo().agent_traces.aggregate([
        {"$match": {"userId": USER_ID}},
        {"$project": {
            "isRoot": {"$cond": [
                {"$eq": [{"$ifNull": ["$parentTraceId", None]}, None]}, 1, 0]},
            "durationMs": {"$ifNull": ["$durationMs", 0]},
            "usage": {"$filter": {
                "input": {"$ifNull": ["$events", []]},
                "cond": {"$eq": ["$$this.kind", "usage"]},
            }},
            "toolCalls": {"$size": {"$filter": {
                "input": {"$ifNull": ["$events", []]},
                "cond": {"$eq": ["$$this.kind", "tool.call"]},
            }}},
        }},
        {"$group": {
            "_id": None,
            "requests": {"$sum": "$isRoot"},
            # Root duration already spans the specialist — the orchestrator
            # blocks on that stream — so this is end-to-end, not orchestrator-only.
            "latencyMs": {"$sum": {"$multiply": ["$durationMs", "$isRoot"]}},
            "tokens": {"$sum": {"$sum": "$usage.totalTokens"}},
            "cost": {"$sum": {"$sum": "$usage.costUsd"}},
            "llmCalls": {"$sum": {"$size": "$usage"}},
            "toolCalls": {"$sum": "$toolCalls"},
        }},
    ])), {})


def render_dashboard() -> None:
    st.markdown("### Live metrics")
    try:
        m = workshop_metrics()
    except Exception as exc:  # noqa: BLE001 — metrics must never block chatting
        st.caption(f"Metrics unavailable: {exc}")
        return

    requests = m.get("requests", 0)
    if not requests:
        st.caption("No turns recorded yet. Ask something.")
        return

    def per_request(field: str) -> float:
        return m.get(field, 0) / requests

    a, b = st.columns(2)
    a.metric("Total tokens", f"{m.get('tokens', 0):,}")
    b.metric("Cost (USD)", f"${m.get('cost', 0.0):.4f}")
    a.metric("Avg latency", f"{per_request('latencyMs') / 1000:.1f}s")
    b.metric("Avg tokens / req", f"{per_request('tokens'):,.0f}")
    a.metric("Avg tool calls / req", f"{per_request('toolCalls'):.1f}")
    b.metric("Avg LLM calls / req", f"{per_request('llmCalls'):.1f}")
    st.caption(f"Across {requests} request(s), from the `agent_traces` collection. "
               "Refresh the page to recompute.")


with st.sidebar:
    render_dashboard()
    st.divider()

    st.markdown("### Sessions")
    if st.button("New chat", use_container_width=True):
        st.session_state.session_id = new_session_id()
        st.session_state.messages = []
        st.rerun()

    try:
        sessions = list(mongo().chat_sessions.find(
            {"userId": USER_ID}, {"_id": 0}
        ).sort("updatedAt", -1).limit(15))
    except Exception as exc:  # noqa: BLE001 — a dead session list must not block chatting
        sessions = []
        st.caption(f"Session list unavailable: {exc}")

    for s in sessions:
        label = (s.get("title") or s["sessionId"])[:45]
        active = s["sessionId"] == st.session_state.session_id
        if st.button(("● " if active else "") + label, key=s["sessionId"],
                     use_container_width=True):
            st.session_state.session_id = s["sessionId"]
            st.session_state.messages = [
                {"role": m["role"], "content": m["text"]}
                for m in mongo().chat_messages.find(
                    {"sessionId": s["sessionId"]}, {"_id": 0, "role": 1, "text": 1}
                ).sort("createdAt", 1)
            ]
            st.rerun()

    st.divider()
    st.caption(f"Long-term memory is shared across every session for `{USER_ID}`. "
               "A new chat still remembers you.")


# ── Chat ──────────────────────────────────────────────────────────────────────

st.title("Multi-agent career assistant")
st.caption("Orchestrator routes to a specialist. Every Atlas read goes through the "
           "MongoDB MCP server on its own AgentCore Runtime.")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("events"):
            render_trace(msg["events"], msg.get("traceId", ""))

if prompt := st.chat_input("Ask about roles, skills, pay, or a company…"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status = st.empty()
        reasoning_box = st.empty()
        answer_box = st.empty()

        answer, reasoning, events, trace_id = "", "", [], ""
        active_agent = "orchestrator"
        steps: list[str] = []

        # Painted before the runtime is even called, because everything after
        # this line is network: the orchestrator has to wake, recall memory and
        # route before it has a word to say. Without it the panel sits blank for
        # the whole of that and the UI looks hung rather than busy.
        status.markdown("🧠 Waking the orchestrator…")

        # Streamlit re-sends the whole accumulated string on every repaint and
        # re-parses it as markdown, so painting per token is quadratic in the
        # length of the answer — which is what makes a long reasoning trace crawl.
        # Coalescing to ~10fps is visually identical and costs one comparison.
        #
        # ponytail: still O(n) bytes per repaint, because Streamlit has no
        # append-only text element — a 20k-token reasoning trace re-sends 20k
        # characters ten times a second. Fine at workshop length; if traces get
        # much longer, render reasoning into a components.html island that
        # appends client-side.
        last_paint = 0.0

        def due() -> bool:
            global last_paint
            now = time.monotonic()
            if now - last_paint < 0.1:
                return False
            last_paint = now
            return True

        try:
            for event in stream_turn(prompt, st.session_state.session_id):
                kind = event.get("type")

                # Every step is appended rather than overwriting the line before
                # it, so the panel is a trace of the turn while the turn is still
                # running — which is the only thing on screen until the model
                # produces its first token.
                if kind == "trace":
                    events.append(event)
                    steps.append(f"**{event.get('agentId')}** · {describe(event)}")
                    status.markdown("\n".join(f"- {s}" for s in steps[-_VISIBLE_STEPS:]))

                elif kind == "agent":
                    active_agent = event["agentId"]
                    steps.append(f"**{active_agent}** is working…")
                    status.markdown("\n".join(f"- {s}" for s in steps[-_VISIBLE_STEPS:]))

                elif kind == "reasoning":
                    reasoning += event.get("text", "")
                    if due():
                        reasoning_box.info(f"**Reasoning** — {reasoning}")

                elif kind == "delta":
                    answer += event.get("text", "")
                    if due():
                        answer_box.markdown(answer)

                elif kind == "error":
                    status.error(event.get("message", "unknown error"))

                elif kind == "done":
                    trace_id = event.get("traceId", "")

        except Exception as exc:  # noqa: BLE001 — show the failure, keep the session usable
            status.error(f"Invocation failed: {exc}")

        status.empty()
        if reasoning:
            reasoning_box.info(f"**Reasoning** — {reasoning}")
        if answer:
            answer_box.markdown(answer)
        if not answer:
            answer = "_No response produced. Open the developer trace below to see where the turn stopped._"
            answer_box.markdown(answer)

        if events:
            render_trace(events, trace_id)

    st.session_state.messages.append({
        "role": "assistant", "content": answer, "events": events, "traceId": trace_id,
    })
