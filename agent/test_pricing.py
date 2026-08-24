#!/usr/bin/env python3
"""Self-check for token cost accounting.

Money is the one number in the developer trace an attendee might repeat out
loud, so the arithmetic behind it gets a test.

    python agent/test_pricing.py
"""

import json
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))

# tracing imports mongo_mcp only to flush finished traces, which needs the MCP
# client and a live Atlas. Neither is required to check arithmetic.
sys.modules.setdefault("mongo_mcp", types.ModuleType("mongo_mcp"))

import tracing  # noqa: E402


def test_priced_per_million_tokens():
    # 1M input + 1M output of Haiku 4.5 at $1.00 / $5.00.
    cost = tracing.cost_usd(
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        {"inputTokens": 1_000_000, "outputTokens": 1_000_000},
    )
    assert abs(cost - 6.00) < 1e-9, cost


def test_model_id_matches_on_substring():
    """Bedrock ids carry a region prefix and a version suffix the table can't list."""
    bare = tracing.cost_usd("claude-haiku-4-5", {"inputTokens": 1_000_000})
    prefixed = tracing.cost_usd(
        "us.anthropic.claude-haiku-4-5-20251001-v1:0", {"inputTokens": 1_000_000}
    )
    assert bare == prefixed == 1.00, (bare, prefixed)


def test_unknown_model_costs_nothing_rather_than_guessing():
    assert tracing.cost_usd("some-model-we-have-never-priced", {"inputTokens": 999}) == 0.0


def test_cache_tokens_are_not_double_counted():
    """inputTokens is the uncached remainder; reads bill at 0.1x, writes at 1.25x."""
    cost = tracing.cost_usd("claude-haiku-4-5", {
        "inputTokens": 1_000_000,
        "cacheReadInputTokens": 1_000_000,
        "cacheWriteInputTokens": 1_000_000,
    })
    assert abs(cost - (1.00 + 0.10 + 1.25)) < 1e-9, cost


def test_total_is_derived_when_the_api_omits_it():
    events = []

    class FakeTrace(tracing.Trace):
        def event(self, kind, **data):  # noqa: D102 — capture instead of emitting
            events.append({"kind": kind, **data})
            return events[-1]

    tr = FakeTrace.__new__(FakeTrace)
    tr.usage("answer", "claude-haiku-4-5", {"inputTokens": 300, "outputTokens": 200})
    assert events[0]["totalTokens"] == 500, events[0]
    assert events[0]["costUsd"] > 0


def test_pricing_can_be_overridden_without_a_rebuild():
    """The table will go stale; MODEL_PRICING is the fix that needs no redeploy."""
    import importlib

    os.environ["MODEL_PRICING"] = json.dumps({"my-model": {"in": 2.0, "out": 4.0}})
    try:
        reloaded = importlib.reload(tracing)
        assert reloaded.cost_usd("my-model", {"inputTokens": 1_000_000}) == 2.0
        assert reloaded.cost_usd("claude-haiku-4-5", {"inputTokens": 1_000_000}) == 0.0
    finally:
        del os.environ["MODEL_PRICING"]
        importlib.reload(tracing)


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
    print("\npricing: all good" if not failures else f"\npricing: {failures} failed")
    sys.exit(1 if failures else 0)
