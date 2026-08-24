"""Turn traces — two sinks, one recorder.

Every event goes to stdout as one JSON line (AgentCore ships stdout to
CloudWatch Logs, and its built-in OTel exporter carries the spans), and the
whole turn is flushed to the Atlas `agent_traces` collection at the end so the
UI can render it and attendees can query agent behaviour with MongoDB instead
of CloudWatch Insights.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import mongo_mcp

# ── Pricing ───────────────────────────────────────────────────────────────────
#
# USD per million tokens, matched by substring against the Bedrock model id.
# These are Anthropic's published list prices; Bedrock is partner-operated and
# bills separately, so treat the numbers as a default, not gospel — confirm at
# https://aws.amazon.com/bedrock/pricing/ before quoting a cost to anyone.
#
# Override without touching this file or rebuilding the image:
#   MODEL_PRICING='{"claude-haiku-4-5": {"in": 1.0, "out": 5.0}}'
_PRICING = {
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-sonnet-4-5": {"in": 3.00, "out": 15.00},
    "claude-opus-4-5": {"in": 5.00, "out": 25.00},
}
if _override := os.environ.get("MODEL_PRICING"):
    try:
        _PRICING = json.loads(_override)
    except json.JSONDecodeError as exc:
        print(json.dumps({"kind": "pricing.bad_override", "error": str(exc)}),
              file=sys.stderr, flush=True)

# Cache reads bill at 0.1x the input rate, writes at 1.25x (5-minute TTL).
_CACHE_READ, _CACHE_WRITE = 0.1, 1.25

# Roughly four characters per token for English prose. Only ever used to split
# a prompt into its parts for the developer trace — every token count that gets
# billed or totalled comes from the API, never from this.
CHARS_PER_TOKEN = 4

_USAGE_FIELDS = ("inputTokens", "outputTokens", "totalTokens",
                 "cacheReadInputTokens", "cacheWriteInputTokens")


def cost_usd(model: str, usage: dict) -> float:
    """Price one call. Returns 0.0 for a model with no entry rather than guessing."""
    rate = next((v for k, v in _PRICING.items() if k in model), None)
    if not rate:
        return 0.0
    # inputTokens is the uncached remainder — cache reads and writes are counted
    # separately by Bedrock, so these three terms do not double-count.
    return (
        usage.get("inputTokens", 0) * rate["in"]
        + usage.get("outputTokens", 0) * rate["out"]
        + usage.get("cacheReadInputTokens", 0) * rate["in"] * _CACHE_READ
        + usage.get("cacheWriteInputTokens", 0) * rate["in"] * _CACHE_WRITE
    ) / 1_000_000


def _now_ms() -> int:
    return int(time.time() * 1000)


class Trace:
    def __init__(self, agent_id: str, session_id: str, user_id: str,
                 parent_trace_id: str = "") -> None:
        self.trace_id = uuid.uuid4().hex
        # Set when the orchestrator invoked us. It is the only thing tying a
        # specialist's trace to the turn that caused it — the two runtimes flush
        # separate documents, so without it the UI cannot tell how many tokens
        # one user question actually cost.
        self.parent_trace_id = parent_trace_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.user_id = user_id
        self.started_ms = _now_ms()
        self.events: list[dict] = []

    def event(self, kind: str, **data) -> dict:
        """Record an event. Returns it so callers can stream the same dict."""
        ev = {
            "kind": kind,
            "agentId": self.agent_id,
            "atMs": _now_ms() - self.started_ms,
            **data,
        }
        self.events.append(ev)
        print(json.dumps({"traceId": self.trace_id, "sessionId": self.session_id, **ev}),
              file=sys.stdout, flush=True)
        return ev

    def usage(self, source: str, model: str, usage: dict, **extra) -> dict:
        """Record what one LLM call cost.

        `source` names the call, because a single turn makes several and they are
        easy to forget: the orchestrator's routing classification, the
        specialist's answer, and the fact-extraction pass that runs after the
        answer has already streamed.
        """
        counted = {k: int(usage.get(k) or 0) for k in _USAGE_FIELDS}
        if not counted["totalTokens"]:
            counted["totalTokens"] = counted["inputTokens"] + counted["outputTokens"]
        return self.event(
            "usage", source=source, model=model,
            costUsd=round(cost_usd(model, counted), 6), **counted, **extra,
        )

    def flush(self, status: str = "ok", error: str | None = None) -> None:
        doc = {
            "traceId": self.trace_id,
            "parentTraceId": self.parent_trace_id or None,
            "sessionId": self.session_id,
            "userId": self.user_id,
            "agentId": self.agent_id,
            "status": status,
            "durationMs": _now_ms() - self.started_ms,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "events": self.events,
        }
        if error:
            doc["error"] = error
        try:
            mongo_mcp.insert("agent_traces", [doc])
        except Exception as exc:  # noqa: BLE001 — a trace write must never fail the turn
            print(json.dumps({"kind": "trace.flush_failed", "traceId": self.trace_id,
                              "error": str(exc)}), file=sys.stderr, flush=True)
