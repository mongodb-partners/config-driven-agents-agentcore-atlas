#!/usr/bin/env python3
"""Drive one turn through the orchestrator and report what memory did.

    python scripts/memtest/turn.py --user qa-x-a --session sess-... "prompt"

Same `invoke_agent_runtime` call the UI makes (ui/app.py:stream_turn), with the
identity under our control — which is what the isolation cases need. Prints a
compact JSON summary to stdout; the full event stream goes to --out.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

import boto3

REGION = "us-east-1"


def orchestrator_arn() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    tf = os.path.join(here, "..", "..", "terraform")
    out = subprocess.run(
        ["terraform", f"-chdir={tf}", "output", "-json", "agent_runtimes"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["orchestrator"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--user", required=True)
    ap.add_argument("--session", default=None, help="33+ chars; generated if omitted")
    ap.add_argument("--out", default=None, help="write the raw event stream here")
    args = ap.parse_args()

    session = args.session or f"sess-{uuid.uuid4().hex}"
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    t0 = time.time()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=orchestrator_arn(),
        runtimeSessionId=session,
        qualifier="DEFAULT",
        payload=json.dumps({
            "prompt": args.prompt, "sessionId": session, "userId": args.user,
        }).encode(),
    )

    events, answer = [], []
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
            ev = json.loads(payload)
        except json.JSONDecodeError:
            ev = {"type": "delta", "text": payload}
        events.append(ev)
        if ev.get("type") == "delta":
            answer.append(ev.get("text", ""))
    elapsed = round(time.time() - t0, 2)

    if args.out:
        with open(args.out, "w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def first(kind):
        for ev in events:
            if ev.get("type") == "trace" and ev.get("kind") == kind:
                return {k: v for k, v in ev.items() if k not in ("type", "ts")}
        return None

    done = next((e for e in events if e.get("type") == "done"), {})
    errors = [e for e in events if e.get("type") == "error"
              or (e.get("type") == "trace" and e.get("kind") == "turn.error")]

    summary = {
        "user": args.user,
        "session": session,
        "traceId": done.get("traceId"),
        "elapsedSec": elapsed,
        "route": (first("route") or {}).get("target") or (first("route") or {}).get("agentId"),
        "recall": first("memory.recall"),
        "write": first("memory.write"),
        "errors": errors,
        "answer": "".join(answer).strip(),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
