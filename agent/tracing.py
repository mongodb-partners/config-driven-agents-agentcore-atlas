"""Turn traces — two sinks, one recorder.

Every event goes to stdout as one JSON line (AgentCore ships stdout to
CloudWatch Logs, and its built-in OTel exporter carries the spans), and the
whole turn is flushed to the Atlas `agent_traces` collection at the end so the
UI can render it and attendees can query agent behaviour with MongoDB instead
of CloudWatch Insights.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timezone

import mongo_mcp


def _now_ms() -> int:
    return int(time.time() * 1000)


class Trace:
    def __init__(self, agent_id: str, session_id: str, user_id: str) -> None:
        self.trace_id = uuid.uuid4().hex
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

    def flush(self, status: str = "ok", error: str | None = None) -> None:
        doc = {
            "traceId": self.trace_id,
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
