#!/usr/bin/env python3
"""Self-check for what the developer trace records about a tool call.

The Atlas calls panel is the part of the workshop where an attendee sees the
query the agent actually sent. It shipped once with every input blank, because
Strands' `current_tool_use` is a buffer that starts empty and fills one JSON
fragment at a time — so anything that reads it on first sight reads "".

    python agent/test_tool_trace.py
"""

import asyncio
import json
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
os.environ.setdefault("CONFIG_DIR", str(REPO / "config"))
os.environ.setdefault("AGENT_ID", "job-match")

# main imports the runtime SDKs and the MCP client at module scope. None of them
# are involved in turning a Strands event into a trace event.
for name, attrs in {
    "mcp": {}, "mcp.client": {}, "mcp.client.streamable_http": {},
    "bedrock_agentcore": {}, "bedrock_agentcore.runtime": {},
    "strands": {}, "strands.models": {}, "strands.types": {}, "strands.types.tools": {},
    "mongo_mcp": {"REGION": "us-east-1", "vector_search": lambda *a, **k: []},
}.items():
    mod = sys.modules.setdefault(name, types.ModuleType(name))
    for k, v in attrs.items():
        setattr(mod, k, v)
sys.modules["bedrock_agentcore.runtime"].BedrockAgentCoreApp = lambda *a, **k: types.SimpleNamespace(
    entrypoint=lambda fn: fn, run=lambda: None
)
sys.modules["strands"].Agent = object
sys.modules["strands"].tool = lambda *a, **k: (lambda fn: fn)
sys.modules["strands.models"].BedrockModel = object

import main  # noqa: E402


class FakeAgent:
    """Replays the event shapes Strands emits for one tool call, in order."""

    def __init__(self, events):
        self._events = events

    async def stream_async(self, prompt):
        for e in self._events:
            yield e


# The real sequence: the buffer is announced empty, grows a fragment at a time,
# and only the model's completed message carries the parsed arguments.
QUERY = {"collection": "jobs", "query_text": "senior python roles in berlin"}
EVENTS = [
    {"current_tool_use": {"toolUseId": "tu-1", "name": "vector_search", "input": ""}},
    {"current_tool_use": {"toolUseId": "tu-1", "name": "vector_search",
                          "input": '{"collection": "jo'}},
    {"current_tool_use": {"toolUseId": "tu-1", "name": "vector_search",
                          "input": json.dumps(QUERY)}},
    {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "tu-1", "name": "vector_search", "input": QUERY}}]}},
    {"message": {"role": "user", "content": [
        {"toolResult": {"toolUseId": "tu-1", "status": "success",
                        "content": [{"text": "3 matching roles"}]}}]}},
    {"data": "Here are three roles."},
]


def run(events):
    class Tr:
        def event(self, kind, **data):
            return {"kind": kind, **data}

    async def drain():
        return [e async for e in main.stream_agent(FakeAgent(events), "find me a job", Tr())]

    return asyncio.run(drain())


def test_tool_input_is_the_arguments_not_an_empty_buffer():
    calls = [e for e in run(EVENTS) if e.get("kind") == "tool.call"]
    assert len(calls) == 1, f"expected one tool.call, got {len(calls)}"
    assert json.loads(calls[0]["input"]) == QUERY, calls[0]["input"]
    assert calls[0]["tool"] == "vector_search"


def test_input_is_json_because_the_panel_renders_it_as_json():
    """st.code(..., language="json") on a Python repr is unreadable."""
    call = next(e for e in run(EVENTS) if e.get("kind") == "tool.call")
    assert "'" not in call["input"], f"Python repr leaked into the panel: {call['input']}"
    json.loads(call["input"])  # raises if it is not JSON


def test_a_call_is_recorded_once_and_before_its_result():
    kinds = [e.get("kind") for e in run(EVENTS) if e.get("type") == "trace"]
    assert kinds.count("tool.call") == 1, kinds
    assert kinds.index("tool.call") < kinds.index("tool.result"), kinds


def test_the_result_still_comes_through():
    result = next(e for e in run(EVENTS) if e.get("kind") == "tool.result")
    assert result["resultChars"] == len("3 matching roles")
    assert result["status"] == "success"
    assert result["toolUseId"] == "tu-1"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print("\ntool trace: all good" if not failures else f"\ntool trace: {failures} failed")
    sys.exit(1 if failures else 0)
