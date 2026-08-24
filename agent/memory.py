"""Memory, split by lifetime.

- **Short term** — AgentCore Memory service. The in-session turn buffer, managed
  for us, scoped to (actor, session).
- **Long term** — MongoDB Atlas, three collections:
  - `agent_facts`    durable facts about a candidate, extracted by an LLM
  - `chat_messages`  every turn, embedded and semantically searchable
  - `chat_sessions`  one row per conversation, for the UI's session list

Facts are recalled at the *start* of a session. `chat_messages` is recalled
*mid-run*, by an agent that decides it needs something from earlier.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import boto3

import embeddings
import mongo_mcp

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
REGION = embeddings.REGION
EXTRACTION_MODEL = os.environ.get(
    "FACT_EXTRACTION_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

_bedrock = None


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=embeddings.REGION)
    return _bedrock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Short-term: AgentCore Memory ──────────────────────────────────────────────

def _memory_client():
    from bedrock_agentcore.memory import MemoryClient
    return MemoryClient(region_name=REGION)


def short_term_turns(user_id: str, session_id: str, k: int = 10) -> list[dict]:
    if not MEMORY_ID:
        return []
    turns = _memory_client().get_last_k_turns(
        memory_id=MEMORY_ID, actor_id=user_id, session_id=session_id, k=k
    )
    # get_last_k_turns returns a list of turns, each a list of message dicts.
    return [msg for turn in turns for msg in turn]


def record_turn(user_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    if not MEMORY_ID:
        return
    _memory_client().create_event(
        memory_id=MEMORY_ID,
        actor_id=user_id,
        session_id=session_id,
        messages=[(user_text, "USER"), (assistant_text, "ASSISTANT")],
    )


# ── Long-term: facts ──────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """Extract durable facts about the candidate from this exchange.

A durable fact is stable and useful in a future, unrelated conversation: their
skills, current role, seniority, years of experience, location, target role,
constraints (remote-only, visa, notice period), and stated preferences.

NOT durable, do not extract: the question they asked, what the assistant
answered, anything about a specific job or company unless the candidate stated a
preference about it, and anything you inferred rather than read.

Return JSON only, no prose:
{"facts": [{"text": "<one self-contained sentence>", "kind": "skill|role|preference|constraint|context"}]}

If there is nothing durable, return {"facts": []}.

CANDIDATE: %s

ASSISTANT: %s"""


def extract_facts(user_id: str, session_id: str, user_text: str,
                  assistant_text: str) -> tuple[list[dict], dict]:
    """LLM-extract durable facts and persist them.

    Returns what was stored, plus the call's token usage — this is a second model
    call hiding behind a memory write, and a cost dashboard that ignores it
    under-reports every turn.
    """
    resp = _bedrock_client().converse(
        modelId=EXTRACTION_MODEL,
        messages=[{"role": "user", "content": [
            {"text": _EXTRACTION_PROMPT % (user_text, assistant_text[:4000])}
        ]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.0},
    )
    usage = resp.get("usage", {})
    raw = resp["output"]["message"]["content"][0]["text"]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return [], usage
    try:
        facts = json.loads(raw[start:end + 1]).get("facts", [])
    except json.JSONDecodeError:
        return [], usage

    kept = [
        ((f.get("text") or "").strip(), f.get("kind", "context"))
        for f in facts
        if (f.get("text") or "").strip()
    ]
    if not kept:
        return [], usage

    # One embedding call for all facts, not one per fact — each round trip is a
    # rate-limit opportunity, and they are all available at once. In auto mode
    # this costs nothing: the text goes in as text and Atlas embeds it.
    fields = embeddings.index_fields([t for t, _ in kept])
    docs = [
        {
            "userId": user_id,
            "sessionId": session_id,
            "text": text,
            "kind": kind,
            "createdAt": _now(),
            **field,
        }
        for (text, kind), field in zip(kept, fields)
    ]
    mongo_mcp.insert("agent_facts", docs)
    return [{"text": d["text"], "kind": d["kind"]} for d in docs], usage


def recall_facts(user_id: str, query_text: str, limit: int = 6) -> list[dict]:
    """Semantic recall over everything known about this candidate."""
    return mongo_mcp.vector_search(
        "agent_facts",
        query_text,
        limit=limit,
        filter={"userId": user_id},
    )


# ── Long-term: chat messages ──────────────────────────────────────────────────

def store_messages(user_id: str, session_id: str, agent_id: str,
                   turns: list[tuple[str, str]]) -> int:
    """Persist `[(role, text), ...]` with one embedding call and one insert.

    The user message and the assistant reply are both known at the end of a turn,
    so embedding them together halves the round trips to the embedding provider.
    """
    kept = [(role, (text or "").strip()[:8000]) for role, text in turns]
    kept = [(role, text) for role, text in kept if text]
    if not kept:
        return 0

    # ponytail: in auto mode Atlas embeds these asynchronously, so a turn is not
    # semantically recallable the instant it is written. Nobody asks about the
    # sentence they just typed, and the short-term buffer covers the current
    # session anyway — add a read-after-write wait only if that stops being true.
    fields = embeddings.index_fields([text for _, text in kept])
    mongo_mcp.insert("chat_messages", [
        {
            "userId": user_id,
            "sessionId": session_id,
            "agentId": agent_id,
            "role": role,
            "text": text,
            "createdAt": _now(),
            **field,
        }
        for (role, text), field in zip(kept, fields)
    ])
    return len(kept)


def recall_conversation(user_id: str, query_text: str, limit: int = 5,
                        exclude_session: str | None = None) -> list[dict]:
    """Semantic search across this candidate's past turns.

    Scoped to `userId` here rather than in the tool signature — an agent chooses
    *what* to look for, never *whose* history to look in.
    """
    mongo_filter: dict = {"userId": user_id}
    if exclude_session:
        mongo_filter["sessionId"] = {"$ne": exclude_session}
    return mongo_mcp.vector_search(
        "chat_messages", query_text, limit=limit, filter=mongo_filter
    )


# ── Long-term: sessions ───────────────────────────────────────────────────────

def touch_session(user_id: str, session_id: str, first_message: str) -> None:
    """Upsert the session row the UI's session list reads."""
    mongo_mcp.call(
        "update-many",
        collection="chat_sessions",
        filter={"sessionId": session_id},
        update={
            "$set": {"userId": user_id, "updatedAt": _now()},
            "$setOnInsert": {
                "sessionId": session_id,
                "createdAt": _now(),
                "title": first_message[:80],
            },
            "$inc": {"turns": 1},
        },
        upsert=True,
    )
