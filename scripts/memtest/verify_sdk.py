#!/usr/bin/env python3
"""Read memory back through the SDK, exactly as the agents authenticate.

Runs inside the deployed agent image, which already has the SDK and
platform_memory.py — so the token is minted the same way and the secret never
leaves your shell:

    export AGENT_ENGINE_CLIENT_ID=… AGENT_ENGINE_CLIENT_SECRET=… AGENTIC_MEMORY_PROJECT_ID=…
    docker run --rm -e AGENT_ENGINE_CLIENT_ID -e AGENT_ENGINE_CLIENT_SECRET -e AGENTIC_MEMORY_PROJECT_ID \
      -v "$PWD/scripts/memtest:/t:ro" \
      979559056307.dkr.ecr.us-east-1.amazonaws.com/auto-embed-byoc-riv-workshop/agent:latest \
      python /t/verify_sdk.py --user qa-mem-20260915-1-a --session sess-qa-mem-20260915-1-a-s1-000000000000 --needle Halcyon

Covers R3 (token budget), X2 (episodes), O2 (token TTL), and the SDK-side half
of R2 / I1. Output is JSON.
"""

import argparse
import json
import sys
import time

sys.path.insert(0, "/app")
import platform_memory  # noqa: E402

RECALL_SOURCES = platform_memory._RECALL_SOURCES
MAX_TOKENS = platform_memory._CONTEXT_MAX_TOKENS


def chunks(items, needle):
    out = []
    for c in items or []:
        content = getattr(c, "content", None) or (c.get("content") if isinstance(c, dict) else str(c))
        out.append({
            "source": str(getattr(c, "source", "")),
            "hit": needle.lower() in (content or "").lower(),
            "content": (content or "")[:160],
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--session", default=None)
    ap.add_argument("--needle", default="Halcyon")
    args = ap.parse_args()

    backend = platform_memory.AgentEngineBackend()
    mem = backend._client()
    # O2: the TTL the service actually granted. _token_expires_at is monotonic
    # and already has the skew subtracted.
    ttl = round(backend._token_expires_at - time.monotonic() + platform_memory._TOKEN_SKEW_SECONDS)

    ctx = platform_memory.MemoryRequestContext(user_id=args.user, session_id=args.session)
    s = mem.bind(ctx)
    report = {"user": args.user, "session": args.session, "token_ttl_seconds": ttl}

    # X2
    try:
        eps = s.list_episodes(user_id=args.user, session_id=args.session, limit=20)
        report["list_episodes"] = {"count": len(eps), "sample": [str(e)[:200] for e in eps[:2]]}
    except Exception as exc:  # noqa: BLE001
        report["list_episodes"] = {"error": f"{type(exc).__name__}: {exc}"}

    # R2 / I1, SDK side
    for name, fn in (
        ("search_semantic", lambda: s.search_semantic(args.needle, user_id=args.user, top_k=10)),
        ("search_episodes", lambda: s.search_episodes(args.needle, user_id=args.user, top_k=10)),
        ("search_all", lambda: s.search(args.needle, user_id=args.user, top_k=10)),
    ):
        try:
            report[name] = chunks(fn(), args.needle)
        except Exception as exc:  # noqa: BLE001
            report[name] = {"error": f"{type(exc).__name__}: {exc}"}

    # R3: the exact call the agent makes
    try:
        c = s.build_context(query="what do you know about me",
                            enabled_sources=RECALL_SOURCES, max_tokens=MAX_TOKENS)
        md = c.metadata
        report["build_context"] = {
            "token_count": md.token_count,
            "within_budget": md.token_count <= MAX_TOKENS,
            "memory_counts": md.memory_counts,
            "formatted_type": type(c.formatted_context).__name__,
            "formatted": platform_memory._text_from_formatted(c.formatted_context)[:400],
        }
    except Exception as exc:  # noqa: BLE001
        report["build_context"] = {"error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
