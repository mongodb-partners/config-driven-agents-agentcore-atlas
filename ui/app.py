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

_ACTIVITY = {
    "turn.start": "Starting turn",
    "memory.recall": "Recalling what we know about you",
    "memory.short_term": "Loading this session",
    "route": "Choosing a specialist",
    "tools.loaded": "Preparing tools",
    "tool.call": "Querying MongoDB Atlas",
    "tool.result": "Reading results",
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
    return _ACTIVITY.get(kind, kind)


def render_trace(events: list[dict], trace_id: str) -> None:
    """The developer surface. Deliberately four things, not forty."""
    with st.expander(f"Developer trace · `{trace_id[:12]}`", expanded=False):
        total = max((e.get("atMs", 0) for e in events), default=0)
        routed = next((e.get("to") for e in events if e.get("kind") == "route"), None)
        tool_calls = [e for e in events if e.get("kind") == "tool.call"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Duration", f"{total / 1000:.1f}s")
        c2.metric("Specialist", routed or "—")
        c3.metric("Tool calls", len(tool_calls))

        tab_flow, tab_tools, tab_memory, tab_raw = st.tabs(
            ["Flow", "Atlas calls", "Memory", "Raw events"]
        )

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
                st.code(call.get("input", ""), language="json")
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

with st.sidebar:
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

        try:
            for event in stream_turn(prompt, st.session_state.session_id):
                kind = event.get("type")

                if kind == "trace":
                    events.append(event)
                    status.caption(f"🧠 **{event.get('agentId')}** · {describe(event)}")

                elif kind == "agent":
                    active_agent = event["agentId"]
                    status.caption(f"🧠 **{active_agent}** is working…")

                elif kind == "reasoning":
                    reasoning += event.get("text", "")
                    reasoning_box.info(f"**Reasoning** — {reasoning}")

                elif kind == "delta":
                    answer += event.get("text", "")
                    answer_box.markdown(answer)

                elif kind == "error":
                    status.error(event.get("message", "unknown error"))

                elif kind == "done":
                    trace_id = event.get("traceId", "")

        except Exception as exc:  # noqa: BLE001 — show the failure, keep the session usable
            status.error(f"Invocation failed: {exc}")

        status.empty()
        if not answer:
            answer = "_No response produced. Open the developer trace below to see where the turn stopped._"
            answer_box.markdown(answer)

        if events:
            render_trace(events, trace_id)

    st.session_state.messages.append({
        "role": "assistant", "content": answer, "events": events, "traceId": trace_id,
    })
